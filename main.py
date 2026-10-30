#!/usr/bin/env python3
"""Video & Narration pipeline CLI.

    python main.py script/nnn.md --assets assets       # full end-to-end build -> renders/nnn.mp4
    python main.py parse script/nnn.md                 # write projects/<name>/project.json
    python main.py validate script/nnn.md              # human-readable validation report
    python main.py narrate script/nnn.md               # segment WAVs + narration.json + timeline.json
    python main.py narrate script/burger.md            # auto-detects explainer channel config
    python main.py regenerate script/nnn.md scene03_seg04 [scene05 ...]
    python main.py lipsync projects/nnn                # Rhubarb mouth cues + blinks -> lipsync.json
    python main.py mix projects/nnn --assets assets    # full_mix.wav: narration + SFX
    python main.py render projects/nnn --assets assets # per-scene MP4s + final assembled MP4

Useful flags:  --backend fake (dry run without Kokoro)   --rhubarb fake   --character ID
               --playlist ID   --voice ID   --scenes 1,3-4   --force   --no-polish
               --no-export   --assets DIR   --strict   --config FILE   -o DIR

Exit code is 1 on errors (or warnings with --strict), else 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from parser import load_config_for_script, parse_file, render_report, validate

COMMANDS = {"parse", "validate", "narrate", "regenerate", "lipsync", "mix", "render", "build"}


def _project_dir(script: str, out: Optional[str]) -> Path:
    return Path(out) if out else Path("projects") / Path(script).stem


def _resolve_existing_project_dir(arg: str, out: Optional[str]) -> Path:
    p = Path(arg)
    if out:
        return Path(out)
    if p.suffix.lower() == ".md":
        return _project_dir(arg, out)
    if p.suffix.lower() == ".json":
        return p.parent
    return p


def _resolve_cfg(args, target_path: Optional[str] = None) -> Dict[str, Any]:
    return load_config_for_script(
        target_path or args.script,
        config_path=getattr(args, "config", None),
        playlist=getattr(args, "playlist", None),
        backend=getattr(args, "backend", None),
        voice=getattr(args, "voice", None),
    )


def _check(args) -> Tuple[Dict[str, Any], Any, Any, bool]:
    cfg = _resolve_cfg(args)
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

    if args.script.lower().endswith(".json"):                       # from an existing project.json
        project_path = Path(args.script)
        project_dir = Path(args.out) if args.out else project_path.parent
        project = load_project(project_path)
        cfg = _resolve_cfg(args, str(project_path))
    else:                                                           # from the script: parse first
        cfg, result, extra, has_errors = _check(args)
        if has_errors:
            print(render_report(Path(args.script).name, result, extra, cfg, bool(args.assets)))
            print("\nFix the errors above before narrating.")
            return 1
        project_dir = _project_dir(args.script, args.out)
        _write_project(result, project_dir)
        project = result.project.to_dict()
    backend = None
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
    finally:
        if backend is not None and hasattr(backend, "close"):
            backend.close()
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


def cmd_lipsync(args) -> int:
    from narration import RhubarbError, lipsync_project

    project_dir = _resolve_existing_project_dir(args.script, args.out)
    cfg = _resolve_cfg(args, str(project_dir))
    rhubarb_backend = args.rhubarb or ("fake" if args.backend == "fake" else "rhubarb")
    try:
        res = lipsync_project(project_dir, cfg, backend=rhubarb_backend, force=args.force)
    except RhubarbError as e:
        print(str(e), file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    for w in res.warnings:
        print(f"WARNING: {w}")
    print(f"Wrote {res.out_path}")
    return 0


def cmd_mix(args) -> int:
    from narration import AudioToolError
    from narration.mix import mix_sfx

    project_dir = _resolve_existing_project_dir(args.script, args.out)
    cfg = _resolve_cfg(args, str(project_dir))
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


def cmd_render(args) -> int:
    from narration import AudioToolError
    from render.assemble import assemble_project_video
    from render.kit import KitError
    from render.scene import render_project_scenes

    project_dir = _resolve_existing_project_dir(args.script, args.out)
    cfg = _resolve_cfg(args, str(project_dir))
    assets_dir = Path(args.assets) if args.assets else Path("assets")
    try:
        sc_res = render_project_scenes(
            project_dir,
            assets_dir,
            cfg,
            scenes_spec=getattr(args, "scenes", None),
            character_override=getattr(args, "character", None),
            force=args.force,
        )
        for w in sc_res.warnings:
            print(f"WARNING: {w}")
        if getattr(args, "scenes", None):
            for p in sc_res.scenes_rendered:
                print(f"Wrote {p}")
            return 0
        asm_res = assemble_project_video(project_dir, cfg)
        for w in asm_res.warnings:
            print(f"WARNING: {w}")
        print(f"Wrote {asm_res.output_mp4}")
        return 0
    except (KitError, AudioToolError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def cmd_build(args) -> int:
    from render.stage_runner import check_memory_preflight, run_stage_subprocess

    cfg = _resolve_cfg(args)
    check_memory_preflight()

    tts_backend = args.backend or cfg["tts"]["backend"]
    project_dir = _resolve_existing_project_dir(args.script, args.out)
    assets_arg = args.assets

    # If no --assets was provided or the character kit does not exist yet (e.g. legacy audio-only tests),
    # run narration and stop cleanly if video assets are absent.
    if not assets_arg and args.no_export:
        code = _narrate(args)
        if code == 0:
            print("Next: pass `--assets assets` (without `--no-export`) to run mix, lipsync, and video render.")
        return code

    assets_dir = Path(assets_arg) if assets_arg else Path("assets")
    # When running with real neural TTS, isolate heavy stages in subprocesses so memory is
    # returned to the OS when the subprocess exits before Rhubarb and FFmpeg run.
    if tts_backend in ("kokoro", "chatterbox_turbo", "chatterbox.tts_turbo"):
        py = sys.executable
        main_py = str(Path(__file__).resolve())
        common = ["-o", str(project_dir), "--assets", str(assets_dir)]
        if args.config:
            common.extend(["--config", args.config])
        if getattr(args, "playlist", None):
            common.extend(["--playlist", args.playlist])
        if getattr(args, "voice", None):
            common.extend(["--voice", args.voice])
        if args.force:
            common.append("--force")

        narr_cmd = [py, main_py, "narrate", args.script, "--backend", tts_backend, *common]
        if args.no_polish:
            narr_cmd.append("--no-polish")
        if args.no_export:
            narr_cmd.append("--no-export")

        mix_cmd = [py, main_py, "mix", str(project_dir), *common]
        ls_cmd = [py, main_py, "lipsync", str(project_dir), *common]
        if args.rhubarb:
            ls_cmd.extend(["--rhubarb", args.rhubarb])
        ren_cmd = [py, main_py, "render", str(project_dir), *common]
        if getattr(args, "scenes", None):
            ren_cmd.extend(["--scenes", args.scenes])
        if getattr(args, "character", None):
            ren_cmd.extend(["--character", args.character])

        try:
            run_stage_subprocess("narrate", narr_cmd)
            if not args.no_export:
                run_stage_subprocess("mix", mix_cmd)
            run_stage_subprocess("lipsync", ls_cmd)
            run_stage_subprocess("render", ren_cmd)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        return 0

    # Fast in-process path for fake backend / tests
    code = _narrate(args)
    if code != 0:
        return code

    from parser.config import canonical_character
    from render.kit import resolve_character_kit
    project_json = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    char_id = canonical_character(
        getattr(args, "character", None) or project_json["project"]["character"], cfg
    )
    kit_dir = resolve_character_kit(char_id, assets_dir, cfg)
    if not (kit_dir / "character.json").exists():
        print(f"Next: create character kit in {kit_dir} or pass --character atlas to render video.")
        return 0

    if not args.no_export:
        mix_args = argparse.Namespace(**{**vars(args), "script": str(project_dir), "assets": str(assets_dir)})
        code = cmd_mix(mix_args)
        if code != 0:
            return code

    ls_args = argparse.Namespace(**{**vars(args), "script": str(project_dir), "assets": str(assets_dir)})
    code = cmd_lipsync(ls_args)
    if code != 0:
        return code

    ren_args = argparse.Namespace(**{**vars(args), "script": str(project_dir), "assets": str(assets_dir)})
    return cmd_render(ren_args)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "build")                                     # python main.py script.md
    ap = argparse.ArgumentParser(prog="main.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    table = (
        ("parse", cmd_parse),
        ("validate", cmd_validate),
        ("narrate", cmd_narrate),
        ("regenerate", cmd_regenerate),
        ("lipsync", cmd_lipsync),
        ("mix", cmd_mix),
        ("render", cmd_render),
        ("build", cmd_build),
    )
    for name, fn in table:
        sp = sub.add_parser(name)
        sp.add_argument(
            "script",
            help=("project folder or project.json" if name in ("mix", "lipsync", "render")
                  else "script .md (or project.json for narrate)"),
        )
        if name == "regenerate":
            sp.add_argument("ids", nargs="+", help="segment ids (scene03_seg04) or scene ids (scene03)")
        sp.add_argument("--config", help="channel config JSON (default: auto-detected from script channel)")
        sp.add_argument("--playlist", help="optional playlist profile name or JSON path")
        sp.add_argument("--voice", help="override TTS voice id")
        sp.add_argument("--assets", help="assets folder to check referenced files against")
        sp.add_argument("--strict", action="store_true", help="treat warnings as failures (validate)")
        sp.add_argument("--force", action="store_true", help="parse: write despite errors; narrate/lipsync/render: ignore caches")
        sp.add_argument("--backend", choices=["kokoro", "chatterbox_turbo", "chatterbox.tts_turbo", "fake"],
                        help="TTS backend (default from config)")
        sp.add_argument("--rhubarb", choices=["rhubarb", "fake"], help="Rhubarb backend (default: rhubarb)")
        sp.add_argument("--character", help="override character kit id (e.g. atlas or CBBob)")
        sp.add_argument("--scenes", help="render only selected scenes, e.g. 1 or 1,3-4")
        sp.add_argument("--no-polish", action="store_true", help="skip the per-segment ffmpeg chain")
        sp.add_argument("--no-export", action="store_true", help="skip the full_narration.wav export")
        sp.add_argument("-o", "--out", help="project folder (default: projects/<script name>)")
        sp.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
