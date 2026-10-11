"""Minimal Windows tray interface for OBS-backed recording and bookmarking."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import keyring
import obsws_python as obs
from PySide6.QtCore import QObject, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSlider, QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .audio import NotificationSounds
from .core import Config, HOTKEY_OPTIONS, RecorderBackend, RecorderError, SessionStore, data_dir
from .hotkeys import HotkeyListener

APP_TITLE = "dogears"
PASSWORD_SERVICE = "dogears.OBS.WebSocket"
LEGACY_PASSWORD_SERVICE = "GameMarker.OBS.WebSocket"
PASSWORD_USER = "127.0.0.1"


def tray_icon(recording: bool) -> QIcon:
    pic = QPixmap(48, 48)
    pic.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pic)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor("#233247"))
    painter.setBrush(QColor("#de4953") if recording else QColor("#97a7bb"))
    painter.drawEllipse(7, 7, 34, 34)
    painter.end()
    return QIcon(pic)


class Bridge(QObject):
    hotkey = Signal(str)
    completed = Signal(str, object, object)  # operation, result, exception


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("dogears settings")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        hotkeys = QGroupBox("Hotkeys")
        hotkeys_form = QFormLayout(hotkeys)
        self.record = QComboBox()
        self.bookmark = QComboBox()
        for select, value in [(self.record, cfg.record_hotkey), (self.bookmark, cfg.bookmark_hotkey)]:
            select.addItems(HOTKEY_OPTIONS)
            select.setCurrentText(value)
        hotkeys_form.addRow("Start / stop recording", self.record)
        hotkeys_form.addRow("Create bookmark", self.bookmark)
        layout.addWidget(hotkeys)

        audio = QGroupBox("Notification sounds")
        audio_form = QFormLayout(audio)
        self.sounds = QCheckBox("Play a short sound on start, bookmark and stop")
        self.sounds.setChecked(cfg.sounds_enabled)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(cfg.volume)
        self.sounds.toggled.connect(self.volume.setEnabled)
        self.volume.setEnabled(cfg.sounds_enabled)
        audio_form.addRow(self.sounds)
        audio_form.addRow("Volume", self.volume)
        layout.addWidget(audio)

        connection = QGroupBox("OBS WebSocket (local connection only)")
        connection_form = QFormLayout(connection)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.obs_port)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Leave empty to keep current password")
        connection_form.addRow("Port", self.port)
        connection_form.addRow("Password", self.password)
        self.clear_password = QCheckBox("Remove previously saved password")
        connection_form.addRow(self.clear_password)
        layout.addWidget(connection)

        info = QLabel("Video quality, sources, audio tracks and the recording folder are configured in OBS.\n"
                      "OBS must be open with Tools > WebSocket Server Settings enabled.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #6b7280")
        layout.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_config(self) -> Config:
        return Config(
            record_hotkey=self.record.currentText(),
            bookmark_hotkey=self.bookmark.currentText(),
            sounds_enabled=self.sounds.isChecked(),
            volume=self.volume.value(),
            obs_port=self.port.value(),
        )

    def accept(self):
        try:
            self.selected_config().validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, config_path: Path, root: Path):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.root = root
        self.bridge = Bridge(self)
        self.bridge.hotkey.connect(self.on_hotkey)
        self.bridge.completed.connect(self.on_completed)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dogearsOBS")
        self.backend = RecorderBackend(SessionStore(root / "sessions"), self.obs_client)
        (root / "sounds").mkdir(parents=True, exist_ok=True)
        self.sounds = NotificationSounds(root / "sounds", self)
        self.sounds.configure(cfg.sounds_enabled, cfg.volume)
        self.is_busy = False
        self.is_recording = False
        self.bookmark_count = 0
        self.last_recording: str | None = None
        self.obs_folder: str | None = None
        self.hotkeys: HotkeyListener | None = None
        self.has_hidden_once = False
        self.setWindowTitle(APP_TITLE)
        self.resize(410, 290)
        self.setMinimumSize(385, 275)
        self.setWindowIcon(tray_icon(False))
        self.build_ui()
        self.build_tray()
        try:
            self.replace_hotkeys(cfg)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Hotkeys unavailable", str(exc))
        self.submit("check", self.backend.check)

    def obs_client(self):
        try:
            password = keyring.get_password(PASSWORD_SERVICE, PASSWORD_USER)
            if password is None:
                password = keyring.get_password(LEGACY_PASSWORD_SERVICE, PASSWORD_USER)
            password = password or ""
        except Exception as exc:
            raise RecorderError(f"Cannot access Windows Credential Manager: {exc}") from exc
        return obs.ReqClient(host="127.0.0.1", port=self.cfg.obs_port, password=password, timeout=3)

    def build_ui(self):
        main = QWidget(self)
        layout = QVBoxLayout(main)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(13)
        title = QLabel("dogears")
        title_font = QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)
        self.status = QLabel("Connecting to OBS…")
        self.status.setStyleSheet("color: #7a8293")
        layout.addWidget(self.status)
        self.count = QLabel("Bookmarks: 0")
        layout.addWidget(self.count)
        buttons = QHBoxLayout()
        self.toggle_button = QPushButton("Start recording")
        self.toggle_button.clicked.connect(lambda: self.submit("toggle", self.backend.toggle))
        self.bookmark_button = QPushButton("Bookmark")
        self.bookmark_button.clicked.connect(lambda: self.submit("bookmark", self.backend.bookmark))
        buttons.addWidget(self.toggle_button)
        buttons.addWidget(self.bookmark_button)
        layout.addLayout(buttons)
        controls = QHBoxLayout()
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self.open_settings)
        folder_button = QPushButton("Open recordings")
        folder_button.clicked.connect(self.open_recordings)
        controls.addWidget(settings_button)
        controls.addWidget(folder_button)
        layout.addLayout(controls)
        self.hint = QLabel("Use the hotkeys while gaming. Closing this window keeps the tray app running.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #7a8293")
        layout.addWidget(self.hint)
        layout.addStretch()
        self.setCentralWidget(main)

    def build_tray(self):
        self.tray = QSystemTrayIcon(tray_icon(False), self)
        menu = QMenu()
        self.action_toggle = QAction("Start recording", self)
        self.action_toggle.triggered.connect(lambda: self.submit("toggle", self.backend.toggle))
        self.action_bookmark = QAction("Bookmark", self)
        self.action_bookmark.triggered.connect(lambda: self.submit("bookmark", self.backend.bookmark))
        action_show = QAction("Open dogears", self)
        action_show.triggered.connect(self.show_from_tray)
        action_folder = QAction("Open recordings", self)
        action_folder.triggered.connect(self.open_recordings)
        action_quit = QAction("Quit", self)
        action_quit.triggered.connect(self.quit_app)
        for action in (self.action_toggle, self.action_bookmark):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(action_show)
        menu.addAction(action_folder)
        menu.addSeparator()
        menu.addAction(action_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
        self.refresh_buttons()

    def replace_hotkeys(self, new_cfg: Config):
        old = self.hotkeys
        if old:
            old.stop()
        self.hotkeys = None
        try:
            listener = HotkeyListener(new_cfg.record_hotkey, new_cfg.bookmark_hotkey, self.bridge.hotkey.emit)
            listener.start()
            self.hotkeys = listener
        except RuntimeError:
            if old:
                # Re-register the previous bindings if new ones conflict.
                recovery = HotkeyListener(self.cfg.record_hotkey, self.cfg.bookmark_hotkey, self.bridge.hotkey.emit)
                try:
                    recovery.start()
                    self.hotkeys = recovery
                except RuntimeError:
                    pass
            raise

    @Slot(str)
    def on_hotkey(self, action: str):
        if action == "toggle":
            self.submit("toggle", self.backend.toggle)
        elif action == "bookmark":
            self.submit("bookmark", self.backend.bookmark)

    def submit(self, action: str, operation):
        if self.is_busy:
            return
        self.is_busy = True
        self.refresh_buttons()
        future = self.pool.submit(operation)

        def completed(fut):
            try:
                result = fut.result()
                self.bridge.completed.emit(action, result, None)
            except Exception as exc:
                self.bridge.completed.emit(action, None, exc)

        future.add_done_callback(completed)

    @Slot(str, object, object)
    def on_completed(self, action: str, result: Any, exc: Exception | None):
        self.is_busy = False
        self.is_recording = self.backend.active is not None
        if exc:
            self.status.setText(str(exc))
            self.status.setToolTip(str(exc))
            if action != "check":
                self.tray.showMessage(APP_TITLE, str(exc), QSystemTrayIcon.MessageIcon.Warning, 3500)
        elif action == "check":
            self.obs_folder = result.get("folder")
            name = result["version"]
            if result["externally_recording"] and not self.is_recording:
                self.status.setText(f"OBS {name} connected — already recording outside dogears")
            else:
                self.status.setText(f"OBS {name} connected — ready")
        elif result["event"] == "started":
            self.bookmark_count = 0
            self.status.setText("Recording")
            self.sounds.play("started")
        elif result["event"] == "bookmarked":
            self.bookmark_count = result["count"]
            self.status.setText(f"Bookmark at {result['bookmark']['timecode']}")
            self.sounds.play("bookmarked")
        elif result["event"] == "stopped":
            self.last_recording = result["path"]
            self.status.setText(f"Saved recording — {result['count']} bookmarks")
            self.sounds.play("stopped")
            if result.get("warning"):
                self.tray.showMessage(APP_TITLE, result["warning"], QSystemTrayIcon.MessageIcon.Warning, 5000)
        self.refresh_buttons()

    def refresh_buttons(self):
        active = self.is_recording
        self.toggle_button.setText("Stop recording" if active else "Start recording")
        self.toggle_button.setEnabled(not self.is_busy)
        self.bookmark_button.setEnabled(active and not self.is_busy)
        self.count.setText(f"Bookmarks: {self.bookmark_count}")
        self.action_toggle.setText("Stop recording" if active else "Start recording")
        self.action_toggle.setEnabled(not self.is_busy)
        self.action_bookmark.setEnabled(active and not self.is_busy)
        icon = tray_icon(active)
        self.tray.setIcon(icon)
        self.setWindowIcon(icon)
        self.tray.setToolTip(f"dogears — {'recording' if active else 'idle'}")

    def open_settings(self):
        if self.is_busy:
            return
        dialog = SettingsDialog(self.cfg, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_cfg = dialog.selected_config()
        try:
            if (new_cfg.record_hotkey, new_cfg.bookmark_hotkey) != (self.cfg.record_hotkey, self.cfg.bookmark_hotkey):
                self.replace_hotkeys(new_cfg)
            if dialog.clear_password.isChecked():
                for service in (PASSWORD_SERVICE, LEGACY_PASSWORD_SERVICE):
                    try:
                        keyring.delete_password(service, PASSWORD_USER)
                    except keyring.errors.PasswordDeleteError:
                        pass
            if dialog.password.text():
                keyring.set_password(PASSWORD_SERVICE, PASSWORD_USER, dialog.password.text())
            new_cfg.save(self.config_path)
            self.cfg = new_cfg
            self.sounds.configure(new_cfg.sounds_enabled, new_cfg.volume)
            self.submit("check", self.backend.check)
        except Exception as exc:
            QMessageBox.warning(self, "Cannot save settings", str(exc))

    def open_recordings(self):
        # While recording, OBS knows the output folder; use latest one otherwise.
        p = (Path(self.last_recording).parent if self.last_recording else
             Path(self.obs_folder) if self.obs_folder else self.root / "sessions")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def closeEvent(self, event):
        if self.tray.isVisible():
            self.hide()
            event.ignore()
            if not self.has_hidden_once:
                self.has_hidden_once = True
                self.tray.showMessage(APP_TITLE, "Running in the system tray. Hotkeys remain active.",
                                      QSystemTrayIcon.MessageIcon.Information, 2000)
        else:
            event.accept()

    def quit_app(self):
        if self.is_busy:
            QMessageBox.information(self, "dogears", "Finish the current operation before quitting.")
            return
        if self.is_recording:
            response = QMessageBox.question(
                self, "Recording is active",
                "OBS will keep recording if you quit. The current bookmarks are saved, but the final "
                "recording file will not be linked automatically. Quit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        if self.hotkeys:
            self.hotkeys.stop()
        self.tray.hide()
        self.pool.shutdown(wait=False, cancel_futures=True)
        QApplication.instance().quit()


def run():
    if sys.platform != "win32":
        print("dogears is a Windows-only application (global hotkeys use the Windows API).")
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)
    root = data_dir()
    path = root / "settings.json"
    cfg = Config.load(path)
    window = MainWindow(cfg, path, root)
    window.show()
    return app.exec()
