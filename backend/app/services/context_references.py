"""Source-preserving reference records shared with the local compiler contract."""
from pathlib import Path
import json
import re
import time
import uuid


REFERENCE_SCHEMA_VERSION = 'context-references-v1'


def request_trace_id(request):
    supplied = request.headers.get('x-reference-trace-id', '')
    return supplied if re.fullmatch(r'[A-Za-z0-9_-]{1,96}', supplied) else uuid.uuid4().hex


def audit_reference_stage(session, trace_id, stage, references, *, object_id, error=None, **details):
    """Connect UI/API selections to immutable image IDs and later job traces."""
    from app.models import AuditEvent
    payload = {'trace_id': trace_id, 'stage': stage, 'image_count': len(references),
               'references': references, **details}
    if error is not None:
        payload.update(error_type=type(error).__name__, error_code=getattr(error, 'code', None))
    session.add(AuditEvent(object_type='reference_input', object_id=object_id,
                           action='reference_input_trace', details_json=payload))


def reference_records(references):
    records = []
    identifiers = set()
    for index, ref in enumerate(references, 1):
        sha = ref.get('sha256')
        image_id = str(ref.get('image_id') or ref.get('asset_id') or f'image-{sha}')
        if image_id in identifiers:
            image_id += f'-occurrence-{index}'
        identifiers.add(image_id)
        records.append({
            'image_id': image_id, 'source_path': ref.get('source_path') or ref.get('relative_path') or ref.get('absolute_path'),
            'sha256': sha, 'product_id': ref.get('product_id'),
            'subject_id': ref.get('subject_id') or ref.get('actor_id') or ref.get('character_id'),
            'reference_role': ref.get('reference_role') or ref.get('role') or ref.get('asset_type') or 'subject',
            'width': ref.get('width'), 'height': ref.get('height'),
            'source_type': ref.get('source_type') or ref.get('source') or 'uploaded_photo',
            'image_description': ref.get('image_description') or ref.get('image_notes') or '',
            'analysis_status': 'pending', 'native_label': f'<Picture {index}>',
        })
    return records


def trace_references(directory, trace_id, stage, references, *, error=None, **details):
    """Append only identifiers and metadata, never inline image payloads or secrets."""
    record = {'trace_id': trace_id, 'stage': stage, 'at_unix': time.time(),
              'image_count': len(references), 'references': references, **details}
    if error is not None:
        record.update(error_type=type(error).__name__, error=str(error))
    path = Path(directory) / 'reference-trace.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
