"""Layered character renderer derived from atlas_talk.py (v3).

Fixes over standalone atlas_talk.py:
  1. Frame-grid quantization: all mouth cues, blink windows, and pose spans are
     quantized to integer frame indices [f_a, f_b) before interval splitting,
     guaranteeing every span is >= 1 frame and durations sum to the exact scene
     frame count with zero cumulative drift.
  2. Persistent content-addressed composite cache: unique (pose, eyes_closed,
     mouth_shape) combinations are composited once via Pillow and saved in
     `projects/<name>/cache/char/<comp_key>.png`.
  3. Union trim box: uses `kit.trim_box` (computed across all kit layers) so
     eyebrows, wide mouths, or props are never clipped.
  4. Phase-continuous motion & premultiplied RGBA rotation: bounce/sway sine
     waves offset `t` by `scene_start` so motion never jumps at scene cuts, and
     RGBA rotation premultiplies alpha to avoid dark edge fringes.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from .kit import CharacterKit

CHAR_CACHE_VERSION = "c1"


@dataclass(frozen=True)
class CharFrameSpan:
    start_frame: int          # scene-relative frame index (0 = scene start)
    end_frame: int            # scene-relative frame index (> start_frame)
    start_s: float            # scene-relative start seconds
    end_s: float              # scene-relative end seconds
    duration_s: float         # (end_frame - start_frame) / fps
    pose: str                 # resolved pose name in kit.poses
    mouth: str                # resolved mouth shape key (A..H, X)
    eyes_closed: bool


def _hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=64)
def _load_rgba_cached(path_str: str, sha_tag: str) -> Image.Image:
    with Image.open(path_str) as im:
        return im.convert("RGBA").copy()


def ensure_composite_png(kit: CharacterKit, pose_name: str, eyes_closed: bool,
                         mouth_shape: str, cache_dir: Path) -> Path:
    """Composite the RGBA layers for (pose_name, eyes_closed, mouth_shape), crop
    to kit.trim_box, and cache atomically in cache_dir."""
    pose = kit.poses.get(pose_name) or kit.poses["default"]
    effective_closed = bool(eyes_closed and pose.blink)
    layer_paths = kit.layers_for(pose.name, effective_closed, mouth_shape)
    rel_layers = [p.relative_to(kit.kit_dir).as_posix() for p in layer_paths]

    comp_key = _hash({
        "v": CHAR_CACHE_VERSION,
        "kit_sha256": kit.kit_sha256,
        "layers": rel_layers,
        "trim_box": list(kit.trim_box),
    })
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"{kit.id}_{pose.name}_{'c' if effective_closed else 'o'}_{mouth_shape}_{comp_key}.png"
    if out_path.exists():
        return out_path

    canvas_im = Image.new("RGBA", kit.canvas, (0, 0, 0, 0))
    for lp in layer_paths:
        layer_im = _load_rgba_cached(str(lp.resolve()), kit.kit_sha256)
        canvas_im = Image.alpha_composite(canvas_im, layer_im)

    x0, y0, x1, y1 = kit.trim_box
    if (x0, y0, x1, y1) != (0, 0, kit.canvas[0], kit.canvas[1]):
        canvas_im = canvas_im.crop((x0, y0, x1, y1))

    tmp = out_path.with_name(out_path.name + f".{os.getpid()}.tmp")
    canvas_im.save(tmp, format="PNG")
    os.replace(tmp, out_path)
    return out_path


def _CanonicalMouth(kit: CharacterKit, raw_shape: str, pose_name: str) -> str:
    """Return the canonical shape key that resolves to an existing mouth file."""
    pose = kit.poses.get(pose_name) or kit.poses["default"]
    s = raw_shape.strip().upper() if raw_shape else "X"
    if s in pose.mouth_overrides:
        return s
    for cand in (s, kit.mouth_fallbacks.get(s), "X"):
        if cand and cand in kit.mouth_shapes:
            return cand
    return "X"


def build_char_timeline(scene_start_frame: int, scene_end_frame: int, fps: int,
                        kit: CharacterKit,
                        mouth_cues: Sequence[Dict[str, Any]],
                        blinks: Sequence[Sequence[float]],
                        pose_spans: Sequence[Dict[str, Any]]) -> List[CharFrameSpan]:
    """Quantize all character state changes to integer frames in [0, total_frames]
    and merge adjacent identical states.

    All input timestamps in `mouth_cues`, `blinks`, and `pose_spans` are in
    absolute timeline seconds; returned `CharFrameSpan` frames/times are
    scene-relative (0 = scene_start_frame).
    """
    fps = max(int(fps), 1)
    total_frames = max(int(scene_end_frame) - int(scene_start_frame), 1)

    def to_rel_frame(abs_t: float) -> int:
        abs_f = int(round(float(abs_t) * fps))
        return min(max(abs_f - scene_start_frame, 0), total_frames)

    # 1. Quantize mouth cues onto the frame grid
    q_mouths: List[Tuple[int, str]] = [(0, "X")]
    for c in mouth_cues:
        f = to_rel_frame(float(c.get("start", 0.0)))
        shape = str(c.get("shape") or c.get("value") or "X").strip().upper()
        if q_mouths and q_mouths[-1][0] == f:
            q_mouths[-1] = (f, shape)
        elif f < total_frames:
            q_mouths.append((f, shape))

    # 2. Quantize blink intervals onto the frame grid (guaranteeing >= 1 frame per blink)
    q_blinks: List[Tuple[int, int]] = []
    for b in blinks:
        if len(b) < 2:
            continue
        bf0 = to_rel_frame(float(b[0]))
        bf1 = to_rel_frame(float(b[1]))
        if bf0 < total_frames:
            bf1 = min(max(bf1, bf0 + 1), total_frames)
            if bf1 > bf0:
                q_blinks.append((bf0, bf1))

    # 3. Quantize pose spans onto the frame grid
    q_poses: List[Tuple[int, str]] = [(0, "default")]
    for ps in pose_spans:
        f = to_rel_frame(float(ps.get("start", 0.0)))
        raw_pose = str(ps.get("pose") or ps.get("emotion") or "default")
        resolved_pose = kit.resolve_pose(raw_pose).name
        if q_poses and q_poses[-1][0] == f:
            q_poses[-1] = (f, resolved_pose)
        elif f < total_frames:
            q_poses.append((f, resolved_pose))

    # 4. Collect all frame transition edges
    edges_set = {0, total_frames}
    edges_set.update(f for f, _ in q_mouths if 0 <= f <= total_frames)
    edges_set.update(f for f, _ in q_poses if 0 <= f <= total_frames)
    for bf0, bf1 in q_blinks:
        edges_set.add(bf0)
        edges_set.add(bf1)
    edges = sorted(edges_set)

    def mouth_at(f: int) -> str:
        val = q_mouths[0][1]
        for mf, mval in q_mouths:
            if mf <= f:
                val = mval
            else:
                break
        return val

    def pose_at(f: int) -> str:
        val = q_poses[0][1]
        for pf, pval in q_poses:
            if pf <= f:
                val = pval
            else:
                break
        return val

    def blink_at(f: int) -> bool:
        return any(bf0 <= f < bf1 for bf0, bf1 in q_blinks)

    raw_spans: List[List[Any]] = []
    for i in range(len(edges) - 1):
        fa, fb = edges[i], edges[i + 1]
        if fb <= fa:
            continue
        pose_name = pose_at(fa)
        pose_spec = kit.poses.get(pose_name) or kit.poses["default"]
        closed = bool(blink_at(fa) and pose_spec.blink)
        mouth_shape = _CanonicalMouth(kit, mouth_at(fa), pose_name)
        if (raw_spans
                and raw_spans[-1][2] == pose_name
                and raw_spans[-1][3] == mouth_shape
                and raw_spans[-1][4] == closed):
            raw_spans[-1][1] = fb
        else:
            raw_spans.append([fa, fb, pose_name, mouth_shape, closed])

    out: List[CharFrameSpan] = []
    for fa, fb, pose_name, mouth_shape, closed in raw_spans:
        out.append(CharFrameSpan(
            start_frame=fa,
            end_frame=fb,
            start_s=round(fa / fps, 6),
            end_s=round(fb / fps, 6),
            duration_s=(fb - fa) / float(fps),
            pose=pose_name,
            mouth=mouth_shape,
            eyes_closed=closed,
        ))
    return out


def prepare_scene_character_concat(spans: Sequence[CharFrameSpan], kit: CharacterKit,
                                   char_cache_dir: Path, concat_file_path: Path) -> Path:
    """Ensure all composite PNGs exist for `spans` and write the FFmpeg concat
    demuxer script to `concat_file_path`."""
    lines: List[str] = []
    for sp in spans:
        png_path = ensure_composite_png(kit, sp.pose, sp.eyes_closed, sp.mouth, char_cache_dir)
        lines.append(f"file '{png_path.resolve().as_posix()}'")
        lines.append(f"duration {sp.duration_s:.6f}")
    if lines:
        lines.append(lines[-2])  # FFmpeg concat demuxer last-frame quirk
    concat_file_path.parent.mkdir(parents=True, exist_ok=True)
    concat_file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_file_path


def build_char_filter_branch(input_idx: int, kit: CharacterKit, fps: int,
                             scene_start_s: float, out_label: str = "char") -> str:
    """Build the FFmpeg filter_complex branch for the character stream.

    Uses phase-continuous time `(t + scene_start_s)` so bounce and sway never
    jump at scene boundaries, and premultiplies RGBA before rotation to avoid
    dark bilinear interpolation fringes.
    """
    out_w = max(2, kit.trim_box[2] - kit.trim_box[0])
    mcfg = kit.motion or {}
    motion_on = bool(mcfg.get("enabled", True))
    bounce_px = float(mcfg.get("bounce_px", 6.0)) * out_w / float(kit.canvas[0])
    sway_deg = float(mcfg.get("sway_deg", 1.0))
    speed = max(float(mcfg.get("speed", 1.0)), 0.05)
    bp = float(mcfg.get("bounce_period_s", 2.4)) / speed
    sp = float(mcfg.get("sway_period_s", 4.1)) / speed

    filters = [f"fps={fps}", "format=rgba"]
    if motion_on and (bounce_px > 0.1 or abs(sway_deg) > 0.01):
        A = max(1, int(round(bounce_px)))
        rad = sway_deg * math.pi / 180.0
        t_expr = f"(t+{scene_start_s:.4f})"
        filters.extend([
            f"pad=iw:ih+{2 * A}:0:{A}:color=black@0",
            f"crop=iw:ih-{2 * A}:0:'{A}+{bounce_px:.3f}*sin(2*PI*{t_expr}/{bp:.4f})'",
            "premultiply=inplace=1",
            f"rotate=a='{rad:.5f}*sin(2*PI*{t_expr}/{sp:.4f})':ow=iw:oh=ih:fillcolor=none",
            "unpremultiply=inplace=1",
        ])
    return f"[{input_idx}:v]{','.join(filters)}[{out_label}]"