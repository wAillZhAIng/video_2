"""Durable audit records for explicit asset selections.

This module records only immutable asset identifiers and metadata.  It never
interprets an image or video, and it is independent of any model pipeline.
"""
from __future__ import annotations

import re
import uuid


def request_trace_id(request) -> str:
    supplied = request.headers.get('x-reference-trace-id', '')
    return supplied if re.fullmatch(r'[A-Za-z0-9_-]{1,96}', supplied) else uuid.uuid4().hex


def audit_reference_stage(session, trace_id: str, stage: str, references: list[dict], *,
                          object_id: str, error=None, **details) -> None:
    """Attach a source-preserving audit event without storing media bytes."""
    from app.models import AuditEvent

    payload = {
        'trace_id': trace_id,
        'stage': stage,
        'image_count': len(references),
        'references': references,
        **details,
    }
    if error is not None:
        payload.update(error_type=type(error).__name__, error_code=getattr(error, 'code', None))
    session.add(AuditEvent(
        object_type='reference_input', object_id=object_id,
        action='reference_input_trace', details_json=payload,
    ))
