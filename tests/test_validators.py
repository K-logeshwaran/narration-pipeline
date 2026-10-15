import os
import tempfile
import unittest

from parser import render_report, validate
from tests.helpers import CFG, check, codes, front_matter, parse_text, scene, script


def errors_for(narration, extra_fm=""):
    _, diags = check(script(narration, extra_fm))
    return [d for d in diags if d.level == "error"]


def warnings_for(text):
    _, diags = check(text)
    return [d for d in diags if d.level == "warning"]


def words(n, word="word"):
    return " ".join([word] * n)


class VocabularyErrorTests(unittest.TestCase):
    def test_unknown_emotion_lists_options_and_suggests(self):
        (e,) = errors_for("(smugg) Hello there.")
        self.assertEqual(e.code, "E_EMOTION_UNKNOWN")
        self.assertIn('Did you mean "smug"', e.message)
        self.assertIn("Available emotions:", e.message)
        self.assertEqual((e.scene, e.segment), ("scene01", "scene01_seg01"))

    def test_unknown_modifier(self):
        self.assertEqual(codes(errors_for("(smug, sarcastic_angry) Hello there.")), ["E_EMOTION_UNKNOWN"])

    def test_digits_in_speech_are_errors_but_not_in_overlays(self):
        self.assertEqual(codes(errors_for("(flat) Wait two minutes.")), [])
        self.assertEqual(codes(errors_for("(flat) Wait 2 minutes.")), ["E_DIGIT"])
        self.assertEqual(codes(errors_for("(flat) Five {text:5 SCAMS|pop} scams.")), [])

    def test_line_word_limit(self):
        self.assertEqual(codes(errors_for("(flat) " + words(30))), [])
        self.assertEqual(codes(errors_for("(flat) " + words(31))), ["E_LINE_TOO_LONG"])

    def test_line_limit_counts_across_pauses(self):
        self.assertEqual(codes(errors_for(f"(flat) {words(20)} [pause] {words(11)}")), ["E_LINE_TOO_LONG"])

    def test_sfx_type_mismatch(self):
        self.assertEqual(codes(errors_for("(flat) Tick {sfx:ticking_clock} tock.")), ["E_SFX_TYPE"])
        self.assertEqual(codes(errors_for("(flat) Tick {bed:ding} tock.")), ["E_BED_TYPE"])

    def test_unknown_sfx_bed_img_cam_style(self):
        (e,) = errors_for("(flat) Hi {sfx:dong} there.")
        self.assertEqual(e.code, "E_SFX_UNKNOWN")
        self.assertIn('Did you mean "ding"', e.message)
        self.assertEqual(codes(errors_for("(flat) Hi {bed:rain} there.")), ["E_BED_UNKNOWN"])
        self.assertEqual(codes(errors_for("(flat) Hi {img:angry} there.")), ["E_IMG_UNKNOWN"])
        self.assertEqual(codes(errors_for("(flat) Hi {cam:spin|2} there.")), ["E_CAM_UNKNOWN"])
        self.assertEqual(codes(errors_for("(flat) Hi {text:OK|sparkle} there.")), ["E_TEXT_STYLE"])

    def test_valid_specials(self):
        self.assertEqual(codes(errors_for("(flat) A {bed:typing_clicks} b {bed:end} c {img:scene} d.")), [])

    def test_overlay_rules(self):
        self.assertEqual(codes(errors_for("(flat) Hi {text:ONE TWO THREE FOUR|pop} there.")), ["E_TEXT_TOO_LONG"])
        self.assertEqual(codes(errors_for("(flat) Hi {text:lower|pop} there.")), ["E_TEXT_CASE"])
        self.assertEqual(codes(errors_for("(flat) Hi {text:WORK?|pop} there.")), [])


