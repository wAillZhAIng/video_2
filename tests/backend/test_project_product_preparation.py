"""Project preparation uses real uploads and persisted, scoped photo choices.

These tests exercise HTTP/SQLite/media/compiler behavior, not model likeness.
The optional workstation fixtures read source files without changing them.
"""
import asyncio
import copy
import hashlib
import os
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest
from sqlalchemy import func, select

from app import models as m
from app.services import product_library
from app.services.cinematic_quality import generation_inputs_hash, visual_inputs
from app.services.cinematic_rules import fingerprint
from app.services.screenplay import generation_inputs


def image_bytes(index=0):
    stream = BytesIO()
    Image.new('RGB', (80 + index, 80), (20 + index * 13, 70, 100)).save(stream, 'PNG')
    return stream.getvalue()


def product_with_images(client, name='捏捏乐', count=2):
    response = client.post('/api/product-library', json={'name': name, 'category': 'isolated test'})
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    response = client.post(f'/api/product-library/{identifier}/images', files=[
        ('files', (f'angle-{index}.png', image_bytes(index), 'image/png')) for index in range(count)
    ], data={'purpose': 'main'})
    assert response.status_code == 201 and not response.json()['errors'], response.text
    return response.json()['product']


def project_with_shots(client):
    project = client.post('/api/projects', json={'name': 'Isolated optional product preparation'}).json()
    with client.app.state.session_factory() as session:
        shots = [m.StoryboardShot(project_id=project['id'], order=order, scene_title=title,
            visual_prompt=prompt, status=status) for order, title, prompt, status in [
                (1, '捏捏乐外盒', '固定产品外盒静止于桌面。', 'generated'),
                (2, '真实内托', '相机展示捏捏乐内部结构。', 'approved'),
                (3, '无关风景', '窗外的树木轻轻晃动。', 'draft'),
            ]]
        session.add_all(shots)
        session.commit()
        return project['id'], [shot.id for shot in shots]


def selection(client, project_id, product_id):
    response = client.get(f'/api/product-library/projects/{project_id}/references',
        params={'product_id': product_id})
    assert response.status_code == 200, response.text
    return response.json()


def save(client, project_id, current, image_ids, shot_ids):
    return client.put(f'/api/product-library/projects/{project_id}/references', json={
        'product_id': current['product']['id'], 'expected_token': current['expected_token'],
        'image_ids': image_ids, 'shot_ids': shot_ids,
    })


def media_path(settings, image):
    path = (settings.media_root / image['media_url'].removeprefix('/api/media/')).resolve()
    assert path.is_relative_to(settings.media_root.resolve())
    return path


def test_upload_trace_keeps_requested_count_partial_error_and_saved_source_hashes(client):
    product=client.post('/api/product-library',json={'name':'Trace isolation'}).json()
    good=[image_bytes(1),image_bytes(2)]
    response=client.post(f"/api/product-library/{product['id']}/images",
        headers={'X-Reference-Trace-ID':'reference-trace-three','X-Reference-Image-Count':'3'},
        files=[('files',('one.png',good[0],'image/png')),('files',('bad.png',b'broken','image/png')),
               ('files',('three.png',good[1],'image/png'))])
    assert response.status_code==201,response.text
    body=response.json()
    assert body['trace_id']=='reference-trace-three' and len(body['added'])==2 and len(body['errors'])==1
    with client.app.state.session_factory() as session:
        events=list(session.scalars(select(m.AuditEvent).where(m.AuditEvent.action=='reference_input_trace')))
        stages={event.details_json['stage']:event.details_json for event in events}
        received=stages['api_upload_received'];saved=stages['asset_persistence']
        assert received['image_count']==3 and received['client_image_count']=='3'
        assert saved['image_count']==2 and saved['received_image_count']==3 and saved['errors'][0]['error_type']=='AppError'
        assert {row['sha256'] for row in saved['references']}=={hashlib.sha256(raw).hexdigest() for raw in good}
        assert {row['image_id'] for row in saved['references']}=={image['id'] for image in body['added']}


