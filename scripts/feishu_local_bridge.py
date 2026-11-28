"""Local Windows Bridge Daemon for vast.ai video_2 <-> Feishu Bitable + rea-video (Antigravity / Codex).

Responsibilities:
1. Maintains an SSH tunnel to vast.ai video_2 (127.0.0.1:18767) if not already reachable.
2. Sends periodic heartbeats to video_2 so the web UI shows live bridge status.
3. Claims '一键同步飞书' jobs:
   - Reads Feishu Bitable records & ensures script columns ('脚本1'..'脚本N') exist from col 5+.
   - Downloads reference videos & reference images locally to E:\\feishu_sync\\<product>_<record_id>\\.
   - Uploads downloaded media to vast.ai video_2 (ReferenceVideo + Product library).
   - Runs local rea-video decompose + invokes user-selected local engine (Antigravity or Codex)
     to generate N distinct scripts according to the configured prompt requirements.
   - Writes '脚本1'..'脚本N' back to Feishu Bitable and reports completion to video_2.
4. Claims finished '一键生成视频' batches (once ALL items finish):
   - Auto-downloads all generated videos to E:\\vast视频\\.
   - Auto-archives every video into product-named folders E:\\vast视频\\<产品名称>\\<任务名称>_脚本k.mp4.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# --- Configuration ---
SYNC_DIR_DEFAULT = Path(os.getenv("FEISHU_LOCAL_SYNC_DIR", r"E:\feishu_sync"))
ARCHIVE_DIR_DEFAULT = Path(os.getenv("FEISHU_LOCAL_ARCHIVE_DIR", r"E:\vast视频"))
VIDEO2_API_URL = os.getenv("VIDEO2_API_URL", "http://127.0.0.1:18767").rstrip("/")
SSH_HOST = os.getenv("VAST_SSH_HOST", "root@220.135.0.171")
SSH_PORT = os.getenv("VAST_SSH_PORT", "59748")
SSH_LOCAL_PORT = int(os.getenv("VAST_LOCAL_PORT", "18767"))
SSH_REMOTE_PORT = int(os.getenv("VAST_REMOTE_PORT", "18767"))
ENDPOINT_FILE = SYNC_DIR_DEFAULT / ".antigravity_endpoint.json"
CONFIG_FILE = SYNC_DIR_DEFAULT / "bridge_config.json"

CODEX_CANDIDATES = [
    os.getenv("CODEX_BIN", ""),
    shutil.which("codex") or "",
    r"C:\Users\MSI\.codex\.sandbox-bin\codex.exe",
    r"C:\Users\MSI\.codex\plugins\.plugin-appserver\codex.exe",
]
FFMPEG_DIRS = [
    r"E:\ComfyUI",
    r"D:\Cache\Temp\jellyfish-rea-bin",
    r"D:\codex项目\guoshen_desktop\assets\bin",
]
VENV_SCRIPTS = SYNC_DIR_DEFAULT / ".venv" / "Scripts"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def build_tool_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [str(VENV_SCRIPTS)] + [d for d in FFMPEG_DIRS if Path(d).is_dir()]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def find_codex_exe() -> str | None:
    for cand in CODEX_CANDIDATES:
        if cand and Path(cand).is_file():
            return str(Path(cand))
    return None


def find_rea_video_exe() -> str | None:
    cand = VENV_SCRIPTS / "rea-video.exe"
    if cand.is_file():
        return str(cand)
    return shutil.which("rea-video")


def load_antigravity_endpoint() -> tuple[str, str]:
    addr = os.getenv("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf = os.getenv("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if addr and csrf:
        return addr, csrf
    if ENDPOINT_FILE.is_file():
        try:
            data = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8"))
            addr = str(data.get("address") or "").strip()
            csrf = str(data.get("csrf_token") or "").strip()
            if addr and csrf:
                return addr, csrf
        except Exception:
            pass
    return "", ""


def check_antigravity_reachable() -> bool:
    addr, csrf = load_antigravity_endpoint()
    if not addr or not csrf:
        return False
    try:
        r = httpx.post(
            f"http://{addr}/product.v1.ConversationService/ListConversations",
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-antigravity-csrf-token": csrf,
            },
            json={"pageSize": 1},
            timeout=3.0,
        )
        return r.status_code == 200
    except Exception:
        return False


def safe_name(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (value or "").strip()).strip(". ")
    return cleaned[:100] or fallback


def extract_cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                txt = item.get("text") or item.get("name") or ""
                if txt:
                    parts.append(str(txt))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


def extract_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "file_token": str(item["file_token"]),
            "name": str(item.get("name") or f"{item['file_token']}.bin"),
            "size": int(item.get("size") or 0),
        }
        for item in value
        if isinstance(item, dict) and item.get("file_token")
    ]


def script_column_names(start_column: str, count: int) -> list[str]:
    base = (start_column or "脚本1").strip()
    m = re.match(r"^(.*?)(\d+)$", base)
    if m:
        prefix = m.group(1) or "脚本"
        start_num = max(1, int(m.group(2)))
    else:
        prefix = base or "脚本"
        start_num = 1
    return [f"{prefix}{start_num + i}" for i in range(max(1, min(int(count or 5), 20)))]


def resolve_columns(fields: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, str]:
    names = [str(f.get("field_name") or "").strip() for f in fields if f.get("field_name")]
    name_set = set(names)
    text_names = [str(f.get("field_name") or "").strip() for f in fields if f.get("field_name") and f.get("type") != 17]
    att_names = [str(f.get("field_name") or "").strip() for f in fields if f.get("field_name") and f.get("type") == 17]

    def _pick(key: str, default_name: str, pool: list[str], fallback_idx: int, hints: tuple[str, ...]) -> str:
        c = str(cfg.get(key) or default_name).strip()
        if c and c in name_set and (not pool or c in pool):
            return c
        for n in (pool or names):
            if any(h in n for h in hints):
                return n
        if 0 <= fallback_idx < len(pool):
            return pool[fallback_idx]
        return ""

    return {
        "col_product_name": _pick("col_product_name", "产品名称", text_names, 0, ("产品名称", "产品", "商品")),
        "col_reference_video": _pick("col_reference_video", "参考视频", att_names or names, 0, ("参考视频", "视频")),
        "col_reference_image": _pick("col_reference_image", "参考图", att_names or names, 1, ("参考图", "图片", "产品图")),
        "col_task_name": _pick("col_task_name", "任务名称", text_names, -1, ("任务名称", "任务", "标题")),
        "col_script_start": str(cfg.get("col_script_start") or "脚本1").strip(),
    }


class SSHTunnel:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None

    def ensure(self) -> bool:
        try:
            r = httpx.get(f"{VIDEO2_API_URL}/api/health", timeout=3.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        if self.proc and self.proc.poll() is None:
            time.sleep(1.0)
            return False
        if not VIDEO2_API_URL.startswith(("http://127.0.0.1:", "http://localhost:")):
            return False
        log(f"Starting SSH tunnel to {SSH_HOST}:{SSH_PORT} (localhost:{SSH_LOCAL_PORT} -> 127.0.0.1:{SSH_REMOTE_PORT})")
        cmd = [
            "ssh",
            "-N",
            "-L", f"{SSH_LOCAL_PORT}:127.0.0.1:{SSH_REMOTE_PORT}",
            "-p", str(SSH_PORT),
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            SSH_HOST,
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(0.5)
            try:
                if httpx.get(f"{VIDEO2_API_URL}/api/health", timeout=2.0).status_code == 200:
                    log("SSH tunnel connected to video_2.")
                    return True
            except Exception:
                pass
        return False


def feishu_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def feishu_list_fields(api_base: str, token: str, app_token: str, table_id: str, view_id: str) -> list[dict[str, Any]]:
    url = f"{api_base}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    with httpx.Client(timeout=30.0) as client:
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if view_id:
                params["view_id"] = view_id
            if page_token:
                params["page_token"] = page_token
            r = client.get(url, headers=feishu_headers(token), params=params)
            data = r.json()
            if r.status_code != 200 or data.get("code") != 0:
                raise RuntimeError(f"Feishu list_fields failed: {data}")
            d = data.get("data") or {}
            items.extend(d.get("items") or [])
            if not d.get("has_more"):
                break
            page_token = d.get("page_token")
    return items


def feishu_ensure_script_columns(api_base: str, token: str, app_token: str, table_id: str, view_id: str, start_col: str, count: int) -> list[str]:
    fields = feishu_list_fields(api_base, token, app_token, table_id, view_id)
    existing = {str(f.get("field_name") or "").strip() for f in fields}
    desired = script_column_names(start_col, count)
    url = f"{api_base}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    with httpx.Client(timeout=30.0) as client:
        for col in desired:
            if col not in existing:
                log(f"Creating missing Feishu script column: {col}")
                r = client.post(url, headers=feishu_headers(token), json={"field_name": col, "type": 1})
                data = r.json()
                if r.status_code != 200 or data.get("code") != 0:
                    raise RuntimeError(f"Failed creating Feishu column {col}: {data}")
                existing.add(col)
    return desired


def feishu_list_records(api_base: str, token: str, app_token: str, table_id: str, view_id: str) -> list[dict[str, Any]]:
    url = f"{api_base}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    with httpx.Client(timeout=30.0) as client:
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if view_id:
                params["view_id"] = view_id
            if page_token:
                params["page_token"] = page_token
            r = client.get(url, headers=feishu_headers(token), params=params)
            data = r.json()
            if r.status_code != 200 or data.get("code") != 0:
                raise RuntimeError(f"Feishu list_records failed: {data}")
            d = data.get("data") or {}
            items.extend(d.get("items") or [])
            if not d.get("has_more"):
                break
            page_token = d.get("page_token")
    return items


def feishu_download_media(api_base: str, token: str, file_token: str, dest: Path, expected_size: int = 0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and expected_size > 0 and dest.stat().st_size == expected_size:
        return dest
    url = f"{api_base}/open-apis/drive/v1/medias/{file_token}/download"
    with httpx.Client(timeout=180.0, follow_redirects=True) as client:
        with client.stream("GET", url, headers=feishu_headers(token)) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Feishu media download {file_token} failed: HTTP {resp.status_code}")
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    fh.write(chunk)
    return dest


def feishu_update_record(api_base: str, token: str, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
    url = f"{api_base}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    with httpx.Client(timeout=30.0) as client:
        r = client.put(url, headers=feishu_headers(token), json={"fields": fields})
        data = r.json()
        if r.status_code != 200 or data.get("code") != 0:
            raise RuntimeError(f"Feishu update record {record_id} failed: {data}")


def run_rea_video_decompose(video_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rea_exe = find_rea_video_exe()
    if not rea_exe:
        raise RuntimeError("本机未找到 rea-video 可执行文件，请检查 E:\\feishu_sync\\.venv")
    env = build_tool_env()
    cmd = [rea_exe, "decompose", str(video_path), "-o", str(out_dir)]
    log(f"Running rea-video decompose: {' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        log(f"rea-video decompose stderr: {proc.stderr[-600:]}")
    return out_dir


def build_agent_prompt(
    *,
    video_path: Path,
    image_paths: list[Path],
    rea_out_dir: Path,
    output_json_path: Path,
    product_name: str,
    task_name: str,
    analysis_prompt: str,
    script_count: int,
    script_cols: list[str],
    engine: str = "codex",
) -> str:
    imgs_text = "\n".join(f"- {p}" for p in image_paths) if image_paths else "- (无参考图)"
    user_req = analysis_prompt.strip() or (
        "保留参考视频的镜头节奏、景别与物理交互结构，严格保持参考图中的产品外观一致性，"
        "生成彼此场景、手部肤色/美甲或服装袖口各不相同的带货分镜脚本。"
    )
    if engine == "antigravity":
        delivery_rule = f"""3. **极其重要（零阻塞规则）**：
   - `rea-video decompose` 已提前跑完，结果全部位于 `{rea_out_dir}`（`VIDEO.md`、`video/*.md`、`video/shots/*.md`、`storyboard.jpg`、`shots/shot_*.jpg`），请只使用 `read_file` 查看这些图文资料与参考图即可；
   - **禁止调用 `run_command`，禁止调用 `write_file` 或 `edit_file`（避免触发人工审批弹窗卡住流程）**；
   - 请直接在你的最终回复正文中输出一个严格合法的 ```json ... ``` 代码块（UTF-8，不要在 JSON 内加注释），结构如下：
