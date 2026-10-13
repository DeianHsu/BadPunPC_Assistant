import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
import httpx
from test_flow import FakeBridge


class ModeTests(unittest.TestCase):
    def config(self):
        return {**badpun.DEFAULTS, "api_key": "test-key", "between_levels_seconds": 0,
                "transition_timeout_seconds": 1}

    def test_current_mode_keeps_the_successful_level_on_screen(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(runner.solver, "solve", return_value={"answer": "纽约"}):
            runner._run("current")
        self.assertEqual(bridge.advanced, [])
        self.assertEqual(bridge.puzzle["state"], "Win")
        self.assertEqual(runner.snapshot()["completed"], 1)

    def test_request_count_can_exceed_eight_without_stopping(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        errors = [badpun.PredictionError("测试无效返回", kind="response") for _ in range(9)]
        with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(runner.solver, "solve", side_effect=errors + [{"answer": "纽约"}]):
            runner._run("current")
        self.assertEqual(runner.snapshot()["model_requests"], 10)
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertEqual(runner.snapshot()["phase"], "completed")

    def test_ten_mode_stops_on_the_tenth_victory_screen(self):
        for starting_state in ("Playing", "Win"):
            with self.subTest(starting_state=starting_state):
                bridge = FakeBridge()
                bridge.puzzle["state"] = starting_state
                original_call = bridge.call
                def advancing_game(method, *args):
                    if method == "next":
                        bridge.advanced.append(args[0])
                        bridge.puzzle.update(level_id=str(int(args[0]) + 1), state="Playing", retry_count=0)
                        return {"requested": True}
                    return original_call(method, *args)
                bridge.call = advancing_game
                runner = badpun.AutoRunner(bridge, self.config())
                with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(runner.solver, "solve", return_value={"answer": "纽约"}):
                    runner._run("ten")
                self.assertEqual(len(bridge.submissions), 10)
                self.assertEqual(len(bridge.advanced), 10 if starting_state == "Win" else 9)
                self.assertEqual(bridge.puzzle["state"], "Win")
                self.assertEqual(runner.snapshot()["completed"], 10)
                self.assertEqual(runner.snapshot()["phase"], "completed")

    def test_failed_level_restarts_and_keeps_old_feedback(self):
        bridge = FakeBridge()
        bridge.puzzle.update(state="Failed", retry_count=8, feedback_history=[{"guess": "纽扣", "letters": []}])
        runner = badpun.AutoRunner(bridge, self.config())
        history = []
        def solve(puzzle, previous):
            history.extend(row["answer"] for row in previous)
            return {"answer": "纽约"}
        with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(runner.solver, "solve", solve):
            runner._run("current")
        self.assertIn("纽扣", history)
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertEqual(bridge.puzzle["state"], "Win")

    def test_continuous_advances_and_can_be_stopped_in_next_level(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        calls = []
        def solve(puzzle, previous):
            calls.append(puzzle["level_id"])
            if len(calls) == 2:
                runner.stop()
            return {"answer": "纽约"}
        with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(runner.solver, "solve", solve):
            runner._run("continuous")
        self.assertEqual(bridge.advanced, ["118"])
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertEqual(calls, ["118", "119"])
        self.assertEqual(runner.snapshot()["phase"], "stopped")

    def test_same_phonetic_hint_moves_without_reusing_the_gray_character(self):
        puzzle = {"expected_length": 3, "feedback_history": [{"guess": "蛋送花", "letters": [
            {"position": 1, "character": "蛋", "pinyin": "dan4", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"},
            {"position": 2, "character": "送", "pinyin": "song4", "character_feedback": "absent", "pinyin_feedback": "present_elsewhere"},
            {"position": 3, "character": "花", "pinyin": "hua1", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"}]}]}
        badpun.check_prediction("松花蛋", puzzle, [], [["song1"], ["hua1"], ["dan4"]])
        with self.assertRaises(badpun.PredictionError):
            badpun.check_prediction("花松蛋", puzzle, [], [["hua1"], ["song1"], ["dan4"]])


class NetworkTests(unittest.TestCase):
    def call_slow_model(self, timeout, stop):
        async def slow(request):
            await asyncio.sleep(20)
            return httpx.Response(200, json={"choices": []})
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(slow), **kwargs)
        with patch.object(httpx, "AsyncClient", client):
            return badpun.request_model(Request("https://example.com/chat/completions", data=b"{}"), timeout, stop)

    def test_request_has_a_total_deadline(self):
        with self.assertRaises(badpun.ModelTemporaryError):
            self.call_slow_model(0.05, threading.Event())

    def test_stop_cancels_a_request_before_its_timeout(self):
        stopped = threading.Event()
        timer = threading.Timer(0.1, stopped.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                self.call_slow_model(10, stopped)
        finally:
            timer.cancel()


if __name__ == "__main__":
    unittest.main()
