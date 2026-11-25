import json
from dataclasses import dataclass,field
import importlib.util
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace

import pytest

from app.services.context_references import reference_records
from app.services.h3_context_provider import prepare_context_compilation


@pytest.mark.parametrize('camera',['固定','正面固定镜头。','手持 Push In。','Slow Push-in','Pull Out','Slow Pull-back','缓慢后拉','手持跟拍镜头。','平缓后撤','手持手机微晃，轻微向前推进'])
def test_application_camera_contract_is_accepted_by_the_installed_node_schema(camera,monkeypatch):
    directory=Path(__file__).resolve().parents[2]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard'
    package=ModuleType('_camera_contract_test');package.__path__=[str(directory)]
    monkeypatch.setitem(sys.modules,package.__name__,package)
    spec=importlib.util.spec_from_file_location(package.__name__+'.compile_node',directory/'compile_node.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    def execute():pass
    monkeypatch.setitem(execute.__globals__,'T',SimpleNamespace(asset_order=lambda slots:[]))
    _,_,_,policy=prepare_context_compilation('A product.\n机位与运镜：'+camera,'',[],[],{'duration_seconds':3})
    assert policy.get('camera_sentence')
    actual=module.reference_contract(SimpleNamespace(execute=execute),{'slots':[]},production_contract=json.dumps(policy))
    assert actual['camera_sentence']==policy['camera_sentence']
    policy['camera_sentence']='The camera invents an unsupported move.'
    with pytest.raises(ValueError,match='Unsupported frozen camera'):
        module.reference_contract(SimpleNamespace(execute=execute),{'slots':[]},production_contract=json.dumps(policy))


def test_word_camera_default_never_overrides_an_explicit_photography_direction():
    from types import SimpleNamespace
    from app.services.context_production_inputs import imported_camera_direction
    detail=SimpleNamespace(source_json={'suggested':['camera_move']},
        photography_json={'camera_position':'手持 Push In。','shot_size':'手持 Vlog 视角近景微推'})
    actual,audit=imported_camera_direction('固定',detail)
    assert actual=='手持 Push In。' and audit['native_sentence']=='The camera pushes in.'
    detail.photography_json={'camera_position':'手持跟拍镜头。'}
    assert imported_camera_direction('固定',detail)[1]['native_sentence']=='The camera tracks with the action.'
    detail.photography_json={'camera_position':'肩扛绕行观察。'}
    assert imported_camera_direction('固定',detail)[0]=='肩扛绕行观察。'
    detail.source_json={'suggested':[]}
    assert imported_camera_direction('固定',detail)==('固定',None)


def test_word_dialogue_uses_native_verbatim_channel_and_keeps_closed_prop_requirements():
    text='"Time to open our Christmas dim sum surprise!"'
    context={'duration_seconds':3,'audio_policy':'native','screenplay_snapshot':{'dialogues':[{'text':text}]}}
    prompt='拍桌期待\n机位与运镜：手持 Push In。\n道具镜头状态：[{"道具":"盲盒","open_state":"关闭"}]'
    _,_,intent,policy=prepare_context_compilation(prompt,'',[],[],context)
    assert 'native typed dialogue channel' in intent
    assert '@speaks(' not in intent
    assert policy['locked_dialogue'][0]['text']==text
    assert policy['camera_sentence']=='The camera pushes in.'
    assert 'starts CLOSED' in intent and policy['screenplay_prop_states']['盲盒']['open_state']=='关闭'
    _,_,skipped,_=prepare_context_compilation(prompt,'',[],[],{**context,'audio_policy':'skip'})
    assert '@speaks(' not in skipped


def test_word_performer_profiles_reach_native_constraints_when_a_hand_closeup_reveals_a_face():
    people=[{'名称':'Child','人物描述':{'description':'Short curly hair, yellow-red hoodie'},'表演':{'action':'Squeeze the toy, then rub nose'}}]
    _,_,intent,policy=prepare_context_compilation('人物：'+json.dumps(people,ensure_ascii=False),'',[],[],{})
    constraint='\n'.join(policy['locked_production_constraints'])
    assert 'Short curly hair, yellow-red hoodie' in constraint
    assert 'Squeeze the toy, then rub nose' in constraint
    assert 'hands-only opening' in constraint and 'performer profiles' in intent
    assert 'locked_dialogue' not in policy


def test_typed_dialogue_keeps_the_explicit_participant_identity_instead_of_image_order():
    context={'audio_policy':'native','screenplay_snapshot':{
        'participants':[{'id':'mother','name':'Mother'},{'id':'child','name':'Child'}],
        'dialogues':[{'participant_id':'child','text':'Look at this!'},{'participant_id':'mother','text':'Wonderful.'}]}}
    _,_,_,policy=prepare_context_compilation('A family at the table.','',[],[],context)
    assert [row['speaker'] for row in policy['locked_dialogue']]==['Child','Mother']


def test_word_prop_appearance_and_current_state_reach_the_native_constraint_channel():
    specs=[{'名称':'盲盒包装','类别':'prop','内容':{'material':'竹蒸笼，蓝色毛绒提手与彩印封套',
             'color':'蓝色圣诞彩灯封套','open_state':'初始关闭，后被拆开'}}]
    prompt=('A boy holds the package.\n已绑定制作设定：'+json.dumps(specs,ensure_ascii=False)
            +'\n道具镜头状态：'+json.dumps([{'道具':'盲盒包装','open_state':'关闭'}],ensure_ascii=False))
    _,_,_,policy=prepare_context_compilation(prompt,'',[],[],{'duration_seconds':3,'audio_policy':'native'})
    constraints=policy['locked_production_constraints']
    assert len(constraints)==1 and '蓝色毛绒提手与彩印封套' in constraints[0]
    assert '"open_state": "关闭"' in constraints[0] and '初始关闭，后被拆开' not in constraints[0]
    assert 'locked_dialogue' not in policy


def test_unique_written_sections_are_ordered_without_dropping_a_body():
    path=Path(__file__).resolve().parents[2]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard/reference_fusion.py'
    spec=importlib.util.spec_from_file_location('ordered_reference_fusion',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    sections=['subject_definitions','summary','retention_analysis','overall_soundscape','non_diegetic_music','detailed_description']
    prompt='\n\n'.join(name+':\nunique-'+name for name in sections)+'\n'
    actual,receipt=module.order_written_sections(prompt)
    assert receipt['all_section_bodies_preserved']
    assert all(actual.count('unique-'+name)==1 for name in sections)
    assert actual.index('detailed_description:')<actual.index('overall_soundscape:')
    assert module.order_written_sections(actual)==(actual,None)
    duplicate=prompt+'\nsummary:\nanother summary\n'
    assert module.order_written_sections(duplicate)==(duplicate,None)


def test_duplicate_subject_retention_preserves_every_view_and_holder_fact():
    path=Path(__file__).resolve().parents[2]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard/reference_fusion.py'
    spec=importlib.util.spec_from_file_location('retention_reference_fusion',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    prompt=('subject_definitions:\n<Subject 1> is a toy in <Picture 1>, <Picture 2>, <Picture 3>.\n'
        'retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved - the green fabric and star remain.\n'
        '<Subject 2> (appears in [Shot 1]): fully_preserved - the boy keeps his hoodie.\n'
        '<Subject 1> (holder state): fully_preserved - the toy is inside a blue-handled box.\n'
        'detailed_description:\n[Shot 1] Original action and <d>original speech.</d>\n')
    actual,edits=module.merge_retention_rows(prompt)
    retention=actual.split('retention_analysis:')[1].split('detailed_description:')[0]
    assert retention.count('<Subject 1>')==1
    assert all(value in actual for value in ['the green fabric and star remain.','the boy keeps his hoodie.',
        'holder state: the toy is inside a blue-handled box.','<Picture 1>, <Picture 2>, <Picture 3>',
        '[Shot 1] Original action and <d>original speech.</d>'])
    assert edits[0]['all_original_bodies_preserved'] and not edits[0]['source_images_removed']
    assert module.merge_retention_rows(actual)==(actual,[])
    conflicting=prompt.replace('(holder state): fully_preserved','(holder state): locally_modified')
    assert module.merge_retention_rows(conflicting)==(conflicting,[])


def test_installed_analysis_cache_identity_contains_the_real_model_and_server_versions(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[2]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard/reference_analysis.py'
    spec=importlib.util.spec_from_file_location('installed_reference_analysis',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    installed=tmp_path/'comfy/custom_nodes/Guard/reference_analysis.py'
    monkeypatch.setattr(module,'__file__',str(installed))
    bundle=tmp_path/'source/bundle';(bundle/'qwen-metadata').mkdir(parents=True)
    for relative in ['serve_screenplay_qwen.py','serve_h3ir_qwen4b.py','qwen-metadata/factory-model-manifest.json']:
        (bundle/relative).write_text(relative,encoding='utf-8')
    runtime=tmp_path/'runtime';runtime.mkdir()
    owner=runtime/'qwen-owned-process.json'
    owner.write_text(json.dumps({'entry':str(bundle/'serve_h3ir_qwen4b.py'),'pid':1}),encoding='utf-8')
    monkeypatch.setenv('VF_RUNTIME_ROOT',str(runtime))
    original=module.model_source_components()
    assert len(original)==3
    owner.write_text(json.dumps({'entry':str(bundle/'serve_h3ir_qwen4b.py'),'pid':99}),encoding='utf-8')
    assert module.model_source_components()==original
    (bundle/'qwen-metadata/factory-model-manifest.json').write_text('changed model revision',encoding='utf-8')
    assert module.model_source_components()!=original
    owner.unlink()
    with pytest.raises(ValueError,match='model revision'):
        module.model_source_components()


def test_group_voice_is_locked_as_speech_and_contained_products_need_not_be_revealed():
    text='"Bring the holiday magic home today! Click the link to shop!"'
    context={'audio_policy':'native','screenplay_snapshot':{'audio_events':[{'kind':'voice','description':'全家合唱：'+text}]}}
    image={'asset_id':'one','sha256':'a'*64,'product_id':'tree','role':'product','product_name':'green plush tree'}
    _,_,intent,policy=prepare_context_compilation('A closed gift box on the table.','',[image],[],context)
    assert policy['locked_dialogue'][0]['text']==text
    assert policy['locked_dialogue'][0]['speaker']=='全家合唱'
    assert 'enclosed/hidden screenplay states' in intent
    assert '@speaks(' not in intent


def test_verified_mention_binding_preserves_written_facts_and_dialogue():
    path=Path(__file__).resolve().parents[2]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard/reference_fusion.py'
    spec=importlib.util.spec_from_file_location('mention_reference_fusion',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    prompt=('subject_definitions:\n<Subject 1> is water bottle in <Picture 1>.\n'
            '<Subject 2> is green plush tree in <Picture 2>.\nsummary: Products.\n'
            'detailed_description:\n[Shot 1] The water bottle stands beside the green plush tree. '
            '<d>"water bottle"</d>\noverall_soundscape: quiet room.\n')
    targets=[{'subject':'<Subject 1>','descriptor':'water bottle','sources':['<Picture 1>']},
             {'subject':'<Subject 2>','descriptor':'green plush tree','sources':['<Picture 2>']}]
    actual,edits=module.bind_existing_mentions(prompt,targets)
    assert len(edits)==2
    assert '[Shot 1] The <Subject 1> (water bottle) stands beside the <Subject 2> (green plush tree).' in actual
    assert '<d>"water bottle"</d>' in actual
    assert all(not edit['visual_facts_added'] and not edit['source_text_removed'] for edit in edits)
    repeated,edits=module.bind_existing_mentions(actual,targets)
    assert repeated==actual and edits==[]
    ambiguous=prompt.replace('green plush tree','red water bottle')
    unchanged,edits=module.bind_existing_mentions(ambiguous,targets[:1])
    assert unchanged==ambiguous and edits==[]


def test_global_fusion_uses_verified_source_bindings_and_keeps_every_view():
    path=Path(__file__).resolve().parents[2]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard/reference_fusion.py'
    spec=importlib.util.spec_from_file_location('isolated_reference_fusion',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    @dataclass
    class Subject:
        label:str
        descriptor:str
        sources:list[str]
        attributes:list[str]=field(default_factory=list)
    subjects=[Subject('<Subject 1>','bottle front',['<Picture 1>'],['orange pattern']),
              Subject('<Subject 2>','hand',['<Picture 2>'],['bare fingers']),
              Subject('<Subject 3>','bottle side',['<Picture 2>'],['black lid']),
              Subject('<Subject 4>','green toy',['<Picture 3>'],['green fabric'])]
    contract={'groups':[{'category':'product','group':'explicit-cup-sku',
                         'appearance_native':['<Picture 1>','<Picture 2>']}]}
    proof={'status':'PASS','approved_product_descriptors_by_native':{
        '<Picture 1>':['bottle front'],'<Picture 2>':['bottle side']}}
    actual,receipt=module.fuse_product_subjects(subjects,contract,proof)
    assert len(actual)==2 and actual[0].sources==['<Picture 1>','<Picture 2>']
    assert actual[0].attributes==['orange pattern','black lid']
    assert actual[1].descriptor=='green toy'
    assert receipt['auxiliary_context_observations'][0]['descriptor']=='hand'
    assert len(subjects)==4 and subjects[0].sources==['<Picture 1>']
    assert receipt['image_inputs_removed'] is False
    unverified,_=module.fuse_product_subjects(subjects,contract,{'status':'BLOCKED'})
    assert unverified is subjects
    proof['approved_product_descriptors_by_native']['<Picture 2>']=['unknown object']
    with pytest.raises(ValueError,match='uniquely bind'):
        module.fuse_product_subjects(subjects,contract,proof)


@pytest.mark.parametrize('count', [0, 1, 2, 3, 4, 6, 8])
def test_all_dynamic_source_records_survive_compiler_preparation(count):
    images = [{'asset_id': f'image-{i}', 'sha256': f'{i:064x}', 'relative_path': f'products/{i}.jpg',
               'product_id': 'same-product' if i < 2 else f'product-{i}', 'role': 'product',
               'product_name': 'test product', 'image_notes': f'image note {i}'} for i in range(count)]
    tray, declarations, intent, policy = prepare_context_compilation(
        'Show the referenced products on the table.', '', images, [], {'duration_seconds': 5.167}, silent=True)
    assert len(tray) == count
    assert [r['sha256'] for r in policy['references']] == [r['sha256'] for r in images]
    assert [r['image_description'] for r in policy['references']] == [r['image_notes'] for r in images]
    assert [r['product_id'] for r in policy['references']] == [r['product_id'] for r in images]
    assert policy['reference_schema_version'] == 'context-references-v1'


def test_reference_schema_with_runtime_validator():
    jsonschema = pytest.importorskip('jsonschema')
    schema = json.loads((Path(__file__).resolve().parents[2] / 'schemas/context-reference-set-v1.json').read_text())
    for count in [0, 1, 2, 3, 4, 6, 8]:
        records = reference_records([{'asset_id': str(i), 'sha256': f'{i:064x}'} for i in range(count)])
        jsonschema.validate({'reference_schema_version': 'context-references-v1', 'references': records}, schema)


def test_unknown_identity_stays_unknown_and_same_hash_keeps_distinct_occurrences():
    images = [{'asset_id': 'one', 'sha256': 'a'*64, 'relative_path': 'library/one.png'},
              {'asset_id': 'one', 'sha256': 'a'*64, 'relative_path': 'library/one.png'}]
    records = reference_records(images)
    assert all(r['product_id'] is None and r['subject_id'] is None for r in records)
    assert len({r['image_id'] for r in records}) == 2
