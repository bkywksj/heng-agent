"""/terminal-setup：让 Windows Terminal 的 Shift+Enter 能换行。

为什么要改终端设置：prompt_toolkit 在 Windows 上开着 VT 输入模式，Windows Terminal 先把按键翻成字符再交给程序，
Shift+Enter 与 Enter 都变成同一个 \\r，Shift 的信息在半路就丢了，程序这边怎么补都分不出来。
所以照 Claude Code 的 /terminal-setup 的做法，让终端自己把 Shift+Enter 换成 \\n 发出来——heng-agent 的输入框里
\\n（即 Ctrl+J）就是换行。走的是字符，跟输入模式无关，最稳。

改之前先备份；已有我们的绑定就不重复加；已有别的 Shift+Enter 绑定就不动；设置文件带注释解析不了时什么都不改，
给出手动添加的片段。Windows Terminal 存好设置会自动重新加载，不用重开。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from heng_agent.core.errors import AgentError

ACTION_ID = "User.heng-agent.ShiftEnterNewline"
KEYS = "shift+enter"
COMMAND = {"action": "sendInput", "input": "\n"}
PACKAGES = (
    ("Windows Terminal", "Microsoft.WindowsTerminal_8wekyb3d8bbwe"),
    ("Windows Terminal Preview", "Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe"),
)
MANUAL = """在 Windows Terminal 设置（Ctrl+Shift+,）里加上：
  "actions": [ { "command": { "action": "sendInput", "input": "\\n" }, "id": "User.heng-agent.ShiftEnterNewline" } ],
  "keybindings": [ { "id": "User.heng-agent.ShiftEnterNewline", "keys": "shift+enter" } ]"""


def find_settings() -> list[tuple[str, Path]]:
    """本机装着的 Windows Terminal（正式版、预览版、非商店版）的设置文件。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    found = [(label, Path(local) / "Packages" / pkg / "LocalState" / "settings.json") for label, pkg in PACKAGES]
    found.append(("Windows Terminal（非商店版）", Path(local) / "Microsoft" / "Windows Terminal" / "settings.json"))
    return [(label, path) for label, path in found if path.is_file()]


def _bound_keys(data: dict[str, Any]) -> dict[str, Any]:
    """所有按键绑定：新格式在 keybindings（id 关联 actions），老格式把 keys 直接写在 actions 里。"""
    bound: dict[str, Any] = {}
    for item in [*(data.get("keybindings") or []), *(data.get("actions") or [])]:
        if isinstance(item, dict) and isinstance(item.get("keys"), str):
            bound[item["keys"].replace(" ", "").lower()] = item.get("id") or item.get("command")
    return bound


def status(path: Path) -> str:
    """ours：已配好；other：Shift+Enter 已绑了别的；none：没绑；broken：读不了。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return "broken"
    bound = _bound_keys(data).get(KEYS)
    if bound is None:
        return "none"
    return "ours" if bound in (ACTION_ID, COMMAND) else "other"


def install(path: Path) -> str:
    """把 Shift+Enter → 发送 \\n 加进这个设置文件，返回说明。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} 带注释或格式特殊，没有自动改（免得弄坏你的设置）。{MANUAL}") from exc
    current = status(path)
    if current == "ours":
        return "已经配好了"
    if current == "other":
        return "Shift+Enter 已经绑了别的功能，没有动它（想用 heng-agent 的换行，先在终端设置里去掉那个绑定）"
    backup = path.with_name(path.name + ".heng-backup")
    shutil.copy2(path, backup)
    actions = data.setdefault("actions", [])
    if "keybindings" in data or not any(isinstance(a, dict) and "keys" in a for a in actions):
        actions.append({"command": COMMAND, "id": ACTION_ID})  # 新格式（1.21 起）：动作与按键分开、用 id 关联
        data.setdefault("keybindings", []).append({"id": ACTION_ID, "keys": KEYS})
    else:
        actions.append({"command": COMMAND, "keys": KEYS})  # 老格式：按键直接写在动作里
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    return f"已加上 Shift+Enter 换行（原文件备份在 {backup.name}）"
