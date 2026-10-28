"""交互界面：输入框、流式渲染、权限确认对话、斜杠命令。"""

from __future__ import annotations

import contextlib
import datetime as _dt
import difflib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from heng_agent import worktree
from heng_agent.agent import Agent
from heng_agent.agents import create_agent, discover_agents
from heng_agent.attachments import ABS_PATH, PATH_STOP, absorb_paths, paste_clipboard, paste_to_mentions
from heng_agent.commands import Command, discover_commands, expand_command, shell_runner
from heng_agent.context import (
    MEMORY_WARN_TOKENS,
    append_memory,
    estimate_tokens,
    expand_mentions,
    load_memories,
    memory_files,
    mention_images,
)
from heng_agent.core.compat import PROJECT_MEMORY_NAMES, claude_home
from heng_agent.core.errors import AgentError
from heng_agent.core.types import AgentEvent, Notice, TextDelta, ThinkingDelta, ToolFinished, ToolStarted, TurnDone
from heng_agent.core.version import is_dev, version_label
from heng_agent.hooks import Hook
from heng_agent.hooks import trust as trust_hooks
from heng_agent.mcp import MCPManager, ServerConfig
from heng_agent.mcp import trust as mcp_trust
from heng_agent.model.client import list_models, make_client
from heng_agent.model.providers import load_profiles
from heng_agent.permissions import Rules
from heng_agent.session import SessionStore
from heng_agent.tools.background import describe
from heng_agent.tools.base import Tool
from heng_agent.tools.shell import clip, run_command, shell_argv
from heng_agent.tools.skill import skill_source
from heng_agent.tools.subagent import SubAgentTool
from heng_agent.tools.todo import render as render_todos
from heng_agent.ui import info, mcp_view, terminal_setup
from heng_agent.ui.config_cmd import ConfigCommand
from heng_agent.ui.doctor import run_checks
from heng_agent.ui.inputbox import InputBox
from heng_agent.ui.keywatch import EscWatcher
from heng_agent.ui.prompt import MODE_LABEL, MODES, STYLE, SlashCompleter, human_tokens, toolbar
from heng_agent.ui.select import Option, ask_line, choose
from heng_agent.ui.session_tools import (
    context_rows,
    copy_text,
    default_export_path,
    export_markdown,
    last_reply,
    session_diff,
)
from heng_agent.ui.sixel import encode as sixel_encode
from heng_agent.ui.sixel import supports_sixel
from heng_agent.updates import cached_newer_version, check_in_background

# 欢迎页的方块字标「HENG」，与 docs/brand/logo.svg 同属一套品牌；正式版青色、开发版品红，颜色在 banner() 里上
LOGO = ("█ █ █▀▀ █▄ █ █▀▀", "█▀█ ██▄ █ ▀█ █▄█")

HELP = """查看状态
  /status                     一屏看全当前状态：版本、模型、会话、目录、权限、MCP、说明文件、扩展（/version 同）
  /usage                      本会话用量：输入输出、缓存命中率、花费、上下文占用（/cost 同）
  /stats                      历史统计：今天 / 近 7 天 / 近 30 天的会话数、token、花费，按项目、按模型排名
  /context                    上下文被什么占着（系统提示词、工具、对话、工具结果……）
  /memory [edit project|user] 读进来的说明文件（优先级、各占多少）；输入以 # 开头的一句话快速记一条
  /mcp [服务] [工具]          MCP 服务 → 某服务的工具 → 某工具的参数；reconnect | disable | enable 服务
  /hooks                      生效的钩子（heng 的与 Claude Code 的）
  /skills · /agents · /tasks · /todo   技能 · 子智能体角色（new 名字 新建）· 后台任务（kill 任务号）· 任务清单
  /diff                       这次会话改过的文件（和第一次改之前比）
  /doctor                     检查配置、服务商连接、终端、git 等运行环境

会话
  /clear                      开始新会话          /resume [关键词]   选一个旧会话接着聊
  /rename 名字                给会话起名          /export [路径]     导出成 Markdown
  /compact [要求]             手动压缩历史（快撑满时会自动压缩）
  /undo                       撤销上一问：恢复它改过的文件，对话也退回去（bash 造成的改动除外）
  /rewind                     选一问，对话与代码一起回到它之前（空输入框里双击 ESC 同样）
  /copy                       把上一条回复复制到剪贴板
  /exit                       退出（/quit、Ctrl+D 同样）

模型与权限
  /provider [名字]            切换服务商；/model [名字] 切换模型；/config provider 引导配置服务商与 API
  /mode ask|accept-edits|plan|auto   每次问 / 改文件不问 / 只读 / 全部不问（Shift+Tab 循环切换）
  /think [on|off|预算|show]    思考模式：先深度思考再回答（难题用）
  /permissions                持久的允许 / 拒绝规则：allow bash(npm test)、deny、remove
  /config                     查看与修改配置

做事
  /init [要求]                分析项目，生成或更新项目说明（默认 AGENTS.md，已有说明就更新现有的）
  /commit [要求]              按仓库风格写提交信息并提交（不 push）
  /review [关注点]            审查没提交的改动（只评审不修改）
  /worktree [new|switch|remove 名字]  在独立的 git 工作树（独立分支）里改，不碰主分支
  /terminal-setup             备用：万一 Shift+Enter 不能换行，让 Windows Terminal 自己发换行符

输入技巧
  @路径 附上文件；拖入文件、Alt+V 粘贴图片或文件；!命令 自己直接跑命令；Shift+Enter（或 Ctrl+J）换行
  Ctrl+C / ESC 清空输入框，任务运行时按 ESC 中断；Ctrl+L 清屏；Ctrl+B 把跑着的命令转到后台
  空输入框里连按两次 Ctrl+C 退出"""

#: 别名：Claude Code 用户习惯的叫法也认
ALIASES = {"/cost": "/usage", "/todos": "/todo", "/version": "/status", "/quit": "/exit", "/bashes": "/tasks"}
RESULT_PREVIEW_LINES = 3  # 命令输出、出错等只留前几行：工具过程要紧凑
REPLY_MARK = "✦"  # AI 回复的标记（用户提问 ❯、工具调用 ●，三者一眼分得开）
MIN_THINKING_BUDGET = 1024  # Anthropic 要求的思考预算下限
MAX_THUMBNAILS = 3
#: 工具调用显示成好读的动词（MCP 工具见 tool_label）
TOOL_LABELS = {
    "read_file": "读取", "glob": "找文件", "grep": "搜索", "write_file": "写入", "edit_file": "修改",
    "bash": "运行", "todo": "任务清单", "web_fetch": "读网页", "skill": "用技能", "task": "派子任务",
    "bash_output": "看输出", "kill_task": "停任务", "tool_search": "找工具",
}  # fmt: skip


def tool_label(name: str) -> str:
    """工具显示名：内置工具用动词；MCP 工具 mcp__sigil__list_credentials 显示成 sigil · list_credentials。"""
    if name in TOOL_LABELS:
        return TOOL_LABELS[name]
    if name.startswith("mcp__") and name.count("__") >= 2:
        _, server, rest = name.split("__", 2)
        return f"{server} · {rest}"
    return name


def approval_body(tool: Tool, args: dict[str, Any]) -> str:
    """确认框里的文字（edit_file / write_file 之外）：先一句工具的用途，再这次的参数。

    不能只放参数摘要：没有参数的工具（如 MCP 的「列出全部」）摘要是空的，会画出一个空框，看着像多了个输入框。
    """
    purpose = next((ln.strip() for ln in tool.description.splitlines() if ln.strip()), "")[:200]
    if not args:
        params = "（无参数）"
    elif tool.kind == "mcp":  # MCP 参数常是嵌套结构，按 JSON 排版才看得清
        params = json.dumps(args, ensure_ascii=False, indent=2)[:3000]
    else:
        params = tool.summary(args) or json.dumps(args, ensure_ascii=False)[:3000]
    if tool.name in ("bash", "web_fetch"):  # 内置工具大家都知道用途，只看要跑什么
        return params
    return f"{purpose}\n\n{params}" if purpose else params


def brief_result(name: str, content: str, is_error: bool) -> list[str]:
    """工具结果的简短显示：按工具展示最有用的那部分。出错与命令输出保留前几行，其余给个数量。"""
    lines = content.splitlines() or [""]
    counted = [line for line in lines if line.strip()]

    def preview(limit: int = RESULT_PREVIEW_LINES) -> list[str]:
        shown = [line[:160] for line in lines[:limit]]
        return shown + ([f"…（共 {len(lines)} 行）"] if len(lines) > limit else [])

    if is_error:
        return preview()
    first = lines[0].strip()
    if name == "read_file":
        return [first] if first == "（空文件）" else [f"{len(counted)} 行"]
    if name in ("glob", "grep"):
        if first.startswith(("没有", "没找到", "未找到")):
            return [first]
        return [f"{len(counted)} 个" if name == "glob" else f"{len(counted)} 处匹配"]
    if name == "web_fetch":
        return [f"读到 {len(content)} 字"]
    if name == "todo":
        return preview(limit=20)  # 任务清单整个显示
    if name in ("write_file", "edit_file", "skill"):
        return [first[:160]]
    return preview()  # bash、task、MCP 工具：输出内容本身有用，留前几行


def edit_in_editor(text: str) -> str | None:
    """Ctrl+G：把输入框里的字放进临时文件，用 $EDITOR（没设就是记事本 / vi）打开；关掉后读回来。

    没改返回 None。编辑器需要「关掉才返回」：VS Code 要写成 `code --wait`。
    """
    import shlex
    import subprocess
    import tempfile

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = Path(fh.name)
    try:
        subprocess.run([*shlex.split(editor, posix=os.name != "nt"), str(path)], check=False)
        edited = path.read_text(encoding="utf-8-sig").rstrip("\n")  # 记事本可能带 BOM、末尾加换行
    except OSError:
        return None
    finally:
        path.unlink(missing_ok=True)
    return None if edited == text else edited


_PLAIN = object()  # _choose 的返回值：选择器用不了，调用方退回打字


