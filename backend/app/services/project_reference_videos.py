"""Project-scoped storage and selection for native Ref2VA video inputs."""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.schemas_cinematic import MotionReferenceInput
from app.schemas_product_library import ProductionPreferenceEdit
from app.schemas_studio import ProjectReferenceVideoConfig
from app.services.native_reference_video import require_project_reference_access
from app.services.reference_video_analysis import get_video, video_dto
from app.services.studio_audio_policy import get_preferences, save_preferences


def _shots(session, project_id: str) -> list[m.StoryboardShot]:
    if not session.get(m.Project, project_id):
        raise AppError(404, 'project_missing', '项目不存在')
    return list(session.scalars(select(m.StoryboardShot).where(
        m.StoryboardShot.project_id == project_id,
        m.StoryboardShot.status != 'deleted',
    ).order_by(m.StoryboardShot.order, m.StoryboardShot.id)))


def _serialize(items: list[MotionReferenceInput] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item.model_dump(exclude_none=True) if isinstance(item, MotionReferenceInput) else copy.deepcopy(item)
        for item in items
    ]


def _shot_override(detail: m.ShotDetail | None) -> list[dict[str, Any]] | None:
    if detail is None:
        return None
    production = (detail.source_json or {}).get('production') or {}
    value = production.get('reference_video_overrides')
    return copy.deepcopy(value) if isinstance(value, list) else None


def project_config(session, project_id: str) -> dict[str, Any]:
    defaults = get_preferences(session, project_id)
    shots = _shots(session, project_id)
    if not shots:
        return {**defaults, 'shot_reference_video_overrides': {}, 'limits': limits()}
    details = {item.shot_id: item for item in session.scalars(select(m.ShotDetail).where(
        m.ShotDetail.shot_id.in_([shot.id for shot in shots])))}
    overrides: dict[str, list[dict[str, Any]]] = {}
    for shot in shots:
        value = _shot_override(details.get(shot.id))
        if value is not None:
            overrides[shot.id] = value
    return {**defaults, 'shot_reference_video_overrides': overrides, 'limits': limits()}


def limits() -> dict[str, int]:
    return {
        'max_reference_videos': 3,
        'max_reference_duration_seconds': 15,
        'min_reference_duration_seconds': 2,
        'max_reference_images': 9,
        'max_mixed_reference_files': 12,
    }


def _explicit_link(session, project_id: str, video_ids: set[str]) -> None:
    if not video_ids:
        return
    existing = set(session.scalars(select(m.ProjectReferenceVideo.video_id).where(
        m.ProjectReferenceVideo.project_id == project_id,
        m.ProjectReferenceVideo.video_id.in_(video_ids),
    )))
    for identifier in sorted(video_ids - existing):
        get_video(session, identifier, active=True)
        session.add(m.ProjectReferenceVideo(project_id=project_id, video_id=identifier))
    session.flush()


def _validate_values(session, project_id: str, values: list[MotionReferenceInput]) -> None:
    for item in values:
        video = get_video(session, item.video_id, active=True)
        if video.revision != item.expected_revision or video.sha256 != item.source_sha256:
            raise AppError(409, 'reference_video_stale', '参考视频版本或哈希已变化，请重新选择并保存')
        duration = (video.metadata_json or {}).get('duration_seconds')
        if not isinstance(duration, (int, float)) or item.trim_end_seconds > duration + 1e-6:
            raise AppError(422, 'reference_video_range', '所选参考片段超出原视频时长')
    require_project_reference_access(session, project_id, _serialize(values))


