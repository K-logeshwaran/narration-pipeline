#!/usr/bin/env python3
"""M1.4 One-Scene Vertical Slice & Per-Stage Subprocess Telemetry Runner.

Runs each pipeline stage in its own isolated child process via `run_stage_subprocess`:
  1. Parse & Slice (1 scene)
  2. Narrate (Kokoro or Fake TTS) -> exits & returns 100% of PyTorch RAM to OS
  3. SFX Mix
  4. Rhubarb Lip-Sync
  5. Scene Render (1920x1080 Background with Active Zoom/Pan + Cutaway + Layered Character + ASS Captions)
  6. Audio/Video Mux + ffprobe Drift Verification

Usage:
    # Full 1-scene benchmark with real Kokoro + real Rhubarb:
    /usr/bin/time -v python3 tools/benchmark_slice.py \
        tests/fixtures/example_v11.md --assets assets --character atlas --backend kokoro --rhubarb rhubarb -o /tmp/m14_slice_kokoro

    # Render-only benchmark (with 1920x1080 scene background + zoom_in + reaction cutaway):
    /usr/bin/time -v python3 tools/benchmark_slice.py \
        --render-only --character atlas --assets assets -o /tmp/m14_slice_kokoro
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from render.stage_runner import StageMetrics, check_memory_preflight, run_stage_subprocess  # noqa: E402


def ensure_test_scene_images(assets_dir: Path) -> None:
    """Ensure a real 1920x1080 scene illustration (`s01_autopsy.png`, `s02_shady_brain.png`)
    and reaction image (`done_with_everything.jpg`) exist in `assets_dir` so zoom/pan
    and cutaway overlays are tested on full 1920x1080 images."""
    from PIL import Image, ImageDraw

    scenes_dir = Path(assets_dir) / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    for stem in ("s01_autopsy", "s02_shady_brain"):
        p = scenes_dir / f"{stem}.png"
        if not p.exists():
            im = Image.new("RGB", (1920, 1080), (248, 248, 252))
            draw = ImageDraw.Draw(im)
            for x in range(0, 1920, 120):
                draw.line([(x, 0), (x, 1080)], fill=(225, 228, 235), width=2)
            for y in range(0, 1080, 120):
                draw.line([(0, y), (1920, y)], fill=(225, 228, 235), width=2)
            draw.rectangle((640, 260, 1560, 820), fill=(235, 240, 250), outline=(40, 50, 70), width=6)
            draw.ellipse((920, 400, 1280, 680), fill=(255, 195, 205), outline=(40, 40, 40), width=5)
            im.save(p, format="PNG")

    chars_dir = Path(assets_dir) / "characters"
    chars_dir.mkdir(parents=True, exist_ok=True)
    react_p = chars_dir / "done_with_everything.jpg"
    if not react_p.exists():
        im = Image.new("RGB", (1920, 1080), (240, 232, 225))
        draw = ImageDraw.Draw(im)
        draw.rectangle((500, 200, 1600, 880), fill=(255, 250, 240), outline=(30, 30, 30), width=6)
        im.save(react_p, format="JPEG", quality=92)


def _ffprobe_stream_duration(path: Path, stream_selector: str = "v:0") -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-select_streams", stream_selector,
        "-show_entries", "stream=duration:format=duration",
        "-of", "json", str(path),
    ], text=True)
    data = json.loads(out)
    streams = data.get("streams") or []
    if streams and streams[0].get("duration") not in (None, "N/A"):
        return float(streams[0]["duration"])
    return float(data["format"]["duration"])


def _slice_script_to_one_scene(script_path: Path, out_dir: Path,
                               config_path: Optional[str], scene_number: int,
                               character: str) -> Path:
    from parser import load_channel_config, parse_file, validate

    cfg = load_channel_config(config_path)
    parsed = parse_file(str(script_path), cfg)
    extra_diags = validate(parsed.project, cfg, None)
    errors = [d for d in (parsed.diagnostics + extra_diags) if d.level == "error"]
    if errors:
        raise ValueError("Script validation failed:\n" + "\n".join(d.format() for d in errors))

    matching = [sc for sc in parsed.project.scenes if sc.number == scene_number]
    if not matching:
        raise ValueError(f"Scene {scene_number} not found in {script_path}")

    sliced_sc = matching[0]
    proj_dict = parsed.project.to_dict()
    proj_dict["project"]["character"] = character
    sc_dict = asdict(sliced_sc)
    sc_dict["number"] = 1
    sc_dict["id"] = "scene01"
    for seg in sc_dict["segments"]:
        seg["scene_id"] = "scene01"
        seg["id"] = f"scene01_seg{seg['index']:02d}"
    proj_dict["scenes"] = [sc_dict]

    out_dir.mkdir(parents=True, exist_ok=True)
    proj_json_path = out_dir / "project.json"
    proj_json_path.write_text(json.dumps(proj_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return proj_json_path


def run_vertical_slice(
    script_path: Path,
    out_dir: Path,
    assets_dir: Path,
    cfg: Dict[str, Any],
    *,
    config_path: Optional[str] = None,
    scene_number: int = 1,
    character: str = "atlas",
    tts_backend: str = "fake",
    rhubarb_backend: str = "rhubarb",
    polish: bool = True,
    force: bool = True,
    render_only: bool = False,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run the 1-scene vertical slice with each stage in an isolated subprocess."""
    out_dir = Path(out_dir).resolve()
    assets_dir = Path(assets_dir).resolve()
    script_path = Path(script_path).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    check_memory_preflight(log=log)
    ensure_test_scene_images(assets_dir)

    fps = int(cfg["render"]["fps"])
    py = sys.executable
    main_py = str(ROOT / "main.py")
    cfg_args = ["--config", str(Path(config_path).resolve())] if config_path else []
    quiet = (log is not print)

    stage_metrics: Dict[str, Dict[str, float]] = {}
    timings: Dict[str, float] = {}
    t_total0 = time.perf_counter()

    def _record(m: StageMetrics) -> None:
        timings[f"{m.name}_s"] = m.elapsed_s
        stage_metrics[m.name] = {
            "elapsed_s": m.elapsed_s,
            "peak_rss_mib": m.peak_rss_mib,
            "wait4_maxrss_mib": m.wait4_maxrss_mib,
            "proc_tree_peak_mib": m.proc_tree_peak_mib,
        }

    proj_json = out_dir / "project.json"
    mcfg = cfg.get("mix") or {}
    mix_path = out_dir / "audio" / mcfg.get("out_name", "full_mix.wav")

    if not render_only:
        # 1. Parse & slice in an isolated subprocess
        parse_code = (
            "import sys; from pathlib import Path; "
            "from tools.benchmark_slice import _slice_script_to_one_scene; "
            f"_slice_script_to_one_scene(Path({str(script_path)!r}), Path({str(out_dir)!r}), "
            f"{config_path!r}, {int(scene_number)}, {character!r})"
        )
        _record(run_stage_subprocess("parse", [py, "-c", parse_code], cwd=ROOT, quiet_output=quiet))

        # 2. Narrate in an isolated subprocess (releases 100% of Kokoro/PyTorch RAM on exit)
        narr_cmd = [
            py, main_py, "narrate", str(proj_json),
            "--backend", tts_backend,
            "--assets", str(assets_dir),
            "-o", str(out_dir),
            *cfg_args,
        ]
        if not polish:
            narr_cmd.append("--no-polish")
        if force:
            narr_cmd.append("--force")
        _record(run_stage_subprocess("narrate", narr_cmd, cwd=ROOT, quiet_output=quiet))

        # 3. Mix SFX in an isolated subprocess
        mix_cmd = [
            py, main_py, "mix", str(out_dir),
            "--assets", str(assets_dir),
            *cfg_args,
        ]
        _record(run_stage_subprocess("mix", mix_cmd, cwd=ROOT, quiet_output=quiet))

        # 4. Rhubarb Lip-Sync in an isolated subprocess
        ls_cmd = [
            py, main_py, "lipsync", str(out_dir),
            "--rhubarb", rhubarb_backend,
            *cfg_args,
        ]
        if force:
            ls_cmd.append("--force")
        _record(run_stage_subprocess("lipsync", ls_cmd, cwd=ROOT, quiet_output=quiet))

    # 5. Scene Render in an isolated subprocess
    sc_cmd = [
        py, "-m", "render.scene", str(out_dir),
        "--assets", str(assets_dir),
        "--scenes", "1",
        "--character", character,
        *cfg_args,
    ]
    if force:
        sc_cmd.append("--force")
    _record(run_stage_subprocess("scene_render", sc_cmd, cwd=ROOT, quiet_output=quiet))

    # 6. Mux Scene Video + Audio in an isolated subprocess & verify drift
    scene_video_mp4 = out_dir / "renders" / "scenes" / "scene01.mp4"
    slice_mp4 = out_dir / "renders" / "scene01_slice.mp4"
    v_dur_in = _ffprobe_stream_duration(scene_video_mp4, "v:0")
    a_dur_in = _ffprobe_stream_duration(mix_path, "a:0")
    tmp_mp4 = slice_mp4.with_name(slice_mp4.name + f".{os.getpid()}.tmp.mp4")
    slice_mp4.parent.mkdir(parents=True, exist_ok=True)

    mux_cmd = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(scene_video_mp4),
        "-i", str(mix_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{v_dur_in:.6f}",
        "-movflags", "+faststart",
        str(tmp_mp4),
    ]
    _record(run_stage_subprocess("mux", mux_cmd, cwd=ROOT, quiet_output=quiet))
    os.replace(tmp_mp4, slice_mp4)

    v_dur_out = _ffprobe_stream_duration(slice_mp4, "v:0")
    a_dur_out = _ffprobe_stream_duration(slice_mp4, "a:0")
    drift_s = abs(v_dur_out - a_dur_in)
    drift_frames = drift_s * float(fps)
    sync_info = {
        "video_duration_s": round(v_dur_out, 4),
        "audio_duration_s": round(a_dur_in, 4),
        "muxed_audio_duration_s": round(a_dur_out, 4),
        "drift_s": round(drift_s, 4),
        "drift_frames": round(drift_frames, 3),
    }

    timings["total_s"] = round(time.perf_counter() - t_total0, 3)

    peak_stage_name = max(stage_metrics, key=lambda k: stage_metrics[k]["peak_rss_mib"])
    overall_peak_mib = stage_metrics[peak_stage_name]["peak_rss_mib"]
    render_peak_mib = stage_metrics["scene_render"]["peak_rss_mib"]

    report = {
        "script": str(script_path),
        "scene_id": "scene01",
        "character": character,
        "tts_backend": tts_backend,
        "rhubarb_backend": rhubarb_backend,
        "render_only": render_only,
        "output_mp4": str(slice_mp4),
        "timings": timings,
        "stages": stage_metrics,
        "memory_mib": {
            "overall_peak_rss_mib": overall_peak_mib,
            "peak_stage": peak_stage_name,
            "scene_render_peak_rss_mib": render_peak_mib,
            "python_self_peak_mib": overall_peak_mib,
        },
        "sync": sync_info,
    }
    (out_dir / "telemetry.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="benchmark_slice.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", nargs="?", default="tests/fixtures/example_v11.md",
                    help="Input Markdown script (default: tests/fixtures/example_v11.md)")
    ap.add_argument("--scene", type=int, default=1, help="Scene number to slice (default: 1)")
    ap.add_argument("--character", default="atlas", help="Character kit ID (default: atlas)")
    ap.add_argument("--assets", default="assets", help="Assets root directory (default: assets)")
    ap.add_argument("--backend", choices=["kokoro", "fake"], default="fake",
                    help="TTS backend (default: fake; pass kokoro to benchmark real TTS)")
    ap.add_argument("--rhubarb", choices=["rhubarb", "fake"], default="rhubarb",
                    help="Rhubarb backend (default: rhubarb)")
    ap.add_argument("--render-only", action="store_true",
                    help="Run only scene_render + mux on an existing slice workspace (-o)")
    ap.add_argument("--no-polish", action="store_true", help="Skip per-segment ffmpeg audio polish")
    ap.add_argument("--config", help="Channel config JSON (default: config/channel_main.json)")
    ap.add_argument("-o", "--out", default="/tmp/m14_slice", help="Output workspace directory")
    args = ap.parse_args(argv)

    from parser import load_channel_config
    cfg = load_channel_config(args.config)
    try:
        report = run_vertical_slice(
            Path(args.script),
            Path(args.out),
            Path(args.assets),
            cfg,
            config_path=args.config,
            scene_number=args.scene,
            character=args.character,
            tts_backend=args.backend,
            rhubarb_backend=args.rhubarb,
            polish=not args.no_polish,
            force=True,
            render_only=args.render_only,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    stages = report["stages"]
    m = report["memory_mib"]
    s = report["sync"]
    print("\n" + "=" * 74)
    print(f"M1.4 SUBPROCESS TELEMETRY  (character={report['character']}, tts={report['tts_backend']}, rhubarb={report['rhubarb_backend']})")
    print("=" * 74)
    print(f"  {'Stage':<18} {'Wall Time':>10} {'Peak RSS':>12} {'(wait4 / /proc tree)':>26}")
    print("  " + "-" * 68)
    for st_name, st in stages.items():
        detail = f"({st['wait4_maxrss_mib']:.1f} / {st['proc_tree_peak_mib']:.1f} MiB)"
        print(f"  {st_name:<18} {st['elapsed_s']:>8.2f} s {st['peak_rss_mib']:>8.1f} MiB {detail:>26}")
    print("  " + "-" * 68)
    print(f"  {'Total Wall Time':<18} {report['timings']['total_s']:>8.2f} s")
    print(f"  {'True Peak RSS':<18} {m['overall_peak_rss_mib']:>8.1f} MiB   [in stage: {m['peak_stage']}]")
    print(f"  {'Render-Only Peak':<18} {m['scene_render_peak_rss_mib']:>8.1f} MiB")
    print("-" * 74)
    print(f"  Video Duration   : {s['video_duration_s']:6.3f} s")
    print(f"  Audio Duration   : {s['audio_duration_s']:6.3f} s")
    print(f"  A/V Sync Drift   : {s['drift_s'] * 1000:6.2f} ms ({s['drift_frames']:.2f} frames)")
    print(f"  Playable Output  : {report['output_mp4']}")
    print("=" * 74)

    if s["drift_frames"] > 1.0:
        print(f"FAIL: A/V sync drift ({s['drift_frames']:.2f} frames) exceeds 1.0 frame limit!", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())