"""Actual CPU grammar tests: no model, service or generated-media fixtures.

Use the local Qwen environment, or install the pinned lm-format-enforcer test
dependency. The ordinary backend container does not need a vision runtime.
"""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from lmformatenforcer import CharacterLevelParserConfig,JsonSchemaParser

ROOT=Path(__file__).resolve().parents[1]


class FixedJsonLiteralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack=ExitStack();cls.addClassCleanup(cls.stack.close)
        folder=cls.stack.enter_context(tempfile.TemporaryDirectory())
        cls.stack.enter_context(patch.dict(os.environ,{'VF_RUNTIME_ROOT':folder,'VF_QWEN_MODEL':folder}))
        path=ROOT/'scripts/local_inference/bundle/serve_screenplay_qwen.py'
        spec=importlib.util.spec_from_file_location('_qwen_fixed_literal_cpu_tests',path)
        cls.server=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=cls.server
        spec.loader.exec_module(cls.server)
        cls.server._install_json_literal_grammar()

    def parse(self,schema,value,pretty=False):
        encoded=json.dumps(value,ensure_ascii=False,indent=2 if pretty else None)
        parser=JsonSchemaParser(schema,CharacterLevelParserConfig(alphabet=''.join(set(encoded))))
        for index,character in enumerate(encoded):
            if character not in parser.get_allowed_characters():return False,index
            parser=parser.add_character(character)
        return parser.can_end(),len(encoded)

    def test_fixed_dialogue_quotes_backslashes_controls_and_unicode_are_valid_json(self):
        for text in ['(S1): <d>[English] "The entire original line."</d>',
                     'Original \\ path', 'first\nsecond\tcolumn\rline',
                     '中文“原文”与 "quoted" text', '', ' leading and trailing ']:
            schema={'type':'object','properties':{'text':{'type':'string','const':text}},
                    'required':['text'],'additionalProperties':False}
            for pretty in [False,True]:
                with self.subTest(text=text,pretty=pretty):
                    self.assertTrue(self.parse(schema,{'text':text},pretty)[0])

    def test_each_escaped_enum_choice_is_accepted_and_other_values_are_rejected(self):
        values=['plain','a "quote"','one\\two','two\nlines']
        schema={'type':'object','properties':{'choice':{'type':'string','enum':values}},
                'required':['choice'],'additionalProperties':False}
        before=json.dumps(schema,sort_keys=True)
        for value in values:self.assertTrue(self.parse(schema,{'choice':value})[0])
        self.assertFalse(self.parse(schema,{'choice':'not an allowed value'})[0])
        self.assertEqual(json.dumps(schema,sort_keys=True),before)

    def test_nested_native_patch_keeps_literal_subject_and_complete_dialogue(self):
        line='(S1): <d>[English] "The complete ending stays intact!"</d>'
        schema={'type':'object','properties':{
            'subject':{'type':'string','const':'<Subject 1>'},
            'retention':{'type':'object','properties':{'relationship':{'type':'string','const':'weak_reference'}},
                         'required':['relationship'],'additionalProperties':False},
            'dialogue':{'type':'string','const':line}},
            'required':['subject','retention','dialogue'],'additionalProperties':False}
        value={'subject':'<Subject 1>','retention':{'relationship':'weak_reference'},'dialogue':line}
        self.assertTrue(self.parse(schema,value,True)[0])
        changed={**value,'dialogue':line.replace('ending','altered')}
        self.assertFalse(self.parse(schema,changed)[0])

    def test_installer_is_idempotent_and_normal_string_validation_remains(self):
        import lmformatenforcer.jsonschemaparser as module
        before=module.get_parser
        self.server._install_json_literal_grammar()
        self.assertIs(module.get_parser,before)
        schema={'type':'object','properties':{'text':{'type':'string','minLength':2,'maxLength':5}},
                'required':['text'],'additionalProperties':False}
        self.assertTrue(self.parse(schema,{'text':'valid'})[0])
        self.assertFalse(self.parse(schema,{'text':'x'})[0])
        self.assertFalse(self.parse(schema,{'text':'too long'})[0])


if __name__=='__main__':unittest.main()
