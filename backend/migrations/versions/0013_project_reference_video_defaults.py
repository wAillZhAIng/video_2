"""Store explicit project-default native reference video selections."""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('project_production_preferences') as batch:
        batch.add_column(sa.Column('reference_video_defaults_json', sa.JSON(), nullable=False, server_default='[]'))


def downgrade():
    with op.batch_alter_table('project_production_preferences') as batch:
        batch.drop_column('reference_video_defaults_json')
