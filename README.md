# dogears v0.2.0 — manual gameplay bookmarks and clip library

A lightweight Windows system-tray controller for OBS. **F9** starts/stops a full recording; **F8** bookmarks a moment. The recording library opens only when requested and lets you manually review and export clips. No automatic clipping, replay buffer, gameplay scanning, or accounts.

## What's new in v0.2.0

- Recording library listing existing dogears sessions (date, approximate duration, bookmark count, file size).
- Embedded video player for recorded MKV/MP4 files, with seekable timeline and green bookmark ticks.
- Click a bookmark to jump to five seconds before the moment.
- **Set In** and **Set Out** at the playback cursor, or enter timecodes in `HH:MM:SS.mmm` format.
- **Export selected clip** to a new MP4, only when you click the export button. Existing video is never modified or overwritten. Exports include the video and all available audio tracks.
- Missing-file relinking for moved recordings; existing v0.1 bookmark JSON works as-is.
- Library loads on demand; no video decoder or FFmpeg process runs during gameplay.

## Requirements

- Windows 10/11, Python 3.11+ (Python 3.13 is supported by the installer).
- OBS Studio 28+ running, with a game/desktop source configured and **Tools → WebSocket Server Settings → Enable WebSocket Server** on port 4455; use your configured password.
- PySide6, obsws-python, keyring (installed by `setup_windows.bat`).
- **FFmpeg is required for clip export only**. The video library and recording work without it. Install with `winget install --id Gyan.FFmpeg -e` (restart dogears afterwards), or place `ffmpeg.exe` in a `tools` folder beside `main.py`. dogears does not bundle FFmpeg.

Qt Multimedia powers playback; codec support can depend on the media and Windows environment. OBS recordings in H.264 MKV/MP4 are the recommended starting point. If playback fails, try remuxing your recording to MP4 from OBS (**File → Remux Recordings**), without deleting the original.

## Installing or upgrading

1. **Quit dogears from its system-tray menu.** Keep a copy of your working v0.1 project or commit first.
2. Copy the v0.2.0 source files into your existing dogears project folder (the one containing `main.py`). Don't delete its `.git` folder. Existing settings and recordings are stored separately in `%APPDATA%\dogears` and in your OBS video folder.
3. Double-click `setup_windows.bat` (reuses your local `.venv` or creates it). It installs/updates the dependencies.
4. Start OBS, then double-click `run_windows.bat`. Use `run_debug_windows.bat` if an error needs diagnosing.
5. In dogears, click **Recording library**. Right-clicking the tray icon also provides this menu item.
6. Select a session, play it, click bookmarks, set the In/Out points and click **Export selected clip…**.

The status beside each session indicates if its video is missing. Click **Locate missing video** if you've moved the file. The library shows sessions recorded with dogears, or standalone `*.bookmarks.json` sidecars found directly inside the current OBS output directory. It does *not* ingest unrelated OBS recordings lacking a dogears session.

## Recording configuration

OBS continues to handle capture, encoding, output quality, folder and audio tracks. In OBS **Settings → Output → Recording**, choose a recording folder, compatible hardware encoder (NVENC / AMD AMF / Intel QSV, if available) and preferably MKV for crash resilience. dogears connects to OBS using its local WebSocket service.

Recording hotkeys default to **F9** (toggle) and **F8** (bookmark). You can customise these and adjust or mute the start/bookmark/stop sounds under dogears **Settings**. Closing the main window leaves the tray and hotkeys running; right-click the tray icon and choose **Quit** to fully exit.

If you record with multiple OBS audio tracks, the manual FFmpeg export maps all audio tracks into the exported MP4. Export is a re-encode using H.264 / AAC for accurate cuts, so exporting a clip takes time and uses CPU. The app shows export progress and allows cancellation. No exporting happens during gameplay unless you explicitly trigger it.

## Storage and privacy

- Original footage: OBS configured output directory.
- Session JSON (saved immediately at each bookmark): `%APPDATA%\dogears\sessions\<session-id>.json`.
- Sidecar (written after normal stop): `<video-name>.bookmarks.json`, alongside the original recording.
- Settings: `%APPDATA%\dogears\settings.json`.
- OBS WebSocket password: Windows Credential Manager (`keyring`), not included in settings JSON.
- Exports: wherever you choose in the save dialogue. Existing files are never overwritten.

Version 0.1 sessions and settings are preserved. No migration or database is necessary.

## Development

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe main.py
```

Pure recording and library helpers are isolated in `dogears/core.py` and `dogears/library_core.py`. The recording/tray app is `dogears/app.py`; the lazily-created player, timeline and exporter are `dogears/library_window.py`.

## Known limitations and safeguards

- **Windows GUI not yet physically tested:** the automated tests validate backend, sidecar indexing, ranges and export arguments; you should test the actual PySide6 player/OBS integration on Windows with a short recording before relying on it. The supplied release is a development prototype, not a polished production build.
- Current implementation only tracks recording sessions it started. If OBS is stopped manually outside dogears, the JSON remains but the final video may need manual relinking.
- The list shows session duration estimated from start/stop timestamps, which can differ from the recorded media duration; the selected player's timeline uses its actual duration.
- Preview functionality requires codecs supported by Qt Multimedia in your environment.
- If hotkeys conflict with a game/other software, change them in Settings. Global keys may not work in elevated or certain anti-cheat-protected games.
- FFmpeg is not needed while recording, is not launched in the background, and never modifies the original recording.
- Notification sounds may be captured if OBS records desktop audio.
- FFmpeg exports use H.264/AAC and can take some time for long clips; FFmpeg itself must be separately installed.
