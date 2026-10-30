#!/usr/bin/env python3
"""prompt_sheet.py - extract image prompts from script.md into a readable sheet or batch list.

Usage:
    python prompt_sheet.py projects/nnn                  # dir (finds script.md) or script file
    python prompt_sheet.py projects/nnn --raw            # plain prompts only (for batch generation)
    python prompt_sheet.py projects/nnn --raw --names    # image_name: prompt (one per line)
    python prompt_sheet.py projects/nnn --search         # also show stock/reaction search queries
    python prompt_sheet.py projects/nnn --say            # also show narration lines per scene
    python prompt_sheet.py projects/nnn --json           # dump extracted prompts as JSON
    python prompt_sheet.py projects/nnn -o prompts.txt   # also save output to a file

Reads script.md (YAML frontmatter + ## SCENE blocks). If timeline.json sits next
to it, scene start/end timecodes are shown in each scene header automatically.

Each scene gets its scene ID, title, image slug, and full generation prompt.
Scenes missing an image or prompt are flagged with !! MISSING PROMPT in the sheet
and listed in the summary footer.
"""
import argparse
import json
import re
import sys
from pathlib import Path


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SCENE_HEADER_RE = re.compile(r"^##\s+SCENE\s+(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE)
SUBHEADER_RE = re.compile(r"^###\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^(image|prompt|search|music|sfx)\s*:\s*(.*?)\s*$", re.IGNORECASE)
EMOTION_TAG_RE = re.compile(r"^\(([^)]+)\)\s*")
INLINE_CUE_RE = re.compile(r"\[[^\]]+\]")


def fmt_time(t):
    if t is None:
        return "--:--"
    tenths = int(round(float(t) * 10))
    minutes, rem = divmod(tenths, 600)
    return f"{minutes:02d}:{rem / 10:04.1f}"


def load_timeline_map(script_path):
    """If timeline.json sits beside the script, map scene number/image to (start, end)."""
    if not script_path or script_path == Path("-"):
        return {}
    tl_path = script_path.parent / "timeline.json"
    if not tl_path.exists():
        return {}
    try:
        data = json.loads(tl_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for idx, s in enumerate(data.get("scenes", []), start=1):
        if isinstance(s, dict):
             times = (s.get("start"), s.get("end"))
             out[idx] = times
             if s.get("id"):
                 out[str(s["id"]).lower()] = times
             if s.get("image"):
                 out[str(s["image"]).lower()] = times
    return out


def parse_frontmatter(text):
    """Return (meta_dict, remaining_text)."""
    meta = {}
    m = FRONTMATTER_RE.match(text)
    if not m:
        return meta, text
    for raw_line in m.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"').strip("'")
        meta[k.strip()] = v
    return meta, text[m.end():]


def parse_script(text):
    """Parse markdown script into (meta, scenes_list)."""
    meta, body = parse_frontmatter(text)
    scenes = []
    current = None
    in_narration = False
    active_field = None

    for raw_line in body.splitlines():
        line = raw_line.strip()

        m_scene = SCENE_HEADER_RE.match(line)
        if m_scene:
            if current is not None:
                scenes.append(current)
            num_str, title = m_scene.group(1), m_scene.group(2)
            current = {
                "number": int(num_str),
                "id": f"scene_{int(num_str):02d}",
                "header": f"SCENE {num_str}: {title}",
                "title": title,
                "image": "",
                "prompt": "",
                "search": [],
                "narration": [],
            }
            in_narration = False
            active_field = None
            continue

        if current is None:
            continue

        m_sub = SUBHEADER_RE.match(line)
        if m_sub:
            in_narration = m_sub.group(1).strip().lower().startswith("narration")
            active_field = None
            continue

        if not in_narration:
            m_field = FIELD_RE.match(line)
            if m_field:
                key = m_field.group(1).lower()
                val = m_field.group(2).strip()
                active_field = key
                if key == "image":
                    current["image"] = val
                elif key == "prompt":
                    current["prompt"] = val
                elif key == "search":
                    current["search"] = [q.strip() for q in val.split("|") if q.strip()]
                continue

            # Support wrapped multi-line prompts before ### Narration
            if line and active_field == "prompt":
                current["prompt"] = (current["prompt"] + " " + line).strip()
                continue

        if in_narration and line:
            current["narration"].append(line)

    if current is not None:
        scenes.append(current)

    return meta, scenes


def clean_narration_line(line, n=68):
    emo = ""
    m = EMOTION_TAG_RE.match(line)
    if m:
        emo = f"({m.group(1)}) "
        line = line[m.end():]
    line = INLINE_CUE_RE.sub("", line)
    line = " ".join(line.split())
    if len(line) > n:
        line = line[: n - 3] + "..."
    return f"{emo}{line}".strip()


def resolve_script_path(path_arg):
    if path_arg == "-":
        return Path("-")
    p = Path(path_arg)
    if p.is_dir():
        for candidate in ("script.md", "prompts.md", "story.md"):
            c = p / candidate
            if c.exists():
                return c
        md_files = sorted(p.glob("*.md"))
        if md_files:
            return md_files[0]
        sys.exit(f"error: no script.md found in directory {p}")
    if not p.exists():
        sys.exit(f"error: {p} not found")
    return p


def build_sheet(meta, scenes, tl_map, show_search, show_say):
    lines = []
    title = meta.get("title", "Untitled Script")
    channel = meta.get("channel", "")
    target = meta.get("target_minutes", "")

    header_parts = [f"PROMPT SHEET    scenes {len(scenes)}"]
    if channel:
        header_parts.append(f"channel {channel}")
    if target:
        header_parts.append(f"target {target}m")
    lines.append("   ".join(header_parts))
    lines.append(f"Title: {title}")

    missing_prompts = []
    missing_images = []
    total_prompts = 0
    total_searches = 0

    for scene in scenes:
        lines.append("")
        lines.append("=" * 72)

        times = (
            tl_map.get(scene["id"])
            or tl_map.get(scene["image"].lower())
            or tl_map.get(scene["number"])
        )
        time_str = f"  {fmt_time(times[0])} -> {fmt_time(times[1])}" if times else ""
        lines.append(f"{scene['header'].upper()}{time_str}")

        img_name = scene["image"] or "!! MISSING IMAGE NAME"
        if not scene["image"]:
            missing_images.append(scene["id"])
        lines.append(f"  image: {img_name}")
        lines.append("=" * 72)

        if scene["prompt"]:
            total_prompts += 1
            lines.append(f"  PROMPT  {scene['prompt']}")
        else:
            missing_prompts.append(scene["image"] or scene["id"])
            lines.append("  PROMPT  !! MISSING PROMPT")

        if show_search and scene["search"]:
            total_searches += len(scene["search"])
            lines.append(f"  SEARCH  {' | '.join(scene['search'])}")
        elif scene["search"]:
            total_searches += len(scene["search"])

        if show_say and scene["narration"]:
            for nar in scene["narration"]:
                lines.append(f"  SAY     {clean_narration_line(nar)}")

    lines.append("")
    lines.append("-" * 72)
    lines.append(
        f"TOTALS  SCENES:{len(scenes)}  PROMPTS:{total_prompts}  SEARCH_QUERIES:{total_searches}"
    )
    if missing_images:
        lines.append("Missing image names: " + ", ".join(missing_images))
    if missing_prompts:
        lines.append("Missing prompts: " + ", ".join(missing_prompts))
    return "\n".join(lines)


def build_raw(scenes, with_names=False):
    out = []
    for s in scenes:
        if not s["prompt"]:
            continue
        if with_names:
            name = s["image"] or s["id"]
            out.append(f"{name}: {s['prompt']}")
        else:
            out.append(s["prompt"])
    return "\n".join(out)


def build_json(meta, scenes):
    payload = {
        "meta": meta,
        "count": len(scenes),
        "prompts": [
            {
                "number": s["number"],
                "id": s["id"],
                "title": s["title"],
                "image": s["image"],
                "prompt": s["prompt"],
                "search": s["search"],
            }
            for s in scenes
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(
        description="Extract image prompts from script.md into a readable sheet or batch format"
    )
    ap.add_argument("path", help="project directory, script.md path, or '-' for stdin")
    ap.add_argument("--raw", action="store_true", help="output plain prompts only (one per line)")
    ap.add_argument(
        "--names",
        action="store_true",
        help="with --raw, prefix each prompt with its image name (image: prompt)",
    )
    ap.add_argument("--search", action="store_true", help="include stock search queries in sheet")
    ap.add_argument("--say", action="store_true", help="include narration lines in sheet")
    ap.add_argument("--json", action="store_true", help="output structured JSON")
    ap.add_argument("-o", "--out", help="also write the output to this file")
    args = ap.parse_args()

    script_path = resolve_script_path(args.path)
    if script_path == Path("-"):
        text = sys.stdin.read()
    else:
        text = script_path.read_text(encoding="utf-8")

    meta, scenes = parse_script(text)
    if not scenes:
        sys.exit(f"error: no '## SCENE' blocks found in {script_path}")

    if args.json:
        output = build_json(meta, scenes)
    elif args.raw:
        output = build_raw(scenes, with_names=args.names)
    else:
        tl_map = load_timeline_map(script_path)
        output = build_sheet(meta, scenes, tl_map, args.search, args.say)

    print(output)
    if args.out:
        Path(args.out).write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()