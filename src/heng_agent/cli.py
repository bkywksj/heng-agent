"""命令行入口：交互模式（默认）与 -p 非交互模式。"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from heng_agent import configfile, worktree
from heng_agent.agent import Agent
from heng_agent.attachments import absorb_paths
from heng_agent.commands import discover_commands, expand_command, shell_runner
from heng_agent.context import expand_mentions, mention_images
from heng_agent.core import logs
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.core.types import AgentEvent, Notice, TextDelta, ToolFinished, ToolStarted
from heng_agent.core.version import is_dev, prepare_dev_home, version_label
from heng_agent.hooks import is_trusted, load_hooks
from heng_agent.mcp import MCPManager, load_servers
from heng_agent.mcp import is_trusted as mcp_is_trusted
from heng_agent.model.client import make_client
from heng_agent.model.providers import ENV_PROFILE, PROJECT_CONFIG, Profile, config_path, load_profiles
from heng_agent.permissions import Permissions, Rules
from heng_agent.session import SessionStore
from heng_agent.tools.base import Tool, builtin_tools
from heng_agent.ui.onboarding import needs_onboarding
from heng_agent.updates import run_upgrade


def _utf8_console() -> None:
    """Windows 控制台默认 GBK：中文输出会乱码甚至报错，程序自己切成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _enable_vt() -> None:
    """Windows：开启控制台的 VT 序列处理。

    控制台默认没开这个开关，rich 检测到就把终端当成老式控制台：只用 16 色、不输出超链接
    （实测 Windows Terminal 里新开的 PowerShell 也是 legacy_windows=True）。先开开关再建 rich 的 Console，
    就是真彩色 + 超链接。非 Windows、或输出不是控制台（被重定向）时什么都不做。
    """
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    for std in (-11, -12):  # STD_OUTPUT_HANDLE、STD_ERROR_HANDLE
        handle = kernel32.GetStdHandle(std)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="heng-agent", description="终端编码智能体：读代码、改代码、跑命令。")
    p.add_argument("--acp", action="store_true",
                   help="ACP 模式：让编辑器（智码、Zed、JetBrains……）通过标准输入输出驱动 heng-agent")  # fmt: skip
    p.add_argument("--version", action="version", version=f"heng-agent {version_label()}")
    p.add_argument(
        "-p",
        "--print",
        dest="prompt",
        nargs="?",
        const="",
        default=None,
        help="非交互：执行这一个任务后退出；管道传进来的内容会附在后面（只写 -p 就把管道内容当问题）",
    )
    p.add_argument("initial", nargs="?", default=None, metavar="要做的事",
                   help="进入交互界面并先发出这句话（同 claude \"…\"；智码「带一句话新建会话」这样启动）")  # fmt: skip
    p.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        default="text",
        help="-p 的输出：text 普通文字；json 最后输出一个对象；stream-json 每个事件一行 JSON",
    )
    p.add_argument(
        "--allowed-tools",
        action="append",
        default=[],
        metavar="规则",
        help='这次运行额外允许的规则，逗号分隔，如 "bash(npm test),edit_file"（-p 没人可问，默认都拒绝）',
    )
    p.add_argument("--disallowed-tools", action="append", default=[], metavar="规则",
                   help="这次运行额外拒绝的规则，逗号分隔，如 \"bash(git push*)\"")  # fmt: skip
    p.add_argument(
        "--provider",
        default=None,
        help="服务商配置档（heng / deepseek / kimi / glm / qwen / …），缺省用配置文件的 default",
    )
    p.add_argument("--list-providers", action="store_true", help="列出可用的服务商配置档与密钥是否已配置")
    p.add_argument("--base-url", default=None, help="临时覆盖配置档的地址")
    p.add_argument("--model", default=None, help="临时覆盖配置档的模型名")
    p.add_argument("--permission-mode", choices=["ask", "accept-edits", "plan", "auto"], default=None)
    p.add_argument("--workdir", default=None, help="工作目录，默认当前目录")
    p.add_argument(
        "--home",
        default=None,
        help="用户级目录（配置、会话、历史），默认正式版 ~/.heng-agent、开发版 ~/.heng-agent-dev；"
        "也可用环境变量 HENG_AGENT_HOME",
    )
    p.add_argument(
        "--resume", "-r", nargs="?", const="", default=None, metavar="会话 id",
        help="接着聊：不带 id 接这个目录最近的会话，带 id（或前缀）接指定的",
    )  # fmt: skip
    p.add_argument("-c", "--continue", dest="resume", action="store_const", const="",
                   help="接着这个目录最近的会话聊（同 --resume 不带 id）")  # fmt: skip
    p.add_argument("--session-id", default=None, metavar="id",
                   help="用指定的 id 开一个新会话（调用方预先分配 id，如智码）；id 已在用时报错")  # fmt: skip
    p.add_argument("--sessions", action="store_true", help="列出这个目录的会话")
    p.add_argument("-w", "--worktree", default=None, metavar="名字",
                   help="在独立的 git 工作树里工作（分支 heng/名字），不碰主分支")  # fmt: skip
    return p


