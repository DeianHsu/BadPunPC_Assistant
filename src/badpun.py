"""BadPunPC_Assistant：《这是谐音梗》本地 API 与视觉模型自动解题助手。"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
import io
import json
import logging
import os
import ssl
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

SOURCE = Path(__file__).resolve().parent
RESOURCES = Path(getattr(sys, "_MEIPASS", SOURCE.parent))
AGENT_PATH = Path(getattr(sys, "_MEIPASS", SOURCE)) / "agent.js"
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else RESOURCES
if (ROOT / ".runtime").is_dir():
    sys.path.insert(0, str(ROOT / ".runtime"))

DEFAULTS = json.loads((RESOURCES / "config.example.json").read_text(encoding="utf-8"))
LOG = logging.getLogger("badpun")
RUN_MODES = {"current": 1, "ten": 10, "continuous": None}


def provider_for_url(address: str) -> str:
    return {"api.deepseek.com": "deepseek", "api.xiaomimimo.com": "mimo"}.get(urlsplit(address.strip()).hostname, "compatible")


def load_config(path: Path) -> dict:
    config = DEFAULTS.copy()
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    if config["host"] not in ("127.0.0.1", "localhost"):
        raise ValueError("本地接口只允许绑定 127.0.0.1")
    return config


class GameBridge:
    def __init__(self, process: str, pid: int | None = None):
        import frida
        self.lock = threading.RLock()
        self.session = None
        self.script = None
        self.detached = False
        device = frida.get_local_device()
        if pid is None:
            matches = [p for p in device.enumerate_processes() if p.name.lower() == process.lower()]
            if len(matches) != 1:
                raise RuntimeError(f"找到 {len(matches)} 个 {process}；请启动游戏，进入单人题目")
            pid = matches[0].pid
        library = RESOURCES / "node_modules/frida-il2cpp-bridge/dist/index.js"
        if not library.exists():
            raise RuntimeError("缺少桥接依赖；请在项目根目录运行 npm ci --ignore-scripts")
        self.session = device.attach(pid)
        self.session.on("detached", self._on_detached)
        try:
            source = library.read_text(encoding="utf-8") + "\n" + AGENT_PATH.read_text(encoding="utf-8")
            self.script = self.session.create_script(source)
            self.script.on("message", self._on_message)
            self.script.load()
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                health = self.script.exports_sync.health()
                if health.get("error"):
                    raise RuntimeError(health["error"])
                if health["ready"]:
                    LOG.info("已连接游戏进程 %s", pid)
                    return
                time.sleep(0.2)
            raise RuntimeError("Unity 接口初始化超时，请确认游戏已经打开")
        except Exception:
            self.close()
            raise

    def _on_detached(self, reason, crash=None):
        self.detached = True
        LOG.info("游戏连接已断开：%s", reason)

    @staticmethod
    def _on_message(message, data):
        if message["type"] == "error":
            LOG.error("游戏接口错误：%s", message.get("stack", message.get("description")))
        elif message["type"] == "send":
            LOG.info("游戏接口：%s", message["payload"])

    def call(self, name: str, *args):
        with self.lock:
            if self.detached or self.script is None:
                raise RuntimeError("游戏已退出，请重启游戏和本程序")
            return getattr(self.script.exports_sync, name)(*args)

    def close(self):
        if self.script is not None and not self.detached:
            try:
                self.call("cleanup")
            except Exception as error:
                LOG.warning("未恢复后台运行标志；退出游戏后恢复默认设置：%s", error)
        if self.session is not None and not self.detached:
            try:
                self.session.detach()
            except Exception:
                pass


def prepare_puzzle(puzzle: dict) -> dict:
    """Crop individual sprites out of their texture atlas, without desktop capture."""
    from PIL import Image
    result = {key: value for key, value in puzzle.items() if key != "images"}
    result["image_data_urls"] = []
    for item in puzzle["images"]:
        with Image.open(io.BytesIO(base64.b64decode(item["png_base64"]))) as image:
            x, y, width, height = [round(n) for n in item["crop"]]
            # Unity texture coordinates start at the lower left; PNG starts at the upper left.
            cropped = image.crop((x, image.height - y - height, x + width, image.height - y)).convert("RGB")
            cropped.thumbnail((1400, 1400))
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            result["image_data_urls"].append("data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"))
    return result


SYSTEM_PROMPT = (
    '这是一个谐音梗题目。答案必须符合分类、答案要求字数及全部已知条件。\n'
    '拼音不含声调。只返回一个 JSON 对象，仅含 answer 字段，其值为答案，不附解释。'
)


class PredictionError(RuntimeError):
    def __init__(self, message: str, answer: str = "", kind: str = "response", rejections=None):
        super().__init__(message)
        self.answer = answer
        self.kind = kind
        self.rejections = rejections or [{"answer": answer, "error": message, "kind": kind}]


class ModelTemporaryError(RuntimeError):
    pass


def request_model(request: Request, timeout: float, stop_event=None) -> dict:
    """A bounded, cancellable request, including time waiting for response headers."""
    import httpx
    async def perform():
        async with httpx.AsyncClient(verify=ssl.create_default_context(), timeout=httpx.Timeout(timeout, connect=min(10, timeout))) as client:
            pending = asyncio.create_task(client.post(request.full_url, headers=dict(request.header_items()), content=request.data))
            started = time.monotonic()
            try:
                while not pending.done():
                    if stop_event is not None and stop_event.is_set():
                        raise InterruptedError("用户已停止")
                    if time.monotonic() - started >= timeout:
                        raise ModelTemporaryError(f"模型请求等待超过 {timeout:g} 秒，将自动重试")
                    await asyncio.wait({pending}, timeout=0.15)
                response = pending.result()
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            if response.status_code >= 400:
                key = request.get_header("Authorization", "").removeprefix("Bearer ")
                detail = response.text[:600].replace(key, "[密钥已隐藏]") if key else response.text[:600]
                if response.status_code in (408, 429) or response.status_code >= 500:
                    raise ModelTemporaryError(f"模型接口暂时不可用（HTTP {response.status_code}），将自动重试")
                raise RuntimeError(f"模型接口返回 HTTP {response.status_code}：{detail}")
            try:
                return response.json()
            except ValueError:
                raise ModelTemporaryError("模型接口返回内容暂不可读取，将自动重试") from None
    try:
        return asyncio.run(perform())
    except (httpx.TimeoutException, httpx.TransportError) as error:
        raise ModelTemporaryError(f"模型网络请求失败（{type(error).__name__}），将自动重试") from None


def merge_history(existing: list[dict], additions: list[dict]) -> list[dict]:
    result = {row.get("answer", row.get("guess", "")): dict(row) for row in existing}
    for item in additions:
        row = dict(item)
        answer = row.pop("guess", row.get("answer", ""))
        if not answer:
            continue
        row["answer"] = answer
        if answer in result and result[answer].get("letters") and not row.get("letters"):
            continue
        if row.get("kind") == "duplicate" and answer in result and result[answer].get("kind") != "duplicate":
            # Repeated output must not erase the first concrete reason it was rejected.
            continue
        result[answer] = row
    return list(result.values())


def feedback_constraints(puzzle: dict, previous: list[dict]) -> dict:
    required, fixed, forbidden = set(), {}, {}
    required_pinyin = set()
    fixed_pinyin, forbidden_pinyin = {}, {}
    min_chars, min_pinyins = {}, {}
    gray_chars, gray_pinyins = set(), set()
    rows = list(puzzle.get("feedback_history", [])) + list(previous)
    rejected = {row.get("guess", row.get("answer", "")) for row in rows}
    for row in rows:
        char_hits, pinyin_hits = Counter(), Counter()
        for letter in row.get("letters", []):
            char = letter.get("character", "")
            position = letter.get("position")
            state = letter.get("character_feedback")
            if char and state == "absent":
                gray_chars.add(char)
            elif char and state == "present_elsewhere":
                char_hits[char] += 1
                required.add(char)
                forbidden.setdefault(char, set()).add(position)
            elif char and state == "correct_position":
                char_hits[char] += 1
                required.add(char)
                fixed[position] = char
            pinyin = letter.get("pinyin", "").rstrip("12345")
            if pinyin and letter.get("pinyin_feedback") == "absent":
                gray_pinyins.add(pinyin)
            elif pinyin and letter.get("pinyin_feedback") in ("correct_position", "present_elsewhere"):
                pinyin_hits[pinyin] += 1
                required_pinyin.add(pinyin)
                if letter["pinyin_feedback"] == "correct_position":
                    fixed_pinyin[position] = pinyin
                else:
                    forbidden_pinyin.setdefault(pinyin, set()).add(position)
        for hits, lower in ((char_hits, min_chars), (pinyin_hits, min_pinyins)):
            for token, count in hits.items():
                lower[token] = max(lower.get(token, 0), count)
    # Orange/green evidence takes precedence over all gray occurrences in this level.
    excluded = gray_chars - required
    excluded_pinyin = gray_pinyins - required_pinyin
    max_chars = {char: 0 for char in excluded}
    max_pinyins = {pinyin: 0 for pinyin in excluded_pinyin}
    return {"excluded_characters": sorted(excluded), "required_characters": sorted(required),
            "fixed_characters": {str(position): char for position, char in sorted(fixed.items())},
            "forbidden_positions": {char: sorted(positions) for char, positions in sorted(forbidden.items())},
            "excluded_pinyins": sorted(excluded_pinyin), "required_pinyins": sorted(required_pinyin),
            "fixed_pinyins": {str(position): pinyin for position, pinyin in sorted(fixed_pinyin.items())},
            "forbidden_pinyin_positions": {pinyin: sorted(positions) for pinyin, positions in sorted(forbidden_pinyin.items())},
            "min_character_counts": min_chars, "max_character_counts": max_chars,
            "min_pinyin_counts": min_pinyins, "max_pinyin_counts": max_pinyins,
            "rejected_answers": sorted(answer for answer in rejected if answer)}


def model_constraints(rules: dict) -> dict:
    """Project local deductions into model information, without guesses or raw feedback."""
    result = {}
    for label, prefix, plural in (("汉字", "character", "characters"), ("拼音", "pinyin", "pinyins")):
        excluded = rules[f"excluded_{plural}"]
        fixed = rules[f"fixed_{plural}"]
        lower = rules[f"min_{prefix}_counts"]
        fixed_counts = Counter(fixed.values())
        forbidden = rules["forbidden_positions" if prefix == "character" else "forbidden_pinyin_positions"]
        info = {}
        if excluded:
            info["排除"] = excluded
        if fixed:
            info["固定位置"] = fixed
        positions = {token: places for token, places in forbidden.items() if token not in excluded}
        if positions:
            info["禁用位置"] = positions
        counts = {token: {"至少": minimum} for token, minimum in sorted(lower.items())
                  if minimum > fixed_counts[token]}
        if counts:
            info["数量"] = counts
        if info:
            result[label] = info
    return result


def model_prompt(puzzle: dict, rules: dict) -> str:
    """Describe puzzle metadata and deduced conditions in natural language."""
    lines = []
    if puzzle.get("reference_word"):
        lines.append(f"第一张图的参考词是“{puzzle['reference_word']}”。")
    if puzzle.get("category"):
        lines.append(f"问题分类是“{puzzle['category']}”。")
    lines.append(f"答案要求字数：{puzzle['expected_length']}个汉字。")
    for kind, info in model_constraints(rules).items():
        if info.get("排除"):
            excluded = "、".join(f"“{token}”" for token in info["排除"])
            if kind == "汉字":
                lines.append(f"答案不能包含汉字{excluded}。")
            else:
                lines.append(f"答案中各字的拼音都不能是{excluded}。")
        for position, token in info.get("固定位置", {}).items():
            if kind == "汉字":
                lines.append(f"第{position}个字必须是“{token}”。")
            else:
                lines.append(f"第{position}个字的拼音必须是“{token}”。")
        for token, count in info.get("数量", {}).items():
            minimum = count["至少"]
            if kind == "汉字":
                lines.append(f"答案必须包含汉字“{token}”。" if minimum == 1
                             else f"汉字“{token}”在答案中至少出现{minimum}次。")
            else:
                lines.append(f"答案中至少有{minimum}个字的拼音是“{token}”。")
        for token, positions in info.get("禁用位置", {}).items():
            places = "、".join(f"第{position}位" for position in positions)
            if kind == "汉字":
                lines.append(f"汉字“{token}”不能在{places}。")
            else:
                lines.append(f"{places}的拼音不能是“{token}”。")
    return "\n".join(lines)


def check_prediction(answer, puzzle: dict, previous: list[dict], pronunciations=None, *, check_duplicate=True):
    if not isinstance(answer, str):
        raise PredictionError("模型返回的答案不是文字", kind="format")
    expected = puzzle["expected_length"]
    if len(answer) != expected:
        raise PredictionError(f"“{answer}”有 {len(answer)} 字，本题需要 {expected} 字", answer, "length")
    if any(c.isspace() for c in answer):
        raise PredictionError(f"“{answer}”含空白字符，答案中不能有空格或换行", answer, "format")
    rules = feedback_constraints(puzzle, previous)
    if check_duplicate and answer in rules["rejected_answers"]:
        raise PredictionError(f"“{answer}”已经失败或被拦截，不能重复猜测", answer, "duplicate")
    banned = sorted(set(answer) & set(rules["excluded_characters"]))
    if banned:
        raise PredictionError(f"“{answer}”包含已排除的灰字：{'、'.join(banned)}", answer, "feedback")
    for position, char in rules["fixed_characters"].items():
        if answer[int(position) - 1] != char:
            raise PredictionError(f"绿色提示要求第 {position} 字为“{char}”", answer, "feedback")
    for char in rules["required_characters"]:
        if char not in answer:
            raise PredictionError(f"答案必须含橙／绿色提示中的“{char}”", answer, "feedback")
    for char in set(rules["min_character_counts"]) | set(rules["max_character_counts"]):
        count = answer.count(char)
        minimum = rules["min_character_counts"].get(char, 0)
        maximum = rules["max_character_counts"].get(char, expected)
        if count < minimum:
            raise PredictionError(f"“{char}”至少需要出现{minimum}次", answer, "feedback")
        if count > maximum:
            raise PredictionError(f"“{char}”最多只能出现{maximum}次", answer, "feedback")
    for char, positions in rules["forbidden_positions"].items():
        for position in positions:
            if answer[position - 1] == char:
                raise PredictionError(f"反馈要求第 {position} 位不能是“{char}”", answer, "feedback")
    if pronunciations is not None:
        bases = [{reading.rstrip("12345") for reading in readings} for readings in pronunciations]
        for index, possible in enumerate(bases):
            if possible and possible <= set(rules["excluded_pinyins"]):
                raise PredictionError(f"第 {index + 1} 字只有已排除的灰色拼音", answer, "feedback")
            possible.difference_update(rules["excluded_pinyins"])
        for position, pinyin in rules["fixed_pinyins"].items():
            index = int(position) - 1
            if pinyin not in bases[index]:
                raise PredictionError(f"绿色拼音要求第 {position} 位读“{pinyin}”", answer, "feedback")
            bases[index] = {pinyin}
        for pinyin, positions in rules["forbidden_pinyin_positions"].items():
            for position in positions:
                possible = bases[position - 1]
                if pinyin in possible:
                    possible.discard(pinyin)
                    if not possible:
                        raise PredictionError(f"反馈要求第 {position} 位不能读“{pinyin}”", answer, "feedback")
        for pinyin in rules["required_pinyins"]:
            if not any(pinyin in possible for possible in bases):
                raise PredictionError(f"答案必须包含提示中的拼音“{pinyin}”", answer, "feedback")
        for pinyin in set(rules["min_pinyin_counts"]) | set(rules["max_pinyin_counts"]):
            minimum = rules["min_pinyin_counts"].get(pinyin, 0)
            maximum = rules["max_pinyin_counts"].get(pinyin, expected)
            if sum(pinyin in possible for possible in bases) < minimum:
                raise PredictionError(f"拼音“{pinyin}”至少需要出现{minimum}次", answer, "feedback")
            if sum(possible == {pinyin} for possible in bases) > maximum:
                raise PredictionError(f"拼音“{pinyin}”最多只能出现{maximum}次", answer, "feedback")


class VisionSolver:
    def __init__(self, config: dict):
        self.config = config

    def key(self) -> str:
        key_env = "MIMO_API_KEY" if provider_for_url(self.config["model_base_url"]) == "mimo" else self.config["api_key_env"]
        key = self.config.get("api_key", "") or os.environ.get(key_env, "")
        if not key.strip():
            raise RuntimeError(f"尚未配置模型接口密钥：设置 {key_env} 或填写 config.local.json 的 api_key")
        return key.strip()

    def solve(self, puzzle: dict, previous: list[dict]) -> dict:
        rules = feedback_constraints(puzzle, previous)
        text = model_prompt(puzzle, rules)
        content = [{"type": "text", "text": text}]
        for url in puzzle["image_data_urls"]:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]
        retry_streak = puzzle.get("retry_streak", 0)
        temperature = self.config.get("temperature", 0.2)
        if retry_streak:
            temperature = max(temperature, 1.2 if retry_streak >= 6 else 1.0 if retry_streak >= 3 else 0.7)
        body = {
            "model": self.config["model"],
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": self.config["max_output_tokens"],
            "temperature": temperature,
            "stream": False
        }
        provider = provider_for_url(self.config["model_base_url"])
        thinking = self.config["thinking_enabled"] and provider in ("deepseek", "mimo")
        if provider in ("deepseek", "mimo"):
            body["thinking"] = {"type": "enabled" if self.config["thinking_enabled"] else "disabled"}
            if provider == "deepseek" and self.config["thinking_enabled"]:
                body["reasoning_effort"] = self.config["reasoning_effort"]
        if thinking:
            # The old 256-token cap includes reasoning and would truncate the final answer.
            # Use the service's documented reasoning budget; keep the short cap for non-thinking requests.
            body.pop("max_tokens")
            body.pop("temperature")
        if provider == "mimo":
            if "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
            if "temperature" in body:
                body["temperature"] = min(body["temperature"], 1.5)
        request = Request(self.config["model_base_url"].rstrip("/") + "/chat/completions",
                          data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self.key(), "Content-Type": "application/json"})
        timeout = max(self.config["model_timeout_seconds"], 180) if thinking else self.config["model_timeout_seconds"]
        payload = request_model(request, timeout, getattr(self, "stop_event", None))
        try:
            message = payload["choices"][0]["message"]["content"]
            if not isinstance(message, str) or not message.strip():
                raise ValueError("模型没有返回答案正文")
            message = message.strip()
            if message.startswith("```"):
                message = message.split("\n", 1)[1].rsplit("```", 1)[0]
            try:
                result = json.loads(message)
                answer = result["answer"] if isinstance(result, dict) else result
            except json.JSONDecodeError:
                answer = message  # also accept a single plain answer from compatible endpoints
            if isinstance(answer, str):
                answer = answer.strip()
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise PredictionError(f"模型返回内容无法读取为答案：{error}", kind="response") from None
        check_prediction(answer, puzzle, previous)
        return {"answer": answer, "usage": payload.get("usage", {})}


class AutoRunner:
    def __init__(self, bridge: GameBridge, config: dict):
        self.bridge = bridge
        self.config = config
        self.solver = VisionSolver(config)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.histories = {}
        self.progress = {"running": False, "phase": "idle", "completed": 0, "error": None}

    def snapshot(self) -> dict:
        with self.lock:
            result = self.progress.copy()
            if result.get("request_started") is not None:
                result["request_elapsed_seconds"] = int(time.monotonic() - result["request_started"])
            return result

    def update(self, **values):
        with self.lock:
            self.progress.update(values)

    def start(self, mode: str | None = None):
        mode = self.config.get("run_mode", "continuous") if mode is None else mode
        if mode not in RUN_MODES:
            raise ValueError("模式只能为 current（猜1题）、ten（猜10题）或 continuous（一直猜）")
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("自动解题已经在运行")
            self.solver.key()
            self.stop_event.clear()
            self.progress = {"running": True, "phase": "starting", "completed": 0, "mode": mode,
                             "target": RUN_MODES[mode], "error": None}
            self.thread = threading.Thread(target=self._run, args=(mode,), daemon=True)
            self.thread.start()
        return self.snapshot()

    def stop(self):
        self.stop_event.set()
        if self.snapshot()["running"]:
            self.update(phase="stopping")
        return self.snapshot()

    def _wait_status(self, level_id: str, changed: bool) -> dict:
        deadline = time.monotonic() + self.config["transition_timeout_seconds"]
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise InterruptedError("用户已停止")
            try:
                status = self.bridge.call("status")
            except Exception:
                if changed:
                    self.stop_event.wait(0.3)
                    continue
                raise
            if changed and status["level_id"] != level_id:
                return status
            if not changed and status["level_id"] == level_id:
                return status
            self.stop_event.wait(0.3)
        raise RuntimeError("等待下一题超时，已停止，避免重复点击")

    def _wait_submission(self, level_id: str, previous_retry_count: int) -> dict:
        deadline = time.monotonic() + self.config["transition_timeout_seconds"]
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise InterruptedError("用户已停止")
            status = self.bridge.call("status")
            if status["level_id"] != level_id:
                raise RuntimeError("提交后题目被切换，自动解题已停止")
            if status["state"] != "Playing" or status["retry_count"] != previous_retry_count:
                return status
            self.stop_event.wait(0.2)
        raise RuntimeError("游戏未确认本次提交，已停止，请检查输入与提交流程")

    def _advance(self, level_id: str):
        deadline = time.monotonic() + self.config["transition_timeout_seconds"]
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise InterruptedError("用户已停止")
            result = self.bridge.call("next", level_id)
            if result.get("requested"):
                self._wait_status(level_id, changed=True)
                return
            self.stop_event.wait(0.25)
        raise RuntimeError("恭喜通关按钮未就绪，自动解题已停止")

    def _write_log(self, path: Path, event: dict):
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **event}, ensure_ascii=False) + "\n")

    def _save_history(self, level_id: str, rows: list[dict]):
        if rows:
            self.histories[level_id] = rows
        else:
            self.histories.pop(level_id, None)
        folder = ROOT / "runs"
        folder.mkdir(exist_ok=True)
        pending = folder / "guess-history.json.tmp"
        try:
            pending.write_text(json.dumps(self.histories, ensure_ascii=False), encoding="utf-8")
            pending.replace(folder / "guess-history.json")
        except OSError as error:
            LOG.warning("本轮历史已保留在内存中，写入磁盘失败：%s", error)

    def _restart(self, level_id: str):
        self.update(phase="restarting", request_started=None)
        LOG.info("本关试错次数已耗尽，自动重开并保留所有已知提示")
        self.bridge.call("restart", level_id)
        deadline = time.monotonic() + self.config["transition_timeout_seconds"]
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise InterruptedError("用户已停止")
            try:
                status = self.bridge.call("status")
            except Exception:
                self.stop_event.wait(0.2)
                continue
            if status["level_id"] != level_id:
                raise RuntimeError("重开时题目发生变化，已停止")
            if status["state"] == "Playing":
                return status
            self.stop_event.wait(0.2)
        raise RuntimeError("等待本关重开超时，请确认游戏状态")

    def _run(self, mode: str):
        target = RUN_MODES[mode]
        folder = ROOT / "runs"
        folder.mkdir(exist_ok=True)
        memory = folder / "guess-history.json"
        if memory.exists():
            try:
                self.histories.update(json.loads(memory.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                LOG.warning("历史文件暂不可读取，将使用答题区现有信息")
        logfile = folder / (time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
        self.update(log_path=str(logfile))
        completed = 0
        try:
            while not self.stop_event.is_set():
                self.update(phase="reading")
                puzzle = prepare_puzzle(self.bridge.call("puzzle"))
                level_id = puzzle["level_id"]
                self.update(level_id=level_id, display_index=puzzle["display_index"], rejected_answer=None,
                            rejection_reason=None, answer=None, model_requests=0, submissions=0)
                status = puzzle
                if status["state"] == "Win" and mode != "current":
                    # A previously completed level is not one of this run's ten new victories.
                    self.update(phase="advancing")
                    self._advance(level_id)
                    if self.stop_event.wait(self.config["between_levels_seconds"]):
                        raise InterruptedError("用户已停止")
                    continue
                previous = merge_history(self.histories.get(level_id, []), puzzle.get("feedback_history", []))
                submissions, attempt, retries, retry_streak = 0, 0, 0, 0
                while status["state"] != "Win":
                    if self.stop_event.is_set():
                        raise InterruptedError("用户已停止")
                    status = self.bridge.call("status")
                    if status["level_id"] != level_id:
                        raise RuntimeError("模型请求前题目已改变，自动解题已停止")
                    previous = merge_history(previous, status.get("feedback_history", []))
                    self._save_history(level_id, previous)
                    if status["state"] == "Win":
                        break
                    if status["state"] == "Failed":
                        status = self._restart(level_id)
                        continue
                    if status["state"] != "Playing":
                        raise RuntimeError(f"当前题目状态为 {status['state']}，请回到正常答题界面")
                    puzzle["feedback_history"] = []  # all clues are retained once in previous
                    attempt += 1
                    puzzle["request_number"] = attempt
                    puzzle["retry_streak"] = retry_streak
                    self.solver.stop_event = self.stop_event
                    self.update(phase="thinking", attempt=attempt, model_requests=attempt,
                                request_started=time.monotonic())
                    LOG.info("第 %s 关，第 %s 次请求模型答案", puzzle.get("display_index", level_id), attempt)
                    self._write_log(logfile, {"event": "model_request", "level_id": level_id, "attempt": attempt})
                    try:
                        prediction = self.solver.solve(puzzle, previous)
                        self.update(request_started=None)
                        if self.stop_event.is_set():
                            raise InterruptedError("用户已停止")
                        status = self.bridge.call("status")
                        if status["level_id"] != level_id:
                            raise RuntimeError("模型返回后题目已变化，未提交答案")
                        if status["state"] == "Win":
                            break
                        previous = merge_history(previous, status.get("feedback_history", []))
                        readings = self.bridge.call("pronunciations", [prediction["answer"]])[prediction["answer"]]
                        check_prediction(prediction["answer"], puzzle, previous, readings)
                        retries = 0
                    except ModelTemporaryError as error:
                        retries += 1
                        delay = min(2 * retries, 15)
                        self.update(phase="retrying", request_started=None, rejection_reason=None, retry_reason=str(error))
                        LOG.warning("%s；%s 秒后重试，不提交游戏", error, delay)
                        if self.stop_event.wait(delay):
                            raise InterruptedError("用户已停止")
                        continue
                    except PredictionError as error:
                        row = {"answer": error.answer, "error": str(error), "kind": error.kind, "expected_length": puzzle["expected_length"]}
                        previous = merge_history(previous, error.rejections)
                        retry_streak += 1
                        self._save_history(level_id, previous)
                        self._write_log(logfile, {"level_id": level_id, "attempt": attempt, "model_error": row})
                        self.update(phase="thinking", request_started=None, rejected_answer=error.answer,
                                    rejection_reason=str(error), model_requests=attempt, submissions=submissions)
                        LOG.warning("题目 %s，答案被拦截：%s；未提交游戏，重新猜测", level_id, error)
                        continue
                    if self.stop_event.is_set():
                        raise InterruptedError("用户已停止")
                    self.update(phase="submitting", answer=prediction["answer"],
                                rejected_answer=None, rejection_reason=None)
                    retry_streak = 0
                    previous_retry_count = status["retry_count"]
                    self.bridge.call("submit", level_id, prediction["answer"])
                    submissions += 1
                    self.update(model_requests=attempt, submissions=submissions)
                    status = self._wait_submission(level_id, previous_retry_count)
                    history = status.get("feedback_history", [])
                    visible_feedback = next((item["letters"] for item in reversed(history) if item["guess"] == prediction["answer"]), [])
                    row = {"answer": prediction["answer"], "state": status["state"], "retry_count": status["retry_count"], "letters": visible_feedback}
                    self._write_log(logfile, {"level_id": level_id, "attempt": attempt, **prediction, "feedback": row})
                    state_text = {"Playing": "未通过", "Win": "通过", "Failed": "失败", "Revealed": "答案已揭晓"}
                    LOG.info("题目 %s，提交 %s，结果 %s", level_id, prediction["answer"], state_text.get(status["state"], status["state"]))
                    if status["state"] == "Win":
                        break
                    previous = merge_history(previous, [row])
                    self._save_history(level_id, previous)
                if status["state"] != "Win":
                    raise RuntimeError(f"当前题目状态为 {status['state']}，请手动切换至未完成题目")
                if self.stop_event.is_set():
                    raise InterruptedError("用户已停止")
                completed += 1
                self.histories.pop(level_id, None)
                self._save_history(level_id, [])
                self.update(completed=completed, phase="completed", request_started=None)
                if target is not None and completed >= target:
                    break
                self.update(phase="advancing")
                self._advance(level_id)
                if self.stop_event.wait(self.config["between_levels_seconds"]):
                    raise InterruptedError("用户已停止")
            self.update(phase="stopped" if self.stop_event.is_set() else "completed")
        except InterruptedError:
            self.update(phase="stopped")
        except Exception as error:
            LOG.error("自动解题停止：%s", error)
            self.update(phase="error", error=str(error))
            self._write_log(logfile, {"error": str(error)})
        finally:
            self.update(running=False, request_started=None)


def handler_type(bridge: GameBridge, runner: AutoRunner):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            LOG.debug(format, *args)

        def reply(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/health":
                    self.reply(200, bridge.call("health"))
                elif path == "/status":
                    self.reply(200, {"game": bridge.call("status"), "runner": runner.snapshot()})
                elif path == "/progress":
                    self.reply(200, runner.snapshot())
                elif path == "/puzzle":
                    self.reply(200, prepare_puzzle(bridge.call("puzzle")))
                else:
                    self.reply(404, {"error": "接口不存在"})
            except Exception as error:
                self.reply(503, {"error": str(error)})

        def do_POST(self):
            try:
                if self.headers.get("Origin"):
                    self.reply(403, {"error": "请从本地程序调用接口"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 16384:
                    raise ValueError("请求体过大")
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urlsplit(self.path).path
                if path == "/start":
                    self.reply(200, runner.start(body.get("mode")))
                elif path == "/stop":
                    self.reply(200, runner.stop())
                elif path == "/shutdown":
                    runner.stop()
                    self.reply(200, {"closing": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif path in ("/submit", "/next"):
                    if runner.snapshot()["running"]:
                        raise RuntimeError("请先停止自动解题，再手动调用提交／下一题接口")
                    args = (body["level_id"], body["answer"]) if path == "/submit" else (body["level_id"],)
                    self.reply(200, bridge.call(path[1:], *args))
                else:
                    self.reply(404, {"error": "接口不存在"})
            except (ValueError, KeyError, TypeError) as error:
                self.reply(400, {"error": str(error)})
            except Exception as error:
                self.reply(409, {"error": str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "inspect", "solve-image"], nargs="?", default="serve")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--length", type=int, default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = load_config(args.config)
    if args.command == "solve-image":
        if args.image is None:
            parser.error("solve-image 需要 --image 图片路径")
        puzzle = {"expected_length": args.length, "image_data_urls": ["data:image/png;base64," + base64.b64encode(args.image.read_bytes()).decode("ascii")]}
        print(json.dumps(VisionSolver(config).solve(puzzle, []), ensure_ascii=False, indent=2))
        return
    bridge = GameBridge(config["game_process"], args.pid)
    runner = AutoRunner(bridge, config)
    try:
        if args.command == "inspect":
            puzzle = prepare_puzzle(bridge.call("puzzle"))
            folder = ROOT / "runs/inspect"
            folder.mkdir(parents=True, exist_ok=True)
            for index, url in enumerate(puzzle.pop("image_data_urls"), 1):
                (folder / f"image-{index}.png").write_bytes(base64.b64decode(url.split(",", 1)[1]))
            print(json.dumps(puzzle, ensure_ascii=False, indent=2))
            print(f"题面已导出到 {folder}")
            return
        server = ThreadingHTTPServer((config["host"], config["port"]), handler_type(bridge, runner))
        LOG.info("本地 API：http://%s:%s；等待 /start 请求", config["host"], config["port"])
        try:
            server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            LOG.info("正在停止服务")
        finally:
            runner.stop()
            server.server_close()
    finally:
        runner.stop()
        bridge.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        LOG.error("启动失败：%s", error)
        sys.exit(1)