def test_optional_project_starts_without_a_product_or_inference_call(client):
    project_id, shots = project_with_shots(client)
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.ProjectProductBinding)) == 0
        assert session.scalar(select(func.count()).select_from(m.GenerationJob)) == 0
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual)) == 0
        assert not product_library.shot_product_snapshot(session, session.get(m.StoryboardShot, shots[0]))


def test_shots_select_from_gallery_independently_without_bulk_binding(client, settings):
    product = product_with_images(client, count=3)
    project_id, shots = project_with_shots(client)
    first, second, third = [item['id'] for item in product['images']]

    def path(shot):
        return f'/api/product-library/projects/{project_id}/shots/{shot}/references'

    def put(shot, revision, video, keyframe):
        return client.put(path(shot), json={'products': [{
            'product_id': product['id'], 'expected_revision': revision,
            'video_image_ids': video, 'keyframe_image_ids': keyframe,
        }]})

    assert client.get(path(shots[0])).json()['products'] == []
    one = put(shots[0], 0, [first, second], [third])
    assert one.status_code == 200, one.text
    assert put(shots[1], 0, [third], []).status_code == 200
    assert put(shots[0], 0, [third], []).status_code == 409
    cleared = put(shots[0], 1, [], [third])
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['products'][0]['video_image_ids'] == []
    assert cleared.json()['products'][0]['keyframe_image_ids'] == [third]
    assert client.get(path(shots[1])).json()['products'][0]['video_image_ids'] == [third]
    with client.app.state.session_factory() as session:
        one = session.get(m.StoryboardShot, shots[0])
        two = session.get(m.StoryboardShot, shots[1])
        assert product_library.shot_references(session, settings, one, stage='video') == []
        assert [image['id'] for image in product_library.shot_references(session, settings, one, stage='keyframe')] == [third]
        assert [image['id'] for image in product_library.shot_references(session, settings, two, stage='video')] == [third]


def test_upload_select_persist_exact_chinese_name_suggestions_and_clear_scoped_shots(client, settings):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    current = selection(client, project_id, product['id'])
    assert not current['bound'] and current['image_ids'] == current['shot_ids'] == []
    assert {shot['id'] for shot in current['shots'] if shot['suggested']} == set(shots[:2])
    ids = [image['id'] for image in product['images']]
    response = save(client, project_id, current, ids, shots[:2])
    assert response.status_code == 200, response.text
    saved = selection(client, project_id, product['id'])
    assert saved['bound'] and set(saved['shot_ids']) == set(shots[:2])
    assert set(saved['image_ids']) == set(ids)
    assert saved['expected_token'] != current['expected_token']
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.GenerationJob)) == 0
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual)) == 0
        for identifier in shots[:2]:
            shot = session.get(m.StoryboardShot, identifier)
            for stage in ('keyframe', 'video'):
                actual = product_library.shot_references(session, settings, shot, stage=stage)
                assert {entry['id'] for entry in actual} == set(ids)
                assert {entry['sha256'] for entry in actual} == {image['sha256'] for image in product['images']}
        assert not product_library.shot_references(session, settings, session.get(m.StoryboardShot, shots[2]))
    cleared = save(client, project_id, saved, ids, [])
    assert cleared.status_code == 200 and cleared.json()['shot_ids'] == [], cleared.text
    assert all(media_path(settings, image).is_file() for image in product['images'])


