from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


VERSION = "context-ir-nl-input-v4"
_HEADER = re.compile(r"^\s*(?P<label>[^:：]{1,48})\s*[:：]\s*(?P<value>.*)$")
_TIME = r"\d{1,3}(?::\d{1,2}){1,2}(?:[.,]\d+)?"
_TIME_RANGE = re.compile(rf"(?P<start>{_TIME})\s*(?:-|–|—|~|～|至|到)\s*(?P<end>{_TIME})")
_SOURCE_TIMELINE_LABELS = {
    "镜头", "镜号", "镜头时间", "分镜时间", "时间码", "时间段", "时段", "timecode",
    "timecode range", "shot time", "shot timing", "timeline",
}
_FIELD_ALIASES = {
    "subject": {"主体", "主体身份", "人物", "角色", "商品", "产品", "道具", "subject", "character", "product", "prop"},
    "appearance": {"外观", "外观描述", "主体外观", "人物外观", "产品外观", "appearance", "visual appearance", "look"},
    "action": {"动作", "动作描述", "行为", "表演", "action", "performance"},
    "camera": {"机位与运镜", "运镜", "镜头运动", "摄影机运动", "camera movement", "camera move", "camera"},
    "framing": {"景别", "构图", "摄影与构图", "镜头构图", "composition", "framing", "shot size"},
    "angle": {"拍摄角度", "摄影角度", "视角", "机位角度", "angle", "camera angle", "viewpoint"},
    "scene": {"场景", "地点", "环境", "背景", "scene", "setting", "environment", "location"},
    "lighting": {"光线", "灯光", "照明", "lighting"},
    "audio": {"声音", "声音事件", "音频", "音效", "对白", "台词", "配音", "音乐", "audio", "sound", "sound design", "dialogue", "voiceover", "music"},
    "positive_constraints": {"正向约束", "必须保留", "保留项", "必须", "positive constraints", "must keep"},
    "negative_constraints": {"禁止项", "禁止事项", "负向约束", "排除项", "不要", "negative constraints", "avoid", "negative prompt"},
    "references": {"参考图", "参考图片", "参考视频", "参考素材", "图像参考", "视频参考", "references", "reference", "reference image", "reference video"},
    "output": {"输出", "输出规格", "画幅", "分辨率", "时长", "output", "output settings", "resolution", "duration"},
}
_ALIAS_TO_FIELD = {alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases}


def _label_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold().rstrip(". ")


