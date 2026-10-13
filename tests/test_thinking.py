"""Test reasoning controls at the model request boundary."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
from desktop_service import simple_model_config
from test_flow import puzzle_payload


class ThinkingTests(unittest.TestCase):
    def request(self, **changes):
        config = {**badpun.DEFAULTS, "api_key": "test-key", **changes}
        captured = []
        def response(request, timeout, *args):
            captured.append((json.loads(request.data), timeout))
            return {"choices": [{"message": {"content": '{"answer":"纽约"}', "reasoning_content": "测试用内部思考"}}]}
        with patch.object(badpun, "request_model", response):
            result = badpun.VisionSolver(config).solve(badpun.prepare_puzzle(puzzle_payload()), [])
        self.assertEqual(result["answer"], "纽约")
        self.assertNotIn("reasoning_content", result)
        return captured[0]

    def test_defaults_are_deepseek_with_high_thinking(self):
        config = simple_model_config({})
        self.assertEqual(config["model_base_url"], "https://api.deepseek.com")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertTrue(config["thinking_enabled"])
        self.assertEqual(config["reasoning_effort"], "high")

    def test_each_enabled_level_is_sent_and_has_room_for_reasoning(self):
        for effort in ("low", "high", "max"):
            with self.subTest(effort=effort):
                body, timeout = self.request(thinking_enabled=True, reasoning_effort=effort)
                self.assertEqual(body["thinking"], {"type": "enabled"})
                self.assertEqual(body["reasoning_effort"], effort)
                self.assertNotIn("max_tokens", body)
                self.assertNotIn("temperature", body)
                self.assertGreaterEqual(timeout, 180)

    def test_off_is_preserved_and_uses_short_answer_limits(self):
        config = simple_model_config({"thinking_enabled": False, "reasoning_effort": "low"})
        self.assertFalse(config["thinking_enabled"])
        body, timeout = self.request(thinking_enabled=False)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["max_tokens"], badpun.DEFAULTS["max_output_tokens"])
        self.assertEqual(timeout, badpun.DEFAULTS["model_timeout_seconds"])

    def test_mimo_thinking_sends_only_its_supported_switch(self):
        body, timeout = self.request(model_base_url="https://api.xiaomimimo.com/v1", model="mimo-v2.6-pro")
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("max_completion_tokens", body)
        self.assertGreaterEqual(timeout, 180)


if __name__ == "__main__":
    unittest.main()
