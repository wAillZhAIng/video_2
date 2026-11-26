from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import mimetypes
import uuid
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import case, delete, func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import AppError
from app.core.paths import media_relative, safe_media_path
from app.models import (
    Approval,
    AuditEvent,
    Character,
    CharacterAsset,
    CharacterVersion,
    GenerationJob,
    Project,
    PublishBatch,
    PublishingAccount,
    PublishingPreset,
    QualityCheck,
    ScriptRevision,
    StoryboardShot,
    ReferenceVideo,
    VideoRevision,
    SourceDocument,
    FinalComposition,
)
from app.models.entities import new_id
from app.schemas import (
    ApprovalRequest,
    ResolveUnknownLocalGeneration,
    CharacterAssignment,
    CharacterCreate,
    CharacterVersionCreate,
    DryRunRequest,
    GenerationRequest,
    ProjectCreate,
    ProjectPatch,
    PublishingAccountCreate,
    PublishingAccountPatch,
    PublishingPresetCreate,
    ReviewRequest,
    RevisionRequest,
    ScriptPatch,
    ShotCreate,
    ShotPatch,
    TextImportRequest,
    UrlImportRequest,
    AssetReviewRequest,
    CompositionRequest,
    FinalDryRunRequest,
)
from app.services.import_persistence import persist_upload
from app.services.imports import (
    fetch_external_document,
    parse_uploaded_document,
    url_without_query_or_fragment,
)
from app.services.parser import DeterministicScriptParser
from app.services.providers import VideoGenerationRequest, provider_for
from app.services.quality import inspect_media


router = APIRouter(prefix="/api")
ModelT = TypeVar("ModelT")


PUBLISHER_CAPABILITIES = {
    "douyin": {"display_name": "抖音", "status": "not_configured", "requires_oauth": True, "requires_platform_review": True, "real_publish_enabled": False},
    "kuaishou": {"display_name": "快手", "status": "not_configured", "requires_oauth": True, "requires_platform_review": True, "real_publish_enabled": False},
    "xiaohongshu": {"display_name": "小红书", "status": "not_configured", "requires_oauth": True, "requires_platform_review": True, "real_publish_enabled": False},
    "wechat_channels": {"display_name": "微信视频号", "status": "not_configured", "requires_oauth": True, "requires_platform_review": True, "real_publish_enabled": False},
}

CHARACTER_ASSET_TYPES = {
    "front_face",
    "left_45",
    "right_45",
    "profile",
    "half_body",
    "full_body",
    "expression",
    "outfit",
    "reference_video",
    "reference_audio",
}


def settings_from(request: Request) -> Settings:
    return request.app.state.settings


def row_dict(row: Any) -> dict[str, Any]:
    return {column.key: getattr(row, column.key) for column in inspect(row).mapper.column_attrs}


def required(session: Session, model: type[ModelT], object_id: str, label: str) -> ModelT:
    instance = session.get(model, object_id)
    if instance is None:
        raise AppError(404, "not_found", f"{label}不存在")
    return instance


def audit(session: Session, object_type: str, object_id: str, action: str, details: dict[str, Any] | None = None) -> None:
    session.add(AuditEvent(object_type=object_type, object_id=object_id, action=action, details_json=details or {}))
    if object_type == "project" and action in {"import_text", "import_file", "import_url"}:
        project = session.get(Project, object_id)
        session.add(SourceDocument(project_id=object_id, filename=project.source_filename, source_uri=project.source_uri, original_text=project.source_text, sha256=hashlib.sha256(project.source_text.encode("utf-8")).hexdigest()))


def invalidate_shot_videos(session: Session, shot_id: str) -> int:
    count = 0
    for video in session.scalars(select(VideoRevision).where(VideoRevision.shot_id == shot_id)):
        if video.review_status in {"approved", "pending"}:
            video.review_status = "needs_regeneration"
            count += 1
    return count


def public_account_dict(account: PublishingAccount) -> dict[str, Any]:
    result = row_dict(account)
    result.pop("credential_reference", None)
    return result


def approve_record(session: Session, object_type: str, object_id: str, revision: int, decision: str, payload: ApprovalRequest | ReviewRequest) -> None:
    session.add(
        Approval(
            object_type=object_type,
            object_id=object_id,
            revision=revision,
            decision=decision,
            reason=payload.reason,
            operator=payload.operator,
        )
    )
    audit(session, object_type, object_id, decision, {"revision": revision, "reason": payload.reason, "operator": payload.operator})


def latest_script(session: Session, project_id: str) -> ScriptRevision | None:
    return session.scalar(
        select(ScriptRevision)
        .where(ScriptRevision.project_id == project_id)
        .order_by(ScriptRevision.revision.desc())
        .limit(1)
    )


def script_for_shot(session,shot):
    from app.models import ShotDetail, Scene
    detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==shot.id))
    scene=session.get(Scene,detail.scene_id) if detail and detail.scene_id else None
    return session.get(ScriptRevision,scene.script_revision_id) if scene and scene.script_revision_id else latest_script(session,shot.project_id)


def project_shot_statement(session,project_id,include_history=False,script_revision_id=None,include_deleted=False):
    from app.models import Scene, ShotDetail
    statement=select(StoryboardShot).where(StoryboardShot.project_id==project_id)
    if include_history:return statement
    if not include_deleted:statement=statement.where(StoryboardShot.status!='deleted')
    current=session.get(ScriptRevision,script_revision_id) if script_revision_id else latest_script(session,project_id)
    if script_revision_id and not current:raise AppError(404,'script_missing','剧本版本不存在')
    if current and current.project_id!=project_id:raise AppError(422,'script_mismatch','剧本版本不属于当前项目')
    if current and 'explicit_shot_ids' in current.parsed_json:
        # New Word revisions may retain unchanged shot IDs belonging to their
        # original scene/script snapshot. Membership must not be inferred from
        # the scene's historical script_revision_id in that case.
        members=current.parsed_json['explicit_shot_ids']
        statement=statement.where(StoryboardShot.id.in_(members))
        return statement.order_by(case({identifier:n for n,identifier in enumerate(members)},value=StoryboardShot.id,else_=len(members))) if members else statement
    if current and not script_revision_id and not session.scalar(select(Scene.id).where(Scene.project_id==project_id,Scene.script_revision_id==current.id).limit(1)):
        previous=session.scalar(select(ScriptRevision).join(Scene,Scene.script_revision_id==ScriptRevision.id).where(Scene.project_id==project_id).order_by(ScriptRevision.revision.desc()).limit(1))
        if previous:current=previous
    if current and session.scalar(select(Scene.id).where(Scene.project_id==project_id,Scene.script_revision_id==current.id).limit(1)):
        statement=statement.where(StoryboardShot.id.in_(select(ShotDetail.shot_id).join(Scene,Scene.id==ShotDetail.scene_id).where(Scene.script_revision_id==current.id)))
    return statement


def add_shot_to_current_revision(session,shot):
    """Advanced add/duplicate joins an imported revision's explicit membership."""
    current=latest_script(session,shot.project_id)
    if current and 'explicit_shot_ids' in current.parsed_json:
        members=list(current.parsed_json['explicit_shot_ids'])
        if shot.id not in members:
            current.parsed_json={**current.parsed_json,'explicit_shot_ids':[*members,shot.id]}


@router.get("/health")
def health(request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    session.execute(select(1))
    settings = settings_from(request)
    return {
        "status": "ok",
        "app": "character-consistency-video-factory",
        "version": "0.1.0",
        "database": "connected",
        "mode": "demo" if settings.h3_demo_mode else "real",
        "real_model_verified": False,
        "timestamp": datetime.now(timezone.utc),
    }


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "projects": session.scalar(select(func.count()).select_from(Project)),
        "pending_shots": session.scalar(select(func.count()).select_from(StoryboardShot).where(StoryboardShot.status == "draft")),
        "running_jobs": session.scalar(select(func.count()).select_from(GenerationJob).where(GenerationJob.status.in_(["queued", "running"]))),
        "pending_video_reviews": session.scalar(select(func.count()).select_from(VideoRevision).where(VideoRevision.review_status == "pending")),
    }


@router.get("/providers/h3/health")
async def h3_health(request: Request) -> dict[str, Any]:
    return (await provider_for(settings_from(request)).health()).to_dict()


