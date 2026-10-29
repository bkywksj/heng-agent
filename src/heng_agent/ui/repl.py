"""交互界面：输入框、流式渲染、权限确认对话、斜杠命令。"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from prompt_toolkit.application import get_app_or_none
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from heng_agent import __version__, worktree
from heng_agent.agent import Agent
from heng_agent.attachments import absorb_paths, paste_clipboard, paste_to_mentions
from heng_agent.commands import Command, discover_commands, expand_command, shell_runner
from heng_agent.context import (
    expand_mentions,
    load_memories,
    mention_images,
)
from heng_agent.core import logs
from heng_agent.core.compat import PROJECT_MEMORY_NAMES, claude_home
from heng_agent.core.errors import AgentError
from heng_agent.core.types import (
    AgentEvent,
    Notice,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    UserInterjection,
)
from heng_agent.core.version import is_dev, version_label
from heng_agent.hooks import Hook
from heng_agent.hooks import trust as trust_hooks
from heng_agent.mcp import MCPManager, ServerConfig
from heng_agent.model import context_window, quota, thinking
from heng_agent.model.client import make_client
from heng_agent.model.context_window import window_key
from heng_agent.model.providers import load_profiles
from heng_agent.model.thinking import EFFORT_NAME, EFFORTS
from heng_agent.session import SessionStore
from heng_agent.tools.shell import clip, run_command, shell_argv
from heng_agent.tools.skill import skill_source
from heng_agent.tools.todo import render as render_todos
from heng_agent.ui import info, mcp_view, statusline, suggest
from heng_agent.ui.approval import ApprovalMixin
from heng_agent.ui.config_cmd import ConfigCommand
from heng_agent.ui.doctor import run_checks
from heng_agent.ui.inputbox import InputBox, TurnSignal
from heng_agent.ui.keywatch import EscWatcher
from heng_agent.ui.prompt import MODE_LABEL, MODES, STYLE, SlashCompleter, toolbar
from heng_agent.ui.render import (
    _PLAIN,
    ALIASES,
    MAX_THUMBNAILS,
    PROMPT_BAR,
    ReplyMarkdown,
    Ticker,
    Trimmed,
    _stdin_is_terminal,
    autolink,
    brief_result,
    edit_in_editor,
    elapsed_text,
    file_link,
    linkify,
    split_done,
    tool_label,
    turn_summary,
)
from heng_agent.ui.repl_commands import CommandsMixin
from heng_agent.ui.repl_mcp import McpMixin
from heng_agent.ui.repl_memory import MemoryMixin
from heng_agent.ui.repl_session import SessionMixin
from heng_agent.ui.repl_subagents import NOTICE_PREFIX, SubagentsMixin, notice_line
from heng_agent.ui.repl_turn import TurnMixin
from heng_agent.ui.select import Option, ask_line, choose
from heng_agent.ui.session_tools import (
    default_export_path,
    export_markdown,
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
  /doctor [copy]              检查配置、服务商连接、终端、git 等；copy 连同最近的日志复制下来（反馈问题用）

会话
  /clear                      开始新会话          /resume [关键词]   选一个旧会话接着聊
  /rename 名字                给会话起名          /export [路径]     导出成 Markdown
  /compact [要求]             手动压缩历史（快撑满时会自动压缩）
  /undo                       撤销上一问：恢复它改过的文件，对话也退回去（bash 造成的改动除外）
  /rewind                     选一问，对话与代码一起回到它之前（空输入框里双击 ESC 同样）
  /copy [N]                   复制上一条（或倒数第 N 条）回复；有代码块时可以只复制其中一段
  /exit                       退出（/quit、Ctrl+D 同样）

模型与权限
  /provider [名字]            切换服务商；/model [名字] 切换模型；/config provider 引导配置服务商与 API
  /mode ask|accept-edits|plan|auto   手动确认 / 自动改文件 / 计划模式 / 全部放行（Shift+Tab 循环切换）
  /think [on|off|预算|show]    思考模式：先深度思考再回答（难题用）
  /effort [low…max|auto]     思考强度：low / medium / high / xhigh / max，auto = 服务默认
  /btw 问题                  旁支问题：随口问一句，不打断正在进行的回答、不进对话历史、不调工具
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
  回答中 ESC / Ctrl+C 打断这一问，Enter 把话排队、↑ 取回来改；答完 Tab / → 采纳灰字的下一句建议
  双击 ESC 清空输入框（空框里双击是 /rewind），Ctrl+C 也清空，清掉的按 ↑ 找回；Ctrl+L 清屏；Ctrl+B 把跑着的命令转到后台
  回答中 Ctrl+X Ctrl+S 立刻发送：框里的话和排着队的话马上发（打断这一问）
  有子智能体在跑时：输入框最后一行按 ↓ 选中它，Enter 看它在做什么，ESC 回到输入框
  空输入框里连按两次 Ctrl+C 退出"""


