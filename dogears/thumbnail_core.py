"""Safe, deterministic thumbnail paths and FFmpeg arguments. No image generation during gameplay."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path


def thumbnail_name(video: Path, mark_seconds: float) -> str:
    metadata = video.stat()
    identity = f'{video.resolve()}|{metadata.st_size}|{metadata.st_mtime_ns}|{mark_seconds:.3f}'
    return sha256(identity.encode('utf-8')).hexdigest()[:28] + '.jpg'


def thumbnail_args(video: Path, mark_seconds: float, destination: Path) -> list[str]:
    if mark_seconds < 0:
        raise ValueError('Negative bookmark timestamp')
    return [
        '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-ss', f'{max(0.0, mark_seconds - 0.1):.3f}',
        '-i', str(video), '-frames:v', '1',
        '-vf', 'scale=160:90:force_original_aspect_ratio=decrease,'
               'pad=160:90:(ow-iw)/2:(oh-ih)/2',
        '-an', '-sn', '-f', 'image2', str(destination),
    ]
