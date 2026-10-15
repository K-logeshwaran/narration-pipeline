#!/usr/bin/env python3
"""chapters.py - print YouTube chapter timestamps from your pipeline output.

Usage:
    python chapters.py projects/nnn
    python chapters.py projects/nnn --offset 0      # shift all times (seconds)
    python chapters.py projects/nnn -o chapters.txt # also save to a file

Reads timeline.json for scene start times and project.json for scene titles
(falls back to the scene id if a title is missing). Paste the output into
your video description.

YouTube chapter rules this script enforces:
  - the first chapter must start at 0:00
  - at least 3 chapters
  - each chapter at least 10 seconds long (shorter ones are merged into the
    previous chapter, and you get a warning)
"""
import argparse
import json
import sys
from pathlib import Path

MIN_CHAPTER_S = 10.0


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def stamp(t):
    t = max(0, int(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def title_map(project_path):
    """Map scene id -> title from project.json, tolerating schema differences."""
    if not project_path.exists():
        return {}
    try:
        data = load_json(project_path)
    except (OSError, ValueError):
        return {}
    scenes = data.get("scenes", []) if isinstance(data, dict) else []
    out = {}
    for sc in scenes:
        if isinstance(sc, dict) and sc.get("id") and sc.get("title"):
            out[sc["id"]] = str(sc["title"]).strip()
    return out


def main():
    ap = argparse.ArgumentParser(description="YouTube chapters from timeline.json")
    ap.add_argument("path", help="project directory or timeline.json")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds added to every time (e.g. if you add an intro)")
    ap.add_argument("-o", "--out", help="also write the chapters to this file")
    args = ap.parse_args()

    p = Path(args.path)
    tl_path = p / "timeline.json" if p.is_dir() else p
    if not tl_path.exists():
        sys.exit(f"error: {tl_path} not found")

    timeline = load_json(tl_path)
    scenes = timeline.get("scenes", [])
    if not scenes:
        sys.exit("error: no scenes in timeline.json")
    titles = title_map(tl_path.parent / "project.json")

    chapters = []
    warnings = []
    for sc in scenes:
        start = float(sc.get("start", 0.0)) + args.offset
        title = titles.get(sc.get("id"), sc.get("id", "Chapter"))
        if chapters and start - chapters[-1][0] < MIN_CHAPTER_S:
            # The PREVIOUS chapter is too short. Fold it into the one before it
            # (its time just becomes part of that chapter) and keep this one.
            if len(chapters) > 1:
                short = chapters.pop()
                warnings.append(
                    f"'{short[1]}' is under {MIN_CHAPTER_S:g}s long, so it was "
                    f"folded into '{chapters[-1][1]}'"
                )
            else:
                warnings.append(
                    f"'{title}' starts under {MIN_CHAPTER_S:g}s after the first "
                    f"chapter, so it was folded into '{chapters[-1][1]}'"
                )
                continue
        chapters.append([start, title])

    # YouTube needs the first chapter at exactly 0:00.
    chapters[0][0] = 0.0

    # The last chapter must also run at least 10 s to the end of the video.
    total = timeline.get("duration")
    if total is not None and len(chapters) > 1:
        if float(total) + args.offset - chapters[-1][0] < MIN_CHAPTER_S:
            warnings.append(
                f"last chapter '{chapters[-1][1]}' is under {MIN_CHAPTER_S:g}s, "
                "so it was merged into the previous one"
            )
            chapters.pop()

    lines = [f"{stamp(t)} {title}" for t, title in chapters]
    print("\n".join(lines))

    if len(chapters) < 3:
        print("\nWARNING: YouTube needs at least 3 chapters or it ignores them.",
              file=sys.stderr)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    print("\nNote: if you append the outro AFTER the last scene, no shift is "
          "needed. If you add anything BEFORE scene 1, use --offset.",
          file=sys.stderr)

    if args.out:
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
