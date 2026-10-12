"""Multi-clip planning, project persistence and real FFmpeg concat coverage."""
import json
import shutil
import subprocess

import pytest

from dogears.library_core import (
    clip_project_path, concat_arguments, concat_manifest, export_arguments,
    load_clip_ranges, save_clip_ranges, separate_clip_destinations,
)


def test_save_and_restore_selection_order(tmp_path):
    clips = [(14.1, 15.3), (2.0, 4.4), (22.0, 35.0)]
    save_clip_ranges(tmp_path / 'sessions', 'session123', clips)
    assert load_clip_ranges(tmp_path / 'sessions', 'session123') == clips
    content = json.loads(clip_project_path(tmp_path / 'sessions', 'session123').read_text())
    assert content['schema_version'] == 1
    assert content['clips'] == [[14.1, 15.3], [2.0, 4.4], [22.0, 35.0]]


def test_malformed_or_unsafe_clip_files_are_ignored(tmp_path):
    folder = tmp_path / 'sessions'
    folder.mkdir()
    assert clip_project_path(folder, '../../escape').parent == folder / 'clip_projects'
    path = clip_project_path(folder, 'session1')
    path.parent.mkdir()
    path.write_text(json.dumps({'session_id': 'session1', 'clips': [[5, 10], ['nan', 9], [9, 8], ['oops', 1], [1, 2]]}))
    assert load_clip_ranges(folder, 'session1') == [(5.0, 10.0), (1.0, 2.0)]
    path.write_text('{garbled')
    assert load_clip_ranges(folder, 'session1') == []
    with pytest.raises(ValueError):
        save_clip_ranges(folder, 'session1', [(8, 5)])


def test_export_names_numbered_in_user_clip_order(tmp_path):
    video = tmp_path / 'session.mkv'
    clips = [(12, 15), (4, 9), (12, 15)]
    outputs = separate_clip_destinations(video, tmp_path, clips)
    assert len(set(outputs)) == 3
    assert '_clip_001_' in outputs[0].name
    assert '_clip_002_' in outputs[1].name
    assert '_clip_003_' in outputs[2].name
    assert all(path.suffix == '.mp4' for path in outputs)
    with pytest.raises(ValueError):
        separate_clip_destinations(video, tmp_path, [])


def test_concat_manifest_only_uses_generated_safe_names(tmp_path):
    segs = [tmp_path / 'segment_001.mp4', tmp_path / 'segment_002.mp4']
    assert concat_manifest(segs) == "file 'segment_001.mp4'\nfile 'segment_002.mp4'\n"
    assert concat_arguments(tmp_path / 'concat.txt', tmp_path / 'merged.mp4')[-2:] == ['-n', str(tmp_path / 'merged.mp4')]
    with pytest.raises(ValueError):
        concat_manifest([tmp_path / 'escape\'.mp4'])


@pytest.mark.skipif(not (shutil.which('ffmpeg') and shutil.which('ffprobe')), reason='FFmpeg unavailable')
def test_two_clips_export_and_merge_video_in_order(tmp_path):
    source = tmp_path / 'source.mp4'
    # Generate small source with changing video and an audio track.
    subprocess.run([
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=15:d=4',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=4',
        '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', '-y', str(source),
    ], check=True)
    clips = [(0.5, 1.5), (2.0, 3.5)]
    segs = [tmp_path / f'segment_{i:03d}.mp4' for i in range(1, 3)]
    for (start, end), seg in zip(clips, segs):
        subprocess.run(['ffmpeg', *export_arguments(source, seg, start, end)], check=True)
    manifest = tmp_path / 'concat.txt'
    manifest.write_text(concat_manifest(segs), encoding='utf-8')
    merged = tmp_path / 'combined.mp4'
    subprocess.run(['ffmpeg', *concat_arguments(manifest, merged)], check=True)
    duration = float(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(merged)], text=True))
    assert merged.is_file()
    assert 2.4 <= duration <= 2.8  # 1.0 + 1.5s, accounting for frame/audio rounding
    assert source.is_file()  # original untouched
