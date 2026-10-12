"""On-demand recording library and manual clip editor.

Only instantiated when the user opens the library; none of the video decoding
or FFmpeg-export functionality is running during gameplay.
"""
from __future__ import annotations

from datetime import datetime
import os
import tempfile
from collections import deque
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, Qt, QUrl, Signal, QSize, QPointF
from PySide6.QtGui import (QBrush, QColor, QPainter, QPen, QShortcut, QKeySequence,
                          QPixmap, QIcon, QPolygonF)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QListWidget, QMainWindow, QMessageBox, QPushButton, QComboBox,
    QSlider, QScrollBar, QSplitter, QScrollArea, QFrame, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .core import timecode
from .timeline_math import (viewport_span, clamp_view_start, ms_to_fraction, fraction_to_ms,
                            ruler_step_ms, ruler_marks, ruler_label, preview_finished)
from .thumbnail_core import thumbnail_name, thumbnail_pipe_args, thumbnail_seek_candidates, clip_thumbnail_time
from .library_core import (
    Recording, export_arguments, find_ffmpeg, load_recordings,
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


class BookmarkTimeline(QWidget):
    """Painted timeline with dedicated bookmark lane, clip grips, playhead and zoom.

    The original QSlider drew bookmark ticks over native controls; its handle and
    style could cover the markers. A single custom painting coordinate system
    keeps ticks, selection grips and seeking aligned even when zoomed.
    """

    seek_requested = Signal(int)
    bookmark_clicked = Signal(int)
    bookmark_double_clicked = Signal(int)
    range_dragged = Signal(float, float)
    range_drag_finished = Signal()
    viewport_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bookmarks: tuple[float, ...] = ()
        self.clip_start: float | None = None
        self.clip_end: float | None = None
        self.duration_ms = 0
        self.position_ms = 0
        self.zoom_factor = 1.0
        self.view_start_ms = 0
        self._drag_handle: str | None = None
        self._seek_dragging = False
        self.setMinimumHeight(130)
        self.setMouseTracking(True)
        self.setToolTip('Green bookmark icons jump to marks; white dot is playback. Drag blue handles to adjust clips. The labelled ruler shows recording time.')

    def setRange(self, minimum: int, maximum: int):
        self.setMaximum(maximum)

    def setMaximum(self, duration_ms: int):
        self.duration_ms = max(0, duration_ms)
        self.position_ms = min(self.position_ms, self.duration_ms)
        self.view_start_ms = clamp_view_start(self.duration_ms, self.zoom_factor, self.view_start_ms)
        self.viewport_changed.emit()
        self.update()

    def setValue(self, value_ms: int):
        self.position_ms = max(0, min(self.duration_ms, value_ms))
        self.update()

    def isSliderDown(self) -> bool:
        return self._seek_dragging or self._drag_handle is not None

    def visible_span_ms(self) -> int:
        return viewport_span(self.duration_ms, self.zoom_factor)

    def max_view_start_ms(self) -> int:
        return max(0, self.duration_ms - self.visible_span_ms())

    def set_zoom(self, zoom: float, focus_ms: int | None = None):
        if focus_ms is None:
            focus_ms = self.position_ms
        old_span = self.visible_span_ms()
        focus_fraction = ms_to_fraction(focus_ms, self.view_start_ms, old_span)
        self.zoom_factor = max(1.0, min(32.0, zoom))
        target = focus_ms - focus_fraction * self.visible_span_ms()
        self.set_view_start(target)

    def set_view_start(self, start_ms: float):
        requested = clamp_view_start(self.duration_ms, self.zoom_factor, start_ms)
        if requested != self.view_start_ms:
            self.view_start_ms = requested
        self.viewport_changed.emit()
        self.update()

    def reveal(self, position_ms: int):
        span = self.visible_span_ms()
        if position_ms < self.view_start_ms or position_ms > self.view_start_ms + span:
            self.set_view_start(position_ms - span * 0.20)

    def set_marks(self, marks: tuple[float, ...]):
        self.bookmarks = marks
        self.update()

    def set_clip_range(self, start: float | None, end: float | None):
        self.clip_start, self.clip_end = start, end
        self.update()

    def _track(self) -> tuple[float, float, float]:
        return 22.0, float(max(23, self.width() - 22)), 61.0

    def _x(self, seconds: float) -> float:
        left, right, _ = self._track()
        frac = ms_to_fraction(seconds * 1000, self.view_start_ms, self.visible_span_ms())
        return left + frac * (right - left)

    def _seconds_at(self, x: float) -> float:
        left, right, _ = self._track()
        return fraction_to_ms((x - left) / (right - left), self.view_start_ms, self.visible_span_ms()) / 1000

    def _handle_at(self, x: float, y: float) -> str | None:
        if self.duration_ms <= 0 or self.clip_start is None or self.clip_end is None:
            return None
        if not 43 <= y <= 80:
            return None
        a, b = self._x(self.clip_start), self._x(self.clip_end)
        distance = min(abs(x - a), abs(x - b))
        if distance > 11:
            return None
        return 'in' if abs(x - a) <= abs(x - b) else 'out'

    def _bookmark_at(self, x: float, y: float) -> int | None:
        if not 8 <= y <= 43:
            return None
        visible = [(abs(x - self._x(t)), idx) for idx, t in enumerate(self.bookmarks)
                   if 0 <= ms_to_fraction(t * 1000, self.view_start_ms, self.visible_span_ms()) <= 1]
        if not visible:
            return None
        distance, index = min(visible)
        return index if distance <= 9 else None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right, mid = self._track()
        painter.setPen(QPen(QColor('#627388'), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(left, mid), QPointF(right, mid))
        if self.duration_ms > 0:
            if self.clip_start is not None and self.clip_end is not None:
                a, b = self._x(self.clip_start), self._x(self.clip_end)
                a, b = max(left, a), min(right, b)
                if a <= b:
                    painter.setPen(QPen(QColor('#55b6f3'), 8))
                    painter.drawLine(QPointF(a, mid), QPointF(b, mid))
                for x in (self._x(self.clip_start), self._x(self.clip_end)):
                    if left <= x <= right:
                        painter.setPen(QPen(QColor('#b9dfff'), 1.5))
                        painter.setBrush(QBrush(QColor('#1677c5')))
                        painter.drawRoundedRect(round(x) - 5, 45, 10, 32, 3, 3)
            # Distinct, simple bookmark silhouette with a V-notch, not a pin or flag.
            for seconds in self.bookmarks:
                x = self._x(seconds)
                if not left <= x <= right:
                    continue
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor('#21d19a')))
                painter.drawPolygon(QPolygonF([
                    QPointF(x - 5, 16), QPointF(x + 5, 16),
                    QPointF(x + 5, 34), QPointF(x, 29), QPointF(x - 5, 34),
                ]))
                painter.setPen(QPen(QColor('#21d19a'), 1.3))
                painter.drawLine(QPointF(x, 35), QPointF(x, 43))
            # A single white dot represents the playhead (no tall triangular scrubber).
            play_x = self._x(self.position_ms / 1000)
            if left <= play_x <= right:
                painter.setPen(QPen(QColor('#213041'), 2))
                painter.setBrush(QBrush(QColor('#f1f6ff')))
                painter.drawEllipse(QPointF(play_x, mid), 6, 6)

            # Actual time ruler; ticks remain aligned to absolute times while panning.
            painter.setFont(self.font())
            span = self.visible_span_ms()
            step = ruler_step_ms(span, max(1, round(right - left)))
            for tick in ruler_marks(self.view_start_ms, span, step):
                x = left + (tick - self.view_start_ms) / span * (right - left)
                painter.setPen(QPen(QColor('#8899aa'), 1.2))
                painter.drawLine(QPointF(x, 90), QPointF(x, 100))
                label = ruler_label(tick, self.duration_ms, step)
                text_width = painter.fontMetrics().horizontalAdvance(label)
                label_x = max(1, min(self.width() - text_width - 1, x - text_width / 2))
                painter.setPen(QColor('#aebdcd'))
                painter.drawText(QPointF(label_x, 119), label)
            # Minor ticks provide scale between the labelled times.
            sub = step // 5
            if sub >= 100 and span > 0:
                for tick in ruler_marks(self.view_start_ms, span, sub):
                    if tick % step == 0:
                        continue
                    x = left + (tick - self.view_start_ms) / span * (right - left)
                    painter.setPen(QPen(QColor('#65788e'), 1))
                    painter.drawLine(QPointF(x, 94), QPointF(x, 99))
        else:
            painter.setPen(QColor('#8995a7'))
            painter.drawText(22, 119, '00:00')
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.duration_ms > 0:
            x, y = event.position().x(), event.position().y()
            bookmark = self._bookmark_at(x, y)
            if bookmark is not None:
                self.bookmark_clicked.emit(bookmark)
                event.accept()
                return
            handle = self._handle_at(x, y)
            if handle:
                self._drag_handle = handle
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                event.accept()
                return
            self._seek_dragging = True
            self.setValue(round(self._seconds_at(x) * 1000))
            self.seek_requested.emit(self.position_ms)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            mark = self._bookmark_at(event.position().x(), event.position().y())
            if mark is not None:
                self.bookmark_double_clicked.emit(mark)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        x, y = event.position().x(), event.position().y()
        if self._drag_handle:
            position = self._seconds_at(x)
            start, end = self.clip_start, self.clip_end
            duration = self.duration_ms / 1000
            if self._drag_handle == 'in':
                start = min(max(0, position), end - 0.1)
            else:
                end = max(min(duration, position), start + 0.1)
            self.set_clip_range(start, end)
            self.range_dragged.emit(start, end)
            event.accept()
            return
        if self._seek_dragging:
            self.setValue(round(self._seconds_at(x) * 1000))
            self.seek_requested.emit(self.position_ms)
            event.accept()
            return
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._bookmark_at(x, y) is not None
                       else Qt.CursorShape.SizeHorCursor if self._handle_at(x, y) else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_handle:
                self._drag_handle = None
                self.range_drag_finished.emit()
                event.accept()
                return
            if self._seek_dragging:
                self._seek_dragging = False
                event.accept()
                return
        super().mouseReleaseEvent(event)


