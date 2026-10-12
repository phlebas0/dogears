"""Pure, testable geometry for a zoomable video timeline (milliseconds)."""
from __future__ import annotations


def viewport_span(duration_ms: int, zoom: float) -> int:
    """Always show at least 1 ms when there is a nonempty video."""
    if duration_ms <= 0:
        return 0
    return min(duration_ms, max(1, round(duration_ms / max(1.0, zoom))))


def clamp_view_start(duration_ms: int, zoom: float, start_ms: float) -> int:
    return max(0, min(round(start_ms), max(0, duration_ms - viewport_span(duration_ms, zoom))))


def ms_to_fraction(timestamp_ms: float, start_ms: float, span_ms: int) -> float:
    return (timestamp_ms - start_ms) / max(1, span_ms)


def fraction_to_ms(fraction: float, start_ms: float, span_ms: int) -> int:
    return round(start_ms + max(0.0, min(1.0, fraction)) * span_ms)