```json
{{
  "summary": "一句话总结参考视频拆解要点（镜头数、总时长、节奏风格）",
  "scripts": [
    {{"index": 1, "column": "{script_cols[0]}", "text": "完整分镜脚本1文本..."}},
    ...
  ]
}}
```
   - 务必确保 JSON 语法完全合法且 `scripts` 数组恰好包含 {script_count} 条脚本！"""
    else:
        delivery_rule = f"""3. 最终请把严格合法的 UTF-8 JSON 写入文件：`{output_json_path}`，JSON 结构如下（不要包含注释或多余文字）：
{{
  "summary": "一句话总结参考视频拆解要点（镜头数、总时长、节奏风格）",
  "scripts": [
    {{"index": 1, "column": "{script_cols[0]}", "text": "完整分镜脚本1文本..."}},
    ...
  ]
}}
写完 `{output_json_path}` 后请校验该 JSON 可被 Python `json.loads` 正确解析且 `len(scripts) == {script_count}`。"""

    return f"""请使用 rea-video 技能对以下参考视频进行深度逐镜头解析，并严格按用户要求生成 {script_count} 份彼此不同的成品视频分镜脚本。

【素材路径（本机磁盘）】
- 产品名称：{product_name}
- 任务名称：{task_name}
- 参考视频：{video_path}
- 参考图：
{imgs_text}
- rea-video 已预拆解目录（内含 VIDEO.md、video/*.md、storyboard.jpg、shots/shot_*.jpg、breakdown.json）：{rea_out_dir}

【用户分析视频提示词与脚本内容要求】
{user_req}

【输出格式要求（极其重要）】
1. 必须生成 **恰好 {script_count} 份彼此不同的分镜脚本**，分别对应飞书列：{', '.join(script_cols)}。
2. 每份脚本文本必须直接适配 video_2 分镜解析器，采用清晰分段的分镜格式（包含全局设定与 `镜头1（0-5秒）`、`镜头2（5-10秒）`...，每镜明确画面主体、动作轨迹、运镜、光线、产品一致性细节与音效）。
{delivery_rule}
"""


def build_antigravity_batch_prompt(
    *,
    pending_rows: list[dict[str, Any]],
    analysis_prompt: str,
    script_count: int,
    script_cols: list[str],
    job_id: str,
) -> str:
    user_req = analysis_prompt.strip() or (
        "保留参考视频的镜头节奏、景别与物理交互结构，严格保持参考图中的产品外观一致性，"
        "生成彼此场景、手部肤色/美甲或服装袖口各不相同的带货分镜脚本。"
    )
    items_blocks: list[str] = []
    for idx, row in enumerate(pending_rows, start=1):
        rid = row["rid"]
        imgs_text = "\n".join(f"  - {p}" for p in row["image_locals"]) if row["image_locals"] else "  - (无参考图)"
        items_blocks.append(
            f"### 第 {idx}/{len(pending_rows)} 条记录（record_id: `{rid}`）\n"
            f"- 产品名称：{row['product_name']}\n"
            f"- 任务名称：{row['task_name']}\n"
            f"- 参考视频：{row['video_local']}\n"
            f"- 参考图：\n{imgs_text}\n"
            f"- rea-video 已预拆解目录：{row['rea_out']}\n"
            f"- 本条输出文件路径（位于你当前对话的 `<Artifact Directory Path>/scratch/` 内）：`scripts_{rid}.json`"
        )
    tasks_section = "\n\n".join(items_blocks)
    single_note = (
        "\n   - （本批次仅 1 条记录，你也可以直接将严格合法的 ```json ... ``` 代码块输出在最终回复正文中。）"
        if len(pending_rows) == 1
        else ""
    )
    return f"""请使用 rea-video 技能，在**当前这一个固定对话内**按顺序逐条解析本批次全部 {len(pending_rows)} 条飞书参考视频记录（批次 `{job_id}`），并为**每一条记录**生成恰好 {script_count} 份彼此不同的成品视频分镜脚本。

【用户分析视频提示词与脚本内容要求（全部记录均须严格遵守）】
{user_req}

【待解析记录列表（共 {len(pending_rows)} 条，请严格按 1 -> {len(pending_rows)} 顺序逐条处理）】
{tasks_section}

【输出格式与零阻塞执行规则（极其重要）】
1. **严格逐条执行（固定单会话）**：不要新建其他对话，不要把多条记录合并进同一个 JSON。每条记录必须生成 **恰好 {script_count} 份彼此不同的分镜脚本**，分别对应飞书列：{', '.join(script_cols)}。
2. 每份脚本文本必须直接适配 video_2 分镜解析器，采用清晰分段的分镜格式（包含全局设定与 `镜头1（0-5秒）`、`镜头2（5-10秒）`...，每镜明确画面主体、动作轨迹、运镜、光线、产品一致性细节与音效）。
3. **零审批阻塞规则**：
   - 每条记录的 `rea-video decompose` 已提前跑完，结果位于各自的 `rea_out` 目录（`VIDEO.md`、`video/*.md`、`video/shots/*.md`、`storyboard.jpg`、`shots/shot_*.jpg`），请**只使用 `read_file`** 查看这些资料与参考图；
   - **禁止调用 `run_command`**；
   - **禁止向 `<Artifact Directory Path>/scratch/` 以外的任何路径调用 `write_file` / `edit_file`**（写外部路径会触发人工审批弹窗卡住流程！）；
   - 每完成第 `i` 条记录的分析后，**立即**调用 `write_file`（`Overwrite=true`，**不要提供 `ArtifactMetadata`**）把严格合法的 UTF-8 JSON 写入你当前对话目录的 `<Artifact Directory Path>/scratch/scripts_<record_id>.json`（注意把 `<Artifact Directory Path>` 替换为你本会话真实的 Artifact Directory Path，例如 `C:\\Users\\MSI\\.gemini\\antigravity\\brain\\<你的Conversation ID>\\scratch\\scripts_<record_id>.json`），JSON 结构如下：
```json
{{
  "record_id": "<该条记录的 record_id>",
  "summary": "一句话总结参考视频拆解要点（镜头数、总时长、节奏风格）",
  "scripts": [
    {{"index": 1, "column": "{script_cols[0]}", "text": "完整分镜脚本1文本..."}},
    ...
  ]
}}
```
   - 写入完第 `i` 条的 `scripts_<record_id>.json` 后，立刻在下一轮继续解析第 `i+1` 条，直至全部 {len(pending_rows)} 条均落盘完成。{single_note}
"""


def _extract_all_json_dicts_from_text(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    found: list[dict[str, Any]] = []
    # 1. Try fenced ```json ... ``` blocks
    for block in re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                found.append(obj)
        except Exception:
            continue
    if found:
        return found
    # 2. Try direct parse
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                return [obj]
        except Exception:
            pass
    # 3. Try greedy outer braces
    m = re.search(r"\{[\s\S]*\"scripts\"[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                return [obj]
        except Exception:
            pass
    return []


def _extract_json_dict_from_text(text: str) -> dict[str, Any] | None:
    items = _extract_all_json_dicts_from_text(text)
    return items[-1] if items else None


def _extract_batch_scripts_from_antigravity(
    conv_id: str,
    remaining_rids: set[str],
    total_pending: int,
) -> dict[str, dict[str, Any]]:
    """Collect any newly ready per-record script JSONs from scratch files OR transcript logs."""
    results: dict[str, dict[str, Any]] = {}
    if not conv_id or not remaining_rids:
        return results
    brain_root = Path.home() / ".gemini" / "antigravity" / "brain" / conv_id

    # 1. Primary source: zero-approval artifact scratch files written by the conversation
    for rid in list(remaining_rids):
        for cand in (
            brain_root / "scratch" / f"scripts_{rid}.json",
            brain_root / f"scripts_{rid}.json",
        ):
            if cand.is_file() and cand.stat().st_size > 20:
                try:
                    obj = json.loads(cand.read_text(encoding="utf-8-sig"))
                    if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                        results[rid] = obj
                        break
                except Exception:
                    pass

    still_missing = remaining_rids - set(results.keys())
    if not still_missing:
        return results

    # 2. Fallback source: transcript_full.jsonl / transcript.jsonl (write_file Content OR assistant text)
    logs_dir = brain_root / ".system_generated" / "logs"
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        tpath = logs_dir / fname
        if not tpath.is_file():
            continue
        try:
            lines = tpath.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "PLANNER_RESPONSE":
                continue

            # A. Check write_file tool_calls
            for tc in entry.get("tool_calls") or []:
                if tc.get("name") != "write_file":
                    continue
                args = tc.get("args") or {}
                raw_path = str(args.get("Path") or "")
                raw_content = args.get("Content")
                if not isinstance(raw_content, str):
                    continue
                try:
                    unwrapped = json.loads(raw_content) if raw_content.startswith('"') else raw_content
                except Exception:
                    unwrapped = raw_content
                if not isinstance(unwrapped, str):
                    continue
                for obj in _extract_all_json_dicts_from_text(unwrapped):
                    matched_rid = str(obj.get("record_id") or "").strip()
                    if not matched_rid:
                        for cand_rid in still_missing:
                            if cand_rid in raw_path:
                                matched_rid = cand_rid
                                break
                    if not matched_rid and total_pending == 1 and len(still_missing) == 1:
                        matched_rid = next(iter(still_missing))
                    if matched_rid in still_missing and matched_rid not in results:
                        results[matched_rid] = obj

            # B. Check assistant text response / content
            for field in ("response", "content", "text"):
                val = entry.get(field)
                if not isinstance(val, str):
                    continue
                for obj in _extract_all_json_dicts_from_text(val):
                    matched_rid = str(obj.get("record_id") or "").strip()
                    if not matched_rid and total_pending == 1 and len(still_missing) == 1:
                        matched_rid = next(iter(still_missing))
                    if matched_rid in still_missing and matched_rid not in results:
                        results[matched_rid] = obj

            still_missing = remaining_rids - set(results.keys())
            if not still_missing:
                return results
    return results


def _extract_scripts_from_antigravity_transcript(conv_id: str) -> dict[str, Any] | None:
    res = _extract_batch_scripts_from_antigravity(conv_id, {"__single__"}, total_pending=1)
    return res.get("__single__")


def _parse_codex_jsonl_stdout(stdout: str) -> tuple[str | None, str]:
    thread_id: str | None = None
    last_agent_text = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            thread_id = str(ev["thread_id"]).strip()
        elif not thread_id and ev.get("session_id"):
            thread_id = str(ev["session_id"]).strip()
        if ev.get("type") == "item.completed":
            item = ev.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                last_agent_text = item["text"]
    return thread_id, last_agent_text


def parse_scripts_output(output_json_path: Path, fallback_text: str, script_cols: list[str], raw_obj: dict[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]]:
    if raw_obj is None:
        if output_json_path.is_file():
            raw_obj = json.loads(output_json_path.read_text(encoding="utf-8-sig"))
        elif fallback_text:
            raw_obj = _extract_json_dict_from_text(fallback_text)
    if not isinstance(raw_obj, dict) or not isinstance(raw_obj.get("scripts"), list):
        raise RuntimeError(f"Agent 未输出合法的 scripts_out.json ({output_json_path})")
    summary = str(raw_obj.get("summary") or "")
    out: list[dict[str, Any]] = []
    for i, col in enumerate(script_cols, start=1):
        entry = raw_obj["scripts"][i - 1] if i - 1 < len(raw_obj["scripts"]) else None
        if isinstance(entry, dict):
            txt = str(entry.get("text") or "").strip()
        elif isinstance(entry, str):
            txt = entry.strip()
        else:
            txt = ""
        if txt:
            out.append({"index": i, "column": col, "text": txt})
    if not out:
        raise RuntimeError("生成的脚本列表为空")
    # Always persist canonical normalized JSON to output_json_path
    output_json_path.write_text(
        json.dumps({"summary": summary, "scripts": out}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary, out


def run_codex_engine(
    prompt: str,
    work_dir: Path,
    output_json_path: Path,
    script_cols: list[str],
    *,
    session_root: Path | None = None,
    thread_id: str | None = None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    codex_exe = find_codex_exe()
    if not codex_exe:
        raise RuntimeError("未在本机找到 codex.exe，请先安装 Codex 或切换为 Antigravity 引擎")
    last_msg = work_dir / "codex_last_message.txt"
    if output_json_path.exists():
        output_json_path.unlink()
    if last_msg.exists():
        last_msg.unlink()
    cwd_root = session_root or work_dir
    if thread_id:
        cmd = [
            codex_exe,
            "exec",
            "resume",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "-o", str(last_msg),
            thread_id,
            "-",
        ]
        log(f"Resuming fixed batch Codex conversation {thread_id} for {work_dir.name}")
    else:
        cmd = [
            codex_exe,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "-C", str(cwd_root),
            "-o", str(last_msg),
            "-",
        ]
        log(f"Starting fixed batch Codex conversation in {cwd_root} for {work_dir.name}")
    proc = subprocess.run(
        cmd,
        input=prompt,
        env=build_tool_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    parsed_tid, agent_msg = _parse_codex_jsonl_stdout(proc.stdout or "")
    effective_tid = parsed_tid or thread_id
    fallback = last_msg.read_text(encoding="utf-8", errors="replace") if last_msg.is_file() else (agent_msg or proc.stdout or "")
    if proc.returncode != 0 and not output_json_path.is_file():
        raise RuntimeError(f"Codex 执行失败 (code={proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}")
    summary, scripts = parse_scripts_output(output_json_path, fallback, script_cols)
    return summary, scripts, effective_tid


def run_antigravity_batch_engine(
    *,
    job_id: str,
    pending_rows: list[dict[str, Any]],
    analysis_prompt: str,
    script_count: int,
    script_cols: list[str],
    on_row_ready: Any,
) -> str:
    """Use ONE fixed Antigravity conversation for all pending rows in a sync batch."""
    addr, csrf = load_antigravity_endpoint()
    if not addr or not csrf:
        raise RuntimeError(
            "未检测到本地 Antigravity ConversationService 端点。请确保 Antigravity 客户端已启动，"
            f"并已写入端点配置 {ENDPOINT_FILE}（或由 Antigravity 环境启动桥接）。"
        )
    for row in pending_rows:
        if row["scripts_out_path"].exists():
            row["scripts_out_path"].unlink()

    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-antigravity-csrf-token": csrf,
    }
    prompt = build_antigravity_batch_prompt(
        pending_rows=pending_rows,
        analysis_prompt=analysis_prompt,
        script_count=script_count,
        script_cols=script_cols,
        job_id=job_id,
    )
    title = f"飞书批量脚本生成 · {job_id[-8:]}（{len(pending_rows)}条）"[:80]
    log(f"Creating SINGLE fixed Antigravity batch conversation via http://{addr} for {len(pending_rows)} rows ({job_id})")
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"http://{addr}/product.v1.ConversationService/CreateConversation",
            headers=headers,
            json={
                "title": title,
                "tags": ["feishu-sync", "rea-video", f"batch-{job_id[-8:]}"],
                "prompt": [{"text": prompt}],
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"调用 Antigravity CreateConversation 失败: HTTP {resp.status_code} {resp.text[:300]}")
        conv_id = ((resp.json().get("conversation") or {}).get("id")) or ""
    log(f"Fixed batch Antigravity conversation started: {conv_id} for {len(pending_rows)} rows")
    post_progress(
        job_id,
        status="analyzing",
        current_stage=f"已在固定 Antigravity 会话（{conv_id[:8]}）中连续解析 {len(pending_rows)} 条记录…",
        log=f"本批次固定使用单个 Antigravity 会话：{conv_id}（共 {len(pending_rows)} 条待解析）",
    )

    row_by_rid = {row["rid"]: row for row in pending_rows}
    remaining = set(row_by_rid.keys())
    total_pending = len(pending_rows)
    deadline = time.time() + max(900, 600 * total_pending)

    def _collect_and_flush() -> None:
        if not remaining:
            return
        # Check direct scripts_out.json first
        for rid in list(remaining):
            row = row_by_rid[rid]
            out_p = row["scripts_out_path"]
            if out_p.is_file() and out_p.stat().st_size > 20:
                try:
                    summary, scripts = parse_scripts_output(out_p, "", script_cols)
                    if len(scripts) >= script_count:
                        log(f"Detected scripts_out.json for {rid} ({len(scripts)} scripts) in fixed conv {conv_id}")
                        on_row_ready(row, summary, scripts)
                        remaining.discard(rid)
                except Exception:
                    pass
        if not remaining:
            return
        extracted_map = _extract_batch_scripts_from_antigravity(conv_id, remaining, total_pending)
        for rid, raw_obj in extracted_map.items():
            if rid not in remaining:
                continue
            row = row_by_rid[rid]
            try:
                summary, scripts = parse_scripts_output(row["scripts_out_path"], "", script_cols, raw_obj=raw_obj)
                if len(scripts) >= script_count:
                    log(f"Extracted {len(scripts)} scripts for {rid} ({row['product_name']}) from fixed Antigravity conv {conv_id}!")
                    on_row_ready(row, summary, scripts)
                    remaining.discard(rid)
            except Exception as exc:
                log(f"Waiting for complete JSON on {rid}: {exc}")

    while time.time() < deadline and remaining:
        _collect_and_flush()
        if not remaining:
            return conv_id
        if conv_id:
            try:
                st = httpx.post(
                    f"http://{addr}/product.v1.ConversationService/GetConversation",
                    headers=headers,
                    json={"conversationId": conv_id},
                    timeout=10.0,
                )
                if st.status_code == 200:
                    run_state = ((st.json().get("conversation") or {}).get("state") or {}).get("runState")
                    if run_state == "RUN_STATE_IDLE":
                        time.sleep(3)
                        _collect_and_flush()
                        if not remaining:
                            return conv_id
                        missing_list = ", ".join(sorted(remaining))
                        raise RuntimeError(f"Antigravity 固定批次对话 {conv_id} 已结束，但仍有未产出完整脚本的记录：{missing_list}")
            except RuntimeError:
                raise
            except Exception:
                pass
        time.sleep(4)
    if remaining:
        missing_list = ", ".join(sorted(remaining))
        raise RuntimeError(f"等待 Antigravity 固定批次对话 ({conv_id}) 生成脚本超时，未完成记录：{missing_list}")
    return conv_id


def run_antigravity_engine(prompt: str, work_dir: Path, output_json_path: Path, script_cols: list[str], title: str) -> tuple[str, list[dict[str, Any]]]:
    """Single-record backwards-compatible wrapper."""
    result_box: list[tuple[str, list[dict[str, Any]]]] = []
    row = {
        "rid": work_dir.name,
        "product_name": title,
        "task_name": title,
        "video_local": work_dir / "reference_video.mp4",
        "image_locals": [],
        "rea_out": work_dir / "rea_out",
        "scripts_out_path": output_json_path,
    }
    run_antigravity_batch_engine(
        job_id=work_dir.name,
        pending_rows=[row],
        analysis_prompt=prompt,
        script_count=len(script_cols),
        script_cols=script_cols,
        on_row_ready=lambda _r, s, sc: result_box.append((s, sc)),
    )
    return result_box[0]


_TUNNEL = SSHTunnel()


def video2_post(
    path: str,
    *,
    json_body: Any = None,
    data: dict[str, Any] | None = None,
    file_specs: list[tuple[str, Path, str]] | None = None,
    timeout: float = 60.0,
    retries: int = 4,
) -> httpx.Response:
    url = f"{VIDEO2_API_URL}{path}"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        opened_handles = []
        try:
            multipart_files = None
            if file_specs is not None:
                multipart_files = []
                for field_name, file_path, mime in file_specs:
                    fh = file_path.open("rb")
                    opened_handles.append(fh)
                    multipart_files.append((field_name, (file_path.name, fh, mime)))
            return httpx.post(url, json=json_body, data=data, files=multipart_files, timeout=timeout)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError, httpx.TimeoutException) as exc:
            last_exc = exc
            log(f"video_2 POST {path} attempt {attempt}/{retries} hit {type(exc).__name__}; ensuring SSH tunnel...")
            _TUNNEL.ensure()
            time.sleep(min(2 * attempt, 6))
        finally:
            for fh in opened_handles:
                try:
                    fh.close()
                except Exception:
                    pass
    raise RuntimeError(f"连接 video_2 失败 ({path}): {last_exc}")


def post_progress(job_id: str, **kwargs: Any) -> None:
    try:
        video2_post(f"/api/feishu/bridge/sync/{job_id}/progress", json_body=kwargs, timeout=15.0, retries=3)
    except Exception as exc:
        log(f"Progress update warning: {exc}")


def process_sync_job(claimed: dict[str, Any]) -> None:
    job = claimed["job"]
    job_id = job["id"]
    cfg = job["config"]
    token = claimed["tenant_access_token"]
    api_base = claimed.get("api_base_url") or "https://open.feishu.cn"
    app_token = cfg["app_token"]
    table_id = cfg["table_id"]
    view_id = cfg.get("view_id") or ""
    engine = (job.get("engine") or cfg.get("analysis_engine") or "codex").lower()
    script_count = int(cfg.get("script_count") or 5)
    analysis_prompt = str(cfg.get("analysis_prompt") or "")
    force_rerun = bool(job.get("force_rerun"))
    sync_root = Path(cfg.get("local_sync_dir") or str(SYNC_DIR_DEFAULT))
    sync_root.mkdir(parents=True, exist_ok=True)

    try:
        post_progress(job_id, status="downloading", current_stage="正在读取飞书多维表格字段并校验脚本列…", log="连接飞书多维表格，检查列结构")
        fields = feishu_list_fields(api_base, token, app_token, table_id, view_id)
        cols = resolve_columns(fields, cfg)
        script_cols = feishu_ensure_script_columns(api_base, token, app_token, table_id, view_id, cols["col_script_start"], script_count)
        records = feishu_list_records(api_base, token, app_token, table_id, view_id)
        post_progress(
            job_id,
            status="downloading",
            total_records=len(records),
            processed_records=0,
            current_stage=f"共读取 {len(records)} 条飞书记录，脚本列：{', '.join(script_cols)}",
            log=f"已确认脚本列 {', '.join(script_cols)}；共 {len(records)} 行记录",
        )

        processed = 0
        pending_rows: list[dict[str, Any]] = []

        def _writeback_row(row: dict[str, Any], summary: str, scripts: list[dict[str, Any]], engine_used: str | None = None) -> None:
            nonlocal processed
            rid_w = row["rid"]
            prod_w = row["product_name"]
            task_w = row["task_name"]
            post_progress(
                job_id,
                status="writing_feishu",
                current_record_id=rid_w,
                current_stage=f"回写 {len(scripts)} 份脚本到飞书「{prod_w}」…",
                log=f"正在回写飞书列（{rid_w}）：{', '.join(s['column'] for s in scripts)}",
            )
            update_fields = {s["column"]: s["text"] for s in scripts}
            feishu_update_record(api_base, token, app_token, table_id, rid_w, update_fields)
            video2_post(
                f"/api/feishu/bridge/sync/{job_id}/record-complete",
                json_body={
                    "feishu_record_id": rid_w,
                    "product_name": prod_w,
                    "task_name": task_w,
                    "scripts": scripts,
                    "analysis_summary": summary,
                    "engine_used": engine_used or engine,
                    "sync_status": "written_back",
                },
                timeout=30.0,
                retries=4,
            )
            processed += 1
            post_progress(job_id, processed_records=processed, log=f"已完成行 {rid_w}（{prod_w}），{len(scripts)} 份脚本已回写飞书")

        # Phase 1: Download & upload all media, reuse finished rows, pre-run rea-video decompose
        for rec in records:
            rid = str(rec.get("record_id") or "")
            fdict = rec.get("fields") or {}
            product_name = extract_cell_text(fdict.get(cols["col_product_name"])) or f"产品_{rid[-6:]}"
            task_name = extract_cell_text(fdict.get(cols["col_task_name"])) or product_name
            video_atts = extract_attachments(fdict.get(cols["col_reference_video"]))
            image_atts = extract_attachments(fdict.get(cols["col_reference_image"]))

            if not video_atts and not image_atts:
                processed += 1
                post_progress(job_id, processed_records=processed, log=f"跳过空附件行 {rid} ({product_name})")
                continue

            rec_dir = sync_root / f"{safe_name(product_name, 'product')}_{rid}"
            rec_dir.mkdir(parents=True, exist_ok=True)

            # 1. Download video & images to E:\feishu_sync\...
            post_progress(
                job_id,
                status="downloading",
                current_record_id=rid,
                current_stage=f"下载「{product_name} / {task_name}」素材至 {rec_dir}",
                log=f"下载行 {rid}（{product_name}）：视频 {len(video_atts)} 个，参考图 {len(image_atts)} 张",
            )
            video_local: Path | None = None
            if video_atts:
                att = video_atts[0]
                ext = Path(att["name"]).suffix or ".mp4"
                video_local = feishu_download_media(api_base, token, att["file_token"], rec_dir / f"reference_video{ext}", expected_size=att.get("size", 0))

            image_locals: list[Path] = []
            for idx, att in enumerate(image_atts, start=1):
                ext = Path(att["name"]).suffix or ".png"
                img_path = feishu_download_media(api_base, token, att["file_token"], rec_dir / f"ref_img_{idx:02d}{ext}", expected_size=att.get("size", 0))
                image_locals.append(img_path)

            # 2. Upload media to vast.ai video_2 so one-click video generation can use them immediately
            file_specs: list[tuple[str, Path, str]] = []
            if video_local and video_local.is_file():
                file_specs.append(("video", video_local, "video/mp4"))
            for img_p in image_locals:
                file_specs.append(("images", img_p, "application/octet-stream"))
            up_resp = video2_post(
                f"/api/feishu/bridge/sync/{job_id}/upload-media",
                data={
                    "feishu_record_id": rid,
                    "product_name": product_name,
                    "task_name": task_name,
                    "local_video_path": str(video_local) if video_local else "",
                    "local_image_paths_json": json.dumps([str(p) for p in image_locals], ensure_ascii=False),
                },
                file_specs=file_specs,
                timeout=300.0,
                retries=4,
            )
            if up_resp.status_code != 200:
                raise RuntimeError(f"上传素材到 video_2 失败: HTTP {up_resp.status_code} {up_resp.text[:300]}")

            # 3. Check existing scripts in Feishu
            existing_scripts = []
            for idx, col in enumerate(script_cols, start=1):
                txt = extract_cell_text(fdict.get(col))
                if txt:
                    existing_scripts.append({"index": idx, "column": col, "text": txt})

            if len(existing_scripts) >= script_count and not force_rerun:
                video2_post(
                    f"/api/feishu/bridge/sync/{job_id}/record-complete",
                    json_body={
                        "feishu_record_id": rid,
                        "product_name": product_name,
                        "task_name": task_name,
                        "scripts": existing_scripts,
                        "engine_used": "existing",
                        "sync_status": "written_back",
                    },
                    timeout=30.0,
                    retries=4,
                )
                processed += 1
                post_progress(job_id, processed_records=processed, log=f"行 {rid}（{product_name}）飞书已有 {len(existing_scripts)} 份脚本，已复用")
                continue

            if not video_local:
                processed += 1
                post_progress(job_id, processed_records=processed, log=f"行 {rid}（{product_name}）缺少参考视频，跳过拆解")
                continue

            # 4. Reuse cached local scripts_out.json if complete (unless force_rerun)
            scripts_out_path = rec_dir / "scripts_out.json"
            row_info = {
                "rid": rid,
                "product_name": product_name,
                "task_name": task_name,
                "rec_dir": rec_dir,
                "video_local": video_local,
                "image_locals": image_locals,
                "rea_out": rec_dir / "rea_out",
                "scripts_out_path": scripts_out_path,
            }
            if scripts_out_path.is_file() and not force_rerun:
                try:
                    summary, scripts = parse_scripts_output(scripts_out_path, "", script_cols)
                    if len(scripts) >= script_count:
                        log(f"Reusing cached local {scripts_out_path} ({len(scripts)} scripts) for {rid}")
                        _writeback_row(row_info, summary, scripts)
                        continue
                except Exception:
                    pass

            # Pre-run rea-video decompose so the single fixed conversation has all assets ready
            post_progress(
                job_id,
                status="analyzing",
                current_record_id=rid,
                current_stage=f"正在预拆解「{product_name}」参考视频（rea-video decompose）…",
                log=f"启动 rea-video decompose ({rid} / {video_local.name})",
            )
            rea_out = row_info["rea_out"]
            if not (rea_out / "breakdown.json").is_file() or force_rerun:
                run_rea_video_decompose(video_local, rea_out)
            pending_rows.append(row_info)

        # Phase 2: Generate scripts for ALL pending rows using ONE fixed conversation for this batch
        if pending_rows:
            if engine == "antigravity":
                run_antigravity_batch_engine(
                    job_id=job_id,
                    pending_rows=pending_rows,
                    analysis_prompt=analysis_prompt,
                    script_count=script_count,
                    script_cols=script_cols,
                    on_row_ready=lambda row, summary, scripts: _writeback_row(row, summary, scripts, engine_used="antigravity"),
                )
            else:
                batch_codex_thread_id: str | None = None
                for idx, row in enumerate(pending_rows, start=1):
                    rid = row["rid"]
                    product_name = row["product_name"]
                    task_name = row["task_name"]
                    stage_note = (
                        f"复用固定 Codex 会话（{batch_codex_thread_id[:8]}）解析第 {idx}/{len(pending_rows)} 条「{product_name}」…"
                        if batch_codex_thread_id
                        else f"启动固定 Codex 会话解析第 {idx}/{len(pending_rows)} 条「{product_name}」…"
                    )
                    post_progress(
                        job_id,
                        status="analyzing",
                        current_record_id=rid,
                        current_stage=stage_note,
                        log=stage_note,
                    )
                    prompt = build_agent_prompt(
                        video_path=row["video_local"],
                        image_paths=row["image_locals"],
                        rea_out_dir=row["rea_out"],
                        output_json_path=row["scripts_out_path"],
                        product_name=product_name,
                        task_name=task_name,
                        analysis_prompt=analysis_prompt,
                        script_count=script_count,
                        script_cols=script_cols,
                        engine="codex",
                    )
                    summary, scripts, new_tid = run_codex_engine(
                        prompt,
                        row["rec_dir"],
                        row["scripts_out_path"],
                        script_cols,
                        session_root=sync_root,
                        thread_id=batch_codex_thread_id,
                    )
                    if new_tid and not batch_codex_thread_id:
                        batch_codex_thread_id = new_tid
                        post_progress(
                            job_id,
                            log=f"本批次固定使用单个 Codex 会话：{batch_codex_thread_id}（共 {len(pending_rows)} 条待解析）",
                        )
                    _writeback_row(row, summary, scripts, engine_used="codex")

        video2_post(
            f"/api/feishu/bridge/sync/{job_id}/complete",
            json_body={"ok": True, "processed_records": processed, "total_records": len(records), "log": f"同步全部完成，共处理 {processed} 条记录"},
            timeout=30.0,
            retries=4,
        )
    except Exception as exc:
        log(f"Sync job {job_id} error: {exc}")
        try:
            video2_post(
                f"/api/feishu/bridge/sync/{job_id}/complete",
                json_body={"ok": False, "error_message": str(exc)[:1000], "log": f"错误：{exc}"},
                timeout=30.0,
                retries=4,
            )
        except Exception as report_exc:
            log(f"Failed reporting sync error for {job_id}: {report_exc}")


def process_download_batch(manifest: dict[str, Any]) -> None:
    batch_id = manifest["batch_id"]
    archive_root = Path(manifest.get("archive_dir") or str(ARCHIVE_DIR_DEFAULT))
    archive_root.mkdir(parents=True, exist_ok=True)
    items = manifest.get("items") or []
    if not items:
        return
    log(f"Auto-downloading {len(items)} completed video(s) for batch {batch_id} -> {archive_root}")

    archived_paths: dict[str, str] = {}
    errors: list[str] = []
    try:
        for item in items:
            item_id = item["item_id"]
            product_folder = safe_name(item.get("product_folder") or item.get("product_name") or "未命名产品", "未命名产品")
            filename = safe_name(item.get("filename") or f"{item_id}.mp4", f"{item_id}.mp4")
            if not Path(filename).suffix:
                filename += ".mp4"
            url = f"{VIDEO2_API_URL}{item['download_url']}"

            target_dir = archive_root / product_folder
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp_download = target_dir / f".downloading_{item_id}_{filename}"

            downloaded_ok = False
            last_err = ""
            for attempt in range(1, 4):
                try:
                    _TUNNEL.ensure()
                    with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0), follow_redirects=True) as client:
                        with client.stream("GET", url) as resp:
                            if resp.status_code != 200:
                                last_err = f"HTTP {resp.status_code}"
                                if resp.status_code < 500:
                                    break
                                time.sleep(2 * attempt)
                                continue
                            expected_len = int(resp.headers.get("content-length") or 0)
                            written = 0
                            with tmp_download.open("wb") as fh:
                                for chunk in resp.iter_bytes(262144):
                                    if chunk:
                                        fh.write(chunk)
                                        written += len(chunk)
                            if written <= 0 or (expected_len > 0 and written < expected_len):
                                last_err = f"incomplete download ({written}/{expected_len} bytes)"
                                tmp_download.unlink(missing_ok=True)
                                time.sleep(2 * attempt)
                                continue
                            downloaded_ok = True
                            break
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    tmp_download.unlink(missing_ok=True)
                    log(f"Download {item_id} attempt {attempt}/3 failed: {last_err}")
                    _TUNNEL.ensure()
                    time.sleep(min(3 * attempt, 9))

            if not downloaded_ok:
                errors.append(f"{item_id}: {last_err}")
                continue

            # Move atomically into E:\vast视频\<产品名称>\<filename>
            target_path = target_dir / filename
            if target_path.exists():
                if target_path.stat().st_size == tmp_download.stat().st_size:
                    tmp_download.unlink(missing_ok=True)
                else:
                    stem = target_path.stem
                    suffix = target_path.suffix
                    counter = 2
                    while target_path.exists():
                        target_path = target_dir / f"{stem}_{counter}{suffix}"
                        counter += 1
                    shutil.move(str(tmp_download), str(target_path))
            else:
                shutil.move(str(tmp_download), str(target_path))

            archived_paths[item_id] = str(target_path)
            size_mb = target_path.stat().st_size / (1024 * 1024)
            log(f"Archived -> {target_path} ({size_mb:.1f} MB)")

            # Report each completed item immediately so DB & UI update in real time
            try:
                video2_post(
                    f"/api/feishu/bridge/download/{batch_id}/complete",
                    json_body={
                        "ok": True,
                        "partial": True,
                        "archive_dir": str(archive_root),
                        "archived_count": 1,
                        "archived_paths": {item_id: str(target_path)},
                    },
                    timeout=30.0,
                    retries=4,
                )
            except Exception as report_exc:
                log(f"Per-item archive report warning for {item_id}: {report_exc}")

        video2_post(
            f"/api/feishu/bridge/download/{batch_id}/complete",
            json_body={
                "ok": len(errors) == 0 and len(archived_paths) > 0,
                "partial": False,
                "archive_dir": str(archive_root),
                "archived_count": len(archived_paths),
                "archived_paths": archived_paths,
                "error_message": "; ".join(errors) if errors else None,
            },
            timeout=30.0,
            retries=4,
        )
    except Exception as exc:
        log(f"Batch download {batch_id} failed: {exc}")
        try:
            video2_post(
                f"/api/feishu/bridge/download/{batch_id}/complete",
                json_body={"ok": False, "partial": False, "archived_paths": archived_paths, "error_message": str(exc)[:1000]},
                timeout=30.0,
                retries=4,
            )
        except Exception as report_exc:
            log(f"Failed reporting download error for {batch_id}: {report_exc}")


def send_heartbeat() -> None:
    engines = []
    if check_antigravity_reachable():
        engines.append("antigravity")
    if find_codex_exe():
        engines.append("codex")
    payload = {
        "hostname": socket.gethostname(),
        "sync_dir": str(SYNC_DIR_DEFAULT),
        "archive_dir": str(ARCHIVE_DIR_DEFAULT),
        "available_engines": engines,
        "rea_video_ready": bool(find_rea_video_exe()),
        "meta": {"codex_exe": find_codex_exe() or "", "rea_video_exe": find_rea_video_exe() or ""},
    }
    httpx.post(f"{VIDEO2_API_URL}/api/feishu/bridge/heartbeat", json=payload, timeout=5.0)


def start_heartbeat_thread() -> None:
    def _loop() -> None:
        while True:
            try:
                send_heartbeat()
            except Exception:
                pass
            time.sleep(15)

    threading.Thread(target=_loop, name="feishu-bridge-heartbeat", daemon=True).start()


def main() -> None:
    SYNC_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
    tunnel = _TUNNEL
    log(f"video2_bridge starting | api={VIDEO2_API_URL} | sync_dir={SYNC_DIR_DEFAULT} | archive_dir={ARCHIVE_DIR_DEFAULT}")
    log(f"Detected tools: codex={find_codex_exe()} | rea-video={find_rea_video_exe()} | antigravity_ready={check_antigravity_reachable()}")
    start_heartbeat_thread()

    while True:
        try:
            if not tunnel.ensure():
                time.sleep(5)
                continue

            # 1. Claim Feishu sync jobs
            r_sync = httpx.post(f"{VIDEO2_API_URL}/api/feishu/bridge/claim-sync", timeout=15.0)
            if r_sync.status_code == 200 and (r_sync.json().get("claimed")):
                process_sync_job(r_sync.json()["claimed"])

            # 2. Claim finished batch video downloads for E:\vast视频\<产品名称>\
            r_dl = httpx.post(f"{VIDEO2_API_URL}/api/feishu/bridge/claim-download", timeout=15.0)
            if r_dl.status_code == 200 and (r_dl.json().get("claimed")):
                process_download_batch(r_dl.json()["claimed"])
        except KeyboardInterrupt:
            log("Bridge stopped by user.")
            break
        except Exception as exc:
            log(f"Loop warning: {exc}")
        time.sleep(3)


if __name__ == "__main__":
    main()
