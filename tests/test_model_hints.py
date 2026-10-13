"""Check the actual multimodal request, including game clues and local rejection reasons."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import badpun
from test_flow import FakeBridge, puzzle_payload


class ModelHintTests(unittest.TestCase):
    def config(self):
        return {**badpun.DEFAULTS, "api_key": "test-key", "between_levels_seconds": 0,
                "transition_timeout_seconds": 1}

    def test_colors_positions_and_images_are_in_the_same_request(self):
        puzzle = badpun.prepare_puzzle(puzzle_payload())
        puzzle["expected_length"] = 3
        previous = [{"answer": "蛋送花", "letters": [
            {"position": 1, "character": "蛋", "pinyin": "dan4", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere", "tone_feedback": "present_elsewhere"},
            {"position": 2, "character": "送", "pinyin": "song4", "character_feedback": "absent", "pinyin_feedback": "correct_position"},
            {"position": 3, "character": "花", "pinyin": "hua1", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"}]}]
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": '{"answer":"花松蛋"}'}}]}
        with patch.object(badpun, "request_model", response):
            badpun.VisionSolver(self.config()).solve(puzzle, previous)
        blocks = captured[0]["messages"][1]["content"]
        self.assertEqual([block["type"] for block in blocks], ["text", "image_url", "image_url"])
        text = blocks[0]["text"]
        self.assertIn("字「送」：灰色，答案中没有这个字", text)
        self.assertIn("第1位，字「蛋」：橙色", text)
        self.assertIn("拼音「song」：绿色，此位置的拼音正确", text)
        self.assertIn('"fixed_pinyins": {"2": "song"}', text)
        self.assertIn('"forbidden_pinyin_positions": {"dan": [1], "hua": [3]}', text)
        self.assertIn("声调「4」：橙色", text)

    def test_local_rejection_is_sent_back_with_images_on_the_next_request(self):
        bridge = FakeBridge()
        runner = badpun.AutoRunner(bridge, self.config())
        guesses = iter(["纽约市", "纽约"])
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": json.dumps({"answer": next(guesses)}, ensure_ascii=False)}}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(badpun, "request_model", response):
            runner._run("current")
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertEqual(len(captured), 2)
        blocks = captured[1]["messages"][1]["content"]
        self.assertEqual(sum(block["type"] == "image_url" for block in blocks), 2)
        self.assertIn("本地拦截候选「纽约市」", blocks[0]["text"])
        self.assertIn("本题需要 2 字", blocks[0]["text"])
        self.assertEqual([item["role"] for item in captured[1]["messages"]], ["system", "user", "assistant", "user"])
        self.assertIn("本题需要 2 字", captured[1]["messages"][-1]["content"])
        self.assertGreater(captured[1]["temperature"], captured[0]["temperature"])
        self.assertNotIn("reason", runner.snapshot())

    def test_duplicate_does_not_replace_the_specific_pinyin_error(self):
        original = {"answer": "和题共济", "error": "绿色拼音要求第 3 位读“ti”", "kind": "feedback"}
        duplicate = {"answer": "和题共济", "error": "不能重复猜测", "kind": "duplicate"}
        self.assertEqual(badpun.merge_history([original], [duplicate]), [original])

    def test_repeated_output_uses_corrective_dialogue_and_more_varied_sampling(self):
        puzzle = badpun.prepare_puzzle(puzzle_payload())
        puzzle.update(expected_length=4, retry_streak=7,
                      last_rejection={"answer": "和题共济", "error": "绿色拼音要求第 3 位读“ti”"})
        previous = [{"answer": "盒子提水", "letters": [
            {"position": 3, "character": "提", "pinyin": "ti2", "character_feedback": "absent", "pinyin_feedback": "correct_position"}]}]
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": '{"answer":"身轻体健"}'}}]}
        with patch.object(badpun, "request_model", response):
            badpun.VisionSolver(self.config()).solve(puzzle, previous)
        body = captured[0]
        self.assertEqual(body["temperature"], 1.2)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(json.loads(body["messages"][-2]["content"]), {"answer": "和题共济"})
        self.assertIn("第3字必须读「ti」", body["messages"][-1]["content"])
        self.assertIn("必须排除的灰字：提", body["messages"][-1]["content"])

    def test_old_generic_duplicate_record_recovers_its_concrete_error(self):
        bridge = FakeBridge()
        bridge.puzzle["feedback_history"] = [{"guess": "约纽", "letters": [
            {"position": 1, "character": "约", "pinyin": "yue1", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"},
            {"position": 2, "character": "纽", "pinyin": "niu3", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"}]}]
        runner = badpun.AutoRunner(bridge, self.config())
        runner.histories["118"] = [{"answer": "纽扣", "error": "不能重复猜测", "kind": "duplicate"}]
        guesses = iter(["纽扣", "纽约"])
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": json.dumps({"answer": next(guesses)}, ensure_ascii=False)}}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(badpun, "ROOT", Path(directory)), patch.object(badpun, "request_model", response):
            runner._run("current")
        self.assertEqual(bridge.submissions, ["纽约"])
        self.assertIn("答案必须含橙／绿色提示中的", captured[1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
