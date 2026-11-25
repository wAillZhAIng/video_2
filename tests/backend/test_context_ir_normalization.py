from __future__ import annotations

import pytest

from app.services.context_ir_normalization import VERSION, normalize_compiler_input
from app.services.h3_context_provider import context_compiler_intent, prepare_context_compilation


def test_out_of_clip_shot_timecode_is_a_source_locator_and_not_a_generated_cut():
    source = (
        '镜头：00:08-00:11\n'
        '机位与运镜：第一人称 POV 视角，微幅微晃自然减弱并静止收尾。\n'
        '全片外观一致性：四段镜头使用同一张实物参考图。推近只改变画面中的大小。\n'
        '禁止项：严禁字幕、非指定商品和正面人脸。'
    )
    result = normalize_compiler_input(
        source, duration_seconds=3, shot_count=1,
        camera_sentence='The camera shakes slightly.',
    )

    assert '镜头：00:08-00:11' not in result['prompt']
    assert '原稿镜头位置（仅来源定位，非本次输出切点' in result['prompt']
    assert result['report']['version'] == VERSION
    assert result['report']['source_timecodes'] == [{
        'line': 1, 'label': '镜头', 'raw_range': '00:08-00:11',
        'start_seconds': 8.0, 'end_seconds': 11.0,
        'meaning': 'source_document_locator_not_output_timeline',
    }]
    assert result['report']['original_prompt_sha256']
    assert result['report']['normalized_prompt_sha256']
    assert 'exactly one selected output shot' in result['directives']
    assert 'The typed output duration is exactly 3 seconds' in result['directives']
    assert 'The only camera movement in the single shot is exactly: The camera shakes slightly.' in result['directives']
    assert 'Do not add another camera movement or cut based on action, product-scale, or continuity wording' in result['directives']
    # Non-timing prose and every explicit exclusion survive the compile view.
    assert '四段镜头使用同一张实物参考图' in result['prompt']
    assert '推近只改变画面中的大小' in result['prompt']
    assert '严禁字幕、非指定商品和正面人脸' in result['prompt']


def test_in_clip_ranges_and_action_timing_are_not_rewritten():
    source = '镜头：00:00-00:03\n动作节拍：00:01-00:02，手抬起杯子'
    result = normalize_compiler_input(source, duration_seconds=3, shot_count=1)
    assert result['prompt'] == source
    assert result['report']['source_timecodes'] == []
    assert result['report']['changes'] == []


def test_explicit_semantic_headings_keep_distinct_typed_categories():
    source = '\n'.join([
        '人物：林岚',
        '外观：短发，深色外套',
        '动作：把杯子放到桌上',
        '机位与运镜：镜头固定',
        '景别：中景',
        '拍摄角度：平视',
        '场景：厨房',
        '灯光：柔和窗光',
        '对白：我来核对',
        '禁止项：不要字幕',
        '参考图：picture1',
        '输出规格：9:16，4 秒',
    ])
    result = normalize_compiler_input(source, duration_seconds=4)
    fields = {item['field'] for item in result['report']['semantic_fields']}
    assert fields == {
        'subject', 'appearance', 'action', 'camera', 'framing', 'angle',
        'scene', 'lighting', 'audio', 'negative_constraints', 'references', 'output',
    }
    assert '林岚' in result['prompt'] and '不要字幕' in result['prompt']
    assert all('value' not in item for item in result['report']['semantic_fields'])


def test_context_compilation_applies_duration_and_source_locator_normalization():
    prompt = (
        '左手握住同一只杯子。\n'
        '全片外观一致性：四段镜头使用同一张实物参考图。\n'
        '镜头：00:08-00:11\n'
        '机位与运镜：手持手机微晃，尾声自然减弱并静止收尾。'
    )
    _, _, intent, contract = prepare_context_compilation(
        prompt, '', [], [], {'duration_seconds': 3.0},
    )
    assert '原稿镜头位置（仅来源定位，非本次输出切点' in intent
    assert 'exactly one selected output shot' in intent
    assert 'typed output duration is exactly 3 seconds' in intent
    assert contract['compiler_input_normalization']['duration_seconds'] == 3.0
    assert contract['compiler_input_normalization']['shot_count'] == 1
    assert contract['compiler_input_normalization']['source_timecodes'][0]['raw_range'] == '00:08-00:11'
    assert contract['camera_sentence'] == 'The camera shakes slightly.'


