#!/usr/bin/env python3
"""Character kit loader, validator, union bounding-box calculator, and content hasher.

Usage as a CLI validator:
    python -m render.kit assets/characters/atlas
    python -m render.kit assets/characters/CBBob --config config/channel_main.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from PIL import Image

from parser.schema import Diagnostic

REQUIRED_MOUTHS = ("A", "B", "C", "D", "E", "F", "X")
OPTIONAL_MOUTHS = ("G", "H")
VALID_LAYER_SLOTS = ("base", "props", "eyes", "brows", "mouth")


class KitError(Exception):
    """Raised when a character kit is missing or fails validation."""

    def __init__(self, diagnostics: List[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("\n".join(d.format() for d in diagnostics))


@dataclass(frozen=True)
class PoseSpec:
    name: str
    base: str
    eyes_open: str
    eyes_closed: str
    brows: Optional[str] = None
    props: Optional[str] = None
    mouth_overrides: Dict[str, str] = field(default_factory=dict)
    blink: bool = True


@dataclass
class CharacterKit:
    schema_version: str
    id: str
    aliases: List[str]
    canvas: Tuple[int, int]
    anchor: Tuple[int, int]
    trim_enabled: bool
    trim_margin_px: int
    pip: Dict[str, Any]
    motion: Dict[str, Any]
    blink: Dict[str, Any]
    layer_order: List[str]
    mouth_shapes: Dict[str, str]
    mouth_fallbacks: Dict[str, str]
    poses: Dict[str, PoseSpec]
    emotion_to_pose: Dict[str, str]
    kit_dir: Path
    manifest_path: Path
    kit_sha256: str = ""
    trim_box: Tuple[int, int, int, int] = (0, 0, 0, 0)

    def resolve_pose(self, emotion: Optional[str]) -> PoseSpec:
        if emotion:
            pose_name = self.emotion_to_pose.get(emotion, emotion)
            if pose_name in self.poses:
                return self.poses[pose_name]
        return self.poses["default"]

    def resolve_mouth_rel(self, shape: str, pose_name: str = "default") -> str:
        pose = self.poses.get(pose_name) or self.poses["default"]
        if shape in pose.mouth_overrides:
            return pose.mouth_overrides[shape]
        for candidate in (shape, self.mouth_fallbacks.get(shape), "X"):
            if candidate and candidate in self.mouth_shapes:
                return self.mouth_shapes[candidate]
        raise KeyError(f'Mouth shape "{shape}" (and fallbacks) not found in kit "{self.id}"')

    def resolve_mouth_path(self, shape: str, pose_name: str = "default") -> Path:
        return self.kit_dir / self.resolve_mouth_rel(shape, pose_name)

    def layers_for(self, pose_name: str, eyes_closed: bool, mouth_shape: str) -> List[Path]:
        """Return ordered RGBA file paths to composite at (0, 0) for a given state."""
        pose = self.poses.get(pose_name) or self.poses["default"]
        slot_map: Dict[str, Optional[str]] = {
            "base": pose.base,
            "props": pose.props,
            "eyes": pose.eyes_closed if (eyes_closed and pose.blink) else pose.eyes_open,
            "brows": pose.brows,
            "mouth": self.resolve_mouth_rel(mouth_shape, pose.name),
        }
        out: List[Path] = []
        for slot in self.layer_order:
            rel = slot_map.get(slot)
            if rel:
                out.append(self.kit_dir / rel)
        return out


def _resolve_manifest_path(kit_path_or_dir: Path | str) -> Path:
    p = Path(kit_path_or_dir)
    return p / "character.json" if p.is_dir() else p


def _parse_manifest(manifest_path: Path) -> Tuple[Optional[CharacterKit], List[Diagnostic]]:
    diags: List[Diagnostic] = []
    if not manifest_path.exists():
        diags.append(Diagnostic("error", "E_KIT_MISSING", f"Character manifest not found: {manifest_path}"))
        return None, diags

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        diags.append(Diagnostic("error", "E_KIT_JSON", f"Invalid character.json in {manifest_path}: {e}"))
        return None, diags

    if not isinstance(raw, dict):
        diags.append(Diagnostic("error", "E_KIT_JSON", f"{manifest_path} root must be a JSON object"))
        return None, diags

    for req in ("id", "canvas", "layer_order", "mouths", "poses"):
        if req not in raw:
            diags.append(Diagnostic("error", "E_KIT_KEY", f'{manifest_path} is missing required key "{req}"'))
    if diags:
        return None, diags

    canvas_raw = raw["canvas"]
    if (not isinstance(canvas_raw, list) or len(canvas_raw) != 2
            or not all(isinstance(v, int) and v > 0 for v in canvas_raw)):
        diags.append(Diagnostic("error", "E_KIT_VALUE", f"{manifest_path}: 'canvas' must be [width, height] positive ints"))
        return None, diags
    canvas = (int(canvas_raw[0]), int(canvas_raw[1]))

    anchor_raw = raw.get("anchor") or [canvas[0] // 2, int(canvas[1] * 0.9)]
    anchor = (int(anchor_raw[0]), int(anchor_raw[1]))

    layer_order = list(raw.get("layer_order") or [])
    unknown_slots = [s for s in layer_order if s not in VALID_LAYER_SLOTS]
    if unknown_slots:
        diags.append(Diagnostic(
            "error", "E_KIT_VALUE",
            f"{manifest_path}: unknown slot(s) in layer_order: {', '.join(unknown_slots)}. "
            f"Valid: {', '.join(VALID_LAYER_SLOTS)}"
        ))
    for must_slot in ("base", "eyes", "mouth"):
        if must_slot not in layer_order:
            diags.append(Diagnostic("error", "E_KIT_VALUE", f'{manifest_path}: layer_order must include "{must_slot}"'))

    mouths_raw = raw.get("mouths") or {}
    shapes = dict(mouths_raw.get("shapes") or {})
    fallbacks = dict(mouths_raw.get("fallbacks") or {"G": "B", "H": "C"})

    for m in REQUIRED_MOUTHS:
        if m not in shapes or not shapes[m]:
            diags.append(Diagnostic("error", "E_KIT_MOUTH", f'{manifest_path}: missing required mouth shape "{m}"'))
    for m in OPTIONAL_MOUTHS:
        if m not in shapes and m not in fallbacks:
            diags.append(Diagnostic(
                "error", "E_KIT_MOUTH",
                f'{manifest_path}: optional mouth shape "{m}" is missing from both shapes and fallbacks'
            ))

    poses_raw = raw.get("poses") or {}
    if "default" not in poses_raw:
        diags.append(Diagnostic("error", "E_KIT_POSE", f'{manifest_path}: poses must define a "default" pose'))

    poses: Dict[str, PoseSpec] = {}
    for pname, pval in poses_raw.items():
        if not isinstance(pval, dict):
            diags.append(Diagnostic("error", "E_KIT_POSE", f'{manifest_path}: pose "{pname}" must be an object'))
            continue
        for req_field in ("base", "eyes_open", "eyes_closed"):
            if not pval.get(req_field):
                diags.append(Diagnostic(
                    "error", "E_KIT_POSE",
                    f'{manifest_path}: pose "{pname}" is missing required layer "{req_field}"'
                ))
        if diags:
            continue
        poses[pname] = PoseSpec(
            name=pname,
            base=str(pval["base"]),
            eyes_open=str(pval["eyes_open"]),
            eyes_closed=str(pval["eyes_closed"]),
            brows=str(pval["brows"]) if pval.get("brows") else None,
            props=str(pval["props"]) if pval.get("props") else None,
            mouth_overrides=dict(pval.get("mouth_overrides") or {}),
            blink=bool(pval.get("blink", True)),
        )

    if diags:
        return None, diags

    trim_cfg = raw.get("trim") or {}
    trim_enabled = bool(trim_cfg.get("enabled", True))
    trim_margin_px = int(trim_cfg.get("margin_px", 40))

    pip_cfg = {
        "scale": 0.42,
        "position": "bottom_right",
        "margin_x": 40,
        "margin_y": 30,
        **(raw.get("pip") or {}),
    }
    motion_cfg = {
        "enabled": True,
        "bounce_px": 6.0,
        "sway_deg": 1.0,
        "speed": 1.0,
        "bounce_period_s": 2.4,
        "sway_period_s": 4.1,
        **(raw.get("motion") or {}),
    }
    blink_cfg = {
        "enabled": True,
        "min_gap_s": 2.5,
        "max_gap_s": 5.0,
        "duration_s": 0.14,
        "seed": 7,
        **(raw.get("blink") or {}),
    }

    kit = CharacterKit(
        schema_version=str(raw.get("schema_version", "1.0")),
        id=str(raw["id"]),
        aliases=[str(a) for a in (raw.get("aliases") or [])],
        canvas=canvas,
        anchor=anchor,
        trim_enabled=trim_enabled,
        trim_margin_px=trim_margin_px,
        pip=pip_cfg,
        motion=motion_cfg,
        blink=blink_cfg,
        layer_order=layer_order,
        mouth_shapes=shapes,
        mouth_fallbacks=fallbacks,
        poses=poses,
        emotion_to_pose={str(k): str(v) for k, v in (raw.get("emotion_to_pose") or {}).items()},
        kit_dir=manifest_path.parent,
        manifest_path=manifest_path,
        trim_box=(0, 0, canvas[0], canvas[1]),
    )
    return kit, diags


def _all_referenced_relpaths(kit: CharacterKit) -> List[str]:
    rels: Set[str] = set(kit.mouth_shapes.values())
    for pose in kit.poses.values():
        rels.add(pose.base)
        rels.add(pose.eyes_open)
        rels.add(pose.eyes_closed)
        if pose.brows:
            rels.add(pose.brows)
        if pose.props:
            rels.add(pose.props)
        rels.update(pose.mouth_overrides.values())
    return sorted(rels)


def validate_kit(kit_path_or_dir: Path | str,
                 allowed_emotions: Optional[Iterable[str]] = None) -> Tuple[Optional[CharacterKit], List[Diagnostic]]:
    """Validate character.json and all layer PNGs (existence, dimensions, RGBA, union trim box, SHA-256)."""
    manifest_path = _resolve_manifest_path(kit_path_or_dir)
    kit, diags = _parse_manifest(manifest_path)
    if kit is None or diags:
        return None, diags

    # Check emotion mappings
    for emo, target_pose in sorted(kit.emotion_to_pose.items()):
        if target_pose not in kit.poses:
            diags.append(Diagnostic(
                "error", "E_KIT_POSE",
                f'{manifest_path}: emotion "{emo}" maps to unknown pose "{target_pose}"'
            ))
    if allowed_emotions is not None:
        for emo in sorted(set(allowed_emotions)):
            if emo not in kit.emotion_to_pose and emo not in kit.poses:
                diags.append(Diagnostic(
                    "warning", "W_KIT_EMOTION",
                    f'{manifest_path}: channel emotion "{emo}" is not in emotion_to_pose (will fall back to "default")'
                ))

    # Check every referenced PNG file
    relpaths = _all_referenced_relpaths(kit)
    hasher = hashlib.sha256()
    canonical_manifest = json.dumps(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    hasher.update(canonical_manifest)

    union_box: Optional[Tuple[int, int, int, int]] = None
    for rel in relpaths:
        fpath = kit.kit_dir / rel
        if not fpath.exists():
            diags.append(Diagnostic("error", "E_ASSET_MISSING", f'Missing kit layer "{rel}" in {kit.kit_dir}'))
            continue
        try:
            raw_bytes = fpath.read_bytes()
            hasher.update(rel.encode("utf-8"))
            hasher.update(hashlib.sha256(raw_bytes).digest())
            with Image.open(fpath) as im:
                if im.size != kit.canvas:
                    diags.append(Diagnostic(
                        "error", "E_KIT_DIMENSION",
                        f'Layer "{rel}" is {im.size[0]}x{im.size[1]}, expected canvas {kit.canvas[0]}x{kit.canvas[1]}'
                    ))
                    continue
                if "A" not in im.getbands():
                    diags.append(Diagnostic(
                        "error", "E_KIT_MODE",
                        f'Layer "{rel}" has mode "{im.mode}" (no alpha channel); all kit layers must be RGBA PNGs'
                    ))
                    continue
                rgba = im.convert("RGBA")
                bbox = rgba.getbbox()
                if bbox is not None:
                    if union_box is None:
                        union_box = bbox
                    else:
                        union_box = (
                            min(union_box[0], bbox[0]),
                            min(union_box[1], bbox[1]),
                            max(union_box[2], bbox[2]),
                            max(union_box[3], bbox[3]),
                        )
        except OSError as e:
            diags.append(Diagnostic("error", "E_KIT_IMAGE", f'Could not read image "{rel}": {e}'))

    if any(d.level == "error" for d in diags):
        return None, diags

    kit.kit_sha256 = hasher.hexdigest()[:16]
    if kit.trim_enabled and union_box is not None:
        m = kit.trim_margin_px
        x0 = max(0, union_box[0] - m)
        y0 = max(0, union_box[1] - m)
        x1 = min(kit.canvas[0], union_box[2] + m)
        y1 = min(kit.canvas[1], union_box[3] + m)
        # Ensure even width and height for H.264 / yuv420p compatibility
        w = max(2, ((x1 - x0) // 2) * 2)
        h = max(2, ((y1 - y0) // 2) * 2)
        kit.trim_box = (x0, y0, min(kit.canvas[0], x0 + w), min(kit.canvas[1], y0 + h))
    else:
        w = (kit.canvas[0] // 2) * 2
        h = (kit.canvas[1] // 2) * 2
        kit.trim_box = (0, 0, w, h)

    return kit, diags


def load_kit(kit_path_or_dir: Path | str,
             allowed_emotions: Optional[Iterable[str]] = None) -> CharacterKit:
    """Load and validate a character kit, raising KitError on any validation error."""
    kit, diags = validate_kit(kit_path_or_dir, allowed_emotions=allowed_emotions)
    errors = [d for d in diags if d.level == "error"]
    if errors or kit is None:
        raise KitError(errors or diags)
    return kit


def resolve_character_kit(character_id: str, assets_dir: Path | str,
                          cfg: Optional[Dict[str, Any]] = None) -> Path:
    """Locate the kit directory for character_id (checking aliases if cfg is given)."""
    if cfg:
        aliases = cfg.get("character_aliases") or {}
        character_id = aliases.get(character_id, aliases.get(character_id.lower(), character_id))
    sub = (cfg or {}).get("assets", {}).get("character_kits", "characters")
    return Path(assets_dir) / sub / character_id


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m render.kit", description="Validate a layered character kit.")
    ap.add_argument("kit", help="Path to kit directory or character.json")
    ap.add_argument("--config", help="Optional channel config JSON to check emotion coverage")
    args = ap.parse_args(argv)

    emotions = None
    if args.config:
        from parser.config import emotion_names, load_channel_config
        emotions = emotion_names(load_channel_config(args.config))

    kit, diags = validate_kit(args.kit, allowed_emotions=emotions)
    for d in diags:
        print(d.format())
    if kit is None:
        print(f"RESULT: FAILED ({sum(1 for d in diags if d.level == 'error')} errors)")
        return 1

    tw = kit.trim_box[2] - kit.trim_box[0]
    th = kit.trim_box[3] - kit.trim_box[1]
    print(f"Kit ID:       {kit.id} (aliases: {', '.join(kit.aliases) or 'none'})")
    print(f"Canvas:       {kit.canvas[0]}x{kit.canvas[1]} RGBA")
    print(f"Trim box:     {kit.trim_box} ({tw}x{th})")
    print(f"Poses:        {', '.join(sorted(kit.poses))}")
    print(f"Mouth shapes: {', '.join(sorted(kit.mouth_shapes))} (fallbacks: {kit.mouth_fallbacks})")
    print(f"Kit SHA-256:  {kit.kit_sha256}")
    print(f"RESULT: OK ({len(diags)} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())