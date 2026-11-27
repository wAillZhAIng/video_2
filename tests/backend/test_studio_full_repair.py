"""Comprehensive tests for universal one-click repair of all stuck/bug states."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import models as m
from app.models.studio import StudioRun


def _patch_idle_and_comfy(monkeypatch, *, active_prompts=None, histories=None, reachable=True, advance_calls=None):
    from app.services import gpu_gate, studio_full_repair, studio_model_recovery, studio_pipeline

    async def idle():
        return {'state': 'idle', 'reason': 'health_idle'}

    async def fake_connections(_app, _session):
        return []

    async def fake_comfy(base_urls, prompt_probes):
        queues = {}
        for u in base_urls:
            base = u.rstrip('/')
            act = set(active_prompts or ())
            queues[base] = {'reachable': reachable, 'active_prompt_ids': act, 'queue_empty': len(act) == 0}
        return {'queues': queues, 'histories': dict(histories or {})}

    monkeypatch.setattr(gpu_gate, 'owned_model_status', idle)
    monkeypatch.setattr(studio_model_recovery, 'recover_model_connections', fake_connections)
    monkeypatch.setattr(studio_full_repair, 'inspect_comfy_state', fake_comfy)
    monkeypatch.setattr(studio_full_repair, 'has_active_upscale_process', lambda: False)
    monkeypatch.setattr(studio_full_repair, 'has_active_assembly_ffmpeg', lambda: False)

    if advance_calls is not None:
        async def fake_advance(_app, _settings, _session, run):
            advance_calls.append((run.id, run.status, [dict(n) for n in run.nodes_json]))
        monkeypatch.setattr(studio_pipeline, 'advance_pipeline', fake_advance)


def test_repair_recovers_submission_outcome_unknown_video_and_unblocks_deps(client, monkeypatch):
    """Exact reproduction of user's stuck shot_59024... submission_outcome_unknown case."""
    advance_calls = []
    _patch_idle_and_comfy(monkeypatch, advance_calls=advance_calls)

    with client.app.state.session_factory() as session:
        project = m.Project(name='未知提交恢复测试')
        session.add(project)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved')
        session.add(shot)
        session.flush()
        job = m.GenerationJob(
            project_id=project.id,
            shot_id=shot.id,
            provider='comfyui_h3',
            provider_job_id=None,
            status='unknown',
            error_code='submission_outcome_unknown',
            error_message='服务在提交期间中断，结果未知；核对供应商原请求后处理，不自动重复提交',
            prompt='test prompt',
            seed=123,
            parameters_json={'service_snapshot': {'provider': 'comfyui', 'is_cloud': False, 'base_url': 'http://127.0.0.1:18188'}},
        )
        session.add(job)
        session.flush()
        run = StudioRun(
            project_id=project.id,
            status='queued',
            snapshot_json={'shot_ids': [shot.id], 'options': {'allow_cloud_costs': False}, 'services': {'video': {'provider': 'comfyui', 'is_cloud': False, 'base_url': 'http://127.0.0.1:18188'}}},
            nodes_json=[
                {'id': f'video:{shot.id}', 'kind': 'video', 'target_id': shot.id, 'dependencies': [], 'status': 'unknown',
                 'code': 'submission_outcome_unknown', 'error': job.error_message, 'child_id': job.id, 'attempts': 1, 'history': []},
                {'id': f'quality:{shot.id}', 'kind': 'quality', 'target_id': shot.id, 'dependencies': [f'video:{shot.id}'],
                 'status': 'waiting_dependencies', 'code': None, 'error': None, 'child_id': None, 'attempts': 0, 'history': []},
            ],
        )
        session.add(run)
        session.commit()
        project_id, run_id, job_id = project.id, run.id, job.id

    # Works even with empty {} body (and with force_recover: True)
    resp = client.post(f'/api/studio/projects/{project_id}/repair', json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert any(a['code'] == 'full_repair_nodes_requeued' and a['status'] == 'applied' for a in data['actions'])
    assert not any(b['code'] == 'submission_outcome_unknown' for b in data['blockers'])
    assert advance_calls and advance_calls[0][0] == run_id

    with client.app.state.session_factory() as session:
        refreshed_job = session.get(m.GenerationJob, job_id)
        assert refreshed_job.status == 'closed_unknown'
        refreshed_run = session.get(StudioRun, run_id)
        vnode = next(n for n in refreshed_run.nodes_json if n['kind'] == 'video')
        assert vnode['status'] == 'pending' and vnode['child_id'] is None and vnode['code'] is None
        assert vnode['history'][-1]['action'] == 'one_click_full_repair'


def test_repair_resumes_live_comfyui_prompt_instead_of_duplicating(client, monkeypatch):
    advance_calls = []
    _patch_idle_and_comfy(monkeypatch, active_prompts={'live-prompt-123'}, advance_calls=advance_calls)

    with client.app.state.session_factory() as session:
        project = m.Project(name='活任务接回测试')
        session.add(project)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved')
        session.add(shot)
        session.flush()
        job = m.GenerationJob(
            project_id=project.id,
            shot_id=shot.id,
            provider='comfyui_h3',
            provider_job_id='live-prompt-123',
            status='unknown',
            error_code='provider_status_unknown',
            error_message='poll failed transiently',
            prompt='p',
            seed=1,
            parameters_json={'service_snapshot': {'provider': 'comfyui', 'is_cloud': False, 'base_url': 'http://127.0.0.1:18188'}},
        )
        session.add(job)
        session.flush()
        run = StudioRun(
            project_id=project.id,
            status='running',
            snapshot_json={'shot_ids': [shot.id], 'options': {'allow_cloud_costs': False}, 'services': {'video': {'provider': 'comfyui', 'is_cloud': False, 'base_url': 'http://127.0.0.1:18188'}}},
            nodes_json=[
                {'id': f'video:{shot.id}', 'kind': 'video', 'target_id': shot.id, 'dependencies': [], 'status': 'unknown',
                 'code': 'provider_status_unknown', 'error': 'poll failed', 'child_id': job.id, 'attempts': 1, 'history': []},
            ],
        )
        session.add(run)
        session.commit()
        project_id, run_id, job_id = project.id, run.id, job.id

    resp = client.post(f'/api/studio/projects/{project_id}/repair', json={'force_recover': True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert any(a['code'] == 'full_repair_live_tasks_resumed' and a['status'] == 'applied' for a in data['actions'])
    with client.app.state.session_factory() as session:
        refreshed_job = session.get(m.GenerationJob, job_id)
        assert refreshed_job.status == 'running' and refreshed_job.provider_job_id == 'live-prompt-123'
        vnode = session.get(StudioRun, run_id).nodes_json[0]
        assert vnode['status'] == 'running' and vnode['child_id'] == job_id


def test_force_recover_clears_unknown_quality_media_visual_character_paused_and_waiting(client, monkeypatch):
    advance_calls = []
    _patch_idle_and_comfy(monkeypatch, advance_calls=advance_calls)

    with client.app.state.session_factory() as session:
        project = m.Project(name='全节点卡死一键修复测试')
        session.add(project)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved')
        session.add(shot)
        session.flush()
        img_svc = m.AIService(
            module='character_generation', name='local comfy img', provider='comfyui',
            base_url='http://127.0.0.1:18188', enabled=True, default_model='flux-2-klein-4b-fp8.safetensors',
        )
        session.add(img_svc)
        src_job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='comfyui_h3', status='generated', prompt='p', seed=1, parameters_json={})
        session.add(src_job)
        session.flush()
        src_vid = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=src_job.id, relative_path='dummy.mp4', revision=1, review_status='pending', metadata_json={})
        session.add(src_vid)
        session.flush()

        # Blocking unknown QualityRender + unknown MediaProcessTask + unknown ProductionVisual
        qr = m.QualityRender(source_video_id=src_vid.id, status='unknown', snapshot_json={'settings': {'processing': 'ai_super_resolution'}}, result_json={})
        mt = m.MediaProcessTask(project_id=project.id, shot_id=shot.id, kind='tts', slot='dialogue_generation', fingerprint='fp_1', status='unknown', snapshot_json={}, result_json={})
        vis = m.ProductionVisual(project_id=project.id, shot_id=shot.id, kind='keyframe', status='unknown', prompt='p', snapshot_json={'service_snapshot': {'provider': 'comfyui', 'is_cloud': False, 'base_url': 'http://127.0.0.1:18188'}})
        session.add_all([qr, mt, vis])
        session.flush()

        run = StudioRun(
            project_id=project.id,
            status='paused',
            snapshot_json={'shot_ids': [shot.id], 'options': {'allow_cloud_costs': False}, 'services': {'image': {'provider': 'comfyui', 'is_cloud': False}}},
            nodes_json=[
                {'id': 'video:done', 'kind': 'video', 'target_id': shot.id, 'dependencies': [], 'status': 'succeeded', 'video_id': src_vid.id, 'child_id': src_job.id, 'attempts': 1, 'history': []},
                {'id': f'quality:{shot.id}', 'kind': 'quality', 'target_id': shot.id, 'dependencies': ['video:done'], 'status': 'unknown', 'code': 'quality_processing_unknown', 'source_video_id': src_vid.id, 'child_id': qr.id, 'attempts': 1, 'history': []},
                {'id': f'tts:{shot.id}', 'kind': 'tts', 'target_id': 'line_1', 'shot_id': shot.id, 'dependencies': [], 'status': 'unknown', 'code': 'media_submission_unknown', 'child_id': mt.id, 'attempts': 1, 'history': []},
                {'id': f'keyframe:{shot.id}', 'kind': 'keyframe', 'target_id': shot.id, 'dependencies': [], 'status': 'unknown', 'code': 'visual_unknown', 'child_id': vis.id, 'attempts': 1, 'history': []},
                {'id': f'quality_check:{shot.id}', 'kind': 'quality_check', 'target_id': shot.id, 'dependencies': ['video:done'], 'status': 'waiting_dependencies', 'child_id': None, 'attempts': 0, 'history': []},
            ],
        )
        session.add(run)
        session.commit()
        project_id, run_id, qr_id, mt_id, vis_id = project.id, run.id, qr.id, mt.id, vis.id

    resp = client.post(f'/api/studio/projects/{project_id}/repair', json={'force_recover': True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['blockers'] == []
    assert any(a['code'] == 'full_repair_nodes_requeued' for a in data['actions'])
    assert any(a['code'] == 'full_repair_dependencies_unblocked' for a in data['actions'])

    with client.app.state.session_factory() as session:
        assert session.get(m.QualityRender, qr_id).status == 'failed'
        assert session.get(m.MediaProcessTask, mt_id).status == 'failed'
        assert session.get(m.ProductionVisual, vis_id).status == 'failed'
        refreshed = session.get(StudioRun, run_id)
        assert refreshed.status == 'queued'
        by_id = {n['id']: n for n in refreshed.nodes_json}
        assert by_id[f'quality:{shot.id}']['status'] == 'pending'
        assert by_id[f'tts:{shot.id}']['status'] == 'pending'
        assert by_id[f'keyframe:{shot.id}']['status'] == 'pending'
        assert by_id[f'quality_check:{shot.id}']['status'] == 'pending'


def test_repair_clears_cross_project_dead_zombie_without_touching_fresh_job(client, monkeypatch):
    advance_calls = []
    _patch_idle_and_comfy(monkeypatch, active_prompts=set(), advance_calls=advance_calls)

    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
    with client.app.state.session_factory() as session:
        p1 = m.Project(name='当前项目')
        p2 = m.Project(name='僵尸阻塞项目')
        session.add_all([p1, p2])
        session.flush()
        s1 = m.StoryboardShot(project_id=p1.id, order=1, status='approved')
        s2 = m.StoryboardShot(project_id=p2.id, order=1, status='approved')
        session.add_all([s1, s2])
        session.flush()
        zombie_job = m.GenerationJob(
            project_id=p2.id, shot_id=s2.id, provider='comfyui_h3', provider_job_id=None,
            status='submitting', prompt='p', seed=1, created_at=old_ts,
            parameters_json={'service_snapshot': {'provider': 'comfyui', 'is_cloud': False, 'base_url': 'http://127.0.0.1:18188'}},
        )
        session.add(zombie_job)
        session.flush()
        r1 = StudioRun(
            project_id=p1.id, status='queued',
            snapshot_json={'shot_ids': [s1.id], 'options': {'allow_cloud_costs': False}, 'services': {}},
            nodes_json=[{'id': f'video:{s1.id}', 'kind': 'video', 'target_id': s1.id, 'dependencies': [], 'status': 'pending', 'child_id': None, 'attempts': 0, 'history': []}],
        )
        r2 = StudioRun(
            project_id=p2.id, status='running',
            snapshot_json={'shot_ids': [s2.id], 'options': {'allow_cloud_costs': False}, 'services': {}},
            nodes_json=[{'id': f'video:{s2.id}', 'kind': 'video', 'target_id': s2.id, 'dependencies': [], 'status': 'running', 'child_id': zombie_job.id, 'attempts': 1, 'history': []}],
        )
        session.add_all([r1, r2])
        session.commit()
        p1_id, r2_id, zjob_id = p1.id, r2.id, zombie_job.id

    resp = client.post(f'/api/studio/projects/{p1_id}/repair', json={'force_recover': True})
    assert resp.status_code == 200, resp.text
    assert any(a['code'] == 'full_repair_cross_run_zombies_cleared' for a in resp.json()['actions'])
    with client.app.state.session_factory() as session:
        assert session.get(m.GenerationJob, zjob_id).status == 'closed_unknown'
        assert session.get(StudioRun, r2_id).nodes_json[0]['status'] == 'pending'


def test_retry_with_force_unknown_recovers_single_unknown_node(client):
    with client.app.state.session_factory() as session:
        project = m.Project(name='单节点强制重试未知测试')
        session.add(project)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved')
        session.add(shot)
        session.flush()
        job = m.GenerationJob(
            project_id=project.id, shot_id=shot.id, provider='comfyui_h3', provider_job_id=None,
            status='unknown', error_code='submission_outcome_unknown', error_message='interrupted', prompt='p', seed=2,
            parameters_json={'service_snapshot': {'provider': 'comfyui', 'is_cloud': False}},
        )
        session.add(job)
        session.flush()
        nid = f'video:{shot.id}'
        run = StudioRun(
            project_id=project.id, status='running',
            snapshot_json={'shot_ids': [shot.id], 'options': {'allow_cloud_costs': False}, 'services': {}},
            nodes_json=[{'id': nid, 'kind': 'video', 'target_id': shot.id, 'dependencies': [], 'status': 'unknown',
                         'code': 'submission_outcome_unknown', 'error': 'interrupted', 'child_id': job.id, 'attempts': 1, 'history': []}],
        )
        session.add(run)
        session.commit()
        project_id, run_id, job_id = project.id, run.id, job.id

    resp = client.post(f'/api/studio/projects/{project_id}/retry', json={'node_id': nid, 'force_unknown': True})
    assert resp.status_code == 200, resp.text
    with client.app.state.session_factory() as session:
        assert session.get(m.GenerationJob, job_id).status == 'closed_unknown'
        assert session.get(StudioRun, run_id).nodes_json[0]['status'] == 'pending'
