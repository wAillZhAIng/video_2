"""Turn source-backed score instructions into timed assembly placements."""

import re
import unicodedata
from collections import defaultdict

from sqlalchemy import or_, select

from app.core.errors import AppError
from app.models import AudioEvent, ShotDetail, StoryboardShot
from app.services.word_duration import _INTERVAL_SEPARATOR, _seconds, _timecode


MUSIC_FIELDS = {'音乐', '背景音乐', '配乐', '背景配乐', 'bgm', 'music', 'background music', 'score'}
MIXED_MUSIC_FIELDS = {'音乐与音效', '背景音乐与音效', '背景音乐及音效', '配乐与音效'}
_SECTION = MUSIC_FIELDS | {
    '背景音乐要求', '背景音乐设计', '配乐要求', '配乐方案', '配乐设计', '音乐要求',
    '音乐设计', 'background music requirements', 'music cues', 'soundtrack',
}
_STYLE_FIELDS = _SECTION | {'音乐风格', '混音要求', '配乐风格', 'music style', 'mixing notes'}
_SHOT_COLUMNS = {'分镜区间', '镜头区间', '分镜', '镜头', 'shot range', 'shots'}
_TIME_COLUMNS = {'时段', '时间段', '起止时间', '时间码', '时间区间', '时间', 'time range', 'timecode', 'time'}
_START_COLUMNS = {'开始时间', '起始时间', 'start time'}
_END_COLUMNS = {'结束时间', 'end time'}
_SCORE_COLUMNS = MUSIC_FIELDS | {'音乐风格', '配乐说明', 'music style', 'music cue'}
_MIX_COLUMNS = {'混音', '混音要求', 'mix', 'mixing notes'}
_SHOTS = re.compile(
    r'^(?:第\s*|(?:shots?|分镜|镜头)\s*#?\s*)(\d+)\s*(?:镜(?:头)?\s*)?'
    r'(?:[-–—~至到]\s*(?:(?:第|shots?|分镜|镜头)\s*)?(\d+)\s*(?:镜(?:头)?)?)?$', re.I,
)
_SILENCE = re.compile(
    r'^(?:无(?:背景)?音乐|无配乐|无|不(?:使用|添加|加)背景音乐|静音|none|n/?a|'
    r'no (?:background )?music)(?:\s*[。.!！，,；;]|\s*$)', re.I,
)
_LOWER = re.compile(r'压低音乐|压低配乐|配乐压低|音乐压低|降低(?:音乐|配乐)|duck(?: the)? (?:music|score)', re.I)
_EFFECT = re.compile(r'气流声|环境声|底噪|摩擦声|摩擦\s*ASMR|音效|room tone|ambience|foley|sound effects?', re.I)
_MUSIC_HINT = re.compile(
    r'背景音乐|配乐|音乐|bgm|score|soundtrack|鼓点|节拍|旋律|电子|合成器|流行|拉丁|车库|house|pop|'
    r'jazz|lo[- ]?fi|hip[- ]?hop|edm|synth', re.I,
)


