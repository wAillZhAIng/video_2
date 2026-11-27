from __future__ import annotations

import socket
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services.imports import parse_uploaded_document, validate_url_target
from app.services.parser import DeterministicScriptParser
from app.services.word_screenplay import read_blocks


ROOT = Path(__file__).resolve().parents[2]


def test_demo_script_parses_three_structured_shots() -> None:
    raw = (ROOT / "fixtures" / "demo_script.md").read_text(encoding="utf-8")
    result = DeterministicScriptParser().parse(raw)

    assert result["title"] == "雨夜归还：人物一致性验证"
    assert [shot["order"] for shot in result["shots"]] == [1, 2, 3]
    assert result["shots"][0]["duration_seconds"] == 5
    assert result["shots"][0]["camera_move"] == "缓慢推进"
    assert "深蓝色笔记本" in result["shots"][1]["visual_prompt"]
    assert "左眼下方小痣" in result["shots"][2]["visual_prompt"]


def test_parser_falls_back_to_paragraphs_and_preserves_content() -> None:
    raw = "第一段没有分镜标签。\n\n第二段仍应保留。"
    result = DeterministicScriptParser().parse(raw)
    assert len(result["shots"]) == 2
    assert "第一段" in result["shots"][0]["narration"]
    assert result["warnings"]


def test_text_and_docx_importers_extract_visible_content() -> None:
    md_data = (ROOT / "fixtures" / "demo_script.md").read_bytes()
    md_text, md_mime = parse_uploaded_document("demo_script.md", md_data, 2_000_000)
    assert md_mime == "text/markdown"
    assert "分镜 3" in md_text

    docx_data = (ROOT / "fixtures" / "demo_script.docx").read_bytes()
    docx_text, docx_mime = parse_uploaded_document("demo_script.docx", docx_data, 2_000_000)
    assert docx_mime.endswith("wordprocessingml.document")
    assert "雨夜街口" in docx_text
    assert "咖啡店窗边" in docx_text


