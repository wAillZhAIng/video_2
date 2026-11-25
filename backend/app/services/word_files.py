"""Read visible Word content in document order, with source block identifiers."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath

from lxml import etree

from app.core.errors import AppError


WORD_EXTENSIONS = frozenset({'.docx', '.docm', '.dotx', '.dotm', '.doc', '.dot', '.rtf'})
WORD_MIME_TYPES = {
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.docm': 'application/vnd.ms-word.document.macroEnabled.12',
    '.dotx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
    '.dotm': 'application/vnd.ms-word.template.macroEnabled.12',
    '.doc': 'application/msword',
    '.dot': 'application/msword',
    '.rtf': 'application/rtf',
}
_OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_WORD_NAMESPACES = {
    'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'http://purl.oclc.org/ooxml/wordprocessingml/main',
}
_COMPAT = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
_WRAPPERS = {'sdt', 'sdtContent', 'customXml', 'ins', 'moveTo', 'smartTag'}
_HIDDEN = {'del', 'moveFrom', 'delText', 'instrText', 'delInstrText', 'pPr', 'rPr'}


def _word_tag(node):
    tag = node.tag
    if isinstance(tag, str) and tag.startswith('{'):
        namespace, _, name = tag[1:].partition('}')
        if namespace in _WORD_NAMESPACES:
            return name
    return ''


def _attribute(node, name):
    return next((value for key, value in node.attrib.items()
                 if key.rpartition('}')[0][1:] in _WORD_NAMESPACES and key.endswith('}' + name)), None)


def _child(node, name):
    return next((item for item in node if _word_tag(item) == name), None) if node is not None else None


def _value(node, name):
    child = _child(node, name)
    return _attribute(child, 'val') if child is not None else None


def _xml(archive, name):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    return etree.fromstring(archive.read(name), parser)


def _children(node):
    for child in node:
        if child.tag == _COMPAT + 'AlternateContent':
            selected = child.find(_COMPAT + 'Choice')
            if selected is None:
                selected = child.find(_COMPAT + 'Fallback')
            if selected is not None:
                yield from _children(selected)
        elif _word_tag(child) not in _HIDDEN:
            yield child


def _blocks(node, wanted):
    for child in _children(node):
        tag = _word_tag(child)
        if tag in wanted:
            yield child
        elif tag in _WRAPPERS:
            yield from _blocks(child, wanted)


def _paragraph_text(paragraph):
    def collect(node):
        tag = _word_tag(node)
        if tag == 't':
            yield node.text or ''
        elif tag == 'tab':
            yield '\t'
        elif tag in {'br', 'cr'}:
            yield '\n'
        elif tag == 'txbxContent':
            yield '\n' + _cell_text(node) + '\n'
        else:
            for child in _children(node):
                yield from collect(child)
    return ''.join(collect(paragraph)).strip()


def _cell_text(cell):
    parts = []
    for block in _blocks(cell, {'p', 'tbl'}):
        if _word_tag(block) == 'p':
            parts.append(_paragraph_text(block))
        else:
            parts.extend('\t'.join(row) for row in _rows(block))
    return '\n'.join(part for part in parts if part).strip()


def _rows(table):
    for row in _blocks(table, {'tr'}):
        before = _value(_child(row, 'trPr'), 'gridBefore')
        values = [''] * min(int(before), 200) if before and before.isascii() and before.isdecimal() else []
        for cell in _blocks(row, {'tc'}):
            values.append(_cell_text(cell))
            span = _value(_child(cell, 'tcPr'), 'gridSpan')
            if span and span.isascii() and span.isdecimal():
                values.extend([''] * max(0, min(int(span), 200) - 1))
        yield values


def _heading_level(properties, style):
    level = _value(properties, 'outlineLvl')
    if level in tuple(str(n) for n in range(9)):
        return int(level) + 1
    match = re.fullmatch(r'(?:heading|标题)\s*([1-9])', style, re.I)
    return int(match[1]) if match else None


def _archive_blocks(data, limit):
    with zipfile.ZipFile(BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > limit * 20:
            raise AppError(413, 'docx_expanded_too_large', 'Word 文件的解压内容过大')
        paths = set(archive.namelist())
        main = 'word/document.xml'
        if '_rels/.rels' in paths:
            for relation in _xml(archive, '_rels/.rels'):
                if relation.get('Type', '').endswith('/officeDocument') and relation.get('TargetMode') != 'External':
                    main = relation.get('Target', '').lstrip('/')
                    break
        if not main or '..' in PurePosixPath(main).parts or main not in paths:
            raise AppError(422, 'invalid_docx', 'Word 文件缺少可读取的正文')
        body = _child(_xml(archive, main), 'body')
        if body is None:
            raise AppError(422, 'invalid_docx', 'Word 文件缺少可读取的正文')
        styles = {}
        style_path = str(PurePosixPath(main).with_name('styles.xml'))
        if style_path in paths:
            for style in _xml(archive, style_path):
                identifier = _attribute(style, 'styleId')
                if identifier:
                    name = _value(style, 'name') or identifier
                    name = name.title() if name.lower().startswith('heading ') or name.lower() in {'title', 'subtitle'} else name
                    styles[identifier] = (name, _heading_level(_child(style, 'pPr'), name))
        result = []
        for index, block in enumerate(_blocks(body, {'p', 'tbl'})):
            if _word_tag(block) == 'p':
                properties = _child(block, 'pPr')
                identifier = _value(properties, 'pStyle') or 'Normal'
                style, fallback_level = styles.get(identifier, (identifier, None))
                result.append({'id': f'paragraph:{index}', 'kind': 'paragraph', 'style': style,
                               'text': _paragraph_text(block),
                               'heading_level': _heading_level(properties, style) or fallback_level})
            else:
                result.append({'id': f'table:{index}', 'kind': 'table', 'rows': list(_rows(block))})
    return result


def _legacy_to_docx(data, extension, limit):
    converter = shutil.which('soffice') or shutil.which('libreoffice')
    if not converter:
        paths = [Path('C:/Program Files/LibreOffice/program/soffice.com'),
                 Path('C:/Program Files/LibreOffice/program/soffice.exe')]
        converter = next((str(path) for path in paths if path.is_file()), None)
    if not converter:
        raise AppError(503, 'word_converter_unavailable', '旧版 Word 和 RTF 需要本机 LibreOffice 转换器')
    try:
        with tempfile.TemporaryDirectory(prefix='word-import-') as temporary:
            root = Path(temporary)
            profile = root / 'profile'
            (profile / 'user').mkdir(parents=True)
            (profile / 'user/registrymodifications.xcu').write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
                '</oor:items>', encoding='utf-8')
            source = root / ('source' + extension)
            source.write_bytes(data)
            subprocess.run([converter, '-env:UserInstallation=' + profile.as_uri(),
                            '--headless', '--nologo', '--norestore', '--nodefault',
                            '--convert-to', 'docx', '--outdir', str(root), str(source)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30, check=True)
            output = root / 'source.docx'
            if not output.is_file():
                raise AppError(422, 'word_conversion_failed', '旧版 Word 无法读取。请检查文件是否损坏或加密')
            with output.open('rb') as converted:
                result = converted.read(limit + 1)
        if len(result) > limit:
            raise AppError(413, 'document_too_large', '转换后的 Word 文档超过大小限制')
        return result
    except subprocess.TimeoutExpired as exc:
        raise AppError(422, 'word_conversion_timeout', '旧版 Word 转换超时，请另存为 DOCX 后重试') from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AppError(422, 'word_conversion_failed', '旧版 Word 无法读取。请检查文件是否损坏或加密') from exc


def blocks_text(blocks):
    return '\n'.join(block['text'] if block['kind'] == 'paragraph' else
                     '\n'.join('\t'.join(row) for row in block['rows']) for block in blocks).strip()


def read_word_blocks(filename, data, limit):
    if not data:
        raise AppError(422, 'empty_document', '上传文档为空')
    if len(data) > limit:
        raise AppError(413, 'document_too_large', '文档超过大小限制')
    if Path(filename).suffix.lower() not in WORD_EXTENSIONS:
        raise AppError(415, 'unsupported_word_type', '请选择 DOCX、DOCM、DOTX、DOTM、DOC、DOT 或 RTF 文件')
    if data.startswith(_OLE):
        if 'EncryptedPackage'.encode('utf-16le') in data:
            raise AppError(422, 'word_password_required', '此 Word 已加密，请在 Word 中解除密码保护后重新上传')
        data = _legacy_to_docx(data, '.doc', limit)
    elif data.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'{\\rtf'):
        data = _legacy_to_docx(data, '.rtf', limit)
    elif not data.startswith(b'PK'):
        raise AppError(422, 'invalid_docx', '文件内容不是可识别的 Word 文档，请在 Word 中另存为 .docx 后重试')
    try:
        result = _archive_blocks(data, limit)
    except (zipfile.BadZipFile, etree.LxmlError, NotImplementedError, KeyError, RuntimeError,
            ValueError, OSError, RecursionError) as exc:
        raise AppError(422, 'invalid_docx', 'Word 文件损坏或无法读取') from exc
    if not blocks_text(result):
        raise AppError(422, 'empty_document', 'Word 中没有可读取的文字。只有扫描图片时，请先转为可编辑文字')
    return result