@router.get("/providers/h3/capabilities")
async def h3_capabilities(request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    settings = settings_from(request)
    health_state = await provider_for(settings).health()
    evidence = None
    for video in session.scalars(select(VideoRevision).order_by(VideoRevision.created_at.desc()).limit(50)):
        details = video.metadata_json
        if details.get("provider") == "comfyui_h3" and details.get("mode") == "local" and details.get("playable_video") and details.get("workflow_version") == health_state.workflow_name:
            evidence = {"video_id": video.id, "comfyui_prompt_id": details.get("comfyui_prompt_id"), "workflow_version": details.get("workflow_version"), "meaning": "该工作流已有真实本地加载/可解码输出证据；不代表所有权重当前常驻显存"}
            break
    return {
        "provider": health_state.provider,
        "mode": health_state.mode,
        "ready": health_state.ready,
        "reference_to_video": health_state.reference_to_video_available,
        "supported_aspect_ratios": ["9:16", "16:9", "1:1"] if settings.h3_demo_mode else [],
        "workflow_specific_limits": "以已配置并验证的 ComfyUI API 工作流为准",
        "real_model_verified": evidence is not None,
        "inference_evidence": evidence,
        "demo_disclaimer": "Demo 仅验证状态机，不会输出或冒充真实视频" if settings.h3_demo_mode else None,
    }


@router.post("/imports/text", status_code=status.HTTP_201_CREATED)
def import_text(payload: TextImportRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = Project(
        name=payload.name,
        source_type=payload.source_type,
        source_text=payload.text,
        status="draft",
    )
    session.add(project)
    session.flush()
    audit(session, "project", project.id, "import_text", {"source_type": payload.source_type})
    session.commit()
    return row_dict(project)


@router.post("/imports/file", status_code=status.HTTP_201_CREATED)
async def import_file(
    request: Request,
    name: str = Form(..., min_length=1, max_length=200),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    settings = settings_from(request)
    original_name = Path(file.filename or "upload").name
    data = await file.read(settings.import_max_bytes + 1)
    return await run_in_threadpool(_save_imported_file, request.app.state.session_factory,
                                   settings, name, original_name, data)


def _save_imported_file(session_factory, settings: Settings, name: str, original_name: str,
                        data: bytes) -> dict[str, Any]:
    text, mime = parse_uploaded_document(original_name, data, settings.import_max_bytes)
    project_id = new_id("prj")
    extension = Path(original_name).suffix.lower()
    relative_path = f"imports/{project_id}/{uuid.uuid4().hex}{extension}"
    destination = safe_media_path(settings.media_root, relative_path)
    with session_factory() as session:
        def stage_project():
            project = Project(id=project_id, name=name, source_type="file", source_filename=original_name,
                              source_text=text, source_uri=relative_path, status="draft")
            session.add(project)
            session.flush()
            audit(session, "project", project.id, "import_file",
                  {"filename": original_name, "mime_type": mime, "size_bytes": len(data)})
            return project

        project = persist_upload(session, destination, data, stage_project)
        return {**row_dict(project), "detected_mime_type": mime}


@router.post("/imports/url", status_code=status.HTTP_201_CREATED)
async def import_url(request: Request, payload: UrlImportRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    text, final_url = await fetch_external_document(str(payload.url), settings_from(request))
    stored_url = url_without_query_or_fragment(final_url)
    project = Project(name=payload.name, source_type="url", source_uri=stored_url, source_text=text, status="draft")
    session.add(project)
    session.flush()
    audit(session, "project", project.id, "import_url", {"url": stored_url})
    session.commit()
    return row_dict(project)


@router.get("/projects")
def list_projects(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [row_dict(row) for row in session.scalars(select(Project).order_by(Project.updated_at.desc())).all()]


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = Project(**payload.model_dump())
    session.add(project)
    session.commit()
    return row_dict(project)


@router.get("/projects/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = required(session, Project, project_id, "项目")
    result = row_dict(project)
    result["shot_count"] = session.scalar(select(func.count()).select_from(StoryboardShot).where(StoryboardShot.project_id == project_id))
    result["latest_script_revision"] = row_dict(script) if (script := latest_script(session, project_id)) else None
    return result


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, payload: ProjectPatch, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = required(session, Project, project_id, "项目")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, key, value)
    audit(session, "project", project.id, "update", payload.model_dump(exclude_unset=True))
    session.commit()
    return row_dict(project)


@router.post("/projects/{project_id}/parse")
def parse_project(project_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = required(session, Project, project_id, "项目")
    if not project.source_text.strip():
        raise AppError(422, "empty_script", "项目没有可解析的原始脚本")
    parsed = DeterministicScriptParser().parse(project.source_text)
    current = latest_script(session, project_id)
    revision = (current.revision + 1) if current else 1
    script = ScriptRevision(project_id=project_id, revision=revision, parsed_json=parsed, raw_text=project.source_text, status="draft")
    session.add(script)
    session.flush()
    from app.models import Scene, ShotDetail
    for item in parsed["shots"]:
        shot=StoryboardShot(project_id=project_id, **item);session.add(shot);session.flush()
        scene=Scene(project_id=project_id,script_revision_id=script.id,order=shot.order,name=shot.location or shot.scene_title,environment_json={'location':shot.location,'lighting':shot.lighting},source_json={'parser':parsed['parser'],'suggested':['scene_grouping']})
        session.add(scene);session.flush();session.add(ShotDetail(shot_id=shot.id,scene_id=scene.id,source_json={'script_revision_id':script.id}))
    project.status = "script_parsed"
    audit(session, "project", project.id, "parse_script", {"script_revision": revision, "shot_count": len(parsed["shots"]), "parser": parsed["parser"]})
    session.commit()
    return {"script": row_dict(script), "shot_count": len(parsed["shots"]), "warnings": parsed["warnings"]}


@router.get("/projects/{project_id}/script")
def get_script(project_id: str, session: Session = Depends(get_db)) -> dict[str, Any] | None:
    required(session, Project, project_id, "项目")
    script = latest_script(session, project_id)
    if not script:
        return None
    return row_dict(script)


@router.patch("/projects/{project_id}/script")
def patch_script(project_id: str, payload: ScriptPatch, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = required(session, Project, project_id, "项目")
    previous = latest_script(session, project_id)
    revision = (previous.revision + 1) if previous else 1
    script = ScriptRevision(project_id=project_id, revision=revision, parsed_json={}, raw_text=payload.raw_text, status="draft")
    project.source_text = payload.raw_text
    project.status = "draft"
    session.add(script)
    # Editing source creates a new revision; prior shots keep their own reviewed
    # script and media. Parsing creates new shots instead of rewriting history.
    audit(session, "project", project.id, "script_source_updated", {"script_revision": revision, "invalidated_video_count": 0, "previous_script_id": previous.id if previous else None})
    session.commit()
    return row_dict(script)


@router.post("/projects/{project_id}/script/approve")
def approve_script(project_id: str, payload: ApprovalRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    project = required(session, Project, project_id, "项目")
    script = latest_script(session, project_id)
    from app.models import Scene, ShotDetail
    linked_shot = session.scalar(select(ShotDetail.id).join(Scene, Scene.id == ShotDetail.scene_id).where(Scene.script_revision_id == script.id).limit(1)) if script else None
    if not script or not (script.parsed_json.get("shots") or linked_shot):
        raise AppError(409, "script_not_parsed", "脚本必须先完成解析")
    script.status = "approved"
    script.approved_at = datetime.now(timezone.utc)
    script.approved_by = payload.operator
    project.status = "script_approved"
    approve_record(session, "script", script.id, script.revision, "approved", payload)
    session.commit()
    return row_dict(script)


@router.get("/projects/{project_id}/shots")
def list_shots(
    project_id: str,
    request: Request,
    include_deleted: bool = False,
    include_history: bool = False,
    script_revision_id: str | None = None,
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    required(session, Project, project_id, "项目")
    statement = project_shot_statement(session,project_id,include_history,script_revision_id,include_deleted=include_deleted)
    if not include_deleted:
        statement = statement.where(StoryboardShot.status != "deleted")
    current=session.get(ScriptRevision,script_revision_id) if script_revision_id else latest_script(session,project_id)
    positions={identifier:n for n,identifier in enumerate(current.parsed_json['explicit_shot_ids'],1)} if current and not include_history and 'explicit_shot_ids' in current.parsed_json else {}
    from app.models import ShotCharacter
    result=[]
    for shot in session.scalars(statement.order_by(StoryboardShot.order)).all():
        from app.models import ShotDetail, Scene
        detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==shot.id))
        scene=session.get(Scene,detail.scene_id) if detail and detail.scene_id else None
        people=list(session.scalars(select(ShotCharacter).where(ShotCharacter.shot_id==shot.id).order_by(ShotCharacter.order)))
        participants=[]
        for p in people:
            c=session.get(Character,p.character_id);v=session.get(CharacterVersion,p.character_version_id) if p.character_version_id else None
            refs=[session.get(CharacterAsset,k) for k in p.reference_asset_ids_json]
            ready=bool(v and v.approval_state=='approved' and refs and all(a and a.status=='approved' and a.character_version_id==v.id for a in refs))
            participants.append({'id':p.id,'character_id':p.character_id,'name':c.display_name if c else '人物缺失','ready':ready})
        from app.services.product_library import shot_references
        product_ready=False
        try:product_ready=bool(shot_references(session,settings_from(request),shot))
        except AppError:pass
        native_text_ready = bool(current and current.status == 'approved')
        result.append({**row_dict(shot),**({'order':positions[shot.id],'source_order':shot.order} if shot.id in positions else {}),'scene_id':scene.id if scene else None,'scene_name':scene.name if scene else None,'script_revision_id':scene.script_revision_id if scene else None,'participants':participants,'generation_ready':all(p['ready'] for p in participants) if participants else product_ready or native_text_ready or bool(shot.character_id and shot.character_asset_version_id)})
    return result


@router.get("/projects/{project_id}/jobs")
def list_project_jobs(project_id: str, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    required(session, Project, project_id, "项目")
    return [
        row_dict(row)
        for row in session.scalars(
            select(GenerationJob)
            .where(GenerationJob.project_id == project_id)
            .order_by(GenerationJob.created_at.desc())
        ).all()
    ]


@router.get("/projects/{project_id}/videos")
def list_project_videos(project_id: str, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    required(session, Project, project_id, "项目")
    result: list[dict[str, Any]] = []
    for row in session.scalars(
        select(VideoRevision)
        .where(VideoRevision.project_id == project_id)
        .order_by(VideoRevision.created_at.desc())
    ).all():
        item = row_dict(row)
        item["media_url"] = f"/api/media/{row.relative_path}"
        result.append(item)
    return result


@router.post("/projects/{project_id}/shots", status_code=status.HTTP_201_CREATED)
def create_shot(project_id: str, payload: ShotCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    required(session, Project, project_id, "项目")
    shot = StoryboardShot(project_id=project_id, **payload.model_dump())
    session.add(shot)
    session.flush()
    from app.models import Scene, ShotDetail
    script=latest_script(session,project_id)
    scene=Scene(project_id=project_id,script_revision_id=script.id if script else None,order=shot.order,name=shot.location or shot.scene_title)
    session.add(scene);session.flush();session.add(ShotDetail(shot_id=shot.id,scene_id=scene.id))
    add_shot_to_current_revision(session,shot)
    session.commit()
    return row_dict(shot)


@router.patch("/shots/{shot_id}")
def patch_shot(shot_id: str, payload: ShotPatch, session: Session = Depends(get_db)) -> dict[str, Any]:
    shot = required(session, StoryboardShot, shot_id, "分镜")
    if shot.status == "deleted":
        raise AppError(409, "shot_deleted", "已删除分镜不可编辑")
    active=session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id==shot.id,GenerationJob.status.in_(['submitting','queued','submitted','running','postprocessing','cancelling','reconciling','unknown'])).limit(1))
    if active:raise AppError(409,'generation_active','分镜生成任务尚未结束或结果未知，不能修改输入',{'job_id':active})
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return row_dict(shot)
    was_approved = shot.status == "approved"
    for key, value in changes.items():
        setattr(shot, key, value)
    shot.revision += 1
    if was_approved:
        shot.status = "draft"
        shot.approved_at = None
        invalidate_shot_videos(session, shot.id)
    audit(session, "shot", shot.id, "update", {"fields": list(changes), "approval_invalidated": was_approved, "revision": shot.revision})
    session.commit()
    return row_dict(shot)


@router.post("/shots/{shot_id}/reject")
def reject_shot(shot_id: str, payload: ReviewRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.reason:
        raise AppError(422, "rejection_reason_required", "驳回分镜必须填写原因")
    shot = required(session, StoryboardShot, shot_id, "分镜")
    if shot.status == "deleted":
        raise AppError(409, "shot_deleted", "已删除分镜不可审核")
    active=session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id==shot.id,GenerationJob.status.in_(['submitting','queued','submitted','running','postprocessing','cancelling','reconciling','unknown'])).limit(1))
    if active:raise AppError(409,'generation_active','分镜生成任务尚未结束或结果未知，不能修改审核状态',{'job_id':active})
    shot.status = "rejected"
    shot.approved_at = None
    shot.rejection_reason = payload.reason
    invalidated_videos = invalidate_shot_videos(session, shot.id)
    approve_record(session, "shot", shot.id, shot.revision, "rejected", payload)
    audit(session, "shot", shot.id, "videos_invalidated", {"count": invalidated_videos})
    session.commit()
    return row_dict(shot)


@router.delete("/shots/{shot_id}")
def delete_shot(shot_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    shot = required(session, StoryboardShot, shot_id, "分镜")
    if shot.status == "deleted":
        return {"id": shot.id, "status": "deleted", "deleted": True}
    active_job = session.scalar(
        select(GenerationJob.id)
        .where(
            GenerationJob.shot_id == shot.id,
            GenerationJob.status.in_(["submitting", "queued", "submitted", "running", "postprocessing", "cancelling", "reconciling", "unknown"]),
        )
        .limit(1)
    )
    if active_job:
        raise AppError(
            409,
            "shot_has_active_generation",
            "分镜有运行中的生成任务，取消任务后才能删除",
            {"job_id": active_job},
        )
    previous_status = shot.status
    invalidated_videos = invalidate_shot_videos(session, shot.id)
    shot.status = "deleted"
    shot.approved_at = None
    shot.rejection_reason = "用户删除（软删除，历史生成记录保留）"
    audit(
        session,
        "shot",
        shot.id,
        "soft_deleted",
        {"previous_status": previous_status, "history_preserved": True, "invalidated_video_count": invalidated_videos},
    )
    session.commit()
    return {"id": shot.id, "status": shot.status, "deleted": True, "history_preserved": True}


@router.post("/shots/{shot_id}/approve")
def approve_shot(shot_id: str, payload: ApprovalRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    shot = required(session, StoryboardShot, shot_id, "分镜")
    if shot.status == "deleted":
        raise AppError(409, "shot_deleted", "已删除分镜不可审核")
    script = script_for_shot(session, shot)
    if not script or script.status != "approved":
        raise AppError(409, "script_not_approved", "脚本审核通过后才能审核分镜")
    shot.status = "approved"
    shot.approved_at = datetime.now(timezone.utc)
    shot.rejection_reason = None
    approve_record(session, "shot", shot.id, shot.revision, "approved", payload)
    session.commit()
    return row_dict(shot)


@router.post("/projects/{project_id}/shots/approve-all")
def approve_all_shots(project_id: str, payload: ApprovalRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    script = latest_script(session, project_id)
    if not script or script.status != "approved":
        raise AppError(409, "script_not_approved", "脚本审核通过后才能审核分镜")
    shots = session.scalars(
        project_shot_statement(session,project_id).where(StoryboardShot.status != "deleted")
    ).all()
    if not shots:
        raise AppError(409, "no_shots", "项目没有分镜")
    now = datetime.now(timezone.utc)
    for shot in shots:
        shot.status = "approved"
        shot.approved_at = now
        shot.rejection_reason = None
        approve_record(session, "shot", shot.id, shot.revision, "approved", payload)
    session.commit()
    return {"approved_count": len(shots)}


@router.get("/characters")
def list_characters(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [{**row_dict(row), "avatar_url": character_avatar(session, row)} for row in session.scalars(select(Character).order_by(Character.created_at.desc())).all()]


def character_avatar(session: Session, character: Character):
    version_id = character.current_approved_version_id or session.scalar(select(CharacterVersion.id).where(CharacterVersion.character_id == character.id).order_by(CharacterVersion.version.desc()).limit(1))
    if not version_id:
        return None
    asset = session.scalar(select(CharacterAsset).where(CharacterAsset.character_version_id == version_id, CharacterAsset.asset_type == "front_face").order_by(CharacterAsset.is_primary_reference.desc(), CharacterAsset.created_at.desc()).limit(1))
    return f"/api/media/{asset.relative_path}" if asset else None


@router.post("/characters", status_code=status.HTTP_201_CREATED)
def create_character(payload: CharacterCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    values = payload.model_dump(exclude_none=True)
    character = Character(**values)
    session.add(character)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise AppError(409, "character_conflict", "角色 ID 或 slug 已存在") from exc
    return row_dict(character)


@router.get("/characters/{character_id}")
def get_character(character_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    character = required(session, Character, character_id, "角色")
    result = row_dict(character)
    result["avatar_url"] = character_avatar(session, character)
    versions = session.scalars(select(CharacterVersion).where(CharacterVersion.character_id == character_id).order_by(CharacterVersion.version.desc())).all()
    result["versions"] = []
    for version in versions:
        value = row_dict(version)
        value["assets"] = [row_dict(asset) for asset in session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == version.id)).all()]
        result["versions"].append(value)
    return result


@router.post("/characters/{character_id}/versions", status_code=status.HTTP_201_CREATED)
def create_character_version(character_id: str, payload: CharacterVersionCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    character = required(session, Character, character_id, "角色")
    version_number = (session.scalar(select(func.max(CharacterVersion.version)).where(CharacterVersion.character_id == character_id)) or 0) + 1
    version = CharacterVersion(character_id=character.id, version=version_number, identity_json=payload.identity, approval_state="draft")
    session.add(version)
    audit(session, "character", character.id, "create_version", {"version": version_number, "preserved_current_approved_version_id": character.current_approved_version_id})
    session.commit()
    return row_dict(version)


@router.patch("/characters/{character_id}/versions/{version_id}")
def patch_character_draft(character_id: str, version_id: str, payload: CharacterVersionCreate, session: Session = Depends(get_db)):
    version = required(session, CharacterVersion, version_id, "角色版本")
    if version.character_id != character_id:
        raise AppError(422, "version_character_mismatch", "角色版本不属于该角色")
    if version.approval_state == "approved":
        raise AppError(409, "character_version_locked", "已锁定身份不可修改，请新建版本")
    if version.anchor_locked_at:
        raise AppError(409, 'anchor_locked', '标准脸身份锚点已锁定；固定身份变化必须创建新版本')
    version.identity_json = payload.identity
    audit(session, "character_version", version.id, "draft_identity_updated", {"identity": payload.identity})
    session.commit()
    return row_dict(version)


def detect_asset_type(filename: str, data: bytes) -> tuple[str, str]:
    extension = Path(filename).suffix.lower()
    if extension in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if extension == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if extension == ".webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if extension == ".mp4" and len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    if extension == ".wav" and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    raise AppError(415, "unsupported_asset_type", "素材签名与扩展名不匹配或类型不受支持")


@router.post("/characters/{character_id}/assets", status_code=status.HTTP_201_CREATED)
async def upload_character_asset(
    request: Request,
    character_id: str,
    version_id: str = Form(...),
    asset_type: str = Form(..., min_length=1, max_length=40),
    tags: str = Form(""),
    is_primary_reference: bool = Form(False),
    approve: bool = Form(False),
    file: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = settings_from(request)
    required(session, Character, character_id, "角色")
    version = required(session, CharacterVersion, version_id, "角色版本")
    if version.character_id != character_id:
        raise AppError(422, "version_character_mismatch", "角色版本不属于该角色")
    if version.approval_state == "approved":
        raise AppError(
            409,
            "character_version_locked",
            "已批准角色版本不可再增删或替换素材；请创建新版本",
        )
    if asset_type not in CHARACTER_ASSET_TYPES:
        raise AppError(
            422,
            "invalid_asset_type",
            "素材类型不在允许列表中",
            {"allowed": sorted(CHARACTER_ASSET_TYPES)},
        )
    if is_primary_reference and version.anchor_locked_at:
        raise AppError(409, 'anchor_locked', '标准脸身份锚点已锁定；请创建新身份版本')
    data = await file.read(settings.import_max_bytes + 1)
    if not data:
        raise AppError(422, "empty_asset", "素材文件为空")
    if len(data) > settings.import_max_bytes:
        raise AppError(413, "asset_too_large", "素材文件超过大小限制")
    original_name = Path(file.filename or "asset").name
    mime_type, extension = detect_asset_type(original_name, data)
    width = height = None
    if mime_type.startswith("image/"):
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if width * height > 40_000_000:
                    raise AppError(422, "image_too_large", "图片超过4000万像素")
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AppError(422, "image_decode_failed", "图片无法解码，不能成为人物参考") from exc
    if asset_type == "reference_video" and not mime_type.startswith("video/"):
        raise AppError(422, "asset_media_mismatch", "reference_video 必须上传视频文件")
    if asset_type == "reference_audio" and not mime_type.startswith("audio/"):
        raise AppError(422, "asset_media_mismatch", "reference_audio 必须上传音频文件")
    if asset_type not in {"reference_video", "reference_audio"} and not mime_type.startswith("image/"):
        raise AppError(422, "asset_media_mismatch", "该人物参考素材类型必须上传图片")
    digest = hashlib.sha256(data).hexdigest()
    relative = f"characters/{character_id}/versions/{version_id}/assets/{uuid.uuid4().hex}{extension}"
    destination = safe_media_path(settings.media_root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    if is_primary_reference:
        for old_asset in session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == version_id, CharacterAsset.is_primary_reference.is_(True))):
            old_asset.is_primary_reference = False
    asset = CharacterAsset(
        character_version_id=version_id,
        asset_type=asset_type,
        relative_path=relative,
        mime_type=mime_type,
        width=width,
        height=height,
        sha256=digest,
        tags_json=[tag.strip() for tag in tags.split(",") if tag.strip()],
        status="approved" if approve else "needs_review",
        is_primary_reference=is_primary_reference,
    )
    session.add(asset)
    audit(session, "character_version", version_id, "asset_uploaded", {"asset_type": asset_type, "sha256": digest, "status": asset.status})
    session.commit()
    return row_dict(asset)


@router.post("/characters/{character_id}/versions/{version_id}/approve")
def approve_character_version(request: Request, character_id: str, version_id: str, payload: ApprovalRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    character = required(session, Character, character_id, "角色")
    version = required(session, CharacterVersion, version_id, "角色版本")
    if version.character_id != character.id:
        raise AppError(422, "version_character_mismatch", "角色版本不属于该角色")
    from app.models import CharacterBatch, CharacterGenerationJob
    if session.scalar(select(CharacterGenerationJob.id).join(CharacterBatch).where(CharacterBatch.character_version_id == version_id, CharacterGenerationJob.status.in_(['queued', 'submitting', 'submitted', 'running', 'cancelling'])).limit(1)):
        raise AppError(409, 'character_generation_active', '素材任务尚未结束，不能提前锁定正式版本')
    if version.identity_json.get('profile') and not version.standard_face_asset_id:
        raise AppError(409, 'standard_face_lock_required', '请先人工审核并锁定标准脸身份锚点')
    if not settings_from(request).h3_demo_mode:
        primary = session.scalar(select(CharacterAsset).where(CharacterAsset.character_version_id == version_id, CharacterAsset.status == "approved", CharacterAsset.is_primary_reference.is_(True)))
        if not primary:
            raise AppError(409, "approved_primary_required", "请先人工审核真实标准脸，再锁定角色版本")
        safe_media_path(settings_from(request).media_root, primary.relative_path, must_exist=True)
    version.approval_state = "approved"
    version.approved_at = datetime.now(timezone.utc)
    if version.identity_json.get('studio_acceptance'):
        version.identity_json={**version.identity_json,'human_identity_approved':payload.operator not in {'automation','acceptance-test'},'identity_review_actor':payload.operator}
    character.current_approved_version_id = version.id
    approve_record(session, "character_version", version.id, version.version, "approved", payload)
    session.commit()
    return row_dict(version)


@router.post("/assets/{asset_id}/review")
def review_asset(asset_id: str, payload: AssetReviewRequest, session: Session = Depends(get_db)):
    asset = required(session, CharacterAsset, asset_id, "素材")
    version = required(session, CharacterVersion, asset.character_version_id, "角色版本")
    if version.approval_state == "approved":
        raise AppError(409, "character_version_locked", "已锁定版本不可改变素材，请新建版本")
    if version.anchor_locked_at and (version.standard_face_asset_id == asset.id or payload.is_primary_reference):
        raise AppError(409, 'anchor_locked', '身份锚点已锁定；更换必须创建新版本')
    if payload.is_primary_reference:
        if not asset.mime_type.startswith("image/") or payload.status != "approved":
            raise AppError(422, "invalid_primary_reference", "标准脸必须为已审核图片")
        for other in session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == version.id)):
            other.is_primary_reference = False
    asset.status = payload.status
    asset.review_reason = payload.reason
    asset.is_primary_reference = payload.is_primary_reference
    audit(session, "character_asset", asset.id, "review", payload.model_dump())
    session.commit()
    return row_dict(asset)


@router.post("/shots/{shot_id}/assign-character")
def assign_character(shot_id: str, payload: CharacterAssignment, session: Session = Depends(get_db)) -> dict[str, Any]:
    shot = required(session, StoryboardShot, shot_id, "分镜")
    active=session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id==shot.id,GenerationJob.status.in_(['submitting','queued','submitted','running','postprocessing','cancelling','reconciling','unknown'])).limit(1))
    if active:raise AppError(409,'generation_active','分镜生成任务尚未结束或结果未知，不能更换人物参考',{'job_id':active})
    character = required(session, Character, payload.character_id, "角色")
    version = required(session, CharacterVersion, payload.character_version_id, "角色版本")
    if version.character_id != character.id or version.approval_state != "approved":
        raise AppError(422, "invalid_character_version", "必须选择该角色已批准的版本")
    changed = shot.character_id != character.id or shot.character_asset_version_id != version.id
    shot.character_id = character.id
    shot.character_asset_version_id = version.id
    assets = session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == version.id, CharacterAsset.status == "approved")).all()
    ids = payload.asset_ids if payload.asset_ids is not None else [asset.id for asset in assets if asset.is_primary_reference]
    if any(asset_id not in {asset.id for asset in assets} for asset_id in ids):
        raise AppError(422, "invalid_reference_asset", "参考素材必须属于锁定版本并已审核")
    changed = changed or shot.reference_asset_ids_json != ids
    shot.reference_asset_ids_json = ids
    if changed:
        if shot.status == "approved":
            shot.status = "draft"
            shot.revision += 1
            shot.approved_at = None
        invalidate_shot_videos(session, shot.id)
    audit(session, "shot", shot.id, "assign_character", {"character_id": character.id, "character_version_id": version.id, "approval_invalidated": changed})
    from app.services.screenplay import sync_legacy
    sync_legacy(session, shot, force=True)
    session.commit()
    return row_dict(shot)


def build_prompt(shot: StoryboardShot, version: CharacterVersion, instruction: str | None) -> tuple[str, str]:
    identity = version.identity_json
    profile = identity.get("profile") or {}
    fixed = identity.get("fixed_identity") or identity.get("description") or "保持所选角色身份一致"
    clothing = identity.get("default_clothing") or next((item.get("description", "") for item in profile.get("wardrobes", []) if item.get("id") == "default"), "")
    pieces = ["Use <Picture 1> as the sole character identity reference. Preserve the same face, facial proportions, hair, identifying marks and body proportions of the referenced character.", f"人物身份固定：{fixed}"]
    for key, label in (("facial_features", "面部特征"), ("hair", "发型"), ("eyes", "眼睛"), ("skin", "肤质"), ("identifying_marks", "识别特征"), ("body", "体态")):
        if profile.get(key):
            pieces.append(f"{label}固定：{profile[key]}")
    if clothing:
        pieces.append(f"默认服装固定：{clothing}")
    pieces.extend(filter(None, [shot.visual_prompt, f"动作：{shot.action}" if shot.action else "", f"镜头运动：{shot.camera_move}" if shot.camera_move else "", f"光线：{shot.lighting}" if shot.lighting else "", f"声音方向：{shot.audio_direction}" if shot.audio_direction else ""]))
    if instruction:
        pieces.append(f"本次修订指令：{instruction}")
    pieces.append("连续性约束：与同项目相邻镜头保持同一人物身份、发型、面部特征和默认服装。")
    negative = "；".join(filter(None, [shot.negative_prompt, identity.get("negative_prompt", "")]))
    return "\n".join(pieces), negative


def _generation_inputs(session, settings, shot, payload, instruction, frozen_inputs, *,
                       allow_no_visual_references=False):
    from app.services.screenplay import uses_structured_screenplay
    structured = (bool(frozen_inputs) or uses_structured_screenplay(session, shot)
                  or (allow_no_visual_references and not shot.character_id))
    structured_inputs = frozen_inputs
    if structured and structured_inputs is None:
        from app.services.screenplay import generation_inputs
        structured_inputs = generation_inputs(
            session, shot, settings,
            allow_no_visual_references=allow_no_visual_references,
        )
    if not structured and (not shot.character_id or not shot.character_asset_version_id) and not allow_no_visual_references:
        raise AppError(409, "character_not_assigned", "分镜未绑定角色与角色版本")
    version = (structured_inputs[0] if structured_inputs else
               required(session, CharacterVersion, shot.character_asset_version_id, "角色版本")
               if shot.character_asset_version_id else None)
    if version is not None and version.approval_state != "approved":
        raise AppError(409, "character_version_not_approved", "绑定的角色版本尚未批准")
    assets = session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == version.id, CharacterAsset.status == "approved").order_by(CharacterAsset.is_primary_reference.desc(), CharacterAsset.created_at)).all() if version else []
    if not settings.h3_demo_mode and not structured:
        assets = [asset for asset in assets if asset.id in shot.reference_asset_ids_json]
        if len(assets) != 1 or not assets[0].mime_type.startswith("image/"):
            raise AppError(409, "locked_reference_required", "当前工作流要求分镜锁定1张已审核身份图片")
    references: list[dict[str, Any]] = list(structured_inputs[1]) if structured_inputs else []
    for asset in ([] if structured else assets):
        absolute = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
        references.append({"asset_id": asset.id, "asset_type": asset.asset_type, "relative_path": asset.relative_path, "absolute_path": str(absolute), "is_primary_reference": asset.is_primary_reference, "sha256": asset.sha256})
    if not settings.h3_demo_mode and not references and not allow_no_visual_references:
        raise AppError(409, "approved_reference_required", "当前参考生视频工作流需要至少一张可用的画面参考，可绑定场景图或关键画面")
    prompt, negative = (structured_inputs[2], structured_inputs[3].get('model_lab_negative_prompt','')) if structured_inputs else build_prompt(shot, version, instruction)
    from app.services.video_revisions import editable_generation_prompt, cloud_revision_targets
    source_snapshot = structured_inputs[3] if structured_inputs else None
    targets = cloud_revision_targets(session, source_snapshot, references)
    return version, references, prompt, editable_generation_prompt(prompt, instruction, structured=structured,
        prompt_override=payload.prompt_override, targets=targets), negative, source_snapshot


_RETRY_DERIVED_PARAMETER_KEYS = frozenset({
    'compiled_prompt', 'script_prompt', 'native_reference_constraints', 'prompt_override_used',
    'provider_submission', 'original_prompt', 'based_on_video_id',
    'actual_motion_reference_uploads', 'actual_reference_video_uploads',
    'actual_reference_uploads', 'actual_workflow_sha256', 'output_relative_dir',
    'generation_inputs_hash', 'motion_inputs_hash', 'reference_videos_fingerprint',
    'reference_manifest', 'service_snapshot', 'screenplay_snapshot', 'script_revision_id',
    'duration_seconds', 'aspect_ratio', 'negative_prompt', 'instruction', 'prompt_override',
    'character_version_id', 'revision_postprocess',
})


def _path_sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _restore_frozen_reference_assets(settings: Settings, references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    restored = []
    for reference in references:
        if not isinstance(reference, dict):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的图片参考快照不完整，无法安全重试')
        relative_path, expected_sha256 = reference.get('relative_path'), reference.get('sha256')
        if not isinstance(relative_path, str) or not isinstance(expected_sha256, str):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的图片参考缺少路径或哈希，无法安全重试')
        path = safe_media_path(settings.media_root, relative_path, must_exist=True)
        if _path_sha256(path) != expected_sha256:
            raise AppError(409, 'retry_reference_changed', '原任务的图片参考文件已变化，未提交重试')
        restored.append({**copy.deepcopy(reference), 'absolute_path': str(path)})
    return restored


def _restore_frozen_motion_references(session: Session, settings: Settings, references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore a task's recorded native-video inputs without consulting current shot settings."""
    if len(references) > 3:
        raise AppError(409, 'retry_snapshot_incomplete', '原任务的参考视频数量超过当前 MiniMax H3 上限')
    restored = []
    total_duration = 0.0
    for reference in references:
        if not isinstance(reference, dict):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的参考视频快照不完整，无法安全重试')
        video_id, expected_revision, expected_sha256 = (
            reference.get('video_id'), reference.get('expected_revision'), reference.get('source_sha256'),
        )
        if not isinstance(video_id, str) or not isinstance(expected_revision, int) or not isinstance(expected_sha256, str):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的参考视频缺少身份、版本或哈希，无法安全重试')
        video = session.get(ReferenceVideo, video_id)
        if (not video or video.state != 'active' or video.revision != expected_revision
                or video.sha256 != expected_sha256):
            raise AppError(409, 'retry_reference_changed', '原任务的参考视频已变化或不可用，未提交重试')
        frame_count = reference.get('effective_frames')
        duration = reference.get('effective_duration_seconds')
        if not isinstance(frame_count, int) or not 48 <= frame_count <= 360 or not isinstance(duration, (int, float)):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的参考视频处理参数不完整，无法安全重试')
        total_duration += float(duration)
        source = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
        restored.append({**copy.deepcopy(reference), 'relative_path': video.relative_path, 'absolute_path': str(source)})
    if total_duration > 15 + 1e-6:
        raise AppError(409, 'retry_snapshot_incomplete', '原任务的参考视频总时长超过当前 MiniMax H3 上限')
    return restored


def _retry_input_snapshot(job: GenerationJob) -> dict[str, Any]:
    parameters = job.parameters_json if isinstance(job.parameters_json, dict) else {}
    stored = parameters.get('retry_input_snapshot')
    if isinstance(stored, dict):
        return copy.deepcopy(stored)
    # Older direct-native tasks did not write a dedicated retry snapshot.  Their
    # persisted job fields are still a frozen record and can be used only when
    # every source path and hash required for replay was retained.
    return {
        'schema_version': 0,
        'prompt': job.prompt,
        'script_prompt': parameters.get('script_prompt', job.prompt),
        'negative_prompt': parameters.get('negative_prompt', ''),
        'instruction': parameters.get('instruction'),
        'prompt_override': parameters.get('prompt_override'),
        'character_version_id': parameters.get('character_version_id'),
        'duration_seconds': parameters.get('duration_seconds'),
        'aspect_ratio': parameters.get('aspect_ratio'),
        'seed': job.seed,
        'reference_assets': copy.deepcopy(job.reference_assets_json or []),
        'motion_references': copy.deepcopy(parameters.get('motion_references', [])),
        'reference_videos_fingerprint': parameters.get('reference_videos_fingerprint'),
        'native_reference_constraints': parameters.get('native_reference_constraints', ''),
        'screenplay_snapshot': copy.deepcopy(parameters.get('screenplay_snapshot')),
        'parameters': {key: copy.deepcopy(value) for key, value in parameters.items()
                       if key not in _RETRY_DERIVED_PARAMETER_KEYS and key not in {'motion_references', 'native_ref2va_workflow'}},
        'service_snapshot': copy.deepcopy(parameters.get('service_snapshot')),
        'native_ref2va_workflow': copy.deepcopy(parameters.get('native_ref2va_workflow')),
        'workflow_version': job.workflow_version,
    }


async def generate_for_shot(
    request: Request,
    session: Session,
    shot: StoryboardShot,
    payload: GenerationRequest,
    *,
    instruction: str | None = None,
    retry_count: int = 0,
    frozen_service: dict | None = None,
    production_scope: dict | None = None,
    revision_postprocess: dict | None = None,
    revision_source: VideoRevision | None = None,
    revision_reference_assets: list[dict[str, Any]] | None = None,
    frozen_native_workflow: dict | None = None,
    frozen_retry_input: dict[str, Any] | None = None,
) -> tuple[GenerationJob, VideoRevision | None]:
    settings = settings_from(request)
    if revision_postprocess:
        from app.services.revision_postprocess import validate_generation_plan
        validate_generation_plan(session, settings, shot, revision_postprocess)
    requested_mode = payload.parameters.get('requested_mode')
    if requested_mode and requested_mode != ('demo' if settings.h3_demo_mode else 'real'):
        raise AppError(409, 'provider_mode_mismatch', '请求模式与后端配置不一致；拒绝自动切换 Provider')
    script = None if frozen_retry_input is not None else script_for_shot(session, shot)
    if script is not None and (script.project_id != shot.project_id or script.status != 'approved'):
        raise AppError(409, 'script_not_approved', '脚本尚未审核通过')
    if script is None and frozen_retry_input is None:
        raise AppError(409, 'script_not_approved', '脚本尚未审核通过')
    if shot.status != 'approved':
        raise AppError(409, 'shot_not_approved', '分镜尚未审核通过')
    duplicate = session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id == shot.id,
        GenerationJob.status.in_(['queued', 'submitting', 'submitted', 'running', 'postprocessing', 'cancelling', 'reconciling', 'unknown'])).limit(1))
    if duplicate:
        raise AppError(409, 'generation_already_active', '该分镜已有运行中的生成任务', {'job_id': duplicate})

    from app.services.model_lab import frozen_generation_inputs
    from app.services.cinematic_quality import generation_inputs_hash
    from app.services.motion_references import resolve_motion_references, verify_motion_sources
    from app.services.native_reference_video import (public_references, reference_prompt_constraints,
                                                     references_fingerprint,
                                                     require_project_reference_access)
    from app.services.video_services import existing_provider, select_video_service

    lab_inputs = None if frozen_retry_input is not None else frozen_generation_inputs(session, settings, shot, payload)
    service_snapshot = copy.deepcopy(frozen_service) if frozen_service is not None else None
    provider = provider_for(settings)
    if not settings.h3_demo_mode:
        if frozen_service is not None:
            provider = existing_provider(session, settings, frozen_service)
        else:
            _, service_snapshot, provider = select_video_service(session, settings, shot.project_id,
                payload.service_id or payload.parameters.get('service_id'), payload.model_id or payload.parameters.get('model_id'))
    motion_inputs_hash = None
    if frozen_retry_input is not None:
        prompt = frozen_retry_input.get('prompt')
        script_prompt = frozen_retry_input.get('script_prompt')
        negative = frozen_retry_input.get('negative_prompt')
        duration_seconds = frozen_retry_input.get('duration_seconds')
        aspect_ratio = frozen_retry_input.get('aspect_ratio')
        if (not isinstance(prompt, str) or not isinstance(script_prompt, str) or not isinstance(negative, str)
                or not isinstance(duration_seconds, (int, float)) or not isinstance(aspect_ratio, str)):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的提示词或规格快照不完整，无法安全重试')
        instruction = frozen_retry_input.get('instruction')
        if instruction is not None and not isinstance(instruction, str):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的修订指令快照无效，无法安全重试')
        references = _restore_frozen_reference_assets(
            settings, frozen_retry_input.get('reference_assets') or [])
        version = None
        screenplay_snapshot = copy.deepcopy(frozen_retry_input.get('screenplay_snapshot'))
    else:
        version, references, script_prompt, prompt, negative, screenplay_snapshot = _generation_inputs(
            session, settings, shot, payload, instruction, lab_inputs,
            allow_no_visual_references=(not settings.h3_demo_mode and provider.name == 'comfyui_h3'))
        duration_seconds = shot.duration_seconds
        aspect_ratio = shot.aspect_ratio
    if revision_reference_assets:
        extra = [dict(item) for item in revision_reference_assets]
        known = {item.get('sha256') for item in references if item.get('sha256')}
        if any(item.get('sha256') in known for item in extra if item.get('sha256')):
            raise AppError(422, 'revision_reference_duplicate', '修订图片与现有参考重复，请移除重复图片')
        references = [*references, *extra]
        if len(references) > 9:
            raise AppError(422, 'reference_limit', 'MiniMax H3 最多支持9张图片参考；请减少修订图片或当前镜头图片')
    version_id = frozen_retry_input.get('character_version_id') if frozen_retry_input is not None else (version.id if version else None)
    prompt_override = frozen_retry_input.get('prompt_override') if frozen_retry_input is not None else payload.prompt_override
    if frozen_retry_input is not None:
        motion_references = _restore_frozen_motion_references(
            session, settings, frozen_retry_input.get('motion_references') or [])
        resolved_seed = frozen_retry_input.get('seed')
        if resolved_seed is None:
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的随机种子快照缺失，无法安全重试')
        motion_inputs_hash = frozen_retry_input.get('motion_inputs_hash')
    else:
        explicit_reference_values = payload.parameters.get('reference_videos') if 'reference_videos' in payload.parameters else None
        motion_references = resolve_motion_references(session, settings, shot, explicit_reference_values)
        resolved_seed = payload.seed if payload.seed is not None else 20261020
    native_reference_fingerprint = references_fingerprint(motion_references)
    expected_reference_fingerprint = (frozen_retry_input.get('reference_videos_fingerprint')
                                      if frozen_retry_input is not None
                                      else payload.parameters.get('reference_videos_fingerprint'))
    if expected_reference_fingerprint is not None and expected_reference_fingerprint != native_reference_fingerprint:
        raise AppError(409, 'reference_video_snapshot_mismatch', '本次提交的参考视频冻结快照不匹配，未发送生成请求')
    if motion_references:
        if settings.h3_demo_mode or provider.name!='comfyui_h3':
            raise AppError(409,'native_reference_video_provider_unsupported','所选服务未实现 MiniMax H3 原生参考视频；未提交或退回纯文本生成')
        if frozen_retry_input is None:
            require_project_reference_access(session, shot.project_id, motion_references)
        await asyncio.to_thread(verify_motion_sources,motion_references)
        if frozen_retry_input is None:
            motion_inputs_hash=generation_inputs_hash(session,shot)
            native_constraints = reference_prompt_constraints(motion_references, picture_count=len(references))
            prompt += '\n\n' + native_constraints
        else:
            native_constraints = frozen_retry_input.get('native_reference_constraints', '')
            if not isinstance(native_constraints, str):
                raise AppError(409, 'retry_snapshot_incomplete', '原任务的参考视频约束快照无效，无法安全重试')
    else:
        native_constraints = ''
    if service_snapshot and service_snapshot.get('settings', {}).get('video_contract') == 'wan26-i2v' and len(references) != 1:
        raise AppError(409, 'wan_single_frame_required', '万相图生视频需要恰好一张已审核合成首帧；请先准备首帧，不能自动丢弃其他参考图')
    if frozen_retry_input is not None:
        effective_parameters = copy.deepcopy(frozen_retry_input.get('parameters'))
        if not isinstance(effective_parameters, dict):
            raise AppError(409, 'retry_snapshot_incomplete', '原任务的生成参数快照不完整，无法安全重试')
    else:
        overrides = {key:value for key,value in payload.parameters.items() if key not in (
            'service_snapshot','screenplay_snapshot','script_revision_id','output_relative_dir','character_version_id',
            'duration_seconds','negative_prompt','reference_manifest','motion_references','motion_inputs_hash',
            'reference_videos','reference_videos_fingerprint','native_reference_constraints',
            'actual_motion_reference_uploads','actual_reference_uploads','quality_request','production_scope','revision_postprocess',
            'based_on_video_id','original_prompt','native_ref2va_workflow')}
        configured = service_snapshot['settings'] if service_snapshot else {}
        from app.services.model_lab import default_generation_parameters
        saved_defaults=default_generation_parameters(session,service_snapshot) if service_snapshot else {}
        default_steps = 4 if provider.name == 'comfyui_h3' else 20
        effective_parameters = {"width": configured.get('width',640), "height": configured.get('height',384), "steps":default_steps, **saved_defaults, **overrides}
    if provider.name == 'comfyui_h3' and effective_parameters.get('steps') not in {4, 20, 30}:
        raise AppError(422, 'native_h3_quality_mode', 'MiniMax H3 原生工作流仅支持 Fast 4-Step、标准 20-Step 或高质量 30-Step')
    if provider.name == 'comfyui_h3':
        from app.services.providers import freeze_native_ref2va_workflow
        # Keep an immutable graph document with the task.  Retrying a failed
        # job reuses it rather than reading an updated source workflow.
        if frozen_retry_input is not None:
            frozen_workflow = frozen_native_workflow or frozen_retry_input.get('native_ref2va_workflow')
            if not isinstance(frozen_workflow, dict):
                raise AppError(409, 'retry_snapshot_incomplete', '原任务的原生 H3 工作流快照缺失，无法安全重试')
            workflow_snapshot = copy.deepcopy(frozen_workflow)
        else:
            workflow_snapshot = (copy.deepcopy(frozen_native_workflow)
                                 if frozen_native_workflow is not None
                                 else freeze_native_ref2va_workflow(effective_parameters))
        effective_parameters['native_ref2va_workflow'] = workflow_snapshot
        if service_snapshot:
            service_snapshot = {
                **service_snapshot,
                'workflow_version': workflow_snapshot['workflow_name'],
                'workflow_template_sha256': workflow_snapshot['template_sha256'],
                'workflow_tier': workflow_snapshot['steps'],
            }
    if 'quality_request' in payload.parameters:
        # Historical explicit controls continue to describe their saved jobs.
        effective_parameters['quality_request']=payload.parameters['quality_request']
    if screenplay_snapshot and screenplay_snapshot.get('reference_manifest'):
        effective_parameters['reference_manifest']=screenplay_snapshot['reference_manifest']
    if service_snapshot and service_snapshot.get('provider') == 'comfyui':
        expected_ratio={'16:9':16/9,'9:16':9/16,'1:1':1}[aspect_ratio]
        if abs(effective_parameters['width']/effective_parameters['height']-expected_ratio)>.025:
            raise AppError(422,'native_h3_aspect_mismatch','实际生成宽高必须匹配本镜画幅，不能只修改文字画幅')
    if service_snapshot:
        effective_parameters['service_snapshot'] = service_snapshot
    if production_scope:
        effective_parameters['production_scope'] = production_scope
    if revision_source is not None:
        from app.services.video_revisions import revision_source_job
        source_job = revision_source_job(session, shot, revision_source)
        effective_parameters.update(based_on_video_id=revision_source.id, original_prompt=source_job.prompt)
    source_script_revision_id = (frozen_retry_input.get('script_revision_id')
                                 if frozen_retry_input is not None else script.id)
    frozen_inputs_hash = (frozen_retry_input.get('generation_inputs_hash')
                          if frozen_retry_input is not None else generation_inputs_hash(session, shot))
    if not isinstance(source_script_revision_id, str) or not isinstance(frozen_inputs_hash, str):
        raise AppError(409, 'retry_snapshot_incomplete', '原任务的脚本或输入快照标识缺失，无法安全重试')
    serialized_references = [{key: value for key, value in ref.items() if key != "absolute_path"} for ref in references]
    retry_input_snapshot = {
        'schema_version': 1,
        'prompt': prompt,
        'script_prompt': script_prompt,
        'negative_prompt': negative,
        'instruction': instruction,
        'prompt_override': prompt_override,
        'character_version_id': version_id,
        'script_revision_id': source_script_revision_id,
        'duration_seconds': duration_seconds,
        'aspect_ratio': aspect_ratio,
        'seed': resolved_seed,
        'generation_inputs_hash': frozen_inputs_hash,
        'reference_assets': copy.deepcopy(serialized_references),
        'motion_references': public_references(motion_references),
        'reference_videos_fingerprint': native_reference_fingerprint,
        'motion_inputs_hash': motion_inputs_hash,
        'native_reference_constraints': native_constraints,
        'screenplay_snapshot': copy.deepcopy(screenplay_snapshot),
        'parameters': copy.deepcopy(effective_parameters),
        'service_snapshot': copy.deepcopy(service_snapshot),
        'native_ref2va_workflow': copy.deepcopy(effective_parameters.get('native_ref2va_workflow')),
        'workflow_version': service_snapshot.get('workflow_version') if service_snapshot else None,
    }
    job = GenerationJob(
        project_id=shot.project_id,
        shot_id=shot.id,
        provider="demo" if settings.h3_demo_mode else provider.name,
        status="running" if settings.h3_demo_mode else "queued",
        prompt=prompt,
        parameters_json={**effective_parameters, 'script_revision_id': source_script_revision_id, 'duration_seconds': duration_seconds,
            'aspect_ratio': aspect_ratio, 'generation_inputs_hash': frozen_inputs_hash,
            'negative_prompt': negative, 'instruction': instruction,
            'script_prompt': script_prompt, 'native_reference_constraints': native_constraints,
            'prompt_override_used': prompt_override is not None, 'prompt_override': prompt_override,
            'character_version_id': version_id, **({'screenplay_snapshot': screenplay_snapshot} if screenplay_snapshot else {}),
            **({'revision_postprocess': revision_postprocess} if revision_postprocess else {}),
            'retry_input_snapshot': retry_input_snapshot,
            **({'motion_references': public_references(motion_references),
                'reference_videos_fingerprint': native_reference_fingerprint,
                'motion_inputs_hash': motion_inputs_hash} if motion_references else {
                    'reference_videos_fingerprint': native_reference_fingerprint})},
        reference_assets_json=serialized_references,
        workflow_version=('demo-watermarked-video-v1' if settings.h3_demo_mode
                          else service_snapshot.get('workflow_version') if service_snapshot else None),
        seed=resolved_seed,
        progress=0.0,
        retry_count=retry_count,
        started_at=datetime.now(timezone.utc),
    )
    session.add(job)
    session.flush()
    revision_number = (session.scalar(select(func.max(VideoRevision.revision)).where(VideoRevision.shot_id == shot.id)) or 0) + 1
    output_dir = safe_media_path(settings.media_root, f"projects/{shot.project_id}/shots/{shot.id}/revisions/{revision_number}")
    job.parameters_json = {**job.parameters_json, "output_relative_dir": media_relative(settings.media_root, output_dir)}
    # Persist before the potentially slow external POST. Holding SQLite's write
    # transaction across that await would block pause/review/import operations.
    # The worker ignores submitting until an actual provider ID is committed.
    job.status='submitting'
    session.commit()
    try:
        if motion_references:
            session.expire_all()
            refreshed=resolve_motion_references(session,settings,shot,public_references(motion_references))
            if public_references(refreshed)!=public_references(motion_references):
                raise AppError(409,'motion_reference_stale','动作参考在提交前发生变化，未发送生成请求')
            await asyncio.to_thread(verify_motion_sources,refreshed)
        accepted = await provider.submit(
            VideoGenerationRequest(
                job_id=job.id,
                project_id=shot.project_id,
                shot_id=shot.id,
                prompt=prompt,
                negative_prompt=negative,
                duration_seconds=duration_seconds,
                aspect_ratio=aspect_ratio,
                seed=resolved_seed,
                parameters=effective_parameters,
                reference_assets=references,
                motion_references=motion_references,
                output_dir=output_dir,
                instruction=instruction,
            )
        )
        if accepted is None:
            raise AppError(409,'local_gpu_busy','本地 GPU 被模型占用或状态不明；未提交本次生成')
    except AppError as exc:
        job.status = 'unknown' if exc.code in {'cloud_submission_unknown','motion_submission_unknown'} else 'failed'
        job.error_code = exc.code
        job.error_message = exc.message + ("\n" + json.dumps(exc.details, ensure_ascii=False) if exc.details else "")
        job.finished_at = datetime.now(timezone.utc)
        audit(session, "generation_job", job.id, "failed", {"code": exc.code, "message": exc.message})
        session.commit()
        raise
    except Exception as exc:
        if not motion_references:raise
        job.status='unknown';job.error_code='motion_submission_unknown'
        job.error_message='任务出现未预期异常，受理状态未知；请人工核对原任务，未自动重发'
        job.finished_at=datetime.now(timezone.utc)
        audit(session,'generation_job',job.id,'unknown',{'code':job.error_code})
        session.commit()
        raise AppError(502,job.error_code,job.error_message) from exc
    job.provider_job_id = accepted.provider_job_id
    job.status = accepted.status
    job.progress = 1.0 if accepted.status == "succeeded" else 0.0
    if accepted.metadata.get("workflow"):
        job.workflow_version = accepted.metadata["workflow"]
    if job.provider == 'cloud_lk888_h3_v2':
        safe_keys = ('seed_applied', 'negative_prompt_applied', 'negative_prompt_method', 'request_resolution', 'requested_width', 'requested_height', 'actual_dimensions_unverified', 'reference_uploads', 'request_model', 'configured_model', 'prompt_adjustment', 'original_prompt_characters', 'sent_prompt_characters', 'sent_prompt_sha256', 'sent_prompt', 'prompt_identifier_aliases')
        job.parameters_json = {**job.parameters_json, 'provider_submission': {key: accepted.metadata[key] for key in safe_keys if key in accepted.metadata}}
    if accepted.metadata.get('workflow_sha256'):
        job.parameters_json={**job.parameters_json,
            'actual_workflow_sha256':accepted.metadata['workflow_sha256'],
            **({'workflow_template_sha256': accepted.metadata['workflow_template_sha256']}
               if accepted.metadata.get('workflow_template_sha256') else {}),
            'actual_reference_uploads':accepted.metadata.get('reference_uploads',[]),
            **({'actual_motion_reference_uploads':accepted.metadata.get('motion_reference_uploads',[])} if motion_references else {})}
    if accepted.metadata.get("log_dir"):
        job.log_path = str(accepted.metadata["log_dir"])
    video: VideoRevision | None = None
    if accepted.status == "succeeded" and accepted.output_path:
        job.finished_at = datetime.now(timezone.utc)
        video = VideoRevision(
            project_id=shot.project_id,
            shot_id=shot.id,
            generation_job_id=job.id,
            relative_path=media_relative(settings.media_root, accepted.output_path),
            metadata_json=accepted.metadata,
            revision=revision_number,
            instruction=instruction,
            review_status="pending",
        )
        session.add(video)
    audit(session, "generation_job", job.id, "submitted", {"provider": job.provider, "mode": "demo" if settings.h3_demo_mode else "real", "video_revision": revision_number})
    session.commit()
    return job, video


@router.post("/shots/{shot_id}/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate_shot(request: Request, shot_id: str, payload: GenerationRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    shot = required(session, StoryboardShot, shot_id, "分镜")
    job, video = await generate_for_shot(request, session, shot, payload)
    return {"job": row_dict(job), "video_revision": row_dict(video) if video else None}


@router.post("/projects/{project_id}/generate-approved-shots", status_code=status.HTTP_202_ACCEPTED)
async def generate_approved_shots(request: Request, project_id: str, payload: GenerationRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    shots = session.scalars(project_shot_statement(session,project_id).where(StoryboardShot.status == "approved").order_by(StoryboardShot.order)).all()
    if not shots:
        raise AppError(409, "no_approved_shots", "项目没有已批准分镜")
    if not settings_from(request).h3_demo_mode:
        from app.services.video_services import select_video_service
        selected_service_id = payload.service_id or payload.parameters.get('service_id')
        selected_model_id = payload.model_id or payload.parameters.get('model_id')
        select_video_service(session, settings_from(request), project_id, selected_service_id,
                             selected_model_id)
    results = []
    for shot in shots:
        job, video = await generate_for_shot(request, session, shot, payload)
        results.append({"job": row_dict(job), "video_revision": row_dict(video) if video else None})
    return {"items": results, "execution": "serial"}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    return row_dict(required(session, GenerationJob, job_id, "任务"))


@router.post("/jobs/{job_id}/reconcile", status_code=status.HTTP_202_ACCEPTED)
def reconcile_job(request: Request, job_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    job = required(session, GenerationJob, job_id, "任务")
    if job.status != 'unknown' or not job.provider_job_id or job.provider not in {'cloud_video_jobs_v1','cloud_wan26_i2v','cloud_lk888_h3_v2'}:
        raise AppError(409, 'job_reconcile_unavailable', '只有持有供应商任务 ID 的未知云端任务可重新查询，不能重发生成请求')
    snapshot = job.parameters_json.get('service_snapshot')
    if not snapshot:
        raise AppError(409, 'job_snapshot_missing', '缺少服务快照，无法安全查询原任务')
    from app.services.video_services import existing_provider
    existing_provider(session, settings_from(request), snapshot)  # verify service and credential without sending a request
    job.status='queued';job.error_code=None;job.error_message=None;job.finished_at=None
    audit(session,'generation_job',job.id,'reconcile_requested',{'provider_job_id':job.provider_job_id,'resubmitted':False})
    session.commit()
    return row_dict(job)


@router.post('/jobs/{job_id}/resolve-unknown')
def resolve_unknown_local_job(job_id: str, payload: ResolveUnknownLocalGeneration, session: Session = Depends(get_db)):
    """Record explicit human review, never infer the lost provider outcome."""
    job=required(session,GenerationJob,job_id,'任务')
    if job.status!='unknown' or job.provider!='comfyui_h3' or job.provider_job_id is not None:
        raise AppError(409,'unknown_resolution_unavailable','仅可人工处理未取得 ComfyUI 任务 ID 的未知任务')
    result=session.execute(update(GenerationJob).where(GenerationJob.id==job_id,GenerationJob.status=='unknown',
        GenerationJob.provider=='comfyui_h3',GenerationJob.provider_job_id.is_(None)).values(
            status='closed_unknown',error_code='operator_closed_unknown',
            error_message='用户已记录人工核对与重复生成风险；原任务结果仍未知，系统未重发或删除输出',
            finished_at=datetime.now(timezone.utc)))
    if result.rowcount!=1:raise AppError(409,'unknown_resolution_stale','任务状态已变化，请重新核对')
    audit(session,'generation_job',job_id,'unknown_closed_after_manual_review',
          {'operator':payload.operator,'evidence_note':payload.evidence_note,'confirmed_inspection':True,
           'accept_duplicate_risk':True,'resubmitted':False,'output_deleted':False,'original_outcome':'unknown'})
    session.commit();session.refresh(job)
    return row_dict(job)


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(request: Request, job_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    previous = required(session, GenerationJob, job_id, "任务")
    if previous.status not in {"failed", "cancelled"}:
        raise AppError(409, "job_not_retryable", "仅失败或已取消任务可以重试")
    from app.services.revision_postprocess import retry as retry_postprocess
    if retry_postprocess(session, settings_from(request), previous):
        audit(session, 'generation_job', previous.id, 'revision_postprocess_retry', {'provider_resubmitted': False})
        session.commit()
        return {'job': row_dict(previous), 'video_revision': None, 'retried_from': previous.id, 'provider_resubmitted': False}
    shot = required(session, StoryboardShot, previous.shot_id, "分镜")
    frozen_input = _retry_input_snapshot(previous)
    selected_snapshot = frozen_input.get('service_snapshot')
    if not isinstance(selected_snapshot, dict) or not selected_snapshot:
        raise AppError(409, 'retry_snapshot_incomplete', '原任务的服务快照缺失，无法安全重试')
    source_video_id = previous.parameters_json.get('based_on_video_id')
    revision_source = session.get(VideoRevision, source_video_id) if source_video_id else None
    job, video = await generate_for_shot(request, session, shot,
        GenerationRequest(seed=frozen_input.get('seed'), parameters={}, service_id=selected_snapshot.get('service_id'),
                          model_id=selected_snapshot.get('model_id'), prompt_override=frozen_input.get('prompt_override')),
        instruction=frozen_input.get('instruction'), retry_count=previous.retry_count + 1,
        frozen_service=selected_snapshot or None,
        frozen_native_workflow=frozen_input.get('native_ref2va_workflow'),
        revision_postprocess=previous.parameters_json.get('revision_postprocess'), revision_source=revision_source,
        frozen_retry_input=frozen_input)
    return {"job": row_dict(job), "video_revision": row_dict(video) if video else None, "retried_from": previous.id}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    job = required(session, GenerationJob, job_id, "任务")
    if job.status not in {"queued", "running"}:
        raise AppError(409, "job_not_cancellable", "当前任务状态不能取消")
    from app.services.video_services import existing_provider
    snapshot = job.parameters_json.get('service_snapshot')
    provider = existing_provider(session, settings_from(request), snapshot) if snapshot else provider_for(settings_from(request))
    cancelled = await provider.cancel(job.provider_job_id or job.id)
    if not cancelled:
        raise AppError(409, "provider_cancel_rejected", "Provider 未接受取消请求")
    job.status = "cancelled"
    job.finished_at = datetime.now(timezone.utc)
    audit(session, "generation_job", job.id, "cancelled")
    session.commit()
    return row_dict(job)


@router.get("/videos/{video_id}")
def get_video(video_id: str, request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    video = required(session, VideoRevision, video_id, "视频版本")
    result = row_dict(video)
    result["media_url"] = f"/api/media/{video.relative_path}"
    job = required(session, GenerationJob, video.generation_job_id, "生成任务")
    character_version_id = job.parameters_json.get("character_version_id")
    character_version = session.get(CharacterVersion, character_version_id) if character_version_id else None
    result["prompt"] = job.prompt
    result["referenceAssets"] = job.reference_assets_json
    result["metadata_json"] = {**video.metadata_json, "prompt": job.prompt, "original_prompt": job.parameters_json.get("original_prompt", job.prompt), "revision_instruction": job.parameters_json.get("instruction"), "provider_job_id": job.provider_job_id, **({'comfyui_prompt_id':job.provider_job_id} if job.provider == 'comfyui_h3' else {}), "character_version_id": character_version_id, "character_id": character_version.character_id if character_version else None, "references": job.reference_assets_json}
    result["quality_checks"] = [row_dict(row) for row in session.scalars(select(QualityCheck).where(QualityCheck.video_revision_id == video.id).order_by(QualityCheck.created_at)).all()]
    from app.services.revision_postprocess import recovery_status
    result['metadata_json']['revision_processing_recovery'] = recovery_status(session, settings_from(request), video)
    latest_revision = session.scalar(select(GenerationJob).where(
        GenerationJob.project_id == video.project_id, GenerationJob.shot_id == video.shot_id,
        GenerationJob.parameters_json['based_on_video_id'].as_string() == video.id,
    ).order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc()).limit(1))
    if latest_revision:
        from app.services.product_library import is_product_reference_error
        message = (latest_revision.error_message or '').splitlines()
        message = message[0] if message else None
        if is_product_reference_error(latest_revision.error_code, message):
            message = '这次修订在商品图识别阶段停止，视频尚未开始渲染。原图片和修订指令已保留。'
        result['metadata_json']['latest_revision_job'] = {
            'id': latest_revision.id, 'status': latest_revision.status, 'error_message': message,
        }
    result["history"] = [row_dict(row) for row in session.scalars(select(VideoRevision).where(VideoRevision.shot_id == video.shot_id).order_by(VideoRevision.revision)).all()]
    return result


@router.post('/videos/{video_id}/resume-revision-processing', status_code=status.HTTP_202_ACCEPTED)
def resume_revision_processing(video_id: str, request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.revision_postprocess import resume_existing
    video = required(session, VideoRevision, video_id, '视频版本')
    job, replayed = resume_existing(session, settings_from(request), video)
    session.commit()
    return {'job': row_dict(job), 'video_revision': row_dict(video),
            'provider_resubmitted': False, 'replayed': replayed}


@router.post("/videos/{video_id}/recheck-duration")
def recheck_video_duration(video_id: str, request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    video=required(session,VideoRevision,video_id,'视频版本')
    from app.services.jobs import recheck_cloud_duration_warning
    recheck_cloud_duration_warning(session,settings_from(request),video)
    return row_dict(video)


@router.get('/videos/{video_id}/revision-reference-targets')
def video_revision_reference_targets(video_id: str, session: Session = Depends(get_db)):
    from app.services.revision_references import revision_targets
    video = required(session, VideoRevision, video_id, '视频版本')
    shot = required(session, StoryboardShot, video.shot_id, '分镜')
    targets = revision_targets(session, shot)
    return {kind: list(items.values()) for kind, items in targets.items()}


@router.post('/videos/{video_id}/revision-images', status_code=status.HTTP_201_CREATED)
async def upload_video_revision_image(video_id: str, request: Request, file: UploadFile = File(...),
        kind: Literal['character', 'product'] = Form(...), session: Session = Depends(get_db)):
    from app.services.revision_references import store_revision_image
    from app.services.screenplay_generation_media import LIMITS
    video = required(session, VideoRevision, video_id, '视频版本')
    data = await file.read(LIMITS['image']['max_bytes'] + 1)
    return await run_in_threadpool(store_revision_image, session, settings_from(request),
                                   video, kind, file.filename or '', data)


@router.post("/videos/{video_id}/revise", status_code=status.HTTP_202_ACCEPTED)
async def revise_video(request: Request, video_id: str, payload: RevisionRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    current = required(session, VideoRevision, video_id, "视频版本")
    shot = required(session, StoryboardShot, current.shot_id, "分镜")
    from app.services.video_revisions import prepare_native_revision
    settings = settings_from(request)
    generation, service, revision_references, preview_digest = prepare_native_revision(session, settings, shot, current, payload)
    if payload.reference_images and payload.reference_preview_sha256 != preview_digest:
        raise AppError(409, 'revision_reference_preview_changed', '请重新预览当前的修订要求和图片，再确认提交')
    from app.services.revision_postprocess import freeze_plan
    processing_plan = freeze_plan(session, settings, shot, current, payload.instruction)
    job, video = await generate_for_shot(request, session, shot, generation, instruction=payload.instruction,
        frozen_service=service, revision_postprocess=processing_plan, revision_source=current,
        revision_reference_assets=revision_references)
    previous_job = session.get(GenerationJob, current.generation_job_id)
    if job.project_id != current.project_id or job.shot_id != current.shot_id:
        raise AppError(409,'revision_processing_scope','修订生成任务与原视频归属不匹配')
    lineage = dict(job.parameters_json or {})
    if lineage.get('based_on_video_id') not in (None, current.id):
        raise AppError(409,'revision_processing_scope','修订生成任务指向其他原视频')
    lineage.setdefault('based_on_video_id', current.id)
    lineage.setdefault('original_prompt', previous_job.prompt)
    lineage.setdefault('revision_instruction', payload.instruction)
    job.parameters_json = lineage
    if previous_job.provider == "comfyui_h3":
        previous_job.status = "revised"
    session.commit()
    return {
        "job": row_dict(job),
        "video_revision": row_dict(video) if video else None,
        "based_on": current.id,
        "editing_semantics": ("按本次选择的角色和道具商品图片重新生成" if payload.reference_images
                              else "基于相同人物参考重新生成，不保证逐帧或局部像素不变"),
    }


@router.post("/videos/{video_id}/approve")
def approve_video(video_id: str, payload: ReviewRequest, request: Request, session: Session = Depends(get_db)) -> dict[str, Any]:
    video = required(session, VideoRevision, video_id, "视频版本")
    from app.services.revision_postprocess import assert_review_ready
    assert_review_ready(session.get(GenerationJob, video.generation_job_id), video, session)
    from app.services.revision_postprocess import recovery_status
    recovery = recovery_status(session, settings_from(request), video)
    if recovery.get('required') and recovery.get('state') not in {'ready'}:
        raise AppError(409, 'revision_postprocess_required', recovery.get('reason') or '请先恢复此修订原片的配音与画质后处理，再审核最终版本')
    from app.services.shot_framing import assert_video_framing_current, video_framing_unchanged
    assert_video_framing_current(session,video)
    if video.review_status == "needs_regeneration":
        if video.metadata_json.get('quality_status') == 'failed':
            raise AppError(409, 'video_needs_regeneration', '云端原始视频已保存，但技术检测不合格，不能批准或合成')
        raise AppError(
            409,
            "video_needs_regeneration",
            "分镜或人物绑定已变化，该旧视频必须重新生成，不能重新批准",
        )
    file_check = session.scalar(select(QualityCheck).where(QualityCheck.video_revision_id == video.id, QualityCheck.check_type == "file_readable").order_by(QualityCheck.created_at.desc()).limit(1))
    if not file_check or file_check.status != "passed":
        raise AppError(409, "quality_check_required", "人工审核前必须通过文件可读性检查")
    decode_check = session.scalar(
        select(QualityCheck)
        .where(
            QualityCheck.video_revision_id == video.id,
            QualityCheck.check_type == "media_decode",
        )
        .order_by(QualityCheck.created_at.desc())
        .limit(1)
    )
    if decode_check and decode_check.status == "failed":
        raise AppError(409, "media_decode_failed", "媒体解码检查失败，不能人工批准")
    warnings: list[str] = []
    if not decode_check:
        warnings.append("尚无媒体解码检查结果")
    elif decode_check.status == "not_available":
        warnings.append("媒体解码工具不可用；本次仅依据文件可读性与人工检查批准")
    job = session.get(GenerationJob, video.generation_job_id)
    if job and (job.parameters_json or {}).get('motion_references'):
        from app.services.cinematic_quality import generation_inputs_hash
        from app.services.motion_references import resolve_motion_references,verify_motion_sources
        from app.services.native_reference_video import public_references
        shot=required(session,StoryboardShot,video.shot_id,'分镜')
        if generation_inputs_hash(session,shot)!=job.parameters_json.get('motion_inputs_hash'):
            raise AppError(409,'motion_video_stale','分镜或动作参考自生成后已变化，旧视频不能批准')
        frozen = job.parameters_json['motion_references']
        motion = resolve_motion_references(session,settings_from(request),shot,frozen)
        if public_references(motion) != frozen:
            raise AppError(409,'motion_video_stale','本镜参考视频冻结快照已变化，旧视频不能批准')
        verify_motion_sources(motion)
    spec_check=session.scalar(select(QualityCheck).where(QualityCheck.video_revision_id==video.id,QualityCheck.check_type=='cinematic_output_spec').order_by(QualityCheck.created_at.desc()).limit(1))
    if spec_check and spec_check.status=='failed':raise AppError(409,'cinematic_output_spec_failed','实际视频规格与电影任务请求不符，修复后重新生成')
    if spec_check and spec_check.status=='warning':warnings.append('实际尺寸或时长与请求有偏差；已保留完整原片，请人工确认画面和声音')
    # Recheck at the write boundary: a concurrent framing edit must not be
    # overwritten by this request's earlier ORM snapshot.
    approved=session.execute(update(VideoRevision).where(VideoRevision.id==video.id,
        VideoRevision.review_status==video.review_status,
        VideoRevision.metadata_json==video.metadata_json,video_framing_unchanged()).values(
            review_status='approved',review_reason=payload.reason).execution_options(synchronize_session=False))
    if approved.rowcount!=1:
        raise AppError(409,'video_review_changed','视频审核或镜头画幅已变化，请刷新；旧画幅不能重新批准。')
    session.expire(video,['review_status','review_reason'])
    if job.provider == "comfyui_h3":
        job.status = "approved"
    approve_record(session, "video_revision", video.id, video.revision, "approved", payload)
    session.commit()
    result = row_dict(video)
    result["warnings"] = warnings
    return result


@router.post("/videos/{video_id}/reject")
def reject_video(video_id: str, payload: ReviewRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.reason:
        raise AppError(422, "rejection_reason_required", "驳回必须填写原因")
    video = required(session, VideoRevision, video_id, "视频版本")
    from app.services.revision_postprocess import assert_review_ready
    assert_review_ready(session.get(GenerationJob, video.generation_job_id), video, session)
    video.review_status = "rejected"
    job = session.get(GenerationJob, video.generation_job_id)
    if job.provider == "comfyui_h3":
        job.status = "rejected"
    video.review_reason = payload.reason
    approve_record(session, "video_revision", video.id, video.revision, "rejected", payload)
    session.commit()
    return row_dict(video)


@router.post("/videos/{video_id}/quality-check")
def run_quality_check(request: Request, video_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    video = required(session, VideoRevision, video_id, "视频版本")
    settings = settings_from(request)
    path = safe_media_path(settings.media_root, video.relative_path)
    results = inspect_media(path, settings)
    job = session.get(GenerationJob, video.generation_job_id)
    if job.provider == "comfyui_h3" and job.status == "generated":
        job.status = "reviewing"
    created = []
    for result in results:
        check = QualityCheck(video_revision_id=video.id, check_type=result["check_type"], status=result["status"], score=result.get("score"), details_json=result.get("details", {}))
        session.add(check)
        session.flush()
        created.append(row_dict(check))
    audit(session, "video_revision", video.id, "quality_check", {"results": [{"type": r["check_type"], "status": r["status"]} for r in results]})
    session.commit()
    return {"checks": created, "automatic_check_only": True, "human_review_status": video.review_status}


# Publishing routes removed — feature not available in this deployment.
@router.get("/publishing/capabilities")
def publishing_capabilities() -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.get("/publishing/accounts")
def list_accounts(include_removed: bool = False, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.post("/publishing/accounts", status_code=status.HTTP_201_CREATED)
def create_account(payload: PublishingAccountCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.patch("/publishing/accounts/{account_id}")
def patch_account(account_id: str, payload: PublishingAccountPatch, session: Session = Depends(get_db)) -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.delete("/publishing/accounts/{account_id}")
def remove_account(account_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.get("/publishing/presets")
def list_presets(account_id: str | None = None, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.post("/publishing/presets", status_code=status.HTTP_201_CREATED)
def create_preset(payload: PublishingPresetCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.post("/publishing/batches/dry-run", status_code=status.HTTP_201_CREATED)
def create_dry_run(payload: DryRunRequest, session: Session = Depends(get_db)) -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")

@router.get("/publishing/batches/{batch_id}")
def get_batch(batch_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    raise AppError(404, "feature_removed", "批量发布功能已停用")




@router.get("/audit-events")
def list_audit_events(limit: int = 100, session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    safe_limit = min(max(limit, 1), 500)
    return [row_dict(row) for row in session.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(safe_limit)).all()]


@router.get("/media/{relative_path:path}")
def serve_media(request: Request, relative_path: str, download: bool = False) -> FileResponse:
    path = safe_media_path(settings_from(request).media_root, relative_path, must_exist=True)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name if download else None)

