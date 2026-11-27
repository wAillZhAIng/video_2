from pathlib import Path
import subprocess

import pytest

from app.services import reference_video_slicer as slicer


@pytest.fixture(autouse=True)
def clear_slicer_cache():
    slicer._MEMORY_CACHE.clear()


def test_auto_slice_uses_the_matching_scene_and_stays_inside_selected_range(monkeypatch):
    monkeypatch.setattr(slicer, 'detect_video_shots', lambda *_: [(0, 4), (4, 10), (10, 18)])

    start, end, receipt = slicer.auto_slice_range_for_shot(
        ffmpeg_path='ffmpeg', source_path=Path('reference.mp4'), source_duration=18,
        sha256='a' * 64, trim_start=2, trim_end=16, shot_index=1,
        shot_duration=3, all_shot_durations=[3, 3, 3],
    )

    assert (start, end) == (7.15, 9.85)
    assert receipt['strategy'] == 'ffmpeg_scene_cut_aligned'
    assert receipt['original_range'] == [2, 16]
    assert receipt['sliced_range'] == [start, end]


def test_auto_slice_keeps_short_or_single_shot_selections(monkeypatch):
    def unexpected_detection(*_):
        raise AssertionError('short and single-shot references should not be scanned')

    monkeypatch.setattr(slicer, 'detect_video_shots', unexpected_detection)
    common = dict(ffmpeg_path='ffmpeg', source_path=Path('reference.mp4'), source_duration=20,
                 sha256='b' * 64, shot_index=0, shot_duration=3)

    assert slicer.auto_slice_range_for_shot(**common, trim_start=0, trim_end=4, all_shot_durations=[3, 3])[:2] == (0, 4)
    assert slicer.auto_slice_range_for_shot(**common, trim_start=0, trim_end=20, all_shot_durations=[3])[:2] == (0, 20)


def test_scene_detection_falls_back_when_ffmpeg_fails(monkeypatch, tmp_path):
    source = tmp_path / 'reference.mp4'
    source.write_bytes(b'fixture')
    monkeypatch.setattr(slicer, '_CACHE_DIR', tmp_path / 'cache')
    calls = []

    def failed_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, stdout='', stderr='pts_time:4.0')

    monkeypatch.setattr(slicer.subprocess, 'run', failed_run)
    result = slicer.detect_video_shots('ffmpeg', source, 12, 'c' * 64)

    assert result == [(0.0, 12)]
    assert calls[0][1]['timeout'] == slicer.DETECT_TIMEOUT_SECONDS
    assert not (tmp_path / 'cache').exists()


def test_scene_detection_cache_is_scoped_to_source_duration(monkeypatch, tmp_path):
    source = tmp_path / 'reference.mp4'
    source.write_bytes(b'fixture')
    monkeypatch.setattr(slicer, '_CACHE_DIR', tmp_path / 'cache')
    calls = []

    def successful_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='pts_time:5.0')

    monkeypatch.setattr(slicer.subprocess, 'run', successful_run)

    assert slicer.detect_video_shots('ffmpeg', source, 8, 'd' * 64) == [(0, 5), (5, 8)]
    assert slicer.detect_video_shots('ffmpeg', source, 6, 'd' * 64) == [(0, 5), (5, 6)]
    assert len(calls) == 2
