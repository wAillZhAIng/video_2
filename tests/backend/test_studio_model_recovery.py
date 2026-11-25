import asyncio
import copy
from hashlib import sha256
import json
import sys
from types import SimpleNamespace

import pytest

from app import models as m
from app.services import gpu_gate, studio_model_recovery as recovery
from app.services.cinematic_quality import generation_inputs_hash


PROMPT = 'afe0a3aa-0e97-4951-a777-f5653fbd9c55'


@pytest.mark.parametrize('after_restart', ['interrupted_no_output', 'output_preserved', 'receipt_missing'])
def test_repair_resumes_only_the_proven_interrupted_context_job(client, settings, monkeypatch, after_restart):
    with client.app.state.session_factory() as session:
        project = m.Project(name='Repair after model restart')
        session.add(project)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved')
        session.add(shot)
        session.flush()
        service = m.AIService(name='Context-IR', module='video_generation', enabled=True, provider='comfyui',
                              base_url=recovery.VAST_CONTEXT, default_model='h3')
        session.add(service)
        session.flush()
        frozen_service = {'service_id': service.id, 'service_revision': service.revision, 'base_url': service.base_url,
                          'provider': 'comfyui', 'is_cloud': False, 'model_id': 'h3', 'h3_workflow': {'quality_mode': 'fast'}}
        frozen = {'service': frozen_service, 'inputs': {'fingerprint': 'frozen-context'}}
        run = m.StudioRun(project_id=project.id, status='running',
                         snapshot_json={'options': {}, 'h3_context_by_shot': {shot.id: frozen}}, nodes_json=[])
        session.add(run)
        session.flush()
        node_key = 'video:' + shot.id
        parameters = {'studio_run_id': run.id, 'studio_node_id': node_key,
                      'service_snapshot': frozen_service, 'h3_workflow': frozen_service['h3_workflow'],
                      'context_inputs': frozen['inputs'], 'actual_workflow_sha256': 'b'*64,
                      'output_relative_dir': 'projects/repair/shot'}
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='comfyui_h3', status='running',
                              parameters_json=parameters, provider_job_id=PROMPT, prompt='retained prompt', seed=617)
        session.add(job)
        session.flush()
        old_result = {'id': 'saved-output', 'kind': 'video', 'status': 'succeeded', 'video_id': 'original'}
        run.nodes_json = [old_result, {'id': node_key, 'kind': 'video', 'target_id': shot.id,
            'status': 'running', 'child_id': job.id, 'attempts': 1, 'history': [],
            'inputs_hash': generation_inputs_hash(session, shot)}]
        identifiers = project.id, run.id, job.id, service.id, service.revision
        session.commit()
    project_id, run_id, job_id, service_id, revision = identifiers
    original_snapshot = copy.deepcopy(frozen)

    async def connections(_app, _session):
        return [{'service_id': service_id, 'revision': revision, 'status': 'recovered'}]

    async def owned_model_status():
        return {'state': 'idle', 'reason': 'health_idle'}

    async def receipt(origin, _profile, job):
        assert origin == recovery.VAST_CONTEXT and job.id == job_id
        if after_restart == 'receipt_missing':
            return None
        return {'job_id': job_id, 'prompt_id': PROMPT, 'outcome': after_restart}

    monkeypatch.setattr(recovery, 'recover_model_connections', connections)
    monkeypatch.setattr(gpu_gate, 'owned_model_status', owned_model_status)
    monkeypatch.setattr(recovery, '_restart_receipt', receipt)
    result = client.post(f'/api/studio/projects/{project_id}/repair')
    assert result.status_code == 200, result.text
    assert result.json()['model_connections'][0]['status'] == 'recovered'
    with client.app.state.session_factory() as session:
        run = session.get(m.StudioRun, run_id)
        job = session.get(m.GenerationJob, job_id)
        node = run.nodes_json[1]
        assert run.nodes_json[0] == old_result
        assert next(iter(run.snapshot_json['h3_context_by_shot'].values())) == original_snapshot
        assert job.provider_job_id == PROMPT and job.seed == 617
        if after_restart == 'interrupted_no_output':
            assert node['status'] == 'pending' and node['child_id'] is None
            assert node['history'][-1]['child_id'] == job.id
            assert job.status == 'failed' and job.error_code == 'context_ir_worker_restarted'
            assert result.json()['actions'][0]['code'] == 'model_connection_nodes_requeued'
        else:
            assert node['child_id'] == job.id and job.status == 'running'


