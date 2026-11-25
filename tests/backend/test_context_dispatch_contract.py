"""Route-to-provider-to-graph contracts; all provider I/O is isolated.

The mock transport deliberately rejects /prompt. No model task is accepted,
no output video is created, and these tests are not inference evidence.
"""
import asyncio
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest
from sqlalchemy import select
from starlette.datastructures import UploadFile
from starlette.requests import Request

from app import models as m
from app.api.diagnostics import revision_preview
from app.api.routes import get_video, retry_job, revise_video, upload_video_revision_image, video_revision_reference_targets
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.schemas import RevisionRequest
from app.services.ai_services import seed_local_services
from app.services import h3_context_provider as provider
from app.services.h3_workflow_router import freeze_workflow
from app.services.providers import VideoGenerationRequest


@pytest.fixture
def dispatch_case(settings):
    settings.h3_demo_mode = False
    # Avoid the Windows-only memory supervisor; the transport never leaves
    # this process regardless of the fixture's configured loopback address.
    settings.h3_context_comfyui_url = 'http://localhost:8188'
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    try:
        with build_session_factory(engine)() as session:
            seed_local_services(session, settings)
            # Context-IR is explicit on portable hosts; do not depend on the
            # workstation-only Windows legacy fallback for service selection.
            service = session.scalar(select(m.AIService).where(m.AIService.module == 'video_generation'))
            service.settings_json = {**service.settings_json, 'workflow_profile': 'context_ir'}
            project = m.Project(name='Isolated cup-scale dispatch contract')
            product = m.Product(name='Green plush tree')
            session.add_all([project, product]); session.flush()
            shot = m.StoryboardShot(project_id=project.id, order=1, status='approved',
                visual_prompt='P02高12.5cm，宽15cm，静置于展台上。',
                duration_seconds=4, aspect_ratio='16:9')
            session.add_all([shot, m.ScriptRevision(project_id=project.id, revision=1,
                raw_text='A green plush tree on the display stand.', status='approved')])
            session.flush()
            prop = m.ProductionEntity(project_id=project.id, logical_id='P02', kind='prop',
                name=product.name, status='approved', spec_json={
                    'dimensions': '高12.5cm，宽15cm', 'color': 'green',
                    'material': 'plush', 'immutable_features': 'yellow star'})
            session.add(prop); session.flush()
            session.add(m.ShotDetail(shot_id=shot.id,
                photography_json={'shot_size':'微距超级特写（Macro Shot）','camera_position':'正面固定近景。'}, source_json={'production': {
                'prop_states': [{'entity_id': prop.id, 'prop_key': 'P02',
                                 'holder': '展台', 'state': '静置于展台'}]}}))
            settings.media_root.mkdir(parents=True, exist_ok=True)
            frozen = []
            for index, color in enumerate(('green', 'darkgreen'), 1):
                path = settings.media_root / f'green-view-{index}.png'
                Image.new('RGB', (24, 24), color).save(path)
                asset = m.ScreenplayInputAsset(input_mode='image', filename=path.name,
                    relative_path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                session.add(asset); session.flush()
                image = m.ProductImage(product_id=product.id, asset_id=asset.id)
                session.add(image); session.flush()
                frozen.append({'id': image.id, 'relative_path': path.name,
                    'sha256': asset.sha256, 'purpose': 'main', 'order': index,
                    'notes': f'Same green product, view {index}'})
            session.add_all([
                m.ProjectProductBinding(project_id=project.id, product_id=product.id,
                    snapshot_json={'name': product.name, 'images': frozen}),
                m.ShotProductBinding(project_id=project.id, shot_id=shot.id,
                    product_id=product.id, image_ids_json=[ref['id'] for ref in frozen],
                    state_json={'source': 'project_product_preparation',
                                'reference_selection_confirmed': True}),
            ])
            source = m.GenerationJob(project_id=project.id, shot_id=shot.id,
                provider='comfyui_h3', status='generated', prompt='Historical shot',
                seed=743, parameters_json={})
            profile = m.QualityProfile(scope='project', target_id=project.id,
                settings_json={'mode': 'fast'})
            session.add_all([source, profile]); session.flush()
            video = m.VideoRevision(project_id=project.id, shot_id=shot.id,
                generation_job_id=source.id, relative_path='historical-not-read.mp4',
                revision=1, review_status='approved')
            session.add(video); session.commit()
            request = Request({'type': 'http', 'app': SimpleNamespace(
                state=SimpleNamespace(settings=settings))})
            yield SimpleNamespace(db=session, request=request, project=project,
                shot=shot, prop=prop, product=product, video=video, source=source,
                profile=profile, references=frozen)
    finally:
        engine.dispose()


def test_revision_and_failed_retry_dispatch_the_preview_cup_constraint_and_exact_references(
        dispatch_case, settings, tmp_path, monkeypatch):
    case = dispatch_case
    envelopes, submitted_graphs, uploads = [], [], []
    actual_submit = provider.ContextIRH3Provider.submit
    real_client = httpx.AsyncClient

    async def capture_submit(self, request):
        envelopes.append(deepcopy(request))
        return await actual_submit(self, request)

    async def isolated_ready(*args, **kwargs):
        return None

    async def isolated_idle(*args, **kwargs):
        return False

    async def isolated_upload(client, origin, data, filename, mime, folder):
        uploads.append({'sha256': hashlib.sha256(data).hexdigest(), 'filename': filename})
        return f'{folder}/{filename} [input]'

    def reject_prompt(request):
        assert request.method == 'POST' and request.url.path == '/prompt'
        submitted_graphs.append(json.loads(request.content)['prompt'])
        # Exercise the application's definite-failure/retry route without
        # accepting a provider job or inventing any generated media.
        return httpx.Response(422, json={'error': 'isolated contract boundary'})

    monkeypatch.setattr(provider.ContextIRH3Provider, 'submit', capture_submit)
    monkeypatch.setattr(provider, 'require_context_ready', isolated_ready)
    monkeypatch.setattr(provider, 'has_work', isolated_idle)
    monkeypatch.setattr(provider, 'upload_verified', isolated_upload)
    monkeypatch.setattr(provider, 'PROJECT_ROOT', tmp_path)
    def isolated_client(**kwargs):
        return real_client(transport=httpx.MockTransport(reject_prompt), **kwargs)

    if hasattr(provider, 'context_client'):
        monkeypatch.setattr(provider, 'context_client', lambda *args, **kwargs: isolated_client(**kwargs))
    else:
        monkeypatch.setattr(httpx, 'AsyncClient', isolated_client)

    forged = {'version': 1, 'fingerprint': 'caller-forged',
        'revision_constraints': {'items': [{'target_key': 'POISON', 'relative_scale': 'two_palms'}]},
        'references': [{'asset_id': 'unselected-foreign-reference'}]}
    payload = RevisionRequest(instruction='P02高10cm、宽8cm，只有水杯大小。第一帧起P02由左手掌心托住。固定中近景，手掌、完整P02与人物脸部同时入镜。P02整体高度为脸部高度的一半。再将P02靠近脸颊。保持绿色和黄色星星不变。',
        use_project_video_service=True, parameters={'context_inputs': forged})
    preview = revision_preview(case.video.id, payload, case.request, case.db)
    expected_contract = preview['production_contract']
    item = expected_contract['revision_constraints']['items'][0]
    assert item['target_key'] == 'P02' and item['target_entity_id'] == case.prop.id
    assert item['relative_scale'] == 'drinking_cup'
    assert item['face_height_fraction']=='0.5'
    assert expected_contract['constraint_target_groups'] == {'P02': 'product-1'}
    assert {r['rule'] for r in expected_contract['scene_requirements']} == {
        'target_first_frame_palm_support','target_hand_face_same_frame','target_raise_to_cheek'}
    assert all(r['target_key']=='P02' for r in expected_contract['scene_requirements'])
    expected_ids = [ref['id'] for ref in case.references]
    expected_hashes = [ref['sha256'] for ref in case.references]
    assert preview['asset_ids'] == expected_ids and len(expected_ids) == 2

    def assert_dispatch(index, job):
        envelope = envelopes[index]
        frozen = job.parameters_json['context_inputs']
        assert frozen != forged
        assert envelope.parameters['context_inputs'] == frozen
        assert frozen['revision_constraints'] == expected_contract['revision_constraints']
        assert [ref['asset_id'] for ref in frozen['references']] == expected_ids
        assert [ref['sha256'] for ref in frozen['references']] == expected_hashes
        assert [ref['asset_id'] for ref in envelope.reference_assets] == expected_ids
        assert [ref['sha256'] for ref in envelope.reference_assets] == expected_hashes
        assert job.reference_assets_json == [
            {key: value for key, value in ref.items() if key != 'absolute_path'}
            for ref in envelope.reference_assets]
        graph = submitted_graphs[index]
        assert graph['4']['class_type'] == 'VideoFactoryLocalH3IRCompile'
        assert graph['4']['inputs']['caller_contract_policy'] == 'revision_semantics_v2'
        actual_contract = json.loads(graph['4']['inputs']['production_contract'])
        expected_wire_contract = deepcopy(expected_contract)
        expected_wire_contract['trace_id'] = envelope.job_id
        for entry in expected_wire_contract['references']:
            entry.update(width=24, height=24, analysis_status='preflight_passed')
        assert actual_contract == expected_wire_contract
        assert graph['4']['inputs']['intent'] == preview['prompt']
        assert 'Current explicit caller scene requirements' in graph['4']['inputs']['intent']
        assert 'the supporting hand' in graph['4']['inputs']['intent']
        assert "The whole P02, the supporting hand and the person's complete face are visible together in the same frame." in graph['4']['inputs']['intent']
        assert 'explicitly inside detailed_description after the opening shot heading' in graph['4']['inputs']['intent']
        assert not __import__('re').search(r'\[Shot\s+\d+\]', graph['4']['inputs']['intent'])
        assert 'The camera holds static throughout this numbered shot.' in graph['4']['inputs']['intent']
        assert 'Current caller reference-use policy for <Picture 1>, <Picture 2>: appearance ONLY.' in graph['4']['inputs']['intent']
        assert 'Macro Shot' not in graph['4']['inputs']['intent']
        assert 'Current explicit caller cinematography: 中近景（Medium Close-Up）' in graph['4']['inputs']['intent']
        assert "half the height of the person's face" in graph['4']['inputs']['intent']
        assert frozen['effective_cinematography']['photography']['shot_size']=='中近景（Medium Close-Up）'
        tray = json.loads(graph['2']['inputs']['tray'])
        assert [slot['label'] for slot in tray] == ['picture1', 'picture2']
        assert [entry['sha256'] for entry in uploads[index * 2:index * 2 + 2]] == expected_hashes
        assert all(ref['sha256'][:16] in slot['file']
                   for ref, slot in zip(case.references, tray))
        assert envelope.seed == 743 and envelope.parameters['h3_workflow']['actual_steps'] == 4
        assert envelope.parameters['service_snapshot']['settings']['workflow_profile'] == 'context_ir'
        assert job.status == 'failed' and job.provider_job_id is None
        assert job.error_code == 'context_ir_graph_rejected'

    with pytest.raises(AppError) as rejected:
        asyncio.run(revise_video(case.request, case.video.id, payload, case.db))
    assert rejected.value.code == 'context_ir_graph_rejected'
    first = case.db.get(m.GenerationJob, envelopes[0].job_id)
    assert_dispatch(0, first)

    # Retry must preserve the failed job's fast recipe and caller constraint,
    # even when the current project preference has since changed.
    case.profile.settings_json = {'mode': 'standard'}
    case.db.commit()
    with pytest.raises(AppError) as rejected:
        asyncio.run(retry_job(case.request, first.id, case.db))
    assert rejected.value.code == 'context_ir_graph_rejected'
    retried = case.db.get(m.GenerationJob, envelopes[1].job_id)
    assert_dispatch(1, retried)
    assert retried.id != first.id and retried.retry_count == first.retry_count + 1
    assert retried.parameters_json['context_inputs'] == first.parameters_json['context_inputs']
    assert len(envelopes) == len(submitted_graphs) == 2 and len(uploads) == 4
    assert list(case.db.scalars(select(m.VideoRevision.id))) == [case.video.id]
    assert case.source.status == 'generated'
    assert case.prop.spec_json['dimensions'] == '高12.5cm，宽15cm'
    assert case.db.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==case.shot.id)).photography_json['shot_size']=='微距超级特写（Macro Shot）'


