# dogears v0.4.0 — bookmark previews and zoomable timeline

A lightweight Windows system-tray controller for OBS. **F9** starts/stops a full recording; **F8** bookmarks a moment. The recording library opens only when requested and lets you manually review and export clips. No automatic clipping, replay buffer, gameplay scanning, or accounts.

## What's new in v0.4.0

- **Restored bookmark markers:** the old native slider has been replaced with a custom-drawn timeline. Bookmark timestamps appear as prominent green flags in a separate lane above the blue clip selection. Click a flag to jump to the corresponding bookmark (with five seconds of preroll).
- **Bookmark thumbnails:** each row in the bookmark list contains a 160×90 preview frame from that part of the recorded video, alongside the timestamp. Frames are generated sequentially *only when opening a recording library*, with FFmpeg, and cached under `%LOCALAPPDATA%\dogears\thumbnails`. They are never generated while gaming; existing recordings work without migration. If FFmpeg is not installed or a particular frame cannot be decoded, the timecoded bookmark remains available with a placeholder.
- **Zoom and pan:** slider and +/- buttons give **1× to 32×** timeline zoom. Use the horizontal scrollbar below the timeline to view another section without changing the playback position. Blue handles, playhead and green bookmarks all stay aligned during zoom.
- **Button labels simplified:** both start and end controls are labelled **Set** beside their respective time fields. Keyboard shortcuts **I** / **O** still set In / Out at the current playhead position.
- The editor pane scrolls vertically when the window is small so clipping/export controls remain accessible.

## Features introduced in v0.3.0

- **Draggable blue In and Out handles** on the recording timeline. Click/drag elsewhere on the timeline to seek; green bookmark markers still show the moments saved during gameplay.
- While the **recording library** window is active, press **I** to set the clip start at the current playhead position and **O** to set the end. The two **Set** buttons (beside Clip start and Clip end) and typed timecodes also remain available. These are **window-local shortcuts**, not global recording hotkeys (F9 and F8 remain unchanged).
- **Multiple clip selections per recording**: set In/Out → **Add clip** → **New selection** → repeat. Click a saved clip to edit it by dragging the handles or using I/O; edits are saved immediately after releasing a handle. Remove or move clips up/down to control export order.
- Selections survive closing the library and restarting dogears. They're kept in `%APPDATA%\dogears\sessions\clip_projects`, separately from recording bookmarks.
- **Export clips…** offers two modes:
  - **Separate MP4 files**: choose a folder; create numbered files for all saved selections.
  - **Merge into one MP4**: choose a final path; dogears encodes the selections into a temporary directory and joins them **in list order** into one video, then cleans up the temporary clips.
- Exports are manual only. The original recording is never modified or overwritten. The library and FFmpeg are not needed while recording gameplay.

### Review and export workflow

1. Open **Recording library** and choose a recording.
2. Seek to the start of an interesting moment. Press **I**, or drag the left blue handle.
3. Seek to its end and press **O**, or drag the right handle. The two timecode boxes show precise In/Out positions.
4. Click **Add clip**. For additional clips, click **New selection**, choose a new range, then **Add clip** again.
5. Click clips in the list to review or adjust them. Move clips up or down if necessary.
6. Choose **Separate MP4 files** or **Merge into one MP4**, then click **Export clips…**. Exports run sequentially with progress and cancellation support.

**Scope:** Clips in one export are from the **currently selected recording**. Cross-recording montage projects are not yet supported. To export a range you must first add it to the clip list. Saved clips are held in local metadata, not pre-rendered video files.

## Requirements

- Windows 10/11, Python 3.11+ (Python 3.13 is supported by the installer).
- OBS Studio 28+ running, with a game/desktop source configured and **Tools → WebSocket Server Settings → Enable WebSocket Server** on port 4455; use your configured password.
- PySide6, obsws-python, keyring (installed by `setup_windows.bat`).
- **FFmpeg is required for clip export only**. The video library and recording work without it. Install with `winget install --id Gyan.FFmpeg -e` (restart dogears afterwards), or place `ffmpeg.exe` in a `tools` folder beside `main.py`. dogears does not bundle FFmpeg.

