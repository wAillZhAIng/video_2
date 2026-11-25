"""Isolated database/API contracts. These are not real model-generation evidence."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
from threading import Event

from docx import Document
import pytest
from sqlalchemy import event, func, select

from app import models as m
from app.services.cinematic_import import build_ir
from app.services.studio_import import content_fingerprint, language_extract
from app.services.word_duration import parse_shot_duration
from app.services.word_screenplay import parse_blocks, read_blocks


def screenplay(*, same_scene=False, second_weather='雨', extra_line='', name='赵舟', stable='studio_zhaozhou',
               aspect_ratio='16:9', durations=('时长：5秒', '时长：5秒')):
    doc=Document();doc.add_heading('自动化三人剧本',0)
    lines=['项目基本信息','项目名称：自动化夜班','画幅：'+aspect_ratio,'人物设定']
    for n,person in enumerate([name,'周青','罗宁']):
        lines += ['人物 '+person,'人物ID：'+(stable if n==0 else ['','studio_zhouqing','studio_luoning'][n]),
            '年龄：'+str(32+n),'身份：海岸站成年值班工程师','外观：普通成人脸，真实皮肤纹理',
            '眼睛：深褐色杏眼','稳定识别特征：左眼下小痣','体态：肩背端正','眉毛：平直眉',
            '鼻部：鼻梁平直','发际线：自然发际线','固定身份特征：左眼下小痣；深褐色杏眼',
            '声音：低沉清晰','服装：深蓝色棉质工作服']
    lines += ['正文剧情']
    for n in range(2):
        room='控制室' if same_scene or n==0 else '门口雨棚'
        lines += [f'场景 {n+1} {room}','地点：'+room,'时间：夜','天气：'+('雨' if n==0 else second_weather),
            '灯光：低亮度顶灯','环境声：雨水击打金属棚顶',f'分镜 {n+1} 三人核对',
            '出场人物：'+name+'、周青、罗宁','画面：三位成年人围在桌旁，记录本始终合上',
            durations[n],'运镜：固定','表演：'+name+'｜动作=手扶合上的记录本；位置=左侧；情绪=克制',
            '表演：周青｜动作=指向桌上记录本；位置=中间；情绪=专注',
            '表演：罗宁｜动作=看向周青并点头；位置=右侧；情绪=认真',
            '对白：'+name+'：记录在这里。','对白：周青：我来核对。','对白：罗宁：收到。']
    if extra_line:lines.append(extra_line)
    for line in lines:doc.add_paragraph(line)
    output=BytesIO();doc.save(output);return output.getvalue()


def upload(client, data=None, **form):
    return client.post('/api/studio/import',files={'file':('自动化.docx',data or screenplay())},data={
        'auto_start':'false',**{k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in form.items()}})


def upload_new_project_file(client, data):
    return client.post('/api/imports/file', files={'file': ('screenplay.docx', data)},
                       data={'name': '并发导入测试'})


def short_sqlite_wait(client):
    engine = client.app.state.engine

    @event.listens_for(engine, 'checkout')
    def set_timeout(connection, _record, _proxy):
        connection.execute('PRAGMA busy_timeout=20')

    return engine


@pytest.mark.parametrize('lock_mode', ['writer', 'reader'])
def test_project_file_import_retries_a_sqlite_lock_with_one_saved_copy(client, settings, lock_mode):
    engine = short_sqlite_wait(client)
    lock_seen = Event()

    @event.listens_for(engine, 'handle_error')
    def observe_sqlite_error(context):
        code = getattr(context.original_exception, 'sqlite_errorcode', 0)
        if code & 0xff == sqlite3.SQLITE_BUSY:
            lock_seen.set()

    data = screenplay()
    with closing(sqlite3.connect(engine.url.database)) as blocker, ThreadPoolExecutor(1) as pool:
        blocker.execute('BEGIN IMMEDIATE' if lock_mode == 'writer' else 'BEGIN')
        # A rollback-journal reader allows writes to start, but blocks commit.
        if lock_mode == 'reader':
            blocker.execute('SELECT count(*) FROM projects').fetchone()
        request = pool.submit(upload_new_project_file, client, data)
        try:
            assert lock_seen.wait(4), 'SQLite should report a real busy error before the retry'
        finally:
            blocker.rollback()
        response = request.result(timeout=8)
    assert response.status_code == 201, response.text
    imported = response.json()
    original = settings.media_root / imported['source_uri']
    assert original.read_bytes() == data
    assert list((settings.media_root / 'imports').rglob('*.docx')) == [original]
    with client.app.state.session_factory() as db:
        assert list(db.scalars(select(m.Project.id))) == [imported['id']]
        source, = db.scalars(select(m.SourceDocument))
        assert source.source_uri == imported['source_uri']
        receipt, = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'import_file'))
        assert receipt.object_id == imported['id']


@pytest.mark.parametrize('endpoint', ['project_file', 'studio_word'])
def test_import_with_persistent_sqlite_lock_reports_busy_without_a_partial_import(client, settings, endpoint):
    engine = short_sqlite_wait(client)
    with closing(sqlite3.connect(engine.url.database)) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        try:
            response = (upload_new_project_file if endpoint == 'project_file' else upload)(client, screenplay())
        finally:
            blocker.rollback()
    assert response.status_code == 503, response.text
    assert response.json()['error']['code'] == 'import_database_busy'
    assert not list(settings.media_root.rglob('*.docx'))
    with client.app.state.session_factory() as db:
        for model in (m.Project, m.SourceDocument, m.WordImport):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_word_import_does_not_hold_a_write_lock_while_the_language_model_waits(client, monkeypatch):
    from app.services import studio_import

    short_sqlite_wait(client)
    model_waiting = Event()
    resume_model = Event()

    async def slow_extract(*args, **kwargs):
        model_waiting.set()
        assert await asyncio.to_thread(resume_model.wait, 8)
        return None, {'mode': 'controlled_test', 'language_model_called': True}

    monkeypatch.setattr(studio_import, 'language_extract', slow_extract)
    unstructured = Document()
    unstructured.add_paragraph('两位成年人走进商店，放下雨伞后在门口交谈。')
    original = BytesIO()
    unstructured.save(original)
    with ThreadPoolExecutor(1) as pool:
        document = original.getvalue()
        pending = pool.submit(upload, client, document)
        try:
            assert model_waiting.wait(4)
            with client.app.state.session_factory() as db:
                assert db.scalar(select(func.count()).select_from(m.WordImport)) == 0
            current = upload_new_project_file(client, document)
            assert current.status_code == 201, current.text
        finally:
            resume_model.set()
        word = pending.result(timeout=8)
    assert word.status_code == 200 and word.json()['status'] == 'pending_review'


def test_structured_word_saves_existing_project_preview_without_waiting_for_a_model(client, monkeypatch):
    from app.services import studio_import

    async def unexpected_model_call(*args, **kwargs):
        pytest.fail('Explicit shots and preserved production notes should go directly to preview')

    monkeypatch.setattr(studio_import, 'language_extract', unexpected_model_call)
    project = client.post('/api/projects', json={'name': '空白项目'}).json()
    note = '正向视频提示词：角色在窗边交谈。'
    response = upload(client, screenplay(extra_line=note), project_id=project['id'])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'pending_review' and body['parser']['language_model_called'] is False
    assert any(item['text'] == note for item in body['preview']['unrecognized'])

    preview = client.get(body['preview_url']).json()
    saved = client.post(body['preview_url'] + '/confirm', json={
        'revision': preview['revision'], 'duplicate_strategy': 'new_revision', 'project_id': project['id'],
    })
    assert saved.status_code == 200 and saved.json()['project_id'] == project['id'], saved.text
    assert len(saved.json()['shot_ids']) == 2
    assert len(client.get('/api/projects').json()) == 1


def test_word_cast_unifies_numbered_headings_annotated_rosters_and_short_names(client):
    doc = Document()
    for text in (
        '人物设定', '人物 人物甲 林知秋', '年龄：25', '身份：制陶师',
        '角色乙：周向阳', '年龄：28', '身份：店主',
        '正文剧情', '场景 1 工作室', '分镜 1 交谈', '画面：两人在桌旁交谈。',
        '出场人物：林知秋（25岁，制陶师（负责制作、上釉））；周向阳（28岁，店主）',
        '表演：知秋｜动作=放下杯子；情绪=平静\n向阳｜动作=点头；情绪=专注',
        '对白：周向阳：我来看看。\n林知秋：请坐。\n周向阳：谢谢。',
        '分镜 2 点头', '画面：两人看向杯子。',
        '出场人物：林知秋、周向阳（均为成年、虚构人物）', '表演：人物甲｜动作=点头',
        '对白：周向阳（轻声）：很好。', '字幕：待审核', '镜头色卡编号：待审核',
    ):
        doc.add_paragraph(text)
    word = BytesIO()
    doc.save(word)
    response = upload(client, word.getvalue())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'pending_review' and result['parser']['language_model_called'] is False
    preview = result['preview']
    people = {person['name']: person for person in preview['characters']}
    assert {name: (person['profile']['age'], person['profile']['description']) for name, person in people.items()} == {
        '林知秋': (25, '制陶师'), '周向阳': (28, '店主'),
    }
    keys = {name: person['key'] for name, person in people.items()}
    first, second = preview['shots']
    for shot in (first, second):
        assert {person['character_key'] for person in shot['participants']} == set(keys.values())
        assert len(shot['participants']) == 2
    assert {person['character_key']: person['performance']['action'] for person in first['participants']} == {
        keys['林知秋']: '放下杯子', keys['周向阳']: '点头',
    }
    assert [(line['character_key'], line['text']) for line in first['dialogues']] == [
        (keys['周向阳'], '我来看看。'), (keys['林知秋'], '请坐。'), (keys['周向阳'], '谢谢。'),
    ]
    assert second['dialogues'][0]['delivery']['emotion'] == '轻声'
    assert second['participants'][0]['performance']['action'] == '点头'
    confirmed = client.post(result['preview_url'] + '/confirm', json={
        'revision': result['revision'], 'duplicate_strategy': 'new_project',
    })
    assert confirmed.status_code == 200, confirmed.text
    assert len(confirmed.json()['new_characters']) == 2


def test_ambiguous_short_name_and_malformed_cast_stay_in_review():
    lines = [
        '人物设定', '人物甲 林知秋', '人物乙 陈知秋', '正文剧情', '场景 1 工作室',
        '分镜 1 交谈', '画面：两人在桌前。', '出场人物：林知秋（25岁，制陶师、陈知秋',
        '对白：知秋：我来吧。', '对白：人物乙：谢谢。',
    ]
    blocks = [{'id': f'paragraph:{i}', 'kind': 'paragraph', 'text': text} for i, text in enumerate(lines)]
    preview = build_ir(blocks, parse_blocks)
    assert {person['name'] for person in preview['characters']} == {'林知秋', '陈知秋'}
    assert {item['text'] for item in preview['unrecognized']} == {lines[-3], lines[-2]}
    assert len(preview['shots'][0]['dialogues']) == 1
    assert preview['shots'][0]['dialogues'][0]['character_key'] == preview['characters'][1]['key']


def test_word_import_preserves_free_text_prop_directions_and_resolves_a_declared_key():
    note = 'P01从手中转到桌面，未打开。P02水杯在桌角，保持直立。'
    undeclared = '备用物件｜开合状态=关闭'
    doc = Document()
    for text in ('道具 P01 木盒', '正文剧情', '场景 1 桌面', '分镜 1 木盒和水杯',
                 '画面：桌面摆着木盒和水杯。', '道具状态：'+note, '道具状态：'+undeclared,
                 '道具状态：P01｜开合状态=合上', '道具状态：'):
        doc.add_paragraph(text)
    word = BytesIO()
    doc.save(word)
    preview = build_ir(read_blocks(word.getvalue(), 2_000_000), parse_blocks)
    notes = preview['shots'][0]['production']['prop_states']
    assert [{k:v for k,v in state.items() if k != 'source_ids'} for state in notes] == [
        {'description':note}, {'description':undeclared}, {'prop_key':'P01', 'open_state':'合上'},
    ]
    assert all(state['source_ids'] for state in notes)
    assert not any(item['text'] == '道具状态：' for item in preview['unrecognized'])


def test_first_word_import_uses_the_selected_empty_project_without_revision_selection(client):
    project = client.post('/api/projects', json={'name': '空白项目'}).json()
    response = upload(client, project_id=project['id'])
    assert response.status_code == 200 and response.json()['status'] == 'imported', response.text
    assert response.json()['project_id'] == project['id']
    assert len(client.get('/api/projects').json()) == 1
    modified = upload(client, screenplay(second_weather='晴'), project_id=project['id'])
    assert modified.status_code == 409 and modified.json()['error']['code'] == 'new_revision_required'


@pytest.mark.parametrize(('value', 'expected'), [
    ('8', 8), ('大约 8 秒', 8), ('８.５ Ｓ', 8.5), ('0分8.5秒', 8.5), ('1 minute', 60), ('00:08', 8),
    ('00:00–00:08｜8秒', 8), ('00:56—01:04（8 seconds）', 8),
    ('00:00:56,250 --> 00:01:04,750', 8.5), ('0～8秒', 8), ('8s 至 16s｜时长：8s', 8),
])
def test_read_word_duration_formats(value, expected):
    assert parse_shot_duration(value) == expected


@pytest.mark.parametrize('value', [
    '0秒', '-8秒', '90秒', '1.2.3秒', '01:80', '8–12秒', '00:08–00:00',
    '00:56–01:04｜7秒', '00:56–01:04｜待定', '00:00–00:08｜0秒',
])
def test_ambiguous_or_invalid_word_durations_are_not_silently_parsed(value):
    with pytest.raises(ValueError):
        parse_shot_duration(value)


def test_studio_import_keeps_elapsed_seconds_and_source_for_each_shot(client):
    response = upload(client, screenplay(durations=('时长：00:00–00:08｜8秒', '镜头时长（秒）：01:04–01:12｜8秒')))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'imported' and not result['parser']['language_model_called']
    with client.app.state.session_factory() as db:
        for shot_id in result['shot_ids']:
            assert db.get(m.StoryboardShot, shot_id).duration_seconds == 8
            detail = db.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id == shot_id))
            assert detail.source_json['provenance']['duration_seconds']['state'] == 'explicit'


def test_studio_import_understands_duration_columns_in_word_tables(client):
    doc = Document()
    doc.add_heading('时间码表格', 0)
    table = doc.add_table(rows=0, cols=5)
    for row in (
        ('Scene', 'Shot', 'Duration (s)', 'Visual', 'Camera'),
        ('工作室', '桌边', '00:08.000 --> 00:13.400', '窗边的台灯亮起', '固定'),
        ('', '窗边', '00:13.400—00:21.400（8秒）', '窗帘缓慢摆动', '固定'),
    ):
        for cell, value in zip(table.add_row().cells, row):
            cell.text = value
    content = BytesIO()
    doc.save(content)
    response = upload(client, content.getvalue())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'imported'
    with client.app.state.session_factory() as db:
        assert [db.get(m.StoryboardShot, shot).duration_seconds for shot in result['shot_ids']] == [5.4, 8]


def test_conflicting_durations_require_review_and_can_be_corrected(client, monkeypatch):
    from app.services import studio_import

    async def unexpected_model_call(*args, **kwargs):
        pytest.fail('Unresolved durations should go directly to preview')

    monkeypatch.setattr(studio_import, 'language_extract', unexpected_model_call)
    invalid = '00:56–01:04｜7秒'
    response = upload(client, screenplay(durations=('时长：1..2秒', '时长：' + invalid)), auto_start=True)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'pending_review' and not result['pipeline']
    assert any('第 1 镜' in issue for issue in result['issues'])
    assert any('第 2 镜' in issue and invalid in issue for issue in result['issues'])
    with client.app.state.session_factory() as db:
        for model in (m.Project, m.StoryboardShot, m.StudioRun):
            assert db.scalar(select(func.count()).select_from(model)) == 0
    preview = result['preview']
    assert preview['shots'][1]['provenance']['duration_seconds']['source_text'] == invalid
    api = result['preview_url']
    blocked = client.post(api + '/confirm', json={'revision': result['revision'], 'duplicate_strategy': 'new_project'})
    assert blocked.status_code == 422, blocked.text
    for shot in preview['shots']:
        shot['duration_seconds'] = 8
        shot['suggested'].remove('duration_seconds')
    edited = client.put(api + '/preview', json={'revision': result['revision'], 'preview': preview})
    assert edited.status_code == 200 and not edited.json()['issues'], edited.text
    saved = client.post(api + '/confirm', json={'revision': edited.json()['revision'], 'duplicate_strategy': 'new_project'})
    assert saved.status_code == 200 and len(saved.json()['shot_ids']) == 2, saved.text


def test_word_schema_error_returns_fields_without_staging_an_untracked_file(client, settings):
    doc = Document(BytesIO(screenplay()))
    heading = next(p for p in doc.paragraphs if p.text.startswith('分镜 1 '))
    heading.text = '分镜 1 ' + '长' * 241
    content = BytesIO()
    doc.save(content)
    response = upload(client, content.getvalue())
    assert response.status_code == 422, response.text
    error = response.json()['error']
    assert error['code'] == 'word_screenplay_invalid'
    assert ['shots', 0, 'title'] in [field['loc'] for field in error['details']['fields']]
    assert not list((settings.media_root / 'word_imports').glob('*/original.docx'))


def test_one_upload_creates_graph_and_every_explicit_identity_trait(client):
    result=upload(client);assert result.status_code==200,result.text
    result=result.json();assert result['status']=='imported'
    assert len(result['character_ids'])==3 and len(result['scene_ids'])==2 and len(result['shot_ids'])==2
    assert result['parser']['language_model_called'] is False
    with client.app.state.session_factory() as db:
        version=db.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id=='studio_zhaozhou'))
        profile=version.identity_json['profile']
        assert profile['eyes']=='深褐色杏眼' and profile['identifying_marks']=='左眼下小痣'
        assert profile['body']=='肩背端正' and '平直眉' in profile['facial_features']
        assert profile['fixed_traits']==['左眼下小痣','深褐色杏眼']
        assert version.identity_json['source_profile']['voice_profile']=='低沉清晰'
        for shot_id in result['shot_ids']:
            assert db.scalar(select(func.count()).select_from(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot_id))==3
            assert db.scalar(select(func.count()).select_from(m.DialogueLine).where(m.DialogueLine.shot_id==shot_id))==3
            detail=db.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot_id))
            assert detail.scene_id in result['scene_ids']
            for line in db.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id==shot_id)):
                assert db.get(m.ShotCharacter,line.participant_id).shot_id==shot_id


def test_import_defaults_to_preparation_without_creating_a_pipeline(client):
    response=client.post('/api/studio/import',files={'file':('prepare.docx',screenplay())})
    assert response.status_code==200,response.text
    assert response.json()['status']=='imported' and not response.json().get('pipeline')
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(m.StudioRun))==0
        assert db.scalar(select(func.count()).select_from(m.GenerationJob))==0
        assert db.scalar(select(func.count()).select_from(m.MediaProcessTask))==0


@pytest.mark.parametrize('processing',['native','reconstruction'])
def test_import_keeps_unverified_quality_methods_blocked_before_persistence(client,monkeypatch,processing):
    from app.services import super_resolution,video_quality
    monkeypatch.setattr(super_resolution,'status',lambda *_:None)
    monkeypatch.setattr(video_quality,'seedvr_capabilities',lambda *_:{'state':'unavailable','verified_targets':[]})
    response=upload(client,quality_json=json.dumps({'resolution':'720p','mode':'standard',
        'aspect_ratio':'16:9','processing':processing}))
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='quality_method_unverified'
    with client.app.state.session_factory() as db:
        for model in (m.Project,m.WordImport,m.StudioRun,m.GenerationJob,m.QualityRender,m.MediaProcessTask):
            assert db.scalar(select(func.count()).select_from(model))==0


def test_explicit_import_auto_start_checks_voice_preparation_before_pipeline(client,monkeypatch):
    from app.core.errors import AppError
    from app.services import studio_voice
    captured=[]
    def missing_reference(session,settings,project_id,*permissions):
        captured.append(project_id)
        raise AppError(409,'studio_voice_preparation_required','请先配置角色配音')
    monkeypatch.setattr(studio_voice,'assert_voice_preparation',missing_reference)
    response=upload(client,auto_start=True)
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='studio_voice_preparation_required' and len(captured)==1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(m.StudioRun))==0
        assert db.scalar(select(func.count()).select_from(m.GenerationJob))==0
        assert db.scalar(select(func.count()).select_from(m.MediaProcessTask))==0


def test_same_doc_and_same_word_content_reuses_project_without_new_work(client):
    data=screenplay();first=upload(client,data).json();same=upload(client,data).json()
    assert same['reused'] is True and same['project_id']==first['project_id']
    doc=Document(BytesIO(data));doc.core_properties.comments='Changed container metadata only'
    output=BytesIO();doc.save(output)
    assert output.getvalue()!=data
    changed_zip=upload(client,output.getvalue()).json()
    assert changed_zip['reused'] is True and changed_zip['script_id']==first['script_id']
    assert len(client.get('/api/projects').json())==1


def test_explicit_new_revision_preserves_old_graph_and_reuses_characters(client):
    first=upload(client).json()
    next_version=upload(client,screenplay(second_weather='晴'),project_id=first['project_id'],new_revision=True)
    assert next_version.status_code==200,next_version.text
    second=next_version.json();assert second['script_id']!=first['script_id']
    assert second['project_id']==first['project_id'] and second['new_characters']==[]
    assert second['character_ids']==first['character_ids']
    with client.app.state.session_factory() as db:
        assert db.get(m.ScriptRevision,first['script_id']) and db.get(m.StoryboardShot,first['shot_ids'][0])
        assert db.get(m.ScriptRevision,second['script_id']).revision==2
    denied=upload(client,screenplay(second_weather='雪'),project_id=first['project_id'])
    assert denied.status_code==409 and denied.json()['error']['code']=='new_revision_required'


def test_identical_repeated_scene_reuses_scene_and_design_but_weather_change_does_not(client):
    first=upload(client,screenplay(same_scene=True)).json()
    assert first['status']=='imported' and len(first['scene_ids'])==1
    with client.app.state.session_factory() as db:
        details=[db.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot)) for shot in first['shot_ids']]
        assert details[0].scene_id==details[1].scene_id
        assert details[0].source_json['production']['scene_state_id']==details[1].source_json['production']['scene_state_id']
    second=upload(client,screenplay(same_scene=True,second_weather='晴')).json()
    assert second['status']=='imported' and len(second['scene_ids'])==2


def test_unrecognized_prose_is_actionable_exception_not_hidden_success(client):
    result=upload(client,screenplay(extra_line='他低头之后，另外一个人说了一句不知归属的话。')).json()
    assert result['status']=='pending_review' and result['issues'] and result['preview']
    assert result['parser']['language_model_called'] is False
    assert len(client.get('/api/projects').json())==0
    assert client.get(result['preview_url']).status_code==200


def test_same_name_existing_characters_is_not_silently_merged(client):
    for identifier in ['same_one','same_two']:
        r=client.post('/api/character-factory/characters',json={'id':identifier,'name':'同名角色',
            'profile':{'age':32,'description':'成年工程师'}})
        assert r.status_code==201
    # No explicit stable ID means the shared display name cannot be resolved.
    doc=Document(BytesIO(screenplay(name='同名角色')))
    for paragraph in doc.paragraphs:
        if paragraph.text=='人物ID：studio_zhaozhou':paragraph.text=''
    output=BytesIO();doc.save(output)
    result=upload(client,output.getvalue()).json()
    assert result['status']=='pending_review'
    assert len(result['preview']['characters'][0]['candidates'])==2
    assert len(client.get('/api/projects').json())==0


def test_existing_approved_character_identity_version_is_reused(client,settings):
    client.post('/api/character-factory/characters',json={'id':'studio_zhaozhou','name':'赵舟',
        'profile':{'age':32,'description':'成年工程师'}})
    with client.app.state.session_factory() as db:
        version=db.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id=='studio_zhaozhou'))
        character=db.get(m.Character,'studio_zhaozhou');character.current_approved_version_id=version.id
        version.approval_state='approved'
        # Existing approved asset metadata is a contract fixture, not evidence
        # that this test called a portrait model.
        asset=m.CharacterAsset(character_version_id=version.id,asset_type='front_face',relative_path='unit-fixture.png',
            mime_type='image/png',sha256='a'*64,status='approved',is_primary_reference=True,source='unit_fixture')
        db.add(asset);db.flush();version.standard_face_asset_id=asset.id
        db.commit();approved_id=version.id;asset_id=asset.id
    result=upload(client).json();assert result['status']=='imported'
    assert len(result['new_characters'])==2
    with client.app.state.session_factory() as db:
        for shot in result['shot_ids']:
            participant=db.scalar(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot,m.ShotCharacter.character_id=='studio_zhaozhou'))
            assert participant.character_version_id==approved_id
            assert participant.reference_asset_ids_json==[asset_id]
        assert db.get(m.Character,'studio_zhaozhou').current_approved_version_id==approved_id


def test_explicit_auto_start_saves_real_pipeline_record_without_model_call(client):
    project=client.post('/api/projects',json={'name':'明确使用视频原声的自动启动项目'}).json()
    preference=client.put('/api/studio/projects/'+project['id']+'/production-preferences',
        json={'expected_revision':0,'dialogue_audio_policy':'native'})
    assert preference.status_code==200,preference.text
    response=client.post('/api/studio/import',files={'file':('自动化.docx',screenplay())},
        data={'project_id':project['id'],'new_revision':'true','auto_start':'true'})
    assert response.status_code==200,response.text
    result=response.json();assert result['status']=='imported' and result['pipeline_run_id']
    with client.app.state.session_factory() as db:
        run=db.get(m.StudioRun,result['pipeline_run_id'])
        assert run and run.project_id==result['project_id']
        assert run.snapshot_json['options']['dialogue_audio_policy']=='native'
        voice_nodes=[node for node in run.nodes_json if node['kind']=='tts_optional']
        assert voice_nodes and all(node['status']=='skipped' and node['code']=='native_audio' for node in voice_nodes)
    repeated=client.post('/api/studio/import',files={'file':('再次.docx',screenplay())}).json()
    assert repeated['pipeline_run_id']==result['pipeline_run_id'] and repeated['reused']


def test_cloud_language_cost_gate_sends_no_document(client,settings,monkeypatch):
    from app.services import ai_services
    with client.app.state.session_factory() as db:
        service=m.AIService(module='language_processing',provider='openai_compatible',name='Contract only',
            base_url='https://example.invalid/v1',default_model='unverified-model',is_default=True)
        db.add(service);db.commit()
        def should_never_call(*args,**kwargs):raise AssertionError('cloud request forbidden')
        monkeypatch.setattr(ai_services,'service_request',should_never_call)
        extracted,receipt=asyncio.run(language_extract(db,settings,None,read_blocks(screenplay(),settings.import_max_bytes),False))
        assert extracted is None and receipt['language_model_called'] is False


def test_language_contract_rejects_invented_source_evidence(client,settings,monkeypatch):
    """Controlled HTTP contract response, explicitly not a live language test."""
    from app.services import ai_services
    import httpx
    with client.app.state.session_factory() as db:
        db.add(m.AIService(module='language_processing',provider='openai_compatible',name='Contract only',
            base_url='https://example.invalid/v1',default_model='unverified-model',is_default=True));db.commit()
        async def contract_response(*args,**kwargs):
            assert args[3]=='/chat/completions'
            content='{"lines":[{"text":"人物 张三","source_ids":["paragraph:0"],"quote":"原文没有这个人物"}],"uncertainties":[]}'
            return httpx.Response(200,request=httpx.Request('POST','https://example.invalid/v1/chat/completions'),json={
                'choices':[{'message':{'content':content}}]})
        monkeypatch.setattr(ai_services,'service_request',contract_response)
        extracted,receipt=asyncio.run(language_extract(db,settings,None,read_blocks(screenplay(),settings.import_max_bytes),True))
        assert extracted is None and receipt['status']=='failed' and receipt['language_model_called'] is True
        assert '原文证据' in receipt['error']


def test_docx_signature_and_size_are_checked_before_persistence(client):
    response=client.post('/api/studio/import',files={'file':('bad.docx',b'not a real Word file')})
    assert response.status_code in (400,415,422)
    response=client.post('/api/studio/import',files={'file':('old.doc',b'old binary')})
    assert response.status_code==422
    assert response.json()['error']['code']=='invalid_docx'
    assert len(client.get('/api/projects').json())==0


def test_four_timecode_headings_import_locally_with_exact_duration_and_framing(client, monkeypatch):
    from app.services import studio_import

    async def unexpected_model(*args, **kwargs):
        pytest.fail('Explicit time ranges must use the local parser')

    monkeypatch.setattr(studio_import, 'language_extract', unexpected_model)
    doc=Document()
    doc.add_heading('城市短片',0)
    doc.add_heading('物理与画面硬性规范',1)
    doc.add_paragraph('场景设定：夜晚的街口，雨水从雨棚滴落。')
    doc.add_paragraph('技术参数：保持低饱和度。')
    doc.add_heading('分镜时间轴',1)
    for period,action in zip(('00:00-00:02','00:02–00:05','00:05—00:08','00:08-00:10'),
                             ('打开房门','停在雨中','转向街灯','推开雨伞')):
        doc.add_heading(period,2)
        doc.add_paragraph('摄影与构图：竖屏 9:16，固定机位，近景。')
        doc.add_paragraph('视觉与动作：'+action)
        doc.add_paragraph('照明与氛围：暖色路灯。')
    raw=BytesIO();doc.save(raw)
    response=client.post('/api/studio/import',files={'file':('时间轴.docx',raw.getvalue())})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='imported',result.get('issues')
    assert result['parser']['language_model_called'] is False
    with client.app.state.session_factory() as db:
        shots=db.scalars(select(m.StoryboardShot).where(m.StoryboardShot.project_id==result['project_id'])
                        .order_by(m.StoryboardShot.order)).all()
        assert [shot.duration_seconds for shot in shots]==[2,3,3,2]
        assert [shot.aspect_ratio for shot in shots]==['9:16']*4
        assert shots[2].visual_prompt=='转向街灯'
        assert shots[2].lighting=='暖色路灯。'


def test_multiline_subtitles_only_keep_explicit_dialogue_for_tts(client):
    doc=Document()
    doc.add_heading('夜路',0)
    for text in ('人物设定','人物 林夏','人物 周云','正文剧情','场景 1 车站',
                 '分镜 1 等候','画面：两人站在雨棚下。','时长：8秒',
                 '对白：林夏：车来了。\n周云：看到了。',
                 '字幕：林夏：车来了。\n周云：看到了。',
                 '对白：周云：看到了。'):
        doc.add_paragraph(text)
    data=BytesIO();doc.save(data)
    response=client.post('/api/word-imports',files={'file':('字幕.docx',data.getvalue())})
    assert response.status_code==201,response.text
    first=response.json()['preview_json']['shots'][0]
    assert [line['text'] for line in first['dialogues']]==['车来了。','看到了。','看到了。']
    assert first['photography']['subtitle_text']=='林夏：车来了。\n周云：看到了。'
    assert not response.json()['preview_json']['unrecognized']


def test_timed_table_keeps_row_order_and_recovers_interrupted_import(client):
    doc=Document()
    doc.add_heading('拍摄计划',0)
    table=doc.add_table(rows=1,cols=4)
    table.rows[0].cells[0].merge(table.rows[0].cells[3]).text='拍摄时间表'
    for row in (('Start Time','End Time','Visual Description','Camera Movement'),
                ('00:01.250','00:04.125','A door opens.','Push in.'),
                ('00:04.125','00:09','The light comes on.','Static shot.')):
        for cell,value in zip(table.add_row().cells,row):
            cell.text=value
    raw=BytesIO();doc.save(raw)
    created=client.post('/api/word-imports',files={'file':('shots.dotx',raw.getvalue())}).json()
    with client.app.state.session_factory() as db:
        old=db.get(m.WordImport,created['id']);old.status='parsing';db.commit()
    url='/api/word-imports/'+created['id']+'/retry'
    stale=client.post(url,params={'expected_revision':created['revision']-1})
    assert stale.status_code==409
    retry=client.post(url,params={'expected_revision':created['revision']})
    assert retry.status_code==200,retry.text
    parsed=retry.json()
    assert parsed['status']=='pending_review'
    assert parsed['revision']==created['revision']+1
    assert [row['duration_seconds'] for row in parsed['preview_json']['shots']]==[2.875,4.875]
    assert [row['visual_prompt'] for row in parsed['preview_json']['shots']]==['A door opens.','The light comes on.']


def test_slow_word_semantics_returns_original_text_and_review_preview(client, monkeypatch):
    from app.services import ai_services, studio_import

    with client.app.state.session_factory() as db:
        db.add(m.AIService(module='language_processing',provider='local_llm',name='Local contract',
                          base_url='http://127.0.0.1:8772/v1',default_model='local-contract',is_default=True))
        db.commit()

    async def slow_model(*args,**kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(ai_services,'service_request',slow_model)
    monkeypatch.setattr(studio_import,'WORD_IMPORT_MODEL_WAIT_SECONDS',.02)
    doc=Document()
    doc.add_paragraph('雨落在屋檐上。')
    doc.add_paragraph('门缓缓打开。')
    raw=BytesIO();doc.save(raw)
    result=upload(client,raw.getvalue()).json()
    assert result['status']=='pending_review'
    assert result['parser']['status']=='timed_out'
    assert result['parser']['language_model_called'] is True
    assert result['preview']['shots'][0]['visual_prompt']=='雨落在屋檐上。\n门缓缓打开。'


def test_editing_one_shot_reuses_other_shot_and_preserves_video_record(client):
    """Metadata regression only; no fabricated clip is used as inference evidence."""
    first=upload(client).json();unchanged_id=first['shot_ids'][0]
    with client.app.state.session_factory() as db:
        job=m.GenerationJob(project_id=first['project_id'],shot_id=unchanged_id,provider='unit-contract-only',
            status='generated',prompt='database preservation contract')
        db.add(job);db.flush()
        video=m.VideoRevision(project_id=first['project_id'],shot_id=unchanged_id,generation_job_id=job.id,
            relative_path='unit-contract-only/not-an-inference-result.mp4',revision=1,review_status='approved')
        db.add(video);db.commit();video_id=video.id
    doc=Document(BytesIO(screenplay()))
    matches=[p for p in doc.paragraphs if p.text=='对白：周青：我来核对。']
    matches[-1].text='对白：周青：现在可以核对。'
    output=BytesIO();doc.save(output)
    second=upload(client,output.getvalue(),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported'
    assert second['shot_ids'][0]==unchanged_id and second['shot_ids'][1]!=first['shot_ids'][1]
    assert second['reused_shot_ids']==[unchanged_id] and second['changed_shot_ids']==[second['shot_ids'][1]]
    current=client.get('/api/projects/'+first['project_id']+'/shots').json()
    assert [s['id'] for s in current]==second['shot_ids']
    history=client.get('/api/projects/'+first['project_id']+'/shots?include_history=true').json()
    assert len(history)==3
    with client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,video_id).shot_id==unchanged_id
        assert db.get(m.VideoRevision,video_id).review_status=='approved'
        original_scene=db.get(m.Scene,first['scene_ids'][0])
        assert original_scene.script_revision_id==first['script_id']
        assert db.get(m.ScriptRevision,second['script_id']).parsed_json['explicit_shot_ids']==second['shot_ids']


def test_unchanged_new_revision_reuses_all_members_and_editor_changes_are_not_reused(client):
    first=upload(client).json()
    second=upload(client,project_id=first['project_id'],new_revision=True).json()
    assert second['shot_ids']==first['shot_ids'] and second['scene_ids']==first['scene_ids']
    assert second['changed_shot_ids']==[]
    assert [s['id'] for s in client.get('/api/projects/'+first['project_id']+'/shots').json()]==first['shot_ids']
    with client.app.state.session_factory() as db:
        edited=db.get(m.StoryboardShot,first['shot_ids'][0]);edited.visual_prompt='手工修改过画面，不得静默套回旧原文'
        db.commit()
    third=upload(client,project_id=first['project_id'],new_revision=True).json()
    assert third['shot_ids'][0]!=first['shot_ids'][0] and third['shot_ids'][1]==first['shot_ids'][1]


def test_advanced_add_duplicate_and_delete_maintain_imported_revision_members(client):
    imported=upload(client).json();pid=imported['project_id']
    added=client.post('/api/projects/'+pid+'/shots',json={'order':3,'scene_title':'手工补充的镜头','duration_seconds':5})
    assert added.status_code==201,added.text
    copied=client.post('/api/shots/'+imported['shot_ids'][0]+'/duplicate')
    assert copied.status_code==201,copied.text
    expected=set([*imported['shot_ids'],added.json()['id'],copied.json()['id']])
    assert {s['id'] for s in client.get('/api/studio/projects/'+pid).json()['shots']}==expected
    removed=client.delete('/api/shots/'+added.json()['id'])
    assert removed.status_code==200,removed.text
    expected.remove(added.json()['id'])
    assert {s['id'] for s in client.get('/api/studio/projects/'+pid).json()['shots']}==expected
    with client.app.state.session_factory() as db:
        from app.api.routes import project_shot_statement
        assert added.json()['id'] in {s.id for s in db.scalars(project_shot_statement(db,pid,include_history=True))}
        assert db.get(m.StoryboardShot,added.json()['id']).status=='deleted'


def test_legacy_manual_add_remains_visible_and_deleted_import_shot_is_not_reused(client):
    legacy=client.post('/api/projects',json={'name':'旧版手工项目'}).json()
    manual=client.post('/api/projects/'+legacy['id']+'/shots',json={'order':1,'scene_title':'旧版入口'}).json()
    assert [s['id'] for s in client.get('/api/studio/projects/'+legacy['id']).json()['shots']]==[manual['id']]
    imported=upload(client).json();old=imported['shot_ids'][0]
    assert client.delete('/api/shots/'+old).status_code==200
    revised=upload(client,project_id=imported['project_id'],new_revision=True).json()
    assert revised['shot_ids'][0]!=old and revised['shot_ids'][1]==imported['shot_ids'][1]
    assert len(client.get('/api/studio/projects/'+imported['project_id']).json()['shots'])==2


def rearranged_screenplay(*, sequence=(0,1), insert=False, duplicate_scene=False, changed_weather=False):
    """A real DOCX with numbering changes only; identities/content stay stable."""
    original=Document(BytesIO(screenplay()))
    texts=[p.text for p in original.paragraphs]
    starts=[i for i,text in enumerate(texts) if text.startswith('场景 ')]
    prefix=texts[1:starts[0]]
    blocks=[texts[starts[n]:starts[n+1] if n+1<len(starts) else len(texts)] for n in range(len(starts))]
    if duplicate_scene:
        blocks[1]=[line.replace('门口雨棚','控制室') for line in blocks[1]]
    if changed_weather:blocks[0]=['天气：晴' if line=='天气：雨' else line for line in blocks[0]]
    selected=[list(blocks[n]) for n in sequence]
    if insert:
        extra=[line.replace('控制室','海堤步道').replace('三人核对','步道核对') for line in blocks[0]]
        selected.insert(0,extra)
    result=Document();result.add_heading(texts[0],0)
    for line in prefix:result.add_paragraph(line)
    import re
    for n,block in enumerate(selected,1):
        for line in block:
            line=re.sub(r'^场景\s+\d+',f'场景 {n}',line)
            line=re.sub(r'^分镜\s+\d+',f'分镜 {n}',line)
            result.add_paragraph(line)
    output=BytesIO();result.save(output);return output.getvalue()


def test_inserting_independent_scene_reuses_shifted_original_shots(client):
    first=upload(client).json()
    second=upload(client,rearranged_screenplay(insert=True),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids'][1:]==first['shot_ids']
    assert second['reused_shot_ids']==first['shot_ids'] and len(second['changed_shot_ids'])==1
    assert second['revision_diff']['order_changed'] is True
    overview=client.get('/api/studio/projects/'+first['project_id']).json()
    assert [s['id'] for s in overview['shots']]==second['shot_ids']
    assert [s['order'] for s in overview['shots']]==[1,2,3]
    assert [s['id'] for s in client.get('/api/projects/'+first['project_id']+'/shots').json()]==second['shot_ids']


def test_deleting_independent_scene_reuses_surviving_shifted_shot(client):
    first=upload(client).json()
    second=upload(client,rearranged_screenplay(sequence=(1,)),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids']==[first['shot_ids'][1]] and second['changed_shot_ids']==[]
    assert second['revision_diff']['removed_members']==[first['shot_ids'][0]]
    overview=client.get('/api/studio/projects/'+first['project_id']).json()
    assert [s['order'] for s in overview['shots']]==[1]
    with client.app.state.session_factory() as db:
        assert db.get(m.StoryboardShot,first['shot_ids'][0])


def test_word_reordering_preserves_review_and_records_new_default_timeline(client):
    first=upload(client).json();original_snapshot={'order':first['shot_ids'],'source':'unit database snapshot, not a render'}
    with client.app.state.session_factory() as db:
        for n,identifier in enumerate(first['shot_ids'],1):
            db.add(m.StudioDecision(project_id=first['project_id'],shot_id=identifier,action='exclude',position=n,
                trim_start=.1,trim_end=1.2,instruction='保留已有审片意见'))
        assembly=m.Assembly(project_id=first['project_id'],name='Historical unit timeline',timeline_json=original_snapshot)
        db.add(assembly);db.flush()
        render=m.AssemblyRender(assembly_id=assembly.id,version=1,kind='render',status='failed',snapshot_json=original_snapshot)
        db.add(render);db.commit();render_id=render.id
    second=upload(client,rearranged_screenplay(sequence=(1,0)),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids']==list(reversed(first['shot_ids'])) and second['changed_shot_ids']==[]
    assert second['revision_diff']['order_changed'] and second['revision_diff']['timeline_reordered']
    overview=client.get('/api/studio/projects/'+first['project_id']).json()
    assert [s['id'] for s in overview['shots']]==second['shot_ids']
    assert [s['order'] for s in overview['shots']]==[1,2]
    advanced=client.get('/api/projects/'+first['project_id']+'/shots').json()
    assert [s['order'] for s in advanced]==[1,2] and [s['source_order'] for s in advanced]==[2,1]
    assert [s['decision']['position'] for s in overview['shots']]==[1,2]
    for shot in overview['shots']:
        assert shot['decision']['action']=='exclude' and shot['decision']['revision']==2
        assert shot['decision']['trim_start']==.1 and shot['decision']['trim_end']==1.2
    with client.app.state.session_factory() as db:
        audits=list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action=='word_revision_order_applied')))
        assert len(audits)==2
        assert db.get(m.AssemblyRender,render_id).snapshot_json==original_snapshot
        assert db.get(m.StoryboardShot,first['shot_ids'][0]).order==1


def test_identical_semantic_shots_are_not_ambiguously_matched(client):
    source=rearranged_screenplay(duplicate_scene=True)
    first=upload(client,source).json()
    second=upload(client,source,project_id=first['project_id'],new_revision=True).json()
    assert first['status']=='imported' and second['status']=='imported'
    assert not set(first['shot_ids'])&set(second['shot_ids'])
    assert second['reused_shot_ids']==[]


def test_independent_scene_environment_change_does_not_invalidate_other_scene(client):
    first=upload(client).json()
    second=upload(client,rearranged_screenplay(changed_weather=True),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids'][0]!=first['shot_ids'][0] and second['shot_ids'][1]==first['shot_ids'][1]
    assert second['revision_diff']['order_changed'] is False


def test_include_deleted_flag_preserves_legacy_and_explicit_revision_members(client):
    imported=upload(client).json();pid=imported['project_id'];deleted=imported['shot_ids'][0]
    assert client.delete('/api/shots/'+deleted).status_code==200
    assert deleted not in {s['id'] for s in client.get('/api/projects/'+pid+'/shots').json()}
    assert deleted in {s['id'] for s in client.get('/api/projects/'+pid+'/shots?include_deleted=true').json()}
    assert deleted not in {s['id'] for s in client.get('/api/studio/projects/'+pid).json()['shots']}
