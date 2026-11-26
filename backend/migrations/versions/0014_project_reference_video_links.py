"""Scope reusable reference videos to projects.

Revision ID: 0014_project_reference_video_links
Revises: 0013
"""
from datetime import datetime, timezone
import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = '0014_project_reference_video_links'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'project_reference_videos',
        sa.Column('id', sa.String(length=64), primary_key=True, nullable=False),
        sa.Column('project_id', sa.String(length=64), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('video_id', sa.String(length=64), sa.ForeignKey('reference_videos.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('project_id', 'video_id', name='uq_project_reference_video'),
    )
    op.create_index('ix_project_reference_videos_project_id', 'project_reference_videos', ['project_id'])
    op.create_index('ix_project_reference_videos_video_id', 'project_reference_videos', ['video_id'])
    _backfill_existing_shot_bindings()


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _backfill_existing_shot_bindings():
    """Preserve only explicit legacy per-shot choices during the schema move.

    Old Studio records stored user-selected native video bindings below
    ``ShotDetail.source_json.production.motion_references``.  They were never
    global defaults, so each existing source is granted only to the project of
    the shot that already named it.  Missing/deactivated library videos are
    intentionally ignored: subsequent validation reports them as unavailable
    rather than creating a dangling foreign-key record.
    """
    bind = op.get_bind()
    shots = sa.table('storyboard_shots', sa.column('id', sa.String), sa.column('project_id', sa.String))
    details = sa.table('shot_details', sa.column('shot_id', sa.String), sa.column('source_json', sa.JSON))
    videos = sa.table('reference_videos', sa.column('id', sa.String), sa.column('state', sa.String))
    links = sa.table('project_reference_videos',
                     sa.column('id', sa.String), sa.column('project_id', sa.String),
                     sa.column('video_id', sa.String), sa.column('created_at', sa.DateTime(timezone=True)),
                     sa.column('updated_at', sa.DateTime(timezone=True)))
    active_ids = set(bind.execute(sa.select(videos.c.id).where(videos.c.state == 'active')).scalars())
    pairs: set[tuple[str, str]] = set()
    rows = bind.execute(sa.select(shots.c.project_id, details.c.source_json).join(
        details, details.c.shot_id == shots.c.id,
    )).mappings()
    for row in rows:
        production = _json_object(row['source_json']).get('production')
        if not isinstance(production, dict):
            continue
        references = production.get('motion_references')
        if not isinstance(references, list):
            continue
        for reference in references:
            video_id = reference.get('video_id') if isinstance(reference, dict) else None
            if isinstance(video_id, str) and video_id in active_ids:
                pairs.add((row['project_id'], video_id))
    if not pairs:
        return
    now = datetime.now(timezone.utc)
    bind.execute(sa.insert(links), [
        {'id': f'prjrefvideo_{uuid4().hex}', 'project_id': project_id, 'video_id': video_id,
         'created_at': now, 'updated_at': now}
        for project_id, video_id in sorted(pairs)
    ])


def downgrade():
    op.drop_index('ix_project_reference_videos_video_id', table_name='project_reference_videos')
    op.drop_index('ix_project_reference_videos_project_id', table_name='project_reference_videos')
    op.drop_table('project_reference_videos')
