"""Build the portable Windows app from explicit public resources."""
import os
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / ".build-deps"), str(ROOT / ".runtime")]
os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT / ".build-deps"), str(ROOT / ".runtime")])
os.environ["TCL_LIBRARY"] = str(Path(sys.base_prefix) / "tcl/tcl8.6")
os.environ["TK_LIBRARY"] = str(Path(sys.base_prefix) / "tcl/tk8.6")

from PyInstaller import PLATFORM
from PyInstaller.__main__ import run

mode = "--onedir" if "--folder" in sys.argv else "--onefile"
dist = ROOT / ("build/preview" if mode == "--onedir" else "release")
args = ["desktop.py", mode, "--name", "这是谐音梗助手", "--noconfirm", "--noupx",
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
if mode == "--onefile":
    shutil.copyfile(ROOT / "便携版使用说明.txt", dist / "使用说明.txt")
    license_paths = [ROOT / "node_modules/frida-il2cpp-bridge/LICENSE",
                     ROOT / ".runtime/frida-17.23.3.dist-info/licenses/COPYING",
                     ROOT / ".build-deps/pyinstaller-6.22.3.dist-info/licenses/COPYING.txt"]
    license_paths.extend((ROOT / ".runtime").glob("httpx-*.dist-info/licenses/LICENSE*"))
    license_paths.extend((ROOT / ".runtime").glob("httpcore-*.dist-info/licenses/LICENSE*"))
    license_paths.extend((ROOT / ".runtime").glob("anyio-*.dist-info/licenses/LICENSE*"))
    notices = ["本程序使用 Python、Tcl/Tk、Pillow、Frida、frida-il2cpp-bridge、HTTPX、AnyIO 和 PyInstaller。\n"
               "项目来源：https://python.org/ 、https://tcl.tk/ 、https://python-pillow.org/ 、https://frida.re/ 、\n"
               "https://github.com/vfsfitvnm/frida-il2cpp-bridge 、https://python-httpx.org/ 、https://anyio.readthedocs.io/ 、https://pyinstaller.org/ 。\n"]
    for path in license_paths:
        if path.exists():
            notices.append(f"\n--- {path.parent.parent.name} / {path.name} ---\n" + path.read_text(encoding="utf-8"))
    (dist / "第三方许可证.txt").write_text("\n".join(notices), encoding="utf-8")
    with zipfile.ZipFile(dist / "这是谐音梗助手-便携版.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        # Deliberately exclude local credentials and user run logs from the distributable.
        for name in ["这是谐音梗助手.exe", "使用说明.txt", "第三方许可证.txt"]:
            archive.write(dist / name, name)
print("构建完成：", dist)
