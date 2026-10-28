"""检查新版本与升级（heng-agent upgrade）。

- 检查：每天最多一次，在后台线程里查 PyPI（连不上换清华镜像），结果记进用户目录的 update-check.json；
  启动时只读上一次的结果、不等网络——有新版本时欢迎页提示一行。开发版不查（代码来自仓库，用 git 更新）。
- 升级：`uv tool install --force --upgrade heng-agent`，PyPI 连不上时换清华镜像重试（和一键安装脚本一致）。

🔴 Windows 上不能在 heng-agent 运行时升级：它加载的文件被占用，uv 重装到一半失败（os error 32），
还会把安装弄坏（之后一启动就 ModuleNotFoundError: heng_agent——实测过，也是以前全局 heng-agent
莫名坏掉的原因）。所以 Windows 上：先查有没有别的 heng-agent 在运行（有就让用户先关掉），
再开一个新窗口、等当前这个进程退出了再升级。macOS / Linux 上替换正在用的文件没问题，直接升级。

测试用：HENG_AGENT_SPEC 指定要装的东西（如本地 wheel）；HENG_AGENT_UPGRADE_NO_PAUSE=1 升级窗口做完不等回车。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from heng_agent import __version__

PACKAGE = "heng-agent"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
#: 查最新版本：先 PyPI，连不上换清华镜像。都用「简单索引」的 JSON 格式（PEP 691），不用 /pypi/<包>/json：
#: 清华镜像的 /pypi/<包>/json 是旧数据（实测 pip 官方 26.2.2、它给 24.2），用它的话国内用户永远收不到新版本提示；
#: 简单索引是安装时真正用的，镜像会及时同步
VERSION_URLS = (
    f"https://pypi.org/simple/{PACKAGE}/",
    f"https://pypi.tuna.tsinghua.edu.cn/simple/{PACKAGE}/",
)
_RELEASE = re.compile(r"^\d+(\.\d+)*$")  # 只算正式版：1.2.3；不算 1.2.3rc1、1.2.3.dev4
_FILE_VERSION = re.compile(r"^heng[_-]agent-(\d+(?:\.\d+)*)(?:-|\.tar)", re.IGNORECASE)
CACHE_FILE = "update-check.json"
CHECK_INTERVAL = 24 * 3600  # 每天最多查一次


def parse_version(text: str) -> tuple[int, ...]:
    """「0.3.0」→ (0, 3, 0)；带 -dev、rc 之类后缀的部分不比。认不出就当 (0,)。"""
    parts: list[int] = []
    for piece in text.strip().lstrip("v").split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
        if digits != piece:  # 带了后缀（如 0-dev）：到此为止，不然 0.2.0-dev（v0.2.0）会读成 0.2.0.2.0
            break
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str = __version__) -> bool:
    return parse_version(latest) > parse_version(current)


def latest_from_index(data: dict[str, object]) -> str | None:
    """从简单索引的 JSON 里取最新的正式版：有 versions 字段（PEP 700）就用它，没有就从文件名里取。"""
    versions = data.get("versions")
    if not isinstance(versions, list):
        files = data.get("files") if isinstance(data.get("files"), list) else []
        versions = [m.group(1) for f in files if isinstance(f, dict)  # type: ignore[union-attr]
                    for m in [_FILE_VERSION.match(str(f.get("filename", "")))] if m]  # fmt: skip
    releases = [str(v) for v in versions if _RELEASE.match(str(v))]
    return max(releases, key=parse_version) if releases else None


def fetch_latest(timeout: float = 5.0) -> str | None:
    """问 PyPI 最新的正式版本号；都连不上返回 None。"""
    for url in VERSION_URLS:
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.pypi.simple.v1+json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # 固定的 https 地址
                latest = latest_from_index(json.load(response))
            if latest:
                return latest
        except Exception:  # 网络、解析出什么错都只是查不到，不影响使用
            continue
    return None


def _read_cache(home: Path) -> dict[str, object]:
    try:
        data = json.loads((home / CACHE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cached_newer_version(home: Path) -> str | None:
    """上一次查到的最新版本比当前新就返回它（启动时用：只读缓存，不等网络）。"""
    latest = _read_cache(home).get("latest")
    return latest if isinstance(latest, str) and is_newer(latest) else None


def refresh(home: Path, now: float | None = None) -> str | None:
    """真去查一次并写缓存；查不到也记下时间，免得网络不通时每次启动都去试。"""
    latest = fetch_latest()
    record: dict[str, object] = {"checked_at": now if now is not None else time.time()}
    if latest:
        record["latest"] = latest
    elif "latest" in (old := _read_cache(home)):
        record["latest"] = old["latest"]
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / CACHE_FILE).write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass
    return latest


def check_in_background(home: Path, now: float | None = None) -> threading.Thread | None:
    """到时间了（距上次超过一天）就在后台线程里查一次；没到时间返回 None。不阻塞启动。"""
    checked = _read_cache(home).get("checked_at")
    current = now if now is not None else time.time()
    if isinstance(checked, (int, float)) and current - checked < CHECK_INTERVAL:
        return None
    thread = threading.Thread(target=refresh, args=(home, current), name="heng-update-check", daemon=True)
    thread.start()
    return thread


def upgrade_commands(uv: str) -> list[list[str]]:
    """升级要依次试的命令：先默认的 PyPI，失败再换清华镜像。"""
    spec = os.environ.get("HENG_AGENT_SPEC") or PACKAGE
    base = [uv, "tool", "install", "--force", "--upgrade", spec]
    return [base, [*base, "--index-url", MIRROR]]


def _windows_processes() -> list[tuple[int, int, str]]:
    """Windows 上全部进程的 (进程号, 父进程号, 程序名)，用系统接口拍一张快照。只读、不结束任何进程。"""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]  # fmt: skip

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    found: list[tuple[int, int, str]] = []
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _own_chain(processes: list[tuple[int, int, str]]) -> list[int]:
    """自己这一路的进程：heng-agent.exe（启动器）→ 虚拟环境的 python.exe（转发器）→ 真正的 python.exe。
    只排除自己和父进程不够（实测：父进程是中间的转发器，最外层的 heng-agent.exe 会被当成「别的窗口」）。"""
    parents = {pid: (ppid, name) for pid, ppid, name in processes}
    chain = [os.getpid()]
    current = os.getpid()
    while current in parents and len(chain) < 10:
        ppid, _ = parents[current]
        name = parents.get(ppid, (0, ""))[1]
        if name not in ("python.exe", "pythonw.exe", "heng-agent.exe"):
            break  # 到了终端、PowerShell 这一层：不是我们的
        chain.append(ppid)
        if name == "heng-agent.exe":
            break
        current = ppid
    return chain


def running_instances(exclude: set[int]) -> list[int]:
    """Windows 上正在运行的 heng-agent.exe 的进程号（不含 exclude）。只查询、不结束任何进程。"""
    return [pid for pid, _, name in _windows_processes() if name == "heng-agent.exe" and pid not in exclude]


def _upgrade_script(uv: str, wait_for: list[int]) -> str:
    """Windows 升级窗口里跑的 PowerShell：等发起升级的 heng-agent 退出 → 升级（失败换镜像）→ 说结果。"""

    def quote(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    commands = [" ".join(quote(part) for part in cmd) for cmd in upgrade_commands(uv)]
    pause = "" if os.environ.get("HENG_AGENT_UPGRADE_NO_PAUSE") else "Read-Host '按回车关闭这个窗口'"
    return "\n".join([
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "Write-Host 'heng-agent 升级：等当前的 heng-agent 退出……'",
        f"Wait-Process -Id {','.join(str(p) for p in wait_for)} -ErrorAction SilentlyContinue",
        f"& {commands[0]}",
        "if ($LASTEXITCODE -ne 0) { Write-Host 'PyPI 上没成功，改用清华镜像再试……' -ForegroundColor Yellow; "
        f"& {commands[1]} }}",
        "if ($LASTEXITCODE -eq 0) { Write-Host '✓ 升级完成。重新运行 heng-agent 即是新版本。' -ForegroundColor Green } "
        "else { Write-Host '升级失败：关掉所有 heng-agent 窗口后重新运行一键安装脚本即可修好。' -ForegroundColor Red }",
        pause,
        "Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue",  # 临时脚本用完删掉
    ])  # fmt: skip


def run_upgrade() -> int:
    """heng-agent upgrade：用 uv 装最新版。返回退出码。"""
    uv = shutil.which("uv")
    if uv is None:
        print("没找到 uv。重新运行一键安装脚本即可升级（见 README 的「安装」）。", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        me = set(_own_chain(_windows_processes()))  # 自己这一路：Python 一直到启动它的 heng-agent.exe
        others = running_instances(me)
        if others:
            print(f"还有 {len(others)} 个 heng-agent 在运行（进程号 {', '.join(map(str, others))}）：先关掉它们再升级。"
                  "Windows 上程序运行时文件被占用，硬升级会把安装弄坏。", file=sys.stderr)  # fmt: skip
            return 1
        script = Path(tempfile.gettempdir()) / f"heng-agent-upgrade-{os.getpid()}.ps1"
        script.write_text(_upgrade_script(uv, sorted(me)), encoding="utf-8-sig")  # 带 BOM：PowerShell 5.1 才不按 GBK 读
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]  # fmt: skip
        print(f"当前版本 {__version__}。升级在新窗口里进行（要等这个进程退出、文件不再被占用）。", file=sys.stderr)
        return 0
    print(f"当前版本 {__version__}，正在升级……", file=sys.stderr)
    code = 1
    for i, command in enumerate(upgrade_commands(uv)):
        if i:
            print("PyPI 上没成功，改用清华镜像再试……", file=sys.stderr)
        code = subprocess.run(command, check=False).returncode
        if code == 0:
            print("✓ 升级完成。重新运行 heng-agent 即是新版本。", file=sys.stderr)
            return 0
    print("升级失败。可以重新运行一键安装脚本，或把上面的报错发到 https://github.com/bkywksj/heng-agent/issues",
          file=sys.stderr)  # fmt: skip
    return code
