"""Rhubarb lip-sync stage: narration.json + segment WAVs -> cache/proc/*.rhubarb.json + lipsync.json.

Per segment:
  1. Writes the segment's display text (`seg["text"]`) to a temporary dialog file.
  2. Runs Rhubarb (`-d dialog.txt -r pocketSphinx -f json`), falling back to
     `-r phonetic` if the primary recognizer fails.
  3. Caches segment cues in `cache/proc/<proc_key>.rhubarb.json` keyed by
     `{proc_key, text, used_recognizer, rhubarb_version}` so a fallback result is
     recorded under the fallback recognizer's key and never mistaken for a
     primary cache hit.
  4. Merges segment cues across each scene (filling pauses/gaps with "X") and
     generates deterministic per-scene blink windows, writing `lipsync.json`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .errors import Problem

LIPSYNC_VERSION = "1.0"
KEY_VERSION = "r1"
FAKE_SHAPES = ("A", "C", "D", "B", "E", "F")

DEFAULTS = {
    "bin": "~/.local/opt/rhubarb/rhubarb",
    "recognizer": "pocketSphinx",
    "fallback_recognizer": "phonetic",
    "max_workers": 3,
    "timeout_s": 60,
    "blink": {
        "enabled": True,
        "min_gap_s": 2.5,
        "max_gap_s": 5.0,
        "duration_s": 0.14,
        "seed": 7,
    },
}


class RhubarbError(Exception):
    """Raised when Rhubarb is missing or fails on both primary and fallback recognizers."""

    def __init__(self, problems: List[Problem]):
        self.problems = problems
        super().__init__("\n\n".join(p.format() for p in problems))


@dataclass
class LipsyncResult:
    lipsync: Dict[str, Any]
    out_path: Path
    stats: Dict[str, int]
    warnings: List[str] = field(default_factory=list)


def _hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _r(x: float) -> float:
    return round(float(x), 3)


def make_rhubarb_key(proc_key: str, text: str, recognizer: str, backend_tag: str) -> str:
    """Compute a content-addressed cache key for a segment's Rhubarb analysis."""
    return _hash({
        "v": KEY_VERSION,
        "proc_key": proc_key,
        "text": text,
        "recognizer": recognizer,
        "backend": backend_tag,
    })


