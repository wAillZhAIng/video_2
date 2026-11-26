"""Local ACE-Step generation and FFmpeg soundtrack mixing for an assembly."""

import asyncio
import hashlib
import json
import logging
import math
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.trusted_service_origins import is_loopback_service
from app.models import AIService, AssemblyRender, ManagedFile
from app.services.ai_services import resolve_service, service_request
from app.services.audio_mix import probe_audio
from app.services.gpu_gate import _connection_refused, gpu_submission


MODULE = 'background_music'
MODEL = 'ace_step_1.5_turbo_aio.safetensors'
GAIN_DB = -17
GRAPH_VERSION = 1
_NODES = ('CheckpointLoaderSimple', 'TextEncodeAceStepAudio1.5', 'EmptyAceStep1.5LatentAudio',
          'ModelSamplingAuraFlow', 'ConditioningZeroOut', 'KSampler', 'VAEDecodeAudio', 'SaveAudio')
_DEFAULT_TASK_TIMEOUT_SECONDS = 1800
_POLL_TIMEOUT_SECONDS = 20
logger = logging.getLogger(__name__)


def validate_endpoint(url):
    try:
        target = urlsplit(url)
        valid = (target.scheme in {'http', 'https'} and is_loopback_service(url) and target.port
                 and target.path in {'', '/'} and target.username is None and target.password is None
                 and not (target.query or target.fragment))
    except ValueError:
        valid = False
    if not valid:
        raise AppError(422, 'music_local_endpoint_required', '背景音乐请填写带端口的本机 ComfyUI 地址')


def check_model(model):
    if model != MODEL:
        raise AppError(422, 'music_model_required', f'背景音乐需要已安装的 ACE-Step 1.5 Turbo AIO 模型 {MODEL}')


def freeze_service(session, project_id):
    try:
        _, snapshot = resolve_service(session, MODULE, project_id=project_id)
    except AppError:
        return None
    validate_endpoint(snapshot['base_url'])
    check_model(snapshot['model_id'])
    return snapshot


async def _request(settings, snapshot, method, route, **kwargs):
    validate_endpoint(snapshot['base_url'])
    return await service_request(settings, snapshot, method, route, client_timeout=20, **kwargs)


async def _json(settings, snapshot, route):
    reply = await _request(settings, snapshot, 'GET', route)
    reply.raise_for_status()
    result = reply.json()
    if not isinstance(result, dict):
        raise AppError(502, 'music_invalid_comfy_response', '本地配乐服务返回了无法识别的数据')
    return result


async def probe_model(settings, snapshot):
    check_model(snapshot['model_id'])
    nodes = await _json(settings, snapshot, '/object_info')
    checkpoints = (nodes.get('CheckpointLoaderSimple', {}).get('input', {}).get('required', {})
                   .get('ckpt_name', [[]])[0])
    if not set(_NODES) <= nodes.keys() or snapshot['model_id'] not in checkpoints:
        raise AppError(409, 'music_model_unavailable', 'ComfyUI 在线，ACE-Step 节点或 Turbo 权重未就绪')
    return {'state': 'detected', 'model_id': snapshot['model_id'],
            'source': '本机 ComfyUI 的 ACE-Step 节点与权重检查'}


def workflow(cue, seed, prefix):
    bpm_match = re.search(r'\b(\d{2,3})(?:\.\d+)?\s*BPM\b', cue['description'], re.I)
    bpm = max(10, min(300, int(bpm_match[1]))) if bpm_match else 100
    length = max(10, math.ceil(cue['duration'] * 25) / 25)
    phases = ' '.join(f"{phase['start']:g}–{phase['end']:g}s: {phase['text']}"
                      for phase in cue['directions'])
    caption = 'Instrumental underscore, no vocals, no narration or sound effects. ' + cue['description']
    if phases:
        caption += ' Arrangement cues: ' + phases

    def node(index, **inputs):
        return {'class_type': _NODES[index], 'inputs': inputs}

    # Parameters match the native ComfyUI ACE-Step 1.5 Turbo template; use
    # direct prompt conditioning for short instrumental cues.
    return {
        '1': node(0, ckpt_name=MODEL),
        '2': node(1, clip=['1', 1], tags=caption, lyrics='[Instrumental]', seed=seed, bpm=bpm,
                  duration=length, timesignature='4', language='unknown', keyscale='C major',
                  generate_audio_codes=False, cfg_scale=2, temperature=0.85, top_p=0.9, top_k=0, min_p=0),
        '3': node(2, seconds=length, batch_size=1),
        '4': node(3, model=['1', 0], shift=3),
        '5': node(4, conditioning=['2', 0]),
        '6': node(5, model=['4', 0], seed=seed, steps=8, cfg=1, sampler_name='euler',
                  scheduler='simple', denoise=1, positive=['2', 0], negative=['5', 0], latent_image=['3', 0]),
        '7': node(6, samples=['6', 0], vae=['1', 2]),
        '8': node(7, audio=['7', 0], filename_prefix=prefix),
    }


