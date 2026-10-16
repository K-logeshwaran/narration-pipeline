import json
import os
import unittest

from parser import parse_file
from tests.helpers import (ROOT, codes, front_matter, parse, parse_text, read_fixture, scene,
                           script, segs)

BURGER_SCRIPT = os.path.join(ROOT, "script", "burger.md")


class FrontMatterTests(unittest.TestCase):
    def test_quoted_values_with_colons(self):
        res = parse_text(read_fixture("example_v11.md"))
        p = res.project
        self.assertEqual(p.title, "Unfair Psychological Tricks to Stop Procrastinating (Without Waking Up at 4 AM)")
        self.assertIn("Why You're Not Lazy: A Biological Autopsy", p.titles_alt)
        self.assertEqual(p.target_minutes, 7)
        self.assertEqual(res.errors, [])

    def test_claims_split_into_three_parts(self):
        res = parse_text(read_fixture("example_v11.md"))
        c = res.project.claims[0]
        self.assertEqual(c["claim"], "Hyperbolic discounting: brains overvalue immediate rewards")
        self.assertEqual(c["source"], "Ainslie, 1975 (verify)")
        self.assertEqual(c["hedge"], "stated as well established")

    def test_claim_missing_parts_are_empty_strings(self):
        res = parse("(flat) Hello there.", extra_fm='claims:\n  - "just a claim"\n')
        self.assertEqual(res.project.claims[0], {"claim": "just a claim", "source": "", "hedge": ""})

    def test_pronunciations_and_sfx_requests(self):
        fm = ('pronunciations:\n  - Zeigarnik = ZYE-gar-nik\n'
              'sfx_requests:\n  - "cash_drawer: a cash drawer opening"\n')
        res = parse("(flat) Hello there.", extra_fm=fm)
        self.assertEqual(res.project.pronunciations, {"Zeigarnik": "ZYE-gar-nik"})
        self.assertEqual(res.project.sfx_requests, [{"name": "cash_drawer", "description": "a cash drawer opening"}])

    def test_unquoted_colon_title_parses_but_warns(self):
        text = "---\ntitle: Foo: Bar\nchannel: main\ncharacter: d\ntarget_minutes: 1\n---\n" + scene(1, "(flat) Hi there.")
        res = parse_text(text)
        self.assertEqual(res.project.title, "Foo: Bar")
        self.assertIn("W_QUOTING", codes(res.diagnostics, "warning"))
        self.assertEqual(res.errors, [])

    def test_missing_required_key(self):
        text = "---\nchannel: main\ncharacter: d\ntarget_minutes: 1\n---\n" + scene(1, "(flat) Hi there.")
        self.assertIn("E_FRONT_KEY", codes(parse_text(text).diagnostics, "error"))

    def test_bad_target_minutes(self):
        text = "---\ntitle: T\nchannel: m\ncharacter: d\ntarget_minutes: soon\n---\n" + scene(1, "(flat) Hi there.")
        self.assertIn("E_FRONT_VALUE", codes(parse_text(text).diagnostics, "error"))

    def test_unclosed_front_matter(self):
        self.assertIn("E_FRONT_UNCLOSED", codes(parse_text("---\ntitle: T\n").diagnostics, "error"))

    def test_missing_front_matter(self):
        text = scene(1, "(flat) Hi there.")
        self.assertIn("E_FRONT_MISSING", codes(parse_text(text).diagnostics, "error"))

    def test_bom_and_windows_line_endings(self):
        text = "\ufeff" + script("(flat) Hi there.\nAgain now.").replace("\n", "\r\n")
        res = parse_text(text)
        self.assertEqual(res.errors, [])
        self.assertEqual(len(segs(res)), 2)

    def test_code_fence_is_tolerated(self):
        text = "```markdown\n" + script("(flat) Hi there.") + "```\n"
        res = parse_text(text)
        self.assertEqual(res.errors, [])
        self.assertIn("W_CODE_FENCE", codes(res.diagnostics, "warning"))
        self.assertEqual(len(segs(res)), 1)


