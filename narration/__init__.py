"""Narration stage: project.json -> segment WAVs, narration.json, timeline.json, captions, SFX mix."""
from .errors import AudioToolError, NarrationError, Problem
from .kokoro import get_backend
from .mix import mix_sfx
from .narrator import NarrationResult, load_project, narrate_project
from .timing import build_timeline

__all__ = ["narrate_project", "load_project", "get_backend", "build_timeline", "mix_sfx",
           "NarrationError", "AudioToolError", "Problem", "NarrationResult"]