def save_project_config(session, project_id: str, payload: ProjectReferenceVideoConfig) -> dict[str, Any]:
    shots = {shot.id: shot for shot in _shots(session, project_id)}
    invalid = sorted(set(payload.shot_reference_video_overrides) - set(shots))
    if invalid:
        raise AppError(422, 'reference_video_shot_scope', '参考视频覆盖镜头必须属于当前项目', {'shot_ids': invalid})
    values = [*payload.reference_video_defaults]
    for group in payload.shot_reference_video_overrides.values():
        values.extend(group)
    # Saving a configuration cannot grant access to a globally stored asset.
    # Uploading through this project is the explicit grant path; the project
    # library endpoint exposes only assets that have already been granted.
    _validate_values(session, project_id, values)
    # The preference revision guards both the project default and the shot
    # overrides, so a stale browser tab cannot overwrite either selection.
    save_preferences(session, project_id, ProductionPreferenceEdit(
        expected_revision=payload.expected_revision,
        reference_video_defaults=payload.reference_video_defaults,
    ), commit=False)
    details = {item.shot_id: item for item in session.scalars(select(m.ShotDetail).where(
        m.ShotDetail.shot_id.in_(list(shots))))}
    # PUT replaces this project's override map as a whole.  In particular,
    # deleting a browser-side override means "inherit the current project
    # default" rather than leaving an invisible stale override in the DB.
    for shot_id, shot in shots.items():
        detail = details.get(shot_id)
        group = payload.shot_reference_video_overrides.get(shot_id)
        if group is None:
            if detail is None:
                continue
            source = copy.deepcopy(detail.source_json or {})
            production = copy.deepcopy(source.get('production') or {})
            if 'reference_video_overrides' not in production:
                continue
            production.pop('reference_video_overrides', None)
            detail.source_json = {**source, 'production': production}
            continue
        if detail is None:
            detail = m.ShotDetail(shot_id=shot_id)
            session.add(detail)
        source = copy.deepcopy(detail.source_json or {})
        production = copy.deepcopy(source.get('production') or {})
        # Presence of an empty list is intentional: it prevents the project
        # default from flowing into this one shot.
        production['reference_video_overrides'] = _serialize(group)
        # This shot has now been deliberately edited through the native
        # reference-video UI.  Retire only its old per-shot selection so it
        # cannot unexpectedly override the new project default later.  Old
        # StudioRun snapshots remain untouched and fully reproducible.
        production.pop('motion_references', None)
        detail.source_json = {**source, 'production': production}
    session.flush()
    return project_config(session, project_id)


def explicit_project_library(session, project_id: str) -> dict[str, Any]:
    _shots(session, project_id)
    # This UI is a project library, not a global asset browser.  A reference
    # may be reusable only after an explicit project grant, so it can never
    # appear merely because another project uploaded it first.
    items = []
    statement = (select(m.ReferenceVideo)
                 .join(m.ProjectReferenceVideo, m.ProjectReferenceVideo.video_id == m.ReferenceVideo.id)
                 .where(m.ProjectReferenceVideo.project_id == project_id,
                        m.ReferenceVideo.state == 'active')
                 .order_by(m.ReferenceVideo.created_at.desc(), m.ReferenceVideo.id.desc())
                 .limit(500))
    for video in session.scalars(statement):
        items.append({**video_dto(session, video), 'linked_to_project': True})
    return {'project_id': project_id, 'items': items, 'limits': limits()}


def available_library(session, project_id: str) -> dict[str, Any]:
    """List active library assets that can be explicitly attached to a project.

    Listing an asset never grants it to the project.  The caller must make a
    separate, auditable attach request before the asset can be selected by a
    shot or reach a generation request.  This preserves project isolation
    while still allowing the user to reuse a video from the existing library.
    """
    _shots(session, project_id)
    linked = set(session.scalars(select(m.ProjectReferenceVideo.video_id).where(
        m.ProjectReferenceVideo.project_id == project_id,
    )))
    items = []
    for video in session.scalars(select(m.ReferenceVideo).where(
        m.ReferenceVideo.state == 'active',
    ).order_by(m.ReferenceVideo.created_at.desc(), m.ReferenceVideo.id.desc()).limit(500)):
        items.append({**video_dto(session, video), 'linked_to_project': video.id in linked})
    return {'project_id': project_id, 'items': items, 'limits': limits()}


def attach_existing_video(session, project_id: str, video_id: str) -> dict[str, Any]:
    """Make one explicit project grant for an existing verified source video."""
    _shots(session, project_id)
    video = get_video(session, video_id, active=True)
    _explicit_link(session, project_id, {video.id})
    return {**video_dto(session, video), 'linked_to_project': True}


def link_uploaded_video(session, project_id: str, video_id: str) -> None:
    _shots(session, project_id)
    _explicit_link(session, project_id, {video_id})
