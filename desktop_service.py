"""Desktop configuration and the embedded local API service."""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import badpun


def simple_model_config(config: dict) -> dict:
    """Keep the three model inputs; normalize hidden options even after a legacy import."""
    result = {**badpun.DEFAULTS, **config}
    for key in ("api_key_env", "thinking_enabled", "reasoning_effort", "temperature",
                "model_timeout_seconds", "max_output_tokens"):
        result[key] = badpun.DEFAULTS[key]
    result["thinking_enabled"] = False
    for obsolete in ("max_levels_per_run", "max_attempts_per_level"):
        result.pop(obsolete, None)
    hostname = urlsplit(str(result["model_base_url"]).strip()).hostname
    result["provider"] = "deepseek" if hostname == "api.deepseek.com" else "compatible"
    return validate_config(result)


def validate_config(config: dict) -> dict:
    result = {**badpun.DEFAULTS, **config}
    address = urlsplit(str(result["model_base_url"]).strip())
    if address.scheme not in ("http", "https") or not address.hostname:
        raise ValueError("接口地址应为完整的 http:// 或 https:// 地址")
    if address.username or address.password or address.query or address.fragment:
        raise ValueError("接口地址不能包含用户名、密码、查询参数或片段")
    result["model_base_url"] = result["model_base_url"].strip().rstrip("/")
    result["model"] = str(result["model"]).strip()
    if not result["model"]:
        raise ValueError("请填写模型名称")
    if result["provider"] not in ("deepseek", "compatible"):
        raise ValueError("请选择接口类型")
    ranges = {"model_timeout_seconds": (10, 600), "max_output_tokens": (128, 131072)}
    labels = {"model_timeout_seconds": "请求超时", "max_output_tokens": "输出令牌上限"}
    for key, (low, high) in ranges.items():
        try:
            value = int(result[key])
        except (ValueError, TypeError):
            raise ValueError(f"{labels[key]}应填写整数") from None
        if not low <= value <= high:
            raise ValueError(f"{labels[key]}必须在 {low}～{high} 之间")
        result[key] = value
    if result["run_mode"] not in badpun.RUN_MODES:
        raise ValueError("请选择猜1题、猜10题或一直猜")
    result["temperature"] = float(result["temperature"])
    result["between_levels_seconds"] = float(result["between_levels_seconds"])
    if not 0 <= result["temperature"] <= 2:
        raise ValueError("温度须在 0～2 之间")
    if not 0 <= result["between_levels_seconds"] <= 60:
        raise ValueError("题间间隔须在 0～60 秒之间")
    if result["host"] not in ("127.0.0.1", "localhost"):
        raise ValueError("本地接口只允许绑定回环地址")
    if not 1 <= int(result["port"]) <= 65535:
        raise ValueError("本地接口端口不正确")
    result["thinking_enabled"] = bool(result["thinking_enabled"])
    if result["reasoning_effort"] not in ("low", "high", "max"):
        raise ValueError("推理强度配置不正确")
    return result


def save_config(path: Path, config: dict):
    config = validate_config(config)
    pending = path.with_suffix(".json.tmp")
    pending.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def fetch_models(config: dict) -> list[str]:
    config = validate_config(config)
    key = badpun.VisionSolver(config).key()
    request = Request(config["model_base_url"] + "/models", headers={"Authorization": "Bearer " + key})
    try:
        with urlopen(request, timeout=min(config["model_timeout_seconds"], 20)) as response:
            payload = json.load(response)
        return sorted(str(item["id"]) for item in payload["data"])
    except Exception as error:
        raise RuntimeError(str(error).replace(key, "[密钥已隐藏]")) from None


class DesktopService:
    def __init__(self):
        self.bridge = None
        self.runner = None
        self.server = None
        self.server_thread = None
        self.lock = threading.RLock()

    def connect(self, config: dict):
        config = validate_config(config)
        with self.lock:
            if self.bridge is not None:
                if self.bridge.detached:
                    self.close()
                else:
                    raise RuntimeError("已连接游戏")
            # Reserve the port before injecting, so an old service cannot cause a second bridge.
            server = ThreadingHTTPServer((config["host"], config["port"]), None)
            try:
                bridge = badpun.GameBridge(config["game_process"])
            except Exception:
                server.server_close()
                raise
            runner = badpun.AutoRunner(bridge, config.copy())
            server.RequestHandlerClass = badpun.handler_type(bridge, runner)
            self.bridge, self.runner, self.server = bridge, runner, server
            self.server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
            self.server_thread.start()
            return f"已连接游戏；本地接口 http://{config['host']}:{config['port']}"

    def start(self, config: dict):
        config = validate_config(config)
        with self.lock:
            if self.runner is None:
                raise RuntimeError("请先连接游戏")
            if self.runner.snapshot()["running"]:
                raise RuntimeError("自动解题已经在运行")
            self.runner.config = config.copy()
            self.runner.solver = badpun.VisionSolver(self.runner.config)
            return self.runner.start(config["run_mode"])

    def status(self):
        if self.bridge is None:
            return {"connected": False}
        result = {"connected": not self.bridge.detached, "runner": self.runner.snapshot()}
        try:
            result["game"] = self.bridge.call("status")
        except Exception as error:
            result["game_error"] = str(error)
        return result

    def stop(self):
        if self.runner is not None:
            return self.runner.stop()
        return {"running": False}

    def close(self):
        with self.lock:
            self.stop()
            if self.server is not None:
                self.server.shutdown()
                self.server.server_close()
            if self.bridge is not None:
                self.bridge.close()
            self.bridge = self.runner = self.server = self.server_thread = None
