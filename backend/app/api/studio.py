"""Unified workstation API; no UI progress is inferred from elapsed time."""
import copy
import asyncio
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app import models as m
from app.models.studio import StudioRun
from app.core.db import get_db
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_studio import (PipelineStart, StudioOverview, StudioProject,
    ProjectReferenceVideoConfig, ShotFramingOverview,ShotFramingUpdate,ShotFramingResult)
from app.schemas_product_library import ShotProductReferencesEdit
from app.services.studio_pipeline import create_pipeline, latest_run, overview, project_list, _capture_services
from app.services.screenplay_production_status import sync_production_status

router = APIRouter(prefix='/api/studio')

@router.get('/projects/{project_id}/shot-framing',response_model=ShotFramingOverview)
def shot_framing(project_id:str,session:Session=Depends(get_db)):
    from app.services.shot_framing import overview as framing_overview
    return framing_overview(session,project_id)

@router.put('/projects/{project_id}/shot-framing',response_model=ShotFramingResult)
async def save_shot_framing(project_id:str,payload:ShotFramingUpdate,request:Request,session:Session=Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    from app.services.shot_framing import save as save_framing
    async with project_guard(request.app,project_id):
        try:
            session.expire_all()
            result=save_framing(session,project_id,payload)
            session.commit();return result
        except Exception:
            session.rollback();raise

@router.get('/projects/{project_id}/production-preferences')
def production_preferences(project_id:str,session:Session=Depends(get_db)):
    from app.services.studio_audio_policy import get_preferences
    return get_preferences(session,project_id)

from app.schemas_product_library import ProductionPreferenceEdit
@router.put('/projects/{project_id}/production-preferences')
def edit_production_preferences(project_id:str,payload:ProductionPreferenceEdit,session:Session=Depends(get_db)):
    from app.services.studio_audio_policy import save_preferences
    return save_preferences(session,project_id,payload)


@router.get('/projects/{project_id}/reference-videos')
def project_reference_videos(project_id: str, session: Session = Depends(get_db)):
    """Return only the project's explicit defaults and per-shot overrides."""
    from app.services.project_reference_videos import project_config
    return project_config(session, project_id)


@router.get('/projects/{project_id}/reference-videos/library')
def project_reference_video_library(project_id: str, session: Session = Depends(get_db)):
    from app.services.project_reference_videos import explicit_project_library
    return explicit_project_library(session, project_id)


@router.get('/projects/{project_id}/reference-videos/available')
def available_project_reference_videos(project_id: str, session: Session = Depends(get_db)):
    """Browse the reusable video library without implicitly granting access."""
    from app.services.project_reference_videos import available_library
    return available_library(session, project_id)


@router.post('/projects/{project_id}/reference-videos/{video_id}/attach', status_code=201)
async def attach_project_reference_video(project_id: str, video_id: str, request: Request,
                                         session: Session = Depends(get_db)):
    """Explicitly grant one existing library video to this project."""
    from app.services.project_reference_videos import attach_existing_video
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id):
        try:
            session.expire_all()
            item = attach_existing_video(session, project_id, video_id)
            session.commit()
            return {'item': item}
        except Exception:
            session.rollback()
            raise


@router.put('/projects/{project_id}/reference-videos')
async def save_project_reference_videos(project_id: str, payload: ProjectReferenceVideoConfig,
                                        request: Request, session: Session = Depends(get_db)):
    from app.services.project_reference_videos import save_project_config
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id):
        try:
            session.expire_all()
            result = save_project_config(session, project_id, payload)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise


@router.post('/projects/{project_id}/reference-videos/upload', status_code=201)
async def upload_project_reference_video(project_id: str, request: Request, file: UploadFile = File(...),
                                         session: Session = Depends(get_db)):
    """Import a verified original and explicitly attach it to this project."""
    from app.services import reference_video_analysis as library
    from app.services.project_reference_videos import link_uploaded_video
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id):
        item = None
        duplicate = True
        try:
            if not session.get(m.Project, project_id):
                raise AppError(404, 'project_missing', '项目不存在')
            data = await file.read(library.MAX_BYTES + 1)
            item, duplicate = await asyncio.to_thread(
                library.import_video, session, request.app.state.settings, file.filename or '', data,
                name='', category='', source_note='project-native-ref2va-upload', usage_scope='native_ref2va', commit=False,
            )
            link_uploaded_video(session, project_id, item.id)
            session.commit()
            return {'item': {**library.video_dto(session, item), 'linked_to_project': True},
                    'duplicate': duplicate}
        except Exception:
            session.rollback()
            if item is not None and not duplicate:
                # The file belongs solely to this failed, uncommitted upload;
                # no prior source or shared library entry is ever removed.
                candidate = safe_media_path(request.app.state.settings.media_root, item.relative_path)
                if candidate.is_file():
                    candidate.unlink()
            raise
        finally:
            await file.close()