@pytest.mark.parametrize("extension", [".docx", ".docm", ".dotx", ".dotm"])
def test_word_processes_content_controls_textboxes_and_tracked_insertions(extension):
    xml=b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                        xmlns:v="urn:schemas-microsoft-com:vml">
       <w:body><w:sdt><w:sdtContent><w:p><w:r><w:t>Controlled text</w:t></w:r></w:p></w:sdtContent></w:sdt>
       <w:p><w:r><w:t>Before</w:t><w:br/><w:t>after</w:t></w:r>
         <w:ins><w:r><w:t> inserted</w:t></w:r></w:ins><w:del><w:r><w:delText> removed</w:delText></w:r></w:del>
       </w:p><w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>
       <w:p><w:r><w:t>Inside the box</w:t></w:r></w:p>
       </w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p></w:body></w:document>'''
    raw=BytesIO()
    with zipfile.ZipFile(raw,"w") as archive:
        archive.writestr("word/document.xml",xml)
    blocks=read_blocks(raw.getvalue(),2_000_000,"layout"+extension)
    assert [row["text"] for row in blocks]==["Controlled text","Before\nafter inserted","Inside the box"]
    flat,mime=parse_uploaded_document("layout"+extension,raw.getvalue(),2_000_000)
    assert flat=="Controlled text\nBefore\nafter inserted\nInside the box"
    assert mime.startswith("application/")


@pytest.mark.parametrize(
    ("filename", "data", "expected_code"),
    [
        ("empty.md", b"", "empty_document"),
        ("fake.docx", b"not-a-zip", "invalid_docx"),
        ("binary.txt", b"hello\x00world", "invalid_text_document"),
        ("script.exe", b"MZ", "unsupported_document_type"),
    ],
)
def test_importer_rejects_invalid_files(filename: str, data: bytes, expected_code: str) -> None:
    with pytest.raises(AppError) as raised:
        parse_uploaded_document(filename, data, 2_000_000)
    assert raised.value.code == expected_code


def test_url_import_is_disabled_by_default() -> None:
    with pytest.raises(AppError) as raised:
        validate_url_target("https://example.com/doc", Settings(allow_external_doc_import=False))
    assert raised.value.code == "url_import_disabled"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/doc",
        "http://localhost/doc",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "http://10.0.0.2/doc",
    ],
)
def test_url_validator_blocks_ssrf_targets(url: str) -> None:
    settings = Settings(allow_external_doc_import=True)
    with pytest.raises(AppError) as raised:
        validate_url_target(url, settings)
    assert raised.value.code in {"unsafe_url", "invalid_url"}


def test_url_validator_enforces_allowlist_and_dns_rebinding_check() -> None:
    settings = Settings(
        allow_external_doc_import=True,
        doc_import_allowed_hosts=("docs.example.test",),
    )

    def public_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    assert validate_url_target("https://docs.example.test/page", settings, public_resolver) == "docs.example.test"

    with pytest.raises(AppError) as not_allowed:
        validate_url_target("https://example.test/page", settings, public_resolver)
    assert not_allowed.value.code == "host_not_allowed"

    def private_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", 443))]

    with pytest.raises(AppError) as rebound:
        validate_url_target("https://docs.example.test/page", settings, private_resolver)
    assert rebound.value.code == "unsafe_url"


def test_parser_handles_feishu_time_ranges_and_global_setting() -> None:
    raw = """【全局设定】
总时长：17秒｜画幅：9:16竖屏｜风格：写实自然光影电影感
人物锚点：28岁东亚青年男性，戴哑光黑城市骑行头盔。
产品锁死锚点：黑色磨砂翼型转向尾灯。
禁止项：禁止出现字幕花字、水印、人物变脸。

镜头1（0-4秒）
景别与机位：中近景，后方追随跟拍。
画面主体：青年男子骑行向前。
动作轨迹：男子急停靠边回头。
运镜：手持稳定器前推跟拍。
光线与产品细节：阴天冷白自然光。
音效：刹车摩擦声。

镜头2（4-6秒）
画面主体：双手打开产品包装盒。
动作轨迹：右手取出转向尾灯展示。
音效：纸盒翻盖声。

镜头3（6-9.5秒）
- 画面主体与动作轨迹：将尾灯扣紧在座管上并点亮。
- 运镜与光线：固定特写，黄昏蓝调冷光。
- 音效：咔哒声。
"""
    result = DeterministicScriptParser().parse(raw)
    assert result["warnings"] == []
    assert len(result["shots"]) == 3
    assert [s["order"] for s in result["shots"]] == [1, 2, 3]
    assert [s["duration_seconds"] for s in result["shots"]] == [4.0, 2.0, 3.5]
    assert "全局设定" in result["metadata"]
    assert "总时长" not in result["metadata"]["全局设定"]
    assert "青年男子骑行向前" in result["shots"][0]["visual_prompt"]
    assert "黑色磨砂翼型转向尾灯" in result["shots"][0]["visual_prompt"]
    assert "禁止出现字幕花字" in result["shots"][0]["negative_prompt"]
    assert "刹车摩擦声" in result["shots"][0]["audio_direction"]
    assert "扣紧在座管上" in result["shots"][2]["visual_prompt"]


def test_parser_handles_compact_inline_shots_without_blank_lines() -> None:
    raw = """【全局设定】
画幅与画质：9:16竖屏，4K写实实拍质感，严禁出现字幕花字、水印。
产品锚点：浅鸭蛋青色哑光鸭蛋捏捏。

【分镜脚本（总时长3.2秒，共3镜）】
镜头1（0-2.0秒）：45°高俯拍POV近景固定机位。双手捧住鸭蛋捏捏捏裂脆壳。
镜头2（2.0-2.7秒）：硬跳切，俯拍近景。右手拇指按入亮橘红软芯。
镜头3（2.7-3.2秒）：硬跳切收尾，掌心托举回弹定格。
"""
    result = DeterministicScriptParser().parse(raw)
    assert result["warnings"] == []
    assert len(result["shots"]) == 3
    assert [s["duration_seconds"] for s in result["shots"]] == [2.0, 0.7, 0.5]
    assert "双手捧住鸭蛋捏捏" in result["shots"][0]["visual_prompt"]
    assert "浅鸭蛋青色哑光鸭蛋捏捏" in result["shots"][1]["visual_prompt"]
    assert "严禁出现字幕花字" in result["shots"][0]["negative_prompt"]