def make_settings(args: argparse.Namespace) -> Settings:
    """优先级：命令行参数 > 项目配置 settings 段 > 用户配置 settings 段 > 内置默认值。"""
    overrides: dict[str, Any] = {}
    for key in ("provider", "base_url", "model", "permission_mode"):
        if getattr(args, key) is not None:
            overrides[key] = getattr(args, key)
    if args.workdir:
        overrides["workdir"] = Path(args.workdir).resolve()
    if args.home:
        overrides["home"] = Path(args.home).resolve()
    base = Settings(**overrides)  # 先定下用户目录与工作目录，才知道去哪读配置文件
    note = prepare_dev_home(base.home)  # 开发版第一次运行：从正式版复制一份配置
    if note:
        print(f"⋯ {note}", file=sys.stderr)
    unknown = configfile.unknown_settings(base.home, base.workdir)
    if unknown:  # 可能是新版写进去的：提一句、忽略，照样启动
        print(f"⋯ 配置里有这个版本不认识的设置（已忽略，可能是新版写的）：{', '.join(unknown)}", file=sys.stderr)
    from_file = configfile.settings_overrides(base.home, base.workdir)
    return Settings(**{**from_file, **overrides, "home": base.home, "workdir": base.workdir})


def resolve_profile(settings: Settings) -> tuple[str, Profile]:
    """按 --provider（或配置文件的 default）取配置档，再套上命令行的 --base-url / --model 临时覆盖。"""
    profiles, default = load_profiles(settings.home, settings.workdir)
    name = settings.provider or default
    if name not in profiles:
        raise AgentError(f"没有叫「{name}」的服务商配置档，可选：{', '.join(profiles)}")
    overrides = {k: v for k, v in (("base_url", settings.base_url), ("model", settings.model)) if v}
    return name, profiles[name].model_copy(update=overrides)


def list_providers(settings: Settings) -> int:
    profiles, default = load_profiles(settings.home, settings.workdir)
    project = settings.workdir / PROJECT_CONFIG
    note = f"；项目配置 {project}" if project.is_file() else ""
    print(f"配置文件：{config_path(settings.home)}{note}（默认 {default}）\n")
    for name, p in profiles.items():
        if name == ENV_PROFILE:  # 调用方（如智码）用环境变量给的：密钥也在环境变量里，不在配置文件
            key = f"密钥：HENG_AGENT_API_KEY {'已设置' if p.api_key else '未设置'}"
        elif p.api_key:
            key = "密钥：已写在配置文件"
        elif p.api_key_env:
            key = f"密钥：{p.api_key_env} {'已设置' if p.resolve_key() else '未设置'}"
        else:
            key = "不需要密钥"
        mark = "*" if name == default else " "
        print(f"{mark} {name:<12} {p.protocol:<9} {p.model:<24} {p.base_url}\n               {key}　{p.note}")
    return 0


SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def new_session_store(settings: Settings, session_id: str | None, *, resuming: bool) -> SessionStore | None:
    """--session-id：用调用方预先分配的 id 开新会话（智码这样就不用事后猜「刚开的是哪个会话」，不会串号）。

    id 只能是字母、数字、点、下划线、短横（也挡住 ../ 之类逃出会话目录）；已经在用就报错——和 Claude Code 一样，
    不悄悄接到旧会话上，接着聊请用 --resume。"""
    if session_id is None:
        return None
    if resuming:
        raise AgentError("--session-id 是开新会话用的，不能和 --resume / --continue 一起用")
    if not SESSION_ID.match(session_id):
        raise AgentError(f"会话 id「{session_id}」不合法：只能用字母、数字、点、下划线、短横，最长 128 个字符")
    store = SessionStore(settings.home, settings.workdir, session_id)
    if store.path.exists():
        raise AgentError(f"会话 id「{session_id}」已经在用了：接着聊请用 --resume {session_id}")
    store.pinned = True  # 调用方指定的 id：/clear 时 id 不变（见 SessionStore.pinned）
    return store


