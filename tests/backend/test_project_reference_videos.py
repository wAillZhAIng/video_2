import hashlib

import pytest

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.schemas_studio import ProjectReferenceVideoConfig
from app.services.project_reference_videos import (
    attach_existing_video,
    available_library,
    explicit_project_library,
    project_config,
    save_project_config,
)
from app.services.motion_references import configured_motion_reference_values, resolve_motion_references


def _binding(video_id, sha):
    return {
        'video_id': video_id,
        'expected_revision': 1,
        'source_sha256': sha,
        'trim_start_seconds': 0,
        'trim_end_seconds': 4,
        'authorized_for_reference': True,
        'reference_role': 'combined',
        'include_audio': False,
    }


def test_reference_video_configuration_is_project_scoped_and_restores_defaults(settings):
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    ours_sha = hashlib.sha256(b'ours').hexdigest()
    other_sha = hashlib.sha256(b'other').hexdigest()
    try:
        with build_session_factory(engine)() as session:
            session.add_all([
                m.Project(id='project-a', name='A'),
                m.Project(id='project-b', name='B'),
            ])
            session.flush()
            session.add_all([
                m.StoryboardShot(id='shot-a', project_id='project-a', order=1),
                m.ReferenceVideo(id='video-a', name='ours', filename='ours.mp4', relative_path='ours.mp4',
                    sha256=ours_sha, metadata_json={'duration_seconds': 8}, state='active', revision=1),
                m.ReferenceVideo(id='video-b', name='other', filename='other.mp4', relative_path='other.mp4',
                    sha256=other_sha, metadata_json={'duration_seconds': 8}, state='active', revision=1),
            ])
            session.flush()
            session.add_all([
                m.ProjectReferenceVideo(project_id='project-a', video_id='video-a'),
                m.ProjectReferenceVideo(project_id='project-b', video_id='video-b'),
                m.ShotDetail(shot_id='shot-a', source_json={'production': {
                    'reference_video_overrides': [_binding('video-a', ours_sha)],
                    'motion_references': [{'legacy': 'must be retired only for this edited shot'}],
                    'other_production_value': 'preserved',
                }}),
            ])
            session.flush()

            assert [item['id'] for item in explicit_project_library(session, 'project-a')['items']] == ['video-a']
            first = save_project_config(session, 'project-a', ProjectReferenceVideoConfig.model_validate({
                'expected_revision': 0,
                'reference_video_defaults': [_binding('video-a', ours_sha)],
                'shot_reference_video_overrides': {'shot-a': []},
            }))
            assert first['shot_reference_video_overrides'] == {'shot-a': []}
            saved_detail = session.scalar(session.query(m.ShotDetail).filter_by(shot_id='shot-a').statement)
            assert saved_detail.source_json['production']['reference_video_overrides'] == []
            assert 'motion_references' not in saved_detail.source_json['production']
            assert saved_detail.source_json['production']['other_production_value'] == 'preserved'

            restored = save_project_config(session, 'project-a', ProjectReferenceVideoConfig.model_validate({
                'expected_revision': first['revision'],
                'reference_video_defaults': [_binding('video-a', ours_sha)],
                'shot_reference_video_overrides': {},
            }))
            assert restored['shot_reference_video_overrides'] == {}
            assert project_config(session, 'project-a')['shot_reference_video_overrides'] == {}
            restored_detail = session.scalar(session.query(m.ShotDetail).filter_by(shot_id='shot-a').statement)
            assert 'reference_video_overrides' not in restored_detail.source_json['production']
            assert restored_detail.source_json['production']['other_production_value'] == 'preserved'
    finally:
        engine.dispose()


