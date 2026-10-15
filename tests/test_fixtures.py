"""Regression tests against the two real scripts. The exact counts are pins:
if they change, the parser's behaviour changed and someone should look."""
import unittest

from parser import validate
from tests.helpers import CFG, codes, parse_text, read_fixture


class ExampleFixture(unittest.TestCase):
    def setUp(self):
        self.res = parse_text(read_fixture("example_v11.md"))

    def test_clean_parse(self):
        self.assertEqual(self.res.errors, [])
        self.assertEqual([d for d in validate(self.res.project, CFG) if d.level == "error"], [])

    def test_shape(self):
        s1, s2 = self.res.project.scenes
        self.assertEqual((len(s1.segments), len(s2.segments)), (4, 8))
        self.assertEqual((s1.music, s2.music), ("none", "none"))

    def test_scene1_details(self):
        segs = self.res.project.scenes[0].segments
        first = segs[0]
        self.assertEqual((first.emotion, first.modifier), ("deadpan", "flat"))
        record = next(c for c in first.cues if c.name == "record_scratch")
        self.assertEqual(first.words[record.word_index], "instead")
        work = next(c for c in first.cues if c.type == "text")
        self.assertEqual((work.name, first.words[work.word_index]), ("WORK?", "work."))
        # "Great choice..." inherited the tag; the [pause] line before it set pause_after on segment 1
        self.assertEqual(segs[0].pause_after, 0.35)
        self.assertTrue(segs[1].emotion_inherited)
        # last line is a lone [pause:long] attached to the final segment
        self.assertEqual(segs[-1].pause_after_tag, "long")
        self.assertEqual(segs[-1].gap_after_s, 0.80)


class NnnFixture(unittest.TestCase):
    def setUp(self):
        self.res = parse_text(read_fixture("nnn.md"))
        self.extra = validate(self.res.project, CFG)

    def test_no_errors(self):
        self.assertEqual(self.res.errors, [])
        self.assertEqual([d for d in self.extra if d.level == "error"], [])

    def test_pins(self):
        p = self.res.project
        self.assertEqual(len(p.scenes), 7)
        self.assertEqual(sum(len(s.segments) for s in p.scenes), 73)
        self.assertEqual(sum(s.word_count for s in p.scenes), 961)
        self.assertEqual(len(p.claims), 23)

    def test_pronunciation_only_in_tts_text(self):
        seg = self.res.project.scenes[0].segments[0]
        self.assertIn("NNN", seg.text)
        self.assertIn("EN-EN-EN", seg.tts_text)
        self.assertNotIn("EN-EN-EN", seg.text)

    def test_known_lint_findings(self):
        c = codes(self.extra)
        self.assertIn("W_SCENE_LENGTH", c)         # scene 6 is far too long
        self.assertIn("W_ENDING_REPEAT", c)
        self.assertIn("W_CLAIM_UNVERIFIED", c)
        self.assertIn("W_SENSITIVITY", c)
        self.assertIn("W_VISUAL_GAP", c)

    def test_bed_is_opened_and_closed(self):
        cues = [c for s in self.res.project.scenes[3].segments for c in s.cues if c.type == "bed"]
        self.assertEqual([c.name for c in cues], ["typing_clicks", "end"])


if __name__ == "__main__":
    unittest.main()
