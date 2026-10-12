"""Cache identity and resilient FFmpeg thumbnail extraction arguments."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path


def thumbnail_name(video: Path, mark_seconds: float) -> str:
    metadata = video.stat()
    identity = f'{video.resolve()}|{metadata.st_size}|{metadata.st_mtime_ns}|{mark_seconds:.3f}'
    return sha256(identity.encode('utf-8')).hexdigest()[:28] + '.jpg'


def thumbnail_seek_candidates(mark_seconds: float) -> tuple[float, ...]:
    """Try the bookmarked instant then earlier frames (for GOP/tail-end edge cases)."""
    if mark_seconds < 0:
        raise ValueError('Negative bookmark timestamp')
    candidates = [max(0.0, mark_seconds - delta) for delta in (0.1, 0.75, 2.0, 5.0)]
    candidates.append(0.0)  # Final fallback if a bookmark lies beyond decoded video EOF.
    # De-duplicate positions very close to the start of short recordings.
    return tuple(dict.fromkeys(round(value, 3) for value in candidates))


def thumbnail_args(video: Path, mark_seconds: float, destination: Path) -> list[str]:
    if mark_seconds < 0:
        raise ValueError('Negative bookmark timestamp')
    return [
        '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-ss', f'{mark_seconds:.3f}',
        '-i', str(video), '-map', '0:v:0', '-frames:v', '1',
        '-vf', 'scale=160:90:force_original_aspect_ratio=decrease,'
               'pad=160:90:(ow-iw)/2:(oh-ih)/2',
        '-an', '-sn', '-f', 'image2', '-update', '1', str(destination),
    ]


def clip_thumbnail_time(start: float, end: float) -> float:
    """Representative frame near the beginning of a user-selected clip."""
    if start < 0 or end <= start:
        raise ValueError('Invalid clip bounds')
    return round(start + min(1.0, (end - start) * 0.25), 3)


def thumbnail_pipe_args(video: Path, mark_seconds: float) -> list[str]:
    """Return FFmpeg arguments that stream one JPEG to stdout (no output path).

    Avoids the Windows image2 muxer failing to open files in the thumbnail cache.
    The caller decodes the JPEG with Qt and caches it separately if possible.
    """
    if mark_seconds < 0:
        raise ValueError('Negative bookmark timestamp')
    return [
        '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-ss', f'{mark_seconds:.3f}',
        '-i', str(video), '-map', '0:v:0', '-frames:v', '1',
        '-vf', 'scale=160:90:force_original_aspect_ratio=decrease,'
               'pad=160:90:(ow-iw)/2:(oh-ih)/2',
        '-an', '-sn', '-c:v', 'mjpeg', '-f', 'image2pipe', 'pipe:1',
    ]
