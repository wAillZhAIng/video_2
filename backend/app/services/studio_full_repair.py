"""Comprehensive one-click stuck/bug recovery for StudioRun pipelines.

Recovers all real-world deadlocks and interrupted states safely:
- Adopts already-valid outputs on disk without wasting GPU.
- Resumes genuinely live ComfyUI prompts still present in /queue or /history.
- Closes dead/unknown/zombie child records (GenerationJob, QualityRender,
  MediaProcessTask, ProductionVisual, CharacterGenerationJob, AssemblyRender)
  and requeues their nodes cleanly while preserving full audit history.
- Resolves fingerprint-blocking unknown QualityRender / MediaProcessTask rows.
- Refreshes local service snapshots and video_upscale routes after config edits.
- Unblocks stale waiting_dependencies, retrying backoffs, paused children,
  failed/blocked automatic final assembly, and cross-project dead zombie locks.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import models as m
from app.core.errors import AppError
from app.models.entities import utcnow
from app.models.studio import StudioRun

logger = logging.getLogger(__name__)

_LOCAL_AUTO_RECOVERABLE_VIDEO_CODES = frozenset({
    'submission_outcome_unknown',
    'provider_status_unknown',
    'provider_result_unavailable',
    'studio_dispatch_unknown',
    'comfyui_interrupted_retryable',
    'native_h3_worker_restarted',
    'comfyui_execution_failed',
    'comfyui_unavailable',
    'comfyui_not_ready',
    'local_gpu_busy',
    'provider_unavailable',
    'native_h3_not_ready',
    'native_workflow_unavailable',
    'studio_internal_error',
    'studio_video_output_missing',
    'studio_service_changed',
})

_NON_AUTO_RETRY_MEDIA_CODES = frozenset({
    'audio_duration_exceeds_shot',
})

_NODE_KIND_LABELS = {
    'video': '分镜视频',
    'quality': '画质处理',
    'quality_check': '视频技术检查',
    'scene': '场景参考',
    'keyframe': '镜头关键画面',
    'character': '人物参考素材',
    'tts': '角色配音',
    'audio_mix': '对白混音',
    'lip_sync': '口型同步',
}


def _age_seconds(dt: datetime | None) -> float:
    if dt is None:
        return 999999.0
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds())


def _has_active_local_process(markers: tuple[str, ...]) -> bool:
    """Fast Linux /proc check to avoid interrupting live local upscale/ffmpeg jobs."""
    if sys.platform != 'linux':
        return False
    proc_root = Path('/proc')
    if not proc_root.is_dir():
        return False
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / 'cmdline').read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmd = raw.replace(b'\x00', b' ').decode('utf-8', errors='ignore')
        if any(marker in cmd for marker in markers):
            return True
    return False


def has_active_upscale_process() -> bool:
    return _has_active_local_process(('upscale_video_realesrgan.py', 'deploy_seedvr2.py'))


def has_active_assembly_ffmpeg() -> bool:
    return _has_active_local_process(('/assemblies/', 'assembly_'))


def _video_outputs_from_history(history_entry: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(history_entry, dict):
        return found
    for output in (history_entry.get('outputs') or {}).values():
        if not isinstance(output, dict):
            continue
        for values in output.values():
            if isinstance(values, list):
                found.extend(
                    item for item in values
                    if isinstance(item, dict)
                    and str(item.get('filename', '')).lower().endswith('.mp4')
                    and item.get('type') == 'output'
                )
    return found


async def inspect_comfy_state(
    base_urls: set[str],
    prompt_probes: list[tuple[str, str]],
) -> dict[str, Any]:
    """Query live ComfyUI /queue and specific /history/{prompt_id} entries."""
    queues: dict[str, dict[str, Any]] = {}
    histories: dict[tuple[str, str], dict[str, Any]] = {}
    cleaned_urls = {u.rstrip('/') for u in base_urls if u and u.startswith(('http://', 'https://'))}
    if not cleaned_urls:
        return {'queues': queues, 'histories': histories}

    async with httpx.AsyncClient(timeout=3.0, trust_env=False, follow_redirects=False) as client:
        for base in cleaned_urls:
            info = {'reachable': False, 'active_prompt_ids': set(), 'queue_empty': False}
            try:
                resp = await client.get(f'{base}/queue')
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict) and isinstance(payload.get('queue_running'), list):
                        active: set[str] = set()
                        for bucket in ('queue_running', 'queue_pending'):
                            for item in payload.get(bucket) or []:
                                if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], str):
                                    active.add(item[1])
                        info = {
                            'reachable': True,
                            'active_prompt_ids': active,
                            'queue_empty': len(active) == 0,
                        }
            except Exception:
                pass
            queues[base] = info

        seen_probes: set[tuple[str, str]] = set()
        for raw_base, prompt_id in prompt_probes:
            base = (raw_base or '').rstrip('/')
            if not base or not prompt_id or (base, prompt_id) in seen_probes:
                continue
            seen_probes.add((base, prompt_id))
            qinfo = queues.get(base)
            if not qinfo or not qinfo.get('reachable'):
                continue
            entry_info = {'in_history': False, 'completed_mp4': False, 'status_str': None}
            try:
                resp = await client.get(f'{base}/history/{prompt_id}')
                if resp.status_code == 200:
                    payload = resp.json()
                    hist = payload.get(prompt_id) if isinstance(payload, dict) else None
                    if isinstance(hist, dict):
                        status_str = (hist.get('status') or {}).get('status_str')
                        entry_info = {
                            'in_history': True,
                            'completed_mp4': bool(_video_outputs_from_history(hist)) and status_str != 'error',
                            'status_str': status_str,
                        }
            except Exception:
                pass
            histories[(base, prompt_id)] = entry_info

    return {'queues': queues, 'histories': histories}


def collect_comfy_probes(session, settings, run: StudioRun, other_runs: list[StudioRun]) -> tuple[set[str], list[tuple[str, str]]]:
    base_urls: set[str] = set()
    probes: list[tuple[str, str]] = []
    if getattr(settings, 'comfyui_base_url', None):
        base_urls.add(str(settings.comfyui_base_url).rstrip('/'))

    def _scan_run(target_run: StudioRun) -> None:
        svc_video = (target_run.snapshot_json or {}).get('services', {}).get('video') or {}
        if svc_video.get('provider') == 'comfyui' and svc_video.get('base_url'):
            base_urls.add(str(svc_video['base_url']).rstrip('/'))
        for node in target_run.nodes_json or []:
            cid = node.get('child_id')
            if not cid:
                continue
            if node.get('kind') == 'video':
                job = session.get(m.GenerationJob, cid)
                if job and job.provider == 'comfyui_h3':
                    base = ((job.parameters_json or {}).get('service_snapshot') or {}).get('base_url') or settings.comfyui_base_url
                    if base:
                        base_urls.add(str(base).rstrip('/'))
                        if job.provider_job_id:
                            probes.append((str(base).rstrip('/'), job.provider_job_id))
            elif node.get('kind') in {'scene', 'keyframe'}:
                vis = session.get(m.ProductionVisual, cid)
                if vis and vis.provider_job_id:
                    snap = (vis.snapshot_json or {}).get('service_snapshot') or {}
                    if snap.get('provider') == 'comfyui':
                        base = snap.get('base_url') or settings.comfyui_base_url
                        if base:
                            base_urls.add(str(base).rstrip('/'))
                            probes.append((str(base).rstrip('/'), vis.provider_job_id))

    _scan_run(run)
    for other in other_runs:
        _scan_run(other)
    return base_urls, probes


def _record_node_reset(node: dict[str, Any], *, action: str, note: str | None = None) -> None:
    node['history'] = [*(node.get('history') or []), {
        'status': node.get('status'),
        'child_id': node.get('child_id'),
        'code': node.get('code'),
        'error': node.get('error'),
        'action': action,
        **({'note': note} if note else {}),
    }]
    node.pop('native_dispatch_wait', None)
    node.pop('retry_after', None)
    node.update(status='pending', child_id=None, error=None, code=None)


def _node_is_cloud(run: StudioRun, node: dict[str, Any], child_snap: dict[str, Any] | None = None) -> bool:
    if isinstance(child_snap, dict) and 'is_cloud' in child_snap:
        return bool(child_snap.get('is_cloud'))
    kind = node.get('kind')
    snap_services = (run.snapshot_json or {}).get('services') or {}
    if kind == 'video':
        route = ((run.snapshot_json or {}).get('routes_by_shot', {}).get(node.get('target_id'), {}).get('video_generation')
                 or (run.snapshot_json or {}).get('routes', {}).get('video_generation') or {})
        sel = (route.get('selected') or {}).get('service_snapshot') or {}
        if 'is_cloud' in sel:
            return bool(sel['is_cloud'])
        return bool((snap_services.get('video') or {}).get('is_cloud', False))
    if kind in {'scene', 'keyframe', 'character'}:
        return bool((snap_services.get('image') or {}).get('is_cloud', False))
    return False


def _refresh_local_service_snapshots(session, settings, run: StudioRun, nodes: list[dict[str, Any]]) -> bool:
    """Keep frozen local service metadata synced if the user edited/re-enabled a local service."""
    from app.services.studio_pipeline import _capture_services
    from app.services.studio_quality_apply import refresh_upscale_routes_for_retry

    changed = False
    snapshots, errors = _capture_services(session, run.project_id)
    run_snap = copy.deepcopy(run.snapshot_json or {})
    services = dict(run_snap.get('services') or {})
    for key, fresh in snapshots.items():
        if fresh.get('is_cloud'):
            continue
        old = services.get(key)
        if not old or not old.get('service_id'):
            continue
        if old.get('service_id') == fresh.get('service_id') and old.get('service_revision') != fresh.get('service_revision'):
            services[key] = fresh
            changed = True
    if changed:
        run_snap['services'] = services
        run_snap['service_errors'] = errors
        run.snapshot_json = run_snap
        flag_modified(run, 'snapshot_json')

    quality_nodes = [n for n in nodes if n.get('kind') == 'quality' and n.get('status') in {'failed', 'exception', 'unknown', 'pending'}]
    if quality_nodes and settings:
        try:
            refreshed_routes = refresh_upscale_routes_for_retry(session, settings, run, quality_nodes)
            if refreshed_routes:
                changed = True
        except Exception:
            logger.debug('refresh_upscale_routes_for_retry skipped during repair', exc_info=True)
    return changed


def _close_generation_job(job: m.GenerationJob, reason: str, *, close_as_unknown: bool = False) -> None:
    from app.services.jobs import clear_comfy_poll_state
    clear_comfy_poll_state(job.id)
    job.status = 'closed_unknown' if close_as_unknown else 'failed'
    job.error_code = job.error_code or 'one_click_repair_reset'
    base_msg = (job.error_message or '原视频任务中断').rstrip('。；;')
    if '一键修复' not in base_msg:
        job.error_message = f'{base_msg}（已由一键修复关闭旧记录并重新排队）'
    if job.finished_at is None:
        job.finished_at = utcnow()


def _close_blocking_quality_renders(session, source_video_id: str | None, current_qr_id: str | None = None) -> int:
    closed = 0
    if current_qr_id:
        qr = session.get(m.QualityRender, current_qr_id)
        if qr and qr.status in {'unknown', 'running'}:
            qr.status = 'failed'
            qr.error_message = (qr.error_message or '原画质任务异常中断') + '（已由一键修复重置）'
            closed += 1
    if source_video_id:
        for other in session.scalars(
            select(m.QualityRender).where(
                m.QualityRender.source_video_id == source_video_id,
                m.QualityRender.status == 'unknown',
            )
        ):
            other.status = 'failed'
            other.error_message = (other.error_message or '原画质任务状态未知') + '（已由一键修复解除阻塞）'
            closed += 1
    return closed


def _close_blocking_media_tasks(session, project_id: str, shot_id: str | None, kind: str, current_task_id: str | None = None) -> int:
    closed = 0
    if current_task_id:
        task = session.get(m.MediaProcessTask, current_task_id)
        if task and task.status in {'unknown', 'running'}:
            task.status = 'failed'
            task.error_message = (task.error_message or '原媒体任务异常中断') + '（已由一键修复重置）'
            closed += 1
    if shot_id:
        for other in session.scalars(
            select(m.MediaProcessTask).where(
                m.MediaProcessTask.project_id == project_id,
                m.MediaProcessTask.shot_id == shot_id,
                m.MediaProcessTask.kind == kind,
                m.MediaProcessTask.status == 'unknown',
            )
        ):
            other.status = 'failed'
            other.error_message = (other.error_message or '原媒体任务状态未知') + '（已由一键修复解除阻塞）'
            closed += 1
    return closed


def _unblock_cross_project_zombies(
    session,
    settings,
    current_run_id: str,
    comfy_state: dict[str, Any],
    *,
    upscale_active: bool,
) -> list[dict[str, Any]]:
    """Clear provably dead zombie locks in OTHER non-terminal runs so scheduling is never deadlocked."""
    from app.services.studio_pipeline import RUN_TERMINAL

    unblocked: list[dict[str, Any]] = []
    queues = comfy_state.get('queues') or {}
    histories = comfy_state.get('histories') or {}
    default_base = (getattr(settings, 'comfyui_base_url', '') or '').rstrip('/')

    other_runs = list(session.scalars(
        select(StudioRun).where(
            StudioRun.id != current_run_id,
            StudioRun.status.not_in(list(RUN_TERMINAL)),
        )
    ))
    for other in other_runs:
        other_nodes = copy.deepcopy(other.nodes_json or [])
        mutated = False
        for node in other_nodes:
            kind = node.get('kind')
            status = node.get('status')
            cid = node.get('child_id')

            if status == 'dispatching' and not cid:
                # Only clear if stale (>180s) AND ComfyUI queue is empty (or unreachable in tests only if very stale)
                qinfo = queues.get(default_base) or {}
                if _age_seconds(other.updated_at) > 180 and qinfo.get('reachable') and qinfo.get('queue_empty'):
                    _record_node_reset(node, action='one_click_zombie_unblock', note='Cleared stale dispatching zombie')
                    mutated = True
                    unblocked.append({'run_id': other.id, 'node_id': node['id'], 'reason': 'stale_dispatching'})
                continue

            if status not in {'running', 'dispatching'} or not cid:
                continue

            if kind == 'video':
                job = session.get(m.GenerationJob, cid)
                if job is None:
                    _record_node_reset(node, action='one_click_zombie_unblock', note='Missing GenerationJob')
                    mutated = True
                    unblocked.append({'run_id': other.id, 'node_id': node['id'], 'reason': 'missing_job'})
                    continue
                if job.provider != 'comfyui_h3':
                    continue
                base = (((job.parameters_json or {}).get('service_snapshot') or {}).get('base_url') or default_base).rstrip('/')
                qinfo = queues.get(base) or {}
                if job.status in {'submitting', 'queued', 'running'} and not job.provider_job_id:
                    if _age_seconds(job.created_at) > 180 and qinfo.get('reachable') and qinfo.get('queue_empty') and not upscale_active:
                        _close_generation_job(job, 'cross_run_zombie_submitting', close_as_unknown=True)
                        _record_node_reset(node, action='one_click_zombie_unblock', note='Closed stale submitting job without prompt_id')
                        mutated = True
                        unblocked.append({'run_id': other.id, 'node_id': node['id'], 'reason': 'zombie_submitting_no_prompt'})
                elif job.status in {'queued', 'running'} and job.provider_job_id and qinfo.get('reachable'):
                    in_queue = job.provider_job_id in (qinfo.get('active_prompt_ids') or set())
                    hist = histories.get((base, job.provider_job_id)) or {}
                    if not in_queue and not hist.get('in_history') and _age_seconds(job.created_at) > 120:
                        _close_generation_job(job, 'comfyui_prompt_lost', close_as_unknown=False)
                        _record_node_reset(node, action='one_click_zombie_unblock', note='ComfyUI prompt lost from queue & history')
                        mutated = True
                        unblocked.append({'run_id': other.id, 'node_id': node['id'], 'reason': 'comfyui_prompt_lost'})

            elif kind == 'quality':
                qr = session.get(m.QualityRender, cid)
                if qr and qr.status == 'running' and not upscale_active and _age_seconds(qr.updated_at) > 180:
                    qr.status = 'failed'
                    qr.error_message = '原画质任务无活跃进程（已由一键修复解除跨项目阻塞）'
                    _record_node_reset(node, action='one_click_zombie_unblock', note='Closed dead running QualityRender')
                    mutated = True
                    unblocked.append({'run_id': other.id, 'node_id': node['id'], 'reason': 'zombie_quality_running'})

        if mutated:
            other.nodes_json = other_nodes
            flag_modified(other, 'nodes_json')
            session.add(m.AuditEvent(
                object_type='studio_run',
                object_id=other.id,
                action='one_click_zombie_unblocked',
                details_json={'triggered_by_run_id': current_run_id, 'nodes': [u for u in unblocked if u['run_id'] == other.id]},
            ))
    return unblocked


def repair_stuck_run(
    session,
    settings,
    run: StudioRun,
    nodes: list[dict[str, Any]],
    *,
    comfy_state: dict[str, Any],
    gpu: dict[str, Any],
    force_recover: bool = False,
    skip_node_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Repair all stuck/bugged nodes, children, dependencies, and assembly in-place on `nodes` and `run`."""
    from app.services.studio_pipeline import _valid_video, _queue_automatic_render, _leaf, MEDIA_KINDS, RUN_TERMINAL

    skip_ids = set(skip_node_ids or ())
    queues = comfy_state.get('queues') or {}
    histories = comfy_state.get('histories') or {}
    default_base = (getattr(settings, 'comfyui_base_url', '') or '').rstrip('/')
    allow_cloud = bool((run.snapshot_json or {}).get('options', {}).get('allow_cloud_costs', False))
    upscale_active = has_active_upscale_process()
    assembly_active = has_active_assembly_ffmpeg()

    repaired_nodes: list[dict[str, str]] = []
    adopted_nodes: list[dict[str, str]] = []
    resumed_nodes: list[dict[str, str]] = []
    unblocked_waiting: list[str] = []
    extra_actions: list[dict[str, Any]] = []
    extra_blockers: list[dict[str, Any]] = []
    assembly_requeued = False
    assembly_completed = False
    run_reopened = False

    # Respect paused/finished runs ONLY in conservative mode (force_recover=False).
    if not force_recover and run.status in (RUN_TERMINAL - {'completed_with_errors'}) | {'paused'}:
        return {
            'repaired_nodes': [],
            'adopted_nodes': [],
            'resumed_nodes': [],
            'unblocked_waiting': [],
            'unblocked_other_runs': [],
            'assembly_requeued': False,
            'assembly_completed': False,
            'run_reopened': False,
            'refreshed_services': False,
            'actions': [],
            'blockers': [],
            'mutated': False,
        }

    # Step A: Refresh local service snapshots & upscale routes
    refreshed_services = _refresh_local_service_snapshots(session, settings, run, nodes)

    # Step B: Unpause stuck child batches/visuals when unpausing or active
    unpaused_children = 0
    if force_recover or run.status != 'paused':
        for node in nodes:
            cid = node.get('child_id')
            if not cid:
                continue
            if node.get('kind') == 'character':
                batch = session.get(m.CharacterBatch, cid)
                if batch and batch.paused:
                    batch.paused = False
                    unpaused_children += 1
            elif node.get('kind') in {'scene', 'keyframe'}:
                vis = session.get(m.ProductionVisual, cid)
                if vis and vis.status == 'studio_paused':
                    vis.status = 'queued'
                    unpaused_children += 1

    # Step C: Inspect & repair every stuck node
    for node in nodes:
        nid = node.get('id', '')
        if nid in skip_ids:
            continue
        kind = node.get('kind', '')
        status = node.get('status', '')
        code = node.get('code') or ''
        cid = node.get('child_id')

        if status == 'succeeded' or (status == 'skipped' and node.get('optional')):
            continue

        # --- 1. VIDEO NODE ---
        if kind == 'video':
            job = session.get(m.GenerationJob, cid) if cid else None
            # Also look for orphan GenerationJob if dispatching with no child_id
            if not job and status == 'dispatching':
                shot_id = node.get('target_id')
                if shot_id:
                    for cand in session.scalars(
                        select(m.GenerationJob)
                        .where(m.GenerationJob.shot_id == shot_id)
                        .order_by(m.GenerationJob.created_at.desc())
                    ):
                        pj = cand.parameters_json or {}
                        if pj.get('studio_run_id') == run.id and pj.get('studio_node_id') == nid:
                            job = cand
                            node['child_id'] = cand.id
                            cid = cand.id
                            break

            # 1a. Adopt already-valid VideoRevision on disk
            if job:
                existing_vid = session.scalar(
                    select(m.VideoRevision)
                    .where(m.VideoRevision.generation_job_id == job.id)
                    .order_by(m.VideoRevision.revision.desc())
                    .limit(1)
                )
                if existing_vid and existing_vid.relative_path:
                    try:
                        _valid_video(session, settings, existing_vid)
                        job.status = 'generated'
                        job.error_code = None
                        job.error_message = None
                        node.update(status='succeeded', child_id=job.id, video_id=existing_vid.id, error=None, code=None)
                        adopted_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                        continue
                    except AppError:
                        pass

            # 1b. Check live ComfyUI queue / completed history before resetting
            child_snap = (job.parameters_json or {}).get('service_snapshot') if job else None
            is_cloud = _node_is_cloud(run, node, child_snap)
            is_local_comfy = (job.provider == 'comfyui_h3') if job else (not is_cloud)
            base = ((child_snap or {}).get('base_url') or default_base).rstrip('/')
            qinfo = queues.get(base) or {}

            if job and is_local_comfy and job.provider_job_id and qinfo.get('reachable'):
                in_queue = job.provider_job_id in (qinfo.get('active_prompt_ids') or set())
                hist = histories.get((base, job.provider_job_id)) or {}
                if in_queue or hist.get('completed_mp4'):
                    if job.status not in {'queued', 'running'} or status != 'running':
                        from app.services.jobs import clear_comfy_poll_state
                        clear_comfy_poll_state(job.id)
                        job.status = 'running'
                        job.error_code = None
                        job.error_message = None
                        job.finished_at = None
                        node.update(status='running', child_id=job.id, error=None, code=None)
                        resumed_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                    continue

            # 1c. Determine if video node is stuck/dead and eligible for repair
            is_zombie_dispatching = status == 'dispatching' and (
                job is None
                or (job.status == 'submitting' and not job.provider_job_id and (
                    force_recover or (_age_seconds(job.created_at) > 90 and (not qinfo.get('reachable') or qinfo.get('queue_empty')))
                ))
            )
            is_zombie_running = status == 'running' and (
                (job is None and (force_recover or _age_seconds(run.updated_at) > 120))
                or (job is not None and job.status in {'failed', 'cancelled', 'unknown', 'closed_unknown'})
                or (job is not None and job.status in {'submitting', 'queued', 'running'} and not job.provider_job_id and (
                    force_recover or _age_seconds(job.created_at) > 90
                ))
                or (
                    job is not None and is_local_comfy and job.provider_job_id and qinfo.get('reachable')
                    and job.provider_job_id not in (qinfo.get('active_prompt_ids') or set())
                    and not (histories.get((base, job.provider_job_id)) or {}).get('in_history')
                    and (_age_seconds(job.created_at) > 45)
                )
            )
            is_terminal_err = status in {'unknown', 'failed', 'exception', 'cancelled', 'retrying'}

            should_repair_video = False
            if is_zombie_dispatching or is_zombie_running:
                should_repair_video = True
            elif is_terminal_err:
                if is_cloud and not allow_cloud:
                    extra_blockers.append({
                        'node_id': nid, 'kind': kind, 'status': status, 'code': 'cloud_cost_not_authorized',
                        'message': '该分镜视频使用云端服务，本轮未勾选云端费用授权；请勾选授权后再重试。',
                    })
                    continue
                if force_recover:
                    should_repair_video = True
                elif is_local_comfy:
                    job_code = (job.error_code if job else '') or code
                    no_prompt = (job is None) or (job.provider_job_id is None)
                    prompt_absent = bool(
                        job and job.provider_job_id and qinfo.get('reachable')
                        and job.provider_job_id not in (qinfo.get('active_prompt_ids') or set())
                        and not (histories.get((base, job.provider_job_id)) or {}).get('in_history')
                    )
                    if no_prompt or prompt_absent or job_code in _LOCAL_AUTO_RECOVERABLE_VIDEO_CODES or status == 'retrying':
                        should_repair_video = True

            if should_repair_video:
                if job and job.status in {'submitting', 'queued', 'running', 'unknown'}:
                    _close_generation_job(job, 'one_click_full_repair', close_as_unknown=job.status in {'submitting', 'unknown'})
                # Also close any orphan submitting GenerationJob for this exact node
                if node.get('target_id'):
                    for orphan in session.scalars(
                        select(m.GenerationJob).where(
                            m.GenerationJob.shot_id == node['target_id'],
                            m.GenerationJob.status == 'submitting',
                            m.GenerationJob.provider_job_id.is_(None),
                        )
                    ):
                        pj = orphan.parameters_json or {}
                        if pj.get('studio_run_id') == run.id and pj.get('studio_node_id') == nid:
                            _close_generation_job(orphan, 'orphan_submitting_cleanup', close_as_unknown=True)
                _record_node_reset(node, action='one_click_full_repair', note=f'Reset stuck video node ({status}/{code})')
                repaired_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
            continue

        # --- 2. QUALITY (UPSCALE / POSTPROCESS) NODE ---
        if kind == 'quality':
            qr = session.get(m.QualityRender, cid) if cid else None
            # 2a. Adopt already-generated quality render
            if qr and qr.status == 'generated' and qr.video_id:
                vid = session.get(m.VideoRevision, qr.video_id)
                if vid:
                    try:
                        _valid_video(session, settings, vid)
                        node.update(status='succeeded', video_id=vid.id, error=None, code=None)
                        adopted_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                        continue
                    except AppError:
                        pass

            # 2b. Try SeedVR2 native output collection if prompt_id exists
            if qr and qr.status in {'failed', 'unknown'} and (qr.snapshot_json or {}).get('settings', {}).get('processing') == 'seedvr2':
                try:
                    from app.services.seedvr_recovery import resume_native
                    resume_native(session, settings, qr.id)
                    node.update(status='running', error=None, code=None)
                    resumed_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                    continue
                except Exception:
                    pass

            is_stuck_quality = (
                status in {'failed', 'exception', 'unknown', 'cancelled', 'retrying'}
                or (status == 'running' and (
                    qr is None
                    or qr.status in {'failed', 'cancelled', 'unknown'}
                    or (qr.status == 'running' and not upscale_active and _age_seconds(qr.updated_at) > 60)
                ))
            )
            if is_stuck_quality:
                src_vid_id = node.get('source_video_id')
                if not src_vid_id:
                    for dep_id in node.get('dependencies') or []:
                        dep = next((x for x in nodes if x.get('id') == dep_id), None)
                        if dep and dep.get('video_id'):
                            src_vid_id = dep['video_id']
                            break
                _close_blocking_quality_renders(session, src_vid_id, cid)
                _record_node_reset(node, action='one_click_full_repair', note=f'Reset stuck quality node ({status}/{code})')
                repaired_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
            continue

        # --- 3. QUALITY CHECK NODE ---
        if kind == 'quality_check':
            if status in {'failed', 'exception', 'unknown', 'cancelled', 'running', 'retrying'}:
                _record_node_reset(node, action='one_click_full_repair', note=f'Reset stuck quality_check ({status}/{code})')
                repaired_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
            continue

        # --- 4. MEDIA NODES (tts, audio_mix, lip_sync) ---
        if kind in MEDIA_KINDS:
            if code in _NON_AUTO_RETRY_MEDIA_CODES:
                continue
            task = session.get(m.MediaProcessTask, cid) if cid else None
            if task and task.status == 'succeeded':
                if status != 'running':
                    node.update(status='running', error=None, code=None)
                    resumed_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                continue
            is_stuck_media = (
                status in {'failed', 'exception', 'unknown', 'cancelled', 'retrying'}
                or (status == 'running' and (task is None or task.status in {'failed', 'blocked', 'cancelled', 'unknown'}))
            )
            if is_stuck_media:
                shot_id = node.get('shot_id') or node.get('target_id')
                _close_blocking_media_tasks(session, run.project_id, shot_id, kind, cid)
                _record_node_reset(node, action='one_click_full_repair', note=f'Reset stuck media node ({status}/{code})')
                repaired_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
            continue

        # --- 5. SCENE / KEYFRAME VISUAL NODES (full recovery mode) ---
        if kind in {'scene', 'keyframe'} and force_recover:
            vis = session.get(m.ProductionVisual, cid) if cid else None
            if vis and vis.status in {'approved', 'needs_review'} and vis.relative_path:
                if status != 'running':
                    node.update(status='running', error=None, code=None)
                    resumed_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                continue
            vis_snap = (vis.snapshot_json or {}).get('service_snapshot') if vis else None
            is_cloud = _node_is_cloud(run, node, vis_snap)
            base = ((vis_snap or {}).get('base_url') or default_base).rstrip('/')
            qinfo = queues.get(base) or {}
            if vis and not is_cloud and vis.provider_job_id and qinfo.get('reachable'):
                in_queue = vis.provider_job_id in (qinfo.get('active_prompt_ids') or set())
                hist = histories.get((base, vis.provider_job_id)) or {}
                if in_queue or hist.get('in_history'):
                    if vis.status not in {'submitted', 'running'} or status != 'running':
                        vis.status = 'running'
                        vis.error_message = None
                        node.update(status='running', error=None, code=None)
                        resumed_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
                    continue

            is_stuck_visual = (
                status in {'failed', 'exception', 'unknown', 'cancelled', 'retrying'}
                or (status in {'running', 'dispatching'} and (
                    vis is None
                    or vis.status in {'failed', 'rejected', 'cancelled', 'unknown'}
                    or (vis.status == 'submitting' and (not qinfo.get('reachable') or qinfo.get('queue_empty')))
                ))
            )
            if is_stuck_visual:
                if is_cloud and not allow_cloud:
                    extra_blockers.append({
                        'node_id': nid, 'kind': kind, 'status': status, 'code': 'cloud_cost_not_authorized',
                        'message': '该画面节点使用云端服务，本轮未勾选云端费用授权。',
                    })
                    continue
                if vis and vis.status in {'submitting', 'unknown', 'studio_paused'}:
                    vis.status = 'failed'
                    vis.error_message = (vis.error_message or '原画面任务中断') + '（已由一键修复重置）'
                _record_node_reset(node, action='one_click_full_repair', note=f'Reset stuck {kind} node ({status}/{code})')
                repaired_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})
            continue

        # --- 6. CHARACTER NODE (full recovery mode) ---
        if kind == 'character' and force_recover:
            batch = session.get(m.CharacterBatch, cid) if cid else None
            cjobs = list(session.scalars(
                select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id == batch.id)
            )) if batch else []
            is_stuck_char = (
                status in {'failed', 'exception', 'unknown', 'cancelled', 'retrying'}
                or (status in {'running', 'dispatching'} and (
                    batch is None
                    or not cjobs
                    or all(j.status in {'failed', 'cancelled', 'unknown', 'rejected'} for j in cjobs)
                ))
            )
            if is_stuck_char:
                # If capability_unverified and literally no enabled character_generation service exists, keep clear blocker
                if code == 'capability_unverified':
                    has_img_svc = session.scalar(
                        select(m.AIService.id).where(
                            m.AIService.module == 'character_generation',
                            m.AIService.enabled.is_(True),
                        ).limit(1)
                    )
                    if not has_img_svc:
                        continue
                if batch:
                    batch.paused = False
                    if batch.status in {'unknown', 'running', 'queued'}:
                        batch.status = 'failed'
                    for cj in cjobs:
                        if cj.status in {'submitting', 'unknown', 'queued', 'running'}:
                            cj.status = 'failed'
                            cj.error_message = (cj.error_message or '原人物任务中断') + '（已由一键修复重置）'
                _record_node_reset(node, action='one_click_full_repair', note=f'Reset stuck character node ({status}/{code})')
                repaired_nodes.append({'node_id': nid, 'kind': kind, 'target_id': node.get('target_id', '')})

    # Step D: Cascade dependency recovery & unblock stale waiting_dependencies
    reset_ids = {item['node_id'] for item in repaired_nodes}
    if reset_ids:
        changed_cascade = True
        while changed_cascade:
            changed_cascade = False
            for node in nodes:
                deps = set(node.get('dependencies') or [])
                if deps & reset_ids:
                    if node.get('status') in {'failed', 'exception', 'unknown', 'cancelled', 'pending'}:
                        if node.get('status') != 'waiting_dependencies':
                            if node.get('status') in {'failed', 'exception', 'unknown', 'cancelled'}:
                                _record_node_reset(node, action='one_click_cascade_reset', note='Upstream dependency repaired')
                                repaired_nodes.append({'node_id': node['id'], 'kind': node.get('kind', ''), 'target_id': node.get('target_id', '')})
                            node['status'] = 'waiting_dependencies'
                            reset_ids.add(node['id'])
                            changed_cascade = True

    succeeded_ids = {n['id'] for n in nodes if n.get('status') == 'succeeded'}
    for node in nodes:
        if node.get('status') == 'waiting_dependencies':
            deps = node.get('dependencies') or []
            if all(dep in succeeded_ids for dep in deps):
                node['status'] = 'pending'
                node['error'] = None
                node['code'] = None
                unblocked_waiting.append(node['id'])

    # Step E: Recover stuck AssemblyRender / automatic final export
    if run.status == 'rendering' and (run.result_json or {}).get('render_id'):
        render = session.get(m.AssemblyRender, run.result_json['render_id'])
        if render and render.status == 'generated':
            is_draft = bool((render.snapshot_json or {}).get('automatic_draft'))
            run.status = 'waiting_review' if is_draft else 'completed'
            run.error_message = None
            rj = dict(run.result_json or {})
            rj['relative_path'] = render.relative_path
            if is_draft:
                rj['automatic_draft'] = {**rj.get('automatic_draft', {}), 'status': 'generated', 'human_review_required': True}
            if (render.snapshot_json or {}).get('automatic_final'):
                rj['automatic_final'] = {**rj.get('automatic_final', {}), 'status': 'generated'}
            run.result_json = rj
            flag_modified(run, 'result_json')
            assembly_completed = True
        elif render is None or render.status in {'failed', 'cancelled'} or (
            force_recover and render.status == 'running' and not assembly_active and _age_seconds(render.updated_at) > 300
        ):
            if render and render.status == 'running':
                render.status = 'failed'
                render.error_message = '成片合成进程中断（已由一键修复重新排队）'
            try:
                _queue_automatic_render(session, settings, run, nodes, retry=True)
                run.error_message = None
                assembly_requeued = True
            except AppError as exc:
                extra_blockers.append({'node_id': 'assembly', 'kind': 'assembly', 'status': 'failed', 'code': exc.code, 'message': exc.message})
    elif run.status in {'completed_with_errors', 'waiting_review', 'failed', 'exceptions', 'paused'} and not repaired_nodes:
        shot_ids = (run.snapshot_json or {}).get('shot_ids') or []
        leaves = [_leaf(nodes, sid) for sid in shot_ids]
        all_shots_ok = bool(leaves) and all(l and l.get('status') == 'succeeded' for l in leaves)
        all_nodes_ok = all(n.get('status') == 'succeeded' or (n.get('status') == 'skipped' and n.get('optional')) for n in nodes)
        wants_auto = bool((run.snapshot_json or {}).get('automatic_final') or (run.snapshot_json or {}).get('options', {}).get('automatic_draft_assembly'))
        auto_stage = ((run.result_json or {}).get('automatic_final') or (run.result_json or {}).get('automatic_draft') or {}).get('status')
        if all_shots_ok and all_nodes_ok and wants_auto and (auto_stage in {None, 'blocked', 'failed', 'cancelled'} or not (run.result_json or {}).get('render_id')):
            try:
                _queue_automatic_render(session, settings, run, nodes, retry=bool((run.result_json or {}).get('render_id')))
                run.error_message = None
                assembly_requeued = True
            except AppError as exc:
                extra_blockers.append({'node_id': 'assembly', 'kind': 'assembly', 'status': 'failed', 'code': exc.code, 'message': exc.message})

    # Step F: Re-open stuck StudioRun status if there is active/pending/repaired work
    has_work = any(n.get('status') in {'pending', 'waiting_dependencies', 'running', 'dispatching', 'retrying'} for n in nodes)
    if not assembly_requeued and not assembly_completed:
        if force_recover and run.status in {'completed_with_errors', 'failed', 'exceptions', 'paused', 'waiting_review'} and has_work:
            run.status = 'queued'
            run.error_message = None
            run_reopened = True
        elif not force_recover and (repaired_nodes or adopted_nodes or resumed_nodes or unblocked_waiting) and run.status in {'completed_with_errors', 'failed', 'exceptions'}:
            run.status = 'queued'
            run.error_message = None
            run_reopened = True
        elif (repaired_nodes or adopted_nodes or resumed_nodes or unblocked_waiting) and run.status in {'queued', 'running'}:
            run.error_message = None

    # Step G: Cross-project provably dead zombie unblocker
    unblocked_other_runs = _unblock_cross_project_zombies(
        session, settings, run.id, comfy_state, upscale_active=upscale_active,
    )

    # Step H: Compose user-friendly Chinese action messages
    if adopted_nodes:
        labels = '、'.join(sorted({_NODE_KIND_LABELS.get(x['kind'], x['kind']) for x in adopted_nodes}))
        extra_actions.append({
            'code': 'full_repair_outputs_adopted',
            'status': 'applied',
            'node_ids': [x['node_id'] for x in adopted_nodes],
            'message': f'已直接采纳 {len(adopted_nodes)} 个已有完整输出文件的节点（{labels}），无需重复生成。',
        })
    if resumed_nodes:
        labels = '、'.join(sorted({_NODE_KIND_LABELS.get(x['kind'], x['kind']) for x in resumed_nodes}))
        extra_actions.append({
            'code': 'full_repair_live_tasks_resumed',
            'status': 'applied',
            'node_ids': [x['node_id'] for x in resumed_nodes],
            'message': f'已接回 {len(resumed_nodes)} 个仍在 ComfyUI 队列/历史中的任务（{labels}），后台正在拉取结果。',
        })
    if repaired_nodes:
        labels = '、'.join(sorted({_NODE_KIND_LABELS.get(x['kind'], x['kind']) for x in repaired_nodes}))
        gpu_note = '后台工作器将在 GPU 空闲后自动继续执行。' if gpu.get('state') != 'idle' else '已重新加入执行队列并触发立即推进。'
        extra_actions.append({
            'code': 'full_repair_nodes_requeued',
            'status': 'applied',
            'node_ids': [x['node_id'] for x in repaired_nodes],
            'message': f'已自动修复 {len(repaired_nodes)} 个卡住/未知/失败节点（{labels}），旧记录已安全归档，{gpu_note}',
        })
    if unblocked_waiting:
        extra_actions.append({
            'code': 'full_repair_dependencies_unblocked',
            'status': 'applied',
            'node_ids': unblocked_waiting,
            'message': f'已解除 {len(unblocked_waiting)} 个前置依赖已就绪但滞留等待的节点。',
        })
    if unpaused_children:
        extra_actions.append({
            'code': 'full_repair_children_unpaused',
            'status': 'applied',
            'message': f'已解除 {unpaused_children} 个子任务的暂停标记。',
        })
    if assembly_completed:
        extra_actions.append({
            'code': 'full_repair_assembly_synced',
            'status': 'applied',
            'message': '检测到成片已生成完毕，已同步项目状态为已完成。',
        })
    elif assembly_requeued:
        extra_actions.append({
            'code': 'full_repair_assembly_requeued',
            'status': 'applied',
            'message': '已重新提交卡住/失败的自动成片合成任务。',
        })
    if unblocked_other_runs:
        extra_actions.append({
            'code': 'full_repair_cross_run_zombies_cleared',
            'status': 'applied',
            'message': f'已清理其他项目中 {len(unblocked_other_runs)} 个无响应僵尸锁，解除全局队列阻塞。',
        })
    if run_reopened and not extra_actions:
        extra_actions.append({
            'code': 'full_repair_run_reopened',
            'status': 'applied',
            'message': '已重新唤醒停滞的自动制作批次并恢复队列推进。',
        })

    mutated = bool(
        repaired_nodes or adopted_nodes or resumed_nodes or unblocked_waiting
        or unpaused_children or assembly_requeued or assembly_completed
        or run_reopened or refreshed_services or unblocked_other_runs
    )
    return {
        'repaired_nodes': repaired_nodes,
        'adopted_nodes': adopted_nodes,
        'resumed_nodes': resumed_nodes,
        'unblocked_waiting': unblocked_waiting,
        'unblocked_other_runs': unblocked_other_runs,
        'assembly_requeued': assembly_requeued,
        'assembly_completed': assembly_completed,
        'run_reopened': run_reopened,
        'refreshed_services': refreshed_services,
        'actions': extra_actions,
        'blockers': extra_blockers,
        'mutated': mutated,
    }
