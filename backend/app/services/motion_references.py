"""Native H3 reference-video preparation with source-preserving time ranges."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_cinematic import MotionReferenceInput
from app.services.native_reference_video import require_project_reference_access
from app.services.reference_video_analysis import file_digest


H3_REFERENCE_FPS = 24


def reference_frame_count(duration_seconds: float) -> int:
    """Return the estimated 24fps frame count for metadata only."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise AppError(422, "motion_reference_range", "所选参考片段必须有正时长")
    return max(1, math.ceil(duration_seconds * H3_REFERENCE_FPS))


def configured_motion_reference_values(session, shot):
    """Return the explicit native-reference selection that applies to one shot."""
    detail = session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id == shot.id))
    production = ((detail.source_json or {}).get("production") or {}) if detail else {}
    if isinstance(production.get("reference_video_overrides"), list):
        return copy.deepcopy(production["reference_video_overrides"])
    if production.get("motion_references"):
        return copy.deepcopy(production["motion_references"])
    from app.services.studio_audio_policy import get_preferences
    return copy.deepcopy(get_preferences(session, shot.project_id)["reference_video_defaults"])


def resolve_motion_references(session, settings, shot, values=None):
    """Freeze source identity and the full selected time range for one job."""
    if values is None:
        values = configured_motion_reference_values(session, shot)
    if not isinstance(values, list) or len(values) > 3:
        raise AppError(422, "motion_reference_limit", "动作参考最多选择3条视频")

    result = []
    seen = set()
    for raw in values:
        candidate = raw.model_dump(exclude_none=True) if isinstance(raw, MotionReferenceInput) else raw
        if isinstance(candidate, dict):
            candidate = {
                key: candidate[key]
                for key in MotionReferenceInput.model_fields
                if key in candidate
            }
        try:
            ref = MotionReferenceInput.model_validate(candidate)
        except Exception as exc:
            raise AppError(422, "motion_reference_invalid", "动作参考配置无效或未明确授权") from exc

        if ref.video_id in seen:
            raise AppError(422, "motion_reference_duplicate", "动作参考视频不能重复选择")
        seen.add(ref.video_id)

        video = session.get(m.ReferenceVideo, ref.video_id)
        if not video or video.state != "active":
            raise AppError(409, "motion_reference_missing", "动作参考不存在或已移出图库")
        if video.revision != ref.expected_revision or video.sha256 != ref.source_sha256:
            raise AppError(409, "motion_reference_stale", "动作参考版本或哈希已变化，请重新选择并确认")

        source_duration = (video.metadata_json or {}).get("duration_seconds")
        if (
            not isinstance(source_duration, (int, float))
            or not math.isfinite(source_duration)
            or ref.trim_end_seconds > source_duration + 1e-6
        ):
            raise AppError(422, "motion_reference_range", "所选片段超出原视频时长")

        source = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
        if not source.is_file():
            raise AppError(409, "motion_reference_changed", "动作参考原视频缺失，未提交生成")

        # When a long project-level reference covers multiple shots, align each
        # shot to a scene segment before freezing its native H3 video input.
        eff_start = ref.trim_start_seconds
        eff_end = ref.trim_end_seconds
        auto_slice_meta = None
        try:
            from app.services.reference_video_slicer import auto_slice_range_for_shot

            project_shots = list(
                session.scalars(
                    select(m.StoryboardShot)
                    .where(m.StoryboardShot.project_id == shot.project_id)
                    .order_by(m.StoryboardShot.order.asc())
                )
            )
            shot_ids = [project_shot.id for project_shot in project_shots]
            shot_idx = (
                shot_ids.index(shot.id)
                if shot.id in shot_ids
                else max(0, (getattr(shot, "order", None) or 1) - 1)
            )
            all_durs = [float(item.duration_seconds or 3.0) for item in project_shots]
            if not all_durs:
                all_durs = [float(shot.duration_seconds or 3.0)]
            eff_start, eff_end, auto_slice_meta = auto_slice_range_for_shot(
                ffmpeg_path=settings.ffmpeg_path,
                source_path=source,
                source_duration=source_duration,
                sha256=video.sha256,
                trim_start=eff_start,
                trim_end=eff_end,
                shot_index=shot_idx,
                shot_duration=float(shot.duration_seconds or 3.0),
                all_shot_durations=all_durs,
            )
        except Exception:
            eff_start, eff_end = ref.trim_start_seconds, ref.trim_end_seconds

        if (
            not math.isfinite(eff_start)
            or not math.isfinite(eff_end)
            or eff_start < ref.trim_start_seconds
            or eff_end > ref.trim_end_seconds
            or eff_end <= eff_start
        ):
            eff_start, eff_end = ref.trim_start_seconds, ref.trim_end_seconds
            auto_slice_meta = {"strategy": "invalid_slice_fallback_original", "sliced": False}

        selected_duration = eff_end - eff_start
        expected_frames = reference_frame_count(selected_duration)
        resolved_reference = ref.model_dump(exclude_none=True)
        resolved_reference["trim_start_seconds"] = eff_start
        resolved_reference["trim_end_seconds"] = eff_end
        if auto_slice_meta and auto_slice_meta.get("sliced"):
            resolved_reference["auto_slice"] = auto_slice_meta

        result.append(
            {
                **resolved_reference,
                "relative_path": video.relative_path,
                "absolute_path": str(source),
                "selected_duration_seconds": selected_duration,
                "source_frame_count_at_24fps": expected_frames,
                "effective_frames": expected_frames,
                "effective_duration_seconds": selected_duration,
                "source_duration_seconds": source_duration,
                "fps": H3_REFERENCE_FPS,
                "source_has_audio": bool((video.metadata_json or {}).get("has_audio")),
                "audio_included": bool(ref.include_audio),
            }
        )

    require_project_reference_access(session, shot.project_id, result)
    return result


