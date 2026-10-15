"""Audio processing: trim, fade, gain, silence, mixing, ffmpeg polish, loudness.

Carried over from the tested narrate.py (same trim/fade/polish ideas) with the
changes agreed in the design discussion:
  * no room-tone noise by default (noise_floor_amp = 0)
  * loudnorm is NOT applied per segment (it would flatten whisper vs shout);
    it runs once, two-pass, on the full narration export
  * emotion volume is a static gain applied AFTER the compressor
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from .errors import AudioToolError


# ---------------------------------------------------------------------------
# numpy helpers
# ---------------------------------------------------------------------------

def _trim_bounds(audio: np.ndarray, sr: int, threshold: float, keep_s: float) -> Tuple[int, int]:
    """Sample range [first, last) that trim_silence keeps."""
    mask = np.abs(audio) > threshold
    if not mask.any():
        return 0, len(audio)
    first = int(np.argmax(mask))
    last = len(mask) - 1 - int(np.argmax(mask[::-1]))
    keep = int(sr * keep_s)
    return max(0, first - keep), min(len(audio), last + keep)


def trim_silence(audio: np.ndarray, sr: int, threshold: float, keep_s: float) -> np.ndarray:
    """Cut Kokoro's leading/trailing dead air, keeping a small natural pad
    instead of a flush cut. Returns the input unchanged if it is all below
    the threshold (the caller decides whether that counts as 'empty')."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return audio
    first, last = _trim_bounds(audio, sr, threshold, keep_s)
    return audio[first:last].copy()


def trim_offset_s(audio: np.ndarray, sr: int, threshold: float, keep_s: float) -> float:
    """Seconds cut from the START of raw audio by trim_silence. Word timestamps
    that refer to the raw audio are shifted left by this amount."""
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return 0.0
    return _trim_bounds(audio, sr, threshold, keep_s)[0] / float(sr)


def fade_edges(audio: np.ndarray, sr: int, fade_s: float, curve: str = "linear") -> np.ndarray:
    """Short fade in/out so clips never click at their edges."""
    n = int(sr * fade_s)
    if audio.size < 2 * n or n <= 0:
        return audio
    audio = audio.copy()
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    ramp = (0.5 - 0.5 * np.cos(np.pi * t)) if curve == "cosine" else t
    audio[:n] *= ramp
    audio[-n:] *= ramp[::-1]
    return audio


def silence(duration_s: float, sr: int, noise_amp: float = 0.0, seed: int = 0) -> np.ndarray:
    n = max(int(round(sr * duration_s)), 0)
    if noise_amp <= 0 or n == 0:
        return np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * noise_amp).astype(np.float32)


def apply_gain(audio: np.ndarray, gain: float, peak_limit: Optional[float] = None) -> np.ndarray:
    """Static gain. If the result would peak above peak_limit, scale it back
    (a safety net against clipping, not a limiter)."""
    out = np.asarray(audio, dtype=np.float32) * np.float32(gain)
    if peak_limit and out.size:
        peak = float(np.max(np.abs(out)))
        if peak > peak_limit:
            out = out * np.float32(peak_limit / peak)
    return out


def is_silent(audio: np.ndarray, floor: float = 1e-4) -> bool:
    return audio.size == 0 or not np.isfinite(audio).all() or float(np.max(np.abs(audio))) < floor


def mix_segments(placements: Iterable[Tuple[float, np.ndarray]], total_s: float, sr: int) -> np.ndarray:
    """Place clips on a silent track at their start times (seconds)."""
    buf = np.zeros(int(round(total_s * sr)) + 1, dtype=np.float32)
    for start_s, clip in placements:
        i = int(round(start_s * sr))
        j = min(i + len(clip), len(buf))
        if j > i:
            buf[i:j] += clip[: j - i]
    return buf


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run_ffmpeg(args: List[str]) -> subprocess.CompletedProcess:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioToolError("ffmpeg was not found on PATH. Install it (Fedora: enable RPM Fusion, "
                             "then `sudo dnf install ffmpeg`) or run with --no-polish --no-export.")
    proc = subprocess.run([exe, "-hide_banner", "-nostats", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise AudioToolError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")
    return proc


def pitch_filter(semitones: float, sr: int) -> str:
    """Pitch shift by resampling + tempo correction. EXPERIMENTAL and off by default:
    judge the natural voice first, large shifts sound artificial."""
    if abs(semitones) < 1e-6:
        return ""
    ratio = 2.0 ** (semitones / 12.0)
    return f"asetrate={int(round(sr * ratio))},aresample={sr},atempo={1.0 / ratio:.6f}"


def build_filter_chain(audio_cfg: Dict[str, Any], polish: bool, pitch_semitones: float, sr: int) -> str:
    parts = []
    p = pitch_filter(pitch_semitones, sr)
    if p:
        parts.append(p)
    if polish and audio_cfg.get("ffmpeg_filter"):
        parts.append(audio_cfg["ffmpeg_filter"])
    return ",".join(parts)


def ffmpeg_filter_file(src: Path, dst: Path, chain: str, sr: int) -> None:
    _run_ffmpeg(["-y", "-loglevel", "error", "-i", str(src), "-af", chain,
                 "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])


_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def parse_loudnorm_json(stderr: str) -> Dict[str, float]:
    m = _LOUDNORM_JSON.search(stderr)
    if not m:
        raise AudioToolError("Could not read loudnorm measurements from ffmpeg output.")
    raw = json.loads(m.group(0))
    out = {}
    for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        out[k] = float(raw[k])
    return out


def loudnorm_two_pass(src: Path, dst: Path, sr: int, target_i: float, target_tp: float,
                      target_lra: float) -> Optional[Dict[str, float]]:
    """Measure, then apply a LINEAR gain (no dynamic pumping). Returns the
    measurements, or None if the input is silent (file is just copied)."""
    base = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}"
    p1 = _run_ffmpeg(["-i", str(src), "-af", f"{base}:print_format=json", "-f", "null", "-"])
    try:
        m = parse_loudnorm_json(p1.stderr)
    except (AudioToolError, ValueError, KeyError):
        raise AudioToolError("loudnorm measurement failed; try --no-export or check the narration audio.")
    if not np.isfinite(m["input_i"]):
        shutil.copyfile(src, dst)
        return None
    af = (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
          f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    _run_ffmpeg(["-y", "-i", str(src), "-af", af, "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
    return m
