"""Keep written H3 media citations tied to the actual attachment inventory."""
from __future__ import annotations

import re


_MEDIA_KINDS = {
    'picture': 'Picture', 'image': 'Picture', 'img': 'Picture', 'photo': 'Picture',
    'frame': 'Picture', 'ref': 'Picture', 'reference': 'Picture',
    'video': 'Video', 'clip': 'Video', 'footage': 'Video',
    'audio': 'Audio', 'sound': 'Audio', 'track': 'Audio',
}
_MEDIA_TAG = re.compile(r'<\s*(' + '|'.join(_MEDIA_KINDS) + r')\s+([0-9]+)\s*>', re.I)
_VERBATIM = re.compile(r'(<d>.*?</d>|"[^"\r\n]*")', re.I | re.S)
_DEFINITIONS = re.compile(r'(^subject_definitions[ \t]*:)(.*?)(?=^[a-z_]+[ \t]*:|\Z)', re.M | re.S)
_SUBJECT = re.compile(r'^[ \t]*<Subject [0-9]+>[ \t]+is\b')
_ATTRIBUTION = re.compile(
    r'(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+)whose[ \t]+'
    r'(?:[a-z][a-z-]*[ \t]+){0,5}(?:motion|movement|gait|voice|audio|sound|appearance|view)[ \t]+'
    r'(?:comes from|is (?:drawn|taken|sourced|copied|derived) from)[ \t]+'
    r'(?P<label><(?:Picture|Video|Audio) [0-9]+>)'
    r'(?=[ \t]*(?:[.!?;]|\r?$|,?[ \t]+and[ \t]+whose\b))', re.I,
)


def _canonical_tag(match):
    number = match[2].lstrip('0') or '0'
    return f'<{_MEDIA_KINDS[match[1].lower()]} {number}>'


def written_media_tags(text):
    return {_canonical_tag(match) for prose in _VERBATIM.split(text)[::2]
            for match in _MEDIA_TAG.finditer(prose)}


def repair_media_bindings(text, labels):
    """Repair syntax and drop only unsupported provenance with a real anchor.

    A definition such as “appearance comes from Picture 1 and whose motion
    comes from Video 1” still has its real appearance source when a nonexistent
    Video 1 clause is removed. References in the action, audio or a definition
    without another real anchor need the native correction and verification.
    """
    allowed = frozenset(labels)
    parts = _VERBATIM.split(text)
    parts[::2] = [_MEDIA_TAG.sub(_canonical_tag, part) for part in parts[::2]]
    normalized = ''.join(parts)
    repairs = ['normalized written media tag spelling without changing attachment ordinals'] if normalized != text else []
    removed = set()

    def repair_definitions(block):
        lines = []
        for line in block[2].splitlines(keepends=True):
            if _SUBJECT.match(line):
                quoted = list(_VERBATIM.finditer(line))

                def remove_attribution(match):
                    label = match['label']
                    prefix = re.split(r'[.!?;][ \t]+', line[:match.start()])[-1]
                    if label in allowed or not allowed.intersection(written_media_tags(prefix)):
                        return match[0]
                    if any(span.start() < match.end() and match.start() < span.end() for span in quoted):
                        return match[0]
                    removed.add(label)
                    return ''

                line = _ATTRIBUTION.sub(remove_attribution, line)
            lines.append(line)
        return block[1] + ''.join(lines)

    repaired = _DEFINITIONS.sub(repair_definitions, normalized)
    if removed:
        repairs.append('removed unsupported source attribution to unattached ' + ', '.join(sorted(removed)))
    return repaired, repairs


def ground_writer_messages(messages, labels):
    """Add request-specific attachment facts to the native writer instructions."""
    media = ', '.join(labels) or 'none'
    instructions = (
        f'Actual media attachments for this request: {media}. Only these media tags can be cited. '
        'The tags and creative details in examples are illustrations. Keep each attachment in its '
        'declared role. Describe requested camera movements, action and sound in the numbered shot '
        'or sound fields as generated content unless the caller supplied the corresponding source.\n\n'
    )
    return [{**messages[0], 'content': instructions + messages[0]['content']}, *messages[1:]]
