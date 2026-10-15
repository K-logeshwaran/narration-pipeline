"""Data model for the parsed project (schema v1.1).

Everything the parser produces is a plain dataclass so the JSON output is
deterministic: field order is fixed and nothing is a set or a dict with
unstable order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.1"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass
class Diagnostic:
    level: str                      # "error" | "warning"
    code: str                       # e.g. E_EMOTION_UNKNOWN
    message: str
    scene: Optional[str] = None     # e.g. "scene03"
    segment: Optional[str] = None   # e.g. "scene03_seg07"
    line: Optional[int] = None      # 1-based line in the source file

    def location(self) -> str:
        parts: List[str] = []
        if self.scene:
            parts.append(self.scene)
        if self.segment:
            parts.append(self.segment.split("_", 1)[-1])
        loc = " / ".join(parts)
        if self.line is not None:
            loc = f"{loc} (line {self.line})" if loc else f"line {self.line}"
        return loc or "file"

    def format(self) -> str:
        tag = "ERROR" if self.level == "error" else "WARN "
        return f"[{tag}] {self.code:<22} {self.location()}: {self.message}"


# Groups of error codes used by the human-readable validation report.
CODES_FRONT = {"E_FRONT_MISSING", "E_FRONT_UNCLOSED", "E_FRONT_KEY", "E_FRONT_VALUE"}
CODES_SCENES = {"E_SCENE_HEADER", "E_SCENE_NUMBER", "E_SCENE_FIELD", "E_SCENE_NO_NARRATION",
                "E_SCENE_EMPTY", "E_STRAY_TEXT", "E_NARRATION_DUP"}
CODES_LINES = {"E_EMOTION_MISSING", "E_EMOTION_MALFORMED", "E_PAUSE_UNKNOWN", "E_PAUSE_ADJACENT",
               "E_CUE_MALFORMED", "E_CUE_TYPE", "E_CUE_ARG", "E_BRACE_STRAY", "E_BRACKET_STRAY",
               "E_DIGIT", "E_LINE_TOO_LONG", "E_TEXT_TOO_LONG", "E_TEXT_CASE"}
CODES_EMOTION = {"E_EMOTION_UNKNOWN"}
CODES_NAMES = {"E_SFX_UNKNOWN", "E_SFX_TYPE", "E_BED_UNKNOWN", "E_BED_TYPE", "E_IMG_UNKNOWN",
               "E_CAM_UNKNOWN", "E_TEXT_STYLE", "E_LAYOUT_UNKNOWN"}
CODES_ASSETS = {"E_ASSET_MISSING", "E_KIT_MISSING", "E_KIT_JSON", "E_KIT_KEY", "E_KIT_VALUE",
                "E_KIT_MOUTH", "E_KIT_POSE", "E_KIT_DIMENSION", "E_KIT_MODE", "E_KIT_IMAGE"}


# ---------------------------------------------------------------------------
# Script model
# ---------------------------------------------------------------------------

@dataclass
class Cue:
    """An inline event such as {sfx:pop} or {text:WORK?|pop}.

    A cue binds to the next spoken word of its segment:
      word_index  index into Segment.words (== word_count when it sits at the end)
      char_offset start of that word in Segment.text (== len(text) at the end)
    """
    type: str                         # sfx | bed | text | img | cam
    name: str                         # asset name, cam move, bed name/"end", or overlay words
    word_index: int
    char_offset: int
    offset_s: float = 0.0             # sfx: the optional +0.15 / -0.10 shift
    style: Optional[str] = None       # text: slam | pop | ...
    duration_s: Optional[float] = None
    scale: Optional[float] = None
    carried: bool = False             # True if the cue sat in a word-less run and was moved forward
    raw: str = ""


@dataclass
class Segment:
    id: str
    scene_id: str
    index: int                        # 1-based order inside the scene
    line: int                         # source line number
    emotion: Optional[str]
    modifier: Optional[str]
    emotion_inherited: bool
    text: str                         # display text (captions use this)
    tts_text: str                     # text with pronunciations applied (Kokoro only)
    words: List[str]
    word_count: int
    pause_before: Optional[float] = None
    pause_before_tag: Optional[str] = None
    pause_after: Optional[float] = None
    pause_after_tag: Optional[str] = None
    gap_after_s: float = 0.0          # resolved silence between this segment and the next
    cues: List[Cue] = field(default_factory=list)


@dataclass
class Scene:
    id: str
    number: int
    title: str
    line: int
    image: str
    prompt: str
    music: str
    music_explicit: bool
    word_count: int
    segments: List[Segment] = field(default_factory=list)
    layout: str = "bob_pip"
    layout_explicit: bool = False


@dataclass
class Project:
    title: str
    channel: str
    character: str
    target_minutes: Any
    titles_alt: List[str]
    sensitivity: List[str]
    pronunciations: Dict[str, str]
    sfx_requests: List[Dict[str, str]]
    claims: List[Dict[str, str]]
    extra: Dict[str, Any]
    settings: Dict[str, Any]
    source: Dict[str, str]
    scenes: List[Scene] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {
                "title": self.title,
                "channel": self.channel,
                "character": self.character,
                "target_minutes": self.target_minutes,
                "fps": self.settings.get("fps"),
                "resolution": self.settings.get("resolution"),
            },
            "meta": {
                "titles_alt": self.titles_alt,
                "sensitivity": self.sensitivity,
                "pronunciations": self.pronunciations,
                "sfx_requests": self.sfx_requests,
                "claims": self.claims,
                "extra": self.extra,
            },
            "settings": {
                "pauses": self.settings.get("pauses"),
                "line_gap_s": self.settings.get("line_gap_s"),
                "words_per_minute": self.settings.get("words_per_minute"),
            },
            "source": self.source,
            "scenes": [asdict(s) for s in self.scenes],
        }


@dataclass
class ParseResult:
    project: Project
    diagnostics: List[Diagnostic]

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors