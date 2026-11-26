"""Durable review-gated DAG over direct image, native H3 and FFmpeg services."""
from __future__ import annotations

import asyncio
import copy

from sqlalchemy import select
from starlette.requests import Request

from app import models as m
from app.core.errors import AppError
from app.schemas import GenerationRequest
from app.schemas_cinematic import VisualInput
from app.schemas_product import AssemblyCreate, AssemblySubmit, TimelineRequest
from app.services.cinematic_quality import generation_inputs_hash, production_context, shot_preflight
from app.services.cinematic_visuals import build_visual
from app.services.screenplay import get


_CLOSED = {'completed', 'cancelled', 'paused', 'failed', 'quality_failed'}


def _snapshot_is_current(session, snapshot):
    service = session.get(m.AIService, snapshot.get('service_id'))
    return bool(service and service.enabled and service.revision == snapshot.get('service_revision'))


def create_run(session, settings, project_id, payload):
    if not payload.confirmed:
        raise AppError(409, 'production_confirmation_required', '先审核本次任务范围与资源需求，再明确确认编排')
    if settings.h3_demo_mode:
        raise AppError(409, 'real_production_required', '电影生产编排需要真实本地或云端服务')
    shots = [get(session, m.StoryboardShot, identifier) for identifier in dict.fromkeys(payload.shot_ids)]
    if any(shot.project_id != project_id for shot in shots):
        raise AppError(422, 'production_project_mismatch', '镜头不属于本项目')
    if len(shots) > 20:
        raise AppError(422, 'assembly_clip_limit', '现有正式合成最多20片段，请分批编排')
    from app.api.routes import script_for_shot
    scripts = {script.id if (script := script_for_shot(session, shot)) else None for shot in shots}
    if len(scripts) != 1 or None in scripts:
        raise AppError(422, 'production_script_mismatch', '一次编排只能使用同一剧本版本')
    script = get(session, m.ScriptRevision, next(iter(scripts)))
    active = session.scalar(select(m.ProductionRun).where(
        m.ProductionRun.project_id == project_id,
        m.ProductionRun.status.not_in(_CLOSED),
    ))
    if active:
        raise AppError(409, 'production_already_active', '当前项目已有待审核或执行中的编排', {'run_id': active.id})

    from app.services.ai_services import resolve_service
    from app.services.cinematic_rules import rules_bundle
    video_service, video_snapshot = resolve_service(
        session, 'video_generation', payload.video_service_id, project_id=project_id)
    visual_service, visual_snapshot = resolve_service(
        session, 'character_generation', payload.visual_service_id, project_id=project_id)
    if video_snapshot['provider'] == 'comfyui' and (
            payload.width % 32 or payload.height % 32 or max(payload.width, payload.height) > 1344):
        raise AppError(422, 'local_video_dimensions', '本地H3宽高需32的倍数且不超过1344')
    if video_snapshot['provider'] == 'video_api' and video_snapshot['settings'].get('video_contract') not in {
            'video-jobs-v1', 'wan26-i2v', 'lk888-h3-v2'}:
        raise AppError(409, 'video_contract_required', '请选择已实现的视频任务协议；聊天接口不能代替视频生成')

    options = payload.model_dump()
    options.update(video_service_id=video_service.id, visual_service_id=visual_service.id)
    if visual_snapshot['provider'] == 'openai_compatible':
        options.update(
            visual_width=int(visual_snapshot['settings'].get('width', 1024)),
            visual_height=int(visual_snapshot['settings'].get('height', 1024)),
        )
    options['assembly_resolution_mode'] = payload.assembly_resolution_mode or (
        'custom' if {'assembly_width', 'assembly_height'} & payload.model_fields_set else 'source')
    steps = [
        {'shot_id': shot.id, 'inputs_hash': generation_inputs_hash(session, shot),
         'stage': 'waiting_design_review', 'attempt': 0, 'video_job_id': None,
         'video_id': None, 'keyframe_id': None}
        for shot in sorted(shots, key=lambda item: item.order)
    ]
    run = m.ProductionRun(
        project_id=project_id,
        script_revision_id=script.id,
        status='waiting_design_review',
        snapshot_json={
            'versions': script.parsed_json.get('versions', {}),
            'rules_snapshot': script.parsed_json.get('rules_snapshot') or rules_bundle(),
            'options': options,
            'selected_service_snapshots': {'visual': visual_snapshot, 'video': video_snapshot},
            'input_scope': [shot.id for shot in shots],
        },
        steps_json=steps,
    )
    session.add(run)
    session.flush()
    return run