def test_browsing_global_library_never_grants_scope_until_explicit_attach(settings):
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    sha = hashlib.sha256(b'reusable-video').hexdigest()
    try:
        with build_session_factory(engine)() as session:
            session.add(m.Project(id='browse-project', name='Browse only'))
            session.add(m.StoryboardShot(id='browse-shot', project_id='browse-project', order=1))
            session.add(m.ReferenceVideo(
                id='global-video', name='Reusable global video', filename='global.mp4',
                relative_path='global.mp4', sha256=sha,
                metadata_json={'duration_seconds': 8}, state='active', revision=1,
            ))
            session.flush()

            available = available_library(session, 'browse-project')
            assert [(item['id'], item['linked_to_project']) for item in available['items']] == [
                ('global-video', False),
            ]
            assert explicit_project_library(session, 'browse-project')['items'] == []
            assert session.query(m.ProjectReferenceVideo).filter_by(
                project_id='browse-project', video_id='global-video',
            ).count() == 0

            payload = ProjectReferenceVideoConfig.model_validate({
                'expected_revision': 0,
                'reference_video_defaults': [_binding('global-video', sha)],
                'shot_reference_video_overrides': {},
            })
            with pytest.raises(AppError) as denied:
                save_project_config(session, 'browse-project', payload)
            assert denied.value.code == 'reference_video_project_scope'

            attached = attach_existing_video(session, 'browse-project', 'global-video')
            assert attached['id'] == 'global-video'
            assert attached['linked_to_project'] is True
            assert [item['id'] for item in explicit_project_library(session, 'browse-project')['items']] == [
                'global-video',
            ]

            saved = save_project_config(session, 'browse-project', payload)
            assert saved['reference_video_defaults'][0]['video_id'] == 'global-video'
    finally:
        engine.dispose()


def test_single_shot_reference_resolution_uses_override_then_project_default(settings, tmp_path):
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    default_bytes = b'project-default-reference'
    override_bytes = b'shot-override-reference'
    default_sha = hashlib.sha256(default_bytes).hexdigest()
    override_sha = hashlib.sha256(override_bytes).hexdigest()
    settings.media_root.mkdir(parents=True, exist_ok=True)
    (settings.media_root / 'project-default.mp4').write_bytes(default_bytes)
    (settings.media_root / 'shot-override.mp4').write_bytes(override_bytes)
    try:
        with build_session_factory(engine)() as session:
            session.add(m.Project(id='reference-project', name='Reference project'))
            session.add(m.StoryboardShot(id='reference-shot', project_id='reference-project', order=1))
            session.add_all([
                m.ReferenceVideo(id='default-video', name='default', filename='project-default.mp4',
                    relative_path='project-default.mp4', sha256=default_sha,
                    metadata_json={'duration_seconds': 8}, state='active', revision=1),
                m.ReferenceVideo(id='override-video', name='override', filename='shot-override.mp4',
                    relative_path='shot-override.mp4', sha256=override_sha,
                    metadata_json={'duration_seconds': 8}, state='active', revision=1),
            ])
            session.add_all([
                m.ProjectReferenceVideo(project_id='reference-project', video_id='default-video'),
                m.ProjectReferenceVideo(project_id='reference-project', video_id='override-video'),
            ])
            session.flush()
            session.add(m.ProjectProductionPreference(
                id='reference-project', reference_video_defaults_json=[_binding('default-video', default_sha)],
            ))
            session.commit()

            shot = session.get(m.StoryboardShot, 'reference-shot')
            assert [item['video_id'] for item in configured_motion_reference_values(session, shot)] == ['default-video']
            assert [item['video_id'] for item in resolve_motion_references(session, settings, shot)] == ['default-video']

            session.add(m.ShotDetail(shot_id=shot.id, source_json={'production': {
                'reference_video_overrides': [_binding('override-video', override_sha)],
            }}))
            session.flush()
            assert [item['video_id'] for item in configured_motion_reference_values(session, shot)] == ['override-video']
            assert [item['video_id'] for item in resolve_motion_references(session, settings, shot)] == ['override-video']

            session.get(m.ShotDetail, shot.id).source_json = {'production': {'reference_video_overrides': []}}
            session.flush()
            assert configured_motion_reference_values(session, shot) == []
            assert resolve_motion_references(session, settings, shot) == []
    finally:
        engine.dispose()
