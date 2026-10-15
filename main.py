#!/usr/bin/env python3
"""Video pipeline CLI.

    python main.py script/nnn.md                      # parse + validate + narrate
    python main.py parse script/nnn.md                # write projects/<name>/project.json
    python main.py validate script/nnn.md             # human-readable validation report
    python main.py narrate script/nnn.md              # segment WAVs + narration.json + timeline.json
    python main.py narrate projects/nnn/project.json  # same, from an existing project.json
    python main.py regenerate script/nnn.md scene03_seg04 [scene05 ...]
    python main.py mix projects/nnn --assets assets        # full_mix.wav: narration + SFX

Useful flags:  --backend fake (dry run without Kokoro)   --force   --no-polish   --no-export
               --assets DIR   --strict   --config FILE   -o DIR

Exit code is 1 on errors (or warnings with --strict), else 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from parser import load_channel_config, parse_file, render_report, validate

COMMANDS = {"parse", "validate", "narrate", "regenerate", "build", "mix"}


def _project_dir(script: str, out: Optional[str]) -> Path:
    return Path(out) if out else Path("projects") / Path(script).stem


def _check(args) -> Tuple[Dict[str, Any], Any, Any, bool]:
    cfg = load_channel_config(args.config)
    result = parse_file(args.script, cfg)
    extra = validate(result.project, cfg, getattr(args, "assets", None))
    has_errors = bool(result.errors) or any(d.level == "error" for d in extra)
    return cfg, result, extra, has_errors


def _write_project(result, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "project.json"
    path.write_text(json.dumps(result.project.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def cmd_validate(args) -> int:
    cfg, result, extra, has_errors = _check(args)
    print(render_report(Path(args.script).name, result, extra, cfg, bool(args.assets)))
    has_warn = any(d.level == "warning" for d in result.diagnostics + extra)
    return 1 if has_errors or (args.strict and has_warn) else 0


def cmd_parse(args) -> int:
    cfg, result, extra, has_errors = _check(args)
    print(render_report(Path(args.script).name, result, extra, cfg, bool(args.assets)))
    if has_errors and not args.force:
        print("\nproject.json NOT written (errors above). Use --force to write it anyway.")
        return 1
    print(f"\nWrote {_write_project(result, _project_dir(args.script, args.out))}")
    return 0


def _narrate(args, only=None, force=False) -> int:
    from narration import AudioToolError, NarrationError, get_backend, load_project, narrate_project

    cfg = load_channel_config(args.config)
    if args.script.lower().endswith(".json"):                       # from an existing project.json
        project_path = Path(args.script)
        project_dir = Path(args.out) if args.out else project_path.parent
        project = load_project(project_path)
    else:                                                           # from the script: parse first
        _, result, extra, has_errors = _check(args)
        if has_errors:
            print(render_report(Path(args.script).name, result, extra, cfg, bool(args.assets)))
            print("\nFix the errors above before narrating.")
            return 1
        project_dir = _project_dir(args.script, args.out)
        _write_project(result, project_dir)
        project = result.project.to_dict()
    try:
        backend = get_backend(args.backend or cfg["tts"]["backend"], cfg)
        res = narrate_project(project, project_dir, cfg, backend, only=only,
                              force=force or args.force, polish=False if args.no_polish else None,
                              export=not args.no_export,
                              assets_dir=Path(args.assets) if args.assets else None)
    except NarrationError as e:
        print(str(e), file=sys.stderr)
        return 1
    except (AudioToolError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for w in res.warnings:
        print(f"WARNING: {w}")
    print(f"Wrote {project_dir / 'narration.json'}, {project_dir / 'timeline.json'}"
          + (f", {res.captions_path}" if res.captions_path else "")
          + (f", {res.export_path}" if res.export_path else ""))
    return 0


def cmd_narrate(args) -> int:
    return _narrate(args)


def cmd_regenerate(args) -> int:
    return _narrate(args, only=args.ids)          # naming ids forces just those; the rest use the cache


def cmd_build(args) -> int:
    code = _narrate(args)
    if code == 0:
        print("Next: `python main.py mix projects/<name> --assets assets` for the SFX mix; "
              "scene renderer and video render are not built yet.")
    return code


def cmd_mix(args) -> int:
    from narration import AudioToolError
    from narration.mix import mix_sfx

    cfg = load_channel_config(args.config)
    p = Path(args.script)
    project_dir = p.parent if p.suffix.lower() == ".json" else p
    timeline_path = project_dir / "timeline.json"
    narration_path = project_dir / "audio" / "full_narration.wav"
    missing = [str(x) for x in (timeline_path, narration_path) if not x.exists()]
    if missing:
        print("ERROR: missing " + ", ".join(missing) + " - run `narrate` first.", file=sys.stderr)
        return 1
    if not args.assets:
        print("ERROR: mix needs --assets DIR (the folder with the sfx library).", file=sys.stderr)
        return 1
    try:
        timeline = json.loads(timeline_path.read_text("utf-8"))
        mcfg = cfg.get("mix") or {}
        out_path = project_dir / "audio" / mcfg.get("out_name", "full_mix.wav")
        out_path, warnings, _ = mix_sfx(timeline, narration_path, Path(args.assets), cfg, out_path)
    except (AudioToolError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"WARNING: {w}")
    print(f"Wrote {out_path}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "build")                                     # python main.py script.md
    ap = argparse.ArgumentParser(prog="main.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    table = (("parse", cmd_parse), ("validate", cmd_validate), ("narrate", cmd_narrate),
             ("regenerate", cmd_regenerate), ("build", cmd_build), ("mix", cmd_mix))
    for name, fn in table:
        sp = sub.add_parser(name)
        sp.add_argument("script", help=("project folder or project.json" if name == "mix"
                                        else "script .md (or project.json for narrate)"))
        if name == "regenerate":
            sp.add_argument("ids", nargs="+", help="segment ids (scene03_seg04) or scene ids (scene03)")
        sp.add_argument("--config", help="channel config JSON (default: config/channel_main.json)")
        sp.add_argument("--assets", help="assets folder to check referenced files against")
        sp.add_argument("--strict", action="store_true", help="treat warnings as failures (validate)")
        sp.add_argument("--force", action="store_true", help="parse: write despite errors; narrate: ignore caches")
        sp.add_argument("--backend", choices=["kokoro", "fake"], help="TTS backend (default from config)")
        sp.add_argument("--no-polish", action="store_true", help="skip the per-segment ffmpeg chain")
        sp.add_argument("--no-export", action="store_true", help="skip the full_narration.wav export")
        sp.add_argument("-o", "--out", help="project folder (default: projects/<script name>)")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
