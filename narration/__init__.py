"""Narration stage: project.json -> segment WAVs, narration.json, timeline.json, captions, SFX mix, lipsync."""
from .errors import AudioToolError, NarrationError, Problem
from .kokoro import get_backend
from .lipsync import LipsyncResult, RhubarbError, find_rhubarb, lipsync_project, rhubarb_available
from .mix import mix_sfx
from .narrator import NarrationResult, load_project, narrate_project
from .timing import build_timeline
from .fake import FakeBackend

__all__ = [
    "FakeBackend",
    "narrate_project",
    "load_project",
    "get_backend",
    "build_timeline",
    "mix_sfx",
    "lipsync_project",
    "find_rhubarb",
    "rhubarb_available",
    "LipsyncResult",
    "RhubarbError",
    "NarrationError",
    "AudioToolError",
    "Problem",
    "NarrationResult",
]
