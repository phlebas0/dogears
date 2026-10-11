import json
from pathlib import Path

import pytest

from dogears.core import Session, SessionStore
from dogears.library_core import (
    export_arguments, load_recordings, parse_timecode, relink_video,
    suggested_clip_name, validate_range,
)


def test_timecode_input():
    assert parse_timecode("01:03:04.250") == 3784.25
    assert parse_timecode("03:04.25") == 184.25
    assert parse_timecode("12.125") == 12.125
    for invalid in ("", "-5", "abc", "00:61", "01:02:61", "nan", "inf", "1:2:3:4"):
        with pytest.raises(ValueError):
            parse_timecode(invalid)


def test_library_reads_old_sessions_and_sidecars_without_duplicates(tmp_path):
    folder = tmp_path / "sessions"
    video_folder = tmp_path / "videos"
    video_folder.mkdir()
    video = video_folder / "gaming.mkv"
    video.write_bytes(b"not a real video")
    store = SessionStore(folder)
    old = Session.new()
    old.status = "completed"
    old.ended_at = "2026-10-12T01:13:00+00:00"
    old.recording_path = str(video)
    old.bookmarks = [{"seconds": 25.5}, {"seconds": 5.25}, {"seconds": "invalid"}]
    store.save(old)
    store.sidecar(old)
    result = load_recordings(folder, video_folder)
    assert len(result) == 1
    assert result[0].bookmarks == (5.25, 25.5)
    assert result[0].available
    assert result[0].video == video
    assert result[0].session_file == store.path(old)
    assert result[0].estimated_duration is not None


def test_standalone_sidecar_and_moved_video(tmp_path):
    original = tmp_path / "old.mkv"
    path = tmp_path / "new.mkv"
    path.write_bytes(b"video")
    sidecar = tmp_path / "new.mkv.bookmarks.json"
    sidecar.write_text(json.dumps({
        "session_id": "x123", "started_at": "2026-10-01T11:00:00Z",
        "recording_path": str(original), "bookmarks": [{"seconds": 1.2}],
    }), encoding="utf-8")
    record = load_recordings(tmp_path / "no-sessions", tmp_path)[0]
    assert record.video == path
    assert record.bookmarks == (1.2,)


def test_missing_video_can_be_relinked(tmp_path):
    session = Session.new()
    session.recording_path = str(tmp_path / "missing.mkv")
    store = SessionStore(tmp_path / "sessions")
    store.save(session)
    result = load_recordings(store.root)
    assert len(result) == 1
    assert not result[0].available
    replacement = tmp_path / "replacement.mkv"
    replacement.write_bytes(b"video")
    relink_video(result[0], replacement)
    assert load_recordings(store.root)[0].video == replacement


def test_invalid_json_is_ignored(tmp_path):
    folder = tmp_path / "sessions"
    folder.mkdir()
    (folder / "corrupted.json").write_text("not json")
    (folder / "invalid.json").write_text(json.dumps({"bookmarks": []}))
    assert load_recordings(folder) == []


def test_clip_export_arguments_are_manual_and_non_destructive(tmp_path):
    source = tmp_path / "raw gameplay.mkv"
    output = tmp_path / "clip.mp4"
    args = export_arguments(source, output, 5.5, 22.75)
    assert args[-1] == str(output)
    assert args[args.index("-ss") + 1] == "5.500"
    assert args[args.index("-t") + 1] == "17.250"
    assert "0:a?" in args  # retain available audio tracks
    assert suggested_clip_name(source, 5.5, 22.75).endswith(".mp4")
    with pytest.raises(ValueError):
        export_arguments(source, source, 0, 10)
    with pytest.raises(ValueError):
        validate_range(15, 10, 20)
    with pytest.raises(ValueError):
        validate_range(10, 30, 25)
