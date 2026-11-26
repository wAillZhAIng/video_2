#!/usr/bin/env python3
"""Folder-to-production launcher with isolated scan failures and guarded submission.

Default behavior is a read-only dry run. A real run requires an explicit file
selection plus --submit --confirm-submit, or the interactive SUBMIT confirmation.
"""
from __future__ import annotations
import argparse, hashlib, json, math, mimetypes, os, re, sys, time, uuid, zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_ROOT = Path(r"D:\脚本")
DEFAULT_API = os.environ.get("VIDEO_FACTORY_API_URL", "http://127.0.0.1:8765").rstrip("/")
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "folder_production_state.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DOCX_MAX_BYTES = 64 * 1024 * 1024
IMAGE_MAX_BYTES = 10 * 1024 * 1024
QUALITY = {"resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "16:9"}
SUPPORTED_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
PROCESSING_PLANS = {
    "lanczos": ("plans", "普通缩放"),
    "ai_super_resolution": ("ai_plans", "Real-ESRGAN 本机 AI 超分"),
    "seedvr2": ("seedvr2_plans", "SeedVR2"),
    "native": ("native_plans", "原生画质"),
    "reconstruction": ("reconstruction_plans", "视频重建"),
}

class WorkflowError(RuntimeError):
    def __init__(self, message: str, *, phase: str = "", details: Any = None, outcome_unknown: bool = False):
        super().__init__(message); self.phase = phase; self.details = details; self.outcome_unknown = outcome_unknown

@dataclass(frozen=True)
class ImageAsset:
    path: Path
    sha256: str
    size: int
    mime: str

@dataclass
class Candidate:
    path: Path
    relative_path: str
    sha256: str
    size: int
    text: str
    images: list[ImageAsset] = field(default_factory=list)
    product_match: bool = False
    product_reason: str = ""
    issues: list[dict[str, str]] = field(default_factory=list)
    duplicate_images: list[str] = field(default_factory=list)

@dataclass
class ScanResult:
    candidates: list[Candidate]
    rejected: list[dict[str, str]]

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def _inside(path: Path, root: Path) -> bool:
    try: path.resolve().relative_to(root.resolve()); return True
    except (OSError, ValueError): return False

def read_docx(path: Path) -> tuple[bytes, str]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > DOCX_MAX_BYTES: raise WorkflowError(f"DOCX 大小超出 1 byte 至 {DOCX_MAX_BYTES} byte：{path.name}", phase="scan")
        raw = path.read_bytes()
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as archive:
            bad_member = archive.testzip()
            if bad_member: raise WorkflowError(f"DOCX ZIP 内容损坏（{bad_member}）：{path.name}", phase="scan")
            xml = archive.read("word/document.xml")
    except WorkflowError: raise
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise WorkflowError(f"无法读取有效 DOCX：{path.name}（{type(exc).__name__}）", phase="scan") from exc
    try: root = ET.fromstring(xml)
    except ET.ParseError as exc: raise WorkflowError(f"Word 正文 XML 损坏：{path.name}", phase="scan") from exc
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}; paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        pieces = []
        for node in paragraph.iter():
            if node.tag.endswith("}t") and node.text: pieces.append(node.text)
            elif node.tag.endswith("}tab"): pieces.append("\t")
            elif node.tag.endswith("}br"): pieces.append("\n")
        value = "".join(pieces).strip()
        if value: paragraphs.append(value)
    return raw, "\n".join(paragraphs)

