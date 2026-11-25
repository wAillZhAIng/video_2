"""Opt-in, scoped compatibility policy for the fixed offline 4B OpenH3 compiler.

No weights, services or vendor modules are imported at module load. Wrappers are
installed once, but do nothing outside an explicit ContextVar scope. Policy
changes decoding budgets, declared-reference metadata and image sizing; caller
text/media and the original model reply remain intact in their audit records.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
import datetime as dt
import base64
import difflib
import functools
import hashlib
import importlib.metadata
import inspect
import json
import os
import re
from pathlib import Path
import ipaddress
import socket
import threading
import time
from types import SimpleNamespace
import unicodedata
from urllib.parse import urlsplit
import uuid

from .media_bindings import ground_writer_messages, repair_media_bindings, written_media_tags


VERSION='local-h3ir-qwen4b-v3'
POLICY_REVISION='multireference-provenance-v29'
REFERENCE_GRAMMAR_SHA256='1e574f356716ad55612247ffb7bbccbcdb484ad96599d63c7dca1af186b1fab7'
MODEL='Qwen/Qwen3-VL-4B-Instruct'
MAX_OUTPUT=4096
CARD_OUTPUT=3072
_ACTIVE=ContextVar('factory_local_h3ir_profile',default=None)
_STAGE=ContextVar('factory_local_h3ir_stage',default=None)
_CALL=ContextVar('factory_local_h3ir_call',default=None)
_COMPOSE_MEDIA=ContextVar('factory_local_h3ir_compose_media',default=None)
_REPAIR_SAMPLING=ContextVar('factory_local_h3ir_repair_sampling',default=None)
_REFERENCE_VISIBILITY_GUIDANCE=(
    'A reference identity binding does not require every reference object to become visible. '
    'Visibility and package open/closed state come from the caller-authored current shot. '
    'Still cite each required Subject in detailed_description. If the caller or existing scene '
    'already establishes that an item is inside a closed container, preserve that established '
    'containment/occlusion relation with its Subject token instead of opening the container or '
    'revealing the item just to satisfy binding. Never invent containment, hide a requested '
    'visible object, or change the requested action to obtain a passing binding.')


def _native_correction_sampling(scope,index):
    # The native compiler owns the unchanged two-round limit. Repeating the
    # same faulty text at the same low temperature/seed gave byte-identical
    # replies. Vary only these already-authorized correction calls, not asset
    # analysis or the first composition; never add a retry or alter a verdict.
    return {'round':index,'temperature':0.7 if index==1 else 0.9,
            'seed':(scope.get('seed',0)+104729*index)%(2**63)}


def _effective_sampling(scope):
    repair=_REPAIR_SAMPLING.get()
    if repair is not None:return repair['temperature'],repair['seed']
    return 0.2,scope.get('analysis_seed',scope['seed'])


_REFERENCE_SECTION_NAMES=('subject_definitions','summary','retention_analysis',
                          'detailed_description','overall_soundscape','non_diegetic_music')


def _reference_correction_schema():
    descriptions={
        'subject_definitions':'Complete corrected original subject definitions with all actual reference bindings. Do not merge a package with its contents or invent a new identity.',
        'summary':'Complete corrected original scene summary, consistent with the caller current-shot state.',
        'retention_analysis':'Complete corrected native retention rows; preserve all original image sources and compatible identity details.',
        'detailed_description':'Complete corrected numbered shots. Explicitly cite the SAME existing media Subject tokens required by the findings in these shots, rather than only in the summary. Preserve existing containment without revealing concealed contents, all performer/action/camera constraints, and each complete native dialogue block.',
        'overall_soundscape':'Complete corrected original soundscape.',
        'non_diegetic_music':'Complete corrected original music requirement, or N/A when absent.'}
    return {'type':'object','additionalProperties':False,
            'properties':{name:{'type':'string','minLength':1,'description':descriptions[name]}
                          for name in _REFERENCE_SECTION_NAMES},
            'required':list(_REFERENCE_SECTION_NAMES)}


def _detail_only_reference_correction(written,findings):
    """Narrow section repair only for an already uniquely verified definition."""
    from h3ir.compile import _split_written
    sections=_split_written(written)
    binding=[f for f in findings if f.rule=='F5-declared-group-binding']
    return (set(sections)==set(_REFERENCE_SECTION_NAMES) and bool(binding)
            and all(f.rule in {'F5-declared-group-binding','D4-dialogue-not-verbatim',
                              'D10-dialogue-line-duplicated','F8-closed-contents-revealed',
                              'F9-correction-added-scene-transition','F10-hidden-visible-retention'} for f in findings)
            and all(re.search(r'The existing declared target <Subject \d+> is already defined .*?never cited in detailed_description',f.msg,re.S)
                    for f in binding))


def _qualified_reference_correction_subjects(written,findings,scope):
    if _detail_only_reference_correction(written,findings):
        return list(dict.fromkeys(re.search(r'The existing declared target (<Subject \d+>)',f.msg)[1]
                     for f in findings if f.rule=='F5-declared-group-binding'))
    allowed={'D4-dialogue-not-verbatim','D10-dialogue-line-duplicated',
             'F8-closed-contents-revealed','F9-correction-added-scene-transition','F10-hidden-visible-retention'}
    if not findings or any(f.rule not in allowed for f in findings):return []
    from h3ir.compile import _split_written
    sections=_split_written(written)
    if (set(sections)!=set(_REFERENCE_SECTION_NAMES)
            or digest(sections['subject_definitions'])!=scope.get('latest_reference_definitions_sha256')):return []
    rows=(scope.get('latest_reference_audit') or {}).get('group_results') or []
    if not rows or any(row.get('status')!='PASS' or not row.get('subject') for row in rows):return []
    return list(dict.fromkeys(row['subject'] for row in rows))


def _binding_fragment_schema(subjects,section,summary=None,concealed_subjects=()):
    if not subjects or len(set(subjects))!=len(subjects) or any(not re.fullmatch(r'<Subject \d+>',s) for s in subjects):
        raise LocalProfileFailure('A structured binding repair requires actual existing unique Subject labels')
    if len(set(concealed_subjects))!=len(concealed_subjects) or any(s not in subjects for s in concealed_subjects):
        raise LocalProfileFailure('Concealed retention must be a unique subset of the actually verified repair subjects')
    properties={}
    if summary is not None:
        properties['summary']={'type':'string','minLength':1,'maxLength':len(summary)+512,
            'description':'Complete corrected native summary consistent with the caller CLOSED container state. Its established contents remain concealed throughout; do not narrate a reveal or opening.'}
    for index,subject in enumerate(subjects,1):
        properties[f'before_subject_{index}']={'type':'string','minLength':1,'maxLength':len(section)+512,
            'description':'The next English text span of the COMPLETE corrected detailed_description before this exact Subject token. Split the section around the required tokens; do not repeat the whole section in each span.'}
        properties[f'subject_{index}']={'type':'string','const':subject,
            'description':'Actual existing declared Subject identity required by the native finding. Its presence does not require revealing concealed contents.'}
    properties['tail']={'type':'string','minLength':1,'maxLength':len(section)+512,
        'description':'The remaining English text AFTER the last required Subject token. Together all text spans and Subject values form the entire corrected detailed_description, including all original action and complete dialogue.'}
    if concealed_subjects:
        properties['concealed_retention']={'type':'object','additionalProperties':False,
            'properties':{f'item_{index}':{'type':'object','additionalProperties':False,
                'properties':{'subject':{'type':'string','const':subject},
                    'relationship':{'type':'string','const':'weak_reference'},
                    'reason':{'type':'string','minLength':1,'maxLength':768,
                        'description':'Native visual retention reason: only broad product-category/context reference for these concealed contents. No visible appearance is retained in this shot. The formatter preserves the mandatory existing native shot-association scope. Preserve the unchanged product identity/source definitions. Do not change the package, performer or any other subject.'}},
                'required':['subject','relationship','reason']}
                for index,subject in enumerate(concealed_subjects,1)},
            'required':[f'item_{index}' for index in range(1,len(concealed_subjects)+1)]}
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


def _serialize_reference_correction(content,original=None,required_subjects=(),replace_summary=False,concealed_subjects=()):
    """Format actual model-written sections; never insert a Subject or visual fact."""
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise LocalProfileFailure('Duplicate native correction section: '+key)
            value[key]=item
        return value
    try:sections=json.loads(content,object_pairs_hook=unique)
    except (ValueError,TypeError) as error:
        raise LocalProfileFailure('Native reference correction returned invalid structured sections; no prose fallback') from error
    if required_subjects:
        if original is None:raise LocalProfileFailure('Fragment correction requires the complete original model candidate')
        from h3ir.compile import _split_written
        original_sections=_split_written(original)
        schema=_binding_fragment_schema(required_subjects,original_sections['detailed_description'],
                                       original_sections['summary'] if replace_summary else None,concealed_subjects)
        if (not isinstance(sections,dict) or set(sections)!=set(schema['properties'])
                or any(not isinstance(value,str) or not value.strip() for key,value in sections.items() if key!='concealed_retention')
                or any(sections[f'subject_{i}']!=subject for i,subject in enumerate(required_subjects,1))):
            raise LocalProfileFailure('Model binding fragments omit or change an actual declared identity')
        parts=[]
        for index in range(1,len(required_subjects)+1):
            parts.extend((sections[f'before_subject_{index}'],sections[f'subject_{index}']))
        parts.append(sections['tail'])
        corrected={'detailed_description':' '.join(parts),**({'summary':sections['summary']} if replace_summary else {})}
        if concealed_subjects:
            rows=sections.get('concealed_retention')
            expected_rows={f'item_{index}' for index in range(1,len(concealed_subjects)+1)}
            if not isinstance(rows,dict) or set(rows)!=expected_rows:
                raise LocalProfileFailure('Concealed retention correction omitted a real Subject row')
            retention=original_sections['retention_analysis']
            for index,subject in enumerate(concealed_subjects,1):
                row=rows[f'item_{index}']
                if (not isinstance(row,dict) or set(row)!={'subject','relationship','reason'}
                        or row['subject']!=subject or row['relationship']!='weak_reference'
                        or not isinstance(row['reason'],str) or not row['reason'].strip()
                        or any(c in row['reason'] for c in '\r\n')):
                    raise LocalProfileFailure('Concealed retention row changed identity/role or added another row')
                pattern=r'(?m)^([ \t]*'+re.escape(subject)+r'[ \t]*(?:\([^\n]*\)[ \t]*)?:)[^\n]*$'
                if len(re.findall(pattern,retention))!=1:
                    raise LocalProfileFailure('Concealed retention needs one unique existing row; no row was guessed')
                # R3 mandates the native appears-in association even for a
                # weak/context reference. Preserve its exact existing scope;
                # do not invent a negative-scope grammar or weaken R3.
                prefix=re.search(pattern,retention)[1]
                model_line=prefix+' '+row['relationship']+' - '+row['reason']
                retention=re.sub(pattern,lambda _:model_line,retention,count=1)
            corrected['retention_analysis']=retention
        sections=corrected
    expected=({'detailed_description',*(['summary'] if replace_summary else []),*(['retention_analysis'] if concealed_subjects else [])}) if original is not None else set(_REFERENCE_SECTION_NAMES)
    if (not isinstance(sections,dict) or set(sections)!=expected
            or any(not isinstance(value,str) or not value.strip() for value in sections.values())):
        raise LocalProfileFailure('Native reference correction omitted or changed a required section; no partial repair accepted')
    if original is not None:
        from h3ir.compile import _split_written
        existing=_split_written(original)
        if set(existing)!=set(_REFERENCE_SECTION_NAMES):
            raise LocalProfileFailure('Cannot preserve missing original sections during a targeted model repair')
        sections={**existing,**sections}
    return '\n\n'.join(name+':\n'+sections[name] for name in _REFERENCE_SECTION_NAMES)+'\n'


def _missing_caller_dialogue_blocks(written,rows,plan):
    from h3ir.compile import _split_written
    from h3ir.repair import normalize
    detail=_split_written(written)['detailed_description']
    present=[normalize(body) for body in re.findall(r'<d>(.*?)</d>',detail,re.S)]
    speakers=getattr(plan,'speakers',[]) if plan is not None else []
    result=[]
    for row in rows:
        if any(normalize(row['text']) in body for body in present):continue
        speaker=next((s for s in speakers if row.get('speaker') and s.descriptor==row['speaker']),None)
        if speaker is None and len(speakers)==1:speaker=speakers[0]
        prefix=(speaker.sid+': ') if speaker is not None else ''
        block=prefix+'<d>['+row['language']+'] '+row['text']+'</d>'
        result.append(block)
        present.append(normalize('['+row['language']+'] '+row['text']))
    return result


def _small_concealed_patch_schema(subjects,concealed,dialogue_blocks):
    # This is an EDIT, not another rewrite of the complete scene. Never force
    # an arbitrary character boundary inside model-written prose to obtain
    # valid JSON; real EOS and all native checks are still required.
    schema=_binding_fragment_schema(subjects,'','',concealed)
    def uncap(value):
        if isinstance(value,dict):
            value.pop('maxLength',None)
            for child in value.values():uncap(child)
        elif isinstance(value,list):
            for child in value:uncap(child)
    uncap(schema)
    props=schema['properties']
    props['summary']['description']='One or two COMPLETE English native summary sentences, beginning [reference generation], preserving the caller closed package and unseen contents.'
    props['summary']['enum']=[
        '[reference generation] The closed container keeps its established contents concealed throughout this shot.',
        '[reference generation] The sealed package remains closed, with its established contents unseen.']
    for index in range(1,len(subjects)+1):
        props[f'before_subject_{index}']['description']='A SHORT natural English clause prefix immediately before this existing Subject token. Only the missing binding statement, not the original scene or a subject definition.'
        props[f'before_subject_{index}']['enum']=(['The established contents,','The concealed contents,'] if index==1 else ['and','together with'])
    props['tail']['description']='A SHORT complete natural English ending of the binding statement. Preserve the established concealed state. Do not repeat any scene, definition or dialogue.'
    props['tail']['enum']=[', remain concealed inside the closed container.',', stay unseen within the sealed package.']
    for item in props['concealed_retention']['properties'].values():
        item['properties']['reason']['description']='One short native retention reason: broad product-category context only; this concealed object has no visible appearance in this shot.'
        item['properties']['reason']['enum']=[
            'Broad product-category context only; no visible appearance is retained in this shot.',
            'Category context for the concealed contents only; their appearance remains unseen.']
    for index,block in enumerate(dialogue_blocks,1):
        props[f'native_dialogue_{index}']={'type':'string','const':block,
            'description':'Complete caller-authored native dialogue block, copied verbatim exactly once.'}
    schema['required']=list(props)
    return schema


def _apply_small_concealed_patch(written,content,subjects,concealed,dialogue_blocks):
    from h3ir.compile import _split_written
    schema=_small_concealed_patch_schema(subjects,concealed,dialogue_blocks)
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise LocalProfileFailure('Duplicate small reference patch field: '+key)
            value[key]=item
        return value
    try:fields=json.loads(content,object_pairs_hook=unique)
    except (TypeError,ValueError) as error:raise LocalProfileFailure('Small reference patch is not complete JSON') from error
    if not isinstance(fields,dict) or set(fields)!=set(schema['properties']):
        raise LocalProfileFailure('Small reference patch omitted or added a field')
    native=[]
    for index,expected in enumerate(dialogue_blocks,1):
        actual=fields.pop(f'native_dialogue_{index}')
        if actual!=expected:raise LocalProfileFailure('Small reference patch changed the complete caller dialogue')
        native.append(actual)
    def check_words(value,definition):
        if 'enum' in definition and value not in definition['enum']:
            raise LocalProfileFailure('Small patch changed wording outside the supported closed-state vocabulary')
        if isinstance(value,dict):
            for key,item in value.items():check_words(item,definition.get('properties',{}).get(key,{}))
    for key,value in fields.items():check_words(value,schema['properties'][key])
    partial=_serialize_reference_correction(json.dumps(fields,ensure_ascii=False),written,subjects,True,concealed)
    sections=_split_written(partial);old=_split_written(written)
    binding=sections['detailed_description']
    if any(tag in binding for tag in ('<d>','</d>','<scenetrans>','[Shot ')):
        raise LocalProfileFailure('A small reference binding patch attempted to add dialogue or a scene control')
    sections['detailed_description']=old['detailed_description']+'\n'+binding+''.join('\n'+block for block in native)
    return '\n\n'.join(name+':\n'+sections[name] for name in _REFERENCE_SECTION_NAMES)+'\n'


def _can_append_concealed_patch(concealed,findings,subjects=()):
    # An append cannot correct an existing reveal, duplicate utterance or cut.
    # Such candidates still need the original native full correction route.
    allowed={'F5-declared-group-binding','F10-hidden-visible-retention','D4-dialogue-not-verbatim'}
    # The summary is replaced by this patch, so a summary-only reveal can be
    # corrected. A bad detailed body must never be preserved verbatim. Mixed
    # visible/missing identities need the complete contextual correction.
    return (bool(concealed) and bool(findings) and (not subjects or set(subjects)==set(concealed))
            and all(f.rule in allowed or (f.rule=='F8-closed-contents-revealed'
                    and f.msg.startswith('summary claims a reveal ')) for f in findings))


def _write_small_concealed_patch(backend,written,scope,subjects,concealed,caller_rules,rows,findings):
    from h3ir.compile import _split_written
    plans=scope.get('written_plans') or []
    plan=plans[-1][1] if plans else None
    blocks=_missing_caller_dialogue_blocks(written,rows,plan)
    schema=_small_concealed_patch_schema(subjects,concealed,blocks)
    # This narrow edit cannot change the original action, performers, camera
    # or prop specifications: those bodies are retained verbatim. Feeding a
    # large unchanged screenplay back to the 4B writer caused it to copy that
    # material into every field. Send only the supported closed-state rules;
    # record all untouched constraints by hash, not as another rewrite task.
    closed_rules=[rule for rule in caller_rules if re.search(
        r'"open_state"\s*:\s*"(?:关闭|closed|sealed)"',rule,re.I)]
    task={'existing_subject_ids':subjects,'concealed_subject_ids':concealed,
          'established_relationship':'The concealed identities are already established contents of the single caller CLOSED package. They remain unseen throughout this shot.',
          'caller_closed_container_rules':closed_rules,'untouched_caller_constraints_sha256':digest(caller_rules),
          'complete_missing_native_dialogue':blocks,
          'actual_rule_ids':[f.rule for f in findings],
          'edit_scope':'Original scene/action/camera/performance text is retained VERBATIM outside this patch. Do not rewrite or repeat it. Add only a short missing reference statement, concise consistent summary, and the requested concealed retention reason.'}
    example={'summary':schema['properties']['summary']['enum'][0],
        'before_subject_1':schema['properties']['before_subject_1']['enum'][0],'subject_1':subjects[0],
        'tail':schema['properties']['tail']['enum'][0],
        'concealed_retention':{'item_1':{'subject':concealed[0],'relationship':'weak_reference',
            'reason':schema['properties']['concealed_retention']['properties']['item_1']['properties']['reason']['enum'][0]}}}
    for index,block in enumerate(blocks,1):example[f'native_dialogue_{index}']=block
    messages=[{'role':'system','content':'Return a SMALL native H3 correction patch as JSON. Write one short grammatical binding statement split around the given existing Subject token fields, and a concise complete summary beginning [reference generation]. The explicitly concealed object stays unseen INSIDE the CLOSED package; no opening or reveal. Keep its product identity, do not turn the package into the toy. Other scene text and retention rows are preserved separately, so do not repeat the scene, definitions, instructions or dialogue in prose fields. Copy any required native_dialogue field verbatim. Each natural-language value should be one short complete clause or sentence. End the JSON normally; no Markdown or explanation.'},
              {'role':'user','content':json.dumps(task,ensure_ascii=False)+'\nConcise one-subject format example (use the actual schema for all required subjects):\n'+json.dumps(example,ensure_ascii=False)+'\nReturn only the short actual JSON patch now.'}]
    stage={'kind':'native_reference_patch','original_messages':deepcopy(messages),
           'original_schema':deepcopy(schema),'effective_schema':deepcopy(schema),'schema_changes':[]}
    token=_STAGE.set(stage)
    try:
        reply=backend.chat(messages,thinking=False,max_tokens=MAX_OUTPUT,
            response_format={'type':'json_schema','json_schema':{'name':'native_reference_patch','strict':True,'schema':schema}})
    finally:_STAGE.reset(token)
    result=_apply_small_concealed_patch(written,reply.content,subjects,concealed,blocks)
    before=_split_written(written);after=_split_written(result)
    preserved=after['detailed_description'].startswith(before['detailed_description']+'\n')
    if not preserved:raise LocalProfileFailure('Small reference patch rewrote the original detailed action')
    write_json(scope['directory']/f"small-reference-patch-{scope['repair_contexts']:03d}.json",{
        'schema_sha256':digest(schema),'model_response_sha256':digest(reply.content),
        'original_candidate_sha256':digest(written),'result_sha256':digest(result),
        'original_detailed_body_preserved_verbatim':preserved,
        'untouched_caller_constraints_sha256':digest(caller_rules),
        'closed_state_rules_provided_to_model':closed_rules,
        'added_dialogue_blocks':len(blocks),'free_text_character_limit':False,
        'state_wording':'schema-constrained noncreative closed-state vocabulary; no new appearance or action facts',
        'creative_words_inserted_by_python':False,'native_gates_unchanged':True})
    return SimpleNamespace(content=result)
_INSTALL_LOCK=threading.RLock()
COMPILER_CALL_SHA256='114d98c174d3d34d020e63aacf6d2fbcc8afc28b4ab944abb794f9fc507122fa'
BRIEF_CONVERTER_SHA256='4b3d58dd3768817fc73250b3c49aa20f0575d6c761941afd0668a62dce457f4b'
SERIALIZER_SHA256='e03828302d1134ca5cef228c2c0e08cad92631ef7dc182e803c2de16248dc414'
RENDER_FIELDS_SHA256='6e2900fadda2d5ca7362f01af771c4862127daba11874b5a6c536fc8c6d601c4'
CONDITION_SHA256='ad11fdeabba7c3f2a73ddc1c2821ea58e048c45dc30edf4a02fa932ef158883c'
STOCK_H3_SHA256='108ceff11e5a5c9a34a57bbc380447cf9941beb673a1bd421abc818de54ccd5c'
WRITTEN_CONTEXT_SHA256='15934b25540728207199551947ce7b4a6de80cccf82ee7b96f529956387a0256'
WRITTEN_VALIDATE_SHA256='b84ea591ee561790dce790dd81c90796cf4a631e1485e24a59d8e84e79c29c79'
COMPILE_BRIEF_SHA256='c75bdd18e15b9e1a1ac1feb06457f241e24e68ff7f00d8a10113a3bdcbc9dfd8'
COMPOSE_BRIEF_SHA256='ba398867bdff893f2dfac97cb23caa0e33a73fa7349a5e4956a66bc4ed0a3df0'
FIX_WITH_FINDINGS_SHA256='0c9ac3ac2c296a507f07b1ea06c4eab12ea777eb8d2719d21bc87ff6205a6e59'
REPAIR_BRIEF_SHA256='fa9740d40b41f6e66ae7b13750b457c4284768744c45e1386a534dd4f9b5026e'
FIX_LABELS_SHA256='d0f6d472700a5f64b3e0d02a17c7ec3b5a762ec092cae1748d44fbe9897a0f40'
RENDER_IR_SHA256='a71e800d15624be4624b7cea1d5507ffcb22e7e218f9a6be37410cab8303da42'
ASSESS_SHA256='5d1218f03aa822deb9aee055ce858d5049af67ae1da6f1dea7a4200155a14a53'
BUILD_SUBJECTS_SHA256='003a82491fb1f02a447a1e3625c216b882c0c097e591414c6d5b563559e150ba'
_CAMERA_REPAIR='restored the frozen camera sentence in the numbered shot'
_VERBATIM_SPANS=re.compile(r'<d>.*?</d>|"[^"]*"|“([^"”]*)”',re.S)
_DESCRIPTIVE_RETENTION = frozenset({
    'scene', 'scene state', 'setting', 'location', 'background', 'lighting', 'lens', 'camera',
    'camera movement', '场景', '场景状态', '背景', '灯光', '机位', '运镜',
})
_DESCRIPTIVE_ROW = re.compile(
    r'[ \t]*(?:[-*][ \t]+)?(?P<label><[^<>\r\n]+>|[\w -]{1,32})[ \t]*'
    r'(?:\(appears in \[Shot 1\]\)[ \t]*)?[:：][ \t]*(?P<text>\S[^\r\n]*)',
    re.I,
)
_REFERENCE_FINDING_RULES=frozenset(('F2-duplicate-definition','F3-required-reference',
    'F4-conflicting-groups','F5-declared-group-binding','F6-declared-binding-projection',
    'F8-closed-contents-revealed','F9-correction-added-scene-transition','F10-hidden-visible-retention',
    'F7-locked-onscreen-text'))


class LocalProfileFailure(RuntimeError):
    """Deliberately not BackendError: upstream must not hide this as draft fallback."""


class LocalProfileBlocked(LocalProfileFailure):
    """The compiler produced a document that cannot be handed to H3."""


def source_hash(function):
    return hashlib.sha256(inspect.getsource(function).encode('utf-8')).hexdigest()


def _reference_repair_grammar():
    """Read the actual pinned native retention grammar, never an invented schema."""
    from h3ir.prose import get_config
    raw=(get_config().paths.spec_dir/'ref-en.txt').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=REFERENCE_GRAMMAR_SHA256:
        raise LocalProfileFailure('Native reference grammar changed; repair syntax was not guessed')
    text=raw.decode('utf-8')
    return text[text.index('## 4. `retention_analysis`'):text.index('## 5. `detailed_description`')]


def _normalize_structural_text(text, table):
    """Translate the validator's known hazards while preserving literal video content."""
    parts = []
    offset = 0
    for match in _VERBATIM_SPANS.finditer(text):
        parts.append(text[offset:match.start()].translate(table))
        caption = match[1]
        parts.append(match[0] if caption is None else '"' + caption + '"')
        offset = match.end()
    parts.append(text[offset:].translate(table))
    return ''.join(parts)


