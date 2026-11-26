"""Deterministic, native Ref2VA reference-video bindings.

This module deliberately contains no visual-language inference.  It validates
the user's explicit source selection, produces a small reproducible prompt
appendix, and fingerprints the media binding that is sent to ComfyUI.  The
actual video bytes are supplied separately by the provider as multiframe
``IMAGE`` inputs to MiniMax H3.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.services.cinematic_rules import fingerprint


_ROLE_TEXT = {
    'structure': 'the timing, action sequence, physical interaction, camera behavior and visual atmosphere',
    'action': 'the subject action, hand/object interaction and physical timing only',
    'camera': 'the camera movement, framing, lens perspective and edit rhythm only',
    'style': 'the lighting, palette, environment mood and photographic texture only',
    'combined': 'the action, physical interaction, camera movement, timing and visual atmosphere',
}


def _public_reference(ref: dict[str, Any]) -> dict[str, Any]:
    """Keep a frozen task record reproducible without leaking host paths."""
    keys = (
        'video_id', 'expected_revision', 'source_sha256',
        'trim_start_seconds', 'trim_end_seconds', 'reference_role', 'include_audio',
        'authorized_for_motion', 'authorized_for_reference',
        'target_character_id', 'effective_frames', 'effective_duration_seconds',
        'source_duration_seconds', 'selected_duration_seconds',
        'source_frame_count_at_24fps', 'fps', 'source_has_audio', 'audio_included',
    )
    return {key: ref[key] for key in keys if key in ref}


def public_references(references: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [_public_reference(ref) for ref in references or []]


def references_fingerprint(references: list[dict[str, Any]] | None) -> str:
    """Stable identity for a frozen native video conditioning selection."""
    return fingerprint(public_references(references))


def require_project_reference_access(session, project_id: str, references: list[dict[str, Any]] | None) -> None:
    """Reject accidental cross-project reuse before an input reaches H3."""
    identifiers = {item.get('video_id') for item in references or [] if item.get('video_id')}
    if not identifiers:
        return
    granted = set(session.scalars(select(m.ProjectReferenceVideo.video_id).where(
        m.ProjectReferenceVideo.project_id == project_id,
        m.ProjectReferenceVideo.video_id.in_(identifiers),
    )))
    missing = sorted(identifiers - granted)
    if missing:
        raise AppError(
            403,
            'reference_video_project_scope',
            '该参考视频尚未明确添加到当前项目；请在项目工作台选择或上传后再生成',
            {'video_ids': missing},
        )


def reference_prompt_constraints(
    references: list[dict[str, Any]] | None,
    *,
    picture_count: int = 0,
) -> str:
    """Return only deterministic, user-visible native conditioning guidance."""
    refs = public_references(references)
    if not refs:
        return ''
    lines = ['Native reference binding (do not treat this as a file-path instruction):']
    if picture_count:
        pictures = ', '.join(f'<Picture {number}>' for number in range(1, picture_count + 1))
        lines.append(
            f'{pictures} define the visual identity and physical structure of the requested subject. '
            'Do not replace that subject with a person or product appearing in a reference video.'
        )
    for number, ref in enumerate(refs, start=1):
        role = ref.get('reference_role', 'combined')
        role_text = _ROLE_TEXT.get(role, _ROLE_TEXT['combined'])
        lines.append(
            f'Use <Video {number}> as a native reference for {role_text}. '
            'Follow it only within the user script; do not copy its unrelated people, products, logos, text or audio.'
        )
        if ref.get('include_audio'):
            lines.append(
                f'<Video {number}> audio is explicitly supplied as a reference input; use it only as timing/ambience guidance, '
                'not as permission to reproduce protected dialogue or music.'
            )
    return '\n'.join(lines)
