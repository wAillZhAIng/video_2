"""Explicit, bounded video references for H3; source files are never modified."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
import copy
from pathlib import Path

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_cinematic import MotionReferenceInput
from app.services.native_reference_video import require_project_reference_access
from app.services.reference_video_analysis import file_digest


def configured_motion_reference_values(session, shot):
    """Return the explicit native-reference selection that applies to one shot.

    An empty per-shot override deliberately disables the project default.  The
    legacy field remains a read-only compatibility source for existing shots
    until the user saves their configuration through the project workspace.
    """
    detail = session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id == shot.id))
    production = ((detail.source_json or {}).get('production') or {}) if detail else {}
    if isinstance(production.get('reference_video_overrides'), list):
        return copy.deepcopy(production['reference_video_overrides'])
    if isinstance(production.get('motion_references'), list):
        return copy.deepcopy(production['motion_references'])
    from app.services.studio_audio_policy import get_preferences
    return copy.deepcopy(get_preferences(session, shot.project_id)['reference_video_defaults'])


def resolve_motion_references(session, settings, shot, values=None):
    """Freeze source identity and bounds; refuse stale or unauthorised media."""
    if values is None:
        values = configured_motion_reference_values(session, shot)
    if not isinstance(values,list) or len(values)>3:
        raise AppError(422,'motion_reference_limit','动作参考最多选择3条视频')
    result=[]; seen=set()
    for raw in values:
        candidate = raw.model_dump(exclude_none=True) if isinstance(raw, MotionReferenceInput) else raw
        # A frozen run includes computed evidence (frame count, checked
        # duration, etc.).  Validate only the original user binding before
        # recalculating that evidence against the current source file.
        if isinstance(candidate, dict):
            candidate = {key: candidate[key] for key in MotionReferenceInput.model_fields if key in candidate}
        try: ref=MotionReferenceInput.model_validate(candidate)
        except Exception as exc: raise AppError(422,'motion_reference_invalid','动作参考配置无效或未明确授权') from exc
        if ref.video_id in seen:raise AppError(422,'motion_reference_duplicate','动作参考视频不能重复选择')
        seen.add(ref.video_id)
        video=session.get(m.ReferenceVideo,ref.video_id)
        if not video or video.state!='active':raise AppError(409,'motion_reference_missing','动作参考不存在或已移出图库')
        if video.revision!=ref.expected_revision or video.sha256!=ref.source_sha256:
            raise AppError(409,'motion_reference_stale','动作参考版本或哈希已变化，请重新选择并确认')
        duration=(video.metadata_json or {}).get('duration_seconds')
        if not isinstance(duration,(int,float)) or not math.isfinite(duration) or ref.trim_end_seconds>duration+1e-6:
            raise AppError(422,'motion_reference_range','所选片段超出原视频时长')
        source=safe_media_path(settings.media_root,video.relative_path,must_exist=True)
        if not source.is_file():raise AppError(409,'motion_reference_changed','动作参考原视频缺失，未提交生成')
        frames=round((ref.trim_end_seconds-ref.trim_start_seconds)*24)
        if not 48<=frames<=360:
            raise AppError(422,'motion_reference_duration','H3 原生视频参考需为2至15秒的24fps片段；请调整所选起止时间')
        include_audio=bool(getattr(ref,'include_audio',False))
        result.append({**ref.model_dump(exclude_none=True),'relative_path':video.relative_path,'absolute_path':str(source),
                       'effective_frames':frames,'effective_duration_seconds':frames/24,
                       'source_duration_seconds':duration,'fps':24,'audio_included':include_audio})
    total_duration = sum(item['effective_duration_seconds'] for item in result)
    if total_duration > 15 + 1e-6:
        raise AppError(422, 'motion_reference_total_duration',
                       '同一镜头的原生参考视频总时长不能超过15秒；请缩短片段或移除部分视频')
    # A valid, active library item is not automatically safe to reuse in a
    # different project.  The project-level binding is the explicit consent
    # boundary that is frozen alongside the generation request.
    require_project_reference_access(session, shot.project_id, result)
    return result


def verify_motion_sources(references):
    """Hash in a worker thread when called from asynchronous request handlers."""
    for ref in references:
        source=Path(ref['absolute_path'])
        if not source.is_file() or file_digest(source)!=ref['source_sha256']:
            raise AppError(409,'motion_reference_changed','动作参考原视频缺失或哈希变化，未提交生成')


def motion_live_state(session, values):
    """Cheap fingerprint data changes whenever selected source state changes."""
    result=[]
    for ref in values or []:
        identifier=ref.get('video_id') if isinstance(ref,dict) else None
        item=session.get(m.ReferenceVideo,identifier) if identifier else None
        result.append({'video_id':identifier,'revision':item.revision if item else None,
                       'sha256':item.sha256 if item else None,'state':item.state if item else 'missing'})
    return result


def active_motion_jobs(session, video_id):
    active={'submitting','queued','submitted','running','postprocessing','unknown','cancelling','reconciling'}
    return [job.id for job in session.scalars(select(m.GenerationJob).where(m.GenerationJob.status.in_(active)))
            if any(isinstance(ref,dict) and ref.get('video_id')==video_id for ref in (job.parameters_json or {}).get('motion_references',[]))]


def motion_bound_shots(session, video_id):
    return [detail.shot_id for detail in session.scalars(select(m.ShotDetail))
            if any(isinstance(ref,dict) and ref.get('video_id')==video_id for ref in
                   ((detail.source_json or {}).get('production') or {}).get('motion_references',[]))]


def prepare_motion_reference(settings, ref):
    """Make a checked 24fps source segment without changing its visual framing."""
    source=Path(ref['absolute_path'])
    if not source.is_file() or file_digest(source)!=ref['source_sha256']:
        raise AppError(409,'motion_reference_changed','动作参考原视频已变化，未提交生成')
    root=Path(settings.media_root)/'tmp';root.mkdir(parents=True,exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='motion-reference-',dir=root) as directory:
            target=Path(directory)/'reference.mp4'
            include_audio=bool(ref.get('include_audio'))
            duration=str(ref['effective_duration_seconds'])
            cmd=[settings.ffmpeg_path,'-nostdin','-v','error','-i',str(source),'-ss',str(ref['trim_start_seconds']),
                 '-t',duration,'-map','0:v:0','-vf','fps=24','-frames:v',str(ref['effective_frames']),
                 '-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p']
            if include_audio:
                cmd.extend(['-map','0:a:0?','-c:a','aac','-b:a','128k','-ac','2','-ar','48000'])
            else:
                cmd.append('-an')
            cmd.extend(['-movflags','+faststart',str(target)])
            run=subprocess.run(cmd,capture_output=True,timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if run.returncode:raise AppError(422,'motion_reference_transcode','动作参考转码失败，未提交生成')
            probe=subprocess.run([settings.ffprobe_path,'-v','error','-count_frames','-show_streams','-of','json',str(target)],capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if probe.returncode:raise AppError(422,'motion_reference_probe','动作参考转码后无法核验，未提交生成')
            streams=json.loads(probe.stdout).get('streams',[])
            video=[stream for stream in streams if stream.get('codec_type')=='video']
            audio=[stream for stream in streams if stream.get('codec_type')=='audio']
            if len(video)!=1 or int(video[0].get('nb_read_frames',0))!=ref['effective_frames'] or video[0].get('avg_frame_rate')!='24/1':
                raise AppError(422,'motion_reference_output_invalid','动作参考帧数、帧率或音轨不符合要求，未提交生成')
            if include_audio and len(audio)!=1:
                raise AppError(422,'motion_reference_audio_missing','已要求使用参考视频音频，但所选片段没有可用音轨')
            if not include_audio and audio:
                raise AppError(422,'motion_reference_audio_unexpected','未要求参考音频，但转码结果包含音轨')
            decode=[settings.ffmpeg_path,'-nostdin','-v','error','-i',str(target),'-map','0:v:0']
            if include_audio:
                decode.extend(['-map','0:a:0'])
            decode.extend(['-f','null','-'])
            checked=subprocess.run(decode,capture_output=True,timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if checked.returncode:
                raise AppError(422,'motion_reference_decode_failed','动作参考转码后无法完整解码，未提交生成')
            if file_digest(source)!=ref['source_sha256']:raise AppError(409,'motion_reference_changed','动作参考处理期间原视频变化，未提交生成')
            data=target.read_bytes()
            if not data or len(data)>64*1024**2:raise AppError(422,'motion_reference_output_size','动作参考转换文件为空或超过64MiB')
            return data,{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'frames':ref['effective_frames'],
                         'fps':24,'audio_included':include_audio,'width':video[0].get('width'),'height':video[0].get('height'),
                         'conversion':{'trim_start_seconds':ref['trim_start_seconds'],'duration_seconds':ref['effective_duration_seconds'],
                                       'fps':24,'video_codec':'libx264','audio_codec':'aac' if include_audio else None,
                                       'scale':'none','orientation':'ffmpeg default autorotate'}}
    except (OSError,subprocess.TimeoutExpired,ValueError,KeyError,TypeError) as exc:
        raise AppError(422,'motion_reference_processing_failed','动作参考处理失败，未提交生成') from exc
