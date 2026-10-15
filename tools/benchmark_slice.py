#!/usr/bin/env python3
"""M1.4 One-Scene Vertical Slice & Telemetry Runner.

Runs a complete 1-scene vertical slice end-to-end:
  Parse -> Narrate -> SFX Mix -> Rhubarb Lip-Sync -> Layered Character + ASS Scene Render -> Audio Mux -> Drift Check

Measures wall-clock time and Peak RSS (self + child processes) so we can compare
real Fedora 44 / Ryzen 5000U numbers against our Phase 2 estimates (300 MB / 4.0s).

Usage:
    /usr/bin/time -v python3 tools/benchmark_slice.py \
        tests/fixtures/example_v11.md --assets assets --character atlas -o /tmp/m14_slice
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from narration import get_backend, lipsync_project, mix_sfx, narrate_project  # noqa: E402
from parser import load_channel_config, parse_file, validate  # noqa: E402
from render import render_project_scenes  # noqa: E402


def _peak_rss_mib() -> Tuple[float, float, float]:
    """Return (self_rss_mib, children_rss_mib, max_observed_mib).
    On Linux, ru_maxrss is in kilobytes."""
    self_kb = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    child_kb = float(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    vm_hwm_kb = 0.0
    status_path = Path("/proc/self/status")
    if status_path.exists():
        for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmHWM:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    vm_hwm_kb = float(parts[1])
                break
    self_mib = max(self_kb, vm_hwm_kb) / 1024.0
    child_mib = child_kb / 1024.0
    # Note: child_mib is the max RSS of the single largest child process;
    # while a child runs, Python parent is also resident in RAM.
    return round(self_mib, 1), round(child_mib, 1), round(max(self_mib, child_mib), 1)


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


def mux_scene_slice(video_mp4: Path, audio_wav: Path, out_mp4: Path, fps: int) -> Dict[str, float]:
    """Mux video-only scene MP4 with full_mix.wav (without re-encoding video)
    and verify < 1 frame duration drift."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp_mp4 = out_mp4.with_name(out_mp4.name + f".{os.getpid()}.tmp.mp4")

    v_dur_in = _ffprobe_stream_duration(video_mp4, "v:0")
    a_dur_in = _ffprobe_stream_duration(audio_wav, "a:0")

    cmd = [
        exe, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
        "-i", str(video_mp4),
        "-i", str(audio_wav),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{v_dur_in:.6f}",
        "-movflags", "+faststart",
        str(tmp_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not tmp_mp4.exists():
        if tmp_mp4.exists():
            tmp_mp4.unlink()
        raise RuntimeError(f"ffmpeg mux failed: {proc.stderr}")
    os.replace(tmp_mp4, out_mp4)

    v_dur_out = _ffprobe_stream_duration(out_mp4, "v:0")
    a_dur_out = _ffprobe_stream_duration(out_mp4, "a:0")
    drift_s = abs(v_dur_out - a_dur_in)
    drift_frames = drift_s * float(fps)
    return {
        "video_duration_s": round(v_dur_out, 4),
        "audio_duration_s": round(a_dur_in, 4),
        "muxed_audio_duration_s": round(a_dur_out, 4),
        "drift_s": round(drift_s, 4),
        "drift_frames": round(drift_frames, 3),
    }


def run_vertical_slice(
    script_path: Path,
    out_dir: Path,
    assets_dir: Path,
    cfg: Dict[str, Any],
    *,
    scene_number: int = 1,
    character: str = "atlas",
    tts_backend: str = "fake",
    rhubarb_backend: str = "rhubarb",
    polish: bool = True,
    force: bool = True,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Run a 1-scene vertical slice and return telemetry & verification metrics."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = int(cfg["render"]["fps"])

    timings: Dict[str, float] = {}
    t_total0 = time.perf_counter()

    # 1. Parse & slice to 1 scene
    t0 = time.perf_counter()
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

    proj_json_path = out_dir / "project.json"
    proj_json_path.write_text(json.dumps(proj_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    timings["parse_s"] = round(time.perf_counter() - t0, 3)

    # 2. Narrate + unload TTS backend
    t0 = time.perf_counter()
    backend = get_backend(tts_backend, cfg)
    narr_res = narrate_project(
        proj_dict, out_dir, cfg, backend,
        force=force, polish=polish, export=True, assets_dir=assets_dir, log=log
    )
    if hasattr(backend, "close"):
        backend.close()
    timings["narrate_s"] = round(time.perf_counter() - t0, 3)

    # 3. Mix SFX
    t0 = time.perf_counter()
    mcfg = cfg.get("mix") or {}
    mix_path = out_dir / "audio" / mcfg.get("out_name", "full_mix.wav")
    mix_sfx(narr_res.timeline, narr_res.export_path, assets_dir, cfg, mix_path, log=log)
    timings["mix_s"] = round(time.perf_counter() - t0, 3)

    # 4. Rhubarb Lip-Sync
    t0 = time.perf_counter()
    lipsync_project(out_dir, cfg, backend=rhubarb_backend, force=force, log=log)
    timings["lipsync_s"] = round(time.perf_counter() - t0, 3)

    # 5. Scene Render (Layered Character + Background + ASS Captions)
    t0 = time.perf_counter()
    sc_res = render_project_scenes(
        out_dir, assets_dir, cfg,
        scenes_spec="1", character_override=character, force=force, log=log
    )
    timings["scene_render_s"] = round(time.perf_counter() - t0, 3)

    # 6. Mux Scene Video + Mixed Audio & Verify Drift
    t0 = time.perf_counter()
    scene_video_mp4 = sc_res.scenes_rendered[0]
    slice_mp4 = out_dir / "renders" / "scene01_slice.mp4"
    sync_info = mux_scene_slice(scene_video_mp4, mix_path, slice_mp4, fps)
    timings["mux_s"] = round(time.perf_counter() - t0, 3)
    timings["total_s"] = round(time.perf_counter() - t_total0, 3)

    self_mib, child_mib, max_mib = _peak_rss_mib()
    report = {
        "script": str(script_path),
        "scene_id": "scene01",
        "character": character,
        "tts_backend": tts_backend,
        "rhubarb_backend": rhubarb_backend,
        "output_mp4": str(slice_mp4),
        "timings": timings,
        "memory_mib": {
            "python_self_peak_mib": self_mib,
            "largest_child_peak_mib": child_mib,
            "combined_upper_bound_mib": round(self_mib + child_mib, 1),
            "phase2_estimate_mib": 300.0,
        },
        "sync": sync_info,
    }
    (out_dir / "telemetry.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Optional[ list[str] ] = None) -> int:
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
    ap.add_argument("--no-polish", action="store_true", help="Skip per-segment ffmpeg audio polish")
    ap.add_argument("--config", help="Channel config JSON (default: config/channel_main.json)")
    ap.add_argument("-o", "--out", default="/tmp/m14_slice", help="Output workspace directory")
    args = ap.parse_args(argv)

    cfg = load_channel_config(args.config)
    try:
        report = run_vertical_slice(
            Path(args.script),
            Path(args.out),
            Path(args.assets),
            cfg,
            scene_number=args.scene,
            character=args.character,
            tts_backend=args.backend,
            rhubarb_backend=args.rhubarb,
            polish=not args.no_polish,
            force=True,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    t = report["timings"]
    m = report["memory_mib"]
    s = report["sync"]
    print("\n" + "=" * 68)
    print(f"M1.4 VERTICAL SLICE TELEMETRY  (character={report['character']}, tts={report['tts_backend']}, rhubarb={report['rhubarb_backend']})")
    print("=" * 68)
    print(f"  Parse & Validate : {t['parse_s']:6.2f} s")
    print(f"  Narrate (TTS)    : {t['narrate_s']:6.2f} s")
    print(f"  SFX Mix          : {t['mix_s']:6.2f} s")
    print(f"  Rhubarb Lip-Sync : {t['lipsync_s']:6.2f} s")
    print(f"  Scene Render     : {t['scene_render_s']:6.2f} s   (Phase 2 estimate: ~4.00 s)")
    print(f"  Audio/Video Mux  : {t['mux_s']:6.2f} s")
    print(f"  ----------------------------------------")
    print(f"  Total Wall Time  : {t['total_s']:6.2f} s")
    print("-" * 68)
    print(f"  Python Peak RSS  : {m['python_self_peak_mib']:6.1f} MiB")
    print(f"  Child Peak RSS   : {m['largest_child_peak_mib']:6.1f} MiB (FFmpeg / Rhubarb)")
    print(f"  Upper-Bound RAM  : {m['combined_upper_bound_mib']:6.1f} MiB   (Phase 2 estimate: ~300.0 MiB)")
    print("-" * 68)
    print(f"  Video Duration   : {s['video_duration_s']:6.3f} s")
    print(f"  Audio Duration   : {s['audio_duration_s']:6.3f} s")
    print(f"  A/V Sync Drift   : {s['drift_s'] * 1000:6.2f} ms ({s['drift_frames']:.2f} frames)")
    print(f"  Playable Output  : {report['output_mp4']}")
    print("=" * 68)

    if s["drift_frames"] > 1.0:
        print(f"FAIL: A/V sync drift ({s['drift_frames']:.2f} frames) exceeds 1.0 frame limit!", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())