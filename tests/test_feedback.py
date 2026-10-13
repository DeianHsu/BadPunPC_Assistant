"""Replay the real repeated-answer / gray-character failure without touching the game."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
from test_flow import FakeBridge


def gray_row(guess):
    return {"guess": guess, "letters": [{"position": i + 1, "character": char,
            "character_feedback": "absent"} for i, char in enumerate(guess)]}


def real_history():
    last = gray_row("恭喜发财")
    last["letters"][0].update(character_feedback="present_elsewhere", pinyin="gong1",
                              pinyin_feedback="present_elsewhere", tone_feedback="present_elsewhere")
    return [gray_row("财源广进"), gray_row("财源滚滚"), last]


def puzzle():
    return {"expected_length": 4, "feedback_history": real_history()}


class FeedbackTests(unittest.TestCase):
    def test_mixed_gray_and_green_ignores_gray_and_keeps_green_position(self):
        case = {"expected_length": 4, "feedback_history": [{"guess": "无拘无束", "letters": [
            {"position": 1, "character": "无", "pinyin": "wu2", "character_feedback": "correct_position", "pinyin_feedback": "correct_position"},
            {"position": 2, "character": "拘", "pinyin": "ju1", "character_feedback": "absent", "pinyin_feedback": "absent"},
            {"position": 3, "character": "无", "pinyin": "wu2", "character_feedback": "absent", "pinyin_feedback": "absent"},
            {"position": 4, "character": "束", "pinyin": "shu4", "character_feedback": "absent", "pinyin_feedback": "absent"}]}]}
        rules = badpun.feedback_constraints(case, [])
        self.assertNotIn("无", rules["excluded_characters"])
        self.assertNotIn("wu", rules["excluded_pinyins"])
        self.assertNotIn("无", rules["max_character_counts"])
        self.assertNotIn("wu", rules["max_pinyin_counts"])
        self.assertNotIn("无", rules["forbidden_positions"])
        self.assertNotIn("wu", rules["forbidden_pinyin_positions"])
        badpun.check_prediction("无所适从", case, [], [["wu2"], ["suo3"], ["shi4"], ["cong2"]])
        badpun.check_prediction("无甲无乙", case, [], [["wu2"], ["jia3"], ["wu2"], ["yi3"]])
        with self.assertRaisesRegex(badpun.PredictionError, "第 1 字"):
            badpun.check_prediction("甲无乙丙", case, [])
        badpun.check_prediction("无武适从", case, [], [["wu2"], ["wu3"], ["shi4"], ["cong2"]])
        info = badpun.model_constraints(rules)
        self.assertEqual(info["汉字"]["固定位置"], {"1": "无"})
        self.assertEqual(info["拼音"]["固定位置"], {"1": "wu"})
        self.assertNotIn("数量", info["汉字"])
        self.assertNotIn("数量", info["拼音"])

    def test_mixed_gray_and_orange_across_history_preserves_only_orange_constraints(self):
        orange = {"answer": "马甲乙", "letters": [{"position": 1, "character": "马", "pinyin": "ma3",
                  "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"}]}
        gray = {"answer": "甲马马", "letters": [{"position": position, "character": "马", "pinyin": "ma3",
                "character_feedback": "absent", "pinyin_feedback": "absent"} for position in (2, 3)]}
        for live, saved in ((orange, gray), (gray, orange)):
            with self.subTest(live=live["answer"]):
                case = {"expected_length": 3, "feedback_history": [live]}
                rules = badpun.feedback_constraints(case, [saved])
                self.assertNotIn("马", rules["excluded_characters"])
                self.assertNotIn("ma", rules["excluded_pinyins"])
                self.assertNotIn("马", rules["max_character_counts"])
                self.assertNotIn("ma", rules["max_pinyin_counts"])
                self.assertEqual(rules["forbidden_positions"], {"马": [1]})
                self.assertEqual(rules["forbidden_pinyin_positions"], {"ma": [1]})
                info = badpun.model_constraints(rules)
                self.assertEqual(info["汉字"]["数量"], {"马": {"至少": 1}})
                self.assertEqual(info["拼音"]["数量"], {"ma": {"至少": 1}})
                badpun.check_prediction("河马马", case, [saved], [["he2"], ["ma3"], ["ma3"]])
                with self.assertRaisesRegex(badpun.PredictionError, "第 1 位"):
                    badpun.check_prediction("马河马", case, [saved])
                with self.assertRaisesRegex(badpun.PredictionError, "第 1 位不能读"):
                    badpun.check_prediction("麻河马", case, [saved], [["ma2"], ["he2"], ["ma3"]])

    def test_known_failed_word_is_reported_as_duplicate(self):
        with self.assertRaises(badpun.PredictionError) as caught:
            badpun.check_prediction("财源广进", puzzle(), [])
        self.assertEqual(caught.exception.kind, "duplicate")
        self.assertIn("重复", str(caught.exception))

    def test_different_word_containing_gray_character_is_rejected(self):
        with self.assertRaises(badpun.PredictionError) as caught:
            badpun.check_prediction("财源四起", puzzle(), [])
        self.assertEqual(caught.exception.kind, "feedback")
        self.assertIn("灰字", str(caught.exception))
        self.assertIn("财", str(caught.exception))

    def test_orange_character_must_be_kept_and_moved(self):
        for answer in ("恭敬有礼", "安居乐业"):
            with self.subTest(answer=answer), self.assertRaises(badpun.PredictionError):
                badpun.check_prediction(answer, puzzle(), [])
        # This is an example that satisfies the visible constraints, not a claim about the solution.
        badpun.check_prediction("谦恭有礼", puzzle(), [])

    def test_gray_character_does_not_exclude_other_homophones(self):
        case = {"expected_length": 2, "feedback_history": [gray_row("和平")]}
        case["feedback_history"][0]["letters"][0].update(pinyin="he2", pinyin_feedback="correct_position")
        rules = badpun.feedback_constraints(case, [])
        self.assertIn("和", rules["excluded_characters"])
        self.assertIn("he", rules["required_pinyins"])
        badpun.check_prediction("何处", case, [])

    def test_wrong_length_has_an_exact_explanation(self):
        with self.assertRaises(badpun.PredictionError) as caught:
            badpun.check_prediction("纽约市", puzzle(), [])
        self.assertEqual(caught.exception.kind, "length")
        self.assertIn("3 字", str(caught.exception))
        self.assertIn("4 字", str(caught.exception))

    def test_restart_blocks_old_and_gray_answers_before_any_game_submission(self):
        bridge = FakeBridge()
        bridge.puzzle.update(level_id="3480", display_index=123, expected_length=4,
                             retry_count=5, feedback_history=real_history())
        original_call = bridge.call
        def game_call(method, *args):
            if method == "submit":
                bridge.submissions.append(args[1])
                bridge.puzzle["state"] = "Win"
                return original_call("status")
            return original_call(method, *args)
        bridge.call = game_call
        config = {**badpun.DEFAULTS, "api_key": "test-key", "between_levels_seconds": 0,
                  "max_attempts_per_level": 3, "transition_timeout_seconds": 1}
        runner = badpun.AutoRunner(bridge, config)
        seen = []
        guesses = iter(["财源广进", "财源四起", "谦恭有礼"])
        def predict(current_puzzle, previous):
            seen.append([row["answer"] for row in previous])
            return {"answer": next(guesses), "reason": "测试候选", "usage": {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(badpun, "ROOT", Path(folder)), patch.object(runner.solver, "solve", predict):
            with self.assertLogs("badpun", level="WARNING") as logging:
                runner._run("current")
        self.assertEqual(bridge.submissions, ["谦恭有礼"])
        self.assertEqual(set(seen[0]), {"财源广进", "财源滚滚", "恭喜发财"})
        self.assertTrue(all("未提交游戏" in line for line in logging.output))
        self.assertEqual(runner.snapshot()["phase"], "completed")


if __name__ == "__main__":
    unittest.main()