def _stdin_is_terminal() -> bool:
    """标准输入是不是终端（脚本、管道、CI、测试里不是）：不是就用不了方向键选择器。"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):  # 被替换成别的对象、或已关闭
        return False


_FENCE = re.compile(r"^\s*(```|~~~)", re.MULTILINE)
_URL = re.compile(r"(?<![<(\[\w])(https?://[^\s<>()（）\[\]【】，。；：！？、\"'`]+)")


def split_done(text: str) -> tuple[str, str]:
    """流式回复里已经写完的部分与还在写的部分：在最后一个空行处切开，但不能切在代码块中间
    （前半部分的 ``` 必须成对）。没有可切的返回 ("", 原文)。"""
    idx = text.rfind("\n\n")
    while idx != -1:
        head = text[:idx]
        if len(_FENCE.findall(head)) % 2 == 0:
            return head, text[idx + 2 :]
        idx = text.rfind("\n\n", 0, idx)
    return "", text


def autolink(text: str) -> str:
    """把回复里的裸网址变成 Markdown 自动链接 <网址>，渲染出来是超链接（Ctrl+点击打开）。

    代码块与行内代码里的不动（那是代码的一部分）；已经是 [文字](网址) 或 <网址> 的不动；
    网址后面紧跟的中文标点不算进网址。
    """
    out, in_fence = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = line.split("`")
        for i in range(0, len(parts), 2):  # 偶数段在行内代码之外
            parts[i] = _URL.sub(_wrap_url, parts[i])
        out.append("`".join(parts))
    return "\n".join(out)


def _wrap_url(match: re.Match[str]) -> str:
    """网址包成 <网址>；末尾的英文句号、逗号等通常是句子的标点，留在外面。"""
    url = match.group(1)
    core = url.rstrip(".,;:!?")
    return f"<{core}>{url[len(core) :]}"


_MENTION_OR_PATH = re.compile(rf"@([^{PATH_STOP}]+)|({ABS_PATH})")  # 与 attachments 共用路径规则


def file_link(path_text: str, workdir: Path) -> str:
    """rich 标记：指向真实文件就渲染成超链接（OSC 8），否则原样（转义后）。

    智码终端会读链接背后的本地路径、悬停弹出图片预览；Windows Terminal 里 Ctrl+点击用默认程序打开。
    不支持超链接的终端 rich 会自动只显示文字。
    """
    raw = Path(path_text)
    target = raw if raw.is_absolute() else workdir / raw
    try:
        exists = target.is_file()
    except OSError:
        exists = False
    if not exists:
        return escape(path_text)
    return f"[link={target.resolve().as_uri()}][underline]{escape(path_text)}[/underline][/link]"


def linkify(text: str, workdir: Path) -> str:
    """一行文字里的 @引用 与绝对路径（指向真实文件的）渲染成超链接，其余转义。"""
    out, last = [], 0
    for m in _MENTION_OR_PATH.finditer(text):
        out.append(escape(text[last : m.start()]))
        if m.group(1):  # @引用：@ 留在链接外
            out.append("@" + file_link(m.group(1), workdir))
        else:
            out.append(file_link(m.group(2), workdir))
        last = m.end()
    out.append(escape(text[last:]))
    return "".join(out)


