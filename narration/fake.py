"""A deterministic stand-in for Kokoro, for tests and dry runs.

It is NOT speech. It makes one tone burst per word, with the same kind of dead
air Kokoro leaves at both ends (so trimming has something to do), and its
duration scales with word count and speed. Same text + speed => identical samples.
"""
from __future__ import annotations

import hashlib
from typing import List, Tuple

import numpy as np


class FakeBackend:
    def __init__(self, sample_rate: int = 24000, lead_s: float = 0.25, tail_s: float = 0.35,
                 word_s: float = 0.30):
        self.sample_rate = sample_rate
        self.lead_s, self.tail_s, self.word_s = lead_s, tail_s, word_s
        self.calls: List[Tuple[str, float]] = []     # tests use this to count real syntheses

    @property
    def id(self) -> str:
        return "fake:v1"

    def synth(self, text: str, speed: float) -> np.ndarray:
        return self.synth_timed(text, speed)[0]

    def synth_timed(self, text: str, speed: float) -> Tuple[np.ndarray, List[Tuple[str, float, float]]]:
        """Same audio as synth, plus exact per-word timestamps (one tone burst
        per word), so tests can exercise the token-timing path."""
        self.calls.append((text, speed))
        sr = self.sample_rate
        words = text.split()
        if not words:
            return np.zeros(0, dtype=np.float32), []
        tokens: List[Tuple[str, float, float]] = []
        parts = [np.full(int(sr * self.lead_s), 0.0004, dtype=np.float32)]
        pos = len(parts[0])
        for w in words:
            seed = int(hashlib.sha1(w.encode("utf-8")).hexdigest()[:8], 16)
            freq = 110.0 + (seed % 150)
            n = int(sr * self.word_s / max(speed, 0.1))
            t = np.arange(n, dtype=np.float32) / sr
            env = np.clip(np.sin(np.pi * np.arange(n, dtype=np.float32) / max(n - 1, 1)), 0.0, None) ** 0.5
            parts.append((0.3 * env * np.sin(2 * np.pi * freq * t)).astype(np.float32))
            tokens.append((w, pos / sr, (pos + n) / sr))
            pos += n
            parts.append(np.full(int(sr * 0.04), 0.0004, dtype=np.float32))
            pos += len(parts[-1])
        tail_n = int(sr * self.tail_s)
        parts.append((0.01 * np.exp(-np.linspace(0, 8, tail_n))).astype(np.float32))
        return np.concatenate(parts), tokens
