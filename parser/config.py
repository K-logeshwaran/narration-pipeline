"""Channel configuration loading. All vocabulary and thresholds live in JSON."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "channel_main.json"
DEFAULT_LAYOUTS = ("full_scene", "bob_pip", "bob_full")


def load_channel_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def canonical_character(name: str, cfg: Dict[str, Any]) -> str:
    """Resolve character aliases (e.g. deadpan_doug -> CBBob)."""
    raw = name.strip()
    if not raw:
        return ""
    aliases = cfg.get("character_aliases") or {}
    return aliases.get(raw, aliases.get(raw.lower(), raw))


def layout_names(cfg: Dict[str, Any]) -> Set[str]:
    return set(cfg.get("layouts") or DEFAULT_LAYOUTS)


def emotion_names(cfg: Dict[str, Any]) -> Set[str]:
    return set(cfg["emotions"].keys())


def sfx_point(cfg: Dict[str, Any]) -> Set[str]:
    return set(cfg["sfx"]["point"])


def sfx_bed(cfg: Dict[str, Any]) -> Set[str]:
    return set(cfg["sfx"]["bed"])


def camera_classes(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Map every camera move name to its class: base | transient | instant."""
    out: Dict[str, str] = {}
    for cls, moves in cfg["camera"].items():
        for name in moves:
            out[name] = cls
    return out


def pause_tags(cfg: Dict[str, Any]) -> Set[str]:
    """Valid explicit pause tag names. '' (bare [pause]) maps to 'default'."""
    return set(cfg["pauses"].keys())