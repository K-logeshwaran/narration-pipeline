import json
import shutil
import subprocess
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from narration import FakeBackend, lipsync_project, narrate_project
from narration import audio as A
from render import (
    TIMELINE_V2_VERSION,
    build_char_timeline,
    build_scene_ass,
    ensure_composite_png,
    load_kit,
    render_project_scenes,
    upgrade_project_timeline,
)
from tests.helpers import CFG, front_matter, scene
from tests.test_narration import TmpDirCase, project_dict, quiet

HAS_FFMPEG = A.ffmpeg_available() and shutil.which("ffprobe") is not None


def _make_rgba_png(path: Path, size=(1000, 1000), box=(350, 250, 650, 850), color=(255, 210, 0, 255)):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    if box:
        draw = ImageDraw.Draw(im)
        draw.rectangle(box, fill=color)
    im.save(path, format="PNG")


def _build_test_assets(root: Path, char_id: str = "CBBob") -> Path:
    assets = root / "assets"
    kit_dir = assets / "characters" / char_id
    kit_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "id": char_id,
        "aliases": ["deadpan_doug", "bob"],
        "canvas": [1000, 1000],
        "anchor": [500, 900],
        "trim": {"enabled": True, "margin_px": 40},
        "pip": {"scale": 0.40, "position": "bottom_right", "margin_x": 40, "margin_y": 30},
        "motion": {"enabled": True, "bounce_px": 6.0, "sway_deg": 1.0, "speed": 1.0},
        "blink": {"enabled": True, "min_gap_s": 2.5, "max_gap_s": 5.0, "duration_s": 0.14, "seed": 7},
        "layer_order": ["base", "props", "eyes", "brows", "mouth"],
        "mouths": {
            "shapes": {s: f"mouth_{s}.png" for s in ("A", "B", "C", "D", "E", "F", "X")},
            "fallbacks": {"G": "B", "H": "C"},
        },
        "poses": {
            "default": {
                "base": "base.png",
                "eyes_open": "eyes_open.png",
                "eyes_closed": "eyes_closed.png",
                "brows": None,
                "props": None,
                "blink": True,
            }
        },
        "emotion_to_pose": {emo: "default" for emo in CFG["emotions"]},
    }
    (kit_dir / "character.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _make_rgba_png(kit_dir / "base.png", (1000, 1000), (300, 200, 700, 900), (240, 200, 50, 255))
    _make_rgba_png(kit_dir / "eyes_open.png", (1000, 1000), (420, 310, 580, 360), (20, 20, 20, 255))
    _make_rgba_png(kit_dir / "eyes_closed.png", (1000, 1000), (420, 330, 580, 340), (20, 20, 20, 255))
    for idx, s in enumerate(("A", "B", "C", "D", "E", "F", "X")):
        _make_rgba_png(
            kit_dir / f"mouth_{s}.png",
            (1000, 1000),
            (450, 420, 550, 435 + idx * 5),
            (180, 40, 40, 255),
        )

    # Create a scene background and a reaction cutaway image
    scenes_dir = assets / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1376, 768), (245, 245, 250)).save(scenes_dir / "s01_test.png")
    Image.new("RGB", (1376, 768), (220, 200, 200)).save(assets / "characters" / "done_with_everything.jpg")
    return assets


class CharTimelineQuantizationTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = _build_test_assets(self.dir)
        self.kit = load_kit(self.assets / "characters" / "CBBob")

    def test_quantizes_to_exact_frame_grid_without_drift_or_blink_corruption(self):
        # Sub-frame cue at t=0.005s and mouth cue 5ms after a blink ends
        mouth_cues = [
            {"start": 0.0, "end": 0.005, "shape": "X"},
            {"start": 0.005, "end": 0.500, "shape": "C"},
            {"start": 0.500, "end": 1.145, "shape": "D"},
            {"start": 1.145, "end": 2.000, "shape": "X"},
        ]
        blinks = [[1.000, 1.140]]  # 30fps: frame 30 (1.000s) to frame 34 (1.133s)
        pose_spans = [{"start": 0.0, "end": 2.0, "pose": "default"}]

        spans = build_char_timeline(0, 60, 30, self.kit, mouth_cues, blinks, pose_spans)
        self.assertEqual(spans[0].start_frame, 0)
        self.assertEqual(spans[-1].end_frame, 60)
        # Every span is at least 1 frame and contiguous
        for a, b in zip(spans, spans[1:]):
            self.assertEqual(a.end_frame, b.start_frame)
            self.assertGreaterEqual(a.end_frame - a.start_frame, 1)
        # Sum of durations equals exact 2.000000s (60 / 30)
        self.assertAlmostEqual(sum(s.duration_s for s in spans), 2.0, places=6)
        # Blink is strictly confined to frames 30..34 (4 frames = 0.1333s), never stretched to 2.0s
        blink_frames = sum(s.end_frame - s.start_frame for s in spans if s.eyes_closed)
        self.assertEqual(blink_frames, 4)

    def test_composite_png_is_cached_and_cropped_to_trim_box(self):
        cache_dir = self.dir / "cache" / "char"
        p1 = ensure_composite_png(self.kit, "default", False, "C", cache_dir)
        self.assertTrue(p1.exists())
        with Image.open(p1) as im:
            tw = self.kit.trim_box[2] - self.kit.trim_box[0]
            th = self.kit.trim_box[3] - self.kit.trim_box[1]
            self.assertEqual(im.size, (tw, th))
            self.assertEqual(im.mode, "RGBA")
        # Second call returns the exact same cached path
        p2 = ensure_composite_png(self.kit, "default", False, "C", cache_dir)
        self.assertEqual(p1, p2)


