import hashlib
import json
from types import SimpleNamespace

from app.services.service_acceptance import accepted_evidence, binding
from app.services import service_acceptance


def test_local_acceptance_stays_bound_to_service_revision_and_artifact(tmp_path, monkeypatch):
    root = tmp_path / 'acceptance'
    root.mkdir()
    monkeypatch.setenv('VIDEO_FACTORY_SERVICE_ACCEPTANCE_DIR', str(root))
    service = SimpleNamespace(id='svc_local', revision=1, provider='ffmpeg', name='FFmpeg',
                              module='final_assembly', base_url='', settings_json={}, default_model='')
    artifact = root / 'receipt.json'
    artifact.write_bytes(b'accepted decoded output receipt')
    entry = {'slot': 'final_assembly', 'model_id': '',
             'binding': binding(None, service, '', 'final_assembly'),
             'proof': {'kind': 'decoded_assembly'}, 'relative_path': artifact.name,
             'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
    index = root / (service.id + '.json')

    def write_index():
        index.write_text(json.dumps({'schema_version': 1, 'entries': [entry]}), encoding='utf-8')

    write_index()
    assert accepted_evidence(None, service, '', 'final_assembly')['kind'] == 'decoded_assembly'
    service.revision = 2
    assert accepted_evidence(None, service, '', 'final_assembly') is None
    service.revision = 1
    artifact.write_bytes(b'changed artifact')
    assert accepted_evidence(None, service, '', 'final_assembly') is None
    elsewhere = tmp_path / 'outside.json'
    elsewhere.write_bytes(b'accepted decoded output receipt')
    entry['relative_path'] = '../outside.json'
    write_index()
    assert accepted_evidence(None, service, '', 'final_assembly') is None


def test_native_h3_acceptance_binding_tracks_fast_and_quality_workflows(tmp_path, monkeypatch):
    workflows = {}
    for steps in (4, 20, 30):
        path = tmp_path / f'h3-{steps}.json'
        path.write_text(f'{{"steps": {steps}, "revision": 1}}', encoding='utf-8')
        workflows[steps] = path
    monkeypatch.setattr(
        service_acceptance,
        'native_ref2va_workflow_path',
        lambda parameters: workflows[int(parameters['steps'])],
    )
    service = SimpleNamespace(
        id='svc_h3', revision=1, provider='comfyui', name='Native H3',
        module='video_generation', base_url='http://127.0.0.1:8189',
        settings_json={}, default_model='minimax_h3_ref2va_pruned_w6a8.safetensors',
    )

    initial = binding(None, service, service.default_model, 'video_generation')
    workflows[20].write_text('{"steps": 20, "revision": 2}', encoding='utf-8')
    changed = binding(None, service, service.default_model, 'video_generation')

    assert changed != initial
