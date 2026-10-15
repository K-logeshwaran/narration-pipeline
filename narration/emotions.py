"""Emotion profiles: script emotion -> Kokoro / audio parameters.

    script emotion -> emotion profile -> speed, pitch, volume

Profiles live in the channel config, so they can be tuned by ear without code changes.
A compound tag like (deadpan, flat) blends the modifier into the primary.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

MIN_SPEED, MAX_SPEED = 0.5, 2.0


class UnknownEmotion(KeyError):
    def __init__(self, name: str, available: List[str]):
        self.name, self.available = name, available
        super().__init__(name)


@dataclass(frozen=True)
class Profile:
    speed: float
    pitch: float      # semitones, only applied if tts.apply_pitch is true
    volume: float     # linear gain


def available(cfg: Dict[str, Any]) -> List[str]:
    return sorted(cfg["emotions"])


def _raw(name: str, cfg: Dict[str, Any]) -> Profile:
    prof = cfg["emotions"].get(name)
    if prof is None:
        raise UnknownEmotion(name, available(cfg))
    return Profile(
        speed=float(prof.get("speed", cfg["tts"]["base_speed"])),
        pitch=float(prof.get("pitch", 0.0)),
        volume=float(prof.get("volume", 1.0)),
    )


def resolve_profile(emotion: Optional[str], modifier: Optional[str], cfg: Dict[str, Any]) -> Profile:
    if not emotion:
        return Profile(float(cfg["tts"]["base_speed"]), 0.0, 1.0)
    primary = _raw(emotion, cfg)
    if not modifier:
        return primary
    mod = _raw(modifier, cfg)
    w = float(cfg["tts"].get("modifier_weight", 0.3))
    mix = lambda a, b: a * (1.0 - w) + b * w
    return Profile(mix(primary.speed, mod.speed), mix(primary.pitch, mod.pitch), mix(primary.volume, mod.volume))


def jitter_factor(seg_id: str, pct: float) -> float:
    """Deterministic per-segment speed wobble in [1 - pct%, 1 + pct%]. Same id,
    same factor, so rebuilds are reproducible and caching still works."""
    if not pct:
        return 1.0
    h = int(hashlib.sha1(seg_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF   # 0..1
    return 1.0 + (h * 2.0 - 1.0) * pct / 100.0


def segment_speed(profile: Profile, seg_id: str, cfg: Dict[str, Any]) -> float:
    tts = cfg["tts"]
    s = profile.speed * float(tts.get("speed_scale", 1.0)) * jitter_factor(seg_id, float(tts.get("speed_jitter_pct", 0.0)))
    return round(min(max(s, MIN_SPEED), MAX_SPEED), 4)
