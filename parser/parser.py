"""Markdown script parser (script spec v1.1, PART G).

Turns a script file into a Project (see schema.py) plus a list of diagnostics.
This module handles STRUCTURE and SYNTAX only. Vocabulary checks (is this an
allowed emotion / sfx / camera move?) and lint rules live in validators.py.

Design rules:
  * Never stop at the first problem. Collect as many diagnostics as possible.
  * Deterministic: same input always gives byte-identical JSON.
  * Every diagnostic carries scene, segment and source line where known.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import canonical_character, load_channel_config, pause_tags
from .schema import Cue, Diagnostic, ParseResult, Project, Scene, Segment

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

SCENE_RE = re.compile(r"^##\s+SCENE\s+(\d+)\s*:\s*(\S.*)$")
HEADING2_RE = re.compile(r"^##\s")
HEADING_ANY_RE = re.compile(r"^#{1,6}\s")
NARRATION_RE = re.compile(r"^###\s+narration\s*$", re.I)
FIELD_RE = re.compile(r"^(image|prompt|music|layout)\s*:\s*(.*)$")
EMOTION_RE = re.compile(r"^\(([^()]*)\)\s*(.*)$")
PAUSE_RE = re.compile(r"\[\s*pause\s*(?::\s*([^\]]*?)\s*)?\]", re.I)
CUE_RE = re.compile(r"\{([^{}]*)\}")
KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):(?:\s+(.*)|\s*)$")
ITEM_RE = re.compile(r"^\s*-\s+(.*)$")
WORD_RE = re.compile(r"\S+")
SFX_ARG_RE = re.compile(r"^([A-Za-z0-9_]+)([+-]\d+(?:\.\d+)?)?$")
IMAGE_STEM_RE = re.compile(r"^s(\d{2})_[a-z0-9_]+$")

CUE_TYPES = ("sfx", "bed", "text", "img", "cam")
LIST_KEYS = ("titles_alt", "sensitivity", "pronunciations", "sfx_requests", "claims")
REQUIRED_KEYS = ("title", "channel", "character", "target_minutes")
ALLOWED_PUNCT = set(".,!?;:'\"-\u2014\u2013\u2026\u2018\u2019\u201c\u201d")


# ---------------------------------------------------------------------------
# Diagnostics helper
# ---------------------------------------------------------------------------

class _Ctx:
    """Collects diagnostics and remembers which scene we are in."""

    def __init__(self, diags: List[Diagnostic]):
        self.diags = diags
        self.scene: Optional[str] = None

    def add(self, level: str, code: str, message: str,
            line: Optional[int] = None, segment: Optional[str] = None) -> None:
        self.diags.append(Diagnostic(level, code, message, self.scene, segment, line))

    def error(self, code: str, message: str, line: Optional[int] = None,
              segment: Optional[str] = None) -> None:
        self.add("error", code, message, line, segment)

    def warn(self, code: str, message: str, line: Optional[int] = None,
             segment: Optional[str] = None) -> None:
        self.add("warning", code, message, line, segment)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(path: str, cfg: Optional[Dict[str, Any]] = None) -> ParseResult:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")      # tolerate a BOM from Windows editors
    return parse_text(text, source_name=p.name, cfg=cfg)


def parse_text(text: str, source_name: str = "<string>",
               cfg: Optional[Dict[str, Any]] = None) -> ParseResult:
    cfg = cfg or load_channel_config()
    diags: List[Diagnostic] = []
    ctx = _Ctx(diags)

    text = text.lstrip("\ufeff")
    lines = _strip_code_fence(text.splitlines(), ctx)
    meta, scene_start = _parse_front_matter(lines, ctx, cfg)
    raw_scenes = _collect_scenes(lines, scene_start, ctx)
    scenes = _build_scenes(raw_scenes, meta["pronunciations"], cfg, ctx)

    project = Project(
        title=meta["title"], channel=meta["channel"], character=meta["character"],
        target_minutes=meta["target_minutes"], titles_alt=meta["titles_alt"],
        sensitivity=meta["sensitivity"], pronunciations=meta["pronunciations"],
        sfx_requests=meta["sfx_requests"], claims=meta["claims"], extra=meta["extra"],
        settings={
            "fps": cfg["render"]["fps"],
            "resolution": cfg["render"]["resolution"],
            "pauses": cfg["pauses"],
            "line_gap_s": cfg["line_gap_s"],
            "words_per_minute": cfg["words_per_minute"],
        },
        source={"name": source_name,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
        scenes=scenes,
    )
    return ParseResult(project, diags)


# ---------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------

def _strip_code_fence(lines: List[str], ctx: _Ctx) -> List[str]:
    """LLMs wrap output in a ```markdown fence. Blank the fence lines (keeping
    line numbers stable) so pasted output still parses."""
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not idx:
        return lines
    out = list(lines)
    first, last = idx[0], idx[-1]
    if out[first].strip().startswith("```"):
        out[first] = ""
        ctx.warn("W_CODE_FENCE", "Removed a surrounding ``` code fence", line=first + 1)
        if last != first and out[last].strip() == "```":
            out[last] = ""
    return out


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"')
    return v


