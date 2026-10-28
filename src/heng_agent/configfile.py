"""配置文件读写：/config 命令与启动时读取 settings 段共用。

配置文件（用户 ~/.heng-agent/config.json、项目 .heng-agent/config.json）里的段：
- default / profiles：服务商（model/providers.py）；
- settings：启动默认值（权限模式、每问最多轮数……），命令行参数优先；
- hooks、mcp_servers：钩子与 MCP（hooks.py、mcp.py）。

键用点号路径（`profiles.yongye.model`）。值按 JSON 解析（数字、true/false、对象），解析不了就当字符串。
每次修改先写入、再整体校验一遍（服务商配置档、settings 字段），不通过就恢复原文件并报错。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from heng_agent.core.compat import claude_home, convert_claude_rule
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError

#: settings 段里允许写的字段（都是 Settings 的字段）
SETTINGS_KEYS = (
    "permission_mode", "max_tokens", "max_rounds", "bash_timeout", "context_window", "compact_ratio", "image_preview",
    "notify_after", "mcp_tool_search", "thinking", "thinking_budget", "claude_user_config", "claude_hooks_skip",
    "update_check", "proxy", "status_line", "effort", "prompt_suggestions",
)  # fmt: skip
#: 只能写在用户配置里的设置：项目配置跟着仓库走，不能让别人的仓库替你决定请求经哪个代理发出去（读配置档时会报错说明）
#: 状态栏命令同理：写在项目配置里，打开别人的仓库就会执行它指定的命令
USER_ONLY_SETTINGS = ("proxy", "status_line")
SECRET_KEYS = ("api_key",)


def config_file(home: Path, workdir: Path, project: bool) -> Path:
    return workdir / ".heng-agent" / "config.json" if project else home / "config.json"


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise AgentError(f"{path} 最外层必须是对象")
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def get_path(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise AgentError(f"配置里没有 {key}")
        node = node[part]
    return node


def set_path(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise AgentError(f"{part} 不是对象，没法往里面写 {key}")
        node = child
    node[parts[-1]] = value


def unset_path(data: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None  # type: ignore[assignment]
        if not isinstance(node, dict):
            raise AgentError(f"配置里没有 {key}")
    if parts[-1] not in node:
        raise AgentError(f"配置里没有 {key}")
    del node[parts[-1]]


def mask(value: Any) -> Any:
    """显示用：密钥只留头尾。递归处理对象。"""
    if isinstance(value, dict):
        return {k: (_mask_secret(v) if k in SECRET_KEYS else mask(v)) for k, v in value.items()}
    return value


def _mask_secret(value: Any) -> str:
    text = str(value)
    return text[:3] + "…" + text[-4:] if len(text) > 10 else "***"


def settings_overrides(home: Path, workdir: Path) -> dict[str, Any]:
    """用户与项目配置的 settings 段合并（项目覆盖用户），只取允许的字段。

    不认识的字段忽略（启动时由 unknown_settings 提示），不报错：新版写进配置的字段，旧版不认识也要照样能启动
    （以前一报错，开发版加一个设置项，正式版就启动不了）。
    用 /config set 改配置时仍由 validate_all 严格检查、抓打错的字段名。
    """
    merged: dict[str, Any] = {}
    for project, path in ((False, config_file(home, workdir, False)), (True, config_file(home, workdir, True))):
        section = load(path).get("settings") or {}
        if not isinstance(section, dict):
            raise AgentError(f"{path} 的 settings 必须是对象")
        allowed = [k for k in SETTINGS_KEYS if not (project and k in USER_ONLY_SETTINGS)]
        merged.update({k: v for k, v in section.items() if k in allowed})
    return merged


def unknown_settings(home: Path, workdir: Path) -> list[str]:
    """配置的 settings 段里这个版本不认识的字段（可能是新版写的，也可能是打错了）。"""
    found: list[str] = []
    for path in (config_file(home, workdir, False), config_file(home, workdir, True)):
        section = load(path).get("settings") or {}
        if isinstance(section, dict):
            found += [k for k in section if k not in SETTINGS_KEYS and k not in found]
    return found


def permission_rules(
    home: Path, workdir: Path, *, claude_user: bool = True
) -> tuple[list[str], list[str], list[str], list[str]]:
    """配置的 permissions 段：返回 (允许规则, 拒绝规则, 被忽略的项目允许规则, 对应不上的 Claude 规则)。

    项目配置只能加拒绝规则：允许规则若能写在项目里，一个恶意仓库在 .heng-agent/config.json 里写
    `bash(*)` 就悄悄给自己放开了一切命令。用户配置两种都能写。

    也读 Claude Code 的 settings.json（~/.claude/ 是用户级；项目 .claude/settings.json、settings.local.json
    是项目级，规矩同上），规则翻成 heng-agent 的写法（core.compat.convert_claude_rule）。
    它的 ask 规则与 defaultMode 不跟：权限模式由 heng-agent 自己定，免得外面的配置悄悄放宽。
    """
    allow: list[str] = []
    deny: list[str] = []
    ignored: list[str] = []
    unsupported: list[str] = []
    sources: list[tuple[Path, bool, bool]] = []  # (文件, 是不是项目级, 是不是 Claude Code 的)
    if claude_user:
        sources.append((claude_home() / "settings.json", False, True))
    sources += [(config_file(home, workdir, False), False, False), (config_file(home, workdir, True), True, False),
                (workdir / ".claude" / "settings.json", True, True),
                (workdir / ".claude" / "settings.local.json", True, True)]  # fmt: skip
    for path, is_project, from_claude in sources:
        try:
            section = load(path).get("permissions") or {}
        except AgentError:
            if from_claude:
                continue  # Claude Code 的配置写坏了不该拦住 heng-agent 启动
            raise
        if not isinstance(section, dict):
            if from_claude:
                continue
            raise AgentError(f'{path} 的 permissions 必须是对象：{{"allow": [...], "deny": [...]}}')
        for key in ("allow", "deny"):
            rules = section.get(key) or []
            if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
                if from_claude:
                    continue
                raise AgentError(f"{path} 的 permissions.{key} 必须是字符串列表")
            if from_claude:
                converted: list[str] = []
                for rule in rules:
                    mapped = convert_claude_rule(rule)
                    if mapped is None:
                        unsupported.append(rule)
                    else:
                        converted += mapped
                rules = converted
            if key == "allow" and is_project:
                ignored += rules
            else:
                (allow if key == "allow" else deny).extend(rules)
    return allow, deny, ignored, unsupported


def validate_all(home: Path, workdir: Path) -> None:
    """两层配置合起来整体检查：服务商配置档（含项目配置不许写密钥）与 settings 段。"""
    from heng_agent.model.providers import load_profiles

    load_profiles(home, workdir)
    unknown = unknown_settings(home, workdir)
    if unknown:  # 改配置时严格：多半是打错了字段名
        raise AgentError(f"settings 里有不认识的字段 {unknown}，可用：{', '.join(SETTINGS_KEYS)}")
    Settings(home=home, workdir=workdir, **settings_overrides(home, workdir))
    permission_rules(home, workdir)


def update(path: Path, change: Callable[[dict[str, Any]], None], validate: Callable[[], None]) -> None:
    """改配置：读 → 改 → 写 → 校验，校验失败恢复原文件并把原因抛出去。"""
    original = path.read_bytes() if path.is_file() else None
    data = load(path)
    change(data)
    save(path, data)
    try:
        validate()  # 由调用方校验：它知道用户目录与工作目录，能把两层配置合起来整体检查
    except Exception as exc:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
        raise AgentError(f"改动没有生效（已恢复原配置）：{exc}") from exc