def test_image_only_review_freezes_scoped_attachments_in_preview_and_actual_request(dispatch_case, settings, monkeypatch):
    case = dispatch_case
    selections = []
    for kind, color in [('character', 'blue'), ('product', 'red')]:
        image = BytesIO()
        Image.new('RGB', (48, 48), color).save(image, 'PNG')
        upload = UploadFile(BytesIO(image.getvalue()), filename=f'{kind}.png')
        row = asyncio.run(upload_video_revision_image(case.video.id, case.request, upload, kind, case.db))
        selections.append({'image_id': row['id'], 'target_id': case.product.id if kind == 'product' else None})
    targets = video_revision_reference_targets(case.video.id, case.db)
    assert any(row['id'] == case.product.id for row in targets['product'])
    payload = RevisionRequest(reference_images=selections, use_project_video_service=True)
    preview = revision_preview(case.video.id, payload, case.request, case.db)
    assert preview['asset_ids'] == [row['image_id'] for row in selections]
    assert [image['kind'] for image in preview['revision_images']] == ['character', 'product']
    assert preview['revision_images'][1]['target_name'] == case.product.name
    assert 'Use <Picture 1> as the appearance reference for the character' in preview['prompt']
    assert 'Use <Picture 2> as the appearance reference for the prop or product' in preview['prompt']
    assert all(image['media_url'].startswith('/api/media/revision_references/') for image in preview['revision_images'])

    other = m.VideoRevision(project_id=case.project.id, shot_id=case.shot.id, generation_job_id=case.source.id,
                            relative_path='other.mp4', revision=2, review_status='pending')
    case.db.add(other)
    case.db.commit()
    with pytest.raises(AppError) as wrong_scope:
        revision_preview(other.id, payload, case.request, case.db)
    assert wrong_scope.value.code == 'revision_reference_scope'
    with pytest.raises(AppError) as missing_confirmation:
        asyncio.run(revise_video(case.request, case.video.id, payload, case.db))
    assert missing_confirmation.value.code == 'revision_reference_preview_changed'

    dispatched = []

    async def capture(self, request):
        dispatched.append(request)
        raise AppError(422, 'fixture_no_sampling', 'Captured actual provider request')

    monkeypatch.setattr(provider.ContextIRH3Provider, 'submit', capture)
    confirmed = payload.model_copy(update={'reference_preview_sha256': preview['reference_preview_sha256']})
    with pytest.raises(AppError) as rejected:
        asyncio.run(revise_video(case.request, case.video.id, confirmed, case.db))
    assert rejected.value.code == 'fixture_no_sampling'
    request, = dispatched
    assert [ref['asset_id'] for ref in request.reference_assets] == preview['asset_ids']
    assert all(ref['sha256'] and ref['absolute_path'] for ref in request.reference_assets)
    assert request.parameters['context_inputs']['revision_reference_source']['selections'] == selections
    tray, declarations, _, _ = provider.prepare_context_compilation(
        request.prompt, request.negative_prompt, request.reference_assets,
        request.motion_references, request.parameters['context_inputs'])
    assert [slot['label'] for slot in tray] == ['picture1', 'picture2']
    assert declarations['reference_groups'] == 'character character-1: picture1\nproduct product-2: picture2'
    binding = case.db.scalar(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id == case.shot.id))
    assert binding.image_ids_json == [ref['id'] for ref in case.references]
    assert case.source.status == 'generated'