@pytest.mark.parametrize(('keyframe','video'), [(True,False), (False,True), (False,False)])
def test_each_shot_product_reference_stage_can_be_left_empty(client, settings, keyframe, video):
    from app.services.context_production_inputs import collect_context_inputs

    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    ids = [image['id'] for image in product['images']]
    assert save(client, project_id, selection(client, project_id, product['id']), ids, shots[:2]).status_code == 200
    with client.app.state.session_factory() as session:
        session.add(m.StudioRun(project_id=project_id, status='exceptions',
            snapshot_json={'shot_ids':shots[:2]}, nodes_json=[]))
        session.commit()
    url = f'/api/product-library/projects/{project_id}/shots/{shots[0]}/references'
    initial = client.get(url).json()['products'][0]
    payload = {'products':[{
        'product_id':product['id'], 'expected_revision':initial['revision'],
        'keyframe_image_ids':ids if keyframe else [], 'video_image_ids':ids if video else [],
    }]}
    updated = client.put(url, json=payload)
    assert updated.status_code == 200, updated.text
    assert client.get(url).json() == updated.json()
    assert client.put(url, json=payload).status_code == 409
    with client.app.state.session_factory() as session:
        shot = session.get(m.StoryboardShot, shots[0])
        for stage,enabled in (('keyframe',keyframe), ('video',video)):
            refs = product_library.shot_references(session, settings, shot, stage=stage)
            assert {reference['id'] for reference in refs} == (set(ids) if enabled else set())
            sibling = session.get(m.StoryboardShot, shots[1])
            assert {reference['id'] for reference in product_library.shot_references(session,settings,sibling,stage=stage)} == set(ids)
        context = collect_context_inputs(session, settings, shot)
        assert {ref['asset_id'] for ref in context['references']} == (set(ids) if video else set())
        assert ('\n商品：' in context['prompt']) is video
        run = session.scalar(select(m.StudioRun).where(m.StudioRun.project_id == project_id))
        assert run.status == 'paused' and run.snapshot_json['product_references_changed'] is True
    overview = selection(client, project_id, product['id'])
    assert set(overview['shot_ids']) == (set(shots[:2]) if keyframe or video else {shots[1]})
    assert all(media_path(settings, image).is_file() for image in product['images'])


@pytest.mark.parametrize('bad_selection', ['foreign-image', 'foreign-shot', 'duplicate-image', 'duplicate-shot'])
def test_rejects_cross_product_project_and_duplicate_choices_atomically(client, bad_selection):
    product = product_with_images(client)
    other_product = product_with_images(client, 'Other product')
    project_id, shots = project_with_shots(client)
    _, foreign_shots = project_with_shots(client)
    current = selection(client, project_id, product['id'])
    images = [product['images'][0]['id']]
    selected_shots = [shots[0]]
    if bad_selection == 'foreign-image': images = [other_product['images'][0]['id']]
    if bad_selection == 'foreign-shot': selected_shots = [foreign_shots[0]]
    if bad_selection == 'duplicate-image': images *= 2
    if bad_selection == 'duplicate-shot': selected_shots *= 2
    response = save(client, project_id, current, images, selected_shots)
    assert response.status_code == 422, response.text
    after = selection(client, project_id, product['id'])
    assert after['expected_token'] == current['expected_token'] and not after['bound']
    assert not after['shot_ids']


def test_stale_tokens_reject_lost_updates_including_library_uploads(client):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    before = selection(client, project_id, product['id'])
    first_id = product['images'][0]['id']
    assert save(client, project_id, before, [first_id], [shots[0]]).status_code == 200
    response = save(client, project_id, before, [first_id], [shots[1]])
    assert response.status_code == 409 and response.json()['error']['code'] == 'project_product_revision'
    current = selection(client, project_id, product['id'])
    uploaded = client.post(f"/api/product-library/{product['id']}/images",
        files=[('files', ('new-angle.png', image_bytes(7), 'image/png'))])
    assert uploaded.status_code == 201 and len(uploaded.json()['added']) == 1
    response = save(client, project_id, current, [first_id], [shots[1]])
    assert response.status_code == 409, response.text
    assert selection(client, project_id, product['id'])['shot_ids'] == [shots[0]]