def find_rhubarb(cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Locate the rhubarb executable from config, PATH, or standard install paths."""
    rcfg = (cfg or {}).get("rhubarb") or {}
    candidates: List[Path] = []
    if rcfg.get("bin"):
        candidates.append(Path(str(rcfg["bin"])).expanduser())
    which_hit = shutil.which("rhubarb")
    if which_hit:
        candidates.append(Path(which_hit))
    candidates.extend([
        Path.home() / ".local" / "opt" / "rhubarb" / "rhubarb",
        Path("/usr/local/bin/rhubarb"),
        Path("/usr/bin/rhubarb"),
    ])
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def rhubarb_available(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return find_rhubarb(cfg) is not None


@lru_cache(maxsize=8)
def rhubarb_version(exe: str) -> str:
    """Return the version string of the Rhubarb binary (memoized per process)."""
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        m = re.search(r"version\s+([0-9A-Za-z.\-_]+)", out, re.I)
        return m.group(1) if m else out.strip().splitlines()[0].strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Cue normalization & merging
# ---------------------------------------------------------------------------

def normalize_cues(raw_cues: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Sort cues, clamp to [0, duration], prepend (0.0 -> X) if needed, and
    merge consecutive identical mouth shapes."""
    dur = max(float(duration), 0.0)
    if dur <= 0.0:
        return [{"start": 0.0, "end": 0.0, "value": "X"}]

    items: List[Tuple[float, str]] = []
    for c in raw_cues:
        start = min(max(float(c.get("start", 0.0)), 0.0), dur)
        shape = str(c.get("value") or c.get("shape") or "X").strip().upper()
        if start < dur:
            items.append((start, shape))
    items.sort(key=lambda x: x[0])

    if not items or items[0][0] > 1e-4:
        items.insert(0, (0.0, "X"))
    else:
        items[0] = (0.0, items[0][1])

    # Collapse identical timestamps (last wins) and consecutive identical shapes
    dedup_time: List[Tuple[float, str]] = []
    for start, shape in items:
        if dedup_time and abs(start - dedup_time[-1][0]) < 1e-4:
            dedup_time[-1] = (dedup_time[-1][0], shape)
        else:
            dedup_time.append((start, shape))

    merged: List[Tuple[float, str]] = []
    for start, shape in dedup_time:
        if merged and merged[-1][1] == shape:
            continue
        merged.append((start, shape))

    out: List[Dict[str, Any]] = []
    for i, (start, shape) in enumerate(merged):
        end = merged[i + 1][0] if i + 1 < len(merged) else dur
        if end - start > 1e-5:
            out.append({"start": _r(start), "end": _r(end), "value": shape})
    if not out:
        out.append({"start": 0.0, "end": _r(dur), "value": "X"})
    else:
        out[-1]["end"] = _r(dur)
    return out


def build_scene_mouth_cues(scene_start: float, scene_end: float,
                           seg_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a continuous mouth cue track for [scene_start, scene_end], filling
    silence/pauses between segments with closed mouth 'X'."""
    points: List[Dict[str, Any]] = [{"start": scene_start, "value": "X"}]
    for seg in sorted(seg_records, key=lambda s: s["start"]):
        s_start = max(float(seg["start"]), scene_start)
        s_end = min(float(seg["end"]), scene_end)
        if s_end <= s_start:
            continue
        for c in seg.get("abs_cues", []):
            cs = max(float(c["start"]), s_start)
            if cs < s_end:
                points.append({"start": cs, "value": c["value"]})
        if s_end < scene_end:
            points.append({"start": s_end, "value": "X"})

    # Convert to scene-relative for normalization, then shift back to absolute
    rel_raw = [{"start": p["start"] - scene_start, "value": p["value"]} for p in points]
    rel_norm = normalize_cues(rel_raw, max(scene_end - scene_start, 0.0))
    return [
        {
            "start": _r(scene_start + c["start"]),
            "end": _r(scene_start + c["end"]),
            "value": c["value"],
        }
        for c in rel_norm
    ]


def make_scene_blinks(scene_start: float, scene_end: float, scene_number: int,
                      bcfg: Dict[str, Any]) -> List[List[float]]:
    """Deterministic blink windows [[start, end], ...] in absolute timeline seconds."""
    if not bcfg.get("enabled", True):
        return []
    total = max(scene_end - scene_start, 0.0)
    min_gap = float(bcfg.get("min_gap_s", 2.5))
    max_gap = float(bcfg.get("max_gap_s", 5.0))
    dur = float(bcfg.get("duration_s", 0.14))
    seed = int(bcfg.get("seed", 7)) + int(scene_number) * 1009

    rng = random.Random(seed)
    t = rng.uniform(1.0, 2.5)
    blinks: List[List[float]] = []
    while t + dur < total - 0.3:
        blinks.append([_r(scene_start + t), _r(scene_start + t + dur)])
        t += rng.uniform(min_gap, max_gap)
    return blinks


# ---------------------------------------------------------------------------
# Fake Rhubarb generator (for tests & offline dry runs)
# ---------------------------------------------------------------------------

def fake_segment_cues(seg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deterministic stand-in for Rhubarb: places 'X' in gaps and cycles
    Preston Blair mouth shapes across each word span."""
    dur = float(seg.get("duration", 0.0))
    seg_start = float(seg.get("start", 0.0))
    raw: List[Dict[str, Any]] = [{"start": 0.0, "value": "X"}]
    for w_info in seg.get("words", []):
        w_text = str(w_info.get("w", ""))
        ws = max(float(w_info["start"]) - seg_start, 0.0)
        we = min(max(float(w_info["end"]) - seg_start, ws), dur)
        if we - ws <= 0.02:
            continue
        digest = hashlib.sha1(w_text.encode("utf-8")).digest()
        n_syllables = min(max(len(w_text) // 3, 1), 3)
        step = (we - ws) / n_syllables
        for idx in range(n_syllables):
            shape = FAKE_SHAPES[digest[idx % len(digest)] % len(FAKE_SHAPES)]
            raw.append({"start": ws + idx * step, "value": shape})
        raw.append({"start": we, "value": "X"})
    return normalize_cues(raw, dur)


# ---------------------------------------------------------------------------
# Subprocess runner for one segment
# ---------------------------------------------------------------------------

def _invoke_rhubarb(exe: str, wav_path: Path, dialog_text: str,
                    recognizer: str, timeout_s: float) -> List[Dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        dialog_file = tmp_dir / "dialog.txt"
        out_file = tmp_dir / "out.json"
        dialog_file.write_text(dialog_text.strip() + "\n", encoding="utf-8")

        cmd = [
            exe,
            str(wav_path),
            "-r", recognizer,
            "-d", str(dialog_file),
            "-f", "json",
            "-o", str(out_file),
            "--quiet",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
        if proc.returncode != 0 or not out_file.exists():
            stderr_tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
            raise RuntimeError(f"rhubarb ({recognizer}) exited with {proc.returncode}: {stderr_tail}")
        data = json.loads(out_file.read_text(encoding="utf-8"))
        return list(data.get("mouthCues") or [])


def _analyze_segment(seg: Dict[str, Any], project_dir: Path, proc_dir: Path,
                     rcfg: Dict[str, Any], backend_mode: str,
                     exe: Optional[str], ver: str,
                     force: bool) -> Dict[str, Any]:
    sid = seg["id"]
    scene_id = seg["scene"]
    proc_key = seg["cache_key"]
    dur = float(seg["duration"])
    text = str(seg.get("text", ""))
    primary_rec = "fake" if backend_mode == "fake" else str(rcfg.get("recognizer", "pocketSphinx"))
    fallback_rec = str(rcfg.get("fallback_recognizer", "phonetic"))
    timeout_s = float(rcfg.get("timeout_s", 60))

    backend_tag = "fake:v1" if backend_mode == "fake" else f"rhubarb:{ver}"
    primary_key = make_rhubarb_key(proc_key, text, primary_rec, backend_tag)
    side_path = proc_dir / f"{proc_key}.rhubarb.json"

    if not force and side_path.exists():
        try:
            cached = json.loads(side_path.read_text(encoding="utf-8"))
            if (cached.get("rhubarb_key") == primary_key
                    and cached.get("recognizer") == primary_rec
                    and isinstance(cached.get("cues"), list)):
                cues = normalize_cues(cached["cues"], dur)
                return {
                    "id": sid,
                    "status": "cache",
                    "rhubarb_key": primary_key,
                    "recognizer": primary_rec,
                    "cues": cues,
                    "warning": None,
                }
        except (OSError, ValueError, KeyError, TypeError):
            pass

    warning: Optional[str] = None
    if backend_mode == "fake":
        cues = fake_segment_cues(seg)
        used_rec = "fake"
        status = "analyzed"
    else:
        assert exe is not None
        wav_path = project_dir / seg["audio"]
        if not wav_path.exists():
            raise RhubarbError([Problem(f"Segment audio file missing: {wav_path}", scene=scene_id, segment=sid)])
        try:
            raw_cues = _invoke_rhubarb(exe, wav_path, text, primary_rec, timeout_s)
            used_rec = primary_rec
            status = "analyzed"
        except Exception as primary_err:
            if fallback_rec and fallback_rec != primary_rec:
                try:
                    raw_cues = _invoke_rhubarb(exe, wav_path, text, fallback_rec, timeout_s)
                    used_rec = fallback_rec
                    status = "fallback"
                    warning = (f'{sid}: primary recognizer "{primary_rec}" failed ({primary_err}); '
                               f'fell back to "{fallback_rec}".')
                except Exception as fb_err:
                    raise RhubarbError([Problem(
                        f'Rhubarb failed with both "{primary_rec}" and "{fallback_rec}": {fb_err}',
                        scene=scene_id, segment=sid, detail=f'Text: "{text}"'
                    )]) from fb_err
            else:
                raise RhubarbError([Problem(
                    f'Rhubarb failed with "{primary_rec}": {primary_err}',
                    scene=scene_id, segment=sid, detail=f'Text: "{text}"'
                )]) from primary_err
        cues = normalize_cues(raw_cues, dur)

    used_key = make_rhubarb_key(proc_key, text, used_rec, backend_tag)
    payload = {
        "rhubarb_key": used_key,
        "proc_key": proc_key,
        "recognizer": used_rec,
        "cues": cues,
    }
    tmp = side_path.with_name(side_path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, side_path)

    return {
        "id": sid,
        "status": status,
        "rhubarb_key": used_key,
        "recognizer": used_rec,
        "cues": cues,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lipsync_project(project_dir: Path, cfg: Dict[str, Any],
                    *, backend: str = "rhubarb",
                    only: Optional[Iterable[str]] = None,
                    force: bool = False,
                    log: Callable[[str], None] = print) -> LipsyncResult:
    """Run (or reuse cached) Rhubarb lip-sync on all segments in project_dir and
    write projects/<name>/lipsync.json."""
    project_dir = Path(project_dir)
    narr_path = project_dir / "narration.json"
    tl_path = project_dir / "timeline.json"
    missing = [str(p) for p in (narr_path, tl_path) if not p.exists()]
    if missing:
        raise ValueError("missing " + ", ".join(missing) + " - run `narrate` first.")

    narration = json.loads(narr_path.read_text(encoding="utf-8"))
    timeline = json.loads(tl_path.read_text(encoding="utf-8"))
    segments = narration.get("segments") or []
    scenes = timeline.get("scenes") or []

    rcfg = {**DEFAULTS, **(cfg.get("rhubarb") or {})}
    bcfg = {**DEFAULTS["blink"], **(rcfg.get("blink") or {})}

    only_set: Optional[Set[str]] = set(only) if only else None
    if only_set:
        known = {s["id"] for s in segments} | {sc["id"] for sc in scenes}
        unknown = sorted(only_set - known)
        if unknown:
            raise RhubarbError([Problem(
                f'Unknown segment or scene id "{unknown[0]}"',
                detail="Use ids like scene03 or scene03_seg07."
            )])

    if backend not in ("rhubarb", "fake"):
        raise ValueError(f'Unknown rhubarb backend "{backend}". Available: rhubarb, fake')

    exe: Optional[str] = None
    ver = "fake"
    if backend == "rhubarb":
        exe = find_rhubarb(cfg)
        if not exe:
            raise RhubarbError([Problem(
                "rhubarb executable was not found on PATH or at ~/.local/opt/rhubarb/rhubarb.",
                detail='Install Rhubarb Lip Sync, set rhubarb.bin in config, or pass --rhubarb fake.'
            )])
        ver = rhubarb_version(exe)

    proc_dir = project_dir / "cache" / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    max_workers = max(int(rcfg.get("max_workers", 3)), 1)
    backend_label = "fake:v1" if backend == "fake" else f"rhubarb:{ver}:{rcfg['recognizer']}"
    log(f"Lip-syncing {len(segments)} segments with {backend_label} (workers={max_workers}) ...")

    def _task(seg: Dict[str, Any]) -> Dict[str, Any]:
        seg_forced = force or bool(only_set and (seg["id"] in only_set or seg["scene"] in only_set))
        return _analyze_segment(seg, project_dir, proc_dir, rcfg, backend, exe, ver, seg_forced)

    if max_workers == 1 or backend == "fake" or len(segments) <= 1:
        results = [_task(s) for s in segments]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(_task, segments))

    stats = {"analyzed": 0, "fallback": 0, "cache": 0}
    warnings: List[str] = []
    seg_out_list: List[Dict[str, Any]] = []
    by_scene: Dict[str, List[Dict[str, Any]]] = {}

    for i, (seg, res) in enumerate(zip(segments, results), 1):
        stats[res["status"]] = stats.get(res["status"], 0) + 1
        if res["warning"]:
            warnings.append(res["warning"])
        s_start = float(seg["start"])
        abs_cues = [
            {
                "start": _r(s_start + c["start"]),
                "end": _r(s_start + c["end"]),
                "value": c["value"],
            }
            for c in res["cues"]
        ]
        seg_record = {
            "id": seg["id"],
            "scene": seg["scene"],
            "start": seg["start"],
            "end": seg["end"],
            "duration": seg["duration"],
            "cache_key": seg["cache_key"],
            "rhubarb_key": res["rhubarb_key"],
            "recognizer": res["recognizer"],
            "cues": res["cues"],
            "abs_cues": abs_cues,
        }
        seg_out_list.append(seg_record)
        by_scene.setdefault(seg["scene"], []).append(seg_record)
        log(f"  [{i:>3}/{len(segments)}] {seg['id']:<15} {len(res['cues']):>3} cues  "
            f"{res['recognizer']:<13} {res['status']}")

    scenes_out: List[Dict[str, Any]] = []
    for idx, sc in enumerate(scenes, 1):
        sc_id = sc["id"]
        sc_start = float(sc["start"])
        sc_end = float(sc["end"])
        sc_segs = by_scene.get(sc_id, [])
        mouth_cues = build_scene_mouth_cues(sc_start, sc_end, sc_segs)
        blinks = make_scene_blinks(sc_start, sc_end, idx, bcfg)
        scenes_out.append({
            "id": sc_id,
            "start": _r(sc_start),
            "end": _r(sc_end),
            "mouth_cues": mouth_cues,
            "blinks": blinks,
        })

    lipsync_data = {
        "schema_version": LIPSYNC_VERSION,
        "source_sha256": narration.get("source_sha256"),
        "backend": backend_label,
        "total_duration": narration.get("total_duration", timeline.get("duration", 0.0)),
        "segments": seg_out_list,
        "scenes": scenes_out,
    }
    out_path = project_dir / "lipsync.json"
    tmp_out = out_path.with_name(out_path.name + ".tmp")
    tmp_out.write_text(json.dumps(lipsync_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_out, out_path)

    log(f"Lip-sync done in {time.time() - t0:.1f}s: {stats['analyzed']} analyzed, "
        f"{stats['fallback']} fallback, {stats['cache']} from cache.")
    return LipsyncResult(lipsync_data, out_path, stats, warnings)