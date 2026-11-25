"""Recognize character declarations and cast lists in Word screenplays."""
import re


_PREFIX = r'(?:人物|角色|character)'
_DELIMITER = r'(?:\s*[:：-]\s*|\s+)'
_ORDINAL = r'(?:[甲乙丙丁戊己庚辛壬癸]|[A-Z]|[\d一二三四五六七八九十]+)'
_HEADING = re.compile(rf'{_PREFIX}{_DELIMITER}(.+)', re.IGNORECASE)
_NUMBERED = re.compile(rf'({_PREFIX})\s*({_ORDINAL}){_DELIMITER}(.+)', re.IGNORECASE)
_BRACKETS = {'(': ')', '（': '）', '[': ']', '【': '】', '“': '”', '‘': '’'}
_CLOSERS = set(_BRACKETS.values())


def character_heading(text: str) -> tuple[str, str | None] | None:
    """Return the name and optional document-local label, e.g. 人物甲."""
    heading = _HEADING.fullmatch(text)
    numbered = _NUMBERED.fullmatch(text)
    if numbered is None and heading:
        numbered = _NUMBERED.fullmatch(heading[1].strip())
    if numbered:
        return numbered[3].strip(), numbered[1] + numbered[2]
    if heading:
        return heading[1].strip(), None
    return None


def cast_entries(text: str) -> list[str]:
    """Split a roster at separators outside annotations and quoted names."""
    entries = []
    closers = []
    start = 0
    for index, char in enumerate(text):
        if char in _BRACKETS:
            closers.append(_BRACKETS[char])
        elif char in _CLOSERS:
            if not closers or closers.pop() != char:
                raise ValueError('出场人物括号或引号不匹配')
        elif char in '、,，;；' and not closers:
            entries.append(text[start:index].strip())
            start = index + 1
    if closers:
        raise ValueError('出场人物括号或引号未闭合')
    entries.append(text[start:].strip())
    return [entry for entry in entries if entry]


def name_without_annotation(label: str) -> str:
    """Use the name portion of a label such as 林知夏（20岁，成年女性）."""
    annotated = re.fullmatch(r'([^（(]+?)(?:（.*）|\(.*\))', label)
    return annotated[1].strip() if annotated else label
