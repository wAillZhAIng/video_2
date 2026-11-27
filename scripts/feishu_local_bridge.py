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


def feishu_download_media(api_base: str, token: str, file_token: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
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


def _extract_json_dict_from_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    # 1. Try fenced ```json ... ``` blocks first (prefer last one)
    for block in reversed(re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                return obj
        except Exception:
            continue
    # 2. Try direct parse
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                return obj
        except Exception:
            pass
    # 3. Try greedy outer braces
    m = re.search(r"\{[\s\S]*\"scripts\"[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and isinstance(obj.get("scripts"), list) and obj["scripts"]:
                return obj
        except Exception:
            pass
    return None


def _extract_scripts_from_antigravity_transcript(conv_id: str) -> dict[str, Any] | None:
    if not conv_id:
        return None
    brain_dir = Path.home() / ".gemini" / "antigravity" / "brain" / conv_id / ".system_generated" / "logs"
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        tpath = brain_dir / fname
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
            # A. Check assistant text response / content
            for field in ("response", "content", "text"):
                val = entry.get(field)
                if isinstance(val, str):
                    parsed = _extract_json_dict_from_text(val)
                    if parsed:
                        return parsed
            # B. Fallback: if model called write_file with Content (even while awaiting approval)
            for tc in entry.get("tool_calls") or []:
                if tc.get("name") == "write_file":
                    raw_content = (tc.get("args") or {}).get("Content")
                    if isinstance(raw_content, str):
                        try:
                            unwrapped = json.loads(raw_content) if raw_content.startswith('"') else raw_content
                        except Exception:
                            unwrapped = raw_content
                        if isinstance(unwrapped, str):
                            parsed = _extract_json_dict_from_text(unwrapped)
                            if parsed:
                                return parsed
    return None


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


def run_codex_engine(prompt: str, work_dir: Path, output_json_path: Path, script_cols: list[str]) -> tuple[str, list[dict[str, Any]]]:
    codex_exe = find_codex_exe()
    if not codex_exe:
        raise RuntimeError("未在本机找到 codex.exe，请先安装 Codex 或切换为 Antigravity 引擎")
    last_msg = work_dir / "codex_last_message.txt"
    if output_json_path.exists():
        output_json_path.unlink()
    cmd = [
        codex_exe,
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", str(work_dir),
        "-o", str(last_msg),
        prompt,
    ]
    log(f"Invoking Codex CLI for rea-video script generation in {work_dir}")
    proc = subprocess.run(cmd, env=build_tool_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    fallback = last_msg.read_text(encoding="utf-8", errors="replace") if last_msg.is_file() else (proc.stdout or "")
    if proc.returncode != 0 and not output_json_path.is_file():
        raise RuntimeError(f"Codex 执行失败 (code={proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}")
    return parse_scripts_output(output_json_path, fallback, script_cols)


def run_antigravity_engine(prompt: str, work_dir: Path, output_json_path: Path, script_cols: list[str], title: str) -> tuple[str, list[dict[str, Any]]]:
    addr, csrf = load_antigravity_endpoint()
    if not addr or not csrf:
        raise RuntimeError(
            "未检测到本地 Antigravity ConversationService 端点。请确保 Antigravity 客户端已启动，"
            f"并已写入端点配置 {ENDPOINT_FILE}（或由 Antigravity 环境启动桥接）。"
        )
    if output_json_path.exists():
        output_json_path.unlink()
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "x-antigravity-csrf-token": csrf,
    }
    log(f"Creating Antigravity conversation via http://{addr} for {title}")
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"http://{addr}/product.v1.ConversationService/CreateConversation",
            headers=headers,
            json={
                "title": f"飞书视频解析 · {title}"[:80],
                "tags": ["feishu-sync", "rea-video"],
                "prompt": [{"text": prompt}],
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"调用 Antigravity CreateConversation 失败: HTTP {resp.status_code} {resp.text[:300]}")
        conv_id = ((resp.json().get("conversation") or {}).get("id")) or ""
    log(f"Antigravity conversation started: {conv_id}, waiting for {output_json_path}")

    deadline = time.time() + 900
    while time.time() < deadline:
        if output_json_path.is_file() and output_json_path.stat().st_size > 20:
            try:
                return parse_scripts_output(output_json_path, "", script_cols)
            except Exception:
                pass
        extracted = _extract_scripts_from_antigravity_transcript(conv_id)
        if extracted:
            log(f"Extracted {len(extracted.get('scripts') or [])} scripts from Antigravity conversation {conv_id}!")
            return parse_scripts_output(output_json_path, "", script_cols, raw_obj=extracted)
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
                        time.sleep(2)
                        extracted = _extract_scripts_from_antigravity_transcript(conv_id)
                        if extracted:
                            return parse_scripts_output(output_json_path, "", script_cols, raw_obj=extracted)
                        if output_json_path.is_file():
                            return parse_scripts_output(output_json_path, "", script_cols)
                        raise RuntimeError(f"Antigravity 对话 {conv_id} 已结束，但未找到有效脚本 JSON")
            except RuntimeError:
                raise
            except Exception:
                pass
        time.sleep(4)
    raise RuntimeError(f"等待 Antigravity ({conv_id}) 生成脚本超时（15分钟）")


def post_progress(job_id: str, **kwargs: Any) -> None:
    try:
        httpx.post(f"{VIDEO2_API_URL}/api/feishu/bridge/sync/{job_id}/progress", json=kwargs, timeout=15.0)
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
                video_local = feishu_download_media(api_base, token, att["file_token"], rec_dir / f"reference_video{ext}")

            image_locals: list[Path] = []
            for idx, att in enumerate(image_atts, start=1):
                ext = Path(att["name"]).suffix or ".png"
                img_path = feishu_download_media(api_base, token, att["file_token"], rec_dir / f"ref_img_{idx:02d}{ext}")
                image_locals.append(img_path)

            # 2. Upload media to vast.ai video_2 so one-click video generation can use them immediately
            multipart_files = []
            opened_handles = []
            try:
                if video_local and video_local.is_file():
                    fh = video_local.open("rb")
                    opened_handles.append(fh)
                    multipart_files.append(("video", (video_local.name, fh, "video/mp4")))
                for img_p in image_locals:
                    fh = img_p.open("rb")
                    opened_handles.append(fh)
                    multipart_files.append(("images", (img_p.name, fh, "application/octet-stream")))
                up_resp = httpx.post(
                    f"{VIDEO2_API_URL}/api/feishu/bridge/sync/{job_id}/upload-media",
                    data={
                        "feishu_record_id": rid,
                        "product_name": product_name,
                        "task_name": task_name,
                        "local_video_path": str(video_local) if video_local else "",
                        "local_image_paths_json": json.dumps([str(p) for p in image_locals], ensure_ascii=False),
                    },
                    files=multipart_files,
                    timeout=300.0,
                )
                if up_resp.status_code != 200:
                    raise RuntimeError(f"上传素材到 video_2 失败: HTTP {up_resp.status_code} {up_resp.text[:300]}")
            finally:
                for fh in opened_handles:
                    fh.close()

            # 3. Check existing scripts in Feishu
            existing_scripts = []
            for idx, col in enumerate(script_cols, start=1):
                txt = extract_cell_text(fdict.get(col))
                if txt:
                    existing_scripts.append({"index": idx, "column": col, "text": txt})

            if len(existing_scripts) >= script_count and not force_rerun:
                httpx.post(
                    f"{VIDEO2_API_URL}/api/feishu/bridge/sync/{job_id}/record-complete",
                    json={
                        "feishu_record_id": rid,
                        "product_name": product_name,
                        "task_name": task_name,
                        "scripts": existing_scripts,
                        "engine_used": "existing",
                        "sync_status": "written_back",
                    },
                    timeout=30.0,
                )
                processed += 1
                post_progress(job_id, processed_records=processed, log=f"行 {rid}（{product_name}）飞书已有 {len(existing_scripts)} 份脚本，已复用")
                continue

            if not video_local:
                processed += 1
                post_progress(job_id, processed_records=processed, log=f"行 {rid}（{product_name}）缺少参考视频，跳过拆解")
                continue

            # 4. Run rea-video decompose + selected engine (Antigravity / Codex)
            scripts_out_path = rec_dir / "scripts_out.json"
            if scripts_out_path.is_file() and not force_rerun:
                try:
                    summary, scripts = parse_scripts_output(scripts_out_path, "", script_cols)
                    if len(scripts) >= script_count:
                        log(f"Reusing cached local {scripts_out_path} ({len(scripts)} scripts) for {rid}")
                    else:
                        scripts = []
                except Exception:
                    scripts = []
            else:
                scripts = []

            if not scripts:
                post_progress(
                    job_id,
                    status="analyzing",
                    current_record_id=rid,
                    current_stage=f"正在用 {engine} + rea-video 解析「{product_name}」并生成 {script_count} 份脚本…",
                    log=f"启动 rea-video decompose ({video_local.name})",
                )
                rea_out = rec_dir / "rea_out"
                if not (rea_out / "breakdown.json").is_file() or force_rerun:
                    run_rea_video_decompose(video_local, rea_out)
                prompt = build_agent_prompt(
                    video_path=video_local,
                    image_paths=image_locals,
                    rea_out_dir=rea_out,
                    output_json_path=scripts_out_path,
                    product_name=product_name,
                    task_name=task_name,
                    analysis_prompt=analysis_prompt,
                    script_count=script_count,
                    script_cols=script_cols,
                    engine=engine,
                )
                if engine == "antigravity":
                    summary, scripts = run_antigravity_engine(prompt, rec_dir, scripts_out_path, script_cols, f"{product_name}-{task_name}")
                else:
                    summary, scripts = run_codex_engine(prompt, rec_dir, scripts_out_path, script_cols)

            # 5. Write generated scripts back to Feishu Bitable columns '脚本1'..'脚本N'
            post_progress(
                job_id,
                status="writing_feishu",
                current_record_id=rid,
                current_stage=f"回写 {len(scripts)} 份脚本到飞书「{product_name}」…",
                log=f"正在回写飞书列：{', '.join(s['column'] for s in scripts)}",
            )
            update_fields = {s["column"]: s["text"] for s in scripts}
            feishu_update_record(api_base, token, app_token, table_id, rid, update_fields)

            httpx.post(
                f"{VIDEO2_API_URL}/api/feishu/bridge/sync/{job_id}/record-complete",
                json={
                    "feishu_record_id": rid,
                    "product_name": product_name,
                    "task_name": task_name,
                    "scripts": scripts,
                    "analysis_summary": summary,
                    "engine_used": engine,
                    "sync_status": "written_back",
                },
                timeout=30.0,
            )
            processed += 1
            post_progress(job_id, processed_records=processed, log=f"已完成行 {rid}（{product_name}），{len(scripts)} 份脚本已回写飞书")

        httpx.post(
            f"{VIDEO2_API_URL}/api/feishu/bridge/sync/{job_id}/complete",
            json={"ok": True, "processed_records": processed, "total_records": len(records), "log": f"同步全部完成，共处理 {processed} 条记录"},
            timeout=30.0,
        )
    except Exception as exc:
        log(f"Sync job {job_id} error: {exc}")
        httpx.post(
            f"{VIDEO2_API_URL}/api/feishu/bridge/sync/{job_id}/complete",
            json={"ok": False, "error_message": str(exc)[:1000], "log": f"错误：{exc}"},
            timeout=30.0,
        )


def process_download_batch(manifest: dict[str, Any]) -> None:
    batch_id = manifest["batch_id"]
    archive_root = Path(manifest.get("archive_dir") or str(ARCHIVE_DIR_DEFAULT))
    archive_root.mkdir(parents=True, exist_ok=True)
    items = manifest.get("items") or []
    log(f"Auto-downloading batch {batch_id} ({len(items)} videos) to {archive_root}")

    archived_paths: dict[str, str] = {}
    errors: list[str] = []
    try:
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            for item in items:
                item_id = item["item_id"]
                product_folder = safe_name(item.get("product_folder") or item.get("product_name") or "未命名产品", "未命名产品")
                filename = safe_name(item.get("filename") or f"{item_id}.mp4", f"{item_id}.mp4")
                if not Path(filename).suffix:
                    filename += ".mp4"
                url = f"{VIDEO2_API_URL}{item['download_url']}"

                # Step A: Download into E:\vast视频\<tmp_filename>
                tmp_download = archive_root / f".downloading_{item_id}_{filename}"
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        errors.append(f"{item_id}: HTTP {resp.status_code}")
                        continue
                    with tmp_download.open("wb") as fh:
                        for chunk in resp.iter_bytes(65536):
                            fh.write(chunk)

                # Step B: Archive into E:\vast视频\<产品名称>\<filename>
                target_dir = archive_root / product_folder
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / filename
                if target_path.exists():
                    stem = target_path.stem
                    suffix = target_path.suffix
                    counter = 2
                    while target_path.exists():
                        target_path = target_dir / f"{stem}_{counter}{suffix}"
                        counter += 1
                shutil.move(str(tmp_download), str(target_path))
                archived_paths[item_id] = str(target_path)
                log(f"Archived -> {target_path}")

        httpx.post(
            f"{VIDEO2_API_URL}/api/feishu/bridge/download/{batch_id}/complete",
            json={
                "ok": len(errors) == 0 and len(archived_paths) > 0,
                "archive_dir": str(archive_root),
                "archived_count": len(archived_paths),
                "archived_paths": archived_paths,
                "error_message": "; ".join(errors) if errors else None,
            },
            timeout=30.0,
        )
    except Exception as exc:
        log(f"Batch download {batch_id} failed: {exc}")
        httpx.post(
            f"{VIDEO2_API_URL}/api/feishu/bridge/download/{batch_id}/complete",
            json={"ok": False, "archived_paths": archived_paths, "error_message": str(exc)[:1000]},
            timeout=30.0,
        )


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
    tunnel = SSHTunnel()
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
