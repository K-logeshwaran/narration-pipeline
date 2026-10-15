"""SFX mix: lay the timeline's resolved sfx events over the full narration.

    full_narration.wav + timeline.json sfx events + assets folder -> audio/full_mix.wav

Rules:
  point   played once at the event's start (offset_s is already baked into
          the start by the timeline), gained by mix.point_gain, clipped at the
          end of the video
  bed     looped to fill start -> end, gained by mix.bed_gain, with a short
          linear fade in/out (mix.bed_fade_s) so loops and cuts never click

Files are read directly when they are 16-bit WAV at the narration sample rate;
anything else (other sample rates, mp3/ogg/flac) is decoded through ffmpeg
into a temp WAV. Missing or undecodable files produce a warning and are
skipped - a mix with fewer sounds is better than no mix. Music and ducking
belong to the later final-mix stage, not here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from . import audio as A
from .errors import AudioToolError
from .wavio import read_wav, write_wav

DEFAULTS = {"point_gain": 1.0, "bed_gain": 0.35, "bed_fade_s": 0.25,
            "out_name": "full_mix.wav"}


def load_sfx_file(path: Path, sr: int) -> np.ndarray:
    """Load an sfx file as float32 mono at sr. Direct read for 16-bit WAV at
    the right rate; everything else goes through ffmpeg."""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            data, file_sr = read_wav(path)
            if file_sr == sr:
                return data
        except (ValueError, OSError):
            pass                                            # fall through to ffmpeg
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "sfx.wav"
        A._run_ffmpeg(["-y", "-loglevel", "error", "-i", str(path),
                       "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst)])
        return read_wav(dst)[0]


def loop_to(clip: np.ndarray, n: int) -> np.ndarray:
    """Repeat or cut a clip to exactly n samples."""
    if len(clip) == 0 or n <= 0:
        return np.zeros(max(n, 0), dtype=np.float32)
    reps = int(np.ceil(n / len(clip)))
    return np.tile(clip, reps)[:n]


def bed_clip(clip: np.ndarray, duration_s: float, sr: int, fade_s: float) -> np.ndarray:
    """Loop a bed to duration and fade both edges."""
    n = int(round(duration_s * sr))
    out = loop_to(clip, n)
    f = min(int(sr * fade_s), n // 2)
    if f > 0:
        ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)
        out[:f] *= ramp
        out[-f:] *= ramp[::-1]
    return out


def mix_sfx(timeline: Dict[str, Any], narration_path: Path, assets_dir: Path,
            cfg: Dict[str, Any], out_path: Path,
            log: Callable[[str], None] = print) -> Tuple[Path, List[str], Dict[str, int]]:
    """Returns (out_path, warnings, stats). stats counts placed/skipped events."""
    mcfg = {**DEFAULTS, **(cfg.get("mix") or {})}
    sfx_sub = cfg.get("assets", {}).get("sfx", "sfx")
    base, sr = read_wav(narration_path)
    total_s = len(base) / float(sr)
    mix = base.copy()
    warnings: List[str] = []
    stats = {"point": 0, "bed": 0, "skipped": 0}
    cache: Dict[str, Optional[np.ndarray]] = {}

    events = [e for e in timeline.get("events", []) if e.get("type") == "sfx"]
    for ev in events:
        name = ev["name"]
        if name not in cache:
            path = Path(assets_dir) / ev.get("file", f"{sfx_sub}/{name}.wav")
            if not path.exists():
                warnings.append(f"SFX file not found, skipped: {path}")
                cache[name] = None
            else:
                try:
                    cache[name] = load_sfx_file(path, sr)
                except (AudioToolError, ValueError, OSError) as e:
                    warnings.append(f"Could not decode {path}, skipped: {e}")
                    cache[name] = None
        clip = cache[name]
        if clip is None:
            stats["skipped"] += 1
            continue

        if ev.get("kind") == "bed":
            dur = float(ev.get("end", total_s)) - ev["start"]
            if dur <= 0:
                warnings.append(f'Bed "{name}" has no room (start >= end), skipped.')
                stats["skipped"] += 1
                continue
            clip = bed_clip(clip, dur, sr, float(mcfg["bed_fade_s"]))
            gain = float(mcfg["bed_gain"])
            stats["bed"] += 1
        else:
            gain = float(mcfg["point_gain"])
            stats["point"] += 1

        i = int(round(ev["start"] * sr))
        j = min(i + len(clip), len(mix))
        if j > i:
            mix[i:j] += clip[: j - i] * np.float32(gain)

    mix = A.apply_gain(mix, 1.0, cfg["audio"].get("peak_limit"))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out_path, mix, sr)
    log(f"Mixed {stats['point']} point sfx + {stats['bed']} beds over {total_s:.1f}s of narration"
        + (f" ({stats['skipped']} skipped)" if stats["skipped"] else ""))
    return out_path, warnings, stats