def verify_motion_sources(references):
    """Verify source hashes immediately before submission."""
    for ref in references:
        source = Path(ref["absolute_path"])
        if not source.is_file() or file_digest(source) != ref["source_sha256"]:
            raise AppError(409, "motion_reference_changed", "动作参考原视频缺失或哈希变化，未提交生成")


def motion_live_state(session, values):
    """Return state used to detect stale reference selections."""
    result = []
    for ref in values or []:
        identifier = ref.get("video_id") if isinstance(ref, dict) else None
        item = session.get(m.ReferenceVideo, identifier) if identifier else None
        result.append(
            {
                "video_id": identifier,
                "revision": item.revision if item else None,
                "sha256": item.sha256 if item else None,
                "state": item.state if item else "missing",
            }
        )
    return result


def active_motion_jobs(session, video_id):
    active = {"submitting", "queued", "submitted", "running", "postprocessing", "unknown", "cancelling", "reconciling"}
    return [
        job.id
        for job in session.scalars(select(m.GenerationJob).where(m.GenerationJob.status.in_(active)))
        if any(
            isinstance(ref, dict) and ref.get("video_id") == video_id
            for ref in (job.parameters_json or {}).get("motion_references", [])
        )
    ]


def motion_bound_shots(session, video_id):
    return [
        detail.shot_id
        for detail in session.scalars(select(m.ShotDetail))
        if any(
            isinstance(ref, dict) and ref.get("video_id") == video_id
            for ref in ((detail.source_json or {}).get("production") or {}).get("motion_references", [])
        )
    ]


def _actual_frame_count(stream: dict) -> int:
    try:
        return int(stream.get("nb_read_frames") or 0)
    except (TypeError, ValueError):
        return 0