def test_additional_library_photo_can_be_selected_without_rewriting_old_original_or_video(client, settings):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    first = product['images'][0]
    original = media_path(settings, first).read_bytes()
    response = save(client, project_id, selection(client, project_id, product['id']), [first['id']], [shots[0]])
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as session:
        shot = session.get(m.StoryboardShot, shots[0])
        previous_hash = generation_inputs_hash(session, shot)
        parameters = {'screenplay_snapshot': {'cinematic': {'preflight': {'generation_inputs_hash': previous_hash}}}}
        job = m.GenerationJob(project_id=project_id, shot_id=shot.id, provider='recorded-existing',
            status='succeeded', prompt='Preserved input', parameters_json=copy.deepcopy(parameters))
        session.add(job)
        session.flush()
        video = m.VideoRevision(project_id=project_id, shot_id=shot.id, generation_job_id=job.id,
            relative_path='historical-output.mp4', revision=1, review_status='approved', metadata_json={'fixture': True})
        session.add(video)
        session.commit()
        job_id, video_id = job.id, video.id
    uploaded = client.post(f"/api/product-library/{product['id']}/images",
        files=[('files', ('new-angle.png', image_bytes(7), 'image/png'))]).json()
    new_image = uploaded['added'][0]
    current = selection(client, project_id, product['id'])
    response = save(client, project_id, current, [new_image['id']], [shots[0]])
    assert response.status_code == 200, response.text
    assert media_path(settings, first).read_bytes() == original
    with client.app.state.session_factory() as session:
        assert session.get(m.GenerationJob, job_id).parameters_json == parameters
        video = session.get(m.VideoRevision, video_id)
        assert video.review_status == 'approved' and video.relative_path == 'historical-output.mp4'
        shot = session.get(m.StoryboardShot, shots[0])
        assert generation_inputs_hash(session, shot) != previous_hash
        actual = product_library.shot_references(session, settings, shot)
        assert [image['id'] for image in actual] == [new_image['id']]
        frozen = session.scalar(select(m.ProjectProductBinding).where(m.ProjectProductBinding.project_id == project_id))
        assert {image['id'] for image in frozen.snapshot_json['images']} == {first['id'], new_image['id']}


def test_changed_product_bytes_block_save_and_do_not_overwrite_bindings(client, settings):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    first = product['images'][0]
    before = selection(client, project_id, product['id'])
    assert save(client, project_id, before, [first['id']], [shots[0]]).status_code == 200
    before = selection(client, project_id, product['id'])
    media_path(settings, first).write_bytes(image_bytes(7))  # pytest temporary copy only
    response = save(client, project_id, before, [first['id']], [shots[1]])
    assert response.status_code == 409 and response.json()['error']['code'] == 'product_source_changed'
    assert selection(client, project_id, product['id'])['shot_ids'] == [shots[0]]


@pytest.mark.parametrize('active_status', ['cancelling', 'reconciling', 'unknown'])
def test_active_generation_blocks_reference_mutation(client, active_status):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    before = selection(client, project_id, product['id'])
    with client.app.state.session_factory() as session:
        session.add(m.GenerationJob(project_id=project_id, shot_id=shots[0], provider='recorded-existing',
            status=active_status, prompt='Isolated active record'))
        session.commit()
    response = save(client, project_id, before, [product['images'][0]['id']], [shots[0]])
    assert response.status_code == 409 and response.json()['error']['code'] == 'product_binding_busy'
    assert not selection(client, project_id, product['id'])['bound']