def current_keyframe(session, shot, settings):
    pre = shot_preflight(session, settings, shot, require_visual=False)
    visuals = list(session.scalars(select(m.ProductionVisual).where(
        m.ProductionVisual.shot_id == shot.id,
        m.ProductionVisual.kind == 'keyframe',
    ).order_by(m.ProductionVisual.created_at.desc())))
    return next((visual for visual in visuals
                 if visual.snapshot_json.get('inputs_hash') == pre['inputs_hash']
                 and visual.status in {'approved', 'needs_review', 'queued', 'submitting', 'submitted', 'running', 'unknown'}), None)


def _preflight(session, settings, shot, *, require_visual):
    report = shot_preflight(session, settings, shot, require_visual=require_visual)
    blockers = [item for item in report['findings'] if item['severity'] == 'blocker']
    if blockers:
        raise AppError(409, 'production_inputs_not_ready', '；'.join(item['message'] for item in blockers), {'findings': blockers})
    return report


def _scene_reference(session, settings, run, shot, options, scene_steps):
    detail, scene, _, production, _ = production_context(session, shot)
    if not scene:
        return None
    record = scene_steps.get(scene.id)
    if record:
        return session.get(m.ProductionVisual, record['visual_id'])
    candidates = list(session.scalars(select(m.ProductionVisual).where(
        m.ProductionVisual.scene_id == scene.id,
        m.ProductionVisual.kind == 'scene_reference',
    ).order_by(m.ProductionVisual.created_at.desc())))
    visual = next((item for item in candidates if item.status in {
        'approved', 'needs_review', 'queued', 'submitting', 'submitted', 'running'}
        and item.entity_id == production.get('scene_state_id')
        and item.snapshot_json.get('visual_inputs', {}).get('environment') == scene.environment_json), None)
    if visual is None:
        visual = build_visual(session, settings, run.project_id, VisualInput(
            kind='scene_reference', scene_id=scene.id,
            entity_id=production.get('scene_state_id'), service_id=options['visual_service_id'],
            width=options.get('visual_width', options['width']),
            height=options.get('visual_height', options['height']), seed=options['seed'],
        ))
    scene_steps[scene.id] = {'visual_id': visual.id}
    return visual


def _bind_approved_visuals(session, shot, keyframe, scene_visual):
    detail, _, _, production, _ = production_context(session, shot)
    required = [keyframe.id, *([scene_visual.id] if scene_visual else [])]
    if set(required).issubset(production.get('visual_ids', [])):
        return
    from app.services.screenplay import touch_shot
    touch_shot(session, shot.id)
    detail.source_json = {
        **detail.source_json,
        'production': {**production, 'visual_ids': list(dict.fromkeys(production.get('visual_ids', []) + required))},
    }


def _existing_job(session, run, step):
    scope = {'run_id': run.id, 'attempt': step['attempt']}
    return next((candidate for candidate in session.scalars(select(m.GenerationJob).where(
        m.GenerationJob.shot_id == step['shot_id']))
                 if candidate.parameters_json.get('production_scope') == scope), None)


