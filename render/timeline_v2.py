"""Timeline v1.2 compiler: upgrades timeline.json with self-contained per-scene render plans.

Preserves the top-level `events` list from v1.1 so `cue_sheet.py`, `chapters.py`,
and `narration/mix.py` continue to work without modification.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from parser.config import camera_classes, canonical_character

from .kit import CharacterKit, load_kit, resolve_character_kit

TIMELINE_V2_VERSION = "1.2"
SCENE_HASH_VERSION = "s2"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _r(x: float) -> float:
    return round(float(x), 3)


def _hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=256)
def _file_sha256_memo(path_str: str, ino: int, size: int, mtime_ns: int) -> str:
    return hashlib.sha256(Path(path_str).read_bytes()).hexdigest()[:16]


def file_content_sha256(path: Optional[Path]) -> Optional[str]:
    """Return a 16-char SHA-256 hex digest of a file's contents (memoized in-process)."""
    if path is None or not path.is_file():
        return None
    st = path.stat()
    return _file_sha256_memo(str(path.resolve()), st.st_ino, st.st_size, st.st_mtime_ns)


def find_image_file(folder: Path, stem: str) -> Optional[Path]:
    if not folder.is_dir() or not stem:
        return None
    for ext in IMAGE_EXTS:
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def _compute_pip_box(resolution: Tuple[int, int], kit: CharacterKit) -> List[int]:
    """Compute [x, y, w, h] in pixels for the character PiP overlay on the target canvas.
    Supports positions: top_left, mid_left, bottom_left, top_right, mid_right, bottom_right, center."""
    vw, vh = resolution
    trim_w = max(2, kit.trim_box[2] - kit.trim_box[0])
    trim_h = max(2, kit.trim_box[3] - kit.trim_box[1])
    scale = float(kit.pip.get("scale", 0.42))
    target_h = max(2, (int(round(vh * scale)) // 2) * 2)
    target_w = max(2, (int(round(target_h * (trim_w / float(trim_h)))) // 2) * 2)

    mx = int(kit.pip.get("margin_x", 40))
    my = int(kit.pip.get("margin_y", 30))
    pos = str(kit.pip.get("position", "mid_left")).lower()

    if "left" in pos:
        x = mx
    elif "right" in pos:
        x = max(0, vw - target_w - mx)
    else:
        x = max(0, (vw - target_w) // 2)

    if "top" in pos:
        y = my
    elif "mid" in pos or "center" in pos:
        y = max(0, (vh - target_h) // 2)
    else:
        y = max(0, vh - target_h - my)

    return [int(x), int(y), int(target_w), int(target_h)]


def _resolve_scene_overlays_and_layouts(
    sc_start: float,
    sc_end: float,
    layout_default: str,
    sc_events: List[Dict[str, Any]],
    kit: CharacterKit,
    assets_dir: Optional[Path],
    cfg: Dict[str, Any],
    resolution: Tuple[int, int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve `{img:}` and `{text:}` cues into timed overlays and layout_spans.

    - `{img:name}`: if `name` is a rigged pose in `kit.poses` (and not 'default'),
      switches `layout_spans` to `bob_full`. Otherwise emits a background cutaway
      overlay; the speaking PiP mascot remains visible unless `hide_pip_on_cutaway`
      is explicitly set in config.
    """
    react_sub = cfg.get("assets", {}).get("reaction_images", "characters")
    hide_pip_on_cutaway = bool(cfg.get("hide_pip_on_cutaway", False))
    pip_box = _compute_pip_box(resolution, kit)

    cues = [e for e in sc_events if e.get("type") == "cue" and e.get("cue_type") in ("img", "text")]
    overlays: List[Dict[str, Any]] = []
    mode_overrides: List[Tuple[float, float, str]] = []

    for idx, ev in enumerate(cues):
        ctype = ev["cue_type"]
        start = min(max(float(ev["start"]), sc_start), sc_end)
        if ctype == "text":
            dur = float(ev.get("duration_s") or 1.2)
            end = min(start + dur, sc_end)
            if end > start:
                overlays.append({
                    "type": "text",
                    "text": str(ev["name"]),
                    "style": str(ev.get("style") or "pop"),
                    "start": _r(start),
                    "end": _r(end),
                })
        elif ctype == "img":
            name = str(ev["name"])
            if name == "scene":
                continue
            dur = float(ev.get("duration_s") or 2.5)
            end = min(start + dur, sc_end)
            for later in cues[idx + 1:]:
                if later["cue_type"] == "img":
                    later_start = float(later["start"])
                    if later_start > start:
                        end = min(end, later_start)
                    break
            if end <= start:
                continue

            if name in kit.poses and name != "default":
                mode_overrides.append((start, end, "bob_full"))
            else:
                rel_file = f"{react_sub}/{name}.jpg"
                exists: Optional[bool] = None
                file_sha: Optional[str] = None
                if assets_dir is not None:
                    found = find_image_file(Path(assets_dir) / react_sub, name)
                    if found is not None:
                        rel_file = found.relative_to(Path(assets_dir)).as_posix()
                        exists = True
                        file_sha = file_content_sha256(found)
                    else:
                        exists = False
                ov: Dict[str, Any] = {
                    "type": "img",
                    "name": name,
                    "file": rel_file,
                    "start": _r(start),
                    "end": _r(end),
                }
                if exists is not None:
                    ov["exists"] = exists
                if file_sha is not None:
                    ov["sha256"] = file_sha
                overlays.append(ov)
                if hide_pip_on_cutaway and exists is not False:
                    mode_overrides.append((start, end, "full_scene"))

    edges = {sc_start, sc_end}
    for a, b, _ in mode_overrides:
        edges.add(min(max(a, sc_start), sc_end))
        edges.add(min(max(b, sc_start), sc_end))
    sorted_edges = sorted(edges)

    layout_spans: List[Dict[str, Any]] = []
    for i in range(len(sorted_edges) - 1):
        a, b = sorted_edges[i], sorted_edges[i + 1]
        if b - a <= 1e-4:
            continue
        mid = 0.5 * (a + b)
        mode = layout_default
        for oa, ob, omode in mode_overrides:
            if oa <= mid < ob:
                mode = omode
        if layout_spans and layout_spans[-1]["mode"] == mode:
            layout_spans[-1]["end"] = _r(b)
        else:
            span_dict: Dict[str, Any] = {"start": _r(a), "end": _r(b), "mode": mode}
            if mode == "bob_pip":
                span_dict["pip_box"] = pip_box
            layout_spans.append(span_dict)

    return overlays, layout_spans


def _resolve_pose_spans(sc_start: float, sc_end: float,
                        sc_narr_events: List[Dict[str, Any]],
                        kit: CharacterKit) -> List[Dict[str, Any]]:
    """Build contiguous pose spans across [sc_start, sc_end] from narration segment emotions."""
    if not sc_narr_events:
        return [{
            "start": _r(sc_start),
            "end": _r(sc_end),
            "pose": "default",
            "emotion": "deadpan",
            "modifier": None,
        }]

    ordered = sorted(sc_narr_events, key=lambda e: float(e["start"]))
    spans: List[Dict[str, Any]] = []
    for idx, ev in enumerate(ordered):
        s = sc_start if idx == 0 else max(float(ev["start"]), sc_start)
        e = float(ordered[idx + 1]["start"]) if idx + 1 < len(ordered) else sc_end
        e = min(max(e, s), sc_end)
        if e <= s:
            continue
        emo = ev.get("emotion") or "deadpan"
        mod = ev.get("modifier")
        pose_name = kit.resolve_pose(emo).name
        if (spans
                and spans[-1]["pose"] == pose_name
                and spans[-1]["emotion"] == emo
                and spans[-1]["modifier"] == mod):
            spans[-1]["end"] = _r(e)
        else:
            spans.append({
                "start": _r(s),
                "end": _r(e),
                "pose": pose_name,
                "emotion": emo,
                "modifier": mod,
            })
    if spans:
        spans[0]["start"] = _r(sc_start)
        spans[-1]["end"] = _r(sc_end)
    return spans


def _resolve_camera_track(sc_start: float, sc_end: float,
                          sc_events: List[Dict[str, Any]],
                          cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolve `{cam:}` cues into base moves and transient punches/shakes."""
    cam_cfg = cfg.get("camera") or {}
    base_defs = cam_cfg.get("base") or {}
    trans_defs = cam_cfg.get("transient") or {}
    classes = camera_classes(cfg)

    cam_cues = [e for e in sc_events if e.get("type") == "cue" and e.get("cue_type") == "cam"]
    base_moves: List[Dict[str, Any]] = []
    transients: List[Dict[str, Any]] = []

    for ev in cam_cues:
        name = str(ev["name"])
        cls = classes.get(name, "base")
        t = min(max(float(ev["start"]), sc_start), sc_end)
        if cls == "base":
            dflt = base_defs.get(name) or {}
            dur = float(ev.get("duration_s") or dflt.get("duration_s") or 6.0)
            scale = float(ev.get("scale") or dflt.get("scale") or 1.0)
            if base_moves and base_moves[-1]["end"] > t:
                base_moves[-1]["end"] = _r(t)
            base_moves.append({
                "start": _r(t),
                "end": _r(min(t + dur, sc_end)),
                "move": name,
                "scale": scale,
            })
        elif cls == "transient":
            dflt = trans_defs.get(name) or {}
            dur = float(ev.get("duration_s") or dflt.get("duration_s") or 0.35)
            item: Dict[str, Any] = {
                "start": _r(t),
                "end": _r(min(t + dur, sc_end)),
                "move": name,
            }
            if ev.get("scale") or dflt.get("scale"):
                item["scale"] = float(ev.get("scale") or dflt.get("scale"))
            if dflt.get("amplitude_px"):
                item["amplitude_px"] = int(dflt["amplitude_px"])
            transients.append(item)
        elif cls == "instant" and name == "cut":
            if base_moves and base_moves[-1]["end"] > t:
                base_moves[-1]["end"] = _r(t)

    if not base_moves:
        base_moves.append({
            "start": _r(sc_start),
            "end": _r(sc_end),
            "move": "hold",
            "scale": 1.0,
        })
    return [{"base_moves": base_moves, "transients": transients}]


def build_timeline_v2(project: Dict[str, Any],
                      timeline_v1: Dict[str, Any],
                      lipsync: Dict[str, Any],
                      cfg: Dict[str, Any],
                      kit: CharacterKit,
                      assets_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Compile a v1.2 timeline dictionary with frame-snapped scenes and scene_hash."""
    fps = int(project.get("project", {}).get("fps") or cfg["render"]["fps"] or 30)
    res_list = project.get("project", {}).get("resolution") or cfg["render"]["resolution"] or [1920, 1080]
    resolution = (int(res_list[0]), int(res_list[1]))
    scene_sub = cfg.get("assets", {}).get("scene_images", "scenes")
    default_layout = str(cfg.get("default_layout", "bob_pip"))

    proj_scenes_by_id = {sc["id"]: sc for sc in project.get("scenes", [])}
    ls_scenes_by_id = {sc["id"]: sc for sc in lipsync.get("scenes", [])}
    all_events = timeline_v1.get("events", [])

    v1_scenes = timeline_v1.get("scenes", [])
    scenes_v2: List[Dict[str, Any]] = []

    for idx, sc_v1 in enumerate(v1_scenes):
        sc_id = sc_v1["id"]
        proj_sc = proj_scenes_by_id.get(sc_id, {})
        ls_sc = ls_scenes_by_id.get(sc_id, {})

        raw_end = float(sc_v1["end"])
        start_frame = 0 if idx == 0 else scenes_v2[-1]["end_frame"]
        end_frame = max(start_frame + 1, int(round(raw_end * fps)))
        sc_start = round(start_frame / float(fps), 6)
        sc_end = round(end_frame / float(fps), 6)
        sc_dur = round((end_frame - start_frame) / float(fps), 6)

        image_stem = str(sc_v1.get("image") or proj_sc.get("image") or "")
        image_rel = f"{scene_sub}/{image_stem}.png" if image_stem else ""
        image_exists: Optional[bool] = None
        image_sha: Optional[str] = None
        if assets_dir is not None and image_stem:
            found_img = find_image_file(Path(assets_dir) / scene_sub, image_stem)
            if found_img is not None:
                image_rel = found_img.relative_to(Path(assets_dir)).as_posix()
                image_exists = True
                image_sha = file_content_sha256(found_img)
            else:
                image_exists = False

        layout_default_sc = str(proj_sc.get("layout") or default_layout)
        sc_events = [
            e for e in all_events
            if (e.get("scene") == sc_id)
            or (e.get("segment", "").startswith(sc_id + "_"))
        ]
        sc_narr_events = [e for e in sc_events if e.get("type") == "narration"]

        overlays, layout_spans = _resolve_scene_overlays_and_layouts(
            sc_start, sc_end, layout_default_sc, sc_events, kit, assets_dir, cfg, resolution
        )
        pose_spans = _resolve_pose_spans(sc_start, sc_end, sc_narr_events, kit)
        camera_track = _resolve_camera_track(sc_start, sc_end, sc_events, cfg)

        mouth_cues = [
            {"start": c["start"], "end": c["end"], "shape": c.get("shape") or c.get("value", "X")}
            for c in (ls_sc.get("mouth_cues") or [])
        ]
        if not mouth_cues:
            mouth_cues = [{"start": _r(sc_start), "end": _r(sc_end), "shape": "X"}]
        blinks = list(ls_sc.get("blinks") or [])

        character_track = {
            "character_id": kit.id,
            "layout_spans": layout_spans,
            "pose_spans": pose_spans,
            "blinks": blinks,
            "mouth_cues": mouth_cues,
        }

        sc_seg_texts = [
            (s.get("id"), s.get("text"), s.get("words"))
            for s in proj_sc.get("segments", [])
        ]

        scene_plan_for_hash = {
            "v": SCENE_HASH_VERSION,
            "id": sc_id,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "fps": fps,
            "resolution": list(resolution),
            "image_rel": image_rel,
            "image_sha": image_sha,
            "kit_sha256": kit.kit_sha256,
            "layout_default": layout_default_sc,
            "camera_track": camera_track,
            "character_track": character_track,
            "overlays": overlays,
            "captions_cfg": cfg.get("captions") or {},
            "segments": sc_seg_texts,
        }
        scene_hash = _hash(scene_plan_for_hash)

        music_val = sc_v1.get("music") or proj_sc.get("music") or "none"
        sc_row: Dict[str, Any] = {
            "id": sc_id,
            "start": _r(sc_start),
            "end": _r(sc_end),
            "start_frame": start_frame,
            "end_frame": end_frame,
            "duration": _r(sc_dur),
            "scene_hash": scene_hash,
            "image": image_stem,
            "image_file": image_rel,
            "layout_default": layout_default_sc,
            "music": music_val,
            "camera_track": camera_track,
            "character_track": character_track,
            "overlays": overlays,
        }
        if image_exists is not None:
            sc_row["image_exists"] = image_exists
        if image_sha is not None:
            sc_row["image_sha256"] = image_sha
        scenes_v2.append(sc_row)

    total_frames = scenes_v2[-1]["end_frame"] if scenes_v2 else 0
    total_dur = _r(total_frames / float(fps)) if scenes_v2 else timeline_v1.get("duration", 0.0)

    mcfg = cfg.get("mix") or {}
    out = {
        "schema_version": TIMELINE_V2_VERSION,
        "source_sha256": timeline_v1.get("source_sha256"),
        "fps": fps,
        "resolution": list(resolution),
        "duration": total_dur,
        "total_frames": total_frames,
        "audio_mix": f"audio/{mcfg.get('out_name', 'full_mix.wav')}",
        "captions_ass": "captions/subtitles.ass",
        "character": {
            "id": kit.id,
            "kit_path": kit.manifest_path.as_posix(),
            "kit_sha256": kit.kit_sha256,
        },
        "scenes": scenes_v2,
        "events": all_events,
    }
    return out


def upgrade_project_timeline(project_dir: Path, cfg: Dict[str, Any],
                             assets_dir: Path,
                             character_override: Optional[str] = None) -> Tuple[Dict[str, Any], CharacterKit]:
    """Load project.json, timeline.json, lipsync.json, and the CharacterKit,
    compile timeline v1.2, write it atomically to timeline.json, and return (timeline_v2, kit)."""
    project_dir = Path(project_dir)
    proj_path = project_dir / "project.json"
    tl_path = project_dir / "timeline.json"
    ls_path = project_dir / "lipsync.json"
    missing = [str(p) for p in (proj_path, tl_path, ls_path) if not p.exists()]
    if missing:
        raise ValueError("missing " + ", ".join(missing) + " - run `narrate` and `lipsync` first.")

    project = json.loads(proj_path.read_text(encoding="utf-8"))
    timeline_v1 = json.loads(tl_path.read_text(encoding="utf-8"))
    lipsync = json.loads(ls_path.read_text(encoding="utf-8"))

    char_id = canonical_character(character_override or project["project"]["character"], cfg)
    kit_dir = resolve_character_kit(char_id, assets_dir, cfg)
    kit = load_kit(kit_dir, allowed_emotions=cfg["emotions"].keys())

    tl_v2 = build_timeline_v2(project, timeline_v1, lipsync, cfg, kit, assets_dir=Path(assets_dir))
    tmp = tl_path.with_name(tl_path.name + ".tmp")
    tmp.write_text(json.dumps(tl_v2, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, tl_path)
    return tl_v2, kit