def _relocate_descriptive_retention(text):
    """Keep non-subject descriptions in the existing single-shot prose."""
    if not isinstance(text, str):
        return text, []
    headings = list(re.finditer(r'(?m)^(retention_analysis|detailed_description|overall_soundscape):[ \t]*\r?$', text))
    if [entry[1] for entry in headings] != ['retention_analysis', 'detailed_description', 'overall_soundscape']:
        return text, []
    retention, detail, sound = headings
    prose = text[detail.end():sound.start()]
    if re.findall(r'\[Shot \d+\]', prose) != ['[Shot 1]']:
        return text, []
    rows, moved = [], []
    for line in text[retention.end():detail.start()].splitlines(keepends=True):
        row = _DESCRIPTIVE_ROW.fullmatch(line.rstrip('\r\n'))
        label = re.sub(r'[-_\s]+', ' ', row['label'].strip('<> ').casefold()) if row else ''
        if label not in _DESCRIPTIVE_RETENTION:
            rows.append(line)
            continue
        sentence = re.sub(r'^fully_preserved\s+[-–—]\s+', '', row['text'], flags=re.I).strip()
        if not sentence or any(character in sentence for character in '<>[]'):
            rows.append(line)
            continue
        moved.append({'label': label, 'text': sentence})
    if not moved or not any(line.strip() for line in rows):
        return text, []
    end = sound.start() - (len(prose) - len(prose.rstrip()))
    suffix = ' '.join(dict.fromkeys(item['text'] for item in moved if item['text'] not in prose))
    before_sound = text[detail.start():end] + (' ' + suffix if suffix else '') + text[end:sound.start()]
    return text[:retention.end()] + ''.join(rows) + before_sound + text[sound.start():], moved


def _format_descriptive_retention(text, scope):
    formatted, moved = _relocate_descriptive_retention(text)
    if moved:
        number = scope.get('descriptive_retention_passes', 0) + 1
        scope['descriptive_retention_passes'] = number
        write_json(scope['directory']/f'descriptive-retention-{number:03d}.json', {
            'source_request_number': scope.get('requests'),
            'moved': moved,
            'before_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'after_sha256': hashlib.sha256(formatted.encode()).hexdigest(),
        })
    from .reference_fusion import merge_retention_rows
    merged,edits=merge_retention_rows(formatted)
    if edits:
        number=scope.get('retention_merge_passes',0)+1
        scope['retention_merge_passes']=number
        write_json(scope['directory']/f'retention-row-merge-{number:03d}.json',{
            'edits':edits,'before_sha256':hashlib.sha256(formatted.encode()).hexdigest(),
            'after_sha256':hashlib.sha256(merged.encode()).hexdigest(),
            'native_gates_unchanged':True,'all_source_images_retained':True})
    return merged


def _install_structural_render_compatibility():
    import h3ir.compile as compiler
    from h3ir.validate import UNICODE_HAZARDS

    with _INSTALL_LOCK:
        original = compiler.render_ir
        if getattr(original, '_factory_structural_render', False):
            return
        if source_hash(original) != RENDER_IR_SHA256 or source_hash(compiler._assess) != ASSESS_SHA256:
            raise LocalProfileFailure('The audited native render-before-validation path changed')
        table = str.maketrans(UNICODE_HAZARDS)

        @functools.wraps(original)
        def render(plan, opts=None):
            result = original(plan, opts)
            if _ACTIVE.get() is None:
                return result
            prompt = _normalize_structural_text(result.prompt, table)
            if prompt == result.prompt:
                return result
            return replace(result, prompt=prompt,
                sections={key: _normalize_structural_text(value, table) for key, value in result.sections.items()},
                shot_bodies={key: _normalize_structural_text(value, table) for key, value in result.shot_bodies.items()},
                notes=[*result.notes, 'normalized structural Unicode outside verbatim dialogue and on-screen text'])

        render._factory_structural_render = True
        compiler.render_ir = render


def _identity_words(value):
    normalized=unicodedata.normalize('NFKC',value)
    return re.sub(r'\s*([,;:])\s*',r'\1',_normal(normalized).casefold())


_PRODUCT_ANCHOR_STOPWORDS={'a','an','the','and','with','of','in','on','from','shown',
    'depicted','appearing','based','whose','that','which','water','brand'}


