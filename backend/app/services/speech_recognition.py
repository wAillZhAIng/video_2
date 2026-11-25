"""Use the selected local speech-recognition service on a verified audio track."""
import base64
import hashlib
import re

import httpx
from sqlalchemy import select

from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models import AIService

MODULE = 'speech_recognition'
PROVIDER = 'local_asr'
PROTOCOL = 'video-factory-local-asr-v1'
DEFAULT_BASE_URL = 'http://127.0.0.1:8778'
DEFAULT_MODEL = 'Systran/faster-whisper-small'
MAX_AUDIO_BYTES = 3_000_000


def default_snapshot(session):
    from app.services.ai_services import service_snapshot

    service = session.scalar(select(AIService).where(AIService.module == MODULE, AIService.provider == PROVIDER,
                                                      AIService.enabled, AIService.is_default))
    if not service:
        return None
    return {**service_snapshot(service), 'model_sha256': service.last_test_json.get('model_sha256')}


async def service_health(settings, snapshot):
    from app.services.ai_services import service_request, validate_configuration

    validate_configuration(MODULE, PROVIDER, snapshot['base_url'], snapshot.get('settings', {}))
    response = await service_request(settings, snapshot, 'GET', '/health', client_timeout=5.0)
    response.raise_for_status()
    health = response.json()
    if (not isinstance(health, dict) or health.get('service') != PROTOCOL or health.get('offline') is not True
            or health.get('ready') is not True or health.get('model_id') != snapshot.get('model_id')
            or not re.fullmatch(r'[0-9a-f]{64}', str(health.get('model_sha256')))
            or snapshot.get('model_sha256') and health['model_sha256'] != snapshot['model_sha256']):
        raise AppError(409, 'speech_recognition_not_ready', '语音识别服务的模型、权重或就绪状态与配置不符')
    return health


def _verified_result(result, source, snapshot, health):
    receipt = result.get('receipt') if isinstance(result, dict) else None
    if (not isinstance(receipt, dict) or receipt.get('service') != PROTOCOL or receipt.get('offline') is not True
            or receipt.get('input_sha256') != source['audio_sha256'] or receipt.get('model_id') != snapshot['model_id']
            or receipt.get('model_sha256') != health['model_sha256'] or receipt.get('revision') != health.get('revision')):
        raise AppError(502, 'speech_recognition_receipt', '原音轨的语音识别回执与输入或模型不符')
    language = result.get('language')
    probability = result.get('language_probability')
    segments = result.get('segments')
    if (result.get('status') not in {'transcribed', 'no_speech'} or not isinstance(segments, list)
            or (language is not None and (not isinstance(language, str) or not re.fullmatch(r'[a-z]{2,3}', language)))
            or (probability is not None and (type(probability) not in (int, float) or not 0 <= probability <= 1))):
        raise AppError(502, 'speech_recognition_result', '语音识别结果格式无效')
    rows = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise AppError(502, 'speech_recognition_result', '语音识别时间段格式无效')
        start, end, text = (segment.get(key) for key in ('start', 'end', 'text'))
        if (type(start) not in (int, float) or type(end) not in (int, float) or not 0 <= start < end <= 96
                or not isinstance(text, str) or not text.strip()):
            raise AppError(502, 'speech_recognition_result', '语音识别时间段格式无效')
        rows.append({'start': start, 'end': end, 'text': text, 'speaker': 'unknown'})
    if bool(rows) != (result['status'] == 'transcribed') or not rows and language:
        raise AppError(502, 'speech_recognition_result', '语音识别状态与实际转写内容不符')
    evidence = {**receipt, 'service_id': snapshot['service_id'], 'service_revision': snapshot['service_revision']}
    if not rows:
        return {**source, 'audio_transcription': 'no_speech_detected', 'transcription_receipt': evidence}
    return {**source, 'audio_transcription': 'transcribed_unverified', 'transcription_receipt': evidence,
            'transcript': {'text': ' '.join(row['text'] for row in rows), 'segments': rows, 'language': language,
                           'language_probability': probability, 'speaker': 'unknown', 'needs_review': True}}


async def transcribe_audio(settings, snapshot, source):
    from app.services.ai_services import service_request

    path = safe_media_path(settings.media_root, source['audio_relative_path'], must_exist=True)
    with path.open('rb') as stream:
        data = stream.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES or hashlib.sha256(data).hexdigest() != source.get('audio_sha256'):
        raise AppError(409, 'screenplay_audio_source_changed', '原音轨大小或 SHA256 与提取记录不符')
    health = await service_health(settings, snapshot)
    timeout = httpx.Timeout(snapshot.get('settings', {}).get('timeout') or 180.0, connect=5.0)
    response = await service_request(settings, snapshot, 'POST', '/factory/transcribe', client_timeout=timeout,
                                     json={'model': snapshot['model_id'], 'audio_base64': base64.b64encode(data).decode('ascii')})
    response.raise_for_status()
    return _verified_result(response.json(), source, snapshot, health)
