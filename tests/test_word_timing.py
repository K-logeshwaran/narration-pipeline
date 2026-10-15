import json
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np

from narration import build_timeline, narrate_project
from narration import captions as C
from narration.align import spans_from_tokens, speech_islands, word_spans
from narration.fake import FakeBackend
from narration.sfx import resolve_sfx_events
from narration.wavio import write_wav
from tests.helpers import CFG, front_matter, scene, script
from tests.test_narration import TmpDirCase, cfg_with, project_dict, quiet

SR = 24000
WCFG = CFG["word_timing"]


def bursts(*spans, sr=SR):
    """Concatenated sine bursts: each (gap_before_s, duration_s)."""
    parts = []
    for gap, dur in spans:
        parts.append(np.zeros(int(sr * gap), dtype=np.float32))
        n = int(sr * dur)
        t = np.arange(n, dtype=np.float32) / sr
        parts.append((0.3 * np.sin(2 * np.pi * 180 * t)).astype(np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


# ---------------------------------------------------------------------------
class IslandTests(unittest.TestCase):
    def test_finds_each_burst(self):
        audio = bursts((0.2, 0.3), (0.3, 0.25), (0.4, 0.2))
        islands = speech_islands(audio, SR, WCFG)
        self.assertEqual(len(islands), 3)
        self.assertAlmostEqual(islands[0][0], 0.2, delta=0.02)
        self.assertAlmostEqual(islands[1][0], 0.8, delta=0.02)
        self.assertAlmostEqual(islands[2][1], 1.65, delta=0.03)

    def test_merges_close_islands_and_drops_tiny_ones(self):
        audio = bursts((0.1, 0.2), (0.02, 0.2), (0.5, 0.01))   # 20 ms gap merges, 10 ms blip drops
        islands = speech_islands(audio, SR, WCFG)
        self.assertEqual(len(islands), 1)
        self.assertAlmostEqual(islands[0][1], 0.52, delta=0.03)

    def test_silence_has_no_islands(self):
        self.assertEqual(speech_islands(np.zeros(SR, dtype=np.float32), SR, WCFG), [])
        self.assertEqual(speech_islands(np.zeros(10, dtype=np.float32), SR, WCFG), [])


# ---------------------------------------------------------------------------
class TokenTests(unittest.TestCase):
    def test_tokens_map_to_words_and_shift_by_trim_offset(self):
        words = ["One", "two,", "three!"]
        tokens = [("One", 0.25, 0.55), ("two,", 0.60, 0.90), ("three!", 0.95, 1.40)]
        spans = spans_from_tokens(words, tokens, trim_offset_s=0.19, duration=1.30)
        self.assertAlmostEqual(spans[0][0], 0.06, places=4)
        self.assertAlmostEqual(spans[2][1], 1.21, places=4)

    def test_multi_token_words_and_punctuation_tokens(self):
        words = ["don't", "stop"]
        tokens = [("do", 0.1, 0.2), ("n't", 0.2, 0.35), (",", 0.35, 0.36), ("stop", 0.4, 0.7)]
        spans = spans_from_tokens(words, tokens, 0.0, 1.0)
        self.assertEqual(spans, [(0.1, 0.35), (0.4, 0.7)])

    def test_mismatched_tokens_return_none(self):
        self.assertIsNone(spans_from_tokens(["alpha", "beta"], [("gamma", 0.0, 0.5)], 0.0, 1.0))
        self.assertIsNone(spans_from_tokens(["alpha"], [], 0.0, 1.0))

    def test_spans_are_clamped_to_segment(self):
        spans = spans_from_tokens(["late"], [("late", 0.5, 2.0)], trim_offset_s=0.6, duration=1.0)
        self.assertEqual(spans, [(0.0, 1.0)])


# ---------------------------------------------------------------------------
class WordSpanMethodTests(unittest.TestCase):
    def test_matching_islands_give_aligned(self):
        audio = bursts((0.1, 0.3), (0.3, 0.3))
        spans, method = word_spans(["aaa", "bbb"], audio, SR, WCFG)
        self.assertEqual(method, "aligned")
        self.assertAlmostEqual(spans[1][0], 0.7, delta=0.02)

    def test_mismatched_islands_fall_back_to_estimated_over_speech_span(self):
        audio = bursts((0.2, 0.8))                             # one island, two words
        spans, method = word_spans(["aaaa", "bb"], audio, SR, WCFG)
        self.assertEqual(method, "estimated")
        self.assertAlmostEqual(spans[0][0], 0.2, delta=0.02)     # anchored to speech, not 0
        self.assertAlmostEqual(spans[1][1], 1.0, delta=0.03)
        self.assertGreater(spans[0][1] - spans[0][0], spans[1][1] - spans[1][0])  # longer word, longer span

    def test_tokens_win_over_audio(self):
        audio = bursts((0.0, 1.0))
        spans, method = word_spans(["aaa", "bbb"], audio, SR, WCFG,
                                   tokens=[("aaa", 0.1, 0.2), ("bbb", 0.5, 0.6)])
        self.assertEqual(method, "tokens")
        self.assertEqual(spans, [(0.1, 0.2), (0.5, 0.6)])


# ---------------------------------------------------------------------------
class TimelineWordTests(unittest.TestCase):
    def build(self, text, durs, word_times=None):
        proj = project_dict(text)
        ids = [s["id"] for sc in proj["scenes"] for s in sc["segments"]]
        return build_timeline(proj, dict(zip(ids, durs)), CFG, word_times=word_times)

    def test_cue_lands_on_its_word_not_the_character_estimate(self):
        text = script("(flat) One two {sfx:pop+0.5} three four.")
        wt = {"scene01_seg01": {"spans": [(0.0, 0.4), (0.5, 0.9), (1.2, 1.6), (1.7, 2.0)],
                                "method": "aligned"}}
        tl, _ = self.build(text, [4.0], wt)
        cue = next(e for e in tl["events"] if e["type"] == "cue")
        self.assertFalse(cue["estimated"])
        self.assertAlmostEqual(cue["start"], 1.2 + 0.5, places=3)

    def test_cue_at_segment_end_uses_last_word_end(self):
        text = script("(flat) One two {sfx:pop}\n[pause]")
        proj = project_dict(text)
        seg = proj["scenes"][0]["segments"][0]
        self.assertEqual(seg["cues"][0]["word_index"], seg["word_count"])
        wt = {seg["id"]: {"spans": [(0.0, 0.4), (0.5, 0.9)], "method": "tokens"}}
        tl, _ = build_timeline(proj, {seg["id"]: 2.0}, CFG, word_times=wt)
        cue = next(e for e in tl["events"] if e["type"] == "cue")
        self.assertAlmostEqual(cue["start"], 0.9, places=3)

    def test_estimated_method_keeps_the_flag(self):
        text = script("(flat) One two {sfx:pop} three.")
        wt = {"scene01_seg01": {"spans": [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5)], "method": "estimated"}}
        tl, _ = self.build(text, [1.5], wt)
        cue = next(e for e in tl["events"] if e["type"] == "cue")
        self.assertTrue(cue["estimated"])
        self.assertAlmostEqual(cue["start"], 1.0, places=3)      # still word-based, just flagged

    def test_no_word_times_keeps_old_estimate(self):
        tl, _ = self.build(script("(flat) One two {sfx:pop} three four."), [4.0])
        cue = next(e for e in tl["events"] if e["type"] == "cue")
        self.assertTrue(cue["estimated"])
        self.assertAlmostEqual(cue["start"], 4.0 * 8 / len("One two three four."), places=3)


# ---------------------------------------------------------------------------
class SfxTests(unittest.TestCase):
    def timeline(self):
        text = (front_matter()
                + scene(1, "(flat) One two {sfx:pop+0.2} three.\n(smug) {bed:ticking_clock} Four five.\n"
                           "Six {bed:end} seven.")
                + scene(2, "(flat) {bed:lab_bubbling} Eight nine."))
        proj = project_dict(text)
        ids = [s["id"] for sc in proj["scenes"] for s in sc["segments"]]
        tl, _ = build_timeline(proj, {i: 2.0 for i in ids}, CFG)
        return tl

    def test_point_sfx_is_resolved(self):
        tl = self.timeline()
        events, warnings = resolve_sfx_events(tl, CFG)
        pop = next(e for e in events if e["type"] == "sfx")
        self.assertEqual(pop["kind"], "point")
        self.assertEqual(pop["name"], "pop")
        self.assertEqual(pop["file"], "sfx/pop.wav")
        self.assertEqual(pop["offset_s"], 0.2)
        self.assertNotIn("exists", pop)                          # no assets dir: nothing checked
        self.assertEqual(warnings, [])
        self.assertFalse(any(e["type"] == "cue" and e.get("cue_type") == "sfx" for e in events))

    def test_bed_runs_until_bed_end_then_scene_end(self):
        tl = self.timeline()
        events, _ = resolve_sfx_events(tl, CFG)
        beds = [e for e in events if e["type"] == "sfx" and e["kind"] == "bed"]
        self.assertEqual(len(beds), 2)
        end_cue_start = next(e["start"] for e in tl["events"]
                             if e["type"] == "cue" and e.get("name") == "end")
        self.assertEqual(beds[0]["end"], end_cue_start)                      # closed by {bed:end}
        self.assertEqual(beds[1]["end"], tl["scenes"][1]["end"])             # no end cue: scene end
        self.assertFalse(any(e.get("name") == "end" for e in events))        # end cue consumed

    def test_missing_and_present_files_with_assets_dir(self):
        tl = self.timeline()
        with tempfile.TemporaryDirectory() as tmp:
            sfx_dir = Path(tmp) / "sfx"
            sfx_dir.mkdir()
            write_wav(sfx_dir / "pop.wav", np.zeros(SR // 2, dtype=np.float32), SR)
            events, warnings = resolve_sfx_events(tl, CFG, Path(tmp))
            pop = next(e for e in events if e.get("name") == "pop")
            self.assertTrue(pop["exists"])
            self.assertEqual(pop["duration_s"], 0.5)
            bed = next(e for e in events if e.get("name") == "ticking_clock")
            self.assertFalse(bed["exists"])
            self.assertTrue(any("ticking_clock" in w for w in warnings))

    def test_events_stay_sorted(self):
        tl = self.timeline()
        events, _ = resolve_sfx_events(tl, CFG)
        starts = [e["start"] for e in events]
        self.assertEqual(starts, sorted(starts))


# ---------------------------------------------------------------------------
class CaptionTests(unittest.TestCase):
    def test_ass_time_format(self):
        self.assertEqual(C.ass_time(0), "0:00:00.00")
        self.assertEqual(C.ass_time(3661.259), "1:01:01.25")

    def test_sentence_split_and_chunking(self):
        words = "Aaa bbb. Ccc ddd eee fff ggg hhh iii jjj.".split()
        chunks = C._chunks(words, 8)
        self.assertEqual(chunks, [(0, 2), (2, 10)])               # sentence break wins
        chunks = C._chunks("one two three four five six seven eight nine".split(), 4)
        self.assertEqual(chunks, [(0, 4), (4, 8), (8, 9)])

    def test_karaoke_durations_tile_the_line(self):
        segs = [{"text": "One two three.", "start": 1.0, "end": 3.0,
                 "words": [{"w": "One", "start": 1.0, "end": 1.4},
                           {"w": "two", "start": 1.5, "end": 1.9},
                           {"w": "three.", "start": 2.0, "end": 2.6}]}]
        dlg = C.build_dialogues(segs, {"tail_pad_s": 0.1, "max_words_per_line": 8})[0]
        self.assertEqual((dlg["start"], round(dlg["end"], 3)), (1.0, 2.7))
        ks = [int(k) for k in re.findall(r"\\k(\d+)", dlg["text"])]
        self.assertEqual(ks, [50, 50, 70])                        # gaps belong to the previous word
        self.assertEqual(sum(ks), round((dlg["end"] - dlg["start"]) * 100))

    def test_render_ass_structure(self):
        segs = [{"text": "Hello there.", "start": 0.0, "end": 1.0,
                 "words": [{"w": "Hello", "start": 0.0, "end": 0.4},
                           {"w": "there.", "start": 0.5, "end": 0.9}]}]
        ass = C.render_ass("My Title", C.build_dialogues(segs, {}), CFG)
        self.assertIn("[Script Info]", ass)
        self.assertIn("Title: My Title", ass)
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("Style: Caption,Arial,64", ass)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.00,Caption,,0,0,0,,", ass)
        self.assertIn("{\\k50}Hello {\\k50}there.", ass)

    def test_karaoke_can_be_turned_off(self):
        segs = [{"text": "One two.", "start": 0.0, "end": 1.0,
                 "words": [{"w": "One", "start": 0.0, "end": 0.4},
                           {"w": "two.", "start": 0.5, "end": 0.9}]}]
        dlg = C.build_dialogues(segs, {"karaoke": False})[0]
        self.assertEqual(dlg["text"], "One two.")


# ---------------------------------------------------------------------------
class NarratorWordTimingTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        text = front_matter() + scene(1, "(deadpan) First line {sfx:pop} here.\n"
                                         "(whisper) Second line here.")
        self.proj = project_dict(text)
        self.backend = FakeBackend()

    def run_narration(self, **kw):
        kw.setdefault("polish", False)
        kw.setdefault("export", False)
        return narrate_project(self.proj, self.dir, CFG, self.backend, log=quiet, **kw)

    def test_narration_json_has_absolute_word_spans_from_tokens(self):
        self.run_narration()
        narr = json.loads((self.dir / "narration.json").read_text())
        seg = narr["segments"][0]
        self.assertEqual(seg["timing"], "tokens")
        self.assertEqual([w["w"] for w in seg["words"]], ["First", "line", "here."])
        # fake raw audio has 0.25 s of lead-in; trim keeps 0.06 s of it
        self.assertAlmostEqual(seg["words"][0]["start"], seg["start"] + 0.06, places=2)
        for a, b in zip(seg["words"], seg["words"][1:]):
            self.assertLessEqual(a["end"], b["start"])
        self.assertEqual(narr["captions"], "captions/subtitles.ass")

    def test_sfx_cue_is_no_longer_estimated(self):
        res = self.run_narration()
        sfx = next(e for e in res.timeline["events"] if e["type"] == "sfx")
        self.assertFalse(sfx["estimated"])
        narr = json.loads((self.dir / "narration.json").read_text())
        word_start = narr["segments"][0]["words"][2]["start"]          # "here."
        self.assertAlmostEqual(sfx["start"], word_start, places=3)

    def test_sfx_event_and_captions_are_written(self):
        res = self.run_narration()
        sfx = next(e for e in res.timeline["events"] if e["type"] == "sfx")
        self.assertEqual((sfx["kind"], sfx["name"], sfx["file"]), ("point", "pop", "sfx/pop.wav"))
        ass = (self.dir / "captions" / "subtitles.ass").read_text()
        self.assertIn("{\\k", ass)
        self.assertIn("First", ass)
        self.assertEqual(res.captions_path, self.dir / "captions" / "subtitles.ass")

    def test_word_timing_survives_a_full_cache_run(self):
        self.run_narration()
        self.backend.calls.clear()
        res = self.run_narration()
        self.assertEqual(res.stats, {"cache": 2})
        self.assertEqual(self.backend.calls, [])
        narr = json.loads((self.dir / "narration.json").read_text())
        self.assertEqual(narr["segments"][0]["timing"], "tokens")

    def test_backend_without_timestamps_falls_back_to_audio(self):
        class NoTokens:                                    # the old backend interface: synth only
            id = "fake:no-tokens"
            sample_rate = SR

            def __init__(self):
                self._fake = FakeBackend()

            def synth(self, text, speed):
                return self._fake.synth(text, speed)

        res = narrate_project(self.proj, self.dir, CFG, NoTokens(), polish=False, export=False,
                              log=quiet)
        narr = json.loads((self.dir / "narration.json").read_text())
        self.assertIn(narr["segments"][0]["timing"], ("aligned", "estimated"))
        sfx = next(e for e in res.timeline["events"] if e["type"] == "sfx")
        self.assertIn("estimated", sfx)

    def test_captions_can_be_disabled(self):
        narrate_project(self.proj, self.dir, cfg_with(captions={"enabled": False}),
                        self.backend, polish=False, export=False, log=quiet)
        self.assertFalse((self.dir / "captions" / "subtitles.ass").exists())


if __name__ == "__main__":
    unittest.main()