class LibraryWindow(QMainWindow):
    def __init__(self, sessions_folder: Path, recording_folder: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("dogears — recording library")
        self.resize(1460, 850)
        self.setMinimumSize(1100, 690)
        self.sessions_folder = sessions_folder
        self.recording_folder = recording_folder
        self.entries: dict[str, Recording] = {}
        self.selected: Recording | None = None
        self.user_adjusted_range = False
        self.clip_ranges: list[tuple[float, float]] = []
        self.selection_start: float | None = None
        self.selection_end: float | None = None
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
        self._thumbnail_epoch = 0
        self._thumbnail_queue = deque()
        self._thumbnail_process: QProcess | None = None
        self._thumbnail_ffmpeg: str | None = None
        self._follow_playhead = True
        self.preview_active = False
        self.preview_end_ms = 0
        self.preview_start_ms = 0
        self.preview_seeking = False
        self._thumbnail_errors: list[str] = []
        self._thumbnail_total = 0
        self._thumbnail_loaded = 0
        cache_root = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())
        self.thumbnail_dir = cache_root / 'dogears' / 'thumbnails'

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
        self.video.setMinimumHeight(200)
        self.player.setVideoOutput(self.video)
        lyt_right.addWidget(self.video, 3)
        playback = QHBoxLayout()
        self.play = QPushButton("Play recording")
        self.play.clicked.connect(self.toggle_playback)
        self.play.setEnabled(False)
        playback.addWidget(self.play)
        self.preview = QPushButton('Play clip')
        self.preview.setToolTip('Play the selected In–Out range and pause at Out. Double-click a saved clip to play it.')
        self.preview.clicked.connect(self.toggle_clip_preview)
        self.preview.setEnabled(False)
        playback.addWidget(self.preview)
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
        self.timeline.seek_requested.connect(self.seek_manually)
        self.timeline.bookmark_clicked.connect(self.select_timeline_bookmark)
        self.timeline.bookmark_double_clicked.connect(self.play_timeline_bookmark)
        self.timeline.range_dragged.connect(self.range_dragged)
        self.timeline.range_drag_finished.connect(self.range_edited)
        self.timeline.viewport_changed.connect(self.sync_timeline_scroll)
        lyt_right.addWidget(self.timeline)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel('Timeline scale'))
        self.zoom_out = QPushButton('−')
        self.zoom_out.setFixedWidth(30)
        self.zoom_out.clicked.connect(lambda: self.zoom_slider.setValue(max(0, self.zoom_slider.value() - 1)))
        zoom_row.addWidget(self.zoom_out)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self.zoom_slider.setRange(0, 5)
        self.zoom_slider.setValue(0)
        self.zoom_slider.setToolTip('Fit entire recording, or zoom to 2×, 4×, 8×, 16× or 32×')
        self.zoom_slider.valueChanged.connect(self.change_timeline_zoom)
        zoom_row.addWidget(self.zoom_slider, 1)
        self.zoom_in = QPushButton('+')
        self.zoom_in.setFixedWidth(30)
        self.zoom_in.clicked.connect(lambda: self.zoom_slider.setValue(min(5, self.zoom_slider.value() + 1)))
        zoom_row.addWidget(self.zoom_in)
        self.zoom_fit = QPushButton('Fit')
        self.zoom_fit.setToolTip('Show the entire recording')
        self.zoom_fit.clicked.connect(lambda: self.zoom_slider.setValue(0))
        zoom_row.addWidget(self.zoom_fit)
        self.zoom_label = QLabel('Fit')
        self.zoom_label.setMinimumWidth(36)
        zoom_row.addWidget(self.zoom_label)
        lyt_right.addLayout(zoom_row)
        self.zoom_info = QLabel('Time ruler: entire recording')
        self.zoom_info.setStyleSheet('color: #8f9eaf; font-size: 11px')
        lyt_right.addWidget(self.zoom_info)
        self.timeline_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.timeline_scroll.setToolTip('Pan through the zoomed recording without changing playback position')
        self.timeline_scroll.valueChanged.connect(self.pan_timeline)
        self.timeline_scroll.setVisible(False)
        lyt_right.addWidget(self.timeline_scroll)
        guide = QLabel('Green bookmarks = saved marks · white dot = playhead · drag blue handles to select a clip')
        guide.setStyleSheet('color: #768092; font-size: 11px')
        lyt_right.addWidget(guide)
        lyt_right.addWidget(QLabel("Bookmarks (single-click to seek; double-click to play)"))
        self.marks = QListWidget()
        self.marks.setIconSize(QSize(160, 90))
        self.marks.setSpacing(3)
        self.marks.setMinimumHeight(130)
        self.marks.setMaximumHeight(230)
        self.marks.currentRowChanged.connect(self.jump_bookmark)
        self.marks.itemDoubleClicked.connect(self.play_bookmark)
        lyt_right.addWidget(self.marks)
        self.thumbnail_status = QLabel('')
        self.thumbnail_status.setWordWrap(True)
        self.thumbnail_status.setStyleSheet('color: #8f9eaf; font-size: 11px')
        lyt_right.addWidget(self.thumbnail_status)

        self.selection_label = QLabel('Selected range: —')
        self.selection_label.setStyleSheet('font-weight: 600; color: #91cef5')
        lyt_right.addWidget(self.selection_label)
        # Clips live in their own right-hand sidebar. The recorder and timeline
        # remain completely independent of this on-demand layout.
        clips_sidebar = QWidget()
        clips_sidebar_layout = QVBoxLayout(clips_sidebar)
        clips_sidebar_layout.setContentsMargins(8, 3, 3, 4)
        clips_sidebar_layout.addWidget(QLabel('Clips in export order'))
        sidebar_help = QLabel('Select to edit · double-click to play · drag blue handles on timeline')
        sidebar_help.setWordWrap(True)
        sidebar_help.setStyleSheet('color: #8f9eaf; font-size: 11px')
        clips_sidebar_layout.addWidget(sidebar_help)
        self.clips_list = QListWidget()
        self.clips_list.setIconSize(QSize(160, 90))
        self.clips_list.setSpacing(3)
        self.clips_list.setMinimumHeight(240)
        self.clips_list.currentRowChanged.connect(self.choose_clip)
        self.clips_list.itemDoubleClicked.connect(self.play_saved_clip)
        clips_sidebar_layout.addWidget(self.clips_list, 1)
        clip_buttons_top = QHBoxLayout()
        clip_buttons_bottom = QHBoxLayout()
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
        for button in (self.new_selection_button, self.add_clip_button):
            clip_buttons_top.addWidget(button)
        for button in (self.remove_clip_button, self.up_button, self.down_button):
            clip_buttons_bottom.addWidget(button)
        clips_sidebar_layout.addLayout(clip_buttons_top)
        clips_sidebar_layout.addLayout(clip_buttons_bottom)

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
        clips_sidebar_layout.addLayout(actions)
        self.detail_status = QLabel('Drag the blue handles to choose a range, click Add clip, then choose an export format.')
        self.detail_status.setWordWrap(True)
        self.detail_status.setStyleSheet('color: #768092')
        lyt_right.addWidget(self.detail_status)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
        clips_sidebar_scroll = QScrollArea()
        clips_sidebar_scroll.setWidgetResizable(True)
        clips_sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        clips_sidebar_scroll.setMinimumWidth(280)
        clips_sidebar_scroll.setWidget(clips_sidebar)
        splitter.addWidget(clips_sidebar_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 7)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([315, 815, 330])
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
        self.cancel_thumbnails()
        self.player.stop()
        self.player.setSource(QUrl())
        self.selected = None
        self.filename.setText("Select a recording")
        self.play.setEnabled(False)
        self.stop_clip_preview()
        self.marks.clear()
        self.thumbnail_status.clear()
        self.clip_ranges = []
        self.current_clip_index = None
        self.clips_list.clear()
        self.timeline.setRange(0, 0)
        self.timeline.set_marks(())
        self.timeline.set_clip_range(None, None)
        self.zoom_slider.setValue(0)
        self.position.setText("00:00:00.000 / 00:00:00.000")
        self.selection_start = None
        self.selection_end = None
        self.update_selection_label()
        self.export.setEnabled(False)
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
        self.stop_clip_preview()
        self.player.stop()
        self.player.setSource(QUrl())
        self.selected = rec
        self._follow_playhead = True
        self.user_adjusted_range = False
        self.filename.setText(rec.video.name if rec.video else "Recording file not linked")
        self.filename.setToolTip(str(rec.video or ""))
        self.cancel_thumbnails()
        self.marks.clear()
        self.thumbnail_status.clear()
        self.clip_ranges = load_clip_ranges(self.sessions_folder, rec.session_id)
        self.current_clip_index = None
        self.refresh_clips()
        for index, seconds in enumerate(rec.bookmarks, 1):
            item = QListWidgetItem(f"{index:02d}    {timecode(seconds)}")
            item.setSizeHint(QSize(210, 96))
            item.setIcon(self.thumbnail_placeholder())
            self.marks.addItem(item)
        self.timeline.setRange(0, 0)
        self.timeline.set_marks(rec.bookmarks)
        self.timeline.set_clip_range(None, None)
        self.zoom_slider.setValue(0)
        self.position.setText("00:00:00.000 / 00:00:00.000")
        self.selection_start = None
        self.selection_end = None
        self.update_selection_label()
        self.export.setEnabled(bool(rec.available and self.clip_ranges))
        self.play.setEnabled(rec.available)
        self.locate_button.setEnabled(not rec.available)
        if rec.available:
            self.detail_status.setText('Drag the blue handles to select a range, then Add clip.')
            self.player.setSource(QUrl.fromLocalFile(str(rec.video)))
            self.queue_thumbnails(rec)
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
        if self.selected and duration_ms > 0 and not self.user_adjusted_range and self.current_clip_index is None:
            self.set_selection(0, duration_ms / 1000)
        self.update_clip_buttons()

    def position_changed(self, position_ms: int):
        if self.preview_active:
            if self.preview_seeking:
                # QMediaPlayer seeks asynchronously. A position update from the OLD
                # playback location may arrive after setPosition(start), including
                # one beyond Out. Do not mistake that update for clip completion.
                if self.preview_start_ms - 300 <= position_ms < self.preview_end_ms:
                    self.preview_seeking = False
            elif preview_finished(position_ms, self.preview_end_ms):
                self.stop_clip_preview()
                self.player.pause()
                self.player.setPosition(self.preview_end_ms)
                return
        self.position.setText(f"{timecode(position_ms / 1000)} / {timecode(self.player.duration() / 1000)}")
        if not self.timeline.isSliderDown():
            self.timeline.setValue(position_ms)
            if self._follow_playhead:
                self.timeline.reveal(position_ms)

    def seek_manually(self, position_ms: int):
        if self.preview_active:
            self.stop_clip_preview()
        self.player.setPosition(position_ms)

    def toggle_playback(self):
        # Ordinary Play returns to full-recording playback.
        self.stop_clip_preview()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def playback_changed(self, state):
        if self.preview_active and state == QMediaPlayer.PlaybackState.StoppedState:
            self.stop_clip_preview()
        self.play.setText("Pause recording" if state == QMediaPlayer.PlaybackState.PlayingState else "Play recording")

    def stop_clip_preview(self):
        self.preview_active = False
        self.preview_start_ms = 0
        self.preview_end_ms = 0
        self.preview_seeking = False
        if hasattr(self, 'preview'):
            self.preview.setText('Play clip')

    def toggle_clip_preview(self):
        """Play/pause only the chosen clip, independently from full-video Play."""
        if self.preview_active:
            self.stop_clip_preview()
            self.player.pause()
            return
        self.start_clip_preview()

    def play_saved_clip(self, item):
        """Double-click a saved clip to select and play it from the beginning."""
        index = self.clips_list.row(item)
        if not 0 <= index < len(self.clip_ranges):
            return
        if self.current_clip_index != index:
            self.clips_list.setCurrentRow(index)  # calls choose_clip
        self.start_clip_preview()

    def start_clip_preview(self):
        """Seek to In, play and stop at Out; never create/export a clip file."""
        if not self.selected or not self.selected.available or self.export_active:
            return
        try:
            start, end = self.current_range()
        except ValueError as exc:
            QMessageBox.warning(self, 'Cannot play clip',
                                f'{exc}\n\nTry playing the full recording first. '
                                'If it has no duration, check the recording format or codec.')
            return
        self.player.pause()
        self.stop_clip_preview()
        start_ms, end_ms = round(start * 1000), round(end * 1000)
        self.preview_start_ms = start_ms
        self.preview_end_ms = end_ms
        self.preview_seeking = True
        self.preview_active = True
        self.preview.setText('Stop clip')
        self._follow_playhead = True
        self.player.setPosition(start_ms)
        self.timeline.reveal(start_ms)
        self.player.play()
        self.detail_status.setText(f'Playing clip: {timecode(start)} to {timecode(end)}')

    def media_error(self, error, message):
        if error != QMediaPlayer.Error.NoError:
            self.detail_status.setText(f"Playback error: {message}. Check the video's format and codec.")

    def jump_bookmark(self, index: int):
        if self.selected and 0 <= index < len(self.selected.bookmarks) and self.selected.available:
            target = max(0, self.selected.bookmarks[index] - 5)
            self._follow_playhead = True
            self.player.setPosition(round(target * 1000))
            self.timeline.reveal(round(self.selected.bookmarks[index] * 1000))

    def play_bookmark(self, item):
        """Double-click a bookmark to seek to its preroll and play normally."""
        index = self.marks.row(item)
        self.play_bookmark_index(index)

    def play_timeline_bookmark(self, index: int):
        self.play_bookmark_index(index)

    def play_bookmark_index(self, index: int):
        if not self.selected or not self.selected.available:
            return
        if not 0 <= index < len(self.selected.bookmarks):
            return
        # A bookmark is a navigational marker, not a clip boundary: playback
        # should continue beyond the mark until the user pauses it.
        self.stop_clip_preview()
        if self.marks.currentRow() != index:
            self.marks.setCurrentRow(index)
        self.jump_bookmark(index)
        self.player.play()
        self.detail_status.setText(f'Playing from bookmark {index + 1}.')

    def select_timeline_bookmark(self, index: int):
        # A single click seeks, a double-click also begins playback.
        self.marks.setCurrentRow(index)
        self.jump_bookmark(index)

    def change_timeline_zoom(self, value: int):
        zoom = 2 ** value
        self._follow_playhead = False  # Keep user's chosen zoom/pan until requested otherwise.
        self.zoom_label.setText('Fit' if value == 0 else f'{zoom}×')
        self.timeline.set_zoom(zoom, self.player.position())

    def pan_timeline(self, value: int):
        self._follow_playhead = False  # Manual panning must not snap back on playback.
        self.timeline.set_view_start(self.timeline.max_view_start_ms() * value / 10000)

    def sync_timeline_scroll(self):
        if not hasattr(self, 'timeline_scroll'):
            return
        maximum = self.timeline.max_view_start_ms()
        self.timeline_scroll.blockSignals(True)
        self.timeline_scroll.setRange(0, 10000 if maximum else 0)
        self.timeline_scroll.setPageStep(max(1, round(10000 * self.timeline.visible_span_ms() /
                                                       max(1, self.timeline.duration_ms))))
        self.timeline_scroll.setValue(round(10000 * self.timeline.view_start_ms / maximum) if maximum else 0)
        self.timeline_scroll.blockSignals(False)
        self.timeline_scroll.setVisible(self.timeline.zoom_factor > 1 and self.timeline.duration_ms > 0)
        if hasattr(self, 'zoom_info'):
            start = self.timeline.view_start_ms
            end = min(self.timeline.duration_ms, start + self.timeline.visible_span_ms())
            self.zoom_info.setText(
                f'Visible: {timecode(start / 1000)} – {timecode(end / 1000)}'
                f'   /   Total: {timecode(self.timeline.duration_ms / 1000)}'
            )

    @staticmethod
    def thumbnail_placeholder() -> QIcon:
        image = QPixmap(160, 90)
        image.fill(QColor('#263445'))
        painter = QPainter(image)
        painter.setPen(QColor('#7e91a8'))
        painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, 'Preview')
        painter.end()
        return QIcon(image)

    def cancel_thumbnails(self):
        self._thumbnail_epoch += 1
        self._thumbnail_queue.clear()
        self._thumbnail_ffmpeg = None
        if self._thumbnail_process is not None:
            process = self._thumbnail_process
            self._thumbnail_process = None
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(500)
            process.deleteLater()

    @staticmethod
    def _thumbnail_icon(path: Path) -> QIcon | None:
        pixmap = QPixmap(str(path))
        return QIcon(pixmap) if not pixmap.isNull() else None

    def queue_thumbnails(self, recording: Recording | None):
        """Generate both bookmark AND clip preview frames only while library is open.

        Sequential short-lived FFmpeg workers avoid stalling the Qt event loop.
        All jobs are invalidated when a different recording/clip collection loads.
        """
        self.cancel_thumbnails()
        self._thumbnail_errors = []
        self._thumbnail_loaded = 0
        if recording is None or not recording.available:
            self.thumbnail_status.setText('Previews unavailable: recording file missing.')
            return
        targets = ([('bookmark', i, seconds) for i, seconds in enumerate(recording.bookmarks)]
                   + [('clip', i, clip_thumbnail_time(start, end))
                      for i, (start, end) in enumerate(self.clip_ranges)])
        self._thumbnail_total = len(targets)
        if not targets:
            self.thumbnail_status.setText('No bookmarks or clips to preview.')
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.thumbnail_status.setText(
                'Thumbnails need FFmpeg. Run: winget install --id Gyan.FFmpeg -e '
                '(restart dogears afterwards), or place ffmpeg.exe in the tools folder.')
            return
        try:
            self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
            self._thumbnail_ffmpeg = ffmpeg
            for kind, index, seconds in targets:
                destination = self.thumbnail_dir / thumbnail_name(recording.video, seconds)
                icon = self._thumbnail_icon(destination) if destination.is_file() else None
                widget = self.marks if kind == 'bookmark' else self.clips_list
                item = widget.item(index)
                if icon:
                    if item is not None:
                        item.setIcon(icon)
                        self._thumbnail_loaded += 1
                else:
                    destination.unlink(missing_ok=True)
                    if item is not None:
                        item.setIcon(self.thumbnail_placeholder())
                    self._thumbnail_queue.append((self._thumbnail_epoch, kind, index,
                                                 recording.video, thumbnail_seek_candidates(seconds),
                                                 0, destination))
            self.thumbnail_status.setText(
                f'Previews (bookmarks + clips): {self._thumbnail_loaded}/{self._thumbnail_total} ready…')
            self._run_next_thumbnail()
        except OSError as exc:
            self.thumbnail_status.setText(f'Cannot prepare thumbnail cache: {exc}')

    def _run_next_thumbnail(self):
        if self._thumbnail_process is not None or not self._thumbnail_queue:
            if not self._thumbnail_queue and self._thumbnail_process is None and self._thumbnail_total:
                message = f'Previews (bookmarks + clips): {self._thumbnail_loaded}/{self._thumbnail_total} ready.'
                if self._thumbnail_errors:
                    message += f' Preview issue: {self._thumbnail_errors[0][:240]}'
                self.thumbnail_status.setText(message)
            return
        job = self._thumbnail_queue.popleft()
        epoch, kind, index, source, seeks, attempt, destination = job
        if epoch != self._thumbnail_epoch:
            QTimer.singleShot(0, self._run_next_thumbnail)
            return
        process = QProcess(self)
        self._thumbnail_process = process
        process.finished.connect(
            lambda exit_code, status, proc=process, this_job=job:
                self._thumbnail_done(proc, this_job, exit_code, status))
        process.errorOccurred.connect(
            lambda error, proc=process, this_job=job:
                self._thumbnail_failed(proc, this_job, error))
        process.start(self._thumbnail_ffmpeg, thumbnail_pipe_args(source, seeks[attempt]))

    def _thumbnail_failed(self, process, job, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._thumbnail_done(process, job, -1, QProcess.ExitStatus.CrashExit)

    def _thumbnail_done(self, process, job, exit_code, exit_status):
        if process is not self._thumbnail_process:
            return
        self._thumbnail_process = None
        epoch, kind, index, source, seeks, attempt, destination = job
        diagnostic = bytes(process.readAllStandardError()).decode('utf-8', errors='replace').strip()
        # FFmpeg writes JPEG bytes to stdout instead of opening a Windows file
        # path itself. Qt decodes those bytes and can cache the image afterwards.
        # This works even when FFmpeg's image2 muxer cannot create a cache file.
        jpeg_bytes = bytes(process.readAllStandardOutput())
        pixmap = QPixmap()
        pixmap_ok = (exit_code == 0 and bool(jpeg_bytes)
                     and pixmap.loadFromData(jpeg_bytes, 'JPG'))
        if epoch == self._thumbnail_epoch and pixmap_ok:
            widget = self.marks if kind == 'bookmark' else self.clips_list
            item = widget.item(index)
            if item is not None:
                item.setIcon(QIcon(pixmap))
                self._thumbnail_loaded += 1
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not pixmap.save(str(destination), 'JPEG', 88):
                    self._thumbnail_errors.append(f'Preview displayed but cache could not be saved: {destination}')
            except OSError as exc:
                # A cache failure should never prevent the preview being shown.
                self._thumbnail_errors.append(f'Preview displayed but cache could not be saved: {exc}')
        if not pixmap_ok:
            if epoch == self._thumbnail_epoch and attempt + 1 < len(seeks):
                self._thumbnail_queue.appendleft((epoch, kind, index, source, seeks, attempt + 1, destination))
            elif epoch == self._thumbnail_epoch:
                self._thumbnail_errors.append(diagnostic or
                    f'No decodable frame for {kind} {index + 1}; check source video codec and FFmpeg installation')
        process.deleteLater()
        QTimer.singleShot(0, self._run_next_thumbnail)

    def set_selection(self, start: float | None, end: float | None):
        """One source of truth: the two draggable handles on the timeline."""
        self.selection_start, self.selection_end = start, end
        self.timeline.set_clip_range(start, end)
        self.update_selection_label()

    def update_selection_label(self):
        if self.selection_start is None or self.selection_end is None:
            self.selection_label.setText('Selected range: —')
        else:
            self.selection_label.setText(
                f'Selected range: {timecode(self.selection_start)} → {timecode(self.selection_end)}'
                f'   ({timecode(self.selection_end-self.selection_start)})')

    def range_dragged(self, start: float, end: float):
        self.stop_clip_preview()
        self.set_selection(start, end)
        self.range_edited(save=False)

    def current_range(self) -> tuple[float, float]:
        if self.selection_start is None or self.selection_end is None:
            raise ValueError('Drag the two blue handles on the timeline to select a clip.')
        start, end = self.selection_start, self.selection_end
        validate_range(start, end, self.player.duration() / 1000)
        return start, end

    def range_edited(self, save: bool = True):
        if self._showing_selection or self.export_active:
            return
        if self.preview_active:
            self.stop_clip_preview()
            self.player.pause()
        self.user_adjusted_range = True
        try:
            start, end = self.current_range()
            self.set_selection(start, end)
            if self.current_clip_index is not None:
                self.clip_ranges[self.current_clip_index] = (start, end)
                item = self.clips_list.item(self.current_clip_index)
                if item is not None:
                    item.setText(f'{self.current_clip_index+1:02d}    '
                                 f'{timecode(start)} → {timecode(end)}    '
                                 f'({timecode(end-start)})')
                if save:
                    self.persist_clips()
                    self.queue_thumbnails(self.selected)
            self.detail_status.setText('Clip selection updated. Drag the blue handles to adjust it.')
        except ValueError:
            self.detail_status.setText('Select a range of at least 0.1 seconds within the recording.')
        self.update_clip_buttons()

    def refresh_clips(self):
        selected_index = self.current_clip_index
        self.clips_list.blockSignals(True)
        self.clips_list.clear()
        for index, (start, end) in enumerate(self.clip_ranges, 1):
            item = QListWidgetItem(f'{index:02d}    {timecode(start)} → {timecode(end)}'
                                   f'    ({timecode(end-start)})')
            item.setSizeHint(QSize(210, 96))
            item.setIcon(self.thumbnail_placeholder())
            self.clips_list.addItem(item)
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
        if self.preview_active:
            self.player.pause()
        self.stop_clip_preview()
        if index < 0 or index >= len(self.clip_ranges):
            self.current_clip_index = None
            self.update_clip_buttons()
            return
        self.current_clip_index = index
        self._showing_selection = True
        start, end = self.clip_ranges[index]
        self.set_selection(start, end)
        if self.timeline.zoom_factor > 1:
            self.timeline.set_view_start(start * 1000 - self.timeline.visible_span_ms() * 0.1)
        self.user_adjusted_range = True
        self._showing_selection = False
        self.detail_status.setText(f'Editing clip {index+1}; changes save automatically.')
        self.update_clip_buttons()

    def new_selection(self):
        if self.export_active or not self.selected or not self.selected.available or self.player.duration() <= 0:
            return
        if self.preview_active:
            self.player.pause()
            self.stop_clip_preview()
        self.current_clip_index = None
        self.clips_list.blockSignals(True)
        self.clips_list.setCurrentRow(-1)
        self.clips_list.blockSignals(False)
        duration = self.player.duration() / 1000
        position = min(self.player.position() / 1000, max(0, duration - 0.1))
        start = max(0, position)
        end = min(duration, max(position + 10, 0.1))
        self.set_selection(start, end)
        self.detail_status.setText('New selection: drag blue handles, then click Add clip.')
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
        self.queue_thumbnails(self.selected)
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
        self.queue_thumbnails(self.selected)

    def move_clip(self, delta: int):
        index = self.current_clip_index
        if index is None or index + delta not in range(len(self.clip_ranges)):
            return
        self.clip_ranges[index], self.clip_ranges[index + delta] = (
            self.clip_ranges[index + delta], self.clip_ranges[index])
        self.current_clip_index = index + delta
        self.refresh_clips()
        self.persist_clips()
        self.queue_thumbnails(self.selected)

    def update_clip_buttons(self):
        valid_video = bool(self.selected and self.selected.available and self.player.duration() > 0)
        editable = valid_video and not self.export_active
        self.new_selection_button.setEnabled(editable)
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
        self.preview.setEnabled(valid_range)

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

        self.stop_clip_preview()
        self.player.pause()
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
        self.cancel_thumbnails()
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setVideoOutput(None)
        event.accept()
