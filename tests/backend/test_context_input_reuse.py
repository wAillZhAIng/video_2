from copy import deepcopy

import pytest

from app.services.context_input_reuse import context_reuse_reason


def example():
    return {'fingerprint':'before','generation_inputs_hash':'unchanged-generation-content',
            'compiler_input_contract_version':'native-typed-word-v1','prompt':'Original words',
            'references':[{'image_id':'a','sha256':'1'*64},{'image_id':'b','sha256':'2'*64},{'image_id':'c','sha256':'3'*64}],
            'screenplay_snapshot':{'shot':{'revision':1,'action':'Open the box','status':'draft','approved_at':None,'updated_at':'old'},
                'detail':{'updated_at':'old','source_json':{'word_source':'paragraph:18'}},
                'participants':[{'id':'child','name':'Child','description':'Short curls, yellow-red hoodie'}],
                'dialogues':[{'participant_id':'child','text':'The complete original line.'}],
                'cinematic':{'preflight':{'generation_inputs_hash':'unchanged-generation-content','ready':False,
                    'findings':[{'code':'needs_review','severity':'review'}],'counts':{'review':1}}}}}


def test_only_operational_approval_metadata_changes_reuse_without_mutating_snapshots():
    before=example();after=deepcopy(before);after['fingerprint']='after'
    shot=after['screenplay_snapshot']['shot'];shot.update(status='approved',approved_at='now',updated_at='now')
    detail=after['screenplay_snapshot']['detail'];detail.update(updated_at='now');detail['source_json']['studio_acceptance']={'automated':True}
    after['screenplay_snapshot']['cinematic']['preflight'].update(ready=True,findings=[],counts={'review':0})
    frozen=deepcopy(before);current=deepcopy(after)
    assert context_reuse_reason(before,after)=='identical_content_approval_metadata_only'
    assert before==frozen and after==current


@pytest.mark.parametrize('field',['image_hash','missing_third_image','image_order','prompt','contract','revision','performer','dialogue','generation_hash','word_source'])
def test_material_model_input_changes_never_reuse(field):
    before=example();after=deepcopy(before);after['fingerprint']='different'
    if field=='image_hash':after['references'][2]['sha256']='4'*64
    elif field=='missing_third_image':after['references'].pop()
    elif field=='image_order':after['references'].reverse()
    elif field=='prompt':after['prompt']='Different words'
    elif field=='contract':after['compiler_input_contract_version']='new-contract'
    elif field=='revision':after['screenplay_snapshot']['shot']['revision']=2
    elif field=='performer':after['screenplay_snapshot']['participants'][0]['description']='Different performer'
    elif field=='dialogue':after['screenplay_snapshot']['dialogues'][0]['text']='Another line'
    elif field=='generation_hash':after['generation_inputs_hash']='changed'
    elif field=='word_source':after['screenplay_snapshot']['detail']['source_json']['word_source']='paragraph:99'
    assert context_reuse_reason(before,after) is None


def test_legacy_exact_fingerprint_remains_readable_but_unknown_diagnostics_do_not_relax_comparison():
    assert context_reuse_reason({'fingerprint':'legacy'},{'fingerprint':'legacy'})=='identical_fingerprint'
    assert context_reuse_reason({}, {}) is None
    a=example();b=deepcopy(a);b['fingerprint']='after';b.pop('generation_inputs_hash')
    assert context_reuse_reason(a,b) is None


@pytest.mark.parametrize('state',['rejected','deleted','needs_revision'])
def test_rejection_and_current_preflight_errors_cannot_be_treated_as_approval_only_changes(state):
    a=example();b=deepcopy(a);b['fingerprint']='after'
    b['screenplay_snapshot']['shot']['status']=state
    assert context_reuse_reason(a,b) is None
    b['screenplay_snapshot']['shot']['status']='approved'
    b['screenplay_snapshot']['cinematic']['preflight']['findings']=[{'severity':'error','code':'invalid_source'}]
    assert context_reuse_reason(a,b) is None
