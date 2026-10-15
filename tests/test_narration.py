import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

import main
from narration import NarrationError, build_timeline, narrate_project
from narration import audio as A
from narration.emotions import UnknownEmotion, jitter_factor, resolve_profile, segment_speed
from narration.fake import FakeBackend
from narration.wavio import read_wav, wav_duration, write_wav
from tests.helpers import CFG, FIXTURES, front_matter, parse_text, read_fixture, scene, script

SR = 24000
HAS_FFMPEG = A.ffmpeg_available()


def project_dict(text):
    res = parse_text(text)
    assert not res.errors, [d.format() for d in res.errors]
    return json.loads(json.dumps(res.project.to_dict()))


def cfg_with(**sections):
    cfg = json.loads(json.dumps(CFG))
    for k, v in sections.items():
        cfg[k].update(v)
    return cfg


def quiet(_msg):
    pass


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# ---------------------------------------------------------------------------
class WavAndAudioTests(TmpDirCase):
    def test_wav_roundtrip(self):
        x = (0.5 * np.sin(np.linspace(0, 100, 5000))).astype(np.float32)
        write_wav(self.dir / "a.wav", x, SR)
        y, sr = read_wav(self.dir / "a.wav")
        self.assertEqual(sr, SR)
        self.assertLess(float(np.max(np.abs(x - y))), 1 / 16000)
        self.assertAlmostEqual(wav_duration(self.dir / "a.wav"), 5000 / SR, places=6)
        self.assertFalse((self.dir / "a.wav.tmp").exists())          # atomic write left no temp file

    def test_trim_keeps_a_natural_pad(self):
        dead = np.full(SR // 2, 0.0005, dtype=np.float32)
        voice = (0.3 * np.sin(np.linspace(0, 2000, SR))).astype(np.float32)
        x = np.concatenate([dead, voice, dead])
        y = A.trim_silence(x, SR, threshold=0.003, keep_s=0.06)
        pad = int(SR * 0.06)
        self.assertAlmostEqual(len(y), len(voice) + 2 * pad, delta=40)   # sine zero-crossings at the edges
        self.assertLess(len(y), len(x))

    def test_trim_leaves_all_quiet_audio_alone(self):
        x = np.full(1000, 0.001, dtype=np.float32)
        self.assertEqual(len(A.trim_silence(x, SR, 0.003, 0.06)), 1000)

    def test_fade_edges(self):
        x = np.ones(SR, dtype=np.float32)
        for curve in ("linear", "cosine"):
            y = A.fade_edges(x, SR, 0.012, curve)
            self.assertEqual((float(y[0]), float(y[-1])), (0.0, 0.0))
            self.assertEqual(float(y[SR // 2]), 1.0)
        self.assertEqual(len(A.fade_edges(np.ones(10, dtype=np.float32), SR, 0.012)), 10)   # too short: untouched

    def test_gain_and_peak_guard(self):
        x = np.array([0.0, 0.5, -0.9], dtype=np.float32)
        self.assertAlmostEqual(float(np.max(np.abs(A.apply_gain(x, 0.5)))), 0.45, places=5)
        y = A.apply_gain(x, 2.0, peak_limit=0.98)
        self.assertAlmostEqual(float(np.max(np.abs(y))), 0.98, places=5)

    def test_silence_is_digital_zero_by_default_and_noise_is_seeded(self):
        self.assertFalse(A.silence(0.5, SR).any())
        a, b = A.silence(0.1, SR, 0.001, seed=3), A.silence(0.1, SR, 0.001, seed=3)
        self.assertTrue(np.array_equal(a, b))
        self.assertTrue(a.any())

    def test_is_silent_catches_nan_and_empty(self):
        self.assertTrue(A.is_silent(np.zeros(0, dtype=np.float32)))
        self.assertTrue(A.is_silent(np.array([np.nan, 0.5], dtype=np.float32)))
        self.assertTrue(A.is_silent(np.zeros(100, dtype=np.float32)))
        self.assertFalse(A.is_silent(np.array([0.2], dtype=np.float32)))

    def test_mix_places_clips_at_sample_accurate_offsets(self):
        clip = np.full(1000, 0.5, dtype=np.float32)
        mix = A.mix_segments([(0.5, clip)], total_s=1.0, sr=SR)
        first = int(np.argmax(mix > 0))
        self.assertEqual(first, SR // 2)
        self.assertEqual(int((mix > 0).sum()), 1000)

    def test_mix_clips_that_run_past_the_end(self):
        mix = A.mix_segments([(0.99, np.full(5000, 0.5, dtype=np.float32))], total_s=1.0, sr=SR)
        self.assertEqual(len(mix), SR + 1)

    def test_filter_chain_building(self):
        acfg = CFG["audio"]
        self.assertEqual(A.build_filter_chain(acfg, polish=False, pitch_semitones=0, sr=SR), "")
        chain = A.build_filter_chain(acfg, polish=True, pitch_semitones=0, sr=SR)
        self.assertTrue(chain.startswith("highpass=f=60,acompressor"))
        self.assertNotIn("loudnorm", chain)               # never per segment
        pitched = A.build_filter_chain(acfg, polish=False, pitch_semitones=-1, sr=SR)
        self.assertIn("asetrate=", pitched)
        self.assertIn("atempo=", pitched)

    def test_parse_loudnorm_json(self):
        stderr = ('[Parsed_loudnorm_0 @ 0x55] \n{\n\t"input_i" : "-17.70",\n\t"input_tp" : "-3.10",\n'
                  '\t"input_lra" : "2.10",\n\t"input_thresh" : "-28.00",\n\t"output_i" : "-16.0",\n'
                  '\t"target_offset" : "0.12"\n}\n')
        m = A.parse_loudnorm_json(stderr)
        self.assertEqual((m["input_i"], m["target_offset"]), (-17.7, 0.12))
        with self.assertRaises(Exception):
            A.parse_loudnorm_json("nothing here")


# ---------------------------------------------------------------------------
class EmotionTests(unittest.TestCase):
    def test_profiles_come_from_config(self):
        p = resolve_profile("whisper", None, CFG)
        self.assertEqual((p.speed, p.pitch, p.volume), (0.82, -1.0, 0.65))

    def test_modifier_blends_into_primary(self):
        p = resolve_profile("deadpan", "flat", CFG)          # weight 0.3
        self.assertAlmostEqual(p.speed, 0.88 * 0.7 + 0.86 * 0.3, places=6)
        self.assertAlmostEqual(p.volume, 1.0 * 0.7 + 0.95 * 0.3, places=6)

    def test_unknown_emotion_lists_what_is_available(self):
        with self.assertRaises(UnknownEmotion) as cm:
            resolve_profile("sarcastic_angry", None, CFG)
        self.assertIn("deadpan", cm.exception.available)

    def test_speed_scale_and_clamp(self):
        p = resolve_profile("deadpan", None, CFG)
        self.assertEqual(segment_speed(p, "s", cfg_with(tts={"speed_scale": 1.1})), round(0.88 * 1.1, 4))
        self.assertEqual(segment_speed(p, "s", cfg_with(tts={"speed_scale": 10})), 2.0)
        self.assertEqual(segment_speed(p, "s", cfg_with(tts={"speed_scale": 0.1})), 0.5)

    def test_jitter_is_deterministic_and_bounded(self):
        self.assertEqual(jitter_factor("scene01_seg01", 0), 1.0)
        a = jitter_factor("scene01_seg01", 3)
        self.assertEqual(a, jitter_factor("scene01_seg01", 3))
        vals = [jitter_factor(f"scene01_seg{i:02d}", 3) for i in range(200)]
        self.assertTrue(all(0.97 <= v <= 1.03 for v in vals))
        self.assertGreater(len(set(vals)), 100)


# ---------------------------------------------------------------------------
class TimelineTests(unittest.TestCase):
    def build(self, text, durs, cfg=CFG):
        proj = project_dict(text)
        ids = [s["id"] for sc in proj["scenes"] for s in sc["segments"]]
        durations = dict(zip(ids, durs))
        tl, spans = build_timeline(proj, durations, cfg)
        return tl, spans

    def test_pauses_never_stack_with_the_line_gap(self):
        text = script("(flat) Aaa bbb.\n[pause]\nCcc ddd.\n(flat) [pause:beat] Eee fff.")
        tl, spans = self.build(text, [1.0, 2.0, 3.0])
        self.assertEqual(spans["scene01_seg01"], (0.0, 1.0))
        self.assertAlmostEqual(spans["scene01_seg02"][0], 1.35)       # explicit pause only
        self.assertAlmostEqual(spans["scene01_seg03"][0], 3.35 + 0.45)   # leading pause only, no 0.22 gap
        self.assertAlmostEqual(tl["duration"], 3.8 + 3.0)

    def test_plain_lines_get_the_automatic_gap(self):
        tl, spans = self.build(script("(flat) Aaa bbb.\nCcc ddd."), [1.0, 1.0])
        self.assertAlmostEqual(spans["scene01_seg02"][0], 1.22)
        kinds = [e["kind"] for e in tl["events"] if e["type"] == "pause"]
        self.assertEqual(kinds, ["line_gap"])

    def test_scene_boundary_uses_gap_when_nothing_explicit(self):
        text = front_matter() + scene(1, "(flat) Aaa bbb.") + scene(2, "(flat) Ccc ddd.")
        tl, spans = self.build(text, [1.0, 1.0])
        self.assertAlmostEqual(spans["scene02_seg01"][0], 1.6)       # 1.0 + scene_gap 0.6
        self.assertEqual([(s["id"], s["start"], s["end"]) for s in tl["scenes"]],
                         [("scene01", 0.0, 1.6), ("scene02", 1.6, 2.6)])

    def test_trailing_explicit_pause_absorbs_the_scene_gap(self):
        text = front_matter() + scene(1, "(flat) Aaa bbb.\n[pause:long]") + scene(2, "(flat) Ccc ddd.")
        _, spans = self.build(text, [1.0, 1.0])
        self.assertAlmostEqual(spans["scene02_seg01"][0], 1.0 + 0.8)     # max(0.6, 0.8), NOT 1.4

    def test_leading_pause_of_next_scene_is_part_of_the_gap(self):
        text = front_matter() + scene(1, "(flat) Aaa bbb.") + scene(2, "(flat) [pause] Ccc ddd.")
        tl, spans = self.build(text, [1.0, 1.0])
        self.assertAlmostEqual(spans["scene02_seg01"][0], 1.0 + 0.6)     # total silence 0.6, not 0.95
        self.assertAlmostEqual(tl["scenes"][1]["start"], 1.0 + 0.25)     # new image appears at the cut

    def test_trailing_pause_on_the_last_segment_counts_toward_duration(self):
        tl, _ = self.build(script("(flat) Aaa bbb.\n[pause:long]"), [2.0])
        self.assertAlmostEqual(tl["duration"], 2.8)

    def test_cue_times_are_estimates_inside_the_segment(self):
        tl, spans = self.build(script("(flat) One two {sfx:pop+0.5} three four."), [4.0])
        cue = next(e for e in tl["events"] if e["type"] == "cue")
        self.assertTrue(cue["estimated"])
        self.assertAlmostEqual(cue["start"], 4.0 * 8 / len("One two three four.") + 0.5, places=3)
        self.assertEqual(cue["offset_s"], 0.5)

    def test_events_are_sorted_and_scenes_tile_the_timeline(self):
        text = read_fixture("nnn.md")
        proj = project_dict(text)
        durs = {s["id"]: 3.0 for sc in proj["scenes"] for s in sc["segments"]}
        tl, _ = build_timeline(proj, durs, CFG)
        starts = [e["start"] for e in tl["events"]]
        self.assertEqual(starts, sorted(starts))
        scenes = tl["scenes"]
        for a, b in zip(scenes, scenes[1:]):
            self.assertEqual(a["end"], b["start"])
        self.assertEqual(scenes[-1]["end"], tl["duration"])


# ---------------------------------------------------------------------------
class NarratorTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        text = front_matter() + scene(1, "(deadpan) First line here.\n(whisper) Second line here.\n"
                                         "(smug) Third line here.")
        self.proj = project_dict(text)
        self.backend = FakeBackend()

    def run_narration(self, cfg=CFG, backend=None, **kw):
        kw.setdefault("polish", False)
        kw.setdefault("export", False)
        return narrate_project(self.proj, self.dir, cfg, backend or self.backend, log=quiet, **kw)

    def test_first_run_synthesizes_everything_and_writes_outputs(self):
        res = self.run_narration()
        self.assertEqual(res.stats, {"synthesized": 3})
        self.assertEqual(len(self.backend.calls), 3)
        for sid in ("scene01_seg01", "scene01_seg02", "scene01_seg03"):
            self.assertTrue((self.dir / "audio" / f"{sid}.wav").exists())
        narr = json.loads((self.dir / "narration.json").read_text())
        self.assertEqual([s["emotion"] for s in narr["segments"]], ["deadpan", "whisper", "smug"])
        self.assertEqual(narr["segments"][0]["audio"], "audio/scene01_seg01.wav")
        self.assertTrue((self.dir / "timeline.json").exists())

    def test_second_run_is_all_cache(self):
        self.run_narration()
        self.backend.calls.clear()
        res = self.run_narration()
        self.assertEqual(res.stats, {"cache": 3})
        self.assertEqual(self.backend.calls, [])

    def test_volume_change_reprocesses_without_resynthesizing(self):
        self.run_narration()
        self.backend.calls.clear()
        res = self.run_narration(cfg=cfg_with(emotions={"whisper": {"speed": 0.82, "pitch": -1, "volume": 0.4}}))
        self.assertEqual(res.stats, {"reprocessed": 1, "cache": 2})
        self.assertEqual(self.backend.calls, [])

    def test_trim_change_reprocesses_every_segment_but_never_resynthesizes(self):
        self.run_narration()
        self.backend.calls.clear()
        res = self.run_narration(cfg=cfg_with(audio={"trim_keep_s": 0.1}))
        self.assertEqual(res.stats, {"reprocessed": 3})
        self.assertEqual(self.backend.calls, [])

    def test_speed_change_resynthesizes_only_what_changed(self):
        self.run_narration()
        self.backend.calls.clear()
        res = self.run_narration(cfg=cfg_with(emotions={"smug": {"speed": 0.5, "pitch": -1, "volume": 1.0}}))
        self.assertEqual(res.stats, {"synthesized": 1, "cache": 2})
        self.assertEqual(len(self.backend.calls), 1)

    def test_text_change_resynthesizes_only_that_segment(self):
        self.run_narration()
        self.backend.calls.clear()
        self.proj["scenes"][0]["segments"][1]["tts_text"] = "A different second line."
        res = self.run_narration()
        self.assertEqual(res.stats, {"synthesized": 1, "cache": 2})

    def test_different_voice_does_not_reuse_cache(self):
        self.run_narration()

        class OtherVoice(FakeBackend):
            id = "fake:other"

        res = self.run_narration(backend=OtherVoice())
        self.assertEqual(res.stats, {"synthesized": 3})

    def test_only_forces_just_the_named_segment(self):
        self.run_narration()
        self.backend.calls.clear()
        res = self.run_narration(only=["scene01_seg02"])
        self.assertEqual(res.stats, {"synthesized": 1, "cache": 2})
        res = self.run_narration(only=["scene01"])
        self.assertEqual(res.stats, {"synthesized": 3})

    def test_force_regenerates_everything(self):
        self.run_narration()
        self.assertEqual(self.run_narration(force=True).stats, {"synthesized": 3})

    def test_unknown_only_id_is_an_error(self):
        with self.assertRaises(NarrationError) as cm:
            self.run_narration(only=["scene07_seg01"])
        self.assertIn("scene07_seg01", str(cm.exception))

    def test_unknown_emotion_error_format(self):
        self.proj["scenes"][0]["segments"][1]["emotion"] = "sarcastic_angry"
        with self.assertRaises(NarrationError) as cm:
            self.run_narration()
        msg = str(cm.exception)
        self.assertIn("scene01 / narration segment 02", msg)
        self.assertIn('Unknown emotion: "sarcastic_angry"', msg)
        self.assertIn("Available emotions:", msg)
        self.assertEqual(self.backend.calls, [])                    # failed before any TTS work

    def test_empty_audio_error_names_the_segment(self):
        class Silent(FakeBackend):
            def synth_timed(self, text, speed):
                return np.zeros(0, dtype=np.float32), None

        with self.assertRaises(NarrationError) as cm:
            self.run_narration(backend=Silent())
        self.assertIn("empty or silent", str(cm.exception))
        self.assertIn("scene01 / narration segment 01", str(cm.exception))

    def test_backend_exception_is_wrapped_with_context(self):
        class Broken(FakeBackend):
            def synth_timed(self, text, speed):
                raise RuntimeError("model exploded")

        with self.assertRaises(NarrationError) as cm:
            self.run_narration(backend=Broken())
        self.assertIn("model exploded", str(cm.exception))
        self.assertIn("scene01 / narration segment 01", str(cm.exception))

    def test_failed_run_resumes_from_cache(self):
        class FailsOnThird(FakeBackend):
            def synth_timed(self, text, speed):
                if len(self.calls) == 2:
                    raise RuntimeError("out of memory")
                return super().synth_timed(text, speed)

        with self.assertRaises(NarrationError):
            self.run_narration(backend=FailsOnThird())
        self.assertFalse((self.dir / "narration.json").exists())      # no half-written metadata
        fresh = FakeBackend()
        res = self.run_narration(backend=fresh)
        self.assertEqual(res.stats, {"cache": 2, "synthesized": 1})
        self.assertEqual(len(fresh.calls), 1)

    def test_tts_text_is_what_gets_spoken(self):
        self.proj["scenes"][0]["segments"][0]["tts_text"] = "EN-EN-EN rules."
        self.run_narration()
        self.assertEqual(self.backend.calls[0][0], "EN-EN-EN rules.")

    def test_outputs_are_byte_identical_between_runs(self):
        other = self.dir / "other"
        self.run_narration()
        narrate_project(self.proj, other, CFG, FakeBackend(), polish=False, export=False, log=quiet)
        for name in ("narration.json", "timeline.json", "audio/scene01_seg02.wav"):
            self.assertEqual((self.dir / name).read_bytes(), (other / name).read_bytes(), name)

    def test_orphan_audio_is_reported_not_deleted(self):
        self.run_narration()
        (self.dir / "audio" / "scene09_seg99.wav").write_bytes(b"x")
        res = self.run_narration()
        self.assertEqual(res.orphans, ["scene09_seg99.wav"])
        self.assertTrue((self.dir / "audio" / "scene09_seg99.wav").exists())

    def test_deleted_working_copy_is_restored_from_cache(self):
        self.run_narration()
        (self.dir / "audio" / "scene01_seg02.wav").unlink()
        self.backend.calls.clear()
        self.run_narration()
        self.assertTrue((self.dir / "audio" / "scene01_seg02.wav").exists())
        self.assertEqual(self.backend.calls, [])

    def test_durations_in_narration_json_match_the_wav_files(self):
        self.run_narration()
        narr = json.loads((self.dir / "narration.json").read_text())
        for s in narr["segments"]:
            self.assertAlmostEqual(s["duration"], wav_duration(self.dir / s["audio"]), places=3)
            self.assertAlmostEqual(s["end"] - s["start"], s["duration"], places=2)

    def test_trimmed_segments_are_shorter_than_raw_tts(self):
        self.run_narration()
        raw_len = len(FakeBackend().synth("First line here.", 0.88)) / SR
        self.assertLess(wav_duration(self.dir / "audio" / "scene01_seg01.wav"), raw_len - 0.3)

    def test_whisper_is_quieter_than_deadpan(self):
        self.run_narration()
        peak = lambda sid: float(np.max(np.abs(read_wav(self.dir / "audio" / f"{sid}.wav")[0])))
        self.assertLess(peak("scene01_seg02"), peak("scene01_seg01") * 0.8)


# ---------------------------------------------------------------------------
@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class FfmpegTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        text = front_matter() + scene(1, "(deadpan) Aaa bbb ccc.\n(whisper) Ddd eee fff.") \
            + scene(2, "(smug) Ggg hhh iii.\n[pause:long]")
        self.proj = project_dict(text)

    def test_polish_runs_and_keeps_duration(self):
        a, b = self.dir / "a", self.dir / "b"
        narrate_project(self.proj, a, CFG, FakeBackend(), polish=False, export=False, log=quiet)
        narrate_project(self.proj, b, CFG, FakeBackend(), polish=True, export=False, log=quiet)
        d1 = wav_duration(a / "audio" / "scene01_seg01.wav")
        d2 = wav_duration(b / "audio" / "scene01_seg01.wav")
        self.assertAlmostEqual(d1, d2, delta=0.02)
        self.assertNotEqual((a / "audio" / "scene01_seg01.wav").read_bytes(),
                            (b / "audio" / "scene01_seg01.wav").read_bytes())

    def test_full_export_matches_timeline_and_is_normalized(self):
        res = narrate_project(self.proj, self.dir, CFG, FakeBackend(), log=quiet)
        self.assertEqual(res.export_path.name, "full_narration.wav")
        self.assertAlmostEqual(wav_duration(res.export_path), res.timeline["duration"], delta=0.05)
        out = A._run_ffmpeg(["-i", str(res.export_path), "-af", "ebur128=peak=true", "-f", "null", "-"])
        import re
        lufs = float(re.findall(r"I:\s+(-?\d+\.\d+) LUFS", out.stderr)[-1])
        self.assertAlmostEqual(lufs, -16.0, delta=1.5)

    def test_export_places_segments_where_the_timeline_says(self):
        no_norm = cfg_with(audio={"final_loudnorm": {"enabled": False}})
        res = narrate_project(self.proj, self.dir, no_norm, FakeBackend(), polish=False, log=quiet)
        mix, _ = read_wav(res.export_path)
        seg = read_wav(self.dir / "audio" / "scene02_seg01.wav")[0]
        start = next(e for e in res.timeline["events"] if e.get("id") == "scene02_seg01")["start"]
        i = int(round(start * SR))
        # timeline.json is rounded to 1 ms (<= 12 samples); the mix itself uses exact times
        best = min(range(-14, 15), key=lambda d: float(np.max(np.abs(mix[i + d: i + d + len(seg)] - seg))))
        self.assertLess(float(np.max(np.abs(mix[i + best: i + best + len(seg)] - seg))), 1e-3)
        self.assertLessEqual(abs(best), 12)
        gap = mix[int((start - 0.2) * SR): i - 14]
        self.assertFalse(gap.any())                                 # true digital silence in the gap


# ---------------------------------------------------------------------------
def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main.main(argv)
    return code, out.getvalue(), err.getvalue()


class NarrateCliTests(TmpDirCase):
    def test_narrate_from_script_with_fake_backend(self):
        code, out, _ = run_cli(["narrate", os.path.join(FIXTURES, "example_v11.md"), "--backend", "fake",
                                "--no-polish", "--no-export", "-o", str(self.dir)])
        self.assertEqual(code, 0)
        self.assertIn("12 synthesized", out)
        for name in ("project.json", "narration.json", "timeline.json"):
            self.assertTrue((self.dir / name).exists(), name)

    def test_narrate_from_existing_project_json(self):
        run_cli(["parse", os.path.join(FIXTURES, "example_v11.md"), "-o", str(self.dir)])
        code, out, _ = run_cli(["narrate", str(self.dir / "project.json"), "--backend", "fake",
                                "--no-polish", "--no-export"])
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "narration.json").exists())

    def test_regenerate_only_touches_named_segments(self):
        base = ["--backend", "fake", "--no-polish", "--no-export", "-o", str(self.dir)]
        script_path = os.path.join(FIXTURES, "example_v11.md")
        run_cli(["narrate", script_path, *base])
        code, out, _ = run_cli(["regenerate", script_path, "scene02_seg03", *base])
        self.assertEqual(code, 0)
        self.assertIn("1 synthesized", out)
        self.assertIn("11 from cache", out)

    def test_script_errors_stop_before_narration(self):
        bad = self.dir / "bad.md"
        bad.write_text(script("(nope) Hello there."), encoding="utf-8")
        code, out, _ = run_cli(["narrate", str(bad), "--backend", "fake", "-o", str(self.dir / "o")])
        # an unknown emotion is a validation error, so narration must not start
        self.assertEqual(code, 1)
        self.assertIn("Fix the errors above", out)
        self.assertFalse((self.dir / "o" / "audio").exists())

    def test_unknown_emotion_in_project_json_is_reported_cleanly(self):
        run_cli(["parse", os.path.join(FIXTURES, "example_v11.md"), "-o", str(self.dir)])
        path = self.dir / "project.json"
        data = json.loads(path.read_text())
        data["scenes"][0]["segments"][0]["emotion"] = "sarcastic_angry"
        path.write_text(json.dumps(data))
        code, _, err = run_cli(["narrate", str(path), "--backend", "fake", "--no-polish", "--no-export"])
        self.assertEqual(code, 1)
        self.assertIn("Available emotions:", err)


if __name__ == "__main__":
    unittest.main()
