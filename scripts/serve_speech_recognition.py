"""Serve an installed multilingual faster-whisper model over loopback."""
import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import wave
from io import BytesIO
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = 'video-factory-local-asr-v1'
MAX_AUDIO_BYTES = 3_000_000
SAMPLE_RATE = 16_000
LANGUAGE_THRESHOLD = .65


class AudioRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str = Field(min_length=1, max_length=240)
    audio_base64: str = Field(min_length=1, max_length=4 * ((MAX_AUDIO_BYTES + 2) // 3))


def read_pcm(raw):
    with wave.open(BytesIO(raw), 'rb') as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError('expected 16 kHz mono PCM16 WAV')
        expected = 2 * source.getnframes()
        if not 0 < expected <= MAX_AUDIO_BYTES:
            raise ValueError('invalid audio length')
        pcm = source.readframes(source.getnframes())
        if len(pcm) != expected:
            raise ValueError('incomplete WAV')
    return pcm


def create_app(model, identity, *, model_factory=None):
    import numpy as np

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = Lock()

    @app.get('/health')
    def health():
        return {'service': PROTOCOL, 'ready': True, 'busy': lock.locked(), 'offline': True,
                'device': 'cpu', 'compute_type': 'int8', 'loaded': model is not None,
                'load_policy': 'per_request' if model_factory else 'persistent', **identity}

    @app.post('/factory/transcribe')
    def transcribe(payload: AudioRequest):
        if payload.model != identity['model_id']:
            raise HTTPException(409, 'Requested transcription model is not loaded')
        try:
            raw = base64.b64decode(payload.audio_base64, validate=True)
            if len(raw) > MAX_AUDIO_BYTES:
                raise ValueError('audio exceeds limit')
            pcm = read_pcm(raw)
        except (ValueError, EOFError, wave.Error, binascii.Error) as exc:
            raise HTTPException(422, 'Expected a complete 16 kHz mono PCM16 WAV') from exc
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Transcription is already running')
        active_model = None
        try:
            active_model = model if model is not None else model_factory()
            samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
            segments, info = active_model.transcribe(samples, task='transcribe', language=None, beam_size=5,
                                              vad_filter=True, condition_on_previous_text=False)
            rows = [{'start': round(s.start, 3), 'end': round(s.end, 3), 'text': s.text.strip(), 'speaker': 'unknown'}
                    for s in segments if s.text.strip() and s.end > s.start]
        finally:
            if model_factory is not None:
                active_model = None
                import gc
                gc.collect()
            lock.release()
        probability = round(info.language_probability, 4) if rows else None
        language = info.language if probability is not None and probability >= LANGUAGE_THRESHOLD else None
        return {'status': 'transcribed' if rows else 'no_speech', 'text': ' '.join(s['text'] for s in rows),
                'segments': rows, 'language': language, 'language_probability': probability,
                'duration_seconds': len(pcm) / (2 * SAMPLE_RATE),
                'receipt': {**identity, 'service': PROTOCOL, 'offline': True,
                            'input_sha256': hashlib.sha256(raw).hexdigest()}}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8778)
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.threads <= 16:
        parser.error('--threads must be between 1 and 16')
    identity = json.loads((args.model_dir / 'deployment-model.json').read_text(encoding='utf-8'))
    if (not isinstance(identity.get('model_id'), str) or not identity['model_id'].strip()
            or not re.fullmatch(r'[a-f0-9]{40}', str(identity.get('revision')))):
        raise ValueError('Installed model receipt is invalid')
    with (args.model_dir / 'model.bin').open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != identity.get('model_sha256'):
            raise ValueError('Installed transcription weights do not match the receipt')
    os.environ['HF_HUB_OFFLINE'] = '1'
    from faster_whisper import WhisperModel
    import uvicorn

    def model_factory():
        return WhisperModel(str(args.model_dir.resolve()), device='cpu', compute_type='int8',
                            cpu_threads=args.threads, local_files_only=True)
    public_identity = {key: identity[key] for key in ('model_id', 'revision', 'model_sha256')}
    uvicorn.run(create_app(None, public_identity, model_factory=model_factory), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
