import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dogears.core import (
    Config, RecorderBackend, RecorderError, SessionStore,
    hotkey_parts, timecode,
)


class FakeObs:
    recording = False
    duration = 0
    paused = False
    output_path = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def get_version(self):
        return SimpleNamespace(obs_version="32.0")

    def get_record_directory(self):
        return SimpleNamespace(record_directory="C:/Videos")

    def get_record_status(self):
        return SimpleNamespace(output_active=self.recording, output_duration=self.duration,
                               output_paused=self.paused)

    def start_record(self):
        self.recording = True

    def stop_record(self):
        self.recording = False
        return SimpleNamespace(output_path=self.output_path)


@pytest.fixture
def setup_backend(tmp_path):
    obs = FakeObs()
    obs.output_path = str(tmp_path / "recording.mkv")
    backend = RecorderBackend(SessionStore(tmp_path / "sessions"), lambda: obs)
    return backend, obs, tmp_path


def test_hotkeys():
    assert hotkey_parts("F9") == (0, 0x78)
    assert hotkey_parts("Ctrl+Alt+B") == (3, ord("B"))
    with pytest.raises(ValueError):
        hotkey_parts("B")
    with pytest.raises(ValueError):
        Config(record_hotkey="F8", bookmark_hotkey="F8").validate()


def test_timecode():
    assert timecode(61.235) == "00:01:01.235"
    assert timecode(3600) == "01:00:00.000"


def test_recording_and_durable_bookmarks(setup_backend):
    backend, obs, root = setup_backend
    assert backend.start()["event"] == "started"
    obs.duration = 85230
    mark = backend.bookmark()["bookmark"]
    assert mark["timecode"] == "00:01:25.230"
    session_files = list((root / "sessions").glob("*.json"))
    assert len(session_files) == 1
    assert json.loads(session_files[0].read_text())["bookmarks"][0]["seconds"] == 85.23
    saved = backend.stop()
    assert saved["event"] == "stopped"
    assert saved["count"] == 1
    assert json.loads(session_files[0].read_text())["status"] == "completed"
    assert backend.active is None
    sidecar = root / "recording.mkv.bookmarks.json"
    assert json.loads(sidecar.read_text())["bookmarks"][0]["timecode"] == "00:01:25.230"


def test_never_take_over_external_recording(setup_backend):
    backend, obs, _ = setup_backend
    obs.recording = True
    with pytest.raises(RecorderError, match="independently"):
        backend.start()


def test_no_bookmark_while_idle(setup_backend):
    backend, *_ = setup_backend
    with pytest.raises(RecorderError, match="Start a recording"):
        backend.bookmark()