def time_bounds(value):
    parts = _INTERVAL_SEPARATOR.split(unicodedata.normalize('NFKC', value).strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    start, end = (_timecode(part) if ':' in part else _seconds(part) for part in parts)
    if start is None or end is None or not 0 <= start < end:
        return None
    return float(start), float(end)


def _score_table(block):
    """Recognize standalone score tables without treating rows as new shots."""
    allowed = _SCORE_COLUMNS | _SHOT_COLUMNS | _TIME_COLUMNS | _START_COLUMNS | _END_COLUMNS | _MIX_COLUMNS | {'', '序号'}
    for index, row in enumerate(block['rows']):
        headers = [unicodedata.normalize('NFKC', cell).strip().casefold() for cell in row]
        columns = set(headers)
        if columns <= allowed and columns & _SCORE_COLUMNS and (
                columns & (_SHOT_COLUMNS | _TIME_COLUMNS) or columns & _START_COLUMNS and columns & _END_COLUMNS):
            break
        if sum(bool(cell) for cell in headers) > 1:
            return None
    else:
        return None
    items = []
    for index, row in enumerate(block['rows'][index+1:], index+1):
        data = {key: text.strip() for key, text in zip(headers, row) if text.strip()}
        if not data:
            continue
        script_text = '\n'.join(text for key, text in data.items() if key in _SCORE_COLUMNS | _MIX_COLUMNS)
        shot_scope = next((text for key, text in data.items() if key in _SHOT_COLUMNS), '')
        time_scope = next((text for key, text in data.items() if key in _TIME_COLUMNS), '')
        start = next((text for key, text in data.items() if key in _START_COLUMNS), '')
        end = next((text for key, text in data.items() if key in _END_COLUMNS), '')
        if not time_scope and start and end:
            time_scope = start + '–' + end
        shot_scope = unicodedata.normalize('NFKC', shot_scope)
        match = _SHOTS.fullmatch(shot_scope) or _SHOTS.fullmatch('shot ' + shot_scope)
        bounds = time_bounds(time_scope)
        if not script_text or (not match and not bounds):
            return None
        scope = {'shot_range': [int(match[1]), int(match[2] or match[1])]} if match else {'time_range': list(bounds)}
        items.append({'description': script_text, 'source': [f"{block['id']}:row:{index}"], **scope})
    return items or None


def word_music_sections(blocks):
    """Extract score sections before the Word parser tries to identify shots."""
    retained, directives = [], []
    in_section = False
    for block in blocks:
        if block['kind'] != 'paragraph':
            cues = _score_table(block) if block['kind'] == 'table' else None
            if cues:
                directives.extend(cues)
                continue
            retained.append(block)
            in_section = False
            continue
        raw = block['text'].strip()
        body = re.sub(r'^(?:[#＃•-]+\s*|\d+[.、)）]\s*)', '', raw).strip()
        if unicodedata.normalize('NFKC', body).rstrip(': ').casefold() in _SECTION:
            in_section = True
            continue
        if not body:
            retained.append(block)
            continue
        # A range may contain timecode colons before the field separator.
        for delimiter in re.finditer('[:：]', body):
            label = unicodedata.normalize('NFKC', body[:delimiter.start()]).strip().strip('【】[] ')
            description = body[delimiter.end():].strip()
            field, _, scope = label.partition('(')
            explicitly_scoped = field.strip().casefold() in MUSIC_FIELDS and scope.endswith(')')
            match_label = scope[:-1].strip() if explicitly_scoped else label.strip('() ')
            shot_range = _SHOTS.fullmatch(match_label)
            bounds = time_bounds(match_label)
            if description and ((in_section and (shot_range or bounds or label.casefold() in _STYLE_FIELDS))
                                or (explicitly_scoped and (shot_range or bounds))):
                item = {'description': description, 'source': [block['id']]}
                if shot_range:
                    item['shot_range'] = [int(shot_range[1]), int(shot_range[2] or shot_range[1])]
                elif bounds:
                    item['time_range'] = list(bounds)
                directives.append(item)
                break
        else:
            in_section = False
            retained.append(block)
    return retained, directives


def music_effects(value):
    """Keep effects mentioned alongside music in the video model's sound brief."""
    effects = []
    for part in re.split(r'[。；，,;]|\n', value):
        if not _EFFECT.search(part) or _LOWER.search(part):
            continue
        if '同步淡出' in part and re.search('音乐|配乐', part):
            part = '环境声淡出' if '环境声' in part else part
        effects.append(part.strip())
    return '。'.join(effects)


def _inferred_audio_music(shots):
    """Promote explicit per-shot sound directions to one source-backed score cue.

    Text and simple Word imports often place music beside effects under
    ``声音提示`` instead of a dedicated score section.  This preserves those
    stated music styles without treating ordinary ambience as generated music.
    """
    directions, sources = [], []
    for shot in shots:
        raw = str(getattr(shot, 'audio_direction', '') or '').strip()
        if not raw or _SILENCE.match(raw) or not _MUSIC_HINT.search(raw):
            continue
        parts = [part.strip() for part in re.split(r'[。；，,;\n]+', raw) if part.strip()]
        music = [part for part in parts if _MUSIC_HINT.search(part)] or [raw]
        for part in music:
            if part not in directions:
                directions.append(part)
        sources.append(f'audio_direction:{shot.id}')
    if not directions:
        return []
    return [{'description': '背景音乐风格：'+'；'.join(directions), 'source': sources}]


def _window(start, end, event):
    left = max(start, start + event.start_seconds)
    right = end if event.end_seconds is None else min(end, start + event.end_seconds)
    return (left, right) if right > left else None


def _candidate(description, start, end, source, volume=1, local=False):
    return {'description': description, 'start': start, 'end': end, 'source': source,
            'volume': volume, 'local': local, 'silence': not volume or bool(_SILENCE.match(description.strip()))}


def _scoped_music(directives, shots, windows, start, end):
    defaults = [item for item in directives if not (item.get('time_range') or item.get('shot_range'))]
    cues = []
    if defaults:
        cues.append(_candidate('\n'.join(item['description'] for item in defaults), start, end,
                               [source for item in defaults for source in item.get('source', [])]))
    for directive in directives:
        bounds = directive.get('time_range')
        if 'shot_range' in directive:
            first, last = directive['shot_range']
            if not 1 <= first <= last <= len(shots):
                raise AppError(422, 'music_shot_range_invalid', '剧本配乐引用的镜头区间超出当前分镜范围')
            bounds = windows[shots[first-1].id][0], windows[shots[last-1].id][1]
        if bounds:
            if not start <= bounds[0] < bounds[1] <= end + 0.001:
                raise AppError(422, 'music_time_range_invalid', '剧本配乐的起止时间超出当前分镜时间轴')
            cues.append(_candidate(directive['description'], *bounds, directive.get('source', [])))
    return cues


def script_music_plan(session, project_id, clips, transitions):
    """Freeze source intervals and their positions on a trimmed/reordered edit."""
    from app.api.routes import latest_script, project_shot_statement

    script = latest_script(session, project_id)
    parsed = script.parsed_json if script else {}
    directives = parsed.get('project', {}).get('background_music')
    if directives is None:
        _, directives = word_music_sections(parsed.get('source_blocks', []))
    if not clips:
        return []
    shots = list(session.scalars(project_shot_statement(session, project_id).order_by(
        StoryboardShot.order, StoryboardShot.id)))
    # Manual assemblies can still select old approved versions. Apply global
    # directives only to shots that belong to the active source timeline.
    if not shots:
        return []
    if not directives:
        directives = _inferred_audio_music(shots)
    by_id = {shot.id: shot for shot in shots}
    details = dict(session.execute(select(ShotDetail.shot_id, ShotDetail.scene_id)
                                   .where(ShotDetail.shot_id.in_(by_id))).all())
    explicit_ids = parsed.get('explicit_shot_ids', [])
    explicit = dict(zip(explicit_ids, parsed.get('shots', [])))
    windows = {}
    offset = 0.0
    for shot in shots:
        bounds = time_bounds(explicit.get(shot.id, {}).get('time_range') or '')
        start, end = bounds or (offset, offset + shot.duration_seconds)
        windows[shot.id] = (start, end)
        offset = max(offset, end)
    timeline_start, timeline_end = min(start for start, _ in windows.values()), offset
    events = list(session.scalars(select(AudioEvent).where(AudioEvent.kind == 'music', or_(
        AudioEvent.shot_id.in_(by_id), AudioEvent.scene_id.in_({sid for sid in details.values() if sid}),
    )).order_by(AudioEvent.created_at, AudioEvent.id)))
    shot_events, scene_events = defaultdict(list), defaultdict(list)
    for event in events:
        if event.description.strip() or event.relative_path:
            (shot_events[event.shot_id] if event.shot_id else scene_events[event.scene_id]).append(event)
    global_cues = _scoped_music(directives, shots, windows, timeline_start, timeline_end)
    scene_windows = {}
    for shot in shots:
        scene_id = details.get(shot.id)
        start, end = windows[shot.id]
        prior = scene_windows.get(scene_id, (start, end))
        scene_windows[scene_id] = min(start, prior[0]), max(end, prior[1])
    scene_cues = defaultdict(list)
    for scene_id, group in scene_events.items():
        for event in group:
            bounds = _window(*scene_windows[scene_id], event)
            if bounds:
                scene_cues[scene_id].append(_candidate(event.description, *bounds, [event.id],
                                                      0 if event.relative_path else event.volume))

    # First, build the complete source arrangement, even for partial edits.
    arrangement = defaultdict(list)
    track_ids = {}
    tracks = []
    for shot in shots:
        start, end = windows[shot.id]
        cues = [*scene_cues[details.get(shot.id)], *global_cues]
        local_cues = []
        for event in shot_events[shot.id]:
            bounds = _window(start, end, event)
            if bounds:
                local_cues.append(_candidate(event.description, *bounds, [event.id],
                                             0 if event.relative_path else event.volume, local=True))
        candidates = [*cues, *local_cues]
        cuts = sorted({start, end, *(point for cue in candidates for point in
                                      (cue['start'], cue['end']) if start < point < end)})
        for left, right in zip(cuts, cuts[1:]):
            active = [cue for cue in candidates if cue['start'] <= left and right <= cue['end']]
            if not active or any(cue['silence'] for cue in active if cue['local']) or active[-1]['silence']:
                continue
            base = next((cue for cue in reversed(active) if not cue['local']), active[-1])
            if base['silence']:
                continue
            sections = [cue['description'] for cue in active if cue['local'] and cue is not base]
            key = (tuple(base['source']), base['start'], base['end'], base['description'], base['volume'])
            if key not in track_ids:
                track_ids[key] = len(tracks)
                tracks.append({
                    'description': base['description'], 'source': base['source'], 'origin': base['start'],
                    'duration': round(base['end'] - base['start'], 6), 'directions': [], 'placements': [],
                })
            track = tracks[track_ids[key]]
            volume = base['volume']
            if sections:
                track['directions'].append({'start': round(left-base['start'], 6),
                                            'end': round(right-base['start'], 6), 'text': '\n'.join(sections)})
                volume *= active[-1]['volume']
                if any(_LOWER.search(text) for text in sections):
                    volume *= 0.63
            elif _LOWER.search(base['description']):
                volume *= 0.63
            arrangement[shot.id].append((track, left-start, right-start, left-base['start'], volume))

    elapsed = 0.0
    for i, clip in enumerate(clips):
        left_overlap = transitions[i-1]['duration']/2 if i else 0
        overlap = transitions[i]['duration'] if i < len(transitions) else 0
        length, trim = clip['duration'], clip.get('trim_start', 0)
        shot_id = clip['shot_id']
        if shot_id in windows:
            scripted_start, scripted_end = windows[shot_id]
            source_length = (clip.get('full_stream_timing') or {}).get('duration') or clip.get('source', {}).get('duration') or length
            scripted_duration = scripted_end - scripted_start
            # A native model can render a few extra frames. If the editor has
            # explicitly trimmed those to the full script duration, keep the
            # score on the original clock, including continuity at the cut.
            trimmed_to_script = clip.get('trim_end') is not None and trim == 0 and abs(length-scripted_duration) < 0.001
            scale = 1 if trimmed_to_script else source_length / scripted_duration
            for track, start, end, offset, volume in arrangement[shot_id]:
                left, right = max(start*scale, trim+left_overlap), min(end*scale, trim+length-overlap/2)
                if right-left < 0.001:
                    continue
                track['placements'].append({
                    'start': round(elapsed+left-trim, 6), 'end': round(elapsed+right-trim, 6),
                    'offset': round(offset+left/scale-start, 6), 'tempo': round(1/scale, 8),
                    'volume': volume, 'shot_id': shot_id,
                })
        elapsed += length-overlap
    return [track for track in tracks if track['placements']]
