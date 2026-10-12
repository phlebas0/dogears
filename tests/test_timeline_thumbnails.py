from pathlib import Path
import shutil
import subprocess

import pytest

from dogears.timeline_math import (
    viewport_span, clamp_view_start, ms_to_fraction, fraction_to_ms,
    ruler_step_ms, ruler_marks, ruler_label, preview_finished,
)
from dogears.thumbnail_core import thumbnail_args, thumbnail_name, thumbnail_seek_candidates


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
    assert cmd[cmd.index('-ss') + 1] == '3.200'
    assert cmd[cmd.index('-frames:v') + 1] == '1'
    assert cmd[-1] == str(output)
    assert cmd[cmd.index('-map') + 1] == '0:v:0'
    assert cmd[cmd.index('-update') + 1] == '1'
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


def test_scale_uses_round_absolute_time_marks():
    assert ruler_step_ms(60_000, 600) == 10_000
    assert ruler_step_ms(3_600_000, 600) == 1_000_000
    ticks = ruler_marks(23_000, 45_000, 10_000)
    assert ticks == [30_000, 40_000, 50_000, 60_000]
    assert ruler_label(90_000, 300_000, 10_000) == '01:30'
    assert ruler_label(3_690_000, 4_000_000, 10_000) == '01:01:30'
    assert ruler_label(1_250, 20_000, 500) == '00:01.2'


def test_thumbnail_fallbacks_handle_markers_beyond_end_of_media():
    positions = thumbnail_seek_candidates(1.2)
    assert positions[0] == 1.1
    assert positions[-1] == 0.0
    positions = thumbnail_seek_candidates(100)
    assert positions[-1] == 0.0
    assert len(positions) == len(set(positions))


def test_preview_stops_at_out_point():
    assert not preview_finished(1500, 2000)
    assert preview_finished(2000, 2000)
    assert preview_finished(2200, 2000)


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg not installed')
def test_ffmpeg_fallback_seek_near_end(tmp_path):
    source = tmp_path / 'short.mp4'
    target = tmp_path / 'short.jpg'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=duration=2:size=320x180:rate=10',
        '-c:v', 'mpeg4', source.as_posix(),
    ], check=True, capture_output=True)
    # 10-second bookmark in a 2-second video: final seek falls back to 0.
    for candidate in thumbnail_seek_candidates(10):
        subprocess.run(['ffmpeg', *thumbnail_args(source, candidate, target)], capture_output=True)
        if target.exists() and target.stat().st_size > 100:
            break
    assert target.is_file() and target.stat().st_size > 100


def test_clip_thumbnail_uses_early_representative_frame():
    from dogears.thumbnail_core import clip_thumbnail_time
    assert clip_thumbnail_time(10, 20) == 11.0
    assert clip_thumbnail_time(10, 11) == 10.25
    with pytest.raises(ValueError):
        clip_thumbnail_time(2, 2)
    with pytest.raises(ValueError):
        clip_thumbnail_time(-1, 5)


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg not installed')
def test_ffmpeg_extracts_clip_and_bookmark_thumbnails(tmp_path):
    from dogears.thumbnail_core import clip_thumbnail_time
    source = tmp_path / 'original video.mkv'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=duration=4:size=320x180:rate=12',
        '-c:v', 'mpeg4', str(source),
    ], check=True, capture_output=True)
    for kind, seconds in [('bookmark', 1.5), ('clip', clip_thumbnail_time(2, 3))]:
        output = tmp_path / f'{kind}.jpg'
        subprocess.run(['ffmpeg', *thumbnail_args(source, seconds, output)], check=True, capture_output=True)
        assert output.is_file()
        assert output.stat().st_size > 100
        assert output.read_bytes().startswith(b'\xff\xd8')
