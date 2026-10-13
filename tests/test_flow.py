"""Focused checks for stale puzzles, wrong guesses and stopping during model calls."""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
from PIL import Image


def puzzle_payload():
    image = Image.new("RGB", (4, 4), "red")
    image.putpixel((1, 2), (0, 255, 0))
    output = io.BytesIO()
    image.save(output, "PNG")
    sprite = {"png_base64": base64.b64encode(output.getvalue()).decode(), "crop": [1, 1, 1, 1]}
    return {"level_id": "118", "display_index": 118, "expected_length": 2,
            "state": "Playing", "retry_count": 0, "images": [sprite, sprite]}


class FakeBridge:
    def __init__(self):
        self.puzzle = puzzle_payload()
        self.submissions = []
        self.advanced = []

    def call(self, method, *args):
        if method == "pronunciations":
            known = {"纽": "niu3", "约": "yue1", "扣": "kou4", "恭": "gong1", "蛋": "dan4", "送": "song4", "松": "song1", "花": "hua1"}
            return {word: [[known.get(char, "unknown1")] for char in word] for word in args[0]}
        if method == "restart":
            self.puzzle.update(state="Playing", retry_count=0, feedback_history=[])
            return {"requested": True}
        if method == "puzzle":
            return self.puzzle.copy()
        if method == "status":
            return {key: value for key, value in self.puzzle.items() if key != "images"}
        if method == "submit":
            level_id, answer = args
            if level_id != self.puzzle["level_id"]:
                raise RuntimeError("题号改变")
            self.submissions.append(answer)
            self.puzzle["retry_count"] += 1
            self.puzzle["state"] = "Win" if answer == "纽约" else "Playing"
            return self.call("status")
        if method == "next":
            self.advanced.append(args[0])
            self.puzzle.update(level_id="119", state="Playing", retry_count=0)
            return {"requested": True}
        raise AssertionError(method)


class FlowTests(unittest.TestCase):
    def config(self):
        return {**badpun.DEFAULTS, "between_levels_seconds": 0,
                "transition_timeout_seconds": 1, "api_key": "test-key"}

    def test_sprite_crop_uses_bottom_left_coordinates(self):
        puzzle = badpun.prepare_puzzle(puzzle_payload())
        pixels = base64.b64decode(puzzle["image_data_urls"][0].split(",", 1)[1])
        with Image.open(io.BytesIO(pixels)) as image:
            self.assertEqual(image.size, (1, 1))
            self.assertEqual(image.getpixel((0, 0)), (0, 255, 0))

    def test_wrong_guess_is_returned_to_model_before_advancing(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        history = []
        def solve(puzzle, previous):
            history.append(list(previous))
            return {"answer": "纽扣" if not previous else "纽约", "reason": "猜测", "usage": {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", solve):
            runner._run("current")
        self.assertEqual(bridge.submissions, ["纽扣", "纽约"])
        self.assertEqual(history[1][0]["state"], "Playing")
        self.assertEqual(bridge.advanced, [])
        self.assertEqual(runner.snapshot()["phase"], "completed")

    def test_stop_during_model_request_does_not_submit(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        def solve(puzzle, previous):
            runner.stop()
            return {"answer": "纽约", "reason": "猜测", "usage": {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", solve):
            runner._run("current")
        self.assertEqual(bridge.submissions, [])
        self.assertEqual(bridge.advanced, [])
        self.assertEqual(runner.snapshot()["phase"], "stopped")

    def test_stale_model_result_does_not_submit_to_new_level(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        def solve(puzzle, previous):
            bridge.puzzle["level_id"] = "119"
            return {"answer": "纽约", "reason": "猜测", "usage": {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", solve):
            runner._run("current")
        self.assertEqual(bridge.submissions, [])
        self.assertEqual(runner.snapshot()["phase"], "error")

    def test_vision_request_includes_images_and_checks_length(self):
        solver = badpun.VisionSolver(self.config())
        puzzle = badpun.prepare_puzzle(puzzle_payload())
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": '{"answer":"纽约"}'}}]}
        with patch.object(badpun, "request_model", response):
            result = solver.solve(puzzle, [])
        self.assertEqual(result["answer"], "纽约")
        self.assertNotIn("reason", result)
        self.assertEqual([block["type"] for block in captured[0]["messages"][1]["content"]], ["text", "image_url", "image_url"])
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("api_key", json.dumps(captured))

    def test_no_game_confirmation_stops_instead_of_guessing_again(self):
        bridge = FakeBridge()
        original_call = bridge.call
        def no_submission(method, *args):
            if method == "submit":
                bridge.submissions.append(args[1])
                return original_call("status")
            return original_call(method, *args)
        bridge.call = no_submission
        runner = badpun.AutoRunner(bridge, {**self.config(), "transition_timeout_seconds": 0.01})
        prediction = {"answer": "纽约", "reason": "猜测", "usage": {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", return_value=prediction):
            runner._run("current")
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertEqual(bridge.advanced, [])
        self.assertEqual(runner.snapshot()["phase"], "error")

    def test_orange_character_and_pinyin_feedback_reaches_model(self):
        bridge = FakeBridge()
        bridge.puzzle["feedback_history"] = [{"guess": "约纽", "letters": [
            {"position": 1, "character": "约", "pinyin": "yue1", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"},
            {"position": 2, "character": "纽", "pinyin": "niu3", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"}
        ]}]
        runner = badpun.AutoRunner(bridge, self.config())
        seen = []
        def solve(puzzle, previous):
            seen.extend(previous[0]["letters"])
            return {"answer": "纽约", "reason": "按反馈换位置", "usage": {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", solve):
            runner._run("current")
        self.assertEqual(seen[0]["pinyin_feedback"], "present_elsewhere")
        self.assertEqual(seen[1]["character_feedback"], "present_elsewhere")
        self.assertEqual(bridge.submissions, ["纽约"])

    def test_invalid_length_is_retried_without_submitting_to_game(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        predictions = [badpun.PredictionError("应为两个字", "纽约市", "length"),
                       {"answer": "纽约", "reason": "修正字数", "usage": {}}]
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", side_effect=predictions):
            runner._run("current")
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertEqual(runner.snapshot()["phase"], "completed")


if __name__ == "__main__":
    unittest.main()