class LintTests(unittest.TestCase):
    def lint_codes(self, text):
        return codes(warnings_for(text))

    def test_generic_sfx_cap_and_repeats(self):
        narr = "(flat) {sfx:ding} a {sfx:whoosh} b {sfx:pop} c {sfx:ding} d"
        self.assertIn("W_SFX_GENERIC", self.lint_codes(script(narr)))
        narr = "(flat) {sfx:bonk} a {sfx:bonk} b {sfx:bonk} c"
        self.assertIn("W_SFX_REPEAT", self.lint_codes(script(narr)))

    def test_sfx_density(self):
        narr = "(flat) {sfx:bonk} a b {sfx:fanfare} c d {sfx:theremin} e f"
        self.assertIn("W_SFX_DENSITY", self.lint_codes(script(narr)))

    def test_pause_names_and_density(self):
        c = self.lint_codes(script("(flat) One two. [pause] Three four. [pause] Five six."))
        self.assertIn("W_PAUSE_NAMES", c)
        self.assertIn("W_PAUSE_DENSITY", c)
        self.assertIn("W_PAUSE_DENSITY", self.lint_codes(script("(flat) One two three four five.")))

    def test_visual_gap(self):
        gap = warnings_for(script("(flat) " + words(20)))
        self.assertIn("W_VISUAL_GAP", codes(gap))
        ok = self.lint_codes(script("(flat) " + words(8) + " {cam:punch_in} " + words(8)))
        self.assertNotIn("W_VISUAL_GAP", ok)

    def test_scene_length(self):
        long_scene = "\n".join("(flat) " + words(29) for _ in range(6))
        self.assertIn("W_SCENE_LENGTH", self.lint_codes(script(long_scene)))

    def test_wordcount_vs_target(self):
        self.assertIn("W_WORDCOUNT", self.lint_codes(script("(flat) Just a few words.", target=7)))

    def test_camera_spacing(self):
        near = "(flat) {cam:zoom_in|6} one two three {cam:pan_left|6} four five"
        self.assertIn("W_CAM_CLOSE", self.lint_codes(script(near)))
        transient = "(flat) {cam:zoom_in|6} one two three {cam:punch_in} four five"
        self.assertNotIn("W_CAM_CLOSE", self.lint_codes(script(transient)))

    def test_same_camera_opener(self):
        text = front_matter() + "".join(scene(i, "(flat) {cam:zoom_in|6} Open now.") for i in range(1, 5))
        self.assertIn("W_CAM_OPENER", self.lint_codes(text))

    def test_repeated_scene_ending(self):
        text = front_matter() + "".join(scene(i, "(flat) Setup here. {img:sarcastic_wow|2} Punchline.") for i in range(1, 5))
        self.assertIn("W_ENDING_REPEAT", self.lint_codes(text))

    def test_too_many_reaction_images(self):
        narr = "(flat) {img:sarcastic_wow} a {img:smug_genius} b {img:playful_wink} c"
        self.assertIn("W_IMG_COUNT", self.lint_codes(script(narr)))

    def test_front_matter_notes(self):
        fm = ('claims:\n  - "bare claim"\n  - "claim | unverified | soft"\n'
              'sfx_requests:\n  - "cash_drawer: a drawer"\nsensitivity:\n  - health\n')
        c = self.lint_codes(script("(flat) Hello there.", fm))
        for expected in ("W_CLAIM_INCOMPLETE", "W_CLAIM_UNVERIFIED", "W_SFX_REQUESTS", "W_SENSITIVITY"):
            self.assertIn(expected, c)

    def test_sensitivity_none_is_quiet(self):
        c = self.lint_codes(script("(flat) Hello there.", "sensitivity:\n  - none\n"))
        self.assertNotIn("W_SENSITIVITY", c)


class AssetTests(unittest.TestCase):
    def test_missing_assets_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sfx"))
            os.makedirs(os.path.join(tmp, "characters"))
            open(os.path.join(tmp, "sfx", "pop.wav"), "w").close()
            open(os.path.join(tmp, "characters", "smug_genius.png"), "w").close()
            res = parse_text(script("(flat) A {sfx:pop} b {sfx:ding} c {img:smug_genius} d {img:playful_wink} e."))
            diags = validate(res.project, CFG, assets_dir=tmp)
            missing = [d.message for d in diags if d.code == "E_ASSET_MISSING"]
            self.assertEqual(len(missing), 2)
            self.assertTrue(any("ding" in m for m in missing))
            self.assertTrue(any("playful_wink" in m for m in missing))
            self.assertIn("W_ASSET_MISSING", codes(diags))     # scene image not generated yet


class ReportTests(unittest.TestCase):
    def test_report_ok_and_failed(self):
        res, extra = parse_text(script("(flat) Hello there.")), []
        extra = validate(res.project, CFG)
        self.assertIn("RESULT: OK", render_report("x.md", res, extra, CFG))
        bad = parse_text(script("(nope) Hello there."))
        report = render_report("x.md", bad, validate(bad.project, CFG), CFG)
        self.assertIn("FAILED", report)
        self.assertIn("[x] all emotion names valid", report)
        self.assertIn("scene01 / seg01", report)


if __name__ == "__main__":
    unittest.main()
