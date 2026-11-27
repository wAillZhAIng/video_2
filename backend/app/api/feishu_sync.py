"""FastAPI endpoints for Feishu Bitable sync, one-click batch generation, and local Windows bridge."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.models.feishu import FeishuGenerationQueueItem
from app.services import feishu_orchestrator as svc
from app.services.content_manager import safe_file

router = APIRouter(prefix="/api/feishu", tags=["feishu-sync"])


class SyncTriggerRequest(BaseModel):
    force_rerun: bool = False
    engine: str | None = Field(default=None, max_length=32)


class GeneratePreviewRequest(BaseModel):
    refresh_from_feishu: bool = True


class GenerateSelectionItem(BaseModel):
    sync_record_id: str
    script_index: int = Field(ge=1, le=20)


class GenerateBatchRequest(BaseModel):
    selections: list[GenerateSelectionItem] | None = None
    include_already_generated: bool = False
    refresh_from_feishu: bool = True
    quality: dict[str, Any] | None = None
    width: int = Field(default=576, ge=256, le=3840)
    height: int = Field(default=1024, ge=256, le=3840)


@router.get("/status")
def feishu_status(session: Session = Depends(get_db)):
    return svc.get_overview_status(session)


@router.post("/sync", status_code=201)
def trigger_sync(payload: SyncTriggerRequest, session: Session = Depends(get_db)):
    return svc.create_sync_job(session, force_rerun=payload.force_rerun, engine_override=payload.engine)


@router.post("/sync/{job_id}/cancel")
def cancel_sync(job_id: str, session: Session = Depends(get_db)):
    return svc.cancel_sync_job(session, job_id)


@router.post("/generate/preview")
def generate_preview(payload: GeneratePreviewRequest, session: Session = Depends(get_db)):
    items = svc.preview_generation_candidates(session, refresh_from_feishu=payload.refresh_from_feishu)
    return {"items": items, "total": len(items)}


@router.post("/generate", status_code=201)
def trigger_generate(payload: GenerateBatchRequest, session: Session = Depends(get_db)):
    sels = [s.model_dump() for s in payload.selections] if payload.selections else None
    return svc.start_generation_batch(
        session,
        selections=sels,
        include_already_generated=payload.include_already_generated,
        refresh_from_feishu=payload.refresh_from_feishu,
        quality=payload.quality,
        width=payload.width,
        height=payload.height,
    )


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str, session: Session = Depends(get_db)):
    return svc.cancel_generation_batch(session, batch_id)


@router.post("/batches/{batch_id}/redownload")
def redownload_batch(batch_id: str, session: Session = Depends(get_db)):
    return svc.trigger_batch_redownload(session, batch_id)


# --- Local Windows Bridge Endpoints ---

@router.post("/bridge/heartbeat")
def bridge_heartbeat(payload: dict[str, Any], session: Session = Depends(get_db)):
    return svc.record_bridge_heartbeat(session, payload)


@router.post("/bridge/token")
def bridge_token(session: Session = Depends(get_db)):
    return svc.mint_bridge_token(session)


@router.post("/bridge/claim-sync")
def bridge_claim_sync(session: Session = Depends(get_db)):
    claimed = svc.claim_next_sync_job(session)
    return {"claimed": claimed}


@router.post("/bridge/sync/{job_id}/progress")
def bridge_sync_progress(job_id: str, payload: dict[str, Any], session: Session = Depends(get_db)):
    return svc.update_sync_progress(session, job_id, payload)


@router.post("/bridge/sync/{job_id}/upload-media")
async def bridge_upload_media(
    job_id: str,
    request: Request,
    feishu_record_id: str = Form(...),
    product_name: str = Form(""),
    task_name: str = Form(""),
    local_video_path: str = Form(""),
    local_image_paths_json: str = Form("[]"),
    video: UploadFile | None = File(default=None),
    images: list[UploadFile] | None = File(default=None),
    session: Session = Depends(get_db),
):
    video_name = Path(video.filename or "reference.mp4").name if video else None
    video_bytes = await video.read() if video else None
    image_uploads: list[tuple[str, bytes]] = []
    for img in images or []:
        data = await img.read()
        if data:
            image_uploads.append((Path(img.filename or "image.png").name, data))
    try:
        local_img_paths = json.loads(local_image_paths_json) if local_image_paths_json else []
    except ValueError:
        local_img_paths = []
    return svc.ingest_record_media(
        session,
        request.app.state.settings,
        job_id,
        feishu_record_id=feishu_record_id,
        product_name=product_name,
        task_name=task_name,
        video_filename=video_name,
        video_bytes=video_bytes,
        image_uploads=image_uploads,
        local_video_path=local_video_path,
        local_image_paths=local_img_paths,
    )


@router.post("/bridge/sync/{job_id}/record-complete")
def bridge_record_complete(job_id: str, payload: dict[str, Any], session: Session = Depends(get_db)):
    return svc.complete_record_sync(session, job_id, payload)


@router.post("/bridge/sync/{job_id}/complete")
def bridge_sync_complete(job_id: str, payload: dict[str, Any], session: Session = Depends(get_db)):
    return svc.complete_sync_job(session, job_id, payload)


@router.post("/bridge/claim-download")
def bridge_claim_download(session: Session = Depends(get_db)):
    claimed = svc.claim_next_download_batch(session)
    return {"claimed": claimed}


@router.get("/bridge/download/{item_id}")
def bridge_download_item(item_id: str, request: Request, session: Session = Depends(get_db)):
    item = session.get(FeishuGenerationQueueItem, item_id)
    if not item or not item.output_relative_path:
        raise AppError(404, "feishu_output_missing", "找不到该任务对应的成片文件")
    path = safe_file(request.app.state.settings.media_root, item.output_relative_path)
    if not path.is_file():
        raise AppError(404, "feishu_output_file_missing", "磁盘上未找到对应成片文件")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.post("/bridge/download/{batch_id}/complete")
def bridge_download_complete(batch_id: str, payload: dict[str, Any], session: Session = Depends(get_db)):
    return svc.complete_download_batch(session, batch_id, payload)
