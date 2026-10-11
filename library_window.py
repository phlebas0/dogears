"""On-demand recording library and manual clip editor.

Only instantiated when the user opens the library; none of the video decoding
or FFmpeg-export functionality is running during gameplay.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton,
    QSlider, QSplitter, QStyle, QStyleOptionSlider, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .core import timecode
from .library_core import (
    Recording, export_arguments, find_ffmpeg, load_recordings, parse_timecode,
    relink_video, suggested_clip_name, validate_range,
)


def compact_date(value: str) -> str:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.astimezone().strftime("%d %b %Y  %H:%M")
    except (ValueError, TypeError):
        return value[:19]


def readable_size(size: int | None) -> str:
    if size is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


class BookmarkTimeline(QSlider):
    """Seek bar with unobtrusive bookmark ticks and selected export range."""

    seek_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.bookmarks: tuple[float, ...] = ()
        self.clip_start: float | None = None
        self.clip_end: float | None = None
        self.setRange(0, 0)
        self.setMinimumHeight(35)
        self.setToolTip("Click or drag to seek; green marks indicate bookmarks")

    def set_marks(self, marks: tuple[float, ...]):
        self.bookmarks = marks
        self.update()

    def set_clip_range(self, start: float | None, end: float | None):
        self.clip_start, self.clip_end = start, end
        self.update()

    def _track(self) -> tuple[float, float, float]:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderGroove, self,
        )
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderHandle, self,
        )
        half = handle.width() / 2
        left = groove.left() + half
        right = max(left + 1, groove.right() - half)
        return left, right, groove.center().y()

    def _x(self, seconds: float, left: float, right: float) -> float:
        duration = self.maximum() / 1000
        return left + (right - left) * min(1.0, max(0.0, seconds / duration)) if duration else left

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.maximum() <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right, middle = self._track()
        if self.clip_start is not None and self.clip_end is not None:
            a, b = (self._x(t, left, right) for t in (self.clip_start, self.clip_end))
            painter.setPen(QPen(QColor(48, 129, 209, 170), 5))
            painter.drawLine(round(a), round(middle + 8), round(b), round(middle + 8))
            painter.setPen(QPen(QColor("#3181d1"), 2))
            for x in (a, b):
                painter.drawLine(round(x), round(middle - 11), round(x), round(middle + 11))
        painter.setPen(QPen(QColor("#20a477"), 2.5))
        for seconds in self.bookmarks:
            x = round(self._x(seconds, left, right))
            painter.drawLine(x, round(middle - 12), x, round(middle - 3))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            left, right, _ = self._track()
            fraction = max(0.0, min(1.0, (event.position().x() - left) / (right - left)))
            value = round(fraction * self.maximum())
            self.setValue(value)
            self.seek_requested.emit(value)
        super().mousePressEvent(event)


class LibraryWindow(QMainWindow):
    def __init__(self, sessions_folder: Path, recording_folder: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("dogears — recording library")
        self.resize(1120, 740)
        self.setMinimumSize(870, 590)
        self.sessions_folder = sessions_folder
        self.recording_folder = recording_folder
        self.entries: dict[str, Recording] = {}
        self.selected: Recording | None = None
        self.user_adjusted_range = False
        self.export_path: Path | None = None
        self.export_error = ""
        self._out_buffer = ""

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.player.setAudioOutput(self.audio)
        self.export_process = QProcess(self)
        self.export_process.readyReadStandardOutput.connect(self.on_export_output)
        self.export_process.readyReadStandardError.connect(self.on_export_error)
        self.export_process.finished.connect(self.export_finished)
        self.export_process.errorOccurred.connect(self.export_process_error)

        self.build_ui()
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.playbackStateChanged.connect(self.playback_changed)
        self.player.errorOccurred.connect(self.media_error)
        self.refresh()

    def build_ui(self):
        root = QWidget(self)
        outer = QVBoxLayout(root)
        heading = QHBoxLayout()
        title = QLabel("Recording library")
        title.setStyleSheet("font-size: 22px; font-weight: 600")
        heading.addWidget(title)
        heading.addStretch()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        heading.addWidget(self.refresh_button)
        self.open_folder_button = QPushButton("Open video folder")
        self.open_folder_button.clicked.connect(self.open_video_folder)
        heading.addWidget(self.open_folder_button)
        outer.addLayout(heading)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lyt_left = QVBoxLayout(left)
        lyt_left.setContentsMargins(0, 3, 4, 0)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Date", "Recording", "Length*", "Marks", "Size"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 146)
        self.table.setColumnWidth(2, 86)
        self.table.setColumnWidth(3, 58)
        self.table.setColumnWidth(4, 75)
        self.table.itemSelectionChanged.connect(self.choose_recording)
        lyt_left.addWidget(self.table)
        self.library_status = QLabel("* Length estimated from session timestamps; player reads the actual duration.")
        self.library_status.setWordWrap(True)
        self.library_status.setStyleSheet("color: #768092; font-size: 11px")
        lyt_left.addWidget(self.library_status)
        splitter.addWidget(left)

        right = QWidget()
        lyt_right = QVBoxLayout(right)
        lyt_right.setContentsMargins(6, 3, 0, 0)
        self.filename = QLabel("Select a recording")
        self.filename.setStyleSheet("font-weight: 600")
        self.filename.setWordWrap(True)
        lyt_right.addWidget(self.filename)
        self.video = QVideoWidget()
        self.video.setMinimumHeight(240)
        self.player.setVideoOutput(self.video)
        lyt_right.addWidget(self.video, 3)
        playback = QHBoxLayout()
        self.play = QPushButton("Play")
        self.play.clicked.connect(self.toggle_playback)
        self.play.setEnabled(False)
        playback.addWidget(self.play)
        self.position = QLabel("00:00:00.000 / 00:00:00.000")
        playback.addWidget(self.position)
        playback.addStretch()
        playback.addWidget(QLabel("Volume"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(90)
        self.volume.setValue(80)
        self.volume.valueChanged.connect(lambda value: self.audio.setVolume(value / 100))
        playback.addWidget(self.volume)
        lyt_right.addLayout(playback)
        self.timeline = BookmarkTimeline()
        self.timeline.seek_requested.connect(self.player.setPosition)
        self.timeline.sliderMoved.connect(self.player.setPosition)
        lyt_right.addWidget(self.timeline)
        lyt_right.addWidget(QLabel("Bookmarks (click to jump to 5 seconds before a mark)"))
        self.marks = QListWidget()
        self.marks.setMaximumHeight(105)
        self.marks.currentRowChanged.connect(self.jump_bookmark)
        lyt_right.addWidget(self.marks)

        editor = QFormLayout()
        start_row = QHBoxLayout()
        self.in_edit = QLineEdit("00:00:00.000")
        self.in_edit.setPlaceholderText("HH:MM:SS.mmm")
        self.in_edit.editingFinished.connect(self.range_edited)
        self.set_in = QPushButton("Set In")
        self.set_in.clicked.connect(self.mark_in)
        start_row.addWidget(self.in_edit)
        start_row.addWidget(self.set_in)
        editor.addRow("Clip start", start_row)
        end_row = QHBoxLayout()
        self.out_edit = QLineEdit("00:00:00.000")
        self.out_edit.setPlaceholderText("HH:MM:SS.mmm")
        self.out_edit.editingFinished.connect(self.range_edited)
        self.set_out = QPushButton("Set Out")
        self.set_out.clicked.connect(self.mark_out)
        end_row.addWidget(self.out_edit)
        end_row.addWidget(self.set_out)
        editor.addRow("Clip end", end_row)
        lyt_right.addLayout(editor)
        actions = QHBoxLayout()
        self.locate_button = QPushButton("Locate missing video…")
        self.locate_button.clicked.connect(self.relink)
        actions.addWidget(self.locate_button)
        actions.addStretch()
        self.cancel = QPushButton("Cancel export")
        self.cancel.clicked.connect(self.cancel_export)
        self.cancel.setVisible(False)
        actions.addWidget(self.cancel)
        self.export = QPushButton("Export selected clip…")
        self.export.clicked.connect(self.export_clip)
        actions.addWidget(self.export)
        lyt_right.addLayout(actions)
        self.detail_status = QLabel("Nothing is exported until you select a range and click Export.")
        self.detail_status.setWordWrap(True)
        self.detail_status.setStyleSheet("color: #768092")
        lyt_right.addWidget(self.detail_status)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        outer.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self.clear_media()

    def refresh(self):
        previous = self.selected.session_id if self.selected else None
        records = load_recordings(self.sessions_folder, self.recording_folder)
        self.entries = {r.session_id: r for r in records}
        self.table.blockSignals(True)
        self.table.setRowCount(len(records))
        reselect = -1
        for row, rec in enumerate(records):
            date = compact_date(rec.started_at)
            name = rec.video.name if rec.video else "Video path unavailable"
            if not rec.available:
                name += "  [missing]"
            dur = rec.estimated_duration
            values = [date, name, f"~{timecode(dur).split('.')[0]}" if dur is not None else "—",
                      str(len(rec.bookmarks)), readable_size(rec.file_size)]
            for col, label in enumerate(values):
                cell = QTableWidgetItem(label)
                cell.setData(Qt.ItemDataRole.UserRole, rec.session_id)
                if not rec.available:
                    cell.setForeground(QBrush(QColor("#b26b4a")))
                self.table.setItem(row, col, cell)
            if rec.session_id == previous:
                reselect = row
        if reselect >= 0:
            self.table.selectRow(reselect)
        elif records:
            self.table.selectRow(0)
        self.table.blockSignals(False)
        if records:
            self.choose_recording()
        else:
            self.clear_media()
        self.library_status.setText(
            f"{len(records)} session(s) · Missing videos can be relinked · * Length is approximate"
            if records else "No saved dogears sessions yet. Make a recording with F9, or check the OBS folder."
        )

    def clear_media(self):
        self.player.stop()
        self.player.setSource(QUrl())
        self.selected = None
        self.filename.setText("Select a recording")
        self.play.setEnabled(False)
        self.marks.clear()
        self.timeline.setRange(0, 0)
        self.timeline.set_marks(())
        self.timeline.set_clip_range(None, None)
        self.position.setText("00:00:00.000 / 00:00:00.000")
        self.in_edit.setText("00:00:00.000")
        self.out_edit.setText("00:00:00.000")
        self.export.setEnabled(False)
        self.set_in.setEnabled(False)
        self.set_out.setEnabled(False)
        self.locate_button.setEnabled(False)

    def choose_recording(self):
        items = self.table.selectedItems()
        if not items:
            self.clear_media()
            return
        rec = self.entries.get(items[0].data(Qt.ItemDataRole.UserRole))
        if not rec:
            self.clear_media()
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.selected = rec
        self.user_adjusted_range = False
        self.filename.setText(rec.video.name if rec.video else "Recording file not linked")
        self.filename.setToolTip(str(rec.video or ""))
        self.marks.clear()
        for index, seconds in enumerate(rec.bookmarks, 1):
            self.marks.addItem(f"{index:02d}    {timecode(seconds)}")
        self.timeline.setRange(0, 0)
        self.timeline.set_marks(rec.bookmarks)
        self.timeline.set_clip_range(None, None)
        self.position.setText("00:00:00.000 / 00:00:00.000")
        self.in_edit.setText("00:00:00.000")
        self.out_edit.setText("00:00:00.000")
        self.export.setEnabled(False)
        self.play.setEnabled(rec.available)
        self.set_in.setEnabled(rec.available)
        self.set_out.setEnabled(rec.available)
        self.locate_button.setEnabled(not rec.available)
        if rec.available:
            self.detail_status.setText("Set In / Out at the playhead, or enter timecodes, then export manually.")
            self.player.setSource(QUrl.fromLocalFile(str(rec.video)))
        else:
            self.detail_status.setText("Video missing or recording unfinished. Use Locate missing video to relink it.")

    def duration_changed(self, duration_ms: int):
        self.timeline.setMaximum(max(0, duration_ms))
        self.position_changed(self.player.position())
        if self.selected and duration_ms > 0 and not self.user_adjusted_range:
            self.in_edit.setText("00:00:00.000")
            self.out_edit.setText(timecode(duration_ms / 1000))
            self.timeline.set_clip_range(0, duration_ms / 1000)
        self.export.setEnabled(bool(self.selected and self.selected.available and duration_ms > 0))

    def position_changed(self, position_ms: int):
        self.position.setText(f"{timecode(position_ms / 1000)} / {timecode(self.player.duration() / 1000)}")
        if not self.timeline.isSliderDown():
            self.timeline.setValue(position_ms)

    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def playback_changed(self, state):
        self.play.setText("Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")

    def media_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.detail_status.setText(f"Playback error: {message}. Check the video's format and codec.")

    def jump_bookmark(self, index: int):
        if self.selected and 0 <= index < len(self.selected.bookmarks) and self.selected.available:
            target = max(0, self.selected.bookmarks[index] - 5)
            self.player.setPosition(round(target * 1000))

    def mark_in(self):
        self.in_edit.setText(timecode(self.player.position() / 1000))
        self.range_edited()

    def mark_out(self):
        self.out_edit.setText(timecode(self.player.position() / 1000))
        self.range_edited()

    def range_edited(self):
        self.user_adjusted_range = True
        try:
            start, end = parse_timecode(self.in_edit.text()), parse_timecode(self.out_edit.text())
            self.timeline.set_clip_range(start, end)
            self.detail_status.setText("Clip range selected. Export will create a separate MP4 file.")
        except ValueError:
            self.timeline.set_clip_range(None, None)
            self.detail_status.setText("Invalid timecode; use HH:MM:SS.mmm.")

    def open_video_folder(self):
        from PySide6.QtGui import QDesktopServices
        folder = self.recording_folder
        if self.selected and self.selected.available:
            folder = self.selected.video.parent
        if folder and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            QMessageBox.information(self, "dogears", "The OBS recording directory is not available. Connect to OBS and reopen the library.")

    def relink(self):
        if not self.selected:
            return
        initial = str(self.recording_folder or Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate recording", initial,
            "Videos (*.mkv *.mp4 *.mov *.avi *.webm);;All files (*)",
        )
        if not path:
            return
        try:
            relink_video(self.selected, Path(path))
            self.refresh()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Cannot link video", str(exc))

    def export_clip(self):
        rec = self.selected
        if not rec or not rec.available or self.export_process.state() != QProcess.ProcessState.NotRunning:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            QMessageBox.information(
                self, "FFmpeg required",
                "Export requires FFmpeg. Install FFmpeg on Windows and restart dogears, "
                "or place ffmpeg.exe in the project's tools folder.",
            )
            return
        try:
            start, end = parse_timecode(self.in_edit.text()), parse_timecode(self.out_edit.text())
            validate_range(start, end, self.player.duration() / 1000)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid clip range", str(exc))
            return
        default_file = rec.video.parent / suggested_clip_name(rec.video, start, end)
        filename, _ = QFileDialog.getSaveFileName(self, "Export clip as MP4", str(default_file), "MP4 video (*.mp4)")
        if not filename:
            return
        output = Path(filename)
        if output.suffix.lower() != ".mp4":
            output = output.with_suffix(".mp4")
        if output.exists():
            QMessageBox.warning(self, "File already exists", "Choose a new filename; dogears never overwrites an existing clip.")
            return
        try:
            arguments = export_arguments(rec.video, output, start, end)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot export", str(exc))
            return
        self.export_path = output
        self.export_error = ""
        self._out_buffer = ""
        self.export.setEnabled(False)
        self.table.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.cancel.setVisible(True)
        self.detail_status.setText("Exporting manually selected clip…")
        self.export_process.start(ffmpeg, arguments)

    def export_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.export_path = None
            self.cancel.setVisible(False)
            self.table.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.export.setEnabled(bool(self.selected and self.selected.available and self.player.duration() > 0))
            self.detail_status.setText("FFmpeg could not start. Check its installation and permissions.")

    def on_export_output(self):
        self._out_buffer += bytes(self.export_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        lines = self._out_buffer.split("\n")
        self._out_buffer = lines.pop()
        for line in lines:
            if line.startswith("out_time="):
                self.detail_status.setText(f"Exporting… processed {line.partition('=')[2].strip()} of clip")

    def on_export_error(self):
        self.export_error += bytes(self.export_process.readAllStandardError()).decode("utf-8", errors="replace")
        self.export_error = self.export_error[-4000:]

    def cancel_export(self):
        if self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.detail_status.setText("Cancelling export…")
            self.export_process.terminate()
            QTimer.singleShot(1500, self.kill_if_running)

    def kill_if_running(self):
        if self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.export_process.kill()

    def export_finished(self, exit_code: int, exit_status):
        self.on_export_output()
        self.on_export_error()
        success = (exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit
                   and self.export_path is not None and self.export_path.is_file())
        finished_path = self.export_path
        # Never delete a failed target automatically: another process could have
        # created it after the save dialogue, or the user may want to inspect it.
        self.export_path = None
        self.cancel.setVisible(False)
        self.table.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.export.setEnabled(bool(self.selected and self.selected.available and self.player.duration() > 0))
        if success:
            self.detail_status.setText(f"Clip saved: {finished_path}")
            QMessageBox.information(self, "Clip exported", f"Saved clip:\n{finished_path}")
        else:
            problem = self.export_error.strip() or "Export was cancelled or failed."
            self.detail_status.setText(f"Export did not complete: {problem[:200]}. Check the output folder for a partial file.")

    def closeEvent(self, event):
        if self.export_process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, "Export in progress", "Cancel or finish the export before closing the library.")
            event.ignore()
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        event.accept()