@router.get('/projects', response_model=list[StudioProject])
def projects(session: Session = Depends(get_db)):
    return project_list(session)


@router.get('/projects/{project_id}', response_model=StudioOverview)
def project(project_id: str, session: Session = Depends(get_db)):
    return overview(session, project_id)


@router.post('/projects/{project_id}/start', response_model=StudioOverview, status_code=202)
async def start(project_id: str, payload: PipelineStart, request: Request, session: Session = Depends(get_db)):
    from app.services.studio_voice import assert_voice_preparation
    from app.services.studio_pipeline import RUN_TERMINAL
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app,project_id):
        session.expire_all()
        existing = latest_run(session, project_id)
        if not existing or existing.status in RUN_TERMINAL | {'paused', 'exceptions'}:
            if payload.dialogue_audio_policy is not None:
                from app.services.studio_audio_policy import get_preferences,save_preferences
                save_preferences(session,project_id,ProductionPreferenceEdit(expected_revision=get_preferences(session,project_id)['revision'],dialogue_audio_policy=payload.dialogue_audio_policy),commit=False)
            assert_voice_preparation(session, request.app.state.settings, project_id, payload.allow_cloud_costs, payload.allow_paid_fallback)
        create_pipeline(session, request.app.state.settings, project_id, payload)
        session.commit()
        return overview(session, project_id)


def _run(session, project_id):
    run = latest_run(session, project_id)
    if not run: raise AppError(409, 'studio_run_missing', '本项目尚无自动制作任务')
    return run


class ProductReferenceRetry(ShotProductReferencesEdit):
    expected_run_id: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=128)


class AssemblyRetry(BaseModel):
    expected_run_id: str = Field(min_length=1, max_length=64)
    refresh: bool = False


@router.post('/projects/{project_id}/retry-assembly', response_model=StudioOverview, status_code=202)
async def retry_assembly(project_id: str, payload: AssemblyRetry, request: Request, session: Session = Depends(get_db)):
    from app.services.studio_pipeline import _queue_automatic_render
    from app.services.studio_project_guard import project_guard
    settings = request.app.state.settings
    async with project_guard(request.app, project_id):
        try:
            session.expire_all()
            run = _run(session, project_id)
            if run.id != payload.expected_run_id:
                raise AppError(409, 'automatic_final_run_changed', '当前制作已切换，请刷新后重试')
            stage = run.result_json.get('automatic_final', {}).get('status')
            if payload.refresh:
                if run.status not in {'completed', 'completed_with_errors'} or stage not in {'generated', 'blocked', 'failed', 'cancelled'}:
                    raise AppError(409, 'automatic_final_refresh_unavailable', '当前成片没有可刷新重合成的完成记录')
            elif run.status != 'completed_with_errors' or stage not in {'blocked', 'failed', 'cancelled'}:
                raise AppError(409, 'automatic_final_retry_unavailable', '当前成片没有可重试的失败记录')
            _queue_automatic_render(session, settings, run, run.nodes_json, retry=True)
            run.error_message = None
            sync_production_status(session, run)
            session.commit()
        except Exception:
            session.rollback()
            raise
        return overview(session, project_id)
    node_id: str = Field(min_length=1, max_length=128)


def _requeue_failed_node(entry):
    # Keep the definitive failure and its child for review. Output-only
    # rechecks continue using the original submitted child.
    entry['history'] = [*(entry.get('history') or []), {
        'child_id': entry.get('child_id'), 'code': entry.get('code'),
        'error': entry.get('error'), 'actor': 'local-user',
    }]
    retained_child = entry.get('child_id') if entry.get('code') == 'output_quality_failed' else None
    entry.pop('native_dispatch_wait', None)
    entry.pop('retry_after', None)
    entry.update(status='pending', child_id=retained_child, error=None, code=None)


@router.post('/projects/{project_id}/retry-product-references', response_model=StudioOverview)
async def retry_product_references(project_id: str, payload: ProductReferenceRetry, request: Request,
                                   session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id):
        session.expire_all()
        try:
            return _retry_product_references(session, request.app.state.settings, project_id, payload)
        except Exception:
            session.rollback()
            raise


