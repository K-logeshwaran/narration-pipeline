import tempfile
import unittest
from pathlib import Path

from parser.config import load_config_for_script


class ChannelConfigVoiceTests(unittest.TestCase):
    def test_explainer_script_resolves_to_am_michael_with_no_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "sample_explainer.md"
            script_path.write_text(
                "---\n"
                "title: Sample Explainer\n"
                "channel: explainer\n"
                "target_minutes: 4\n"
                "---\n\n"
                "## SCENE 01: Item One\n"
                "search: sample item one | backup item one\n\n"
                "### Narration\n"
                "Item One.\n"
                "This is a calm explanation line.\n",
                encoding="utf-8",
            )
            cfg = load_config_for_script(script_path)
            self.assertEqual(cfg["channel"], "explainer")
            self.assertEqual(cfg["tts"]["backend"], "kokoro")
            self.assertEqual(cfg["tts"]["voice"], "am_michael")
            self.assertEqual(cfg["tts"]["base_speed"], 0.90)

    def test_cli_overrides_preserve_precedence(self):
        cfg_backend = load_config_for_script("script/burger.md", backend="fake")
        self.assertEqual(cfg_backend["tts"]["backend"], "fake")
        self.assertEqual(cfg_backend["tts"]["voice"], "am_michael")

        cfg_voice = load_config_for_script("script/burger.md", voice="af_heart")
        self.assertEqual(cfg_voice["tts"]["voice"], "af_heart")

        cfg_file = load_config_for_script("script/burger.md", config_path="config/channel_main.json")
        self.assertEqual(cfg_file["tts"]["voice"], "am_fenrir")


if __name__ == "__main__":
    unittest.main()