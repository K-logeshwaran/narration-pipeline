import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from narration import FakeBackend, RhubarbError, lipsync_project, narrate_project, rhubarb_available
from narration.lipsync import build_scene_mouth_cues, make_rhubarb_key, make_scene_blinks, normalize_cues
from tests.helpers import CFG, FIXTURES, front_matter, scene
from tests.test_narration import TmpDirCase, cfg_with, project_dict, quiet, run_cli

HAS_RHUBARB = rhubarb_available(CFG)


class CueMathTests(unittest.TestCase):
    def test_normalize_cues_prepends_x_merges_and_clamps(self):
        raw = [
            {"start": 0.20, "value": "C"},
            {"start": 0.35, "value": "C"},
            {"start": 0.60, "value": "D"},
            {"start": 1.50, "value": "B"},  # past duration 1.0 -> dropped
        ]
        cues = normalize_cues(raw, duration=1.0)
        self.assertEqual(cues, [
            {"start": 0.0, "end": 0.2, "value": "X"},
            {"start": 0.2, "end": 0.6, "value": "C"},
            {"start": 0.6, "end": 1.0, "value": "D"},
        ])

    def test_build_scene_mouth_cues_fills_gaps_with_x(self):
        segs = [
            {
                "start": 0.5,
                "end": 1.0,
                "abs_cues": [
                    {"start": 0.5, "end": 0.8, "value": "B"},
                    {"start": 0.8, "end": 1.0, "value": "X"},
                ],
            },
            {
                "start": 1.5,
                "end": 2.0,
                "abs_cues": [
                    {"start": 1.5, "end": 2.0, "value": "D"},
                ],
            },
        ]
        merged = build_scene_mouth_cues(0.0, 2.5, segs)
        self.assertEqual(merged, [
            {"start": 0.0, "end": 0.5, "value": "X"},
            {"start": 0.5, "end": 0.8, "value": "B"},
            {"start": 0.8, "end": 1.5, "value": "X"},
            {"start": 1.5, "end": 2.0, "value": "D"},
            {"start": 2.0, "end": 2.5, "value": "X"},
        ])

    def test_scene_blinks_are_deterministic_and_vary_by_scene(self):
        bcfg = CFG["rhubarb"]["blink"]
        b1_a = make_scene_blinks(0.0, 15.0, 1, bcfg)
        b1_b = make_scene_blinks(0.0, 15.0, 1, bcfg)
        b2 = make_scene_blinks(0.0, 15.0, 2, bcfg)
        self.assertEqual(b1_a, b1_b)
        self.assertNotEqual(b1_a, b2)
        self.assertTrue(all(0.0 <= s < e <= 15.0 for s, e in b1_a))


class LipsyncOrchestrationTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        text = (front_matter()
                + scene(1, "(deadpan) First line here.\n(smug) Second line here.")
                + scene(2, "(flat) Third line in scene two."))
        self.proj = project_dict(text)
        narrate_project(self.proj, self.dir, CFG, FakeBackend(), polish=False, export=False, log=quiet)

    def test_fake_lipsync_writes_lipsync_json_and_caches(self):
        res1 = lipsync_project(self.dir, CFG, backend="fake", log=quiet)
        self.assertEqual(res1.stats["analyzed"], 3)
        self.assertEqual(res1.stats["cache"], 0)
        self.assertTrue((self.dir / "lipsync.json").exists())
        self.assertEqual(len(res1.lipsync["segments"]), 3)
        self.assertEqual(len(res1.lipsync["scenes"]), 2)

        # Second run is 100% cache hit
        res2 = lipsync_project(self.dir, CFG, backend="fake", log=quiet)
        self.assertEqual(res2.stats["analyzed"], 0)
        self.assertEqual(res2.stats["cache"], 3)

        # Selective regeneration with `only`
        res3 = lipsync_project(self.dir, CFG, backend="fake", only=["scene01_seg02"], log=quiet)
        self.assertEqual(res3.stats["analyzed"], 1)
        self.assertEqual(res3.stats["cache"], 2)

    def test_dialog_text_change_invalidates_only_that_segment(self):
        lipsync_project(self.dir, CFG, backend="fake", log=quiet)
        narr_path = self.dir / "narration.json"
        narr = json.loads(narr_path.read_text(encoding="utf-8"))
        narr["segments"][0]["text"] = "Edited dialog text for segment one."
        narr_path.write_text(json.dumps(narr), encoding="utf-8")

        res = lipsync_project(self.dir, CFG, backend="fake", log=quiet)
        self.assertEqual(res.stats["analyzed"], 1)
        self.assertEqual(res.stats["cache"], 2)

    def test_fallback_recognizer_used_when_primary_fails(self):
        calls = []

        def fake_invoke(exe, wav_path, dialog_text, recognizer, timeout_s):
            calls.append((wav_path.stem, recognizer))
            if wav_path.stem == "scene01_seg01" and recognizer == "pocketSphinx":
                raise RuntimeError("simulated pocketSphinx failure on seg01")
            return [{"start": 0.0, "value": "X"}, {"start": 0.1, "value": "C"}]

        with patch("narration.lipsync.find_rhubarb", return_value="/usr/bin/rhubarb"), \
             patch("narration.lipsync.rhubarb_version", return_value="1.13.0"), \
             patch("narration.lipsync._invoke_rhubarb", side_effect=fake_invoke):
            # Run 1: 3 segments total; seg01 falls back to phonetic, seg02 & scene02_seg01 succeed on pocketSphinx
            res1 = lipsync_project(self.dir, CFG, backend="rhubarb", log=quiet)
            self.assertEqual(len(res1.lipsync["segments"]), 3)
            self.assertEqual(res1.stats, {"analyzed": 2, "fallback": 1, "cache": 0})
            self.assertEqual(len(res1.warnings), 1)
            self.assertIn('scene01_seg01: primary recognizer "pocketSphinx" failed', res1.warnings[0])

            # Check recorded recognizer and cache keys on disk
            segs_by_id = {s["id"]: s for s in res1.lipsync["segments"]}
            self.assertEqual(segs_by_id["scene01_seg01"]["recognizer"], "phonetic")
            self.assertEqual(segs_by_id["scene01_seg02"]["recognizer"], "pocketSphinx")
            self.assertEqual(segs_by_id["scene02_seg01"]["recognizer"], "pocketSphinx")

            seg01_proc = segs_by_id["scene01_seg01"]["cache_key"]
            side01 = json.loads((self.dir / "cache" / "proc" / f"{seg01_proc}.rhubarb.json").read_text("utf-8"))
            expected_phonetic_key = make_rhubarb_key(
                seg01_proc, "First line here.", "phonetic", "rhubarb:1.13.0"
            )
            expected_primary_key = make_rhubarb_key(
                seg01_proc, "First line here.", "pocketSphinx", "rhubarb:1.13.0"
            )
            self.assertEqual(side01["recognizer"], "phonetic")
            self.assertEqual(side01["rhubarb_key"], expected_phonetic_key)
            self.assertNotEqual(side01["rhubarb_key"], expected_primary_key)

            # Run 2: seg02 and scene02_seg01 are primary cache hits; seg01 is NOT falsely reported as a primary hit
            res2 = lipsync_project(self.dir, CFG, backend="rhubarb", log=quiet)
            self.assertEqual(res2.stats, {"analyzed": 0, "fallback": 1, "cache": 2})

            # Run 3: if config recognizer is set to "phonetic", seg01's cached phonetic result IS a primary hit
            phonetic_cfg = cfg_with(rhubarb={"recognizer": "phonetic"})
            res3 = lipsync_project(self.dir, phonetic_cfg, backend="rhubarb", log=quiet)
            self.assertEqual(res3.stats["cache"], 1)
            self.assertEqual(res3.stats["analyzed"], 2)
            self.assertEqual(res3.stats["fallback"], 0)

    def test_unknown_only_id_raises_rhubarb_error(self):
        with self.assertRaises(RhubarbError):
            lipsync_project(self.dir, CFG, backend="fake", only=["scene99_seg01"], log=quiet)


@unittest.skipUnless(HAS_RHUBARB, "rhubarb binary not found")
class RealRhubarbIntegrationTests(TmpDirCase):
    def test_real_rhubarb_with_dialog_file_and_cache(self):
        text = front_matter() + scene(1, "(deadpan) Welcome back to the video.")
        proj = project_dict(text)
        narrate_project(proj, self.dir, CFG, FakeBackend(), polish=False, export=False, log=quiet)

        res1 = lipsync_project(self.dir, CFG, backend="rhubarb", log=quiet)
        self.assertEqual(len(res1.lipsync["segments"]), 1)
        seg = res1.lipsync["segments"][0]
        self.assertIn(seg["recognizer"], ("pocketSphinx", "phonetic"))
        self.assertGreaterEqual(len(seg["cues"]), 1)
        self.assertAlmostEqual(seg["cues"][0]["start"], 0.0, places=3)
        self.assertAlmostEqual(seg["cues"][-1]["end"], seg["duration"], places=2)

        # Second run reuses sidecar cache without invoking Rhubarb again
        res2 = lipsync_project(self.dir, CFG, backend="rhubarb", log=quiet)
        self.assertEqual(res2.stats["cache"], 1)
        self.assertEqual(res2.stats["analyzed"], 0)


class LipsyncCliTests(TmpDirCase):
    def test_cli_lipsync_command(self):
        proj_dir = self.dir / "proj"
        code, _, err = run_cli(["narrate", os.path.join(FIXTURES, "example_v11.md"),
                                "--backend", "fake", "--no-polish", "--no-export", "-o", str(proj_dir)])
        self.assertEqual(code, 0, err)
        code, out, err = run_cli(["lipsync", str(proj_dir), "--rhubarb", "fake"])
        self.assertEqual(code, 0, err)
        self.assertIn("lipsync.json", out)
        self.assertTrue((proj_dir / "lipsync.json").exists())


if __name__ == "__main__":
    unittest.main()