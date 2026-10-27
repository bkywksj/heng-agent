"""/terminal-setup：给 Windows Terminal 加 Shift+Enter → 发送 \\n（输入框里是换行）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.ui import terminal_setup as ts
from heng_agent.ui.repl import Repl

NEW_FORMAT = {"actions": [], "keybindings": [{"id": "Terminal.CopyToClipboard", "keys": "ctrl+c"}], "profiles": {}}
OLD_FORMAT = {"actions": [{"command": "copy", "keys": "ctrl+c"}], "profiles": {}}


def _settings(tmp_path: Path, data: object, name: str = "settings.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def test_install_new_format_and_idempotent(tmp_path: Path) -> None:
    path = _settings(tmp_path, NEW_FORMAT)
    assert ts.status(path) == "none"
    assert "已加上" in ts.install(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"command": {"action": "sendInput", "input": "\n"}, "id": ts.ACTION_ID} in data["actions"]
    assert {"id": ts.ACTION_ID, "keys": "shift+enter"} in data["keybindings"]
    assert {"id": "Terminal.CopyToClipboard", "keys": "ctrl+c"} in data["keybindings"]  # 原有的不动
    assert (tmp_path / "settings.json.heng-backup").is_file()  # 改之前备份了
    assert ts.status(path) == "ours"
    assert ts.install(path) == "已经配好了"
    assert len(json.loads(path.read_text(encoding="utf-8"))["keybindings"]) == 2  # 再跑一次不重复加


def test_install_old_format(tmp_path: Path) -> None:
    path = _settings(tmp_path, OLD_FORMAT)
    ts.install(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"command": {"action": "sendInput", "input": "\n"}, "keys": "shift+enter"} in data["actions"]
    assert "keybindings" not in data  # 老格式就照老格式写
    assert ts.status(path) == "ours"


def test_existing_binding_left_alone(tmp_path: Path) -> None:
    other = {"actions": [], "keybindings": [{"id": "Terminal.Something", "keys": "Shift+Enter"}]}
    path = _settings(tmp_path, other)
    before = path.read_text(encoding="utf-8")
    assert ts.status(path) == "other"
    assert "没有动它" in ts.install(path)
    assert path.read_text(encoding="utf-8") == before


def test_jsonc_not_touched(tmp_path: Path) -> None:
    path = _settings(tmp_path, '{\n  // 我的注释\n  "actions": []\n}')
    before = path.read_text(encoding="utf-8")
    with pytest.raises(AgentError, match=r"手动|sendInput"):
        ts.install(path)
    assert path.read_text(encoding="utf-8") == before and ts.status(path) == "broken"


def test_terminal_setup_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _settings(tmp_path, NEW_FORMAT)
    monkeypatch.setattr(ts, "find_settings", lambda: [("Windows Terminal", path)])
    console = Console(record=True, width=160, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.banner()
    repl.handle_command("/terminal-setup")
    shown = console.export_text()
    assert "/terminal-setup" not in shown.split("✓")[0]  # 欢迎页不再提醒：Shift+Enter 默认就能换行
    assert "✓ Windows Terminal：已加上 Shift+Enter 换行" in shown and ts.status(path) == "ours"


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上打补丁")
def test_shift_enter_patch_really_applies() -> None:
    """回归：补丁曾因导入路径写错（prompt_toolkit.input.win32_types）静默失效，Shift+Enter 一直不能换行。"""
    from prompt_toolkit.input import win32

    from heng_agent.ui import winkeys

    assert winkeys.enable_shift_enter() is True
    assert win32._is_win_vt100_input_enabled() is False  # 改用读原始按键事件的读取器：Shift 信息不在半路丢掉

    class Ev:  # 一条「Shift 按着时按下 Enter」的原始按键事件
        KeyDown, VirtualKeyCode, ControlKeyState = 1, 0x0D, 0x0010

    keys = win32.ConsoleInputReader._event_to_key_presses(object(), Ev())  # type: ignore[arg-type]
    assert [k.key for k in keys] == ["c-j"]  # 交出 Ctrl+J：输入框里是换行
