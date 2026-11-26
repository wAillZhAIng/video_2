"""Recover direct local model connections without altering frozen jobs.

The repair action only starts the configured native ComfyUI service when it is
stopped, then probes registered services. It never starts a prompt compiler,
rewrites a script, or resubmits a task whose provider acceptance is unknown.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import httpx
from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.secrets import redact
from app.models.entities import utcnow
from app.services.ai_services import probe_service
from app.services.gpu_gate import _connection_refused


VAST_COMFY = 'http://127.0.0.1:18188'
SUPERVISOR_CONFIG = Path('/etc/supervisor/supervisord.conf')
_PROGRAMS = frozenset({'comfyui'})
_NO_SUBMISSION = frozenset({
    'provider_unavailable', 'local_gpu_busy', 'native_h3_not_ready',
    'comfyui_unavailable', 'comfyui_not_ready', 'native_workflow_unavailable',
})


def _supervisor(action: str, program: str):
    if program not in _PROGRAMS or action not in {'status', 'start'}:
        raise ValueError('Unsupported model process operation')
    return subprocess.run(
        ['supervisorctl', '-c', str(SUPERVISOR_CONFIG), action, program],
        cwd=SUPERVISOR_CONFIG.parent,
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )


async def _read_status(client: httpx.AsyncClient, url: str):
    try:
        response = await client.get(url)
        response.raise_for_status()
        value = response.json()
        if isinstance(value, dict):
            return value, False
    except httpx.ConnectError as error:
        return None, _connection_refused(error)
    except (httpx.HTTPError, ValueError):
        pass
    return None, False


async def _start_comfy_if_stopped(client: httpx.AsyncClient):
    status, refused = await _read_status(client, VAST_COMFY + '/queue')
    if not refused:
        return status, False
    try:
        result = await asyncio.to_thread(_supervisor, 'status', 'comfyui')
        fields = result.stdout.split(maxsplit=2)
        if len(fields) < 2 or fields[0] != 'comfyui':
            return None, False
        if fields[1] in {'STOPPED', 'EXITED', 'FATAL'}:
            result = await asyncio.to_thread(_supervisor, 'start', 'comfyui')
            started = result.returncode == 0
        elif fields[1] in {'STARTING', 'BACKOFF', 'RUNNING'}:
            started = False
        else:
            return None, False
        for _ in range(56):
            status, still_refused = await _read_status(client, VAST_COMFY + '/queue')
            if status is not None or not still_refused:
                return status, started
            await asyncio.sleep(.5)
    except (OSError, subprocess.SubprocessError):
        pass
    return None, False


async def _recover_vast(app, services):
    if (sys.platform != 'linux' or app.state.settings.container_mode
            or os.getenv('VIDEO_FACTORY_VAST_GPU_COORDINATION') != '1'
            or not SUPERVISOR_CONFIG.is_file()):
        return set()
    origins = {service.base_url.rstrip('/') for service in services if service.provider == 'comfyui'}
    if VAST_COMFY not in origins:
        return set()
    async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
        _, started = await _start_comfy_if_stopped(client)
    return {VAST_COMFY} if started else set()


async def _probe(service, settings):
    try:
        if service.provider == 'local_llm':
            # Local language services remain optional screenplay tools. They
            # are never a prerequisite for native H3 video generation.
            from app.services.screenplay_runtime import MODEL, ensure_local_service, factory_base_url
            base = factory_base_url(required=False)
            if base and service.base_url.rstrip('/') == base and service.default_model == MODEL:
                await ensure_local_service({'provider': service.provider, 'base_url': base, 'model_id': MODEL})
        result = await probe_service(service, settings)
        if (service.provider == 'local_llm' and result['status'] == 'connected' and service.default_model
                and not any(item['id'] == service.default_model for item in result['models'])):
            result.update(status='reachable', error='服务有响应，但模型列表中没有所选模型。')
        return result
    except AppError as error:
        return {'status': 'failed', 'diagnostic_code': error.code, 'error': error.message}
    except (OSError, ValueError, TypeError, AttributeError, httpx.HTTPError):
        return {'status': 'failed', 'error': '未能完成连接检查，请查看系统设置。'}


async def recover_model_connections(app, session):
    settings = app.state.settings
    if settings.h3_demo_mode:
        return []
    lock = getattr(app.state, 'model_connection_recovery_lock', None)
    if lock is None:
        lock = app.state.model_connection_recovery_lock = asyncio.Lock()
    async with lock:
        services = list(session.scalars(select(m.AIService).where(m.AIService.enabled.is_(True))
                                       .order_by(m.AIService.module, m.AIService.name, m.AIService.id)))
        for service in services:
            session.expunge(service)
        session.rollback()
        started = await _recover_vast(app, services)
        concurrency = asyncio.Semaphore(4)

        async def probe(service):
            async with concurrency:
                try:
                    return await asyncio.wait_for(_probe(service, settings), timeout=25)
                except asyncio.TimeoutError:
                    return {'status': 'failed', 'diagnostic_code': 'timeout', 'error': '连接检查超时。'}

        results = await asyncio.gather(*(probe(service) for service in services))
        reports = []
        for original, result in zip(services, results):
            current = session.get(m.AIService, original.id, populate_existing=True)
            if not current or not current.enabled or current.revision != original.revision:
                continue
            current.last_test_json = result
            connected = result['status'] == 'connected'
            if connected:
                current.last_success_at = utcnow()
                current.capabilities_json = {**(current.capabilities_json or {}), **{
                    key: value for key, value in result.get('capabilities', {}).items()
                    if value.get('state') in {'detected', 'verified'}
                    and (current.capabilities_json or {}).get(key, {}).get('state') != 'verified'}}
            status = ('recovered' if current.base_url.rstrip('/') in started else 'connected') if connected else result['status']
            message = result.get('error') or result.get('notice')
            if not message:
                message = '连接检查通过。' if connected else '请在系统设置中检查模型配置和运行状态。'
            reports.append({'service_id': current.id, 'revision': current.revision, 'name': current.name,
                            'module': current.module, 'model_id': current.default_model, 'status': status,
                            'message': redact(str(message))[:500]})
        session.commit()
        return reports


def _video_snapshot(run, shot_id):
    route = (run.snapshot_json.get('routes_by_shot', {}).get(shot_id, {}).get('video_generation')
             or run.snapshot_json.get('routes', {}).get('video_generation') or {})
    return (route.get('selected') or {}).get('service_snapshot') or run.snapshot_json.get('services', {}).get('video', {})


async def interrupted_native_nodes(session, run, connections):
    """Return only tasks proven not to have reached a provider.

    A prompt id or unknown provider status is deliberately excluded: retrying
    it could create a duplicate paid or GPU-bound generation.
    """
    ready = {(item['service_id'], item['revision']) for item in connections
             if item['status'] in {'connected', 'recovered'}}
    recovered = {}
    for node in run.nodes_json or []:
        if node.get('kind') != 'video' or node.get('status') not in {'running', 'retrying', 'failed', 'exception'}:
            continue
        service = _video_snapshot(run, node.get('target_id'))
        if (service.get('provider') != 'comfyui' or service.get('is_cloud') is not False
                or (service.get('service_id'), service.get('service_revision')) not in ready):
            continue
        child_id = node.get('child_id')
        if not child_id:
            if node.get('code') in _NO_SUBMISSION:
                recovered[node['id']] = None
            continue
        job = session.get(m.GenerationJob, child_id)
        if not job or job.provider != 'comfyui_h3' or job.provider_job_id:
            continue
        parameters = job.parameters_json or {}
        child_service = parameters.get('service_snapshot') or {}
        if (job.project_id != run.project_id or job.shot_id != node.get('target_id')
                or parameters.get('studio_run_id') != run.id or parameters.get('studio_node_id') != node.get('id')
                or any(child_service.get(key) != service.get(key) for key in ('service_id', 'service_revision', 'base_url', 'model_id'))):
            continue
        if job.status == 'failed' and job.error_code in _NO_SUBMISSION:
            recovered[node['id']] = {'job_id': job.id, 'prompt_id': None}
    return recovered