def _measure(path, settings, duration):
    info = probe_audio(path, settings)
    length = float(info['format']['duration'])
    if length + 0.045 < duration:
        raise AppError(502, 'music_output_too_short', '模型返回的背景音乐短于剧本区间')
    decoded = subprocess.run([settings.ffmpeg_path, '-hide_banner', '-nostdin', '-xerror',
                              '-i', str(path), '-map', '0:a:0', '-af', 'volumedetect', '-f', 'null', '-'],
                             capture_output=True, timeout=120)
    peak = re.search(rb'max_volume:\s*(-?[\d.]+) dB', decoded.stderr)
    if decoded.returncode or not peak or float(peak[1]) < -60:
        raise AppError(502, 'music_output_invalid', '背景音乐未通过完整解码与非静音检查')
    return length


def _sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


async def _idle(settings, snapshot):
    own = snapshot['base_url'].rstrip('/')
    origins = dict.fromkeys([own, *snapshot.get('gpu_peer_origins', [])])
    for origin in origins:
        try:
            queue = await _json(settings, {**snapshot, 'base_url': origin}, '/queue')
        except httpx.ConnectError as exc:
            if origin != own and _connection_refused(exc):
                continue
            raise
        except httpx.HTTPStatusError as exc:
            raise AppError(502, 'music_queue_check_failed',
                           f'无法核对已配置的本机 GPU 队列（HTTP {exc.response.status_code}）') from exc
        if not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
            raise AppError(502, 'music_queue_unknown', '无法确认本机 GPU 队列状态')
        if queue['queue_running'] or queue['queue_pending']:
            return False
    return True


def _configured_gpu_peers(session, settings):
    """Read actual enabled Compute origins, independent of legacy port defaults."""
    services = session.scalars(select(AIService).where(
        AIService.enabled.is_(True), AIService.module == 'video_generation', AIService.provider == 'comfyui'))
    candidates = [settings.comfyui_base_url]
    candidates.extend(row.base_url for row in services if row.settings_json.get('h3_context_ir') is True)
    peers = []
    for origin in dict.fromkeys(candidates):
        try:
            validate_endpoint(origin)
        except AppError:
            continue
        peers.append(origin.rstrip('/'))
    return peers