def _written_product_anchor_matches(body,descriptor):
    """Accept a faithful written paraphrase without accepting another co-visible subject.

    The source tag anchors the actual reference. Its verified noun must also be
    the written definition's leading subject, and at least two distinctive
    verified appearance terms must occur in that same definition. This avoids
    requiring the prose writer to copy the vision model's entire caption while
    still rejecting a box/operator that merely mentions the product later.
    """
    anchor=re.match(r'(.+?)\s+(?:(?:shown|depicted|appearing)\s+in|from|in|based\s+on)\s+'
                    r'<(?:Picture|Video|Audio)\s+\d+>',body,re.I|re.S)
    if not anchor:return False

    def tokens(value):
        result=[]
        for token in re.findall(r"[a-z0-9]+",_identity_words(value)):
            token=token.removesuffix('ed') if token.endswith('ed') and len(token)>5 else token
            result.append(token)
        return result

    # A container/operator can mention the verified product in a relational
    # phrase: "gift box behind the plush Christmas tree". Only its leading
    # noun phrase identifies the subject of this definition.
    lead_phrase=re.split(r'\s+(?:behind|beside|near|under|above|alongside|next\s+to|in\s+front\s+of|'
                         r'with|holding|carrying|containing|supporting|featuring|whose|that|which)\b',
                         anchor[1],maxsplit=1,flags=re.I)[0]
    lead=tokens(lead_phrase)
    normalized_descriptor=_identity_words(descriptor)
    if _identity_words(anchor[1]).removeprefix('the ') == normalized_descriptor.removeprefix('the '):
        return True
    primary=re.split(r'\s+(?:with|featuring|whose|that|which)\b',normalized_descriptor,maxsplit=1)[0]
    primary_tokens=[token for token in tokens(primary) if token not in _PRODUCT_ANCHOR_STOPWORDS]
    if not primary_tokens or not lead or primary_tokens[-1] != lead[-1]:
        return False
    distinctive={token for token in tokens(normalized_descriptor)
                  if token not in _PRODUCT_ANCHOR_STOPWORDS and token!=primary_tokens[-1]}
    observed=set(tokens(body))
    return len(distinctive & observed)>=2


def _product_subject_labels(doc,contract,sources,*,prompt=None):
    """Match written identity anchors, not the per-file candidate Plan labels.

    The native writer can merge/relabel its candidates. Their labels therefore
    cannot identify the resulting prose. Only an exact leading noun phrase
    already approved by real image verification anchors a physical product.
    """
    verified=contract.get('verified_product_descriptors_by_native')
    if verified is None:return None  # Historical, explicitly recorded contracts.
    labels=set();descriptor_sources=set(sources);direct_verified_sources=set()
    for group in contract.get('groups',[]):
        if group.get('category')=='product' and descriptor_sources.intersection(group.get('appearance_native',[])):
            descriptor_sources.update(group.get('appearance_native',[]))
    def noun(value):return re.sub(r'^(?:the|an?|same)\s+','',_identity_words(value))
    descriptors={noun(value) for source in descriptor_sources for value in verified.get(source,[]) if noun(value)}
    written=prompt if prompt is not None else doc.prompt
    from h3ir.compile import _split_written
    definitions=_split_written(written).get('subject_definitions','')
    # A model-written subject can accurately paraphrase a verified caption
    # instead of repeating its exact opening words. Permit that only when the
    # exact native appearance reference resolves to one unique Plan object
    # whose descriptor exactly matches the independently verified image facts.
    # The written definition must then cite that source and retain multiple
    # distinctive visual features; otherwise the strict leading-phrase rule
    # below remains authoritative.
    appearance_sources={source for group in contract.get('groups',[])
                        if group.get('category')=='product'
                        for source in group.get('appearance_native',[])}
    for source in set(sources)&appearance_sources:
        approved={_identity_words(value) for value in verified.get(source,[])
                  if isinstance(value,str) and value.strip()}
        observed=[subject for subject in getattr(getattr(doc,'plan',None),'subjects',[])
                  if subject.kind=='object' and source in subject.sources
                  and _identity_words(subject.descriptor) in approved]
        if len(observed)==1:
            direct_verified_sources.add(source)
    for match in _DEFINITION_PATTERN.finditer(definitions):
        label,body=match[1],match[2]
        refs=set(re.findall(r'<(?:Picture|Video|Audio)\s+\d+>',body))
        source_hits=refs.intersection(sources)
        exact_anchor=any(re.match(re.escape(value)+r'(?=\W|$)',noun(body)) for value in descriptors)
        paraphrased_anchor=any(source in direct_verified_sources and
            _written_product_anchor_matches(body,descriptor)
            for source in source_hits for descriptor in verified.get(source,[])
            if isinstance(descriptor,str) and descriptor.strip())
        if source_hits and (exact_anchor or paraphrased_anchor):
            labels.add(label)
    return labels


def reference_binding(doc,contract):
    """Audit final written bindings, separately from candidate Plan sources.

    Upstream deliberately builds per-file SubjectPlan candidates, then permits
    the writer to merge them according to the request (MERGE_SUBJECTS). We do
    not claim that the candidate plan or wiring retention proves that merge.
    """
    from h3ir.compile import _split_written
    sections=_split_written(doc.prompt)
    definitions=sections.get('subject_definitions','')
    detail=sections.get('detailed_description','')
    pattern=r'(?ms)^\s*(<Subject\s+\d+>)\s+is\b(.*?)(?=^\s*<(?:Subject|Picture|Video|Audio)\s+\d+>\s*(?:is|:)|^\s*\[Shot\s+\d+\]|\Z)'
    actual={};errors=[]
    for label,text in re.findall(pattern,definitions):
        if label in actual:errors.append({'rule':'F2-duplicate-definition','message':label+' is defined more than once'})
        actual[label]=sorted(set(re.findall(r'<(?:Picture|Video|Audio)\s+\d+>',text)))
    native=sorted({m.label for m in getattr(doc.plan,'manifest',[])})
    plan_sources=[{'label':s.label,'kind':s.kind,'sources':list(s.sources),
                   'descriptor':s.descriptor} for s in getattr(doc.plan,'subjects',[])]
    candidate_sources={x for s in plan_sources for x in s['sources']}
    for finding in doc.findings:
        if finding.rule in ('L4-unused-media','R25-subject-defined-twice') or finding.severity=='ERROR':
            errors.append({'rule':finding.rule,'message':finding.msg})
    for label in contract.get('required_native',[]):
        if label not in native or label not in definitions:
            errors.append({'rule':'F3-required-reference','message':label+' must be an actual attached reference cited in subject_definitions; a wiring row or summary mention is insufficient'})
    results=[];targets={}
    for group in contract.get('groups',[]):
        wanted=set(group['native_sources'])
        eligible=_product_subject_labels(doc,contract,wanted) if group['category']=='product' else None
        matching=[label for label,refs in actual.items() if wanted.intersection(refs)
                  and (eligible is None or label in eligible)]
        target=matching[0] if len(matching)==1 else None
        complete=bool(target and wanted.issubset(actual[target]))
        cited=bool(target and target in detail)
        supported=wanted.issubset(candidate_sources) and wanted.issubset(native)
        passed=complete and cited and supported
        row={'category':group['category'],'group':group['group'],'required_sources':sorted(wanted),
             'appearance_sources':group['appearance_native'],'motion_sources':group['motion_native'],
             'subject':target,'intersecting_definitions':matching,'all_sources_in_one_definition':complete,
             'subject_cited_in_detailed_description':cited,'candidate_plan_sources_present':supported,
             'status':'PASS' if passed else 'BLOCKED'}
        if target and target in targets:
            passed=False;row['status']='BLOCKED'
            errors.append({'rule':'F4-conflicting-groups','message':target+' combines independently declared groups '+targets[target]+' and '+group['group']})
        if target:targets[target]=group['group']
        if not passed:
            errors.append({'rule':'F5-declared-group-binding','message':
                'Declared '+group['category']+' group '+group['group']+' requires exactly one Subject definition drawing on '
                +', '.join(sorted(wanted))+', that Subject cited in detailed_description, and no duplicate target definitions. '
                +'Appearance sources: '+', '.join(group['appearance_native'])+'. Explicit motion sources: '
                +(', '.join(group['motion_native']) or 'none; do not invent a motion/identity assignment')+'.'})
        results.append(row)
    return {'status':'BLOCKED' if errors else 'PASS','required_native':contract.get('required_native',[]),
            'actual_subject_definitions':actual,'group_results':results,'plan_subject_sources':plan_sources,
            'plan_sources_semantics':'per-file observed candidates; final same-subject binding is verified in written definitions',
            'native_manifest_labels':native,'errors':errors,'semantic_visual_quality':'NOT_REVIEWED'}


_DEFINITION_PATTERN=re.compile(r'(?ms)^\s*(<Subject\s+\d+>)\s+is\b(.*?)(?=^\s*<(?:Subject|Picture|Video|Audio)\s+\d+>\s*(?:is|:)|^\s*\[Shot\s+\d+\]|\Z)')


def _redirect(text,aliases):
    return re.sub(r'<Subject\s+\d+>',lambda m:aliases.get(m[0],m[0]),text)


def _normal(text):return ' '.join(text.split())


def _bind_verified_product_mentions(prompt, scope, plan):
    contract = scope.get('reference_contract') or {}
    proof = scope.get('product_identity_verification') or {}
    if proof.get('status') != 'PASS' or not contract.get('groups'):
        return prompt
    resolved = {**contract, 'verified_product_descriptors_by_native':
                proof['approved_product_descriptors_by_native']}
    audit = reference_binding(SimpleNamespace(prompt=prompt, plan=plan, findings=[]), resolved)
    heads = dict(re.findall(r'(?m)^\s*(<Subject\s+\d+>)\s+is\s+(.+?)\s+in\s+<Picture\s+\d+>',
                           prompt.split('summary:', 1)[0]))
    targets = [{'subject': row['subject'], 'descriptor': heads[row['subject']],
                'sources': row['required_sources']} for row in audit['group_results']
               if row['category'] == 'product' and row['subject'] in heads
               and row['all_sources_in_one_definition'] and row['candidate_plan_sources_present']
               and not row['subject_cited_in_detailed_description']]
    from .reference_fusion import bind_existing_mentions
    normalized, edits = bind_existing_mentions(prompt, targets)
    if edits:
        records = scope.setdefault('subject_mention_bindings', [])
        records.append({'original_prompt': prompt, 'normalized_prompt': normalized, 'edits': edits,
                        'validation': 'all original native gates run after this exact identity-token annotation'})
        write_json(scope['directory'] / 'subject-mention-bindings.json', records)
    return normalized


def _order_written_sections(prompt,scope):
    from .reference_fusion import order_written_sections
    ordered,receipt=order_written_sections(prompt)
    if receipt:
        rows=scope.setdefault('written_section_format',[])
        rows.append({**receipt,'original_prompt':prompt,'normalized_prompt':ordered,
                     'validation':'all native validators run after lossless section ordering'})
        write_json(scope['directory']/'written-section-format.json',rows)
    return ordered


def _product_anchor_instructions(contract, proof):
    if not contract or not proof or proof.get('status')!='PASS':
        return ''
    approved=proof['approved_product_descriptors_by_native']
    anchors=[]
    for group in contract.get('groups', []):
        if group.get('category')!='product' or not group.get('appearance_native'):
            continue
        source=group['appearance_native'][0]
        descriptor=next((text for text in approved.get(source, []) if isinstance(text,str) and text.strip()),None)
        if descriptor:
            anchors.append(f'- For {source}, start exactly one product definition with: <Subject N> is {descriptor} in {source}.')
    if not anchors:
        return ''
    return ('Verified product anchors for subject_definitions. Replace N with the correct subject number. '
            'Copy each verified descriptor verbatim, even when it is in a different language. Keep the product name '
            'and other required details elsewhere in the same definition or shot description. These are reference '
            'definitions, not on-screen text. Use the same subject number in the action.\n' + '\n'.join(anchors))