def _clock_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    numbers = [float(part) for part in parts]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    raise ValueError("unsupported time code")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_compiler_input(
    prompt: str,
    *,
    duration_seconds: float | int | None,
    shot_count: int = 1,
    camera_sentence: str = "",
) -> dict[str, Any]:
    """Add a source-preserving semantic envelope for a single Context-IR render.

    Only a dedicated shot/timecode heading that lies outside the typed output
    duration is relabeled as a source-document locator. The untouched document
    and original prompt remain owned by the caller; all other prose is copied
    byte-for-byte at the Python string level.
    """
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    if type(shot_count) is not int or shot_count < 1:
        raise ValueError("shot_count must be a positive integer")
    if duration_seconds is not None and (
        type(duration_seconds) not in (int, float) or duration_seconds <= 0
    ):
        raise ValueError("duration_seconds must be a positive number or None")

    normalized_lines: list[str] = []
    fields: list[dict[str, Any]] = []
    source_timecodes: list[dict[str, Any]] = []
    line_changes: list[dict[str, Any]] = []
    for line_number, line in enumerate(prompt.splitlines(), start=1):
        match = _HEADER.match(line)
        if not match:
            normalized_lines.append(line)
            continue
        label, value = match.group("label").strip(), match.group("value")
        key = _label_key(label)
        field = _ALIAS_TO_FIELD.get(key)
        time_match = _TIME_RANGE.search(value)
        if key in _SOURCE_TIMELINE_LABELS and time_match:
            field = "timeline"
        if field:
            fields.append({"field": field, "label": label, "line": line_number, "value_chars": len(value), "value_sha256": _sha256(value)})
        if (key in _SOURCE_TIMELINE_LABELS and time_match and duration_seconds is not None):
            try:
                start_seconds = _clock_seconds(time_match.group("start"))
                end_seconds = _clock_seconds(time_match.group("end"))
            except ValueError:
                normalized_lines.append(line)
                continue
            # One frame of tolerance accommodates 24-fps endpoint rounding.
            if start_seconds > 0 and end_seconds > float(duration_seconds) + (1 / 24) + 0.001:
                raw_range = time_match.group(0)
                source_timecodes.append({
                    "line": line_number,
                    "label": label,
                    "raw_range": raw_range,
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "meaning": "source_document_locator_not_output_timeline",
                })
                replacement = "原稿镜头位置（仅来源定位，非本次输出切点；原时间码记录于编译审计元数据）"
                # Replace only the out-of-clip range. The rest of the user's
                # line may contain action, product, or negative constraints.
                replacement_start = match.start("value") + time_match.start()
                replacement_end = match.start("value") + time_match.end()
                normalized_lines.append(
                    line[:replacement_start] + replacement + line[replacement_end:]
                )
                line_changes.append({"line": line_number, "field": "timeline", "change": "relabel_out_of_clip_timecode_as_source_locator"})
                continue
        normalized_lines.append(line)

    normalized_prompt = "\n".join(normalized_lines)
    if prompt.endswith(("\n", "\r")) and normalized_prompt and not normalized_prompt.endswith("\n"):
        normalized_prompt += "\n"

    if duration_seconds is None:
        duration_rule = "The typed output duration is authoritative; do not infer or extend it from prose."
    else:
        duration_rule = f"The typed output duration is exactly {float(duration_seconds):g} seconds; the rendered local timeline starts at 0 seconds."
    if shot_count == 1:
        shot_rule = (
            "This compile request represents exactly one selected output shot. Emit one "
            "correctly numbered shot heading for shot 1. Do not emit any additional numbered "
            "shot heading, timestamped cut, transition, or alternate-angle block. Keep all "
            "caller-specified beats in the body of this one shot and preserve their order."
        )
        shot_scope = "the single shot"
    else:
        shot_rule = (
            f"This compile request represents exactly {shot_count} selected output shots. "
            f"Emit exactly {shot_count} numbered shot blocks with contiguous numbering in "
            "ascending order starting at 1. Do not add extra shot blocks."
        )
        shot_scope = "the requested shot sequence"
    directives = [
        "[Context-IR normalized compile contract]",
        shot_rule,
        duration_rule + " Keep all described actions within the selected shot count. Do not place a cut at or after the output end.",
        "Original script time ranges are source-document locators only when explicitly labeled as such; they do not define this output clip's timeline.",
        "Whole-project or multi-shot continuity statements remain continuity constraints for this selected output; they do not request additional output shots.",
        "Preserve every stated subject, product identity, action, positive constraint, and negative constraint. Do not invent missing identities or key actions.",
        "Use normalized semantic field labels as typed cues: subject, appearance, action, camera, framing, angle, scene, lighting, timeline, audio, positive/negative constraints, references, and output. Unlabeled or ambiguous prose remains source text, not a confirmed field value.",
    ]
    if camera_sentence:
        directives.append(
            f"The only camera movement in {shot_scope} is exactly: {camera_sentence} "
            "Do not add another camera movement or cut based on action, product-scale, or continuity wording; preserve those instructions as constraints in the shot body."
        )
    report = {
        "version": VERSION,
        "original_prompt_sha256": _sha256(prompt),
        "normalized_prompt_sha256": _sha256(normalized_prompt),
        "duration_seconds": float(duration_seconds) if duration_seconds is not None else None,
        "shot_count": shot_count,
        "semantic_fields": fields,
        "source_timecodes": source_timecodes,
        "changes": line_changes,
        "camera_field_authority": "explicit_camera_heading_only" if camera_sentence else "not_determined",
        "original_text_preserved_outside_compile_view": True,
    }
    report["normalization_sha256"] = _sha256(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return {"prompt": normalized_prompt, "directives": "\n".join(directives), "report": report}