class Repl:
    #: 内置命令：同名的自定义命令不生效
    BUILTIN = frozenset({"help", "clear", "mode", "model", "provider", "compact", "undo", "todo", "cost", "skills",
                         "mcp", "config", "rewind", "resume", "rename", "permissions", "copy", "export",
                         "context", "diff", "memory", "doctor", "tasks", "think", "agents", "worktree", "exit",
                         "quit", "terminal-setup", "usage", "status", "stats", "hooks", "todos", "version",
                         "bashes"})  # fmt: skip

    #: _choose 的返回值「选择器用不了、请退回打字」：给 ConfigCommand 等别的模块判断用
    PLAIN = _PLAIN

    def __init__(
        self,
        agent: Agent,
        console: Console | None = None,
        untrusted_hooks: list[Hook] | None = None,
        untrusted_mcp: list[ServerConfig] | None = None,
        provider_name: str = "",
        mcp_status: list[tuple[str, bool, str]] | None = None,
        onboarding: bool = False,
    ) -> None:
        self.agent = agent
        self.console = console or Console()
        self.untrusted_hooks = untrusted_hooks or []
        self.untrusted_mcp = untrusted_mcp or []
        self.provider_name = provider_name
        self.onboarding = onboarding  # 第一次运行（还没有用户配置）：欢迎页之后跑新手引导
        self.mcp_status = list(mcp_status or [])  # 用户级 MCP 服务的连接情况（cli 启动），项目级的信任后追加
        self.mcp_managers: list[MCPManager] = []  # 用户级（cli 启动）与信任后启动的项目 MCP 服务；重连、退出时关掉
        self.mcp_disabled: set[str] = set()  # 本会话用 /mcp disable 停用的服务
        self._mcp_synced: set[int] = set()  # 已把工具接进智能体的后台 MCP 管理器（id），见 _sync_mcp
        self.commands = discover_commands(
            agent.settings.home, agent.settings.workdir, claude_user=agent.settings.claude_user_config
        )
        agent.settings.home.mkdir(parents=True, exist_ok=True)
        self._box: InputBox | None = None  # 带边框的输入框，第一次输入时创建
        self._watcher: EscWatcher | None = None  # 任务运行期间监听 ESC
        self._esc_pressed = False
        self._prefill = ""  # 下次输入框预填的文字（/rewind 后把那一问放回来）
        # 确认框里选了「以后都允许」：把规则写进用户配置
        agent.permissions.on_persist = self._persist_rule
        self._plain_input = False  # 输入框用不了时退回普通输入
        self._status: Any = None  # 等待动画（rich Status）
        self._event_lock = threading.RLock()
        self._live: Live | None = None  # 正在流式渲染的一段 Markdown
        self._reply_started = False  # 这条回复已经固定打印过至少一段（后面的段落不再带 ✦）
        self._thought = ""  # 正在进行的这段思考
        self._think_started = 0.0
        self.last_thought = ""  # 最近一次完整的思考（/think show）
        self._after_text = False  # 刚结束一段 AI 回复（下一个工具行要先换行）
        self._buf = ""
        self._pending_shell: list[str] = []  # 用户 !命令 的输出，附在下一问里
        self._hook_retry()

    def _hook_retry(self) -> None:
        """模型客户端重试时在界面上说一声（切换服务商后要重新挂）。"""
        if hasattr(self.agent.client, "on_retry"):
            self.agent.client.on_retry = lambda msg: self.on_event(Notice(msg))  # type: ignore[attr-defined]

    def _commit(self, text: str) -> None:
        """把写完的一段正式打印到屏幕上（先擦掉实时区域）。同一条回复只有第一段带 ●，后面的段落之间空一行。"""
        if self._live is not None:
            self._live.stop()
            self._live = None
        if not text.strip():
            return
        if self._reply_started:
            self.console.print()
        self.console.print(self._reply(text, first=not self._reply_started))
        self._reply_started = True

    def _end_text(self) -> None:
        """结束这条回复：剩下没固定的那段也正式打印。"""
        if self._live is not None or self._buf:
            self._commit(self._buf)
            self._buf = ""
        if self._reply_started:
            self._reply_started = False
            self._after_text = True  # 接下来若是工具调用，先换行：否则会接在回复最后一行后面

    @staticmethod
    def _reply(text: str, first: bool = True) -> Table:
        """AI 的一段回复：左边紫色 ✦（只有第一段），右边 Markdown。两栏排版，换行后文字仍然对齐。

        标记和别处区分开：用户提问是 ❯、工具调用是灰色 ●，AI 回复用 ✦（像 Kimi 的 ✨，但 ✨ 是 emoji、
        在终端里占两格，不同终端宽度不一，第二行会对不齐；✦ 只占一格）。
        裸网址转成链接（Windows Terminal 里 Ctrl+点击就能打开）。
        """
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(f"[bold magenta]{REPLY_MARK}[/]" if first else "", Markdown(autolink(text)))
        return grid

    def _model_label(self) -> str:
        p = getattr(self.agent.client, "profile", None)
        if p is None:
            return "-"
        return f"{self.provider_name} · {p.model}" if self.provider_name else p.model

    def _toolbar(self) -> Any:
        a = self.agent
        return toolbar(a.permissions.mode, self._model_label(), str(a.settings.workdir), a.last_input_tokens,
                       a.context_window, len(a.ctx.tasks.running()), thinking=a.thinking,
                       compact_ratio=a.settings.compact_ratio, dev=is_dev())  # fmt: skip

    def _set_mode(self, mode: str) -> None:
        self.agent.permissions.mode = mode  # type: ignore[assignment]

    def _input(self, message: str) -> str:
        """优先用带边框的输入框（多行、补全、状态栏、Shift+Tab）；它在没有原生 Windows 控制台的终端里建不起来
        （如 Git Bash 默认的 mintty、被重定向的输入），这时退回普通的逐行输入，状态栏改成输入前打印一行。"""
        if not self._plain_input:
            try:
                if self._box is None:
                    self._box = InputBox(
                        history=FileHistory(str(self.agent.settings.home / "history")),
                        commands=SlashCompleter(
                            # 有 argument-hint 的命令把参数写法一起显示：“[issue 编号] 修复指定的 issue”
                            lambda: {n: f"{c.hint} {c.description}".strip() for n, c in self.commands.items()},
                            self._arg_hints,
                        ),
                        workdir=self.agent.settings.workdir,
                        toolbar=self._toolbar,
                        get_mode=lambda: self.agent.permissions.mode,
                        set_mode=self._set_mode,
                        modes=MODES,
                        style=STYLE,
                        on_paste=lambda text: paste_to_mentions(text, self.agent.settings.workdir),
                        on_clipboard=lambda: paste_clipboard(self.agent.settings.workdir),
                        on_ctrl_v=lambda: paste_clipboard(self.agent.settings.workdir, text_fallback=True),
                        on_editor=edit_in_editor,
                        todos=lambda: render_todos(self.agent.ctx.todos) if self.agent.ctx.todos else "",
                    )
                prefill, self._prefill = self._prefill, ""
                return self._box.prompt(default=prefill)
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception:  # prompt_toolkit 在不同终端下抛的异常类型不一，统一退回
                self._plain_input = True
        return self.console.input(message)

    def _ask(self, message: str, *, password: bool = False) -> str:
        """一行小问题（网址、名字、密钥、y/n）。ESC 或 Ctrl+C 取消：抛 KeyboardInterrupt，由命令外层说「已取消」。

        以前用最基础的 console.input：按 Ctrl+C 的中断没人接，引导流程做到一半想取消，整个程序带着堆栈退出了。
        """
        with self._pause_watcher():
            if not self._plain_input:
                try:
                    return ask_line(message, password=password)
                except (KeyboardInterrupt, EOFError):
                    raise
                except Exception:  # prompt_toolkit 在不同终端下抛的异常类型不一：退回普通输入
                    self._plain_input = True
            return self.console.input(message, password=password)

    def _arg_hints(self, cmd: str, prior: list[str]) -> list[tuple[str, str, bool]]:
        """命令参数的补全候选：(值, 说明, 选它之后还要不要再输)。"""
        a = self.agent
        if cmd == "worktree":
            if not prior:
                return [("new", "新建并切过去：new 名字", True), ("switch", "切到某个工作树或 main", True),
                        ("remove", "删一个工作树", True)]  # fmt: skip
            if prior[0] in ("switch", "remove") and len(prior) == 1:
                try:
                    trees = worktree.list_worktrees(worktree.repo_root(a.settings.workdir))
                except AgentError:
                    return []
                names = [(w.path.name, w.branch, False) for w in trees if not w.main]
                return ([("main", "主目录", False)] if prior[0] == "switch" else []) + names
            return []
        if cmd == "agents" and not prior:
            return [("new", "建一个角色：new 名字", True)]
        if cmd == "think" and not prior:
            return [("on", "打开思考模式", False), ("off", "关闭", False), ("show", "看最近一次思考", False),
                    ("16000", "把思考预算设为 16000 token 并打开", False)]  # fmt: skip
        if cmd == "mcp":
            return mcp_view.arg_hints(self, prior)
        if cmd == "config" and not prior:
            return [("provider", "引导式添加或修改服务商", False), ("edit", "用编辑器打开配置文件", False),
                    ("set", "改字段：set 键 值", True), ("get", "看字段：get 键", True),
                    ("unset", "删字段：unset 键", True)]  # fmt: skip
        if cmd == "mode" and not prior:
            return [(m, MODE_LABEL[m], False) for m in MODES]
        if cmd == "provider" and not prior:
            try:
                profiles, _ = load_profiles(a.settings.home, a.settings.workdir)
            except AgentError:
                return []
            return [(name, f"{p.model} · {p.base_url}", False) for name, p in profiles.items()]
        if cmd == "memory":
            if not prior:
                return [("edit", "用编辑器改：edit project / edit user", True)]
            if prior == ["edit"]:
                return [("project", "项目的 HENG.md", False), ("user", "用户的 HENG.md（所有项目共用）", False)]
        if cmd == "tasks":
            if not prior:
                return [("kill", "停止一个后台任务", True)]
            if prior == ["kill"]:
                return [(t.id, t.command[:60], False) for t in a.ctx.tasks.running()]
        if cmd == "permissions":
            if not prior:
                return [("allow", "加允许规则", True), ("deny", "加拒绝规则", True), ("remove", "删一条规则", True)]
            if prior == ["remove"]:
                rules = a.permissions.rules
                return [(r, "允许", False) for r in rules.allow] + [(r, "拒绝", False) for r in rules.deny]
        return []

    # ---------- 等待动画 ----------

    def _spin(self, text: str, hint: str = "ESC 中断") -> None:
        """发出请求、跑工具时显示旋转动画：否则屏幕没动静，分不清是卡住还是在干活。"""
        if self._live is not None:
            return  # 正在流式输出文字时不叠加动画（rich 同一时间只能有一个实时区域）
        message = f"[cyan]{escape(text)}[/] [dim]{escape(hint)}[/]"
        if self._status is None:
            self._status = self.console.status(message, spinner="dots")
            self._status.start()
        else:
            self._status.update(message)

    def _unspin(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    # ---------- 输出 ----------

    def on_event(self, event: AgentEvent) -> None:
        with self._event_lock:  # 并行的子智能体会从多个线程同时报告事件
            self._on_event(event)

    def _on_event(self, event: AgentEvent) -> None:
        c = self.console
        if isinstance(event, ThinkingDelta):
            # 思考过程不刷屏：只在等待动画里显示想了多少字，结束后折叠成一行（/think show 看全文）
            if not self._thought:
                self._think_started = time.monotonic()
            self._thought += event.text
            self._spin(f"思考中… 已想 {len(self._thought)} 字")
            return
        if self._thought:
            self._flush_thought()
        if isinstance(event, TextDelta):
            self._unspin()
            # 流式 Markdown，分段固定：写完的段落永久打印、不再改，实时区域只留最后没写完的那一段。
            # 不能整段回复一直重绘：Live 只能改写屏幕可见范围，回复长过一屏后上面滚出去的部分改不了，
            # 只好在下面再打一遍——实测「…统计信息如下：」重复了十几次
            self._buf += event.text
            done, rest = split_done(self._buf)
            if done:
                self._commit(done)
                self._buf = rest
            if self._buf:
                if self._live is None:
                    # transient：停止时擦掉，由 _commit 把完整内容正式打印；crop：超长的一段只显示放得下的部分，不重复
                    self._live = Live(console=c, refresh_per_second=10, transient=True, vertical_overflow="crop")
                    self._live.start()
                self._live.update(self._reply(self._buf, first=not self._reply_started))
            return
        self._end_text()
        self._unspin()
        if isinstance(event, Notice):
            c.print(f"[yellow]⋯ {escape(event.text)}[/]")
            self._spin("处理中…")
        elif isinstance(event, ToolStarted):
            tool = self.agent.tools.get(event.name)
            summary = tool.summary(event.input) if tool else str(event.input)
            label = tool_label(event.name)
            # 工具调用过程做成「注释」的样子：不加粗、偏暗、只占一行（超长截断），不抢回复的视线。
            # 连续几个工具之间不空行；只有紧跟在 AI 的文字后面时先换行（回复结束时光标留在最后一行末尾）
            if self._after_text:
                c.print()
                self._after_text = False
            line = Text.assemble(("● ", "grey50"), (label, "dark_cyan"), (f" {summary}" if summary else "", "grey50"))
            line.truncate(max(20, c.width - 2), overflow="ellipsis")
            c.print(line, no_wrap=True)
            foreground_bash = event.name == "bash" and not event.input.get("run_in_background")
            self._spin(f"{label}…", "ESC 中断 · Ctrl+B 转后台" if foreground_bash else "ESC 中断")
        elif isinstance(event, ToolFinished):
            if event.denied:
                c.print(Text("  ⎿ 已拒绝", style="yellow"))
            else:
                lines = brief_result(event.name, event.result.content, event.result.is_error)
                style = "red" if event.result.is_error else "grey42"
                for i, text in enumerate(lines):
                    row = Text(("  ⎿ " if i == 0 else "    ") + text, style=style)
                    row.truncate(max(20, c.width - 2), overflow="ellipsis")  # 每条只占一行，不折行
                    c.print(row, no_wrap=True)
            self._spin("思考中…")  # 工具结果要交回给模型，接下来又是等模型
        elif isinstance(event, TurnDone):
            self._after_text = False  # 这一问结束了：下一问的第一个工具前不必再换行
            u = event.usage
            tokens = f"输入 {u.get('input_tokens', 0)} / 输出 {u.get('output_tokens', 0)} token"
            c.print(f"\n[dim]（{event.rounds} 轮，{tokens}）[/]")

    # ---------- 确认 ----------

    def approve(self, tool: Tool, args: dict[str, Any]) -> Any:
        """确认框；允许后马上把等待动画重新打开。

        问之前会关掉动画（免得和选择器纠缠），以前允许后没再打开：工具可能要跑好几秒（MCP 调外部接口），
        这段时间屏幕没有任何动静，看着像卡住了。
        """
        answer = self._ask_approval(tool, args)
        if answer != "deny":
            foreground_bash = tool.name == "bash" and not args.get("run_in_background")
            self._spin(f"{tool_label(tool.name)}…", "ESC 中断 · Ctrl+B 转后台" if foreground_bash else "ESC 中断")
        return answer

    def _ask_approval(self, tool: Tool, args: dict[str, Any]) -> Any:
        self._unspin()  # 要问用户了：先停动画，否则会和输入提示纠缠在一起
        c = self.console
        if tool.name == "edit_file":
            diff = difflib.unified_diff(
                str(args.get("old_string", "")).splitlines(), str(args.get("new_string", "")).splitlines(),
                fromfile=str(args.get("path")), tofile=str(args.get("path")), lineterm="",
            )  # fmt: skip
            body: Any = Syntax("\n".join(diff), "diff", theme="ansi_dark", word_wrap=True)
        elif tool.name == "write_file":
            content = str(args.get("content", ""))
            body = Syntax(content[:3000] + ("\n…" if len(content) > 3000 else ""), "text", word_wrap=True)
        else:
            body = escape(approval_body(tool, args))
        c.print(Panel(body, title=f"[bold]要执行 {escape(tool_label(tool.name))}[/]", border_style="yellow"))
        with self._pause_watcher():  # 问用户期间暂停 ESC 监听，免得它抢走按键
            picked = self._choose("允许吗？", [
                Option("once", "这次允许", "y"),
                Option("always", f"本会话一直允许 {tool.name}", "a"),
                Option("persist", f"以后都允许 {Rules.suggest(tool, args)[:60]}", "p", "写进用户配置"),
                Option("deny", "拒绝", "n", "ESC 也是拒绝"),
            ])  # fmt: skip
            if picked is not _PLAIN:
                answer = picked or "deny"  # ESC 取消 = 拒绝
                shown = dict(
                    once="这次允许", always="本会话一直允许", persist="以后都允许（已写进配置）", deny="已拒绝"
                )
                c.print(f"[dim]→ {shown[str(answer)]}[/]")
                return answer
            while True:  # 选择器用不了的终端：退回打字
                answer = (
                    c.input("允许吗？[bold]y[/] 这次 / [bold]a[/] 本会话一直允许 / [bold]n[/] 拒绝 > ").strip().lower()
                )
                if answer in ("y", "yes", "是", ""):
                    return "once"
                if answer in ("a", "always"):
                    return "always"
                if answer in ("n", "no", "否"):
                    return "deny"

    def _persist_rule(self, rule: str) -> None:
        """写配置失败（比如配置文件坏了）只提示：不能让错误冒到工具执行里把任务打断，这次的允许照样有效。"""
        try:
            ConfigCommand(self).add_rule("allow", rule)
        except AgentError as exc:
            self.console.print(f"[yellow]规则没能写进配置（这次仍然允许）：{escape(str(exc))}[/]")

    def _choose(self, title: str, options: list[Option], default: int = 0) -> str | object | None:
        """方向键选择器；用不了（没有原生控制台的终端、测试）返回 _PLAIN，由调用方退回打字。

        标准输入不是终端（脚本、管道、CI）时直接退回打字：不然选择器读到「输入结束」会当成用户按 ESC 取消、
        抛出中断——CI 上第一次在 Linux / macOS 跑，配置向导就这样被当成取消，整轮测试被中断（Windows 上
        碰巧因为没有原生控制台、走了另一条路才没暴露）。
        """
        if self._plain_input or not _stdin_is_terminal():
            return _PLAIN
        try:
            return choose(title, options, default=default)
        except (KeyboardInterrupt, EOFError):
            return None
        except Exception:  # prompt_toolkit 在不同终端下抛的异常类型不一
            self._plain_input = True
            return _PLAIN

    @contextlib.contextmanager
    def _cancellable(self) -> Iterator[None]:
        """斜杠命令、# 记忆里的交互：任何一步按 ESC / Ctrl+C 都只是取消、回到输入框，什么也不保存。"""
        try:
            yield
        except (KeyboardInterrupt, EOFError):
            self.console.print("\n[dim]已取消[/]")

    @contextlib.contextmanager
    def _pause_watcher(self) -> Iterator[None]:
        if self._watcher is None:
            yield
            return
        with self._watcher.paused():
            yield

    def _on_escape(self) -> None:
        self._esc_pressed = True
        self.agent.cancel()

    def pick_model(self, name: str = "") -> None:
        """/model：不带名字就向服务商要模型列表、方向键选；带名字直接切。当前会话立即生效，再问要不要存成默认。"""
        a, c = self.agent, self.console
        profile = getattr(a.client, "profile", None)
        if profile is None:
            c.print("[yellow]当前模型客户端不支持切换模型名[/]")
            return
        if not name:
            c.print("[dim]正在向服务商取可用模型……[/]")
            try:
                models = list_models(profile)
            except AgentError as exc:
                c.print(f"[yellow]{escape(str(exc))}[/]，可以直接写：/model 模型名")
                return
            if not models:
                c.print("[yellow]服务商没返回模型列表[/]，可以直接写：/model 模型名")
                return
            current = models.index(profile.model) if profile.model in models else 0
            options = [Option(m, m, detail="当前" if m == profile.model else "") for m in models]
            picked = self._choose(f"选择模型（{self.provider_name or '当前服务商'}）", options, default=current)
            if picked is _PLAIN:
                for i, m in enumerate(models, 1):
                    c.print(f"  {i:>2} {escape(m)}")
                raw = self._ask("选哪个（编号或名字，回车取消）> ").strip()
                picked = models[int(raw) - 1] if raw.isdigit() and 1 <= int(raw) <= len(models) else (raw or None)
            if not picked:
                c.print("[dim]已取消[/]")
                return
            name = str(picked)
        a.client.profile = profile.model_copy(update={"model": name})  # type: ignore[attr-defined]
        c.print(f"模型已切换为 [bold]{escape(name)}[/]（当前会话）")
        if not self.provider_name or name == profile.model:
            return
        keep = self._choose(f"也存成 {self.provider_name} 的默认模型吗？", [
            Option("no", "只在这次会话里用", "n"), Option("yes", "存成默认（以后启动也用它）", "y"),
        ])  # fmt: skip
        if keep is _PLAIN:
            keep = "yes" if self._ask("也存成默认模型吗？[y/N] > ").strip().lower() in ("y", "yes", "是") else "no"
        if keep == "yes":
            try:
                ConfigCommand(self).save_model(self.provider_name, name)
                c.print(f"[green]✓ 已存成 {escape(self.provider_name)} 的默认模型[/]")
            except AgentError as exc:
                c.print(f"[red]{escape(str(exc))}[/]")

    def pick_session(self, keyword: str = "") -> None:
        """/resume [关键词]：列出这个目录的会话（按名字 / 第一句话筛选），选一个接着聊。"""
        a, c = self.agent, self.console
        current = a.session.id if a.session else ""
        sessions = [s for s in SessionStore.all(a.settings.home, a.settings.workdir)
                    if not keyword or keyword.lower() in f"{s.title} {s.first_question}".lower()]  # fmt: skip
        if not sessions:
            c.print(f"没有{'对得上「' + escape(keyword) + '」的' if keyword else ''}会话。")
            return

        def label(s: Any) -> str:
            name = s.title or (s.first_question.strip().splitlines() or ["（没有提问）"])[0]
            return name[:50]

        def detail(s: Any) -> str:
            when = _dt.datetime.fromtimestamp(s.updated).strftime("%m-%d %H:%M")
            return f"{when} · {s.messages} 条{' · 当前' if s.id == current else ''}"

        options = [Option(s.id, label(s), detail=detail(s)) for s in sessions]
        picked = self._choose("接着哪个会话聊？", options)
        if picked is _PLAIN:
            for i, s in enumerate(sessions, 1):
                c.print(f"  {i:>2} {escape(label(s))}  [dim]{detail(s)}[/]")
            raw = self._ask("选哪个（编号，回车取消）> ").strip()
            picked = sessions[int(raw) - 1].id if raw.isdigit() and 1 <= int(raw) <= len(sessions) else None
        if not picked:
            c.print("[dim]已取消[/]")
            return
        if picked == current:
            c.print("就是当前这个会话。")
            return
        count = a.resume(SessionStore(a.settings.home, a.settings.workdir, str(picked)))
        c.print(f"已接上会话 {escape(str(picked))}（{count} 条消息）。它之前的提问不能 /undo 或 /rewind。")

    def show_context(self) -> None:
        """/context：上下文被什么占着。各部分按字符数从模型报的真实总数里分。"""
        a, c = self.agent, self.console
        specs = a.tool_specs()  # 实际发给模型的声明：MCP 按需加载时没加载的不算
        rows = context_rows(a.system_prompt(), specs, a.messages, a.last_input_tokens)
        total, window = sum(n for _, n in rows), a.context_window
        basis = "按最近一次请求的真实用量" if a.last_input_tokens else "还没发过请求，按字数粗估"
        c.print(
            f"[bold]上下文[/] {human_tokens(total)} / {human_tokens(window)}（{total / window * 100:.0f}%，{basis}）"
        )
        for name, n in rows:
            width = round(n / window * 40) if window else 0
            bar = "█" * width + "[dim]" + "·" * (40 - width) + "[/]"
            c.print(f"  {name:<10}{bar} {human_tokens(n):>6}")
        c.print(f"[dim]到 {a.settings.compact_ratio:.0%} 会自动压缩；/compact [要求] 手动压缩[/]")

    def notify_if_long(self, seconds: float) -> None:
        """任务跑得久（超过 notify_after 秒）就响铃。

        Windows Terminal 收到响铃会闪任务栏、标签页显示铃铛，切到别的窗口也看得到。
        """
        limit = self.agent.settings.notify_after
        if limit and seconds >= limit and self.console.is_terminal:
            self.console.file.write("\a")
            self.console.file.flush()

    def quick_memory(self, text: str) -> None:
        """`# 这个项目用 pnpm`：选记到项目还是用户的 HENG.md，写完重建系统提示词，下一问就生效。"""
        files = memory_files(self.agent.settings)
        picked = self._choose("记到哪里？", [
            Option("project", "项目记忆", "p", f"{files['project']}（跟着仓库走）"),
            Option("user", "用户记忆", "u", f"{files['user']}（所有项目共用）"),
        ])  # fmt: skip
        if picked is _PLAIN:
            raw = self._ask("记到哪里？p 项目 / u 用户（回车取消）> ").strip().lower()
            picked = {"p": "project", "u": "user"}.get(raw)
        if picked not in files:
            self.console.print("[dim]已取消[/]")
            return
        try:
            append_memory(files[str(picked)], text)
        except (AgentError, OSError) as exc:
            self.console.print(f"[red]{escape(str(exc))}[/]")
            return
        self.agent.reload_system()
        self.console.print(f"[green]✓ 已记入 {file_link(str(files[str(picked)]), self.agent.settings.workdir)}[/]")

    def memory_command(self, rest: str) -> None:
        """/memory：看两份记忆；/memory edit project|user：用编辑器改，改完重建系统提示词。"""
        c, files = self.console, memory_files(self.agent.settings)
        sub, _, which = rest.partition(" ")
        which = which.strip() or "project"
        if sub == "edit":
            if which not in files:
                c.print("用法：/memory edit project 或 /memory edit user")
                return
            path = files[which]
            old = path.read_text(encoding="utf-8-sig") if path.is_file() else "# 记忆\n"
            new = edit_in_editor(old)
            if new is None:
                c.print("[dim]没有改动[/]")
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
            self.agent.reload_system()
            c.print(f"[green]✓ 已更新 {file_link(str(path), self.agent.settings.workdir)}，下一问生效[/]")
            return
        self.memory_overview()

    def memory_overview(self) -> None:
        """/memory：读进来的全部说明文件——按优先级从低到高（冲突时以后面的为准）、各自多大、# 快速记忆写到哪里。
        几份说明并存时用户看得清每份占多少、谁说了算；合计太大时提醒（每次对话都带着）。"""
        c, a = self.console, self.agent
        loaded = load_memories(a.settings)
        files = memory_files(a.settings)
        if not loaded:
            c.print("[dim]还没有说明文件。输入 /init 让 AI 熟悉项目、生成 AGENTS.md；或用 # 开头的一句话快速记一条[/]")
        else:
            c.print("[bold]常驻说明[/]  [dim]按优先级从低到高，内容冲突时以后面的为准[/]")
            total = 0
            for i, (path, block) in enumerate(loaded, 1):
                tokens = estimate_tokens(block)
                total += tokens
                c.print(f"  {i}. {file_link(str(path), a.settings.workdir)}  [dim]约 {human_tokens(tokens)} token[/]")
            share = f"，占上下文 {total / a.context_window:.0%}" if a.context_window else ""
            c.print(f"  [dim]合计约 {human_tokens(total)} token{share}，每次对话都带着[/]")
            if total > MEMORY_WARN_TOKENS:
                c.print(f"[yellow]说明文件偏大（超过 {human_tokens(MEMORY_WARN_TOKENS)} token）：可以精简，"
                        "或把重复的合到一份（如 CLAUDE.md 里只写一行 @AGENTS.md）；"
                        "不想带 ~/.claude/CLAUDE.md 就 /config set settings.claude_user_config false[/]")  # fmt: skip
        project, user = files["project"], files["user"]
        note = "" if project.is_file() else "（还没有，第一次记的时候新建）"
        c.print(f"[bold]写到哪里[/]  # 快速记忆与 /memory edit project → {file_link(str(project), a.settings.workdir)}"
                f"[dim]{note}[/]；/memory edit user → {escape(str(user))}")  # fmt: skip
        c.print("[dim]子目录里的 HENG.md / AGENTS.md / CLAUDE.md 在读到那个目录的文件时自动附上，比根目录的更具体[/]")

    def rewind(self) -> None:
        """/rewind 或空框双击 ESC：选一问，对话与代码一起回到它之前；那一问的原话放回输入框便于改了重发。"""
        a, c = self.agent, self.console
        titles = a.turn_titles()
        if not titles:
            c.print("这次启动后还没有可以回退的提问（接上的旧会话里的提问不在其内）。")
            return
        options = [Option(str(i), f"第 {i + 1} 问：{t[:60]}", detail="最近" if i == len(titles) - 1 else "")
                   for i, t in enumerate(titles)]  # fmt: skip
        picked = self._choose("回退到哪一问之前？（对话与它改过的文件一起退回）", options, default=len(titles) - 1)
        if picked is _PLAIN:
            raw = self._ask(f"回退到第几问之前（1-{len(titles)}，回车取消）> ").strip()
            picked = str(int(raw) - 1) if raw.isdigit() and 1 <= int(raw) <= len(titles) else None
        if picked is None:
            c.print("[dim]已取消[/]")
            return
        index = int(str(picked))
        question = titles[index]
        full = a.messages[a.turns[index]["start"]]["content"][0].get("text", question)
        restored = a.rewind(index)
        files = "、".join(restored) if restored else "无"
        c.print(f"已回退到第 {index + 1} 问之前；恢复的文件：{escape(files)}。那一问放回了输入框，可以改了再发。")
        c.print("[dim]bash 命令造成的改动（删文件、git 操作、构建产物）无法撤销。[/]")
        self._prefill = full

    # ---------- 主循环 ----------

    def banner(self) -> None:
        a, s = self.agent, self.agent.settings
        session = a.session.id if a.session else "-"
        p = getattr(a.client, "profile", None)
        model = f"{p.model}  [dim]{escape(p.base_url)}（{p.protocol}）[/]" if p else "-"
        mcp_ok = [st for st in self.mcp_status if st[1]]
        mcp_line = f"{len(mcp_ok)} 个已连接" if self.mcp_status else "未配置"
        if mcp_view.connecting(self):
            mcp_line = f"后台连接中（{len(mcp_view.connecting(self))} 个）"
        mode = a.permissions.mode
        mode_text = "[bold black on yellow] auto 全部不问 [/]" if mode == "auto" else mode
        dev = is_dev()
        version = escape(version_label()) + (f"  [bold white on magenta] 开发版 [/]  [dim]数据 {escape(str(s.home))}[/]"
                                             if dev else "")  # fmt: skip
        rows = [
            ("目录", escape(str(s.workdir))),
            ("会话", session),
            ("模型", (f"[bold]{escape(self.provider_name)}[/] · " if self.provider_name else "") + model),
            ("版本", version),
            ("权限", mode_text),
            ("MCP", mcp_line),
            ("扩展", f"技能 {len(a.skills)} 个 · 自定义命令 {len(self.commands)} 个"),
        ]
        # 开发版一眼分得出：品红边框 + 徽标 + 窗口标题（同时开着正式版与开发版两个窗口时不会认错）
        badge = "  [bold white on magenta] 开发版 [/]" if dev else ""
        color = "magenta" if dev else "cyan"
        with contextlib.suppress(Exception):
            self.console.set_window_title("heng-agent [开发版]" if dev else "heng-agent")
        body = (
            f"[bold {color}]{LOGO[0]}[/]   [bold]欢迎使用 heng-agent！[/]{badge}\n"
            f"[bold {color}]{LOGO[1]}[/]   [dim]输入 /help 查看命令[/]\n\n"
        )
        body += "\n".join(f"[bold]{k:<4}[/] {v}" for k, v in rows)
        self.console.print(Panel(body, border_style=color, padding=(1, 2)))
        self.console.print(
            "[cyan]✦[/] [dim]@文件 附上内容 · !命令 直接执行 · / 补全命令 · Shift+Tab 切换权限模式 · Ctrl+C 中断[/]"
        )
        for line in mcp_view.startup_line(self):  # 都连上了只一行汇总；连不上的才逐个红字列出
            self.console.print(line)
        if self.agent.settings.update_check and not is_dev():
            # 只读上一次查到的结果（不等网络），再在后台查下一次：有新版本下次启动就能提示
            newer = cached_newer_version(self.agent.settings.home)
            if newer:
                tip = f"有新版本 {escape(newer)}（当前 {escape(version_label())}）：退出后运行 heng-agent upgrade"
                self.console.print(f"[yellow]{tip}[/]")
            check_in_background(self.agent.settings.home)
        compat = self.compat_items()
        if compat:
            self.console.print(f"[dim]兼容加载：{escape('、'.join(compat))}[/]")
        if not any((self.agent.settings.workdir / name).is_file() for name in PROJECT_MEMORY_NAMES):
            # 用户不知道有 /init 就不会用：项目还没有说明时提一句（有了就不再提）
            self.console.print(
                "[dim]首次在这个项目里用？输入 /init 让 AI 先熟悉项目（生成 AGENTS.md，以后每次对话都会读）[/]"
            )

    def compat_items(self) -> list[str]:
        """从别的编码工具兼容加载了哪些配置（欢迎页与 /doctor 显示）：读了什么要让用户看得见。"""
        a = self.agent
        home, work = claude_home().resolve(), a.settings.workdir.resolve()
        items: list[str] = []
        for path, _ in load_memories(a.settings):
            if path.name == "HENG.md":
                continue  # heng-agent 自己的不算兼容
            in_claude_home = path.absolute().parent in (home, claude_home().absolute())
            items.append("~/.claude/CLAUDE.md" if in_claude_home else path.name)

        def from_claude(path: Path) -> bool:
            # 解析前后都看：技能目录常是链接（junction / 符号链接），resolve 之后就不在 ~/.claude 下面了
            roots = {home, claude_home().absolute(), work / ".claude", (a.settings.workdir / ".claude").absolute()}
            return any(root in p.parents for p in (path.absolute(), path.resolve()) for root in roots)

        skills = sum(1 for s in a.skills.values() if from_claude(s.path))
        agents = sum(1 for r in a.agent_defs.values() if from_claude(r.path))
        commands = sum(1 for c in self.commands.values() if c.path.is_absolute() and from_claude(c.path))
        rules = 0
        settings_files = [work / ".claude" / "settings.json", work / ".claude" / "settings.local.json"]
        if a.settings.claude_user_config:
            settings_files.insert(0, claude_home() / "settings.json")
        for path in settings_files:
            try:
                section = json.loads(path.read_text(encoding="utf-8-sig")).get("permissions") or {}
                rules += sum(len(section.get(k) or []) for k in ("allow", "deny"))
            except (OSError, ValueError, AttributeError, TypeError):
                continue
        if skills:
            items.append(f"Claude 技能 {skills} 个")
        if agents:
            items.append(f"Claude 子智能体 {agents} 个")
        if commands:
            items.append(f"Claude 命令 {commands} 个")
        if rules:
            items.append(f"Claude 权限规则 {rules} 条")
        hooks = sum(1 for h in a.hooks.hooks if h.style == "claude")
        if hooks:
            items.append(f"Claude 钩子 {hooks} 个")
        if (work / ".mcp.json").is_file():
            items.append(".mcp.json")
        return items

    def print_mcp_status(self, entries: list[tuple[str, bool, str]] | None = None) -> None:
        """每个 MCP 服务一行：MCP server "名字" connected · N tools (stdio / http)；失败的红色写原因。"""
        for name, ok, detail in entries if entries is not None else self.mcp_status:
            if ok:
                self.console.print(f'[green]MCP server "{escape(name)}" connected · {escape(detail)}[/]')
            else:
                self.console.print(f'[red]MCP server "{escape(name)}" failed · {escape(detail)}[/]')

    def handle_command(self, text: str) -> bool:
        """处理斜杠命令；返回 False 表示退出。"""
        cmd, _, rest = text.partition(" ")
        rest = rest.strip()
        a, c = self.agent, self.console
        if cmd in ("/exit", "/quit"):
            return False
        cmd = ALIASES.get(cmd, cmd)
        if cmd == "/help":
            for line in HELP.splitlines():  # 不缩进的行是分组标题，加粗
                c.print(f"[bold]{escape(line)}[/]" if line and not line.startswith(" ") else escape(line))
            custom = [n for n in self.commands if n not in self.BUILTIN]
            if custom:
                c.print("自定义命令：")
                for n in custom:
                    c.print(f"  /{escape(n):<26} {escape(self.commands[n].description)}")
            if a.skills:
                c.print(f"技能（模型按需读取）：{escape('、'.join(a.skills))}；/skills 看说明")
        elif cmd == "/mcp":
            self.mcp_command(rest)
        elif cmd == "/skills":
            # 按来源分组：内置的谁都有；用户、项目的可以改写同名的内置技能（放一个同名的 SKILL.md 即可）
            groups: dict[str, list[Any]] = {}
            for s in a.skills.values():
                groups.setdefault(skill_source(s, a.settings.home, a.settings.workdir), []).append(s)
            order = ["项目", "Claude 项目", "用户", "Claude 用户", "内置", "其他"]
            for label in sorted(groups, key=lambda x: order.index(x) if x in order else len(order)):
                c.print(f"[bold]{label}[/]")
                for s in groups[label]:
                    summary = s.description.strip().splitlines()[0] if s.description.strip() else ""
                    c.print(f"  [cyan]{escape(s.name):<18}[/] {escape(summary)}")
            c.print("[dim]自己的技能放在 .heng-agent/skills/<名字>/SKILL.md（项目）或 ~/.heng-agent/skills/（用户）；"
                    "和内置技能同名就会替换它[/]")  # fmt: skip
        elif cmd == "/terminal-setup":
            self.terminal_setup()
        elif cmd == "/worktree":
            self.worktree_command(rest)
        elif cmd == "/agents":
            self.agents_command(rest)
        elif cmd == "/think":
            self.think_command(rest)
        elif cmd == "/tasks":
            self.tasks_command(rest)
        elif cmd == "/permissions":
            ConfigCommand(self).permissions(rest)
        elif cmd == "/resume":
            self.pick_session(rest)
        elif cmd == "/rename" and rest:
            if a.session is None:
                c.print("[yellow]这个会话不保存，没法命名[/]")
            else:
                a.session.title = rest
                a._save()  # 经智能体存：连按模型分的用量一起存，别把文件里已有的冲掉
                c.print(f"会话已命名：{escape(rest)}（/resume {escape(rest)} 就能找到它）")
        elif cmd == "/clear":
            a.messages.clear()
            a.turns.clear()
            a.originals.clear()
            a.ctx.todos.clear()
            # 用量也清零：新会话的文件只记新会话的用量，否则 /stats 把所有会话加起来时上一个会话会被重复算
            a.usage = dict.fromkeys(a.usage, 0)
            a.usage_by_model = {}
            a.last_input_tokens = 0
            a.session = SessionStore(a.settings.home, a.settings.workdir)
            c.print(f"已开始新会话 {a.session.id}")
        elif cmd == "/mode":
            mode = rest
            if mode not in MODES:  # 不带参数（或写错了）：方向键选
                current = MODES.index(a.permissions.mode) if a.permissions.mode in MODES else 0
                picked = self._choose("切换权限模式", [Option(m, MODE_LABEL[m]) for m in MODES], default=current)
                if picked is _PLAIN:
                    picked = self._ask(f"权限模式（{' / '.join(MODES)}）> ").strip()
                if picked not in MODES:
                    c.print("[dim]已取消[/]")
                    return True
                mode = str(picked)
            a.permissions.mode = mode  # type: ignore[assignment]
            c.print(f"权限模式：{MODE_LABEL[mode]}")
        elif cmd == "/model":
            self.pick_model(rest)
        elif cmd == "/provider":
            profiles, default = load_profiles(a.settings.home, a.settings.workdir)
            if rest in profiles:
                a.client = make_client(profiles[rest], a.settings.max_tokens)
                self.provider_name = rest
                self._hook_retry()
                p = profiles[rest]
                c.print(f"已切换到 {rest}：{escape(p.model)} @ {escape(p.base_url)}（对话历史保留）")
            else:
                c.print(f"可选的服务商：{', '.join(profiles)}（配置文件默认 {default}）。用法：/provider deepseek")
        elif cmd == "/config":
            ConfigCommand(self).run(rest)
        elif cmd == "/compact":
            a.compact(self.on_event, force=True, instruction=rest)
        elif cmd == "/copy":
            reply = last_reply(a.messages)
            if not reply:
                c.print("还没有可以复制的回复。")
            else:
                ok, note = copy_text(reply)
                c.print(f"[{'green' if ok else 'red'}]{escape(note)}[/]")
        elif cmd == "/export":
            session_id = a.session.id if a.session else ""
            path = Path(rest).expanduser() if rest else default_export_path(a.settings.workdir, session_id)
            if not path.is_absolute():
                path = a.settings.workdir / path
            title = a.session.title if a.session and a.session.title else f"heng-agent 会话 {session_id}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(export_markdown(a.messages, title), encoding="utf-8")
            c.print(f"已导出：{file_link(str(path), a.settings.workdir)}")
        elif cmd == "/context":
            self.show_context()
        elif cmd == "/diff":
            diffs = session_diff(a.originals, a.settings.workdir)
            if not diffs:
                c.print("这次会话还没有改过文件（bash 命令造成的改动不在其内，可以 !git diff 看）。")
            for shown, diff in diffs:
                c.print(Panel(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True), title=escape(shown),
                              border_style="cyan"))  # fmt: skip
        elif cmd == "/memory":
            self.memory_command(rest)
        elif cmd == "/doctor":
            icons = {"ok": "[green]✓[/]", "warn": "[yellow]![/]", "fail": "[red]✗[/]"}
            c.print("[dim]正在检查（会连一次服务商）……[/]")
            for status, name, note in run_checks(self):
                c.print(f"  {icons[status]} [bold]{name:<6}[/] {escape(note)}")
        elif cmd == "/rewind":
            self.rewind()
        elif cmd == "/undo":
            restored = a.undo()
            if restored is None:
                c.print("没有可以撤销的。")
            else:
                files = "、".join(restored) if restored else "无（这一问没有用 write_file / edit_file 改文件）"
                c.print(f"已撤销上一问：恢复的文件 {escape(files)}；对话历史也退回到这一问之前。")
                c.print("[dim]注意：bash 命令造成的改动（删文件、git 操作、构建产物）无法撤销。[/]")
        elif cmd == "/todo":
            c.print(escape(render_todos(a.ctx.todos)) if a.ctx.todos else "还没有任务清单。")
        elif cmd == "/usage":
            info.show_usage(self)
        elif cmd == "/status":
            info.show_status(self)
        elif cmd == "/stats":
            info.show_stats(self)
        elif cmd == "/hooks":
            info.show_hooks(self)
        else:
            c.print(f"[yellow]不认识的命令或缺少参数：{escape(text)}[/]，/help 看用法")
        return True

    def read(self) -> str:
        self.console.print()
        if self._plain_input:  # 退回普通输入的终端：没有边框与底部状态栏，用分隔线 + 一行状态代替
            a = self.agent
            pct = a.last_input_tokens / a.context_window * 100 if a.context_window else 0
            self.console.rule(style="grey35")
            self.console.print(f"[dim]{a.permissions.mode} · {escape(self._model_label())} · 上下文 {pct:.0f}%[/]")
        line = self._input("> ")
        while self._plain_input and line.endswith("\\"):  # 普通输入里用行尾 \ 续行；输入框里直接 Ctrl+J
            line = line[:-1] + "\n" + self._input("… ")
        return line.strip()

    def echo_user(self, text: str) -> None:
        """输入框提交后已被擦掉：把这句话用醒目的标记打印到对话记录里，和模型回复区分开。

        里面指向真实文件的 @引用 与绝对路径渲染成超链接（见 linkify）。"""
        lines = text.splitlines() or [""]
        workdir = self.agent.settings.workdir
        self.console.print(f"[bold cyan]❯[/] [bold]{linkify(lines[0], workdir)}[/]")
        for line in lines[1:]:
            self.console.print(f"  [bold]{linkify(line, workdir)}[/]")
        self.console.print()  # 和下面的「思考中」、回复拉开一点，不挤

    def show_thumbnails(self, pictures: list[str]) -> None:
        """附上的图片在终端里画缩略图（Sixel），最多 3 张。只在支持的终端里画：不支持的会刷一屏乱码。"""
        if not pictures or not self.console.is_terminal or not supports_sixel(self.agent.settings.image_preview):
            return
        for rel in pictures[:MAX_THUMBNAILS]:
            seq = sixel_encode(self.agent.settings.workdir / rel)
            if seq:
                self.console.file.write(seq + "\n")
        self.console.file.flush()

    def _command_shell(self, command: Command) -> Callable[[str], str] | None:
        """自定义命令里的 ``!`命令` ``：用户级的（自己写的）直接跑；项目里的跟着仓库来，先把要跑的命令给用户看、
        问一句（auto 模式除外）。不跑就返回 None，命令照样展开、原处注明「没有运行」。"""
        a = self.agent
        if command.project and a.permissions.mode != "auto":
            listing = "\n".join(f"  $ {s}" for s in command.shell_snippets)
            self.console.print(
                f"[yellow]/{escape(command.name)} 是项目里的命令，要先在本机运行：[/]\n{escape(listing)}"
            )
            picked = self._choose("运行吗？", [Option("yes", "运行", "y"), Option("no", "不运行（照样发送）", "n")])
            if picked is _PLAIN:
                picked = "yes" if self._ask("运行吗？[y/N] ").strip().lower() in ("y", "yes") else "no"
            if picked != "yes":
                return None
        return shell_runner(a.settings.workdir, a.settings.bash_timeout)

    def shell(self, command: str) -> None:
        """!命令：用户自己在工作目录跑命令，不经过模型、不需要确认；输出会附在下一问里告诉模型。"""
        if not command:
            return
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            code, output = run_command(
                shell_argv(command), self.agent.settings.workdir, self.agent.settings.bash_timeout, env
            )
        except KeyboardInterrupt:
            self.console.print("[yellow]已中断[/]")
            return
        output = clip(output, 5000).rstrip()
        self.console.print(escape(output) if output else "[dim]（无输出）[/]")
        status = "超时" if code is None else f"退出码 {code}"
        self.console.print(f"[dim]{status}；输出会附在你下一句话里告诉模型[/]")
        self._pending_shell.append(f"（我在终端执行了 `{command}`，{status}，输出：\n```\n{output}\n```）")

    def ask_trust_mcp(self) -> None:
        """项目里配置的 MCP 服务等于「启动一个程序」：完整列出启动命令，用户确认后才启动。"""
        servers = self.untrusted_mcp
        lines = "\n".join(
            f"{s.name}：{s.url}（http）" if s.url else f"{s.name}：{s.command} {' '.join(s.args)}".rstrip()
            for s in servers
        )
        self.console.print(
            Panel(escape(lines), title="[bold]这个项目配置了 MCP 服务（会启动这些程序）[/]", border_style="red")
        )
        answer = self._ask("信任并启动这些 MCP 服务吗？不认识就选 n [y/n] > ").strip().lower()
        self.untrusted_mcp = []
        if answer not in ("y", "yes", "是"):
            self.console.print("本次不启动项目 MCP 服务。")
            return
        mcp_trust(self.agent.settings.home, self.agent.settings.workdir, servers)
        manager = MCPManager(servers, self.agent.settings.workdir)
        self.mcp_managers.append(manager)
        for tool in manager.tools:
            self.agent.tools[tool.name] = tool
        self.mcp_status += manager.status
        self.print_mcp_status(manager.status)  # 失败的服务在这里以红色显示原因
        self.console.print(
            f"已启动 {len(manager.clients)} 个 MCP 服务，接入 {len(manager.tools)} 个工具。配置一改会再问你。"
        )

    def close(self) -> None:
        for manager in self.mcp_managers:
            manager.close()
        stopped = self.agent.ctx.tasks.kill_all()  # 不留后台任务在那里偷偷跑
        if stopped:
            self.console.print(f"[dim]已停止 {stopped} 个后台任务[/]")

    def mcp_command(self, rest: str) -> None:
        """/mcp 服务总览（方向键选服务 → 看工具 / 重连 / 停用）；/mcp 服务、/mcp 服务 工具 看工具与详情；
        /mcp reconnect|disable|enable 服务。"""
        parts = rest.split()
        if parts and parts[0] in mcp_view.ACTIONS:
            if len(parts) < 2:
                self.console.print(f"用法：/mcp {parts[0]} 服务名")
            else:
                self.mcp_action(parts[0], parts[1])
            return
        if len(parts) >= 2:
            mcp_view.show_tool(self, parts[0], parts[1])
            return
        if parts:
            mcp_view.show_server(self, parts[0])
            return
        infos = mcp_view.servers(self)
        if not infos:
            self.console.print("没有接入 MCP 服务。在 config.json 的 mcp_servers 里配置（见 README）。")
            return
        hint = "[dim]/mcp 服务名 看工具；/mcp reconnect | disable | enable 服务名[/]"
        if self._plain_input:  # 选择器用不了：纯文字列一遍
            mcp_view.show_summary(self)
            self.console.print(hint)
            return
        # 服务只列一次：状态、连接方式、工具数直接写在选择器的每一行里（以前先打一遍总览、再弹一遍选择器）
        self.console.print(mcp_view.headline(self, infos))
        width = max(len(s.name) for s in infos)
        options = [Option(s.name, mcp_view.option_label(s, width), detail=mcp_view.option_detail(s)) for s in infos]
        picked = self._choose("选一个服务：看工具、重连或停用（ESC 退出）", options)
        if picked is _PLAIN:
            mcp_view.show_summary(self)
            self.console.print(hint)
            return
        if not picked:
            return
        info = next(s for s in infos if s.name == picked)
        if info.ok:
            actions = [Option("tools", "看工具", "t"), Option("reconnect", "重新连接", "r", "服务崩了或刚更新过时用"),
                       Option("disable", "本会话停用", "d", "断开并移除它的工具")]  # fmt: skip
        elif info.disabled:
            actions = [Option("enable", "重新启用", "e")]
        else:
            actions = [Option("reconnect", "重新连接", "r"), Option("tools", "看失败原因", "t")]
        action = self._choose(f"{picked}：做什么？", actions)
        if action == "tools":
            mcp_view.show_server(self, str(picked))
        elif action in mcp_view.ACTIONS:
            self.mcp_action(str(action), str(picked))

    def mcp_action(self, action: str, name: str) -> None:
        """重连 / 停用 / 启用一个 MCP 服务，并把智能体的工具表同步过来（模型马上看得到新工具、看不到停用的）。"""
        c, a = self.console, self.agent
        manager = next((m for m in self.mcp_managers if m.has(name)), None)
        if manager is None:
            c.print(f"没有叫 {escape(name)} 的 MCP 服务。")
            return
        if name in mcp_view.connecting(self):  # 跳过了等待、后台还在连：这时重连会和后台连接撞在一起
            c.print(f"{escape(name)} 还在后台连接，连好后再试。")
            return
        if action == "disable":
            for tool_name in manager.disconnect(name):
                a.tools.pop(tool_name, None)
            self.mcp_disabled.add(name)
            self._set_mcp_status(name, False, "已停用（本会话）")
            c.print(f"[dim]○ 已停用 {escape(name)}（本会话；/mcp enable {escape(name)} 重新启用）[/]")
            return
        c.print(f"[dim]正在连接 {escape(name)}……[/]")
        removed, tools = manager.reconnect(name)
        for tool_name in removed:
            a.tools.pop(tool_name, None)
        for tool in tools:
            a.tools[tool.name] = tool
        self.mcp_disabled.discard(name)
        entry = next(e for e in manager.status if e[0] == name)
        self._set_mcp_status(*entry)
        self.print_mcp_status([entry])

    def _sync_mcp(self) -> None:
        """后台连接的 MCP 服务：连完后把工具接进智能体、打印连接结果。只在主线程、用户提交输入时调。

        不在后台线程里直接改 agent.tools：主线程每轮都在遍历它，同时改会出错。还没连完就等（模型这一轮可能要用），
        等的时候 Ctrl+C 跳过：这一句先不带这些工具，下次输入时再接。
        """
        # 按「哪些后台管理器还没接入」判断，不能看状态里有没有「连接中」：服务连得比建界面还快时，
        # 界面拿到的已经是连好的状态，看状态会以为不用接，工具就永远进不了智能体（测试里实际撞到过）
        managers = [m for m in self.mcp_managers if m.background and id(m) not in self._mcp_synced]
        if not managers:
            return
        shown_connecting = bool(mcp_view.connecting(self))  # 欢迎页显示的是「连接中」，连完要补打印结果
        pending = [name for m in managers for name in m.pending()]
        if pending:
            self._spin(f"等待 MCP 服务连接（{len(pending)} 个）…", "Ctrl+C 跳过")
            try:
                for manager in managers:
                    while not manager.wait(0.2):  # 分段等：Windows 上一直阻塞在 wait() 里收不到 Ctrl+C
                        pass
            except KeyboardInterrupt:
                self._unspin()
                self.console.print("[dim]已跳过等待：这一句先不带 MCP 工具，连好后下次输入时接入。[/]")
                return
            self._unspin()
        for manager in managers:
            for tool in manager.tools:
                self.agent.tools[tool.name] = tool
            for entry in manager.status:
                self._set_mcp_status(*entry)
            self._mcp_synced.add(id(manager))
        if shown_connecting:  # 欢迎页已经显示过结果的就不重复打印
            for line in mcp_view.startup_line(self):
                self.console.print(line)

    def _set_mcp_status(self, name: str, ok: bool, detail: str) -> None:
        for i, (old, _, _) in enumerate(self.mcp_status):
            if old == name:
                self.mcp_status[i] = (name, ok, detail)
                return
        self.mcp_status.append((name, ok, detail))

    def _flush_thought(self) -> None:
        """一段思考结束：折叠成一行灰字，全文留给 /think show。"""
        self._unspin()
        seconds = time.monotonic() - self._think_started
        self.last_thought = self._thought
        self.console.print(f"[grey50]✻ 思考了 {seconds:.1f} 秒（约 {len(self._thought)} 字）· /think show 查看[/]")
        self._thought = ""

    def terminal_setup(self) -> None:
        """/terminal-setup：给本机的 Windows Terminal 加上 Shift+Enter 换行（见 terminal_setup 模块的说明）。"""
        c = self.console
        found = terminal_setup.find_settings()
        if not found:
            c.print("没找到 Windows Terminal 的设置文件。其他终端里用 Ctrl+J 或 Alt+Enter 换行。")
            return
        for label, path in found:
            try:
                note = terminal_setup.install(path)
                c.print(f"[green]✓ {escape(label)}：{escape(note)}[/]")
            except AgentError as exc:
                c.print(f"[yellow]{escape(label)}：{escape(str(exc))}[/]")
        c.print("[dim]Windows Terminal 会自动重新加载设置，现在就能用 Shift+Enter 换行。"
                "在别的程序里 Shift+Enter 会发送换行符（多数程序里等同回车）。[/]")  # fmt: skip

    def worktree_command(self, rest: str) -> None:
        """/worktree 列出；/worktree new 名字 新建并切过去；/worktree switch 名字|main；/worktree remove 名字。"""
        a, c = self.agent, self.console
        try:
            root = worktree.repo_root(a.settings.workdir)
        except AgentError as exc:
            c.print(f"[yellow]这里不是 git 仓库，用不了工作树：{escape(str(exc))}[/]")
            return
        sub, _, name = rest.partition(" ")
        name = name.strip()
        try:
            if sub == "new" and name:
                created = worktree.create(root, name)
                self.switch_workdir(created.path, f"工作树 {name}（分支 {created.branch}）")
                c.print(f"[dim]改完用 /commit 提交，再 /worktree switch main 回主目录 git merge {created.branch}[/]")
            elif sub == "switch" and name:
                target = root if name == "main" else worktree.path_of(root, name)
                if not any(w.path == target.resolve() for w in worktree.list_worktrees(root)):
                    c.print(f"没有叫 {escape(name)} 的工作树。/worktree new {escape(name)} 新建一个")
                    return
                self.switch_workdir(target, "主目录" if name == "main" else f"工作树 {name}")
            elif sub == "remove" and name:
                if worktree.path_of(root, name) == a.settings.workdir.resolve():
                    c.print("[yellow]正在这个工作树里，先 /worktree switch main 再删[/]")
                    return
                c.print(escape(worktree.remove(root, name)))
            elif not rest:
                current = a.settings.workdir.resolve()
                for w in worktree.list_worktrees(root):
                    mark = "[green]●[/]" if w.path == current else " "
                    label = "主目录" if w.main else w.path.name
                    c.print(f" {mark} [bold]{escape(label)}[/]  [cyan]{escape(w.branch or '（分离 HEAD）')}[/]  "
                            f"[dim]{escape(str(w.path))}[/]")  # fmt: skip
                c.print("[dim]/worktree new 名字 新建并切过去；/worktree switch 名字|main；/worktree remove 名字[/]")
            else:
                c.print("用法：/worktree、/worktree new 名字、/worktree switch 名字|main、/worktree remove 名字")
        except AgentError as exc:
            c.print(f"[red]{escape(str(exc))}[/]")

    def switch_workdir(self, path: Path, label: str) -> None:
        """会话切到另一个工作目录：智能体的读写边界、系统提示词跟着换；那里的自定义命令、角色重新读。"""
        self.agent.set_workdir(path)
        self.commands = discover_commands(
            self.agent.settings.home, path, claude_user=self.agent.settings.claude_user_config
        )
        self.reload_agents()
        self.console.print(f"[green]✓ 已切到{escape(label)}：{escape(str(path))}[/]")

    def agents_command(self, rest: str) -> None:
        """/agents 列出自定义子智能体角色；/agents new 名字 建一个模板并用编辑器打开，存好就能用。"""
        a, c = self.agent, self.console
        sub, _, name = rest.partition(" ")
        if sub == "new":
            name = name.strip()
            if not re.fullmatch(r"[\w\-]+", name):
                c.print("用法：/agents new 名字（字母、数字、中文、_ -）")
                return
            path = create_agent(a.settings.workdir, name)
            original = path.read_text(encoding="utf-8")
            edited = edit_in_editor(original)
            if edited is not None:
                path.write_text(edited.rstrip("\n") + "\n", encoding="utf-8")
            self.reload_agents()
            note = "[dim]模型现在就能派它了[/]"
            c.print(f"[green]✓ 角色 {escape(name)}：{file_link(str(path), a.settings.workdir)}[/]  {note}")
            return
        self.reload_agents()  # 手动改过文件也能马上生效
        if not a.agent_defs:
            c.print("没有自定义角色。/agents new 名字 建一个（如 测试员、审查员），"
                    "或放在 .heng-agent/agents/名字.md（项目）、~/.heng-agent/agents/（用户）。")  # fmt: skip
            return
        hint = "模型用 task 工具时按用途挑；只读的可以并行，带改文件 / 跑命令工具的会向你确认"
        c.print(f"[bold]子智能体角色[/]  [dim]{hint}[/]")
        for role in a.agent_defs.values():
            kind = "[green]只读[/]" if role.read_only else "[yellow]会改动[/]"
            c.print(f"  [bold cyan]{escape(role.name)}[/] {kind}  {escape(role.description)}")
            c.print(f"    [dim]工具：{escape(', '.join(role.tools))} · {escape(str(role.path))}[/]")
            if role.unknown_tools:
                c.print(f"    [yellow]不认识或不允许的工具（已忽略）：{escape(', '.join(role.unknown_tools))}[/]")

    def reload_agents(self) -> None:
        """重新读角色文件，并把 task 工具换成带新角色列表的（工具说明里列着角色，模型靠它挑）。"""
        a = self.agent
        a.agent_defs = discover_agents(a.settings.home, a.settings.workdir, claude_user=a.settings.claude_user_config)
        if "task" in a.tools:
            a.tools["task"] = SubAgentTool(a._spawn_subagent, a._add_usage, a.agent_defs)

    def think_command(self, rest: str) -> None:
        """/think 切换思考模式；/think on|off；/think 16000 设预算并打开；/think show 看最近一次思考。"""
        a, c = self.agent, self.console
        arg = rest.strip().lower()
        if arg == "show":
            if self.last_thought:
                c.print(Panel(escape(self.last_thought), title="最近一次思考", border_style="grey50"))
            else:
                c.print("这次会话还没有思考过。")
            return
        if arg.isdigit():
            if int(arg) < MIN_THINKING_BUDGET:
                c.print(f"[yellow]思考预算至少 {MIN_THINKING_BUDGET}（Anthropic 的要求）[/]")
                return
            a.thinking_budget, a.thinking = int(arg), True
        elif arg in ("on", "off"):
            a.thinking = arg == "on"
        elif not arg:
            a.thinking = not a.thinking
        else:
            c.print("用法：/think（切换）、/think on|off、/think 16000（设预算）、/think show")
            return
        state = f"已打开（预算 {a.thinking_budget} token）" if a.thinking else "已关闭"
        c.print(f"✻ 思考模式{state}")
        profile = getattr(a.client, "profile", None)
        if a.thinking and profile is not None and profile.protocol == "openai":
            c.print("[dim]这个服务商走 OpenAI 协议：思不思考由模型本身决定（如 deepseek-reasoner 总会思考），"
                    "/think 对它不起作用；模型输出的思考过程照样会显示[/]")  # fmt: skip

    def tasks_command(self, rest: str) -> None:
        """/tasks：列出后台任务；/tasks kill 任务号：停掉。"""
        c, manager = self.console, self.agent.ctx.tasks
        sub, _, task_id = rest.partition(" ")
        if sub == "kill" and task_id.strip():
            task = manager.kill(task_id)
            c.print(f"已停止 {escape(task.id)}：{escape(task.command)}" if task else f"没有后台任务 {escape(task_id)}")
            return
        if not manager.tasks:
            c.print("没有后台任务。让模型用 bash 的 run_in_background 启动，或在命令跑着时按 Ctrl+B 转到后台。")
            return
        for task in manager.tasks.values():
            color = "green" if task.code is None else "dim"
            c.print(f"[{color}]{escape(describe(task, limit=100))}[/]")
        c.print("[dim]/tasks kill 任务号 停止；退出 heng-agent 时会停掉全部后台任务[/]")

    def ask_trust(self) -> None:
        """项目钩子可能来自别人的仓库：完整列出要执行的命令，用户确认后才运行。"""
        hooks = self.untrusted_hooks
        lines = "\n".join(f"[{h.event}] 匹配 {h.match}：{h.command}" for h in hooks)
        self.console.print(
            Panel(escape(lines), title="[bold]这个项目配置了钩子（会自动执行命令）[/]", border_style="red")
        )
        answer = self._ask("信任并启用这些钩子吗？不认识就选 n [y/n] > ").strip().lower()
        if answer in ("y", "yes", "是"):
            trust_hooks(self.agent.settings.home, self.agent.settings.workdir, hooks)
            self.agent.hooks.hooks = [*self.agent.hooks.hooks, *hooks]
            self.console.print("已启用。钩子内容一改会再问你。")
        else:
            self.console.print("本次不启用项目钩子。")
        self.untrusted_hooks = []

    def loop(self) -> int:
        self.banner()
        if self.onboarding:
            from heng_agent.ui import onboarding

            onboarding.run(self)
        if self.untrusted_hooks:
            self.ask_trust()
        if self.untrusted_mcp:
            self.ask_trust_mcp()
        while True:
            try:
                text = self.read()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not text:
                continue
            if not self._plain_input:
                self.echo_user(text)  # 输入框已被擦掉：把这句话留在对话记录里
            self._sync_mcp()  # 后台连的 MCP 服务：处理这句输入之前接进来（还没连完就等）
            if text.startswith("#") and not text.startswith("#!"):
                with self._cancellable():
                    self.quick_memory(text)  # # 开头：记进 HENG.md，不发给模型
                continue
            if text.startswith("/"):
                name = text[1:].split(" ", 1)[0]
                command = None if name in self.BUILTIN else self.commands.get(name)
                runner = self._command_shell(command) if command and command.shell_snippets else None
                expanded = None if command is None else expand_command(text, self.commands, runner)
                if expanded is None:
                    keep = True
                    with self._cancellable():  # 命令里任何一步按 ESC / Ctrl+C：只是取消，程序不退出
                        keep = self.handle_command(text)
                    if not keep:
                        return 0
                    continue
                self.console.print(f"[dim]展开自定义命令 /{escape(name)}[/]")
                text = expanded
            if text.startswith("!"):
                self.shell(text[1:].strip())
                continue
            # 发送前把消息里真实存在的文件绝对路径换成 @：智码终端里 Ctrl+V 截图、拖入文件都是写路径，
            # 且不依赖粘贴标记（老版 conhost 会吞掉它们）
            text, absorbed = absorb_paths(text, self.agent.settings.workdir)
            text, attached = expand_mentions(text, self.agent.settings.workdir)
            images, pictures = mention_images(text, self.agent.settings.workdir)
            shown = list(dict.fromkeys(absorbed + attached + pictures))
            if shown:
                links = "、".join(file_link(rel, self.agent.settings.workdir) for rel in shown)
                hint = "（悬停预览需在智码终端里；Ctrl+点击打开）"
                self.console.print(f"[dim]已附上：[/]{links} [dim]{hint}[/]")
            self.show_thumbnails(pictures)
            if self._pending_shell:
                text = "\n".join(self._pending_shell) + "\n\n" + text
                self._pending_shell.clear()
            try:
                self._spin("思考中…")
                self._esc_pressed = False
                self._watcher = EscWatcher(self._on_escape, self.agent.ctx.detach.set)  # ESC 中断；Ctrl+B 转后台
                self._watcher.start()
                started = time.monotonic()
                self.agent.run(text, self.on_event, self.approve, images=images)
                self.notify_if_long(time.monotonic() - started)
            except KeyboardInterrupt:
                self._end_text()
                self._unspin()
                how = "按了 ESC" if self._esc_pressed else "按了 Ctrl+C"
                self.console.print(f"\n[yellow]已中断（{how}；正在跑的命令连同子进程已终止）。可以接着说下一步。[/]")
            except AgentError as exc:
                self._end_text()
                self._unspin()
                self.console.print(f"\n[red]{escape(str(exc))}[/]")
            finally:
                if self._watcher is not None:
                    self._watcher.stop()
                    self._watcher = None
                self._unspin()