@pytest.mark.parametrize('historical_kind', ['deleted', 'old-script'])
def test_editing_current_references_preserves_deleted_or_old_script_bindings_and_history(client, historical_kind):
    product = product_with_images(client, count=3)
    project_id, shots = project_with_shots(client)
    photos = [image['id'] for image in product['images']]
    initial = selection(client, project_id, product['id'])
    assert save(client, project_id, initial, photos[:2], shots[:2]).status_code == 200
    historical_state = {'keyframe_image_ids': [photos[1]], 'visible_state': 'historical view',
        'source': 'immutable historical fixture'}
    parameters = {'source_snapshot': {'product_id': product['id'], 'image_ids': [photos[1]]}}
    with client.app.state.session_factory() as session:
        current_binding = session.scalar(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id == shots[0]))
        current_binding.image_ids_json = [photos[0]]
        current_binding.state_json = {'keyframe_image_ids': [photos[0]]}
        historical = session.scalar(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id == shots[1]))
        historical.image_ids_json = [photos[1]]
        historical.state_json = copy.deepcopy(historical_state)
        historical.revision = 7
        historical_id = historical.id
        if historical_kind == 'deleted':
            session.get(m.StoryboardShot, shots[1]).status = 'deleted'
        else:
            session.add(m.ScriptRevision(project_id=project_id, revision=1, status='approved', raw_text='New revision',
                parsed_json={'explicit_shot_ids': [shots[0]]}))
        job = m.GenerationJob(project_id=project_id, shot_id=shots[1], provider='recorded-existing',
            status='succeeded', prompt='Historical product input', parameters_json=copy.deepcopy(parameters))
        session.add(job)
        session.flush()
        video = m.VideoRevision(project_id=project_id, shot_id=shots[1], generation_job_id=job.id,
            relative_path='historical-product-output.mp4', revision=1, review_status='approved')
        session.add(video)
        session.commit()
        job_id, video_id = job.id, video.id
    current = selection(client, project_id, product['id'])
    assert current['shot_ids'] == [shots[0]] and current['image_ids'] == [photos[0]]
    assert shots[1] not in {shot['id'] for shot in current['shots']}
    response = save(client, project_id, current, [photos[2]], [shots[0]])
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as session:
        historical = session.get(m.ShotProductBinding, historical_id)
        assert historical.image_ids_json == [photos[1]]
        assert historical.state_json == historical_state and historical.revision == 7
        assert session.get(m.GenerationJob, job_id).parameters_json == parameters
        video = session.get(m.VideoRevision, video_id)
        assert video.review_status == 'approved' and video.relative_path == 'historical-product-output.mp4'


@pytest.mark.parametrize('source_state', ['archived', 'corrupt', 'missing'])
def test_remove_all_current_references_remains_possible_with_archived_or_damaged_sources(client, settings, source_state):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    photo = product['images'][0]
    before = selection(client, project_id, product['id'])
    assert save(client, project_id, before, [photo['id']], shots[:2]).status_code == 200
    with client.app.state.session_factory() as session:
        snapshot = copy.deepcopy(session.scalar(select(m.ProjectProductBinding).where(
            m.ProjectProductBinding.project_id == project_id)).snapshot_json)
    path = media_path(settings, photo)
    if source_state == 'archived':
        response = client.delete(f"/api/product-library/{product['id']}")
        assert response.status_code == 200 and response.json()['state'] == 'archived'
    elif source_state == 'corrupt':
        path.write_bytes(b'isolated damaged source')
    else:
        path.unlink()  # Only the verified path inside this test's temporary media_root.
    current = selection(client, project_id, product['id'])
    assert set(current['shot_ids']) == set(shots[:2])
    if source_state == 'archived': assert current['product']['state'] == 'archived'
    response = save(client, project_id, current, [photo['id']], [])
    assert response.status_code == 200, response.text
    assert response.json()['shot_ids'] == response.json()['image_ids'] == []
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.ShotProductBinding)) == 0
        assert session.scalar(select(m.ProjectProductBinding).where(
            m.ProjectProductBinding.project_id == project_id)).snapshot_json == snapshot
    if source_state == 'archived':
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == photo['sha256']
    elif source_state == 'corrupt':
        assert path.read_bytes() == b'isolated damaged source'
    else:
        assert not path.exists()


@pytest.mark.parametrize('active_status', ['running', 'reconciling', 'unknown'])
def test_removing_damaged_references_still_blocks_while_generation_may_be_active(client, settings, active_status):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    photo = product['images'][0]
    assert save(client, project_id, selection(client, project_id, product['id']), [photo['id']], [shots[0]]).status_code == 200
    media_path(settings, photo).write_bytes(b'isolated damaged source')
    with client.app.state.session_factory() as session:
        session.add(m.GenerationJob(project_id=project_id, shot_id=shots[0], provider='recorded-existing',
            status=active_status, prompt='Isolated unresolved task'))
        session.commit()
    current = selection(client, project_id, product['id'])
    response = save(client, project_id, current, [photo['id']], [])
    assert response.status_code == 409 and response.json()['error']['code'] == 'product_binding_busy'
    assert selection(client, project_id, product['id'])['shot_ids'] == [shots[0]]