class SegmentationTests(unittest.TestCase):
    def test_ids_are_sequential_and_scoped_to_scene(self):
        text = front_matter() + scene(1, "(flat) One here.\nTwo here.") + scene(2, "(flat) Three here.")
        res = parse_text(text)
        self.assertEqual([s.id for s in segs(res, 0)], ["scene01_seg01", "scene01_seg02"])
        self.assertEqual([s.id for s in segs(res, 1)], ["scene02_seg01"])

    def test_pause_only_line_attaches_to_previous_segment(self):
        res = parse("(deadpan) Hello there friend.\n[pause]\nGoodbye for now.")
        a, b = segs(res)
        self.assertEqual((a.pause_after_tag, a.pause_after), ("default", 0.35))
        self.assertEqual(a.gap_after_s, 0.35)          # explicit pause only, no extra line gap
        self.assertIsNone(b.pause_before)

    def test_mid_line_pause_splits_into_two_segments(self):
        res = parse("(smug) One thing. [pause:beat] Two things.")
        a, b = segs(res)
        self.assertEqual((a.text, b.text), ("One thing.", "Two things."))
        self.assertEqual((a.pause_after_tag, a.pause_after), ("beat", 0.45))
        self.assertEqual((a.emotion, b.emotion), ("smug", "smug"))
        self.assertFalse(b.emotion_inherited)

    def test_leading_pause_becomes_pause_before(self):
        res = parse("(flat) First line here.\n(flat) [pause] Second line here.")
        a, b = segs(res)
        self.assertEqual(b.pause_before, 0.35)
        self.assertEqual(a.gap_after_s, 0.0)           # no stacking with the automatic gap

    def test_automatic_gap_between_plain_lines(self):
        res = parse("(flat) First line here.\nSecond line here.")
        a, b = segs(res)
        self.assertEqual(a.gap_after_s, 0.22)
        self.assertEqual(b.gap_after_s, 0.0)           # last segment of a scene

    def test_all_pause_lengths_resolve_from_config(self):
        res = parse("(flat) A a. [pause:short] B b. [pause] C c. [pause:beat] D d. [pause:long] E e.")
        self.assertEqual([s.pause_after for s in segs(res)], [0.20, 0.35, 0.45, 0.80, None])

    def test_adjacent_pauses_inline_and_across_lines(self):
        self.assertIn("E_PAUSE_ADJACENT", codes(parse("(flat) Hello. [pause] [pause:short] More.").diagnostics))
        self.assertIn("E_PAUSE_ADJACENT", codes(parse("(flat) Hello there. [pause]\n[pause:long]\nMore words.").diagnostics))

    def test_unknown_pause_tag(self):
        res = parse("(flat) Hello. [pause:medium] More.")
        self.assertIn("E_PAUSE_UNKNOWN", codes(res.diagnostics, "error"))

    def test_stray_bracket_text(self):
        self.assertIn("E_BRACKET_STRAY", codes(parse("(flat) Hello [laughs] there.").diagnostics, "error"))

    def test_scene_opening_with_pause_only_line(self):
        res = parse("(flat) [pause:long]\nFirst words here.")
        self.assertEqual(segs(res)[0].pause_before, 0.80)


class EmotionTests(unittest.TestCase):
    def test_inheritance(self):
        res = parse("(smug) First line.\nSecond line.\n(flat) Third line.")
        a, b, c = segs(res)
        self.assertEqual((a.emotion, b.emotion, c.emotion), ("smug", "smug", "flat"))
        self.assertEqual((a.emotion_inherited, b.emotion_inherited, c.emotion_inherited), (False, True, False))

    def test_first_line_without_tag_is_an_error(self):
        self.assertIn("E_EMOTION_MISSING", codes(parse("No tag here.").diagnostics, "error"))

    def test_emotion_does_not_leak_between_scenes(self):
        text = front_matter() + scene(1, "(smug) One two.") + scene(2, "No tag here.")
        self.assertIn("E_EMOTION_MISSING", codes(parse_text(text).diagnostics, "error"))

    def test_compound_and_normalization(self):
        res = parse("(Deadpan, Flat) Hello.\n(fake excitement) Yay.\n(fake-fancy) Ahem.")
        a, b, c = segs(res)
        self.assertEqual((a.emotion, a.modifier), ("deadpan", "flat"))
        self.assertEqual(b.emotion, "fake_excitement")
        self.assertEqual(c.emotion, "fake_fancy")

    def test_malformed_tags(self):
        self.assertIn("E_EMOTION_MALFORMED", codes(parse("(a, b, c) Hello.").diagnostics, "error"))
        self.assertIn("E_EMOTION_MALFORMED", codes(parse("(smug Hello.").diagnostics, "error"))
        self.assertIn("E_EMOTION_MALFORMED", codes(parse("(smug, ) Hello.").diagnostics, "error"))


