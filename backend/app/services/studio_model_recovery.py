"""Restore configured model connections without changing saved routes or prompts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import httpx
from sqlalchemy import select

from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.secrets import redact
from app.core.trusted_service_origins import local_service_client
from app.models.entities import utcnow
from app.services.ai_services import probe_service
from app.services.gpu_gate import _connection_refused
from app.services.h3_context_provider import context_runtime_baseline_stale


VAST_COMFY = 'http://127.0.0.1:18188'
VAST_CONTEXT = 'http://127.0.0.1:8788'
VAST_CONTEXT_WORKER = 'http://127.0.0.1:28188'
VAST_QWEN = 'http://127.0.0.1:8772/v1'
SUPERVISOR_CONFIG = Path('/etc/supervisor/supervisord.conf')
CONTEXT_RUNTIME = '/video_factory/runtime'
VAST_NATIVE_ROOT = PROJECT_ROOT / '.vast/context-runtime'
_PROGRAMS = frozenset({'comfyui', 'ai-video-context-comfy', 'ai-video-context'})
_CONTEXT_NO_SUBMISSION = frozenset({'context_ir_comfy_starting', 'context_ir_comfy_unavailable',
    'context_ir_qwen_starting', 'context_ir_qwen_unavailable', 'context_ir_runtime_required',
    'context_ir_runtime_not_ready', 'context_ir_runtime_stale', 'context_ir_qwen_release_unknown', 'context_ir_gpu_wait_timeout'})
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def _supervisor(action, program):
    if program not in _PROGRAMS or action not in (
            {'status', 'start', 'restart', 'pid'} if program == 'ai-video-context' else {'status', 'start'}):
        raise ValueError('Unsupported model process operation')
    return subprocess.run(['supervisorctl', '-c', str(SUPERVISOR_CONFIG), action, program],
                          cwd=SUPERVISOR_CONFIG.parent, capture_output=True, text=True,
                          timeout=30 if action == 'restart' else 12, check=False)


async def _read_status(client, url):
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


async def _start_if_stopped(client, program, url):
    data, refused = await _read_status(client, url)
    if not refused:
        return data, False
    try:
        result = await asyncio.to_thread(_supervisor, 'status', program)
        fields = result.stdout.split(maxsplit=2)
        if len(fields) < 2 or fields[0] != program:
            return None, False
        state = fields[1]
        started = False
        if state in {'STOPPED', 'EXITED', 'FATAL'}:
            _, still_refused = await _read_status(client, url)
            if still_refused:
                result = await asyncio.to_thread(_supervisor, 'start', program)
                started = result.returncode == 0
        elif state not in {'STARTING', 'BACKOFF', 'RUNNING'}:
            return None, False
        # An existing launch may be between process creation and HTTP readiness.
        for _ in range(56):
            data, refused = await _read_status(client, url)
            if data is not None or not refused:
                return data, started
            await asyncio.sleep(0.5)
        return None, started
    except (OSError, subprocess.SubprocessError):
        return None, False


def _queue_idle(queue):
    return isinstance(queue, dict) and all(queue.get(key) == [] for key in ('queue_running', 'queue_pending'))


async def _refresh_stale_vast_runtime(client, status):
    """Restart only the configured Vast gateway after proving its old runtime is idle."""
    if not context_runtime_baseline_stale(status):
        return False
    import fcntl

    try:
        root = VAST_NATIVE_ROOT.resolve(strict=True)
        config = json.loads((root / 'native-runtime.json').read_bytes())
        manifest = PROJECT_ROOT / 'runtime-manifests/context-ir-native-v1.json'
        checksum = hashlib.sha256(manifest.read_bytes()).hexdigest()
        peer = config.get('peer_comfy_origin')
        if (config.get('profile_id') != 'local-h3ir-qwen4b-v3'
                or config.get('listen', '127.0.0.1') != '127.0.0.1' or config.get('port', 8788) != 8788
                or config.get('comfy_origin') != VAST_CONTEXT_WORKER or peer not in (None, VAST_COMFY)
                or Path(config.get('runtime_root', '')).resolve() != root
                or config.get('source_manifest_sha256') != checksum
                or status['source_baseline']['manifest_sha256'] != checksum):
            return False
        with (root / 'cache/qwen/gpu-inference.lock').open('a+b') as lease:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime, _ = await _read_status(client, VAST_CONTEXT + CONTEXT_RUNTIME)
            if (not context_runtime_baseline_stale(runtime)
                    or runtime['source_baseline']['manifest_sha256'] != checksum
                    or runtime.get('busy') is not False or runtime.get('active_prompts') != []):
                return False
            for origin in (VAST_CONTEXT_WORKER, peer):
                if origin is not None:
                    queue, _ = await _read_status(client, origin + '/queue')
                    if not _queue_idle(queue):
                        return False
            health, refused = await _read_status(client, VAST_QWEN.removesuffix('/v1') + '/health')
            if not refused:
                process = await asyncio.to_thread(_supervisor, 'pid', 'ai-video-context')
                if (process.returncode or not health or health.get('busy') is not False
                        or health.get('loaded') is not False or health.get('inference_phase') != 'idle'
                        or (runtime.get('qwen_health') or {}).get('pid') != health.get('pid')
                        or not any(check.get('kind') == 'qwen_owned_process' and check.get('ok') is True
                                   for check in runtime.get('checks', []) if isinstance(check, dict))):
                    return False
                pid, qwen_pid = int(process.stdout.strip()), health.get('pid')
                if type(qwen_pid) is not int or pid <= 1 or os.getpgid(qwen_pid) != pid:
                    return False
            restart = asyncio.create_task(asyncio.to_thread(_supervisor, 'restart', 'ai-video-context'))
            try:
                result = await asyncio.shield(restart)
            except asyncio.CancelledError:
                # Keep the lease until Supervisor finishes its managed restart.
                await restart
                raise
        if result.returncode:
            return False
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            runtime, _ = await _read_status(client, VAST_CONTEXT + CONTEXT_RUNTIME)
            if (runtime and runtime.get('ready') is True and runtime.get('memory_policy') == 'runtime_owned'
                    and (runtime.get('source_baseline') or {}).get('ok') is True
                    and runtime['source_baseline'].get('manifest_sha256') == checksum):
                return True
            await asyncio.sleep(0.5)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return False


async def _recover_vast(app, services):
    if (sys.platform != 'linux' or app.state.settings.container_mode
            or os.getenv('VIDEO_FACTORY_VAST_GPU_COORDINATION') != '1' or not SUPERVISOR_CONFIG.is_file()):
        return set()
    origins = {service.base_url.rstrip('/') for service in services if service.provider in {'comfyui', 'local_llm'}}
    started = set()
    async with httpx.AsyncClient(timeout=4, trust_env=False, follow_redirects=False) as client:
        if VAST_COMFY in origins:
            _, launched = await _start_if_stopped(client, 'comfyui', VAST_COMFY + '/queue')
            if launched:
                started.add(VAST_COMFY)
        if not origins.intersection({VAST_CONTEXT, VAST_QWEN}):
            return started
        queue, launched = await _start_if_stopped(client, 'ai-video-context-comfy', VAST_CONTEXT_WORKER + '/queue')
        if launched:
            started.add(VAST_CONTEXT)
        if not _queue_idle(queue):
            return started
        status, launched = await _start_if_stopped(client, 'ai-video-context', VAST_CONTEXT + CONTEXT_RUNTIME)
        if launched:
            started.update({VAST_CONTEXT, VAST_QWEN})
        if context_runtime_baseline_stale(status):
            if await _refresh_stale_vast_runtime(client, status):
                started.update({VAST_CONTEXT, VAST_QWEN})
            return started
        if (status and status.get('service') == 'video-factory-native-context-runtime-v1'
                and status.get('busy') is False and status.get('qwen_health') is None):
            try:
                response = await client.post(VAST_CONTEXT + '/video_factory/recover-qwen', timeout=20)
                if response.status_code == 200 and response.json().get('qwen_ready') is True:
                    started.update({VAST_CONTEXT, VAST_QWEN})
            except (httpx.HTTPError, ValueError):
                pass
    return started


async def _probe(service, settings):
    try:
        if service.provider == 'local_llm':
            from app.services.screenplay_runtime import ensure_local_service, factory_base_url, MODEL

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
            runtime = result.get('runtime') or {}
            status = ('recovered' if current.base_url.rstrip('/') in started else 'connected') if connected else (
                'busy' if runtime.get('busy') is True and runtime.get('active_prompts') else result['status'])
            message = result.get('error') or result.get('notice')
            if status == 'busy':
                message = '服务正在执行原任务，完成后会继续处理队列。'
            if not message:
                message = '连接检查通过。' if connected else '请在系统设置中检查模型配置和运行状态。'
            reports.append({'service_id': current.id, 'revision': current.revision, 'name': current.name,
                            'module': current.module, 'model_id': current.default_model, 'status': status,
                            'message': redact(str(message))[:500]})
        session.commit()
        return reports


async def _restart_receipt(origin, profile, job):
    if not isinstance(job.provider_job_id, str) or not _UUID.fullmatch(job.provider_job_id):
        return None
    from app.services.h3_context_provider import context_origin

    try:
        origin = context_origin(origin, profile)
        async with local_service_client([origin], timeout=4) as client:
            response = await client.get(f'{origin}/video_factory/recovery/{job.provider_job_id}')
            response.raise_for_status()
            receipt = response.json()
        if (receipt.get('protocol') == 'native-context-recovery-v1' and receipt.get('job_id') == job.id
                and receipt.get('prompt_id') == job.provider_job_id and job.parameters_json.get('actual_workflow_sha256')
                and receipt.get('graph_sha256') == job.parameters_json['actual_workflow_sha256']
                and type(receipt.get('comfy_started_at')) in {int, float}
                and type(receipt.get('submitted_at')) in {int, float}
                and receipt['comfy_started_at'] > receipt['submitted_at']):
            return receipt
    except (AppError, httpx.HTTPError, ValueError, AttributeError):
        pass
    return None


async def interrupted_context_nodes(session, run, connections):
    """Find only local video nodes whose original work can be safely retried."""
    ready = {(item['service_id'], item['revision']) for item in connections
             if item['status'] in {'connected', 'recovered'}}
    recovered = {}
    for node in run.nodes_json or []:
        if node['kind'] != 'video' or node['status'] not in {'running', 'retrying', 'failed', 'exception', 'unknown'}:
            continue
        frozen = run.snapshot_json.get('h3_context_by_shot', {}).get(node['target_id']) or {}
        service = frozen.get('service') or {}
        if (service.get('provider') != 'comfyui' or service.get('is_cloud') is not False
                or (service.get('service_id'), service.get('service_revision')) not in ready):
            continue
        child_id = node.get('child_id')
        if not child_id:
            if node.get('code') in _CONTEXT_NO_SUBMISSION:
                recovered[node['id']] = None
            continue
        job = session.get(m.GenerationJob, child_id)
        if not job or job.status not in {'queued', 'running', 'failed', 'unknown'} or job.provider != 'comfyui_h3':
            continue
        parameters = job.parameters_json
        child_service = parameters.get('service_snapshot') or {}
        if (job.project_id != run.project_id or job.shot_id != node['target_id']
                or parameters.get('studio_run_id') != run.id or parameters.get('studio_node_id') != node['id']
                or any(child_service.get(key) != service.get(key) for key in ('service_id', 'service_revision', 'base_url', 'model_id'))
                or parameters.get('h3_workflow') != service.get('h3_workflow')
                or not (frozen.get('inputs') or {}).get('fingerprint')
                or parameters.get('context_inputs', {}).get('fingerprint') != frozen['inputs']['fingerprint']):
            continue
        if not job.provider_job_id:
            if job.status == 'failed' and job.error_code in _CONTEXT_NO_SUBMISSION:
                recovered[node['id']] = {'job_id': job.id, 'prompt_id': None}
            continue
        receipt = await _restart_receipt(service['base_url'], service.get('settings') or {}, job)
        if receipt and receipt['outcome'] == 'interrupted_no_output':
            recovered[node['id']] = receipt
    return recovered
