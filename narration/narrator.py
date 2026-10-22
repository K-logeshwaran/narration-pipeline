"""Narration orchestration: project.json  ->  segment WAVs + narration.json + timeline.json.

Per segment:

    tts_text --Kokoro--> raw audio --trim--> --fade--> --ffmpeg polish--> --emotion gain--> segment WAV

Two cache levels, both keyed by content hashes, so iteration is fast:

    cache/raw/<key>.wav   straight from the TTS engine (expensive)
                          key = text + backend/voice + speed + sample rate
    cache/proc/<key>.wav  after trim/fade/polish/gain (cheap)
                          key = raw key + every processing parameter

  * Change a pause, an overlay or a camera cue   -> nothing is regenerated.
  * Change an emotion's volume or the trim values -> only re-processing, no TTS.
  * Change a line's text, speed or the voice       -> only that line is re-synthesized.

audio/<segment id>.wav is the working copy the later stages read.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np

from . import audio as A
from .align import word_spans
from .captions import write_ass, write_srt
from .emotions import UnknownEmotion, available, resolve_profile, segment_speed
from .errors import AudioToolError, NarrationError, Problem
from .kokoro import TTSBackend
from .sfx import resolve_sfx_events
from .timing import build_timeline
from .wavio import read_wav, wav_duration, write_wav

KEY_VERSION = "n1"          # bump to invalidate every cache entry after a logic change
NARRATION_VERSION = "1.1"
WORD_TIMING_VERSION = "1.0"
FULL_EXPORT_NAME = "full_narration.wav"
CAPTIONS_PATH = Path("captions") / "subtitles.ass"
CAPTIONS_SRT_PATH = Path("captions") / "subtitles.srt"
WORD_TIMING_NAME = "word_timing.json"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def load_project(path: Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "scenes" not in data or "project" not in data:
        raise ValueError(f"{path} does not look like a project.json (missing 'scenes'/'project').")
    return data


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


@dataclass
class NarrationResult:
    narration: Dict[str, Any]
    timeline: Dict[str, Any]
    stats: Dict[str, int]
    export_path: Optional[Path]
    orphans: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    captions_path: Optional[Path] = None
    srt_path: Optional[Path] = None
    word_timing_path: Optional[Path] = None
    markers_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------

def _preflight(project: Dict[str, Any], cfg: Dict[str, Any], only: Optional[Set[str]]) -> List[Problem]:
    problems: List[Problem] = []
    known: Set[str] = set()
    for sc in project["scenes"]:
        known.add(sc["id"])
        for seg in sc["segments"]:
            known.add(seg["id"])
            if not seg["tts_text"].strip():
                problems.append(Problem("Segment has no text to speak", sc["id"], seg["id"], seg["line"]))
            try:
                resolve_profile(seg["emotion"], seg["modifier"], cfg)
            except UnknownEmotion as e:
                problems.append(Problem(f'Unknown emotion: "{e.name}"', sc["id"], seg["id"], seg["line"],
                                        detail="Available emotions:\n" + ", ".join(available(cfg))))
    for wanted in sorted(only or ()):
        if wanted not in known:
            problems.append(Problem(f'Unknown segment or scene id "{wanted}"',
                                    detail="Use ids like scene03 or scene03_seg07."))
    return problems


# ---------------------------------------------------------------------------
# one segment
# ---------------------------------------------------------------------------

def _process_audio(raw: np.ndarray, sr: int, acfg: Dict[str, Any], chain: str,
                   volume: float) -> Tuple[np.ndarray, float]:
    """Returns (processed audio, trim offset in seconds). The offset is how much
    was cut from the start of the raw audio; token timestamps from the backend
    refer to the raw audio and are shifted left by it."""
    offset = A.trim_offset_s(raw, sr, acfg["trim_threshold"], acfg["trim_keep_s"])
    x = A.trim_silence(raw, sr, acfg["trim_threshold"], acfg["trim_keep_s"])
    x = A.fade_edges(x, sr, acfg["fade_s"], acfg.get("fade_curve", "linear"))
    if chain:
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
            write_wav(src, x, sr)
            A.ffmpeg_filter_file(src, dst, chain, sr)
            x, _ = read_wav(dst)
    return A.apply_gain(x, volume, acfg.get("peak_limit")), offset


# ---------------------------------------------------------------------------
# word-level timing (cached next to the processed audio)
# ---------------------------------------------------------------------------

def _load_tokens(raw_path: Path) -> Optional[List[Tuple[str, float, float]]]:
    side = raw_path.with_suffix(".tokens.json")
    if not side.exists():
        return None
    try:
        data = json.loads(side.read_text("utf-8"))
        return [(t[0], float(t[1]), float(t[2])) for t in data]
    except (ValueError, TypeError, IndexError):
        return None


def _write_tokens(raw_path: Path, tokens: Optional[List[Tuple[str, float, float]]]) -> None:
    side = raw_path.with_suffix(".tokens.json")
    if tokens:
        tmp = side.with_name(side.name + ".tmp")
        tmp.write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, side)
    elif side.exists():
        side.unlink()


def _word_info(seg: Dict[str, Any], proc_path: Path, raw_path: Path, cfg: Dict[str, Any],
               tokens: Optional[List[Tuple[str, float, float]]],
               trim_offset: Optional[float], force: bool,
               pronunciations: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Word spans for one segment, cached in cache/proc/<proc key>.words.json so
    a fully cached run costs nothing. Tokens need the trim offset; when it is
    unknown (processed audio came from cache) it is recomputed from the raw WAV."""
    side = proc_path.with_suffix(".words.json")
    words_key = _hash({"words": seg["words"], "tts_text": seg["tts_text"],
                       "pron": pronunciations or {}})
    if not force and side.exists():
        try:
            data = json.loads(side.read_text("utf-8"))
            if data.get("words_key", words_key) == words_key:
                return {"spans": [tuple(s) for s in data["spans"]], "method": data["method"]}
        except (ValueError, KeyError, TypeError):
            pass                                        # corrupt sidecar: recompute

    if tokens is None:
        tokens = _load_tokens(raw_path)
    if tokens is not None and trim_offset is None:
        if raw_path.exists():
            raw, sr = read_wav(raw_path)
            trim_offset = A.trim_offset_s(raw, sr, cfg["audio"]["trim_threshold"],
                                          cfg["audio"]["trim_keep_s"])
        else:
            tokens = None                               # cannot place tokens without the offset
    audio, sr = read_wav(proc_path)
    spans, method = word_spans(seg["words"], audio, sr, cfg.get("word_timing"),
                               tokens=tokens, trim_offset_s=trim_offset or 0.0,
                               pronunciations=pronunciations)
    info = {"words_key": words_key, "spans": [[round(s, 4), round(e, 4)] for s, e in spans],
            "method": method}
    tmp = side.with_name(side.name + ".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, side)
    return {"spans": [tuple(s) for s in info["spans"]], "method": method}


def _segment(sc: Dict[str, Any], seg: Dict[str, Any], cfg: Dict[str, Any], backend: TTSBackend,
             dirs: Dict[str, Path], polish_on: bool, force: bool,
             pronunciations: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    tts, acfg = cfg["tts"], cfg["audio"]
    sr = int(tts["sample_rate"])
    profile = resolve_profile(seg["emotion"], seg["modifier"], cfg)
    speed = segment_speed(profile, seg["id"], cfg)
    pitch = profile.pitch if tts.get("apply_pitch") else 0.0
    chain = A.build_filter_chain(acfg, polish_on, pitch, sr)
    volume = round(profile.volume, 4)

    raw_key = _hash({"v": KEY_VERSION, "backend": backend.id, "text": seg["tts_text"],
                     "speed": speed, "sr": sr})
    proc_key = _hash({"v": KEY_VERSION, "raw": raw_key,
                      "trim": [acfg["trim_threshold"], acfg["trim_keep_s"]],
                      "fade": [acfg["fade_s"], acfg.get("fade_curve", "linear")],
                      "chain": chain, "volume": volume, "peak": acfg.get("peak_limit")})
    raw_path = dirs["raw"] / f"{raw_key}.wav"
    proc_path = dirs["proc"] / f"{proc_key}.wav"
    where = dict(scene=sc["id"], segment=seg["id"], line=seg["line"])

    status = "cache"
    tokens: Optional[List[Tuple[str, float, float]]] = None
    trim_offset: Optional[float] = None
    if force or not proc_path.exists():
        if not force and raw_path.exists():
            raw, _ = read_wav(raw_path)
            tokens = _load_tokens(raw_path)
            status = "reprocessed"
        else:
            try:
                if hasattr(backend, "synth_timed"):
                    raw, tokens = backend.synth_timed(seg["tts_text"], speed)
                else:
                    raw, tokens = backend.synth(seg["tts_text"], speed), None
                raw = np.asarray(raw, dtype=np.float32)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:                          # wrap with segment context
                raise NarrationError([Problem(f"TTS backend failed: {e}", **where,
                                              detail=f'Text: "{seg["tts_text"]}"')]) from e
            if A.is_silent(raw):
                raise NarrationError([Problem("TTS returned empty or silent audio", **where,
                                              detail=f'Text: "{seg["tts_text"]}"')])
            write_wav(raw_path, raw, sr)
            _write_tokens(raw_path, tokens)
            status = "synthesized"
        out, trim_offset = _process_audio(raw, sr, acfg, chain, volume)
        if A.is_silent(out):
            raise NarrationError([Problem("Audio is empty after trimming", **where,
                                          detail="Check trim_threshold in the channel config.")])
        write_wav(proc_path, out, sr)

    winfo = _word_info(seg, proc_path, raw_path, cfg, tokens, trim_offset, force,
                       pronunciations=pronunciations)
    return {"status": status, "speed": speed, "volume": volume, "pitch": pitch,
            "raw_key": raw_key, "proc_key": proc_key, "proc_path": proc_path,
            "duration": wav_duration(proc_path),
            "word_spans": winfo["spans"], "word_method": winfo["method"]}


# ---------------------------------------------------------------------------
# full narration export
# ---------------------------------------------------------------------------

def _export_full(spans: Dict[str, Tuple[float, float]], audio_dir: Path, total_s: float,
                 cfg: Dict[str, Any], log: Callable[[str], None], warnings: List[str]) -> Path:
    sr = int(cfg["tts"]["sample_rate"])
    ln = cfg["audio"].get("final_loudnorm", {})
    placements = [(start, read_wav(audio_dir / f"{sid}.wav")[0]) for sid, (start, _) in spans.items()]
    mix = A.apply_gain(A.mix_segments(placements, total_s, sr), 1.0, cfg["audio"].get("peak_limit"))
    out = audio_dir / FULL_EXPORT_NAME

    with tempfile.TemporaryDirectory() as tmp:
        raw_mix = Path(tmp) / "mix.wav"
        write_wav(raw_mix, mix, sr)
        if ln.get("enabled", True) and A.ffmpeg_available():
            m = A.loudnorm_two_pass(raw_mix, Path(tmp) / "norm.wav", sr, ln["I"], ln["TP"], ln["LRA"])
            if m is None:
                shutil.copyfile(raw_mix, out)
            else:
                shutil.copyfile(Path(tmp) / "norm.wav", out)
                log(f"  loudness: measured {m['input_i']:.1f} LUFS -> target {ln['I']} LUFS (linear, one pass of gain)")
        else:
            if ln.get("enabled", True):
                warnings.append("ffmpeg not found: full_narration.wav was exported WITHOUT loudness normalization.")
            shutil.copyfile(raw_mix, out)
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def narrate_project(project: Dict[str, Any], project_dir: Path, cfg: Dict[str, Any], backend: TTSBackend,
                    *, only: Optional[Iterable[str]] = None, force: bool = False,
                    polish: Optional[bool] = None, export: bool = True,
                    assets_dir: Optional[Path] = None,
                    log: Callable[[str], None] = print) -> NarrationResult:
    """Generate (or reuse) narration for every segment and write narration.json,
    timeline.json (with resolved SFX events) and captions/subtitles.ass.

    only       segment or scene ids to force-regenerate (everything else comes from cache)
    force      regenerate everything, ignoring both caches
    assets_dir folder with the SFX library; without it sfx file paths are
               conventional and existence is not checked
    """
    only_set = set(only) if only else None
    problems = _preflight(project, cfg, only_set)
    if problems:
        raise NarrationError(problems)

    sr = int(cfg["tts"]["sample_rate"])
    polish_on = cfg["audio"].get("polish", True) if polish is None else polish
    dirs = {"audio": project_dir / "audio", "raw": project_dir / "cache" / "raw",
            "proc": project_dir / "cache" / "proc"}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    prev_keys: Dict[str, str] = {}
    prev_file = project_dir / "narration.json"
    if prev_file.exists():
        try:
            prev_keys = {s["id"]: s["cache_key"] for s in json.loads(prev_file.read_text("utf-8"))["segments"]}
        except (ValueError, KeyError):
            prev_keys = {}

    pronunciations = project.get("meta", {}).get("pronunciations") or {}
    flat = [(sc, seg) for sc in project["scenes"] for seg in sc["segments"]]
    stats: Counter = Counter()
    durations: Dict[str, float] = {}
    infos: Dict[str, Dict[str, Any]] = {}
    t0 = time.time()

    log(f"Narrating {len(flat)} segments with {backend.id} "
        f"(polish {'on' if polish_on else 'off'}) ...")
    for i, (sc, seg) in enumerate(flat, 1):
        forced = force or bool(only_set and (seg["id"] in only_set or sc["id"] in only_set))
        info = _segment(sc, seg, cfg, backend, dirs, polish_on, forced,
                        pronunciations=pronunciations)
        stats[info["status"]] += 1
        durations[seg["id"]] = info["duration"]
        infos[seg["id"]] = info
        dst = dirs["audio"] / f"{seg['id']}.wav"
        if forced or not dst.exists() or prev_keys.get(seg["id"]) != info["proc_key"]:
            _atomic_copy(info["proc_path"], dst)
        log(f"  [{i:>3}/{len(flat)}] {seg['id']:<15} {seg['emotion'] or '-':<16} "
            f"{info['duration']:5.2f}s  {info['status']}")

    word_times = {sid: {"spans": infos[sid]["word_spans"], "method": infos[sid]["word_method"]}
                  for sid in durations}
    timeline, spans = build_timeline(project, durations, cfg, word_times=word_times)

    warnings: List[str] = []
    sfx_events, sfx_warnings = resolve_sfx_events(timeline, cfg, assets_dir)
    timeline["events"] = sfx_events
    warnings.extend(sfx_warnings)

    segments_out = []
    for sc, seg in flat:
        info = infos[seg["id"]]
        start, end = spans[seg["id"]]
        segments_out.append({
            "id": seg["id"], "scene": sc["id"], "audio": f"audio/{seg['id']}.wav",
            "duration": round(info["duration"], 3), "start": round(start, 3), "end": round(end, 3),
            "emotion": seg["emotion"], "modifier": seg["modifier"], "speed": info["speed"],
            "volume": info["volume"], "pitch": info["pitch"], "cache_key": info["proc_key"],
            "text": seg["text"], "timing": info["word_method"],
            "words": [{"w": w, "start": round(start + s, 3), "end": round(start + e, 3)}
                      for w, (s, e) in zip(seg["words"], info["word_spans"])],
        })
    acfg = cfg["audio"]
    narration = {
        "schema_version": NARRATION_VERSION,
        "source_sha256": project.get("source", {}).get("sha256"),
        "backend": backend.id,
        "sample_rate": sr,
        "processing": {"trim_threshold": acfg["trim_threshold"], "trim_keep_s": acfg["trim_keep_s"],
                       "fade_s": acfg["fade_s"], "fade_curve": acfg.get("fade_curve", "linear"),
                       "polish": polish_on, "ffmpeg_filter": acfg["ffmpeg_filter"] if polish_on else "",
                       "peak_limit": acfg.get("peak_limit")},
        "total_duration": timeline["duration"],
        "segments": segments_out,
    }

    captions_path: Optional[Path] = None
    srt_path: Optional[Path] = None
    if cfg.get("captions", {}).get("enabled", True):
        captions_path = write_ass(project, segments_out, cfg, project_dir / CAPTIONS_PATH)
        srt_path = write_srt(segments_out, cfg, project_dir / CAPTIONS_SRT_PATH)
        narration["captions"] = str(CAPTIONS_PATH)
        narration["captions_srt"] = str(CAPTIONS_SRT_PATH)

    word_timing_data = {
        "schema_version": WORD_TIMING_VERSION,
        "source_sha256": project.get("source", {}).get("sha256"),
        "backend": backend.id,
        "total_duration": timeline["duration"],
        "segments": [
            {
                "id": s["id"],
                "scene": s["scene"],
                "start": s["start"],
                "end": s["end"],
                "duration": s["duration"],
                "text": s["text"],
                "timing": s["timing"],
                "words": s["words"],
            }
            for s in segments_out
        ],
    }
    word_timing_path = project_dir / WORD_TIMING_NAME
    word_timing_path.write_text(_dump(word_timing_data), encoding="utf-8")

    (project_dir / "narration.json").write_text(_dump(narration), encoding="utf-8")
    (project_dir / "timeline.json").write_text(_dump(timeline), encoding="utf-8")

    export_path: Optional[Path] = None
    if export:
        log("Exporting full narration ...")
        export_path = _export_full(spans, dirs["audio"], timeline["duration"], cfg, log, warnings)

    mix_out_name = (cfg.get("mix") or {}).get("out_name", "full_mix.wav")
    expected = {f"{seg['id']}.wav" for _, seg in flat} | {FULL_EXPORT_NAME, mix_out_name}
    orphans = sorted(p.name for p in dirs["audio"].glob("*.wav") if p.name not in expected)
    if orphans:
        warnings.append(f"{len(orphans)} audio files no longer belong to the script "
                        f"(not deleted): {', '.join(orphans[:5])}{' ...' if len(orphans) > 5 else ''}")

    methods = Counter(infos[sid]["word_method"] for _, seg in flat for sid in [seg["id"]])
    log(f"Word timing: {methods.get('tokens', 0)} from tokens, {methods.get('aligned', 0)} aligned, "
        f"{methods.get('estimated', 0)} estimated.")
    mins, secs = divmod(timeline["duration"], 60)
    log(f"Done in {time.time() - t0:.1f}s: {stats['synthesized']} synthesized, "
        f"{stats['reprocessed']} reprocessed, {stats['cache']} from cache. "
        f"Narration length {int(mins)}:{secs:04.1f}")
    return NarrationResult(narration, timeline, dict(stats), export_path, orphans, warnings,
                           captions_path, srt_path=srt_path, word_timing_path=word_timing_path)
