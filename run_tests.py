#!/usr/bin/env python3
"""run_tests.py — intent-router 全量测试的规范入口：自动检测活体 hermes venv，直接用宿主 python 跑。

背景（owner 裁决 2026-11-18，两次修正）：
- hermes 的 venv 路径随安装代际漂移（hermes-agent\\venv 3.11 vs tools\\python-3.14.x），
  不允许每次靠 PATH 猜或硬编码 venv 路径。
- 也不派生 scratch venv（owner 修正："用宿主 python 直接跑"）——宿主解释器缺
  pytest 就直接装进它（测试运行器对运行时惰性无副作用），零副本。

检测链：
  1. <HERMES_HOME>/gateway.pid  (JSON: {pid, argv, hermes_home, kind}) → 活体 gateway pid
  2. pid → python.exe 路径（Windows: Get-Process -Id | Path；POSIX: /proc/<pid>/exe）
     pid 已死（gateway 重启过）→ 兜底扫描 exe/命令行含 hermes 的 python 进程
  3. 宿主解释器无 pytest → ensurepip 引导 + pip install pytest（直装宿主，幂等）
  4. 宿主解释器跑 `pytest tests/ -q`（透传附加参数）

用法：
  python run_tests.py              # 全量测试（自动检测 + 自动装 pytest）
  python run_tests.py -k lifecycle # 透传 pytest 参数
  python run_tests.py --detect-only# 只打印检测到的解释器

注意：不同解释器收集的用例数可能不同（版本门控测试）——规范验收基线
= 活体 gateway 解释器（本脚本检测到的那个），与宿主运行时对齐。
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _log(msg: str) -> None:
    print(f"[run_tests] {msg}", file=sys.stderr)


def find_hermes_home() -> Path | None:
    """候选链（agent shell 里 HERMES_HOME 可能是 profile 目录、不含 gateway.pid，
    缺文件必须继续试下一个候选，不能一票否决）："""
    candidates: list[Path] = []
    env = os.environ.get("HERMES_HOME")
    if env:
        candidates.append(Path(env))
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "hermes")
    else:
        candidates.append(Path.home() / ".hermes")
    for p in candidates:
        if (p / "gateway.pid").is_file():
            return p
    return None


def read_gateway_pid(hermes_home: Path) -> int | None:
    try:
        data = json.loads((hermes_home / "gateway.pid").read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        return pid or None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    if platform.system() == "Windows":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Out-Null; $?"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() == "True"
    return Path(f"/proc/{pid}").exists()


def _pid_to_exe(pid: int) -> str | None:
    """pid → python 可执行路径。Windows: Get-Process Path; POSIX: /proc 链接。"""
    if platform.system() == "Windows":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Path"],
            capture_output=True, text=True,
        )
        out = r.stdout.strip()
        return out if out and "python" in out.lower() else None
    link = Path(f"/proc/{pid}/exe")
    return str(link.resolve()) if link.exists() else None


def scan_hermes_python_fallback() -> str | None:
    """gateway.pid 的 pid 已死（重启）时的兜底：找 exe/命令行含 hermes 的 python 进程。"""
    if platform.system() != "Windows":
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.ExecutablePath -like '*hermes*' -or $_.CommandLine -like '*hermes*' } | "
             "Select-Object -ExpandProperty ExecutablePath"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in r.stdout.splitlines():
        path = line.strip()
        if path:
            return path
    return None


def detect_live_python() -> str | None:
    """检测链主入口。返回活体 hermes 解释器路径（失败 None，调用方回退 sys.executable）。"""
    home = find_hermes_home()
    if home is None:
        _log("未找到 HERMES_HOME/gateway.pid，回退 sys.executable")
        return None
    pid = read_gateway_pid(home)
    if pid and _pid_alive(pid):
        exe = _pid_to_exe(pid)
        if exe:
            _log(f"gateway.pid pid={pid} → {exe}")
            return exe
        _log(f"pid={pid} 存活但取不到 python exe（可能非 python 进程），进入兜底扫描")
    else:
        _log(f"gateway.pid 缺失/pid={pid} 已死（gateway 重启过），进入兜底扫描")
    exe = scan_hermes_python_fallback()
    if exe:
        _log(f"兜底扫描命中 → {exe}")
    return exe


def _python_version(python_exe: str) -> str:
    r = subprocess.run([python_exe, "-V"], capture_output=True, text=True)
    return r.stdout.strip().replace("Python ", "") or r.stderr.strip().replace("Python ", "")


def _has_pytest(python_exe: str) -> bool:
    r = subprocess.run([python_exe, "-m", "pytest", "--version"],
                       capture_output=True, text=True)
    return r.returncode == 0


def ensure_pytest(python_exe: str) -> None:
    """宿主解释器缺 pytest 就直装（owner 裁决：不派生 venv，用宿主 python 直接跑）。"""
    if _has_pytest(python_exe):
        return
    _log(f"{python_exe} 无 pytest，ensurepip 引导 + 直装宿主")
    subprocess.run([python_exe, "-m", "ensurepip", "--upgrade"],
                   capture_output=True, text=True)
    r = subprocess.run([python_exe, "-m", "pip", "install", "-q", "pytest"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pip install pytest 失败: {r.stderr.strip()[:300]}")
    if not _has_pytest(python_exe):
        raise RuntimeError(f"装后仍不可用: {python_exe}")


def main(argv: list[str]) -> int:
    detect_only = "--detect-only" in argv
    extra = [a for a in argv if a != "--detect-only"]

    live = detect_live_python()
    py = live or sys.executable
    if live is None:
        _log(f"检测失败，回退 sys.executable={py}")
    if detect_only:
        print(py)
        return 0
    ensure_pytest(py)
    _log(f"宿主解释器: {py} (v{_python_version(py)})")
    r = subprocess.run([py, "-m", "pytest", "tests/", "-q"] + extra, cwd=REPO)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
