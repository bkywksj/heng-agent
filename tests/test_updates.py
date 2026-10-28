"""检查新版本与升级（heng-agent upgrade）。Windows 专有的部分用模拟的进程列表测，各平台的 CI 都能跑。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent import __version__, cli, updates
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.ui import repl as repl_module
from heng_agent.ui.repl import Repl


def test_version_compare() -> None:
    assert updates.parse_version("0.10.0") > updates.parse_version("0.9.0")  # 按数字比，不按字符串
    assert updates.parse_version("v1.2.3") == (1, 2, 3)
    assert updates.parse_version("0.2.0-dev（v0.2.0-3-gabc）") == (0, 2, 0)  # 后缀不比
    assert updates.is_newer("0.3.0", "0.2.0") and not updates.is_newer("0.2.0", "0.2.0")
    assert not updates.is_newer("乱写", "0.2.0")


def test_cache_and_daily_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=5.0: calls.append("查") or "99.0.0")
    assert updates.cached_newer_version(tmp_path) is None  # 没查过：不提示，也不等网络
    thread = updates.check_in_background(tmp_path, now=1000.0)
    assert thread is not None
    thread.join(5)
    assert updates.cached_newer_version(tmp_path) == "99.0.0"  # 下次启动就能提示
    assert updates.check_in_background(tmp_path, now=1000.0 + 3600) is None  # 一天内不再查
    assert calls == ["查"]
    assert updates.check_in_background(tmp_path, now=1000.0 + 25 * 3600) is not None  # 过了一天再查


def test_offline_keeps_last_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / updates.CACHE_FILE).write_text(json.dumps({"checked_at": 0, "latest": "9.9.9"}), encoding="utf-8")
    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=5.0: None)  # 网络不通
    updates.refresh(tmp_path, now=500.0)
    data = json.loads((tmp_path / updates.CACHE_FILE).read_text(encoding="utf-8"))
    assert data == {"checked_at": 500.0, "latest": "9.9.9"}  # 保留上次的结果，并记下时间免得每次启动都去试


def test_upgrade_commands_fall_back_to_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HENG_AGENT_SPEC", raising=False)
    first, second = updates.upgrade_commands("uv")
    assert first == ["uv", "tool", "install", "--force", "--upgrade", "heng-agent"]
    assert second[-2:] == ["--index-url", updates.MIRROR]


def test_own_chain_covers_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    # 实际的进程链：终端 → heng-agent.exe（启动器）→ 虚拟环境的 python.exe（转发器）→ 真正的 python.exe
    monkeypatch.setattr(updates.os, "getpid", lambda: 40)
    processes = [(10, 1, "windowsterminal.exe"), (20, 10, "heng-agent.exe"), (30, 20, "python.exe"),
                 (40, 30, "python.exe"), (50, 10, "heng-agent.exe")]  # fmt: skip
    chain = updates._own_chain(processes)
    assert chain == [40, 30, 20]  # 一直到启动器为止，终端不算
    monkeypatch.setattr(updates, "_windows_processes", lambda: processes)
    assert updates.running_instances(set(chain)) == [50]  # 只有另一个窗口算「别的」


def test_upgrade_refuses_when_others_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.shutil, "which", lambda name: "C:/uv.exe")
    monkeypatch.setattr(updates.os, "getpid", lambda: 40)
    monkeypatch.setattr(updates, "_windows_processes", lambda: [(20, 10, "heng-agent.exe"), (40, 20, "python.exe"),
                                                                 (77, 10, "heng-agent.exe")])  # fmt: skip
    spawned: list[Any] = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **kw: spawned.append(a))
    assert updates.run_upgrade() == 1
    assert spawned == []  # 别的 heng-agent 开着：不升级（硬升级会把安装弄坏）


def test_upgrade_runs_in_new_window_after_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates.sys, "platform", "win32")
    monkeypatch.setattr(updates.shutil, "which", lambda name: "C:/uv.exe")
    monkeypatch.setattr(updates.os, "getpid", lambda: 40)
    monkeypatch.setattr(updates, "_windows_processes", lambda: [(20, 10, "heng-agent.exe"), (40, 20, "python.exe")])
    monkeypatch.setattr(updates.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(updates.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    spawned: list[Any] = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda args, **kw: spawned.append((args, kw)))
    assert updates.run_upgrade() == 0
    args, kw = spawned[0]
    assert args[0] == "powershell.exe" and kw["creationflags"] == 16  # 新窗口
    script = Path(args[-1]).read_text(encoding="utf-8-sig")
    assert "Wait-Process -Id 20,40" in script  # 等这一路进程（含启动器）都退出、文件不再被占用再升级
    assert script.index("Wait-Process") < script.index("tool' 'install'")
    assert updates.MIRROR in script and "Remove-Item -LiteralPath $PSCommandPath" in script
    assert Path(args[-1]).read_bytes().startswith(b"\xef\xbb\xbf")  # 带 BOM：PowerShell 5.1 才不按 GBK 读


def test_upgrade_without_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates.shutil, "which", lambda name: None)
    assert updates.run_upgrade() == 1


def test_cli_upgrade_in_dev_build(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["upgrade"]) == 1  # 测试从源码跑 = 开发版
    assert "git pull" in capsys.readouterr().err


def test_banner_shows_new_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / updates.CACHE_FILE).write_text(json.dumps({"checked_at": 9e18, "latest": "99.0.0"}), encoding="utf-8")
    monkeypatch.setattr(repl_module, "is_dev", lambda: False)

    def banner(**kw: Any) -> str:
        console = Console(record=True, width=200, force_terminal=False)
        Repl(Agent(Settings(workdir=tmp_path, home=home, **kw), FakeModel([])), console=console).banner()
        return console.export_text()

    assert "有新版本 99.0.0" in banner() and "heng-agent upgrade" in banner()
    assert "有新版本" not in banner(update_check=False)  # 可以关掉
    monkeypatch.setattr(repl_module, "is_dev", lambda: True)
    assert "有新版本" not in banner()  # 开发版不提示
    assert __version__  # 当前版本号存在（提示里用）