async def advance_run(app, settings, session, run):
    if run.status in _CLOSED:
        return
    snapshots = run.snapshot_json.get('selected_service_snapshots', {})
    if not all(_snapshot_is_current(session, snapshot) for snapshot in snapshots.values()):
        run.status = 'paused'
        run.error_message = '服务配置已变化，原编排保留快照；请核对设置并新建编排'
        session.commit()
        return
    options = run.snapshot_json['options']
    steps = copy.deepcopy(run.steps_json)
    script = get(session, m.ScriptRevision, run.script_revision_id)
    request = Request({'type': 'http', 'app': app})

    # Validate only stable source inputs before emitting visual work. Keyframe
    # review is intentionally handled after a real frame exists.
    for step in steps:
        shot = get(session, m.StoryboardShot, step['shot_id'])
        if generation_inputs_hash(session, shot) != step['inputs_hash']:
            run.status = 'paused'
            run.error_message = '镜头内容已修改，旧编排保持原快照。请为受影响镜头新建编排'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return
        try:
            report = _preflight(session, settings, shot, require_visual=False)
        except AppError as error:
            run.status = 'waiting_review'
            step['stage'] = 'waiting_review'
            step['findings'] = error.details.get('findings', []) if error.details else []
            run.steps_json = copy.deepcopy(steps)
            run.error_message = error.message
            session.commit()
            return
        reviews = [item for item in report['findings']
                   if item['severity'] == 'review' and item['code'] != 'keyframe_review_required']
        if reviews:
            run.status = 'waiting_review'
            step['stage'] = 'waiting_review'
            step['findings'] = reviews
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return

    scene_steps = copy.deepcopy(run.result_json.get('scene_steps', {}))
    waiting_scenes = False
    for step in steps:
        shot = get(session, m.StoryboardShot, step['shot_id'])
        visual = _scene_reference(session, settings, run, shot, options, scene_steps)
        run.result_json = {**run.result_json, 'scene_steps': copy.deepcopy(scene_steps)}
        session.commit()
        if visual and visual.status in {'failed', 'rejected', 'unknown'}:
            run.status = 'quality_failed' if visual.status == 'rejected' else 'failed'
            run.error_message = visual.error_message or '场景参考未通过，请定向修复'
            session.commit()
            return
        waiting_scenes = waiting_scenes or bool(visual and visual.status != 'approved')
    if waiting_scenes:
        run.status = 'waiting_visual_review'
        session.commit()
        return

    waiting_keyframes = False
    for step in steps:
        shot = get(session, m.StoryboardShot, step['shot_id'])
        keyframe = session.get(m.ProductionVisual, step['keyframe_id']) if step.get('keyframe_id') else current_keyframe(session, shot, settings)
        if keyframe is None:
            keyframe = build_visual(session, settings, run.project_id, VisualInput(
                kind='keyframe', shot_id=shot.id, service_id=options['visual_service_id'],
                width=options.get('visual_width', options['width']),
                height=options.get('visual_height', options['height']),
                seed=options['seed'] + shot.order + step['attempt'],
                instructions=step.get('repair_instruction', ''),
            ))
        step['keyframe_id'] = keyframe.id
        if keyframe.status in {'failed', 'unknown', 'rejected'}:
            run.status = 'quality_failed' if keyframe.status == 'rejected' else 'failed'
            run.error_message = keyframe.error_message or '关键画面未通过，需要定向修复'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return
        if keyframe.status != 'approved':
            waiting_keyframes = True
            step['stage'] = 'waiting_visual_review'
        else:
            detail, scene, _, _, _ = production_context(session, shot)
            scene_visual = session.get(m.ProductionVisual, scene_steps[scene.id]['visual_id']) if scene and scene.id in scene_steps else None
            _bind_approved_visuals(session, shot, keyframe, scene_visual)
        run.steps_json = copy.deepcopy(steps)
        session.commit()
    if waiting_keyframes:
        run.status = 'waiting_visual_review'
        session.commit()
        return

    waiting_videos = False
    for step in steps:
        shot = get(session, m.StoryboardShot, step['shot_id'])
        report = _preflight(session, settings, shot, require_visual=True)
        if not report['ready'] or script.status != 'approved' or shot.status != 'approved':
            step['stage'] = 'waiting_review'
            run.status = 'waiting_review'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return
        job = session.get(m.GenerationJob, step['video_job_id']) if step.get('video_job_id') else None
        if job is None and step['stage'] == 'video_dispatching':
            job = _existing_job(session, run, step)
            if job:
                step['video_job_id'] = job.id
                run.steps_json = copy.deepcopy(steps)
                session.commit()
        if job is None:
            reused = None
            for video in session.scalars(select(m.VideoRevision).where(
                m.VideoRevision.shot_id == shot.id,
                m.VideoRevision.review_status == 'approved',
            ).order_by(m.VideoRevision.revision.desc())):
                previous = session.get(m.GenerationJob, video.generation_job_id)
                if previous and previous.parameters_json.get('generation_inputs_hash') == step['inputs_hash']:
                    reused = video
                    break
            if reused:
                step.update(video_id=reused.id, video_job_id=reused.generation_job_id, stage='approved_reused')
                run.steps_json = copy.deepcopy(steps)
                session.commit()
                continue
            from app.api.routes import generate_for_shot
            step['stage'] = 'video_dispatching'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            scope = {'run_id': run.id, 'attempt': step['attempt']}
            try:
                job, _ = await generate_for_shot(
                    request, session, shot,
                    GenerationRequest(
                        service_id=options['video_service_id'],
                        model_id=snapshots['video']['model_id'],
                        seed=options['seed'] + shot.order + step['attempt'],
                        parameters={
                            'width': options['width'], 'height': options['height'],
                            'requested_mode': 'real',
                        },
                    ),
                    instruction=step.get('repair_instruction'),
                    frozen_service=snapshots['video'],
                    production_scope=scope,
                )
            except AppError:
                recovered = _existing_job(session, run, step)
                if recovered:
                    step['video_job_id'] = recovered.id
                    run.steps_json = copy.deepcopy(steps)
                raise
            step.update(video_job_id=job.id, stage='running')
            run.steps_json = copy.deepcopy(steps)
            run.status = 'running'
            session.commit()
            return
        if job.status in {'failed', 'unknown', 'closed_unknown', 'cancelled'}:
            step['stage'] = 'failed'
            run.status = 'failed'
            run.error_message = job.error_message or job.status
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return
        video = session.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id == job.id))
        if not video:
            run.status = 'running'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return
        step['video_id'] = video.id
        if session.scalar(select(m.QualityCheck.id).where(
                m.QualityCheck.video_revision_id == video.id,
                m.QualityCheck.status == 'failed').limit(1)):
            run.status = 'quality_failed'
            step['stage'] = 'quality_failed'
            run.error_message = '实际视频技术检查未通过，先修复该镜；不会继续批量生成或合成'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            return
        if video.review_status != 'approved':
            if video.review_status == 'rejected':
                run.status = 'quality_failed'
                step['stage'] = run.status
            else:
                waiting_videos = True
                step['stage'] = 'waiting_video_review'
            run.steps_json = copy.deepcopy(steps)
            session.commit()
            continue
        step['stage'] = 'approved'
        run.steps_json = copy.deepcopy(steps)
        session.commit()
    if waiting_videos:
        run.status = 'waiting_video_review'
        session.commit()
        return

    assembly_id = run.result_json.get('assembly_id')
    if not assembly_id:
        from app.api.assemblies import create
        existing = session.scalar(select(m.Assembly).where(
            m.Assembly.project_id == run.project_id,
            m.Assembly.name == '电影编排 ' + run.id,
        ))
        if existing:
            assembly_id = existing.id
        else:
            output = {
                'width': options['assembly_width'], 'height': options['assembly_height'], 'fps': 24,
                'resolution_mode': options.get('assembly_resolution_mode') or 'source',
                'allow_downscale': options.get('assembly_allow_downscale', False),
                'codec': 'libx264', 'audio_policy': 'preserve',
            }
            timeline = TimelineRequest.model_validate({
                'clips': [{'video_id': step['video_id']} for step in steps],
                'transitions': [{'type': 'cut', 'duration': 0, 'audio': 'cut'} for _ in steps[1:]],
                'output': output,
            })
            assembly = create(AssemblyCreate(project_id=run.project_id, name='电影编排 ' + run.id, timeline=timeline),
                              request, session)
            assembly_id = assembly['id']
        run.result_json = {**run.result_json, 'assembly_id': assembly_id}
        session.commit()
    assembly = get(session, m.Assembly, assembly_id)
    render = session.scalar(select(m.AssemblyRender).where(
        m.AssemblyRender.assembly_id == assembly_id,
        m.AssemblyRender.kind == 'render',
    ).order_by(m.AssemblyRender.version.desc()))
    if not render:
        from app.api.assemblies import submit
        submitted = submit(assembly_id, AssemblySubmit(revision=assembly.revision), request, session, 'render')
        render = get(session, m.AssemblyRender, submitted['id'])
    run.result_json = {**run.result_json, 'render_id': render.id, 'relative_path': render.relative_path}
    run.status = 'completed' if render.status == 'generated' else 'failed' if render.status in {'failed', 'cancelled'} else 'rendering'
    run.error_message = render.error_message
    session.commit()


async def production_worker(app, settings, session_factory):
    while True:
        try:
            with session_factory() as session:
                for run in session.scalars(select(m.ProductionRun).where(
                        m.ProductionRun.status.not_in(_CLOSED)).order_by(m.ProductionRun.created_at)):
                    try:
                        await advance_run(app, settings, session, run)
                    except AppError as error:
                        run.status = 'waiting_service_verification' if error.code == 'cloud_visual_single_validation_required' else 'failed'
                        run.error_message = error.message
                        session.commit()
                    except Exception as error:
                        session.rollback()
                        run = session.get(m.ProductionRun, run.id)
                        run.status = 'failed'
                        run.error_message = type(error).__name__ + '：编排失败，请检查日志后定向重试'
                        session.commit()
                        import logging
                        logging.getLogger(__name__).exception('Production orchestration failed')
        except asyncio.CancelledError:
            raise
        await asyncio.sleep(3)
