from io import BytesIO
from docx import Document
import pytest

from app.services.cinematic_import import build_ir
from app.services.word_screenplay import parse_blocks, read_blocks
from app.services.word_duration import parse_shot_duration


def _make_docx():
    document = Document()
    document.add_heading('脚本21：褐石公寓晨读时光', 0)
    document.add_heading('物理与硬性规范', 1)
    document.add_paragraph('产品形态与比例：32 oz 保温杯，高度 27 cm；杯身纹样、颜色和品牌标识保持一致。')
    document.add_paragraph('严禁将商品缩小为 24 oz，严禁改变杯盖结构。')
    document.add_heading('场景设定', 1)
    document.add_paragraph('空间环境：波士顿褐石公寓二楼书房，胡桃木书桌临窗摆放。')
    document.add_paragraph('氛围基调：安静温润的晚秋晨光。')
    document.add_heading('人物设定', 1)
    document.add_paragraph('出镜人物：仅出现男性右手与手腕，不出现面部。')
    document.add_paragraph('声音特质：低沉、温和的地道美式英语男声。')
    document.add_heading('技术参数', 1)
    document.add_paragraph('画幅与设备：9:16 竖屏智能手机 Vlog 视角，模拟 26mm 主摄。')
    document.add_heading('禁止项', 1)
    document.add_paragraph('严禁出现字幕、贴纸或水印。')
    document.add_heading('逐秒时间线提示词（00:00-00:15）', 1)
    for start, end, visual, audio in [
        ('00:00', '00:03', '水杯稳立在胡桃木桌面，男性右手伸向杯身。', '木桌轻微摩擦声；英文口播：“The cup feels solid.” 中文翻译：“杯子手感扎实。”'),
        ('00:03', '00:07', '男性右手握住杯身并平稳拿起。', '指腹摩擦声；英文口播：“It fits my morning.” 中文翻译：“它适合我的早晨。”'),
        ('00:07', '00:11', '近景展示杯盖和杯身纹样。', '室内安静底噪；英文口播：“The lid stays secure.” 中文翻译：“杯盖稳固。”'),
        ('00:11', '00:15', '水杯放回桌面，手部退出画面。', '杯底轻触桌面；英文口播：“Ready for every day.” 中文翻译：“每天都能使用。”'),
    ]:
        document.add_heading(f'【{start}-{end}】', 2)
        document.add_paragraph('摄影与构图：竖屏手机 Vlog 视角，中近景固定微晃机位。')
        document.add_paragraph('视觉与动作：' + visual)
        document.add_paragraph('照明与氛围：柔和窗边自然光。')
        document.add_paragraph('音频与口播：' + audio)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def test_timeline_marketing_word_is_fully_preserved_for_automation():
    blocks = read_blocks(_make_docx(), 5_000_000, 'timeline-marketing.docx')
    ir = build_ir(blocks, parse_blocks)

    assert ir['project']['aspect_ratio'] == '9:16'
    assert len(ir['scenes']) == 1
    assert '波士顿褐石公寓' in str(ir['scenes'][0]['environment'])
    assert '32 oz' in ir['project']['constraints']
    assert '24 oz' in ir['project']['constraints']
    assert len(ir['shots']) == 4
    assert [shot['duration_seconds'] for shot in ir['shots']] == [3.0, 4.0, 4.0, 4.0]
    assert all('视觉与动作' in shot['visual_prompt'] or shot['visual_prompt'] for shot in ir['shots'])
    spoken = [event['description'] for shot in ir['shots'] for event in shot['audio_events'] if event['kind'] == 'voice']
    assert len(spoken) == 4
    assert 'The cup feels solid.' in spoken[0]
    assert '中文翻译' in spoken[0]
    assert not ir['unrecognized']


