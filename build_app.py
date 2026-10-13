"""Build the portable Windows app from explicit public resources."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
APP_NAME = "BadPunPC_Assistant"
sys.path[:0] = [str(ROOT / ".build-deps"), str(ROOT / ".runtime")]
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / ".build-deps"), str(ROOT / ".runtime")])
os.environ["TCL_LIBRARY"] = str(Path(sys.base_prefix) / "tcl/tcl8.6")
os.environ["TK_LIBRARY"] = str(Path(sys.base_prefix) / "tcl/tk8.6")

from PyInstaller import PLATFORM
from PyInstaller.__main__ import run

mode = "--onedir" if "--folder" in sys.argv else "--onefile"
dist = ROOT / ("build/preview" if mode == "--onedir" else "release")
args = ["desktop.py", mode, "--name", APP_NAME, "--noconfirm", "--noupx",
        "--windowed", "--distpath", str(dist),
        "--workpath", str(ROOT / "build/pyinstaller"), "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT / ".runtime"), "--collect-all", "frida",
        "--hidden-import", "anyio._backends._asyncio",
        "--add-data", str(ROOT / "config.example.json") + ":.", "--add-data", str(ROOT / "agent.js") + ":.",
        "--add-data", str(ROOT / "node_modules/frida-il2cpp-bridge/dist/index.js") + ":node_modules/frida-il2cpp-bridge/dist",
        "--log-level", "WARN"]
bootloaders = ROOT / ".build-deps/PyInstaller/bootloader" / PLATFORM
if not (bootloaders / "runw.exe").exists():
    if not (bootloaders / "runw_d.exe").exists():
        raise SystemExit("官方窗口版启动器缺失，无法构建无终端程序。")
    # This official windowed variant logs loader diagnostics to OutputDebugString,
    # and does not create a console. Do not restore quarantined files or fall back to console mode.
    args += ["--debug", "bootloader"]
    print("使用已安装的官方窗口版诊断启动器；程序启动不会创建终端。")
for module in ["numpy", "scipy", "pandas", "matplotlib", "torch", "tensorflow", "IPython", "pytest"]:
    args += ["--exclude-module", module]
run(args)
print("构建完成：", dist)