def list_sessions(settings: Settings) -> int:
    sessions = SessionStore.all(settings.home, settings.workdir)
    if not sessions:
        print("这个目录还没有会话。")
        return 0
    for s in sessions:
        when = datetime.fromtimestamp(s.updated).strftime("%m-%d %H:%M")
        print(f"{s.id}  {when}  {s.messages:>3} 条  {s.first_question[:40]}")
    print("\n接着聊：heng-agent --resume <id 或前缀>")
    return 0


def undo_msys_pathconv(prompt: str, command_names: set[str]) -> str:
    """Git Bash 调用 Windows 程序时会把以 / 开头的参数当成路径转换：`-p "/addtest x"` 到这里变成
    `D:/software/dev/Git/addtest x`。三个条件都满足才还原，不误伤普通输入：在 Git Bash 里（有 MSYSTEM）、
    以 Git 安装目录开头、剩下的第一段确实是已定义的自定义命令。
    """
    if not os.environ.get("MSYSTEM") or not command_names:
        return prompt
    roots = set()
    if os.environ.get("EXEPATH"):
        exe = Path(os.environ["EXEPATH"])
        roots |= {exe, exe.parent}
    bash = shutil.which("bash")
    if bash:
        roots |= set(Path(bash).resolve().parents[:3])
    normalized = prompt.replace("\\", "/")
    for root in sorted(roots, key=lambda r: len(str(r)), reverse=True):
        prefix = str(root).replace("\\", "/").rstrip("/") + "/"
        if normalized.lower().startswith(prefix.lower()):
            rest = normalized[len(prefix) :]
            if rest.split(" ", 1)[0] in command_names:
                return "/" + rest
    return prompt


