from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import one_click_folder_production as launcher


def _write_docx(path: Path, text: str = "商品展示") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def _write_png(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + marker.ljust(8, b"0"))


def test_scan_binds_shared_folder_images_and_keeps_groups_separate(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    group_a = root / "product-a"
    group_b = root / "product-b"
    _write_docx(group_a / "a.docx")
    _write_docx(group_a / "b.docx")
    _write_png(group_a / "front.png", b"front")
    (group_a / "side.jpg").write_bytes(b"\xff\xd8\xff" + b"side".ljust(13, b"0"))
    _write_docx(group_b / "c.docx")
    _write_png(group_b / "reference.png", b"other")

    result = launcher.scan(root)

    assert len(result.candidates) == 3
    group_a_candidates = [item for item in result.candidates if item.path.parent == group_a]
    group_b_candidate = next(item for item in result.candidates if item.path.parent == group_b)
    assert all(item.product_match for item in result.candidates)
    assert len(group_a_candidates) == 2
    assert {image.sha256 for image in group_a_candidates[0].images} == {image.sha256 for image in group_a_candidates[1].images}
    assert len(group_a_candidates[0].images) == 2
    assert len(group_b_candidate.images) == 1
    assert launcher.product_identity(group_a_candidates[0]) == ("product-a", "")
    assert launcher.product_identity(group_b_candidate) == ("product-b", "")


def test_explicit_product_name_is_used_without_guessing_brand(tmp_path: Path) -> None:
    root = tmp_path / "scripts" / "generic"
    _write_docx(root / "script.docx", "主体展示")
    _write_png(root / "photo.png", b"image")
    candidate = launcher.scan(root.parent).candidates[0]

    assert launcher.product_identity(candidate, product_name="用户产品") == ("用户产品", "")
    assert launcher.product_identity(candidate, brand="明确品牌") == ("generic", "明确品牌")


def test_explicit_product_identity_propagates_only_within_identical_shared_image_group(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    group = root / "drinkware"
    _write_docx(group / "01-explicit.docx", "Owala FreeSip 32oz 商品展示")
    _write_docx(group / "02-generic.docx", "手持水杯展示")
    _write_png(group / "front.png", b"front")
    candidates = launcher.scan(root).candidates

    identities = launcher.resolve_product_group_identities(candidates)

    assert len(candidates) == 2
    assert {identities[item.sha256]["product_name"] for item in candidates} == {"Owala FreeSip"}
    assert {identities[item.sha256]["brand"] for item in candidates} == {"Owala"}
    assert {identities[item.sha256]["source"] for item in candidates} == {"shared_image_group_explicit_anchor"}


def test_unrecognized_product_names_do_not_become_a_guessed_identity(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    group = root / "mixed"
    _write_docx(group / "a.docx", "Hydro Flask 商品展示")
    _write_docx(group / "b.docx", "Hydro Flask 商品展示")
    _write_png(group / "shared.png", b"same")
    candidates = launcher.scan(root).candidates
    identities = launcher.resolve_product_group_identities(candidates)
    assert {identities[item.sha256]["product_name"] for item in candidates} == {"mixed"}
    assert all(identities[item.sha256]["brand"] == "" for item in candidates)
    assert all(identities[item.sha256]["source"] == "folder_label_unverified" for item in candidates)


def test_completed_import_index_matches_exact_sha_without_reupload() -> None:
    digest = "a" * 64

    class FakeApi:
        def get(self, path: str):
            assert path == "/api/word-imports"
            return [{"id": "import-1", "sha256": digest, "status": "completed",
                     "result_json": {"project_id": "project-1"}}]

    assert launcher.completed_import_index(FakeApi(), {digest}) == {
        digest: {"import_id": "import-1", "status": "completed", "project_id": "project-1", "sha256": digest}
    }


def test_api_default_timeout_covers_server_side_word_import_parse() -> None:
    assert launcher.Api("http://example.test").timeout == 120


def test_all_selection_respects_hard_limit(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    for number in range(3):
        _write_docx(root / f"{number}.docx")
    candidates = launcher.scan(root).candidates

    assert len(launcher.select_all(candidates, limit=3)) == 3
    with pytest.raises(launcher.WorkflowError, match="超过 --limit"):
        launcher.select_all(candidates, limit=2)


def test_state_ledger_persists_idempotency_data_without_api_query(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    with launcher.RunLedger(state_path) as ledger:
        ledger.record("fingerprint", {"status": "started", "project_id": "project-1", "stage": "started"})
        with pytest.raises(launcher.WorkflowError, match="另一个进程占用"):
            launcher.RunLedger(state_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["entries"]["fingerprint"]["project_id"] == "project-1"
    assert state_path.with_suffix(".json.lock").exists()
    with launcher.RunLedger(state_path) as resumed:
        assert resumed.get("fingerprint")["status"] == "started"


def test_api_fingerprint_strips_credentials_and_query() -> None:
    assert launcher.api_fingerprint("https://user:secret@example.test:8443/api?token=private") == "https://example.test:8443/api"


def test_default_quality_uses_fast_local_realesrgan_and_exact_ai_plan() -> None:
    assert launcher.QUALITY == {
        "resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "16:9"
    }

    class FakeApi:
        def get(self, path: str):
            assert path == "/api/quality/capabilities"
            return {"ai_plans": [{
                "requested": {"resolution": "4k", "aspect_ratio": "16:9"},
                "supported": True,
                "ai_super_resolution": True,
                "postprocess_model": {"name": "RealESRGAN_x4plus"},
            }]}

    supported, evidence = launcher.quality_supported(FakeApi(), launcher.QUALITY)
    assert supported is True
    assert evidence["postprocess_model"]["name"] == "RealESRGAN_x4plus"


def test_quality_preflight_checks_only_the_selected_processing_plan() -> None:
    class FakeApi:
        def __init__(self, capabilities):
            self.capabilities = capabilities

        def get(self, path: str):
            assert path == "/api/quality/capabilities"
            return self.capabilities

    # The default Real-ESRGAN route remains available even when the service
    # does not expose any SeedVR2 capabilities.
    ai_capabilities = {"ai_plans": [{
        "requested": {"resolution": "4k", "aspect_ratio": "16:9"},
        "supported": True,
        "ai_super_resolution": True,
        "postprocess_model": {"name": "RealESRGAN_x4plus"},
    }]}
    assert launcher.quality_supported(FakeApi(ai_capabilities), launcher.QUALITY)[0] is True

    # Explicit SeedVR2 selection still checks SeedVR2's own exact capability;
    # it neither borrows Real-ESRGAN evidence nor forces SeedVR2 on other routes.
    seed_quality = {**launcher.QUALITY, "processing": "seedvr2"}
    assert launcher.quality_supported(FakeApi(ai_capabilities), seed_quality)[0] is False
    seed_capabilities = {"seedvr2_plans": [{
        "requested": {"resolution": "4k", "aspect_ratio": "16:9"},
        "supported": True,
    }]}
    assert launcher.quality_supported(FakeApi(seed_capabilities), seed_quality)[0] is True


def test_identity_reference_preserves_all_selected_views_with_cover_first() -> None:
    all_images = ["cover", "in-hand-closeup"]
    product = {"cover_image_id": "cover", "images": [
        {"id": "cover", "sha256": "a" * 64},
        {"id": "in-hand-closeup", "sha256": "b" * 64},
    ]}

    assert launcher.identity_reference_image_ids(product, all_images) == all_images
    assert launcher.identity_reference_image_ids({}, all_images) == all_images
    assert launcher.identity_reference_image_ids(product, ["in-hand-closeup", "cover", "detail-3"]) == ["cover", "in-hand-closeup", "detail-3"]
    assert launcher.identity_reference_image_ids(product, []) == []


def test_batch_continues_after_failure_and_skips_already_dispatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "scripts"
    _write_docx(root / "a.docx", "商品展示 A")
    _write_docx(root / "b.docx", "商品展示 B")
    state_path = tmp_path / "data" / "ledger.json"

    class FakeApi:
        base = "http://example.test:8765"

        def __init__(self, *_args, **_kwargs): pass

        def get(self, path: str):
            if path == "/api/health": return {"status": "ok"}
            if path == "/api/providers/h3/health":
                return {"provider": "comfyui_h3", "mode": "real", "ready": True,
                        "reference_to_video_available": True}
            if path == "/api/quality/capabilities":
                return {"ai_plans": [{"requested": {"resolution": "4k", "aspect_ratio": "16:9"},
                    "supported": True, "ai_super_resolution": True, "postprocess_model": {"name": "RealESRGAN_x4plus"}}],
                    "modes": {"fast": {"steps": 4}}}
            return {"items": []}

    submitted: list[str] = []

    def fake_submit(_api, candidate, *, progress=None, **_kwargs):
        submitted.append(candidate.relative_path)
        if candidate.path.name == "a.docx":
            raise launcher.WorkflowError("synthetic preflight failure", phase="preflight")
        if progress: progress("started", {"project_id": "p-b", "run_id": "r-b"})
        return {"status": "started", "project_id": "p-b", "run_id": "r-b"}

    monkeypatch.setattr(launcher, "Api", FakeApi)
    monkeypatch.setattr(launcher, "submit_candidate", fake_submit)
    args = ["--root", str(root), "--all", "--submit", "--confirm-submit", "--limit", "10", "--state-file", str(state_path), "--api", FakeApi.base]

    assert launcher.main(args) == 1
    assert len(submitted) == 2
    saved = json.loads(state_path.read_text(encoding="utf-8"))["entries"]
    statuses = {entry["script"]: entry["status"] for entry in saved.values()}
    assert statuses == {"a.docx": "preflight_failed", "b.docx": "started"}

    submitted.clear()
    assert launcher.main(args) == 1
    assert submitted == []