def _retry_product_references(session, settings, project_id, payload):
    from app.services.product_library import is_product_reference_error, reference_selection, save_reference_selection

    run = _run(session, project_id)
    if run.id != payload.expected_run_id:
        raise AppError(409, 'studio_retry_scope', '当前制作批次已变化，请刷新后重试')
    if run.status not in {'paused', 'running', 'exceptions', 'completed_with_errors'}:
        raise AppError(409, 'studio_product_retry_unavailable', '当前制作已变化或无法重试，请刷新并核对本次制作')
    nodes = copy.deepcopy(run.nodes_json)
    node = next((entry for entry in nodes if entry['id'] == payload.node_id), None)
    if not node or node['kind'] != 'video' or node['status'] not in {'exception', 'failed'}:
        raise AppError(409, 'studio_product_retry_unavailable', '只能为当前批次中已确认失败的分镜视频重选商品图')
    shot_id = node['target_id']
    child = session.get(m.GenerationJob, node['child_id']) if node.get('child_id') else None
    if (not child or child.status != 'failed' or child.provider != 'comfyui_h3'
            or child.project_id != project_id or child.shot_id != shot_id
            or not is_product_reference_error(child.error_code, child.error_message)):
        raise AppError(409, 'studio_product_retry_unavailable', '原任务没有已确认的商品参考身份异常，未重复提交')
    if any(entry['status'] in {'dispatching', 'running', 'retrying'} for entry in nodes if entry is not node):
        raise AppError(409, 'product_binding_busy', '制作批次仍有节点正在处理，请等待后重选商品图')

    # A paused batch must not start unrelated pending work as a side effect of
    # correcting one shot. Block only nodes that could proceed immediately.
    if run.status == 'paused':
        completed = {entry['id'] for entry in nodes if entry['status'] == 'succeeded'}
        if any(entry.get('shot_id', entry['target_id']) != shot_id
               and entry['status'] in {'pending', 'waiting_dependencies'}
               and set(entry.get('dependencies', [])) <= completed for entry in nodes):
            raise AppError(409, 'product_binding_busy', '本批次暂停时仍有其他待执行节点，请先处理这些节点')

    shot = session.get(m.StoryboardShot, shot_id)
    if not shot or shot.project_id != project_id or shot.status == 'deleted':
        raise AppError(409, 'studio_product_retry_scope', '原分镜已变化或不存在；请创建新的制作批次')
    # Requeueing always uses the current run's direct native H3 snapshot; the
    # retained failed child is evidence only and can never select a legacy path.

    saved = {item['product_id']: item for item in reference_selection(session, project_id, shot_id)['products']}
    if len(payload.products) != len(saved) or {item.product_id for item in payload.products} != set(saved):
        raise AppError(422, 'product_reference_scope', '请提交本分镜已绑定的全部产品')
    if any(item.keyframe_image_ids != saved[item.product_id]['keyframe_image_ids'] for item in payload.products):
        raise AppError(422, 'studio_product_retry_video_only', '本次只需更改分镜视频的商品图；关键画面选图请单独保存')
    if not any(set(item.video_image_ids) != set(saved[item.product_id]['video_image_ids']) for item in payload.products):
        raise AppError(422, 'studio_product_retry_selection_unchanged', '请至少更改本分镜的一张商品视频参考图后再重试')

    save_reference_selection(session, project_id, shot_id, ShotProductReferencesEdit(products=payload.products),
                             settings=settings, commit=False, allow_stalled_run=True)
    session.flush()
    _requeue_failed_node(node)
    run.nodes_json = nodes
    run.status = 'queued'
    run.error_message = None
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='product_reference_video_retry_requested',
        details_json={'node_id': node['id'], 'shot_id': shot_id, 'previous_child_id': child.id,
            'route': 'native_h3_direct', 'changed_product_reference_ids': [item.product_id for item in payload.products]}))
    sync_production_status(session, run)
    session.commit()
    return overview(session, project_id)


@router.post('/projects/{project_id}/pause', response_model=StudioOverview)
def pause(project_id: str, session: Session = Depends(get_db)):
    run = _run(session, project_id); nodes = copy.deepcopy(run.nodes_json)
    if run.status == 'rendering':
        raise AppError(409, 'studio_final_already_rendering', '成片已交给合成队列，暂停后续模型任务不影响正在合成的成片')
    run.status = 'paused'
    for n in nodes:
        if n['kind'] == 'character' and n.get('child_id'):
            batch = session.get(m.CharacterBatch, n['child_id'])
            if batch: batch.paused = True
        if n['kind'] in {'scene', 'keyframe'} and n.get('child_id'):
            visual = session.get(m.ProductionVisual, n['child_id'])
            if visual and visual.status == 'queued': visual.status = 'studio_paused'
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='paused_pending_dispatch', details_json={'accepted_provider_tasks_continue': True}))
    sync_production_status(session, run)
    session.commit(); return overview(session, project_id)


@router.post('/projects/{project_id}/resume', response_model=StudioOverview)
async def resume(project_id: str, request:Request, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app,project_id):
        # A different project's async dispatch can briefly own SQLite's writer
        # lock. A synchronous commit here blocks the event loop that must finish
        # that dispatch and release its lock. Keep the same-project guard while
        # the complete synchronous transaction waits in a worker thread.
        return await run_in_threadpool(_resume_transaction,project_id,session)


