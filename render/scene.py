#!/usr/bin/env python3
"""Per-scene FFmpeg video compositor and renderer.

Each scene is rendered independently to `cache/scenes/<scene_hash>.mp4`
(video-only, exact frame count `-frames:v (end_frame - start_frame)`, burned
scene-local ASS captions) and copied to `renders/scenes/<scene_id>.mp4`.

Usage as a standalone CLI:
    python -m render.scene projects/example_v11 --assets assets --scenes 1 --character atlas
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from narration.captions import DEFAULTS as CAPTION_DEFAULTS, build_dialogues, render_ass
from narration.errors import AudioToolError
from parser.config import load_channel_config

if False:
    from parser.prompt_sheet_select import parse_scene_selection
else:
    parse_scene_selection = None

from .character import (
    build_char_filter_branch,
    build_char_timeline,
    prepare_scene_character_concat,
)
from .kit import CharacterKit, KitError
from .timeline_v2 import find_image_file, upgrade_project_timeline


@dataclass
class SceneRenderResult:
    scenes_rendered: List[Path]
    stats: Dict[str, int]
    warnings: List[str] = field(default_factory=list)


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + f".{os.getpid()}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _ffmpeg_escape_filter_path(path: Path) -> str:
    """Escape a file path for FFmpeg's `ass=filename` filter parser."""
    s = path.resolve().as_posix()
    for ch in ("\\", ":", "'", "[", "]", ",", ";"):
        s = s.replace(ch, "\\" + ch)
    return s


def _parse_scenes_arg(spec: Optional[str], available_ids: Sequence[str]) -> Optional[Set[str]]:
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
            for n in range(int(a), int(b) + 1):
                wanted_nums.add(n)
        else:
            wanted_nums.add(int(part))
    for n in wanted_nums:
        wanted_ids.add(f"scene{n:02d}")
    unknown = sorted(wanted_ids - set(available_ids))
    if unknown:
        raise ValueError(f"Unknown scene(s): {', '.join(unknown)} (available: {', '.join(available_ids)})")
    return wanted_ids


def build_scene_ass(scene_id: str, scene_start: float, scene_end: float,
                    narration: Dict[str, Any], cfg: Dict[str, Any],
                    title: str = "Scene Captions") -> Optional[str]:
    """Build a scene-local ASS subtitle script with timestamps shifted by `-scene_start`
    and clamped to `[0, scene_end - scene_start]`. Returns None if captions are disabled
    or no segments exist in the scene."""
    ccfg = {**CAPTION_DEFAULTS, **(cfg.get("captions") or {})}
    if not ccfg.get("enabled", True):
        return None

    scene_dur = max(scene_end - scene_start, 0.01)
    shifted_segs: List[Dict[str, Any]] = []
    for seg in narration.get("segments", []):
        if seg.get("scene") != scene_id:
            continue
        s_start = max(float(seg["start"]) - scene_start, 0.0)
        s_end = min(max(float(seg["end"]) - scene_start, s_start + 0.01), scene_dur)
        words_shifted = []
        for w in seg.get("words", []):
            ws = min(max(float(w["start"]) - scene_start, 0.0), scene_dur)
            we = min(max(float(w["end"]) - scene_start, ws), scene_dur)
            words_shifted.append({"w": w["w"], "start": ws, "end": we})
        shifted_segs.append({
            "id": seg["id"],
            "text": seg.get("text", ""),
            "start": s_start,
            "end": s_end,
            "words": words_shifted,
        })

    if not shifted_segs:
        return None

    dialogues = build_dialogues(shifted_segs, ccfg)
    clamped: List[Dict[str, Any]] = []
    for d in dialogues:
        ds = min(max(float(d["start"]), 0.0), scene_dur)
        de = min(max(float(d["end"]), ds + 0.01), scene_dur)
        if de > ds:
            clamped.append({"start": ds, "end": de, "text": d["text"]})
    if not clamped:
        return None
    return render_ass(title, clamped, cfg)


