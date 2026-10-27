"""钩子：工具执行前后自动跑的命令（改完文件自动格式化、拦截危险命令……）。

配置写在 config.json（用户 ~/.heng-agent/ 与项目 .heng-agent/ 都可以）：

    "hooks": {
      "pre_tool":  [{"match": "bash", "command": "python scripts/check_cmd.py"}],
      "post_tool": [{"match": "edit_file|write_file", "command": "ruff format \\"$HENG_FILE\\""}]
    }

- match：按工具名的正则（整名匹配）；
- 钩子在工作目录用 bash 执行，环境变量：HENG_TOOL（工具名）、HENG_TOOL_INPUT（参数 JSON）、
  HENG_FILE（参数里有 path 时的绝对路径）；
- pre_tool 退出码 2 = 拦下这次调用，钩子输出作为原因交给模型；其他非零退出码只提示、不拦；
- post_tool 的输出附在工具结果后面交给模型（格式化改了什么、检查报了什么错，模型马上看得到）。

信任：用户目录的钩子是用户自己写的，直接生效。**项目里的钩子可能来自别人的仓库**——克隆一个恶意项目就能借钩子
执行任意命令——所以必须用户确认信任才运行；确认记录的是钩子内容的指纹，内容一改就要重新确认。

**兼容 Claude Code 的钩子**（~/.claude/settings.json 与项目 .claude/settings.json、settings.local.json 的 hooks）：
照它的约定跑——事件名、工具名（Bash、Edit……）、参数名（file_path）翻成 Claude 的，参数从标准输入以 JSON 传入，
设 CLAUDE_PROJECT_DIR；认退出码 2（标准错误是原因），也认标准输出的 JSON 决定（decision: block、
permissionDecision: deny、additionalContext……）——实际的安全钩子大多是退出码 0 + JSON 决定，只看退出码会全部失效。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from heng_agent.core import trust as trust_store
from heng_agent.core.compat import CLAUDE_EVENTS, HENG_TO_CLAUDE, claude_home
from heng_agent.core.errors import AgentError
from heng_agent.tools.shell import IS_WINDOWS, clip, kill_tree, run_command, shell_argv

Event = Literal["pre_tool", "post_tool", "user_prompt_submit", "stop", "session_start"]
TOOL_EVENTS = ("pre_tool", "post_tool")
#: 不针对工具的事件：match 可省；退出码 2 的含义各不同（见 HookRunner.run_event）
SESSION_EVENTS = ("user_prompt_submit", "stop", "session_start")
BLOCK_EXIT_CODE = 2
MAX_HOOK_OUTPUT = 4000


@dataclass(frozen=True)
class Hook:
    event: Event
    match: str
    command: str
    timeout: float = 60.0
    source: str = "user"  # user / project
    style: str = "heng"  # heng：本工具的约定；claude：Claude Code 的约定（标准输入 JSON、Claude 工具名）

    def matches(self, tool_name: str) -> bool:
        name = HENG_TO_CLAUDE.get(tool_name, tool_name) if self.style == "claude" else tool_name
        return re.fullmatch(self.match, name) is not None


@dataclass(frozen=True)
class HookOutcome:
    blocked: bool
    message: str  # 拦截原因，或附加给模型的输出；空串表示没什么要说


def _read_hooks(path: Path, source: str) -> list[Hook]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} 不是合法 JSON：{exc}") from exc
    hooks = []
    for event in (*TOOL_EVENTS, *SESSION_EVENTS):
        for item in (data.get("hooks") or {}).get(event) or []:
            try:
                match = item.get("match", ".*") if event in SESSION_EVENTS else item["match"]
                re.compile(match)
                hooks.append(Hook(event, match, item["command"], float(item.get("timeout", 60)), source))
            except (KeyError, TypeError, AttributeError, re.error) as exc:
                raise AgentError(
                    f"{path} 的 hooks.{event} 有一项写错了（要有 command，工具事件还要 match）：{exc}"
                ) from exc
    return hooks


def _read_claude_hooks(path: Path, source: str) -> list[Hook]:
    """Claude Code settings.json 的 hooks：{事件: [{matcher, hooks: [{type: command, command, timeout}]}]}。
    写坏了、不支持的事件与类型都跳过：Claude Code 的配置不该拦住 heng-agent 启动。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    hooks: list[Hook] = []
    section = data.get("hooks") if isinstance(data, dict) else None
    for claude_event, groups in (section or {}).items():
        event = CLAUDE_EVENTS.get(claude_event)
        if event is None or not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = str(group.get("matcher") or "")
            pattern = ".*" if matcher in ("", "*") else matcher
            try:
                re.compile(pattern)
            except re.error:
                continue
            for item in group.get("hooks") or []:
                if isinstance(item, dict) and item.get("type", "command") == "command" and item.get("command"):
                    timeout = float(item.get("timeout") or 60)
                    hooks.append(Hook(event, pattern, str(item["command"]), timeout, source, "claude"))  # type: ignore[arg-type]
    return hooks