def _resume_transaction(project_id: str, session: Session):
    from app.services.shot_framing import requires_new_run
    session.expire_all()
    run = _run(session, project_id)
    if run.status != 'paused': raise AppError(409, 'studio_not_paused', '只有已暂停的任务可以继续')
    if requires_new_run(session,project_id,run):
        raise AppError(409,'studio_framing_changed','镜头实际画幅已更改，请点击「开始／增量续作」创建新制作；原冻结编排和视频保留。',{'run_id':run.id})
    if run.snapshot_json.get('product_references_changed'):
        raise AppError(409,'studio_product_inputs_changed','暂停期间产品参考已变化，请点击「开始／增量续作」重新核对输入；旧视频保留，未继续旧输入')
    other = session.scalar(select(StudioRun).where(StudioRun.project_id == project_id, StudioRun.id != run.id, StudioRun.status.in_(['queued', 'running'])))
    if other: raise AppError(409, 'studio_already_active', '项目已有另一条执行中的任务')
    for n in run.nodes_json:
        if n['kind'] == 'character' and n.get('child_id'):
            batch = session.get(m.CharacterBatch, n['child_id'])
            if batch: batch.paused = False
        if n['kind'] in {'scene', 'keyframe'} and n.get('child_id'):
            visual = session.get(m.ProductionVisual, n['child_id'])
            if visual and visual.status == 'studio_paused': visual.status = 'queued'
    run.status = 'rendering' if run.result_json.get('render_id') and run.result_json.get('automatic_final') else 'queued'
    sync_production_status(session, run)
    session.commit(); return overview(session, project_id)


@router.post('/projects/{project_id}/retry', response_model=StudioOverview, name='retry')
async def retry_project(project_id: str, request: Request, payload: dict | None = None, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id) as nested:
        if not nested and not session.dirty and not session.new:
            session.expire_all()
        return retry(project_id, payload, session, settings=request.app.state.settings)


def retry(project_id: str, payload: dict | None = None, session: Session = Depends(get_db), *, settings=None):
    run = _run(session, project_id); nodes = copy.deepcopy(run.nodes_json)
    if run.snapshot_json.get('product_references_changed'):
        raise AppError(409,'studio_product_inputs_changed','产品参考已变化，请点击「开始／增量续作」创建使用新输入的制作')
    options = payload or {}
    if options.get('expected_run_id', run.id) != run.id:
        raise AppError(409, 'studio_retry_scope', '当前制作批次已变化，请刷新后重试')
    target = options.get('node_id')
    targets = options.get('node_ids')
    if target is not None and (not isinstance(target, str) or not target):
        raise AppError(422, 'studio_retry_scope', '请选择本轮的一个失败节点')
    if targets is not None and (target or not isinstance(targets, list) or not targets or len(targets) > len(nodes)
                                or not all(isinstance(value, str) for value in targets) or len(set(targets)) != len(targets)):
        raise AppError(422, 'studio_retry_scope', '请提供一组互不重复的本轮失败节点')
    targets = set(targets) if targets is not None else {target} if isinstance(target, str) and target else None
    selected = [n for n in nodes if (targets is None or n['id'] in targets) and n['status'] in {'exception', 'failed', 'unknown'}]
    if targets is not None and {n['id'] for n in selected} != targets:
        raise AppError(409, 'studio_retry_scope', '指定节点中有节点已经恢复或不在本轮失败列表')
    unresolved_unknown = []
    for n in selected:
        if n['status'] == 'unknown':
            job_id = n.get('child_id') or n.get('job_id') or n.get('external_id') or n.get('ref_id')
            from app.models import GenerationJob as _GJ
            job_row = session.get(_GJ, job_id) if job_id else None
            if not (job_row and job_row.status in {'closed_unknown', 'failed'}):
                unresolved_unknown.append(n['id'])
    if unresolved_unknown:
        raise AppError(409, 'studio_unknown_not_retryable', '含结果未知的请求；请先核对原服务任务，系统未重复提交以避免重复费用')
    # A retry must keep its frozen service and native reference inputs.  For
    # historical runs that have no frozen route, refresh only local metadata
    # and refuse a changed cloud scope rather than silently changing cost.
    snapshots, errors = _capture_services(session, project_id)
    changed_cloud = any(v['is_cloud'] for k, v in snapshots.items()
                        if run.snapshot_json.get('services', {}).get(k, {}).get('service_id') != v['service_id']
                        or run.snapshot_json.get('services', {}).get(k, {}).get('service_revision') != v['service_revision'])
    if changed_cloud:
        raise AppError(409, 'studio_cloud_scope_changed', '云端服务或密钥设置已改变，请通过重新启动确认新服务的本轮费用范围')
    run.snapshot_json = {**run.snapshot_json, 'services': snapshots, 'service_errors': errors}
    from app.services.studio_quality_apply import refresh_upscale_routes_for_retry
    quality_routes = refresh_upscale_routes_for_retry(session, settings, run, selected) if settings else {}
    for n in selected:
        _requeue_failed_node(n)
    run.nodes_json = nodes; run.status = 'queued'; run.error_message = None
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='failed_nodes_retry_requested',
                             details_json={'node_ids': [n['id'] for n in selected], 'native_reference_snapshots_preserved': True,
                                           'refreshed_upscale_routes': quality_routes}))
    sync_production_status(session, run)
    session.commit(); return overview(session, project_id)


