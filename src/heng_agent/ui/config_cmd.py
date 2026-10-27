"""/config：在对话里查看与修改配置，改完立即生效（不用重启）。

    /config                      总览：默认服务商、所有配置档（密钥打码）、settings、钩子与 MCP
    /config provider             引导式添加 / 修改服务商：选预设或自定义 → 自动取模型列表让你挑 → 输密钥（不回显）
    /config set <键> <值>         改任意字段，如 profiles.yongye.model claude-opus-5、settings.permission_mode plan
    /config get <键>  /  /config unset <键>
    /config edit                 用记事本（或 $EDITOR）打开配置文件，保存关闭后自动检查并生效
    以上都可加 --project：写项目配置（.heng-agent/config.json，不许写密钥）

每次改动先校验（服务商配置档、settings 字段），不通过自动恢复原配置并说明原因。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.table import Table

from heng_agent import configfile
from heng_agent.core.errors import AgentError
from heng_agent.hooks import load_hooks
from heng_agent.mcp import load_servers
from heng_agent.model.client import list_models, make_client
from heng_agent.model.providers import PRESETS, Profile, load_profiles
from heng_agent.permissions import Rules
from heng_agent.ui.select import Option

if TYPE_CHECKING:
    from heng_agent.ui.repl import Repl

USAGE = """用法：
  /config                    查看全部配置
  /config provider           添加或修改服务商（引导式，自动取模型列表）
  /config set <键> <值>       例：/config set profiles.yongye.model claude-opus-5
                                  /config set settings.permission_mode accept-edits
                                  /config set default deepseek
  /config get <键>            /config unset <键>
  /config edit               用编辑器打开配置文件
  加 --project 改项目配置（不许写密钥）"""

RULES_USAGE = """规则写法：工具名 或 工具名(匹配内容)，可用 * 通配。bash 比命令、文件工具比路径、web_fetch 比网址。
  /permissions allow bash(npm test)        /permissions allow mcp__calc__*
  /permissions deny bash(git push*)        /permissions deny write_file(.env*) --project
  /permissions remove bash(npm test)"""


class ConfigCommand:
    def __init__(self, repl: Repl) -> None:
        self.repl = repl
        self.agent = repl.agent
        self.console = repl.console

    @property
    def home(self) -> Any:
        return self.agent.settings.home

    @property
    def workdir(self) -> Any:
        return self.agent.settings.workdir

    def run(self, rest: str) -> None:
        project = " --project" in f" {rest} "
        rest = f" {rest} ".replace(" --project ", " ").strip()
        sub, _, args = rest.partition(" ")
        args = args.strip()
        path = configfile.config_file(self.home, self.workdir, project)
        try:
            if sub in ("", "show"):
                self.show()
            elif sub == "get" and args:
                value = configfile.get_path(configfile.load(path), args)
                self.console.print(escape(json.dumps(configfile.mask({"v": value})["v"], ensure_ascii=False, indent=2)))
            elif sub == "set" and " " in args:
                key, _, raw = args.partition(" ")
                value = configfile.parse_value(raw.strip())
                self._change(path, lambda d: configfile.set_path(d, key, value), f"{key} = {raw.strip()}", key)
            elif sub == "unset" and args:
                self._change(path, lambda d: configfile.unset_path(d, args), f"已删除 {args}", args)
            elif sub == "edit":
                self.edit(path)
            elif sub == "provider":
                self.provider_wizard()
            else:
                self.console.print(escape(USAGE))
        except AgentError as exc:
            self.console.print(f"[red]{escape(str(exc))}[/]")

    # ---------- 改动与生效 ----------

    def _change(self, path: Any, change: Any, done: str, key: str) -> None:
        configfile.update(path, change, lambda: configfile.validate_all(self.home, self.workdir))
        self.console.print(f"[green]✓ {escape(done)}[/] [dim]（{escape(str(path))}）[/]")
        self.apply(switch_to_default=key == "default")

    def apply(self, switch_to_default: bool = False) -> None:
        """把配置的改动用到正在运行的会话上：当前服务商的配置档有变化就重建客户端；settings 立即生效。"""
        profiles, default = load_profiles(self.home, self.workdir)
        name = default if switch_to_default or self.repl.provider_name not in profiles else self.repl.provider_name
        current = getattr(self.agent.client, "profile", None)
        if profiles[name] != current or name != self.repl.provider_name:
            self.agent.client = make_client(profiles[name], self.agent.settings.max_tokens)
            self.repl.provider_name = name
            self.repl._hook_retry()
            self.console.print(f"[dim]当前服务商：{escape(name)} · {escape(profiles[name].model)}[/]")
        overrides = configfile.settings_overrides(self.home, self.workdir)
        if overrides:
            self.agent.settings = self.agent.settings.model_copy(update=overrides)
            if "permission_mode" in overrides:
                self.agent.permissions.mode = overrides["permission_mode"]

    # ---------- 总览 ----------

    def show(self) -> None:
        c = self.console
        user = configfile.config_file(self.home, self.workdir, False)
        proj = configfile.config_file(self.home, self.workdir, True)
        profiles, default = load_profiles(self.home, self.workdir)
        c.print(f"[bold]用户配置[/] {escape(str(user))}{'' if user.is_file() else ' [dim]（还没有）[/]'}")
        c.print(f"[bold]项目配置[/] {escape(str(proj))}{'' if proj.is_file() else ' [dim]（还没有）[/]'}")
        c.print(f"[bold]默认服务商[/] {escape(default)}    [bold]当前[/] {escape(self.repl.provider_name or default)}")
        table = Table(show_edge=False, header_style="bold", pad_edge=False, box=None, padding=(0, 2))
        for col in ("名字", "协议", "模型", "地址", "密钥"):
            table.add_column(col)
        for name, p in profiles.items():
            key = p.api_key and configfile.mask({"api_key": p.api_key})["api_key"]
            key_text = key or (f"{p.api_key_env}{' ✓' if p.resolve_key() else ' 未设置'}" if p.api_key_env else "—")
            mark = "[bold cyan]*[/]" if name == (self.repl.provider_name or default) else " "
            table.add_row(f"{mark}{escape(name)}", p.protocol, escape(p.model), escape(p.base_url), escape(key_text))
        c.print(table)
        settings = configfile.settings_overrides(self.home, self.workdir)
        shown = escape(json.dumps(settings, ensure_ascii=False)) if settings else "[dim]（都用默认值）[/]"
        c.print(f"[bold]settings[/] {shown}")
        s = self.agent.settings
        user_hooks, project_hooks = load_hooks(
            self.home, self.workdir, claude_user=s.claude_user_config, skip=s.claude_hooks_skip
        )
        user_mcp, project_mcp = load_servers(self.home, self.workdir)
        c.print(f"[bold]钩子[/] 用户 {len(user_hooks)} 个、项目 {len(project_hooks)} 个    "
                f"[bold]MCP[/] 用户 {len(user_mcp)} 个、项目 {len(project_mcp)} 个")  # fmt: skip
        c.print(f"[dim]{escape(USAGE)}[/]")

    # ---------- 编辑器 ----------

    def edit(self, path: Any) -> None:
        editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "vi")
        if not path.is_file():
            configfile.save(path, {})
        backup = path.read_bytes()
        self.console.print(f"[dim]已用 {escape(editor)} 打开 {escape(str(path))}，保存并关闭后自动检查……[/]")
        subprocess.run([editor, str(path)], check=False)
        try:
            configfile.validate_all(self.home, self.workdir)
        except AgentError as exc:
            path.write_bytes(backup)
            raise AgentError(f"改过的配置有问题，已恢复原样：{exc}") from exc
        self.console.print("[green]✓ 配置已更新[/]")
        self.apply()

    # ---------- 权限规则（/permissions） ----------

    def permissions(self, rest: str) -> None:
        """/permissions：查看；/permissions allow|deny 规则 [--project]；/permissions remove 规则 [--project]。"""
        project = " --project" in f" {rest} "
        rest = f" {rest} ".replace(" --project ", " ").strip()
        sub, _, rule = rest.partition(" ")
        rule = rule.strip()
        try:
            if sub in ("", "list"):
                self.show_rules()
            elif sub in ("allow", "deny") and rule:
                if sub == "allow" and project:
                    raise AgentError("项目配置只能加拒绝规则：允许规则写在项目里，别人的仓库就能给自己放开命令")
                self.add_rule(sub, rule, project)
                self.console.print(f"[green]✓ 已加{'允许' if sub == 'allow' else '拒绝'}规则 {escape(rule)}[/]")
            elif sub == "remove" and rule:
                self.remove_rule(rule, project)
                self.console.print(f"[green]✓ 已删除规则 {escape(rule)}[/]")
            else:
                self.console.print(escape(RULES_USAGE))
        except AgentError as exc:
            self.console.print(f"[red]{escape(str(exc))}[/]")

    def add_rule(self, kind: str, rule: str, project: bool = False) -> None:
        path = configfile.config_file(self.home, self.workdir, project)

        def change(data: dict[str, Any]) -> None:
            rules = data.setdefault("permissions", {}).setdefault(kind, [])
            if rule not in rules:
                rules.append(rule)

        configfile.update(path, change, lambda: configfile.validate_all(self.home, self.workdir))
        self.reload_rules()

    def remove_rule(self, rule: str, project: bool = False) -> None:
        path = configfile.config_file(self.home, self.workdir, project)
        found = False

        def change(data: dict[str, Any]) -> None:
            nonlocal found
            for kind in ("allow", "deny"):
                rules = (data.get("permissions") or {}).get(kind) or []
                if rule in rules:
                    rules.remove(rule)
                    found = True

        configfile.update(path, change, lambda: configfile.validate_all(self.home, self.workdir))
        if not found:
            raise AgentError(f"{'项目' if project else '用户'}配置里没有规则 {rule}")
        self.reload_rules()

    def reload_rules(self) -> None:
        allow, deny, _, _ = configfile.permission_rules(
            self.home, self.workdir, claude_user=self.agent.settings.claude_user_config
        )
        self.agent.permissions.rules = Rules(allow, deny)

    def show_rules(self) -> None:
        c = self.console
        allow, deny, ignored, unsupported = configfile.permission_rules(
            self.home, self.workdir, claude_user=self.agent.settings.claude_user_config
        )
        c.print("[bold]允许规则[/]（免确认；bash 组合命令不适用）")
        for r in allow or ["（无）"]:
            c.print(f"  [green]{escape(r)}[/]")
        c.print("[bold]拒绝规则[/]（优先于一切，auto 模式也拒绝）")
        for r in deny or ["（无）"]:
            c.print(f"  [red]{escape(r)}[/]")
        if ignored:
            c.print(f"[yellow]项目配置里的允许规则已忽略（只有用户配置能加允许）：{escape(', '.join(ignored))}[/]")
        if unsupported:
            c.print(
                f"[dim]Claude Code 配置里对应不上的规则（没有这类工具，已跳过）：{escape(', '.join(unsupported))}[/]"
            )
        always = sorted(self.agent.permissions.always)
        if always:
            c.print(f"[bold]本会话一直允许[/]（不存）：{escape(', '.join(always))}")
        c.print(f"[dim]{escape(RULES_USAGE)}[/]")

    # ---------- 存默认模型（/model 选完后用） ----------

    def save_model(self, name: str, model: str) -> None:
        """把 model 存成服务商 name 的默认模型（用户配置）。

        内置预设（deepseek 等）在用户配置里可能还没有条目：只写 model 会丢掉预设的地址与协议，
        所以同时写上 preset，表示「继承预设，只改模型」。
        """
        path = configfile.config_file(self.home, self.workdir, False)

        def change(data: dict[str, Any]) -> None:
            entry = data.setdefault("profiles", {}).setdefault(name, {})
            if name in PRESETS and "preset" not in entry and "protocol" not in entry:
                entry["preset"] = name
            entry["model"] = model

        configfile.update(path, change, lambda: configfile.validate_all(self.home, self.workdir))

    # ---------- 引导式服务商 ----------

    def provider_wizard(self) -> None:
        """引导式添加 / 修改服务商。

        选择题用方向键选，地址、名字、密钥才打字；任何一步按 ESC / Ctrl+C 取消、什么都不保存。
        """
        c, ask = self.console, self.repl._ask
        presets = list(PRESETS)
        c.print("[bold]添加或修改服务商[/]  [dim]方向键选、Enter 确定，任何一步按 ESC 取消（什么都不保存）[/]")
        options = [Option(n, n, detail=PRESETS[n].note) for n in presets]
        options.append(Option("custom", "自定义", detail="中转站、自建服务……"))
        choice = self._select("选服务商", options, default=len(options) - 1)
        if choice != "custom":
            preset: str | None = choice
            base = PRESETS[choice]
            entry: dict[str, Any] = {"preset": choice}
        else:
            preset = None
            protocol = self._select("协议", [
                Option("anthropic", "anthropic", detail="Claude 及大多数中转站"),
                Option("openai", "openai", detail="OpenAI 兼容接口"),
            ])  # fmt: skip
            url = ask("地址（如 https://cc.example.cn）> ").strip()
            if not url.startswith(("http://", "https://")):
                raise AgentError("地址要以 http:// 或 https:// 开头，什么都没保存")
            base = Profile(protocol=protocol, base_url=url, model="?")  # type: ignore[arg-type]
            entry = {"protocol": protocol, "base_url": url}
        name = ask(f"配置档名字（直接回车用 {preset or 'relay'}）> ").strip() or (preset or "relay")
        key = ask("密钥（输入不显示；直接回车 = 用环境变量或不需要密钥）> ", password=True).strip()
        if key:
            entry["api_key"] = key
        probe = base.model_copy(update={"api_key": key or base.api_key})
        model = self._pick_model(probe)
        if not model or model == "?":
            raise AgentError("没有选模型，什么都没保存")
        entry["model"] = model
        make_default = self._select("设为默认服务商吗？", [
            Option("yes", "设为默认（以后启动就用它）", "y"), Option("no", "先不设", "n"),
        ]) == "yes"  # fmt: skip
        path = configfile.config_file(self.home, self.workdir, False)  # 带密钥：只写用户配置

        def change(data: dict[str, Any]) -> None:
            data.setdefault("profiles", {})[name] = entry
            if make_default:
                data["default"] = name

        configfile.update(path, change, lambda: configfile.validate_all(self.home, self.workdir))
        c.print(f"[green]✓ 已保存服务商 {escape(name)}（{escape(model)}）[/]")
        self.repl.provider_name = name if make_default else self.repl.provider_name
        self.apply(switch_to_default=make_default)

    def _select(self, title: str, options: list[Option], default: int = 0) -> str:
        """方向键选一项。ESC 取消（抛 KeyboardInterrupt，由命令外层说「已取消」）；选择器用不了的终端退回输编号。"""
        picked = self.repl._choose(title, options, default=default)
        if picked is None:
            raise KeyboardInterrupt
        if picked is not self.repl.PLAIN:
            return str(picked)
        for i, opt in enumerate(options, 1):
            self.console.print(f"  [cyan]{i:>2}[/] {escape(opt.label):<14} [dim]{escape(opt.detail)}[/]")
        raw = self.repl._ask(f"{title}（编号，直接回车选 {default + 1}）> ").strip()
        if not raw:
            return options[default].value
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1].value
        match = next((o.value for o in options if raw in (o.value, o.key)), None)
        if match is None:
            raise AgentError(f"没有这一项：{raw}，什么都没保存")
        return match

    def _pick_model(self, probe: Profile) -> str:
        """向服务商取模型列表用方向键选（最后一项是「手动输入」）；取不到列表就直接输名字。"""
        c, ask = self.console, self.repl._ask
        c.print("[dim]正在向服务商取可用模型……[/]")
        try:
            models = list_models(probe)[:60]
        except AgentError as exc:
            c.print(f"[yellow]{escape(str(exc))}[/]")
            models = []
        if models:
            options = [Option(m, m, detail="当前" if m == probe.model else "") for m in models]
            options.append(Option("__manual__", "手动输入模型名……"))
            default = models.index(probe.model) if probe.model in models else 0
            picked = self._select("选模型", options, default=default)
            if picked != "__manual__":
                return picked
        default_name = "" if probe.model == "?" else probe.model
        hint = f"（直接回车用 {default_name}）" if default_name else ""
        return ask(f"模型名{hint} > ").strip() or default_name
