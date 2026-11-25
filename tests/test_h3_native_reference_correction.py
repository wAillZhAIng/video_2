"""Declared binding feedback through the actual pinned native correction loop.

Authored model-reply fixtures only: no inference server, GPU, or media output.
Run directly with: python tests/test_h3_native_reference_correction.py
"""
from contextlib import ExitStack
from copy import deepcopy
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import h3ir.compile as compiler
import h3ir.prose as prose
import h3ir.repair as repair
from h3ir.models import (AssetKind, Brief, DialogueLine, IRDocument, ManifestEntry,
                         Mode, ModeDecision, Plan, Role, SubjectPlan, Target)

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard'
spec=importlib.util.spec_from_file_location('_native_reference_gate',SOURCE/'__init__.py',
    submodule_search_locations=[str(SOURCE)])
package=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=package
spec.loader.exec_module(package)
profile=importlib.import_module(spec.name+'.local_h3ir_backend_profile')


def authored_reply(text,options):
    """Explicit authored fixtures for both actual transport output shapes."""
    if options.get('response_format'):
        sections=compiler._split_written(text)
        fields=options['response_format']['json_schema']['schema']['properties']
        if 'before_subject_1' in fields:
            remaining=sections['detailed_description'];parts=({'summary':sections['summary']} if 'summary' in fields else {})
            if 'concealed_retention' in fields:
                parts['concealed_retention']={key:{'subject':schema['properties']['subject']['const'],
                    'relationship':'weak_reference','reason':'Only broad category/context for concealed contents; no product appearance is visible in this shot.'}
                    for key,schema in fields['concealed_retention']['properties'].items()}
            index=1
            while f'subject_{index}' in fields:
                target=fields[f'subject_{index}']['const']
                before,found,remaining=remaining.partition(target)
                if not found:raise AssertionError('Authored positive fixture does not contain the requested binding')
                parts[f'before_subject_{index}']=before.rstrip()
                parts[f'subject_{index}']=target
                remaining=remaining.lstrip();index+=1
            parts['tail']=remaining
            text=json.dumps(parts,ensure_ascii=False)
        else:text=json.dumps({name:sections[name] for name in fields},ensure_ascii=False)
    return SimpleNamespace(content=text)


def fixture(*,cited=False,aliases=False,caption=True):
    brief=Brief(intent='A photographic live-action product demonstration.',seconds=124/24,
        shots=1,dialogue=[DialogueLine('Look at this tree.',voiceover=True)],
        onscreen_text=['Tiny tree'],silent=True)
    sources=['<Picture 1>','<Picture 2>'] if aliases else ['<Picture 1>']
    plan=Plan(mode=Mode.REF2VA,target=Target(124/24,124,(672,384)),
        manifest=[ManifestEntry(i,source,AssetKind.IMAGE,str(i)*64,f'ref_image_{i}',Role.SUBJECT)
                  for i,source in enumerate(sources,1)],
        subjects=[SubjectPlan(f'<Subject {i}>','object',[source],'the plush Christmas tree')
                  for i,source in enumerate(sources,1)],speakers=[],shots=[],task_types=['reference generation'])
    contract={'identity_validation_required':True,'required_native':sources,
        'groups':[{'group':'product-1','category':'product','native_sources':sources,
                   'appearance_native':sources,'motion_native':[]}]}
    proof={'status':'PASS','approved_product_descriptors_by_native':
           {source:['the plush Christmas tree'] for source in sources}}
    definition='\n'.join(f'<Subject {i}> is the plush Christmas tree from {source}.'
                         for i,source in enumerate(sources,1))
    retention='\n'.join(f'<Subject {i}> (appears in [Shot 1]): fully_preserved - plush tree shape and embroidered face.'
                        for i in range(1,len(sources)+1))
    target=('<Subject 2>' if aliases else '<Subject 1>') if cited else 'the plush tree'
    detail=(f'The target video uses a photographic live-action style.\n[Shot 1] '
        f'A static camera frames {target} resting on a plain wooden tabletop. '
        f'The camera holds its position while a hand gently turns {target} toward the lens. '
        'The hand opens around the base without obscuring the embroidered face. '
        'Soft window light falls from the left, revealing the green fabric and rounded branches. '
        'A neutral wall stays softly out of focus behind the tabletop. '
        'The hand pauses and then lowers the toy back to the tabletop. '
        'The face stays visible as the hand withdraws slowly from the frame. '
        'The camera maintains the same composition through the final frame. '
        'An off-screen voiceover says <d>[English]Look at this tree.</d>. '
        + ('On-screen text reads "Tiny tree". ' if caption else '')
        + 'Quiet cloth rustling accompanies the movement, followed by still room tone.')
    prompt=(f'subject_definitions:\n{definition}\n\nsummary:\n[reference generation] <Subject 1> is demonstrated.\n\n'
        f'retention_analysis:\n{retention}\n\ndetailed_description:\n{detail}\n\n'
        'overall_soundscape:\nQuiet room tone and soft cloth rustling; an off-screen voiceover '
        'says "Look at this tree."\n\nnon_diegetic_music:\nN/A\n')
    return brief,plan,contract,proof,prompt


class NativeReferenceCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        for owner,name in ((compiler,'_written_context'),(compiler,'validate'),
                           (compiler,'compose_brief'),(compiler,'repair_brief'),
                           (repair,'_fix_labels'),(prose,'fix_with_findings')):
            replacement=patch.object(owner,name,inspect.unwrap(getattr(owner,name)))
            replacement.start();self.addCleanup(replacement.stop)
        token=profile._ACTIVE.set(None)
        self.addCleanup(profile._ACTIVE.reset,token)
        self.brief,self.plan,self.contract,self.proof,self.missing=fixture()
        self.corrected=fixture(cited=True)[-1]
        self.scope={'directory':self.directory,'reference_contract':self.contract,
            'product_identity_verification':self.proof,'reference_corrections':0,
            'compiler_documents':0,'binding_projections':0,'cards':{}}

    def activate(self):
        profile._install_revision_validation()
        token=profile._ACTIVE.set(self.scope)
        self.addCleanup(profile._ACTIVE.reset,token)

    def test_typed_word_dialogue_preserves_quotes_without_json_escape_roundtrip(self):
        def native_converter(payload,sha_of):
            return Brief(intent=payload['intent'],dialogue=payload.get('dialogue',[]),seconds=3,shots=1)
        namespace={'brief_from_payload':native_converter}
        text='"Time to open our Christmas dim sum surprise!"'
        self.scope['reference_contract']={'locked_dialogue':[{'text':text,'language':'English'}]}
        with patch.object(profile,'BRIEF_CONVERTER_SHA256',profile.source_hash(native_converter)):
            profile._install_typed_dialogue(namespace)
            wrapped=namespace['brief_from_payload']
            self.assertEqual(wrapped({'intent':'A closed gift box.'},None).dialogue,[])
            token=profile._ACTIVE.set(self.scope)
            try:
                actual=wrapped({'intent':'A closed gift box.'},None)
                self.assertEqual(actual.dialogue,[DialogueLine(text,'English')])
                self.assertNotIn('\\',actual.dialogue[0].text)
                self.assertEqual(actual.seconds,3)
                receipt=json.loads((self.directory/'native-typed-dialogue.json').read_text(encoding='utf-8'))
                self.assertFalse(receipt['text_characters_modified'])
                with self.assertRaisesRegex(profile.LocalProfileBlocked,'conflicts'):
                    wrapped({'intent':'A box.','dialogue':[DialogueLine('A conflicting line.')]},None)
                profile._install_typed_dialogue(namespace)
                self.assertIs(namespace['brief_from_payload'],wrapped)
            finally:profile._ACTIVE.reset(token)

    def test_native_prop_constraints_preserve_existing_constraints_without_inventing_dialogue(self):
        def native_converter(payload,sha_of):
            return Brief(intent=payload['intent'],constraints=list(payload.get('constraints',[])))
        namespace={'brief_from_payload':native_converter}
        statement='Caller prop P01: blue fuzzy handle, blue printed sleeve, closed lid.'
        self.scope['reference_contract']={'locked_production_constraints':[statement]}
        with patch.object(profile,'BRIEF_CONVERTER_SHA256',profile.source_hash(native_converter)):
            profile._install_typed_dialogue(namespace)
            token=profile._ACTIVE.set(self.scope)
            try:
                actual=namespace['brief_from_payload']({'intent':'A closed gift box.','constraints':['Existing caller rule.']},None)
                self.assertEqual(actual.constraints,['Existing caller rule.',statement])
                self.assertEqual(actual.dialogue,[])
                self.assertFalse((self.directory/'native-typed-dialogue.json').exists())
                receipt=json.loads((self.directory/'native-typed-production-constraints.json').read_text(encoding='utf-8'))
                self.assertTrue(receipt['existing_native_constraints_preserved'])
            finally:profile._ACTIVE.reset(token)

    def test_native_repair_gets_complete_locked_speech_and_existing_text_defined_people(self):
        from h3ir.models import Finding
        line='"Bring the holiday magic home today! Click the link to shop! This ending must not be lost."'
        self.contract['locked_dialogue']=[{'text':line,'language':'English','speaker':'全家合唱'}]
        self.activate()
        candidate=self.corrected.replace('summary:\n','<Subject 2> is the boy explicitly described by the caller.\nsummary:\n',1)
        observed=[]
        class Writer:
            def chat(self,messages,**kwargs):
                observed.append(messages)
                return authored_reply(candidate,kwargs)
        result=prose.fix_with_findings(Writer(),candidate,
            [Finding('D4-dialogue-not-verbatim','ERROR','Missing user line '+line[:60])],
            labels=('<Picture 1>','<Subject 1>'))
        assert result==candidate  # adapter never writes or inserts an IR line
        ask=observed[0][-1]['content']
        self.assertIn(line,ask)
        self.assertIn('<d>[English] '+line+'</d>',ask)
        self.assertNotIn('<caller_line>',ask)
        self.assertIn('FINAL CORRECTION CHECKLIST',ask.split('YOUR TEXT',1)[1])
        inventory=ask.split('These are the ONLY labels')[1].split('Fix every finding')[0]
        self.assertIn('<Subject 2>',inventory)
        self.assertNotIn('<Picture 2>',inventory)
        receipt=json.loads((self.directory/'repair-caller-context-001.json').read_text(encoding='utf-8'))
        self.assertTrue(receipt['model_must_write_the_fix'])
        self.assertFalse(receipt['new_media_labels_added'])

    def test_native_repair_receives_the_hash_verified_retention_grammar_after_the_candidate(self):
        from h3ir.models import Finding
        self.activate();observed=[]
        class Writer:
            def chat(self,messages,**kwargs):
                observed.append(messages)
                return authored_reply(self_result,kwargs)
        self_result=self.corrected
        result=prose.fix_with_findings(Writer(),self.corrected,
            [Finding('R1-malformed-entry','ERROR','unparseable Subject speaks fully_copy row')],
            labels=('<Picture 1>','<Subject 1>'))
        self.assertEqual(result,self.corrected)
        tail=observed[0][-1]['content'].split('FINAL CORRECTION CHECKLIST',1)[1]
        self.assertIn('[R1-malformed-entry]',tail)
        self.assertIn('## 4. `retention_analysis`',tail)
        self.assertIn('### 4.2 Audio',tail)
        self.assertIn('A visual Subject is not an Audio reference',tail)
        receipt=json.loads((self.directory/'repair-grammar-001.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['native_grammar_sha256'],profile.REFERENCE_GRAMMAR_SHA256)
        self.assertTrue(receipt['semantic_edits_by_model_only'])

    def test_native_repair_resends_complete_caller_states_and_profiles_without_rewriting_ir(self):
        from h3ir.models import Finding
        original='Keep the blue-handled package CLOSED; its established contents remain concealed.'
        performer='The same boy keeps short curls and the specified yellow-red hoodie.'
        self.brief.constraints=[original]
        self.contract['locked_production_constraints']=[performer,original]
        self.activate()
        compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        observed=[]
        candidate=self.missing
        class Writer:
            def chat(self,messages,**kwargs):
                observed.append(messages)
                return authored_reply(candidate,kwargs)
        result=prose.fix_with_findings(Writer(),candidate,
            [Finding('F5-declared-group-binding','ERROR','Existing Subject is not cited in detailed_description')],
            labels=('<Picture 1>','<Subject 1>'))
        self.assertEqual(result,candidate)
        tail=observed[0][-1]['content'].split('FINAL CORRECTION CHECKLIST',1)[1]
        self.assertIn(original,tail)
        self.assertIn(performer,tail)
        self.assertIn('Never invent containment',tail)
        self.assertIn('Still cite each required Subject in detailed_description',tail)
        receipt=json.loads((self.directory/'repair-caller-context-001.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['original_constraints_resent'],[original,performer])
        self.assertTrue(receipt['native_gates_unchanged'])
        # Merely adding instructions never turns an unchanged bad reply into PASS.
        findings=compiler.validate(result,self.scope['written_contexts'][-1])
        self.assertTrue(any(f.rule=='F5-declared-group-binding' and f.severity=='ERROR' for f in findings))

    def test_missing_hidden_product_citation_uses_only_existing_supported_containment_as_feedback(self):
        rule='Caller-authored screenplay prop Blind box: {"open_state": "closed"}.'
        self.brief.constraints=[rule]
        self.contract['locked_production_constraints']=[rule]
        summary=('[reference generation] The boy holds a closed bamboo steamer box. '
            'The product inside is the green plush tree, <Subject 1>.')
        from h3ir.compile import _split_written
        original_summary=_split_written(self.missing)['summary'].strip()
        candidate=self.missing.replace(original_summary,summary,1)
        before=deepcopy((self.brief,self.plan,self.contract,candidate))
        result=self.preview(prompt=candidate)
        self.assertEqual(result['status'],'BLOCKED')
        finding=next(row for row in result['findings'] if row['rule']=='F5-declared-group-binding')
        self.assertIn('The product inside is the green plush tree, <Subject 1>.',finding['message'])
        self.assertIn('section-placement correction',finding['message'])
        self.assertIn('keep its established contents concealed',finding['message'])
        self.assertEqual(before,(self.brief,self.plan,self.contract,candidate))
        # No source relationship or no caller closed-container rule: no invented hiding.
        self.assertEqual(profile._existing_containment_guidance(self.missing,'<Subject 1>',self.brief,self.contract),'')
        self.assertEqual(profile._existing_containment_guidance(candidate,'<Subject 1>',Brief(intent='No container rule.'),{}),'')

    def test_native_corrections_have_distinct_bounded_sampling_without_changing_normal_analysis(self):
        from h3ir.models import Finding
        self.scope.update(seed=123,analysis_seed=456)
        self.activate();observed=[];candidate=self.missing
        class Writer:
            def chat(self,messages,**kwargs):
                observed.append((deepcopy(profile._REPAIR_SAMPLING.get()),profile._effective_sampling(self_scope)))
                return authored_reply(candidate,kwargs)
        self_scope=self.scope
        self.assertEqual(profile._effective_sampling(self.scope),(0.2,456))
        for _ in range(2):
            result=prose.fix_with_findings(Writer(),candidate,
                [Finding('F5-declared-group-binding','ERROR','Missing detail identity token')],
                labels=('<Picture 1>','<Subject 1>'))
            self.assertEqual(result,candidate)
            self.assertIsNone(profile._REPAIR_SAMPLING.get())
        self.assertEqual([r[1][0] for r in observed],[0.7,0.9])
        self.assertNotEqual(observed[0][1][1],observed[1][1][1])
        self.assertEqual(observed[0][0]['round'],1)
        self.assertEqual(profile._effective_sampling(self.scope),(0.2,456))

    def test_native_correction_sampling_resets_when_backend_fails(self):
        from h3ir.models import Finding
        self.activate()
        class FailingWriter:
            def chat(self,messages,**kwargs):raise RuntimeError('real transport failure')
        with self.assertRaisesRegex(RuntimeError,'real transport failure'):
            prose.fix_with_findings(FailingWriter(),self.missing,
                [Finding('F5-declared-group-binding','ERROR','Missing binding')],
                labels=('<Picture 1>','<Subject 1>'))
        self.assertIsNone(profile._REPAIR_SAMPLING.get())

    def test_structured_reference_correction_serializes_only_model_section_bodies(self):
        sections=compiler._split_written(self.corrected)
        actual=profile._serialize_reference_correction(json.dumps(sections))
        self.assertEqual(compiler._split_written(actual),sections)
        self.assertEqual(actual,self.corrected)
        # A bad model still fails the real binding gate; serialization does
        # not invent the missing citation or use the summary as a substitute.
        missing=profile._serialize_reference_correction(json.dumps(compiler._split_written(self.missing)))
        self.assertEqual(self.preview(missing)['status'],'BLOCKED')

    def test_structured_reference_correction_rejects_partial_duplicate_and_non_json_outputs(self):
        for invalid in ['plain prose', '{}', '{"summary":"one","summary":"two"}',
                        json.dumps({**compiler._split_written(self.corrected),'extra':'unrequested'}),
                        json.dumps({**compiler._split_written(self.corrected),'detailed_description':''})]:
            with self.subTest(invalid=invalid[:60]):
                with self.assertRaises(profile.LocalProfileFailure):
                    profile._serialize_reference_correction(invalid)

    def test_reference_fix_uses_actual_guided_schema_without_replacing_native_validation(self):
        from h3ir.models import Finding
        observed=[]
        self.activate()
        candidate=self.corrected
        class Writer:
            def chat(self,messages,**kwargs):
                observed.append((messages,kwargs,profile._STAGE.get()))
                return authored_reply(candidate,kwargs)
        actual=prose.fix_with_findings(Writer(),self.missing,
            [Finding('F5-declared-group-binding','ERROR','Missing detail identity token')],
            labels=('<Picture 1>','<Subject 1>'))
        self.assertEqual(actual,candidate)
        self.assertEqual(observed[0][1]['response_format']['json_schema']['schema'],profile._reference_correction_schema())
        self.assertEqual(observed[0][2]['kind'],'native_reference_correction')
        self.assertIsNone(profile._STAGE.get())
        receipt=json.loads((self.directory/'structured-reference-repair-001.json').read_text())
        self.assertFalse(receipt['semantic_text_inserted_by_adapter'])
        self.assertTrue(receipt['native_gates_unchanged'])

    def test_focused_binding_repair_preserves_other_model_written_sections(self):
        from h3ir.models import Finding
        diagnostic=next(f for f in self.preview()['findings'] if f['rule']=='F5-declared-group-binding')
        findings=[Finding(diagnostic['rule'],diagnostic['severity'],diagnostic['message'])]
        self.assertTrue(profile._detail_only_reference_correction(self.missing,findings))
        observed=[]
        corrected=self.corrected
        class Writer:
            def chat(self,messages,**kwargs):
                observed.append((messages,kwargs))
                return authored_reply(corrected,kwargs)
        self.activate()
        actual=prose.fix_with_findings(Writer(),self.missing,findings,
                                     labels=('<Picture 1>','<Subject 1>'))
        before=compiler._split_written(self.missing);after=compiler._split_written(actual)
        self.assertEqual(after['detailed_description'],compiler._split_written(self.corrected)['detailed_description'])
        self.assertTrue(all(after[k]==v for k,v in before.items() if k!='detailed_description'))
        schema=observed[0][1]['response_format']['json_schema']['schema']
        self.assertEqual(schema['required'],['before_subject_1','subject_1','tail'])
        self.assertEqual(schema['properties']['subject_1']['const'],'<Subject 1>')
        task=json.loads(observed[0][0][-1]['content'])
        self.assertEqual(task['existing_subject_definitions'],before['subject_definitions'])
        self.assertEqual(task['current_section_body'],before['detailed_description'])
        receipt=json.loads((self.directory/'structured-reference-repair-001.json').read_text())
        self.assertEqual(receipt['replaced_sections'],['detailed_description'])
        self.assertTrue(receipt['unchanged_sections_preserved'])

    def test_focused_section_repair_cannot_guess_unbound_definition_or_other_faults(self):
        from h3ir.models import Finding
        self.assertFalse(profile._detail_only_reference_correction(self.missing,
            [Finding('F5-declared-group-binding','ERROR','Multiple duplicate product definitions')]))
        diagnostic=next(f for f in self.preview()['findings'] if f['rule']=='F5-declared-group-binding')
        valid=Finding(diagnostic['rule'],diagnostic['severity'],diagnostic['message'])
        self.assertFalse(profile._detail_only_reference_correction(self.missing,
            [valid,Finding('R1-malformed-entry','ERROR','Bad retention grammar')]))
        with self.assertRaises(profile.LocalProfileFailure):
            profile._serialize_reference_correction(json.dumps({'summary':'new story'}),self.missing)

    def test_binding_fragment_schema_is_dynamic_and_identity_values_are_model_fields(self):
        subjects=['<Subject 1>','<Subject 7>','<Subject 12>']
        schema=profile._binding_fragment_schema(subjects,'A complete native description.')
        self.assertEqual(len(schema['required']),7)
        parts={}
        for index,subject in enumerate(subjects,1):
            self.assertEqual(schema['properties'][f'subject_{index}']['const'],subject)
            parts[f'before_subject_{index}']=f'Model-written span {index}'
            parts[f'subject_{index}']=subject
        parts['tail']='Model-written final span.'
        actual=profile._serialize_reference_correction(json.dumps(parts),self.missing,subjects)
        expected=' '.join(parts.values())
        self.assertEqual(compiler._split_written(actual)['detailed_description'],expected)
        # Wrong/missing model identity fields never get inserted by Python.
        parts['subject_2']='<Subject 99>'
        with self.assertRaisesRegex(profile.LocalProfileFailure,'omit or change'):
            profile._serialize_reference_correction(json.dumps(parts),self.missing,subjects)
        with self.assertRaises(profile.LocalProfileFailure):profile._binding_fragment_schema([],self.missing)

    def test_caller_closed_container_reveal_is_rejected_in_actual_native_validation(self):
        from h3ir.compile import _split_written
        rule='Caller-authored screenplay prop Blind box: {"open_state": "closed"}.'
        self.brief.constraints=[rule]
        self.contract['locked_production_constraints']=[rule]
        summary=('[reference generation] The boy holds a closed bamboo steamer box. '
                 'The product inside is revealed as the green plush tree, <Subject 1>.')
        bad=self.corrected.replace(_split_written(self.corrected)['summary'],summary)
        self.activate()
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        findings=compiler.validate(bad,context)
        self.assertIn('F8-closed-contents-revealed',{f.rule for f in findings if f.severity=='ERROR'})
        self.assertEqual(self.scope['caller_containment_state']['closed_containment_subjects'],['<Subject 1>'])
        good=bad.replace('The product inside is revealed as the green plush tree, <Subject 1>.',
                         'The green plush tree, <Subject 1>, stays concealed inside the closed box.')
        row=next(line for line in _split_written(good)['retention_analysis'].splitlines() if line.startswith('<Subject 1>'))
        good=good.replace(row,'<Subject 1> (appears in [Shot 1]): weak_reference - only broad category/context for the concealed contents; no visible product appearance.')
        self.assertNotIn('F8-closed-contents-revealed',{f.rule for f in compiler.validate(good,context)})
        self.assertFalse([f for f in compiler.validate(good,context) if f.severity=='ERROR'])
        state=json.loads((self.directory/'caller-state-verify-002.json').read_text())
        self.assertEqual(state['status'],'PASS')
        self.assertFalse(state['rewrites_creative_text'])

    def test_mechanical_correction_cannot_introduce_a_new_scene_transition(self):
        self.activate()
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        compiler.validate(self.corrected,context)
        altered=self.corrected.replace('Quiet cloth rustling','<scenetrans> Quiet cloth rustling')
        self.assertIn('F9-correction-added-scene-transition',{f.rule for f in compiler.validate(altered,context)})

    def test_closed_summary_repair_is_model_written_and_preserves_other_four_sections(self):
        original=compiler._split_written(self.corrected)
        schema=profile._binding_fragment_schema(['<Subject 1>'],original['detailed_description'],original['summary'])
        summary='[reference generation] The sealed box keeps <Subject 1> concealed.'
        prefix,_,tail=original['detailed_description'].partition('<Subject 1>')
        reply={'summary':summary,'before_subject_1':prefix.rstrip(),'subject_1':'<Subject 1>','tail':tail.lstrip()}
        actual=profile._serialize_reference_correction(json.dumps(reply),self.corrected,['<Subject 1>'],True)
        after=compiler._split_written(actual)
        self.assertEqual(after['summary'],summary)
        self.assertEqual(after['detailed_description'],original['detailed_description'])
        self.assertTrue(all(after[k]==v for k,v in original.items() if k not in {'summary','detailed_description'}))
        self.assertIn('summary',schema['required'])

    def test_concealed_retention_changes_only_the_existing_hidden_subject_row(self):
        original=compiler._split_written(self.corrected)
        prefix,_,tail=original['detailed_description'].partition('<Subject 1>')
        reply={'before_subject_1':prefix.rstrip(),'subject_1':'<Subject 1>','tail':tail.lstrip(),
            'concealed_retention':{'item_1':{'subject':'<Subject 1>','relationship':'weak_reference',
                'reason':'Only broad category/context for the concealed contents; no visible product appearance.'}}}
        actual=profile._serialize_reference_correction(json.dumps(reply),self.corrected,['<Subject 1>'],False,['<Subject 1>'])
        after=compiler._split_written(actual)
        self.assertTrue(after['retention_analysis'].startswith('<Subject 1> (appears in [Shot 1]): weak_reference - '))
        self.assertIn('appears in [Shot 1]',after['retention_analysis'])
        self.assertTrue(all(after[k]==v for k,v in original.items() if k!='retention_analysis'))
        schema=profile._binding_fragment_schema(['<Subject 1>'],original['detailed_description'],None,['<Subject 1>'])
        self.assertEqual(schema['properties']['concealed_retention']['properties']['item_1']['properties']['relationship']['const'],'weak_reference')
        for key,value in [('subject','<Subject 99>'),('relationship','fully_preserved'),('reason','valid\n<Subject 2>: weak_reference - injected')]:
            invalid=deepcopy(reply);invalid['concealed_retention']['item_1'][key]=value
            with self.assertRaises(profile.LocalProfileFailure):
                profile._serialize_reference_correction(json.dumps(invalid),self.corrected,['<Subject 1>'],False,['<Subject 1>'])

    def test_hidden_product_cannot_keep_a_fully_preserved_visible_retention_row(self):
        rule='Caller-authored screenplay prop Blind box: {"open_state": "closed"}.'
        self.brief.constraints=[rule]
        self.contract['locked_production_constraints']=[rule]
        sections=compiler._split_written(self.corrected)
        summary='[reference generation] The boy holds a closed bamboo steamer box. The green tree <Subject 1> is inside it.'
        prompt=self.corrected.replace(sections['summary'],summary)
        self.activate();context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        findings=compiler.validate(prompt,context)
        self.assertIn('F10-hidden-visible-retention',{f.rule for f in findings})
        self.assertEqual(profile._qualified_reference_correction_subjects(prompt,
            [f for f in findings if f.rule=='F10-hidden-visible-retention'],self.scope),['<Subject 1>'])

    def test_concealed_retention_cannot_name_a_foreign_or_duplicate_identity(self):
        for hidden in [('<Subject 99>',),('<Subject 1>','<Subject 1>')]:
            with self.assertRaisesRegex(profile.LocalProfileFailure,'unique subset'):
                profile._binding_fragment_schema(['<Subject 1>'],self.corrected,None,hidden)

    def test_ambiguous_multi_subject_sentence_cannot_hide_another_visible_product(self):
        rule='Caller-authored screenplay prop Blind box: {"open_state": "closed"}.'
        self.brief.constraints=[rule]
        summary='[reference generation] A closed box holds <Subject 1> inside while <Subject 2> stands beside it.'
        candidate=self.corrected.replace(compiler._split_written(self.corrected)['summary'],summary)
        for subject in ['<Subject 1>','<Subject 2>']:
            self.assertEqual(profile._existing_containment_guidance(candidate,subject,self.brief,{}),'')

    def small_patch(self):
        schema=profile._small_concealed_patch_schema(['<Subject 1>'],['<Subject 1>'],[])['properties']
        return {'summary':schema['summary']['enum'][0],
            'before_subject_1':schema['before_subject_1']['enum'][0],'subject_1':'<Subject 1>',
            'tail':schema['tail']['enum'][0],
            'concealed_retention':{'item_1':{'subject':'<Subject 1>','relationship':'weak_reference',
                'reason':schema['concealed_retention']['properties']['item_1']['properties']['reason']['enum'][0]}}}

    def test_small_patch_preserves_original_action_and_all_unrelated_sections(self):
        reply=self.small_patch()
        block='Narrator: <d>[English]A complete caller line.</d>'
        reply['native_dialogue_1']=block
        actual=profile._apply_small_concealed_patch(self.missing,json.dumps(reply),['<Subject 1>'],['<Subject 1>'],[block])
        old=compiler._split_written(self.missing);new=compiler._split_written(actual)
        self.assertTrue(new['detailed_description'].startswith(old['detailed_description']+'\n'))
        self.assertTrue(new['detailed_description'].endswith(block))
        for name in ['subject_definitions','overall_soundscape','non_diegetic_music']:
            self.assertEqual(new[name],old[name])
        self.assertIn('<Subject 1> (appears in [Shot 1]): weak_reference',new['retention_analysis'])
        self.assertEqual(new['detailed_description'].count(block),1)

    def test_small_patch_schema_does_not_force_mid_sentence_character_cutoffs(self):
        schema=profile._small_concealed_patch_schema(['<Subject 1>'],['<Subject 1>'],[])
        self.assertNotIn('maxLength',json.dumps(schema))
        self.assertEqual(schema['properties']['subject_1']['const'],'<Subject 1>')
        self.assertFalse(schema['additionalProperties'])

    def test_small_patch_rejects_duplicate_keys_dialogue_edits_and_scene_controls(self):
        reply=self.small_patch();encoded=json.dumps(reply)
        duplicate=encoded[:-1]+',"tail":"duplicate"}'
        with self.assertRaisesRegex(profile.LocalProfileFailure,'Duplicate'):
            profile._apply_small_concealed_patch(self.missing,duplicate,['<Subject 1>'],['<Subject 1>'],[])
        for tag in ['<d>invented</d>','<scenetrans>','[Shot 2]']:
            bad=deepcopy(reply);bad['tail']+=tag
            with self.assertRaises(profile.LocalProfileFailure):
                profile._apply_small_concealed_patch(self.missing,json.dumps(bad),['<Subject 1>'],['<Subject 1>'],[])
        reply['native_dialogue_1']='changed line'
        with self.assertRaisesRegex(profile.LocalProfileFailure,'complete caller dialogue'):
            profile._apply_small_concealed_patch(self.missing,json.dumps(reply),['<Subject 1>'],['<Subject 1>'],['<d>[English]Exact line.</d>'])

    def test_missing_native_blocks_use_complete_caller_text_without_repeating_present_lines(self):
        rows=[{'text':'Look at this tree.','language':'English','speaker':None},
              {'text':'This entire ending must remain intact.','language':'English','speaker':None}]
        self.assertEqual(profile._missing_caller_dialogue_blocks(self.corrected,rows,self.plan),
                         ['<d>[English] This entire ending must remain intact.</d>'])

    def test_append_only_route_never_preserves_a_known_bad_reveal_duplicate_or_cut(self):
        from h3ir.models import Finding
        def finding(rule):return Finding(rule,'ERROR','authored unit finding')
        for rule in ['F8-closed-contents-revealed','F9-correction-added-scene-transition','D10-dialogue-line-duplicated','L2-unknown-subject']:
            self.assertFalse(profile._can_append_concealed_patch(['<Subject 1>'],[finding(rule)]))
        self.assertFalse(profile._can_append_concealed_patch([],[finding('F5-declared-group-binding')]))
        self.assertTrue(profile._can_append_concealed_patch(['<Subject 1>'],[finding('F10-hidden-visible-retention')]))
        summary=Finding('F8-closed-contents-revealed','ERROR','summary claims a reveal of the established contents.')
        detail=Finding('F8-closed-contents-revealed','ERROR','detailed_description claims a reveal of the established contents.')
        self.assertTrue(profile._can_append_concealed_patch(['<Subject 1>'],[summary],['<Subject 1>']))
        self.assertFalse(profile._can_append_concealed_patch(['<Subject 1>'],[detail],['<Subject 1>']))
        self.assertFalse(profile._can_append_concealed_patch(['<Subject 1>'],[summary],['<Subject 1>','<Subject 2>']))

    def test_small_writer_edits_only_closed_state_and_keeps_full_original_constraints_auditable(self):
        from h3ir.models import Finding
        reply=self.small_patch();observed=[]
        rule='Caller-authored screenplay prop box: {"open_state":"closed"}.'
        unchanged='An unrelated original performer and camera constraint.'
        self.scope['repair_contexts']=1
        def chat(messages,**options):
            observed.append((messages,options))
            return SimpleNamespace(content=json.dumps(reply))
        actual=profile._write_small_concealed_patch(SimpleNamespace(chat=chat),self.missing,
            self.scope,['<Subject 1>'],['<Subject 1>'],[rule,unchanged],[],
            [Finding('F10-hidden-visible-retention','ERROR','authored unit finding')])
        self.assertEqual(len(observed),1)
        messages,options=observed[0]
        self.assertEqual(options['max_tokens'],profile.MAX_OUTPUT)
        self.assertNotIn(unchanged,json.dumps(messages))
        self.assertIn('caller_closed_container_rules',messages[1]['content'])
        self.assertIn(profile.digest([rule,unchanged]),messages[1]['content'])
        self.assertTrue(compiler._split_written(actual.content)['detailed_description'].startswith(
            compiler._split_written(self.missing)['detailed_description']+'\n'))
        receipt=json.loads((self.directory/'small-reference-patch-001.json').read_bytes())
        self.assertEqual(receipt['closed_state_rules_provided_to_model'],[rule])
        self.assertEqual(receipt['untouched_caller_constraints_sha256'],profile.digest([rule,unchanged]))
        self.assertTrue(receipt['original_detailed_body_preserved_verbatim'])
        self.assertFalse(receipt['free_text_character_limit'])

    def test_modified_native_repair_grammar_is_rejected_instead_of_guessed(self):
        (self.directory/'ref-en.txt').write_text('changed upstream grammar',encoding='utf-8')
        with patch.object(prose,'get_config',return_value=SimpleNamespace(paths=SimpleNamespace(spec_dir=self.directory))):
            with self.assertRaisesRegex(profile.LocalProfileFailure,'grammar changed'):
                profile._reference_repair_grammar()

    def test_duplicate_fully_preserved_retention_is_formatted_before_native_validation(self):
        self.missing=self.corrected.replace('retention_analysis:\n',
            'retention_analysis:\n<Subject 1> (holder state): fully_preserved - the original green toy stays inside the blue-handled box.\n',1)
        doc,calls=self.compile([])
        self.assertEqual(doc.source,'written')
        self.assertEqual(calls,[])
        self.assertIn('holder state: the original green toy stays inside the blue-handled box.',doc.prompt)
        self.assertTrue(list(self.directory.glob('retention-row-merge-*.json')))

    def preview(self,prompt=None,brief=None,plan=None,contract=None,proof=None):
        return profile._native_reference_verification(prompt or self.missing,
            brief or self.brief,plan or self.plan,contract or self.contract,proof or self.proof)

    def test_writer_punctuation_normalization_preserves_the_verified_product_anchor(self):
        proof=deepcopy(self.proof)
        proof['approved_product_descriptors_by_native']['<Picture 1>']=['哑黑杯盖，琥珀纹杯身']
        written=self.corrected.replace('the plush Christmas tree from','哑黑杯盖, 琥珀纹杯身 from')
        result=self.preview(prompt=written,proof=proof)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['binding_only_audit']['group_results'][0]['subject'],'<Subject 1>')
        self.assertIn('哑黑杯盖，琥珀纹杯身',profile._product_anchor_instructions(self.contract,proof))
        wrong=written.replace('琥珀纹杯身','蓝色杯身')
        self.assertEqual(self.preview(prompt=wrong,proof=proof)['status'],'BLOCKED')

    def test_missing_actual_detail_token_becomes_precise_native_finding_without_mutation(self):
        original=deepcopy((self.brief,self.plan,self.contract,self.proof,self.scope))
        result=self.preview()
        self.assertEqual([row['rule'] for row in result['findings']],['F5-declared-group-binding'])
        self.assertIn('exact token <Subject 1> in its existing action text',result['findings'][0]['message'])
        self.assertIn('dialogue and on-screen text',result['findings'][0]['message'])
        self.assertEqual(result['binding_only_audit']['group_results'][0]['subject'],'<Subject 1>')
        self.assertTrue(all(result['projection_invariants'].values()))
        self.assertEqual((self.brief,self.plan,self.contract,self.proof,self.scope),original)
        self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.preview(self.corrected)['findings'],[])

    def test_valid_alias_projection_does_not_ask_model_to_undo_declared_merge(self):
        brief,plan,contract,proof,prompt=fixture(cited=True,aliases=True)
        result=self.preview(prompt,brief,plan,contract,proof)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['binding_only_audit']['group_results'][0]['subject'],'<Subject 1>')
        self.assertTrue(all(result['projection_invariants'].values()))

    def test_identity_failure_blocks_before_any_repair_or_projection(self):
        self.proof['status']='BLOCKED'
        with patch.object(profile,'project_declared_bindings') as project, \
             patch.object(prose,'fix_with_findings') as fix:
            with self.assertRaisesRegex(profile.LocalProfileBlocked,'身份'):
                self.preview()
        project.assert_not_called();fix.assert_not_called()

    def test_locked_caption_is_error_and_smart_quotes_are_normalized(self):
        missing=fixture(cited=True,caption=False)[-1]
        self.assertEqual([x['rule'] for x in self.preview(missing)['findings']],['F7-locked-onscreen-text'])
        self.assertEqual(self.preview(self.corrected.replace('"Tiny tree"','“Tiny tree”'))['findings'],[])

    def compile(self,replies):
        self.activate()
        backend=SimpleNamespace(cfg=SimpleNamespace(model=profile.MODEL),
            require_available=lambda:None,server_version=lambda:'fixture')
        calls=[]
        queue=iter(replies)
        def chat(messages,**kwargs):
            calls.append((deepcopy(messages),kwargs))
            return authored_reply(next(queue),kwargs)
        backend.chat=chat
        decision=ModeDecision(Mode.REF2VA,1.0,'fixture')
        draft=SimpleNamespace(prompt='authored deterministic draft fixture',sections={})
        with ExitStack() as stack:
            for name,value in {'check_request':None,'check_capacity':None,'measure_assets':None,
                'analyse_all':{},'infer_mode':decision,'deterministic_draft':self.plan,
                '_assess':(draft,[],200),'compose_brief':self.missing,
                'wiring_findings':[]}.items():
                stack.enter_context(patch.object(compiler,name,return_value=value))
            doc=compiler.compile_brief(self.brief,backend=backend,seed=23)
        return doc,calls

    def test_actual_native_loop_corrects_binding_and_preserves_locked_text(self):
        doc,calls=self.compile([self.corrected,self.corrected])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.provenance['fix_rounds'],1)
        self.assertEqual(self.scope['reference_corrections'],1)
        self.assertEqual(len(calls),1)
        ask=calls[0][0][1]['content']
        task=json.loads(ask)
        self.assertEqual(task['replace_sections'],['detailed_description'])
        self.assertEqual(task['actual_findings'][0]['rule'],'F5-declared-group-binding')
        self.assertEqual(task['existing_subject_definitions'],compiler._split_written(self.missing)['subject_definitions'])
        self.assertEqual(task['required_existing_subjects'],['<Subject 1>'])
        self.assertIn('Look at this tree.',doc.prompt)
        self.assertIn('"Tiny tree"',doc.prompt)
        self.assertEqual(self.preview(doc.prompt)['status'],'PASS')
        records=sorted(self.directory.glob('reference-written-verify-*.json'))
        self.assertEqual([json.loads(p.read_text(encoding='utf-8'))['status'] for p in records],['BLOCKED','PASS'])
        self.assertEqual(self.scope['binding_projections'],0)
        namespace={'_as_the_service_answers':lambda doc,_:{'degraded':False,'prompt':doc.prompt}}
        profile._install_document_gate(namespace)
        body=namespace['_as_the_service_answers'](doc,self.brief)
        self.assertIn('<Subject 1>',body['prompt'])
        folder=self.directory/'compiler-001'
        final=json.loads((folder/'reference-binding.final.json').read_text(encoding='utf-8'))
        projection=json.loads((folder/'binding-projection.json').read_text(encoding='utf-8'))
        self.assertEqual(final['status'],'PASS')
        self.assertEqual(final['correction_attempts'],1)
        self.assertTrue(all(projection['invariants'].values()))
        self.assertEqual(self.scope['binding_projections'],1)
        self.assertEqual(self.scope['reference_corrections'],1)

    def test_native_loop_restores_caption_dropped_during_binding_correction(self):
        doc,calls=self.compile([fixture(cited=True,caption=False)[-1],self.corrected])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.provenance['fix_rounds'],2)
        self.assertEqual(self.scope['reference_corrections'],2)
        self.assertIn('[F7-locked-onscreen-text]',calls[1][0][1]['content'])
        self.assertIn('"Tiny tree"',doc.prompt)
        self.assertIn('<d>[English]Look at this tree.</d>',doc.prompt)

    def test_actual_native_repair_restores_dialogue_paraphrased_by_correction_reply(self):
        reply=self.corrected.replace('<d>[English]Look at this tree.</d>',
            '<d>[English]Different spoken words.</d>')
        doc,calls=self.compile([reply])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(len(calls),1)
        self.assertIn('<d>[English]Look at this tree.</d>',doc.prompt)
        self.assertNotIn('Different spoken words.',doc.prompt)
        self.assertTrue(any('caller\'s exact line' in row for row in doc.provenance['repairs']))

    def test_native_verification_restores_frozen_pullback_without_rewriting_shot(self):
        self.missing=self.corrected.replace('A static camera frames','A medium shot shows') \
            .replace('The camera holds its position while a hand gently turns','A hand gently turns') \
            .replace('The camera maintains the same composition through the final frame. ','')
        sentence='The camera pulls out at slow speed.'
        self.contract['camera_sentence']=sentence
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        self.assertIn('P5-camera-no-motion-type',{f.rule for f in compiler.validate(self.missing,context)})

        doc,calls=self.compile([])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.provenance['fix_rounds'],0)
        self.assertEqual(calls,[])
        self.assertEqual(doc.prompt.replace(sentence+' ',''),self.missing)
        self.assertTrue(any(profile._CAMERA_REPAIR in row for row in doc.provenance['repairs']))
        self.assertEqual(profile._restore_camera_motion(doc.prompt,sentence),doc.prompt)

    def test_camera_repair_preserves_existing_movement_and_skips_ambiguous_shots(self):
        sentence='The camera pulls out at slow speed.'
        self.assertEqual(profile._restore_camera_motion(self.corrected,sentence),self.corrected)
        multiple='detailed_description:\n[Shot 1] One scene.\n[Shot 2] Another scene.'
        self.assertEqual(profile._restore_camera_motion(multiple,sentence),multiple)

    def test_scene_descriptions_use_the_shot_prose_and_preserve_subjects_and_dialogue(self):
        scene='The curved screen and cream leather interior stay in the background.'
        extra=f'<Scene State> (appears in [Shot 1]): fully_preserved - {scene}'
        self.missing=self.corrected.replace('\ndetailed_description:', f'\n{extra}\n\ndetailed_description:',1)
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        self.assertIn('R1-malformed-entry',{f.rule for f in compiler.validate(self.missing,context)})

        doc,calls=self.compile([])

        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(calls,[])
        self.assertNotIn('<Scene State>',doc.prompt)
        detail=doc.prompt.split('detailed_description:',1)[1].split('overall_soundscape:',1)[0]
        self.assertIn(scene,detail)
        self.assertIn('<d>[English]Look at this tree.</d>',doc.prompt)
        self.assertIn('"Tiny tree"',doc.prompt)
        self.assertEqual(profile._relocate_descriptive_retention(doc.prompt),(doc.prompt,[]))
        self.assertTrue(list(self.directory.glob('descriptive-retention-*.json')))

    def test_descriptive_retention_keeps_unknown_entity_tags_for_native_validation(self):
        bad=self.corrected.replace('\ndetailed_description:', '\n<Subject 99>: fully_preserved - unknown.\n\ndetailed_description:',1)
        self.assertEqual(profile._relocate_descriptive_retention(bad),(bad,[]))
        multiple=bad.replace('overall_soundscape:', '[Shot 2] Another view.\n\noverall_soundscape:',1)
        self.assertEqual(profile._relocate_descriptive_retention(multiple),(multiple,[]))

    def test_native_loop_removes_missing_motion_and_voice_attribution(self):
        original='the plush Christmas tree from <Picture 1>'
        with_phantoms=(original+' and whose walking motion comes from <Video 1>'
                       +' and whose voice is drawn from <Audio 1>')
        self.missing=self.corrected.replace(original,with_phantoms,1)
        doc,calls=self.compile([])

        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.prompt,self.corrected)
        self.assertEqual(doc.provenance['fix_rounds'],0)
        self.assertFalse(calls)
        self.assertEqual([entry.label for entry in doc.plan.manifest],['<Picture 1>'])
        self.assertIn('unattached <Audio 1>, <Video 1>',' '.join(doc.provenance['repairs']))

    def test_native_loop_asks_for_unsupported_action_citation_to_be_corrected(self):
        self.missing=self.corrected.replace('A static camera frames',
            'Taking its composition from <Picture 9>, a static camera frames',1)
        doc,calls=self.compile([self.corrected])

        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.prompt,self.corrected)
        self.assertEqual(len(calls),1)
        ask=calls[0][0][1]['content']
        self.assertIn('[L3-phantom-media]',ask)
        self.assertIn('Unattached tags to remove: <Picture 9>',ask)
        self.assertIn('Taking its composition from <Picture 9>',ask)
        self.assertNotIn('Taking its composition from <Picture 1>',ask)

    def test_unknown_attachment_claim_that_survives_correction_is_blocked(self):
        self.missing=self.corrected.replace('from <Picture 1>.','from <Picture 0>.',1)
        doc,calls=self.compile([self.missing,self.missing])

        self.assertEqual(len(calls),2)
        self.assertEqual(doc.source,'draft')
        self.assertIn('L3-phantom-media',doc.fallback_reason)

    def test_media_repairs_preserve_real_attachments_and_spoken_or_onscreen_text(self):
        original=('subject_definitions:\n<Subject 1> is the player from <Image 1>'
                  ' and whose motion comes from <Video 1> and whose voice comes from <Audio 1>.\n'
                  'detailed_description:\n[Shot 1] The camera arcs left and slowly tilts down. '
                  '<d>[English]Play <Video 10>.</d> The label reads "<Picture 07>".\n')
        repaired,changes=profile.repair_media_bindings(original,('<Picture 1>','<Video 1>','<Audio 1>'))

        self.assertEqual(repaired,original.replace('<Image 1>','<Picture 1>'))
        self.assertEqual(len(changes),1)
        self.assertEqual(profile.written_media_tags(repaired),{'<Picture 1>','<Video 1>','<Audio 1>'})

    def test_writer_guidance_uses_actual_inventory_and_leaves_caller_message_intact(self):
        caller={'role':'user','content':[{'type':'text','text':'Handheld, then a crane shot.'}]}
        messages=[{'role':'system','content':'Native compiler instructions.'},caller]
        for labels,expected in (((), 'none'), (('<Picture 1>',), '<Picture 1>'),
                                (('<Video 1>','<Audio 1>'), '<Video 1>, <Audio 1>')):
            effective=profile.ground_writer_messages(messages,labels)
            self.assertIn('Actual media attachments for this request: '+expected+'.',effective[0]['content'])
            self.assertTrue(effective[0]['content'].endswith(messages[0]['content']))
            self.assertEqual(effective[1],caller)

    def test_reference_and_existing_revision_findings_are_joint_native_errors(self):
        constraints=importlib.import_module(spec.name+'.revision_constraints')
        semantics=importlib.import_module(spec.name+'.revision_semantics')
        instruction='P02高10cm，宽8cm。第一帧P02由左手掌心托住，手与脸在同一画面；随后把P02抬起贴近脸颊。'
        scale=constraints.extract_revision_constraints(instruction)
        self.contract.update(revision_constraints=scale,constraint_target_groups={'P02':'product-1'},
            scene_requirements=semantics.extract_scene_requirements(instruction,scale))
        self.activate()
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        findings=compiler.validate(self.missing,context)
        rules={item.rule for item in findings if item.severity=='ERROR'}
        self.assertIn('F5-declared-group-binding',rules)
        self.assertIn('target_first_frame_palm_support',rules)
        self.assertEqual(self.scope['reference_corrections'],0)

    def test_unrepaired_binding_exhausts_native_two_rounds_and_source_gate_blocks_draft(self):
        # An unresolved definition cannot use the narrowly qualified text-span
        # route. It must still exhaust the original native two-round loop.
        self.missing=self.missing.replace('the plush Christmas tree from <Picture 1>.',
                                          'the plush Christmas tree without a reference definition.')
        doc,calls=self.compile([self.missing,self.missing])
        self.assertEqual(doc.source,'draft')
        self.assertEqual(doc.provenance['fix_rounds'],2)
        self.assertEqual(self.scope['reference_corrections'],2)
        self.assertEqual(len(calls),2)
        namespace={'_as_the_service_answers':lambda *_:{'degraded':False}}
        profile._install_document_gate(namespace)
        with self.assertRaisesRegex(profile.LocalProfileBlocked,'requires actual source=written'):
            namespace['_as_the_service_answers'](doc,self.brief)
        gate=json.loads((self.directory/'compiler-001/source-gate.json').read_text(encoding='utf-8'))
        self.assertEqual(gate['status'],'BLOCKED')
        self.assertFalse((self.directory/'compiler-001/body.json').exists())

    def test_repeated_install_does_not_stack_correction_counters(self):
        self.activate()
        first=prose.fix_with_findings
        profile._install_revision_validation()
        self.assertIs(prose.fix_with_findings,first)
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        compiler.validate(self.missing,context)
        compiler.validate(self.missing,context)
        self.assertEqual(self.scope['reference_corrections'],0)


if __name__=='__main__':unittest.main()