Qt Multimedia powers playback; codec support can depend on the media and Windows environment. OBS recordings in H.264 MKV/MP4 are the recommended starting point. If playback fails, try remuxing your recording to MP4 from OBS (**File → Remux Recordings**), without deleting the original.

## Installing or upgrading

1. **Quit dogears from its system-tray menu.** Keep a copy of your working v0.1 project or commit first.
2. Copy the v0.4.0 source files into your existing dogears project folder (the one containing `main.py`). Don't delete its `.git` folder. Existing settings and recordings are stored separately in `%APPDATA%\dogears` and in your OBS video folder.
3. Double-click `setup_windows.bat` (reuses your local `.venv` or creates it). It installs/updates the dependencies.
4. Start OBS, then double-click `run_windows.bat`. Use `run_debug_windows.bat` if an error needs diagnosing.
5. In dogears, click **Recording library**. Right-clicking the tray icon also provides this menu item.
6. Select a session, play it, click bookmarks, choose ranges with I/O or the handles, **Add clip**, and use **Export clips…**.

The status beside each session indicates if its video is missing. Click **Locate missing video** if you've moved the file. The library shows sessions recorded with dogears, or standalone `*.bookmarks.json` sidecars found directly inside the current OBS output directory. It does *not* ingest unrelated OBS recordings lacking a dogears session.

## Recording configuration

OBS continues to handle capture, encoding, output quality, folder and audio tracks. In OBS **Settings → Output → Recording**, choose a recording folder, compatible hardware encoder (NVENC / AMD AMF / Intel QSV, if available) and preferably MKV for crash resilience. dogears connects to OBS using its local WebSocket service.

Recording hotkeys default to **F9** (toggle) and **F8** (bookmark). You can customise these and adjust or mute the start/bookmark/stop sounds under dogears **Settings**. Closing the main window leaves the tray and hotkeys running; right-click the tray icon and choose **Quit** to fully exit.

If you record with multiple OBS audio tracks, the manual FFmpeg export maps all available audio tracks into the exported MP4. Each clip is re-encoded to H.264 / AAC for accurate cuts, so exporting takes CPU time. Merge mode joins these consistently encoded pieces using FFmpeg without re-encoding the concatenation. The app shows sequential export progress and allows cancellation. Cancelled/failed separate exports may leave already completed files; merge mode cleans up its own temporary segments. No exporting happens during gameplay.

## Storage and privacy

- Original footage: OBS configured output directory.
- Session JSON (saved immediately at each bookmark): `%APPDATA%\dogears\sessions\<session-id>.json`.
- Clip project JSON (saved on edits): `%APPDATA%\dogears\sessions\clip_projects\<hashed-session-id>.json`.
- Sidecar (written after normal stop): `<video-name>.bookmarks.json`, alongside the original recording.
- Settings: `%APPDATA%\dogears\settings.json`.
- OBS WebSocket password: Windows Credential Manager (`keyring`), not included in settings JSON.
- Exports: wherever you choose in the save dialogue. Existing files are never overwritten.

Existing v0.1 and v0.2 sessions and settings are preserved. No migration or database is necessary.

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

- **Windows GUI not yet physically tested for v0.4:** automated tests cover clip persistence, export arguments, zoom calculations, thumbnail extraction and an actual FFmpeg merge, but the Qt timeline, image list and scrollbar still need testing on your Windows machine. The supplied release is a development prototype, not a polished production build.
- Current implementation only tracks recording sessions it started. If OBS is stopped manually outside dogears, the JSON remains but the final video may need manual relinking.
- The list shows session duration estimated from start/stop timestamps, which can differ from the recorded media duration; the selected player's timeline uses its actual duration.
- Preview functionality requires codecs supported by Qt Multimedia in your environment.
- If hotkeys conflict with a game/other software, change them in Settings. Global keys may not work in elevated or certain anti-cheat-protected games.
- FFmpeg is not needed while recording, is not launched in the background, and never modifies the original recording.
- Notification sounds may be captured if OBS records desktop audio.
- FFmpeg exports use H.264/AAC and can take some time for long clips; FFmpeg itself must be separately installed.
