import asyncio

from app import models as m
from app.services import studio_model_recovery as recovery


def _direct_run(session):
    project = m.Project(name='Native recovery scope')
    session.add(project)
    session.flush()
    shot = m.StoryboardShot(project_id=project.id, order=1, status='approved')
    session.add(shot)
    session.flush()
    service = m.AIService(name='Native H3', module='video_generation', enabled=True,
                          provider='comfyui', base_url=recovery.VAST_COMFY,
                          default_model='minimax_h3_ref2va_pruned_w6a8.safetensors')
    session.add(service)
    session.flush()
    snapshot = {
        'service_id': service.id, 'service_revision': service.revision,
        'base_url': service.base_url, 'provider': 'comfyui', 'is_cloud': False,
        'model_id': service.default_model,
    }
    run = m.StudioRun(project_id=project.id, status='running', nodes_json=[],
                      snapshot_json={'services': {'video': snapshot}})
    session.add(run)
    session.flush()
    return project, shot, service, snapshot, run


def test_repair_only_requeues_native_job_proven_not_submitted(client):
    with client.app.state.session_factory() as session:
        project, shot, service, snapshot, run = _direct_run(session)
        node_id = 'video:' + shot.id
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='comfyui_h3',
                              status='failed', error_code='local_gpu_busy', prompt='unchanged prompt',
                              seed=91, parameters_json={
                                  'studio_run_id': run.id, 'studio_node_id': node_id,
                                  'service_snapshot': snapshot,
                              })
        session.add(job)
        session.flush()
        run.nodes_json = [{'id': node_id, 'kind': 'video', 'target_id': shot.id,
                           'status': 'failed', 'code': 'local_gpu_busy',
                           'child_id': job.id, 'history': []}]
        session.commit()
        result = asyncio.run(recovery.interrupted_native_nodes(
            session, run, [{'service_id': service.id, 'revision': service.revision, 'status': 'recovered'}]))
        assert result == {node_id: {'job_id': job.id, 'prompt_id': None}}
        assert job.provider_job_id is None and job.prompt == 'unchanged prompt'


def test_repair_never_requeues_native_job_with_provider_prompt_id(client):
    with client.app.state.session_factory() as session:
        project, shot, service, snapshot, run = _direct_run(session)
        node_id = 'video:' + shot.id
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='comfyui_h3',
                              status='failed', error_code='provider_unavailable',
                              provider_job_id='real-comfy-prompt-id', prompt='preserved', seed=92,
                              parameters_json={
                                  'studio_run_id': run.id, 'studio_node_id': node_id,
                                  'service_snapshot': snapshot,
                              })
        session.add(job)
        session.flush()
        run.nodes_json = [{'id': node_id, 'kind': 'video', 'target_id': shot.id,
                           'status': 'failed', 'code': 'provider_unavailable',
                           'child_id': job.id, 'history': []}]
        session.commit()
        assert asyncio.run(recovery.interrupted_native_nodes(
            session, run, [{'service_id': service.id, 'revision': service.revision, 'status': 'connected'}])) == {}