def prepare_motion_reference(settings, ref):
    """Create a checked 24fps file containing the complete selected reference range."""
    source = Path(ref["absolute_path"])
    if not source.is_file() or file_digest(source) != ref["source_sha256"]:
        raise AppError(409, "motion_reference_changed", "动作参考原视频已变化，未提交生成")

    trim_start = float(ref["trim_start_seconds"])
    trim_end = float(ref["trim_end_seconds"])
    selected_duration = trim_end - trim_start
    reference_frame_count(selected_duration)
    include_audio = bool(ref.get("include_audio"))
    if include_audio and ref.get("source_has_audio") is False:
        raise AppError(422, "motion_reference_audio_missing", "已要求使用参考视频音频，但所选视频没有可用音轨")

    root = Path(settings.media_root) / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="motion-reference-", dir=root) as directory:
            target = Path(directory) / "reference.mp4"
            video_filters = [
                f"trim=start={trim_start:.9f}:end={trim_end:.9f}",
                "setpts=PTS-STARTPTS",
                f"fps={H3_REFERENCE_FPS}",
            ]
            command = [
                settings.ffmpeg_path,
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-filter:v",
                ",".join(video_filters),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
            ]
            if include_audio:
                audio_filters = [
                    f"atrim=start={trim_start:.9f}:end={trim_end:.9f}",
                    "asetpts=PTS-STARTPTS",
                ]
                command.extend(
                    [
                        "-map",
                        "0:a:0?",
                        "-filter:a",
                        ",".join(audio_filters),
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-ac",
                        "2",
                        "-ar",
                        "48000",
                    ]
                )
            else:
                command.append("-an")
            command.extend(["-movflags", "+faststart", str(target)])

            encoded = subprocess.run(
                command,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if encoded.returncode:
                raise AppError(422, "motion_reference_transcode", "动作参考转码失败，未提交生成")

            probed = subprocess.run(
                [
                    settings.ffprobe_path,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-of",
                    "json",
                    str(target),
                ],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if probed.returncode:
                raise AppError(422, "motion_reference_probe", "动作参考转码后无法核验，未提交生成")
            streams = json.loads(probed.stdout).get("streams", [])
            video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
            audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
            if (
                len(video_streams) != 1
                or video_streams[0].get("avg_frame_rate") != "24/1"
                or _actual_frame_count(video_streams[0]) < 1
            ):
                raise AppError(422, "motion_reference_output_invalid", "动作参考帧率或帧序列无效，未提交生成")
            if include_audio and len(audio_streams) != 1:
                raise AppError(422, "motion_reference_audio_missing", "已要求使用参考视频音频，但所选片段没有可用音轨")
            if not include_audio and audio_streams:
                raise AppError(422, "motion_reference_audio_unexpected", "未要求参考音频，但转码结果包含音轨")

            decoded = [settings.ffmpeg_path, "-nostdin", "-v", "error", "-i", str(target), "-map", "0:v:0"]
            if include_audio:
                decoded.extend(["-map", "0:a:0"])
            decoded.extend(["-f", "null", "-"])
            checked = subprocess.run(
                decoded,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if checked.returncode:
                raise AppError(422, "motion_reference_decode_failed", "动作参考转码后无法完整解码，未提交生成")
            if file_digest(source) != ref["source_sha256"]:
                raise AppError(409, "motion_reference_changed", "动作参考处理期间原视频变化，未提交生成")

            data = target.read_bytes()
            if not data:
                raise AppError(422, "motion_reference_output_size", "动作参考转换文件为空")
            frame_count = _actual_frame_count(video_streams[0])
            return data, {
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "frames": frame_count,
                "fps": H3_REFERENCE_FPS,
                "audio_included": include_audio,
                "width": video_streams[0].get("width"),
                "height": video_streams[0].get("height"),
                "conversion": {
                    "trim_start_seconds": trim_start,
                    "trim_end_seconds": trim_end,
                    "selected_duration_seconds": selected_duration,
                    "fps": H3_REFERENCE_FPS,
                    "video_codec": "libx264",
                    "audio_codec": "aac" if include_audio else None,
                    "scale": "none",
                    "orientation": "ffmpeg default autorotate",
                },
            }
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        raise AppError(422, "motion_reference_processing_failed", "动作参考处理失败，未提交生成") from exc
