"""Orchestrator for Feishu Bitable sync, local bridge coordination, and serial batch video generation."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.core.errors import AppError
from app.core.secrets import reveal
from app.models.entities import utcnow
from app.models.feishu import (
    FeishuBridgeHeartbeat,
    FeishuGenerationBatch,
    FeishuGenerationQueueItem,
    FeishuSyncJob,
    FeishuSyncRecord,
)
from app.services.feishu_client import (
    DEFAULT_COLUMN_MAPPING,
    DEFAULT_FEISHU_API_BASE,
    DEFAULT_FEISHU_BITABLE_URL,
    FeishuClient,
    extract_cell_text,
    parse_feishu_bitable_url,
    resolve_column_names,
    script_column_names,
)

logger = logging.getLogger(__name__)

BRIDGE_ONLINE_TTL_SECONDS = 60
TERMINAL_SYNC_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_BATCH_STATUSES = {"archived", "failed", "cancelled"}
TERMINAL_ITEM_STATUSES = {"completed", "failed", "cancelled"}


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalized_feishu_config(service: m.AIService) -> dict[str, Any]:
    opts = dict(service.settings_json or {})
    bitable_url = str(opts.get("bitable_url") or DEFAULT_FEISHU_BITABLE_URL).strip()
    parsed = parse_feishu_bitable_url(bitable_url)
    app_token = str(opts.get("app_token") or parsed["app_token"]).strip()
    table_id = str(opts.get("table_id") or parsed["table_id"]).strip()
    view_id = str(opts.get("view_id") or parsed["view_id"]).strip()
    engine = str(opts.get("analysis_engine") or "codex").strip().lower()
    if engine not in {"antigravity", "codex"}:
        engine = "codex"
    try:
        script_count = max(1, min(20, int(opts.get("script_count") or 5)))
    except (TypeError, ValueError):
        script_count = 5
    return {
        "service_id": service.id,
        "service_name": service.name,
        "api_base_url": (service.base_url or DEFAULT_FEISHU_API_BASE).rstrip("/"),
        "app_id": str(opts.get("app_id") or "").strip(),
        "bitable_url": bitable_url,
        "app_token": app_token,
        "table_id": table_id,
        "view_id": view_id,
        "col_product_name": str(opts.get("col_product_name") or DEFAULT_COLUMN_MAPPING["col_product_name"]).strip(),
        "col_reference_video": str(opts.get("col_reference_video") or DEFAULT_COLUMN_MAPPING["col_reference_video"]).strip(),
        "col_reference_image": str(opts.get("col_reference_image") or DEFAULT_COLUMN_MAPPING["col_reference_image"]).strip(),
        "col_task_name": str(opts.get("col_task_name") or DEFAULT_COLUMN_MAPPING["col_task_name"]).strip(),
        "col_script_start": str(opts.get("col_script_start") or DEFAULT_COLUMN_MAPPING["col_script_start"]).strip(),
        "analysis_engine": engine,
        "analysis_prompt": str(opts.get("analysis_prompt") or "").strip(),
        "script_count": script_count,
        "local_sync_dir": str(opts.get("local_sync_dir") or r"E:\feishu_sync").strip(),
        "local_archive_dir": str(opts.get("local_archive_dir") or r"E:\vast视频").strip(),
    }


def get_active_feishu_service(session: Session) -> tuple[m.AIService, dict[str, Any], str]:
    rows = list(
        session.scalars(
            select(m.AIService)
            .where(m.AIService.module == "feishu_sync", m.AIService.enabled.is_(True))
            .order_by(m.AIService.is_default.desc(), m.AIService.created_at.desc())
        )
    )
    if not rows:
        raise AppError(404, "feishu_service_not_configured", "请先在「系统设置 → 服务档案」中添加并启用飞书多维表格同步配置")
    service = rows[0]
    cfg = normalized_feishu_config(service)
    secret = reveal(service.encrypted_secret) if service.encrypted_secret else ""
    if not cfg["app_id"] or not secret:
        raise AppError(422, "feishu_credentials_missing", "飞书配置缺少 App ID 或 App Secret，请在系统设置补全后重试")
    if not cfg["app_token"] or not cfg["table_id"]:
        raise AppError(422, "feishu_table_missing", "飞书多维表格链接无效，请填写包含 base/{app_token}?table={table_id} 的完整地址")
    return service, cfg, secret


def bridge_status(session: Session) -> dict[str, Any]:
    hb = session.get(FeishuBridgeHeartbeat, "default")
    if not hb:
        return {
            "online": False,
            "hostname": "",
            "sync_dir": r"E:\feishu_sync",
            "archive_dir": r"E:\vast视频",
            "available_engines": [],
            "rea_video_ready": False,
            "last_seen_at": None,
            "meta": {},
        }
    seen = _as_utc(hb.last_seen_at)
    online = bool(seen and (utcnow() - seen) <= timedelta(seconds=BRIDGE_ONLINE_TTL_SECONDS))
    return {
        "online": online,
        "hostname": hb.hostname,
        "sync_dir": hb.sync_dir,
        "archive_dir": hb.archive_dir,
        "available_engines": list(hb.available_engines_json or []),
        "rea_video_ready": bool(hb.rea_video_ready),
        "last_seen_at": seen.isoformat() if seen else None,
        "meta": dict(hb.meta_json or {}),
    }


def record_bridge_heartbeat(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    hb = session.get(FeishuBridgeHeartbeat, "default")
    if not hb:
        hb = FeishuBridgeHeartbeat(id="default")
        session.add(hb)
    hb.hostname = str(payload.get("hostname") or "")[:160]
    hb.sync_dir = str(payload.get("sync_dir") or r"E:\feishu_sync")[:500]
    hb.archive_dir = str(payload.get("archive_dir") or r"E:\vast视频")[:500]
    hb.available_engines_json = [str(x) for x in (payload.get("available_engines") or [])][:10]
    hb.rea_video_ready = bool(payload.get("rea_video_ready"))
    hb.meta_json = dict(payload.get("meta") or {})
    hb.last_seen_at = utcnow()
    session.commit()
    return bridge_status(session)


def sync_job_dto(job: FeishuSyncJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "service_id": job.service_id,
        "status": job.status,
        "engine": job.engine,
        "force_rerun": bool(job.force_rerun),
        "config": dict(job.config_snapshot_json or {}),
        "progress": dict(job.progress_json or {}),
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def sync_record_dto(rec: FeishuSyncRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "feishu_app_token": rec.feishu_app_token,
        "feishu_table_id": rec.feishu_table_id,
        "feishu_record_id": rec.feishu_record_id,
        "product_name": rec.product_name,
        "task_name": rec.task_name,
        "reference_video_id": rec.reference_video_id,
        "reference_video_local_path": rec.reference_video_local_path,
        "product_id": rec.product_id,
        "product_image_ids": list(rec.product_image_ids_json or []),
        "reference_image_local_paths": list(rec.reference_image_local_paths_json or []),
        "scripts": list(rec.scripts_json or []),
        "analysis_summary": rec.analysis_summary,
        "engine_used": rec.engine_used,
        "sync_status": rec.sync_status,
        "error_message": rec.error_message,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def queue_item_dto(item: FeishuGenerationQueueItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "sync_record_id": item.sync_record_id,
        "feishu_record_id": item.feishu_record_id,
        "product_name": item.product_name,
        "task_name": item.task_name,
        "script_index": item.script_index,
        "script_column": item.script_column,
        "script_preview": (item.script_text or "")[:160],
        "project_id": item.project_id,
        "studio_run_id": item.studio_run_id,
        "status": item.status,
        "output_relative_path": item.output_relative_path,
        "archived_local_path": item.archived_local_path,
        "error_message": item.error_message,
        "order_index": item.order_index,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def batch_dto(session: Session, batch: FeishuGenerationBatch) -> dict[str, Any]:
    items = list(
        session.scalars(
            select(FeishuGenerationQueueItem)
            .where(FeishuGenerationQueueItem.batch_id == batch.id)
            .order_by(FeishuGenerationQueueItem.order_index, FeishuGenerationQueueItem.created_at)
        )
    )
    return {
        "id": batch.id,
        "status": batch.status,
        "archive_dir": batch.archive_dir,
        "total_items": batch.total_items,
        "completed_items": batch.completed_items,
        "failed_items": batch.failed_items,
        "options": dict(batch.options_json or {}),
        "download_report": dict(batch.download_report_json or {}),
        "error_message": batch.error_message,
        "items": [queue_item_dto(x) for x in items],
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "updated_at": batch.updated_at.isoformat() if batch.updated_at else None,
        "finished_at": batch.finished_at.isoformat() if batch.finished_at else None,
    }


def get_overview_status(session: Session) -> dict[str, Any]:
    service_info = None
    try:
        service, cfg, _ = get_active_feishu_service(session)
        service_info = {
            "configured": True,
            "service_id": service.id,
            "name": service.name,
            "bitable_url": cfg["bitable_url"],
            "app_token": cfg["app_token"],
            "table_id": cfg["table_id"],
            "view_id": cfg["view_id"],
            "analysis_engine": cfg["analysis_engine"],
            "script_count": cfg["script_count"],
            "analysis_prompt": cfg["analysis_prompt"],
            "local_sync_dir": cfg["local_sync_dir"],
            "local_archive_dir": cfg["local_archive_dir"],
            "columns": {
                "col_product_name": cfg["col_product_name"],
                "col_reference_video": cfg["col_reference_video"],
                "col_reference_image": cfg["col_reference_image"],
                "col_task_name": cfg["col_task_name"],
                "col_script_start": cfg["col_script_start"],
            },
        }
    except AppError as exc:
        service_info = {"configured": False, "error": exc.message}

    latest_sync = session.scalar(select(FeishuSyncJob).order_by(FeishuSyncJob.created_at.desc()).limit(1))
    records = list(session.scalars(select(FeishuSyncRecord).order_by(FeishuSyncRecord.updated_at.desc()).limit(50)))
    batches = list(session.scalars(select(FeishuGenerationBatch).order_by(FeishuGenerationBatch.created_at.desc()).limit(10)))
    return {
        "service": service_info,
        "bridge": bridge_status(session),
        "latest_sync_job": sync_job_dto(latest_sync) if latest_sync else None,
        "records": [sync_record_dto(r) for r in records],
        "batches": [batch_dto(session, b) for b in batches],
    }


def create_sync_job(session: Session, *, force_rerun: bool = False, engine_override: str | None = None) -> dict[str, Any]:
    service, cfg, _ = get_active_feishu_service(session)
    active = session.scalar(
        select(FeishuSyncJob)
        .where(FeishuSyncJob.status.in_(["queued", "claimed", "downloading", "analyzing", "writing_feishu"]))
        .order_by(FeishuSyncJob.created_at.desc())
        .limit(1)
    )
    if active:
        return sync_job_dto(active)
    engine = (engine_override or cfg["analysis_engine"] or "codex").strip().lower()
    if engine not in {"antigravity", "codex"}:
        engine = cfg["analysis_engine"]
    snapshot = {**cfg, "analysis_engine": engine}
    job = FeishuSyncJob(
        service_id=service.id,
        status="queued",
        engine=engine,
        force_rerun=bool(force_rerun),
        config_snapshot_json=snapshot,
        progress_json={
            "total_records": 0,
            "processed_records": 0,
            "current_stage": "等待本机桥接守护程序认领任务…",
            "logs": [f"[{utcnow().strftime('%H:%M:%S')}] 已创建飞书同步任务（引擎：{engine}），等待本机桥接执行"],
        },
    )
    session.add(job)
    session.commit()
    return sync_job_dto(job)


def cancel_sync_job(session: Session, job_id: str) -> dict[str, Any]:
    job = session.get(FeishuSyncJob, job_id)
    if not job:
        raise AppError(404, "feishu_sync_job_missing", "飞书同步任务不存在")
    if job.status not in TERMINAL_SYNC_STATUSES:
        job.status = "cancelled"
        job.finished_at = utcnow()
        job.error_message = "用户手动取消"
        session.commit()
    return sync_job_dto(job)


def mint_bridge_token(session: Session) -> dict[str, Any]:
    _, cfg, secret = get_active_feishu_service(session)
    client = FeishuClient(cfg["app_id"], secret, base_url=cfg["api_base_url"])
    token = client.get_tenant_access_token(force_refresh=True)
    return {
        "tenant_access_token": token,
        "api_base_url": cfg["api_base_url"],
        "app_token": cfg["app_token"],
        "table_id": cfg["table_id"],
        "view_id": cfg["view_id"],
    }


def claim_next_sync_job(session: Session) -> dict[str, Any] | None:
    job = session.scalar(
        select(FeishuSyncJob)
        .where(FeishuSyncJob.status == "queued")
        .order_by(FeishuSyncJob.created_at.asc())
        .limit(1)
    )
    if not job:
        return None
    _, cfg, secret = get_active_feishu_service(session)
    client = FeishuClient(cfg["app_id"], secret, base_url=cfg["api_base_url"])
    token = client.get_tenant_access_token(force_refresh=True)
    job.status = "claimed"
    prog = dict(job.progress_json or {})
    logs = list(prog.get("logs") or [])
    logs.append(f"[{utcnow().strftime('%H:%M:%S')}] 本机桥接已认领任务，开始拉取飞书表格并落盘至 {cfg['local_sync_dir']}")
    prog["current_stage"] = "本机桥接已认领，正在连接飞书…"
    prog["logs"] = logs[-80:]
    job.progress_json = prog
    session.commit()
    return {
        "job": sync_job_dto(job),
        "tenant_access_token": token,
        "api_base_url": cfg["api_base_url"],
    }


def update_sync_progress(session: Session, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = session.get(FeishuSyncJob, job_id)
    if not job:
        raise AppError(404, "feishu_sync_job_missing", "飞书同步任务不存在")
    if job.status == "cancelled":
        return sync_job_dto(job)
    if payload.get("status"):
        job.status = str(payload["status"])
    prog = dict(job.progress_json or {})
    for key in ("total_records", "processed_records", "current_record_id", "current_stage"):
        if key in payload and payload[key] is not None:
            prog[key] = payload[key]
    if payload.get("log"):
        logs = list(prog.get("logs") or [])
        logs.append(f"[{utcnow().strftime('%H:%M:%S')}] {payload['log']}")
        prog["logs"] = logs[-120:]
    job.progress_json = prog
    session.commit()
    return sync_job_dto(job)


def ingest_record_media(
    session: Session,
    settings,
    job_id: str,
    *,
    feishu_record_id: str,
    product_name: str,
    task_name: str,
    video_filename: str | None,
    video_bytes: bytes | None,
    image_uploads: list[tuple[str, bytes]],
    local_video_path: str = "",
    local_image_paths: list[str] | None = None,
) -> dict[str, Any]:
    from app.schemas_product_library import ProductCreate
    from app.services import product_library as pl_service
    from app.services import reference_video_analysis as rv_service

    job = session.get(FeishuSyncJob, job_id)
    if not job:
        raise AppError(404, "feishu_sync_job_missing", "飞书同步任务不存在")
    cfg = dict(job.config_snapshot_json or {})
    app_token = str(cfg.get("app_token") or "")
    table_id = str(cfg.get("table_id") or "")

    rec = session.scalar(
        select(FeishuSyncRecord).where(
            FeishuSyncRecord.feishu_app_token == app_token,
            FeishuSyncRecord.feishu_table_id == table_id,
            FeishuSyncRecord.feishu_record_id == feishu_record_id,
        )
    )
    if not rec:
        rec = FeishuSyncRecord(
            feishu_app_token=app_token,
            feishu_table_id=table_id,
            feishu_record_id=feishu_record_id,
        )
        session.add(rec)
        session.flush()

    rec.product_name = (product_name or rec.product_name or "未命名产品").strip()[:240]
    rec.task_name = (task_name or rec.task_name or rec.product_name).strip()[:240]
    rec.last_sync_job_id = job.id
    if local_video_path:
        rec.reference_video_local_path = local_video_path
    if local_image_paths is not None:
        rec.reference_image_local_paths_json = list(local_image_paths)

    if video_bytes and video_filename:
        rv, _reused = rv_service.import_video(
            session,
            settings,
            video_filename,
            video_bytes,
            name=f"{rec.product_name}-{rec.task_name}"[:120],
            category="feishu_sync",
            source_note=f"Feishu record {feishu_record_id}",
            usage_scope="motion_reference",
            commit=False,
        )
        rec.reference_video_id = rv.id
        rec.reference_video_sha256 = rv.sha256

    if image_uploads:
        product = session.get(m.Product, rec.product_id) if rec.product_id else None
        if not product:
            product = session.scalar(
                select(m.Product).where(m.Product.name == rec.product_name, m.Product.state == "active").limit(1)
            )
        if not product:
            product_dto = pl_service.create_product(
                session,
                ProductCreate(name=rec.product_name[:120], category="feishu_sync", notes=f"飞书同步自动建档 ({feishu_record_id})"),
            )
            product = session.get(m.Product, product_dto["id"])
        rec_id = rec.id
        product_id = product.id
        rec.product_id = product_id
        session.commit()
        existing_imgs = list(session.scalars(select(m.ProductImage).where(m.ProductImage.product_id == product_id)))
        by_sha = {}
        for img in existing_imgs:
            asset = session.get(m.ScreenplayInputAsset, img.asset_id) if img.asset_id else None
            if asset and asset.sha256:
                by_sha[asset.sha256] = img.id
        image_ids: list[str] = []
        import hashlib
        for idx, (fname, data) in enumerate(image_uploads):
            if not data:
                continue
            sha = hashlib.sha256(data).hexdigest()
            if sha in by_sha:
                image_ids.append(by_sha[sha])
            else:
                purpose = "front" if idx == 0 else "detail"
                created = pl_service.add_image(session, settings, product_id, fname, data, purpose=purpose, notes=f"飞书参考图 #{idx+1}")
                created_obj = created[0] if isinstance(created, tuple) else created
                img_id = created_obj["id"] if isinstance(created_obj, dict) else created_obj.id
                by_sha[sha] = img_id
                image_ids.append(img_id)
        rec = session.get(FeishuSyncRecord, rec_id)
        if image_ids and rec is not None:
            rec.product_image_ids_json = list(dict.fromkeys(image_ids))

    rec.sync_status = "media_synced"
    session.commit()
    return sync_record_dto(rec)


def complete_record_sync(session: Session, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = session.get(FeishuSyncJob, job_id)
    if not job:
        raise AppError(404, "feishu_sync_job_missing", "飞书同步任务不存在")
    cfg = dict(job.config_snapshot_json or {})
    app_token = str(cfg.get("app_token") or "")
    table_id = str(cfg.get("table_id") or "")
    feishu_record_id = str(payload.get("feishu_record_id") or "").strip()
    if not feishu_record_id:
        raise AppError(422, "feishu_record_id_required", "缺少 feishu_record_id")

    rec = session.scalar(
        select(FeishuSyncRecord).where(
            FeishuSyncRecord.feishu_app_token == app_token,
            FeishuSyncRecord.feishu_table_id == table_id,
            FeishuSyncRecord.feishu_record_id == feishu_record_id,
        )
    )
    if not rec:
        rec = FeishuSyncRecord(
            feishu_app_token=app_token,
            feishu_table_id=table_id,
            feishu_record_id=feishu_record_id,
            product_name=str(payload.get("product_name") or "未命名产品")[:240],
            task_name=str(payload.get("task_name") or "")[:240],
            last_sync_job_id=job.id,
        )
        session.add(rec)

    if payload.get("product_name"):
        rec.product_name = str(payload["product_name"])[:240]
    if payload.get("task_name"):
        rec.task_name = str(payload["task_name"])[:240]
    if payload.get("scripts") is not None:
        rec.scripts_json = list(payload["scripts"])
    if payload.get("analysis_summary") is not None:
        rec.analysis_summary = str(payload["analysis_summary"])
    if payload.get("engine_used"):
        rec.engine_used = str(payload["engine_used"])[:32]
    rec.sync_status = str(payload.get("sync_status") or "written_back")
    rec.error_message = payload.get("error_message")
    rec.last_sync_job_id = job.id
    session.commit()
    return sync_record_dto(rec)


def complete_sync_job(session: Session, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = session.get(FeishuSyncJob, job_id)
    if not job:
        raise AppError(404, "feishu_sync_job_missing", "飞书同步任务不存在")
    ok = bool(payload.get("ok", True))
    job.status = "completed" if ok else "failed"
    job.error_message = None if ok else str(payload.get("error_message") or "飞书同步失败")
    job.finished_at = utcnow()
    prog = dict(job.progress_json or {})
    prog["current_stage"] = "同步完成，脚本已回写飞书" if ok else f"同步失败：{job.error_message}"
    if payload.get("processed_records") is not None:
        prog["processed_records"] = payload["processed_records"]
    if payload.get("total_records") is not None:
        prog["total_records"] = payload["total_records"]
    if payload.get("log"):
        logs = list(prog.get("logs") or [])
        logs.append(f"[{utcnow().strftime('%H:%M:%S')}] {payload['log']}")
        prog["logs"] = logs[-120:]
    job.progress_json = prog
    session.commit()
    return sync_job_dto(job)


def refresh_scripts_from_feishu(session: Session) -> list[dict[str, Any]]:
    """Pull latest script columns from Feishu so manual edits in Bitable are respected before generating."""
    _, cfg, secret = get_active_feishu_service(session)
    client = FeishuClient(cfg["app_id"], secret, base_url=cfg["api_base_url"])
    fields = client.list_fields(cfg["app_token"], cfg["table_id"], cfg["view_id"] or None)
    cols = resolve_column_names(fields, cfg)
    script_cols = script_column_names(cols["col_script_start"], cfg["script_count"])
    feishu_rows = client.list_records(cfg["app_token"], cfg["table_id"], cfg["view_id"] or None)

    by_rid = {
        rec.feishu_record_id: rec
        for rec in session.scalars(
            select(FeishuSyncRecord).where(
                FeishuSyncRecord.feishu_app_token == cfg["app_token"],
                FeishuSyncRecord.feishu_table_id == cfg["table_id"],
            )
        )
    }

    for row in feishu_rows:
        rid = str(row.get("record_id") or "")
        rec = by_rid.get(rid)
        if not rec:
            continue
        fdict = row.get("fields") or {}
        prod = extract_cell_text(fdict.get(cols["col_product_name"]))
        tname = extract_cell_text(fdict.get(cols["col_task_name"]))
        if prod:
            rec.product_name = prod[:240]
        if tname:
            rec.task_name = tname[:240]
        updated_scripts: list[dict[str, Any]] = []
        for idx, col_name in enumerate(script_cols, start=1):
            text = extract_cell_text(fdict.get(col_name))
            if text:
                updated_scripts.append({"index": idx, "column": col_name, "text": text})
        if updated_scripts:
            rec.scripts_json = updated_scripts
    session.commit()
    return preview_generation_candidates(session, refresh_from_feishu=False)


def preview_generation_candidates(session: Session, *, refresh_from_feishu: bool = True) -> list[dict[str, Any]]:
    if refresh_from_feishu:
        try:
            return refresh_scripts_from_feishu(session)
        except Exception as exc:
            logger.warning("Could not refresh scripts from Feishu live, falling back to cached DB records: %s", exc)

    records = list(session.scalars(select(FeishuSyncRecord).order_by(FeishuSyncRecord.updated_at.desc())))
    done_pairs: dict[tuple[str, int], str] = {}
    for item in session.scalars(
        select(FeishuGenerationQueueItem).where(FeishuGenerationQueueItem.status.in_(["queued", "preparing", "running", "completed"]))
    ):
        if item.project_id:
            done_pairs[(item.sync_record_id, item.script_index)] = item.project_id

    candidates: list[dict[str, Any]] = []
    for rec in records:
        for script_entry in rec.scripts_json or []:
            text = str(script_entry.get("text") or "").strip()
            if not text:
                continue
            s_idx = int(script_entry.get("index") or 1)
            s_col = str(script_entry.get("column") or f"脚本{s_idx}")
            existing_pid = done_pairs.get((rec.id, s_idx))
            candidates.append({
                "sync_record_id": rec.id,
                "feishu_record_id": rec.feishu_record_id,
                "product_name": rec.product_name or "未命名产品",
                "task_name": rec.task_name or rec.product_name or rec.feishu_record_id,
                "script_index": s_idx,
                "script_column": s_col,
                "script_preview": text[:160],
                "script_length": len(text),
                "has_reference_video": bool(rec.reference_video_id),
                "reference_image_count": len(rec.product_image_ids_json or []),
                "already_generated": bool(existing_pid),
                "existing_project_id": existing_pid,
            })
    return candidates


def start_generation_batch(
    session: Session,
    *,
    selections: list[dict[str, Any]] | None = None,
    include_already_generated: bool = False,
    refresh_from_feishu: bool = True,
    quality: dict[str, Any] | None = None,
    width: int = 576,
    height: int = 1024,
) -> dict[str, Any]:
    archive_dir = r"E:\vast视频"
    try:
        _, cfg, _ = get_active_feishu_service(session)
        archive_dir = cfg["local_archive_dir"] or archive_dir
    except AppError:
        pass

    if refresh_from_feishu:
        try:
            refresh_scripts_from_feishu(session)
        except Exception as exc:
            logger.warning("Pre-generate Feishu refresh skipped: %s", exc)

    records_by_id = {r.id: r for r in session.scalars(select(FeishuSyncRecord))}
    already_done = {
        (item.sync_record_id, item.script_index)
        for item in session.scalars(
            select(FeishuGenerationQueueItem).where(FeishuGenerationQueueItem.status.in_(["queued", "preparing", "running", "completed"]))
        )
    }

    chosen: list[tuple[FeishuSyncRecord, dict[str, Any]]] = []
    if selections:
        for sel in selections:
            rec = records_by_id.get(str(sel.get("sync_record_id") or ""))
            s_idx = int(sel.get("script_index") or 0)
            if not rec:
                continue
            for entry in rec.scripts_json or []:
                if int(entry.get("index") or 0) == s_idx and str(entry.get("text") or "").strip():
                    chosen.append((rec, entry))
                    break
    else:
        for rec in sorted(records_by_id.values(), key=lambda x: x.created_at):
            for entry in rec.scripts_json or []:
                s_idx = int(entry.get("index") or 0)
                if not str(entry.get("text") or "").strip():
                    continue
                if not include_already_generated and (rec.id, s_idx) in already_done:
                    continue
                chosen.append((rec, entry))

    if not chosen:
        raise AppError(409, "feishu_nothing_to_generate", "没有可入队生成的飞书脚本，请先点击「一键同步飞书」或勾选需要重生成的脚本")

    q_opts = quality or {
        "resolution": "4k",
        "mode": "fast",
        "aspect_ratio": "9:16",
        "processing": "ai_super_resolution",
    }
    batch = FeishuGenerationBatch(
        status="queued",
        archive_dir=archive_dir,
        total_items=len(chosen),
        completed_items=0,
        failed_items=0,
        options_json={"quality": q_opts, "width": width, "height": height},
        download_report_json={},
    )
    session.add(batch)
    session.flush()

    for order_idx, (rec, entry) in enumerate(chosen, start=1):
        s_idx = int(entry.get("index") or order_idx)
        s_col = str(entry.get("column") or f"脚本{s_idx}")
        session.add(
            FeishuGenerationQueueItem(
                batch_id=batch.id,
                sync_record_id=rec.id,
                feishu_record_id=rec.feishu_record_id,
                product_name=rec.product_name or "未命名产品",
                task_name=rec.task_name or rec.product_name or rec.feishu_record_id,
                script_index=s_idx,
                script_column=s_col,
                script_text=str(entry.get("text") or "").strip(),
                status="queued",
                order_index=order_idx,
            )
        )
    session.commit()
    return batch_dto(session, batch)


def cancel_generation_batch(session: Session, batch_id: str) -> dict[str, Any]:
    batch = session.get(FeishuGenerationBatch, batch_id)
    if not batch:
        raise AppError(404, "feishu_batch_missing", "批量生成任务不存在")
    for item in session.scalars(select(FeishuGenerationQueueItem).where(FeishuGenerationQueueItem.batch_id == batch.id)):
        if item.status in ("queued", "preparing"):
            item.status = "cancelled"
            item.finished_at = utcnow()
    if batch.status not in TERMINAL_BATCH_STATUSES:
        batch.status = "cancelled"
        batch.finished_at = utcnow()
    session.commit()
    return batch_dto(session, batch)


def trigger_batch_redownload(session: Session, batch_id: str) -> dict[str, Any]:
    batch = session.get(FeishuGenerationBatch, batch_id)
    if not batch:
        raise AppError(404, "feishu_batch_missing", "批量生成任务不存在")
    completed = list(
        session.scalars(
            select(FeishuGenerationQueueItem).where(
                FeishuGenerationQueueItem.batch_id == batch.id,
                FeishuGenerationQueueItem.status == "completed",
                FeishuGenerationQueueItem.output_relative_path.is_not(None),
            )
        )
    )
    if not completed:
        raise AppError(409, "feishu_batch_no_outputs", "该批次尚无已完成的成片可下载")
    batch.status = "ready_for_download"
    batch.error_message = None
    session.commit()
    return batch_dto(session, batch)


def _prepare_and_start_queue_item(session: Session, settings, batch: FeishuGenerationBatch, item: FeishuGenerationQueueItem) -> None:
    from app.api.routes import approve_record
    from app.schemas import ApprovalRequest
    from app.schemas_cinematic import MotionReferenceInput
    from app.schemas_product_library import ExistingProjectProductBind, ExistingProjectShotProduct
    from app.schemas_studio import PipelineStart, ProjectReferenceVideoConfig
    from app.services.parser import DeterministicScriptParser
    from app.services import product_library as pl_service
    from app.services import project_reference_videos as prv_service
    from app.services.studio_pipeline import create_pipeline

    rec = session.get(FeishuSyncRecord, item.sync_record_id)
    if not rec:
        raise AppError(404, "feishu_record_missing", "对应的飞书同步记录不存在")

    base_label = item.task_name or item.product_name or item.feishu_record_id
    project_name = f"{item.product_name}_{base_label}_{item.script_column}" if item.product_name and item.product_name not in base_label else f"{base_label}_{item.script_column}"
    project = m.Project(
        name=project_name[:180],
        source_type="text",
        source_text=item.script_text,
        status="draft",
    )
    session.add(project)
    session.flush()

    parsed = DeterministicScriptParser().parse(item.script_text)
    if not parsed.get("shots"):
        raise AppError(422, "feishu_script_no_shots", f"{item.script_column} 无法解析出分镜，请检查脚本格式")

    script = m.ScriptRevision(
        project_id=project.id,
        revision=1,
        parsed_json=parsed,
        raw_text=item.script_text,
        status="draft",
    )
    session.add(script)
    session.flush()

    shots: list[m.StoryboardShot] = []
    for s_item in parsed["shots"]:
        shot = m.StoryboardShot(project_id=project.id, **s_item)
        session.add(shot)
        session.flush()
        scene = m.Scene(
            project_id=project.id,
            script_revision_id=script.id,
            order=shot.order,
            name=shot.location or shot.scene_title,
            environment_json={"location": shot.location, "lighting": shot.lighting},
            source_json={"parser": parsed["parser"], "suggested": ["feishu_sync"]},
        )
        session.add(scene)
        session.flush()
        session.add(m.ShotDetail(shot_id=shot.id, scene_id=scene.id, source_json={"script_revision_id": script.id}))
        shots.append(shot)
    session.flush()

    # Bind synced reference video as project default motion reference
    rv_obj = session.get(m.ReferenceVideo, rec.reference_video_id) if rec.reference_video_id else None
    if rv_obj and rv_obj.sha256:
        prv_service.link_uploaded_video(session, project.id, rv_obj.id)
        meta = rv_obj.metadata_json or {}
        dur = float(meta.get("duration_seconds") or 5.0)
        if dur <= 0.1:
            dur = 5.0
        cur_cfg = prv_service.project_config(session, project.id)
        prv_service.save_project_config(
            session,
            project.id,
            ProjectReferenceVideoConfig(
                expected_revision=int(cur_cfg.get("revision") or 0),
                reference_video_defaults=[
                    MotionReferenceInput(
                        video_id=rv_obj.id,
                        expected_revision=int(rv_obj.revision or 1),
                        source_sha256=rv_obj.sha256,
                        trim_start_seconds=0.0,
                        trim_end_seconds=dur,
                        authorized_for_motion=True,
                        authorized_for_reference=True,
                        reference_role="combined",
                    )
                ],
                shot_reference_video_overrides={},
            ),
        )

    # Bind synced product reference images to all draft shots BEFORE approving
    img_ids = [iid for iid in (rec.product_image_ids_json or []) if session.get(m.ProductImage, iid)]
    if rec.product_id and img_ids and session.get(m.Product, rec.product_id):
        pl_service.bind_existing_project(
            session,
            settings,
            project.id,
            ExistingProjectProductBind(
                product_id=rec.product_id,
                image_ids=img_ids[:9],
                shots=[ExistingProjectShotProduct(shot_id=s.id, image_ids=img_ids[:4]) for s in shots],
            ),
        )

    # Approve script & all shots
    now = utcnow()
    approval_payload = ApprovalRequest(reviewer="feishu_batch", comment="飞书一键生成自动审核")
    script.status = "approved"
    script.approved_at = now
    approve_record(session, "script", script.id, script.revision, "approved", approval_payload)
    for shot in shots:
        shot.status = "approved"
        shot.approved_at = now
        shot.rejection_reason = None
        approve_record(session, "shot", shot.id, shot.revision, "approved", approval_payload)
    project.status = "script_approved"
    session.flush()

    opts = dict(batch.options_json or {})
    start_payload = PipelineStart(
        dialogue_audio_policy="auto",
        automatic_draft_assembly=False,
        automatic_final_assembly=True,
        quality=opts.get("quality"),
        allow_cloud_costs=False,
        allow_paid_fallback=False,
        width=int(opts.get("width") or 576),
        height=int(opts.get("height") or 1024),
        max_retries=2,
    )
    run = create_pipeline(session, settings, project.id, start_payload)
    item.project_id = project.id
    item.studio_run_id = run.id
    item.status = "running"
    batch.status = "running"
    session.commit()


def _resolve_project_final_video_path(session: Session, project_id: str) -> str | None:
    render = session.scalar(
        select(m.AssemblyRender)
        .join(m.Assembly, m.Assembly.id == m.AssemblyRender.assembly_id)
        .where(m.Assembly.project_id == project_id, m.AssemblyRender.status == "succeeded")
        .order_by(m.AssemblyRender.created_at.desc())
        .limit(1)
    )
    if render and render.relative_path:
        return render.relative_path
    rev = session.scalar(
        select(m.VideoRevision)
        .where(m.VideoRevision.project_id == project_id)
        .order_by(m.VideoRevision.created_at.desc())
        .limit(1)
    )
    return rev.relative_path if rev and rev.relative_path else None


def advance_feishu_queue_once(session: Session, settings) -> bool:
    """One deterministic tick of the Feishu serial batch video scheduler. Returns True if state changed."""
    from app.services.studio_pipeline import RUN_TERMINAL

    changed = False
    # 1. Reconcile any currently running queue items
    running_items = list(
        session.scalars(
            select(FeishuGenerationQueueItem).where(FeishuGenerationQueueItem.status == "running")
        )
    )
    for item in running_items:
        run = session.get(m.StudioRun, item.studio_run_id) if item.studio_run_id else None
        if not run:
            item.status = "failed"
            item.error_message = "关联的自动制作任务丢失"
            item.finished_at = utcnow()
            changed = True
            continue
        if run.status == "completed":
            item.output_relative_path = _resolve_project_final_video_path(session, item.project_id) if item.project_id else None
            item.status = "completed"
            item.finished_at = utcnow()
            changed = True
        elif run.status in (RUN_TERMINAL | {"exceptions"}):
            item.status = "failed"
            item.error_message = f"制作任务结束状态：{run.status}"
            item.finished_at = utcnow()
            changed = True

    if changed:
        session.commit()

    # 2. Update batch counters & promote finished batches to ready_for_download
    active_batches = list(
        session.scalars(
            select(FeishuGenerationBatch)
            .where(FeishuGenerationBatch.status.in_(["queued", "running"]))
            .order_by(FeishuGenerationBatch.created_at.asc())
        )
    )
    for batch in active_batches:
        items = list(
            session.scalars(
                select(FeishuGenerationQueueItem)
                .where(FeishuGenerationQueueItem.batch_id == batch.id)
                .order_by(FeishuGenerationQueueItem.order_index, FeishuGenerationQueueItem.created_at)
            )
        )
        completed_n = sum(1 for x in items if x.status == "completed")
        failed_n = sum(1 for x in items if x.status == "failed")
        terminal_n = sum(1 for x in items if x.status in TERMINAL_ITEM_STATUSES)
        if batch.completed_items != completed_n or batch.failed_items != failed_n:
            batch.completed_items = completed_n
            batch.failed_items = failed_n
            changed = True
        if items and terminal_n == len(items):
            has_outputs = any(x.status == "completed" and x.output_relative_path for x in items)
            batch.status = "ready_for_download" if has_outputs else "failed"
            if not has_outputs:
                batch.error_message = "本批次没有成功生成的视频成片"
            batch.finished_at = utcnow()
            changed = True
    if changed:
        session.commit()

    # 3. Start next queued item if nothing is actively running in Feishu queue
    still_running = session.scalar(
        select(FeishuGenerationQueueItem.id).where(FeishuGenerationQueueItem.status == "running").limit(1)
    )
    if still_running:
        return changed

    for batch in active_batches:
        if batch.status not in ("queued", "running"):
            continue
        next_item = session.scalar(
            select(FeishuGenerationQueueItem)
            .where(FeishuGenerationQueueItem.batch_id == batch.id, FeishuGenerationQueueItem.status == "queued")
            .order_by(FeishuGenerationQueueItem.order_index, FeishuGenerationQueueItem.created_at)
            .limit(1)
        )
        if not next_item:
            continue
        try:
            _prepare_and_start_queue_item(session, settings, batch, next_item)
        except Exception as exc:
            logger.exception("Feishu queue item %s failed to start", next_item.id)
            session.rollback()
            fresh = session.get(FeishuGenerationQueueItem, next_item.id)
            if fresh:
                fresh.status = "failed"
                fresh.error_message = str(exc)[:500]
                fresh.finished_at = utcnow()
                session.commit()
        return True
    return changed


async def feishu_queue_worker(settings, session_factory) -> None:
    while True:
        try:
            def _tick():
                with session_factory() as session:
                    return advance_feishu_queue_once(session, settings)
            await asyncio.to_thread(_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Feishu queue worker tick error")
        await asyncio.sleep(4)


def _safe_filename(name: str, fallback: str = "video") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip()).strip(". ")
    return (cleaned[:120] or fallback)


def claim_next_download_batch(session: Session) -> dict[str, Any] | None:
    batch = session.scalar(
        select(FeishuGenerationBatch)
        .where(FeishuGenerationBatch.status == "ready_for_download")
        .order_by(FeishuGenerationBatch.created_at.asc())
        .limit(1)
    )
    if not batch:
        return None
    items = list(
        session.scalars(
            select(FeishuGenerationQueueItem)
            .where(
                FeishuGenerationQueueItem.batch_id == batch.id,
                FeishuGenerationQueueItem.status == "completed",
                FeishuGenerationQueueItem.output_relative_path.is_not(None),
            )
            .order_by(FeishuGenerationQueueItem.order_index)
        )
    )
    batch.status = "downloading"
    session.commit()
    manifest = []
    for item in items:
        ext = Path(item.output_relative_path or "video.mp4").suffix or ".mp4"
        product_folder = _safe_filename(item.product_name, "未命名产品")
        base_file = _safe_filename(f"{item.task_name or item.product_name}_{item.script_column}", f"video_{item.script_index}")
        manifest.append({
            "item_id": item.id,
            "product_name": item.product_name,
            "product_folder": product_folder,
            "task_name": item.task_name,
            "script_index": item.script_index,
            "script_column": item.script_column,
            "filename": f"{base_file}{ext}",
            "output_relative_path": item.output_relative_path,
            "download_url": f"/api/feishu/bridge/download/{item.id}",
        })
    return {
        "batch_id": batch.id,
        "archive_dir": batch.archive_dir or r"E:\vast视频",
        "items": manifest,
    }


def complete_download_batch(session: Session, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    batch = session.get(FeishuGenerationBatch, batch_id)
    if not batch:
        raise AppError(404, "feishu_batch_missing", "批量生成任务不存在")
    ok = bool(payload.get("ok", True))
    archived_map = payload.get("archived_paths") or {}
    for item_id, local_path in archived_map.items():
        item = session.get(FeishuGenerationQueueItem, item_id)
        if item and item.batch_id == batch.id:
            item.archived_local_path = str(local_path)[:1000]
    batch.download_report_json = dict(payload)
    batch.status = "archived" if ok else "ready_for_download"
    batch.error_message = None if ok else str(payload.get("error_message") or "本地下载归档失败")
    session.commit()
    return batch_dto(session, batch)
