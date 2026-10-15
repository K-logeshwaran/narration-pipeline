"""Timeline: exact start/end for every segment, pause, scene and cue.

Built AFTER audio durations are known; the audio is the source of truth.

Rules (no pause ever stacks with an automatic gap):
  inside a scene   start(next) = end(this) + gap_after_s(this) + pause_before(next)
                   (gap_after_s is already the explicit pause OR the automatic line gap,
                    never both - the parser resolved that)
  between scenes   silence = max(scene_gap_s - pause_before(next), trailing pause of previous)
                   then the next scene's own leading pause plays on its new image

Cue times use word-level timing when the caller passes it (word_times maps
segment id -> {"spans": [(start, end), ...] segment-local, "method": str});
otherwise they fall back to character-position ESTIMATES.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

TIMELINE_VERSION = "1.1"
_RANK = {"narration": 0, "pause": 1, "cue": 2, "sfx": 2}


def _r(x: float) -> float:
    return round(float(x), 3)


def cue_time(seg: Dict[str, Any], cue: Dict[str, Any], start: float, dur: float,
             wt: Optional[Dict[str, Any]] = None) -> Tuple[float, bool]:
    """Returns (absolute time, estimated). With word spans a cue lands on the
    start of the word it binds to (or the end of the last word when it sits at
    the segment end); without them it keeps the character-fraction estimate."""
    spans = (wt or {}).get("spans") or []
    if spans:
        wi = cue["word_index"]
        t = start + (spans[wi][0] if wi < len(spans) else spans[-1][1])
        estimated = (wt or {}).get("method") == "estimated"
    else:
        n = len(seg["text"])
        frac = 0.0 if n == 0 else min(max(cue["char_offset"] / n, 0.0), 1.0)
        t = start + frac * dur
        estimated = True
    return max(t + float(cue.get("offset_s") or 0.0), 0.0), estimated


def build_timeline(project: Dict[str, Any], durations: Dict[str, float],
                   cfg: Dict[str, Any],
                   word_times: Optional[Dict[str, Dict[str, Any]]] = None
                   ) -> Tuple[Dict[str, Any], Dict[str, Tuple[float, float]]]:
    """Returns (timeline_dict, spans) where spans maps segment id -> (start, end) in
    unrounded seconds, for placing audio on the mix."""
    scene_gap = float(cfg["timeline"]["scene_gap_s"])
    events: List[Dict[str, Any]] = []
    spans: Dict[str, Tuple[float, float]] = {}
    scene_rows: List[Dict[str, Any]] = []

    prev_end = 0.0
    prev_gap = 0.0
    prev_last_id = None
    first_scene = True

    for sc in project["scenes"]:
        segs = sc["segments"]
        if not segs:
            continue
        lead0 = segs[0].get("pause_before") or 0.0

        if first_scene:
            scene_start = 0.0
        else:
            boundary = max(scene_gap - lead0, prev_gap, 0.0)
            if boundary > 0:
                kind = "explicit" if prev_gap > 0 and prev_gap >= scene_gap - lead0 else "scene_gap"
                events.append({"type": "pause", "kind": kind, "start": prev_end,
                               "end": prev_end + boundary, "after": prev_last_id})
            scene_start = prev_end + boundary
        first_scene = False
        scene_rows.append({"id": sc["id"], "start": scene_start, "end": None,
                           "image": sc["image"], "music": sc["music"]})

        cursor = scene_start
        for j, seg in enumerate(segs):
            dur = durations[seg["id"]]
            lead = seg.get("pause_before") or 0.0
            if lead > 0:
                events.append({"type": "pause", "kind": "explicit", "start": cursor,
                               "end": cursor + lead, "before": seg["id"]})
                cursor += lead
            start, end = cursor, cursor + dur
            spans[seg["id"]] = (start, end)
            events.append({"type": "narration", "id": seg["id"], "scene": sc["id"], "start": start,
                           "end": end, "emotion": seg["emotion"], "modifier": seg["modifier"]})
            for ci, cue in enumerate(seg["cues"]):
                t, estimated = cue_time(seg, cue, start, dur, (word_times or {}).get(seg["id"]))
                ev = {"type": "cue", "cue_type": cue["type"], "name": cue["name"], "segment": seg["id"],
                      "start": t, "estimated": estimated}
                for k in ("style", "duration_s", "scale", "offset_s"):
                    if cue.get(k) not in (None, 0.0):
                        ev[k] = cue[k]
                events.append(ev)
            cursor = end
            if j < len(segs) - 1:
                gap = seg["gap_after_s"]
                if gap > 0:
                    events.append({"type": "pause",
                                   "kind": "explicit" if seg.get("pause_after") is not None else "line_gap",
                                   "start": cursor, "end": cursor + gap, "after": seg["id"]})
                    cursor += gap
            else:
                prev_end, prev_gap, prev_last_id = end, seg["gap_after_s"], seg["id"]

    # trailing explicit pause after the very last segment still belongs to the video
    total = prev_end
    if prev_gap > 0:
        events.append({"type": "pause", "kind": "explicit", "start": prev_end, "end": prev_end + prev_gap,
                       "after": prev_last_id})
        total = prev_end + prev_gap

    for i, row in enumerate(scene_rows):
        row["end"] = scene_rows[i + 1]["start"] if i + 1 < len(scene_rows) else total

    order = {id(e): i for i, e in enumerate(events)}
    events.sort(key=lambda e: (e["start"], _RANK[e["type"]], order[id(e)]))

    def rounded(ev: Dict[str, Any]) -> Dict[str, Any]:
        return {k: (_r(v) if isinstance(v, float) else v) for k, v in ev.items()}

    timeline = {
        "schema_version": TIMELINE_VERSION,
        "source_sha256": project.get("source", {}).get("sha256"),
        "fps": project["project"]["fps"],
        "duration": _r(total),
        "scenes": [rounded(r) for r in scene_rows],
        "events": [rounded(e) for e in events],
    }
    return timeline, spans
