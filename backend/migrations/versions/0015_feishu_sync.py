"""Add Feishu Bitable sync, bridge heartbeat, and batch video generation tables.

Revision ID: 0015_feishu_sync
Revises: 0014_project_reference_video_links
"""
from alembic import op
import sqlalchemy as sa


revision = "0015_feishu_sync"
down_revision = "0014_project_reference_video_links"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feishu_bridge_heartbeats",
        sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("hostname", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("sync_dir", sa.String(length=500), nullable=False, server_default=r"E:\feishu_sync"),
        sa.Column("archive_dir", sa.String(length=500), nullable=False, server_default=r"E:\vast视频"),
        sa.Column("available_engines_json", sa.JSON(), nullable=False),
        sa.Column("rea_video_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("meta_json", sa.JSON(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "feishu_sync_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("service_id", sa.String(length=64), sa.ForeignKey("ai_services.id"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("force_rerun", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("progress_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "feishu_sync_records",
        sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("feishu_app_token", sa.String(length=128), nullable=False),
        sa.Column("feishu_table_id", sa.String(length=128), nullable=False),
        sa.Column("feishu_record_id", sa.String(length=128), nullable=False),
        sa.Column("product_name", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("task_name", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("reference_video_id", sa.String(length=64), sa.ForeignKey("reference_videos.id"), nullable=True),
        sa.Column("reference_video_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("reference_video_local_path", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("product_id", sa.String(length=64), sa.ForeignKey("products.id"), nullable=True),
        sa.Column("product_image_ids_json", sa.JSON(), nullable=False),
        sa.Column("reference_image_local_paths_json", sa.JSON(), nullable=False),
        sa.Column("scripts_json", sa.JSON(), nullable=False),
        sa.Column("analysis_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("engine_used", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("sync_status", sa.String(length=32), nullable=False, server_default="synced"),
        sa.Column("last_sync_job_id", sa.String(length=64), sa.ForeignKey("feishu_sync_jobs.id"), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("feishu_app_token", "feishu_table_id", "feishu_record_id", name="uq_feishu_sync_record"),
    )
    op.create_index("ix_feishu_sync_records_app_token", "feishu_sync_records", ["feishu_app_token"])
    op.create_index("ix_feishu_sync_records_table_id", "feishu_sync_records", ["feishu_table_id"])
    op.create_index("ix_feishu_sync_records_record_id", "feishu_sync_records", ["feishu_record_id"])

    op.create_table(
        "feishu_generation_batches",
        sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("archive_dir", sa.String(length=500), nullable=False, server_default=r"E:\vast视频"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("download_report_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "feishu_generation_queue_items",
        sa.Column("id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("batch_id", sa.String(length=64), sa.ForeignKey("feishu_generation_batches.id"), nullable=False),
        sa.Column("sync_record_id", sa.String(length=64), sa.ForeignKey("feishu_sync_records.id"), nullable=False),
        sa.Column("feishu_record_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("product_name", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("task_name", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("script_index", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("script_column", sa.String(length=64), nullable=False, server_default="脚本1"),
        sa.Column("script_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("studio_run_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("output_relative_path", sa.String(length=1000), nullable=True),
        sa.Column("archived_local_path", sa.String(length=1000), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_feishu_queue_batch_id", "feishu_generation_queue_items", ["batch_id"])
    op.create_index("ix_feishu_queue_sync_record_id", "feishu_generation_queue_items", ["sync_record_id"])


def downgrade():
    op.drop_table("feishu_generation_queue_items")
    op.drop_table("feishu_generation_batches")
    op.drop_table("feishu_sync_records")
    op.drop_table("feishu_sync_jobs")
    op.drop_table("feishu_bridge_heartbeats")