def load_hooks(
    home: Path, workdir: Path, *, claude_user: bool = True, skip: list[str] | tuple[str, ...] = ()
) -> tuple[list[Hook], list[Hook]]:
    """返回 (用户钩子, 项目钩子)。也读 Claude Code 的：~/.claude/settings.json 算用户的（claude_user 可关），
    项目 .claude/settings.json 与 settings.local.json 算项目的（要用户确认信任才跑）。
    skip：命令里含其中任一段文字的 Claude 钩子不跑——有些是专给 Claude Code 自己用的集成（如智码的会话记录器，
    在 heng-agent 里跑会把 heng 的会话当成 Claude 的记进去）。"""
    user = _read_hooks(home / "config.json", "user")
    if claude_user:
        user += _read_claude_hooks(claude_home() / "settings.json", "user")
    project = _read_hooks(workdir / ".heng-agent" / "config.json", "project")
    for name in ("settings.json", "settings.local.json"):
        project += _read_claude_hooks(workdir / ".claude" / name, "project")

    def keep(hook: Hook) -> bool:
        return hook.style != "claude" or not any(s and s in hook.command for s in skip)

    return [h for h in user if keep(h)], [h for h in project if keep(h)]


def fingerprint(hooks: list[Hook]) -> str:
    raw = json.dumps([(h.event, h.match, h.command, h.timeout, h.style) for h in hooks], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_trusted(home: Path, workdir: Path, hooks: list[Hook]) -> bool:
    return not hooks or trust_store.is_trusted(home, str(workdir.resolve()), fingerprint(hooks))


def trust(home: Path, workdir: Path, hooks: list[Hook]) -> None:
    trust_store.trust(home, str(workdir.resolve()), fingerprint(hooks))


#: heng-agent 的权限模式 → Claude Code 的（钩子输入里的 permission_mode）
_CLAUDE_MODES = {"ask": "default", "accept-edits": "acceptEdits", "plan": "plan", "auto": "bypassPermissions"}
#: heng-agent 的事件 → Claude Code 的事件名（钩子输入里的 hook_event_name）
_CLAUDE_EVENT_NAMES = {v: k for k, v in CLAUDE_EVENTS.items()}
_FILE_TOOLS = ("read_file", "write_file", "edit_file")


def run_with_stdin(
    command: str, cwd: Path, timeout: float, env: dict[str, str], data: str
) -> tuple[int | None, str, str]:
    """跑命令并从标准输入喂 data，返回 (退出码, 标准输出, 标准错误)；超时返回 (None, …)。
    Claude 钩子要分开读两路输出：退出码 2 时标准错误是原因，退出码 0 时标准输出可能是 JSON 决定。"""
    extra: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        shell_argv(command), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, **extra,
    )  # fmt: skip
    try:
        out, err = proc.communicate(data.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        return None, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except KeyboardInterrupt:
        kill_tree(proc)
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _json_output(text: str) -> dict[str, Any]:
    """钩子标准输出里的 JSON 决定；不是 JSON 就当没有。"""
    text = text.strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class HookRunner:
    def __init__(self, hooks: list[Hook], workdir: Path, info: Callable[[], dict[str, Any]] | None = None) -> None:
        """info()：Claude 钩子输入里的会话信息（session_id、transcript_path、permission_mode），由智能体提供。"""
        self.hooks = hooks
        self.workdir = workdir
        self.info = info or (lambda: {})

    # ---------- Claude Code 的钩子 ----------

    def _claude_env(self) -> dict[str, str]:
        return {**os.environ, "PYTHONIOENCODING": "utf-8", "CLAUDE_PROJECT_DIR": str(self.workdir)}

    def _claude_payload(self, event: Event, **extra: Any) -> str:
        info = self.info()
        mode = _CLAUDE_MODES.get(str(info.get("permission_mode", "ask")), "default")
        payload = {
            "session_id": info.get("session_id", ""), "transcript_path": info.get("transcript_path", ""),
            "cwd": str(self.workdir), "permission_mode": mode, "hook_event_name": _CLAUDE_EVENT_NAMES[event], **extra,
        }  # fmt: skip
        return json.dumps(payload, ensure_ascii=False)

    def _claude_tool_input(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """参数翻成 Claude 的写法：读写文件的 path → 绝对路径的 file_path（Claude 的钩子脚本按 file_path 取）。"""
        converted = dict(args)
        if tool_name in _FILE_TOOLS and isinstance(args.get("path"), str):
            converted["file_path"] = str((self.workdir / args["path"]).resolve())
            converted.pop("path", None)
        return converted

    def _call_claude(self, hook: Hook, event: Event, **extra: Any) -> tuple[int | None, str, str]:
        payload = self._claude_payload(event, **extra)
        return run_with_stdin(hook.command, self.workdir, hook.timeout, self._claude_env(), payload)

    def _run_claude_tool(
        self, hook: Hook, event: Event, tool_name: str, args: dict[str, Any], result: str | None
    ) -> tuple[bool, str]:
        """一个 Claude 的 PreToolUse / PostToolUse 钩子 → (拦不拦, 给模型的话)。"""
        extra: dict[str, Any] = {
            "tool_name": HENG_TO_CLAUDE.get(tool_name, tool_name),
            "tool_input": self._claude_tool_input(tool_name, args),
        }
        if event == "post_tool":
            extra["tool_response"] = {"content": result or ""}
        code, out, err = self._call_claude(hook, event, **extra)
        if code is None:
            return False, f"[钩子超时：{hook.command}]"
        if code == BLOCK_EXIT_CODE:  # PreToolUse：拦下，标准错误是原因；PostToolUse：标准错误交给模型
            return event == "pre_tool", clip((err or out).strip(), MAX_HOOK_OUTPUT) or "钩子没有说明原因"
        if code != 0:
            return False, f"[钩子 {hook.command} 退出码 {code}]\n{clip(err.strip(), MAX_HOOK_OUTPUT)}".rstrip()
        data = _json_output(out)
        spec = data.get("hookSpecificOutput") or {}
        if event == "pre_tool":
            denied = spec.get("permissionDecision") == "deny" or data.get("decision") == "block"
            if denied or data.get("continue") is False:
                reason = spec.get("permissionDecisionReason") or data.get("reason") or data.get("stopReason")
                return True, str(reason or "钩子拒绝了这次调用")
            return False, ""
        notes = [str(data["reason"])] if data.get("decision") == "block" and data.get("reason") else []
        if spec.get("additionalContext"):
            notes.append(str(spec["additionalContext"]))
        return False, "\n".join(notes)

    def _run_claude_event(self, hook: Hook, event: Event, prompt: str, stop_active: bool) -> tuple[bool, str]:
        """一个 Claude 的 UserPromptSubmit / Stop / SessionStart 钩子 → (拦下 / 要求继续, 话)。"""
        extra: dict[str, Any] = {}
        if event == "user_prompt_submit":
            extra["prompt"] = prompt
        elif event == "stop":
            extra = {"stop_hook_active": stop_active, "last_assistant_message": prompt}
        elif event == "session_start":
            extra["source"] = "startup"
        code, out, err = self._call_claude(hook, event, **extra)
        if code is None:
            return False, f"[钩子超时：{hook.command}]"
        if code == BLOCK_EXIT_CODE and event in ("user_prompt_submit", "stop"):
            return True, clip((err or out).strip(), MAX_HOOK_OUTPUT) or "钩子没有说明原因"
        if code != 0:
            return False, f"[钩子 {hook.command} 退出码 {code}]\n{clip(err.strip(), MAX_HOOK_OUTPUT)}".rstrip()
        data = _json_output(out)
        if data.get("decision") == "block" and event in ("user_prompt_submit", "stop"):
            return True, str(data.get("reason") or "钩子没有说明原因")
        if data.get("continue") is False and event == "user_prompt_submit":
            return True, str(data.get("stopReason") or "钩子没有说明原因")
        if event == "stop":
            return False, ""  # 退出码 0 的输出 Claude Code 只记进记录、不打扰用户
        context = (data.get("hookSpecificOutput") or {}).get("additionalContext")
        if context:
            return False, str(context)
        return False, "" if data else clip(out.strip(), MAX_HOOK_OUTPUT)  # 纯文字输出：当作补充上下文

    # ---------- 对外 ----------

    def run(self, event: Event, tool_name: str, args: dict[str, Any], result: str | None = None) -> HookOutcome:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "HENG_TOOL": tool_name,
               "HENG_TOOL_INPUT": json.dumps(args, ensure_ascii=False)}  # fmt: skip
        if isinstance(args.get("path"), str):
            env["HENG_FILE"] = str((self.workdir / args["path"]).resolve())
        notes = []
        for hook in (h for h in self.hooks if h.event == event and h.matches(tool_name)):
            if hook.style == "claude":
                blocked, message = self._run_claude_tool(hook, event, tool_name, args, result)
                if blocked:
                    return HookOutcome(True, f"被钩子拦下（{hook.command}）：{message}")
                if message:
                    notes.append(message)
                continue
            code, output = run_command(shell_argv(hook.command), self.workdir, hook.timeout, env)
            output = clip(output.strip(), MAX_HOOK_OUTPUT)
            if event == "pre_tool" and code == BLOCK_EXIT_CODE:
                return HookOutcome(True, f"被钩子拦下（{hook.command}）：{output or '没有说明原因'}")
            if code is None:
                notes.append(f"[钩子超时：{hook.command}]")
            elif code != 0:
                notes.append(f"[钩子 {hook.command} 退出码 {code}]\n{output}".rstrip())
            elif output and event == "post_tool":
                notes.append(f"[钩子 {hook.command} 输出]\n{output}")
        return HookOutcome(False, "\n".join(notes))

    def has(self, event: Event) -> bool:
        return any(h.event == event for h in self.hooks)

    def run_event(self, event: Event, prompt: str = "", *, stop_active: bool = False) -> HookOutcome:
        """不针对工具的事件。环境变量 HENG_EVENT、HENG_PROMPT（用户这句话 / 模型最后的回复）。

        - user_prompt_submit：退出码 2 = 拦下这条消息（输出是原因）；否则输出作为补充上下文附在消息后面；
        - stop：退出码 2 = 要求模型继续（输出交给模型作为下一步依据）；否则输出只显示给用户；
        - session_start：输出附进系统提示词。
        其他非零退出码只提示、不拦。Claude 的钩子照它的约定解读（见 _run_claude_event）。
        stop_active：是不是 stop 钩子要求继续之后又停下（Claude 钩子输入里的 stop_hook_active，防死循环用）。
        """
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "HENG_EVENT": event, "HENG_PROMPT": prompt}
        notes = []
        for hook in (h for h in self.hooks if h.event == event):
            if hook.style == "claude":
                blocked, message = self._run_claude_event(hook, event, prompt, stop_active)
                if blocked:
                    return HookOutcome(True, message)
                if message:
                    notes.append(message)
                continue
            code, output = run_command(shell_argv(hook.command), self.workdir, hook.timeout, env)
            output = clip(output.strip(), MAX_HOOK_OUTPUT)
            if code == BLOCK_EXIT_CODE:
                return HookOutcome(True, output or "钩子没有说明原因")
            if code is None:
                notes.append(f"[钩子超时：{hook.command}]")
            elif code != 0:
                notes.append(f"[钩子 {hook.command} 退出码 {code}]\n{output}".rstrip())
            elif output:
                notes.append(output)
        return HookOutcome(False, "\n".join(notes))
