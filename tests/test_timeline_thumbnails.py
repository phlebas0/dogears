from pathlib import Path
import shutil
import subprocess

import pytest

from dogears.timeline_math import (
    viewport_span, clamp_view_start, ms_to_fraction, fraction_to_ms,
)
from dogears.thumbnail_core import thumbnail_args, thumbnail_name


def test_zoom_changes_visible_span_without_changing_absolute_timestamps():
    assert viewport_span(600_000, 1) == 600_000
    assert viewport_span(600_000, 4) == 150_000
    assert viewport_span(600_000, 32) == 18_750
    assert clamp_view_start(600_000, 4, 550_000) == 450_000
    assert clamp_view_start(600_000, 4, -100) == 0
    assert ms_to_fraction(200_000, 150_000, 100_000) == 0.5
    assert fraction_to_ms(0.5, 150_000, 100_000) == 200_000
    assert fraction_to_ms(-1, 150_000, 100_000) == 150_000
    assert fraction_to_ms(2, 150_000, 100_000) == 250_000


def test_zoom_empty_recording_and_one_millisecond():
    assert viewport_span(0, 32) == 0
    assert viewport_span(1, 32) == 1
    assert clamp_view_start(0, 10, 999) == 0


def test_thumbnail_name_invalidates_on_file_change(tmp_path):
    video = tmp_path / 'game.mkv'
    video.write_bytes(b'first')
    original = thumbnail_name(video, 1.25)
    assert thumbnail_name(video, 1.25) == original
    assert thumbnail_name(video, 2.25) != original
    video.write_bytes(b'second longer video')
    assert thumbnail_name(video, 1.25) != original


def test_ffmpeg_thumbnail_command(tmp_path):
    source = tmp_path / 'gameplay test.mp4'
    output = tmp_path / 'thumb.jpg'
    cmd = thumbnail_args(source, 3.2, output)
    assert cmd[cmd.index('-ss') + 1] == '3.100'
    assert cmd[cmd.index('-frames:v') + 1] == '1'
    assert cmd[-1] == str(output)
    with pytest.raises(ValueError):
        thumbnail_args(source, -1, output)


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg not installed')
def test_ffmpeg_can_extract_bookmark_preview(tmp_path):
    source = tmp_path / 'source.mp4'
    target = tmp_path / 'bookmark.jpg'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=duration=2:size=320x180:rate=10',
        '-c:v', 'mpeg4', source.as_posix(),
    ], check=True, capture_output=True)
    subprocess.run(['ffmpeg', *thumbnail_args(source, 1.0, target)], check=True, capture_output=True)
    assert target.is_file() and target.stat().st_size > 100
    assert target.read_bytes().startswith(b'\xff\xd8')
