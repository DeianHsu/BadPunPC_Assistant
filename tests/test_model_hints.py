"""Check that model requests contain deductions rather than raw feedback or rejected words."""
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
        return {**badpun.DEFAULTS, "api_key": "test-key", "thinking_enabled": False, "between_levels_seconds": 0,
                "transition_timeout_seconds": 1}

    def test_processed_constraints_and_images_are_in_the_same_request(self):
        puzzle = badpun.prepare_puzzle(puzzle_payload())
        puzzle.update(expected_length=4, category="成语", reference_word="参考词", puzzle_text="不发送的谜面文字",
                      last_rejection={"answer": "不发送的错误答案", "error": "不发送的错误原因"})
        previous = [{"answer": "无拘无束", "letters": [
            {"position": 1, "character": "无", "pinyin": "wu2", "character_feedback": "correct_position", "pinyin_feedback": "correct_position"},
            {"position": 2, "character": "拘", "pinyin": "ju1", "character_feedback": "absent", "pinyin_feedback": "absent"},
            {"position": 3, "character": "无", "pinyin": "wu2", "character_feedback": "absent", "pinyin_feedback": "absent"},
            {"position": 4, "character": "束", "pinyin": "shu4", "character_feedback": "absent", "pinyin_feedback": "absent"}]}]
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": '{"answer":"无所适从"}'}}]}
        with patch.object(badpun, "request_model", response):
            badpun.VisionSolver(self.config()).solve(puzzle, previous)
        blocks = captured[0]["messages"][1]["content"]
        self.assertEqual([message["role"] for message in captured[0]["messages"]], ["system", "user"])
        self.assertEqual([block["type"] for block in blocks], ["text", "image_url", "image_url"])
        text = blocks[0]["text"]
        self.assertIn("第一张图的参考词是“参考词”。", text)
        self.assertIn("问题分类是“成语”。", text)
        self.assertIn("答案要求字数：4个汉字。", text)
        self.assertIn("第1个字必须是“无”。", text)
        self.assertIn("答案不能包含汉字“拘”、“束”。", text)
        self.assertIn("第1个字的拼音必须是“wu”。", text)
        self.assertIn("答案中各字的拼音都不能是“ju”、“shu”。", text)
        for prompt in (captured[0]["messages"][0]["content"], text):
            self.assertNotIn("{", prompt)
            self.assertNotIn("}", prompt)
            self.assertNotIn("[", prompt)
        self.assertNotIn("至少", text)
        for omitted in ("无拘无束", "不发送", "character_feedback", "pinyin_feedback", "tone_feedback", "rejected_answers"):
            self.assertNotIn(omitted, text)

    def test_local_rejection_retries_without_sending_the_error_or_word(self):
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
        self.assertNotIn("纽约市", blocks[0]["text"])
        self.assertNotIn("本题需要 2 字", blocks[0]["text"])
        self.assertEqual([item["role"] for item in captured[1]["messages"]], ["system", "user"])
        self.assertGreater(captured[1]["temperature"], captured[0]["temperature"])
        self.assertNotIn("reason", runner.snapshot())

    def test_duplicate_does_not_replace_the_specific_pinyin_error(self):
        original = {"answer": "和题共济", "error": "绿色拼音要求第 3 位读“ti”", "kind": "feedback"}
        duplicate = {"answer": "和题共济", "error": "不能重复猜测", "kind": "duplicate"}
        self.assertEqual(badpun.merge_history([original], [duplicate]), [original])

    def test_repeated_output_keeps_deductions_and_sampling_without_corrective_dialogue(self):
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
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        text = body["messages"][1]["content"][0]["text"]
        self.assertNotIn("和题共济", text)
        self.assertIn("第3个字的拼音必须是“ti”。", text)
        self.assertIn("答案不能包含汉字“提”。", text)

    def test_orange_positions_are_described_in_plain_language(self):
        puzzle = badpun.prepare_puzzle(puzzle_payload())
        puzzle.update(expected_length=3, category="典故", reference_word="参考词")
        previous = [{"answer": "马拉犁", "letters": [
            {"position": 1, "character": "马", "pinyin": "ma3", "character_feedback": "present_elsewhere", "pinyin_feedback": "present_elsewhere"},
            {"position": 2, "character": "拉", "pinyin": "la1", "character_feedback": "absent", "pinyin_feedback": "absent"},
            {"position": 3, "character": "犁", "pinyin": "li2", "character_feedback": "absent", "pinyin_feedback": "present_elsewhere"}]}]
        captured = []
        def response(request, *args):
            captured.append(json.loads(request.data))
            return {"choices": [{"message": {"content": '{"answer":"李马戏"}'}}]}
        with patch.object(badpun, "request_model", response):
            badpun.VisionSolver(self.config()).solve(puzzle, previous)
        text = captured[0]["messages"][1]["content"][0]["text"]
        self.assertIn("答案要求字数：3个汉字。", text)
        self.assertIn("答案必须包含汉字“马”。", text)
        self.assertIn("汉字“马”不能在第1位。", text)
        self.assertIn("答案不能包含汉字“拉”、“犁”。", text)
        self.assertIn("答案中至少有1个字的拼音是“ma”。", text)
        self.assertIn("答案中至少有1个字的拼音是“li”。", text)
        self.assertIn("第1位的拼音不能是“ma”。", text)
        self.assertIn("第3位的拼音不能是“li”。", text)
        self.assertNotIn("马拉犁", text)

    def test_old_duplicate_record_is_used_only_for_local_rejection(self):
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
        self.assertEqual([message["role"] for message in captured[1]["messages"]], ["system", "user"])
        text = captured[1]["messages"][1]["content"][0]["text"]
        self.assertNotIn("纽扣", text)
        self.assertNotIn("不能重复猜测", text)


if __name__ == "__main__":
    unittest.main()
