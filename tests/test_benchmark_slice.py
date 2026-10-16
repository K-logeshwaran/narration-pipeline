import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narration import FakeBackend, narrate_project
from narration import audio as A
from narration.kokoro import KokoroBackend, ensure_spacy_model_installed
from render.stage_runner import (
    check_memory_preflight,
    compute_scene_workers,
    run_stage_subprocess,
)
from render.timeline_v2 import _compute_pip_box
from tests.helpers import CFG, FIXTURES, front_matter, scene
from tests.test_narration import TmpDirCase, project_dict, quiet
from tests.test_scene_render import _build_test_assets
from tools.benchmark_slice import run_vertical_slice

HAS_FFMPEG = A.ffmpeg_available() and shutil.which("ffprobe") is not None


class KokoroCleanupAndSpacyGuardTests(unittest.TestCase):
    def test_kokoro_close_clears_pipeline_reference(self):
        b = KokoroBackend("am_fenrir", "a")
        b._pipeline = object()
        b.close()
        self.assertIsNone(b._pipeline)

    def test_missing_spacy_model_fails_fast_without_downloading(self):
        with patch("importlib.util.find_spec", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                ensure_spacy_model_installed("en_core_web_sm")
            self.assertIn("en_core_web_sm", str(cm.exception))
            self.assertIn("Runtime auto-download is disabled", str(cm.exception))


class StageRunnerAndPreflightTests(TmpDirCase):
    def test_stage_runner_measures_isolated_subprocess_rss(self):
        code = "x = bytearray(20 * 1024 * 1024)"
        m = run_stage_subprocess("alloc_test", [sys.executable, "-c", code], quiet_output=True)
        self.assertEqual(m.returncode, 0)
        self.assertGreaterEqual(m.peak_rss_mib, 20.0)
        self.assertLess(m.peak_rss_mib, 250.0)

    def test_memory_preflight_warns_below_threshold_without_aborting(self):
        fake_meminfo = self.dir / "meminfo"
        fake_meminfo.write_text("MemTotal:        7444888 kB\nMemAvailable:    1400000 kB\n", encoding="utf-8")
        logged = []
        warn = check_memory_preflight(min_mib=2048.0, log=logged.append, meminfo_path=fake_meminfo)
        self.assertIsNotNone(warn)
        self.assertIn("close heavy apps before building", warn)
        self.assertEqual(len(logged), 1)

        # Above threshold: no warning
        fake_meminfo.write_text("MemTotal:        7444888 kB\nMemAvailable:    4500000 kB\n", encoding="utf-8")
        self.assertIsNone(check_memory_preflight(min_mib=2048.0, log=logged.append, meminfo_path=fake_meminfo))

    def test_scene_workers_derived_from_mem_available_and_capped_at_one(self):
        self.assertEqual(compute_scene_workers(mem_available_mib=1400.0, per_scene_peak_mib=910.0), 1)
        self.assertEqual(compute_scene_workers(mem_available_mib=4600.0, per_scene_peak_mib=910.0, hard_max=1), 1)

    def test_no_runtime_warning_when_executing_render_scene_as_module(self):
        proc = subprocess.run(
            [sys.executable, "-W", "error::RuntimeWarning", "-m", "render.scene", "--help"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("RuntimeWarning", proc.stderr)

    def test_full_mix_wav_is_not_flagged_as_orphan(self):
        proj = project_dict(front_matter() + scene(1, "(deadpan) First line."))
        narrate_project(proj, self.dir, CFG, FakeBackend(), polish=False, export=False, log=quiet)
        (self.dir / "audio" / "full_mix.wav").write_bytes(b"RIFF")
        res = narrate_project(proj, self.dir, CFG, FakeBackend(), polish=False, export=False, log=quiet)
        self.assertNotIn("full_mix.wav", res.orphans)
        self.assertFalse(any("full_mix.wav" in w for w in res.warnings))

    def test_pip_box_supports_mid_left_and_top_left(self):
        assets = _build_test_assets(self.dir, char_id="atlas")
        from render.kit import load_kit
        kit = load_kit(assets / "characters" / "atlas")

        kit.pip["position"] = "mid_left"
        mx, my, mw, mh = _compute_pip_box((1920, 1080), kit)
        self.assertEqual(mx, 40)
        self.assertEqual(my, (1080 - mh) // 2)

        kit.pip["position"] = "top_left"
        tx, ty, tw, th = _compute_pip_box((1920, 1080), kit)
        self.assertEqual(tx, 40)
        self.assertEqual(ty, 30)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe required")
class VerticalSliceTests(TmpDirCase):
    def test_one_scene_vertical_slice_end_to_end_and_render_only_mode(self):
        assets = _build_test_assets(self.dir, char_id="atlas")
        out_dir = self.dir / "slice_out"
        script_path = Path(FIXTURES) / "example_v11.md"

        report = run_vertical_slice(
            script_path,
            out_dir,
            assets,
            CFG,
            scene_number=1,
            character="atlas",
            tts_backend="fake",
            rhubarb_backend="fake",
            polish=False,
            force=True,
            log=quiet,
        )
        out_mp4 = Path(report["output_mp4"])
        self.assertTrue(out_mp4.exists())
        self.assertTrue((out_dir / "telemetry.json").exists())
        self.assertLessEqual(report["sync"]["drift_frames"], 1.0)
        self.assertIn("narrate", report["stages"])
        self.assertIn("scene_render", report["stages"])

        render_only_report = run_vertical_slice(
            script_path,
            out_dir,
            assets,
            CFG,
            scene_number=1,
            character="atlas",
            render_only=True,
            force=True,
            log=quiet,
        )
        self.assertEqual(list(render_only_report["stages"].keys()), ["scene_render", "mux"])
        # Machine-dependent regression guard: measured ~871-910 MiB on Fedora 44 (Ryzen 5000U)
        self.assertLess(render_only_report["memory_mib"]["scene_render_peak_rss_mib"], 1100.0)


if __name__ == "__main__":
    unittest.main()