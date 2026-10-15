import json
import unittest
from pathlib import Path

import numpy as np

import main
from narration import audio as A
from narration.mix import bed_clip, load_sfx_file, loop_to, mix_sfx
from narration.wavio import read_wav, wav_duration, write_wav
from tests.helpers import CFG
from tests.test_narration import TmpDirCase, quiet, run_cli

SR = 24000
HAS_FFMPEG = A.ffmpeg_available()


def tone(dur_s, amp=0.5, freq=440.0, sr=SR):
    t = np.arange(int(sr * dur_s), dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def timeline_with(*sfx_events):
    return {"schema_version": "1.1", "duration": 2.0, "scenes": [], "events": list(sfx_events)}


class MixSetup(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = self.dir / "assets"
        (self.assets / "sfx").mkdir(parents=True)
        self.narr = self.dir / "narration.wav"
        write_wav(self.narr, tone(2.0, amp=0.1, freq=200.0), SR)   # quiet 2 s "narration"

    def make_sfx(self, name, audio, sr=SR):
        path = self.assets / "sfx" / f"{name}.wav"
        write_wav(path, audio, sr)
        return path

    def run_mix(self, events, cfg=CFG):
        out = self.dir / "full_mix.wav"
        return mix_sfx(timeline_with(*events), self.narr, self.assets, cfg, out, log=quiet)


# ---------------------------------------------------------------------------
class ClipHelpersTests(unittest.TestCase):
    def test_loop_to_repeats_and_cuts(self):
        clip = np.array([1.0, -1.0], dtype=np.float32)
        self.assertEqual(loop_to(clip, 5).tolist(), [1.0, -1.0, 1.0, -1.0, 1.0])
        self.assertEqual(loop_to(clip, 1).tolist(), [1.0])
        self.assertEqual(len(loop_to(clip, 0)), 0)
        self.assertEqual(len(loop_to(np.zeros(0, dtype=np.float32), 7)), 7)

    def test_bed_clip_fades_both_edges_and_holds_length(self):
        clip = np.full(SR, 0.8, dtype=np.float32)
        out = bed_clip(clip, 3.0, SR, 0.25)
        self.assertEqual(len(out), 3 * SR)
        self.assertEqual(float(out[0]), 0.0)
        self.assertAlmostEqual(float(out[-1]), 0.0, places=5)
        self.assertAlmostEqual(float(out[SR]), 0.8, places=4)          # middle at full level
        self.assertAlmostEqual(float(out[int(0.25 * SR)]), 0.8, places=2)

    def test_bed_clip_shorter_than_two_fades_does_not_crash(self):
        out = bed_clip(np.full(100, 0.5, dtype=np.float32), 100 / SR, SR, 0.25)
        self.assertEqual(len(out), 100)


# ---------------------------------------------------------------------------
class MixTests(MixSetup):
    def test_point_sfx_placed_at_event_start_with_gain(self):
        self.make_sfx("pop", tone(0.2, amp=0.6))
        ev = {"type": "sfx", "kind": "point", "name": "pop", "file": "sfx/pop.wav", "start": 0.5}
        out_path, warnings, stats = self.run_mix([ev])
        self.assertEqual(warnings, [])
        self.assertEqual(stats["point"], 1)
        base, _ = read_wav(self.narr)
        mix, sr = read_wav(out_path)
        self.assertEqual(sr, SR)
        self.assertEqual(len(mix), len(base))
        diff = mix - base
        first = int(np.argmax(np.abs(diff) > 0.01))
        self.assertAlmostEqual(first / SR, 0.5, delta=0.01)
        self.assertAlmostEqual(float(np.max(np.abs(diff))), 0.6 * CFG["mix"]["point_gain"], places=2)
        self.assertFalse((np.abs(mix[int(0.9 * SR):] - base[int(0.9 * SR):]) > 1e-4).any())  # nothing after

    def test_point_sfx_is_clipped_at_video_end(self):
        self.make_sfx("pop", tone(2.0, amp=0.6))
        ev = {"type": "sfx", "kind": "point", "name": "pop", "file": "sfx/pop.wav", "start": 1.5}
        out_path, _, _ = self.run_mix([ev])
        self.assertAlmostEqual(wav_duration(out_path), 2.0, places=3)

    def test_bed_loops_fades_and_fills_its_span(self):
        self.make_sfx("ticking_clock", tone(0.3, amp=0.8))     # short file, long span
        ev = {"type": "sfx", "kind": "bed", "name": "ticking_clock",
              "file": "sfx/ticking_clock.wav", "start": 0.5, "end": 1.5}
        out_path, _, stats = self.run_mix([ev])
        self.assertEqual(stats["bed"], 1)
        base, _ = read_wav(self.narr)
        mix, _ = read_wav(out_path)
        diff = mix - base
        g = CFG["mix"]["bed_gain"]
        self.assertLess(float(np.abs(diff[int(0.51 * SR)])), 0.05)          # fade-in region
        self.assertAlmostEqual(float(np.max(np.abs(diff[int(0.9 * SR): int(1.1 * SR)]))), 0.8 * g, places=2)
        self.assertFalse((np.abs(diff[int(1.6 * SR):]) > 1e-4).any())       # silent after end

    def test_missing_file_warns_and_skips(self):
        ev = {"type": "sfx", "kind": "point", "name": "ghost", "file": "sfx/ghost.wav", "start": 0.5}
        out_path, warnings, stats = self.run_mix([ev])
        self.assertEqual(stats["skipped"], 1)
        self.assertTrue(any("ghost" in w for w in warnings))
        base, _ = read_wav(self.narr)
        mix, _ = read_wav(out_path)
        self.assertTrue(np.allclose(mix, base, atol=1 / 32768))

    def test_bed_without_room_is_skipped(self):
        self.make_sfx("ticking_clock", tone(0.3))
        ev = {"type": "sfx", "kind": "bed", "name": "ticking_clock",
              "file": "sfx/ticking_clock.wav", "start": 1.5, "end": 1.5}
        _, warnings, stats = self.run_mix([ev])
        self.assertEqual(stats["skipped"], 1)
        self.assertTrue(any("no room" in w for w in warnings))

    def test_peak_limit_keeps_the_mix_below_full_scale(self):
        self.make_sfx("pop", tone(0.2, amp=0.95, freq=200.0))   # same freq as narration: sums hot
        ev = {"type": "sfx", "kind": "point", "name": "pop", "file": "sfx/pop.wav", "start": 0.0}
        out_path, _, _ = self.run_mix([ev])
        mix, _ = read_wav(out_path)
        self.assertLessEqual(float(np.max(np.abs(mix))), CFG["audio"]["peak_limit"] + 1e-3)

    def test_output_is_deterministic(self):
        self.make_sfx("pop", tone(0.2))
        ev = {"type": "sfx", "kind": "point", "name": "pop", "file": "sfx/pop.wav", "start": 0.5}
        a, _, _ = self.run_mix([ev])
        b_path, _, _ = mix_sfx(timeline_with(ev), self.narr, self.assets, CFG,
                               self.dir / "b.wav", log=quiet)
        self.assertEqual(a.read_bytes(), b_path.read_bytes())


# ---------------------------------------------------------------------------
@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class DecodeTests(MixSetup):
    def test_wrong_sample_rate_wav_is_resampled(self):
        path = self.assets / "sfx" / "pop.wav"
        write_wav(path, tone(0.2, sr=48000), 48000)             # 48 kHz file, 24 kHz project
        data = load_sfx_file(path, SR)
        self.assertAlmostEqual(len(data) / SR, 0.2, delta=0.01)

    def test_mp3_is_decoded(self):
        import subprocess
        src = self.dir / "src.wav"
        write_wav(src, tone(0.2), SR)
        mp3 = self.assets / "sfx" / "pop.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), str(mp3)], check=True)
        data = load_sfx_file(mp3, SR)
        self.assertGreater(len(data), 0)
        self.assertGreater(float(np.max(np.abs(data))), 0.1)


# ---------------------------------------------------------------------------
class MixCliTests(TmpDirCase):
    def test_mix_command_end_to_end(self):
        import os
        from tests.helpers import FIXTURES
        assets = self.dir / "assets"
        (assets / "sfx").mkdir(parents=True)
        write_wav(assets / "sfx" / "pop.wav", tone(0.1), SR)
        proj_dir = self.dir / "proj"
        code, _, _ = run_cli(["narrate", os.path.join(FIXTURES, "example_v11.md"), "--backend", "fake",
                              "--no-polish", "-o", str(proj_dir)])
        self.assertEqual(code, 0)
        code, out, err = run_cli(["mix", str(proj_dir), "--assets", str(assets)])
        self.assertEqual(code, 0, err)
        self.assertIn("full_mix.wav", out)
        mix, sr = read_wav(proj_dir / "audio" / "full_mix.wav")
        self.assertEqual(sr, SR)
        base, _ = read_wav(proj_dir / "audio" / "full_narration.wav")
        self.assertEqual(len(mix), len(base))
        self.assertGreater(float(np.max(np.abs(mix - base))), 0.01)     # the pop got mixed in

    def test_mix_without_narration_or_assets_fails_cleanly(self):
        code, _, err = run_cli(["mix", str(self.dir), "--assets", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("run `narrate` first", err)
        (self.dir / "timeline.json").write_text("{}")
        (self.dir / "audio").mkdir()
        (self.dir / "audio" / "full_narration.wav").write_bytes(b"x")
        code, _, err = run_cli(["mix", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("--assets", err)


if __name__ == "__main__":
    unittest.main()