@router.post('/projects/{project_id}/repair')
async def repair(project_id: str, request: Request, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    async with project_guard(request.app, project_id) as nested:
        if not nested and not session.dirty and not session.new:
            session.expire_all()
        return await _repair(project_id, request, session)


@router.post('/projects/{project_id}/quality/apply')
async def apply_quality(project_id: str, request: Request, payload: dict | None = None, session: Session = Depends(get_db)):
    from app.services.studio_project_guard import project_guard
    from app.services.studio_quality_apply import apply_saved_quality
    expected=(payload or {}).get('expected_profile_revision')
    if expected is not None and (type(expected) is not int or expected<0):
        raise AppError(422,'quality_profile_revision','画质修订号须为非负整数')
    async with project_guard(request.app,project_id):
        session.expire_all()
        result=apply_saved_quality(session,request.app.state.settings,project_id,expected_profile_revision=expected)
        session.commit();return result


async def _repair(project_id: str, request: Request, session: Session):
    """Diagnose a stuck run and resume only tasks with a known-safe next step."""
    from app.services.gpu_gate import owned_model_status
    from app.services.studio_pipeline import RUN_TERMINAL, advance_pipeline
    from app.services.studio_model_recovery import recover_model_connections, interrupted_native_nodes

    original_run_id = _run(session, project_id).id
    model_connections = await recover_model_connections(request.app, session)
    gpu = await owned_model_status()
    # Health checks yield while the worker or another request may persist a
    # child, pause, or input change. Never overwrite that newer durable state.
    session.expire_all()
    run = _run(session, project_id)
    recovery = {}
    if (run.id == original_run_id and not run.snapshot_json.get('product_references_changed')
            and run.status not in (RUN_TERMINAL - {'completed_with_errors'}) | {'paused'} and gpu['state'] == 'idle'):
        recovery = await interrupted_native_nodes(session, run, model_connections)
        session.expire_all()
        run = _run(session, project_id)
    def report(actions, blockers):
        return {'project_id': project_id, 'run_id': run.id, 'run_status': run.status,
                'gpu_gate': gpu, 'actions': actions, 'blockers': blockers, 'model_connections': model_connections}

    if run.id != original_run_id or run.snapshot_json.get('product_references_changed'):
        message = '本轮制作或参考输入已变化。请点击「开始／增量续作」核对当前输入。'
        raise AppError(409, 'studio_product_inputs_changed', message, {'repair_report': report([], [])})
    blockers = []
    actions = []
    queued_visual = None
    queued_node = None
    recoverable_characters = []
    nodes = copy.deepcopy(run.nodes_json or [])
    if recovery and run.status not in (RUN_TERMINAL - {'completed_with_errors'}) | {'paused'}:
        requeued = _resume_native_after_connection_repair(session, request.app.state.settings, run, nodes, recovery)
        if requeued:
            run.nodes_json = nodes
            run.status = 'queued'
            run.error_message = None
            sync_production_status(session, run)
            session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
                                    action='model_connection_nodes_requeued', details_json={'node_ids': requeued}))
            session.commit()
            actions.append({'code': 'model_connection_nodes_requeued', 'status': 'applied', 'node_ids': requeued,
                            'message': f'已把 {len(requeued)} 个确认中断的本地分镜按原设置重新排队。'})
    from app.services.reference_budget_repair import inspect_reference_budget_repairs
    recoverable_keyframes,keyframe_diagnostics,keyframe_blockers=inspect_reference_budget_repairs(
        session,request.app.state.settings,run,nodes)
    keyframe_candidates={item['id'] for item in recoverable_keyframes}
    keyframe_reported={item['node_id'] for item in keyframe_blockers}
    blockers.extend(keyframe_blockers)

    def character_capability_ready(item):
        """Match the current image adapter to the run's frozen route/snapshot.

        build_batch_plan accepts detected native ComfyUI node/model capability
        records. It does not accept arbitrary reachability as proof, so require
        one of those concrete capability records and the same model/service.
        """
        route = item.get('route') or {}
        selected = (route.get('selected') or {}).get('service_snapshot') or {}
        captured = selected or run.snapshot_json.get('services', {}).get('image') or {}
        service_id = captured.get('service_id')
        service = session.get(m.AIService, service_id) if service_id else None
        if (not service or not service.enabled or service.module != 'character_generation' or
                service.revision != captured.get('service_revision')):
            return False
        # This failure is raised by the native image adapter's model/workflow
        # preflight. Cloud adapters have a separate real-probe contract.
        if service.provider != 'comfyui':
            return False
        model_id = captured.get('model_id')
        if service.provider == 'comfyui':
            model_id = ((captured.get('settings') or {}).get('candidate_model') or
                        service.settings_json.get('candidate_model') or model_id or service.default_model)
        from app.models.character_reference import CharacterReferenceAsset
        has_project_references = session.scalar(select(CharacterReferenceAsset.id).where(
            CharacterReferenceAsset.project_id == project_id,
            CharacterReferenceAsset.character_id == item.get('target_id'),
            CharacterReferenceAsset.state == 'saved').limit(1)) is not None
        capability_name = 'reference_image' if has_project_references else 'text_to_image'
        capability = (service.capabilities_json or {}).get(capability_name) or {}
        if capability.get('state') not in {'detected', 'verified'}:
            return False
        capability_model = capability.get('model_id')
        if capability_model and capability_model != model_id:
            return False
        if has_project_references and model_id != 'flux-2-klein-4b-fp8.safetensors':
            return False
        return bool(model_id)

    for item in nodes:
        if item.get('status') in {'exception', 'failed', 'unknown'}:
            code = item.get('code') or 'task_failed'
            if item.get('id') in keyframe_candidates|keyframe_reported:
                continue
            if (item.get('kind') == 'character' and code == 'capability_unverified' and
                    item.get('status') in {'exception', 'failed'} and not item.get('child_id') and
                    character_capability_ready(item)):
                recoverable_characters.append(item)
                continue
            messages = {
                'capability_unverified': '图片模型或参考图工作流尚无匹配的真实输出验证；不会伪造验证结果。',
                'unknown': '服务请求结果未知；必须核对原服务任务，不能自动重提。',
            }
            blockers.append({'node_id': item.get('id'), 'kind': item.get('kind'),
                             'status': item.get('status'), 'code': code,
                             'message': messages.get(code, '存在明确失败节点；保留原记录，未自动重复提交。')})
        if item.get('kind') not in {'scene', 'keyframe'} or not item.get('child_id'):
            continue
        visual = session.get(m.ProductionVisual, item['child_id'])
        if visual and visual.status == 'queued' and queued_visual is None:
            queued_visual, queued_node = visual, item

    def dispatch_control(current):
        unchanged = not current.snapshot_json.get('product_references_changed')
        return (unchanged and current.status not in RUN_TERMINAL | {'paused', 'cancelled'},
                unchanged and current.status == 'completed_with_errors')

    run_can_dispatch, can_reopen = dispatch_control(run)
    if recoverable_keyframes:
        if not (run_can_dispatch or can_reopen) or gpu['state']!='idle':
            state='skipped' if not (run_can_dispatch or can_reopen) else 'waiting' if gpu['state']=='busy' else 'blocked'
            message='任务已暂停或结束，未越过运行控制。' if state=='skipped' else '参考图和原生链预检已通过；GPU尚未确认空闲，原节点保留，未提交生成。'
            actions.append({'code':'keyframe_reference_repair_waiting','status':state,'message':message,
                'node_ids':[item['id'] for item in recoverable_keyframes],'references':keyframe_diagnostics})
            blockers.extend({'node_id':item['id'],'kind':'keyframe','status':item['status'],'code':item['code'],'message':message} for item in recoverable_keyframes)
        else:
            for item in recoverable_keyframes:
                item['history']=[*(item.get('history') or []),{key:copy.deepcopy(item.get(key)) for key in ('status','code','error','child_id')}|
                    {'action':'one_click_reference_budget_repair'}]
                item.update(status='pending',error=None,code=None)
            run.nodes_json=nodes;run.status='running';run.error_message=None
            session.add(m.AuditEvent(object_type='studio_run',object_id=run.id,action='one_click_reference_budget_repair',
                details_json={'nodes':keyframe_diagnostics,'no_reference_removed':True,'human_visual_approval':False}))
            session.commit()
            worker_error=None
            try:
                await advance_pipeline(request.app,request.app.state.settings,session,run)
            except Exception as exc:worker_error=type(exc).__name__
            session.expire_all();run=_run(session,project_id)
            run_can_dispatch, can_reopen = dispatch_control(run)
            actions.append({'code':'keyframe_reference_nodes_requeued','status':'applied',
                'message':'已恢复参考预算误拦截的关键画面；全部人物、场景和产品原图保留。后台按原配置执行，实际输出解码后才记录多图验证。',
                'node_ids':[item['id'] for item in recoverable_keyframes],'references':keyframe_diagnostics,
                **({'worker_error':worker_error} if worker_error else {})})
            # The advance may have updated other nodes; do not overwrite them
            # with the diagnosis snapshot when repairing characters below.
            nodes=copy.deepcopy(run.nodes_json or [])
            recoverable_characters=[next((current for current in nodes if current['id']==old['id']),old) for old in recoverable_characters]
    if recoverable_characters and gpu['state'] != 'idle':
        actions.append({'code': 'character_repair_gpu_blocked',
                        'status': 'waiting' if gpu['state'] == 'busy' else 'blocked',
                        'message': '人物节点已有可用的本机模型/工作流能力记录；GPU 未确认空闲，保留原节点状态，稍后再次修复。',
                        'node_ids': [item['id'] for item in recoverable_characters]})
        for item in recoverable_characters:
            blockers.append({'node_id': item.get('id'), 'kind': item.get('kind'),
                             'status': item.get('status'), 'code': item.get('code'),
                             'message': 'GPU 尚未确认空闲，人物失败节点未重排。'})
        recoverable_characters = []
    if recoverable_characters and (run_can_dispatch or can_reopen):
        for item in recoverable_characters:
            item['history'] = [*(item.get('history') or []), {
                'status': item.get('status'), 'code': item.get('code'),
                'error': item.get('error'), 'child_id': item.get('child_id'),
                'action': 'one_click_repair_requeued'}]
            item.update(status='pending', error=None, code=None)
        run.nodes_json = nodes
        run.status = 'running'
        run.error_message = None
        session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
            action='one_click_repair_requeued_characters', details_json={
                'node_ids': [item['id'] for item in recoverable_characters],
                'gpu_gate': gpu['state'], 'gpu_reason': gpu['reason']}))
        session.commit()
        worker_error = None
        try:
            await advance_pipeline(request.app, request.app.state.settings, session, run)
        except Exception as exc:
            worker_error = type(exc).__name__
        session.expire_all()
        run = _run(session, project_id)
        run_can_dispatch, can_reopen = dispatch_control(run)
        if worker_error:
            actions.append({'code': 'character_repair_worker_error', 'status': 'error',
                            'message': '人物节点已重新排队；本轮 DAG 推进失败（'+worker_error+'），后台工作器将继续处理。',
                            'node_ids': [item['id'] for item in recoverable_characters]})
        else:
            actions.append({'code': 'character_nodes_requeued', 'status': 'applied',
                            'message': '已恢复具备当前服务能力的确定失败人物节点；旧错误保留在历史中，原 unknown 请求不会重提。',
                            'node_ids': [item['id'] for item in recoverable_characters]})
    elif recoverable_characters:
        for item in recoverable_characters:
            blockers.append({'node_id': item.get('id'), 'kind': item.get('kind'),
                             'status': item.get('status'), 'code': item.get('code'),
                             'message': '任务已结束且不是可恢复的 completed_with_errors 状态；人物节点未重排。'})
    if not queued_visual:
        actions.append({'code': 'no_queued_visual', 'status': 'not_needed',
                        'message': '没有可安全续派的已排队场景或关键帧任务。'})
    elif not run_can_dispatch:
        actions.append({'code': 'run_not_active', 'status': 'skipped',
                        'message': '任务当前已暂停或结束；保留队列，不越过运行控制。',
                        'visual_id': queued_visual.id})
    elif gpu['state'] == 'busy':
        actions.append({'code': 'gpu_in_use', 'status': 'waiting',
                        'message': '本机模型正占用GPU；保留原视觉队列，后台会在GPU空闲后继续。',
                        'visual_id': queued_visual.id})
    elif gpu['state'] == 'unknown':
        actions.append({'code': 'gpu_health_unknown', 'status': 'blocked',
                        'message': '本地模型进程仍无法核实；为避免两个模型同时抢占GPU，没有派发。',
                        'visual_id': queued_visual.id})
    elif queued_visual.snapshot_json.get('service_snapshot', {}).get('is_cloud') and not run.snapshot_json.get('options', {}).get('allow_cloud_costs'):
        actions.append({'code': 'cloud_cost_not_authorized', 'status': 'blocked',
                        'message': '此任务使用云端服务，但本轮记录没有云端费用授权；没有提交请求。',
                        'visual_id': queued_visual.id})
    else:
        # Only tick the selected durable child. The shared GPU lock and child
        # status make this safe alongside the normal worker; this never builds
        # a new task or retries a provider request with an unknown outcome.
        session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
            action='one_click_repair_requested', details_json={
                'visual_id': queued_visual.id, 'node_id': queued_node.get('id'),
                'gpu_gate': gpu['state'], 'gpu_reason': gpu['reason']}))
        session.commit()
        timed_out = False
        worker_error = None
        try:
            from app.services.cinematic_visuals import visual_tick
            await asyncio.wait_for(visual_tick(request.app.state.settings,
                request.app.state.session_factory, visual_id=queued_visual.id), timeout=20)
        except asyncio.TimeoutError:
            # The POST may have reached ComfyUI before the local wait expired.
            # Leave correlation/state to the worker and never auto-submit again.
            timed_out = True
        except Exception as exc:
            worker_error = type(exc).__name__
        session.expire_all()
        refreshed = session.get(m.ProductionVisual, queued_visual.id)
        current_status = refreshed.status if refreshed else 'missing'
        if worker_error:
            actions.append({'code': 'visual_repair_worker_error', 'status': 'error',
                            'message': '本地视觉工作器诊断失败（'+worker_error+'）；原任务仍保留，后台工作器会继续核对。',
                            'visual_id': queued_visual.id})
        elif timed_out or current_status in {'submitting', 'unknown'}:
            actions.append({'code': 'visual_submission_outcome_unknown', 'status': 'unknown',
                            'message': '提交核对超时或结果未知；保留原任务，禁止重复提交。',
                            'visual_id': queued_visual.id})
        elif current_status in {'submitted', 'running'}:
            actions.append({'code': 'visual_dispatched', 'status': 'applied',
                            'message': '已把已有视觉任务派发给本机ComfyUI；原任务ID与输入快照保留。',
                            'visual_id': queued_visual.id, 'provider_job_id': refreshed.provider_job_id})
        elif current_status == 'queued':
            actions.append({'code': 'visual_still_queued', 'status': 'waiting',
                            'message': '任务仍在本机队列；检查实际GPU/ComfyUI队列后由后台工作器继续，不会创建重复任务。',
                            'visual_id': queued_visual.id})
        else:
            actions.append({'code': 'visual_dispatch_failed', 'status': current_status,
                            'message': refreshed.error_message if refreshed else '视觉记录已不存在。',
                            'visual_id': queued_visual.id})

    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id,
        action='one_click_repair_diagnosed', details_json={
            'gpu_gate': gpu['state'], 'gpu_reason': gpu['reason'],
            'model_connections': [{'service_id': item['service_id'], 'status': item['status']} for item in model_connections],
            'action_codes': [item['code'] for item in actions],
            'blocker_codes': [item['code'] for item in blockers]}))
    session.commit()
    return report(actions, blockers)


