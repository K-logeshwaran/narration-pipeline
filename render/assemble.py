#!/usr/bin/env python3
"""Final multi-scene video assembler and automated ffprobe A/V sync verifier.

Concatenates all per-scene MP4s (`renders/scenes/sceneNN.mp4`) without re-encoding
video (`-c:v copy`), muxes the mixed narration+SFX audio track (`audio/full_mix.wav`
or `audio/full_narration.wav`) once across the full timeline, and verifies via
`ffprobe` that total video stream duration matches total audio duration within
<= 1.0 frame at the output frame rate.

Usage as a standalone CLI:
    python3 -m render.assemble projects/example_v11
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

from narration.errors import AudioToolError
from parser.config import load_channel_config


@dataclass
class AssembleResult:
    output_mp4: Path
    scene_count: int
    total_frames: int
    video_duration_s: float
    audio_duration_s: float
    drift_s: float
    drift_frames: float
    warnings: List[str] = field(default_factory=list)


def _ffprobe_stream_info(path: Path, selector: str) -> Dict[str, Any]:
    exe = shutil.which("ffprobe")
    if not exe:
        raise AudioToolError("ffprobe was not found on PATH.")
    out = subprocess.check_output([
        exe, "-v", "error",
        "-select_streams", selector,
        "-show_entries", "stream=duration,nb_frames:format=duration",
        "-of", "json", str(path),
    ], text=True)
    data = json.loads(out)
    streams = data.get("streams") or []
    st = streams[0] if streams else {}
    dur = st.get("duration")
    if dur in (None, "N/A"):
        dur = (data.get("format") or {}).get("duration", 0.0)
    nb_frames = st.get("nb_frames")
    return {
        "duration_s": float(dur),
        "nb_frames": int(nb_frames) if str(nb_frames).isdigit() else None,
    }


def verify_av_sync(video_mp4: Path,
                   audio_wav: Path,
                   fps: int,
                   expected_frames: Optional[int] = None,
                   max_drift_frames: float = 1.0) -> Dict[str, float]:
    """Compare video stream duration in `video_mp4` against `audio_wav` duration
    using `ffprobe`. Raises `AudioToolError` if drift exceeds `max_drift_frames`."""
    v_info = _ffprobe_stream_info(video_mp4, "v:0")
    a_info = _ffprobe_stream_info(audio_wav, "a:0")
    mux_a_info = _ffprobe_stream_info(video_mp4, "a:0")

    v_dur = v_info["duration_s"]
    a_dur = a_info["duration_s"]
    drift_s = abs(v_dur - a_dur)
    drift_frames = drift_s * float(fps)

    if expected_frames is not None and v_info["nb_frames"] is not None:
        if abs(v_info["nb_frames"] - expected_frames) > 1:
            raise AudioToolError(
                f"Frame count mismatch in {video_mp4.name}: expected {expected_frames} frames, "
                f"ffprobe found {v_info['nb_frames']} frames."
            )

    if drift_frames > max_drift_frames:
        raise AudioToolError(
            f"A/V sync drift check failed for {video_mp4.name}: "
            f"video={v_dur:.4f}s vs audio={a_dur:.4f}s "
            f"(drift={drift_s * 1000:.2f}ms / {drift_frames:.2f} frames > {max_drift_frames:.2f} frame limit at {fps}fps)."
        )

    return {
        "video_duration_s": round(v_dur, 4),
        "audio_duration_s": round(a_dur, 4),
        "muxed_audio_duration_s": round(mux_a_info["duration_s"], 4),
        "drift_s": round(drift_s, 4),
        "drift_frames": round(drift_frames, 3),
    }


def assemble_project_video(project_dir: Path,
                           cfg: Dict[str, Any],
                           *,
                           out_path: Optional[Path] = None,
                           log: Callable[[str], None] = print) -> AssembleResult:
    """Concatenate all scene MP4s in `project_dir`, mux the master audio track,
    and verify < 1 frame A/V drift across the entire multi-scene video."""
    project_dir = Path(project_dir)
    tl_path = project_dir / "timeline.json"
    if not tl_path.exists():
        raise ValueError(f"missing {tl_path} - run `render` first.")

    timeline = json.loads(tl_path.read_text(encoding="utf-8"))
    scenes = timeline.get("scenes") or []
    if not scenes:
        raise ValueError(f"no scenes found in {tl_path}")

    fps = int(timeline.get("fps") or cfg["render"]["fps"] or 30)
    mcfg = cfg.get("mix") or {}
    mix_wav = project_dir / "audio" / mcfg.get("out_name", "full_mix.wav")
    narr_wav = project_dir / "audio" / "full_narration.wav"

    warnings: List[str] = []
    if mix_wav.is_file():
        audio_path = mix_wav
    elif narr_wav.is_file():
        audio_path = narr_wav
        warnings.append(f"{mix_wav.name} not found; assembling with {narr_wav.name} (no SFX mix).")
    else:
        raise ValueError(f"missing {mix_wav} and {narr_wav} - run `narrate` and `mix` first.")

    scenes_dir = project_dir / "renders" / "scenes"
    scene_files: List[Path] = []
    total_frames = 0
    for sc in scenes:
        sc_id = sc["id"]
        sc_mp4 = scenes_dir / f"{sc_id}.mp4"
        if not sc_mp4.is_file():
            raise ValueError(f"missing rendered scene file {sc_mp4} - run scene render for {sc_id} first.")
        scene_files.append(sc_mp4)
        if "start_frame" in sc and "end_frame" in sc:
            total_frames += int(sc["end_frame"]) - int(sc["start_frame"])

    if out_path is None:
        out_path = project_dir / "renders" / f"{project_dir.name}.mp4"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_mp4 = out_path.with_name(out_path.name + f".{os.getpid()}.tmp.mp4")

    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioToolError("ffmpeg was not found on PATH.")

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        concat_list = Path(tmp) / "scenes.txt"
        lines = [f"file '{p.resolve().as_posix()}'" for p in scene_files]
        concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

        target_dur = total_frames / float(fps) if total_frames > 0 else float(timeline.get("duration", 0.0))
        cmd = [
            exe, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
        ]
        if target_dur > 0:
            cmd.extend(["-t", f"{target_dur:.6f}"])
        cmd.extend(["-movflags", "+faststart", str(tmp_mp4)])

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not tmp_mp4.exists():
            if tmp_mp4.exists():
                tmp_mp4.unlink()
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
            raise AudioToolError(f"ffmpeg assembly failed (exit {proc.returncode}):\n{tail}")

        os.replace(tmp_mp4, out_path)

    sync = verify_av_sync(
        out_path,
        audio_path,
        fps=fps,
        expected_frames=total_frames if total_frames > 0 else None,
        max_drift_frames=1.0,
    )
    log(
        f"Assembled {len(scene_files)} scene(s) -> {out_path} in {time.time() - t0:.2f}s "
        f"(video={sync['video_duration_s']:.3f}s, audio={sync['audio_duration_s']:.3f}s, "
        f"drift={sync['drift_frames']:.2f}f)."
    )
    return AssembleResult(
        output_mp4=out_path,
        scene_count=len(scene_files),
        total_frames=total_frames,
        video_duration_s=sync["video_duration_s"],
        audio_duration_s=sync["audio_duration_s"],
        drift_s=sync["drift_s"],
        drift_frames=sync["drift_frames"],
        warnings=warnings,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m render.assemble",
                                 description="Assemble rendered scene MP4s and verify A/V sync.")
    ap.add_argument("project", help="Project folder (e.g. projects/example_v11)")
    ap.add_argument("--config", help="Channel config JSON (default: config/channel_main.json)")
    ap.add_argument("-o", "--out", help="Output MP4 path (default: <project>/renders/<name>.mp4)")
    args = ap.parse_args(argv)

    cfg = load_channel_config(args.config)
    try:
        res = assemble_project_video(
            Path(args.project),
            cfg,
            out_path=Path(args.out) if args.out else None,
        )
    except (AudioToolError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in res.warnings:
        print(f"WARNING: {w}")
    print(f"Wrote {res.output_mp4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())