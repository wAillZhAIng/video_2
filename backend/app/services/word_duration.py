"""Read elapsed shot durations from Word cells and template lines."""

from decimal import Decimal
import re
import unicodedata


_SECONDS = re.compile(
    r'(?:约\s*|大约\s*|about\s+|approx\.?\s+)?'
    r'(?P<number>[0-9]+(?:\.[0-9]+)?|\.[0-9]+)'
    r'\s*(?:秒钟?|s|secs?\.?|seconds?)?(?:\s*(?:左右|上下))?', re.I)
_MINUTES = re.compile(r'(?P<minutes>[0-9]{1,6})\s*(?:分钟?|min(?:ute)?s?|m)\s*(?P<rest>.*)', re.I)
_TIMECODE = re.compile(
    r'(?:(?P<hours>[0-9]{1,6}):)?(?P<minutes>[0-9]{1,6}):'
    r'(?P<seconds>[0-9]{1,2}(?:[.,][0-9]{1,9})?)')
_ANNOTATION = re.compile(r'(?P<interval>[^|()]+)(?:\|(?P<pipe>[^|()]+)|\((?P<paren>[^()]*)\))')
_INTERVAL_SEPARATOR = re.compile(r'-->|[-–—−~〜至到→]')


def _seconds(value: str) -> Decimal | None:
    match = _SECONDS.fullmatch(value.strip())
    return Decimal(match['number']) if match else None


def _elapsed(value: str) -> Decimal | None:
    minutes = _MINUTES.fullmatch(value)
    if not minutes:
        return _seconds(value)
    seconds = _seconds(minutes['rest']) if minutes['rest'] else Decimal(0)
    if seconds is None or seconds >= 60:
        return None
    return Decimal(minutes['minutes']) * 60 + seconds


def _timecode(value: str) -> Decimal | None:
    match = _TIMECODE.fullmatch(value.strip())
    if not match:
        return None
    seconds = Decimal(match['seconds'].replace(',', '.'))
    hours, minutes = match['hours'], int(match['minutes'])
    if seconds >= 60 or (hours is not None and minutes >= 60):
        return None
    return seconds + minutes * 60 + int(hours or 0) * 3600


def _interval_seconds(text: str, declared: Decimal | None, *, timeline=False) -> Decimal | None:
    bounds = _INTERVAL_SEPARATOR.split(text, maxsplit=1)
    if len(bounds) != 2:
        return None
    start, end = map(_timecode, bounds)
    if start is None and end is None:
        start, end = map(_seconds, bounds)
        # A bare numerical range could be an estimate for a single duration.
        # A declared elapsed value or a zero origin establishes an interval.
        if not timeline and declared is None and start != 0:
            return None
    if start is None or end is None:
        return None
    if end <= start:
        raise ValueError('时间区间的结束时间须晚于开始时间')
    return end - start


def is_time_range(value: str, *, timeline=False) -> bool:
    text = unicodedata.normalize('NFKC', value).strip()
    bounds = _INTERVAL_SEPARATOR.split(text, maxsplit=1)
    if len(bounds) != 2:
        return False
    return all(_TIMECODE.fullmatch(part.strip()) for part in bounds) or (
        timeline and all(_SECONDS.fullmatch(part.strip()) for part in bounds))


def parse_shot_duration(value: str, *, timeline=False) -> float:
    """Return elapsed seconds, or explain what needs confirmation."""
    text = ' '.join(unicodedata.normalize('NFKC', value).split())
    seconds = _elapsed(text)
    if seconds is None:
        seconds = _timecode(text)
    if seconds is None:
        annotation = _ANNOTATION.fullmatch(text)
        declared = None
        if annotation:
            duration = annotation['pipe'] or annotation['paren']
            duration = re.sub(r'^(?:时长|duration)\s*[:=]?\s*', '', duration.strip(), flags=re.I)
            declared = _elapsed(duration)
            # Both “00:00–00:03（3秒）” and “3秒（00:00–00:03）”
            # declare the same interval. Validate both values; never default
            # a valid reversed annotation to the suggested five-second shot.
            leading = _elapsed(annotation['interval'].strip())
            if declared is None and leading is not None and is_time_range(duration, timeline=True):
                declared = leading
                text = duration
            else:
                text = annotation['interval']
            if declared is None:
                raise ValueError('时间区间附带的时长无法识别')
        seconds = _interval_seconds(text, declared, timeline=timeline)
        if seconds is None:
            raise ValueError('无法确定时长，请填写秒数或明确的起止时间码')
        if declared is not None:
            if abs(declared - seconds) > Decimal('0.001'):
                raise ValueError('时间区间和标注的时长不一致')
            seconds = declared
    if not 1 <= seconds <= 60:
        raise ValueError('单个分镜的时长须在 1 至 60 秒之间')
    return float(seconds)