async def _await_audio(settings, service, intent, duration):
    prompt_id = intent['prompt_id']
    try:
        timeout = float(service['settings'].get('timeout', _DEFAULT_TASK_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TASK_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = _DEFAULT_TASK_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    missing = 0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            record = (await asyncio.wait_for(
                _json(settings, service, '/history/' + prompt_id),
                timeout=min(_POLL_TIMEOUT_SECONDS, remaining),
            )).get(prompt_id)
        except TimeoutError:
            break
        if record:
            outputs = record.get('outputs', {}).get('8', {}).get('audio', [])
            if outputs:
                item = outputs[0]
                if (item.get('type') != 'output' or not str(item.get('filename', '')).lower().endswith('.flac')
                        or Path(item['filename']).name != item['filename']
                        or not str(item['filename']).startswith(intent['prefix'].rsplit('/', 1)[-1] + '_')
                        or item.get('subfolder') != 'video_factory_music'):
                    raise AppError(502, 'music_artifact_mismatch', '本机返回的配乐文件与本次生成任务不匹配')
                return item
            status = record.get('status', {})
            if status.get('status_str') == 'error' or status.get('completed'):
                messages = status.get('messages', [])
                error = next((values.get('exception_message', '') for kind, values in messages
                              if kind == 'execution_error' and isinstance(values, dict)), '')
                raise AppError(502, 'music_inference_failed', '本地背景音乐生成失败。' + error[:480])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            queue = await asyncio.wait_for(
                _json(settings, service, '/queue'), timeout=min(_POLL_TIMEOUT_SECONDS, remaining))
        except TimeoutError:
            break
        running = [*queue.get('queue_running', []), *queue.get('queue_pending', [])]
        missing = 0 if any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id for row in running) else missing + 1
        if missing >= 12:
            break
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
    raise AppError(502, 'music_submission_unknown', '背景音乐任务结果待核对；重试会查询原本地任务')


async def generate_track(settings, project_id, cue, service):
    """Recover the original queue ID if the upstream response was interrupted."""
    duration = cue['duration']
    if not 0 < duration <= 600:
        raise AppError(422, 'music_duration_unsupported', '单个配乐区间须不超过 600 秒，请在剧本中划分配乐区间')
    spec = {key: cue[key] for key in ('description', 'duration', 'directions')}
    content = [GRAPH_VERSION, service['base_url'], service['model_id'], spec]
    key = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    relative = f'assemblies/{project_id}/background_music/{key}.flac'
    path = safe_media_path(settings.media_root, relative)
    receipt_path = path.with_suffix('.json')
    if receipt_path.exists():
        intent = json.loads(receipt_path.read_text(encoding='utf-8'))
        if path.is_file() and intent.get('sha256') == await asyncio.to_thread(_sha256, path):
            return intent
    else:
        intent = None
    if not intent:
        if not await _idle(settings, service):
            return None
        await probe_model(settings, service)
        path.parent.mkdir(parents=True, exist_ok=True)
        prompt_id = str(uuid4())
        prefix = 'video_factory_music/' + prompt_id
        intent = {'prompt_id': prompt_id, 'prefix': prefix, 'model_id': service['model_id']}
        _write_json(receipt_path, intent)
        graph = workflow(cue, int(key[:12], 16), prefix)
        try:
            reply = await _request(settings, service, 'POST', '/prompt',
                                   json={'prompt': graph, 'prompt_id': prompt_id, 'client_id': prompt_id})
        except httpx.TransportError:
            pass  # Consult the submitted ID before considering another attempt.
        else:
            if 400 <= reply.status_code < 500:
                receipt_path.unlink()
                raise AppError(502, 'music_workflow_rejected', '本机拒绝了 ACE-Step 工作流。' + reply.text[:480])
            if reply.is_success:
                returned = reply.json().get('prompt_id')
                if isinstance(returned, str) and re.fullmatch(r'[a-f0-9-]{36}', returned):
                    intent['prompt_id'] = returned
                    _write_json(receipt_path, intent)
    try:
        item = await _await_audio(settings, service, intent, duration)
    except AppError as exc:
        if exc.code == 'music_inference_failed':
            receipt_path.unlink(missing_ok=True)
        raise
    reply = await _request(settings, service, 'GET', '/view', params={
        key: item[key] for key in ('filename', 'subfolder', 'type')})
    reply.raise_for_status()
    if not reply.content.startswith(b'fLaC') or len(reply.content) > 256 * 1024**2:
        raise AppError(502, 'music_output_invalid', '本地配乐服务未返回有效的 FLAC 音频')
    temporary = path.with_suffix('.partial.flac')
    try:
        temporary.write_bytes(reply.content)
        length = await asyncio.to_thread(_measure, temporary, settings, duration)
        sha256 = await asyncio.to_thread(_sha256, temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {**intent, 'relative_path': relative, 'duration_seconds': length, 'sha256': sha256}
    _write_json(receipt_path, receipt)
    return receipt


def register_track(session, render_id, receipt):
    path = receipt['relative_path']
    item = session.scalar(select(ManagedFile).where(ManagedFile.relative_path == path))
    if not item:
        item = ManagedFile(relative_path=path)
        session.add(item)
    owner = {'table': 'assembly_renders', 'id': render_id}
    owners = item.ownership_json or []
    item.ownership_json = owners if owner in owners else [*owners, owner]
    item.state = 'active'
    item.metadata_json = {'kind': MODULE, 'verified_owned': True, 'sha256': receipt['sha256']}


@gpu_submission
async def prepare_render(settings, session_factory, identifier, snapshot):
    service = snapshot.get('music_service_snapshot')
    try:
        if not service:
            raise AppError(409, 'music_service_not_configured', '剧本要求背景音乐。请在系统设置启用本地背景音乐服务，再重新提交合成。')
        with session_factory() as session:
            current = session.get(AIService, service['service_id'])
            if not current or not current.enabled or current.revision != service['service_revision']:
                raise AppError(409, 'music_service_changed', '背景音乐服务配置已变更，请重新提交合成')
            service = {**service, 'gpu_peer_origins': _configured_gpu_peers(session, settings)}
        sounds = []
        for cue in snapshot['background_music']:
            with session_factory() as session:
                row = session.get(AssemblyRender, identifier)
                if not row or row.status != 'queued':
                    return None
                row.metadata_json = {'progress': 0, 'stage': 'background_music',
                                     'music_ready': len(sounds), 'music_total': len(snapshot['background_music'])}
                session.commit()
            receipt = await generate_track(settings, snapshot['project_id'], cue, service)
            if not receipt:
                return None
            sounds.append(receipt)
            with session_factory() as session:
                row = session.get(AssemblyRender, identifier)
                if row:
                    register_track(session, identifier, receipt)
                    session.commit()
        try:
            if await _idle(settings, service):
                await _request(settings, service, 'POST', '/free', json={'unload_models': True, 'free_memory': True})
        except (AppError, httpx.HTTPError, ValueError):
            logger.info('ACE-Step GPU release will be checked by the next local task')
        with session_factory() as session:
            current = session.get(AIService, service['service_id'])
            if current and current.enabled and current.revision == service['service_revision']:
                current.capabilities_json = {**current.capabilities_json, MODULE: {
                    'state': 'verified', 'model_id': service['model_id'],
                    'source': '本地生成并完整解码的配乐', 'render_id': identifier,
                }}
                session.commit()
        return sounds
    except Exception as exc:
        if not isinstance(exc, AppError):
            logger.warning('Assembly music stage failed: %s', type(exc).__name__)
        with session_factory() as session:
            row = session.get(AssemblyRender, identifier)
            if row and row.status == 'queued':
                row.status = 'failed'
                row.error_message = exc.message if isinstance(exc, AppError) else '本地背景音乐服务连接或音频读取失败，请核对系统设置后重试'
                session.commit()
        return None


def _tempo_filters(tempo):
    if not math.isfinite(tempo) or tempo <= 0:
        raise AppError(422, 'music_invalid_timing', '无法计算配乐的剪辑速度')
    filters = []
    while tempo > 2:
        filters.append('atempo=2')
        tempo /= 2
    while tempo < 0.5:
        filters.append('atempo=0.5')
        tempo *= 2
    if abs(tempo - 1) > 0.0001:
        filters.append(f'atempo={tempo:.8f}')
    return filters


def clip_music_plan(plan, start, duration):
    """Crop a final timeline's placements into a transition preview."""
    output = []
    for cue in plan:
        placements = []
        for p in cue['placements']:
            left, right = max(start, p['start']), min(start + duration, p['end'])
            if right - left > 0.001:
                placements.append({**p, 'start': left-start, 'end': right-start,
                                   'offset': p['offset'] + (left-p['start']) * p['tempo']})
        if placements:
            output.append({**cue, 'placements': placements})
    return output


def append_mix(settings, snapshot, command, filters, base_audio, sounds):
    tracks = snapshot.get('background_music', [])
    if not tracks:
        return base_audio
    if len(tracks) != len(sounds):
        raise AppError(409, 'music_assets_missing', '本次时间线的背景音乐素材尚未生成')
    gain = 10 ** (snapshot['music_service_snapshot']['settings'].get('music_gain_db', GAIN_DB) / 20)
    mixed = [f'[{base_audio}]']
    for index, (cue, sound) in enumerate(zip(tracks, sounds)):
        path = safe_media_path(settings.media_root, sound['relative_path'], must_exist=True)
        if _sha256(path) != sound['sha256']:
            raise AppError(409, 'music_asset_changed', '背景音乐文件发生变更，请重新合成')
        command.extend(['-i', str(path)])
        source = len(snapshot['clips']) + index
        placements = sorted(cue['placements'], key=lambda p: p['start'])
        labels = [f'music{index}_{j}' for j in range(len(placements))]
        if len(labels) > 1:
            filters.append(f'[{source}:a:0]asplit={len(labels)}' + ''.join(f'[{label}]' for label in labels))
        else:
            labels = [f'{source}:a:0']
        for j, (place, label) in enumerate(zip(placements, labels)):
            duration = place['end'] - place['start']
            delay = round(place['start'] * 48000)
            previous = placements[j - 1] if j else None
            following = placements[j + 1] if j + 1 < len(placements) else None
            continuous_before = previous and abs(previous['end'] - place['start']) < 0.002
            continuous_after = following and abs(following['start'] - place['end']) < 0.002
            chain = [f"atrim=start={place['offset']:.8f}:duration={duration*place['tempo']:.8f}",
                     'asetpts=PTS-STARTPTS', 'aresample=48000', 'aformat=sample_fmts=fltp:channel_layouts=stereo',
                     *_tempo_filters(place['tempo']), 'apad', f'atrim=duration={duration:.8f}',
                     f"volume={gain*place['volume']:.8f}"]
            if not continuous_before:
                chain.append(f'afade=t=in:d={min(0.22, duration/3):.6f}')
            if not continuous_after:
                fade = min(0.75, duration/3)
                chain.append(f'afade=t=out:st={duration-fade:.6f}:d={fade:.6f}')
            out = f'bed{index}_{j}'
            filters.append(f'[{label}]' + ','.join([*chain, f'adelay={delay}S:all=1']) + f'[{out}]')
            mixed.append(f'[{out}]')
    filters.append(''.join(mixed) + f'amix=inputs={len(mixed)}:duration=first:normalize=0:dropout_transition=0,'
                   'alimiter=limit=0.97:level=0:latency=1[music_mix]')
    return 'music_mix'
