"""/doctor：一项项检查运行环境，出问题时先跑它看是哪一环。

每项返回 (状态, 名称, 说明)：状态 ok / warn / fail。服务商那一项会真的连一次（取模型列表），看地址和密钥能不能用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Literal

from heng_agent import configfile
from heng_agent.context import MEMORY_WARN_TOKENS, estimate_tokens, load_memories, memory_files
from heng_agent.core.errors import AgentError
from heng_agent.core.net import describe_proxy
from heng_agent.core.version import version_label
from heng_agent.hooks import is_trusted, load_hooks
from heng_agent.mcp import is_trusted as mcp_is_trusted
from heng_agent.mcp import load_servers
from heng_agent.model.client import list_models
from heng_agent.ui.sixel import supports_sixel

if TYPE_CHECKING:
    from heng_agent.ui.repl import Repl

Status = Literal["ok", "warn", "fail"]
Check = tuple[Status, str, str]


def run_checks(repl: Repl) -> list[Check]:
    a, console = repl.agent, repl.console
    home, workdir = a.settings.home, a.settings.workdir
    checks: list[Check] = [("ok", "版本", f"heng-agent {version_label()} · Python {sys.version.split()[0]}")]

    try:
        configfile.validate_all(home, workdir)
        checks.append(("ok", "配置文件", f"{configfile.config_file(home, workdir, False)} 无误"))
    except AgentError as exc:
        checks.append(("fail", "配置文件", str(exc)))

    profile = getattr(a.client, "profile", None)
    if profile is None:
        checks.append(("warn", "服务商", "当前模型客户端没有配置档（测试或自定义客户端）"))
    else:
        name = repl.provider_name or "当前"
        if profile.protocol == "anthropic" or profile.api_key or profile.api_key_env:
            key_ok = bool(profile.resolve_key())
            key_note = "已设置" if key_ok else f"没有密钥（{profile.api_key_env or 'api_key'} 未设置）"
            checks.append(("ok" if key_ok else "warn", "密钥", key_note))
        # 连不上时第一个要问的：请求到底走没走代理、走的哪个（地址里的密码遮掉）
        checks.append(("ok", "代理", describe_proxy(profile.proxy)))
        started = time.monotonic()
        try:
            models = list_models(profile)
            cost = time.monotonic() - started
            hit = "，当前模型在列表里" if profile.model in models else "（当前模型不在列表里，名字可能写错了）"
            status: Status = "ok" if profile.model in models or not models else "warn"
            checks.append(
                (status, "服务商连接", f"{name} · {profile.base_url} 可用，{cost:.1f} 秒，{len(models)} 个模型{hit}")
            )
        except AgentError as exc:
            checks.append(("fail", "服务商连接", f"{name} · {profile.base_url}：{exc}"))

    legacy = getattr(console, "legacy_windows", False)
    color = console.color_system or "无"
    term_status: Status = "warn" if legacy or color in ("windows", "standard", "无") else "ok"
    term_note = f"颜色 {color}{'，老式控制台（没有超链接）' if legacy else '，支持超链接'}"
    term_note += "，可画图片缩略图" if supports_sixel(a.settings.image_preview) else "，不画图片缩略图"
    checks.append((term_status, "终端", term_note))

    git = shutil.which("git")
    if git is None:
        checks.append(("warn", "git", "没装 git：/commit、/review 与 !git 用不了"))
    else:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=workdir, capture_output=True,
                                text=True, check=False).stdout.strip() == "true"  # fmt: skip
        checks.append(("ok", "git", f"{git}{'，这里是 git 仓库' if inside else '，这个目录不是 git 仓库'}"))

    loaded = load_memories(a.settings)
    total = sum(estimate_tokens(block) for _, block in loaded)
    names = "、".join(p.name if p.parent == a.settings.workdir else str(p) for p, _ in loaded)
    if not loaded:
        checks.append(("ok", "说明文件", "还没有（/init 生成 AGENTS.md）"))
    else:
        level = "warn" if total > MEMORY_WARN_TOKENS else "ok"
        hint = "，偏大：/memory 看每份多大" if level == "warn" else ""
        checks.append((level, "说明文件", f"{len(loaded)} 份，约 {total} token（{names}）{hint}"))
    checks.append(("ok", "快速记忆写到", str(memory_files(a.settings)["project"])))

    try:
        s = repl.agent.settings
        user_hooks, project_hooks = load_hooks(
            home, workdir, claude_user=s.claude_user_config, skip=s.claude_hooks_skip
        )
        user_mcp, project_mcp = load_servers(home, workdir)
        hook_trust = "已信任" if is_trusted(home, workdir, project_hooks) else "未信任（交互模式里确认一次）"
        mcp_trust = "已信任" if mcp_is_trusted(home, workdir, project_mcp) else "未信任"
        checks.append(("ok", "钩子", f"用户 {len(user_hooks)} 个、项目 {len(project_hooks)} 个"
                                     + (f"（项目钩子{hook_trust}）" if project_hooks else "")))  # fmt: skip
        failed = [n for n, ok, _ in repl.mcp_status if not ok]
        mcp_status: Status = "warn" if failed else "ok"
        checks.append((mcp_status, "MCP", f"用户 {len(user_mcp)} 个、项目 {len(project_mcp)} 个"
                                          + (f"（项目服务{mcp_trust}）" if project_mcp else "")
                                          + (f"；连接失败：{', '.join(failed)}" if failed else "")))  # fmt: skip
    except AgentError as exc:
        checks.append(("fail", "钩子 / MCP 配置", str(exc)))

    sessions = home / "sessions"
    try:
        sessions.mkdir(parents=True, exist_ok=True)
        probe = sessions / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("ok", "会话目录", f"{sessions} 可写"))
    except OSError as exc:
        checks.append(("fail", "会话目录", f"{sessions} 写不进去：{exc}"))

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    checks.append(("ok", "编辑器", f"{editor}（Ctrl+G、/memory edit 用）" if editor else "没设 EDITOR，用记事本"))
    compat = repl.compat_items()
    checks.append(
        ("ok", "兼容配置", "、".join(compat) if compat else "没有（项目里没有 AGENTS.md、CLAUDE.md、.claude/ 等）")
    )
    if sys.platform == "win32":
        from heng_agent.ui import winkeys

        on = winkeys.enable_shift_enter()
        note = "能换行" if on else "换行补丁没打上（先用 Ctrl+J 换行；/terminal-setup 可让终端代劳）"
        checks.append(("ok" if on else "warn", "Shift+Enter", note))
    return checks
