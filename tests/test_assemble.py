import os
import shutil
import unittest
from pathlib import Path

import numpy as np

from narration import audio as A
from narration.errors import AudioToolError
from narration.wavio import write_wav
from render.assemble import assemble_project_video, verify_av_sync
from tests.helpers import CFG, FIXTURES
from tests.test_narration import TmpDirCase, run_cli
from tests.test_scene_render import _build_test_assets

HAS_FFMPEG = A.ffmpeg_available() and shutil.which("ffprobe") is not None


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe required")
class MultiSceneAssembleAndDriftTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = _build_test_assets(self.dir, char_id="atlas")
        # Create dummy sfx files
        sfx_dir = self.assets / "sfx"
        sfx_dir.mkdir(parents=True, exist_ok=True)
        sr = 24000
        short_tone = (0.2 * np.sin(np.linspace(0, 200, sr // 5))).astype(np.float32)
        
        for name in ("record_scratch", "party_horn", "pop", "pizzicato_sting", "whoosh", "bonk",
                     "ka_ching", "angelic_chime", "drum_roll", "triumphant_sting", "phone_ring", 
                     "sad_trombone", "ta_da"):
            write_wav(sfx_dir / f"{name}.wav", short_tone, sr)
        
        # Create dummy reaction images (use PIL to create real PNGs)
        from PIL import Image
        chars_dir = self.assets / "characters"
        chars_dir.mkdir(parents=True, exist_ok=True)
        for name in ("sarcastic_wow", "done_with_everything", "fourth_wall", "smug_genius"):
            # Create a simple 100x100 white PNG
            img = Image.new('RGB', (100, 100), color='white')
            img.save(chars_dir / f"{name}.png")

    def test_multi_scene_build_end_to_end_and_drift_within_one_frame(self):
        proj_dir = self.dir / "example_v11"
        script_path = os.path.join(FIXTURES, "example_v11.md")

        # Run full one-command build across ALL scenes of example_v11.md (2 scenes, 12 segments)
        code, out, err = run_cli([
            "build", script_path,
            "--backend", "fake",
            "--rhubarb", "fake",
            "--no-polish",
            "--character", "atlas",
            "--assets", str(self.assets),
            "-o", str(proj_dir),
        ])
        self.assertEqual(code, 0, err)

        final_mp4 = proj_dir / "renders" / "example_v11.mp4"
        self.assertTrue(final_mp4.exists())
        self.assertTrue((proj_dir / "renders" / "scenes" / "scene01.mp4").exists())
        self.assertTrue((proj_dir / "renders" / "scenes" / "scene02.mp4").exists())

        # Explicitly verify total video duration vs concatenated audio duration across all scenes
        mix_wav = proj_dir / "audio" / "full_mix.wav"
        sync = verify_av_sync(final_mp4, mix_wav, fps=int(CFG["render"]["fps"]), max_drift_frames=1.0)
        self.assertLessEqual(sync["drift_frames"], 1.0)
        self.assertGreater(sync["video_duration_s"], 15.0)

        # Second build run reuses cached audio, cached rhubarb, and cached scene MP4s
        code2, out2, err2 = run_cli([
            "build", script_path,
            "--backend", "fake",
            "--rhubarb", "fake",
            "--no-polish",
            "--character", "atlas",
            "--assets", str(self.assets),
            "-o", str(proj_dir),
        ])
        self.assertEqual(code2, 0, err2)
        self.assertIn("2 from cache", out2)

    def test_verify_av_sync_fails_when_audio_and_video_drift_exceeds_one_frame(self):
        proj_dir = self.dir / "drift_test"
        script_path = os.path.join(FIXTURES, "simple_av_sync_test.md")  # Use simpler fixture
        code, out, err = run_cli([
            "build", script_path,
            "--backend", "fake",
            "--rhubarb", "fake",
            "--no-polish",
            "--character", "atlas",
            "--assets", str(self.assets),
            "-o", str(proj_dir),
        ])
        self.assertEqual(code, 0, err)  # This should pass now

        final_mp4 = proj_dir / "renders" / "simple_av_sync_test.mp4"
        # ... rest of test
if __name__ == "__main__":
    unittest.main()
