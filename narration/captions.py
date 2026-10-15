"""ASS (SubStation Alpha) captions built from narration.json segments.

One Dialogue event per caption line. Lines are split at sentence punctuation
and chunked to at most captions.max_words_per_line words. With karaoke enabled
each word carries a {\k} tag (duration in centiseconds until the next word
starts), so players highlight word by word in sync with the voice.

Captions are always built from the segment's display `text` (original
spelling), never from `tts_text` - pronunciations are for the voice only.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

DEFAULTS = {
    "enabled": True, "karaoke": True, "font": "Arial", "size": 64, "bold": True,
    "max_words_per_line": 8, "tail_pad_s": 0.10,
    "primary_colour": "&H00FFFFFF", "secondary_colour": "&H0000D7FF",
    "outline_colour": "&H00000000", "back_colour": "&H64000000",
    "outline": 3, "shadow": 1, "alignment": 2, "margin_l": 40, "margin_r": 40,
    "margin_v": 60,
}

_SENT_END = re.compile(r'[.!?][\"\'\)\]]*$')


def ass_time(s: float) -> str:
    """ASS timestamp h:mm:ss.cc (centiseconds, truncated not rounded, so a
    caption never starts before the word it highlights)."""
    cs = max(int(s * 100), 0)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, c = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{c:02d}"


def _chunks(words: Sequence[str], max_words: int) -> List[Tuple[int, int]]:
    """Split word indices into caption lines: break after sentence-ending
    punctuation, and chunk long sentences to max_words words."""
    sentences: List[Tuple[int, int]] = []
    start = 0
    for i, w in enumerate(words):
        if _SENT_END.search(w):
            sentences.append((start, i + 1))
            start = i + 1
    if start < len(words):
        sentences.append((start, len(words)))
    out: List[Tuple[int, int]] = []
    for a, b in sentences:
        while b - a > max_words:
            out.append((a, a + max_words))
            a += max_words
        out.append((a, b))
    return out


def _karaoke_text(words: Sequence[str], spans: Sequence[Tuple[float, float]],
                  end: float) -> str:
    parts = []
    for i, (w, (ws, _)) in enumerate(zip(words, spans)):
        nxt = spans[i + 1][0] if i + 1 < len(spans) else end
        dur_cs = max(int(round((nxt - ws) * 100)), 1)
        parts.append(f"{{\\k{dur_cs}}}{w}")
    return " ".join(parts)


def build_dialogues(segments: Sequence[Dict[str, Any]], ccfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """segments: narration.json segment dicts (text + absolute word spans).
    Returns [{start, end, text}] in seconds, sorted by start."""
    cfg = {**DEFAULTS, **(ccfg or {})}
    max_words = max(int(cfg["max_words_per_line"]), 1)
    tail = float(cfg["tail_pad_s"])
    karaoke = bool(cfg["karaoke"])
    raw: List[Dict[str, Any]] = []
    for seg in segments:
        words = [w["w"] for w in seg.get("words", [])]
        spans = [(float(w["start"]), float(w["end"])) for w in seg.get("words", [])]
        if not words:                                   # no timing: one plain line
            if seg.get("text"):
                raw.append({"start": seg["start"], "end": seg["end"], "plain": seg["text"]})
            continue
        for a, b in _chunks(words, max_words):
            raw.append({"start": spans[a][0], "end": spans[b - 1][1] + tail,
                        "words": words[a:b], "spans": spans[a:b]})
    raw.sort(key=lambda d: d["start"])

    out: List[Dict[str, Any]] = []
    for i, d in enumerate(raw):
        # the tail pad never runs into the next caption line
        end = min(d["end"], raw[i + 1]["start"]) if i + 1 < len(raw) else d["end"]
        end = max(end, d["start"] + 0.01)
        if "plain" in d:
            text = d["plain"]
        elif karaoke:
            text = _karaoke_text(d["words"], d["spans"], end)
        else:
            text = " ".join(d["words"])
        out.append({"start": d["start"], "end": end, "text": text})
    return out


def render_ass(title: str, dialogues: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> str:
    c = {**DEFAULTS, **(cfg.get("captions") or {})}
    res = cfg.get("render", {}).get("resolution") or [1920, 1080]
    bold = -1 if c["bold"] else 0
    style = (f"Style: Caption,{c['font']},{c['size']},{c['primary_colour']},{c['secondary_colour']},"
             f"{c['outline_colour']},{c['back_colour']},{bold},0,0,0,100,100,0,0,1,"
             f"{c['outline']},{c['shadow']},{c['alignment']},{c['margin_l']},{c['margin_r']},"
             f"{c['margin_v']},1")
    lines = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        f"PlayResX: {res[0]}",
        f"PlayResY: {res[1]}",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
         "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
         "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for d in dialogues:
        lines.append(f"Dialogue: 0,{ass_time(d['start'])},{ass_time(d['end'])},Caption,,0,0,0,,{d['text']}")
    return "\n".join(lines) + "\n"


def write_ass(project: Dict[str, Any], segments: Sequence[Dict[str, Any]],
              cfg: Dict[str, Any], path: Path) -> Path:
    ccfg = {**DEFAULTS, **(cfg.get("captions") or {})}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    title = project.get("project", {}).get("title") or "Captions"
    path.write_text(render_ass(title, build_dialogues(segments, ccfg), cfg), encoding="utf-8")
    return path
