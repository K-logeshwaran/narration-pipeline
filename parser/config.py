"""Layered configuration loading: Engine -> Channel -> Playlist -> CLI overrides."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"
DEFAULT_CONFIG_PATH = CONFIG_ROOT / "channel_main.json"
ENGINES_DIR = CONFIG_ROOT / "engines"
CHANNELS_DIR = CONFIG_ROOT / "channels"
PLAYLISTS_DIR = CONFIG_ROOT / "playlists"

DEFAULT_LAYOUTS = ("full_scene", "bob_pip", "bob_full")

CHANNEL_ALIASES: Dict[str, str] = {
    "main": "cbbob",
    "cbbob": "cbbob",
    "deadpan_doug": "cbbob",
    "bob": "cbbob",
    "explainer": "explainer",
    "list_explainer": "explainer",
}


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `override` into a copy of `base`."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def canonical_channel(name: Optional[str]) -> str:
    """Resolve channel aliases (e.g. main, CBBob, deadpan_doug -> cbbob; explainer -> explainer)."""
    if not name:
        return "cbbob"
    raw = name.strip()
    if not raw:
        return "cbbob"
    return CHANNEL_ALIASES.get(raw, CHANNEL_ALIASES.get(raw.lower(), raw.lower()))


def canonical_character(name: str, cfg: Dict[str, Any]) -> str:
    """Resolve character aliases (e.g. deadpan_doug -> CBBob)."""
    raw = name.strip()
    if not raw:
        return str(cfg.get("default_character", ""))
    aliases = cfg.get("character_aliases") or {}
    return aliases.get(raw, aliases.get(raw.lower(), raw))


def _load_engine_layer(engine_name: Optional[str]) -> Dict[str, Any]:
    if not engine_name:
        return {}
    p = ENGINES_DIR / f"{engine_name}.json"
    if p.is_file():
        return _read_json(p)
    return {}


def _resolve_channel_file(channel_or_path: Optional[str]) -> Path:
    if not channel_or_path:
        return DEFAULT_CONFIG_PATH
    p = Path(channel_or_path)
    if p.is_file():
        return p
    canon = canonical_channel(channel_or_path)
    cand = CHANNELS_DIR / f"{canon}.json"
    if cand.is_file():
        return cand
    raw_cand = CHANNELS_DIR / f"{channel_or_path}.json"
    if raw_cand.is_file():
        return raw_cand
    if canon == "cbbob" and DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    raise FileNotFoundError(f"Channel config not found: {channel_or_path}")


def _resolve_playlist_file(playlist_or_path: str) -> Path:
    p = Path(playlist_or_path)
    if p.is_file():
        return p
    cand = PLAYLISTS_DIR / f"{playlist_or_path.strip()}.json"
    if cand.is_file():
        return cand
    raise FileNotFoundError(f"Playlist config not found: {playlist_or_path}")


def resolve_layered_config(
    channel: Optional[str] = None,
    *,
    config_path: Optional[str] = None,
    playlist: Optional[str] = None,
    engine: Optional[str] = None,
    voice: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge Engine -> Channel -> Playlist -> CLI overrides deterministically."""
    if config_path:
        ch_path = _resolve_channel_file(config_path)
    elif channel:
        canon = canonical_channel(channel)
        if canon == "explainer" and (CHANNELS_DIR / "explainer.json").is_file():
            ch_path = CHANNELS_DIR / "explainer.json"
        elif canon == "cbbob" and (CHANNELS_DIR / "cbbob.json").is_file() and channel.strip().lower() != "main":
            ch_path = CHANNELS_DIR / "cbbob.json"
        else:
            ch_path = DEFAULT_CONFIG_PATH
    else:
        ch_path = DEFAULT_CONFIG_PATH

    ch_cfg = _read_json(ch_path)
    engine_name = engine or ch_cfg.get("engine") or (ch_cfg.get("tts") or {}).get("backend") or "kokoro"
    eng_cfg = _load_engine_layer(engine_name)

    merged = _deep_merge(eng_cfg, ch_cfg)
    if engine:
        merged.setdefault("tts", {})["backend"] = engine
    base_voice = (merged.get("tts") or {}).get("voice")

    if playlist:
        pl_path = _resolve_playlist_file(playlist)
        pl_cfg = _read_json(pl_path)
        pl_voice = (pl_cfg.get("tts") or {}).get("voice") or pl_cfg.get("voice")
        merged = _deep_merge(merged, pl_cfg)
        if pl_voice and pl_voice != base_voice:
            merged.setdefault("tts", {})["voice"] = pl_voice
            merged["_playlist_voice_override"] = {
                "playlist": pl_cfg.get("playlist") or Path(playlist).stem,
                "from_voice": base_voice,
                "to_voice": pl_voice,
            }

    if voice:
        merged.setdefault("tts", {})["voice"] = voice

    return merged


