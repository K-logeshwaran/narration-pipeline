"""Word-level timing: where inside each segment every word is spoken.

Three sources, in order of preference:

  1. "tokens"    the TTS backend returned per-token timestamps (Kokoro when the
                 installed version exposes them, the fake backend in tests).
                 Token times refer to the RAW audio, so the trim offset found
                 during processing is subtracted.
  2. "aligned"   energy-island detection on the processed segment WAV: when the
                 number of speech islands equals the word count the mapping is
                 1:1 and trustworthy.
  3. "estimated" words are spread over the detected speech span (or the whole
                 segment when no speech is found) proportional to their length.
                 Same idea as the old character estimate, but anchored to real
                 speech instead of the raw segment bounds.

All returned spans are segment-local seconds (0 = start of the processed
segment WAV, which is exactly what the timeline places).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

Span = Tuple[float, float]
Token = Tuple[str, float, float]            # (text, start_s, end_s) in raw audio

DEFAULTS = {"rel_threshold": 0.2, "abs_threshold": 0.01,
            "min_island_s": 0.03, "merge_gap_s": 0.05}


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


# ---------------------------------------------------------------------------
# energy islands
# ---------------------------------------------------------------------------

def speech_islands(audio: np.ndarray, sr: int, wcfg: Dict[str, Any]) -> List[Span]:
    """Detect speech islands: runs of frames whose RMS stays above
    max(abs_threshold, rel_threshold * peak). Islands separated by less than
    merge_gap_s are merged; islands shorter than min_island_s are dropped."""
    cfg = {**DEFAULTS, **(wcfg or {})}
    audio = np.asarray(audio, dtype=np.float32)
    win = max(int(sr * 0.02), 1)
    hop = max(int(sr * 0.01), 1)
    if audio.size < win:
        return []
    csum = np.concatenate([[0.0], np.cumsum(audio.astype(np.float64) ** 2)])
    starts = np.arange(0, audio.size - win + 1, hop)
    rms = np.sqrt((csum[starts + win] - csum[starts]) / win)
    peak = float(rms.max())
    if peak <= 0.0:
        return []
    thr = max(float(cfg["abs_threshold"]), float(cfg["rel_threshold"]) * peak)
    active = rms >= thr
    if not active.any():
        return []

    islands: List[Span] = []
    i = 0
    n = len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and active[j + 1]:
            j += 1
        islands.append((starts[i] / sr, min((starts[j] + win) / sr, audio.size / sr)))
        i = j + 1

    merge = float(cfg["merge_gap_s"])
    merged: List[Span] = []
    for s, e in islands:
        if merged and s - merged[-1][1] < merge:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    min_len = float(cfg["min_island_s"])
    return [(s, e) for s, e in merged if e - s >= min_len]


# ---------------------------------------------------------------------------
# token timestamps -> word spans
# ---------------------------------------------------------------------------

def spans_from_tokens(words: Sequence[str], tokens: Sequence[Token],
                      trim_offset_s: float, duration: float) -> Optional[List[Span]]:
    """Group backend tokens into the segment's words by matching their
    normalized characters. Returns None when the token stream does not line up
    with the words (caller falls back to audio alignment)."""
    if not words or not tokens:
        return None
    spans: List[Span] = []
    ti = 0
    for word in words:
        target = _norm(word)
        if not target:
            return None
        acc = ""
        start = None
        end = None
        while ti < len(tokens) and len(acc) < len(target):
            text, ts, te = tokens[ti]
            ti += 1
            if not _norm(text):
                continue                            # punctuation-only token
            if start is None:
                start = ts
            end = te
            acc += _norm(text)
        if acc != target or start is None:
            return None
        spans.append((max(start - trim_offset_s, 0.0),
                      min(max(end - trim_offset_s, 0.0), duration)))
    if any(e < s for s, e in spans):
        return None
    return spans


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def word_spans(words: Sequence[str], audio: np.ndarray, sr: int,
               wcfg: Optional[Dict[str, Any]] = None,
               tokens: Optional[Sequence[Token]] = None,
               trim_offset_s: float = 0.0) -> Tuple[List[Span], str]:
    """Returns (spans, method) with method in {"tokens", "aligned", "estimated"}.
    spans[i] is the (start, end) of words[i] in segment-local seconds."""
    words = list(words)
    duration = len(audio) / float(sr) if sr else 0.0
    if not words:
        return [], "estimated"

    if tokens:
        spans = spans_from_tokens(words, tokens, trim_offset_s, duration)
        if spans is not None:
            return spans, "tokens"

    islands = speech_islands(audio, sr, wcfg or {})
    if len(islands) == len(words):
        return islands, "aligned"

    # proportional spread over the detected speech span (or the whole segment)
    lo, hi = (islands[0][0], islands[-1][1]) if islands else (0.0, duration)
    weights = [max(len(_norm(w)), 1) for w in words]
    total = float(sum(weights))
    spans = []
    cursor = lo
    for w in weights:
        nxt = lo + (hi - lo) * (sum(weights[: len(spans) + 1]) / total)
        spans.append((cursor, nxt))
        cursor = nxt
    return spans, "estimated"