class TimelineV2CompilationTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = _build_test_assets(self.dir)
        text = (
            front_matter()
            + scene(
                1,
                "(deadpan) {cam:zoom_in|6} Welcome to the {text:TEST|pop} test.\n"
                "(flat) {img:done_with_everything|2.0} Here is a cutaway. {img:scene} Back early.",
            )
            + scene(2, "(smug) Second scene here.", {"layout": "bob_full"})
        )
        self.proj = project_dict(text)
        (self.dir / "project.json").write_text(json.dumps(self.proj, indent=2), encoding="utf-8")
        narrate_project(self.proj, self.dir, CFG, FakeBackend(), polish=False, export=False, log=quiet)
        lipsync_project(self.dir, CFG, backend="fake", log=quiet)

    def test_upgrade_timeline_v2_preserves_events_and_builds_scene_plans(self):
        tl_v2, kit = upgrade_project_timeline(self.dir, CFG, self.assets)
        self.assertEqual(tl_v2["schema_version"], TIMELINE_V2_VERSION)
        self.assertEqual(tl_v2["character"]["id"], "CBBob")
        self.assertEqual(tl_v2["character"]["kit_sha256"], kit.kit_sha256)
        self.assertTrue(len(tl_v2["events"]) > 0)  # Backward compatibility kept

        s1, s2 = tl_v2["scenes"]
        self.assertEqual(s1["start_frame"], 0)
        self.assertEqual(s1["end_frame"], s2["start_frame"])
        self.assertEqual(s2["end_frame"], tl_v2["total_frames"])
        self.assertTrue(s1["image_exists"])

        # Verify `{img:scene}` closed the `done_with_everything` cutaway early (< 2.0s)
        img_ov = next(o for o in s1["overlays"] if o["type"] == "img")
        self.assertEqual(img_ov["name"], "done_with_everything")
        self.assertLess(img_ov["end"] - img_ov["start"], 2.0)

        # Verify layout_spans switches to `full_scene` during the static cutaway
        modes = [sp["mode"] for sp in s1["character_track"]["layout_spans"]]
        self.assertIn("bob_pip", modes)
        self.assertIn("full_scene", modes)
        self.assertEqual(s2["layout_default"], "bob_full")

    def test_scene_hash_changes_when_image_bytes_change(self):
        tl_a, _ = upgrade_project_timeline(self.dir, CFG, self.assets)
        hash_a = tl_a["scenes"][0]["scene_hash"]

        # Modify pixel bytes of s01_test.png
        Image.new("RGB", (1376, 768), (100, 120, 140)).save(self.assets / "scenes" / "s01_test.png")
        tl_b, _ = upgrade_project_timeline(self.dir, CFG, self.assets)
        hash_b = tl_b["scenes"][0]["scene_hash"]
        self.assertNotEqual(hash_a, hash_b)

    def test_build_scene_ass_shifts_and_clamps_timestamps(self):
        tl_v2, _ = upgrade_project_timeline(self.dir, CFG, self.assets)
        narr = json.loads((self.dir / "narration.json").read_text(encoding="utf-8"))
        s2 = tl_v2["scenes"][1]
        ass = build_scene_ass("scene02", s2["start"], s2["end"], narr, CFG)
        self.assertIsNotNone(ass)
        self.assertIn("Dialogue: 0,0:00:00.", ass)  # Shifted relative to scene02 start


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe required")
class SceneRenderIntegrationTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = _build_test_assets(self.dir)
        text = front_matter() + scene(1, "(deadpan) Quick scene one.\n(smug) Line two.")
        self.proj = project_dict(text)
        (self.dir / "project.json").write_text(json.dumps(self.proj, indent=2), encoding="utf-8")
        narrate_project(self.proj, self.dir, CFG, FakeBackend(), polish=False, export=False, log=quiet)
        lipsync_project(self.dir, CFG, backend="fake", log=quiet)

    def test_render_scene_produces_exact_frame_count_and_caches(self):
        res1 = render_project_scenes(self.dir, self.assets, CFG, log=quiet)
        self.assertEqual(res1.stats, {"rendered": 1, "cache": 0})
        mp4 = res1.scenes_rendered[0]
        self.assertTrue(mp4.exists())

        tl_v2 = json.loads((self.dir / "timeline.json").read_text(encoding="utf-8"))
        expected_frames = tl_v2["scenes"][0]["end_frame"] - tl_v2["scenes"][0]["start_frame"]

        # Verify exact frame count and resolution with ffprobe
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries", "stream=width,height,nb_read_frames",
            "-of", "json", str(mp4),
        ], text=True)
        stream = json.loads(probe)["streams"][0]
        self.assertEqual((int(stream["width"]), int(stream["height"])), (1920, 1080))
        self.assertEqual(int(stream["nb_read_frames"]), expected_frames)

        # Second run is a cache hit
        res2 = render_project_scenes(self.dir, self.assets, CFG, log=quiet)
        self.assertEqual(res2.stats, {"rendered": 0, "cache": 1})


if __name__ == "__main__":
    unittest.main()