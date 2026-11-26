"""Caller framing must override only the frozen generation copy, without models."""
from copy import deepcopy

import pytest
from app.services.revision_cinematography import apply_cinematography_override, explicit_cinematography


@pytest.mark.parametrize('instruction,key',[
    ('固定中近景，手掌与脸同时入镜。','medium_close_up'),
    ('本次景别固定中景，画面覆盖人物腰部以上及完整脸部。','medium'),
    ('改为微距超级特写。','macro'),
    ('景别改成超级特写。','extreme_close_up'),
    ('Use a medium close-up.','medium_close_up'),
    ('Use a medium shot.','medium'),
    ('从特写改为中景。','medium'),
    ('不要特写，改成中景。','medium'),
    ('Change the framing from close-up to medium shot.','medium'),
    ('Do not use a macro shot. Use a medium shot.','medium'),
    ('全景拍摄。','full'),
])
def test_explicit_shot_size_keeps_close_up_and_medium_distinct(instruction,key):
    result=explicit_cinematography(instruction)
    assert result['shot_size']==key
    assert result['source']=='instruction' and result['source_text']


@pytest.mark.parametrize('instruction',[
    None,'','P02只有水杯大小。','不要中景。','不要改为中景。',
    '中景或特写都可以。','中近景／中景二选一。','Use either a medium shot or a close-up.',
    '不要使用固定中景。','Do not use a medium shot.','不要特写，保持原镜头。',
    '对白：“改为中景。”，产品不变。','产品图片中近景标注保持不变。',
    '中景。特写。',
    '机位不固定。','不采用中景。','不切换为中景。',
])
def test_unknown_negative_ambiguous_or_content_is_not_a_camera_override(instruction):
    assert explicit_cinematography(instruction)=={}


def test_camera_position_retains_direction_and_lens_while_only_framing_changes():
    photo={'shot_size':'微距超级特写（Macro Shot）','framing':'product close-up',
           'camera_position':'正面固定近景，略高机位','lens':'35 mm','lighting':'暖光'}
    before=deepcopy(photo)
    move,effective,audit=apply_cinematography_override('缓慢前推',photo,'改成中近景。')
    assert move=='缓慢前推'
    assert effective['shot_size']=='中近景（Medium Close-Up）'
    assert effective['framing']=='product Medium Close-Up'
    assert effective['camera_position']=='正面固定中近景，略高机位'
    assert effective['lens']=='35 mm' and effective['lighting']=='暖光'
    assert photo==before and audit['before']['photography']==before
    assert audit['after']['photography']==effective and audit['changed']
    assert apply_cinematography_override(move,effective,'改成中近景。')[:2]==(move,effective)
    effective['lens']='changed copy'
    assert audit['after']['photography']['lens']=='35 mm' and photo==before


def test_explicit_static_camera_changes_motion_without_guessing_a_new_angle():
    photo={'camera_position':'正面略高机位','shot_size':'特写'}
    move,effective,audit=apply_cinematography_override('缓慢左移',photo,'机位固定。')
    assert move=='固定（holds static）' and effective==photo
    assert audit['camera_move']=='固定（holds static）'
    assert photo=={'camera_position':'正面略高机位','shot_size':'特写'}
