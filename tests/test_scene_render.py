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
from tests.test_narration import TmpDirCase, cfg_with, project_dict, quiet

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
        "pip": {"scale": 0.40, "position": "mid_left", "margin_x": 40, "margin_y": 30},
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

    # Create full 1920x1080 scene backgrounds and reaction cutaway image
    scenes_dir = assets / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    for stem in ("s01_test", "s02_test", "s01_autopsy", "s02_shady_brain"):
        bg = Image.new("RGB", (1920, 1080), (245, 245, 250))
        bg_draw = ImageDraw.Draw(bg)
        bg_draw.rectangle((600, 200, 1600, 880), fill=(230, 235, 245), outline=(40, 50, 70), width=4)
        bg.save(scenes_dir / f"{stem}.png")

    react = Image.new("RGB", (1920, 1080), (220, 200, 200))
    ImageDraw.Draw(react).rectangle((500, 200, 1600, 880), fill=(245, 230, 220), outline=(30, 30, 30), width=4)
    react.save(assets / "characters" / "done_with_everything.jpg")
    return assets


class CharTimelineQuantizationTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = _build_test_assets(self.dir)
        self.kit = load_kit(self.assets / "characters" / "CBBob")

    def test_quantizes_to_exact_frame_grid_without_drift_or_blink_corruption(self):
        mouth_cues = [
            {"start": 0.0, "end": 0.005, "shape": "X"},
            {"start": 0.005, "end": 0.500, "shape": "C"},
            {"start": 0.500, "end": 1.145, "shape": "D"},
            {"start": 1.145, "end": 2.000, "shape": "X"},
        ]
        blinks = [[1.000, 1.140]]
        pose_spans = [{"start": 0.0, "end": 2.0, "pose": "default"}]

        spans = build_char_timeline(0, 60, 30, self.kit, mouth_cues, blinks, pose_spans)
        self.assertEqual(spans[0].start_frame, 0)
        self.assertEqual(spans[-1].end_frame, 60)
        for a, b in zip(spans, spans[1:]):
            self.assertEqual(a.end_frame, b.start_frame)
            self.assertGreaterEqual(a.end_frame - a.start_frame, 1)
        self.assertAlmostEqual(sum(s.duration_s for s in spans), 2.0, places=6)
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

    def test_upgrade_timeline_v2_preserves_events_and_keeps_pip_during_cutaway(self):
        tl_v2, kit = upgrade_project_timeline(self.dir, CFG, self.assets)
        self.assertEqual(tl_v2["schema_version"], TIMELINE_V2_VERSION)
        self.assertEqual(tl_v2["character"]["id"], "CBBob")
        self.assertEqual(tl_v2["character"]["kit_sha256"], kit.kit_sha256)
        self.assertTrue(len(tl_v2["events"]) > 0)

        s1, s2 = tl_v2["scenes"]
        self.assertEqual(s1["start_frame"], 0)
        self.assertEqual(s1["end_frame"], s2["start_frame"])
        self.assertEqual(s2["end_frame"], tl_v2["total_frames"])
        self.assertTrue(s1["image_exists"])

        # Verify `{img:scene}` closed the `done_with_everything` cutaway early (< 2.0s)
        img_ov = next(o for o in s1["overlays"] if o["type"] == "img")
        self.assertEqual(img_ov["name"], "done_with_everything")
        self.assertLess(img_ov["end"] - img_ov["start"], 2.0)

        # By default, PiP mascot stays visible (`bob_pip`) across the entire scene
        modes = [sp["mode"] for sp in s1["character_track"]["layout_spans"]]
        self.assertEqual(modes, ["bob_pip"])
        self.assertEqual(s2["layout_default"], "bob_full")

        # When hide_pip_on_cutaway=True, layout_spans switches to full_scene during the cutaway
        cfg_hide = cfg_with()
        cfg_hide["hide_pip_on_cutaway"] = True
        tl_hide, _ = upgrade_project_timeline(self.dir, cfg_hide, self.assets)
        modes_hide = [sp["mode"] for sp in tl_hide["scenes"][0]["character_track"]["layout_spans"]]
        self.assertIn("full_scene", modes_hide)

    def test_scene_hash_changes_when_image_bytes_change(self):
        tl_a, _ = upgrade_project_timeline(self.dir, CFG, self.assets)
        hash_a = tl_a["scenes"][0]["scene_hash"]

        Image.new("RGB", (1920, 1080), (100, 120, 140)).save(self.assets / "scenes" / "s01_test.png")
        tl_b, _ = upgrade_project_timeline(self.dir, CFG, self.assets)
        hash_b = tl_b["scenes"][0]["scene_hash"]
        self.assertNotEqual(hash_a, hash_b)

    def test_build_scene_ass_shifts_and_clamps_timestamps(self):
        tl_v2, _ = upgrade_project_timeline(self.dir, CFG, self.assets)
        narr = json.loads((self.dir / "narration.json").read_text(encoding="utf-8"))
        s2 = tl_v2["scenes"][1]
        ass = build_scene_ass("scene02", s2["start"], s2["end"], narr, CFG)
        self.assertIsNotNone(ass)
        self.assertIn("Dialogue: 0,0:00:00.", ass)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe required")
class SceneRenderIntegrationTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.assets = _build_test_assets(self.dir)
        text = front_matter() + scene(1, "(deadpan) {cam:zoom_in|6} Quick scene one.\n(smug) Line two.")
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

        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries", "stream=width,height,nb_read_frames",
            "-of", "json", str(mp4),
        ], text=True)
        stream = json.loads(probe)["streams"][0]
        self.assertEqual((int(stream["width"]), int(stream["height"])), (1920, 1080))
        self.assertEqual(int(stream["nb_read_frames"]), expected_frames)

        res2 = render_project_scenes(self.dir, self.assets, CFG, log=quiet)
        self.assertEqual(res2.stats, {"rendered": 0, "cache": 1})


if __name__ == "__main__":
    unittest.main()