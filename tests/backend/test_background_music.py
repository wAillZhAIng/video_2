import array
import asyncio
import hashlib
import math
import shutil
import subprocess
from dataclasses import replace
from io import BytesIO

import pytest
from docx import Document
from sqlalchemy.orm import Session

from app.core.db import Base, build_engine
from app.models import AudioEvent, Project, ScriptRevision, StoryboardShot
from app.services.assembly import command_for, probe
from app.services import background_music
from app.services.background_music import clip_music_plan
from app.services.cinematic_import import build_ir
from app.services.music_cues import script_music_plan
from app.services.word_screenplay import parse_blocks, read_blocks


def test_music_word_section_is_independent_of_shot_and_effect_parsing():
    doc = Document()
    for line in (
        '背景音乐要求', '音乐风格：温暖电钢琴，约 88 BPM', '混音要求：全片同一条纯器乐',
        '第 2 镜至第 3 镜：改用轻打击乐', '00:09–00:11：无配乐',
        '场景 1 夜景', '分镜时间轴', '00:00–00:02', '画面：车辆驶入',
        '背景音乐与音效：电钢琴进入。保留车内气流声。',
        '00:02–00:05', '画面：镜头推近', '配乐：加一点鼓',
    ):
        doc.add_paragraph(line)
    handle = BytesIO()
    doc.save(handle)
    saved = build_ir(read_blocks(handle.getvalue(), 1_000_000), parse_blocks)
    music = saved['project']['background_music']
    assert len(music) == 4
    assert music[2]['shot_range'] == [2, 3]
    assert music[3]['time_range'] == [9, 11]
    assert [shot['duration_seconds'] for shot in saved['shots']] == [2, 3]
    assert [event['kind'] for event in saved['shots'][0]['audio_events']] == ['music', 'sfx']
    assert saved['shots'][0]['audio_events'][1]['description'] == '保留车内气流声'
    assert all(item['source'] for item in music)


def test_standalone_music_table_does_not_create_additional_shots():
    doc = Document()
    doc.add_paragraph('场景 1 夜景')
    doc.add_paragraph('分镜时间轴')
    doc.add_paragraph('00:00–00:03')
    doc.add_paragraph('画面：环境夜景')
    doc.add_paragraph('00:03–00:06')
    doc.add_paragraph('画面：杯身特写')
    table = doc.add_table(rows=3, cols=3)
    for row, values in zip(table.rows, (
        ('开始时间', '结束时间', '背景音乐'),
        ('00:00', '00:03', '低音合成器'),
        ('00:03', '00:06', '电钢琴'),
    )):
        for cell, value in zip(row.cells, values):
            cell.text = value
    handle = BytesIO()
    doc.save(handle)
    saved = build_ir(read_blocks(handle.getvalue(), 1_000_000), parse_blocks)
    assert len(saved['shots']) == 2
    assert [(d['time_range'], d['description']) for d in saved['project']['background_music']] == [
        ([0, 3], '低音合成器'), ([3, 6], '电钢琴'),
    ]


def test_background_music_checks_configured_context_queue_instead_of_legacy_port(settings, monkeypatch):
    app_settings = replace(settings, comfyui_base_url='http://127.0.0.1:18188',
                           h3_context_comfyui_url='http://127.0.0.1:8188')
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    from app.models import AIService

    with Session(engine) as session:
        session.add(AIService(module='video_generation', provider='comfyui', name='Context-IR',
                              base_url='http://127.0.0.1:8788', settings_json={'h3_context_ir': True}))
        session.flush()
        peers = background_music._configured_gpu_peers(session, app_settings)
    origins = []

    async def queue(_settings, selected, route):
        assert route == '/queue'
        origins.append(selected['base_url'])
        return {'queue_running': [], 'queue_pending': []}

    monkeypatch.setattr(background_music, '_json', queue)
    assert asyncio.run(background_music._idle(
        app_settings, {'base_url': 'http://127.0.0.1:18188', 'gpu_peer_origins': peers}))
    assert origins == ['http://127.0.0.1:18188', 'http://127.0.0.1:8788']