@pytest.mark.parametrize('duration_seconds', [1, 3, 4.5, 12, 30])
def test_timecode_normalization_is_idempotent_for_render_duration(duration_seconds):
    source = f'镜头：00:00-00:{duration_seconds:02.0f}\n动作：连续一个动作，不剪切'
    first = normalize_compiler_input(source, duration_seconds=duration_seconds)
    second = normalize_compiler_input(first['prompt'], duration_seconds=duration_seconds)
    assert second['prompt'] == first['prompt']
    assert first['report']['shot_count'] == 1


def test_out_of_clip_timeline_rewrite_preserves_same_line_content():
    source = '镜头：00:08-00:11，人物走出门并回头，商品保持不变，禁止字幕'
    result = normalize_compiler_input(source, duration_seconds=3, shot_count=1)

    assert '00:08-00:11' not in result['prompt']
    assert '人物走出门并回头' in result['prompt']
    assert '商品保持不变' in result['prompt']
    assert '禁止字幕' in result['prompt']
    assert result['report']['source_timecodes'][0]['raw_range'] == '00:08-00:11'
    assert result['report']['changes'][0]['change'] == 'relabel_out_of_clip_timecode_as_source_locator'



def test_single_shot_contract_uses_one_exact_marker_without_wildcard_examples():
    source = (
        'Continuity: the same product reference is used across four source shots.\n'
        'Action: hold the product, move it closer, and settle.'
    )
    result = normalize_compiler_input(source, duration_seconds=3, shot_count=1)

    assert 'exactly one selected output shot' in result['directives']
    assert 'correctly numbered shot heading for shot 1' in result['directives']
    assert 'Do not emit any additional numbered shot heading' in result['directives']
    assert not __import__('re').search(r'\[Shot\s+\d+\]', result['directives'])
    assert result['prompt'] == source


def test_multi_shot_contract_names_the_exact_ordered_markers():
    result = normalize_compiler_input(
        'Subject: same cup.', duration_seconds=6, shot_count=3,
    )

    assert 'exactly 3 selected output shots' in result['directives']
    assert 'exactly 3 numbered shot blocks' in result['directives']
    assert 'contiguous numbering in ascending order starting at 1' in result['directives']
    assert not __import__('re').search(r'\[Shot\s+\d+\]', result['directives'])


def test_explicit_camera_sentence_suppresses_camera_menu_and_extra_movement():
    intent = context_compiler_intent(
        'Camera: first-person view, slight shake that settles.\n'
        'Product note: pushing closer changes only apparent size.',
        '', [], duration_seconds=3, shot_count=1,
        camera_sentence='The camera shakes slightly.',
    )

    assert 'The only camera movement in the single shot is exactly: The camera shakes slightly.' in intent
    assert 'pushes in, pulls out' not in intent
    assert 'Product note: pushing closer changes only apparent size.' in intent
    assert 'Do not add a second camera movement or cut' in intent


def test_context_compiler_without_frozen_camera_does_not_inject_camera_menu():
    prompt = 'Action: product moves closer to camera; hand gently adjusts grip.'
    intent = context_compiler_intent(
        prompt, '', [], duration_seconds=3, shot_count=1,
    )

    assert 'pushes in, pulls out' not in intent
    assert 'Use only camera movement requested by the caller' in intent
    assert prompt in intent


def test_app_generated_context_instructions_do_not_embed_shot_header_tokens():
    prompt = 'Action: keep the product steady while the hand settles.'
    intent = context_compiler_intent(
        prompt, '', ['Preserve the source framing.'], duration_seconds=3, shot_count=1,
    )

    # OpenH3-IR 0.4.1 scans the whole draft body for these tokens. App-generated
    # format examples must describe the count without embedding a second literal header.
    assert not __import__('re').search(r'\[Shot\s+\d+\]', intent)
    assert prompt in intent
    assert 'exactly one correctly numbered shot-one heading' in intent