def read_piped_stdin() -> str:
    """管道传进来的内容（`cat 日志 | heng-agent -p "分析"`）；直接在终端里运行（没有管道）时返回空。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except (OSError, ValueError):  # 标准输入被关掉或被别处接管（如测试框架）
        return ""


def split_rules(values: list[str]) -> list[str]:
    """--allowed-tools 的值按逗号拆成规则；括号里的逗号不拆（`bash(git log --format=%h,%s)` 是一条）。"""
    rules: list[str] = []
    for value in values:
        depth, current = 0, ""
        for ch in value:
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                rules.append(current.strip())
                current = ""
            else:
                current += ch
        rules.append(current.strip())
    return [r for r in rules if r]


def run_print_mode(agent: Agent, prompt: str, output_format: str = "text") -> int:
    """非交互：执行一个任务后退出。需要确认的操作一律拒绝（没人可问；用 --allowed-tools 事先放行）。

    text：文字写标准输出，工具执行情况写标准错误；
    json：结束时在标准输出写一个 JSON 对象（result、session_id、usage、rounds、is_error）；
    stream-json：每个事件一行 JSON（text / tool_start / tool_end / notice / done），供别的程序实时展示进度。
    """
    texts: list[str] = []

    def emit(obj: dict[str, Any]) -> None:
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    def on_event(event: AgentEvent) -> None:
        if isinstance(event, TextDelta):
            texts.append(event.text)
        if output_format == "stream-json":
            if isinstance(event, TextDelta):
                emit({"type": "text", "text": event.text})
            elif isinstance(event, ToolStarted):
                emit({"type": "tool_start", "id": event.id, "name": event.name, "input": event.input})
            elif isinstance(event, ToolFinished):
                emit({"type": "tool_end", "id": event.id, "name": event.name, "denied": event.denied,
                      "is_error": event.result.is_error, "content": event.result.content[:2000]})  # fmt: skip
            elif isinstance(event, Notice):
                emit({"type": "notice", "text": event.text})
            return
        if output_format == "json":
            return  # 结束时一次性输出
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolStarted):
            print(f"\n● {event.name} {event.input}", file=sys.stderr, flush=True)
        elif isinstance(event, ToolFinished) and event.denied:
            print("  ⎿ 已拒绝（非交互模式不确认；用 --allowed-tools 放行，或 --permission-mode accept-edits）",
                  file=sys.stderr)  # fmt: skip
        elif isinstance(event, Notice):
            print(f"⋯ {event.text}", file=sys.stderr, flush=True)

    if hasattr(agent.client, "on_retry"):
        agent.client.on_retry = lambda msg: on_event(Notice(msg))  # type: ignore[attr-defined]

    def approve(tool: Tool, args: dict[str, Any]) -> Any:
        return "deny"

    workdir = agent.settings.workdir
    prompt, _ = absorb_paths(prompt, workdir)  # 消息里的文件绝对路径 → @（外部文件复制进附件目录）
    images, _ = mention_images(prompt, workdir)
    session_id = agent.session.id if agent.session else None
    try:
        done = agent.run(expand_mentions(prompt, workdir)[0], on_event, approve, images=images)
    except AgentError as exc:
        if output_format == "text":
            raise
        emit({"type": "done", "is_error": True, "error": str(exc), "result": "".join(texts), "session_id": session_id})
        return 1
    finally:
        stopped = agent.ctx.tasks.kill_all()  # -p 跑完就退出：后台任务不能留着没人管
        if stopped:
            print(f"⋯ 已停止 {stopped} 个后台任务（-p 模式结束时不保留后台任务）", file=sys.stderr)
    if output_format == "text":
        print()
    else:
        summary = {"result": "".join(texts), "session_id": session_id, "usage": done.usage,
                   "rounds": done.rounds, "is_error": False}  # fmt: skip
        emit({"type": "done", **summary} if output_format == "stream-json" else summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    """入口：意料之外的异常在这里兜底——记进日志（带调用栈），给用户一句话和日志位置，不甩一屏堆栈。"""
    try:
        return _main(argv)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        logs.log.exception("意料之外的错误")
        path = logs.current()
        if is_dev():  # 开发版照样打出调用栈，改代码时直接看
            traceback.print_exc()
        where = f"出错位置记在日志里：{path}（反馈问题时请附上，或在 heng-agent 里运行 /doctor copy）" if path else ""
        print(f"出错了：{type(exc).__name__}: {exc}\n{where}".rstrip(), file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    _utf8_console()
    _enable_vt()  # 必须在建 rich Console 之前：否则 rich 按老式控制台渲染（16 色、无超链接）
    # 必须在任何提示、选择器之前：prompt_toolkit 的输入读取器建一次就缓存，晚了 Shift+Enter 就分不出来了
    from heng_agent.ui.winkeys import enable_shift_enter

    if not enable_shift_enter() and sys.platform == "win32":
        # 以前补丁因为导入路径写错静默失效，Shift+Enter 一直不能换行却没人发现：失败了要说出来
        print("⋯ Shift+Enter 换行没能启用（prompt_toolkit 版本变了？），可以先用 Ctrl+J 换行", file=sys.stderr)
    raw = sys.argv[1:] if argv is None else argv
    if raw[:1] == ["upgrade"]:  # heng-agent upgrade：升级到最新版
        if is_dev():
            print("这是开发版（直接跑仓库源码）：用 git pull 更新；正式版才用 heng-agent upgrade。", file=sys.stderr)
            return 1
        return run_upgrade()
    args = build_parser().parse_args(argv)
    if args.prompt == "" and args.initial:  # heng-agent "问题" -p：这句话就是 -p 的问题
        args.prompt, args.initial = args.initial, None
    if args.acp:  # 协议模式：服务商、目录等都由客户端按会话给，下面的交互式准备一概不做
        from heng_agent import acp_server

        return acp_server.run()
    try:
        settings = make_settings(args)  # 会读配置文件的 settings 段：写错了要给出可读的错误，而不是一堆堆栈
        if args.worktree:  # -w 名字：在独立的 git 工作树（独立分支）里干活，整个会话从那里开始
            created = worktree.create(worktree.repo_root(settings.workdir), args.worktree)
            settings = settings.model_copy(update={"workdir": created.path})
            print(f"⋯ 在工作树 {args.worktree} 里工作（分支 {created.branch}）：{created.path}", file=sys.stderr)
    except (AgentError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    note = logs.setup(settings.home)
    if note:
        print(f"⋯ {note}", file=sys.stderr)
    mode = "脚本模式（-p）" if args.prompt is not None else "交互模式"
    logs.log.info("启动 %s · %s · Python %s · %s · 目录 %s", version_label(), mode, platform.python_version(),
                  platform.platform(), settings.workdir)  # fmt: skip
    try:
        if args.list_providers:
            return list_providers(settings)
        if args.sessions:
            return list_sessions(settings)
        provider_name, profile = resolve_profile(settings)
        session = None
        if args.resume is not None:
            session = (
                SessionStore.find(settings.home, settings.workdir, args.resume)
                if args.resume
                else SessionStore.latest(settings.home, settings.workdir)
            )
            if args.resume and session is not None:
                session.pinned = True  # 按 id 接着聊：/clear 时 id 不变（见 SessionStore.pinned）
        new_store = new_session_store(settings, args.session_id, resuming=args.resume is not None)
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    client = make_client(profile, settings.max_tokens)
    try:
        user_hooks, project_hooks = load_hooks(
            settings.home, settings.workdir, claude_user=settings.claude_user_config, skip=settings.claude_hooks_skip
        )
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    trusted = is_trusted(settings.home, settings.workdir, project_hooks)
    hooks = user_hooks + (project_hooks if trusted else [])
    try:
        user_mcp, project_mcp = load_servers(settings.home, settings.workdir)
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    mcp_trusted = mcp_is_trusted(settings.home, settings.workdir, project_mcp)
    # 交互模式在后台连 MCP：欢迎页不用等最慢的服务（实测要 5～7 秒），用户提交第一句时再接入（Repl._sync_mcp）；
    # -p 一开始就要用工具，照旧连完再往下走
    interactive = args.prompt is None
    servers = user_mcp + (project_mcp if mcp_trusted else [])
    manager = MCPManager(servers, settings.workdir, background=interactive, proxy=settings.proxy)
    if args.prompt is not None:  # 交互模式由欢迎页逐行显示连接状态，这里只管非交互
        for error in manager.errors:
            print(f"⋯ {error}（跳过这个 MCP 服务）", file=sys.stderr)
    try:
        allow, deny, ignored, _ = configfile.permission_rules(
            settings.home, settings.workdir, claude_user=settings.claude_user_config
        )
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        manager.close()
        return 1
    if ignored:
        print(f"⋯ 项目配置里的允许规则已忽略（只有用户配置能加允许）：{', '.join(ignored)}", file=sys.stderr)
    agent = Agent(
        settings,
        client,
        tools=[*builtin_tools(), *([] if interactive else manager.tools)],  # 后台连的由 Repl 连好后接入
        permissions=Permissions(
            settings.permission_mode,
            Rules(allow + split_rules(args.allowed_tools), deny + split_rules(args.disallowed_tools)),
        ),
        session=session or new_store or SessionStore(settings.home, settings.workdir),
        hooks=hooks,
    )
    if session is not None:
        n = agent.resume(session)
        print(f"已接上会话 {session.id}（{n} 条消息）", file=sys.stderr)
    elif args.resume is not None:
        print("这个目录还没有会话，开始新会话。", file=sys.stderr)
    try:
        if args.prompt is not None:
            if needs_onboarding(settings.home) and not args.provider:
                # 脚本模式没法引导：默认连本机的 heng serve，多半连不上——先说清楚该怎么配
                print("⋯ 还没配置模型服务：先直接运行一次 heng-agent 按引导配好（或用 --provider 指定）。"
                      "现在先试连本机的 heng serve。", file=sys.stderr)  # fmt: skip
            if (project_hooks and not trusted) or (project_mcp and not mcp_trusted):
                print(
                    "⋯ 这个项目的钩子 / MCP 服务还没被信任，本次跳过（在交互模式里确认一次即可）。",
                    file=sys.stderr,
                )
            commands = discover_commands(settings.home, settings.workdir, claude_user=settings.claude_user_config)
            prompt = undo_msys_pathconv(args.prompt, set(commands))
            command = commands.get(prompt[1:].split(" ", 1)[0]) if prompt.startswith("/") else None
            # 命令里的 !`命令`：用户级的照跑；项目里的跟着仓库来、脚本模式又没法问，只在 auto 时跑
            trusted = command is not None and (not command.project or settings.permission_mode == "auto")
            runner = shell_runner(settings.workdir, settings.bash_timeout) if trusted else None
            prompt = expand_command(prompt, commands, runner) or prompt
            piped = read_piped_stdin()
            if piped.strip():
                prompt = f"{prompt}\n\n以下是通过管道传进来的内容：\n```\n{piped.rstrip()}\n```" if prompt else piped
            if not prompt.strip():
                print("错误：-p 后面没有问题，管道里也没有内容", file=sys.stderr)
                return 1
            return run_print_mode(agent, prompt, args.output_format)
        from heng_agent.ui.repl import Repl  # 交互界面依赖较重，非交互模式不加载

        repl = Repl(
            agent,
            untrusted_hooks=[] if trusted else project_hooks,
            untrusted_mcp=[] if mcp_trusted else project_mcp,
            provider_name=provider_name,
            mcp_status=manager.status,
            onboarding=needs_onboarding(settings.home) and not args.provider,
            initial_prompt=args.initial,
        )
        repl.mcp_managers.insert(0, manager)  # /mcp reconnect / disable 要找得到用户配置里的服务
        repl.watch_mcp(manager)  # 后台连接中：状态栏转圈、显示进度，连完立刻显示结果
        try:
            return repl.loop()
        finally:
            repl.close()
    except AgentError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        manager.close()  # 不管怎么退出，都把 MCP 服务的进程树关掉


if __name__ == "__main__":
    sys.exit(main())
