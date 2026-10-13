"""Windows desktop entry point for the portable solver."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font

import badpun
from desktop_service import DesktopService, validate_config, simple_model_config, save_config, fetch_models

PHASES = {"idle": "待命", "starting": "准备开始", "reading": "读取题面", "thinking": "请求模型解题",
          "submitting": "提交答案", "advancing": "进入下一题", "completed": "本次运行完成",
          "stopping": "正在停止", "stopped": "已停止", "error": "需要处理"}
PHASES.update(retrying="请求超时／网络重试中", restarting="重开当前关卡")
RUN_MODE_LABELS = {"current": "猜1题", "ten": "猜10题", "continuous": "一直猜"}


def configure_tk():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    if not getattr(sys, "frozen", False):
        runtime = Path(sys.base_prefix) / "tcl"
        if (runtime / "tcl8.6/init.tcl").exists():
            os.environ["TCL_LIBRARY"] = str(runtime / "tcl8.6")
            os.environ["TK_LIBRARY"] = str(runtime / "tk8.6")


class QueueLog(logging.Handler):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        self.messages.put(("log", self.format(record)))


class DesktopApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config_path = badpun.ROOT / "config.local.json"
        self.config = badpun.load_config(self.config_path)
        self.service = DesktopService()
        self.messages = queue.Queue()
        self.pending = set()
        self.connected = False
        self.running = False
        self.closing = False
        self.closed = False
        self.latest = {}
        self.variables = {}
        self.log_handler = QueueLog(self.messages)
        logging.getLogger("badpun").addHandler(self.log_handler)
        logging.getLogger("badpun").setLevel(logging.INFO)
        root.title("这是谐音梗 · 自动解题助手")
        root.geometry("1020x900")
        root.minsize(940, 720)
        root.configure(bg="#f3f6fb")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.style()
        self.build()
        self.set_form(self.config)
        self.update_buttons()
        root.after(100, self.drain)
        root.after(1000, self.poll)

    def style(self):
        font.nametofont("TkDefaultFont").configure(family="Microsoft YaHei UI", size=10)
        font.nametofont("TkTextFont").configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fb")
        style.configure("TLabel", background="#f3f6fb", foreground="#203047", padding=2)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Muted.TLabel", foreground="#65758b")
        style.configure("Card.TLabelframe", background="#f3f6fb", bordercolor="#d7dfeb", padding=14)
        style.configure("Card.TLabelframe.Label", background="#f3f6fb", foreground="#33445b", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TNotebook", background="#f3f6fb", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(22, 7), background="#e7edf6")
        # Override the theme's selected padding, which otherwise shrinks our active tab.
        style.map("TNotebook.Tab", padding=[("selected", (22, 11)), ("!selected", (22, 7))],
                  background=[("selected", "#f3f6fb"), ("!selected", "#e7edf6")],
                  foreground=[("selected", "#203047"), ("!selected", "#65758b")])
        style.configure("TButton", padding=(14, 8), background="#e8edf5", bordercolor="#d7dfeb")
        style.configure("Primary.TButton", foreground="white", background="#2467e8", bordercolor="#2467e8")
        style.map("Primary.TButton", background=[("active", "#1557cd"), ("disabled", "#bdcce3")])
        style.configure("TEntry", padding=6, fieldbackground="white")
        style.configure("TCombobox", padding=6)
        style.configure("RunMode.TCombobox", padding=6, fieldbackground="white", background="white",
                        foreground="#203047", selectbackground="white", selectforeground="#203047")
        style.map("RunMode.TCombobox",
                  fieldbackground=[("disabled", "#e2e6ed"), ("!disabled", "white")],
                  background=[("disabled", "#e2e6ed"), ("!disabled", "white")],
                  foreground=[("disabled", "#8994a3"), ("!disabled", "#203047")],
                  arrowcolor=[("disabled", "#8994a3"), ("!disabled", "#203047")],
                  selectbackground=[("!disabled", "white")], selectforeground=[("!disabled", "#203047")])
        style.configure("TSpinbox", padding=6)
        style.configure("TCheckbutton", background="#f3f6fb")

    def build(self):
        shell = ttk.Frame(self.root, padding=22)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="这是谐音梗 · 自动解题助手", style="Title.TLabel").pack(anchor="w")
        ttk.Label(shell, text="识别题面 → 结合字与拼音反馈猜答案 → 自动提交并进入下一题", style="Muted.TLabel").pack(anchor="w", pady=(4, 16))
        self.notice = tk.StringVar(value="先填写模型配置，再启动游戏进入单人图片题，点击“连接游戏”。")
        self.notice_label = ttk.Label(shell, textvariable=self.notice, style="Muted.TLabel", wraplength=950)
        self.notice_label.pack(side="bottom", fill="x", pady=(12, 0))
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        self.run_page = ttk.Frame(self.tabs, padding=18)
        self.model_page = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(self.run_page, text="运行控制")
        self.tabs.add(self.model_page, text="视觉语言模型（VLM）配置")
        self.model_form = self.model_page
        self.build_run()
        self.build_model()

    def build_run(self):
        top = ttk.Frame(self.run_page)
        top.pack(fill="x")
        self.connection = tk.StringVar(value="尚未连接游戏")
        self.phase = tk.StringVar(value="待命")
        self.summary = tk.StringVar(value="题目 —  ·  本次完成 0 题")
        ttk.Label(top, textvariable=self.connection, font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        ttk.Label(top, textvariable=self.phase, foreground="#155ad4").pack(side="right")
        ttk.Label(self.run_page, textvariable=self.summary, style="Muted.TLabel").pack(anchor="w", pady=(6, 14))
        options = ttk.LabelFrame(self.run_page, text="本次运行", style="Card.TLabelframe")
        options.pack(fill="x")
        self.variables["run_mode"] = tk.StringVar(value=RUN_MODE_LABELS["continuous"])
        self.run_mode_picker = ttk.Combobox(options, textvariable=self.variables["run_mode"],
                                            values=list(RUN_MODE_LABELS.values()), state="readonly", width=18,
                                            style="RunMode.TCombobox")
        self.run_mode_picker.pack(side="left")
        ttk.Label(self.run_page, text="每关持续尝试直到答对；试错耗尽后自动重开，保留已知提示。", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        buttons = ttk.Frame(self.run_page)
        buttons.pack(fill="x", pady=16)
        self.connect_button = ttk.Button(buttons, text="连接游戏", command=self.connect)
        self.connect_button.pack(side="left")
        self.start_button = ttk.Button(buttons, text="开始自动解题", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left", padx=10)
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop)
        self.stop_button.pack(side="left")
        self.disconnect_button = ttk.Button(buttons, text="断开连接", command=self.disconnect)
        self.disconnect_button.pack(side="left", padx=10)
        ttk.Button(buttons, text="打开日志目录", command=self.open_logs).pack(side="right")
        self.last_answer = tk.StringVar(value="最近提交：—")
        ttk.Label(self.run_page, textvariable=self.last_answer, wraplength=920).pack(anchor="w")
        self.feedback = tk.StringVar(value="灰色：答案中没有，后续排除；橙色：存在但位置不对；绿色：位置正确。")
        ttk.Label(self.run_page, textvariable=self.feedback, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(4, 12))
        log_frame = ttk.LabelFrame(self.run_page, text="运行日志", style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=10, relief="flat", bg="white", fg="#33445b", wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    def field(self, parent, row, label, key, choices=None, secret=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
        self.variables[key] = tk.StringVar()
        if choices:
            widget = ttk.Combobox(parent, textvariable=self.variables[key], values=choices)
        else:
            widget = ttk.Entry(parent, textvariable=self.variables[key], show="●" if secret else "")
        widget.grid(row=row, column=1, sticky="ew", pady=5)
        return widget

    def build_model(self):
        server = ttk.LabelFrame(self.model_form, text="接口与模型", style="Card.TLabelframe")
        server.pack(fill="x")
        server.columnconfigure(1, weight=1)
        self.field(server, 0, "接口地址", "model_base_url")
        self.model_entry = self.field(server, 1, "模型名称", "model", ["deepseek-flash"])
        self.key_entry = self.field(server, 2, "接口密钥", "api_key", secret=True)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(server, text="显示", variable=self.show_key, command=lambda: self.key_entry.configure(show="" if self.show_key.get() else "●")).grid(row=2, column=2, padx=(10, 0))
        ttk.Label(self.model_form, text="其他参数使用默认值，推理模式关闭。", style="Muted.TLabel").pack(anchor="w", pady=14)
        actions = ttk.Frame(self.model_form)
        actions.pack(fill="x", pady=(2, 10))
        ttk.Button(actions, text="保存配置", style="Primary.TButton", command=self.save).pack(side="left")
        self.verify_button = ttk.Button(actions, text="验证接口并读取模型", command=self.verify)
        self.verify_button.pack(side="left", padx=10)
        ttk.Button(actions, text="导入已有配置", command=self.import_config).pack(side="left")
        ttk.Label(self.model_form, text="配置保存在本机，下次开始解题时生效。", style="Muted.TLabel").pack(anchor="w", pady=(0, 6))

    def set_form(self, config):
        config = {**badpun.DEFAULTS, **config}
        for key, variable in self.variables.items():
            value = config[key]
            if key == "run_mode":
                value = RUN_MODE_LABELS[value]
            variable.set(value)

    def get_config(self):
        result = {**self.config, **{key: variable.get() for key, variable in self.variables.items()}}
        result["run_mode"] = {label: mode for mode, label in RUN_MODE_LABELS.items()}[result["run_mode"]]
        return simple_model_config(result)

    def save(self):
        try:
            config = self.get_config()
            save_config(self.config_path, config)
            self.config = config
            self.notice.set("配置已保存。下一次点击“开始自动解题”时生效。")
            return config
        except Exception as error:
            messagebox.showerror("配置未保存", str(error), parent=self.root)
            return None

    def import_config(self):
        path = filedialog.askopenfilename(parent=self.root, title="导入模型配置", filetypes=[("配置文件", "*.json")])
        if path:
            try:
                config = simple_model_config(badpun.load_config(Path(path)))
                self.config = config
                self.set_form(config)
                self.notice.set("配置已导入，点击“保存配置”后保存在程序旁。")
            except Exception as error:
                messagebox.showerror("导入失败", str(error), parent=self.root)

    def task(self, name, action, callback=None):
        if name in self.pending: return
        self.pending.add(name)
        self.update_buttons()
        def worker():
            try: self.messages.put(("result", name, action(), callback))
            except Exception as error: self.messages.put(("error", name, str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        config = self.save()
        if config:
            self.notice.set("正在连接游戏，请稍候…")
            self.task("connect", lambda: self.service.connect(config), self.did_connect)

    def did_connect(self, message):
        self.connected = True
        self.notice.set(message)
        self.connection.set("游戏已连接")

    def start(self):
        config = self.save()
        if config: self.task("start", lambda: self.service.start(config), self.did_start)

    def did_start(self, _):
        self.running = True
        self.notice.set("自动解题已开始；游戏可最小化运行。")

    def stop(self):
        self.service.stop()
        self.notice.set("已请求停止，正在取消等待中的模型请求。")

    def disconnect(self):
        self.task("disconnect", self.service.close, self.did_disconnect)

    def did_disconnect(self, _):
        self.connected = self.running = False
        self.connection.set("尚未连接游戏")
        self.phase.set("待命")
        self.notice.set("已断开连接。")

    def verify(self):
        try: config = self.get_config()
        except Exception as error:
            messagebox.showerror("配置不正确", str(error), parent=self.root)
            return
        self.notice.set("正在验证接口并读取模型列表…")
        def complete(models):
            self.model_entry.configure(values=models)
            selected = config["model"]
            self.notice.set(f"接口连接成功，读取到 {len(models)} 个模型。" + ("当前模型在列表中。" if selected in models else "请确认当前模型名称支持图片输入。"))
        self.task("verify", lambda: fetch_models(config), complete)

    def open_logs(self):
        path = badpun.ROOT / "runs"
        path.mkdir(exist_ok=True)
        os.startfile(str(path))

    def poll(self):
        if self.closing: return
        if self.connected and "disconnect" not in self.pending:
            self.task("status", self.service.status, self.show_status)
        self.root.after(1000, self.poll)

    def show_status(self, status):
        self.latest = status
        self.connected = status.get("connected", False)
        runner = status.get("runner", {})
        self.running = runner.get("running", False)
        self.connection.set("游戏已连接" if self.connected else "游戏连接已断开")
        self.phase.set(PHASES.get(runner.get("phase"), "待命"))
        game = status.get("game", {})
        requests = runner.get("model_requests", 0)
        self.summary.set(f"当前第 {game.get('display_index', '—')} 题  ·  {game.get('category', '')}  ·  本次完成 {runner.get('completed', 0)} 题  ·  本关请求 {requests} 次，提交 {runner.get('submissions', 0)} 次")
        if runner.get("phase") == "thinking" and runner.get("request_started") is not None:
            self.phase.set(f"等待模型答案 · 已等待 {runner.get('request_elapsed_seconds', 0)} 秒")
        if runner.get("answer"):
            self.last_answer.set(f"最近提交：{runner['answer']}  ·  第 {runner.get('display_index', '—')} 题")
        history = game.get("feedback_history", [])
        if history:
            names = {"correct_position": "位置正确", "present_elsewhere": "存在、位置不对", "absent": "不存在"}
            letters = history[-1]["letters"]
            self.feedback.set("最近反馈：" + "；".join(f"{item['character']}：字{names.get(item.get('character_feedback'), '—')}，拼音{names.get(item.get('pinyin_feedback'), '—')}" for item in letters))
        if runner.get("error"):
            self.notice.set(runner["error"])
        elif runner.get("phase") == "retrying":
            self.notice.set(runner.get("retry_reason", "正在重试模型请求…"))
        elif runner.get("phase") == "completed":
            self.notice.set(f"本次已答对 {runner.get('completed', 0)} 题，已停留在最后一关。")
        elif runner.get("rejection_reason"):
            self.notice.set("答案被拦截：" + runner["rejection_reason"] + "；未提交游戏，重新猜测。")
        elif status.get("game_error"):
            self.notice.set("题目暂不可读取，请确认已进入单人图片题。")
        self.update_buttons()

    def update_buttons(self):
        busy = bool(self.pending & {"connect", "disconnect", "start", "close"})
        self.connect_button.configure(state="normal" if not self.connected and not busy else "disabled")
        self.start_button.configure(state="normal" if self.connected and not self.running and not busy else "disabled")
        self.stop_button.configure(state="normal" if self.running else "disabled")
        self.disconnect_button.configure(state="normal" if self.connected and not busy else "disabled")
        self.verify_button.configure(state="disabled" if "verify" in self.pending else "normal")
        self.run_mode_picker.configure(state="disabled" if self.running or busy else "readonly")

    def drain(self):
        while True:
            try: message = self.messages.get_nowait()
            except queue.Empty: break
            if message[0] == "log":
                self.log.configure(state="normal")
                self.log.insert("end", message[1] + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
            else:
                kind, name = message[:2]
                self.pending.discard(name)
                if kind == "result":
                    if message[3]: message[3](message[2])
                    if self.closed: return
                elif name == "status":
                    self.notice.set("读取状态失败：" + message[2])
                else:
                    self.notice.set(message[2])
                    messagebox.showerror("操作未完成", message[2], parent=self.root)
                self.update_buttons()
        if not self.closing or self.pending:
            self.root.after(100, self.drain)

    def close(self):
        if self.closing: return
        self.closing = True
        self.service.stop()
        self.notice.set("正在停止服务并断开游戏连接…")
        def finished(_):
            logging.getLogger("badpun").removeHandler(self.log_handler)
            self.closed = True
            self.root.destroy()
        self.task("close", self.service.close, finished)


def smoke_test(output: Path):
    """Exercise packaged resources and both pages, without game mutations or model requests."""
    import tempfile
    import ctypes
    import frida
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import Request
    from PIL import Image
    root = tk.Tk()
    root.withdraw()
    app = DesktopApp(root)
    # Never use a real credential when exercising save/reload.
    config = badpun.DEFAULTS.copy()
    app.set_form(config)
    config = app.get_config()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        save_config(path, config)
        loaded = badpun.load_config(path)
    mode_roundtrips = {}
    for mode in RUN_MODE_LABELS:
        app.set_form({**config, "run_mode": mode})
        mode_roundtrips[mode] = app.get_config()["run_mode"] == mode
    app.set_form(config)
    root.update_idletasks()
    class LocalModel(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"choices": [{"message": {"content": '{"answer":"测试"}'}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous_proxy = os.environ.get("NO_PROXY")
    os.environ["NO_PROXY"] = "127.0.0.1,localhost" + ("," + previous_proxy if previous_proxy else "")
    try:
        local_response = badpun.request_model(Request(f"http://127.0.0.1:{server.server_port}/chat/completions", data=b"{}"), 3)
    finally:
        if previous_proxy is None: os.environ.pop("NO_PROXY", None)
        else: os.environ["NO_PROXY"] = previous_proxy
        server.shutdown()
        server.server_close()
    result = {"pages": [app.tabs.tab(tab, "text") for tab in app.tabs.tabs()],
              "console_attached": bool(ctypes.windll.kernel32.GetConsoleWindow()) if sys.platform == "win32" else None,
              "standard_streams_unavailable": all(stream is None for stream in (sys.stdin, sys.stdout, sys.stderr)),
              "thinking_enabled": loaded["thinking_enabled"], "config_roundtrip": loaded == config,
              "model_fields": sorted(set(app.variables) - {"run_mode"}),
              "run_mode": loaded["run_mode"],
              "run_mode_options": list(app.run_mode_picker["values"]), "run_mode_roundtrips": mode_roundtrips,
              "run_mode_enabled_background": ttk.Style(root).lookup("RunMode.TCombobox", "fieldbackground", ("readonly",)),
              "run_mode_disabled_background": ttk.Style(root).lookup("RunMode.TCombobox", "fieldbackground", ("disabled",)),
              "model_transport_available": bool(local_response.get("choices")),
              "frida_version": frida.__version__, "pillow_available": Image is not None,
              "agent_resource": (badpun.RESOURCES / "agent.js").exists(),
              "bridge_resource": (badpun.RESOURCES / "node_modules/frida-il2cpp-bridge/dist/index.js").exists()}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    root.destroy()


def main():
    configure_tk()
    if "--smoke-test" in sys.argv:
        smoke_test(Path(sys.argv[sys.argv.index("--smoke-test") + 1]))
        return
    root = tk.Tk()
    try:
        DesktopApp(root)
        root.mainloop()
    except Exception as error:
        messagebox.showerror("启动失败", str(error), parent=root)
        root.destroy()


if __name__ == "__main__":
    main()
