"""End-to-end backend tests for Feishu Bitable config, sync, bridge protocol, serial batch generation & archiving."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import models as m
from app.core.config import Settings
from app.core.db import Base, build_engine, build_session_factory
from app.main import create_app
from app.services.feishu_client import (
    DEFAULT_FEISHU_BITABLE_URL,
    extract_attachments,
    extract_cell_text,
    parse_feishu_bitable_url,
    resolve_column_names,
    script_column_names,
)
from app.services import feishu_orchestrator as svc


def test_parse_url_and_script_columns():
    parsed = parse_feishu_bitable_url(DEFAULT_FEISHU_BITABLE_URL)
    assert parsed["app_token"] == "Dakxb6f3aalMdssK4Tmc2WninNf"
    assert parsed["table_id"] == "tblVtjouz9F0855v"
    assert parsed["view_id"] == "vewT6bwcpf"

    assert script_column_names("脚本1", 5) == ["脚本1", "脚本2", "脚本3", "脚本4", "脚本5"]
    assert script_column_names("脚本", 3) == ["脚本1", "脚本2", "脚本3"]

    fields = [
        {"field_name": "产品名称"},
        {"field_name": "参考视频"},
        {"field_name": "参考图"},
        {"field_name": "任务名称"},
        {"field_name": "脚本1"},
    ]
    resolved = resolve_column_names(fields, {})
    assert resolved["col_product_name"] == "产品名称"
    assert resolved["col_reference_video"] == "参考视频"
    assert resolved["col_reference_image"] == "参考图"
    assert resolved["col_task_name"] == "任务名称"
    assert resolved["col_script_start"] == "脚本1"
    assert extract_cell_text([{"text": "脆皮咸蛋黄"}, {"text": "捏捏乐"}]) == "脆皮咸蛋黄捏捏乐"
    assert extract_attachments([{"file_token": "ft_1", "name": "demo.mp4", "size": 123}])[0]["file_token"] == "ft_1"


def test_feishu_service_sync_generate_and_download_flow(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "secret.key"
    key_file.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    os.chmod(key_file, 0o600)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY_FILE", str(key_file))

    db_url = f"sqlite:///{tmp_path / 'feishu_test.db'}"
    media_root = tmp_path / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    settings = Settings(app_env="test", database_url=db_url, media_root=media_root, h3_demo_mode=True)
    engine = build_engine(db_url)
    Base.metadata.create_all(engine)
    Session = build_session_factory(engine)

    app = create_app(settings)
    try:
        with TestClient(app) as client:
            # 1. Create Feishu service profile via Settings API
            resp = client.post("/api/settings/ai-services", json={
                "module": "feishu_sync",
                "provider": "feishu_bitable",
                "name": "主飞书多维表格",
                "base_url": "https://open.feishu.cn",
                "enabled": True,
                "api_key": "cli_secret_test_value_123",
                "settings": {
                    "app_id": "cli_a1234567890",
                    "bitable_url": DEFAULT_FEISHU_BITABLE_URL,
                    "col_product_name": "产品名称",
                    "col_reference_video": "参考视频",
                    "col_reference_image": "参考图",
                    "col_task_name": "任务名称",
                    "col_script_start": "脚本1",
                    "analysis_engine": "antigravity",
                    "analysis_prompt": "保留产品一致性，生成5个不同室内光影与肤色手部场景的16-20秒脚本",
                    "script_count": 2,
                    "local_sync_dir": r"E:\feishu_sync",
                    "local_archive_dir": r"E:\vast视频",
                },
            })
            assert resp.status_code == 201, resp.text
            svc_data = resp.json()
            assert svc_data["has_api_key"] is True
            assert svc_data["settings"]["app_token"] == "Dakxb6f3aalMdssK4Tmc2WninNf"
            assert svc_data["settings"]["analysis_engine"] == "antigravity"
            assert svc_data["settings"]["script_count"] == 2

            # 2. Local bridge heartbeat
            hb = client.post("/api/feishu/bridge/heartbeat", json={
                "hostname": "MSI-WIN11",
                "sync_dir": r"E:\feishu_sync",
                "archive_dir": r"E:\vast视频",
                "available_engines": ["antigravity", "codex"],
                "rea_video_ready": True,
            })
            assert hb.status_code == 200 and hb.json()["online"] is True

            # 3. User clicks "一键同步飞书"
            r_sync = client.post("/api/feishu/sync", json={"force_rerun": False})
            assert r_sync.status_code == 201, r_sync.text
            job_id = r_sync.json()["id"]
            assert r_sync.json()["engine"] == "antigravity"

            # 4. Bridge claims sync job (mocking Feishu tenant token)
            with patch("app.services.feishu_client.FeishuClient.get_tenant_access_token", return_value="t-mock-token"):
                claimed = client.post("/api/feishu/bridge/claim-sync")
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()["claimed"]["job"]["id"] == job_id
            assert claimed.json()["claimed"]["tenant_access_token"] == "t-mock-token"

            # 5. Bridge uploads synced record metadata & scripts
            rec_done = client.post(f"/api/feishu/bridge/sync/{job_id}/record-complete", json={
                "feishu_record_id": "rec_001",
                "product_name": "咸蛋黄捏捏乐",
                "task_name": "春季爆浆测试",
                "engine_used": "antigravity",
                "analysis_summary": "4镜头俯拍ASMR拆解",
                "scripts": [
                    {"index": 1, "column": "脚本1", "text": "镜头1：0-5秒，木托盘特写，拍扁咸蛋黄捏捏乐。\n镜头2：5-10秒，双手揉捏爆浆。"},
                    {"index": 2, "column": "脚本2", "text": "镜头1：0-5秒，大理石台面特写，切开捏捏乐。\n镜头2：5-10秒，展示流心质感。"},
                ],
            })
            assert rec_done.status_code == 200, rec_done.text
            sync_rec_id = rec_done.json()["id"]

            job_done = client.post(f"/api/feishu/bridge/sync/{job_id}/complete", json={"ok": True, "processed_records": 1, "total_records": 1})
            assert job_done.status_code == 200 and job_done.json()["status"] == "completed"

            # 6. User clicks "一键生成视频" preview & start batch
            prev = client.post("/api/feishu/generate/preview", json={"refresh_from_feishu": False})
            assert prev.status_code == 200 and prev.json()["total"] == 2

            batch_resp = client.post("/api/feishu/generate", json={"refresh_from_feishu": False})
            assert batch_resp.status_code == 201, batch_resp.text
            batch_id = batch_resp.json()["id"]
            assert batch_resp.json()["total_items"] == 2
            assert len(batch_resp.json()["items"]) == 2

            # 7. Simulate completed videos & batch auto-download manifest for E:\vast视频\{产品名称}\
            out1 = media_root / "assemblies/a1/final1.mp4"
            out2 = media_root / "assemblies/a2/final2.mp4"
            out1.parent.mkdir(parents=True, exist_ok=True)
            out2.parent.mkdir(parents=True, exist_ok=True)
            out1.write_bytes(b"mp4-content-1")
            out2.write_bytes(b"mp4-content-2")

            with Session() as s:
                items = list(s.query(m.FeishuGenerationQueueItem).filter_by(batch_id=batch_id).order_by(m.FeishuGenerationQueueItem.order_index))
                items[0].status = "completed"
                items[0].output_relative_path = "assemblies/a1/final1.mp4"
                items[1].status = "completed"
                items[1].output_relative_path = "assemblies/a2/final2.mp4"
                b = s.get(m.FeishuGenerationBatch, batch_id)
                b.status = "running"
                s.commit()
                svc.advance_feishu_queue_once(s, settings)
                b = s.get(m.FeishuGenerationBatch, batch_id)
                assert b.status == "ready_for_download"

            dl_claim = client.post("/api/feishu/bridge/claim-download")
            assert dl_claim.status_code == 200
            manifest = dl_claim.json()["claimed"]
            assert manifest["batch_id"] == batch_id
            assert manifest["archive_dir"] == r"E:\vast视频"
            assert len(manifest["items"]) == 2
            assert manifest["items"][0]["product_folder"] == "咸蛋黄捏捏乐"

            # Stream download file 1
            f1 = client.get(manifest["items"][0]["download_url"])
            assert f1.status_code == 200 and f1.content == b"mp4-content-1"

            # Bridge confirms archived paths
            arch = client.post(f"/api/feishu/bridge/download/{batch_id}/complete", json={
                "ok": True,
                "archived_paths": {
                    manifest["items"][0]["item_id"]: r"E:\vast视频\咸蛋黄捏捏乐\春季爆浆测试_脚本1.mp4",
                    manifest["items"][1]["item_id"]: r"E:\vast视频\咸蛋黄捏捏乐\春季爆浆测试_脚本2.mp4",
                },
            })
            assert arch.status_code == 200 and arch.json()["status"] == "archived"
    finally:
        engine.dispose()


def test_approval_request_and_final_video_path_resolution(client):
    from app import models as m
    from app.schemas import ApprovalRequest
    from app.services.feishu_orchestrator import _resolve_project_final_video_path

    # Regression: ApprovalRequest forbids extra fields like reviewer/comment
    req = ApprovalRequest(operator="feishu_batch", reason="飞书一键生成自动审核")
    assert req.operator == "feishu_batch"

    with client.app.state.session_factory() as session:
        proj = m.Project(name="feishu_final_resolve_test", source_type="text", source_text="test", status="draft")
        session.add(proj)
        session.flush()
        asm = m.Assembly(project_id=proj.id, name="自动成片", timeline_json={})
        session.add(asm)
        session.flush()
        render = m.AssemblyRender(
            assembly_id=asm.id,
            version=1,
            kind="render",
            status="generated",
            relative_path="assemblies/test_final.mp4",
            snapshot_json={},
        )
        session.add(render)
        session.commit()
        assert _resolve_project_final_video_path(session, proj.id) == "assemblies/test_final.mp4"