class CueTests(unittest.TestCase):
    def test_cue_binds_to_next_word(self):
        (seg,) = segs(parse("(flat) One two {sfx:pop} three four."))
        cue = seg.cues[0]
        self.assertEqual(seg.text, "One two three four.")     # cues are removed from captions
        self.assertEqual((cue.word_index, cue.char_offset), (2, 8))

    def test_cue_at_end_of_segment(self):
        (seg,) = segs(parse("(flat) One two three. {sfx:pop}"))
        self.assertEqual(seg.cues[0].word_index, 3)
        self.assertEqual(seg.cues[0].char_offset, len(seg.text))

    def test_adjacent_cues_share_a_word(self):
        (seg,) = segs(parse("(flat) {sfx:pop}{cam:cut} Go now."))
        self.assertEqual([(c.type, c.word_index) for c in seg.cues], [("sfx", 0), ("cam", 0)])

    def test_cue_details(self):
        (seg,) = segs(parse("(flat) {sfx:pop+0.15} A {sfx:bonk-0.1} b {text:5 SCAMS|pop|2} c "
                            "{cam:zoom_in|8|1.2} d {img:smug_genius|3} e {bed:ticking_clock} f {bed:end} g."))
        sfx1, sfx2, txt, cam, img, bed, end = seg.cues
        self.assertEqual((sfx1.name, sfx1.offset_s), ("pop", 0.15))
        self.assertEqual((sfx2.name, sfx2.offset_s), ("bonk", -0.1))
        self.assertEqual((txt.name, txt.style, txt.duration_s), ("5 SCAMS", "pop", 2.0))
        self.assertEqual((cam.name, cam.duration_s, cam.scale), ("zoom_in", 8.0, 1.2))
        self.assertEqual((img.name, img.duration_s), ("smug_genius", 3.0))
        self.assertEqual((bed.type, bed.name, end.name), ("bed", "ticking_clock", "end"))

    def test_cue_only_line_is_carried_to_next_segment(self):
        a, b = segs(parse("(flat) First line here.\n{bed:end}\nSecond line here."))
        self.assertEqual(a.cues, [])
        self.assertTrue(b.cues[0].carried)
        self.assertEqual(b.cues[0].word_index, 0)

    def test_cue_after_pause_binds_to_following_text(self):
        a, b = segs(parse("(flat) Hello there. [pause] {sfx:pop} Again now."))
        self.assertEqual(a.cues, [])
        self.assertEqual(b.cues[0].word_index, 0)

    def test_cue_errors(self):
        self.assertIn("E_CUE_TYPE", codes(parse("(flat) Hi {foo:bar} there.").diagnostics))
        self.assertIn("E_CUE_MALFORMED", codes(parse("(flat) Hi {pop} there.").diagnostics))
        self.assertIn("E_CUE_ARG", codes(parse("(flat) Hi {text:HELLO} there.").diagnostics))
        self.assertIn("E_CUE_ARG", codes(parse("(flat) Hi {cam:zoom_in|fast} there.").diagnostics))
        self.assertIn("E_CUE_ARG", codes(parse("(flat) Hi {sfx:pop|0.5} there.").diagnostics))
        self.assertIn("E_BRACE_STRAY", codes(parse("(flat) Hi {sfx:pop there.").diagnostics))

    def test_midword_cue_warns(self):
        res = parse("(flat) un{sfx:pop}believable thing.")
        self.assertIn("W_CUE_MIDWORD", codes(res.diagnostics, "warning"))
        self.assertEqual(segs(res)[0].cues[0].word_index, 0)


class TextTests(unittest.TestCase):
    def test_pronunciation_applies_to_tts_only(self):
        res = parse("(flat) NNN's rules are simple. Read the NNNN notes.", extra_fm="pronunciations:\n  - NNN = EN-EN-EN\n")
        (seg,) = segs(res)
        self.assertEqual(seg.text, "NNN's rules are simple. Read the NNNN notes.")
        self.assertEqual(seg.tts_text, "EN-EN-EN's rules are simple. Read the NNNN notes.")

    def test_pronunciation_is_case_insensitive_whole_word_and_flexible_whitespace(self):
        fm = (
            "pronunciations:\n"
            "  - smashburger = smash BUR-ger\n"
            "  - patty melt = PAT-ee melt\n"
            "  - cheeseburger = CHEEZ-bur-ger\n"
        )
        res = parse(
            "(flat) Cheeseburger. A cheeseburger and a Smash Burger, plus a smash-burger and a Patty Melt.",
            extra_fm=fm,
        )
        (seg,) = segs(res)
        self.assertEqual(
            seg.text,
            "Cheeseburger. A cheeseburger and a Smash Burger, plus a smash-burger and a Patty Melt.",
        )
        self.assertEqual(
            seg.tts_text,
            "CHEEZ-bur-ger. A CHEEZ-bur-ger and a smash BUR-ger, plus a smash BUR-ger and a PAT-ee melt.",
        )

    @unittest.skipUnless(os.path.exists(BURGER_SCRIPT), "script/burger.md not found")
    def test_burger_script_pronunciations_match_capitalized_and_spaced_forms(self):
        res = parse_file(BURGER_SCRIPT)
        sc_cheese = res.project.scenes[1]       # SCENE 02: Cheeseburger
        sc_smash = res.project.scenes[2]        # SCENE 03: Smash Burger
        sc_patty = res.project.scenes[11]       # SCENE 12: Patty Melt

        self.assertEqual(sc_cheese.segments[0].text, "Cheeseburger.")
        self.assertEqual(sc_cheese.segments[0].tts_text, "CHEEZ-bur-ger.")
        self.assertIn("A CHEEZ-bur-ger takes", sc_cheese.segments[1].tts_text)

        self.assertEqual(sc_smash.segments[0].text, "Smash Burger.")
        self.assertEqual(sc_smash.segments[0].tts_text, "smash BUR-ger.")
        self.assertIn("A smash BUR-ger starts", sc_smash.segments[1].tts_text)

        self.assertEqual(sc_patty.segments[0].text, "Patty Melt.")
        self.assertEqual(sc_patty.segments[0].tts_text, "PAT-ee melt.")
        self.assertIn("A PAT-ee melt combines", sc_patty.segments[1].tts_text)

    def test_unusual_symbols_warn(self):
        res = parse("(flat) Fifty % of people *agree*.")
        self.assertIn("W_SYMBOL", codes(res.diagnostics, "warning"))

    def test_words_list_matches_word_count(self):
        (seg,) = segs(parse("(flat) Well - that was, um, fine."))
        self.assertEqual(seg.word_count, len(seg.words))
        self.assertNotIn("-", seg.words)          # a lone dash is not a spoken word