@pytest.mark.parametrize('control', ['resume', 'retry', 'repair'])
@pytest.mark.parametrize('initial_status', ['paused', 'exceptions'])
def test_changed_paused_run_requires_new_incremental_run_before_dispatch(client, control, initial_status):
    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    with client.app.state.session_factory() as session:
        run = m.StudioRun(project_id=project_id, status=initial_status, nodes_json=[], snapshot_json={})
        session.add(run)
        session.commit()
        run_id = run.id
    before = selection(client, project_id, product['id'])
    response = save(client, project_id, before, [product['images'][0]['id']], [shots[0]])
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as session:
        run = session.get(m.StudioRun, run_id)
        assert run.status == 'paused' and run.snapshot_json['product_references_changed'] is True
    response = client.post(f'/api/studio/projects/{project_id}/{control}', json={})
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'studio_product_inputs_changed'


def test_background_advance_never_dispatches_a_run_with_changed_product_inputs(client, settings, monkeypatch):
    from app.services import studio_pipeline
    project_id, shots=project_with_shots(client)
    async def forbidden(*args,**kwargs):
        raise AssertionError('changed product inputs must block old-run dispatch')
    monkeypatch.setattr(studio_pipeline,'_video',forbidden)
    monkeypatch.setattr(studio_pipeline,'_visual',forbidden)
    with client.app.state.session_factory() as session:
        nodes=[{'id':'video:'+shots[0],'kind':'video','target_id':shots[0],'status':'pending','dependencies':[]}]
        run=m.StudioRun(project_id=project_id,status='exceptions',nodes_json=nodes,
            snapshot_json={'product_references_changed':True})
        session.add(run);session.flush()
        asyncio.run(studio_pipeline.advance_pipeline(client.app,settings,session,run))
        assert run.status=='paused' and run.nodes_json==nodes


def test_multiple_products_share_keyframe_budget_without_silently_dropping_any_image(client):
    first = product_with_images(client, 'First product', 3)
    second = product_with_images(client, 'Second product', 2)
    project_id, shots = project_with_shots(client)
    first_ids = [image['id'] for image in first['images']]
    second_ids = [image['id'] for image in second['images']]
    assert save(client, project_id, selection(client, project_id, first['id']), first_ids, [shots[0]]).status_code == 200
    before = selection(client, project_id, second['id'])
    response = save(client, project_id, before, second_ids, [shots[0]])
    assert response.status_code == 422 and response.json()['error']['code'] == 'product_keyframe_budget'
    assert not selection(client, project_id, second['id'])['bound']
    assert set(selection(client, project_id, first['id'])['image_ids']) == set(first_ids)


