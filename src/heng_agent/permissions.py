"""权限：按工具类别（read / edit / exec）与权限模式决定直接执行、问用户还是拒绝；再叠加配置里的持久规则。"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Callable
from typing import Any, Literal

from heng_agent.core.config import PermissionMode
from heng_agent.tools.base import Tool

Decision = Literal["allow", "ask", "deny"]
#: 界面层实现的确认对话：once = 这次允许，always = 本会话一直允许这个工具，
#: persist = 以后都允许（规则写进配置），deny = 拒绝
Approver = Callable[[Tool, dict[str, Any]], Literal["once", "always", "persist", "deny"]]
#: bash 命令里的连接符、管道、重定向、命令替换、换行：出现就不适用允许规则
_SHELL_CONTROL = re.compile(r"&&|\|\||[;|`<>\n]|\$\(")
#: 把组合命令拆成一段段（拒绝规则逐段检查）
_SHELL_SPLIT = re.compile(r"&&|\|\||[;|\n]|\$\(|`")
FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})

#: 模式 × 工具类别 → 决定。plan 模式只读：改文件和跑命令一律拒绝；
#: net（读网页）除 auto 外都问：不改本地，但模型可能把代码内容拼进网址带出去；
#: mcp（外部工具）除 auto 外都问：不知道它会做什么（写数据库、发消息……），plan 模式也不例外；
#: auto 全部直接执行——只用于 CI、一次性沙箱等可信环境（路径越界仍由 tools/paths.py 拦住）
RULES: dict[str, dict[str, Decision]] = {
    "ask": {"read": "allow", "edit": "ask", "exec": "ask", "net": "ask", "mcp": "ask"},
    "accept-edits": {"read": "allow", "edit": "allow", "exec": "ask", "net": "ask", "mcp": "ask"},
    "plan": {"read": "allow", "edit": "deny", "exec": "deny", "net": "ask", "mcp": "ask"},
    "auto": {"read": "allow", "edit": "allow", "exec": "allow", "net": "allow", "mcp": "allow"},
}


class Rules:
    """持久的允许 / 拒绝规则（配置文件的 permissions 段）。规则写法：`工具名` 或 `工具名(匹配内容)`，可用 `*` 通配。

    匹配内容：bash 比命令，读写文件比路径，web_fetch 比网址；其余工具只比名字。例：
    `bash(git status*)`、`bash(npm test)`、`write_file(src/*)`、`mcp__calc__*`、`web_fetch(https://docs.python.org/*)`。
    拒绝优先于一切（auto 模式下也拒绝）；允许只能免去询问，plan 模式的只读约束仍然有效。
    """

    def __init__(self, allow: list[str] | None = None, deny: list[str] | None = None) -> None:
        self.allow = list(allow or [])
        self.deny = list(deny or [])

    @staticmethod
    def subject(tool: Tool, args: dict[str, Any]) -> str:
        return str(args.get("command") or args.get("path") or args.get("url") or "")

    @classmethod
    def matches(cls, rule: str, tool: Tool, args: dict[str, Any]) -> bool:
        name, _, rest = rule.strip().partition("(")
        if not fnmatch.fnmatchcase(tool.name, name.strip()):
            return False
        if not rest:
            return True  # 只写了工具名：这个工具的所有调用
        pattern = rest[:-1] if rest.endswith(")") else rest
        subject = cls.subject(tool, args)
        if fnmatch.fnmatchcase(subject, pattern):
            return True
        if tool.name in FILE_TOOLS:
            # 同一个文件有好几种写法：.env、./.env、E:\\项目\\.env——统一斜杠、去掉 ./，
            # 相对路径的规则也对绝对路径的结尾生效，否则换个写法就绕过了拒绝规则
            path = posixpath.normpath(subject.replace("\\", "/")).removeprefix("./")
            relative = not (pattern.startswith(("/", "*", "~")) or re.match(r"[A-Za-z]:", pattern))
            return fnmatch.fnmatchcase(path, pattern) or (relative and fnmatch.fnmatchcase(path, f"*/{pattern}"))
        return False

    @classmethod
    def denied(cls, rule: str, tool: Tool, args: dict[str, Any]) -> bool:
        """拒绝规则对组合命令的每一段都生效：`taskkill /IM node*` 也要拦住 `cd x && taskkill /IM node.exe`。"""
        if cls.matches(rule, tool, args):
            return True
        if tool.name != "bash":
            return False
        parts = [p.strip() for p in _SHELL_SPLIT.split(cls.subject(tool, args)) if p.strip()]
        return len(parts) > 1 and any(cls.matches(rule, tool, {**args, "command": p}) for p in parts)

    def decide(self, tool: Tool, args: dict[str, Any]) -> Decision | None:
        if any(self.denied(r, tool, args) for r in self.deny):
            return "deny"
        if tool.name == "bash" and _SHELL_CONTROL.search(self.subject(tool, args)):
            # 组合命令不适用允许规则：`bash(git status*)` 本意放行 git status，通配符却也对得上
            # `git status && rm -rf 重要目录`。含连接符、管道、重定向、命令替换的一律照常问（拒绝规则照常生效）
            return None
        if any(self.matches(r, tool, args) for r in self.allow):
            return "allow"
        return None

    @classmethod
    def suggest(cls, tool: Tool, args: dict[str, Any]) -> str:
        """确认框里「以后都允许」写进配置的规则：bash 只允许这一条命令（别一下放开所有命令），其余放开这个工具。"""
        if tool.name == "bash" and cls.subject(tool, args):
            return f"bash({cls.subject(tool, args)})"
        return tool.name


class Permissions:
    def __init__(self, mode: PermissionMode = "ask", rules: Rules | None = None) -> None:
        self.mode: PermissionMode = mode
        self.always: set[str] = set()  # 本会话里用户选了「一直允许」的工具名
        self.rules = rules or Rules()
        #: 用户在确认框里选了「以后都允许」：界面把规则写进配置文件（设了才写）
        self.on_persist: Callable[[str], None] | None = None

    def decide(self, tool: Tool, args: dict[str, Any] | None = None) -> Decision:
        args = args or {}
        mode_decision = RULES[self.mode][tool.kind]
        ruled = self.rules.decide(tool, args)
        if ruled == "deny" or mode_decision == "deny":  # 拒绝规则与 plan 的只读约束都盖过允许规则
            return "deny"
        if ruled == "allow" or (mode_decision == "ask" and tool.name in self.always):
            return "allow"
        return mode_decision

    def check(self, tool: Tool, args: dict[str, Any], approve: Approver) -> bool:
        """返回是否允许执行。需要确认时调界面的确认对话。"""
        decision = self.decide(tool, args)
        if decision == "allow":
            return True
        if decision == "deny":
            return False
        answer = approve(tool, args)
        if answer == "always":
            self.always.add(tool.name)
        elif answer == "persist":
            rule = Rules.suggest(tool, args)
            self.rules.allow.append(rule)
            if self.on_persist is not None:
                self.on_persist(rule)
        return answer in ("once", "always", "persist")
