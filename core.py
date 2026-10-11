"""Settings, durable bookmark files and the small OBS recording controller."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class RecorderError(RuntimeError):
    """User-facing failure, normally OBS not connected or recording unavailable."""


def data_dir() -> Path:
    base = os.getenv("APPDATA")
    path = (Path(base) / "dogears") if base else (Path.home() / ".dogears")
    # Upgrade from the early GameMarker prototype without deleting its data.
    legacy = (Path(base) / "GameMarker") if base else (Path.home() / ".game-marker")
    if not path.exists() and legacy.is_dir():
        shutil.copytree(legacy, path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def hotkey_parts(shortcut: str) -> tuple[int, int]:
    """Return Windows MOD flags and VK code for a supported global hotkey."""
    parts = [p.strip().upper() for p in shortcut.split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError(f"Invalid hotkey: {shortcut}")
    mods = 0
    mod_values = {"CTRL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004}
    for mod in parts[:-1]:
        if mod not in mod_values or mods & mod_values[mod]:
            raise ValueError(f"Invalid hotkey modifier: {shortcut}")
        mods |= mod_values[mod]
    key = parts[-1]
    match = re.fullmatch(r"F([1-9]|1[01])", key)
    if match:
        vk = 0x70 + int(match.group(1)) - 1
    elif re.fullmatch(r"[A-Z]", key) and mods:
        vk = ord(key)
    else:
        raise ValueError(f"Unsupported hotkey: {shortcut}")
    return mods, vk


HOTKEY_OPTIONS = (
    [f"F{i}" for i in range(1, 12)]
    + [f"Ctrl+Alt+{chr(i)}" for i in range(ord("A"), ord("Z") + 1)]
    + [f"Ctrl+Shift+{chr(i)}" for i in range(ord("A"), ord("Z") + 1)]
)


@dataclass
class Config:
    record_hotkey: str = "F9"
    bookmark_hotkey: str = "F8"
    sounds_enabled: bool = True
    volume: int = 40
    obs_port: int = 4455

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
            cfg.validate()
            return cfg
        except (OSError, ValueError, TypeError):
            return cls()

    def validate(self) -> None:
        hotkey_parts(self.record_hotkey)
        hotkey_parts(self.bookmark_hotkey)
        if self.record_hotkey.lower() == self.bookmark_hotkey.lower():
            raise ValueError("Recording and bookmarking must have different hotkeys.")
        if not (0 <= int(self.volume) <= 100):
            raise ValueError("Sound volume must be between 0 and 100.")
        if not (1 <= int(self.obs_port) <= 65535):
            raise ValueError("OBS port must be between 1 and 65535.")

    def save(self, path: Path) -> None:
        self.validate()
        atomic_json(path, asdict(self))


def atomic_json(path: Path, content: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(content, fp, indent=2, ensure_ascii=False)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timecode(seconds: float) -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, left = divmod(milliseconds, 3_600_000)
    minutes, left = divmod(left, 60_000)
    secs, ms = divmod(left, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


@dataclass
class Session:
    session_id: str
    started_at: str
    status: str = "recording"
    ended_at: str | None = None
    recording_path: str | None = None
    bookmarks: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = 1

    @classmethod
    def new(cls) -> Session:
        return cls(session_id=uuid4().hex, started_at=utc_now())


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session: Session) -> Path:
        return self.root / f"{session.session_id}.json"

    def save(self, session: Session) -> Path:
        destination = self.path(session)
        atomic_json(destination, asdict(session))
        return destination

    def sidecar(self, session: Session) -> tuple[Path | None, str | None]:
        """Best-effort copy next to finished video; AppData always keeps the source."""
        if not session.recording_path:
            return None, "OBS did not provide the saved video path. Session data remains in the app folder."
        movie = Path(session.recording_path)
        sidecar = movie.with_name(movie.name + ".bookmarks.json")
        try:
            atomic_json(sidecar, asdict(session))
            return sidecar, None
        except OSError as exc:
            return None, f"Could not write next to the video ({exc}). Bookmarks remain in the app folder."


class RecorderBackend:
    """Run methods serially in one worker; never run OBS requests on the Qt UI thread."""

    def __init__(
        self,
        store: SessionStore,
        client_factory: Callable[[], Any],
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.store = store
        self.client_factory = client_factory
        self.monotonic = monotonic
        self.active: Session | None = None
        self.started_monotonic: float | None = None

    def check(self) -> dict[str, Any]:
        try:
            with self.client_factory() as obs:
                version = obs.get_version().obs_version
                active = bool(obs.get_record_status().output_active)
                folder = obs.get_record_directory().record_directory
                return {"version": version, "externally_recording": active, "folder": folder}
        except Exception as exc:
            raise RecorderError(f"Cannot connect to OBS. Check that OBS is running and its WebSocket server is enabled. Detail: {exc}") from exc

    def toggle(self) -> dict[str, Any]:
        return self.stop() if self.active else self.start()

    def start(self) -> dict[str, Any]:
        if self.active is not None:
            raise RecorderError("A recording is already in progress.")
        try:
            with self.client_factory() as obs:
                if obs.get_record_status().output_active:
                    raise RecorderError("OBS is already recording independently. Stop that recording in OBS first.")
                obs.start_record()
                self.started_monotonic = self.monotonic()
                candidate = Session.new()
                try:
                    self.store.save(candidate)
                except Exception:
                    obs.stop_record()  # Do not leave an untracked recording running.
                    raise
                self.active = candidate
        except RecorderError:
            raise
        except Exception as exc:
            raise RecorderError(f"Could not start recording: {exc}") from exc
        return {"event": "started", "session_id": self.active.session_id}

    def bookmark(self) -> dict[str, Any]:
        session = self.active
        if session is None:
            raise RecorderError("Start a recording before adding bookmarks.")
        pressed_time = self.monotonic()
        try:
            with self.client_factory() as obs:
                state = obs.get_record_status()
                if not state.output_active:
                    session.status = "interrupted"
                    self.store.save(session)
                    self.active = None
                    self.started_monotonic = None
                    raise RecorderError("OBS recording stopped outside this application. Bookmarks were retained in the app folder.")
                if getattr(state, "output_paused", False):
                    raise RecorderError("Recording is paused in OBS; resume it before bookmarking.")
                millis = float(getattr(state, "output_duration", 0) or 0)
                estimated = max(0.0, pressed_time - (self.started_monotonic or pressed_time))
                # OBS occasionally reports zero duration for affected output types.
                seconds = millis / 1000 if millis > 0 or estimated < 2 else estimated
                source = "obs_duration" if millis > 0 or estimated < 2 else "local_clock_fallback"
                mark = {
                    "seconds": round(seconds, 3),
                    "timecode": timecode(seconds),
                    "created_at": utc_now(),
                    "timestamp_source": source,
                }
                session.bookmarks.append(mark)
                try:
                    self.store.save(session)
                except Exception:
                    session.bookmarks.pop()
                    raise
                return {"event": "bookmarked", "bookmark": mark, "count": len(session.bookmarks)}
        except RecorderError:
            raise
        except Exception as exc:
            raise RecorderError(f"Could not save bookmark: {exc}") from exc

    def stop(self) -> dict[str, Any]:
        session = self.active
        if session is None:
            raise RecorderError("There is no recording to stop.")
        try:
            with self.client_factory() as obs:
                if not obs.get_record_status().output_active:
                    session.status = "interrupted"
                    self.store.save(session)
                    self.active = None
                    self.started_monotonic = None
                    raise RecorderError("OBS has already stopped recording. Bookmarks remain in the app folder, but the video needs to be matched manually.")
                result = obs.stop_record()
                session.ended_at = utc_now()
                session.recording_path = getattr(result, "output_path", None)
                session.status = "completed"
                self.store.save(session)
                sidecar_path, warning = self.store.sidecar(session)
                self.active = None
                self.started_monotonic = None
                return {
                    "event": "stopped",
                    "path": session.recording_path,
                    "sidecar": str(sidecar_path) if sidecar_path else None,
                    "warning": warning,
                    "count": len(session.bookmarks),
                }
        except RecorderError:
            raise
        except Exception as exc:
            # Keep in-memory session intact so user can retry/recover.
            raise RecorderError(f"Could not finish recording cleanly: {exc}. Check OBS before retrying.") from exc
