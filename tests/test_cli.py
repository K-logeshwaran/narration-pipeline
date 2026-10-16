import contextlib
import io
import json
import os
import tempfile
import unittest

import main
from tests.helpers import FIXTURES, script


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main.main(argv)
    return code, buf.getvalue()


class CliTests(unittest.TestCase):
    def test_validate_ok_exit_code(self):
        code, out = run(["validate", os.path.join(FIXTURES, "nnn.md")])
        self.assertEqual(code, 0)
        self.assertIn("RESULT: OK", out)

    def test_strict_fails_on_warnings(self):
        code, _ = run(["validate", os.path.join(FIXTURES, "nnn.md"), "--strict"])
        self.assertEqual(code, 1)

    def test_parse_writes_project_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run(["parse", os.path.join(FIXTURES, "example_v11.md"), "-o", tmp])
            self.assertEqual(code, 0)
            with open(os.path.join(tmp, "project.json"), encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["schema_version"], "1.1")
            self.assertEqual(len(data["scenes"]), 2)

    def test_parse_output_is_byte_identical_between_runs(self):
        blobs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                run(["parse", os.path.join(FIXTURES, "nnn.md"), "-o", tmp])
                with open(os.path.join(tmp, "project.json"), "rb") as f:
                    blobs.append(f.read())
        self.assertEqual(blobs[0], blobs[1])

    def test_errors_block_output_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.md")
            with open(bad, "w", encoding="utf-8") as f:
                f.write(script("(nope) Hello there."))
            out_dir = os.path.join(tmp, "out")
            code, out = run(["parse", bad, "-o", out_dir])
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(os.path.join(out_dir, "project.json")))
            self.assertIn("NOT written", out)
            code, _ = run(["parse", bad, "-o", out_dir, "--force"])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(os.path.join(out_dir, "project.json")))

    def test_bare_script_path_runs_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run([os.path.join(FIXTURES, "example_v11.md"), "--backend", "fake",
                             "--no-polish", "--no-export", "-o", tmp])
            self.assertEqual(code, 0)
            self.assertIn("12 synthesized", out)
            #self.assertIn("not built yet", out)


if __name__ == "__main__":
    unittest.main()