def project_declared_bindings(doc,contract,brief):
    """Compile only caller-declared relationships over existing written units.

    No subject is created, no Plan is merged, and no visual statement is
    generated. Ambiguous written anchors or source/group intersections refuse.
    Original alias descriptions survive verbatim under the canonical unit.
    """
    from h3ir.compile import _split_written
    sections=_split_written(doc.prompt)
    if set(sections)!=set(('subject_definitions','summary','retention_analysis','detailed_description','overall_soundscape','non_diegetic_music')):
        raise LocalProfileBlocked('Declared binding projection requires the actual six-section Ref2VA document')
    definitions=sections['subject_definitions'];records={};matches=list(_DEFINITION_PATTERN.finditer(definitions))
    for match in matches:
        body=match[2].strip();refs=set(re.findall(r'<(?:Picture|Video|Audio)\s+\d+>',match[2]))
        if match[1] in records:
            records[match[1]]['bodies'].append({'body':body,'refs':refs})
            records[match[1]]['refs'].update(refs)
        else:
            records[match[1]]={'label':match[1],'body':body,'bodies':[{'body':body,'refs':refs}],
                'refs':refs.copy(),'start':match.start(),'end':match.end()}
    kinds={}
    for subject in doc.plan.subjects:
        if subject.label in kinds:raise LocalProfileBlocked('Plan repeats a Subject label')
        kinds[subject.label]=subject.kind
    groups=contract.get('groups',[]);source_owner={};aliases={};assignments=[];owned={}
    for group in groups:
        for source in group['native_sources']:
            if source in source_owner:raise LocalProfileBlocked('A native reference crosses declared groups: '+source)
            source_owner[source]=group['group']
    duplicate_merges=[]
    for label,item in records.items():
        if len(item['bodies'])<2:continue
        verified=contract.get('verified_product_descriptors_by_native')
        owners={source_owner.get(source) for source in item['refs']}
        candidates=[group for group in groups if group['category']=='product' and owners=={group['group']}]
        if not isinstance(verified,dict) or len(candidates)!=1 or None in owners:
            raise LocalProfileBlocked('Duplicate written definition lacks one verified product group: '+label)
        group=candidates[0];wanted=set(group['native_sources'])
        approved={_identity_words(value) for source in group['appearance_native']
                  for value in verified.get(source,[]) if isinstance(value,str) and value.strip()}
        observed={_identity_words(subject.descriptor) for subject in doc.plan.subjects
                  if subject.kind=='object' and set(subject.sources).intersection(wanted)}
        anchors=[]
        for part in item['bodies']:
            # Match the complete leading noun phrase, not a descriptor occurring
            # later in a box/operator definition. Unknown wording stays blocked.
            anchor=re.match(r'(.+?)\s+(?:(?:shown|depicted|appearing)\s+in|from|in|based\s+on)\s+<(?:Picture|Video|Audio)\s+\d+>',
                            part['body'],re.I|re.S)
            name=_identity_words(anchor[1].rstrip(' ,;:，；：')) if anchor else ''
            if (not part['refs'] or not part['refs'].issubset(wanted)
                    or not part['refs'].issubset(source_owner) or name not in approved.intersection(observed)):
                raise LocalProfileBlocked('Duplicate written definition has an unverified or conflicting leading product anchor: '+label)
            anchors.append({'leading_noun_anchor':anchor[1],'native_sources':sorted(part['refs']),
                            'original_body_sha256':hashlib.sha256(part['body'].encode('utf-8')).hexdigest()})
        duplicate_merges.append({'subject':label,'group':group['group'],'original_definition_count':len(anchors),
            'verified_leading_anchors':anchors,'operation':'merge repeated same-label verified product definitions; preserve every original model body'})
    for group in groups:
        kind='person' if group['category']=='character' else 'object' if group['category']=='product' else None
        if not kind or not group['appearance_native']:raise LocalProfileBlocked('Declared group has no supported appearance anchor')
        wanted=set(group['native_sources']);first=group['appearance_native'][0]
        eligible=_product_subject_labels(doc,contract,wanted) if group['category']=='product' else None
        def qualified(label):
            return label in eligible if eligible is not None else kinds.get(label)==kind
        candidates=[label for label,item in records.items() if first in item['refs'] and qualified(label)]
        if len(candidates)!=1:
            raise LocalProfileBlocked('The first appearance source must select one existing written '+kind+' definition: '+first+'; candidates='+repr(candidates))
        canonical=candidates[0]
        members=[]
        for source in group['native_sources']:
            source_eligible=_product_subject_labels(doc,contract,{source}) if group['category']=='product' else None
            selected=[label for label,item in records.items() if source in item['refs'] and
                      (label in source_eligible if source_eligible is not None else kinds.get(label)==kind)]
            if len(selected)>1:raise LocalProfileBlocked('Declared source has several written target candidates: '+source)
            members.extend(selected)
        members=list(dict.fromkeys(members))
        for label in members:
            foreign=records[label]['refs'].intersection(source_owner)-wanted
            if foreign or label in owned:
                raise LocalProfileBlocked('A written Subject crosses declared groups: '+label)
            owned[label]=group['group']
            if label!=canonical:aliases[label]=canonical
        assignments.append({'category':group['category'],'group':group['group'],'first_appearance_source':first,
            'canonical_subject':canonical,'existing_group_subjects':members,
            'appearance_sources':list(group['appearance_native']),'motion_sources':list(group['motion_native'])})
    if any(any(label in line.text for label in aliases) for line in brief.dialogue):
        raise LocalProfileBlocked('A Subject alias occurs in locked caller dialogue; projection would change a verbatim input')
    if any(any(label in text for label in aliases) for text in brief.onscreen_text):
        raise LocalProfileBlocked('A Subject alias occurs in caller on-screen text')
    replacements={};removed=set(aliases)
    for assignment in assignments:
        canonical=assignment['canonical_subject'];body=records[canonical]['body']
        # These are metadata relationships supplied by the caller, not observed
        # attributes. Existing model bodies below are copied, never paraphrased.
        clause='Declared reference binding (caller): appearance sources '+', '.join(assignment['appearance_sources'])
        if assignment['motion_sources']:clause+='; motion content sources '+', '.join(assignment['motion_sources'])
        clause+='.'
        additions=['Additional original model description: '+part['body']
                   for label in assignment['existing_group_subjects'] for index,part in enumerate(records[label]['bodies'])
                   if label!=canonical or index>0]
        replacements[canonical]=canonical+' is '+body+' '+clause+(' '+' '.join(additions) if additions else '')+'\n'
    projected_definitions=definitions
    for match in reversed(matches):
        label=match[1]
        if label in replacements or label in removed:
            replacement=replacements.get(label,'') if match.start()==records[label]['start'] else ''
            projected_definitions=projected_definitions[:match.start()]+replacement+projected_definitions[match.end():]
    result={key:_redirect(value,aliases) for key,value in sections.items()}
    result['subject_definitions']=_redirect(projected_definitions,aliases)
    # Only the colliding retention entries need structural normalization. Every
    # original marker, scope and description is preserved as a source-specific
    # model note. The canonical marker is never applied to the motion note.
    retention=sections['retention_analysis'];entries=[]
    entry_pattern=re.compile(r'(?m)^\s*(<Subject\s+\d+>)([^\n]*)$')
    for match in entry_pattern.finditer(retention):
        parsed=re.fullmatch(r'\s*(\([^\n]*?\))?\s*:\s*([a-z_]+)\s*-\s*(.*)',match[2])
        if match[1] in owned and parsed is None:raise LocalProfileBlocked('An affected retention entry has an unsupported structure')
        entries.append((match,parsed))
    for assignment in assignments:
        canonical=assignment['canonical_subject'];members=set(assignment['existing_group_subjects'])
        affected=[(match,parsed) for match,parsed in entries if match[1] in members]
        if len([1 for match,_ in affected if match[1]==canonical])!=1 or len({m[1] for m,_ in affected})!=len(affected):
            raise LocalProfileBlocked('Canonical retention is missing or repeated: '+canonical)
    retention_replacements={};retention_removed=set()
    for assignment in assignments:
        canonical=assignment['canonical_subject'];members=set(assignment['existing_group_subjects'])
        affected=[(match,parsed) for match,parsed in entries if match[1] in members]
        canonical_match,canonical_data=next((m,p) for m,p in affected if m[1]==canonical)
        notes=[]
        for match,data in affected:
            if match[1]==canonical:continue
            notes.append(' Source-specific original model retention ('+data[2]+'; '+(data[1] or 'no scope')+'): '+data[3])
            retention_removed.add(match.start())
        retention_replacements[canonical_match.start()]=canonical_match[0]+''.join(notes)
    for match,_ in reversed(entries):
        if match.start() in retention_removed or match.start() in retention_replacements:
            retention=retention[:match.start()]+retention_replacements.get(match.start(),'')+retention[match.end():]
    result['retention_analysis']=_redirect(retention,aliases)
    for assignment in assignments:
        if assignment['category']!='product':continue
        tag=re.escape(assignment['canonical_subject'])
        # This checks an invalid relation introduced by alias projection, not
        # inferred visual identity or a general ban on self-directed actions.
        if re.search(tag+r'\s+(?:is\s+)?(?:being\s+)?(?:squeezed|held|carried|picked up)\s+by\s+'+tag,
                     result.get('detailed_description',''),re.I):
            raise LocalProfileBlocked('商品与操作者被合并为同一主体，已停止采样；请分开商品身份图与人物／动作参考。')
    prompt='\n\n'.join(key+':\n'+value.strip() for key,value in result.items())+'\n'
    components={'definitions':[{'label':label,'text':_redirect(part['body'],aliases)}
                               for label,item in records.items() for part in item['bodies']],
        'retention':[{'label':match[1],'scope':parsed[1] or '', 'marker':parsed[2],
                      'text':_redirect(parsed[3],aliases)} for match,parsed in entries if parsed is not None],
        'creative_sections':{key:_redirect(value,aliases) for key,value in sections.items()
                             if key not in ('subject_definitions','retention_analysis')}}
    residual=definitions
    for match in reversed(matches):residual=residual[:match.start()]+residual[match.end():]
    components['definition_residual']=_redirect(residual,aliases)
    residual=sections['retention_analysis']
    for match,_ in reversed(entries):residual=residual[:match.start()]+residual[match.end():]
    components['retention_residual']=_redirect(residual,aliases)
    evidence={'status':'PROJECTED_PENDING_VALIDATION','creative_text_source':'model_written',
        'binding_source':'declared_contract_projection','visual_facts_added':False,
        'alias_map':aliases,'group_assignments':assignments,'same_label_definition_merges':duplicate_merges,
        'preserved_model_components':components,
        'plan_before_sha256':digest(asdict(doc.plan)),
        'input_prompt_sha256':hashlib.sha256(doc.prompt.encode('utf-8')).hexdigest()}
    return prompt,evidence


def projection_invariants(prompt,evidence):
    from h3ir.compile import _split_written
    sections=_split_written(prompt);components=evidence['preserved_model_components']
    checks={'creative_sections_label_redirection_only':all(_normal(sections.get(key,''))==_normal(value)
        for key,value in components['creative_sections'].items()),
        'model_definition_bodies_preserved':all(_normal(item['text']) in _normal(sections.get('subject_definitions','')) for item in components['definitions']),
        'retention_descriptions_preserved':all(_normal(item['text']) in _normal(sections.get('retention_analysis','')) for item in components['retention']),
        'retention_markers_and_scopes_preserved':all(item['marker'] in sections.get('retention_analysis','') and
            (not item['scope'] or item['scope'] in sections.get('retention_analysis','')) for item in components['retention'])}
    residual=sections.get('subject_definitions','')
    for match in reversed(list(_DEFINITION_PATTERN.finditer(residual))):residual=residual[:match.start()]+residual[match.end():]
    checks['nondefinition_residual_label_redirection_only']=_normal(residual)==_normal(components['definition_residual'])
    residual=sections.get('retention_analysis','')
    for match in reversed(list(re.finditer(r'(?m)^\s*(<Subject\s+\d+>)([^\n]*)$',residual))):
        residual=residual[:match.start()]+residual[match.end():]
    checks['retention_residual_label_redirection_only']=_normal(residual)==_normal(components['retention_residual'])
    return checks


def _audit_or_project(doc,brief,scope,folder):
    contract=scope.get('reference_contract') or {}
    if contract.get('identity_validation_required') and any(g['category']=='product' for g in contract.get('groups',[])):
        proof=scope.get('product_identity_verification') or {}
        if proof.get('status')!='PASS':
            raise LocalProfileBlocked('商品参考身份尚未通过本地实图一致性校验，未加载H3模型。')
        contract={**contract,'verified_product_descriptors_by_native':proof['approved_product_descriptors_by_native']}
    before=reference_binding(doc,contract)
    write_json(folder/'reference-binding.before.json',before)
    final=before;result=doc
    if contract.get('groups'):
        scope['binding_projections']+=1
        if scope['binding_projections']>1 or 'cards' not in scope:
            raise LocalProfileBlocked('Declared binding projection requires complete captured cards and a single projection budget')
        projection={'status':'BLOCKED','creative_text_source':'model_written',
                    'binding_source':'declared_contract_projection','visual_facts_added':False}
        try:
            prompt,projection=project_declared_bindings(doc,contract,brief)
            (folder/'projection.before.txt').write_text(doc.prompt,encoding='utf-8')
            (folder/'projection.after.txt').write_text(prompt,encoding='utf-8')
            from h3ir.compile import (_written_context,_split_written,repair_brief,token_count,validate,wiring_findings)
            from h3ir.licence import resolve_licence
            from h3ir.style import resolve_style
            from h3ir.prose import _definition_lines
            cards=scope['cards'];labels=tuple(m.label for m in doc.plan.manifest)
            licence=resolve_licence(brief,cards);style=resolve_style(brief,cards,licence)
            repaired=repair_brief(prompt,target=doc.plan.target,mode=doc.mode,labels=labels,
                dialogue=tuple(d.text for d in brief.dialogue),style_phrase=style.phrase,
                definitions=tuple(_definition_lines(doc.plan.subjects,cards,labels)),task_types=tuple(doc.plan.task_types))
            result=deepcopy(doc);result.prompt=repaired.text;result.sections=_split_written(repaired.text)
            result.prompt_tokens=token_count(repaired.text)
            result.findings=list(wiring_findings(result.plan,brief))+validate(repaired.text,
                _written_context(brief,doc.mode,doc.plan,cards,result.prompt_tokens,style,licence))
            checks=projection_invariants(result.prompt,projection)
            checks['official_repair_only_whitespace']=_normal(result.prompt)==_normal(prompt)
            checks['plan_unchanged']=digest(asdict(result.plan))==projection['plan_before_sha256']
            projection.update(status='PASS' if all(checks.values()) else 'BLOCKED',invariants=checks,
                plan_after_sha256=digest(asdict(result.plan)),repair_actions=repaired.repairs,
                output_prompt_sha256=hashlib.sha256(result.prompt.encode('utf-8')).hexdigest(),
                additional_llm_calls=0,visual_analysis_repeated=False)
            result.provenance.update(creative_text_source='model_written',binding_source='declared_contract_projection',
                                     visual_facts_added=False,factory_binding_projection=projection)
            write_json(folder/'document.projected.json',_document_evidence(result))
            (folder/'binding-projection.diff.txt').write_text(''.join(difflib.unified_diff(
                doc.prompt.splitlines(keepends=True),result.prompt.splitlines(keepends=True),
                fromfile='model_written',tofile='declared_contract_projection')),encoding='utf-8')
            if not all(checks.values()):raise LocalProfileBlocked('Declared projection or upstream repair changed protected model text or Plan')
            final=reference_binding(result,contract)
            projection['status']=final['status']
        except Exception as error:
            projection.update(status='BLOCKED',error_type=type(error).__name__,error=str(error));raise
        finally:write_json(folder/'binding-projection.json',projection)
    final.update(correction_attempts=scope['reference_corrections'],binding_projections=scope['binding_projections'],
        creative_text_source='model_written',binding_source='declared_contract_projection' if scope['binding_projections'] else 'model_written',
        visual_facts_added=False)
    write_json(folder/'reference-binding.final.json',final)
    if final['status']!='PASS':
        raise LocalProfileBlocked('Reference binding BLOCKED before H3 loading after native written verification; evidence: '+str(folder))
    scope['final_reference_binding']=final
    return result


def _apply_revision_constraints(doc,brief,scope,folder):
    contract=scope.get('reference_contract') or {}
    constraints=contract.get('revision_constraints') or {}
    if not constraints.get('items'):return doc
    from .revision_constraints import enforce_revision_constraints
    from h3ir.compile import _split_written,_written_context,token_count,validate,wiring_findings
    from h3ir.licence import resolve_licence
    from h3ir.style import resolve_style
    sections=_split_written(doc.prompt)
    bindings={row['group']:row['subject'] for row in scope.get('final_reference_binding',{}).get('group_results',[])
              if row.get('status')=='PASS'}
    subjects={};receipt={'status':'BLOCKED','source':'caller_revision_constraints'}
    try:
        for item in constraints['items']:
            key=item['target_key'];group=contract.get('constraint_target_groups',{}).get(key)
            if group:
                subject=bindings.get(group)
                if not subject:raise ValueError('The revised product has no validated written Subject: '+key)
            else:
                # A text-only caller label must occur in exactly one real
                # definition. Never apply a size to every visible object.
                names=[str(key),str(item.get('target_name') or '')]
                matching=[m[1] for m in _DEFINITION_PATTERN.finditer(sections.get('subject_definitions',''))
                          if any(name and re.search(r'(?<![\w])'+re.escape(name)+r'(?![\w])',m[2],re.I) for name in names)]
                if len(matching)!=1:raise ValueError('Cannot uniquely bind revised target '+key+' to a written Subject')
                subject=matching[0]
            subjects[key]=subject
        prompt,receipt=enforce_revision_constraints(doc.prompt,constraints,subjects)
        result=deepcopy(doc);result.prompt=prompt;result.sections=_split_written(prompt);result.prompt_tokens=token_count(prompt)
        cards=scope['cards'];licence=resolve_licence(brief,cards);style=resolve_style(brief,cards,licence)
        result.findings=list(wiring_findings(result.plan,brief))+validate(prompt,
            _written_context(brief,result.mode,result.plan,cards,result.prompt_tokens,style,licence))
        errors=[{'rule':f.rule,'message':f.msg} for f in result.findings if f.severity=='ERROR']
        if errors:raise ValueError('Caller constraints failed native document validation: '+str(errors))
        # A second application must be byte-identical: retries do not accrue
        # conflicting or repeated scale clauses in the written shot body.
        if enforce_revision_constraints(prompt,constraints,subjects)[0]!=prompt:
            raise ValueError('Revision constraint application is not idempotent')
        receipt.update(status='PASS',plan_unchanged=digest(asdict(doc.plan))==digest(asdict(result.plan)),
                       visual_facts_added=False,caller_authored_constraints_added=True)
        result.provenance.update(factory_revision_constraints=receipt,caller_constraints_source='explicit_revision',
                                 creative_text_source='model_written',caller_authored_constraints_added=True)
        write_json(folder/'document.constrained.json',_document_evidence(result))
        (folder/'constraints.before.txt').write_text(doc.prompt,encoding='utf-8')
        (folder/'constraints.after.txt').write_text(prompt,encoding='utf-8')
        return result
    except Exception as error:
        receipt.update(status='BLOCKED',error=str(error))
        raise LocalProfileBlocked('本次产品尺寸约束未能唯一绑定并写入最终镜头，未加载H3模型：'+str(error)) from error
    finally:write_json(folder/'revision-constraints.json',receipt)


def _document_evidence(doc):
    return {'source':getattr(doc,'source',None),'fallback_reason':getattr(doc,'fallback_reason',None),
            'prompt':getattr(doc,'prompt',None),'provenance':getattr(doc,'provenance',None),
            'plan':asdict(doc.plan) if is_dataclass(getattr(doc,'plan',None)) else None,
            'findings':[{'rule':f.rule,'severity':f.severity,'message':f.msg}
                        for f in getattr(doc,'findings',())]}


