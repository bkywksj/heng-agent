"""第一次运行的引导：选服务商、填密钥、测一下连接、告诉用户怎么开始用。目标是新用户 3 分钟内用起来。

以前没有引导：没有配置文件时默认连本机的 heng serve（127.0.0.1:8765），新用户装好一启动看着一切正常，
一发消息就「连接被拒绝」，不知道下一步该干什么。

很多人在别的工具里已经配过密钥，先自动检测、能一键用上：
- 环境变量里的 DEEPSEEK_API_KEY、ANTHROPIC_API_KEY……（预设服务商各自的 api_key_env）；
- Claude Code 里配的服务（~/.claude/settings.json 的 env，或当前环境变量里的 ANTHROPIC_BASE_URL 与
  ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY）——国内用中转站的人大多是这样配的；
- Codex CLI 配的服务商（~/.codex/config.toml 的 model_providers：base_url + env_key 指的环境变量），
  以及 `codex login --api-key` 存下的 OpenAI 密钥（~/.codex/auth.json 的 OPENAI_API_KEY）。
  **不碰 ChatGPT 登录**（auth.json 的 tokens）：订阅账号的登录令牌只能给官方客户端用，第三方拿来调接口违反服务条款。
都没有就走 /config provider 的向导。任何一步按 ESC 都可以跳过，之后随时 /config provider。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from rich.markup import escape
from rich.panel import Panel

from heng_agent import configfile
from heng_agent.core.compat import claude_home, codex_home
from heng_agent.core.errors import AgentError
from heng_agent.core.types import TextDelta
from heng_agent.model.providers import PRESETS, Profile, config_path
from heng_agent.ui.select import Option

if TYPE_CHECKING:
    from heng_agent.ui.repl import Repl

TEST_PROMPT = "这是一次连接测试，请只回复两个字：好的"


@dataclass(frozen=True)
class Found:
    """检测到的现成配置。"""

    kind: str  # env：环境变量里的预设服务商密钥；claude：Claude Code 里配的服务；codex：Codex 配的服务商
    label: str
    source: str  # 从哪找到的（给用户看，让人放心）
    preset: str = ""
    base_url: str = ""
    token: str = ""
    model: str = ""
    auth: str = "auto"  # ANTHROPIC_AUTH_TOKEN 是 Bearer 认证，ANTHROPIC_API_KEY 是 x-api-key（和 Claude Code 一致）
    protocol: str = "anthropic"  # Codex 的服务商是 OpenAI 协议
    name: str = "relay"  # 存进配置用的配置档名


def needs_onboarding(home: Path) -> bool:
    """还没有用户配置文件 = 第一次用。调用方用环境变量给了服务商（HENG_AGENT_BASE_URL，如智码启动时）就不用引导。"""
    return not config_path(home).exists() and not os.environ.get("HENG_AGENT_BASE_URL", "").strip()


def _claude_env(settings_file: Path) -> dict[str, str]:
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _codex_found(codex_dir: Path, environ: Mapping[str, str]) -> list[Found]:
    """Codex 配置里用 API key 的服务商。密钥只认 env_key 指的环境变量、experimental_bearer_token 与
    auth.json 的 OPENAI_API_KEY；ChatGPT 登录的 tokens 一概不读（见模块说明）。本机服务（Ollama 等）不要密钥。"""
    config = codex_dir / "config.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    source = f"Codex 配置（{config}）"
    profiles = [p for p in (data.get("profiles") or {}).values() if isinstance(p, dict)]

    def model_for(provider: str) -> str:
        if data.get("model_provider") == provider and isinstance(data.get("model"), str):
            return data["model"]
        return next((p["model"] for p in profiles if p.get("model_provider") == provider
                     and isinstance(p.get("model"), str)), "")  # fmt: skip

    found: list[Found] = []
    providers = data.get("model_providers") or {}
    for key, entry in providers.items() if isinstance(providers, dict) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get("base_url"), str):
            continue
        base = entry["base_url"].strip().rstrip("/")
        env_key = entry.get("env_key")
        token = (environ.get(env_key, "") if isinstance(env_key, str) else "").strip()
        token = token or str(entry.get("experimental_bearer_token") or "").strip()
        host = urlparse(base).hostname or base
        if not base or (not token and host not in LOCAL_HOSTS):
            continue  # 要密钥却没找到（环境变量没设）：用不了，不列出来
        label = str(entry.get("name") or key)
        found.append(Found("codex", f"Codex 用的服务：{label}（{host}）", source, base_url=base, token=token,
                           model=model_for(key), auth="bearer", protocol="openai",
                           name="codex-" + (re.sub(r"[^A-Za-z0-9_-]+", "-", key).strip("-") or "relay")))  # fmt: skip
    try:
        auth = json.loads((codex_dir / "auth.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        auth = {}
    api_key = auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None
    if isinstance(api_key, str) and api_key.strip():  # codex login --api-key 存的是普通 API key，可以用
        found.append(Found("codex", "Codex 登录用的 OpenAI API key", f"Codex 登录信息（{codex_dir / 'auth.json'}）",
                           base_url="https://api.openai.com/v1", token=api_key.strip(), model=model_for("openai"),
                           auth="bearer", protocol="openai", name="codex-openai"))  # fmt: skip
    return found


def detect(environ: Mapping[str, str], claude_settings: Path, codex_dir: Path | None = None) -> list[Found]:
    """电脑上现成能用的配置：先 Claude Code 的服务（多是中转站，最常见），再 Codex 的，再环境变量里各家的密钥。"""
    found: list[Found] = []
    for source, env in ((f"Claude Code 配置（{claude_settings}）", _claude_env(claude_settings)),
                        ("当前的环境变量", dict(environ))):  # fmt: skip
        base = env.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
        bearer = env.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        token = bearer or env.get("ANTHROPIC_API_KEY", "").strip()
        if base and token and not any(f.kind == "claude" and f.base_url == base for f in found):
            host = urlparse(base).netloc or base
            found.append(Found("claude", f"Claude Code 用的服务：{host}", source, base_url=base, token=token,
                               model=env.get("ANTHROPIC_MODEL", "").strip(),
                               auth="bearer" if bearer else "x-api-key"))  # fmt: skip
    if codex_dir is not None:
        found += [f for f in _codex_found(codex_dir, environ) if not any(g.base_url == f.base_url for g in found)]
    for name, preset in PRESETS.items():
        if name != "heng" and preset.api_key_env and environ.get(preset.api_key_env):
            found.append(Found("env", preset.note or name, f"环境变量 {preset.api_key_env}", preset=name))
    return found


def mask(token: str) -> str:
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "…"


def explain(error: str) -> str:
    """连接测试失败的原因，说成用户能处理的话。"""
    low = error.lower()
    if any(k in low for k in ("401", "403", "unauthorized", "invalid api key", "authentication", "permission")):
        return "密钥不对或没有权限：检查密钥有没有复制完整、是不是这家服务商的"
    if any(k in low for k in ("402", "insufficient", "balance", "余额", "quota", "billing")):
        return "账户余额不足或额度用完了：去服务商后台充值或换一个密钥"
    if any(k in low for k in ("404", "not found", "model")):
        return "模型名或地址可能不对：检查地址、换一个模型"
    if any(k in low for k in ("连不上", "connect", "timed out", "timeout", "name resolution", "refused")):
        return "连不上服务：检查地址与网络（国外服务可能需要代理）"
    return "见上面的报错"


def connection_test(repl: Repl) -> tuple[bool, str]:
    """真发一句话：通了返回 (True, 模型回复)，不通返回 (False, 报错)。"""
    message = [{"role": "user", "content": [{"type": "text", "text": TEST_PROMPT}]}]
    reply: list[str] = []
    try:
        for event in repl.agent.client.stream("", message, []):
            if isinstance(event, TextDelta):
                reply.append(event.text)
    except AgentError as exc:
        return False, str(exc)
    return True, "".join(reply).strip() or "（空回复，但连接是通的）"


def _save_found(repl: Repl, found: Found, cmd: Any) -> None:
    """把检测到的配置写进用户配置并设为默认。环境变量里的密钥不抄进配置文件（它本来就在环境变量里）。"""
    if found.kind == "env":
        name, entry = found.preset, {"preset": found.preset}
    else:
        name = found.name
        entry = {"protocol": found.protocol, "base_url": found.base_url, "auth": found.auth}
        if found.token:  # 本机服务（Ollama 等）不要密钥
            entry["api_key"] = found.token
        probe = Profile(protocol=found.protocol, base_url=found.base_url, model=found.model or "?",
                        api_key=found.token or None, auth=found.auth)  # type: ignore[arg-type]  # fmt: skip
        entry["model"] = found.model or cmd._pick_model(probe)
        if not entry["model"] or entry["model"] == "?":
            raise AgentError("没有选模型，什么都没保存")
    path = configfile.config_file(repl.agent.settings.home, repl.agent.settings.workdir, False)

    def change(data: dict[str, Any]) -> None:
        data.setdefault("profiles", {})[name] = entry
        data["default"] = name

    configfile.update(path, change, lambda: configfile.validate_all(repl.agent.settings.home,
                                                                     repl.agent.settings.workdir))  # fmt: skip
    repl.console.print(f"[green]✓ 已保存并设为默认：{escape(name)}[/]")
    cmd.apply(switch_to_default=True)


def _configure(repl: Repl) -> bool:
    """选一种方式配好服务商；返回是否配好了。ESC / 选「以后再说」返回 False。"""
    from heng_agent.ui.config_cmd import ConfigCommand

    cmd = ConfigCommand(repl)
    found = detect(os.environ, claude_home() / "settings.json", codex_home())
    if found:
        repl.console.print("在你的电脑上找到了现成的配置，可以直接用：")
    options = [Option(str(i), f.label, detail=f"{f.source}" + (f" · 密钥 {mask(f.token)}" if f.token else ""))
               for i, f in enumerate(found)]  # fmt: skip
    options += [Option("manual", "手动配置", detail="选服务商（DeepSeek、Kimi、通义、GLM、Claude……）、填密钥"),
                Option("later", "以后再说", detail="之后随时输入 /config provider")]  # fmt: skip
    choice = cmd._select("怎么配置模型服务？", options)
    if choice == "later":
        return False
    if choice == "manual":
        cmd.provider_wizard()
    else:
        _save_found(repl, found[int(choice)], cmd)
    return True


def run(repl: Repl) -> None:
    """第一次运行的完整引导。"""
    c = repl.console
    c.print(Panel("第一次使用 heng-agent：先配一个模型服务（大约 1 分钟），配好就能开始干活。\n"
                  "[dim]方向键选、Enter 确定；任何一步按 ESC 跳过，之后随时输入 /config provider 再配[/]",
                  border_style="cyan", padding=(0, 2)))  # fmt: skip
    while True:
        try:
            if not _configure(repl):
                c.print("[dim]好的，之后输入 /config provider 配置服务商。没配好之前发消息会连不上模型。[/]")
                return
        except KeyboardInterrupt:
            c.print("[dim]已跳过。之后输入 /config provider 配置服务商。[/]")
            return
        except AgentError as exc:
            c.print(f"[red]{escape(str(exc))}[/]")
            continue
        c.print("[dim]测试一下连接……[/]")
        ok, text = connection_test(repl)
        if ok:
            c.print(f"[green]✓ 连接成功[/]  [dim]模型回复：{escape(text[:60])}[/]")
            break
        c.print(f"[red]✗ 连接失败：{escape(text[:300])}[/]\n[yellow]{explain(text)}[/]")
        from heng_agent.ui.config_cmd import ConfigCommand

        again = ConfigCommand(repl)._select("怎么办？", [Option("retry", "重新配置"),
                                                       Option("keep", "先这样，之后再改")])  # fmt: skip
        if again != "retry":
            c.print("[dim]之后输入 /config provider 修改；/doctor 可以检查连接。[/]")
            return
    c.print(Panel(
        "[bold]开始用：[/]直接用中文说要做什么，比如「给登录接口加上验证码」「这个报错是什么原因」\n"
        "· 第一次在一个项目里用，先输入 [cyan]/init[/]：让 AI 熟悉项目，以后每次对话都会读\n"
        "· [cyan]@文件名[/] 附上文件内容；[cyan]!命令[/] 直接执行命令\n"
        "· [cyan]Shift+Tab[/] 切换权限模式（默认改文件、跑命令前会问你）\n"
        "· [cyan]/help[/] 看全部命令；[cyan]Esc[/] 中断 AI；[cyan]Shift+Enter[/] 换行",
        border_style="green", padding=(0, 2)))  # fmt: skip
