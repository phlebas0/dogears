"""Two global Windows RegisterHotKey bindings, without keyboard hooks."""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

from .core import hotkey_parts

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000


class HotkeyListener:
    def __init__(self, recording_key: str, bookmark_key: str, on_hotkey: Callable[[str], None]):
        self.combos = {1: hotkey_parts(recording_key), 2: hotkey_parts(bookmark_key)}
        self.on_hotkey = on_hotkey
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self._error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._listen, name="dogearsHotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3):
            self.stop()
            raise RuntimeError("Timed out while registering global hotkeys.")
        if self._error:
            self.stop()
            raise RuntimeError(self._error)

    def _listen(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        registered = []
        try:
            self._tid = int(kernel32.GetCurrentThreadId())
            for hotkey_id, (mods, key) in self.combos.items():
                if not user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, key):
                    self._error = f"Windows refused a hotkey (ID {hotkey_id}, error {ctypes.get_last_error()}). Another program may be using it."
                    return
                registered.append(hotkey_id)
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and int(msg.wParam) in self.combos:
                    name = "toggle" if int(msg.wParam) == 1 else "bookmark"
                    self.on_hotkey(name)
        except Exception as exc:
            self._error = f"Hotkey listener failed: {exc}"
        finally:
            self._ready.set()
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._tid = 0

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            if self._tid:
                ctypes.WinDLL("user32").PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)
        self._thread = None