def _revision_semantic_findings(prompt,scope,*,plan=None,final_folder=None):
    from .revision_semantics import revision_semantic_findings
    contract=scope.get('reference_contract') or {}
    proof=scope.get('product_identity_verification') or {}
    if proof.get('status')=='PASS' and plan is not None:
        contract={**contract,'verified_product_descriptors_by_native':proof['approved_product_descriptors_by_native']}
        targets={}
        for key,name in contract.get('constraint_target_groups',{}).items():
            group=next((g for g in contract.get('groups',[]) if g.get('group')==name),None)
            if group is not None:
                targets[key]=sorted(_product_subject_labels(None,contract,set(group['native_sources']),prompt=prompt))
        contract={**contract,'verified_target_subjects':targets}
    findings=revision_semantic_findings(prompt,contract)
    index=scope.get('revision_validation_passes',0)+1
    scope['revision_validation_passes']=index
    record={'policy':POLICY_REVISION,'stage':'final_serialization' if final_folder else 'native_written_verification',
        'status':'BLOCKED' if findings else 'PASS','prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'findings':findings,'verified_target_subjects':contract.get('verified_target_subjects'),
        'rewrites_creative_text':False,
        'correction_owner':'pinned upstream compile_brief / fix_with_findings; maximum two rounds'}
    write_json(scope['directory']/f'revision-verify-{index:03d}.json',record)
    (scope['directory']/f'revision-verify-{index:03d}.prompt.txt').write_text(prompt,encoding='utf-8')
    if final_folder:write_json(final_folder/'revision-semantics.final.json',record)
    return findings


def _native_reference_verification(prompt,brief,plan,contract,proof=None):
    """Preview the final binding policy without editing the writer's candidate.

    A caller-declared alias merge is legal, so audit its pure projection rather
    than asking the writer to undo it. This preview consumes no projection
    budget and does not alter the Plan, prompt, contract, or captured cards.
    Native validation errors remain owned by the original validator; this
    record contains only additional declared-reference findings.
    """
    if not contract or not (contract.get('groups') or contract.get('required_native')):
        return None
    resolved=deepcopy(contract)
    if resolved.get('identity_validation_required') and any(
            group['category']=='product' for group in resolved.get('groups',[])):
        if not proof or proof.get('status')!='PASS':
            raise LocalProfileBlocked('商品参考身份尚未通过本地实图一致性校验，不能要求模型补写身份绑定。')
        resolved['verified_product_descriptors_by_native']=deepcopy(
            proof['approved_product_descriptors_by_native'])
    candidate=SimpleNamespace(prompt=prompt,plan=plan,findings=[])
    projected=prompt;checks={};projection_error=None
    try:
        if resolved.get('groups'):
            projected,evidence=project_declared_bindings(candidate,resolved,brief)
            checks=projection_invariants(projected,evidence)
            checks['plan_unchanged']=digest(asdict(plan))==evidence['plan_before_sha256']
            if not all(checks.values()):
                raise LocalProfileFailure('Native binding preview changed protected model text or Plan')
        audit=reference_binding(SimpleNamespace(prompt=projected,plan=plan,findings=[]),resolved)
        errors=[dict(item) for item in audit['errors']]
        blocked_groups=iter(row for row in audit['group_results'] if row['status']!='PASS')
        for item in errors:
            if item['rule']!='F5-declared-group-binding':continue
            row=next(blocked_groups)
            if row['subject'] and not row['subject_cited_in_detailed_description']:
                item['message']+=(' The existing declared target '+row['subject']
                    +' is already defined from the attached references but is never cited in '
                    'detailed_description. In that section, name this same target with the exact '
                    'token '+row['subject']+' in its existing action text. A summary mention is '
                    'insufficient. Preserve the existing people, shots, camera, action, dialogue '
                    'and on-screen text. Do not add a subject, visual fact, or motion source.')
                item['message']+=_existing_containment_guidance(projected,row['subject'],brief,resolved)
    except LocalProfileBlocked as error:
        projection_error=str(error)
        audit=None
        errors=[{'rule':'F6-declared-binding-projection','message':
            'The existing written reference definitions cannot satisfy the caller-declared binding: '
            +str(error)+'. Correct only the reported binding in the six written sections, using '
            'the attached reference labels and existing verified identity descriptions. Preserve '
            'people, shots, camera, action, dialogue and on-screen text; do not invent a subject, '
            'visual fact, or motion source. '+_product_anchor_instructions(contract,proof)}]
    from h3ir.repair import normalize
    normalized=normalize(prompt)
    for text in brief.onscreen_text:
        if text and '"'+normalize(text)+'"' not in normalized:
            errors.append({'rule':'F7-locked-onscreen-text','message':
                'Keep the caller-locked on-screen text verbatim in double quotes: '+repr(text)
                +'. Restore it in the existing scene without changing its words or the reference binding.'})
    return {'policy':POLICY_REVISION,'stage':'native_written_verification',
        'status':'BLOCKED' if errors else 'PASS',
        'prompt_sha256':hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
        'findings':[dict(item,severity='ERROR') for item in errors],
        'binding_only_audit':audit,'projection_invariants':checks,
        'projection_error':projection_error,'rewrites_creative_text':False,
        'correction_owner':'pinned upstream compile_brief / fix_with_findings; maximum two rounds'}


def _existing_containment_guidance(prompt,subject,brief,contract):
    """Show a supported existing relationship to the writer; never edit its IR.

    A closed-package scene may name P01 in the action and the product Subject
    only in its summary. Generic "cite the product in its action" feedback can
    misleadingly suggest exposing the hidden contents. Point at the actual
    written relationship, only with one explicitly closed caller container.
    """
    rules=list(dict.fromkeys([*getattr(brief,'constraints',[]),
        *(contract.get('locked_production_constraints') or [])]))
    closed=[rule for rule in rules if re.search(
        r'Caller-authored screenplay prop [^:：\n]*(?:包装|盒|蒸笼|\bbox\b|\bpackage\b|\bcontainer\b)[^:：\n]*[:：]',rule,re.I)
        and re.search(r'"open_state"\s*:\s*"(?:关闭|closed|sealed)"',rule,re.I)]
    if len(closed)!=1:return ''
    from h3ir.compile import _split_written
    summary=_split_written(prompt).get('summary','')
    if not re.search(r'\b(?:closed|sealed)\b.{0,50}\b(?:box|package|container|steamer)\b',summary,re.I):return ''
    witnesses=[line for line in re.split(r'(?<=[.!?。！？])\s+',summary)
        if set(re.findall(r'<Subject \d+>',line))=={subject}
        and re.search(r'\b(?:inside|within|contain(?:s|ed)?|enclos(?:es|ed)?)\b',line,re.I)]
    if not witnesses:return ''
    return (' The candidate ALREADY states this containment relationship in summary: '
        +json.dumps(witnesses,ensure_ascii=False)+'. Re-express that EXISTING relationship in '
        'the numbered shot of detailed_description with the EXACT token '+subject
        +'. This is a section-placement correction, not a new visual fact. User prop codes '
        'such as P01 name the package; they do not substitute for the required media Subject '
        'token. Keep both identities distinct. The caller requires the container CLOSED, so '
        'keep its established contents concealed; do not copy any conflicting reveal claim '
        'from the faulty summary and do not open the package. Preserve every other action, '
        'performer and caller constraint. The model must write the complete corrected brief; '
        'a summary-only citation still fails F5.')


def _closed_containment_findings(prompt,brief,contract,audit,state=None):
    """Reject a concrete observed contradiction, without rewriting model prose."""
    from h3ir.compile import _split_written
    sections=_split_written(prompt)
    subjects=list((state or {}).get('closed_containment_subjects',[]))
    for row in (audit or {}).get('group_results',[]):
        subject=row.get('subject')
        if subject and _existing_containment_guidance(prompt,subject,brief,contract) and subject not in subjects:
            subjects.append(subject)
    if state is not None:state['closed_containment_subjects']=subjects
    findings=[]
    for name in ('summary','detailed_description'):
        # A caller's dialogue may discuss a reveal without showing one.
        prose=re.sub(r'<d>.*?</d>','',sections.get(name,''),flags=re.S)
        for sentence in re.split(r'(?<=[.!?。！？])\s+|\n',prose):
            if re.search(r'\bis\s+revealed\s+as\b',sentence,re.I) and any(s in sentence for s in subjects):
                findings.append({'rule':'F8-closed-contents-revealed','severity':'ERROR',
                    'message':f'{name} claims a reveal of the established contents of the caller CLOSED container. '
                    'Correct this contradictory reveal claim in summary and detailed_description; keep the same product '
                    'identity concealed inside the unchanged sealed package, all actions and every complete caller line. '
                    'Do not open the package, expose its contents, or remove a reference to pass binding.'})
                break
    for subject in subjects:
        if re.search(r'(?m)^[ \t]*'+re.escape(subject)+r'[ \t]*(?:\([^\n]*\)[ \t]*)?:[ \t]*fully_preserved\b',sections.get('retention_analysis','')):
            findings.append({'rule':'F10-hidden-visible-retention','severity':'ERROR',
                'message':subject+' is established as concealed inside the caller closed container, but its visual '
                'retention row still asks to fully preserve its appearance in this shot. Use the native weak_reference '
                'marker only for its broad category/context while it stays concealed, with no visible appearance '
                'retained. Preserve the mandatory existing native shot-association scope. Keep its identity definition, every original picture source and all other '
                'retention rows unchanged. Do not expose the toy to satisfy this visual retention conflict.'})
    if state is not None:
        transitions=sections.get('detailed_description','').count('<scenetrans>')
        baseline=state.setdefault('initial_scene_transition_count',transitions)
        if transitions>baseline:
            findings.append({'rule':'F9-correction-added-scene-transition','severity':'ERROR',
                'message':'The mechanical correction added a scene transition absent from the original written shot. '
                'Preserve its original shot/camera continuity; speak each caller line once, without adding a cut to repair duplicate dialogue.'})
    return findings


def _restore_camera_motion(prompt,sentence):
    """Carry forward explicit camera direction only when the writer omitted a move."""
    from h3ir.validate import CAMERA_MOTION_PHRASE,CAMERA_STEM_IN_CONTEXT

    description=re.search(
        r'^(?:detailed_description|integrated_multimodal_description)[ \t]*:(.*?)'
        r'(?=^(?:overall_soundscape|non_diegetic_music)[ \t]*:|\Z)',prompt,re.M|re.S)
    if not description:
        return prompt
    detail=description[1]
    shots=list(re.finditer(r'^[ \t]*\[Shot (\d+)\](?=\s|$)',detail,re.M))
    if len(shots)!=1 or shots[0][1]!='1' or not detail[shots[0].end():].strip():
        return prompt
    if CAMERA_MOTION_PHRASE.search(detail) or any(
            CAMERA_STEM_IN_CONTEXT.search(clause)
            for clause in re.split(r'(?<=[.!?])\s+',detail)
            if re.search(r'\b(?:camera|shot|lens|frame)\b',clause,re.I)):
        return prompt
    position=description.start(1)+shots[0].end()
    return prompt[:position]+' '+sentence+prompt[position:]


