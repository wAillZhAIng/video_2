"""Feishu Open Platform Bitable client & URL/column mapping utilities."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.errors import AppError

DEFAULT_FEISHU_API_BASE = "https://open.feishu.cn"
DEFAULT_FEISHU_BITABLE_URL = (
    "https://xcnzovkanfw8.feishu.cn/base/Dakxb6f3aalMdssK4Tmc2WninNf"
    "?table=tblVtjouz9F0855v&view=vewT6bwcpf"
)

DEFAULT_COLUMN_MAPPING = {
    "col_product_name": "产品名称",
    "col_reference_video": "参考视频",
    "col_reference_image": "参考图",
    "col_task_name": "任务名称",
    "col_script_start": "脚本1",
}


def parse_feishu_bitable_url(url: str) -> dict[str, str]:
    raw = (url or "").strip()
    if not raw:
        return {"app_token": "", "table_id": "", "view_id": ""}
    parsed = urlsplit(raw)
    path_parts = [p for p in parsed.path.split("/") if p]
    app_token = ""
    for marker in ("base", "wiki"):
        if marker in path_parts:
            idx = path_parts.index(marker)
            if idx + 1 < len(path_parts):
                app_token = path_parts[idx + 1]
                break
    query = parse_qs(parsed.query)
    table_id = (query.get("table") or [""])[0].strip()
    view_id = (query.get("view") or [""])[0].strip()
    return {"app_token": app_token, "table_id": table_id, "view_id": view_id}


def script_column_names(start_column: str, count: int) -> list[str]:
    """Build ['脚本1', '脚本2', ..., '脚本N'] from start_column (e.g. '脚本1' or '脚本')."""
    base = (start_column or "脚本1").strip()
    match = re.match(r"^(.*?)(\d+)$", base)
    if match:
        prefix = match.group(1) or "脚本"
        start_num = max(1, int(match.group(2)))
    else:
        prefix = base or "脚本"
        start_num = 1
    total = max(1, min(int(count or 1), 20))
    return [f"{prefix}{start_num + i}" for i in range(total)]


def extract_cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts: list[str] = []
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
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("file_token"):
            out.append({
                "file_token": str(item["file_token"]),
                "name": str(item.get("name") or f"{item['file_token']}.bin"),
                "size": int(item.get("size") or 0),
                "type": str(item.get("type") or ""),
                "url": str(item.get("url") or ""),
                "tmp_url": str(item.get("tmp_url") or ""),
            })
    return out


def resolve_column_names(fields: list[dict[str, Any]], mapping: dict[str, str]) -> dict[str, str]:
    """Match configured column names against real Bitable field names without binding attachments to text cols."""
    names = [str(f.get("field_name") or "").strip() for f in fields if f.get("field_name")]
    name_set = set(names)
    text_names = [str(f.get("field_name") or "").strip() for f in fields if f.get("field_name") and f.get("type") != 17]
    att_names = [str(f.get("field_name") or "").strip() for f in fields if f.get("field_name") and f.get("type") == 17]

    def _pick(configured: str, pool: list[str], fallback_idx: int, hints: tuple[str, ...] = ()) -> str:
        c = (configured or "").strip()
        if c and c in name_set and (not pool or c in pool):
            return c
        for n in (pool or names):
            if any(h in n for h in hints):
                return n
        if 0 <= fallback_idx < len(pool):
            return pool[fallback_idx]
        return ""

    return {
        "col_product_name": _pick(mapping.get("col_product_name", "产品名称"), text_names, 0, ("产品名称", "产品", "商品")),
        "col_reference_video": _pick(mapping.get("col_reference_video", "参考视频"), att_names or names, 0, ("参考视频", "视频")),
        "col_reference_image": _pick(mapping.get("col_reference_image", "参考图"), att_names or names, 1, ("参考图", "图片", "产品图")),
        "col_task_name": _pick(mapping.get("col_task_name", "任务名称"), text_names, -1, ("任务名称", "任务", "标题")),
        "col_script_start": (mapping.get("col_script_start") or "脚本1").strip(),
    }


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, base_url: str = DEFAULT_FEISHU_API_BASE, timeout: float = 30.0):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.base_url = (base_url or DEFAULT_FEISHU_API_BASE).rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = DEFAULT_FEISHU_API_BASE
        self.timeout = timeout
        self._token: str | None = None

    def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        if self._token and not force_refresh:
            return self._token
        if not self.app_id or not self.app_secret:
            raise AppError(422, "feishu_credentials_missing", "请先在系统设置中填写飞书 App ID 和 App Secret")
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json={"app_id": self.app_id, "app_secret": self.app_secret})
        try:
            payload = resp.json()
        except Exception as exc:
            raise AppError(502, "feishu_auth_invalid", f"飞书认证响应异常: HTTP {resp.status_code}") from exc
        if resp.status_code != 200 or payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise AppError(
                502,
                "feishu_auth_failed",
                f"飞书认证失败 (code={payload.get('code')}): {payload.get('msg') or resp.text[:200]}",
            )
        self._token = str(payload["tenant_access_token"])
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_tenant_access_token()}"}

    def list_fields(self, app_token: str, table_id: str, view_id: str | None = None) -> list[dict[str, Any]]:
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                params: dict[str, Any] = {"page_size": 100}
                if view_id:
                    params["view_id"] = view_id
                if page_token:
                    params["page_token"] = page_token
                resp = client.get(url, headers=self._headers(), params=params)
                payload = resp.json()
                if resp.status_code != 200 or payload.get("code") != 0:
                    raise AppError(
                        502,
                        "feishu_fields_failed",
                        f"读取飞书表格字段失败 (code={payload.get('code')}): {payload.get('msg')}",
                    )
                data = payload.get("data") or {}
                items.extend(data.get("items") or [])
                if not data.get("has_more"):
                    break
                page_token = data.get("page_token")
        return items

    def create_text_field(self, app_token: str, table_id: str, field_name: str) -> dict[str, Any]:
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._headers(), json={"field_name": field_name, "type": 1})
        payload = resp.json()
        if resp.status_code != 200 or payload.get("code") != 0:
            raise AppError(
                502,
                "feishu_create_field_failed",
                f"自动新增飞书脚本列「{field_name}」失败 (code={payload.get('code')}): {payload.get('msg')}",
            )
        return (payload.get("data") or {}).get("field") or {}

    def ensure_script_columns(
        self,
        app_token: str,
        table_id: str,
        start_column: str,
        script_count: int,
        *,
        view_id: str | None = None,
    ) -> list[str]:
        desired = script_column_names(start_column, script_count)
        existing = {str(f.get("field_name") or "").strip() for f in self.list_fields(app_token, table_id, view_id)}
        for col in desired:
            if col not in existing:
                self.create_text_field(app_token, table_id, col)
                existing.add(col)
        return desired

    def list_records(self, app_token: str, table_id: str, view_id: str | None = None) -> list[dict[str, Any]]:
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        with httpx.Client(timeout=self.timeout) as client:
            while True:
                params: dict[str, Any] = {"page_size": 100}
                if view_id:
                    params["view_id"] = view_id
                if page_token:
                    params["page_token"] = page_token
                resp = client.get(url, headers=self._headers(), params=params)
                payload = resp.json()
                if resp.status_code != 200 or payload.get("code") != 0:
                    raise AppError(
                        502,
                        "feishu_records_failed",
                        f"读取飞书记录失败 (code={payload.get('code')}): {payload.get('msg')}",
                    )
                data = payload.get("data") or {}
                items.extend(data.get("items") or [])
                if not data.get("has_more"):
                    break
                page_token = data.get("page_token")
        return items

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.put(url, headers=self._headers(), json={"fields": fields})
        payload = resp.json()
        if resp.status_code != 200 or payload.get("code") != 0:
            raise AppError(
                502,
                "feishu_update_failed",
                f"回写飞书记录 {record_id} 失败 (code={payload.get('code')}): {payload.get('msg')}",
            )
        return (payload.get("data") or {}).get("record") or {}
