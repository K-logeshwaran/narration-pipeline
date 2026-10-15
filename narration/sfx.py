"""Resolve sfx/bed cue events into concrete SFX events on the timeline.

  {sfx:pop}            -> {"type": "sfx", "kind": "point", "file", "start", ...}
  {bed:ticking_clock}  -> {"type": "sfx", "kind": "bed", "file", "start", "end"}
                          end = the next {bed:end} cue in the same scene,
                          or the scene's end when there is none
  {bed:end}            -> consumed (closes the open bed, emits no event)

File resolution: with an assets folder the first existing
<assets>/<cfg.assets.sfx>/<name>.<wav|mp3|ogg|flac> wins and "exists" is true;
a missing file keeps the conventional .wav path with "exists": false and a
warning. Without an assets folder the path is conventional and "exists" is
left out (nothing was checked). Point sfx with a readable .wav also get
"duration_s" so the mix stage can plan ducking without opening the file.
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_EXTS = (".wav", ".mp3", ".ogg", ".flac")
_RANK = {"narration": 0, "pause": 1, "cue": 2, "sfx": 2}


def _find_file(assets_dir: Path, sub: str, name: str) -> Tuple[str, bool]:
    for ext in _EXTS:
        p = assets_dir / sub / f"{name}{ext}"
        if p.exists():
            return f"{sub}/{name}{ext}", True
    return f"{sub}/{name}.wav", False


def _wav_seconds(path: Path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 3)
    except (wave.Error, OSError):
        return None


def resolve_sfx_events(timeline: Dict[str, Any], cfg: Dict[str, Any],
                       assets_dir: Optional[Path] = None
                       ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Returns (events, warnings): the timeline's events with every sfx/bed cue
    replaced by a resolved sfx event, sorted by (start, rank, original order)."""
    sub = cfg.get("assets", {}).get("sfx", "sfx")
    events = timeline.get("events", [])
    seg_scene = {e["id"]: e["scene"] for e in events if e["type"] == "narration"}
    scene_end = {s["id"]: s["end"] for s in timeline.get("scenes", [])}
    warnings: List[str] = []
    out: List[Dict[str, Any]] = []

    for i, ev in enumerate(events):
        if ev["type"] != "cue" or ev.get("cue_type") not in ("sfx", "bed"):
            out.append(ev)
            continue
        name = ev["name"]
        if ev["cue_type"] == "bed" and name == "end":
            continue                                            # closes the open bed
        scene = seg_scene.get(ev.get("segment"))
        if assets_dir is not None:
            rel, exists = _find_file(Path(assets_dir), sub, name)
            if not exists:
                warnings.append(f'SFX file not found: {sub}/{name} (looked in {assets_dir / sub})')
        else:
            rel, exists = f"{sub}/{name}.wav", None

        sfx: Dict[str, Any] = {
            "type": "sfx",
            "kind": "point" if ev["cue_type"] == "sfx" else "bed",
            "name": name,
            "file": rel,
            "start": ev["start"],
            "estimated": ev.get("estimated", True),
            "segment": ev.get("segment"),
        }
        if exists is not None:
            sfx["exists"] = exists
        if ev.get("offset_s"):
            sfx["offset_s"] = ev["offset_s"]

        if sfx["kind"] == "bed":
            end = None
            for later in events[i + 1:]:
                if (later["type"] == "cue" and later.get("cue_type") == "bed"
                        and later.get("name") == "end"
                        and seg_scene.get(later.get("segment")) == scene):
                    end = later["start"]
                    break
            sfx["end"] = end if end is not None else scene_end.get(scene, timeline["duration"])
        elif exists and rel.endswith(".wav"):
            dur = _wav_seconds(Path(assets_dir) / rel)
            if dur is not None:
                sfx["duration_s"] = dur
        out.append(sfx)

    order = {id(e): i for i, e in enumerate(out)}
    out.sort(key=lambda e: (e["start"], _RANK[e["type"]], order[id(e)]))
    return out, warnings