def inspect_image(path: Path) -> ImageAsset:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream: header = stream.read(16)
    except OSError as exc: raise WorkflowError(f"产品图片不可读：{path.name}", phase="scan") from exc
    if not 0 < size <= IMAGE_MAX_BYTES: raise WorkflowError(f"图片大小超出 1 byte 至 {IMAGE_MAX_BYTES} byte：{path.name}", phase="scan")
    suffix = path.suffix.casefold()
    valid = (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n")
        or suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff")
        or suffix == ".webp" and len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    if not valid: raise WorkflowError(f"图片扩展名与真实格式不符：{path.name}", phase="scan")
    return ImageAsset(path, sha256_file(path), size, mimetypes.guess_type(path.name)[0] or "application/octet-stream")

def match_product(path: Path, text: str, *, brand: str | None = None, product_name: str | None = None) -> tuple[bool, str]:
    """Use explicit product mentions when present; folder assets are bound separately."""
    haystack = "\n".join((path.stem, path.parent.name, text)).casefold()
    brand_hit = bool(brand and brand.strip() and brand.strip().casefold() in haystack)
    model_hit = bool(product_name and product_name.strip() and product_name.strip().casefold() in haystack)
    if brand_hit or model_hit:
        return True, "剧本明确包含命令行指定的品牌或产品名称"
    if "owala" in haystack and "freesip" in haystack:
        return True, "剧本文字明确包含 Owala FreeSip"
    return False, "剧本未明确标注具体产品；若同目录存在图片，将按文件夹产品组绑定"


def explicit_product_identity(candidate: Candidate, *, product_name: str | None = None,
                              brand: str | None = None) -> tuple[str, str] | None:
    """Return only an identity explicitly anchored by the user or screenplay."""
    haystack = "\n".join((candidate.path.stem, candidate.path.parent.name, candidate.text)).casefold()
    if product_name and product_name.strip():
        resolved_name = product_name.strip()
        resolved_brand = brand.strip() if brand and brand.strip() else (
            "Owala" if "owala" in resolved_name.casefold() and "freesip" in resolved_name.casefold() else "")
        return resolved_name[:200], resolved_brand[:200]
    if "owala" in haystack and "freesip" in haystack:
        return "Owala FreeSip", brand.strip()[:200] if brand and brand.strip() else "Owala"
    if brand and brand.strip():
        group = Path(candidate.relative_path).parent.as_posix().strip("./")
        return (group or candidate.path.stem)[:200], brand.strip()[:200]
    return None


def product_identity(candidate: Candidate, *, product_name: str | None = None,
                     brand: str | None = None) -> tuple[str, str]:
    """Resolve a stable product identity without guessing from a folder label."""
    explicit = explicit_product_identity(candidate, product_name=product_name, brand=brand)
    if explicit:
        return explicit
    group = Path(candidate.relative_path).parent.as_posix().strip("./")
    return (group or candidate.path.stem)[:200], ""


def resolve_product_group_identities(candidates: list[Candidate], *, product_name: str | None = None,
                                     brand: str | None = None) -> dict[str, dict[str, Any]]:
    """Propagate a single explicit product identity only across identical folder image sets."""
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(Path(candidate.relative_path).parent.as_posix().casefold(), []).append(candidate)
    resolved: dict[str, dict[str, Any]] = {}
    for group in groups.values():
        image_sets = {tuple(sorted(image.sha256 for image in candidate.images)) for candidate in group}
        explicit = {identity for candidate in group
                    if (identity := explicit_product_identity(candidate, product_name=product_name, brand=brand))}
        shared_images = len(image_sets) == 1 and bool(next(iter(image_sets), ()))
        if shared_images and len(explicit) > 1:
            for candidate in group:
                resolved[candidate.sha256] = {"product_name": "", "brand": "", "source": "conflict",
                    "error": "同一文件夹共用的产品图对应多个明确商品身份；请先拆分产品目录或明确产品身份"}
            continue
        if shared_images and len(explicit) == 1:
            name, resolved_brand = next(iter(explicit))
            for candidate in group:
                resolved[candidate.sha256] = {"product_name": name, "brand": resolved_brand,
                    "source": "shared_image_group_explicit_anchor"}
            continue
        for candidate in group:
            identity = explicit_product_identity(candidate, product_name=product_name, brand=brand)
            name, resolved_brand = identity or product_identity(candidate)
            resolved[candidate.sha256] = {"product_name": name, "brand": resolved_brand,
                "source": "explicit_script" if identity else "folder_label_unverified"}
    return resolved

def resolve_aspect_ratio(text: str, requested: str = "auto") -> str:
    """Use an explicit CLI override or preserve the ratio stated in the screenplay."""
    if requested != "auto":
        if requested not in SUPPORTED_ASPECT_RATIOS:
            raise WorkflowError(f"不支持的画幅：{requested}", phase="quality")
        return requested
    ratios = set()
    for value in re.findall(r"(?<!\d)(16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1)(?!\d)", text):
        left, right = (int(part.strip()) for part in re.split(r"[:：]", value))
        ratios.add(f"{left}:{right}")
    # Dimensions are a fallback when the screenplay gives pixels but no ratio.
    if not ratios:
        dimensions = re.findall(r"(?<!\d)(\d{3,5})\s*[xX×]\s*(\d{3,5})(?!\d)", text)
        for width, height in dimensions:
            divisor = math.gcd(int(width), int(height))
            ratio = f"{int(width) // divisor}:{int(height) // divisor}"
            if ratio in SUPPORTED_ASPECT_RATIOS:
                ratios.add(ratio)
    if len(ratios) > 1:
        raise WorkflowError("剧本包含互相冲突的输出画幅，未导入或提交：" + ", ".join(sorted(ratios)), phase="quality")
    return next(iter(ratios), QUALITY["aspect_ratio"])

def quality_for_candidate(candidate: Candidate, *, resolution: str, mode: str, processing: str,
                          requested_aspect_ratio: str = "auto") -> dict[str, str]:
    return {"resolution": resolution, "mode": mode, "processing": processing,
        "aspect_ratio": resolve_aspect_ratio(candidate.text, requested_aspect_ratio)}

def native_h3_canvas(aspect_ratio: str, megapixels: float = 0.59) -> tuple[int, int]:
    """Match the native H3 32-pixel grid at the selected portrait/landscape ratio."""
    if aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
        raise WorkflowError(f"MiniMax H3 不支持画幅 {aspect_ratio}", phase="quality")
    width_ratio, height_ratio = (int(part) for part in aspect_ratio.split(":"))
    ratio = width_ratio / height_ratio
    pixels = round(megapixels * 1_000_000, 2)
    native_height = math.sqrt(pixels / ratio)
    width = max(32, round(native_height * ratio / 32) * 32)
    height = max(32, round(native_height / 32) * 32)
    return width, height

def _image_files(directory: Path) -> Iterable[Path]:
    try: entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except OSError: return ()
    return (path for path in entries if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS)

def inspect_candidate(path: Path, root: Path, *, brand: str | None, product_name: str | None) -> Candidate:
    raw, text = read_docx(path); matched, reason = match_product(path, text, brand=brand, product_name=product_name)
    candidate = Candidate(path, path.relative_to(root).as_posix(), sha256_bytes(raw), len(raw), text,
                          product_match=matched, product_reason=reason)
    seen = set()
    for image_path in _image_files(path.parent):
        try:
            if not _inside(image_path, root): raise WorkflowError(f"图片解析后指向脚本根目录之外：{image_path.name}", phase="scan")
            image = inspect_image(image_path)
            if image.sha256 in seen: candidate.duplicate_images.append(image_path.name); continue
            seen.add(image.sha256); candidate.images.append(image)
        except WorkflowError as exc: candidate.issues.append({"file": image_path.name, "error": str(exc)})
    if candidate.images and not candidate.product_match:
        candidate.product_match = True
        candidate.product_reason = "同目录有效图片按文件夹约定视为共享产品参考图"
    return candidate

def scan(root: Path, *, brand: str | None = None, product_name: str | None = None) -> ScanResult:
    root = root.resolve()
    if not root.is_dir(): raise WorkflowError(f"脚本目录不存在：{root}", phase="scan")
    try:
        paths = sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() == ".docx"
            and not p.name.startswith("~$") and not any(part.startswith(".") for part in p.relative_to(root).parts)
            and _inside(p, root)), key=lambda p: p.as_posix().casefold())
    except OSError as exc: raise WorkflowError(f"扫描脚本目录失败：{exc}", phase="scan") from exc
    if not paths: raise WorkflowError(f"{root} 中没有可扫描的 .docx 文件", phase="scan")
    candidates, rejected = [], []
    for path in paths:
        try: candidates.append(inspect_candidate(path, root, brand=brand, product_name=product_name))
        except WorkflowError as exc: rejected.append({"file": path.relative_to(root).as_posix(), "error": str(exc)})
        except Exception as exc: rejected.append({"file": path.relative_to(root).as_posix(), "error": f"扫描异常 {type(exc).__name__}: {exc}"})
    return ScanResult(candidates, rejected)