def test_unbound_review_photo_replays_old_snapshot_without_a_synthetic_product_group(dispatch_case, settings, monkeypatch):
    from app.services.cinematic_rules import fingerprint
    from app.services.revision_postprocess import freeze_plan
    from app.services.video_revisions import prepare_context_revision

    case = dispatch_case
    binding = case.db.scalar(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id == case.shot.id))
    binding.image_ids_json = []
    case.source.parameters_json = {'quality_request': {'processing': 'ai_upscale'}}
    case.db.commit()
    image = BytesIO()
    Image.new('RGB', (48, 48), 'orange').save(image, 'PNG')
    upload = UploadFile(BytesIO(image.getvalue()), filename='held-bottle.png')
    photo = asyncio.run(upload_video_revision_image(case.video.id, case.request, upload, 'product', case.db))
    payload = RevisionRequest(instruction='修改产品图', reference_images=[{'image_id': photo['id']}])
    preview = revision_preview(case.video.id, payload, case.request, case.db)
    assert preview['asset_ids'] == [photo['id']]
    assert preview['production_contract']['product_groups'] == {}
    _, service, quality, context = prepare_context_revision(case.db, settings, case.shot, case.video, payload)
    context['references'][0]['product_id'] = photo['id']
    context.pop('fingerprint')
    context['fingerprint'] = fingerprint(context)
    plan = freeze_plan(case.db, settings, case.shot, case.video, payload.instruction, quality=quality)
    assert plan and plan['state'] == 'waiting_generation'
    previous = m.GenerationJob(project_id=case.project.id, shot_id=case.shot.id, provider='comfyui_h3',
        status='failed', seed=743, prompt=context['prompt'], error_code='comfyui_execution_failed',
        error_message='商品参考身份不一致或无法确定：无法唯一定位商品及独立操作者。',
        parameters_json={'based_on_video_id': case.video.id, 'h3_workflow': service['h3_workflow'],
            'service_snapshot': service, 'quality_request': quality, 'context_inputs': context,
            'revision_postprocess': plan, 'instruction': payload.instruction})
    case.db.add(previous)
    case.db.commit()
    old_attempt = get_video(case.video.id, case.request, case.db)['metadata_json']['latest_revision_job']
    assert old_attempt['id'] == previous.id and '尚未开始渲染' in old_attempt['error_message']

    async def capture(self, request):
        tray, declarations, _, contract = provider.prepare_context_compilation(
            request.prompt, request.negative_prompt, request.reference_assets, request.motion_references,
            request.parameters['context_inputs'])
        assert request.reference_assets[0]['asset_id'] == photo['id']
        assert request.instruction == payload.instruction and request.seed == previous.seed
        assert declarations['reference_groups'] == '' and declarations['required_references'] == 'picture1'
        assert contract['product_groups'] == {} and 'product-unit:' not in tray[0]['note']
        raise AppError(422, 'fixture_no_sampling', 'Captured actual retry')

    monkeypatch.setattr(provider.ContextIRH3Provider, 'submit', capture)
    with pytest.raises(AppError) as err:
        asyncio.run(retry_job(case.request, previous.id, case.db))
    assert err.value.code == 'fixture_no_sampling'
    attempt = get_video(case.video.id, case.request, case.db)['metadata_json']['latest_revision_job']
    retried = case.db.get(m.GenerationJob, attempt['id'])
    assert retried.id != previous.id and retried.parameters_json['context_inputs'] == context
    assert retried.parameters_json['revision_postprocess'] == plan