def _install_revision_validation():
    """Let the real upstream writer repair caller-contract conflicts.

    The initial deterministic draft uses a different Context and must not be
    mistaken for the writer's candidate. Keep the exact written Context objects
    in this scope; no global policy or model is changed outside this wrapper.
    """
    import h3ir.compile as compiler
    import h3ir.prose as prose
    import h3ir.repair as repair
    from h3ir.models import Finding
    with _INSTALL_LOCK:
        original_context=compiler._written_context
        original_validate=compiler.validate
        original_compose=compiler.compose_brief
        original_fix=inspect.unwrap(prose.fix_with_findings)
        original_repair=inspect.unwrap(compiler.repair_brief)
        original_labels=inspect.unwrap(repair._fix_labels)
        if getattr(original_validate,'_factory_revision_validation_version',None)==POLICY_REVISION:return
        if (source_hash(inspect.unwrap(original_context))!=WRITTEN_CONTEXT_SHA256
                or source_hash(inspect.unwrap(original_validate))!=WRITTEN_VALIDATE_SHA256
                or source_hash(compiler.compile_brief)!=COMPILE_BRIEF_SHA256
                or source_hash(inspect.unwrap(original_compose))!=COMPOSE_BRIEF_SHA256
                or source_hash(original_repair)!=REPAIR_BRIEF_SHA256
                or source_hash(original_labels)!=FIX_LABELS_SHA256
                or source_hash(original_fix)!=FIX_WITH_FINDINGS_SHA256):
            raise LocalProfileFailure('The audited native written verification/correction path changed')

        @functools.wraps(original_compose)
        def compose_written(backend,brief,subjects,cards,target,labels,**kwargs):
            scope=_ACTIVE.get()
            if scope is None:
                return original_compose(backend,brief,subjects,cards,target,labels,**kwargs)
            token=_COMPOSE_MEDIA.set(labels)
            try:
                hints=_product_anchor_instructions(scope.get('reference_contract'),scope.get('product_identity_verification'))
                writer_brief=replace(brief,intent=brief.intent+'\n\n'+hints+'\n'+_REFERENCE_VISIBILITY_GUIDANCE) if hints else brief
                text=original_compose(backend,writer_brief,subjects,cards,target,labels,**kwargs)
                text=_order_written_sections(text,scope)
                text = _format_descriptive_retention(text,scope) if brief.shots==1 else text
                from h3ir.plan import build_manifest
                binding_plan = SimpleNamespace(subjects=subjects, manifest=build_manifest(brief,target))
                text=_bind_verified_product_mentions(text, scope, binding_plan)
                sentence=(scope.get('reference_contract') or {}).get('camera_sentence')
                return _restore_camera_motion(text,sentence) if sentence else text
            finally:
                _COMPOSE_MEDIA.reset(token)

        @functools.wraps(original_labels)
        def repair_labels(text,labels,result):
            if _ACTIVE.get() is None:
                return original_labels(text,labels,result)
            repaired,actions=repair_media_bindings(text,labels)
            result.repairs.extend(actions)
            return repaired

        @functools.wraps(original_repair)
        def repair_written(*args,**kwargs):
            result=original_repair(*args,**kwargs)
            scope=_ACTIVE.get() or {}
            if scope:
                result.text=_order_written_sections(result.text,scope)
                formatted=_format_descriptive_retention(result.text,scope)
                if formatted!=result.text:
                    result.text=formatted
                    result.repairs.append('moved descriptive retention rows into their numbered shot')
                plans = scope.get('written_plans') or []
                if plans:
                    bound = _bind_verified_product_mentions(result.text, scope, plans[-1][1])
                    if bound != result.text:
                        result.text = bound
                        result.repairs.append('bound unique existing product nouns to their verified identity tokens')
            sentence=(scope.get('reference_contract') or {}).get('camera_sentence')
            if sentence:
                repaired=_restore_camera_motion(result.text,sentence)
                if repaired!=result.text:
                    result.text=repaired
                    result.repairs.append(_CAMERA_REPAIR+': '+sentence)
            return result

        @functools.wraps(original_context)
        def written_context(*args,**kwargs):
            context=original_context(*args,**kwargs)
            scope=_ACTIVE.get()
            if scope is not None:
                scope.setdefault('written_contexts',[]).append(context)
                plan=inspect.signature(original_context).bind(*args,**kwargs).arguments.get('plan')
                scope.setdefault('written_plans',[]).append((context,plan))
                brief=inspect.signature(original_context).bind(*args,**kwargs).arguments.get('brief')
                scope.setdefault('written_briefs',[]).append((context,brief))
            return context

        @functools.wraps(original_validate)
        def validate_written(prompt,context,*args,**kwargs):
            findings=original_validate(prompt,context,*args,**kwargs)
            scope=_ACTIVE.get()
            if scope is None or not any(context is item for item in scope.get('written_contexts',[])):
                return findings
            plan=next((plan for item,plan in scope.get('written_plans',[]) if item is context),None)
            brief=next((brief for item,brief in scope.get('written_briefs',[]) if item is context),None)
            if plan is not None and not any(item.rule=='L3-phantom-media' for item in findings):
                missing=written_media_tags(prompt)-{entry.label for entry in plan.manifest}
                if missing:
                    findings.append(Finding('L3-phantom-media','ERROR',
                        'These tags have no attached source: '+', '.join(sorted(missing))))
            additional=_revision_semantic_findings(prompt,scope,plan=plan)
            reference=_native_reference_verification(prompt,brief,plan,
                scope.get('reference_contract'),scope.get('product_identity_verification'))
            if reference is not None:
                audit=reference.get('binding_only_audit')
                scope['latest_reference_audit']=deepcopy(audit)
                from h3ir.compile import _split_written
                scope['latest_reference_definitions_sha256']=digest(_split_written(prompt).get('subject_definitions',''))
                state=scope.setdefault('caller_containment_state',{})
                state_findings=_closed_containment_findings(prompt,brief,scope.get('reference_contract') or {},audit,state)
                additional.extend(state_findings)
                write_json(scope['directory']/f"caller-state-verify-{scope['revision_validation_passes']:03d}.json",{
                    'status':'BLOCKED' if state_findings else 'PASS','prompt_sha256':digest(prompt),
                    'state':deepcopy(state),'findings':state_findings,'rewrites_creative_text':False})
                write_json(scope['directory']/f"reference-written-verify-{scope['revision_validation_passes']:03d}.json",reference)
                additional=[*additional,*reference['findings']]
            return [*findings,*[Finding(item['rule'],item['severity'],item['message']) for item in additional]]

        @functools.wraps(original_fix)
        def fix_written(backend,written,findings,*args,**kwargs):
            scope=_ACTIVE.get()
            if scope is not None:
                # Native plan subjects come from media. The written scene may
                # also define caller-requested people without photographs.
                # Their existing definitions remain valid internal labels;
                # this never adds an image/video/audio attachment to inventory.
                definition=re.search(r'^subject_definitions[ \t]*:(.*?)(?=^(?:summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)[ \t]*:|\Z)',written,re.M|re.S)
                defined=re.findall(r'^[ \t]*(<Subject \d+>)[ \t]+(?:\([Ss]\d+\)[ \t]+)?is\b',definition[1],re.M) if definition else []
                original_labels=tuple(kwargs.get('labels',()))
                if original_labels:
                    kwargs={**kwargs,'labels':tuple(dict.fromkeys([*original_labels,*defined]))}
                rows=(scope.get('reference_contract') or {}).get('locked_dialogue') or []
                if rows and any(f.rule=='D4-dialogue-not-verbatim' for f in findings):
                    # D4's native diagnostic abbreviates the missing line.
                    # A repair cannot recover words absent from its candidate
                    # and that shortened diagnostic. Resend the COMPLETE
                    # frozen source; the model still writes the correction,
                    # and every unchanged native verifier runs afterwards.
                    plans=scope.get('written_plans') or []
                    plan=plans[-1][1] if plans else None
                    speakers=[{key:getattr(speaker,key,None) for key in ('sid','subject','descriptor','onscreen')}
                              for speaker in (getattr(plan,'speakers',None) or [])]
                    source='\nComplete required utterances in NATIVE H3 syntax (not abbreviated; preserve all punctuation and BOTH <d> and </d> tags):\n'
                    for index,row in enumerate(rows,1):
                        match=next((speaker for speaker in speakers if row.get('speaker') and speaker['descriptor']==row['speaker']),None)
                        if match is None and len(speakers)==1:match=speakers[0]
                        prefix=(match['sid']+': ') if match and match.get('sid') else ''
                        source+=f"Line {index}, speaker={row.get('speaker') or 'the existing assigned speaker'}:\n{prefix}<d>[{row['language']}] {row['text']}</d>\n"
                    source+='Existing native speaker registry: '+json.dumps(speakers,ensure_ascii=False)+'\n'
                    source+='Copy each complete required native utterance into detailed_description exactly once, with its existing speaker identity and closing </d> tag. Do not substitute a custom XML element or put speech only in summary/soundscape. For an explicitly requested group line, the group speak together. Do not change the visuals or invent another line.'
                    findings=[Finding(f.rule,f.severity,f.msg+source) if f.rule=='D4-dialogue-not-verbatim' else f for f in findings]
                # The native repair call receives the faulty candidate and
                # findings, but not Brief.constraints. Resend the actual typed
                # caller rules, just as D4 repair resends the complete line.
                # The model must write the correction; no gate or IR is edited.
                native_briefs=scope.get('written_briefs') or []
                native_rules=getattr(native_briefs[-1][1],'constraints',[]) if native_briefs else []
                caller_rules=(scope.get('reference_contract') or {}).get('locked_production_constraints') or []
                repair_constraints=list(dict.fromkeys([*native_rules,*caller_rules]))
                constraint_feedback=''
                if repair_constraints:
                    constraint_feedback=('\nComplete original caller constraints (not abbreviated; retain current '
                        'prop states, performer identities, clothes and actions):\n'
                        +json.dumps(repair_constraints,ensure_ascii=False)+'\n'
                        +_REFERENCE_VISIBILITY_GUIDANCE+'\n')
                scope['repair_contexts']=scope.get('repair_contexts',0)+1
                write_json(scope['directory']/f"repair-caller-context-{scope['repair_contexts']:03d}.json",{
                    'original_labels':original_labels,'labels':kwargs.get('labels',()),'existing_written_subjects':defined,
                    'new_media_labels_added':False,'full_locked_dialogue_resent':bool(rows and any(f.rule=='D4-dialogue-not-verbatim' for f in findings)),
                    'source_dialogue':rows,'original_constraints_resent':repair_constraints,
                    'original_constraints_sha256':digest(repair_constraints),
                    'model_must_write_the_fix':True,'native_gates_unchanged':True})
                if any(f.rule in _REFERENCE_FINDING_RULES for f in findings):
                    # Count real native correction invocations, independent of projections.
                    scope['reference_corrections']=scope.get('reference_corrections',0)+1
                if any(f.rule=='L3-phantom-media' for f in findings):
                    missing=written_media_tags(written)-set(kwargs.get('labels',()))
                    help_text=(' Remove the claim that the action, voice or view came from the unattached source. '
                               'Keep real attachment tags in their original roles and describe the requested '
                               'camera movement, performance, or sound as generated content.')
                    if missing:
                        help_text=' Unattached tags to remove: '+', '.join(sorted(missing))+'.'+help_text
                    findings=[Finding(f.rule,f.severity,f.msg+help_text) if f.rule=='L3-phantom-media' else f
                              for f in findings]
            if scope is not None:
                # The small local writer can copy YOUR TEXT unchanged when the
                # instructions precede several pages of prose. Repeat the real
                # findings after that text; never perform their semantic edit
                # in Python or declare a failed native finding successful.
                original_backend=backend
                checklist='\n\nFINAL CORRECTION CHECKLIST (apply before returning the full six sections):\n'
                checklist+='Verification findings take priority over preserving the faulty fragments. Keep every reference source, identity, action and caller line; copying the faulty text unchanged is not a correction.\n'
                checklist+='Actual unresolved native findings:\n'+'\n'.join(f'[{f.rule}] {f.msg}' for f in findings)+'\n'
                checklist+=constraint_feedback
                retention_grammar=any(f.rule.startswith('R') for f in findings)
                if retention_grammar:
                    checklist+='Audited native reference grammar (keep the original label roles):\n'+_reference_repair_grammar()+'\n'
                    checklist+='Generated speech belongs in detailed_description. A visual Subject is not an Audio reference and cannot use fully_copy. Keep the performer appearance in its visual retention row; keep speech in its native <d> block. Do not invent an attached Audio source.\n'
                write_json(scope['directory']/f"repair-grammar-{scope['repair_contexts']:03d}.json",{
                    'native_grammar_used':retention_grammar,
                    'native_grammar_sha256':REFERENCE_GRAMMAR_SHA256 if retention_grammar else None,
                    'actual_findings_repeated_after_candidate':True,'semantic_edits_by_model_only':True,
                    'native_gates_unchanged':True})
                if any(f.rule in {'R24-label-analysed-twice','R32-transfer-target-unnamed'} for f in findings):
                    checklist+='Write exactly one retention_analysis row for each Subject. A performer doing an action specified in the screenplay is not an attribute transferred from a reference photo. Resolve the reported retention conflict according to that Subject definition; preserve compatible appearance facts and keep the requested gesture in detailed_description. A genuine attribute_transfer between two different reference subjects must still identify its receiving target.\n'
                if rows and any(f.rule=='D4-dialogue-not-verbatim' for f in findings):
                    checklist+=source+'\n'
                checklist+='Return the corrected complete six-section brief only, without explanation or a code fence.'
                qualified_subjects=_qualified_reference_correction_subjects(written,findings,scope)
                structured_reference_fix=bool(qualified_subjects) or any(f.rule=='F5-declared-group-binding' for f in findings)
                class FeedbackAtEnd:
                    def chat(self,messages,**options):
                        messages=[dict(message) for message in messages]
                        messages[-1]['content']+=checklist
                        if not structured_reference_fix:
                            return original_backend.chat(messages,**options)
                        # The real small writer repeatedly copied the faulty
                        # prose even with the complete findings and distinct
                        # sampling. Ask it for all six independently named
                        # sections under the existing local guided-JSON API.
                        # Only headers/separators are serialized here; every
                        # word and Subject token still comes from the model.
                        schema=_reference_correction_schema()
                        detail_only=bool(qualified_subjects)
                        required_subjects=[]
                        replace_summary=False
                        concealed_subjects=[]
                        if detail_only:
                            from h3ir.compile import _split_written
                            sections=_split_written(written)
                            required_subjects=qualified_subjects
                            concealed=(scope.get('caller_containment_state') or {}).get('closed_containment_subjects',[])
                            concealed_subjects=[subject for subject in required_subjects if subject in concealed]
                            replace_summary=bool(concealed_subjects)
                            if _can_append_concealed_patch(concealed_subjects,findings,required_subjects):
                                return _write_small_concealed_patch(original_backend,written,scope,
                                    required_subjects,concealed_subjects,repair_constraints,rows,findings)
                            schema=_binding_fragment_schema(required_subjects,sections['detailed_description'],
                                                            sections['summary'] if replace_summary else None,concealed_subjects)
                            # Full-brief correction repeatedly copied the bad
                            # section in real replies. Focus this native round
                            # on the one faulty section. Other model-written
                            # sections remain exactly the previous candidate's
                            # bodies; no Python-created binding or story text.
                            presented_findings=[{'rule':f.rule,'message':(
                                'The existing declared Subject is uniquely defined but missing from the detailed_description. '
                                'Include its exact required identity field in a coherent existing relationship; do not invent a reveal.'
                                if f.rule=='F5-declared-group-binding' else f.msg)} for f in findings]
                            task={'replace_sections':['detailed_description',*(['summary'] if replace_summary else []),*(['retention_analysis (concealed subjects only)'] if concealed_subjects else [])],
                                  'required_existing_subjects':required_subjects,
                                  'current_section_body':sections['detailed_description'],
                                  'actual_findings':presented_findings,
                                  'existing_subject_definitions':sections['subject_definitions'],
                                  'established_containment':[{'subject':subject,'relationship':'inside the single caller CLOSED container',
                                      'visibility':'concealed throughout this shot; no opening or reveal'}
                                      for subject in required_subjects if subject in concealed],
                                  'concealed_retention_requirement':'Return the requested native weak_reference row for concealed contents only; keep all other subject/source rows untouched. This is semantic context, not a claim that the hidden product appearance is visually constrained in this shot.',
                                  'complete_caller_constraints':repair_constraints,
                                  'complete_locked_dialogue':rows,
                                  'native_dialogue_instructions':source if rows and any(f.rule=='D4-dialogue-not-verbatim' for f in findings) else '',
                                  'reference_visibility_rule':_REFERENCE_VISIBILITY_GUIDANCE}
                            messages=[{'role':'system','content':'Correct the faulty H3 detailed_description in ENGLISH with the smallest supported edit. Resolve every finding. Preserve each caller-stated open/closed and visibility state; established contents of a CLOSED container stay concealed in BOTH summary and description. Never narrate a reveal while the package is sealed. Never hide a requested visible product. Preserve every unrelated action, performer, camera beat and complete dialogue. Speak each caller line EXACTLY ONCE; do not repeat it in separate spans or add a scene cut. Return the COMPLETE corrected description SPLIT around each required existing Subject token: before_subject_1 + subject_1 + before_subject_2 + subject_2 ... + tail, joined with spaces, must be the complete coherent section. These identity values come from already-verified definitions, not new subjects. Write each text span only once; do not duplicate the whole section in multiple spans. End a before span at the exact place its following Subject fits grammatically. If the schema also requests summary, write a complete caller-consistent summary from the supplied current action/constraints, preserving established concealed contents. No section header, explanation or Markdown. All other sections are preserved separately; do not output them.'},
                                      {'role':'user','content':json.dumps(task,ensure_ascii=False)}]
                        else:
                            messages[-1]['content']+='\nSTRUCTURED CORRECTION OUTPUT: Return one JSON object with exactly these six keys: '+', '.join(_REFERENCE_SECTION_NAMES)+'. Each value is the complete corrected section BODY, without its header. The detailed_description value itself must resolve the reported Subject binding, while preserving all caller states and complete dialogue. Do not merely copy the bad detailed_description. No Markdown or explanation.\n'
                        stage={'kind':'native_reference_correction','original_messages':deepcopy(messages),
                               'original_schema':deepcopy(schema),'effective_schema':deepcopy(schema),'schema_changes':[]}
                        token=_STAGE.set(stage)
                        try:
                            reply=original_backend.chat(messages,**{**options,'response_format':{
                                'type':'json_schema','json_schema':{'name':'native_reference_correction',
                                'strict':True,'schema':schema}}})
                        finally:_STAGE.reset(token)
                        corrected=_serialize_reference_correction(reply.content,written if detail_only else None,required_subjects,replace_summary,concealed_subjects)
                        write_json(scope['directory']/f"structured-reference-repair-{scope['repair_contexts']:03d}.json",{
                            'schema_sha256':digest(schema),'model_response_sha256':digest(reply.content),
                            'serialized_prompt_sha256':digest(corrected),'section_names':list(_REFERENCE_SECTION_NAMES),
                            'replaced_sections':['detailed_description',*(['summary'] if replace_summary else []),*(['retention_analysis (concealed subjects only)'] if concealed_subjects else [])] if detail_only else list(_REFERENCE_SECTION_NAMES),
                            'required_existing_subjects':required_subjects,
                            'unchanged_sections_preserved':detail_only,
                            'serialization':'headers and separators only; replacement bodies from actual model response, other bodies from original model-written candidate',
                            'semantic_text_inserted_by_adapter':False,'native_gates_unchanged':True})
                        return SimpleNamespace(content=corrected)
                backend=FeedbackAtEnd()
            sampling_token=None
            if scope is not None:
                sampling_token=_REPAIR_SAMPLING.set(_native_correction_sampling(scope,scope['repair_contexts']))
            try:return original_fix(backend,written,findings,*args,**kwargs)
            finally:
                if sampling_token is not None:_REPAIR_SAMPLING.reset(sampling_token)
        validate_written._factory_revision_validation_version=POLICY_REVISION
        compiler._written_context=written_context
        compiler.validate=validate_written
        compiler.compose_brief=compose_written
        compiler.repair_brief=repair_written
        repair._fix_labels=repair_labels
        prose.fix_with_findings=fix_written


