# dogears v0.5.3

A minimal OBS-backed gameplay recorder and manual clip editor for Windows. **F9** starts/stops a recording, and **F8** adds bookmarks. Nothing is automatically clipped or analysed. The recording library only loads when requested.

## v0.5.3 changes

- **Double-click a bookmark to play:** single-click seeks to five seconds before the mark; double-click begins playing from there. This works in the bookmark list and on the green bookmark icons in the timeline. Playback continues until paused (a bookmark is not an automatic clip).
- **Clips moved to a right sidebar:** recording sessions stay on the left, the video/timeline/bookmark list in the centre, and clips plus export controls on the right. The resizable splitter lets you adjust pane widths.
- **Thumbnail fix for Windows:** FFmpeg now streams each preview as JPEG bytes to the application. Qt displays the frame and optionally caches it under `%LOCALAPPDATA%\dogears\thumbnails`; FFmpeg no longer attempts to write thumbnail files into that directory. If caching fails, the image still displays. The preview status will report any decoding/cache failure.

No gameplay capture controls or video/clip export formats were changed.

## Recording and editing

1. Install OBS Studio and enable **Tools → WebSocket Server Settings → Enable WebSocket Server** on port **4455**. Configure a game-capture source and recording folder in OBS. OBS must be running for recording.
2. Run `setup_windows.bat` once, then `run_windows.bat`. Configure the OBS WebSocket password in dogears.
3. While gaming, press **F9** to start, **F8** to bookmark, and **F9** again to finish. Hotkeys and sound volume are configurable in Settings.
4. Open **Recording library**. Choose a recording; single-click a bookmark to seek or double-click to start playing from its five-second lead-in.
5. Drag the **blue handles** to select the clip start and end. A labelled ruler, zoom controls (Fit through 32×) and horizontal scrolling help position the handles precisely.
6. Click **Play clip** to review the selection, then **Add clip**. For more clips, click **New selection**, drag the handles and click Add clip again.
7. Manage clips in the right sidebar; reorder with Move up/down, then export **Separate MP4 files** or **Merge into one MP4**. Source recordings are never modified. Selections survive restarting the app.

## Requirements

- Windows 10 or 11; Python 3.11+ (including Python 3.13).
- OBS Studio 28+ with the WebSocket server enabled.
- PySide6, `obsws-python`, `keyring`: installed by `setup_windows.bat`.
- FFmpeg for thumbnails and manual clip export. Install using `winget install --id Gyan.FFmpeg -e`, restart dogears afterwards, or put `ffmpeg.exe` in the project's `tools` folder. dogears does not bundle FFmpeg.

**If thumbnails are blank**, first check the **Previews (bookmarks + clips)** status under the bookmark list. FFmpeg now streams JPEG frames directly to the application, so it should not need write access to the cache folder. The status reports when FFmpeg is missing or decoding fails. You can also run `ffmpeg -version` in PowerShell to check whether the executable is available on PATH. Note that FFmpeg being available does not guarantee every input codec is decodable.

**If the video player cannot play the recording**, inspect the playback error displayed beneath the clip controls. Qt Multimedia playback and FFmpeg thumbnail generation are separate; try recording in OBS with H.264 and MKV/MP4, or remux the recording to MP4 via **OBS → File → Remux Recordings**. Do not delete your original video.

## Files and privacy

- OBS video files: OBS's configured recording directory.
- Immediate bookmark/session metadata: `%APPDATA%\dogears\sessions\<session-id>.json`.
- Saved clip selections: `%APPDATA%\dogears\sessions\clip_projects\<hashed-session-id>.json`.
- Extra bookmark sidecar: `<video-name>.bookmarks.json`, written at the end of a completed recording.
- Thumbnail cache: `%LOCALAPPDATA%\dogears\thumbnails` (can be deleted; recreated when needed).
- Settings: `%APPDATA%\dogears\settings.json`.
- OBS credentials: Windows Credential Manager via keyring.

Nothing uploads recordings, and no image/video decoding or export jobs run while the recording library is closed.

## Git update (from v0.5.2)

Quit dogears from the tray, then apply the patch **from the repository root (the directory containing `main.py`)**:

```powershell
cd C:\Users\cmpbl\OneDrive\projects\dogears
git apply --check "$env:USERPROFILE\Downloads\dogears-v0.5.3.patch"
git apply "$env:USERPROFILE\Downloads\dogears-v0.5.3.patch"
.\run_windows.bat
```

Keep v0.5.3 on your feature branch until you have tested Windows playback and both thumbnail lists.

## Tests

```powershell
.venv\Scripts\python.exe -m pip install pytest
.venv\Scripts\python.exe -m pytest -q
```

The backend tests include real FFmpeg streaming of bookmark and clip preview frames. The Windows Qt GUI itself still requires physical testing on Windows.
