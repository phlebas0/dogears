"""Recording-library indexing and manual-export helpers (no Qt or OBS dependency)."""
from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .core import atomic_json, timecode


@dataclass(frozen=True)
class Recording:
    session_id: str
    session_file: Path
    video: Path | None
    started_at: str
    ended_at: str | None
    status: str
    bookmarks: tuple[float, ...]
    file_size: int | None

    @property
    def available(self) -> bool:
        return self.video is not None and self.video.is_file()

    @property
    def estimated_duration(self) -> float | None:
        """Only a fallback estimate, not the media player's measured duration."""
        if not self.ended_at:
            return None
        try:
            start = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
            return max(0.0, (end - start).total_seconds())
        except (TypeError, ValueError):
            return None


def load_recordings(sessions_folder: Path, recording_folder: Path | None = None) -> list[Recording]:
    """Load durable AppData sessions and any standalone sidecars in the OBS folder.

    Sessions saved in AppData take priority over duplicate sidecars. Corrupt files
    and unsupported records are ignored, not allowed to prevent loading others.
    The OBS folder scan is shallow: no recursive video crawl during gameplay.
    """
    candidates = sorted(sessions_folder.glob("*.json")) if sessions_folder.is_dir() else []
    if recording_folder and recording_folder.is_dir():
        try:
            candidates.extend(sorted(recording_folder.glob("*.bookmarks.json")))
        except OSError:
            pass

    found: dict[str, Recording] = {}
    for source in candidates:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            session_id = payload.get("session_id")
            started_at = payload.get("started_at")
            if not isinstance(session_id, str) or not session_id or not isinstance(started_at, str):
                continue
            if session_id in found:
                continue
            video_value = payload.get("recording_path")
            video = Path(video_value) if isinstance(video_value, str) and video_value else None
            # A sidecar can be moved together with its video; prefer the local video.
            if source.name.endswith(".bookmarks.json"):
                beside = source.with_name(source.name[: -len(".bookmarks.json")])
                if beside.is_file() and (video is None or not video.is_file()):
                    video = beside
            raw_marks = payload.get("bookmarks", [])
            marks: list[float] = []
            if isinstance(raw_marks, list):
                for mark in raw_marks:
                    if isinstance(mark, dict):
                        try:
                            seconds = float(mark.get("seconds", -1))
                            if math.isfinite(seconds) and seconds >= 0:
                                marks.append(seconds)
                        except (TypeError, ValueError):
                            continue
            marks.sort()
            try:
                size = video.stat().st_size if video and video.is_file() else None
            except OSError:
                size = None
            found[session_id] = Recording(
                session_id=session_id, session_file=source, video=video,
                started_at=started_at, ended_at=payload.get("ended_at"),
                status=str(payload.get("status", "unknown")), bookmarks=tuple(marks), file_size=size,
            )
        except (OSError, ValueError, TypeError, UnicodeError):
            continue
    return sorted(found.values(), key=lambda record: record.started_at, reverse=True)


def relink_video(record: Recording, video: Path) -> None:
    """Update this record's durable metadata, never touch the media itself."""
    if not video.is_file():
        raise ValueError("The selected recording does not exist.")
    payload = json.loads(record.session_file.read_text(encoding="utf-8"))
    payload["recording_path"] = str(video.resolve())
    atomic_json(record.session_file, payload)


def parse_timecode(value: str) -> float:
    """Accept H:MM:SS.mmm, MM:SS.mmm or decimal seconds."""
    parts = value.strip().split(":")
    if not parts or len(parts) > 3:
        raise ValueError("Use HH:MM:SS.mmm (or seconds).")
    try:
        if len(parts) == 1:
            result = float(parts[0])
        else:
            seconds = float(parts[-1])
            minutes = int(parts[-2])
            hours = int(parts[0]) if len(parts) == 3 else 0
            if seconds >= 60 or minutes >= 60 or min(seconds, minutes, hours) < 0:
                raise ValueError
            result = hours * 3600 + minutes * 60 + seconds
        if not math.isfinite(result) or result < 0:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid timecode. Use HH:MM:SS.mmm (or seconds).") from exc


def validate_range(start: float, end: float, duration: float) -> None:
    if not all(math.isfinite(x) for x in (start, end, duration)) or duration <= 0:
        raise ValueError("The video duration is not available yet.")
    if start < 0 or end <= start + 0.099 or end > duration + 0.25:
        raise ValueError("Choose a clip of at least 0.1 seconds within the video duration.")


def find_ffmpeg() -> str | None:
    """Find FFmpeg on PATH or in a local tools folder beside the project."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    local = Path(__file__).resolve().parent.parent / "tools" / "ffmpeg.exe"
    return str(local) if local.is_file() else None


def export_arguments(video: Path, destination: Path, start: float, end: float) -> list[str]:
    """FFmpeg arguments for accurate, manual MP4 extraction (not auto-clipping).

    Input seeking, followed by encoding, permits accurate cuts without decoding
    a multi-hour video from its beginning. Includes all existing audio tracks.
    """
    validate_range(start, end, max(end, 0.1))
    if video.resolve() == destination.resolve():
        raise ValueError("Choose a different output file; the original must be preserved.")
    return [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1",
        "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end-start:.3f}",
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast",
        "-crf", "19", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-n", str(destination),
    ]


def suggested_clip_name(video: Path, start: float, end: float) -> str:
    def slug(n: float) -> str:
        return timecode(n).split(".")[0].replace(":", "-")
    return f"{video.stem}_clip_{slug(start)}_{slug(end)}.mp4"