def test_arrangement_keeps_one_track_and_only_changes_explicit_ranges(settings):
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Project(id='project', name='test'))
        session.add(ScriptRevision(project_id='project', revision=1, raw_text='', parsed_json={
            'project': {'background_music': [
                {'description': '电钢琴'}, {'description': '合成器', 'shot_range': [3, 3]},
                {'description': '无配乐', 'time_range': [9, 11]},
            ]},
            'explicit_shot_ids': ['s1', 's2', 's3', 's4'],
            'shots': [{'time_range': t} for t in ('0–2', '2–5', '5–8', '8–11')],
        }))
        for index, duration in enumerate((2, 3, 3, 3), 1):
            session.add(StoryboardShot(id='s'+str(index), project_id='project', order=index,
                                        duration_seconds=duration))
        session.add(AudioEvent(shot_id='s2', kind='music', description='摩擦时压低音乐'))
        session.commit()

        clips = [{'shot_id': 's'+str(i), 'duration': d} for i, d in enumerate((2, 3, 3, 3), 1)]
        plan = script_music_plan(session, 'project', clips, [{'duration': 0}] * 3)
        piano, synth = plan
        assert (piano['description'], synth['description']) == ('电钢琴', '合成器')
        assert [(p['start'], p['end'], p['offset']) for p in piano['placements']] == [
            (0, 2, 0), (2, 5, 2), (8, 9, 8),
        ]
        assert piano['placements'][1]['volume'] == 0.63
        assert piano['directions'] == [{'start': 2, 'end': 5, 'text': '摩擦时压低音乐'}]
        assert [(p['start'], p['end'], p['offset']) for p in synth['placements']] == [(5, 8, 0)]
        preview = clip_music_plan(plan, 4, 2)
        assert preview[0]['placements'][0]['offset'] == 4
        assert preview[1]['placements'][0]['start'] == 1
        clips[0].update(trim_end=2, source={'duration': 2.333333})
        trimmed = script_music_plan(session, 'project', clips, [{'duration': 0}] * 3)
        assert trimmed[0]['placements'][0]['tempo'] == 1
        assert trimmed[0]['placements'][1]['offset'] == 2
        current = session.query(ScriptRevision).one()
        current.parsed_json = {**current.parsed_json, 'project': {'background_music': []}}
        for clip in clips:
            identifier = clip['shot_id']
            session.add(AudioEvent(shot_id=identifier, kind='music', description='器乐 '+identifier))
        session.flush()
        per_shot = script_music_plan(session, 'project', clips, [{'duration': 0}] * 3)
        assert [cue['description'] for cue in per_shot] == ['器乐 s1', '器乐 s2', '器乐 s3', '器乐 s4']


def test_ffmpeg_places_each_real_audio_input_without_replacing_original_sound(settings):
    if not (shutil.which(settings.ffmpeg_path) and shutil.which(settings.ffprobe_path)):
        pytest.skip('requires FFmpeg and FFprobe')
    settings.media_root.mkdir(parents=True)
    for hz, filename, duration in ((330, 'clip.mp4', 3), (550, 'first.flac', 1), (770, 'second.flac', 1)):
        cmd = [settings.ffmpeg_path, '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency={hz}:sample_rate=48000']
        if filename.endswith('.mp4'):
            cmd += ['-f', 'lavfi', '-i', 'color=size=160x128:rate=24', '-c:v', 'libx264', '-pix_fmt', 'yuv420p']
        subprocess.run([*cmd, '-t', str(duration), str(settings.media_root / filename)], check=True)

    def checksum(filename):
        return hashlib.sha256((settings.media_root / filename).read_bytes()).hexdigest()

    plan = [{'placements': [{'start': start, 'end': start+1, 'offset': 0, 'tempo': 1, 'volume': 1}]}
            for start in (0, 2)]
    snapshot = {
        'clips': [{'relative_path': 'clip.mp4', 'sha256': checksum('clip.mp4'), 'duration': 3, 'source': {'has_audio': True}}],
        'transitions': [], 'output': {'width': 160, 'height': 128, 'fps': 24, 'codec': 'libx264', 'audio_policy': 'preserve'},
        'service_snapshot': {'settings': {}}, 'background_music': plan,
        'music_service_snapshot': {'settings': {'music_gain_db': -6}}, 'duration': 3,
    }
    assets = [{'relative_path': name, 'sha256': checksum(name)} for name in ('first.flac', 'second.flac')]
    output = settings.media_root / 'mixed.mp4'
    subprocess.run(command_for(settings, snapshot, output, assets), check=True, capture_output=True)
    assert probe(output, settings)['duration'] == pytest.approx(3, abs=0.03)
    pcm = subprocess.check_output([settings.ffmpeg_path, '-v', 'error', '-i', str(output), '-vn',
                                   '-f', 's16le', '-ar', '48000', '-ac', '1', '-'])
    samples = array.array('h', pcm)

    def amplitude(frequency, time):
        start = round(time*48000)
        window = samples[start:start+4800]
        return abs(sum(value * complex(math.cos(angle), math.sin(angle)) for j, value in enumerate(window)
                       for angle in [2*math.pi*frequency*j/48000])) / len(window)

    assert all(amplitude(330, time) > 750 for time in (0.4, 1.4, 2.4))
    assert amplitude(550, 0.4) > 8*amplitude(550, 1.4)
    assert amplitude(770, 2.4) > 8*amplitude(770, 0.4)
