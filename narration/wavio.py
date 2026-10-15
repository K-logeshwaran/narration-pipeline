"""16-bit mono WAV reading/writing with only the standard library + numpy.

No SoundFile dependency. Writes are atomic (temp file + rename) so an
interrupted run can never leave a half-written file in the cache.
"""
from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Tuple

import numpy as np


def read_wav(path) -> Tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        channels, width, sr, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported (got {width * 8}-bit)")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, sr


def wav_duration(path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def write_wav(path, audio: np.ndarray, sr: int) -> None:
    x = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    pcm = np.round(x * 32767.0).astype("<i2")
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)