def _install_document_gate(namespace):
    """Wrap the serializer's real namespace, never a replacement Main/output."""
    with _INSTALL_LOCK:
        original=namespace['_as_the_service_answers']
        previous=getattr(original,'_factory_source_gate_version',None)
        if previous==VERSION:return
        if previous:raise LocalProfileFailure('Another compiler source gate is installed')

        @functools.wraps(original)
        def checked_document(doc,brief):
            scope=_ACTIVE.get()
            if scope is None:return original(doc,brief)
            scope['compiler_documents']+=1
            folder=scope['directory']/('compiler-'+str(scope['compiler_documents']).zfill(3))
            folder.mkdir()
            source=getattr(doc,'source',None)
            record={'profile':VERSION,'source':source,
                    'fallback_reason':getattr(doc,'fallback_reason',None),
                    'gate':'actual IRDocument.source before Main loads H3 weights',
                    'required_source':'written','status':'BLOCKED'}
            # These are the actual compiler facts, including rejected prose and
            # rules when present; do not infer quality from HTTP or output count.
            write_json(folder/'document.json',_document_evidence(doc))
            if is_dataclass(brief):write_json(folder/'brief.json',asdict(brief))
            try:
                if source!='written' or getattr(doc,'fallback_reason',None):
                    reason=record['fallback_reason'] or ''
                    validation=re.fullmatch(r'the written brief still failed after [0-9]{1,2} correction pass\(es\): '
                                            r'([A-Za-z0-9_;, -]{1,300})',reason)
                    rule_summary=''
                    if validation:
                        record['validation_rules']=re.split(r'[;,] ',validation[1])
                        rule_summary=f'validation rules: {validation[1]}; '
                    raise LocalProfileBlocked('Compiler source gate BLOCKED '+repr(source)
                        +'; requires actual source=written without fallback; '+rule_summary+'evidence: '+str(folder))
                if scope.get('reference_contract') is not None:
                    try:doc=_audit_or_project(doc,brief,scope,folder)
                    except Exception as error:
                        path=folder/'reference-binding.final.json'
                        if not path.exists():
                            baseline=json.loads((folder/'reference-binding.before.json').read_text(encoding='utf-8')) if (folder/'reference-binding.before.json').exists() else {}
                            write_json(path,{**baseline,'status':'BLOCKED',
                                'correction_attempts':scope['reference_corrections'],
                                'binding_projections':scope['binding_projections'],
                                'correction_error_type':type(error).__name__,'correction_error':str(error)})
                        raise LocalProfileBlocked('Declared reference projection BLOCKED before H3 loading: '
                            +type(error).__name__+': '+str(error)+'; evidence: '+str(folder)) from error
                doc=_apply_revision_constraints(doc,brief,scope,folder)
                semantic_findings=_revision_semantic_findings(doc.prompt,scope,plan=doc.plan,final_folder=folder)
                if semantic_findings:
                    raise LocalProfileBlocked('Caller revision contract still conflicts before H3 loading: '
                        +'; '.join(item['message'] for item in semantic_findings)+'; evidence: '+str(folder))
                body=original(doc,brief)
                write_json(folder/'body.json',body)
                if not isinstance(body,dict) or body.get('degraded') is not False or body.get('fallback_reason'):
                    raise LocalProfileBlocked('Compiler body contradicts source=written; evidence: '+str(folder))
                record.update(status='PASS',body_sha256=digest(body))
                from .reference_analysis import trace
                contract = scope.get('reference_contract') or {}
                trace(scope, 'ir_schema_semantic_gate_passed', contract.get('references') or [],
                      source=source, body_sha256=record['body_sha256'], direct_h3_reference_count=len(brief.assets),
                      semantic_fallback=False, visual_constraint_scope='all attached originals remain available to the native workflow')
                provenance=getattr(doc,'provenance',{}) or {}
                record.update(creative_text_source='model_written',
                    binding_source=provenance.get('binding_source','model_written'),
                    visual_facts_added=False,
                    caller_authored_constraints_added=bool(provenance.get('caller_authored_constraints_added')
                        or any(item.startswith(_CAMERA_REPAIR) for item in provenance.get('repairs',[]))))
                binding_path=folder/'reference-binding.final.json'
                if binding_path.exists():
                    binding=json.loads(binding_path.read_text(encoding='utf-8'))
                    binding.update(body_sha256=digest(body),
                        final_document_filename='document.projected.json' if (folder/'document.projected.json').exists() else 'document.json')
                    write_json(binding_path,binding)
                return body
            except Exception as error:
                record.update(error_type=type(error).__name__,error=str(error))
                from .reference_analysis import trace
                trace(scope, 'ir_schema_semantic_gate_failed', (scope.get('reference_contract') or {}).get('references') or [],
                      function='checked_document', error_type=type(error).__name__, error=str(error), evidence=str(folder))
                raise
            finally:write_json(folder/'source-gate.json',record)

        checked_document._factory_source_gate_version=VERSION
        namespace['_as_the_service_answers']=checked_document


def install_compiler_gate(compile_here):
    """Pin the audited path: compile_here serializes before Main's loaders.

    The vendor body deliberately drops source, so checking its degraded flag
    alone cannot distinguish every draft. Inspect the real Document first.
    """
    if source_hash(compile_here)!=COMPILER_CALL_SHA256:
        raise LocalProfileFailure('The installed compile_here contract changed; source gate was not guessed')
    namespace=compile_here.__globals__
    serializer=namespace.get('_as_the_service_answers')
    if not callable(serializer) or source_hash(inspect.unwrap(serializer))!=SERIALIZER_SHA256:
        raise LocalProfileFailure('The installed document serializer changed; source gate was not guessed')
    _install_document_gate(namespace)
    _install_typed_dialogue(namespace)
    _install_analysis_capture()
    _install_reference_fusion()
    _install_revision_validation()
    _install_structural_render_compatibility()
    return {'compile_here_sha256':COMPILER_CALL_SHA256,'serializer_sha256':SERIALIZER_SHA256,
            'source_field':'actual h3ir.models.IRDocument.source',
            'required_source':'written','location':'before vendor serializer returns body to Main; before H3 loaders'}


def _install_typed_dialogue(namespace):
    original=namespace.get('brief_from_payload')
    if getattr(original,'_factory_typed_dialogue',False):return
    if not callable(original) or source_hash(original)!=BRIEF_CONVERTER_SHA256:
        raise LocalProfileFailure('The audited native Brief converter changed; typed dialogue was not guessed')
    @functools.wraps(original)
    def converted(payload,sha_of):
        brief=original(payload,sha_of)
        scope=_ACTIVE.get()
        rows=((scope or {}).get('reference_contract') or {}).get('locked_dialogue')
        constraints=((scope or {}).get('reference_contract') or {}).get('locked_production_constraints')
        if rows is None and constraints is None:return brief
        from h3ir.models import DialogueLine
        lines=[DialogueLine(text=row['text'],language=row['language'],speaker_hint=row.get('speaker')) for row in rows] if rows is not None else brief.dialogue
        if rows is not None and brief.dialogue and [asdict(line) for line in brief.dialogue]!=[asdict(line) for line in lines]:
            raise LocalProfileBlocked('Explicit MediaTray speech conflicts with the frozen Word dialogue; neither input was overwritten')
        if rows is not None:
            write_json(scope['directory']/'native-typed-dialogue.json',{'status':'PASS','source':'frozen caller Word dialogue',
                'lines':[asdict(line) for line in lines],'text_characters_modified':False,
                'converter_sha256':BRIEF_CONVERTER_SHA256,'native_verbatim_validation_required':True})
        merged=list(dict.fromkeys([*brief.constraints,*(constraints or [])]))
        if constraints is not None:
            write_json(scope['directory']/'native-typed-production-constraints.json',{'status':'PASS',
                'source':'caller-authored selected Word prop specifications, shot states and performer profiles',
                'constraints':constraints,'existing_native_constraints_preserved':True,
                'converter_sha256':BRIEF_CONVERTER_SHA256,'original_source_text_retained':True})
        return replace(brief,dialogue=lines,constraints=merged)
    converted._factory_typed_dialogue=True
    namespace['brief_from_payload']=converted


def _install_reference_fusion():
    import h3ir.plan as planner
    with _INSTALL_LOCK:
        original = planner.build_subjects
        if getattr(original, '_factory_reference_fusion', None) == POLICY_REVISION:
            return
        if source_hash(inspect.unwrap(original)) != BUILD_SUBJECTS_SHA256:
            raise LocalProfileFailure('The audited native subject planner changed; fusion was not guessed')

        @functools.wraps(original)
        def fused_subjects(*args, **kwargs):
            subjects = original(*args, **kwargs)
            scope = _ACTIVE.get()
            if scope is None:
                return subjects
            from .reference_fusion import fuse_product_subjects
            fused, receipt = fuse_product_subjects(subjects, scope.get('reference_contract'),
                                                   scope.get('product_identity_verification'))
            write_json(scope['directory'] / 'global-reference-fusion.json', receipt)
            return fused

        fused_subjects._factory_reference_fusion = POLICY_REVISION
        planner.build_subjects = fused_subjects


def _install_analysis_capture():
    import h3ir.compile as compiler
    with _INSTALL_LOCK:
        original=compiler.analyse_all
        if getattr(original,'_factory_card_capture_version',None)!=VERSION:
            @functools.wraps(original)
            def captured(*args,**kwargs):
                scope=_ACTIVE.get()
                if scope is None:
                    return original(*args, **kwargs)
                from .reference_analysis import analyse_references, ANALYSIS_SEED
                bound = inspect.signature(original).bind(*args, **kwargs)
                scope['analysis_seed'] = ANALYSIS_SEED
                try:
                    cards = analyse_references(original, bound, scope)
                except BaseException:
                    scope.pop('analysis_seed', None)
                    raise
                if scope is not None:
                    scope['cards']=cards
                    write_json(scope['directory']/'analysis-cards.json',
                               {key:asdict(card) for key,card in cards.items()})
                    contract=scope.get('reference_contract') or {}
                    if contract.get('identity_validation_required'):
                        from .product_reference_validation import verify_product_references,ProductReferenceConflict
                        bound=inspect.signature(original).bind(*args,**kwargs)
                        try:
                            scope['product_identity_verification']=verify_product_references(
                                bound.arguments['backend'],bound.arguments['refs'],cards,contract,
                                scope['directory'].parent/'product-identity-cache',ANALYSIS_SEED,
                                receipt_root=scope['directory'])
                        except ProductReferenceConflict as error:
                            scope.pop('analysis_seed', None)
                            raise LocalProfileBlocked(str(error)) from error
                scope.pop('analysis_seed', None)
                return cards
            captured._factory_card_capture_version=VERSION
            compiler.analyse_all=captured


