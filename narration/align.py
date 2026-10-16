"""Word-level timing: where inside each segment every word is spoken.

Three sources, in order of preference:

  1. "tokens"    the TTS backend returned per-token timestamps (Kokoro when the
                 installed version exposes them, the fake backend in tests).
                 Token times refer to the RAW audio, so the trim offset found
                 during processing is subtracted.
  2. "aligned"   energy-island detection on the processed segment WAV: when the
                 number of speech islands equals the word count the mapping is
                 1:1 and trustworthy.
  3. "estimated" words are spread over the detected speech span (or the whole
                 segment when no speech is found) proportional to their length.
                 Same idea as the old character estimate, but anchored to real
                 speech instead of the raw segment bounds.

All returned spans are segment-local seconds (0 = start of the processed
segment WAV, which is exactly what the timeline places).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

Span = Tuple[float, float]
Token = Tuple[str, float, float]            # (text, start_s, end_s) in raw audio

DEFAULTS = {"rel_threshold": 0.2, "abs_threshold": 0.01,
            "min_island_s": 0.03, "merge_gap_s": 0.05}

_POSSESSIVE_RE = re.compile(r"(['\u2019]s)([^\w]*)$", re.IGNORECASE)
_PLURAL_SKIP_SUFFIXES = ("s", "x", "z", "ch", "sh")


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _allows_plural_s(parts: Sequence[str]) -> bool:
    joined = "".join(parts).lower()
    if len(joined) < 4 or not joined.isalpha():
        return False
    return not joined.endswith(_PLURAL_SKIP_SUFFIXES)


def _pronunciation_regex(word: str) -> Optional[re.Pattern[str]]:
    """Build a case-insensitive, whole-word regex for a pronunciation key,
    allowing flexible whitespace/hyphens between words or compound parts
    and an optional plural 's' on eligible stems."""
    clean = word.strip()
    if not clean:
        return None
    parts = [p for p in re.split(r"[\s\-]+", clean) if p]
    if not parts:
        return None
    if len(parts) > 1:
        body = r"[\s\-]*".join(re.escape(p) for p in parts)
    else:
        tok = parts[0]
        alts = [re.escape(tok)]
        if len(tok) >= 6 and tok.isalpha():
            for i in range(3, len(tok) - 2):
                alts.append(re.escape(tok[:i]) + r"[\s\-]+" + re.escape(tok[i:]))
        body = "|".join(alts)
    plural_pat = r"(?P<plural>s)?" if _allows_plural_s(parts) else ""
    return re.compile(r"(?<![\w])(?:" + body + r")" + plural_pat + r"(?![\w])", re.IGNORECASE)


def _distribute_respelling(respelling: str, count: int,
                           ref_words: Sequence[str]) -> Optional[List[str]]:
    """Distribute a respelling string across `count` display words."""
    if count <= 0:
        return None
    if count == 1:
        return [respelling]
    ws_parts = [p for p in respelling.split() if _norm(p)]
    if len(ws_parts) == count:
        return ws_parts
    syl_parts = [p for p in re.split(r"[\s\-]+", respelling.strip()) if _norm(p)]
    if len(syl_parts) == count:
        return syl_parts
    if len(syl_parts) > count:
        weights = [max(len(_norm(w)), 1) for w in ref_words]
        total_w = sum(weights)
        n_syl = len(syl_parts)
        out: List[str] = []
        cursor = 0
        acc_w = 0
        for idx in range(count):
            acc_w += weights[idx]
            if idx == count - 1:
                nxt = n_syl
            else:
                target_end = int(round(n_syl * (acc_w / total_w)))
                min_end = cursor + 1
                max_end = n_syl - (count - 1 - idx)
                nxt = min(max(target_end, min_end), max_end)
            out.append("".join(syl_parts[cursor:nxt]))
            cursor = nxt
        return out
    return None


def _match_last_word_suffix(word: str, target_stem: str,
                            allow_plural: bool) -> Optional[str]:
    """Check if `word` matches `target_stem` exactly, with possessive `'s`,
    or with plural `s`. Returns the trailing 's' suffix ('s' or ''), or None."""
    if _POSSESSIVE_RE.search(word):
        stripped = _POSSESSIVE_RE.sub("", word)
        if _norm(stripped) == target_stem:
            return "s"
    norm_w = _norm(word)
    if norm_w == target_stem:
        return ""
    if allow_plural and norm_w == target_stem + "s":
        return "s"
    return None


def _apply_single_word_pronunciations(word: str, ordered_keys: Sequence[str],
                                      pronunciations: Dict[str, str]) -> str:
    out = word
    placeholders: List[Tuple[str, str]] = []
    for idx, k in enumerate(ordered_keys):
        pat = _pronunciation_regex(k)
        if pat is None:
            continue
        base_token = f"\x00PRON{idx}_B\x00"
        plural_token = f"\x00PRON{idx}_S\x00"

        def _repl(m: re.Match[str]) -> str:
            if m.groupdict().get("plural"):
                return plural_token
            return base_token

        out, n = pat.subn(_repl, out)
        if n > 0:
            placeholders.append((plural_token, pronunciations[k] + "s"))
            placeholders.append((base_token, pronunciations[k]))
    for token, repl in placeholders:
        out = out.replace(token, repl)
    return out


def phonetic_targets(words: Sequence[str],
                     pronunciations: Optional[Dict[str, str]] = None) -> List[str]:
    """Map display words to their normalized phonetic character targets after
    applying pronunciations (including case-insensitive matches, plural/possessive
    forms, hyphenated respellings, single-to-multi-word expansions like
    'smashburger = smash BUR-ger' matching 'Smash Burger' or 'smash burgers',
    and multi-word phrases like 'patty melt = PAT-ee melt')."""
    if not pronunciations:
        return [_norm(w) for w in words]

    spoken = list(words)
    replaced_mask = [False] * len(spoken)
    ordered_keys = sorted(pronunciations, key=lambda k: len(k.strip()), reverse=True)

    # 1. Match multi-word sequences in `spoken` against multi-word keys (e.g. "patty melt")
    #    or closed compound keys (e.g. "smashburger" matching ["Smash", "Burger."] or ["smash", "burgers"]).
    for k in ordered_keys:
        k_parts = [_norm(p) for p in re.split(r"[\s\-]+", k.strip()) if _norm(p)]
        if not k_parts:
            continue
        k_joined = "".join(k_parts)
        allow_plural = _allows_plural_s(k_parts)

        if len(k_parts) >= 2:
            m = len(k_parts)
            i = 0
            while i <= len(spoken) - m:
                if not any(replaced_mask[i : i + m]):
                    head_norms = [_norm(spoken[i + j]) for j in range(m - 1)]
                    if head_norms == k_parts[:-1]:
                        suffix = _match_last_word_suffix(
                            spoken[i + m - 1], k_parts[-1], allow_plural
                        )
                        if suffix is not None:
                            dist = _distribute_respelling(pronunciations[k], m, spoken[i : i + m])
                            if dist is not None:
                                for j in range(m):
                                    val = dist[j] + (suffix if j == m - 1 else "")
                                    spoken[i + j] = val
                                    replaced_mask[i + j] = True
                                i += m
                                continue
                i += 1
        elif len(k_joined) >= 6 and k_joined.isalpha():
            i = 0
            while i <= len(spoken) - 2:
                if not replaced_mask[i] and not replaced_mask[i + 1]:
                    w0 = _norm(spoken[i])
                    if len(w0) >= 3 and k_joined.startswith(w0):
                        rem = k_joined[len(w0):]
                        if len(rem) >= 3:
                            suffix = _match_last_word_suffix(spoken[i + 1], rem, allow_plural)
                            if suffix is not None:
                                dist = _distribute_respelling(pronunciations[k], 2, spoken[i : i + 2])
                                if dist is not None:
                                    spoken[i] = dist[0]
                                    spoken[i + 1] = dist[1] + suffix
                                    replaced_mask[i] = True
                                    replaced_mask[i + 1] = True
                                    i += 2
                                    continue
                i += 1

    # 2. Apply single-word / regex substitutions per remaining word
    out: List[str] = []
    for idx, w in enumerate(spoken):
        if replaced_mask[idx]:
            out.append(_norm(w))
        else:
            out.append(_norm(_apply_single_word_pronunciations(w, ordered_keys, pronunciations)))
    return out


# ---------------------------------------------------------------------------
# energy islands
# ---------------------------------------------------------------------------

def speech_islands(audio: np.ndarray, sr: int, wcfg: Dict[str, Any]) -> List[Span]:
    """Detect speech islands: runs of frames whose RMS stays above
    max(abs_threshold, rel_threshold * peak). Islands separated by less than
    merge_gap_s are merged; islands shorter than min_island_s are dropped."""
    cfg = {**DEFAULTS, **(wcfg or {})}
    audio = np.asarray(audio, dtype=np.float32)
    win = max(int(sr * 0.02), 1)
    hop = max(int(sr * 0.01), 1)
    if audio.size < win:
        return []
    csum = np.concatenate([[0.0], np.cumsum(audio.astype(np.float64) ** 2)])
    starts = np.arange(0, audio.size - win + 1, hop)
    rms = np.sqrt((csum[starts + win] - csum[starts]) / win)
    peak = float(rms.max())
    if peak <= 0.0:
        return []
    thr = max(float(cfg["abs_threshold"]), float(cfg["rel_threshold"]) * peak)
    active = rms >= thr
    if not active.any():
        return []

    islands: List[Span] = []
    i = 0
    n = len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and active[j + 1]:
            j += 1
        islands.append((starts[i] / sr, min((starts[j] + win) / sr, audio.size / sr)))
        i = j + 1

    merge = float(cfg["merge_gap_s"])
    merged: List[Span] = []
    for s, e in islands:
        if merged and s - merged[-1][1] < merge:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    min_len = float(cfg["min_island_s"])
    return [(s, e) for s, e in merged if e - s >= min_len]


# ---------------------------------------------------------------------------
# token timestamps -> word spans
# ---------------------------------------------------------------------------

def spans_from_tokens(words: Sequence[str], tokens: Sequence[Token],
                      trim_offset_s: float, duration: float,
                      pronunciations: Optional[Dict[str, str]] = None) -> Optional[List[Span]]:
    """Group backend tokens into the segment's words by matching their
    normalized characters (accounting for pronunciation respellings). Returns
    None when the token stream does not line up with the words (caller falls
    back to audio alignment)."""
    if not words or not tokens:
        return None
    targets = phonetic_targets(words, pronunciations)
    spans: List[Span] = []
    ti = 0
    for target in targets:
        if not target:
            return None
        acc = ""
        start = None
        end = None
        while ti < len(tokens) and len(acc) < len(target):
            text, ts, te = tokens[ti]
            ti += 1
            if not _norm(text):
                continue                            # punctuation-only token
            if start is None:
                start = ts
            end = te
            acc += _norm(text)
        if acc != target or start is None:
            return None
        spans.append((max(start - trim_offset_s, 0.0),
                      min(max(end - trim_offset_s, 0.0), duration)))
    if any(e < s for s, e in spans):
        return None
    return spans


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def word_spans(words: Sequence[str], audio: np.ndarray, sr: int,
               wcfg: Optional[Dict[str, Any]] = None,
               tokens: Optional[Sequence[Token]] = None,
               trim_offset_s: float = 0.0,
               pronunciations: Optional[Dict[str, str]] = None) -> Tuple[List[Span], str]:
    """Returns (spans, method) with method in {"tokens", "aligned", "estimated"}.
    spans[i] is the (start, end) of words[i] in segment-local seconds."""
    words = list(words)
    duration = len(audio) / float(sr) if sr else 0.0
    if not words:
        return [], "estimated"

    if tokens:
        spans = spans_from_tokens(words, tokens, trim_offset_s, duration,
                                  pronunciations=pronunciations)
        if spans is not None:
            return spans, "tokens"

    islands = speech_islands(audio, sr, wcfg or {})
    if len(islands) == len(words):
        return islands, "aligned"

    # proportional spread over the detected speech span (or the whole segment)
    lo, hi = (islands[0][0], islands[-1][1]) if islands else (0.0, duration)
    weights = [max(len(_norm(w)), 1) for w in words]
    total = float(sum(weights))
    spans = []
    cursor = lo
    for w in weights:
        nxt = lo + (hi - lo) * (sum(weights[: len(spans) + 1]) / total)
        spans.append((cursor, nxt))
        cursor = nxt
    return spans, "estimated"