import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
from desktop_service import validate_config, simple_model_config, save_config, DesktopService


class DesktopTests(unittest.TestCase):
    def test_legacy_model_options_are_reset_and_thinking_stays_off(self):
        old = {**badpun.DEFAULTS, "thinking_enabled": True, "reasoning_effort": "max",
               "temperature": 1.8, "model_timeout_seconds": 500, "max_output_tokens": 65536,
               "model": "custom-vision", "api_key": "test-only-key", "run_mode": "current", "max_levels_per_run": 7}
        simple = simple_model_config(old)
        self.assertFalse(simple["thinking_enabled"])
        for key in ("temperature", "model_timeout_seconds", "max_output_tokens", "reasoning_effort"):
            self.assertEqual(simple[key], badpun.DEFAULTS[key])
        self.assertEqual(simple["model"], old["model"])
        self.assertEqual(simple["api_key"], old["api_key"])
        self.assertEqual(simple["run_mode"], "current")
        self.assertNotIn("max_levels_per_run", simple)

    def test_generic_endpoint_selects_compatible_protocol_automatically(self):
        config = simple_model_config({**badpun.DEFAULTS, "model_base_url": "https://example.com/v1"})
        self.assertEqual(config["provider"], "compatible")
        self.assertFalse(config["thinking_enabled"])

    def test_ten_question_mode_is_valid_and_saved(self):
        config = simple_model_config({**badpun.DEFAULTS, "run_mode": "ten"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.local.json"
            save_config(path, config)
            self.assertEqual(badpun.load_config(path)["run_mode"], "ten")

    def test_config_roundtrip_keeps_user_fields_and_disabled_thinking(self):
        config = {**badpun.DEFAULTS, "model_base_url": "https://example.com/v1/", "model": "vision-test",
                  "api_key": "test-local-secret", "thinking_enabled": False, "model_timeout_seconds": "35"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.local.json"
            save_config(path, config)
            loaded = badpun.load_config(path)
        self.assertEqual(loaded["model_base_url"], "https://example.com/v1")
        self.assertEqual(loaded["model_timeout_seconds"], 35)
        self.assertEqual(loaded["api_key"], "test-local-secret")
        self.assertFalse(loaded["thinking_enabled"])

    def test_bad_endpoint_and_negative_timeout_are_rejected(self):
        for changes in [{"model_base_url": "example.com"}, {"model_timeout_seconds": -1}, {"temperature": 3}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_config({**badpun.DEFAULTS, **changes})

    def test_new_configuration_applies_to_next_run(self):
        class Runner:
            config = {}
            def snapshot(self): return {"running": False}
            def start(self, count): return count
        service = DesktopService()
        service.runner = Runner()
        mode = service.start({**badpun.DEFAULTS, "model": "new-vision", "run_mode": "current"})
        self.assertEqual(mode, "current")
        self.assertEqual(service.runner.solver.config["model"], "new-vision")

    def test_existing_run_configuration_is_preserved(self):
        class Runner:
            config = {"model": "original"}
            def snapshot(self): return {"running": True}
        service = DesktopService()
        service.runner = Runner()
        with self.assertRaises(RuntimeError):
            service.start({**badpun.DEFAULTS, "model": "new-model"})
        self.assertEqual(service.runner.config["model"], "original")


if __name__ == "__main__":
    unittest.main()
