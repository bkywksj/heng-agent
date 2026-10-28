"""状态栏的外部数据：git 分支，以及用户自己的状态栏命令（兼容 Claude Code 的 statusLine）。

两样都要起子进程，不能在每次重画时跑（输入框每 0.1 秒就可能重画一次）：都在后台线程里跑、结果缓存，
跑完叫输入框重画。每次显示输入框前（一问答完）刷新一次，跟 Claude Code「对话有变化时更新」的节奏一致。

状态栏命令：settings 的 status_line（用户配置里写，项目配置不认——它会执行命令）。没写就用内置的第一行；
写 "claude" 表示用 Claude Code 的 ~/.claude/settings.json 里的 statusLine。命令从标准输入收到一段 JSON
（字段与 Claude Code 的一致：model、workspace、cwd、session_id、context_window……，另有 heng 段），
标准输出的前几行替换状态栏第一行，可以带 ANSI 颜色。这样给 Claude Code 写的状态栏脚本可以直接用。
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text

from heng_agent.core import logs
from heng_agent.core.compat import claude_home
from heng_agent.tools.shell import shell_argv

COMMAND_TIMEOUT = 5.0  # 状态栏命令最多跑这么久：卡住的脚本不能让状态栏一直不更新
MAX_LINES = 3  # 命令输出最多取前几行
CLAUDE = "claude"  # status_line 写这个：用 Claude Code 配的 statusLine


def git_state(workdir: Path) -> tuple[str, bool] | None:
    """(分支名, 有没有没提交的改动)；不是 git 仓库、没装 git 返回 None。一次 git status 拿两样。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "status", "--porcelain=v1", "--branch", "--untracked-files=no"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.startswith("## "):
        return None
    lines = out.stdout.splitlines()
    head = lines[0][3:]
    if head.startswith("No commits yet on "):
        branch = head.removeprefix("No commits yet on ")
    elif head.startswith("HEAD (no branch)"):
        branch = "HEAD"  # 游离的提交
    else:
        branch = head.split("...", 1)[0].split(" ", 1)[0]
    return branch, len(lines) > 1


def load_command(settings: Any) -> str | None:
    """要跑的状态栏命令；None = 用内置的第一行。

    以前没写就自动用 Claude Code 的 statusLine，结果打开 heng 看到的是给 Claude 写的那一行，不是 heng 自己的
    （用户反馈「应该跑我们设计的逻辑」）。现在要明确写 "claude" 才用它。
    """
    own = (getattr(settings, "status_line", None) or "").strip()
    if own.lower() != CLAUDE:
        return own or None
    if not getattr(settings, "claude_user_config", True):
        return None  # 关掉了读 Claude Code 的用户配置
    try:
        data = json.loads((claude_home() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get("statusLine") if isinstance(data, dict) else None
    return command_of(entry)


def command_of(entry: Any) -> str | None:
    """配置里的写法：字符串，或 Claude Code 的 {"type": "command", "command": "..."}。"""
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict) and entry.get("type", "command") == "command" and isinstance(entry.get("command"), str):
        return entry["command"].strip() or None
    return None


@dataclass
class Background:
    """在后台线程里算一个值、缓存起来；同一时刻只跑一个，跑完调 on_change（输入框重画）。"""

    on_change: Callable[[], None] = lambda: None
    value: Any = None
    _running: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def refresh(self, compute: Callable[[], Any]) -> None:
        with self._lock:
            if self._running:
                return  # 上一次还没跑完：不叠着起子进程
            self._running = True

        def work() -> None:
            try:
                new = compute()
            except Exception:  # 状态栏是锦上添花：出错只记日志，不能弄坏输入框
                logs.log.exception("状态栏数据刷新出错")
                new = self.value
            finally:
                with self._lock:
                    self._running = False
            if new != self.value:
                self.value = new
                self.on_change()

        threading.Thread(target=work, name="statusline", daemon=True).start()


def run_command(command: str, payload: dict[str, Any], cwd: Path) -> list[tuple[str, str]] | str:
    """跑状态栏命令：返回格式化后的前几行；出错返回一句说明（显示在第一行，免得用户不知道脚本坏了）。"""
    try:
        out = subprocess.run(
            shell_argv(command), input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT, cwd=str(cwd), check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return f"状态栏命令超过 {COMMAND_TIMEOUT:.0f} 秒没结束"
    except OSError as exc:
        return f"状态栏命令跑不起来：{exc}"
    lines = [line for line in out.stdout.splitlines() if line.strip()][:MAX_LINES]
    if not lines:
        reason = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else f"退出码 {out.returncode}"
        return f"状态栏命令没有输出（{reason[:80]}）"
    return list(to_formatted_text(ANSI("\n".join(" " + line for line in lines))))


def payload(*, session_id: str, transcript: str, workdir: Path, model: str, provider: str, version: str,
            mode: str, used: int, window: int, total_in: int, total_out: int,
            speed: float | None) -> dict[str, Any]:  # fmt: skip
    """给状态栏命令的输入：字段名照 Claude Code 的 statusLine 文档，给它写的脚本能直接读；heng 特有的放在 heng 段。"""
    return {
        "hook_event_name": "Status",
        "session_id": session_id,
        "transcript_path": transcript,
        "cwd": str(workdir),
        "model": {"id": model, "display_name": model},
        "workspace": {"current_dir": str(workdir), "project_dir": str(workdir)},
        "version": version,
        "output_style": {"name": "default"},
        "context_window": {
            "context_window_size": window,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            # 上一次请求的上下文占用；heng 记的是三项之和，放进 input_tokens（脚本一般四项相加）
            "current_usage": {
                "input_tokens": used,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "used_percentage": round(used / window * 100) if window else 0,
        },
        "heng": {"provider": provider, "permission_mode": mode, "output_speed": speed},
    }
