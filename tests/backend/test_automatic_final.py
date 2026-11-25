from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.services.automatic_final import export_target, timeline_parameters, preserve_native_speech


def test_word_export_uses_vertical_resolution_and_the_screenplay_clock():
    shots = [SimpleNamespace(id=f's{i}', aspect_ratio='9:16', duration_seconds=duration)
             for i, duration in enumerate([2, 3, 3, 3])]
    script = SimpleNamespace(parsed_json={'project': {'aspect_ratio': '9:16',
        'constraints': '保留实拍质感\n技术参数：4K分辨率、60fps、全片11秒。'}})
    videos = [SimpleNamespace(id=f'v{i}', shot_id=shot.id) for i, shot in enumerate(shots)]
    probes = [{'duration': 4.0, 'fps': 24} for _ in shots]
    clips, transitions, output = timeline_parameters(export_target(script, shots), videos, probes)
    assert [clip['video_id'] for clip in clips] == ['v0', 'v1', 'v2', 'v3']
    assert [clip['trim_end'] for clip in clips] == [2, 3, 3, 3]
    assert [edge['type'] for edge in transitions] == ['cut', 'cut', 'cut']
    assert (output['width'], output['height'], output['fps']) == (2160, 3840, 60)
    assert output['audio_policy'] == 'preserve' and not output['subtitles']
    probes[2]['duration'] = 2.7
    with pytest.raises(AppError) as error:
        timeline_parameters(export_target(script, shots), videos, probes)
    assert error.value.code == 'automatic_final_short_clip'


def test_requested_overlap_uses_only_real_tail_to_keep_the_total_duration():
    shots = [SimpleNamespace(id=f's{i}', aspect_ratio='16:9', duration_seconds=2) for i in range(2)]
    videos = [SimpleNamespace(id=f'v{i}', shot_id=shot.id) for i, shot in enumerate(shots)]
    target = export_target(None, shots, ['交叉淡化 0.5 秒'])
    probes = [{'duration': 3, 'fps': 24}, {'duration': 2, 'fps': 24}]
    clips, transitions, _ = timeline_parameters(target, videos, probes)
    assert clips[0]['trim_end'] == 2.5 and transitions[0]['type'] == 'crossfade'
    assert sum(clip['trim_end'] for clip in clips) - transitions[0]['duration'] == 4
    probes[0]['duration'] = 2
    clips, transitions, _ = timeline_parameters(target, videos, probes)
    assert clips[0]['trim_end'] == 2 and transitions[0]['type'] == 'cut'


@pytest.mark.parametrize('speech', [
    {'dialogues': [{'text': 'Click the link to shop!'}]},
    {'audio_events': [{'kind': 'voice', 'description': 'Christmas surprise!'}]},
])
def test_native_speech_preserves_complete_legal_h3_take_without_mutating_snapshot(speech):
    from copy import deepcopy
    shot=SimpleNamespace(id='s1',aspect_ratio='9:16',duration_seconds=4)
    target=export_target(None,[shot]);before=deepcopy(target)
    context={'s1':{'inputs':{'audio_policy':'native','screenplay_snapshot':speech}}}
    original=deepcopy(context)
    derived=preserve_native_speech(target,context)
    clips,transitions,output=timeline_parameters(derived,[SimpleNamespace(id='v1',shot_id='s1')],
        [{'fps':24,'duration':107/24}])
    assert clips==[{'video_id':'v1','trim_start':0,'trim_end':None}]
    assert derived['duration_policy']=='preserve_complete_native_speech_takes'
    assert derived['requested_storyboard_duration']==4
    assert derived['preserve_native_speech_shots']==['s1']
    assert context==original and target==before
    assert output['audio_policy']=='preserve' and transitions==[]


@pytest.mark.parametrize('policy,speech', [
    ('native',{}),('native',{'dialogues':[{'text':' '}]}),
    ('tts',{'dialogues':[{'text':'Line handled separately.'}]}),
    ('mute',{'dialogues':[{'text':'Silenced by the caller.'}]}),
    ('native',{'audio_events':[{'kind':'music','description':'Instrumental music.'}]}),
])
def test_non_speech_and_non_native_policies_keep_the_storyboard_clock(policy,speech):
    shot=SimpleNamespace(id='s1',aspect_ratio='9:16',duration_seconds=4)
    target=export_target(None,[shot])
    derived=preserve_native_speech(target,{'s1':{'inputs':{'audio_policy':policy,'screenplay_snapshot':speech}}})
    clips,_,_=timeline_parameters(derived,[SimpleNamespace(id='v1',shot_id='s1')],[{'fps':24,'duration':107/24}])
    assert clips[0]['trim_end']==4 and derived['duration_policy']=='use_storyboard_duration'


def test_mixed_native_speech_and_music_transitions_keep_all_speech_frames():
    shots=[SimpleNamespace(id=s,aspect_ratio='16:9',duration_seconds=4) for s in ['spoken','music']]
    target=preserve_native_speech(export_target(None,shots,['crossfade 0.25 s']),
        {'spoken':{'inputs':{'audio_policy':'native','screenplay_snapshot':{'dialogues':[{'text':'End word.'}]}}}})
    videos=[SimpleNamespace(id=s.id,shot_id=s.id) for s in shots]
    clips,edges,_=timeline_parameters(target,videos,[{'fps':24,'duration':107/24}]*2)
    assert clips[0]['trim_end'] is None and clips[1]['trim_end']==4
    assert edges[0]['type']=='crossfade' and edges[0]['duration']==.25
    # Complete speech does not authorize a source that is shorter than the script.
    with pytest.raises(AppError) as error:
        timeline_parameters(target,videos,[{'fps':24,'duration':3.8}]*2)
    assert error.value.code=='automatic_final_short_clip'
