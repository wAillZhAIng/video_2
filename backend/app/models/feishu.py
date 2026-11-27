"""Database models for Feishu Bitable sync, local-bridge orchestration, and batch video generation."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.entities import utcnow


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class FeishuBridgeHeartbeat(Base):
    __tablename__ = "feishu_bridge_heartbeats"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    hostname: Mapped[str] = mapped_column(String(160), default="")
    sync_dir: Mapped[str] = mapped_column(String(500), default=r"E:\feishu_sync")
    archive_dir: Mapped[str] = mapped_column(String(500), default=r"E:\vast视频")
    available_engines_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    rea_video_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeishuSyncJob(Base):
    __tablename__ = "feishu_sync_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("fsj"))
    service_id: Mapped[str | None] = mapped_column(ForeignKey("ai_services.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    engine: Mapped[str] = mapped_column(String(32), default="codex")
    force_rerun: Mapped[bool] = mapped_column(Boolean, default=False)
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeishuSyncRecord(Base):
    __tablename__ = "feishu_sync_records"
    __table_args__ = (
        UniqueConstraint("feishu_app_token", "feishu_table_id", "feishu_record_id", name="uq_feishu_sync_record"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("fsr"))
    feishu_app_token: Mapped[str] = mapped_column(String(128), index=True)
    feishu_table_id: Mapped[str] = mapped_column(String(128), index=True)
    feishu_record_id: Mapped[str] = mapped_column(String(128), index=True)
    product_name: Mapped[str] = mapped_column(String(240), default="")
    task_name: Mapped[str] = mapped_column(String(240), default="")
    reference_video_id: Mapped[str | None] = mapped_column(ForeignKey("reference_videos.id"), nullable=True)
    reference_video_sha256: Mapped[str] = mapped_column(String(64), default="")
    reference_video_local_path: Mapped[str] = mapped_column(String(1000), default="")
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    product_image_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    reference_image_local_paths_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    scripts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    analysis_summary: Mapped[str] = mapped_column(Text, default="")
    engine_used: Mapped[str] = mapped_column(String(32), default="")
    sync_status: Mapped[str] = mapped_column(String(32), default="synced")
    last_sync_job_id: Mapped[str | None] = mapped_column(ForeignKey("feishu_sync_jobs.id"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FeishuGenerationBatch(Base):
    __tablename__ = "feishu_generation_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("fgb"))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    archive_dir: Mapped[str] = mapped_column(String(500), default=r"E:\vast视频")
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    options_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    download_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeishuGenerationQueueItem(Base):
    __tablename__ = "feishu_generation_queue_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("fgi"))
    batch_id: Mapped[str] = mapped_column(ForeignKey("feishu_generation_batches.id"), index=True)
    sync_record_id: Mapped[str] = mapped_column(ForeignKey("feishu_sync_records.id"), index=True)
    feishu_record_id: Mapped[str] = mapped_column(String(128), default="")
    product_name: Mapped[str] = mapped_column(String(240), default="")
    task_name: Mapped[str] = mapped_column(String(240), default="")
    script_index: Mapped[int] = mapped_column(Integer, default=1)
    script_column: Mapped[str] = mapped_column(String(64), default="脚本1")
    script_text: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    studio_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    output_relative_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    archived_local_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
