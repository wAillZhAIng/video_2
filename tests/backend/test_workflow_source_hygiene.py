"""Public native Ref2VA workflow hygiene; no model is loaded here."""
import json
import re
from pathlib import Path

import pytest


WORKFLOWS = Path(__file__).resolve().parents[2] / 'workflows'
PRIVATE_PATH = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]')
PRIVATE_ID = re.compile(r'\b(?:prj|job|shot|spjob|run|prod)_[0-9a-f]{16,}\b', re.IGNORECASE)


def walk(value, location='$'):
    if isinstance(value, dict):
        for key, child in value.items():
            yield location, key
            yield from walk(child, location + '/' + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, location + '/' + str(index))
    elif isinstance(value, str):
        yield location, value


@pytest.mark.parametrize('path', sorted(WORKFLOWS.glob('h3_ref2va_native_*_v2.json')), ids=lambda path: path.name)
def test_native_ref2va_workflows_have_no_private_paths_or_retired_compiler_nodes(path):
    document = json.loads(path.read_text('utf-8-sig'))
    serialized = json.dumps(document, ensure_ascii=False)
    for location, value in walk(document):
        assert not PRIVATE_PATH.search(value), f'{path.name}: private drive path at {location}'
        assert not PRIVATE_ID.search(value), f'{path.name}: private project/task ID at {location}'
    retired_markers = ('H3IR', 'Context' + '-IR')
    assert all(marker not in serialized for marker in retired_markers)
    assert 'MiniMaxH3ReferenceToVideo' in serialized


@pytest.mark.parametrize(('filename', 'steps'), [
    ('h3_ref2va_native_fast4_v2.json', 4),
    ('h3_ref2va_native_quality20_v2.json', 20),
    ('h3_ref2va_native_quality30_v2.json', 30),
])
def test_native_ref2va_workflows_declare_real_video_conditioning_contract(filename, steps):
    document = json.loads((WORKFLOWS / filename).read_text('utf-8'))
    metadata = document['_video_factory']
    prompt = document['prompt']

    assert metadata['mode'] == 'reference_to_video'
    assert metadata['workflow_profile'] == 'native_ref2va'
    assert metadata['sampling_contract']['steps'] == steps
    assert metadata['reference_video_contract'] == {
        'loader_class': 'LoadVideo',
        'components_class': 'GetVideoComponents',
        'video_input_prefix': 'ref_videos.ref_video_',
        'audio_input_prefix': 'ref_video_audios.ref_video_audio_',
        'required_fps': 24,
        'max_images': 9,
        'max_videos': 3,
    }
    target = next(node for node in prompt.values() if node['class_type'] == 'MiniMaxH3ReferenceToVideo')
    assert target['inputs']['vae'] == ['119', 0]
    assert target['inputs']['audio_vae'] == ['120', 0]
