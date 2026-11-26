"""Read administrator-installed local inference acceptance receipts."""
import hashlib
import json
import os
import re
from pathlib import Path

from app.core.errors import AppError
from app.services.ai_services import service_snapshot
from app.services.providers import native_ref2va_workflow_path
from app.services.runtime_registry import file_sha


def binding(settings, service, model, slot):
    snapshot = service_snapshot(service, model)
    snapshot.pop('captured_at')
    snapshot.pop('name')
    data = {'slot': slot, 'service': snapshot}
    if service.provider == 'comfyui' and slot in {'video_generation', 'motion_driving'}:
        data['workflows'] = [
            {'steps': steps, 'sha256': file_sha(native_ref2va_workflow_path({'steps': steps}))}
            for steps in (4, 20, 30)
        ]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def accepted_evidence(settings, service, model, slot):
    configured = os.getenv('VIDEO_FACTORY_SERVICE_ACCEPTANCE_DIR')
    if not configured or not re.fullmatch(r'[A-Za-z0-9_-]+', service.id):
        return None
    if service_snapshot(service, model)['is_cloud']:
        return None
    try:
        root = Path(configured).resolve(strict=True)
        receipt = (root / (service.id + '.json')).resolve(strict=True)
        if not receipt.is_relative_to(root) or receipt.stat().st_size > 256 * 1024:
            return None
        document = json.loads(receipt.read_text(encoding='utf-8'))
        if document['schema_version'] != 1:
            return None
        fingerprint = binding(settings, service, model, slot)
        for entry in document['entries']:
            if (entry['slot'], entry['model_id'], entry['binding']) != (slot, model, fingerprint):
                continue
            relative = Path(entry['relative_path'])
            if relative.is_absolute() or '..' in relative.parts:
                continue
            artifact = (root / relative).resolve(strict=True)
            proof = entry['proof']
            if (artifact.is_relative_to(root) and isinstance(proof, dict) and proof.get('kind')
                    and file_sha(artifact) == entry['sha256']):
                return {**proof, 'source': 'administrator_verified_local_acceptance',
                        'acceptance_artifact_sha256': entry['sha256']}
    except (OSError, ValueError, TypeError, KeyError, AppError):
        return None
    return None
