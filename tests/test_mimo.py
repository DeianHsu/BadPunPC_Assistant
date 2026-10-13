"""Validate the documented MiMo request contract without real credentials or API calls."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
from test_flow import puzzle_payload


class MimoTests(unittest.TestCase):
    def config(self):
        return {**badpun.DEFAULTS, "model_base_url": "https://api.xiaomimimo.com/v1",
                "model": "mimo-v2.6-pro", "api_key": "test-mimo-key", "thinking_enabled": False}

    def test_mimo_request_has_images_disabled_thinking_and_correct_token_parameter(self):
        captured = []
        def response(request, *args):
            captured.append((request.full_url, json.loads(request.data)))
            return {"choices": [{"message": {"content": '{"answer":"纽约"}'}}]}
        with patch.object(badpun, "request_model", response):
            result = badpun.VisionSolver(self.config()).solve(badpun.prepare_puzzle(puzzle_payload()), [])
        url, body = captured[0]
        self.assertEqual(url, "https://api.xiaomimimo.com/v1/chat/completions")
        self.assertEqual(body["model"], "mimo-v2.6-pro")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_completion_tokens"], badpun.DEFAULTS["max_output_tokens"])
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual([block["type"] for block in body["messages"][1]["content"]], ["text", "image_url", "image_url"])
        self.assertEqual(result["answer"], "纽约")

    def test_mimo_does_not_fall_back_to_a_deepseek_environment_key(self):
        config = {**self.config(), "api_key": ""}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-deepseek-key"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MIMO_API_KEY"):
                badpun.VisionSolver(config).key()
        with patch.dict(os.environ, {"MIMO_API_KEY": "test-mimo-key"}, clear=True):
            self.assertEqual(badpun.VisionSolver(config).key(), "test-mimo-key")


if __name__ == "__main__":
    unittest.main()