def test_prefixed_time_axis_and_en_dash_bullets_preserve_native_word_timing():
    document = Document()
    document.add_heading('脚本 01 水杯与车内晨光', 0)
    document.add_heading('物理与硬性规范：', 1)
    document.add_paragraph('核心产品锁定：32 oz，高度 27 cm，杯身保持同款玳瑁花色。')
    document.add_paragraph('时长与画幅锁定：9.0秒，9:16 竖屏，1080x1920。')
    document.add_heading('场景设定：', 1)
    document.add_paragraph('车型与座舱形状：保时捷卡宴主驾驶座舱。')
    document.add_paragraph('内饰材质与道具细节：干邑棕真皮、米白围巾。')
    document.add_heading('人物设定：', 1)
    document.add_paragraph('身份与外貌特征：第一人称 POV，仅见女性左手。')
    document.add_heading('技术参数和禁止项：', 1)
    document.add_paragraph('技术参数：30fps 原生手机质感。')
    document.add_heading('严格禁止项：', 2)
    document.add_paragraph('严禁水印：画面不得出现叠加水印。')
    for start, end in [(0, 2), (2, 4), (4, 6), (6, 8), (8, 9)]:
        document.add_heading(f'【时间轴】 00:{start:02} - 00:{end:02}（稳定握杯）', 2)
        document.add_paragraph('– 摄影与构图: 第一人称中近景，镜头保持稳定。')
        document.add_paragraph('– 视觉与动作: 左手握住同款水杯，00:01 时轻转。')
        document.add_paragraph('– 音频与音效: 自然的座舱环境声。')
    stream = BytesIO()
    document.save(stream)
    ir = build_ir(read_blocks(stream.getvalue(), 5_000_000, 'cup.docx'), parse_blocks)
    assert ir['project']['aspect_ratio'] == '9:16'
    assert [shot['duration_seconds'] for shot in ir['shots']] == [2, 2, 2, 2, 1]
    assert len(ir['characters']) == 0
    assert '27 cm' in ir['project']['constraints']
    assert '30fps' in ir['project']['constraints']
    assert '女性左手' in ir['project']['constraints']
    assert '保时捷' in str(ir['scenes'][0]['environment'])
    assert '米白围巾' in str(ir['scenes'][0]['environment'])
    assert all('00:01' in shot['visual_prompt'] for shot in ir['shots'])


@pytest.mark.parametrize(('value', 'expected'), [
    ('3秒（00:00 - 00:03）', 3), ('4 seconds (00:03–00:07)', 4),
    ('8.5秒（00:00:56.250 --> 00:01:04.750）', 8.5),
])
def test_elapsed_duration_followed_by_interval(value, expected):
    assert parse_shot_duration(value) == expected


def test_reversed_duration_annotation_still_rejects_conflicting_values():
    with pytest.raises(ValueError, match='不一致'):
        parse_shot_duration('3秒（00:00 - 00:04）')


def test_annotated_cast_labels_and_family_chorus_do_not_create_phantom_people():
    document = Document()
    document.add_paragraph('脚本 5：全家拆盒')
    document.add_paragraph('人物设定')
    for label, annotation in [('人物甲', '幼童'), ('人物乙', '父亲'), ('人物丙', '母亲')]:
        document.add_paragraph('人物：'+label+'（'+annotation+'）')
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = '属性'
        table.cell(0, 1).text = '内容'
        table.cell(1, 0).text = '身份'
        table.cell(1, 1).text = annotation
    document.add_paragraph('正文剧情')
    document.add_paragraph('场景 1：庭院')
    document.add_paragraph('分镜 1：全家推荐')
    document.add_paragraph('出场人物：人物甲、人物乙、人物丙')
    document.add_paragraph('时长：4秒（00:11 - 00:15）')
    document.add_paragraph('对白：全家合唱：Bring the holiday magic home!')
    stream = BytesIO()
    document.save(stream)
    ir = build_ir(read_blocks(stream.getvalue(), 5_000_000), parse_blocks)
    assert len(ir['characters']) == 3
    assert len(ir['shots'][0]['participants']) == 3
    voice = ir['shots'][0]['audio_events'][0]
    assert voice['kind'] == 'voice' and 'Bring the holiday magic home!' in voice['description']
    assert len(voice['group_character_keys']) == 3
    assert not ir['unrecognized']