class SceneStructureTests(unittest.TestCase):
    def test_non_consecutive_scene_numbers(self):
        text = front_matter() + scene(1, "(flat) One.") + scene(3, "(flat) Three.")
        self.assertIn("E_SCENE_NUMBER", codes(parse_text(text).diagnostics, "error"))

    def test_missing_narration_heading(self):
        text = front_matter() + "\n## SCENE 01: T\nimage: s01_t\nprompt: p\n"
        self.assertIn("E_SCENE_NO_NARRATION", codes(parse_text(text).diagnostics, "error"))

    def test_empty_narration(self):
        text = front_matter() + "\n## SCENE 01: T\nimage: s01_t\nprompt: p\n\n### Narration\n"
        self.assertIn("E_SCENE_EMPTY", codes(parse_text(text).diagnostics, "error"))

    def test_missing_image_and_prompt(self):
        text = front_matter() + scene(1, "(flat) Hello there.", {"image": None, "prompt": None})
        errs = [d for d in parse_text(text).diagnostics if d.code == "E_SCENE_FIELD"]
        self.assertEqual(len(errs), 2)

    def test_image_name_format_warns(self):
        text = front_matter() + scene(1, "(flat) Hello there.", {"image": "Autopsy Pic"})
        self.assertIn("W_IMAGE_FORMAT", codes(parse_text(text).diagnostics, "warning"))

    def test_malformed_scene_header(self):
        text = front_matter() + "\n## Scene one\nimage: x\n"
        self.assertIn("E_SCENE_HEADER", codes(parse_text(text).diagnostics, "error"))

    def test_text_before_first_scene(self):
        text = front_matter() + "\nSome stray intro.\n" + scene(1, "(flat) Hello there.")
        self.assertIn("E_STRAY_TEXT", codes(parse_text(text).diagnostics, "error"))

    def test_unknown_line_before_narration(self):
        text = front_matter() + scene(1, "(flat) Hello there.").replace("### Narration", "mood: sad\n\n### Narration")
        self.assertIn("E_SCENE_FIELD", codes(parse_text(text).diagnostics, "error"))

    def test_music_persists_until_changed(self):
        text = (front_matter() + scene(1, "(flat) One two.", {"music": "calm_loop"})
                + scene(2, "(flat) Three four.") + scene(3, "(flat) Five six.", {"music": "none"}))
        s = parse_text(text).project.scenes
        self.assertEqual([x.music for x in s], ["calm_loop", "calm_loop", "none"])
        self.assertEqual([x.music_explicit for x in s], [True, False, True])

    def test_music_defaults_to_none(self):
        text = front_matter() + scene(1, "(flat) One two.")
        self.assertEqual(parse_text(text).project.scenes[0].music, "none")


class DeterminismTests(unittest.TestCase):
    def test_same_input_gives_identical_json(self):
        text = read_fixture("nnn.md")
        a = json.dumps(parse_text(text).project.to_dict(), indent=2)
        b = json.dumps(parse_text(text).project.to_dict(), indent=2)
        self.assertEqual(a, b)

    def test_source_hash_changes_with_content(self):
        a = parse_text(script("(flat) One two.")).project.source["sha256"]
        b = parse_text(script("(flat) One three.")).project.source["sha256"]
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()