def test_reference_scale_use_policy_only_changes_the_explicitly_revised_product_group():
    images=[{'role':'product','product_id':'green','product_name':'Green tree','constraint_target_keys':['P02']},
            {'role':'product','product_id':'other','product_name':'Other product','constraint_target_keys':['P03']},
            {'role':'character','actor_id':'operator'}]
    context={'instruction':'P02只有水杯大小。','revision_constraints':{'version':1,'items':[
        {'target_key':'P02','relative_scale':'drinking_cup'}]}}
    before=deepcopy((images,context))
    _,_,intent,contract=provider.prepare_context_compilation('Show two separate products.','',images,[],context)
    policy=[line for line in intent.splitlines() if line.startswith('Current caller reference-use policy')]
    assert len(policy)==1 and policy[0].startswith('Current caller reference-use policy for <Picture 1>:')
    assert '<Picture 2>' not in policy[0] and '<Picture 3>' not in policy[0]
    assert contract['constraint_target_groups']=={'P02':'product-1','P03':'product-2'}
    assert (images,context)==before
    context['revision_constraints']={'version':1,'items':[]}
    assert 'Current caller reference-use policy' not in provider.prepare_context_compilation(
        'Show two separate products.','',images,[],context)[2]


def test_first_frame_palm_request_does_not_invent_unrequested_face_composition():
    images=[{'role':'product','product_id':'green','constraint_target_keys':['P02']}]
    context={'instruction':'P02只有水杯大小。首帧P02由左手掌心托住。',
        'revision_constraints':{'version':1,'items':[{'target_key':'P02','relative_scale':'drinking_cup'}]}}
    _,_,intent,contract=provider.prepare_context_compilation('A hand supports the toy.','',images,[],context)
    assert [row['rule'] for row in contract['scene_requirements']]==['target_first_frame_palm_support']
    assert 'At the first frame P02 is supported by the open left palm' in intent
    assert 'complete face' not in intent and 'visible-composition requirement' not in intent


