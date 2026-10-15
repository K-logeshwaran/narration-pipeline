import shutil
import unittest
from pathlib import Path

from narration import audio as A
from narration.kokoro import KokoroBackend
from tests.helpers import CFG, FIXTURES
from tests.test_narration import TmpDirCase, quiet
from tests.test_scene_render import _build_test_assets
from tools.benchmark_slice import run_vertical_slice

HAS_FFMPEG = A.ffmpeg_available() and shutil.which("ffprobe") is not None


class KokoroCleanupTests(unittest.TestCase):
    def test_kokoro_close_clears_pipeline_reference(self):
        b = KokoroBackend("am_fenrir", "a")
        b._pipeline = object()
        b.close()
        self.assertIsNone(b._pipeline)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe required")
class VerticalSliceTests(TmpDirCase):
    def test_one_scene_vertical_slice_end_to_end_and_drift_under_one_frame(self):
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
        self.assertGreater(report["sync"]["video_duration_s"], 5.0)
        self.assertGreater(report["memory_mib"]["python_self_peak_mib"], 0.0)


if __name__ == "__main__":
    unittest.main()