def load_channel_config(path: Optional[str] = None,
                        playlist: Optional[str] = None,
                        engine: Optional[str] = None,
                        voice: Optional[str] = None) -> Dict[str, Any]:
    """Load channel config (merging engine/playlist layers when applicable).
    When called with no arguments, loads config/channel_main.json."""
    if path is None and playlist is None and engine is None and voice is None:
        return resolve_layered_config(config_path=str(DEFAULT_CONFIG_PATH))
    return resolve_layered_config(config_path=path, playlist=playlist, engine=engine, voice=voice)


def peek_script_front_matter(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (channel, playlist) from raw Markdown front matter without full parsing."""
    lines = text.lstrip("\ufeff").splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not idx:
        return None, None
    first = idx[0]
    if lines[first].strip().startswith("```"):
        idx = idx[1:]
        if not idx:
            return None, None
        first = idx[0]
    if lines[first].strip() != "---":
        return None, None
    channel: Optional[str] = None
    playlist: Optional[str] = None
    for i in range(first + 1, len(lines)):
        s = lines[i].strip()
        if s == "---":
            break
        m = re.match(r"^(channel|playlist)\s*:\s*(.+)$", s)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            if m.group(1) == "channel":
                channel = val
            else:
                playlist = val
    return channel, playlist


def load_config_for_script(
    script_or_project_path: str | Path,
    *,
    config_path: Optional[str] = None,
    playlist: Optional[str] = None,
    backend: Optional[str] = None,
    voice: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the layered config for a script (.md) or project.json path."""
    p = Path(script_or_project_path)
    script_channel: Optional[str] = None
    script_playlist: Optional[str] = None

    if p.is_dir():
        proj_file = p / "project.json"
        if proj_file.is_file():
            p = proj_file

    if p.is_file():
        if p.suffix.lower() == ".json":
            try:
                data = _read_json(p)
                proj = data.get("project") or {}
                meta = data.get("meta") or {}
                script_channel = proj.get("channel")
                script_playlist = proj.get("playlist") or (meta.get("extra") or {}).get("playlist")
            except (OSError, ValueError):
                pass
        else:
            try:
                script_channel, script_playlist = peek_script_front_matter(
                    p.read_text(encoding="utf-8-sig")
                )
            except OSError:
                pass

    effective_playlist = playlist or script_playlist
    return resolve_layered_config(
        channel=script_channel,
        config_path=config_path,
        playlist=effective_playlist,
        engine=backend,
        voice=voice,
    )


def layout_names(cfg: Dict[str, Any]) -> Set[str]:
    return set(cfg.get("layouts") or DEFAULT_LAYOUTS)


def emotion_names(cfg: Dict[str, Any]) -> Set[str]:
    return set(cfg["emotions"].keys())


def sfx_point(cfg: Dict[str, Any]) -> Set[str]:
    return set((cfg.get("sfx") or {}).get("point") or ())


def sfx_bed(cfg: Dict[str, Any]) -> Set[str]:
    return set((cfg.get("sfx") or {}).get("bed") or ())


def camera_classes(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Map every camera move name to its class: base | transient | instant."""
    out: Dict[str, str] = {}
    for cls, moves in (cfg.get("camera") or {}).items():
        for name in moves:
            out[name] = cls
    return out


def pause_tags(cfg: Dict[str, Any]) -> Set[str]:
    """Valid explicit pause tag names. '' (bare [pause]) maps to 'default'."""
    return set(cfg["pauses"].keys())