def _resume_native_after_connection_repair(session, settings, run, nodes, recovery):
    from app.core.paths import safe_media_path
    from app.models.entities import utcnow
    from app.services.cinematic_quality import generation_inputs_hash

    requeued = []
    for node in nodes:
        if node['id'] not in recovery or node['status'] not in {'running', 'retrying', 'failed', 'exception', 'unknown'}:
            continue
        receipt = recovery[node['id']]
        if node.get('child_id') != (receipt or {}).get('job_id'):
            continue
        shot = session.get(m.StoryboardShot, node['target_id'])
        if not shot or shot.project_id != run.project_id or shot.status == 'deleted':
            continue
        job = session.get(m.GenerationJob, node['child_id']) if node.get('child_id') else None
        input_hash = node.get('inputs_hash')
        if job:
            if (job.provider_job_id != receipt.get('prompt_id') or job.status not in {'queued', 'running', 'failed', 'unknown'}
                    or session.scalar(select(m.VideoRevision.id).where(m.VideoRevision.generation_job_id == job.id).limit(1))):
                continue
            try:
                folder = safe_media_path(settings.media_root, job.parameters_json['output_relative_dir'])
                if folder.is_dir() and any(path.name.startswith(job.id) for path in folder.iterdir()):
                    continue
            except (AppError, OSError, KeyError):
                continue
        if (job and job.provider_job_id and not input_hash) or (input_hash and input_hash != generation_inputs_hash(session, shot)):
            continue
        if job and job.provider_job_id:
            job.status = 'failed'
            job.error_code = 'native_h3_worker_restarted'
            job.error_message = '本机 ComfyUI 工作进程已重启，原任务未留下视频输出。'
            job.finished_at = utcnow()
            job.parameters_json = {**job.parameters_json, 'native_restart_recovery': receipt}
            node.update(code=job.error_code, error=job.error_message)
        _requeue_failed_node(node)
        requeued.append(node['id'])
    return requeued
