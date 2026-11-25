import hashlib
import json
from types import SimpleNamespace

from app.services.service_acceptance import accepted_evidence, binding


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
