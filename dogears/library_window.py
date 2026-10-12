"""On-demand recording library and manual clip editor.

Only instantiated when the user opens the library; none of the video decoding
or FFmpeg-export functionality is running during gameplay.
"""
from __future__ import annotations

from datetime import datetime
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QShortcut, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton, QComboBox,
    QSlider, QSplitter, QStyle, QStyleOptionSlider, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .core import timecode
from .library_core import (
    Recording, export_arguments, find_ffmpeg, load_recordings, parse_timecode,
    relink_video, validate_range, load_clip_ranges, save_clip_ranges,
    separate_clip_destinations, concat_manifest, concat_arguments,
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
    """Seek bar with bookmark ticks and two independently draggable clip handles."""

    seek_requested = Signal(int)
    range_dragged = Signal(float, float)
    range_drag_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.bookmarks: tuple[float, ...] = ()
        self.clip_start: float | None = None
        self.clip_end: float | None = None
        self._drag_handle: str | None = None
        self._seek_dragging = False
        self.setRange(0, 0)
        self.setMinimumHeight(48)
        self.setMouseTracking(True)
        self.setToolTip('Drag blue In/Out handles; click elsewhere to seek. Green ticks are bookmarks.')

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

    def _seconds_at(self, x: float) -> float:
        left, right, _ = self._track()
        fraction = max(0.0, min(1.0, (x - left) / (right - left)))
        return round(fraction * self.maximum()) / 1000

    def _handle_at(self, x: float, y: float) -> str | None:
        if self.maximum() <= 0 or self.clip_start is None or self.clip_end is None:
            return None
        left, right, mid = self._track()
        # Reserve a taller hit target than the drawn handle, including at 0/duration.
        if abs(y - mid) > 22:
            return None
        start_dist = abs(x - self._x(self.clip_start, left, right))
        end_dist = abs(x - self._x(self.clip_end, left, right))
        closest = min(start_dist, end_dist)
        if closest > 11:
            return None
        return 'in' if start_dist <= end_dist else 'out'

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.maximum() <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right, mid = self._track()
        if self.clip_start is not None and self.clip_end is not None:
            a = self._x(self.clip_start, left, right)
            b = self._x(self.clip_end, left, right)
            # Wide blue range with grip handles distinguishable from the seek thumb.
            painter.setPen(QPen(QColor(54, 134, 211, 190), 6))
            painter.drawLine(round(a), round(mid + 10), round(b), round(mid + 10))
            for x in (a, b):
                painter.setPen(QPen(QColor('#195e9b'), 1))
                painter.setBrush(QBrush(QColor('#66bbf1')))
                painter.drawRoundedRect(round(x) - 5, round(mid) - 16, 10, 32, 3, 3)
        painter.setPen(QPen(QColor('#20a477'), 2.5))
        for seconds in self.bookmarks:
            x = round(self._x(seconds, left, right))
            painter.drawLine(x, round(mid - 20), x, round(mid - 9))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            handle = self._handle_at(event.position().x(), event.position().y())
            if handle:
                self._drag_handle = handle
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                event.accept()
                return
            value = round(self._seconds_at(event.position().x()) * 1000)
            self._seek_dragging = True
            self.setSliderDown(True)
            self.setValue(value)
            self.seek_requested.emit(value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_handle is not None:
            position = self._seconds_at(event.position().x())
            start, end = self.clip_start, self.clip_end
            minimum = 0.1
            duration = self.maximum() / 1000
            if self._drag_handle == 'in':
                start = min(max(0, position), end - minimum)
            else:
                end = max(min(duration, position), start + minimum)
            self.set_clip_range(start, end)
            self.range_dragged.emit(start, end)
            event.accept()
            return
        if self._seek_dragging:
            value = round(self._seconds_at(event.position().x()) * 1000)
            self.setValue(value)
            self.seek_requested.emit(value)
            event.accept()
            return
        over = self._handle_at(event.position().x(), event.position().y())
        self.setCursor(Qt.CursorShape.SizeHorCursor if over else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_handle is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_handle = None
            self.range_drag_finished.emit()
            event.accept()
            return
        if self._seek_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._seek_dragging = False
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class LibraryWindow(QMainWindow):
    def __init__(self, sessions_folder: Path, recording_folder: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("dogears — recording library")
        self.resize(1250, 850)
        self.setMinimumSize(990, 690)
        self.sessions_folder = sessions_folder
        self.recording_folder = recording_folder
        self.entries: dict[str, Recording] = {}
        self.selected: Recording | None = None
        self.user_adjusted_range = False
        self.clip_ranges: list[tuple[float, float]] = []
        self.current_clip_index: int | None = None
        self._showing_selection = False
        self.export_error = ""
        self._out_buffer = ""
        self.export_active = False
        self.export_cancelled = False
        self.export_jobs: list[tuple[Path, list[str]]] = []
        self.job_count = 0
        self.job_output: Path | None = None
        self.export_outputs: list[Path] = []
        self.export_mode = 'separate'
        self.export_temp: tempfile.TemporaryDirectory | None = None
        self.export_summary = ''

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
        self.timeline.range_dragged.connect(self.range_dragged)
        self.timeline.range_drag_finished.connect(self.range_edited)
        lyt_right.addWidget(self.timeline)
        guide = QLabel('Drag the blue In/Out handles on the timeline · I = In · O = Out (playhead)')
        guide.setStyleSheet('color: #768092; font-size: 11px')
        lyt_right.addWidget(guide)
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
        # Multiple manual clips are kept in a per-session project file.
        clips_label = QLabel('Clips in export order (select to edit with handles or I/O)')
        lyt_right.addWidget(clips_label)
        self.clips_list = QListWidget()
        self.clips_list.setMaximumHeight(122)
        self.clips_list.currentRowChanged.connect(self.choose_clip)
        lyt_right.addWidget(self.clips_list)
        clip_buttons = QHBoxLayout()
        self.new_selection_button = QPushButton('New selection')
        self.new_selection_button.clicked.connect(self.new_selection)
        self.add_clip_button = QPushButton('Add clip')
        self.add_clip_button.clicked.connect(self.add_clip)
        self.remove_clip_button = QPushButton('Remove clip')
        self.remove_clip_button.clicked.connect(self.remove_clip)
        self.up_button = QPushButton('Move up')
        self.up_button.clicked.connect(lambda: self.move_clip(-1))
        self.down_button = QPushButton('Move down')
        self.down_button.clicked.connect(lambda: self.move_clip(1))
        for button in (self.new_selection_button, self.add_clip_button,
                       self.remove_clip_button, self.up_button, self.down_button):
            clip_buttons.addWidget(button)
        lyt_right.addLayout(clip_buttons)

        actions = QHBoxLayout()
        self.locate_button = QPushButton('Locate missing video…')
        self.locate_button.clicked.connect(self.relink)
        actions.addWidget(self.locate_button)
        self.export_format = QComboBox()
        self.export_format.addItem('Separate MP4 files', 'separate')
        self.export_format.addItem('Merge into one MP4', 'merged')
        self.export_format.setToolTip('Export clips in list order; merging preserves the original recording.')
        actions.addWidget(self.export_format)
        actions.addStretch()
        self.cancel = QPushButton('Cancel export')
        self.cancel.clicked.connect(self.cancel_export)
        self.cancel.setVisible(False)
        actions.addWidget(self.cancel)
        self.export = QPushButton('Export clips…')
        self.export.clicked.connect(self.export_clips)
        actions.addWidget(self.export)
        lyt_right.addLayout(actions)
        self.detail_status = QLabel('Select an In/Out range, click Add clip, then choose an export format.')
        self.detail_status.setWordWrap(True)
        self.detail_status.setStyleSheet('color: #768092')
        lyt_right.addWidget(self.detail_status)
        # Window-local shortcuts, not global gameplay hotkeys. Text entry still works.
        self.shortcut_in = QShortcut(QKeySequence('I'), self)
        self.shortcut_out = QShortcut(QKeySequence('O'), self)
        self.shortcut_in.activated.connect(self.mark_in)
        self.shortcut_out.activated.connect(self.mark_out)
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
        self.clip_ranges = []
        self.current_clip_index = None
        self.clips_list.clear()
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
        self.update_clip_buttons()

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
        self.clip_ranges = load_clip_ranges(self.sessions_folder, rec.session_id)
        self.current_clip_index = None
        self.refresh_clips()
        for index, seconds in enumerate(rec.bookmarks, 1):
            self.marks.addItem(f"{index:02d}    {timecode(seconds)}")
        self.timeline.setRange(0, 0)
        self.timeline.set_marks(rec.bookmarks)
        self.timeline.set_clip_range(None, None)
        self.position.setText("00:00:00.000 / 00:00:00.000")
        self.in_edit.setText("00:00:00.000")
        self.out_edit.setText("00:00:00.000")
        self.export.setEnabled(bool(rec.available and self.clip_ranges))
        self.play.setEnabled(rec.available)
        self.set_in.setEnabled(rec.available)
        self.set_out.setEnabled(rec.available)
        self.locate_button.setEnabled(not rec.available)
        if rec.available:
            self.detail_status.setText('Use I/O or drag handles to select a range, then Add clip.')
            self.player.setSource(QUrl.fromLocalFile(str(rec.video)))
            if self.clip_ranges:
                self.clips_list.setCurrentRow(0)
            else:
                self.current_clip_index = None
        else:
            self.detail_status.setText('Video missing or recording unfinished. Use Locate missing video to relink it.')
        self.update_clip_buttons()

    def duration_changed(self, duration_ms: int):
        self.timeline.setMaximum(max(0, duration_ms))
        self.position_changed(self.player.position())
        if self.selected and duration_ms > 0 and not self.user_adjusted_range:
            self.in_edit.setText("00:00:00.000")
            self.out_edit.setText(timecode(duration_ms / 1000))
            self.timeline.set_clip_range(0, duration_ms / 1000)
        self.update_clip_buttons()

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
        if not self.export_active and self.selected and self.selected.available:
            self.in_edit.setText(timecode(self.player.position() / 1000))
            self.range_edited()

    def mark_out(self):
        if not self.export_active and self.selected and self.selected.available:
            self.out_edit.setText(timecode(self.player.position() / 1000))
            self.range_edited()

    def range_dragged(self, start: float, end: float):
        self.in_edit.setText(timecode(start))
        self.out_edit.setText(timecode(end))
        self.range_edited(save=False)

    def current_range(self) -> tuple[float, float]:
        start, end = parse_timecode(self.in_edit.text()), parse_timecode(self.out_edit.text())
        validate_range(start, end, self.player.duration() / 1000)
        return start, end

    def range_edited(self, save: bool = True):
        if self._showing_selection or self.export_active:
            return
        self.user_adjusted_range = True
        try:
            start, end = self.current_range()
            self.timeline.set_clip_range(start, end)
            if self.current_clip_index is not None:
                self.clip_ranges[self.current_clip_index] = (start, end)
                item = self.clips_list.item(self.current_clip_index)
                if item is not None:
                    item.setText(f'{self.current_clip_index+1:02d}    '
                                 f'{timecode(start)} → {timecode(end)}    '
                                 f'({timecode(end-start)})')
                if save:
                    self.persist_clips()
            self.detail_status.setText('Clip selection updated. Drag handles or press I/O at the playhead.')
        except ValueError:
            self.detail_status.setText('Set a valid In/Out range inside the recording (minimum 0.1 sec).')
        self.update_clip_buttons()

    def refresh_clips(self):
        selected_index = self.current_clip_index
        self.clips_list.blockSignals(True)
        self.clips_list.clear()
        for index, (start, end) in enumerate(self.clip_ranges, 1):
            self.clips_list.addItem(f'{index:02d}    {timecode(start)} → {timecode(end)}'
                                    f'    ({timecode(end-start)})')
        self.clips_list.setCurrentRow(-1 if selected_index is None else selected_index)
        self.clips_list.blockSignals(False)
        self.update_clip_buttons()

    def persist_clips(self):
        if not self.selected:
            return
        try:
            save_clip_ranges(self.sessions_folder, self.selected.session_id, self.clip_ranges)
        except (OSError, ValueError) as exc:
            self.detail_status.setText(f'Could not save clip selections: {exc}')

    def choose_clip(self, index: int):
        if index < 0 or index >= len(self.clip_ranges):
            self.current_clip_index = None
            self.update_clip_buttons()
            return
        self.current_clip_index = index
        self._showing_selection = True
        start, end = self.clip_ranges[index]
        self.in_edit.setText(timecode(start))
        self.out_edit.setText(timecode(end))
        self.timeline.set_clip_range(start, end)
        self.user_adjusted_range = True
        self._showing_selection = False
        self.detail_status.setText(f'Editing clip {index+1}; changes save automatically.')
        self.update_clip_buttons()

    def new_selection(self):
        if self.export_active or not self.selected or not self.selected.available or self.player.duration() <= 0:
            return
        self.current_clip_index = None
        self.clips_list.blockSignals(True)
        self.clips_list.setCurrentRow(-1)
        self.clips_list.blockSignals(False)
        duration = self.player.duration() / 1000
        position = min(self.player.position() / 1000, max(0, duration - 0.1))
        start = max(0, position)
        end = min(duration, max(position + 10, 0.1))
        self.in_edit.setText(timecode(start))
        self.out_edit.setText(timecode(end))
        self.timeline.set_clip_range(start, end)
        self.detail_status.setText('New selection: drag handles or use I/O, then Add clip.')
        self.update_clip_buttons()

    def add_clip(self):
        if self.export_active or not self.selected or not self.selected.available:
            return
        try:
            chosen = self.current_range()
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid clip range', str(exc))
            return
        if len(self.clip_ranges) >= 500:
            QMessageBox.warning(self, 'Clip limit', 'A session supports up to 500 clips.')
            return
        self.clip_ranges.append(chosen)
        self.current_clip_index = len(self.clip_ranges) - 1
        self.refresh_clips()
        self.persist_clips()
        self.detail_status.setText(f'Added clip {len(self.clip_ranges)}. Choose New selection to make another.')

    def remove_clip(self):
        if self.current_clip_index is None:
            return
        old = self.current_clip_index
        self.clip_ranges.pop(old)
        self.current_clip_index = min(old, len(self.clip_ranges) - 1) if self.clip_ranges else None
        self.refresh_clips()
        if self.current_clip_index is not None:
            self.choose_clip(self.current_clip_index)
        self.persist_clips()

    def move_clip(self, delta: int):
        index = self.current_clip_index
        if index is None or index + delta not in range(len(self.clip_ranges)):
            return
        self.clip_ranges[index], self.clip_ranges[index + delta] = (
            self.clip_ranges[index + delta], self.clip_ranges[index])
        self.current_clip_index = index + delta
        self.refresh_clips()
        self.persist_clips()

    def update_clip_buttons(self):
        valid_video = bool(self.selected and self.selected.available and self.player.duration() > 0)
        editable = valid_video and not self.export_active
        for btn in (self.new_selection_button, self.set_in, self.set_out):
            btn.setEnabled(editable)
        try:
            valid_range = bool(editable and self.current_range())
        except ValueError:
            valid_range = False
        self.add_clip_button.setEnabled(valid_range and len(self.clip_ranges) < 500)
        index = self.current_clip_index
        self.remove_clip_button.setEnabled(editable and index is not None)
        self.up_button.setEnabled(editable and index is not None and index > 0)
        self.down_button.setEnabled(editable and index is not None and index < len(self.clip_ranges) - 1)
        self.export.setEnabled(editable and bool(self.clip_ranges))

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

    def export_clips(self):
        rec = self.selected
        if not rec or not rec.available or self.export_active or not self.clip_ranges:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            QMessageBox.information(self, 'FFmpeg required',
                'Export requires FFmpeg. Install with winget install --id Gyan.FFmpeg -e, '
                'then restart dogears; or place ffmpeg.exe in the tools folder.')
            return
        try:
            for start, end in self.clip_ranges:
                validate_range(start, end, self.player.duration() / 1000)
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid clips', str(exc))
            return

        mode = self.export_format.currentData()
        temp = None
        try:
            if mode == 'separate':
                folder = QFileDialog.getExistingDirectory(self, 'Save clips to folder', str(rec.video.parent))
                if not folder:
                    return
                outputs = separate_clip_destinations(rec.video, Path(folder), self.clip_ranges)
                if any(path.exists() for path in outputs):
                    QMessageBox.warning(self, 'Files already exist',
                        'One or more clip filenames already exist. Choose another directory '
                        'or move/rename existing exports; dogears never overwrites them.')
                    return
                jobs = [(path, export_arguments(rec.video, path, start, end))
                        for path, (start, end) in zip(outputs, self.clip_ranges)]
                summary = f'{len(outputs)} separate clips in {folder}'
            else:
                default = rec.video.parent / f'{rec.video.stem}_merged_clips.mp4'
                filename, _ = QFileDialog.getSaveFileName(
                    self, 'Export merged clips as MP4', str(default), 'MP4 video (*.mp4)')
                if not filename:
                    return
                output = Path(filename)
                if output.suffix.lower() != '.mp4':
                    output = output.with_suffix('.mp4')
                if output.exists() or output.resolve() == rec.video.resolve():
                    QMessageBox.warning(self, 'File already exists',
                        'Choose a new filename; dogears never overwrites an existing video.')
                    return
                # Identical encoding and streams, then a stream-copy concatenate.
                # Temporary segments live on the same drive as the output.
                temp = tempfile.TemporaryDirectory(prefix='dogears-clips-', dir=str(output.parent))
                directory = Path(temp.name)
                segments = [directory / f'segment_{n:03d}.mp4'
                            for n in range(1, len(self.clip_ranges) + 1)]
                manifest = directory / 'concat.txt'
                manifest.write_text(concat_manifest(segments), encoding='utf-8')
                jobs = [(segment, export_arguments(rec.video, segment, start, end))
                        for segment, (start, end) in zip(segments, self.clip_ranges)]
                jobs.append((output, concat_arguments(manifest, output)))
                outputs = [output]
                summary = f'{len(self.clip_ranges)} clips merged into {output}'
        except (OSError, ValueError) as exc:
            if temp is not None:
                temp.cleanup()
            QMessageBox.warning(self, 'Cannot prepare export', str(exc))
            return

        self.export_mode = mode
        self.export_temp = temp
        self.export_jobs = jobs
        self.job_count = len(jobs)
        self.job_output = None
        self.export_outputs = outputs
        self.export_summary = summary
        self.export_active = True
        self.export_cancelled = False
        self.export_error = ''
        self.export.setEnabled(False)
        self.table.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.clips_list.setEnabled(False)
        self.in_edit.setEnabled(False)
        self.out_edit.setEnabled(False)
        self.timeline.setEnabled(False)
        self.export_format.setEnabled(False)
        self.cancel.setVisible(True)
        self.update_clip_buttons()
        self.detail_status.setText('Preparing clip export…')
        self._ffmpeg_binary = ffmpeg
        self.run_next_export()

    def run_next_export(self):
        if not self.export_active or self.export_cancelled:
            return
        if not self.export_jobs:
            self.finish_export(success=True)
            return
        self.job_output, args = self.export_jobs.pop(0)
        number = self.job_count - len(self.export_jobs)
        self._out_buffer = ''
        self.export_error = ''
        action = 'Joining clips' if self.export_mode == 'merged' and not self.export_jobs else 'Encoding clip'
        self.detail_status.setText(f'{action} — step {number}/{self.job_count}…')
        self.export_process.start(self._ffmpeg_binary, args)

    def export_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart and self.export_active:
            self.finish_export(success=False, problem='Could not start FFmpeg. Check its installation and permissions.')

    def on_export_output(self):
        self._out_buffer += bytes(self.export_process.readAllStandardOutput()).decode('utf-8', errors='replace')
        lines = self._out_buffer.split('\n')
        self._out_buffer = lines.pop()
        for line in lines:
            if line.startswith('out_time='):
                number = self.job_count - len(self.export_jobs)
                self.detail_status.setText(
                    f'Export step {number}/{self.job_count} · processed {line.partition("=")[2].strip()}')

    def on_export_error(self):
        self.export_error += bytes(self.export_process.readAllStandardError()).decode('utf-8', errors='replace')
        self.export_error = self.export_error[-4000:]

    def cancel_export(self):
        if not self.export_active:
            return
        self.export_cancelled = True
        if self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.detail_status.setText('Cancelling export…')
            self.export_process.terminate()
            QTimer.singleShot(1500, self.kill_if_running)
        else:
            self.finish_export(success=False, problem='Export cancelled.')

    def kill_if_running(self):
        if self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.export_process.kill()

    def export_finished(self, exit_code: int, exit_status):
        if not self.export_active:
            return
        self.on_export_output()
        self.on_export_error()
        if self.export_cancelled:
            self.finish_export(success=False, problem='Export cancelled.')
            return
        if (exit_code != 0 or exit_status != QProcess.ExitStatus.NormalExit
                or self.job_output is None or not self.job_output.is_file()):
            self.finish_export(success=False, problem=self.export_error.strip() or 'FFmpeg export failed.')
            return
        # Avoid starting another QProcess while handling the previous one's finished signal.
        QTimer.singleShot(0, self.run_next_export)

    def finish_export(self, success: bool, problem: str = ''):
        if not self.export_active:
            return
        self.export_active = False
        self.export_jobs.clear()
        self.job_output = None
        temp = self.export_temp
        self.export_temp = None
        if temp is not None:
            temp.cleanup()
        self.cancel.setVisible(False)
        self.table.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.clips_list.setEnabled(True)
        self.in_edit.setEnabled(True)
        self.out_edit.setEnabled(True)
        self.timeline.setEnabled(True)
        self.export_format.setEnabled(True)
        self.update_clip_buttons()
        if success:
            self.detail_status.setText(f'Export complete: {self.export_summary}')
            QMessageBox.information(self, 'Clips exported', f'Saved: {self.export_summary}')
        else:
            outputs = [str(path) for path in self.export_outputs if path.exists()]
            partial = (' Some output files were created: ' + ', '.join(outputs[:3])) if outputs else ''
            self.detail_status.setText(f'Export stopped: {problem[:260]}.{partial}')
            QMessageBox.warning(self, 'Export incomplete', f'{problem[:800]}{partial}')

    def closeEvent(self, event):
        if self.export_active or self.export_process.state() != QProcess.ProcessState.NotRunning:
            QMessageBox.information(self, 'Export in progress',
                                    'Cancel or finish the export before closing the library.')
            event.ignore()
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        event.accept()
