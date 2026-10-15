"""Validation: vocabulary errors (block the build) and lint warnings (reported only).

Split from parser.py on purpose: the parser answers "is this well-formed?",
the validators answer "is this allowed, and is it any good?".
Everything is driven by the channel config, so a second channel is a new JSON file.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import camera_classes, emotion_names, layout_names, sfx_bed, sfx_point
from .schema import (CODES_ASSETS, CODES_EMOTION, CODES_FRONT, CODES_LINES, CODES_NAMES,
                     CODES_SCENES, Diagnostic, ParseResult, Project, Scene, Segment)

PAUSE_NAMES = ("short", "default", "beat", "long")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _hint(name: str, options: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(options), n=1, cutoff=0.6)
    return f' Did you mean "{close[0]}"?' if close else ""


def _err(out: List[Diagnostic], code: str, msg: str, seg: Optional[Segment] = None,
         scene: Optional[Scene] = None, line: Optional[int] = None) -> None:
    out.append(Diagnostic("error", code, msg,
                          seg.scene_id if seg else (scene.id if scene else None),
                          seg.id if seg else None,
                          line if line is not None else (seg.line if seg else (scene.line if scene else None))))


def _warn(out: List[Diagnostic], code: str, msg: str, seg: Optional[Segment] = None,
          scene: Optional[Scene] = None, line: Optional[int] = None) -> None:
    out.append(Diagnostic("warning", code, msg,
                          seg.scene_id if seg else (scene.id if scene else None),
                          seg.id if seg else None,
                          line if line is not None else (seg.line if seg else (scene.line if scene else None))))


def _positions(scene: Scene) -> List[Tuple[Segment, int]]:
    """(segment, index of its first word within the scene)."""
    out, running = [], 0
    for seg in scene.segments:
        out.append((seg, running))
        running += seg.word_count
    return out


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def _check_errors(project: Project, cfg: Dict[str, Any]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    emos = emotion_names(cfg)
    layouts = layout_names(cfg)
    points, beds = sfx_point(cfg), sfx_bed(cfg)
    reactions = set(cfg["reaction_images"])
    styles = set(cfg["text_styles"])
    cams = camera_classes(cfg)
    max_line = cfg["limits"]["max_line_words"]
    max_overlay = cfg["limits"]["max_overlay_words"]

    for scene in project.scenes:
        if scene.layout not in layouts:
            _err(out, "E_LAYOUT_UNKNOWN",
                 f'Unknown scene layout "{scene.layout}".{_hint(scene.layout, layouts)} '
                 f'Available layouts: {", ".join(sorted(layouts))}', scene=scene)

        # per source line word limit (a line may hold several segments)
        per_line: Dict[int, List[Segment]] = defaultdict(list)
        for seg in scene.segments:
            per_line[seg.line].append(seg)
        for line_no, segs in per_line.items():
            total = sum(s.word_count for s in segs)
            if total > max_line:
                _err(out, "E_LINE_TOO_LONG",
                     f"Narration line has {total} spoken words (max {max_line}). Split it into shorter lines.",
                     seg=segs[0])

        for seg in scene.segments:
            # emotions
            for label, name in (("emotion", seg.emotion), ("modifier", seg.modifier)):
                if name and name not in emos:
                    _err(out, "E_EMOTION_UNKNOWN",
                         f'Unknown {label}: "{name}".{_hint(name, emos)} '
                         f'Available emotions: {", ".join(sorted(emos))}', seg=seg)
            # numbers must be words
            m = re.search(r"\d", seg.text)
            if m:
                lo = max(m.start() - 15, 0)
                _err(out, "E_DIGIT",
                     f'Digit in spoken text near "...{seg.text[lo:m.end() + 15]}...". '
                     f"Write numbers as words (e.g. \"two minutes\").", seg=seg)

            for cue in seg.cues:
                if cue.type == "sfx":
                    if cue.name in beds:
                        _err(out, "E_SFX_TYPE",
                             f'"{cue.name}" is a bed sound. Use {{bed:{cue.name}}} ... {{bed:end}}, not {{sfx:}}.',
                             seg=seg)
                    elif cue.name not in points:
                        _err(out, "E_SFX_UNKNOWN",
                             f'Unknown sfx "{cue.name}".{_hint(cue.name, points)} '
                             f"If it is a new sound, add it to sfx_requests.", seg=seg)
                elif cue.type == "bed":
                    if cue.name == "end":
                        continue
                    if cue.name in points:
                        _err(out, "E_BED_TYPE",
                             f'"{cue.name}" is a point sound. Use {{sfx:{cue.name}}} instead of {{bed:}}.', seg=seg)
                    elif cue.name not in beds:
                        _err(out, "E_BED_UNKNOWN",
                             f'Unknown bed "{cue.name}".{_hint(cue.name, beds)} Beds: {", ".join(sorted(beds))}',
                             seg=seg)
                elif cue.type == "img":
                    if cue.name != "scene" and cue.name not in reactions:
                        _err(out, "E_IMG_UNKNOWN",
                             f'Unknown reaction image "{cue.name}".{_hint(cue.name, reactions)} '
                             f'Available: {", ".join(sorted(reactions))}, scene', seg=seg)
                elif cue.type == "cam":
                    if cue.name not in cams:
                        _err(out, "E_CAM_UNKNOWN",
                             f'Unknown camera move "{cue.name}".{_hint(cue.name, cams)} '
                             f'Available: {", ".join(sorted(cams))}', seg=seg)
                elif cue.type == "text":
                    n = len(cue.name.split())
                    if n > max_overlay:
                        _err(out, "E_TEXT_TOO_LONG",
                             f'Overlay "{cue.name}" has {n} words (max {max_overlay}).', seg=seg)
                    if cue.name != cue.name.upper():
                        _err(out, "E_TEXT_CASE", f'Overlay "{cue.name}" must be UPPERCASE.', seg=seg)
                    if cue.style and cue.style not in styles:
                        _err(out, "E_TEXT_STYLE",
                             f'Unknown text style "{cue.style}".{_hint(cue.style, styles)} '
                             f'Available: {", ".join(sorted(styles))}', seg=seg)
    return out


# ---------------------------------------------------------------------------
# assets (optional)
# ---------------------------------------------------------------------------

def _find_asset(folder: Path, name: str) -> bool:
    return folder.is_dir() and any(folder.glob(f"{name}.*"))


def _check_assets(project: Project, cfg: Dict[str, Any], assets_dir: str) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    root = Path(assets_dir)
    sfx_dir = root / cfg["assets"]["sfx"]
    react_dir = root / cfg["assets"]["reaction_images"]
    scene_dir = root / cfg["assets"]["scene_images"]
    kits_sub = cfg["assets"].get("character_kits", "characters")
    kit_manifest = root / kits_sub / project.character / "character.json"
    if kit_manifest.exists():
        from render.kit import validate_kit
        _, kit_diags = validate_kit(kit_manifest, allowed_emotions=emotion_names(cfg))
        out.extend(kit_diags)

    seen = set()
    for scene in project.scenes:
        if scene.image and not _find_asset(scene_dir, scene.image):
            _warn(out, "W_ASSET_MISSING", f'Scene image "{scene.image}" not found in {scene_dir}', scene=scene)
        for seg in scene.segments:
            for cue in seg.cues:
                key = (cue.type, cue.name)
                if key in seen:
                    continue
                if cue.type in ("sfx", "bed") and cue.name != "end":
                    if not _find_asset(sfx_dir, cue.name):
                        seen.add(key)
                        _err(out, "E_ASSET_MISSING", f'SFX file "{cue.name}.*" not found in {sfx_dir}', seg=seg)
                elif cue.type == "img" and cue.name != "scene":
                    if not _find_asset(react_dir, cue.name):
                        seen.add(key)
                        _err(out, "E_ASSET_MISSING",
                             f'Reaction image "{cue.name}.*" not found in {react_dir}', seg=seg)
    return out


# ---------------------------------------------------------------------------
# lint warnings
# ---------------------------------------------------------------------------

def _lint(project: Project, cfg: Dict[str, Any]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    lint = cfg["lint"]
    wpm = cfg["words_per_minute"]
    cams = camera_classes(cfg)
    generic = set(cfg["sfx"]["generic"])
    total_words = sum(s.word_count for s in project.scenes)

    # word count vs target
    try:
        target = float(project.target_minutes) * wpm
    except (TypeError, ValueError):
        target = 0
    if target and total_words:
        diff = (total_words - target) / target
        if abs(diff) > lint["target_tolerance"]:
            _warn(out, "W_WORDCOUNT", f"{total_words} spoken words vs target ~{int(target)} "
                                      f"({diff:+.0%}). Adjust length or target_minutes.")

    # pause budget
    tags = [t for sc in project.scenes for sg in sc.segments
            for t in (sg.pause_before_tag, sg.pause_after_tag) if t]
    if total_words:
        if not tags:
            _warn(out, "W_PAUSE_DENSITY", "No pauses at all. Deadpan comedy needs timing.")
        else:
            wpp = total_words / len(tags)
            lo, hi = lint["words_per_pause"]
            if not lo <= wpp <= hi:
                _warn(out, "W_PAUSE_DENSITY",
                      f"One pause per {wpp:.0f} words (target roughly {lo}-{hi}; {len(tags)} pauses total).")
        missing = [n for n in PAUSE_NAMES if n not in set(tags)]
        if missing and tags:
            _warn(out, "W_PAUSE_NAMES", f"Pause names never used: {', '.join(missing)}.")

    # sfx budget
    all_sfx = [(sc, sg, c) for sc in project.scenes for sg in sc.segments for c in sg.cues if c.type == "sfx"]
    if all_sfx and total_words:
        wps = total_words / len(all_sfx)
        if wps < lint["words_per_sfx_min"]:
            _warn(out, "W_SFX_DENSITY",
                  f"{len(all_sfx)} SFX in {total_words} words (one per {wps:.0f}; limit one per "
                  f"{lint['words_per_sfx_min']}).")
    video_counts: Counter = Counter(c.name for _, _, c in all_sfx)
    for name, n in sorted(video_counts.items()):
        if n > lint["same_sfx_per_video_max"]:
            _warn(out, "W_SFX_REPEAT", f'"{name}" is used {n} times in the video '
                                       f'(max {lint["same_sfx_per_video_max"]}).')
    for sc in project.scenes:
        counts = Counter(c.name for s in sc.segments for c in s.cues if c.type == "sfx")
        g = sum(n for name, n in counts.items() if name in generic)
        if g > lint["generic_per_scene_max"]:
            _warn(out, "W_SFX_GENERIC", f"{g} generic sounds (ding/whoosh/pop) in one scene "
                                        f"(max {lint['generic_per_scene_max']}).", scene=sc)
        for name, n in sorted(counts.items()):
            if n > lint["same_sfx_per_scene_max"]:
                _warn(out, "W_SFX_REPEAT", f'"{name}" used {n} times in one scene '
                                           f'(max {lint["same_sfx_per_scene_max"]}).', scene=sc)

    # per-scene checks
    openers: Counter = Counter()
    for sc in project.scenes:
        pos = _positions(sc)
        total = sc.word_count

        if lint.get("max_scene_words") and total > lint["max_scene_words"]:
            _warn(out, "W_SCENE_LENGTH", f"Scene has {total} spoken words (about {total / wpm * 60:.0f}s "
                                         f"on one image). Consider splitting it.", scene=sc)

        imgs = sum(1 for s in sc.segments for c in s.cues if c.type == "img")
        if imgs > cfg["limits"]["max_img_per_scene"]:
            _warn(out, "W_IMG_COUNT", f"{imgs} reaction images (max {cfg['limits']['max_img_per_scene']}).",
                  scene=sc)

        # visual cue gaps
        marks = sorted({start + c.word_index for seg, start in pos for c in seg.cues
                        if c.type in ("img", "cam", "text")})
        edges = [0] + marks + [total]
        for a, b in zip(edges, edges[1:]):
            if b - a > lint["visual_gap_words"]:
                seg = next((s for s, st in reversed(pos) if st <= a), None)
                _warn(out, "W_VISUAL_GAP",
                      f"{b - a} spoken words without a visual cue (words {a + 1}-{b}).", seg=seg, scene=sc)

        # base camera spacing + opener
        base_marks = sorted(start + c.word_index for seg, start in pos for c in seg.cues
                            if c.type == "cam" and cams.get(c.name) == "base")
        for a, b in zip(base_marks, base_marks[1:]):
            if b - a < lint["base_cam_min_gap_words"]:
                _warn(out, "W_CAM_CLOSE", f"Two base camera moves only {b - a} words apart "
                                          f"(want {lint['base_cam_min_gap_words']}+).", scene=sc)
        first = next((c for seg, st in pos for c in seg.cues
                      if c.type == "cam" and cams.get(c.name) == "base" and st + c.word_index == 0), None)
        if first:
            openers[first.name] += 1

    for name, n in sorted(openers.items()):
        if n > lint["same_opener_max_scenes"]:
            _warn(out, "W_CAM_OPENER", f'{n} scenes open with "{name}" (max {lint["same_opener_max_scenes"]}).')

    # ending variety
    if len(project.scenes) >= 4:
        sigs = []
        for sc in project.scenes:
            last = sc.segments[-1] if sc.segments else None
            if last:
                sigs.append((any(c.type == "img" for c in last.cues),
                             last.pause_before_tag or "-", last.pause_after_tag or "-"))
        if sigs:
            sig, n = Counter(sigs).most_common(1)[0]
            if n / len(sigs) > lint["ending_repeat_fraction"]:
                _warn(out, "W_ENDING_REPEAT", f"{n} of {len(sigs)} scenes end with the same pattern "
                                              f"(reaction image={sig[0]}, lead pause={sig[1]}, tail pause={sig[2]}).")

    # front-matter notes
    if project.sfx_requests:
        names = ", ".join(r["name"] for r in project.sfx_requests)
        _warn(out, "W_SFX_REQUESTS", f"{len(project.sfx_requests)} new sounds to source before rendering: {names}")
    incomplete = [c for c in project.claims if not c["source"] or not c["hedge"]]
    if incomplete:
        _warn(out, "W_CLAIM_INCOMPLETE", f"{len(incomplete)} claims are missing a source or hedge "
                                         f'(expected "claim | source | hedge").')
    unverified = [c for c in project.claims if c["source"].lower() == "unverified"]
    if unverified:
        _warn(out, "W_CLAIM_UNVERIFIED", f"{len(unverified)} of {len(project.claims)} claims have "
                                         f"unverified sources. Check before recording.")
    if [s for s in project.sensitivity if s.strip().lower() != "none"]:
        _warn(out, "W_SENSITIVITY", f"Flagged sensitive topics: {', '.join(project.sensitivity)}. "
                                    f"Check platform policy before publishing.")
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def validate(project: Project, cfg: Dict[str, Any], assets_dir: Optional[str] = None) -> List[Diagnostic]:
    """Return vocabulary errors and lint warnings for a parsed project."""
    diags = _check_errors(project, cfg)
    if assets_dir:
        diags += _check_assets(project, cfg, assets_dir)
    diags += _lint(project, cfg)
    return diags


def _stats(project: Project, cfg: Dict[str, Any]) -> Dict[str, Any]:
    segs = [s for sc in project.scenes for s in sc.segments]
    words = sum(s.word_count for s in segs)
    silence = sum((s.pause_before or 0) + s.gap_after_s for s in segs)
    est = words / cfg["words_per_minute"] * 60 + silence
    cues = Counter(c.type for s in segs for c in s.cues)
    return {"scenes": len(project.scenes), "segments": len(segs), "words": words,
            "est_seconds": est, "cues": cues}


def render_report(source_name: str, result: ParseResult, extra: List[Diagnostic],
                  cfg: Dict[str, Any], assets_checked: bool = False) -> str:
    """Human-readable validation report (spec section 28)."""
    diags = list(result.diagnostics) + list(extra)
    errors = [d for d in diags if d.level == "error"]
    warnings = [d for d in diags if d.level == "warning"]
    codes = {d.code for d in errors}
    p = result.project
    st = _stats(p, cfg)

    def mark(group: Iterable[str]) -> str:
        return "x" if codes & set(group) else "v"

    lines = [f"Validation report: {source_name}", "=" * 60]
    lines.append(f"  [{mark(CODES_FRONT)}] front matter valid")
    lines.append(f"  [{mark(CODES_SCENES)}] all scenes parsed ({st['scenes']} scenes, {st['segments']} segments)")
    lines.append(f"  [{mark(CODES_LINES)}] all narration lines valid")
    lines.append(f"  [{mark(CODES_EMOTION)}] all emotion names valid")
    lines.append(f"  [{mark(CODES_NAMES)}] all sfx / bed / image / camera / style names valid")
    if assets_checked:
        lines.append(f"  [{mark(CODES_ASSETS)}] all referenced assets exist")
    else:
        lines.append("  [-] asset check skipped (use --assets DIR)")
    mins, secs = divmod(int(round(st["est_seconds"])), 60)
    cue_txt = ", ".join(f"{k}={v}" for k, v in sorted(st["cues"].items())) or "none"
    lines += ["", f"  Spoken words: {st['words']}   Estimated length: {mins}:{secs:02d}   "
                  f"(target {p.target_minutes} min)", f"  Cues: {cue_txt}"]

    def block(title: str, items: List[Diagnostic]) -> None:
        lines.append("")
        lines.append(f"{title} ({len(items)})")
        lines.append("-" * 60)
        for d in items:
            lines.append(d.format())

    if errors:
        block("ERRORS", errors)
    if warnings:
        block("WARNINGS", warnings)
    lines += ["", "RESULT: " + ("FAILED - fix the errors above" if errors
                                else f"OK ({len(warnings)} warnings)")]
    return "\n".join(lines)