"""Preloaded adjustable-volume low-latency WAV sounds."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtMultimedia import QSoundEffect


def make_tone(path: Path, frequencies: tuple[float, ...], duration: float = 0.14):
    if path.exists():
        return
    rate = 22050
    count = int(rate * duration)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        samples = []
        for i in range(count):
            t = i / rate
            ramp = min(1.0, i / (rate * 0.008), (count - i) / (rate * 0.025))
            wave_sum = sum(math.sin(2 * math.pi * f * t) for f in frequencies) / len(frequencies)
            samples.append(struct.pack("<h", int(9000 * max(0, ramp) * wave_sum)))
        wav.writeframes(b"".join(samples))


class NotificationSounds(QObject):
    def __init__(self, folder: Path, parent=None):
        super().__init__(parent)
        self.effects: dict[str, QSoundEffect] = {}
        for name, tones in {"started": (650, 880), "bookmarked": (1050,), "stopped": (600, 420)}.items():
            path = folder / f"{name}.wav"
            make_tone(path, tones)
            effect = QSoundEffect(self)
            effect.setSource(QUrl.fromLocalFile(str(path)))
            self.effects[name] = effect
        self.enabled = True

    def configure(self, enabled: bool, volume: int):
        self.enabled = enabled
        for effect in self.effects.values():
            effect.setVolume(volume / 100.0)

    def play(self, name: str) -> None:
        if self.enabled and name in self.effects:
            self.effects[name].play()
