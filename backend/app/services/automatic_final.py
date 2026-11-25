"""Translate the frozen screenplay timeline into an unattended FFmpeg export."""

from copy import deepcopy
import math
import re

from app.core.errors import AppError
from app.services.video_quality import SIZES


TRANSITION_NAMES = {
    'cut':'cut', '硬切':'cut', '直切':'cut', 'crossfade':'crossfade', '交叉淡化':'crossfade',
    'dissolve':'dissolve', '叠化':'dissolve', 'fadeblack':'fadeblack', '淡黑':'fadeblack',
    'fadewhite':'fadewhite', '淡白':'fadewhite', 'slide':'slide', '滑动':'slide',
    'push':'push', '推入':'push', 'wipe':'wipe', '擦除':'wipe',
}


def _transition(value):
    text=str(value).strip().lower() if not isinstance(value,dict) else str(value.get('type','')).strip().lower()
    name=re.split(r'[\s：:（(,，]',text,maxsplit=1)[0]
    kind=TRANSITION_NAMES.get(name,'cut')
    if kind=='cut':
        return {'type':'cut','duration':0,'audio':'cut'}
    match=re.search(r'(\d+(?:\.\d+)?)\s*(?:秒|s\b)',text)
    specified=value.get('duration',.25) if isinstance(value,dict) else match[1] if match else .25
    try:
        duration=float(specified)
    except (TypeError,ValueError):
        duration=.25
    if not 0<duration<=10:
        return {'type':'cut','duration':0,'audio':'cut'}
    return {'type':kind,'duration':duration,'audio':'crossfade'}


def export_target(script, shots, transition_values=None):
    project = (script.parsed_json or {}).get('project', {}) if script else {}
    constraints = project.get('constraints') or []
    lines = constraints.splitlines() if isinstance(constraints, str) else constraints
    specifications = '\n'.join(line for line in lines if isinstance(line, str) and re.match(
        r'^\s*(?:技术参数|输出参数|导出规格|画质参数|Technical specifications|Export settings)\s*[:：]',
        line, re.I))
    resolution = re.search(r'\b(720p|1080p|2k|4k)\b', specifications, re.I | re.ASCII)
    frame_rate = re.search(r'(\d{1,3})\s*(?:fps\b|帧\s*/?\s*秒)', specifications, re.I | re.ASCII)
    fps = int(frame_rate[1]) if frame_rate else None
    if fps is not None and not 12 <= fps <= 60:
        raise AppError(422, 'automatic_final_fps', '剧本指定的成片帧率超出 12–60 fps 导出范围')
    ratio = project.get('aspect_ratio')
    if ratio not in {'16:9', '9:16', '1:1'}:
        ratio = shots[0].aspect_ratio
    transitions=[_transition(value) for value in (transition_values or [])]
    return {'aspect_ratio': ratio, 'resolution': resolution[1].lower() if resolution else None,
            'fps': fps, 'subtitles': False, 'source': 'screenplay' if specifications else 'source_clips',
            'shot_durations': {shot.id: shot.duration_seconds for shot in shots},
            'duration_policy': 'use_storyboard_duration', 'unspecified_transition': 'cut',
            'transitions':transitions}


def preserve_native_speech(target, context_by_shot):
    """Derive export policy without changing the frozen storyboard snapshot.

    H3 rounds requested durations to its legal frame grid. Cutting a native
    speech take back to the nominal storyboard duration can remove its final
    word even though the source and SeedVR2 output both contain the full line.
    """
    result=deepcopy(target)
    preserved=[]
    for shot_id in target.get('shot_durations',{}):
        inputs=(context_by_shot.get(shot_id) or {}).get('inputs') or {}
        if inputs.get('audio_policy')!='native':continue
        snapshot=inputs.get('screenplay_snapshot') or {}
        spoken=any(isinstance(row.get('text'),str) and row['text'].strip() for row in snapshot.get('dialogues',[]))
        group=any(row.get('kind')=='voice' and str(row.get('description')or'').strip()
                  for row in snapshot.get('audio_events',[]))
        if spoken or group:preserved.append(shot_id)
    if preserved:
        result.update(preserve_native_speech_shots=preserved,
            duration_policy='preserve_complete_native_speech_takes',
            requested_storyboard_duration=sum(target['shot_durations'].values()))
    return result


def timeline_parameters(target, videos, probes):
    fps = target.get('fps') or max(24, math.ceil(max(probe['fps'] for probe in probes) - 1e-6))
    if fps > 60:
        raise AppError(422, 'automatic_final_fps', '源片帧率超过自动成片支持的 60 fps')
    output = {'resolution_mode': 'source', 'fps': fps, 'codec': 'libx264',
              'audio_policy': 'preserve', 'subtitles': False}
    resolution = target.get('resolution')
    if resolution:
        width, height = SIZES[resolution]
        if target['aspect_ratio'] == '9:16':
            width, height = height, width
        elif target['aspect_ratio'] == '1:1':
            width = height
        output.update(resolution_mode='custom', width=width, height=height,
                      allow_downscale=True)
    transitions=list(target.get('transitions') or [_transition(None) for _ in videos[1:]])
    clips = []
    preserve=set(target.get('preserve_native_speech_shots') or [])
    for index, (video, source) in enumerate(zip(videos, probes), 1):
        seconds = target['shot_durations'][video.shot_id]
        end = round(seconds * fps) / fps
        if end < 1 / fps or source['duration'] + 0.001 < end:
            raise AppError(409, 'automatic_final_short_clip',
                f'第{index}镜有效视频只有 {source["duration"]:.3f} 秒，剧本需要 {end:.3f} 秒。请修复本镜素材后重试。')
        if index<=len(transitions):
            # Use extra real tail frames for overlaps, keeping the screenplay's
            # timeline and music cues on the same clock.
            edge=transitions[index-1]
            overlap=round(edge['duration']*fps)/fps
            next_length=target['shot_durations'][videos[index].shot_id]
            if video.shot_id in preserve:
                # Full takes remain full takes across caller-selected visual
                # transitions. Do not spend their speech tail as trim padding.
                if overlap and not 2/fps<=overlap<min(source['duration'],next_length)-1/fps:
                    transitions[index-1]=_transition(None)
            elif end+overlap<=source['duration']+.001 and (not overlap or 2/fps<=overlap<min(end,next_length)-1/fps):
                end+=overlap
            else:
                transitions[index-1]=_transition(None)
        clips.append({'video_id': video.id, 'trim_start': 0,
                      'trim_end':None if video.shot_id in preserve else end})
    return clips, transitions, output
