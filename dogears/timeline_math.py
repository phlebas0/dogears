"""Pure, testable geometry and labelled scale for a zoomable timeline (milliseconds)."""
from __future__ import annotations

import math


def viewport_span(duration_ms: int, zoom: float) -> int:
    if duration_ms <= 0:
        return 0
    return min(duration_ms, max(1, round(duration_ms / max(1.0, zoom))))


def clamp_view_start(duration_ms: int, zoom: float, start_ms: float) -> int:
    return max(0, min(round(start_ms), max(0, duration_ms - viewport_span(duration_ms, zoom))))


def ms_to_fraction(timestamp_ms: float, start_ms: float, span_ms: int) -> float:
    return (timestamp_ms - start_ms) / max(1, span_ms)


def fraction_to_ms(fraction: float, start_ms: float, span_ms: int) -> int:
    return round(start_ms + max(0.0, min(1.0, fraction)) * span_ms)


def ruler_step_ms(visible_span_ms: int, track_width: int, minimum_spacing_px: int = 95) -> int:
    """Pick a round 1/2/5 time-step with legible labels at the given pixel width."""
    if visible_span_ms <= 0:
        return 1000
    target = max(100, visible_span_ms * minimum_spacing_px / max(1, track_width))
    base = 10 ** math.floor(math.log10(target))
    for multiplier in (1, 2, 5, 10):
        candidate = int(base * multiplier)
        if candidate >= target:
            return max(100, candidate)
    return max(100, int(base * 10))


def ruler_marks(start_ms: int, span_ms: int, step_ms: int) -> list[int]:
    """Absolute timestamps inside the viewport, including only visible major ticks."""
    if span_ms <= 0 or step_ms <= 0:
        return []
    first = (start_ms + step_ms - 1) // step_ms * step_ms
    return list(range(first, start_ms + span_ms + 1, step_ms))


def ruler_label(ms: int, duration_ms: int, step_ms: int) -> str:
    """Use absolute timecodes; reveal tenths only when the ruler is highly zoomed."""
    seconds, millis = divmod(max(0, ms), 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if duration_ms >= 3_600_000:
        result = f'{hours:02d}:{minutes:02d}:{secs:02d}'
    else:
        result = f'{minutes + hours * 60:02d}:{secs:02d}'
    if step_ms < 1000:
        result += f'.{millis // 100}'
    return result


def preview_finished(position_ms: int, end_ms: int) -> bool:
    """A clip preview ends precisely at its selected Out point."""
    return position_ms >= end_ms
