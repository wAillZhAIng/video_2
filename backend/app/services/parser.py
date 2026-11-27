from __future__ import annotations

import re
from dataclasses import asdict, dataclass


SHOT_HEADER = re.compile(
    r"^\s*(?:分镜|镜头)\s*(\d+)\s*[：:]\s*(.*?)(?:[（(]\s*(\d+(?:\.\d+)?)\s*秒\s*[)）])?\s*$"
)
FIELD_LINE = re.compile(r"^\s*([^：:]{1,20})[：:]\s*(.*)$")

_TIME_RANGE_INNER = r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)?\s*[-–—~～到至]\s*(\d+(?:\.\d+)?)\s*(?:秒|s|S)?"
_SINGLE_DUR_INNER = r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)"

_SHOT_PAREN_FIRST = re.compile(
    rf"^(?:分镜|镜头)\s*(\d+)\s*[（(]\s*(?:{_TIME_RANGE_INNER}|{_SINGLE_DUR_INNER})\s*[)）]\s*(?:([：:])\s*)?(.*)$"
)
_SHOT_COLON_FIRST = re.compile(r"^(?:分镜|镜头)\s*(\d+)\s*[：:]\s*(.*)$")
_TRAILING_TIME_PAREN = re.compile(
    rf"[（(]\s*(?:{_TIME_RANGE_INNER}|{_SINGLE_DUR_INNER})\s*[)）]\s*$"
)
_LEADING_TIME_RANGE = re.compile(rf"^{_TIME_RANGE_INNER}(?:\s+(.*))?$")

_GLOBAL_HEADER_LINE = re.compile(r"^\s*(?:【\s*(?:全局设定|全局锚点|全局规则|通用设定)\s*】|(?:全局设定|全局锚点)\s*[：:])\s*(.*)$")
_SECTION_DIVIDER_LINE = re.compile(r"^\s*(?:【[^】]*(?:分镜|镜头|脚本)[^】]*】|-{3,}|={3,})\s*$")
_ASPECT_RATIO_RE = re.compile(r"\b(9:16|16:9|1:1)\b")
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-•*▪▫]\s+)")

_LEGACY_META_KEYS = {"项目标题", "内容类型", "统一主角", "统一画面风格"}
_NEGATIVE_KEYS = ("负面提示词", "禁止项", "质量与禁止项", "禁止")
_AUDIO_KEYS = ("声音提示", "声音", "音效", "音乐")
_DIALOGUE_KEYS = ("台词", "对话")
_CAMERA_KEYS = ("运镜", "运镜/衔接", "运镜与衔接", "运镜与光线", "景别与运镜")
_LIGHTING_KEYS = ("光线", "光线与产品细节", "光线/产品细节", "光线与音效", "运镜与光线")
_VISUAL_PRIORITY_KEYS = ("画面提示词", "画面", "提示词")
_RICH_VISUAL_KEYS = (
    "景别与机位",
    "景别/机位",
    "景别与运镜",
    "画面主体",
    "画面主体与动作轨迹",
    "动作轨迹",
    "运镜",
    "运镜/衔接",
    "运镜与衔接",
    "运镜与光线",
    "光线",
    "光线与产品细节",
    "光线/产品细节",
    "光线与音效",
    "产品细节",
    "产品一致性细节",
)


@dataclass(slots=True)
class _HeaderSpec:
    order: int
    title: str
    duration_seconds: float
    inline_body: str = ""


@dataclass(slots=True)
class ParsedShot:
    order: int
    scene_title: str
    narration: str = ""
    dialogue: str = ""
    visual_prompt: str = ""
    negative_prompt: str = ""
    action: str = ""
    camera_move: str = ""
    location: str = ""
    lighting: str = ""
    audio_direction: str = ""
    duration_seconds: float = 5.0
    aspect_ratio: str = "9:16"