def test_real_photos_upload_freeze_and_compiler_reference_hashes_are_original(client, settings, record_property):
    source_directory = Path(os.environ.get('PRODUCT_REFERENCE_TEST_DIRECTORY') or Path.home() / 'Desktop' / '新建文件夹')
    paths = [source_directory / 'IMG_20261029_192810.jpg',
        source_directory / 'd9f17c7ba578501f11b9ee46218fb358b176bae3f1b35d468296093a64de1610.png']
    if not all(path.is_file() for path in paths):
        pytest.skip('Optional workstation photos are not present on this installation')
    settings.import_max_mb = 50
    product = client.post('/api/product-library', json={'name': '捏捏乐真实照片回归'}).json()
    sources = [(path.name, path.read_bytes(), 'image/jpeg' if path.suffix == '.jpg' else 'image/png') for path in paths]
    response = client.post(f"/api/product-library/{product['id']}/images", files=[('files', item) for item in sources])
    assert response.status_code == 201 and len(response.json()['added']) == 2 and not response.json()['errors'], response.text
    photos = response.json()['added']
    expected = {hashlib.sha256(data).hexdigest() for _, data, _ in sources}
    record_property('source_sha256', ','.join(sorted(expected)))
    assert {photo['sha256'] for photo in photos} == expected
    for photo in photos:
        assert hashlib.sha256(media_path(settings, photo).read_bytes()).hexdigest() == photo['sha256']
        with Image.open(media_path(settings, photo)) as decoded:
            decoded.load()
            assert decoded.size == (photo['width'], photo['height'])
    project_id, shots = project_with_shots(client)
    response = save(client, project_id, selection(client, project_id, product['id']),
        [photo['id'] for photo in photos], [shots[0]])
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as session:
        shot = session.get(m.StoryboardShot, shots[0])
        version, refs, prompt, snapshot = generation_inputs(session, shot, settings)
        assert version is None and [ref['role'] for ref in refs] == ['product', 'product']
        assert {ref['sha256'] for ref in refs} == expected
        assert [ref['picture_index'] for ref in refs] == [1, 2]
        assert {ref['sha256'] for ref in snapshot['reference_manifest']['references']} == expected
        assert all(photo['id'] in {ref['asset_id'] for ref in refs} for photo in photos)
        assert 'Preserve its evidenced shape' in prompt
    # The test read and copied sources; originals remain exactly unchanged.
    assert {hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == expected


def test_character_and_product_reference_roles_stay_separate_and_keyframe_change_stales_input(client, settings):
    from app.schemas_cinematic import VisualInput
    from app.services.cinematic_visuals import build_visual

    product = product_with_images(client)
    project_id, shots = project_with_shots(client)
    ids = [image['id'] for image in product['images']]
    response = save(client, project_id, selection(client, project_id, product['id']), ids, [shots[0]])
    assert response.status_code == 200, response.text
    with client.app.state.session_factory() as session:
        character = m.Character(slug='isolated-product-performer', display_name='成年演示者')
        session.add(character)
        session.flush()
        version = m.CharacterVersion(character_id=character.id, version=1, approval_state='approved',
            identity_json={'age': 30, 'gender': 'female'})
        session.add(version)
        session.flush()
        face_data = image_bytes(9)
        path = settings.media_root / 'explicit-face-reference.png'
        path.write_bytes(face_data)
        asset = m.CharacterAsset(character_version_id=version.id, asset_type='front_face',
            relative_path=path.name, mime_type='image/png', sha256=hashlib.sha256(face_data).hexdigest(),
            width=89, height=80, status='approved', is_primary_reference=True)
        session.add(asset)
        session.flush()
        version.standard_face_asset_id = asset.id
        character.current_approved_version_id = version.id
        shot = session.get(m.StoryboardShot, shots[0])
        session.add(m.ShotCharacter(shot_id=shot.id, character_id=character.id,
            character_version_id=version.id, order=1, reference_asset_ids_json=[asset.id],
            performance_json={'position': 'center', 'action': 'stands still'}))
        session.commit()
        _, refs, _, snapshot = generation_inputs(session, shot, settings)
        assert [ref['role'] for ref in refs] == ['identity', 'product', 'product']
        assert refs[0]['sha256'] == asset.sha256
        assert len(snapshot['reference_manifest']['actors']) == 1
        settings.h3_demo_mode = False
        service = session.scalar(select(m.AIService).where(m.AIService.module == 'character_generation'))
        assert service is not None
        # Compile the real keyframe input without committing/dispatching its
        # queue entry; this checks the same input builder used by automation.
        savepoint = session.begin_nested()
        visual = build_visual(session, settings, project_id,
            VisualInput(kind='keyframe', shot_id=shot.id, service_id=service.id))
        assert [ref['role'] for ref in visual.snapshot_json['references']] == ['identity', 'product', 'product']
        assert {ref['sha256'] for ref in visual.snapshot_json['references'][1:]} == {
            image['sha256'] for image in product['images']}
        savepoint.rollback()
        before_visual = fingerprint(visual_inputs(session, shot))
        before_video = generation_inputs_hash(session, shot)
        binding = session.scalar(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id == shot.id))
        binding.state_json = {**binding.state_json, 'keyframe_image_ids': [ids[1]]}
        session.commit()
        assert fingerprint(visual_inputs(session, shot)) != before_visual
        assert generation_inputs_hash(session, shot) != before_video
        assert [image['id'] for image in product_library.shot_references(session, settings, shot, stage='keyframe')] == [ids[1]]
        assert {image['id'] for image in product_library.shot_references(session, settings, shot)} == set(ids)