class Api:
    def __init__(self, base: str, timeout: float = 120): self.base = base.rstrip("/"); self.timeout = timeout
    def request(self, method: str, path: str, *, body: bytes | None = None, content_type: str | None = None, read_only: bool = False) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "VideoFactoryFolderLauncher/1.1"}
        if content_type: headers["Content-Type"] = content_type
        request = Request(self.base + path, data=body, headers=headers, method=method)
        for attempt in range(3 if read_only else 1):
            try:
                with urlopen(request, timeout=self.timeout) as response: raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
            except HTTPError as exc:
                text = exc.read(256000).decode("utf-8", errors="replace")
                try:
                    obj = json.loads(text); detail = obj.get("detail", obj.get("message", obj)) if isinstance(obj, dict) else obj
                    message = str(detail.get("message") or detail.get("error") or detail) if isinstance(detail, dict) else str(detail)
                except (json.JSONDecodeError, TypeError): message = text[:1200]
                raise WorkflowError(f"API {method} {path} 返回 HTTP {exc.code}: {message}", phase="api", details={"http_status": exc.code}) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if read_only and attempt < 2: time.sleep(.4 * (attempt + 1)); continue
                raise WorkflowError(f"API {method} {path} 连接失败；写操作不会自动重试：{exc}", phase="api", outcome_unknown=not read_only) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkflowError(f"API {method} {path} 返回无效 JSON", phase="api", outcome_unknown=not read_only) from exc
        raise WorkflowError(f"API {method} {path} 请求失败", phase="api")
    def get(self, path: str) -> Any: return self.request("GET", path, read_only=True)
    def post_json(self, path: str, value: Any) -> Any:
        return self.request("POST", path, body=json.dumps(value, ensure_ascii=False).encode("utf-8"), content_type="application/json")
    def post_multipart(self, path: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> Any:
        boundary = "----VideoFactory" + uuid.uuid4().hex; parts = []
        for name, value in fields.items():
            parts.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode("utf-8"), b"\r\n"))
        for field_name, filename, data, mime in files:
            safe_filename = Path(filename.replace("\\", "/")).name.replace('"', "_")
            parts.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_filename}"\r\n'.encode("utf-8"), f"Content-Type: {mime}\r\n\r\n".encode(), data, b"\r\n"))
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, body=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}")