@pytest.mark.parametrize('instruction,expected',[
    ('固定中近景，P02与手脸同框。',True),('镜头保持固定。',True),('The camera holds static.',True),
    ('不要固定机位，跟随产品。',False),('避免固定镜头。',False),
    ('Do not use a static camera.',False),('Keep the product static; the camera moves.',False)])
def test_caller_camera_direction_never_reverses_an_explicit_exclusion(instruction,expected):
    assert provider._caller_static_camera(instruction) is expected


@pytest.mark.parametrize('snapshot', [None, {}, {'version': 2, 'fingerprint': 'untrusted'}])
def test_provider_requires_a_frozen_context_before_graph_upload_or_network(
        settings, tmp_path, monkeypatch, snapshot):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append('unexpected provider I/O or graph compilation')
        raise AssertionError('Missing context must be rejected before graph, upload, or HTTP')

    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    if hasattr(provider, 'context_client'):
        monkeypatch.setattr(provider, 'context_client', forbidden)
    monkeypatch.setattr(provider, 'build_graph', forbidden)
    monkeypatch.setattr(provider, 'upload_verified', forbidden)
    parameters = {'width': 1024, 'height': 576}
    if snapshot is not None:
        parameters['context_inputs'] = snapshot
    request = VideoGenerationRequest(job_id='isolated_job', project_id='isolated_project',
        shot_id='isolated_shot', prompt='P02只有水杯大小。', negative_prompt='',
        duration_seconds=4, aspect_ratio='16:9', seed=743, parameters=parameters,
        reference_assets=[], output_dir=tmp_path, instruction='P02只有水杯大小。')
    with pytest.raises(AppError) as rejected:
        asyncio.run(provider.ContextIRH3Provider(settings, freeze_workflow('fast')).submit(request))
    assert rejected.value.code == 'context_ir_context_snapshot_required'
    assert calls == []