#: 下一句建议的回答最多多少 token（只要一句话；留些余量给关不掉思考的模型）
SUGGEST_MAX_TOKENS = 1024


class Repl(ApprovalMixin, McpMixin, SessionMixin, MemoryMixin, CommandsMixin, TurnMixin, SubagentsMixin):
    #: 内置命令：同名的自定义命令不生效
    BUILTIN = frozenset({"help", "clear", "mode", "model", "provider", "compact", "undo", "todo", "cost", "skills",
                         "mcp", "config", "rewind", "resume", "rename", "permissions", "copy", "export",
                         "context", "diff", "memory", "doctor", "tasks", "think", "effort", "btw", "agents", "worktree",
                         "exit", "quit", "terminal-setup", "usage", "status", "stats", "hooks", "todos", "version",
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
        initial_prompt: str | None = None,
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
        self._send_now = False  # 这一问是 Ctrl+X Ctrl+S 打断的（立刻发送排队的话）
        self._prefill = ""  # 下次输入框预填的文字（/rewind 后把那一问放回来）
        # 确认框里选了「以后都允许」：把规则写进用户配置
        agent.permissions.on_persist = self._persist_rule
        self._plain_input = False  # 输入框用不了时退回普通输入
        self._queued: list[str] = []  # 回答中敲下 Enter 的话：这一问结束后按顺序发出（见 repl_turn）
        if initial_prompt:  # heng-agent "要做的事"：欢迎页（与引导、信任确认）之后先发出它，像刚输入的一样
            self._queued.append(initial_prompt)
        self._btw_log: list[tuple[str, str]] = []  # 问过的旁支问题与回答（/btw 带上它们，只在内存里）
        self._setup_subagents()  # 子智能体面板、后台子智能体做完时的通知（见 repl_subagents）
        self._turn_live_render: Any = None  # 回答中：还没写完的那段回复，画在输入框上方（见 repl_turn）
        self._turn_activity: Any = None  # 回答中：转圈旁边的文字
        self._turn_started = time.time()  # 当前这一问是什么时候提的：结尾那行写时间与用时
        self._tool_started = time.time()  # 正在跑的工具什么时候开始的：等待动画计时、结果后面写用时
        # 状态栏要起子进程才拿得到的数据（git 分支、用户的状态栏命令）：后台跑、缓存，跑完重画输入框
        self._bar_git = statusline.Background(on_change=self._refresh_input)
        self._bar_custom = statusline.Background(on_change=self._refresh_input)
        self._bar_command = statusline.load_command(agent.settings)
        self._bar_quota = statusline.Background(on_change=self._refresh_input)
        self._quota_for: tuple[str, float] = ("", 0.0)  # (查的是哪个服务地址, 什么时候查的)：换服务商或过 3 分钟再查
        self._status: Any = None  # 等待动画（rich Status）
        self._event_lock = threading.RLock()
        self._live: Live | None = None  # 正在流式渲染的一段 Markdown
        self._reply_started = False  # 这条回复已经固定打印过至少一段（后面的段落不再带 ✦）
        self._thought = ""  # 正在进行的这段思考
        self._think_started = 0.0
        self.last_thought = ""  # 最近一次完整的思考（/think show）
        #: 上一行是不是已经空着（块与块之间空一行，不空两行，见 _gap）
        self._block_start = True
        self._buf = ""
        self._pending_shell: list[str] = []  # 用户 !命令 的输出，附在下一问里
        self._hook_retry()

    def _hook_retry(self) -> None:
        """模型客户端重试时在界面上说一声（切换服务商后要重新挂）。"""
        if hasattr(self.agent.client, "on_retry"):
            self.agent.client.on_retry = self._on_retry  # type: ignore[attr-defined]

    def _on_retry(self, message: str) -> None:
        """模型请求出错、等着重试：在转圈那行原地显示「等模型服务…（12s）· 原因 · 第 2/4 次重试」。

        以前每次重试另起一行黄字（还带着整段 JSON），四次重试刷满一屏（用户反馈）。日志里照旧每次都记。"""
        reason, _, plan = message.rpartition("；")
        reason = reason or message
        reason = reason.removeprefix("模型服务返回 ")
        if len(reason) > 60:
            reason = reason[:58] + "…"
        attempt = plan[plan.find("第") :].rstrip("）") if "第" in plan else ""
        self._spin(
            "等模型服务…", hint=" · ".join(p for p in (reason, f"{attempt}重试" if attempt else "", "ESC 中断") if p)
        )

    def _gap(self) -> None:
        """开始一个新的块（提问、思考摘要、一段回复、一次工具调用、过程提示、结尾那行……）之前调：
        块与块之间空一行，上一行已经空着就不再空。

        以前各处各自决定空不空：思考摘要、工具结果、过程提示后面紧跟着回复，连续的工具挤在一起，
        排队的话贴着上一问的结尾……用户反馈「显得很挤」。统一成这一条规矩。调完紧接着打印内容。
        """
        if not self._block_start:
            self.console.print()
        self._block_start = False

    def _commit(self, text: str) -> None:
        """把写完的一段正式打印到屏幕上（先擦掉实时区域）。同一条回复只有第一段带 ●，后面的段落之间空一行。"""
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._turn_live_render = None  # 回答中：这段要正式打印了，「正在进行」那块不再画它
        if not text.strip():
            return
        if self._reply_started:
            self.console.print()
        else:
            self._gap()
        self.console.print(self._reply(text, first=not self._reply_started))
        self._reply_started = True
        self._block_start = False

    def _end_text(self) -> None:
        """结束这条回复：剩下没固定的那段也正式打印。"""
        if self._live is not None or self._buf:
            self._commit(self._buf)
            self._buf = ""
        if self._reply_started:
            self._reply_started = False

    @staticmethod
    def _reply(text: str, first: bool = True) -> Any:
        """AI 的一段回复：顶格写，不加图标（提问左侧是青色竖条，工具调用是灰色 ●，靠这个区分）。

        以前整段缩进两格，再加上 rich 把每行补齐到终端宽度、代码块铺底色：鼠标拖选复制出来每行前后都是空格，
        Python、YAML 贴进去缩进就错了（用户反馈）。现在顶格、行尾空格去掉、代码块不铺背景。
        rich 的 Markdown 段落本来就是放不下折到下一行（以前表格单元格默认截断，中文长串会丢字）。
        裸网址转成链接（Windows Terminal 里 Ctrl+点击就能打开）。first 保留给调用方，现在每段画法一样。
        """
        return Trimmed(ReplyMarkdown(autolink(text)))

    def _model_label(self) -> str:
        p = getattr(self.agent.client, "profile", None)
        if p is None:
            return "-"
        return f"{self.provider_name} · {p.model}" if self.provider_name else p.model

    def _toolbar(self) -> Any:
        a = self.agent
        app = get_app_or_none()
        width = app.output.get_size().columns if app is not None else None
        branch, dirty = self._bar_git.value or (None, False)
        return toolbar(a.permissions.mode, self._model_label(), str(a.settings.workdir), a.last_input_tokens,
                       a.context_window, len(a.ctx.tasks.running()),
                       thinking=thinking.bar_label(a.model_name, a.thinking, a.effort),
                       compact_ratio=a.settings.compact_ratio, mcp=mcp_view.toolbar_part(self), branch=branch,
                       dirty=dirty, speed=a.last_speed, width=width, custom=self._custom_line(),
                       quota=self._quota_part())  # fmt: skip

    def _quota_part(self) -> tuple[str, str] | None:
        found = self._bar_quota.value
        if found is None or self._quota_for[0] != self._profile_url():
            return None  # 还没查到，或刚换了服务商、查到的是上一个的
        style = {"warn": "class:bar.ctx-warn", "hot": "class:bar.ctx-hot"}.get(found.level, "class:bar.quota")
        return style, found.text

    def _profile_url(self) -> str:
        profile = getattr(self.agent.client, "profile", None)
        return getattr(profile, "base_url", "") or ""

    def _suggest_next(self) -> None:
        """这一问答完：在后台问一句「下一句建议」，到了就灰字显示在空的输入框里（见 ui/suggest.py）。

        关掉了（prompt_suggestions）、没有带边框的输入框（灰字没处显示）、客户端不是按配置档建的（测试）都不问。"""
        box = self._box
        if box is None or self._plain_input:
            return
        box.suggestion = ""  # 上一句建议先作废（关掉了也一样）
        # 计划模式里多半还在商量方案，不给（同 Claude Code）
        if not self.agent.settings.prompt_suggestions or self.agent.permissions.mode == "plan":
            return
        profile = getattr(self.agent.client, "profile", None)
        if profile is None:
            return
        agent, messages = self.agent, list(self.agent.messages)
        request = suggest.plan(agent.system_prompt(), messages, agent.tool_specs(), agent.last_cache_tokens > 0)
        if request is None:
            return

        def work() -> None:
            try:
                client = make_client(profile, SUGGEST_MAX_TOKENS)
                if hasattr(client, "effort"):
                    if request.cached:  # 思考设置也和主对话一样：改了思考参数，对话部分的缓存就失效
                        client.thinking_budget = agent.thinking_budget if agent.thinking else 0
                        client.effort = agent.effort
                    else:
                        client.effort = "low"  # 一句话的事：想得少一点，快、便宜
                text = suggest.ask(client, request)
            except Exception as exc:  # 建议拿不到不影响使用：记日志，不打扰
                logs.log.info("下一句建议没拿到：%s", exc)
                return
            if text and self.agent.messages == messages:  # 这期间又发了一问：这句建议过时了
                box.suggestion = text
                box.refresh()

        threading.Thread(target=work, name="heng-suggest", daemon=True).start()

    def _ensure_window(self) -> None:
        """配置档没写 context_window 时，在后台向服务商查一次这个模型的上下文长度（换了模型就再查）。

        查到之前按模型名推断；查不到记 0，不再查。查到后状态栏、自动压缩都用新值。"""
        a = self.agent
        profile = getattr(a.client, "profile", None)
        if profile is None or profile.context_window:
            return
        key = window_key(profile)
        if key in a.fetched_windows:
            return
        a.fetched_windows[key] = 0  # 先占上：正在查，别重复发

        def work() -> None:
            a.fetched_windows[key] = context_window.fetch(profile) or 0
            app = get_app_or_none()
            if app is not None:
                app.invalidate()  # 输入框开着：状态栏马上换成查到的值

        threading.Thread(target=work, name="heng-window", daemon=True).start()

    def _custom_line(self) -> list[tuple[str, str]] | None:
        """用户状态栏命令的输出（还没跑完第一次时 None：先显示内置的第一行）；出错的说明用红字。"""
        value = self._bar_custom.value
        if isinstance(value, str):
            return [("class:bar.ctx-hot", f" {value}")]
        return value

    def _refresh_bar(self) -> None:
        """显示输入框前（一问答完）刷新状态栏的外部数据：git 分支、用户的状态栏命令。都在后台跑，不拖慢输入框。"""
        a = self.agent
        workdir = a.settings.workdir
        self._bar_git.refresh(lambda: statusline.git_state(workdir))
        profile = getattr(a.client, "profile", None)
        url = self._profile_url()
        if profile is not None and url and (url != self._quota_for[0] or time.monotonic() - self._quota_for[1] > 180):
            # 额度：每 3 分钟最多查一次（和 tauri-cc 的缓存一样），换了服务商立刻查
            self._quota_for = (url, time.monotonic())
            key, proxy = profile.resolve_key(), getattr(profile, "proxy", None)
            self._bar_quota.refresh(lambda: quota.fetch(url, key, proxy=proxy))
        self._ensure_window()
        command = self._bar_command
        if command:
            data = statusline.payload(
                session_id=a.session.id if a.session else "", transcript=str(a.session.path) if a.session else "",
                workdir=workdir, model=a.model_name, provider=self.provider_name or "", version=__version__,
                mode=a.permissions.mode, used=a.last_input_tokens, window=a.context_window,
                total_in=a.usage.get("input_tokens", 0), total_out=a.usage.get("output_tokens", 0),
                speed=a.last_speed,
            )  # fmt: skip
            self._bar_custom.refresh(lambda: statusline.run_command(command, data, workdir))

    def _refresh_input(self) -> None:
        """后台 MCP 连完时（连接线程里调）：输入框正开着就重画，状态栏立刻显示连上了几个。"""
        if self._box is not None:
            self._box.refresh()

    def _mcp_connected(self) -> None:
        """后台 MCP 连完（连接线程里调）：输入框正开着就把每个服务的结果打印到它上方、工具接进来
        （以前只更新状态栏，用户回答得快、输入框先出来时，看不到连了哪些服务）；没开着就等下次显示输入框前再接。"""
        box = self._box
        if box is None or not box.print_above(lambda: self._sync_mcp(wait=False)):
            self._refresh_input()

    def watch_mcp(self, manager: MCPManager) -> None:
        """后台连接的 MCP 管理器：连接中每 0.1 秒重画一次输入框（状态栏的转圈与进度动起来），连完再重画一次。"""
        manager.on_done(self._mcp_connected)
        if manager.ready:
            return

        def tick() -> None:
            while not manager.wait(0.1):
                self._refresh_input()

        threading.Thread(target=tick, name="mcp-progress", daemon=True).start()

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
                        agents=self._agent_rows,
                        on_view_agent=self.view_subagent,
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
        if cmd == "doctor" and not prior:
            return [("copy", "检查后把结果连同最近的日志复制下来，反馈问题时贴上", False)]
        if cmd == "think" and not prior:
            return [("on", "打开思考模式", False), ("off", "关闭", False), ("show", "看最近一次思考", False),
                    ("16000", "把思考预算设为 16000 token 并打开", False)]  # fmt: skip
        if cmd == "effort" and not prior:
            return [(e, f"{EFFORT_NAME[e]}", False) for e in EFFORTS] + [("auto", "用服务的默认强度", False)]
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

    def _spin(self, text: str, hint: str = "ESC 中断", started: float | None = None,
              limit: float | None = None) -> None:  # fmt: skip
        """发出请求、跑工具时显示旋转动画：否则屏幕没动静，分不清是卡住还是在干活。

        括号里的时间一直在走（见 render.Ticker）：started 不给就从这一问提出时算（等模型时看的是这一问一共等了多久）；
        跑工具时给工具开始的时刻，limit 是 bash 的超时。
        """
        if self._live is not None:
            return  # 正在流式输出文字时不叠加动画（rich 同一时间只能有一个实时区域）
        message = Ticker(text, hint, self._turn_started if started is None else started, limit,
                         tokens=lambda: self.agent.turn_output_tokens)  # fmt: skip
        if self._in_live_turn():  # 回答中输入框一直在：转圈画在输入框上方，不用 rich 的 Status（会和输入框抢光标）
            self._turn_activity = message
            return
        if self._status is None:
            self._status = self.console.status(message, spinner="dots")
            self._status.start()
        else:
            self._status.update(message)

    def _spin_tool(self, name: str, args: dict[str, Any]) -> None:
        """跑工具时的等待动画：从现在开始计时；前台 bash 带上超时与「Ctrl+B 转后台」。确认框批准后也走这里。"""
        self._tool_started = time.time()
        foreground_bash = name == "bash" and not args.get("run_in_background")
        limit = float(args.get("timeout") or self.agent.settings.bash_timeout) if foreground_bash else None
        hint = "ESC 中断 · Ctrl+B 转后台" if foreground_bash else "ESC 中断"
        self._spin(f"{tool_label(name)}…", hint, started=self._tool_started, limit=limit)

    def _in_live_turn(self) -> bool:
        return self._box is not None and self._box.turn is not None

    def _unspin(self) -> None:
        self._turn_activity = None
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
            if self._buf and self._in_live_turn():
                self._turn_live_render = self._reply(self._buf, first=not self._reply_started)
            elif self._buf:
                if self._live is None:
                    # transient：停止时擦掉，由 _commit 把完整内容正式打印；crop：超长的一段只显示放得下的部分，不重复
                    self._live = Live(console=c, refresh_per_second=10, transient=True, vertical_overflow="crop")
                    self._live.start()
                self._live.update(self._reply(self._buf, first=not self._reply_started))
            return
        self._end_text()
        self._unspin()
        if isinstance(event, UserInterjection) and event.text.startswith(NOTICE_PREFIX):
            self._gap()  # 后台子智能体做完、结论插进了这一问：一行提示，不当成你说的话
            c.print(notice_line(event.text))
            self._spin("思考中…")
        elif isinstance(event, UserInterjection):  # 回答中插的话已经交给模型：照提问的样子显示在它该在的位置
            self.echo_user(event.text, restart_timer=False)  # echo_user 自己会先空一行
            self._spin("思考中…")
        elif isinstance(event, Notice):
            self._gap()
            c.print(f"[yellow]⋯ {escape(event.text)}[/]")
            self._spin("处理中…")
        elif isinstance(event, ToolStarted):
            tool = self.agent.tools.get(event.name)
            summary = tool.summary(event.input) if tool else str(event.input)
            label = tool_label(event.name)
            # 工具调用过程做成「注释」的样子：不加粗、偏暗、只占一行（超长截断），不抢回复的视线。
            # 每次调用（连同下面的结果）算一块，和前后空一行：以前连续几个工具挤在一起（用户反馈太挤）
            self._gap()
            line = Text.assemble(("● ", "grey50"), (label, "dark_cyan"), (f" {summary}" if summary else "", "grey50"))
            line.truncate(max(20, c.width - 2), overflow="ellipsis")
            c.print(line, no_wrap=True)
            self._spin_tool(event.name, event.input)
        elif isinstance(event, ToolFinished):
            if event.denied:
                c.print(Text("  ⎿ 已拒绝", style="yellow"))
            else:
                lines = brief_result(event.name, event.result.content, event.result.is_error)
                spent = time.time() - self._tool_started
                if lines and spent >= 1:  # 跑得久的工具在结果后面写上用了多久
                    lines[0] += f" · {elapsed_text(spent)}"
                style = "red" if event.result.is_error else "grey42"
                for i, text in enumerate(lines):
                    row = Text(("  ⎿ " if i == 0 else "    ") + text, style=style)
                    row.truncate(max(20, c.width - 2), overflow="ellipsis")  # 每条只占一行，不折行
                    c.print(row, no_wrap=True)
            self._spin("思考中…")  # 工具结果要交回给模型，接下来又是等模型
        elif isinstance(event, TurnDone):
            self._gap()
            c.print(turn_summary(event, self._turn_started))

    # ---------- 确认 ----------

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

    def notify_if_long(self, seconds: float) -> None:
        """任务跑得久（超过 notify_after 秒）就响铃。

        Windows Terminal 收到响铃会闪任务栏、标签页显示铃铛，切到别的窗口也看得到。
        """
        limit = self.agent.settings.notify_after
        if limit and seconds >= limit and self.console.is_terminal:
            self.console.file.write("\a")
            self.console.file.flush()

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
        mode_text = escape(MODE_LABEL.get(mode, mode))
        if mode == "auto":  # 全部放行是危险模式：欢迎页上醒目标出
            mode_text = f"[bold red]{mode_text}[/]"
        dev = is_dev()
        # 开发版只在标题旁显示一个徽标（以前版本行、状态栏也各有一个，满屏「开发版」）；这里只补一句数据目录
        version = escape(version_label()) + (f"  [dim]数据 {escape(str(s.home))}[/]" if dev else "")
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
        # 按键提示只在这里说一次（以前还写在输入框的上边框上，每次都在，用户觉得不好看）；全的在 /help。
        # 每行不超过 80 列（test_banner_hints_fit_80_columns）：rich 只在空格处断行，
        # 中英混排的长句会把后半截整段挪到下一行，84 列的窗口里实测在 60 列处就折了，右边空一大块
        self.console.print(
            "[cyan]✦[/] [dim]@文件 附上内容 · !命令 直接执行 · / 补全命令 · Shift+Enter 换行 · Alt+V 贴图[/]"
        )
        self.console.print(
            "[cyan]✦[/] [dim]Shift+Tab 切换模式 · ESC 中断 · 回答中接着打字、Enter 排队 · /help 看全部[/]"
        )
        for line in mcp_view.startup_line(self):  # 每个服务一行，最后一行汇总
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
            self.console.print("[dim]首次在这个项目里用？输入 /init，让 AI 先熟悉项目、写好 AGENTS.md（每次都会读）[/]")

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
        elif cmd == "/effort":
            self.effort_command(rest)
        elif cmd == "/btw":
            self.btw_command(rest)
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
            self.copy_command(rest)
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
            checks = run_checks(self)
            for status, name, note in checks:
                c.print(f"  {icons[status]} [bold]{name:<6}[/] {escape(note)}")
            if rest == "copy":  # 反馈问题时用：检查结果 + 最近的日志，一次复制走
                self.copy_diagnosis(checks)
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
        self._block_start = True
        if self._plain_input:  # 退回普通输入的终端：没有边框与底部状态栏，用分隔线 + 一行状态代替
            a = self.agent
            pct = a.last_input_tokens / a.context_window * 100 if a.context_window else 0
            self.console.rule(style="grey35")
            self.console.print(f"[dim]{a.permissions.mode} · {escape(self._model_label())} · 上下文 {pct:.0f}%[/]")
        line = self._input("> ")
        if isinstance(line, TurnSignal):
            # 后台子智能体做完、把空闲的输入框叫醒了：原样交给主循环（它发起一问）。以前这里直接 .strip()，
            # 程序当场退出（真实终端里实测：后台子智能体一做完窗口就回到了 PowerShell）
            return line  # type: ignore[return-value]
        while self._plain_input and line.endswith("\\"):  # 普通输入里用行尾 \ 续行；输入框里直接 Ctrl+J
            line = line[:-1] + "\n" + self._input("… ")
        return line.strip()

    def echo_user(self, text: str, restart_timer: bool = True) -> None:
        """输入框提交后已被擦掉：把这句话用醒目的标记打印到对话记录里，和模型回复区分开。

        里面指向真实文件的 @引用 与绝对路径渲染成超链接（见 linkify）。"""
        lines = text.splitlines() or [""]
        workdir = self.agent.settings.workdir
        self._gap()  # 排队的话自动发出时前面没有 read() 那行空行，不空的话贴着上一问的结尾（实测）
        for line in lines:  # 每行一根竖条：多行的提问也一眼看得出是一整段
            # 文字也上色（和竖条同色）：长对话里一眼扫得到哪些是自己问的（用户对照 Kimi 的样子提的）
            self.console.print(f"[bold cyan]{PROMPT_BAR}[/] [bold cyan]{linkify(line, workdir)}[/]")
        self.console.print()  # 和下面的「思考中」、回复拉开一点，不挤
        self._block_start = True
        if restart_timer:  # 回答中插的话、马上执行的命令不重新计时：用时算的还是这一问
            self._turn_started = time.time()  # 这一问从这里开始算用时（每问结尾的那行灰字）

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

    def close(self) -> None:
        for manager in self.mcp_managers:
            manager.close()
        stopped = self.agent.ctx.tasks.kill_all()  # 不留后台任务在那里偷偷跑
        if stopped:
            self.console.print(f"[dim]已停止 {stopped} 个后台任务[/]")

    def _flush_thought(self) -> None:
        """一段思考结束：折叠成一行灰字，全文留给 /think show。"""
        self._unspin()
        seconds = time.monotonic() - self._think_started
        self.last_thought = self._thought
        self._gap()
        self.console.print(f"[grey50]✻ 思考了 {seconds:.1f} 秒（约 {len(self._thought)} 字）· /think show 查看[/]")
        self._thought = ""

    def switch_workdir(self, path: Path, label: str) -> None:
        """会话切到另一个工作目录：智能体的读写边界、系统提示词跟着换；那里的自定义命令、角色重新读。"""
        self.agent.set_workdir(path)
        self.commands = discover_commands(
            self.agent.settings.home, path, claude_user=self.agent.settings.claude_user_config
        )
        self.reload_agents()
        self.console.print(f"[green]✓ 已切到{escape(label)}：{escape(str(path))}[/]")

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
            # 欢迎页之后若有确认问题（信任钩子、信任 MCP）挡着，等用户答完服务往往已经连好：
            # 在输入框出现前就把结果打出来，不然欢迎页一直写着「后台连接中」，看起来没有任何变化（用户反馈）
            self._sync_mcp(wait=False)
            self._refresh_bar()
            try:
                # 回答中排进队列的话：这一问结束后按顺序发出，像刚输入的一样（会显示在对话里）
                notices = self._take_notices()
                text = "\n\n".join(notices) if notices else self._queued.pop(0) if self._queued else self.read()
            except (EOFError, KeyboardInterrupt):
                return 0
            if isinstance(text, TurnSignal):  # 后台子智能体做完、把空闲的输入框叫醒了：框里的字留着，下一圈发起一问
                self._prefill = text.draft
                continue
            if not text:
                continue
            if notices:  # 自动发起的一问：每个子智能体一行提示，不当成你说的话
                self._gap()
                for notice in notices:
                    self.console.print(notice_line(notice))
                self.console.print()
                self._block_start = True
                self._turn_started = time.time()
            elif not self._plain_input:
                self.echo_user(text)  # 输入框已被擦掉：把这句话留在对话记录里
            # 后台连的 MCP 服务：发给模型的话要等它们连好（模型这一轮可能要用）；# 记忆与内置命令在本地处理、
            # 用不上 MCP，不等——以前 /effort 这类命令也要等十几秒（真实终端里实测）
            local = text.startswith("#") or (text.startswith("/") and text[1:].split(" ", 1)[0] in self.BUILTIN)
            local = local and not notices
            self._sync_mcp(wait=not local)
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
                self._esc_pressed = self._send_now = False
                started = time.monotonic()
                if self._live_turn_ok():
                    self._run_turn_live(text, images)  # 回答中输入框一直在（见 repl_turn）
                else:  # 没有带边框输入框的终端：照旧在主线程跑，后台线程只认 ESC / Ctrl+B
                    self._watcher = EscWatcher(self._on_escape, self.agent.ctx.detach.set)
                    self._watcher.start()
                    self.agent.run(text, self.on_event, self.approve, images=images)
                self.notify_if_long(time.monotonic() - started)
                self._suggest_next()
            except KeyboardInterrupt:
                self._end_text()
                self._unspin()
                self._gap()
                if getattr(self, "_send_now", False):  # Ctrl+X Ctrl+S：主循环接着就发排队的话
                    self.console.print("[yellow]已打断这一问，马上发你排队的话（正在跑的命令连同子进程已终止）。[/]")
                else:
                    how = "按了 ESC" if self._esc_pressed else "按了 Ctrl+C"
                    self.console.print(f"[yellow]已中断（{how}；正在跑的命令连同子进程已终止）。可以接着说下一步。[/]")
            except AgentError as exc:
                self._end_text()
                self._unspin()
                logs.log.warning("这一问出错：%s", exc)
                self._gap()
                self.console.print(f"[red]{escape(str(exc))}[/]")
            except Exception as exc:
                # 意料之外的错误（程序的 bug）：以前整个程序崩掉、对话也没了。现在记下调用栈，这一问作废，会话照常
                self._end_text()
                self._unspin()
                logs.log.exception("这一问遇到意料之外的错误")
                where = f"，出错位置记在日志里：{logs.current()}" if logs.current() else ""
                hint = f"{where}。可以接着说下一步；反馈问题时运行 /doctor copy"
                self._gap()
                self.console.print(f"[red]内部错误：{escape(type(exc).__name__)}: {escape(str(exc))}[/]"
                                   f"[dim]{escape(hint)}[/]")  # fmt: skip
            finally:
                if self._watcher is not None:
                    self._watcher.stop()
                    self._watcher = None
                self._unspin()