def _compute_duration(start_s: str | None, end_s: str | None, single_s: str | None, default: float = 5.0) -> float:
    if start_s is not None and end_s is not None:
        diff = round(float(end_s) - float(start_s), 3)
        if diff > 0:
            return max(0.25, min(60.0, diff))
    if single_s is not None:
        val = round(float(single_s), 3)
        if val > 0:
            return max(0.25, min(60.0, val))
    return default


def _clean_header_candidate(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
    cleaned = _BULLET_PREFIX_RE.sub("", cleaned)
    if cleaned.startswith("**") and cleaned.endswith("**") and len(cleaned) > 4:
        cleaned = cleaned[2:-2].strip()
    elif cleaned.startswith("**"):
        cleaned = cleaned[2:].replace("**", "", 1).strip()
    return cleaned


def _match_shot_header(line: str) -> _HeaderSpec | None:
    candidate = _clean_header_candidate(line)
    if not candidate:
        return None

    m_paren = _SHOT_PAREN_FIRST.match(candidate)
    if m_paren:
        order = int(m_paren.group(1))
        duration = _compute_duration(m_paren.group(2), m_paren.group(3), m_paren.group(4))
        has_colon = bool(m_paren.group(5))
        rest = (m_paren.group(6) or "").strip()
        title = f"镜头 {order}"
        inline_body = ""
        if rest:
            if has_colon and (len(rest) > 24 or any(p in rest for p in ("。", "；", "，", ","))):
                inline_body = rest
            elif len(rest) > 32:
                inline_body = rest
            else:
                title = rest
        return _HeaderSpec(order=order, title=title, duration_seconds=duration, inline_body=inline_body)

    m_colon = _SHOT_COLON_FIRST.match(candidate)
    if m_colon:
        order = int(m_colon.group(1))
        tail = (m_colon.group(2) or "").strip()
        duration = 5.0
        inline_body = ""
        m_trail = _TRAILING_TIME_PAREN.search(tail)
        if m_trail:
            duration = _compute_duration(m_trail.group(1), m_trail.group(2), m_trail.group(3))
            tail = tail[: m_trail.start()].strip()
        else:
            m_lead = _LEADING_TIME_RANGE.match(tail)
            if m_lead:
                duration = _compute_duration(m_lead.group(1), m_lead.group(2), None)
                tail = (m_lead.group(3) or "").strip()
        if tail and (len(tail) > 36 and any(p in tail for p in ("。", "；", "，"))):
            inline_body = tail
            title = f"镜头 {order}"
        else:
            title = tail or f"分镜 {order}"
        return _HeaderSpec(order=order, title=title, duration_seconds=duration, inline_body=inline_body)

    return None


def _extract_global_setting(preamble_lines: list[str]) -> tuple[dict[str, str], str, str, str]:
    metadata: dict[str, str] = {}
    global_lines: list[str] = []
    negative_parts: list[str] = []
    aspect_ratio = "9:16"
    in_global_section = False

    for raw_line in preamble_lines:
        stripped = raw_line.strip()
        if not stripped or _SECTION_DIVIDER_LINE.match(stripped):
            continue

        ar_match = _ASPECT_RATIO_RE.search(stripped)
        if ar_match:
            aspect_ratio = ar_match.group(1)

        g_match = _GLOBAL_HEADER_LINE.match(stripped)
        if g_match:
            in_global_section = True
            rest = g_match.group(1).strip()
            if rest:
                _classify_global_line(rest, global_lines, negative_parts)
            continue

        field = FIELD_LINE.match(_BULLET_PREFIX_RE.sub("", stripped))
        if field:
            key = field.group(1).strip()
            val = field.group(2).strip()
            if key in _LEGACY_META_KEYS and not in_global_section:
                metadata[key] = val
                continue
            in_global_section = True
            if any(nk in key for nk in _NEGATIVE_KEYS):
                if val:
                    negative_parts.append(val)
                continue
            if key in {"总时长", "画幅", "画幅比例", "画幅与画质"}:
                # Keep style/quality hints if mixed in, drop duration declaration so per-shot prompts don't cite total duration.
                cleaned = re.sub(r"总时长\s*[：:]?\s*\d+(?:\.\d+)?\s*(?:秒|s|S)?\s*[｜|，,。；;]?\s*", "", stripped)
                if cleaned and any(ch in cleaned for ch in ("风格", "质感", "景深", "光影", "音效", "视角")):
                    _classify_global_line(cleaned, global_lines, negative_parts)
                continue
            if val:
                _classify_global_line(f"{key}：{val}", global_lines, negative_parts)
            continue

        if in_global_section:
            _classify_global_line(stripped, global_lines, negative_parts)

    global_context = "\n".join(global_lines).strip()
    global_negative = "；".join(dict.fromkeys(p.strip("；;。") for p in negative_parts if p.strip())).strip()
    if global_context:
        metadata["全局设定"] = global_context
    return metadata, global_context, global_negative, aspect_ratio


def _classify_global_line(line: str, global_lines: list[str], negative_parts: list[str]) -> None:
    cleaned = re.sub(r"总时长\s*[：:]?\s*\d+(?:\.\d+)?\s*(?:秒|s|S)?\s*[｜|，,。；;]?\s*", "", line).strip()
    cleaned = re.sub(r"共\s*\d+\s*个?(?:镜头|分镜)\s*[，,。；;]?\s*", "", cleaned).strip()
    if not cleaned:
        return
    # Split trailing prohibition clause if present in an inline global paragraph
    neg_match = re.search(r"(?:质量与禁止项|禁止项|严禁出现|严禁|禁止出现|禁止)[：:]?([^。\n]+)", cleaned)
    if neg_match and not cleaned.startswith(("人物", "服装", "产品", "车辆", "场景")):
        neg_clause = neg_match.group(0).strip(" ，,。；;")
        if neg_clause:
            negative_parts.append(neg_clause)
        before = cleaned[: neg_match.start()].strip(" ，,。；;|｜")
        if before:
            global_lines.append(before)
        return
    if cleaned.startswith(("禁止", "严禁")):
        negative_parts.append(cleaned)
        return
    global_lines.append(cleaned)


class ScriptParser:
    def parse(self, raw_text: str) -> dict:
        raise NotImplementedError


class DeterministicScriptParser(ScriptParser):
    """Rule-based parser that remains usable without a text model."""

    def parse(self, raw_text: str) -> dict:
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise ValueError("脚本文本为空")
        lines = text.split("\n")
        preamble_lines: list[str] = []
        blocks: list[tuple[_HeaderSpec, list[str]]] = []
        current: tuple[_HeaderSpec, list[str]] | None = None

        for line in lines:
            header = _match_shot_header(line)
            if header:
                if current:
                    blocks.append(current)
                initial_lines = [header.inline_body] if header.inline_body else []
                current = (header, initial_lines)
                continue
            if current:
                current[1].append(line)
            else:
                preamble_lines.append(line)
        if current:
            blocks.append(current)

        has_explicit_headers = bool(blocks)
        if has_explicit_headers:
            metadata, global_context, global_negative, aspect_ratio = _extract_global_setting(preamble_lines)
        else:
            metadata, global_context, global_negative, aspect_ratio = {}, "", "", "9:16"
            blocks = self._fallback_blocks(text)

        shots = [
            self._parse_block(
                header,
                block_lines,
                i + 1,
                global_context=global_context,
                global_negative=global_negative,
                aspect_ratio=aspect_ratio,
            )
            for i, (header, block_lines) in enumerate(blocks)
        ]
        return {
            "parser": "deterministic-v1",
            "title": metadata.get("项目标题", "未命名项目"),
            "metadata": metadata,
            "shots": [asdict(shot) for shot in shots],
            "warnings": [] if has_explicit_headers else ["未发现明确分镜标记，已按段落规则拆分"],
        }

    def _fallback_blocks(self, text: str) -> list[tuple[_HeaderSpec, list[str]]]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if not paragraphs:
            paragraphs = [text]
        blocks: list[tuple[_HeaderSpec, list[str]]] = []
        for index, paragraph in enumerate(paragraphs, start=1):
            spec = _HeaderSpec(order=index, title=f"段落 {index}", duration_seconds=5.0)
            blocks.append((spec, [f"剧情：{paragraph}"]))
        return blocks

    def _parse_block(
        self,
        header: _HeaderSpec,
        lines: list[str],
        fallback_order: int,
        *,
        global_context: str = "",
        global_negative: str = "",
        aspect_ratio: str = "9:16",
    ) -> ParsedShot:
        order = int(header.order or fallback_order)
        title = (header.title or f"分镜 {order}").strip()[:180]
        duration = float(header.duration_seconds or 5.0)
        fields: dict[str, list[str]] = {}
        field_order: list[str] = []
        active_key = "剧情"
        fields[active_key] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line or _SECTION_DIVIDER_LINE.match(line):
                continue
            unbulleted = _BULLET_PREFIX_RE.sub("", line)
            match = FIELD_LINE.match(unbulleted)
            if match:
                active_key = match.group(1).strip()
                if active_key not in fields:
                    fields[active_key] = []
                    field_order.append(active_key)
                val = match.group(2).strip()
                if val:
                    fields[active_key].append(val)
            else:
                if active_key not in field_order:
                    field_order.append(active_key)
                fields.setdefault(active_key, []).append(unbulleted)

        def value(*keys: str) -> str:
            for key in keys:
                if fields.get(key):
                    return "\n".join(fields[key]).strip()
            return ""

        narration = value("剧情", "旁白", "叙述")
        explicit_visual = value(*_VISUAL_PRIORITY_KEYS)
        if explicit_visual:
            visual = explicit_visual
        else:
            rich_parts: list[str] = []
            for key in field_order:
                if key in _AUDIO_KEYS or key in _DIALOGUE_KEYS or any(nk in key for nk in _NEGATIVE_KEYS):
                    continue
                joined = "\n".join(fields.get(key) or []).strip()
                if not joined:
                    continue
                if key == "剧情":
                    rich_parts.append(joined)
                else:
                    rich_parts.append(f"{key}：{joined}")
            visual = "\n".join(rich_parts).strip() or narration or title

        if not narration:
            narration = value("画面主体与动作轨迹", "动作轨迹", "画面主体") or visual

        action = value("动作轨迹", "画面主体与动作轨迹", "动作", "剧情") or narration
        audio = value(*_AUDIO_KEYS) or value("光线与音效")
        dialogue = value(*_DIALOGUE_KEYS)

        neg_items: list[str] = []
        shot_neg = value(*_NEGATIVE_KEYS)
        if shot_neg:
            neg_items.append(shot_neg)
        if global_negative and global_negative not in shot_neg:
            neg_items.append(global_negative)
        negative = "；".join(neg_items).strip()

        camera = value(*_CAMERA_KEYS)[:180] or self._first_match(
            visual, ["缓慢推进", "平稳横移", "镜头推进", "推近", "固定镜头", "固定机位", "摇镜", "跟拍", "追随", "俯拍", "甩镜"]
        )
        lighting = value(*_LIGHTING_KEYS)[:180] or self._first_match(
            visual, ["暖冷混合照明", "暖色室内灯光", "写实电影灯光", "自然光", "柔光", "日光", "路灯"]
        )

        full_visual = f"{visual}\n全局一致设定：{global_context}".strip() if global_context else visual

        return ParsedShot(
            order=order,
            scene_title=title,
            narration=narration,
            dialogue=dialogue,
            visual_prompt=full_visual,
            negative_prompt=negative,
            action=action,
            camera_move=camera,
            location=title,
            lighting=lighting,
            audio_direction=audio,
            duration_seconds=duration,
            aspect_ratio=aspect_ratio,
        )

    @staticmethod
    def _first_match(text: str, candidates: list[str]) -> str:
        return next((candidate for candidate in candidates if candidate in text), "")
