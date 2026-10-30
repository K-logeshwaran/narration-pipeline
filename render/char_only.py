#!/usr/bin/env python3
"""Transparent, character-only renderer (no background, no captions, no audio).

Reuses the pipeline's own character code, so the output is frame-exact with the
scene videos and Bob's bounce/sway is phase-continuous across scene cuts:

    timeline.json (v1.2) -> build_char_timeline -> composite PNG cache ->
    build_char_filter_branch -> (premultiplied downscale) -> ProRes 4444 / WebM alpha

Outputs (default `<project>/renders/char/`):
    scene01.mov, scene02.mov, ...   one clip per scene, exactly (end_frame - start_frame) frames
    full_char.mov                   one clip for the whole video (with --combined)

Usage:
    python3 -m render.char_only projects/example --assets assets --character CBbob
    python3 -m render.char_only projects/example --scenes 1,3-4 --format webm --height 540
    python3 -m render.char_only projects/example --combined
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from narration.errors import AudioToolError
from parser.config import load_channel_config

from .character import (
    CharFrameSpan,
    build_char_filter_branch,
    build_char_timeline,
    prepare_scene_character_concat,
)
from .kit import CharacterKit, KitError
from .timeline_v2 import upgrade_project_timeline

CHAR_VIDEO_VERSION = "cv1"
FORMATS = ("mov", "webm")


def _hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _parse_scenes_arg(spec: Optional[str], available_ids: Sequence[str]) -> Optional[Set[str]]:
    """Same syntax as render.scene: `1`, `1,3-4`, `scene05`."""
    if not spec:
        return None
    wanted_nums: Set[int] = set()
    wanted_ids: Set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower().startswith("scene"):
            wanted_ids.add(part.lower())
        elif "-" in part:
            a, b = part.split("-", 1)
            wanted_nums.update(range(int(a), int(b) + 1))
        else:
            wanted_nums.add(int(part))
    for n in wanted_nums:
        wanted_ids.add(f"scene{n:02d}")
    unknown = sorted(wanted_ids - set(available_ids))
    if unknown:
        raise ValueError(f"Unknown scene(s): {', '.join(unknown)} (available: {', '.join(available_ids)})")
    return wanted_ids


def _out_dims(kit: CharacterKit, height: Optional[int]) -> Optional[Tuple[int, int]]:
    """Target (w, h), both even, keeping the kit's trim aspect. None = keep native size."""
    tw = kit.trim_box[2] - kit.trim_box[0]
    th = kit.trim_box[3] - kit.trim_box[1]
    if not height or height >= th:
        return None
    h = max(2, int(height) // 2 * 2)
    w = max(2, int(round(h * tw / th)) // 2 * 2)
    return w, h


def _probe_frames(path: Path) -> Optional[int]:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.check_output(
            [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=nb_frames", "-of", "default=nw=1:nk=1", str(path)],
            text=True, stderr=subprocess.DEVNULL).strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def _encode_clip(spans: Sequence[CharFrameSpan], kit: CharacterKit, fps: int,
                 scene_start_s: float, n_frames: int, dst: Path, fmt: str,
                 dims: Optional[Tuple[int, int]], char_cache_dir: Path) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioToolError("ffmpeg was not found on PATH.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = dst.with_name(f"{dst.stem}.{os.getpid()}.tmp{dst.suffix}")

    pix = "yuva444p10le" if fmt == "mov" else "yuva420p"
    with tempfile.TemporaryDirectory() as tmp:
        concat_txt = prepare_scene_character_concat(spans, kit, char_cache_dir, Path(tmp) / "char_cues.txt")
        chain = [build_char_filter_branch(0, kit, fps, scene_start_s, out_label="char_raw")]
        if dims:
            w, h = dims
            # downscale on premultiplied alpha so edges don't pick up dark fringes
            chain.append(
                f"[char_raw]format=rgba,premultiply=inplace=1,"
                f"scale={w}:{h}:flags=lanczos,unpremultiply=inplace=1,format={pix}[v_out]")
        else:
            chain.append(f"[char_raw]format={pix}[v_out]")

        cmd = [exe, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(concat_txt),
               "-filter_complex", ";".join(chain),
               "-map", "[v_out]", "-an",
               "-fps_mode", "cfr", "-r", str(fps),
               "-frames:v", str(n_frames)]
        if fmt == "mov":
            cmd += ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", pix, "-vendor", "apl0"]
        else:
            cmd += ["-c:v", "libvpx-vp9", "-pix_fmt", pix, "-b:v", "0", "-crf", "24",
                    "-auto-alt-ref", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
        cmd.append(str(tmp_out))

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not tmp_out.exists():
            if tmp_out.exists():
                tmp_out.unlink()
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
            raise AudioToolError(f"ffmpeg failed rendering {dst.name} (exit {proc.returncode}):\n{tail}")
    os.replace(tmp_out, dst)


def _stamp_path(dst: Path) -> Path:
    return dst.with_name(dst.name + ".hash")


def _is_fresh(dst: Path, h: str) -> bool:
    sp = _stamp_path(dst)
    return dst.exists() and sp.exists() and sp.read_text(encoding="utf-8").strip() == h


def _render_one(label: str, spans: Sequence[CharFrameSpan], kit: CharacterKit, fps: int,
                scene_start_s: float, n_frames: int, dst: Path, fmt: str,
                dims: Optional[Tuple[int, int]], char_cache_dir: Path,
                h: str, force: bool, warnings: List[str]) -> str:
    if not force and _is_fresh(dst, h):
        return "cache"
    _encode_clip(spans, kit, fps, scene_start_s, n_frames, dst, fmt, dims, char_cache_dir)
    _stamp_path(dst).write_text(h + "\n", encoding="utf-8")
    got = _probe_frames(dst)
    if got is not None and got != n_frames:
        warnings.append(f"{label}: expected {n_frames} frames but {dst.name} has {got}")
    return "rendered"


def render_char_clips(project_dir: Path, assets_dir: Path, cfg: Dict[str, Any], *,
                      scenes_spec: Optional[str] = None,
                      character_override: Optional[str] = None,
                      fmt: str = "mov",
                      height: Optional[int] = 540,
                      combined: bool = False,
                      out_dir: Optional[Path] = None,
                      force: bool = False,
                      log: Callable[[str], None] = print) -> Tuple[List[Path], List[str]]:
    project_dir = Path(project_dir)
    assets_dir = Path(assets_dir)
    out_dir = Path(out_dir) if out_dir else project_dir / "renders" / "char"
    char_cache_dir = project_dir / "cache" / "char"
    warnings: List[str] = []

    tl, kit = upgrade_project_timeline(project_dir, cfg, assets_dir, character_override=character_override)
    fps = int(tl["fps"])
    all_scenes = tl.get("scenes", [])
    wanted = _parse_scenes_arg(scenes_spec, [s["id"] for s in all_scenes])
    selected = [s for s in all_scenes if wanted is None or s["id"] in wanted]
    dims = _out_dims(kit, height)
    size_txt = f"{dims[0]}x{dims[1]}" if dims else f"{kit.trim_box[2] - kit.trim_box[0]}x{kit.trim_box[3] - kit.trim_box[1]}"

    log(f"Rendering {len(selected)} character clip(s){' + combined' if combined else ''} "
        f"with kit '{kit.id}' ({size_txt} @ {fps}fps, .{fmt}, transparent, video only) ...")

    out_paths: List[Path] = []
    stats = {"rendered": 0, "cache": 0}
    t0 = time.time()

    for i, sc in enumerate(selected, 1):
        t_sc = time.time()
        tr = sc["character_track"]
        sf, ef = int(sc["start_frame"]), int(sc["end_frame"])
        n_frames = max(ef - sf, 1)
        spans = build_char_timeline(sf, ef, fps, kit, tr.get("mouth_cues", []),
                                    tr.get("blinks", []), tr.get("pose_spans", []))
        dst = out_dir / f"{sc['id']}.{fmt}"
        h = _hash({"v": CHAR_VIDEO_VERSION, "kit": kit.kit_sha256, "track": tr, "sf": sf, "ef": ef,
                   "fps": fps, "fmt": fmt, "dims": dims})
        status = _render_one(sc["id"], spans, kit, fps, float(sc["start"]), n_frames, dst, fmt, dims,
                             char_cache_dir, h, force, warnings)
        stats[status] += 1
        out_paths.append(dst)
        log(f"  [{i:>2}/{len(selected)}] {sc['id']}  {n_frames / fps:6.2f}s ({n_frames:>4}f)  "
            f"{status} ({time.time() - t_sc:.1f}s)")

    if combined:
        t_c = time.time()
        total = int(tl["total_frames"])
        mouth: List[Dict[str, Any]] = []
        blinks: List[Sequence[float]] = []
        poses: List[Dict[str, Any]] = []
        for sc in all_scenes:  # always the whole video, regardless of --scenes
            tr = sc["character_track"]
            mouth += list(tr.get("mouth_cues", []))
            blinks += list(tr.get("blinks", []))
            poses += list(tr.get("pose_spans", []))
        mouth.sort(key=lambda c: float(c.get("start", 0.0)))
        blinks.sort(key=lambda b: float(b[0]))
        poses.sort(key=lambda p: float(p.get("start", 0.0)))
        spans = build_char_timeline(0, total, fps, kit, mouth, blinks, poses)
        dst = out_dir / f"full_char.{fmt}"
        h = _hash({"v": CHAR_VIDEO_VERSION, "kit": kit.kit_sha256, "total": total, "fps": fps, "fmt": fmt,
                   "dims": dims, "tracks": [s["character_track"] for s in all_scenes]})
        status = _render_one("full_char", spans, kit, fps, 0.0, total, dst, fmt, dims,
                             char_cache_dir, h, force, warnings)
        stats[status] += 1
        out_paths.append(dst)
        log(f"  [all] full_char  {total / fps:6.2f}s ({total:>5}f)  {status} ({time.time() - t_c:.1f}s)")

    log(f"Done in {time.time() - t0:.1f}s: {stats['rendered']} rendered, {stats['cache']} up to date.")
    return out_paths, warnings


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m render.char_only",
                                 description="Render transparent character-only clips (per scene and/or combined).")
    ap.add_argument("project", help="Project folder (e.g. projects/example)")
    ap.add_argument("--assets", default="assets", help="Assets folder (default: assets)")
    ap.add_argument("--scenes", help="Scene filter, e.g. 1 or 1,3-4 (per-scene clips only)")
    ap.add_argument("--character", help="Character kit id or alias (e.g. CBbob)")
    ap.add_argument("--config", help="Channel config JSON (default: config/channel_main.json)")
    ap.add_argument("--format", dest="fmt", choices=FORMATS, default="mov",
                    help="mov = ProRes 4444 alpha (default), webm = VP9 alpha")
    ap.add_argument("--height", type=int, default=540,
                    help="output height in px (0 = native kit size). Default 540")
    ap.add_argument("--combined", action="store_true",
                    help="also render one clip for the whole video (full_char.<fmt>)")
    ap.add_argument("--out", help="output folder (default: <project>/renders/char)")
    ap.add_argument("--force", action="store_true", help="re-render even if up to date")
    args = ap.parse_args(argv)

    cfg = load_channel_config(args.config)
    try:
        paths, warns = render_char_clips(
            Path(args.project), Path(args.assets), cfg,
            scenes_spec=args.scenes, character_override=args.character,
            fmt=args.fmt, height=args.height or None, combined=args.combined,
            out_dir=Path(args.out) if args.out else None, force=args.force)
    except (KitError, AudioToolError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in warns:
        print(f"WARNING: {w}")
    for p in paths:
        print(f"Wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
