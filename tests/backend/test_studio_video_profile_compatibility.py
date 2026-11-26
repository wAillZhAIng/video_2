"""Native MiniMax H3 quality-tier contracts without auxiliary compilers."""
from types import SimpleNamespace

import pytest

from app.schemas_quality import QualitySettings
from app.services.studio_pipeline import generation_quality_parameters


@pytest.mark.parametrize(('mode', 'expected_steps'), [
    ('fast', 4),
    ('standard', 20),
    ('high', 30),
])
def test_native_h3_quality_tiers_preserve_distinct_sampler_contracts(mode, expected_steps):
    shot = SimpleNamespace(id=f'native-{mode}')
    quality = QualitySettings(
        mode=mode,
        resolution='4k',
        aspect_ratio='9:16',
        processing='ai_super_resolution',
    ).model_dump()
    run = SimpleNamespace(snapshot_json={'options': {'quality_by_shot': {shot.id: quality}}})

    actual = generation_quality_parameters(run, shot)

    assert actual['steps'] == expected_steps
    assert actual['quality_request'] == quality