def install_render_policy(main_class):
    execute=getattr(main_class.execute,'__func__',main_class.execute)
    namespace=execute.__globals__;original=namespace['render_fields']
    if source_hash(inspect.unwrap(original))!=RENDER_FIELDS_SHA256 or source_hash(main_class._condition)!=CONDITION_SHA256:
        raise LocalProfileFailure('The audited image sizing/conditioning contract changed')
    stock=Path(inspect.getfile(main_class)).resolve().parents[2]/'comfy_extras/nodes_minimax_h3.py'
    if not stock.is_file() or hashlib.sha256(stock.read_bytes()).hexdigest()!=STOCK_H3_SHA256:
        raise LocalProfileFailure('The audited stock H3 image/video sizing implementation changed')
    with _INSTALL_LOCK:
        if getattr(original,'_factory_image_sizing_version',None)==VERSION:return
        @functools.wraps(original)
        def render_fields(body):
            scope=_ACTIVE.get()
            if scope is None:return original(body)
            record={'profile':VERSION,'status':'BLOCKED','body_sha256':digest(body),
                    'requested_image_sizing':scope['sizing'],
                    'render_fields_source_sha256':RENDER_FIELDS_SHA256,
                    'execute_source_sha256':source_hash(main_class.execute),
                    'condition_source_sha256':CONDITION_SHA256,'stock_h3_file_sha256':STOCK_H3_SHA256,
                    'call_site':'actual Main.execute -> scoped render_fields -> actual Main._condition(ref_image_size=effective_tuple[4])',
                    'video_sizing_modified':False,'wiring_modified':False,'memory_stability':'NOT_VERIFIED'}
            try:
                fields=original(body)
                record['original_tuple']=list(fields)
                images=[w for w in body.get('wiring',[]) if w.get('kind')=='image' and str(w.get('wiring','')).startswith('ref_image_')]
                videos=[w for w in body.get('wiring',[]) if w.get('kind')=='video']
                record.update(image_wiring=images,video_wiring=videos)
                sizings={w.get('sizing') for w in images}
                if images and (len(sizings)!=1 or next(iter(sizings))!=scope['sizing']):
                    raise LocalProfileBlocked('Image wiring disagrees with the declared image sizing before H3 loading')
                effective=fields[:4]+(scope['sizing'] if images else fields[4],)
                record.update(status='PASS',effective_tuple=list(effective),
                              sizing_rule='image wiring only; stock video encoding independently uses adapt_canvas')
                return effective
            except Exception as error:
                record.update(error_type=type(error).__name__,error=str(error))
                if isinstance(error,LocalProfileBlocked):
                    raise LocalProfileBlocked('Image sizing BLOCKED before H3 loading: '+str(error)
                        +'; evidence: '+str(scope['directory'])) from error
                raise
            finally:write_json(scope['directory']/'render-fields.json',record)
        render_fields._factory_image_sizing_version=VERSION
        namespace['render_fields']=render_fields


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def write_json(path,value):
    Path(path).write_bytes(json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')+b'\n')


def fixed_endpoint(address):
    try:
        p=urlsplit(str(address))
        return (p.scheme=='http' and p.hostname in ('127.0.0.1','localhost') and p.port==8772
                and p.path.rstrip('/')=='/v1' and not any((p.username,p.password,p.query,p.fragment)))
    except ValueError:
        return False


def require_environment():
    expected={'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','H3IR_GUIDED_DECODING':'1',
              'H3IR_LLM_THINKING':'0','H3IR_LLM_MAX_TOKENS':'4096'}
    wrong=[key for key,value in expected.items() if os.environ.get(key)!=value]
    if wrong:
        raise LocalProfileFailure('Local 4B profile requires explicit offline/compiler environment: '+', '.join(wrong))
    return expected


def bounded_card_schema(schema):
    """Preserve vocabulary/required keys; bound only previously unbounded prose."""
    result=deepcopy(schema)
    changes=[]
    array_limits={'subjects':8,'attributes':8,'pose':6,'visible_text':8,'palette':8}
    string_limits={'summary':512,'descriptor':256,'attributes':160,'pose':160,
                   'environment':512,'lighting':256,'framing':256,'style':256,
                   'visible_text':128,'palette':64,'camera':256}
    def walk(node,path=(),field=''):
        if not isinstance(node,dict):return
        if node.get('type')=='string' and 'enum' not in node and 'const' not in node and 'maxLength' not in node:
            node['maxLength']=string_limits.get(field,512)
            changes.append({'path':'/'.join(path),'added_maxLength':node['maxLength']})
        if node.get('type')=='array' and 'maxItems' not in node:
            node['maxItems']=array_limits.get(field,8)
            changes.append({'path':'/'.join(path),'added_maxItems':node['maxItems']})
        for key,child in node.get('properties',{}).items():walk(child,path+('properties',key),key)
        if isinstance(node.get('items'),dict):walk(node['items'],path+('items',),field)
        for key in ('anyOf','oneOf','allOf'):
            for i,child in enumerate(node.get(key,[])):walk(child,path+(key,str(i)),field)
        for key in ('$defs','definitions'):
            for name,child in node.get(key,{}).items():walk(child,path+(key,name),name)
    walk(result)
    return result,changes


def concise_card_messages(messages):
    result=deepcopy(messages)
    instruction=('Local 4B analysis budget: return concise complete factual phrases. '
        'Use at most 8 reusable subjects, 8 short attributes and 6 short pose observations per subject; '
        'at most 8 clearly legible text entries, without repeating text. '
        'Do not fill the budget with guessed objects, words, materials or identity claims. '
        'Summarize observed evidence within the supplied field lengths; do not copy this instruction or a schema. '
        'Unknown or unobservable details should be stated as uncertain, without inventing an answer.')
    for message in reversed(result):
        if message.get('role')=='user':
            content=message.get('content')
            if isinstance(content,list):message['content']=content+[{'type':'text','text':instruction}]
            elif isinstance(content,str):message['content']=content+'\n\n'+instruction
            else:raise LocalProfileFailure('Unsupported user message; no input was coerced or truncated')
            return result
    raise LocalProfileFailure('Analysis call has no user message; no replacement request was invented')


def active_for(backend):
    scope=_ACTIVE.get()
    cfg=getattr(backend,'cfg',None)
    if scope is None or cfg is None:return None
    if not fixed_endpoint(getattr(cfg,'base_url','')) or getattr(cfg,'model',None)!=MODEL:
        raise LocalProfileFailure('An active local compiler scope refused a different endpoint or model')
    if getattr(cfg,'api_key','not-needed') not in ('','not-needed',None):
        raise LocalProfileFailure('This fixed offline profile does not accept or persist API credentials')
    return scope


class RecordingClient:
    def __init__(self,client,folder,record):
        self.client,self.folder,self.record=client,folder,record

    def __getattr__(self,name):return getattr(self.client,name)

    def post(self,*args,**kwargs):
        try:
            response=self.client.post(*args,**kwargs)
        except Exception as error:
            request=getattr(error,'request',None)
            if request is not None:
                try:self.save_request(request.content)
                except Exception:pass
            raise
        self.record['http_status']=response.status_code
        raw=response.content
        (self.folder/'response.raw.bin').write_bytes(raw)
        self.record['response_sha256']=hashlib.sha256(raw).hexdigest()
        self.record['response_bytes']=len(raw)
        request=getattr(response,'request',None)
        if request is not None:self.save_request(request.content)
        try:
            self.record['_response']=response.json()
            write_json(self.folder/'response.json',self.record['_response'])
        except ValueError:pass
        return response

    def save_request(self,raw):
        (self.folder/'request.wire.json').write_bytes(raw)
        self.record['wire_request_sha256']=hashlib.sha256(raw).hexdigest()
        self.record['wire_request_bytes']=len(raw)


def install(backend_class=None):
    """Install inert wrappers once; only profile_scope can activate them."""
    if backend_class is None:
        if importlib.metadata.version('open-h3-ir')!='0.4.1':
            raise LocalProfileFailure('This compatibility profile is verified only against open-h3-ir 0.4.1')
        from h3ir.backend import Backend
        backend_class=Backend
    with _INSTALL_LOCK:
        previous=getattr(backend_class,'_factory_local_profile_version',None)
        if previous==VERSION:return
        if previous:raise LocalProfileFailure('Another local compiler profile is already installed: '+str(previous))
        original_json=backend_class.json_call
        original_chat=backend_class.chat
        original_once=backend_class._once
        original_http=backend_class._http

        @functools.wraps(original_http)
        def http(self):
            if active_for(self) is None:return original_http(self)
            if self._client is None:
                import httpx
                self._client=httpx.Client(timeout=self.cfg.timeout_s,trust_env=False,follow_redirects=False)
            return self._client

        @functools.wraps(original_json)
        def json_call(self,messages,schema,**kwargs):
            scope=active_for(self)
            if scope is None:return original_json(self,messages,schema,**kwargs)
            is_card=schema.get('title') in ('ImageCard','VideoCard')
            effective,changes=bounded_card_schema(schema) if is_card else (deepcopy(schema),[])
            requested={'max_tokens':kwargs.get('max_tokens') or self.cfg.max_tokens,
                       'temperature':kwargs.get('temperature',0.0),'thinking':kwargs.get('thinking'),
                       'seed':kwargs.get('seed'),'retries':kwargs.get('retries',2),**kwargs}
            stage={'kind':'asset_card' if is_card else 'structured',
                   'original_schema':deepcopy(schema),'effective_schema':effective,'schema_changes':changes,
                   'original_messages':deepcopy(messages),'requested_parameters':deepcopy(requested)}
            token=_STAGE.set(stage)
            effective_kwargs={**kwargs,'retries':0,'temperature':0.2,'thinking':False,'seed':scope.get('analysis_seed',scope['seed'])}
            effective_kwargs['max_tokens']=min(kwargs.get('max_tokens') or self.cfg.max_tokens,CARD_OUTPUT if is_card else MAX_OUTPUT)
            try:
                value=original_json(self,concise_card_messages(messages) if is_card else messages,effective,**effective_kwargs)
                # The fixed local service executes jsonschema post-validation;
                # _once verifies its effective-schema SHA and receipt. Do not
                # add a missing second dependency to the Comfy environment.
                return value
            except LocalProfileFailure:raise
            except Exception as error:
                raise LocalProfileFailure('Local compiler structured stage failed without retry/fallback: '
                    +type(error).__name__+': '+str(error)+'; profile receipts: '+str(scope['directory'])) from error
            finally:_STAGE.reset(token)

        @functools.wraps(original_chat)
        def chat(self,messages,**kwargs):
            scope=active_for(self)
            if scope is None:return original_chat(self,messages,**kwargs)
            stage=_STAGE.get()
            temperature,seed=_effective_sampling(scope)
            limit=CARD_OUTPUT if stage and stage['kind']=='asset_card' else MAX_OUTPUT
            effective={**kwargs,'max_tokens':min(kwargs.get('max_tokens') or self.cfg.max_tokens,limit),
                       'temperature':temperature,'thinking':False,'seed':seed,'retries':0}
            requested={'max_tokens':kwargs.get('max_tokens') or self.cfg.max_tokens,
                       'temperature':kwargs.get('temperature',0.7),'thinking':kwargs.get('thinking'),
                       'seed':kwargs.get('seed'),'retries':kwargs.get('retries',2),**kwargs}
            call={'stage':deepcopy(stage),'original_messages':deepcopy(stage['original_messages'] if stage else messages),
                  'requested_parameters':deepcopy(requested),'effective_parameters':deepcopy(effective)}
            media=_COMPOSE_MEDIA.get()
            if media is not None and stage is None:
                messages=ground_writer_messages(messages,media)
            token=_CALL.set(call)
            try:return original_chat(self,messages,**effective)
            except LocalProfileFailure:raise
            except Exception as error:
                raise LocalProfileFailure('Local compiler chat failed without retry/fallback: '
                    +type(error).__name__+': '+str(error)+'; profile receipts: '+str(scope['directory'])) from error
            finally:_CALL.reset(token)

        @functools.wraps(original_once)
        def once(self,body):
            scope=active_for(self)
            if scope is None:return original_once(self,body)
            scope['requests']+=1
            folder=scope['directory']/('request-'+str(scope['requests']).zfill(3))
            folder.mkdir()
            call=_CALL.get() or {}
            stage=_STAGE.get()
            limit=CARD_OUTPUT if stage and stage['kind']=='asset_card' else MAX_OUTPUT
            temperature,seed=_effective_sampling(scope)
            effective=deepcopy(body)
            effective['trace_id'] = (scope.get('reference_contract') or {}).get('trace_id') or scope['directory'].name
            effective.update(max_tokens=min(body.get('max_tokens') or self.cfg.max_tokens,limit),
                             temperature=temperature,seed=seed,chat_template_kwargs={'enable_thinking':False})
            requested=deepcopy(body)
            requested['messages']=deepcopy(call.get('original_messages',body.get('messages')))
            original_parameters=(stage or {}).get('requested_parameters',call.get('requested_parameters',{}))
            for key in ('max_tokens','temperature','seed'):
                if key in original_parameters:requested[key]=original_parameters[key]
            requested_thinking=original_parameters.get('thinking')
            if requested_thinking is None:requested_thinking=self.cfg.default_thinking
            if requested_thinking:requested.pop('chat_template_kwargs',None)
            if stage and 'response_format' in requested:
                requested['response_format']['json_schema']['schema']=deepcopy(stage['original_schema'])
            write_json(folder/'request.original.json',requested)
            write_json(folder/'request.effective.json',effective)
            if stage:
                write_json(folder/'schema.original.json',stage['original_schema'])
                write_json(folder/'schema.effective.json',stage['effective_schema'])
            record={'version':VERSION,'stage':(stage or {}).get('kind','prose'),
                    'native_correction_sampling':deepcopy(_REPAIR_SAMPLING.get()),
                    'original_request_sha256':digest(requested),'effective_request_sha256':digest(effective),
                    'requested_parameters':original_parameters,'effective_parameters':call.get('effective_parameters',{}),
                    'schema_changes':(stage or {}).get('schema_changes',[]),'automatic_retries':0,
                    'schema_validation_location':'fixed offline VLM service; verified receipt and schema SHA' if stage else None,
                    'input_text_or_media_truncated':False,'status':'FAIL'}
            image_inputs = []
            for message in effective.get('messages', []):
                parts = message.get('content')
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if part.get('type') != 'image_url':
                        continue
                    value = part.get('image_url', {}).get('url', '')
                    raw = base64.b64decode(value.split(',', 1)[1], validate=True)
                    image_sha = hashlib.sha256(raw).hexdigest()
                    bindings = [row for row in (scope.get('reference_contract') or {}).get('references', [])
                                if row.get('sha256') == image_sha]
                    image_inputs.append({'sha256': image_sha, 'image_ids': [row['image_id'] for row in bindings],
                                         'bytes': len(raw), 'request_image_ordinal': len(image_inputs)+1})
            record.update(trace_id=effective['trace_id'], image_count=len(image_inputs), source_images=image_inputs,
                          external_interface=self.cfg.base_url.rstrip('/')+'/chat/completions')
            from .reference_analysis import trace
            trace(scope, 'qwen_request', image_inputs, external_interface=record['external_interface'],
                  request_index=scope['requests'], compiler_stage=record['stage'])
            if stage:record.update(original_schema_sha256=digest(stage['original_schema']),effective_schema_sha256=digest(stage['effective_schema']))
            started=time.monotonic()
            real_client=self._http()
            self._client=RecordingClient(real_client,folder,record)
            try:
                reply=original_once(self,effective)
                if reply.finish_reason!='stop':
                    raise LocalProfileFailure('Local compiler reply did not finish normally: '+str(reply.finish_reason))
                response=record.get('_response')
                if not isinstance(response,dict):raise LocalProfileFailure('No actual local HTTP response was captured')
                receipt=response.get('factory_receipt',{})
                if (receipt.get('offline') is not True or receipt.get('model_id')!=MODEL
                        or receipt.get('gpu_cleanup',{}).get('released') is not True):
                    raise LocalProfileFailure('Actual reply did not prove fixed offline model and confirmed GPU release')
                if stage:
                    control=receipt.get('response_format_control',{})
                    if (control.get('guided_decoding') is not True or control.get('post_validation') is not True
                            or control.get('schema_sha256')!=digest(stage['effective_schema'])):
                        raise LocalProfileFailure('Actual reply did not prove enforcement of the effective schema')
                    # Upstream extracts JSON substrings. This profile requires
                    # the entire real reply to be one complete JSON object;
                    # schema validation remains at the fixed VLM service.
                    def invalid_constant(value):
                        raise ValueError('Non-JSON numeric constant: '+value)
                    parsed=json.loads(reply.content,parse_constant=invalid_constant)
                    if not isinstance(parsed,dict):
                        raise LocalProfileFailure('Structured reply must be one complete JSON object')
                    record.update(complete_json_parse=True,parsed_json_sha256=digest(parsed))
                record.update(status='PASS',finish_reason=reply.finish_reason,
                              usage=response.get('usage'),factory_receipt=receipt)
                return reply
            except Exception as error:
                record.update(error_type=type(error).__name__,error=str(error))
                trace(scope, 'qwen_request_failed', image_inputs, error_type=type(error).__name__, error=str(error),
                      function='Backend._once', evidence=str(folder))
                raise LocalProfileFailure('Local compiler request failed without retry/draft fallback: '
                    +type(error).__name__+': '+str(error)+'; evidence: '+str(folder)) from error
            finally:
                self._client=real_client
                record.pop('_response',None)
                record['elapsed_seconds']=round(time.monotonic()-started,3)
                write_json(folder/'receipt.json',record)

        backend_class.json_call=json_call
        backend_class.chat=chat
        backend_class._once=once
        backend_class._http=http
        backend_class._factory_local_profile_version=VERSION


@contextmanager
def profile_scope(receipt_root,setup,seed=20261104,backend_class=None,compiler_function=None,
                  main_class=None,reference_contract=None,sizing='max'):
    if (not isinstance(setup,dict) or setup.get('server') or setup.get('llm_model')!=MODEL
            or not fixed_endpoint(setup.get('llm_url',''))):
        raise LocalProfileFailure('This explicit compiler scope only supports the fixed local 4B Setup')
    if type(seed) is not int or not 0<=seed<2**63:
        raise LocalProfileFailure('The fixed local VLM requires the real integer workflow seed in 0..2^63-1')
    environment=require_environment()
    if urlsplit(setup['llm_url']).hostname=='localhost':
        resolved=socket.getaddrinfo('localhost',8772,type=socket.SOCK_STREAM)
        if not resolved or not all(ipaddress.ip_address(entry[4][0]).is_loopback for entry in resolved):
            raise LocalProfileFailure('localhost must resolve exclusively to loopback addresses')
    install(backend_class)
    compiler_contract=install_compiler_gate(compiler_function) if compiler_function else None
    if sizing not in ('max','match'):raise LocalProfileFailure('Unsupported declared image sizing')
    if main_class is not None:install_render_policy(main_class)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%fZ')
    directory=Path(receipt_root)/(stamp+'-'+uuid.uuid4().hex[:8]);directory.mkdir(parents=True)
    scope={'directory':directory,'seed':seed,'requests':0,'compiler_documents':0,
           'reference_corrections':0,'binding_projections':0,'reference_contract':reference_contract,'setup':deepcopy(setup),'sizing':sizing}
    receipt={'profile':VERSION,'policy_revision':POLICY_REVISION,'model':MODEL,'base_url':setup['llm_url'],'environment':environment,
             'max_output_tokens':MAX_OUTPUT,'card_max_output_tokens':CARD_OUTPUT,'seed':seed,
             'temperature':0.2,'retries':0,'status':'FAIL','semantic_status':'NOT_REVIEWED',
             'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'scope':'explicit project wrapper Main only; all user/media inputs preserved'}
    receipt['compiler_source_gate']=compiler_contract
    receipt.update(card_cache_policy='per-image SHA, model/source, schema/prompt, fixed visual seed, note and provenance; image facts reused across video seeds',analysis_seed=1247,max_reference_corrections=2,
                   max_binding_projections=1,
                   reference_contract_required=reference_contract is not None,
                   requested_image_sizing=sizing)
    if reference_contract is not None:write_json(directory/'reference-contract.json',reference_contract)
    write_json(directory/'profile.json',receipt)
    token=_ACTIVE.set(scope)
    try:
        yield directory/'profile.json'
        if compiler_function and scope['compiler_documents']!=1:
            raise LocalProfileBlocked('Main did not produce exactly one audited compiler document')
        receipt['status']='PROFILE_SCOPE_COMPLETE'
    except Exception as error:
        receipt.update(status='BLOCKED' if isinstance(error,LocalProfileBlocked) else 'FAIL',
                       error_type=type(error).__name__,error=str(error));raise
    finally:
        _ACTIVE.reset(token)
        receipt['requests']=scope['requests']
        receipt['compiler_documents']=scope['compiler_documents']
        receipt['reference_corrections']=scope['reference_corrections']
        receipt['binding_projections']=scope['binding_projections']
        write_json(directory/'profile.json',receipt)
