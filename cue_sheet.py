#!/usr/bin/env python3
"""cue_sheet.py - turn timeline.json into a readable edit list for CapCut.

Usage:
    python cue_sheet.py projects/nnn                  # dir or timeline.json
    python cue_sheet.py projects/nnn --frames         # MM:SS:FF timecodes
    python cue_sheet.py projects/nnn --no-say         # hide narration lines
    python cue_sheet.py projects/nnn -o cuesheet.txt  # also save to a file

Reads timeline.json (scenes + events). If narration.json sits next to it,
the spoken line is shown next to each cue so you know where it lands.
A "~" after a time means that time was estimated, not word-aligned.




Each scene gets its start/end, image name, and music.
SAY rows show which spoken line each cue lands on. They come from narration.json if it's in the same folder.
A ~ after a time means that cue's timing was estimated, not word-aligned. Double-check those by ear.
SFX with exists: false show !! FILE MISSING, and the footer lists all of them.
Beds show both a start row and an "ends" row.
Add --pauses if you want pause rows too.




"""
import argparse
import bisect
import json
import sys
from pathlib import Path


def fmt(t, fps, frames):
    if t is None:
        return "--:--"
    if frames:
        f = max(1, int(round(fps)))
        total = int(round(t * f))
        minutes, rem = divmod(total, 60 * f)
        secs, ff = divmod(rem, f)
        return f"{minutes:02d}:{secs:02d}:{ff:02d}"
    tenths = int(round(t * 10))
    minutes, rem = divmod(tenths, 600)
    return f"{minutes:02d}:{rem / 10:04.1f}"


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def segment_text_map(narration_path):
    if not narration_path.exists():
        return {}
    try:
        data = load_json(narration_path)
    except (OSError, ValueError):
        return {}
    segs = data.get("segments", data) if isinstance(data, dict) else data
    out = {}
    if isinstance(segs, list):
        for s in segs:
            if isinstance(s, dict) and "id" in s and "text" in s:
                out[s["id"]] = s["text"]
    return out


def trunc(text, n=64):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "..."


def dur(v):
    if v is None:
        return ""
    return f" {v:g}s"


def describe(ev, texts):
    """Return (label, detail) for one event, or None to skip."""
    kind = ev.get("type")
    if kind == "_bedend":
        return "BED", f"{ev.get('name', '')} ends"
    if kind == "narration":
        line = texts.get(ev.get("id"), "")
        emo = ev.get("emotion", "")
        if ev.get("modifier"):
            emo += "," + ev["modifier"]
        detail = f"({emo}) {trunc(line)}" if line else f"({emo}) {ev.get('id', '')}"
        return "SAY", detail
    if kind == "cue":
        ct = ev.get("cue_type", "cue")
        name = ev.get("name", "?")
        if ct == "text":
            style = f" [{ev['style']}]" if ev.get("style") else ""
            return "TEXT", f"{name}{style}{dur(ev.get('duration_s'))}"
        if ct == "img":
            return "IMG", f"{name}{dur(ev.get('duration_s'))}"
        if ct == "cam":
            scale = f" x{ev['scale']:g}" if ev.get("scale") else ""
            return "CAM", f"{name}{dur(ev.get('duration_s'))}{scale}"
        return ct.upper(), name
    if kind == "sfx":
        name = ev.get("name", "?")
        miss = "  !! FILE MISSING" if ev.get("exists") is False else ""
        if ev.get("kind") == "bed":
            return "BED", f"{name} (loop){miss}"
        return "SFX", f"{name}{miss}"
    return None


def build(timeline, texts, fps, frames, show_say, show_pauses):
    scenes = timeline.get("scenes", [])
    events = [e for e in timeline.get("events", []) if e.get("start") is not None]
    for e in list(events):
        if e.get("type") == "sfx" and e.get("kind") == "bed" and e.get("end") is not None:
            events.append({"type": "_bedend", "name": e.get("name", ""), "start": e["end"]})
    events.sort(key=lambda e: (e["start"], {"narration": 0, "pause": 1}.get(e.get("type"), 2)))

    starts = [s.get("start", 0.0) for s in scenes]
    buckets = {i: [] for i in range(len(scenes))}
    for ev in events:
        i = max(0, bisect.bisect_right(starts, ev["start"] + 1e-6) - 1) if scenes else 0
        buckets.setdefault(i, []).append(ev)

    lines = []
    total = timeline.get("duration")
    lines.append(f"CUE SHEET   total {fmt(total, fps, frames)}   fps {fps:g}")
    lines.append("~ = estimated time (not word-aligned)")
    counts, estimated, missing = {}, 0, []

    for i, scene in enumerate(scenes or [{}]):
        lines.append("")
        lines.append("=" * 72)
        head = (
            f"{scene.get('id', 'scene').upper()}  "
            f"{fmt(scene.get('start'), fps, frames)} -> {fmt(scene.get('end'), fps, frames)}"
        )
        lines.append(head)
        lines.append(f"  image: {scene.get('image', '?')}   music: {scene.get('music') or 'none'}")
        lines.append("=" * 72)
        for ev in buckets.get(i, []):
            if ev.get("type") == "pause":
                if show_pauses:
                    k = ev.get("kind", "pause")
                    lines.append(
                        f"{fmt(ev['start'], fps, frames):>9}   PAUSE   {k} "
                        f"{(ev.get('end', ev['start']) - ev['start']):.2f}s"
                    )
                continue
            d = describe(ev, texts)
            if d is None:
                continue
            label, detail = d
            if label == "SAY" and not show_say:
                continue
            mark = "~" if ev.get("estimated") else " "
            lines.append(f"{fmt(ev['start'], fps, frames):>9}{mark}  {label:<5}  {detail}")
            if label != "SAY" and ev.get("type") != "_bedend":
                counts[label] = counts.get(label, 0) + 1
                estimated += 1 if ev.get("estimated") else 0
                if ev.get("exists") is False:
                    missing.append(ev.get("name", "?"))

    lines.append("")
    lines.append("-" * 72)
    summary = "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "no cues"
    lines.append(f"TOTALS  {summary}")
    if estimated:
        lines.append(f"{estimated} cue(s) have estimated timing (marked ~)")
    if missing:
        lines.append("Missing SFX files: " + ", ".join(sorted(set(missing))))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Readable edit list from timeline.json")
    ap.add_argument("path", help="project directory or timeline.json")
    ap.add_argument("--frames", action="store_true", help="MM:SS:FF timecodes")
    ap.add_argument("--no-say", action="store_true", help="hide narration lines")
    ap.add_argument("--pauses", action="store_true", help="include pauses")
    ap.add_argument("--fps", type=float, default=None, help="override fps")
    ap.add_argument("-o", "--out", help="also write the sheet to this file")
    args = ap.parse_args()

    p = Path(args.path)
    tl_path = p / "timeline.json" if p.is_dir() else p
    if not tl_path.exists():
        sys.exit(f"error: {tl_path} not found")
    timeline = load_json(tl_path)
    texts = segment_text_map(tl_path.parent / "narration.json")
    fps = args.fps or timeline.get("fps") or 30

    sheet = build(timeline, texts, fps, args.frames, not args.no_say, args.pauses)
    print(sheet)
    if args.out:
        Path(args.out).write_text(sheet + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
