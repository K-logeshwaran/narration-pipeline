#!/usr/bin/env python3
"""Find out WHY two WAV files sound different.

    python tools/compare_audio.py audio/full_narration.wav audio/full_mix.wav

Prints level / loudness / noise-floor numbers for each file, then a null test:
  * identical            -> they are the same audio (you are hearing a placebo or a volume difference)
  * same audio, B scaled -> B is just louder/quieter: the fix is a gain, not a mix
  * B has extra content  -> something was ADDED (sfx, beds, noise): shows where
16-bit PCM WAV only (what this pipeline writes).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from narration.wavio import read_wav  # noqa: E402

LSB = 1.0 / 32768.0


def db(x: float) -> float:
    return 20.0 * np.log10(max(float(x), 1e-12))


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


def ebur128(path: str):
    if not shutil.which("ffmpeg"):
        return None, None
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    summary = p.stderr.split("Summary:")[-1]
    i = re.findall(r"\bI:\s+(-?\d+\.\d+) LUFS", summary)
    pk = re.findall(r"Peak:\s+(-?\d+\.\d+) dBFS", summary)
    return (float(i[-1]) if i else None), (float(pk[-1]) if pk else None)


def stats(path: str):
    x, sr = read_wav(path)
    win = int(0.05 * sr)
    frames = x[: (len(x) // win) * win].reshape(-1, win)
    win_rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)) if len(frames) else np.zeros(1)
    active = x[np.abs(x) > 0.003]
    lufs, tp = ebur128(path)
    return x, sr, {
        "duration (s)": len(x) / sr,
        "sample peak (dBFS)": db(np.max(np.abs(x))),
        "loudness (LUFS)": lufs,
        "true peak (dBTP)": tp,
        "voice level, loud samples (dBFS RMS)": db(rms(active)),
        "quietest 10% of 50ms windows (dBFS)": db(np.percentile(win_rms, 10)),
        "exact digital silence (% of samples)": 100.0 * float(np.mean(x == 0)),
    }


def fmt(v):
    return "n/a" if v is None else f"{v:9.2f}"


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    a, sr_a, sa = stats(argv[1])
    b, sr_b, sb = stats(argv[2])
    print(f"\nA = {argv[1]}\nB = {argv[2]}\n")
    print(f"{'':42}{'A':>10}{'B':>10}{'B - A':>10}")
    for k in sa:
        va, vb = sa[k], sb[k]
        diff = None if va is None or vb is None else vb - va
        print(f"{k:42}{fmt(va):>10}{fmt(vb):>10}{fmt(diff):>10}")

    print("\nNULL TEST")
    if sr_a != sr_b:
        print(f"  Different sample rates ({sr_a} vs {sr_b}); cannot compare sample by sample.")
        return 0
    if abs(len(a) - len(b)) > 0.01 * sr_a:
        print(f"  WARNING: lengths differ by {abs(len(a) - len(b)) / sr_a:.2f}s; comparing the overlap only.")
    n = min(len(a), len(b))
    a, b = a[:n].astype(np.float64), b[:n].astype(np.float64)
    max_lsb = float(np.max(np.abs(a - b))) / LSB
    k = float(np.dot(a, b) / max(np.dot(a, a), 1e-12))            # B ~ k * A
    resid = b - k * a
    resid_db = db(rms(resid) / max(rms(b), 1e-12))
    print(f"  largest sample difference: {max_lsb:.1f} LSB")
    if max_lsb <= 2.0:
        print("  VERDICT: IDENTICAL (within rounding). Same audio; any difference you hear is perceived "
              "loudness or expectation. Compare again at matched volume.")
        return 0
    if resid_db < -60:
        print(f"  VERDICT: SAME AUDIO, DIFFERENT LEVEL. B = A x {k:.3f} ({db(k):+.2f} dB). "
              f"Nothing was added; the fix is a gain, not a mix.")
        return 0
    print(f"  VERDICT: B HAS EXTRA CONTENT or different processing "
          f"(residual {resid_db:.1f} dB below B after matching level; best-fit level change {db(k):+.2f} dB).")
    w = int(0.1 * sr_a)
    m = (n // w) * w
    per = np.sqrt(np.mean(resid[:m].reshape(-1, w) ** 2, axis=1))
    order = np.argsort(per)[::-1]
    picked = []
    for idx in order:
        if per[idx] < 1e-4 or len(picked) == 6:
            break
        if all(abs(idx - p) > 5 for p in picked):
            picked.append(int(idx))
    print("  Biggest differences (these are where sounds were added):")
    for idx in sorted(picked):
        print(f"    {idx * 0.1:7.1f}s   {db(per[idx]):6.1f} dBFS")
    quiet_a = np.percentile(np.sqrt(np.mean(a[:m].reshape(-1, w) ** 2, axis=1)), 10)
    quiet_b = np.percentile(np.sqrt(np.mean(b[:m].reshape(-1, w) ** 2, axis=1)), 10)
    if db(quiet_b) - db(quiet_a) > 6 and db(quiet_b) > -70:
        print("  NOTE: B's quiet moments are much louder than A's: something is filling the silence "
              "(a bed or a noisy sfx file), not just point sounds.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))