"""The Windows image2 file-output failure must not affect thumbnail decoding."""
from pathlib import Path
import shutil
import subprocess

import pytest

from dogears.thumbnail_core import thumbnail_pipe_args, thumbnail_seek_candidates


def test_pipe_thumbnail_does_not_ask_ffmpeg_to_write_jpeg(tmp_path):
    source = tmp_path / 'game.mp4'
    args = thumbnail_pipe_args(source, 2.25)
    assert args[args.index('-ss') + 1] == '2.250'
    assert args[args.index('-map') + 1] == '0:v:0'
    assert args[-3:] == ['-f', 'image2pipe', 'pipe:1']
    assert '-update' not in args
    assert not any('.partial.jpg' in str(arg) for arg in args)
    with pytest.raises(ValueError):
        thumbnail_pipe_args(source, -0.1)


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg unavailable')
def test_thumbnail_streams_valid_jpeg_even_when_cache_folder_is_absent(tmp_path):
    source = tmp_path / 'recording with spaces.mkv'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
        '-i', 'testsrc2=duration=3:size=320x180:rate=12',
        '-c:v', 'mpeg4', str(source),
    ], check=True, capture_output=True)

    # The failing Windows system couldn't create thumbnails in the FFmpeg
    # destination. The new command writes to stdout without an output path.
    nonexistent_cache = tmp_path / 'does not exist' / 'thumbnails'
    assert not nonexistent_cache.exists()
    result = subprocess.run(['ffmpeg', *thumbnail_pipe_args(source, 1.1)],
                            check=True, capture_output=True)
    assert result.stdout.startswith(b'\xff\xd8')
    assert result.stdout.endswith(b'\xff\xd9')
    assert len(result.stdout) > 100
    assert not nonexistent_cache.exists()
    from PIL import Image
    import io
    image = Image.open(io.BytesIO(result.stdout))
    assert image.size == (160, 90)


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg unavailable')
def test_thumbnail_pipe_can_fallback_to_first_frame(tmp_path):
    source = tmp_path / 'short source.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
        '-i', 'testsrc2=duration=2:size=320x180:rate=12',
        '-c:v', 'mpeg4', str(source),
    ], check=True, capture_output=True)
    for when in thumbnail_seek_candidates(50):
        result = subprocess.run(['ffmpeg', *thumbnail_pipe_args(source, when)],
                                capture_output=True)
        if result.returncode == 0 and result.stdout.startswith(b'\xff\xd8'):
            break
    assert result.stdout.startswith(b'\xff\xd8')
