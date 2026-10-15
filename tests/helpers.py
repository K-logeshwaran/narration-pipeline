"""Shared helpers for building small scripts in tests."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from parser import load_channel_config, parse_text, validate  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
CFG = load_channel_config()


def front_matter(extra="", target=1):
    return ("---\ntitle: Test Video\nchannel: main\ncharacter: deadpan_doug\n"
            f"target_minutes: {target}\n{extra}---\n")


def scene(n, narration, fields=None):
    f = {"image": f"s{n:02d}_test", "prompt": "A test prompt. Plain white background, 16:9."}
    f.update(fields or {})
    head = "".join(f"{k}: {v}\n" for k, v in f.items() if v is not None)
    return f"\n## SCENE {n:02d}: Test {n}\n{head}\n### Narration\n{narration}\n"


def script(narration, extra_fm="", target=1):
    """A one-scene script."""
    return front_matter(extra_fm, target) + scene(1, narration)


def parse(narration, extra_fm="", target=1):
    return parse_text(script(narration, extra_fm, target))


def codes(diags, level=None):
    return [d.code for d in diags if level is None or d.level == level]


def check(text):
    """Parse + validate. Returns (result, all_diagnostics)."""
    res = parse_text(text)
    return res, res.diagnostics + validate(res.project, CFG)


def segs(result, scene_index=0):
    return result.project.scenes[scene_index].segments


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()
