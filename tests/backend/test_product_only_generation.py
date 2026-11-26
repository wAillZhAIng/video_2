"""Product-only generation/API regressions; provider is an explicit contract stub."""
import copy
import hashlib
import asyncio
from types import SimpleNamespace

from PIL import Image
import pytest
from sqlalchemy import func, select
from starlette.requests import Request

from app import models as m
from app.api import routes
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.models.product_library import Product, ProductImage, ProjectProductBinding, ShotProductBinding
from app.schemas import GenerationRequest
from app.services.cinematic_quality import generation_inputs_hash, inspect_ir, shot_preflight, visual_inputs
from app.services.cinematic_rules import fingerprint, rules_bundle
from app.services.providers import JobAccepted
from app.services.screenplay import generation_inputs, uses_structured_screenplay


def run_contract(coroutine):
    """Drive the in-process contract without creating a socket/event loop."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError('This contract must finish before any external/asynchronous I/O')


class ContractProvider:
    name = 'explicit_contract_stub'

    def __init__(self):
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        return JobAccepted('contract-only-no-inference', 'queued')


@pytest.fixture()
def product_shot(settings):
    settings.h3_demo_mode = False
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        project = m.Project(name='Product-only isolated generation fixture')
        session.add(project)
        session.flush()
        rules = rules_bundle()
        script = m.ScriptRevision(project_id=project.id, revision=1, raw_text='Fixture product presentation, no performers', status='approved',
            parsed_json={'versions': rules['versions'], 'rules_snapshot': rules})
        session.add(script)
        session.flush()
        scene = m.Scene(project_id=project.id, script_revision_id=script.id, order=1, name='Studio tabletop',
                        environment_json={'location': 'plain tabletop', 'lighting': 'soft practical light'})
        session.add(scene)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='Product presentation', status='approved',
            aspect_ratio='16:9', duration_seconds=4, visual_prompt='The supplied product remains visible on a tabletop.',
            action='Product rotates slowly.', camera_move='Fixed camera')
        session.add(shot)
        session.flush()
        detail = m.ShotDetail(shot_id=shot.id, scene_id=scene.id, photography_json={'framing': 'product close-up'}, source_json={'production': {}})
        product = Product(name='Isolated product fixture')
        session.add_all([detail, product])
        session.flush()
        settings.media_root.mkdir(parents=True, exist_ok=True)
        path = settings.media_root / 'explicit_product_fixture.png'
        Image.new('RGB', (64, 64), (20, 100, 80)).save(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        source = m.ScreenplayInputAsset(input_mode='image', filename=path.name, relative_path=path.name, sha256=sha,
                                       metadata_json={'width': 64, 'height': 64, 'bytes': path.stat().st_size})
        session.add(source)
        session.flush()
        picture = ProductImage(product_id=product.id, asset_id=source.id, purpose='main', order=1)
        session.add(picture)
        session.flush()
        project_binding = ProjectProductBinding(project_id=project.id, product_id=product.id,
            snapshot_json={'id': product.id, 'name': product.name, 'images': [{'id': picture.id, 'asset_id': source.id,
                'relative_path': path.name, 'sha256': sha, 'purpose': 'main', 'order': 1}]})
        binding = ShotProductBinding(project_id=project.id, shot_id=shot.id, product_id=product.id,
            image_ids_json=[picture.id], state_json={'visible_state': 'fully visible', 'presentation': 'rotating on tabletop'})
        session.add_all([project_binding, binding])
        session.flush()
        keyframe = m.ProductionVisual(project_id=project.id, shot_id=shot.id, kind='keyframe', status='approved', prompt='Explicit fixture input',
            relative_path=path.name, sha256=sha, snapshot_json={'inputs_hash': fingerprint(visual_inputs(session, shot))})
        session.add(keyframe)
        session.flush()
        detail.source_json = {'production': {'visual_ids': [keyframe.id]}}
        session.add(m.AudioEvent(shot_id=shot.id, kind='voice', description='Optional narration with speaker unspecified; no character identity asserted'))
        session.commit()
        yield SimpleNamespace(session=session, settings=settings, project=project, script=script, scene=scene,
            shot=shot, detail=detail, product=product, binding=binding, project_binding=project_binding,
            picture=picture, source=source, path=path, keyframe=keyframe)
    engine.dispose()


def test_product_only_compiles_real_refs_without_fabricating_a_character(product_shot):
    p = product_shot
    assert uses_structured_screenplay(p.session, p.shot)
    version, refs, prompt, snapshot = generation_inputs(p.session, p.shot, p.settings)
    assert version is None
    assert [r['role'] for r in refs] == ['product', 'composition']
    assert refs[0]['asset_id'] == p.picture.id and refs[0]['product_id'] == p.product.id
    assert [r['picture_index'] for r in refs] == [1, 2]
    assert snapshot['reference_manifest']['actors'] == [] and snapshot['reference_manifest']['speakers'] == []
    assert snapshot['reference_manifest']['references'][0]['sha256'] == p.source.sha256
    assert snapshot['product_appearance_review']['exact_logo_text_verified'] is False
    assert snapshot['reference_manifest']['sha256'] == fingerprint({k: v for k, v in snapshot['reference_manifest'].items() if k != 'sha256'})
    assert 'natural adult skin' not in prompt and 'pores and age cues' not in prompt and 'Costume state' not in prompt
    assert 'product photography' in prompt and 'logo reproduction require visual review' in prompt
    assert 'Optional narration' in prompt
    assert not p.session.scalar(select(func.count()).select_from(m.Character))
    preflight = shot_preflight(p.session, p.settings, p.shot)
    assert preflight['ready'], preflight['findings']
    assert preflight['reference_count'] == 2


def test_product_only_keyframe_prompt_keeps_product_identity_without_performer_language(product_shot, monkeypatch):
    from app.schemas_cinematic import VisualInput
    from app.services import cinematic_visuals
    from app.services.ai_services import service_snapshot

    p = product_shot
    service = m.AIService(module='character_generation', provider='comfyui', name='Contract only; no image execution',
        base_url='http://127.0.0.1:9', default_model='flux-2-klein-4b-fp8.safetensors',
        settings_json={'text_encoder': 'qwen_3_4b.safetensors', 'vae': 'flux2-vae.safetensors'})
    p.session.add(service)
    p.session.flush()
    resolution_calls = []

    def contract_resolution(session, module, service_id=None, project_id=None, model_id=None):
        assert session is p.session and module == 'character_generation'
        assert service_id == service.id and project_id == p.project.id and model_id is None
        resolution_calls.append(service_id)
        return service, service_snapshot(service, model_id)

    async def forbidden_transport(*args, **kwargs):
        raise AssertionError('Prompt compilation must not submit an external or GPU request')

    monkeypatch.setattr(cinematic_visuals, 'resolve_service', contract_resolution)
    monkeypatch.setattr(cinematic_visuals, 'service_request', forbidden_transport)
    original_bytes = p.path.read_bytes()
    visual = cinematic_visuals.build_visual(p.session, p.settings, p.project.id,
        VisualInput(kind='keyframe', shot_id=p.shot.id, service_id=service.id))
    assert resolution_calls == [service.id]
    assert visual.status == 'queued' and visual.relative_path is None
    assert visual.snapshot_json['product_only'] is True
    assert visual.snapshot_json['reference_manifest']['actors'] == []
    assert [reference['role'] for reference in visual.snapshot_json['references']] == ['product']
    reference = visual.snapshot_json['references'][0]
    assert reference['asset_id'] == p.picture.id and reference['product_id'] == p.product.id
    assert reference['sha256'] == p.source.sha256
    assert visual.snapshot_json['product_reference_selection']['actual'] == [p.picture.id]
    assert 'PRODUCT-ONLY' in visual.prompt
    assert 'Absolutely no people, faces, hands' in visual.prompt
    assert f'Reference image 1 defines only product {p.product.id} ({p.product.name})' in visual.prompt
    assert 'Preserve its shape, proportions, color and packaging.' in visual.prompt
    assert 'A believable adult' not in visual.prompt
    assert 'grounded body weight' not in visual.prompt
    assert 'Show only the explicitly referenced performers' not in visual.prompt
    language = visual.snapshot_json['rules_snapshot']['visual_language']
    assert all(language[key] in visual.prompt for key in ('scene', 'camera'))
    assert all(language[key] not in visual.prompt for key in ('identity', 'wardrobe', 'action'))
    assert p.path.read_bytes() == original_bytes
    assert not p.session.scalar(select(func.count()).select_from(m.Character))


def test_product_state_or_selected_image_changes_invalidate_visual_inputs(product_shot):
    p = product_shot
    before = generation_inputs_hash(p.session, p.shot)
    p.binding.state_json = {'visible_state': 'packaging open'}
    p.session.flush()
    assert generation_inputs_hash(p.session, p.shot) != before
    assert any(f['code'] == 'keyframe_stale' for f in shot_preflight(p.session, p.settings, p.shot)['findings'])
    p.binding.image_ids_json = []
    p.session.flush()
    assert 'keyframe_stale' in {f['code'] for f in shot_preflight(p.session, p.settings, p.shot, False)['findings']}


def test_legacy_reference_video_accepts_approved_frame_with_product_images_cleared(product_shot):
    p = product_shot
    p.binding.state_json = {**p.binding.state_json, 'keyframe_image_ids':[]}
    p.binding.image_ids_json = []
    p.session.flush()
    p.keyframe.snapshot_json = {'inputs_hash':fingerprint(visual_inputs(p.session,p.shot))}
    p.session.flush()
    version, references, prompt, _, _, snapshot = routes._legacy_generation_inputs(
        p.session, p.settings, p.shot, GenerationRequest(), None, None)
    assert version is None
    assert [ref['role'] for ref in references] == ['composition']
    assert references[0]['asset_id'] == p.keyframe.id
    assert 'product_appearance_review' not in snapshot
    assert 'the approved visual composition' in prompt


def test_missing_product_ref_and_missing_participants_do_not_restore_visual_input_gate(product_shot):
    p = product_shot
    p.session.delete(p.binding)
    p.session.flush()
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'participants_required'
    assert not any(f['code'] == 'cast_not_ready' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])
    ir = {'characters': [], 'scenes': [], 'shots': [{'key': 'plain', 'participants': [], 'duration_seconds': 4}]}
    assert any(f['code'] == 'cast_unknown' for f in inspect_ir(ir)['findings'])
    assert 'products' not in visual_inputs(p.session, p.shot)


def test_native_h3_allows_a_pure_script_without_visual_references(product_shot):
    p = product_shot
    p.session.delete(p.binding)
    p.detail.source_json = {'production': {}}
    p.session.flush()

    version, references, script_prompt, prompt, negative, snapshot = routes._generation_inputs(
        p.session, p.settings, p.shot, GenerationRequest(), None, None,
        allow_no_visual_references=True,
    )

    assert version is None and references == []
    assert script_prompt == prompt and negative == ''
    assert snapshot['reference_manifest']['references'] == []
    assert not any(f['code'] == 'cast_not_ready' for f in shot_preflight(p.session, p.settings, p.shot)['findings'])


def test_changed_product_bytes_are_rejected_before_model_dispatch(product_shot):
    p = product_shot
    Image.new('RGB', (64, 64), 'red').save(p.path)
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'product_source_changed'
    assert not shot_preflight(p.session, p.settings, p.shot, False)['ready']


def test_product_binding_does_not_bypass_a_declared_performers_identity_review(product_shot):
    p = product_shot
    character = m.Character(slug='explicit-unapproved-fixture', display_name='Explicit test performer')
    p.session.add(character)
    p.session.flush()
    version = m.CharacterVersion(character_id=character.id, version=1, approval_state='draft', identity_json={})
    p.session.add(version)
    p.session.flush()
    p.session.add(m.ShotCharacter(shot_id=p.shot.id, character_id=character.id, character_version_id=version.id,
                                 reference_asset_ids_json=[], order=1))
    p.session.flush()
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'participant_identity_unapproved'
    assert any(f['code'] == 'identity_not_ready' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])


def test_product_reference_budget_is_the_actual_h3_budget(product_shot):
    p = product_shot
    snapshot = copy.deepcopy(p.project_binding.snapshot_json)
    base = snapshot['images'][0]
    snapshot['images'] = [{**base, 'id': 'fixture-image-' + str(i), 'order': i} for i in range(10)]
    p.project_binding.snapshot_json = snapshot
    p.binding.image_ids_json = [i['id'] for i in snapshot['images']]
    p.session.flush()
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'reference_limit'
    assert any(f['code'] == 'reference_limit' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])


@pytest.mark.parametrize('override', [None, '本地完整输入：' + '景' * 20000], ids=['compiled', 'edited-long'])
def test_generation_entry_persists_null_character_version_and_product_manifest(product_shot, monkeypatch, override):
    from app.services import video_services
    p = product_shot
    provider = ContractProvider()
    requests = provider.requests
    monkeypatch.setattr(routes, 'provider_for', lambda settings: provider)
    monkeypatch.setattr(video_services, 'select_video_service', lambda *args: (None,
        {'settings': {}, 'provider': 'comfyui', 'is_cloud': False}, provider))
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=p.settings))})
    job, result = run_contract(routes.generate_for_shot(request, p.session, p.shot,
        GenerationRequest(parameters={'width': 1024, 'height': 576}, prompt_override=override), legacy_production=True))
    assert result is None and job.status == 'queued'
    if override is not None:
        assert requests[0].prompt == override == job.prompt
        assert job.parameters_json['prompt_override'] == override
        assert job.parameters_json['compiled_prompt'] != override
    assert job.parameters_json['character_version_id'] is None
    assert job.reference_assets_json[0]['role'] == 'product'
    assert requests[0].reference_assets[0]['asset_id'] == p.picture.id
    assert requests[0].parameters['reference_manifest']['actors'] == []
    assert not p.session.scalar(select(func.count()).select_from(m.Character))
    shots = routes.list_shots(p.project.id, request, session=p.session)
    assert shots[0]['generation_ready'] is True and shots[0]['participants'] == []


def test_single_shot_generation_resolves_saved_project_reference_video(product_shot, monkeypatch):
    from app.services import providers, video_services

    p = product_shot
    source = p.settings.media_root / 'single-shot-reference.mp4'
    source.write_bytes(b'project-scoped-reference-video')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    reference = m.ReferenceVideo(
        id='single-shot-reference', name='Single-shot native reference', filename=source.name,
        relative_path=source.name, sha256=sha, metadata_json={'duration_seconds': 8},
        state='active', revision=1,
    )
    p.session.add_all([
        reference,
        m.ProjectReferenceVideo(project_id=p.project.id, video_id=reference.id),
        m.ProjectProductionPreference(
            id=p.project.id,
            reference_video_defaults_json=[{
                'video_id': reference.id,
                'expected_revision': reference.revision,
                'source_sha256': sha,
                'trim_start_seconds': 0,
                'trim_end_seconds': 4,
                'authorized_for_reference': True,
                'reference_role': 'action',
                'include_audio': False,
            }],
        ),
    ])
    p.session.commit()

    class NativeContractProvider(ContractProvider):
        name = 'comfyui_h3'

    provider = NativeContractProvider()
    monkeypatch.setattr(video_services, 'select_video_service', lambda *args: (
        None,
        {'service_id': 'native-contract', 'model_id': 'minimax-h3', 'provider': 'comfyui',
         'is_cloud': False, 'settings': {}},
        provider,
    ))
    monkeypatch.setattr(providers, 'freeze_native_ref2va_workflow', lambda parameters: {
        'workflow_name': 'h3-ref2va-native-fast4-v2', 'template_sha256': 'a' * 64,
        'steps': 4, 'graph': {},
    })
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=p.settings))})

    job, result = asyncio.run(routes.generate_for_shot(
        request, p.session, p.shot,
        GenerationRequest(parameters={'width': 1024, 'height': 576}),
    ))

    assert result is None and job.status == 'queued'
    assert [item['video_id'] for item in job.parameters_json['motion_references']] == [reference.id]
    assert [item['video_id'] for item in provider.requests[0].motion_references] == [reference.id]
    assert '<Video 1>' in job.parameters_json['native_reference_constraints']


@pytest.mark.parametrize('kind', ['cloud', 'native'])
def test_direct_generation_respects_selected_native_profile(product_shot, monkeypatch, kind):
    from app.services import ai_services, providers, video_services
    p = product_shot
    provider = ContractProvider()
    if kind == 'native':
        provider.name = 'comfyui_h3'
        provider._load_workflow = lambda: ({}, {})
        monkeypatch.setattr(providers, 'validate_sampling_contract', lambda *_args: 4)
    service = m.AIService(module='video_generation', name='Isolated direct ' + kind,
        provider='video_api' if kind == 'cloud' else 'comfyui',
        base_url='https://example.invalid/v1' if kind == 'cloud' else 'http://127.0.0.1:8189',
        default_model='contract-cloud-video' if kind == 'cloud' else 'minimax_h3_ref2va_pruned_w6a8.safetensors',
        settings_json=({'video_contract': 'video-jobs-v1'} if kind == 'cloud' else
                       {'workflow_profile': 'native_h3_r2v'}))
    p.session.add(service); p.session.commit()
    selected = []
    resolved = []
    real_resolve = ai_services.resolve_service

    def resolve_profile(session, module, service_id=None, project_id=None, model_id=None):
        resolved.append((module, service_id, project_id, model_id))
        return real_resolve(session, module, service_id, project_id, model_id)

    def select_contract(session, settings, project_id, service_id=None, model_id=None, **kwargs):
        assert session is p.session and settings is p.settings and project_id == p.project.id
        assert service_id == service.id and model_id == service.default_model
        selected.append(kwargs)
        return service, ai_services.service_snapshot(service, model_id), provider

    monkeypatch.setattr(ai_services, 'resolve_service', resolve_profile)
    monkeypatch.setattr(video_services, 'select_video_service', select_contract)
    monkeypatch.setattr(routes, 'provider_for', lambda _settings: provider)
    complete_prompt = ('Preserve the supplied product and its packaging. ' * 450).strip()
    original_bytes = p.path.read_bytes()
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=p.settings))})
    # Deliberately omit the internal orchestration override: direct API selection must dispatch
    # by the saved profile, not depend on an internal orchestration override.
    job, result = run_contract(routes.generate_for_shot(request, p.session, p.shot,
        GenerationRequest(service_id=service.id, model_id=service.default_model, prompt_override=complete_prompt,
                          parameters={'width': 1024, 'height': 576})))
    assert result is None and job.status == 'queued'
    assert resolved == [('video_generation', service.id, p.project.id, service.default_model)]
    assert selected == [{}] and len(provider.requests) == 1
    submitted = provider.requests[0]
    assert len(complete_prompt) > 7000
    assert submitted.prompt == job.prompt == complete_prompt
    assert job.parameters_json['prompt_override'] == complete_prompt
    assert job.parameters_json['compiled_prompt'] != complete_prompt
    assert job.parameters_json['native_reference_constraints'] == ''
    assert job.parameters_json['service_snapshot']['service_id'] == service.id
    assert job.parameters_json['service_snapshot']['settings'] == service.settings_json
    assert submitted.parameters['width'] == 1024 and submitted.parameters['height'] == 576
    if kind == 'native':
        assert submitted.parameters['steps'] == 4
    assert submitted.parameters['reference_manifest']['actors'] == []
    assert job.parameters_json['character_version_id'] is None
    assert submitted.reference_assets[0]['asset_id'] == p.picture.id
    assert job.reference_assets_json[0]['role'] == 'product'
    assert p.path.read_bytes() == original_bytes
    assert not p.session.scalar(select(func.count()).select_from(m.Character))


def test_generate_approved_shots_uses_native_h3_direct_profile(product_shot, monkeypatch):
    from app.services import ai_services, video_services

    p = product_shot
    service_id = 'native-h3-service'
    model_id = 'minimax_h3_ref2va_pruned_w6a8.safetensors'
    native_profile = {
        'service_id': service_id,
        'model_id': model_id,
        'provider': 'comfyui',
        'settings': {'workflow_profile': 'native_h3_r2v'},
    }
    resolved = []
    selected = []
    generated = []

    def contract_resolve(session, module, service_id_arg, project_id, model_id_arg):
        assert session is p.session and module == 'video_generation'
        assert service_id_arg == service_id and project_id == p.project.id
        assert model_id_arg == model_id
        resolved.append(service_id_arg)
        return SimpleNamespace(id=service_id), native_profile

    def contract_select(session, settings, project_id, service_id_arg, model_id_arg, **options):
        assert session is p.session and settings is p.settings and project_id == p.project.id
        assert service_id_arg == service_id and model_id_arg == model_id
        selected.append(options)
        return SimpleNamespace(id=service_id), native_profile, SimpleNamespace(name='comfyui_h3')

    async def contract_generate(request, session, shot, payload):
        generated.append(shot.id)
        return SimpleNamespace(), None

    monkeypatch.setattr(ai_services, 'resolve_service', contract_resolve)
    monkeypatch.setattr(video_services, 'select_video_service', contract_select)
    monkeypatch.setattr(routes, 'generate_for_shot', contract_generate)
    monkeypatch.setattr(routes, 'row_dict', lambda _value: {})

    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=p.settings))})
    result = run_contract(routes.generate_approved_shots(request, p.project.id,
        GenerationRequest(service_id=service_id, model_id=model_id), p.session))

    assert resolved == [service_id]
    assert selected == [{}]
    assert generated == [p.shot.id]
    assert result == {'items': [{'job': {}, 'video_revision': None}], 'execution': 'serial'}