def _is_quoted(value: str) -> bool:
    v = value.strip()
    return len(v) >= 2 and v[0] == '"' and v[-1] == '"'


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

def _parse_front_matter(lines: List[str], ctx: _Ctx,
                        cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    empty = _empty_meta()
    n = len(lines)
    i = 0
    while i < n and not lines[i].strip():
        i += 1
    if i >= n or lines[i].strip() != "---":
        ctx.error("E_FRONT_MISSING",
                  "File must start with a front-matter block between two '---' lines",
                  line=(i + 1) if i < n else 1)
        return empty, 0
    start = i + 1
    j = start
    while j < n and lines[j].strip() != "---":
        j += 1
    if j >= n:
        ctx.error("E_FRONT_UNCLOSED", "Front matter is missing its closing '---' line", line=i + 1)
        return empty, n
    raw = _read_kv_block(lines[start:j], start + 1, ctx)
    return _normalize_meta(raw, ctx, i + 1, cfg), j + 1


def _empty_meta() -> Dict[str, Any]:
    return {"title": "", "channel": "", "character": "", "target_minutes": 0,
            "titles_alt": [], "sensitivity": [], "pronunciations": {},
            "sfx_requests": [], "claims": [], "extra": {}}


def _read_kv_block(block: List[str], first_line: int, ctx: _Ctx) -> Dict[str, Dict[str, Any]]:
    """Minimal front-matter reader (NOT YAML). Splits on the first ': ' only."""
    data: Dict[str, Dict[str, Any]] = {}
    cur: Optional[str] = None
    for off, raw in enumerate(block):
        line_no = first_line + off
        s = raw.rstrip()
        if not s.strip() or s.strip().startswith("#"):
            continue
        item = ITEM_RE.match(s)
        if item and cur is not None and (isinstance(data[cur]["value"], list) or data[cur]["value"] == ""):
            if not isinstance(data[cur]["value"], list):
                data[cur]["value"] = []
            val = item.group(1)
            if not _is_quoted(val) and (": " in val or " | " in val) and cur not in ("pronunciations",):
                ctx.warn("W_QUOTING", f'List item under "{cur}" contains ": " or " | " and should be '
                                      f'wrapped in double quotes', line=line_no)
            data[cur]["value"].append((_unquote(val), line_no))
            continue
        key = KEY_RE.match(s) if not raw[:1].isspace() else None
        if key:
            k, v = key.group(1), (key.group(2) or "")
            if k in data:
                ctx.warn("W_FRONT_DUP", f'Duplicate front-matter key "{k}"; the last one wins', line=line_no)
            if k in LIST_KEYS and v.strip():
                ctx.warn("W_FRONT_VALUE", f'"{k}" should be a list of "- item" lines', line=line_no)
            if v and not _is_quoted(v) and ": " in v and k == "title":
                ctx.warn("W_QUOTING", 'Value contains ": " and should be wrapped in double quotes', line=line_no)
            data[k] = {"value": _unquote(v) if v else "", "line": line_no}
            cur = k
            continue
        ctx.warn("W_FRONT_LINE", f'Unrecognized front-matter line ignored: "{s.strip()}"', line=line_no)
    return data


def _normalize_meta(raw: Dict[str, Dict[str, Any]], ctx: _Ctx, fm_line: int,
                    cfg: Dict[str, Any]) -> Dict[str, Any]:
    meta = _empty_meta()

    for key in REQUIRED_KEYS:
        entry = raw.get(key)
        if entry is None or entry["value"] in ("", []):
            ctx.error("E_FRONT_KEY", f'Front matter is missing required key "{key}"', line=fm_line)

    def scalar(key: str) -> str:
        e = raw.get(key)
        if not e:
            return ""
        v = e["value"]
        if isinstance(v, list):
            ctx.error("E_FRONT_VALUE", f'"{key}" must be a single value, not a list', line=e["line"])
            return ""
        return v

    meta["title"] = scalar("title")
    meta["channel"] = scalar("channel")
    meta["character"] = canonical_character(scalar("character"), cfg)

    tm = scalar("target_minutes")
    if tm != "":
        try:
            f = float(tm)
            if f <= 0:
                raise ValueError
            meta["target_minutes"] = int(f) if f.is_integer() else f
        except ValueError:
            ctx.error("E_FRONT_VALUE", f'target_minutes must be a positive number, got "{tm}"',
                      line=raw["target_minutes"]["line"])

    def items(key: str) -> List[Tuple[str, int]]:
        e = raw.get(key)
        if not e:
            return []
        v = e["value"]
        if isinstance(v, list):
            return v
        return [(v, e["line"])] if v else []

    meta["titles_alt"] = [t for t, _ in items("titles_alt")]
    meta["sensitivity"] = [t for t, _ in items("sensitivity")]

    for text, ln in items("pronunciations"):
        m = re.match(r"^(.+?)\s*=\s*(.+)$", text)
        if not m:
            ctx.warn("W_FRONT_VALUE", f'Pronunciation "{text}" should look like: Word = respelling', line=ln)
            continue
        meta["pronunciations"][m.group(1).strip()] = m.group(2).strip()

    for text, ln in items("sfx_requests"):
        name, sep, desc = text.partition(":")
        meta["sfx_requests"].append({"name": name.strip(), "description": desc.strip() if sep else ""})

    for text, ln in items("claims"):
        parts = re.split(r"\s+\|\s+", text, maxsplit=2)
        claim = parts[0].strip()
        source = parts[1].strip() if len(parts) > 1 else ""
        hedge = parts[2].strip() if len(parts) > 2 else ""
        meta["claims"].append({"claim": claim, "source": source, "hedge": hedge})

    known = set(REQUIRED_KEYS) | set(LIST_KEYS)
    for k, e in raw.items():
        if k not in known:
            v = e["value"]
            meta["extra"][k] = [t for t, _ in v] if isinstance(v, list) else v
    return meta


# ---------------------------------------------------------------------------
# Scene collection (structure only)
# ---------------------------------------------------------------------------

def _collect_scenes(lines: List[str], start: int, ctx: _Ctx) -> List[Dict[str, Any]]:
    scenes: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    in_narr = False
    stray_reported = False

    for idx in range(start, len(lines)):
        line_no = idx + 1
        s = lines[idx].strip()
        if not s:
            continue

        m = SCENE_RE.match(s)
        if m:
            cur = {"number": int(m.group(1)), "digits": m.group(1), "title": m.group(2).strip(),
                   "line": line_no, "fields": {}, "narr": [], "has_narr": False}
            scenes.append(cur)
            in_narr = False
            continue

        if HEADING2_RE.match(s):
            ctx.scene = None
            ctx.error("E_SCENE_HEADER", f'Malformed scene header "{s}". Expected: ## SCENE NN: Title',
                      line=line_no)
            cur = None
            in_narr = False
            stray_reported = True
            continue

        if cur is None:
            if not stray_reported:
                ctx.error("E_STRAY_TEXT", "Text found outside any scene (before the first '## SCENE' header)",
                          line=line_no)
                stray_reported = True
            continue

        if NARRATION_RE.match(s):
            if cur["has_narr"]:
                ctx.scene = f"scene{cur['number']:02d}"
                ctx.error("E_NARRATION_DUP", 'Scene has more than one "### Narration" heading', line=line_no)
            else:
                cur["has_narr"] = True
                cur["narr_line"] = line_no
            in_narr = True
            continue

        if HEADING_ANY_RE.match(s):
            ctx.scene = f"scene{cur['number']:02d}"
            ctx.warn("W_HEADING", f'Unexpected heading ignored: "{s}"', line=line_no)
            continue

        if in_narr:
            cur["narr"].append((line_no, s))
            continue

        fm = FIELD_RE.match(s)
        if fm:
            key = fm.group(1)
            if key in cur["fields"]:
                ctx.scene = f"scene{cur['number']:02d}"
                ctx.warn("W_SCENE_FIELD_DUP", f'Duplicate "{key}:" field; the last one wins', line=line_no)
            cur["fields"][key] = (fm.group(2).strip(), line_no)
        else:
            ctx.scene = f"scene{cur['number']:02d}"
            ctx.error("E_SCENE_FIELD", f'Unrecognized line before "### Narration": "{s}"', line=line_no)
    ctx.scene = None
    return scenes


# ---------------------------------------------------------------------------
# Scene building
# ---------------------------------------------------------------------------

def _build_scenes(raw_scenes: List[Dict[str, Any]], pron: Dict[str, str],
                  cfg: Dict[str, Any], ctx: _Ctx) -> List[Scene]:
    scenes: List[Scene] = []
    prev_music = "none"
    default_layout = str(cfg.get("default_layout", "bob_pip"))
    expected = 1

    if not raw_scenes:
        ctx.scene = None
        ctx.error("E_SCENE_HEADER", "No scenes found. Each scene starts with: ## SCENE 01: Title", line=1)

    for raw in raw_scenes:
        num = raw["number"]
        scene_id = f"scene{num:02d}"
        ctx.scene = scene_id

        if len(raw["digits"]) != 2:
            ctx.warn("W_SCENE_NUMBER", f'Scene number "{raw["digits"]}" should use two digits', line=raw["line"])
        if num != expected:
            ctx.error("E_SCENE_NUMBER",
                      f"Scene numbers must be consecutive: expected {expected:02d}, found {num:02d}",
                      line=raw["line"])
        expected = num + 1

        fields = raw["fields"]
        image = fields.get("image", ("", raw["line"]))[0]
        prompt = fields.get("prompt", ("", raw["line"]))[0]
        if not image:
            ctx.error("E_SCENE_FIELD", 'Scene is missing the required "image:" field', line=raw["line"])
        else:
            m = IMAGE_STEM_RE.match(image)
            if not m:
                ctx.warn("W_IMAGE_FORMAT", f'image "{image}" should look like sNN_slug (lowercase, underscores)',
                         line=fields["image"][1])
            elif int(m.group(1)) != num:
                ctx.warn("W_IMAGE_FORMAT", f'image "{image}" does not match scene number {num:02d}',
                         line=fields["image"][1])
        if not prompt:
            ctx.error("E_SCENE_FIELD", 'Scene is missing the required "prompt:" field', line=raw["line"])

        if "music" in fields and fields["music"][0]:
            music, music_explicit = fields["music"][0], True
        else:
            music, music_explicit = prev_music, False
        prev_music = music

        if "layout" in fields and fields["layout"][0]:
            layout, layout_explicit = fields["layout"][0].strip().lower(), True
        else:
            layout, layout_explicit = default_layout, False

        segments: List[Segment] = []
        if not raw["has_narr"]:
            ctx.error("E_SCENE_NO_NARRATION", 'Scene is missing its "### Narration" section', line=raw["line"])
        elif not raw["narr"]:
            ctx.error("E_SCENE_EMPTY", "Scene has a Narration heading but no lines", line=raw["narr_line"])
        else:
            segments = _parse_narration(scene_id, raw["narr"], pron, cfg, ctx)
            if not segments:
                ctx.error("E_SCENE_EMPTY", "Scene produced no spoken segments", line=raw["narr_line"])

        scenes.append(Scene(
            id=scene_id, number=num, title=raw["title"], line=raw["line"], image=image, prompt=prompt,
            music=music, music_explicit=music_explicit,
            word_count=sum(s.word_count for s in segments), segments=segments,
            layout=layout, layout_explicit=layout_explicit,
        ))
    ctx.scene = None
    return scenes


# ---------------------------------------------------------------------------
# Narration parsing
# ---------------------------------------------------------------------------

def _norm_emotion(s: str) -> str:
    return re.sub(r"[\s\-]+", "_", s.strip().lower())


def _is_word(tok: str) -> bool:
    return any(c.isalnum() for c in tok)


def _count_words(s: str) -> int:
    return sum(1 for t in s.split() if _is_word(t))


def _num(s: str, what: str, raw: str, ctx: _Ctx, line: int) -> Optional[float]:
    try:
        return float(s)
    except ValueError:
        ctx.error("E_CUE_ARG", f'{what} must be a number in {raw}, got "{s}"', line=line)
        return None


def _parse_cue(content: str, raw: str, word_index: int, ctx: _Ctx, line: int) -> Optional[Cue]:
    if ":" not in content:
        ctx.error("E_CUE_MALFORMED", f'Malformed cue {raw}: expected {{type:argument}}', line=line)
        return None
    ctype, _, rest = content.partition(":")
    ctype = ctype.strip().lower()
    args = [a.strip() for a in rest.split("|")]
    if ctype not in CUE_TYPES:
        ctx.error("E_CUE_TYPE", f'Unknown cue type "{ctype}" in {raw}. Valid types: {", ".join(CUE_TYPES)}',
                  line=line)
        return None
    if not args or not args[0]:
        ctx.error("E_CUE_MALFORMED", f"Cue {raw} is missing its first argument", line=line)
        return None

    def too_many(limit: int) -> bool:
        if len(args) > limit:
            ctx.error("E_CUE_ARG", f"Cue {raw} has too many arguments (max {limit})", line=line)
            return True
        return False

    cue = Cue(type=ctype, name="", word_index=word_index, char_offset=0, raw=raw)

    if ctype == "sfx":
        if too_many(1):
            return None
        m = SFX_ARG_RE.match(args[0])
        if not m:
            ctx.error("E_CUE_ARG", f'sfx argument in {raw} must look like name or name+0.15', line=line)
            return None
        cue.name = m.group(1).lower()
        cue.offset_s = float(m.group(2)) if m.group(2) else 0.0
    elif ctype == "bed":
        if too_many(1):
            return None
        cue.name = args[0].lower()
    elif ctype == "text":
        if too_many(3):
            return None
        if len(args) < 2 or not args[1]:
            ctx.error("E_CUE_ARG", f"text cue {raw} needs a style: {{text:WORDS|style}}", line=line)
            return None
        cue.name = args[0]
        cue.style = args[1].lower()
        if len(args) == 3:
            cue.duration_s = _num(args[2], "text duration", raw, ctx, line)
    elif ctype == "img":
        if too_many(2):
            return None
        cue.name = args[0].lower()
        if len(args) == 2:
            cue.duration_s = _num(args[1], "img duration", raw, ctx, line)
    elif ctype == "cam":
        if too_many(3):
            return None
        cue.name = args[0].lower()
        if len(args) >= 2:
            cue.duration_s = _num(args[1], "cam duration", raw, ctx, line)
        if len(args) == 3:
            cue.scale = _num(args[2], "cam scale", raw, ctx, line)
    return cue


def _process_run(run: str, ctx: _Ctx, line: int) -> Tuple[str, List[Cue], List[Tuple[str, int]]]:
    """Extract cues from a text run. Returns (clean_text, cues, words) where
    words is [(token, char_start)] for the spoken words of clean_text."""
    acc = ""
    specs: List[Tuple[str, str, str, bool]] = []
    pos = 0
    for m in CUE_RE.finditer(run):
        acc += run[pos:m.start()]
        before = acc[-1:]
        after = run[m.end():m.end() + 1]
        midword = bool(before and after and before.isalnum() and after.isalnum())
        specs.append((m.group(0), m.group(1), acc, midword))
        pos = m.end()
    acc += run[pos:]

    stray = CUE_RE.sub("", run)
    if "{" in stray or "}" in stray:
        ctx.error("E_BRACE_STRAY", "Unbalanced or nested { } braces in narration", line=line)

    text = " ".join(acc.split())
    words = [(mm.group(), mm.start()) for mm in WORD_RE.finditer(text) if _is_word(mm.group())]

    cues: List[Cue] = []
    for raw, content, snapshot, midword in specs:
        wi = _count_words(snapshot)
        if midword:
            ctx.warn("W_CUE_MIDWORD", f"Cue {raw} sits in the middle of a word; it will bind to that word",
                     line=line)
            wi = max(wi - 1, 0)
        cue = _parse_cue(content, raw, wi, ctx, line)
        if cue is None:
            continue
        cue.char_offset = words[wi][1] if wi < len(words) else len(text)
        cues.append(cue)
    return text, cues, words


def _apply_pronunciations(text: str, pron: Dict[str, str]) -> str:
    out = text
    for word in sorted(pron, key=len, reverse=True):
        out = re.sub(r"(?<![\w])" + re.escape(word) + r"(?![\w])", pron[word], out)
    return out


def _parse_narration(scene_id: str, narr_lines: List[Tuple[int, str]], pron: Dict[str, str],
                     cfg: Dict[str, Any], ctx: _Ctx) -> List[Segment]:
    pauses = cfg["pauses"]
    valid_tags = pause_tags(cfg)
    segments: List[Segment] = []
    carried: List[Cue] = []
    last_kind: Optional[str] = None            # "text" | "pause" | None
    cur_emotion: Optional[str] = None
    cur_modifier: Optional[str] = None
    emotion_missing_reported = False
    scene_pending_before: Optional[str] = None  # pause-only line at the very start of a scene

    for line_no, s in narr_lines:
        body = s
        explicit = False

        if s.startswith("("):
            m = EMOTION_RE.match(s)
            names = [_norm_emotion(p) for p in m.group(1).split(",")] if m else []
            if not m or len(names) > 2 or any(not n for n in names):
                ctx.error("E_EMOTION_MALFORMED",
                          f'Malformed emotion tag in "{s[:50]}". Use (emotion) or (primary, modifier)',
                          line=line_no)
                continue
            cur_emotion = names[0]
            cur_modifier = names[1] if len(names) == 2 else None
            explicit = True
            body = m.group(2)
        elif cur_emotion is None and not emotion_missing_reported:
            ctx.error("E_EMOTION_MISSING",
                      "The first narration line of a scene needs an explicit (emotion) tag", line=line_no)
            emotion_missing_reported = True

        # ---- tokenize the line body into text runs and pause tags
        tokens: List[Tuple[str, Any]] = []
        pos = 0
        for pm in PAUSE_RE.finditer(body):
            tokens.append(("text", body[pos:pm.start()]))
            tokens.append(("pause", (pm.group(1) or "").strip().lower()))
            pos = pm.end()
        tokens.append(("text", body[pos:]))

        # stray [ ] left over once valid pauses and cues are removed
        leftover = CUE_RE.sub("", PAUSE_RE.sub("", body))
        if "[" in leftover or "]" in leftover:
            ctx.error("E_BRACKET_STRAY", "Unrecognized [bracket] text in narration; only [pause] tags are allowed",
                      line=line_no)

        line_has_words = any(t[0] == "text" and _count_words(CUE_RE.sub(" ", t[1])) > 0 for t in tokens)
        line_segments: List[Segment] = []
        before_pending: Optional[str] = None

        for kind, val in tokens:
            if kind == "text":
                text, cues, words = _process_run(val, ctx, line_no)
                if not words:
                    if text.strip():
                        ctx.warn("W_NO_WORDS", f'Ignored text with no spoken words: "{text}"', line=line_no)
                    carried.extend(cues)
                    continue
                idx = len(segments) + 1
                seg = Segment(
                    id=f"{scene_id}_seg{idx:02d}", scene_id=scene_id, index=idx, line=line_no,
                    emotion=cur_emotion, modifier=cur_modifier, emotion_inherited=not explicit,
                    text=text, tts_text=_apply_pronunciations(text, pron),
                    words=[w for w, _ in words], word_count=len(words),
                )
                for c in carried:
                    c.carried = True
                    c.word_index = 0
                    c.char_offset = 0
                seg.cues = carried + cues
                carried = []

                tag = before_pending or scene_pending_before
                if tag:
                    seg.pause_before_tag = tag
                    seg.pause_before = pauses[tag]
                before_pending = None
                scene_pending_before = None

                bad = sorted({ch for ch in text if not (ch.isalnum() or ch.isspace() or ch in ALLOWED_PUNCT)})
                if bad:
                    ctx.warn("W_SYMBOL", f"Unusual characters in spoken text: {' '.join(bad)}",
                             line=line_no, segment=seg.id)
                segments.append(seg)
                line_segments.append(seg)
                last_kind = "text"
            else:
                tag = val or "default"
                if tag not in valid_tags:
                    ctx.error("E_PAUSE_UNKNOWN",
                              f'Unknown pause tag [pause:{val}]. Valid: [pause], '
                              + ", ".join(f"[pause:{t}]" for t in sorted(valid_tags - {"default"})),
                              line=line_no, segment=line_segments[-1].id if line_segments else None)
                    continue
                if last_kind == "pause":
                    ctx.error("E_PAUSE_ADJACENT",
                              "Two pause tags with no spoken text between them",
                              line=line_no, segment=segments[-1].id if segments else None)
                    continue
                last_kind = "pause"
                if line_segments:
                    seg = line_segments[-1]
                    seg.pause_after_tag, seg.pause_after = tag, pauses[tag]
                elif line_has_words:
                    before_pending = tag
                elif segments:
                    seg = segments[-1]
                    seg.pause_after_tag, seg.pause_after = tag, pauses[tag]
                else:
                    scene_pending_before = tag

    if carried:
        if segments:
            last = segments[-1]
            for c in carried:
                c.carried = True
                c.word_index = last.word_count
                c.char_offset = len(last.text)
            last.cues.extend(carried)
        else:
            ctx.warn("W_CUES_ORPHAN", "Cues found but the scene has no spoken words")

    # ---- resolve the silence between consecutive segments (no pause stacking)
    line_gap = cfg["line_gap_s"]
    for i, seg in enumerate(segments):
        if seg.pause_after is not None:
            seg.gap_after_s = seg.pause_after
        elif i + 1 < len(segments):
            seg.gap_after_s = 0.0 if segments[i + 1].pause_before is not None else line_gap
        else:
            seg.gap_after_s = 0.0
    return segments