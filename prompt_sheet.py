#!/usr/bin/env python3
"""prompt_sheet.py - pull every scene's image prompt out of a script into ONE text file.

Usage:
    python prompt_sheet.py script/nnn.md                 # writes projects/nnn/image-generate-prompts.txt
    python prompt_sheet.py projects/nnn                  # a project folder (reads its project.json)
    python prompt_sheet.py projects/nnn/project.json     # or the file itself
    python prompt_sheet.py script/nnn.md -o prompts.txt  # choose the output file
    python prompt_sheet.py script/nnn.md --scenes 2,5-6  # only some scenes (to regenerate a few images)
    python prompt_sheet.py script/nnn.md --with-style    # prepend the character/style block from the config
    python prompt_sheet.py script/nnn.md --prefix "Simple 2D stick figure, flat colors."
    python prompt_sheet.py script/nnn.md --prompt-below  # put each prompt on its own line (clean triple-click copy)
    python prompt_sheet.py script/nnn.md --format plain  # old one-line-per-scene format
    python prompt_sheet.py script/nnn.md -q              # write the file, print nothing but a summary

Default output, one block per scene:

    ============================================================
    scene 1 :
    image-name: s01_calendar_stare
    prompt: Doug stands center frame holding a giant November calendar page ...
    ============================================================

    ============================================================
    scene 2 :
    ...

  scene N      the scene number from "## SCENE NN"
  image-name   the "image:" field (save the generated picture under exactly this name;
               the pipeline looks for this stem)
  prompt       the "prompt:" field, unchanged (one line, see docs/SCRIPT_FORMAT_SPEC_v1.1.md B3)

--format plain   one line per scene:  scene-1 s01_calendar_stare Doug stands ...
--format tsv     same three fields separated by tabs (pastes into a spreadsheet)

The prompt in the script describes ONLY the scene. The spec says Doug's look and the art style are added
separately, so use --with-style (config key "image_style_prefix") or --prefix when you paste into ImageFX.

A scene with a missing image or prompt is still listed as !!MISSING and the exit code is 1.
A prompt wrapped onto a second line is a structural error (the spec requires one line): it is reported and exits 1.
Scripts only need a valid front matter and scene headers; narration errors do not stop this tool.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OUT_NAME = "image-generate-prompts.txt"
BAR_WIDTH = 60
BAR = "=" * BAR_WIDTH

# parser diagnostics that can affect an image name or prompt (everything else is about narration)
RELEVANT_CODES = {
    "E_FRONT_MISSING", "E_FRONT_UNCLOSED", "E_SCENE_HEADER", "E_SCENE_NUMBER", "E_SCENE_FIELD",
    "E_STRAY_TEXT", "W_IMAGE_FORMAT", "W_SCENE_FIELD_DUP", "W_SCENE_NUMBER",
}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_scenes(src: str, config_path: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str], Path]:
    """Returns (scenes, notes, default_output_folder).
    scenes = [{number, id, title, image, prompt}], notes = problems worth showing the user."""
    p = Path(src)
    if p.is_dir():
        p = p / "project.json"
    if not p.exists():
        raise FileNotFoundError(f"{src} not found")

    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if "scenes" not in data:
            raise ValueError(f"{p} does not look like a project.json (no 'scenes')")
        scenes = [{"number": s["number"], "id": s["id"], "title": s.get("title", ""),
                   "image": s.get("image", ""), "prompt": s.get("prompt", "")} for s in data["scenes"]]
        return scenes, [], p.parent

    from parser import load_channel_config, parse_file          # imported late: .json input needs no parser
    cfg = load_channel_config(config_path)
    res = parse_file(str(p), cfg)
    notes = [d.format() for d in res.diagnostics if d.code in RELEVANT_CODES]
    scenes = [{"number": sc.number, "id": sc.id, "title": sc.title, "image": sc.image, "prompt": sc.prompt}
              for sc in res.project.scenes]
    return scenes, notes, Path("projects") / p.stem


def load_style(config_path: Optional[str]) -> str:
    from parser import load_channel_config
    return str(load_channel_config(config_path).get("image_style_prefix", "")).strip()


# ---------------------------------------------------------------------------
# selecting
# ---------------------------------------------------------------------------

def parse_scene_selection(spec: str) -> List[int]:
    """'2,5-6' -> [2, 5, 6]"""
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                raise ValueError(f'bad range "{part}"')
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(part))
    if not out:
        raise ValueError("empty --scenes value")
    return out


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def prepare(scenes: List[Dict[str, Any]], prefix: str = "") -> Tuple[List[Tuple[int, str, str]], List[str]]:
    """Clean each scene into (number, image, prompt) and collect problems.
    The prompt is used exactly as written (whitespace collapsed); missing parts become !!MISSING markers."""
    rows: List[Tuple[int, str, str]] = []
    problems: List[str] = []
    seen: Dict[str, int] = {}
    prefix = " ".join(prefix.split())
    for sc in scenes:
        n = sc["number"]
        image = sc["image"].strip()
        prompt = " ".join(sc["prompt"].split())
        if not image:
            problems.append(f"scene-{n}: missing image name")
            image = "!!MISSING-IMAGE"
        elif image in seen:
            problems.append(f'scene-{n}: image name "{image}" is already used by scene-{seen[image]} '
                            f"(the second picture would overwrite the first)")
        else:
            seen[image] = n
        if not prompt:
            problems.append(f"scene-{n}: missing prompt")
            prompt = "!!MISSING-PROMPT"
        elif prefix:
            prompt = f"{prefix} {prompt}"
        rows.append((n, image, prompt))
    return rows, problems


def format_block(n: int, image: str, prompt: str, prompt_below: bool = False) -> str:
    lines = [BAR, f"scene {n} :", f"image-name: {image}"]
    lines += ["prompt:", prompt] if prompt_below else [f"prompt: {prompt}"]
    lines.append(BAR)
    return "\n".join(lines)


def render(rows: List[Tuple[int, str, str]], fmt: str = "blocks", prompt_below: bool = False) -> str:
    """The whole file text, ending with exactly one newline."""
    if fmt == "blocks":
        return "\n\n".join(format_block(n, i, p, prompt_below) for n, i, p in rows) + "\n"
    sep = "\t" if fmt == "tsv" else " "
    return "\n".join(f"scene-{n}{sep}{i}{sep}{p}" for n, i, p in rows) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="prompt_sheet.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="script .md, project.json, or a project folder")
    ap.add_argument("-o", "--out", help=f"output file (default: <project folder>/{OUT_NAME})")
    ap.add_argument("--scenes", help="only these scenes, e.g. 3 or 2,5-6")
    ap.add_argument("--format", choices=["blocks", "plain", "tsv"], default="blocks",
                    help="blocks = framed scene blocks (default); plain = one line per scene; tsv = tab-separated")
    ap.add_argument("--prompt-below", action="store_true",
                    help="blocks format: put the prompt text on the line below 'prompt:'")
    ap.add_argument("--prefix", help="text added in front of every prompt (overrides --with-style)")
    ap.add_argument("--with-style", action="store_true",
                    help='prepend config key "image_style_prefix" (Doug\'s look and the art style)')
    ap.add_argument("--config", help="channel config JSON (default: config/channel_main.json)")
    ap.add_argument("-q", "--quiet", action="store_true", help="do not print the prompts, only the summary")
    args = ap.parse_args(argv)

    try:
        scenes, notes, default_dir = load_scenes(args.source, args.config)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not scenes:
        print("ERROR: no scenes found. Each scene starts with: ## SCENE 01: Title", file=sys.stderr)
        for n in notes:
            print(n, file=sys.stderr)
        return 1

    if args.scenes:
        try:
            wanted = parse_scene_selection(args.scenes)
        except ValueError as e:
            print(f"ERROR: --scenes: {e}", file=sys.stderr)
            return 1
        have = {s["number"] for s in scenes}
        unknown = sorted(set(wanted) - have)
        if unknown:
            print(f"ERROR: no scene {', '.join(map(str, unknown))} "
                  f"(this script has scenes {min(have)}-{max(have)})", file=sys.stderr)
            return 1
        scenes = [s for s in scenes if s["number"] in set(wanted)]

    prefix = args.prefix or ""
    if args.with_style and not prefix:
        try:
            prefix = load_style(args.config)
        except (OSError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if not prefix:
            print('ERROR: --with-style needs a config key "image_style_prefix" '
                  "(or pass the text with --prefix).", file=sys.stderr)
            return 1

    rows, problems = prepare(scenes, prefix)
    body = render(rows, args.format, args.prompt_below)

    out = Path(args.out) if args.out else default_dir / OUT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    if not args.quiet:
        print(body, end="")
        print()
    for n in notes:
        print(n, file=sys.stderr)
    for pr in problems:
        print(f"PROBLEM: {pr}", file=sys.stderr)
    print(f"Wrote {len(rows)} prompt{'s' if len(rows) != 1 else ''} to {out}")
    structural = any(n.startswith("[ERROR]") for n in notes)      # e.g. a prompt wrapped onto a second line
    if structural:
        print("WARNING: the script has structural errors above; a prompt may be cut short. "
              "Each prompt must be ONE line.", file=sys.stderr)
    return 1 if structural or any("missing" in p for p in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
