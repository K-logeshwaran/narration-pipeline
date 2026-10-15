import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from render.kit import KitError, load_kit, resolve_character_kit, validate_kit
from tests.helpers import CFG, check, codes, front_matter, parse_text, scene, script
from tools.export_kit import export_layers


def _make_rgba_png(path: Path, size=(1000, 1000), box=(400, 300, 600, 700), color=(255, 200, 0, 255)):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    if box:
        draw = ImageDraw.Draw(im)
        draw.rectangle(box, fill=color)
    im.save(path, format="PNG")


class KitValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _build_valid_kit(self, canvas=(1000, 1000)) -> Path:
        manifest = {
            "schema_version": "1.0",
            "id": "CBBob",
            "aliases": ["deadpan_doug", "bob"],
            "canvas": list(canvas),
            "anchor": [500, 920],
            "trim": {"enabled": True, "margin_px": 40},
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
                    "brows": "brows_neutral.png",
                    "props": None,
                    "blink": True,
                }
            },
            "emotion_to_pose": {emo: "default" for emo in CFG["emotions"]},
        }
        (self.dir / "character.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _make_rgba_png(self.dir / "base.png", canvas, (300, 200, 700, 900))
        _make_rgba_png(self.dir / "eyes_open.png", canvas, (420, 310, 580, 360))
        _make_rgba_png(self.dir / "eyes_closed.png", canvas, (420, 330, 580, 340))
        # Brow extends higher than base.png (y=150) to verify union bbox across all layers
        _make_rgba_png(self.dir / "brows_neutral.png", canvas, (410, 150, 590, 180))
        for s in ("A", "B", "C", "D", "E", "F", "X"):
            _make_rgba_png(self.dir / f"mouth_{s}.png", canvas, (450, 420, 550, 460))
        return self.dir

    def test_valid_kit_loads_and_computes_union_trim_box(self):
        kit = load_kit(self._build_valid_kit(), allowed_emotions=CFG["emotions"].keys())
        self.assertEqual(kit.id, "CBBob")
        self.assertEqual(kit.canvas, (1000, 1000))
        # Union bbox is x: 300..700, y: 150..900; with 40px margin -> (260, 110, 740, 940)
        self.assertEqual(kit.trim_box, (260, 110, 740, 940))
        self.assertEqual((kit.trim_box[2] - kit.trim_box[0]) % 2, 0)
        self.assertEqual((kit.trim_box[3] - kit.trim_box[1]) % 2, 0)
        self.assertEqual(len(kit.kit_sha256), 16)

    def test_fallback_mouths_and_layer_order(self):
        kit = load_kit(self._build_valid_kit())
        self.assertEqual(kit.resolve_mouth_path("G").name, "mouth_B.png")
        self.assertEqual(kit.resolve_mouth_path("H").name, "mouth_C.png")
        layers = [p.name for p in kit.layers_for("default", eyes_closed=True, mouth_shape="G")]
        self.assertEqual(layers, ["base.png", "eyes_closed.png", "brows_neutral.png", "mouth_B.png"])

    def test_dimension_mismatch_fails_validation(self):
        self._build_valid_kit()
        _make_rgba_png(self.dir / "mouth_D.png", size=(800, 800))
        kit, diags = validate_kit(self.dir)
        self.assertIsNone(kit)
        self.assertIn("E_KIT_DIMENSION", codes(diags, "error"))

    def test_non_rgba_layer_fails_validation(self):
        self._build_valid_kit()
        rgb = Image.new("RGB", (1000, 1000), (255, 255, 255))
        rgb.save(self.dir / "eyes_open.png", format="PNG")
        kit, diags = validate_kit(self.dir)
        self.assertIsNone(kit)
        self.assertIn("E_KIT_MODE", codes(diags, "error"))

    def test_missing_layer_fails_validation(self):
        self._build_valid_kit()
        (self.dir / "mouth_A.png").unlink()
        with self.assertRaises(KitError) as cm:
            load_kit(self.dir)
        self.assertIn("E_ASSET_MISSING", [d.code for d in cm.exception.diagnostics])

    def test_content_hash_changes_when_pixel_changes(self):
        self._build_valid_kit()
        h1 = load_kit(self.dir).kit_sha256
        _make_rgba_png(self.dir / "mouth_A.png", (1000, 1000), (440, 410, 560, 470))
        h2 = load_kit(self.dir).kit_sha256
        self.assertNotEqual(h1, h2)


class ParserAliasAndLayoutTests(unittest.TestCase):
    def test_deadpan_doug_resolves_to_cbbob(self):
        res = parse_text(script("(flat) Hello there."))
        self.assertEqual(res.project.character, "CBBob")
        self.assertEqual(res.errors, [])
        self.assertEqual(resolve_character_kit("deadpan_doug", "assets", CFG),
                         Path("assets/characters/CBBob"))

    def test_scene_layout_defaults_and_overrides(self):
        text = (
            front_matter()
            + scene(1, "(flat) Default layout scene.")
            + scene(2, "(flat) Full screen Bob.", {"layout": "bob_full"})
            + scene(3, "(flat) Full scene illustration.", {"layout": "full_scene"})
        )
        res = parse_text(text)
        self.assertEqual(res.errors, [])
        s1, s2, s3 = res.project.scenes
        self.assertEqual((s1.layout, s1.layout_explicit), ("bob_pip", False))
        self.assertEqual((s2.layout, s2.layout_explicit), ("bob_full", True))
        self.assertEqual((s3.layout, s3.layout_explicit), ("full_scene", True))

    def test_unknown_scene_layout_is_validation_error(self):
        text = front_matter() + scene(1, "(flat) Bad layout.", {"layout": "split_screen"})
        _, diags = check(text)
        errs = [d for d in diags if d.level == "error"]
        self.assertEqual(codes(errs), ["E_LAYOUT_UNKNOWN"])


class ExportKitOraTests(unittest.TestCase):
    def test_ora_export_places_cropped_layers_at_exact_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ora_file = tmp_path / "test.ora"
            small_patch = Image.new("RGBA", (50, 40), (255, 0, 0, 255))
            buf = io.BytesIO()
            small_patch.save(buf, format="PNG")
            stack_xml = (
                '<image w="1000" h="1000">'
                '  <stack>'
                '    <layer name="mouth_A" src="data/layer0.png" x="450" y="420" />'
                '  </stack>'
                '</image>'
            )
            with zipfile.ZipFile(ora_file, "w") as zf:
                zf.writestr("stack.xml", stack_xml)
                zf.writestr("data/layer0.png", buf.getvalue())

            out_dir = tmp_path / "kit"
            written = export_layers(ora_file, out_dir)
            self.assertEqual(len(written), 1)
            with Image.open(out_dir / "mouth_A.png") as exported:
                self.assertEqual(exported.size, (1000, 1000))
                self.assertEqual(exported.getbbox(), (450, 420, 500, 460))


if __name__ == "__main__":
    unittest.main()