def _build_background_filter(input_idx: Optional[int], resolution: Tuple[int, int],
                             fps: int, out_label: str = "bg") -> str:
    """Build the scene background filter chain (16:9 Lanczos crop + mild unsharp,
    or 9:16 Shorts center-crop + white vertical pad)."""
    vw, vh = resolution
    if input_idx is None:
        return f"color=c=white:s={vw}x{vh}:r={fps},format=yuv420p[{out_label}]"

    if vw >= vh:
        # 16:9 Long-form: Lanczos scale + center-crop + mild unsharp luma pass
        return (
            f"[{input_idx}:v]fps={fps},"
            f"scale={vw}:{vh}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={vw}:{vh},"
            f"unsharp=3:3:0.3:3:3:0.0,format=yuv420p[{out_label}]"
        )
    else:
        # 9:16 Shorts: crop center 9:11.4 area, Lanczos scale to width, pad top/bottom on white
        box_h = min(vh, int(round(vw * 1.263)) // 2 * 2)
        return (
            f"[{input_idx}:v]fps={fps},"
            f"crop='min(iw,ih*0.79)':'ih':(iw-ow)/2:0,"
            f"scale={vw}:{box_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={vw}:{box_h},"
            f"unsharp=3:3:0.3:3:3:0.0,"
            f"pad={vw}:{vh}:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p[{out_label}]"
        )


def _enable_expr(spans: Sequence[Tuple[float, float]], scene_start: float,
                 scene_dur: float) -> Optional[str]:
    """Build an FFmpeg `enable='...'` expression for scene-relative intervals.
    Returns '1' if the spans cover the entire scene, or None if empty."""
    terms: List[str] = []
    for a, b in spans:
        ra = max(float(a) - scene_start, 0.0)
        rb = min(float(b) - scene_start, scene_dur)
        if rb - ra > 1e-4:
            if ra <= 1e-3 and rb >= scene_dur - 1e-3:
                return "1"
            terms.append(f"between(t,{ra:.3f},{rb:.3f})")
    if not terms:
        return None
    return "+".join(terms)


def render_scene(scene_plan: Dict[str, Any],
                 narration: Dict[str, Any],
                 kit: CharacterKit,
                 project_dir: Path,
                 assets_dir: Path,
                 cfg: Dict[str, Any],
                 *,
                 force: bool = False) -> Tuple[Path, str, List[str]]:
    """Render a single scene to `cache/scenes/<scene_hash>.mp4` and copy to
    `renders/scenes/<scene_id>.mp4`. Returns `(dst_path, status, warnings)`."""
    sc_id = scene_plan["id"]
    sc_hash = scene_plan["scene_hash"]
    fps = int(cfg["render"]["fps"])
    res_list = cfg["render"]["resolution"]
    resolution = (int(res_list[0]), int(res_list[1]))
    vw, vh = resolution

    start_frame = int(scene_plan["start_frame"])
    end_frame = int(scene_plan["end_frame"])
    n_frames = max(end_frame - start_frame, 1)
    sc_start = float(scene_plan["start"])
    sc_end = float(scene_plan["end"])
    sc_dur = n_frames / float(fps)

    scene_cache_dir = project_dir / "cache" / "scenes"
    char_cache_dir = project_dir / "cache" / "char"
    renders_scenes_dir = project_dir / "renders" / "scenes"
    scene_cache_dir.mkdir(parents=True, exist_ok=True)
    renders_scenes_dir.mkdir(parents=True, exist_ok=True)

    cached_mp4 = scene_cache_dir / f"{sc_id}_{sc_hash}.mp4"
    dst_mp4 = renders_scenes_dir / f"{sc_id}.mp4"
    warnings: List[str] = []

    if not force and cached_mp4.exists():
        if not dst_mp4.exists() or dst_mp4.stat().st_size != cached_mp4.stat().st_size:
            _atomic_copy(cached_mp4, dst_mp4)
        return dst_mp4, "cache", warnings

    # 1. Build character frame timeline & composite PNG cache
    ctrack = scene_plan["character_track"]
    char_spans = build_char_timeline(
        start_frame,
        end_frame,
        fps,
        kit,
        ctrack.get("mouth_cues", []),
        ctrack.get("blinks", []),
        ctrack.get("pose_spans", []),
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        concat_txt = prepare_scene_character_concat(
            char_spans, kit, char_cache_dir, tmp_dir / "char_cues.txt"
        )

        inputs: List[str] = ["-f", "concat", "-safe", "0", "-i", str(concat_txt)]
        next_idx = 1

        # 2. Scene background image (if present on disk)
        bg_idx: Optional[int] = None
        img_rel = scene_plan.get("image_file") or ""
        img_path = (Path(assets_dir) / img_rel) if img_rel else None
        if img_path is None or not img_path.is_file():
            scene_sub = cfg.get("assets", {}).get("scene_images", "scenes")
            img_path = find_image_file(Path(assets_dir) / scene_sub, scene_plan.get("image", ""))
        if img_path is not None and img_path.is_file():
            bg_idx = next_idx
            inputs.extend(["-loop", "1", "-i", str(img_path)])
            next_idx += 1
        else:
            if scene_plan.get("image"):
                warnings.append(
                    f'{sc_id}: scene image "{scene_plan["image"]}" not found in {assets_dir}; '
                    f"using plain white canvas fallback."
                )

        # 3. Static reaction cutaway images (`{img:...}` overlays)
        react_overlays: List[Tuple[int, float, float]] = []
        for ov in scene_plan.get("overlays", []):
            if ov.get("type") != "img":
                continue
            ov_path = Path(assets_dir) / ov["file"]
            if not ov_path.is_file():
                react_sub = cfg.get("assets", {}).get("reaction_images", "characters")
                found = find_image_file(Path(assets_dir) / react_sub, ov.get("name", ""))
                if found is not None:
                    ov_path = found
            if ov_path.is_file():
                r_idx = next_idx
                inputs.extend(["-loop", "1", "-i", str(ov_path)])
                next_idx += 1
                react_overlays.append((r_idx, float(ov["start"]), float(ov["end"])))
            else:
                warnings.append(f'{sc_id}: reaction image "{ov.get("name")}" not found; cutaway skipped.')

        # 4. Assemble FFmpeg filter_complex graph
        chain: List[str] = []
        chain.append(_build_background_filter(bg_idx, resolution, fps, out_label="bg0"))
        cur_v = "bg0"

        # Overlay static reaction cutaways on the background
        for k, (r_idx, r_start, r_end) in enumerate(react_overlays):
            en = _enable_expr([(r_start, r_end)], sc_start, sc_dur)
            if not en:
                continue
            r_lbl = f"react{k}"
            next_v = f"bg_r{k}"
            chain.append(
                f"[{r_idx}:v]fps={fps},"
                f"scale={vw}:{vh}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={vw}:{vh},format=yuv420p[{r_lbl}]"
            )
            en_opt = "" if en == "1" else f":enable='{en}'"
            chain.append(f"[{cur_v}][{r_lbl}]overlay=0:0{en_opt}[{next_v}]")
            cur_v = next_v

        # Character branch (`bob_pip` and/or `bob_full`)
        layout_spans = ctrack.get("layout_spans") or []
        pip_intervals = [(s["start"], s["end"]) for s in layout_spans if s.get("mode") == "bob_pip"]
        full_intervals = [(s["start"], s["end"]) for s in layout_spans if s.get("mode") == "bob_full"]
        pip_en = _enable_expr(pip_intervals, sc_start, sc_dur)
        full_en = _enable_expr(full_intervals, sc_start, sc_dur)

        if pip_en or full_en:
            chain.append(build_char_filter_branch(0, kit, fps, sc_start, out_label="char_raw"))
            if pip_en and full_en:
                chain.append("[char_raw]split=2[char_pip_in][char_full_in]")
                pip_src, full_src = "char_pip_in", "char_full_in"
            elif pip_en:
                pip_src, full_src = "char_raw", None
            else:
                pip_src, full_src = None, "char_raw"

            if pip_src and pip_en:
                pip_box = next(
                    (s["pip_box"] for s in layout_spans if s.get("pip_box")),
                    [vw - 540, vh - 530, 500, 500],
                )
                px, py, pw, ph = [int(v) for v in pip_box]
                chain.append(f"[{pip_src}]scale={pw}:{ph}:flags=lanczos[char_pip]")
                en_opt = "" if pip_en == "1" else f":enable='{pip_en}'"
                chain.append(f"[{cur_v}][char_pip]overlay={px}:{py}:format=auto{en_opt}[v_pip]")
                cur_v = "v_pip"

            if full_src and full_en:
                full_h = max(2, (int(round(vh * 0.90)) // 2) * 2)
                chain.append(f"[{full_src}]scale=-2:{full_h}:flags=lanczos[char_full]")
                en_opt = "" if full_en == "1" else f":enable='{full_en}'"
                chain.append(
                    f"[{cur_v}][char_full]overlay=(W-w)/2:H-h:format=auto{en_opt}[v_full]"
                )
                cur_v = "v_full"

        # 5. Burn scene-local ASS captions (if any)
        ass_text = build_scene_ass(sc_id, sc_start, sc_end, narration, cfg, title=sc_id)
        if ass_text:
            ass_file = tmp_dir / "scene.ass"
            ass_file.write_text(ass_text, encoding="utf-8")
            esc_ass = _ffmpeg_escape_filter_path(ass_file)
            chain.append(f"[{cur_v}]ass='{esc_ass}',format=yuv420p[v_out]")
        else:
            chain.append(f"[{cur_v}]format=yuv420p[v_out]")

        tmp_mp4 = cached_mp4.with_name(cached_mp4.name + f".{os.getpid()}.tmp.mp4")
        exe = shutil.which("ffmpeg")
        if not exe:
            raise AudioToolError("ffmpeg was not found on PATH.")

        cmd = [
            exe, "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            *inputs,
            "-filter_complex", ";".join(chain),
            "-map", "[v_out]",
            "-an",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr",
            "-r", str(fps),
            "-video_track_timescale", "15360",
            "-bf", "2",
            "-g", str(fps),
            "-frames:v", str(n_frames),
            str(tmp_mp4),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not tmp_mp4.exists():
            if tmp_mp4.exists():
                tmp_mp4.unlink()
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
            raise AudioToolError(f"ffmpeg failed rendering {sc_id} (exit {proc.returncode}):\n{tail}")

        os.replace(tmp_mp4, cached_mp4)
        _atomic_copy(cached_mp4, dst_mp4)

    return dst_mp4, "rendered", warnings


def render_project_scenes(project_dir: Path,
                          assets_dir: Path,
                          cfg: Dict[str, Any],
                          *,
                          scenes_spec: Optional[str] = None,
                          character_override: Optional[str] = None,
                          force: bool = False,
                          log: Callable[[str], None] = print) -> SceneRenderResult:
    """Compile timeline v1.2 and render all (or selected) scenes in `project_dir`."""
    project_dir = Path(project_dir)
    assets_dir = Path(assets_dir)
    narr_path = project_dir / "narration.json"
    if not narr_path.exists():
        raise ValueError(f"missing {narr_path} - run `narrate` first.")

    tl_v2, kit = upgrade_project_timeline(
        project_dir, cfg, assets_dir, character_override=character_override
    )
    narration = json.loads(narr_path.read_text(encoding="utf-8"))
    all_scenes = tl_v2.get("scenes", [])
    wanted_ids = _parse_scenes_arg(scenes_spec, [s["id"] for s in all_scenes])

    selected = [s for s in all_scenes if wanted_ids is None or s["id"] in wanted_ids]
    stats = {"rendered": 0, "cache": 0}
    warnings: List[str] = []
    out_paths: List[Path] = []
    t0 = time.time()

    log(f"Rendering {len(selected)} scene(s) with character kit '{kit.id}' "
        f"({cfg['render']['resolution'][0]}x{cfg['render']['resolution'][1]} @ {cfg['render']['fps']}fps) ...")

    for i, sc_plan in enumerate(selected, 1):
        t_sc = time.time()
        dst_mp4, status, sc_warns = render_scene(
            sc_plan, narration, kit, project_dir, assets_dir, cfg, force=force
        )
        stats[status] = stats.get(status, 0) + 1
        warnings.extend(sc_warns)
        out_paths.append(dst_mp4)
        n_frames = int(sc_plan["end_frame"]) - int(sc_plan["start_frame"])
        log(f"  [{i:>2}/{len(selected)}] {sc_plan['id']}  {sc_plan['duration']:6.2f}s "
            f"({n_frames:>4}f)  hash={sc_plan['scene_hash'][:8]}  {status} ({time.time() - t_sc:.1f}s)")

    log(f"Scenes done in {time.time() - t0:.1f}s: {stats['rendered']} rendered, {stats['cache']} from cache.")
    return SceneRenderResult(out_paths, stats, warnings)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m render.scene",
                                 description="Compile timeline v1.2 and render project scene MP4s.")
    ap.add_argument("project", help="Project folder (e.g. projects/example_v11)")
    ap.add_argument("--assets", default="assets", help="Assets folder (default: assets)")
    ap.add_argument("--scenes", help="Optional scene filter, e.g. 1 or 1,3-4")
    ap.add_argument("--character", help="Override character kit id (e.g. atlas)")
    ap.add_argument("--config", help="Channel config JSON (default: config/channel_main.json)")
    ap.add_argument("--force", action="store_true", help="Ignore scene cache and re-render")
    args = ap.parse_args(argv)

    cfg = load_channel_config(args.config)
    try:
        res = render_project_scenes(
            Path(args.project),
            Path(args.assets),
            cfg,
            scenes_spec=args.scenes,
            character_override=args.character,
            force=args.force,
        )
    except (KitError, AudioToolError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    for w in res.warnings:
        print(f"WARNING: {w}")
    for p in res.scenes_rendered:
        print(f"Wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
