"""Keep source CI aligned with the shipped native H3 runtime."""
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ci_python_entrypoints_exist():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    for job in workflow['jobs'].values():
        for step in job.get('steps', []):
            for entrypoint in re.findall(r'python (?!-)([\w/.-]+\.py)', step.get('run', '')):
                assert (ROOT / entrypoint).is_file(), entrypoint


def test_ci_uses_shipped_native_tests_without_removed_compiler():
    source = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    assert 'python -m pytest -rs tests/backend' in source
    for removed in ('open-h3-ir', 'context-ir-native-v1.json', 'upstream-compiler-verification.json',
                    'test_native_runtime.py', 'test_h3_revision_integrity.py'):
        assert removed not in source


def test_one_off_scripts_and_placeholder_workflow_are_absent():
    for removed in ('.pytest-tmp-context-ir-replay.py', 'generate_direct.py',
                    'workflows/h3_workflow.example.json',
                    'scripts/local_inference/requirements-qwen-baseline.txt'):
        assert not (ROOT / removed).exists()
    for quality in ('fast4', 'quality20', 'quality30'):
        assert (ROOT / f'workflows/h3_ref2va_native_{quality}_v2.json').is_file()
