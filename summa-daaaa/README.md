# Video pipeline: parser + narration (Phases 1-3)

Turns a Markdown script written to `docs/SCRIPT_FORMAT_SPEC_v1.1.md` into `project.json`, checks it,
then turns it into per-segment narration audio, `narration.json` and `timeline.json`.

Needs: Python 3.10+, `numpy`, `ffmpeg`, and `kokoro` (only for the real voice). SoundFile is no longer needed.

```bash
python main.py script/nnn.md                  # parse + validate (later: the full build)
python main.py validate script/nnn.md         # human-readable report, exit code 1 on errors
python main.py parse script/nnn.md            # writes projects/nnn/project.json
python main.py validate script/nnn.md --assets assets    # also check sfx / image files exist
python main.py validate script/nnn.md --strict           # warnings fail too
python main.py narrate script/nnn.md          # parse, then synthesize every segment (cached)
python main.py regenerate script/nnn.md scene03_seg04   # redo one segment (or a whole scene: scene03)
python main.py narrate script/nnn.md --backend fake     # dry run, no Kokoro needed
python -m unittest discover -s tests -t .     # run the tests
```

## Layout

```
main.py                    CLI (parse / validate / build)
config/channel_main.json   ALL vocabulary + thresholds for a channel (emotions, sfx types, pauses, lint limits)
parser/schema.py           dataclasses: Project > Scene > Segment > Cue, plus Diagnostic
parser/parser.py           Markdown -> Project (structure + syntax), collects every error it can
parser/validators.py       vocabulary errors, lint warnings, asset checks, the report
parser/config.py           config loading helpers
narration/kokoro.py        raw TTS (the ONLY file that imports Kokoro); backend interface + factory
narration/fake.py          deterministic stand-in for tests and dry runs (not speech)
narration/emotions.py      emotion profile -> speed / pitch / volume (config-driven, modifier blending)
narration/audio.py         trim, fade, gain, mixing, ffmpeg polish, two-pass loudness
narration/wavio.py         16-bit WAV read/write (stdlib + numpy)
narration/narrator.py      orchestration + two-level cache -> audio/, narration.json, timeline.json
narration/timing.py        durations -> exact start/end for segments, pauses, scenes, cues
tests/                     unittest suite + two real script fixtures
docs/                      the script format spec
```

A second channel = a new JSON in `config/` (`--config`), not a code change.

## Errors vs warnings

Errors block `parse` from writing `project.json` (override with `--force`). Warnings are reported only.

| Errors | Meaning |
|---|---|
| `E_FRONT_*` | front matter missing, unclosed, missing key, bad value |
| `E_SCENE_*`, `E_STRAY_TEXT` | bad header, non-consecutive numbers, missing image/prompt/Narration |
| `E_EMOTION_MISSING/MALFORMED/UNKNOWN` | first line untagged, bad tag, name not in config |
| `E_PAUSE_UNKNOWN/ADJACENT` | `[pause:xyz]`, or two pauses with no speech between |
| `E_CUE_*`, `E_BRACE_STRAY`, `E_BRACKET_STRAY` | malformed `{cues}` or stray brackets |
| `E_SFX_UNKNOWN/TYPE`, `E_BED_*`, `E_IMG_UNKNOWN`, `E_CAM_UNKNOWN`, `E_TEXT_*` | names not in config, point/bed mixed up, overlay rules |
| `E_LINE_TOO_LONG`, `E_DIGIT` | over 30 words in a line, digits in spoken text |
| `E_ASSET_MISSING` | with `--assets`: sfx or reaction image file not found |

Warnings (`W_*`): pause density and names, SFX density/repeats/generic cap, visual-cue gaps,
camera spacing and repeated openers, repeated scene endings, scene length, word count vs target,
claims without a source or hedge, unverified claims, sensitive-topic flag, new sounds to source,
symbols in spoken text, quoting problems. Thresholds live in `config/channel_main.json` under `lint`.

## What project.json gives the next stages

* Every spoken line is a **segment** with a stable id (`scene03_seg07`), an emotion (+ optional modifier),
  `text` (captions use this) and `tts_text` (pronunciations applied, Kokoro only).
* Pauses are resolved once: `pause_before`, `pause_after`, and `gap_after_s`, the silence to leave before the
  next segment. An explicit pause suppresses the automatic line gap, so pauses never stack.
  Timeline rule: `start(next) = end(this) + gap_after_s(this) + pause_before(next)`.
* Cues carry `word_index` + `char_offset` into the segment, so once TTS gives word timings a cue time is
  `segment_start + word_start(word_index) + offset_s`. Until then, estimate by character position.
* `settings` snapshots the pause table, line gap and fps, so a build is reproducible from the JSON alone.

## Known limits (deliberate, for later phases)

* The parser does not decide poses, resolve cue times, or apply emotion profiles. Those belong to the
  narration, timeline and animation stages.
* Camera overlap rules (base vs transient) are defined in the spec but only *checked* here (spacing lint);
  resolving them into moves happens in the timeline stage.
* `lint.max_scene_words` is an addition beyond spec v1.1. Set it to `null` to disable.

## Narration stage

```
projects/<name>/
  project.json     from the parser
  audio/           scene01_seg01.wav ...   one working WAV per segment (what later stages read)
                   full_narration.wav      convenience export, loudness-normalized once
  narration.json   per segment: audio file, duration, start, end, emotion, speed, volume, cache key
  timeline.json    scenes, narration, pauses and (estimated) cue events with exact times
  cache/raw/       straight TTS output, keyed by text + voice + speed
  cache/proc/      after trim/fade/polish/gain, keyed by raw key + every processing parameter
```

What triggers what:

| You change | What reruns |
|---|---|
| a pause, overlay, SFX or camera cue | nothing (timeline only) |
| an emotion's `volume`, trim/fade/polish values | re-processing only, no TTS |
| a line's text, an emotion's `speed`, `speed_scale`, the voice | only the affected segments are re-synthesized |

Design decisions (all in `config/channel_main.json`):

* `audio.noise_floor_amp = 0`: gaps are true digital silence (room tone made the polished output hiss).
* Trim `0.003 / 0.06 s` and fade `0.012 s` are the values from the tested `narrate.py`.
* The per-segment ffmpeg chain is `highpass + compressor` only. **`loudnorm` is NOT run per segment** (it would
  flatten whisper vs. shout). It runs once, two-pass and linear, on `full_narration.wav`.
* Emotion `volume` is a static gain applied AFTER the compressor, so a whisper stays a whisper.
* Pitch (`tts.apply_pitch`) is OFF until you have judged the natural voice. Speed jitter (`tts.speed_jitter_pct`)
  is OFF (0). Both are available when you want them.
* `timeline.scene_gap_s = 0.6` is a placeholder. Silence between scenes is
  `max(scene_gap_s - next scene's leading pause, previous scene's trailing pause)`, so pauses never stack.
* `timeline.json` cue times are ESTIMATES (by character position inside the segment, `"estimated": true`).
  Word-level timing replaces them in the captions phase.
* The voice reads `tts_text` (pronunciations applied); captions use `text`.