class RunLedger:
    """Local idempotency journal; never stores API credentials or media contents."""
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            self.lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.lock_fd).st_size == 0: os.write(self.lock_fd, b"\0")
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(self.lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            fd = getattr(self, "lock_fd", None)
            if fd is not None: os.close(fd); self.lock_fd = None
            raise WorkflowError(f"批量制作已由另一个进程占用：{self.lock_path}", phase="ledger") from exc
        self.entries: dict[str, dict[str, Any]] = {}
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("version") != 1 or not isinstance(loaded.get("entries"), dict):
                    raise ValueError("unknown ledger structure")
                self.entries = loaded["entries"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.close()
            raise WorkflowError(f"批量防重复记录不可读取；为避免重复提交已停止：{path}", phase="ledger") from exc

    def close(self) -> None:
        fd = getattr(self, "lock_fd", None)
        if fd is None: return
        try:
            os.close(fd)
        finally:
            self.lock_fd = None

    def __enter__(self) -> "RunLedger": return self
    def __exit__(self, *_: Any) -> None: self.close()

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.entries.get(key)
        return value if isinstance(value, dict) else None

    def record(self, key: str, value: dict[str, Any]) -> None:
        self.entries[key] = {**value, "updated_at": datetime.now(timezone.utc).isoformat()}
        payload = json.dumps({"version": 1, "entries": self.entries}, ensure_ascii=False, indent=2)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)


def api_fingerprint(base: str) -> str:
    parsed = urlsplit(base)
    # Strip user-info, path parameters, query and fragment so credentials never land in the journal.
    host = parsed.hostname or "unknown-host"
    if parsed.port: host += f":{parsed.port}"
    return f"{parsed.scheme.casefold()}://{host}{parsed.path.rstrip('/')}"


def candidate_fingerprint(candidate: Candidate, quality: dict[str, str], api: Api) -> str:
    identity = {"path": candidate.relative_path, "docx_sha256": candidate.sha256,
        "images": sorted(image.sha256 for image in candidate.images), "quality": quality,
        "endpoint": api_fingerprint(api.base)}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

def quality_supported(api: Api, quality: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    capabilities = api.get("/api/quality/capabilities"); ratio = quality["aspect_ratio"]; resolution = quality["resolution"]
    plan_key, label = PROCESSING_PLANS.get(quality.get("processing", ""), ("", "所选处理方式"))
    plans = capabilities.get(plan_key, []) if isinstance(capabilities, dict) and plan_key else []
    matching = [item for item in plans if item.get("requested", {}).get("resolution") == resolution and item.get("requested", {}).get("aspect_ratio") == ratio]
    if matching:
        evidence = matching[-1]
        supported = evidence.get("supported") is True
        if quality.get("processing") == "ai_super_resolution":
            supported = supported and evidence.get("ai_super_resolution") is True and bool(evidence.get("postprocess_model"))
        return supported, evidence
    return False, {"supported": False, "reason": f"服务未返回 {label} 的 {resolution} · {ratio} 验收能力", "capability_count": len(plans)}

def generation_supported(api: Api, quality: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    """Require the direct native H3 route and the requested sampler mapping."""
    health = api.get("/api/providers/h3/health")
    expected_steps = {"preview": 12, "standard": 20, "fast": 4, "high": 30}.get(quality.get("mode"))
    capabilities = api.get("/api/quality/capabilities")
    mode = capabilities.get("modes", {}).get(quality.get("mode"), {}) if isinstance(capabilities, dict) else {}
    steps = mode.get("steps") if isinstance(mode, dict) else None
    result = {"health": health, "mode": quality.get("mode"), "steps": steps,
               "expected_steps": expected_steps, "workflow_route": "native MiniMax H3 Ref2VA"}
    ready = (isinstance(health, dict) and health.get("provider") == "comfyui_h3"
        and health.get("mode") == "real" and health.get("ready") is True
        and health.get("reference_to_video_available") is True
        and expected_steps is not None and steps == expected_steps)
    if not ready:
        result["reason"] = "本机 MiniMax H3 原生视频路由未就绪，或画质档位没有匹配的采样步数映射"
    return ready, result

def product_lookup(api: Api, name: str, brand: str) -> list[dict[str, Any]]:
    response = api.get("/api/product-library?q=" + quote(name)); items = response.get("items", []) if isinstance(response, dict) else []
    return [item for item in items if str(item.get("name", "")).casefold() == name.casefold()
            or (brand.casefold() in str(item.get("brand", "")).casefold() and "freesip" in str(item.get("name", "")).casefold())]


def completed_import_index(api: Api, script_hashes: set[str]) -> dict[str, dict[str, Any]]:
    """Index exact Word SHA matches so a timed-out import is reconciled, never duplicated."""
    response = api.get("/api/word-imports")
    items = response.get("items", []) if isinstance(response, dict) else response
    if not isinstance(items, list):
        raise WorkflowError("Word 导入记录接口返回结构无效；为避免重复建项，批次停止", phase="reconcile")
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("sha256") not in script_hashes:
            continue
        digest = item["sha256"]
        result = item.get("result_json") or item.get("result") or {}
        if isinstance(result, dict) and isinstance(result.get("result"), dict):
            result = result["result"]
        project_id = result.get("project_id") if isinstance(result, dict) else None
        record = {"import_id": item.get("id"), "status": item.get("status"),
                  "project_id": project_id, "sha256": digest}
        previous = found.get(digest)
        if previous and previous.get("project_id") != project_id:
            raise WorkflowError(f"同一 Word SHA 对应多个远端项目（{digest[:12]}）；停止以避免重复生产",
                                phase="reconcile", details={"matches": [previous, record]})
        if not previous or item.get("status") == "completed":
            found[digest] = record
    return found

def dry_run_plan(candidate: Candidate, api: Api | None, quality: dict[str, str], product_name: str | None, brand: str | None,
                 resolved_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_identity = resolved_identity or {}
    fallback_identity = product_identity(candidate, product_name=product_name, brand=brand)
    resolved_name = resolved_identity.get("product_name") or fallback_identity[0]
    resolved_brand = resolved_identity.get("brand", fallback_identity[1])
    result = {"script": candidate.relative_path, "script_sha256": candidate.sha256, "script_bytes": candidate.size,
        "product_match": candidate.product_match, "product_match_reason": candidate.product_reason,
        "product_group": Path(candidate.relative_path).parent.as_posix(), "product_name": resolved_name, "brand": resolved_brand,
        "product_images": [{"name": x.path.name, "sha256": x.sha256, "bytes": x.size, "mime": x.mime} for x in candidate.images],
        "duplicate_image_files_skipped": candidate.duplicate_images, "isolated_image_issues": candidate.issues,
        "product_identity_source": resolved_identity.get("source", "candidate"),
        "quality": quality, "would_submit_model": False}
    if resolved_identity.get("error"):
        result.setdefault("blockers", []).append(resolved_identity["error"])
    if candidate.product_match and not candidate.images: result["blockers"] = ["脚本匹配产品但目录没有有效产品图；禁止空绑定"]
    if len(candidate.images) > 8: result.setdefault("blockers", []).append("同一产品组图片超过 8 张；产品参考 API 上限为 8，程序不会静默丢弃图片")
    if api is None: result["api"] = {"reachable": False, "reason": "未执行 API 只读预检"}; return result
    try: result["api"] = {"reachable": True, "health": api.get("/api/health")}
    except WorkflowError as exc: result["api"] = {"reachable": False, "reason": str(exc)}; return result
    try:
        ready, evidence = generation_supported(api, quality)
        result["generation_preflight"] = {"ready": ready, "evidence": evidence}
        if not ready:
            result.setdefault("blockers", []).append("本机 MiniMax H3 原生工作流未就绪，未创建项目或提交模型")
    except WorkflowError as exc:
        result["generation_preflight"] = {"ready": False, "error": str(exc)}
        result.setdefault("blockers", []).append("无法核验本机 MiniMax H3 原生工作流，未创建项目或提交模型")
    try:
        supported, evidence = quality_supported(api, quality); result["quality_preflight"] = {"supported": supported, "evidence": evidence}
        if not supported: result.setdefault("blockers", []).append("本机超分处理方式、画幅和分辨率尚无匹配验收能力；真实提交前阻断，不创建项目或任务")
    except WorkflowError as exc:
        result["quality_preflight"] = {"supported": False, "error": str(exc)}; result.setdefault("blockers", []).append("无法读取画质能力，真实提交前阻断")
    if candidate.product_match and candidate.images:
        try:
            matches = product_lookup(api, resolved_name, resolved_brand)
            result["product_gallery"] = {"matches": [{"id": x.get("id"), "name": x.get("name"), "brand": x.get("brand"), "known_image_hashes": [i.get("sha256") for i in x.get("images", [])]} for x in matches]}
            if len(matches) > 1: result.setdefault("blockers", []).append("产品图库存在多个候选，不能自动猜选")
        except WorkflowError as exc:
            result["product_gallery"] = {"error": str(exc)}; result.setdefault("blockers", []).append("无法核验图库，真实提交前阻断")
    return result

def _result(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("result"); return value if isinstance(value, dict) else response

def import_word(api: Api, candidate: Candidate, quality: dict[str, str]) -> dict[str, Any]:
    fields = {"auto_start": "false", "allow_cloud_costs": "false", "allow_paid_fallback": "false", "quality_json": json.dumps(quality, ensure_ascii=False)}
    data = candidate.path.read_bytes()
    return api.post_multipart("/api/studio/import", fields, [("file", candidate.path.name, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])

def ensure_product(api: Api, candidate: Candidate, *, product_name: str, brand: str) -> tuple[dict[str, Any], list[str]]:
    matches = product_lookup(api, product_name, brand)
    if len(matches) > 1: raise WorkflowError("产品图库有多个相似产品；请先手动指定唯一产品", phase="product")
    product = matches[0] if matches else api.post_json("/api/product-library", {"name": product_name, "brand": brand, "category": "Drinkware",
        "notes": "由一键脚本同步入口导入；图片按 SHA-256 去重。", "selling_points": [],
        "evidence_notes": "商品信息来自用户提供图片与剧本文字，不推断未提供的规格。", "target_audience": ""})
    known = {item.get("sha256") for item in product.get("images", [])}; missing = [image for image in candidate.images if image.sha256 not in known]
    if missing:
        uploaded = api.post_multipart(f"/api/product-library/{quote(product['id'], safe='')}/images",
            {"purpose": "main", "notes": "Word 同目录图片；SHA-256=" + ",".join(x.sha256 for x in missing)},
            [("files", image.path.name, image.path.read_bytes(), image.mime) for image in missing])
        errors = uploaded.get("errors", []) if isinstance(uploaded, dict) else []
        if errors: raise WorkflowError("部分产品图上传失败：" + json.dumps(errors, ensure_ascii=False), phase="product", details={"product_id": product["id"]})
        product = uploaded.get("product", product)
    wanted = {x.sha256 for x in candidate.images}; image_ids = [item["id"] for item in product.get("images", []) if item.get("sha256") in wanted]
    if not image_ids:
        product = api.get(f"/api/product-library/{quote(product['id'], safe='')}")
        image_ids = [item["id"] for item in product.get("images", []) if item.get("sha256") in wanted]
    if len(image_ids) != len(candidate.images):
        raise WorkflowError("上传后图库 SHA-256 与图片 ID 不匹配，停止生成", phase="product", details={"expected": sorted(wanted), "found": [i.get("sha256") for i in product.get("images", [])]})
    return product, image_ids

def identity_reference_image_ids(product: dict[str, Any], image_ids: list[str]) -> list[str]:
    """Preserve every explicitly selected product view for native H3 input.

    The cover remains the first anchor.  The selected IDs are passed in their
    stable order; native H3 receives the images directly rather than a separate
    image-analysis or semantic-compiler stage.
    """
    selected = list(dict.fromkeys(image_ids))
    cover = product.get("cover_image_id")
    return [cover, *(key for key in selected if key != cover)] if cover in selected else selected

def _project_has_run(overview: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    run = overview.get("run") if isinstance(overview, dict) else None; return bool(run and run.get("id")), run

def poll_existing_run(api: Api, project_id: str, run_id: str, *, wait: bool = False,
                     poll_seconds: float = 5, wait_seconds: float = 7200) -> dict[str, Any]:
    """Resume observation of a known run without importing or submitting it again."""
    deadline = time.monotonic() + wait_seconds
    while True:
        view = api.get(f"/api/studio/projects/{quote(project_id, safe='')}")
        current = view.get("run") or {}
        if current.get("id") != run_id:
            raise WorkflowError("项目 run ID 已变化；停止恢复轮询", phase="poll", details=current)
        auto = (current.get("result_json") or {}).get("automatic_final") or {}
        auto_status = auto.get("status")
        run_status = current.get("status")
        terminal = run_status in {"completed", "completed_with_errors", "cancelled", "waiting_review"}
        if terminal and auto_status in {"generated", "failed", "blocked", "cancelled", "completed"}:
            videos = [x.get("video") for x in view.get("shots", []) if x.get("video")]
            renders = view.get("renders", [])
            exceptions = view.get("exceptions", [])
            failed = auto_status in {"failed", "blocked", "cancelled"} or run_status in {"completed_with_errors", "cancelled", "waiting_review"} or bool(exceptions)
            status = ("cancelled" if auto_status == "cancelled" or run_status == "cancelled"
                      else "blocked" if auto_status == "blocked" or run_status == "waiting_review"
                      else "failed" if failed else "finished")
            return {"status": status, "project_id": project_id, "run_id": run_id,
                "run_status": run_status, "automatic_final": auto, "videos": videos,
                "renders": renders, "exceptions": exceptions,
                "resumed_without_resubmitting": True}
        if not wait:
            return {"status": "still_running", "project_id": project_id, "run_id": run_id,
                "run_status": run_status, "automatic_final": auto,
                "resumed_without_resubmitting": True}
        if time.monotonic() >= deadline:
            return {"status": "still_running_or_wait_timeout", "project_id": project_id,
                "run_id": run_id, "run_status": run_status, "automatic_final": auto,
                "resumed_without_resubmitting": True}
        time.sleep(poll_seconds)

def submit_candidate(api: Api, candidate: Candidate, *, quality: dict[str, str], product_name: str | None, brand: str | None,
                     wait: bool = False, poll_seconds: float = 5, wait_seconds: float = 7200,
                     progress: Callable[[str, dict[str, Any]], None] | None = None,
                     resolved_identity: dict[str, Any] | None = None,
                     existing_import: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved_identity = resolved_identity or {}
    if resolved_identity.get("error"):
        raise WorkflowError(str(resolved_identity["error"]), phase="preflight")
    fallback_identity = product_identity(candidate, product_name=product_name, brand=brand)
    resolved_name = resolved_identity.get("product_name") or fallback_identity[0]
    resolved_brand = resolved_identity.get("brand", fallback_identity[1])
    if len(candidate.images) > 8: raise WorkflowError("同一产品组图片超过 8 张；未导入 Word 或提交模型", phase="preflight",
        details={"image_count": len(candidate.images), "maximum": 8})
    ready, generation = generation_supported(api, quality)
    if not ready:
        raise WorkflowError("本机 MiniMax H3 原生工作流未就绪，未导入 Word 或提交模型", phase="preflight", details=generation)
    supported, capability = quality_supported(api, quality)
    if not supported: raise WorkflowError("所选本机画质处理规格未验证，未导入 Word 或提交模型", phase="preflight", details=capability)
    if candidate.product_match and not candidate.images: raise WorkflowError("脚本匹配产品但无有效同目录产品图，未导入或提交", phase="preflight")
    matches = product_lookup(api, resolved_name, resolved_brand) if candidate.product_match else []
    if len(matches) > 1: raise WorkflowError("产品图库存在多个候选，未导入或提交", phase="preflight")
    imported = None
    if existing_import:
        if existing_import.get("status") != "completed" or not existing_import.get("project_id"):
            raise WorkflowError("发现相同 Word 的未完成导入记录；保留现场，不重复上传", phase="reconcile", details=existing_import)
        project_id = existing_import["project_id"]
        if progress: progress("import_reused", {"project_id": project_id, "import_id": existing_import.get("import_id"),
                                                  "reconciled_by_sha256": candidate.sha256})
        overview = api.get(f"/api/studio/projects/{quote(project_id, safe='')}")
        if (overview.get("project") or {}).get("id") != project_id:
            raise WorkflowError("SHA 对账得到的项目 ID 与项目详情不一致；停止提交", phase="reconcile", details=existing_import)
        shot_ids = [shot.get("id") for shot in overview.get("shots", []) if shot.get("id")]
    else:
        if progress: progress("import_pending", {})
        imported = import_word(api, candidate, quality)
        if not isinstance(imported, dict): raise WorkflowError("Word 导入响应结构无效", phase="import")
        result = _result(imported)
        observed_project_id = result.get("project_id") or imported.get("project_id")
        if progress: progress("import_response", {"project_id": observed_project_id})
        if imported.get("status") not in {"imported", "completed"} or imported.get("issues"):
            raise WorkflowError("Word 未形成可自动制作项目：" + json.dumps(imported.get("issues", imported.get("preview", {})), ensure_ascii=False)[:1600], phase="import", details=imported)
        project_id = result.get("project_id") or imported.get("project_id"); shot_ids = result.get("shot_ids") or imported.get("shot_ids") or []
        if not project_id or not shot_ids: raise WorkflowError("导入响应缺少 project_id 或 shot_ids", phase="import", details=imported)
    if len(shot_ids) > 20: raise WorkflowError(f"该 Word 有 {len(shot_ids)} 镜，自动成片单项目限 20 镜，未启动制作", phase="preflight", details={"project_id": project_id, "shot_count": len(shot_ids)})
    product = None; image_ids = []; product_plan = None
    if candidate.product_match:
        product, image_ids = ensure_product(api, candidate, product_name=resolved_name, brand=resolved_brand)
        anchor_image_ids = identity_reference_image_ids(product, image_ids)
        product_plan = {"product_id": product["id"], "image_ids": anchor_image_ids,
            "selection_reason": "按 SHA-256 同步并保留本次选定的全部商品视图，封面作为首个锚点；逐图分析区分商品与操作者，商品身份仍需真实视觉校验。"}
        if progress: progress("product_synced", {"project_id": project_id, "product_id": product.get("id"), "image_ids": image_ids})
    if existing_import is None:
        overview = api.get(f"/api/studio/projects/{quote(project_id, safe='')}")
    has_run, old_run = _project_has_run(overview)
    if has_run:
        if wait and old_run and old_run.get("id"):
            outcome = poll_existing_run(api, project_id, old_run["id"], wait=True,
                poll_seconds=poll_seconds, wait_seconds=wait_seconds)
            outcome["existing_run_preserved"] = True
        else:
            outcome = {"status": "existing_run_preserved", "project_id": project_id, "run": old_run,
                "message": "检测到已有制作记录；为避免重复派发，本次未再次启动"}
        if progress: progress(outcome.get("status", "existing_run_preserved"),
                              {"project_id": project_id, "run": old_run, "existing_run_preserved": True})
        return outcome
    width, height = native_h3_canvas(quality["aspect_ratio"])
    payload = {"quality": quality, "width": width, "height": height, "automatic_draft_assembly": False,
        "automatic_final_assembly": True, "allow_cloud_costs": False, "allow_paid_fallback": False}
    if product_plan: payload["product_reference_plan"] = product_plan
    if progress: progress("start_pending", {"project_id": project_id})
    started = api.post_json(f"/api/studio/projects/{quote(project_id, safe='')}/start", payload); run = started.get("run") if isinstance(started, dict) else None
    if not run or not run.get("id"): raise WorkflowError("启动响应缺少 run ID；先查询项目状态再重试", phase="start", details=started, outcome_unknown=True)
    outcome = {"status": "started", "project_id": project_id, "run_id": run["id"], "product_id": product.get("id") if product else None,
        "product_image_ids": image_ids, "shot_count": len(shot_ids), "quality": quality, "run_status": run.get("status")}
    if progress: progress("started", {"project_id": project_id, "run_id": run["id"], "run_status": run.get("status")})
    if wait:
        outcome.update(poll_existing_run(api, project_id, run["id"], wait=True,
            poll_seconds=poll_seconds, wait_seconds=wait_seconds))
        if progress: progress(outcome["status"], {"project_id": project_id, "run_id": run["id"],
            "run_status": outcome.get("run_status"), "video_count": len(outcome.get("videos", []))})
    return outcome

def resolve_selection(candidates: list[Candidate], selectors: list[str], *, limit: int) -> list[Candidate]:
    if not selectors: raise WorkflowError("真实提交必须精确选文件；请使用 --file 或 --interactive", phase="selection")
    selected = []
    for selector in selectors:
        normalized = selector.replace("\\", "/").casefold()
        matches = [x for x in candidates if x.relative_path.casefold() == normalized or x.path.as_posix().casefold() == normalized]
        if not matches and "/" not in normalized: matches = [x for x in candidates if Path(x.relative_path).name.casefold() == normalized]
        if len(matches) != 1: raise WorkflowError(f"选择 {selector!r} 匹配 {len(matches)} 份，请指定唯一相对路径", phase="selection")
        if matches[0] not in selected: selected.append(matches[0])
    if len(selected) > limit: raise WorkflowError(f"选择 {len(selected)} 份，超过 --limit={limit}", phase="selection")
    return selected


def select_all(candidates: list[Candidate], *, limit: int) -> list[Candidate]:
    if not candidates: raise WorkflowError("没有可处理的 Word 文件", phase="selection")
    if len(candidates) > limit:
        raise WorkflowError(f"扫描到 {len(candidates)} 份 Word，超过 --limit={limit}；提高限制后再启动", phase="selection")
    return list(candidates)

def interactive_selection(candidates: list[Candidate], limit: int) -> list[Candidate]:
    if not sys.stdin.isatty(): raise WorkflowError("当前非交互终端，请用 --file 精确选择", phase="selection")
    for n, item in enumerate(candidates, 1): print(f"{n:>3}. {item.relative_path}{' [匹配产品]' if item.product_match else ''}")
    raw = input(f"输入要处理的编号（最多 {limit} 个，逗号分隔）：").strip()
    try: indexes = [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError as exc: raise WorkflowError("编号须为逗号分隔的数字", phase="selection") from exc
    if not indexes or len(indexes) > limit or len(set(indexes)) != len(indexes) or any(x < 1 or x > len(candidates) for x in indexes):
        raise WorkflowError(f"请选择 1 至 {limit} 个范围内且不重复的编号", phase="selection")
    return [candidates[x - 1] for x in indexes]

def print_json(value: Any) -> None: print(json.dumps(value, ensure_ascii=False, indent=2, default=str))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="脚本目录导入与自动制作（默认只读 dry-run）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT); parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--file", action="append", default=[], help="精确相对路径或唯一文件名；可重复指定")
    parser.add_argument("--all", dest="all_files", action="store_true", help="将扫描到的全部 Word 按目录产品组顺序处理")
    parser.add_argument("--interactive", action="store_true"); parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--brand", default=None, help="可选：明确指定产品品牌；默认不臆测")
    parser.add_argument("--product-name", default=None, help="可选：明确指定产品名称；默认按目录产品组或文档明确内容")
    parser.add_argument("--resolution", choices=("720p", "1080p", "2k", "4k"), default=QUALITY["resolution"])
    parser.add_argument("--mode", choices=("high", "standard", "fast", "preview"), default=QUALITY["mode"])
    parser.add_argument("--processing", choices=("lanczos", "native", "ai_super_resolution", "seedvr2", "reconstruction"), default=QUALITY["processing"])
    parser.add_argument("--aspect-ratio", choices=("auto", "16:9", "9:16", "1:1"), default="auto",
        help="默认从每份 Word 解析；未标注时回退到 16:9")
    parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--submit", action="store_true")
    parser.add_argument("--confirm-submit", action="store_true"); parser.add_argument("--wait", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true", help="批量任务中隔离失败项并继续其余文件")
    parser.add_argument("--stop-on-error", action="store_true", help="第一项失败后停止批量任务")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="防重复提交状态文件（默认位于项目 data/）")
    parser.add_argument("--retry-failed", action="store_true", help="仅重试已记录为安全的 preflight_failed 项")
    parser.add_argument("--timeout", type=float, default=120); parser.add_argument("--poll-seconds", type=float, default=5); parser.add_argument("--wait-seconds", type=float, default=7200)
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1 or min(args.timeout, args.poll_seconds, args.wait_seconds) <= 0: print("limit/timeout/poll/wait 必须为正数", file=sys.stderr); return 2
    if args.all_files and (args.file or args.interactive): print("--all 不能与 --file 或 --interactive 同时使用", file=sys.stderr); return 2
    if args.stop_on_error and args.continue_on_error: print("--stop-on-error 与 --continue-on-error 不能同时使用", file=sys.stderr); return 2
    if args.submit and args.dry_run: print("--submit 与 --dry-run 不能同时使用", file=sys.stderr); return 2
    if args.submit and not args.confirm_submit: print("拒绝提交：真实运行还需要 --confirm-submit", file=sys.stderr); return 2
    if args.confirm_submit and not args.submit and not args.interactive: print("--confirm-submit 只能与 --submit 或 --interactive 搭配", file=sys.stderr); return 2
    try:
        found = scan(args.root, brand=args.brand, product_name=args.product_name)
        print(f"扫描完成：有效 Word {len(found.candidates)} 份；隔离 Word {len(found.rejected)} 份")
        for item in found.rejected: print(f"隔离：{item['file']} — {item['error']}")
        if args.all_files: selected = select_all(found.candidates, limit=args.limit)
        elif args.interactive: selected = interactive_selection(found.candidates, args.limit)
        elif args.file: selected = resolve_selection(found.candidates, args.file, limit=args.limit)
        elif args.submit: raise WorkflowError("真实提交必须使用 --all、--file 或 --interactive 明确选择", phase="selection")
        else: selected = found.candidates[:1]; print("未选文件：只展示排序最前候选；真实批量提交请使用 --all。")
        qualities = [quality_for_candidate(item, resolution=args.resolution, mode=args.mode,
            processing=args.processing, requested_aspect_ratio=args.aspect_ratio) for item in selected]
        api = Api(args.api, args.timeout)
        identities = resolve_product_group_identities(selected, product_name=args.product_name, brand=args.brand)
        plans = [dry_run_plan(item, api, quality, args.product_name, args.brand, identities.get(item.sha256))
            for item, quality in zip(selected, qualities)]
        print_json({"selected_count": len(selected), "limit": args.limit, "mode": "all" if args.all_files else "selected", "plans": plans, "rejected_word_files": found.rejected,
            "note": "默认只读；所选本机处理方式没有对应画幅与分辨率的 capability supported=true 时，不会开始导入或生成。"})
        should_submit = args.submit
        if args.interactive and not args.dry_run and not args.submit:
            should_submit = input("复核计划后输入 START 开始所选真实任务；其他输入退出：").strip() == "START"
        if not should_submit: print("Dry-run 完成：没有写数据库、上传产品图或提交模型任务。"); return 0
        outcomes: list[dict[str, Any]] = []; continue_on_error = args.continue_on_error or args.all_files
        imports = completed_import_index(api, {candidate.sha256 for candidate in selected})
        with RunLedger(args.state_file) as ledger:
            for candidate, plan, quality in zip(selected, plans, qualities):
                key = candidate_fingerprint(candidate, quality, api); previous = ledger.get(key)
                existing_import = imports.get(candidate.sha256)
                if previous:
                    safe_retry = args.retry_failed and previous.get("status") == "preflight_failed"
                    previous_status = previous.get("status")
                    previous_stage = previous.get("stage")
                    project_id = previous.get("project_id")
                    run_id = previous.get("run_id")
                    start_rejection = (previous_stage in {"start_pending", "start_pending_retry"}
                        and "HTTP 422" in str(previous.get("error", "")))
                    can_reconcile_import = bool(existing_import and existing_import.get("status") == "completed"
                        and existing_import.get("project_id")
                        and previous_status in {"unknown", "import_pending", "failed", "start_pending"})
                    resumable = previous_status in {"started", "still_running", "still_running_or_wait_timeout"}
                    if resumable and project_id and run_id:
                        try:
                            item = poll_existing_run(api, project_id, run_id, wait=args.wait,
                                poll_seconds=args.poll_seconds, wait_seconds=args.wait_seconds)
                            item["script"] = candidate.relative_path
                            ledger.record(key, {**previous, **item, "stage": item["status"]})
                        except WorkflowError as exc:
                            item = {"status": "manual_check_required", "script": candidate.relative_path,
                                "previous_status": previous_status, "project_id": project_id, "run_id": run_id,
                                "error": str(exc), "phase": exc.phase, "details": exc.details}
                            ledger.record(key, {**previous, **item, "stage": "resume_check"})
                        print_json(item); outcomes.append(item); continue
                    if previous_status in {"finished", "existing_run_preserved", "duplicate_prevented"}:
                        item = {"status": "duplicate_prevented", "script": candidate.relative_path,
                            "previous_status": previous_status, "project_id": project_id, "run_id": run_id,
                            "message": "此前已核验或保留该项目；未重复导入或派发。"}
                        print_json(item); outcomes.append(item); continue
                    if not (safe_retry or start_rejection or can_reconcile_import):
                        item = {"status": "manual_check_required", "script": candidate.relative_path,
                            "previous_status": previous_status, "project_id": project_id, "run_id": run_id,
                            "message": "已有未决状态且没有可核对的精确 SHA 导入记录；未重复导入或提交。"}
                        print_json(item); outcomes.append(item); continue
                    if start_rejection and project_id:
                        if not existing_import or existing_import.get("status") != "completed" or existing_import.get("project_id") != project_id:
                            item = {"status": "manual_check_required", "script": candidate.relative_path,
                                "project_id": project_id,
                                "message": "旧启动被明确拒绝，但 Word SHA 对账没有确认此项目归属；未再次启动。"}
                            print_json(item); outcomes.append(item); continue
                if not plan.get("api", {}).get("reachable"):
                    item = {"status": "blocked", "script": candidate.relative_path, "reason": plan.get("api", {}).get("reason", "服务不可达")}
                    ledger.record(key, {**item, "status": "preflight_failed", "stage": "preflight"}); print_json(item); outcomes.append(item)
                    if not continue_on_error: break
                    continue
                if plan.get("blockers"):
                    item = {"status": "blocked", "script": candidate.relative_path, "blockers": plan["blockers"]}
                    ledger.record(key, {**item, "status": "preflight_failed", "stage": "preflight"}); print_json(item); outcomes.append(item)
                    if not continue_on_error: break
                    continue
                current: dict[str, Any] = {"stage": "preflight"}
                def progress(stage: str, details: dict[str, Any], *, _key: str = key,
                             _candidate: Candidate = candidate, _quality: dict[str, str] = quality) -> None:
                    current.update(details); current["stage"] = stage; current["status"] = stage
                    current["script"] = _candidate.relative_path
                    current["script_sha256"] = _candidate.sha256
                    current["image_sha256"] = sorted(asset.sha256 for asset in _candidate.images)
                    current["quality"] = _quality; current["api"] = api_fingerprint(api.base)
                    ledger.record(_key, current)
                try:
                    if existing_import and existing_import.get("status") != "completed":
                        raise WorkflowError("发现相同 Word SHA 的未完成导入记录；保留现场，不重复上传",
                            phase="reconcile", details=existing_import)
                    outcome = submit_candidate(api, candidate, quality=quality, product_name=args.product_name, brand=args.brand,
                        wait=args.wait, poll_seconds=args.poll_seconds, wait_seconds=args.wait_seconds, progress=progress,
                        resolved_identity=identities.get(candidate.sha256), existing_import=existing_import)
                    outcomes.append(outcome)
                    if current.get("stage") == "preflight": progress(outcome.get("status", "finished"), {"project_id": outcome.get("project_id"), "run_id": outcome.get("run_id")})
                    print_json(outcome)
                except WorkflowError as exc:
                    stage = current.get("stage", "preflight")
                    definitive_start_rejection = stage.startswith("start_pending") and (exc.details or {}).get("http_status") in {400, 401, 403, 404, 409, 422}
                    status = "unknown" if exc.outcome_unknown or stage in {"import_pending", "import_response"} or (stage.startswith("start_pending") and not definitive_start_rejection) else ("preflight_failed" if stage == "preflight" else "failed")
                    ledger.record(key, {**current, "status": status, "stage": stage, "script": candidate.relative_path,
                        "error": str(exc), "phase": exc.phase, "details": exc.details, "outcome_unknown": exc.outcome_unknown})
                    item = {"status": status, "script": candidate.relative_path, "phase": exc.phase, "error": str(exc),
                        "details": exc.details, "outcome_unknown": exc.outcome_unknown}
                    outcomes.append(item); print_json(item)
                    if not continue_on_error: break
        return 0 if outcomes and all(x.get("status") in {"started", "finished", "existing_run_preserved", "duplicate_prevented"} for x in outcomes) else 1
    except WorkflowError as exc:
        print(f"失败阶段：{exc.phase or 'unknown'}\n{exc}", file=sys.stderr)
        if exc.details is not None: print_json({"details": exc.details, "outcome_unknown": exc.outcome_unknown})
        if exc.outcome_unknown: print("写操作状态未知；先查询项目/图库再决定是否继续，不要盲目重复提交。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("用户中断；已提交任务状态需在应用中核对，不会自动重提。", file=sys.stderr); return 130

if __name__ == "__main__": raise SystemExit(main())