def test_repair_probes_enabled_services_and_keeps_config_and_verified_capabilities(client, settings, monkeypatch):
    with client.app.state.session_factory() as session:
        project = m.Project(name='Check all models')
        rows = [m.AIService(id='qwen', name='Qwen', module='language_processing', provider='local_llm',
                            base_url=recovery.VAST_QWEN, enabled=True, default_model='qwen', revision=4,
                            capabilities_json={'other': {'state': 'verified'}}),
                m.AIService(id='tts', name='TTS', module='dialogue_generation', provider='local_runtime',
                            base_url='', enabled=True, default_model='tts'),
                m.AIService(id='disabled', name='Disabled', module='language_processing', provider='local_llm',
                            base_url='http://127.0.0.1:8888/v1', enabled=False, default_model='off')]
        session.add_all([project, *rows])
        session.flush()
        project_id = project.id
        session.add(m.StudioRun(project_id=project_id, status='paused', snapshot_json={'options': {}}, nodes_json=[]))
        session.commit()

    async def start(_app, services):
        assert {'qwen', 'tts'} <= {service.id for service in services}
        return {recovery.VAST_QWEN}

    async def probe(service, _settings):
        assert service.id != 'disabled'
        return {'status': 'unavailable' if service.id == 'tts' else 'connected',
                'models': [{'id': service.default_model}], 'capabilities': {'other': {'state': 'detected'}}}

    monkeypatch.setattr(recovery, '_recover_vast', start)
    monkeypatch.setattr(recovery, 'probe_service', probe)
    settings.h3_demo_mode = False
    result = client.post(f'/api/studio/projects/{project_id}/repair').json()
    states = {row['service_id']: row['status'] for row in result['model_connections']}
    assert states['qwen'] == 'recovered' and states['tts'] == 'unavailable' and 'disabled' not in states
    assert result['run_status'] == 'paused'
    with client.app.state.session_factory() as session:
        qwen = session.get(m.AIService, 'qwen')
        assert qwen.revision == 4 and qwen.default_model == 'qwen' and qwen.base_url == recovery.VAST_QWEN
        assert qwen.last_test_json['status'] == 'connected' and qwen.capabilities_json['other']['state'] == 'verified'


@pytest.mark.skipif(sys.platform != 'linux', reason='Vast runtime recovery takes the real Linux Qwen file lock')
@pytest.mark.parametrize('blocker', [None, 'worker', 'peer', 'qwen', 'foreign'])
def test_only_a_stale_owned_idle_vast_gateway_is_restarted(tmp_path, monkeypatch, blocker):
    runtime_root = tmp_path / 'native'
    (runtime_root / 'cache/qwen').mkdir(parents=True)
    manifest = tmp_path / 'runtime-manifests/context-ir-native-v1.json'
    manifest.parent.mkdir()
    manifest.write_bytes(b'verified new source manifest')
    checksum = sha256(manifest.read_bytes()).hexdigest()
    (runtime_root / 'native-runtime.json').write_text(json.dumps({
        'profile_id': 'local-h3ir-qwen4b-v3', 'runtime_root': str(runtime_root),
        'comfy_origin': recovery.VAST_CONTEXT_WORKER, 'peer_comfy_origin': recovery.VAST_COMFY,
        'source_manifest_sha256': checksum,
    }))
    healthy = {'pid': 617, 'busy': blocker == 'qwen', 'loaded': False, 'inference_phase': 'idle'}
    stale = {'schema_version': 1, 'service': 'video-factory-native-context-runtime-v1',
             'profile_id': 'local-h3ir-qwen4b-v3', 'ready': False, 'busy': False, 'watcher_running': True,
             'source_baseline': {'ok': False, 'manifest_sha256': checksum},
             'installation_check': {'ready': True, 'manifest_sha256': 'a' * 64},
             'active_prompts': [], 'qwen_health': healthy,
             'checks': [{'kind': 'qwen_owned_process', 'ok': True}]}
    calls = []
    endpoints = {recovery.VAST_CONTEXT_WORKER + '/queue': 'worker', recovery.VAST_COMFY + '/queue': 'peer'}

    async def status(_client, url):
        if url in endpoints:
            return {'queue_running': ['active'] if endpoints[url] == blocker else [], 'queue_pending': []}, False
        if url == recovery.VAST_QWEN.removesuffix('/v1') + '/health':
            return healthy, False
        assert url == recovery.VAST_CONTEXT + recovery.CONTEXT_RUNTIME
        return ({**stale, 'ready': True, 'memory_policy': 'runtime_owned',
                 'source_baseline': {'ok': True, 'manifest_sha256': checksum}} if 'restart' in calls else stale), False

    def supervisor(action, program):
        assert program == 'ai-video-context'
        calls.append(action)
        return SimpleNamespace(returncode=0, stdout='311\n')

    monkeypatch.setattr(recovery, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(recovery, 'VAST_NATIVE_ROOT', runtime_root)
    monkeypatch.setattr(recovery, '_read_status', status)
    monkeypatch.setattr(recovery, '_supervisor', supervisor)
    monkeypatch.setattr(recovery.os, 'getpgid', lambda pid: 444 if blocker == 'foreign' else 311)
    assert asyncio.run(recovery._refresh_stale_vast_runtime(None, stale)) is (blocker is None)
    assert ('restart' in calls) is (blocker is None)
