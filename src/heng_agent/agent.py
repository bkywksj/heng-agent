"""对话循环：发给模型 → 收到工具调用 → 按权限执行 → 结果喂回，直到模型不再调工具。

循环不认识界面：输出交给 on_event，需要确认时调 approve。交互界面、-p 非交互模式与测试共用这一个循环。
"""

from __future__ import annotations

import _thread
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from heng_agent.agents import AgentDef, discover_agents
from heng_agent.compaction import compact as compact_history
from heng_agent.context import MODE_NOTES, build_system_prompt, mode_section, nested_memory
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError, ModelError
from heng_agent.core.types import (
    AgentEvent,
    Message,
    Notice,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolCall,
    ToolFinished,
    ToolResult,
    ToolStarted,
    TurnDone,
    TurnEnd,
    UserInterjection,
)
from heng_agent.hooks import Hook, HookRunner
from heng_agent.model import context_window
from heng_agent.model.client import ModelClient
from heng_agent.model.context_window import window_key
from heng_agent.permissions import Approver, Permissions
from heng_agent.session import SessionStore
from heng_agent.tools.base import Tool, ToolContext, builtin_tools
from heng_agent.tools.paths import display
from heng_agent.tools.plan import APPROVED_MODE
from heng_agent.tools.plan import NAME as EXIT_PLAN
from heng_agent.tools.shell import clip
from heng_agent.tools.skill import SkillTool, discover_skills
from heng_agent.tools.subagent import SubAgentTool
from heng_agent.tools.tool_search import ToolSearchTool

MAX_PARALLEL_SUBAGENTS = 4
MAX_STOP_CONTINUES = 3  # stop 钩子最多连续要求继续几次
MAX_OVERFLOW_RECOVERIES = 2  # 上下文超长时最多补救几次（先压缩、再截短工具结果），免得一直重发
MAX_EMPTY_RETRIES = 1  # 模型返回空回复时自动重新请求几次
SHRINK_TOOL_RESULT = 8000  # 补救时把超过这么多字的工具结果截短到这么长
SHRINK_MARK = "\n（上下文超限，这段工具结果被截短了）"
MCP_DEFER_RATIO = 0.1  # MCP 工具声明超过上下文的这个比例就按需加载
CHARS_PER_TOKEN = 3  # 粗估：工具声明多是 JSON 与英文，约 3 字符一个 token
#: 用量记哪几项：缓存读 / 写单独记，计价倍数不同（见 Profile.cost）
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

DENIED = "用户拒绝了这个操作。不要原样重试：说明你想做什么、为什么，或者换一种做法。"
INTERRUPTED = "用户中断了，这个工具没有执行。"


class Agent:
    def __init__(
        self,
        settings: Settings,
        client: ModelClient,
        tools: list[Tool] | None = None,
        permissions: Permissions | None = None,
        session: SessionStore | None = None,
        hooks: list[Hook] | None = None,
        subagents: bool = True,
    ) -> None:
        self.settings = settings
        self.client = client
        self._usage_lock = threading.Lock()  # 并行的子智能体会同时回报用量
        tool_list = list(tools if tools is not None else builtin_tools())
        self.skills = discover_skills(settings.home, settings.workdir, claude_user=settings.claude_user_config)
        #: 自定义子智能体角色（.heng-agent/agents/*.md）；子智能体自己不再派子智能体，用不到
        self.agent_defs: dict[str, AgentDef] = (
            discover_agents(settings.home, settings.workdir, claude_user=settings.claude_user_config)
            if subagents
            else {}
        )
        #: 当前这一问的确认对话：带改文件工具的子智能体要通过它问用户
        self._approve: Approver | None = None
        if self.skills and not any(t.name == "skill" for t in tool_list):
            tool_list.append(SkillTool(self.skills))
        if subagents and not any(t.name == "task" for t in tool_list):
            tool_list.append(SubAgentTool(self._spawn_subagent, self._add_usage, self.agent_defs))
        #: MCP 工具按需加载：已经搜到并加载的 MCP 工具名（按加载顺序，顺序固定才不会反复打破提示缓存）
        self.loaded_mcp: list[str] = []
        if subagents:
            tool_list.append(ToolSearchTool(self._mcp_tools, self._load_mcp))
        self.tools = {t.name: t for t in tool_list}
        self.permissions = permissions or Permissions(settings.permission_mode)
        self.session = session
        self.hooks = HookRunner(hooks or [], settings.workdir, info=self._hook_info)
        self.ctx = ToolContext(
            workdir=settings.workdir, bash_timeout=settings.bash_timeout, proxy=settings.proxy, snapshot=self._snapshot
        )
        self.system = build_system_prompt(settings, self.skills)
        self.seen_memory: set[str] = set()  # 已经附过的子目录 HENG.md
        self._stop_depth = 0  # stop 钩子要求继续的嵌套层数
        self.thinking = settings.thinking  # 思考模式开关（/think 切换）
        self.thinking_budget = settings.thinking_budget
        self.effort = settings.effort  # 思考强度（/effort 切换；None = 服务默认）
        #: 回答过程中用户插的话（交互界面设上；-p、ACP 不设）：每批工具跑完时取一次，接在工具结果后面交给模型，
        #: 和 Claude Code 一样——不用等这一问整个结束，模型在下一步就能看到
        self.interjections: Callable[[], list[str]] | None = None
        self._session_note = ""  # session_start 钩子的输出（重建系统提示词时保留）
        if self.hooks.has("session_start"):
            outcome = self.hooks.run_event("session_start")
            if outcome.message:
                self._session_note = f"\n\n# 会话开始时钩子提供的信息\n\n{outcome.message}"
                self.system += self._session_note
        self.messages: list[Message] = []
        self.usage: dict[str, int] = dict.fromkeys(USAGE_KEYS, 0)
        self.usage_by_model: dict[str, dict[str, int]] = {}  # 模型名 → 用量（存进会话文件，/stats 按模型统计）
        self.last_input_tokens = 0  # 最近一次请求的输入 token：判断是否快撑满上下文
        #: 最近一次请求的输出速度（token/秒，从第一段输出到结束）；状态栏显示。没测到（太短、没用量）是 None
        self.last_speed: float | None = None
        #: 接口查到的上下文长度，按（地址, 模型）记；界面在后台查（见 Repl._ensure_window），查不到记 0
        self.fetched_windows: dict[tuple[str, str], int] = {}
        #: 每一问一个检查点：这一问开始时历史有多长、改过的文件原内容（None = 原来不存在）。/undo 用
        self.turns: list[dict[str, Any]] = []
        #: 整个会话里每个被改过的文件「第一次改之前」的内容（None = 原来不存在）。/diff 用
        self.originals: dict[str, bytes | None] = {}
        #: 当前在干什么：model（等模型）/ tool（跑工具）/ None（空闲）。cancel() 据此选中断方式
        self._phase: str | None = None
        #: 跑 run() 的线程：cancel() 只在它是主线程时才给主线程发中断
        self._run_thread: threading.Thread | None = None
        #: 取消标志是不是自己的（子智能体共用主智能体的，不归它清）
        self._owns_cancel = True

    def cancel(self) -> None:
        """从别的线程中断当前任务（ESC）。按所处阶段选法子，两种不能同时用：

        - 等模型：主线程卡在网络读取，关掉连接（client.cancel）它就报错退出，再被换成 KeyboardInterrupt；
        - 跑工具：给主线程发中断（命令工具每 0.1 秒醒一次，收到就杀进程树）。
        等模型时若也发中断，会多出一个挂起的 KeyboardInterrupt，之后在随便什么地方冒出来。
        """
        self.ctx.cancelled.set()  # 不在主线程跑时（--acp）靠它：工具与每一轮开始前都会看
        if self._phase == "model" and hasattr(self.client, "cancel"):
            self.client.cancel()
        elif self._phase is not None and self._run_thread in (None, threading.main_thread()):
            _thread.interrupt_main()

    def _share_cancel(self, sub: Agent) -> None:
        """子智能体共用主智能体的取消标志：取消主任务时它一起停。由主智能体负责清标志——子智能体自己开始时
        不能清，否则主任务刚被取消、子智能体一启动就把取消吞了。"""
        sub.ctx.cancelled = self.ctx.cancelled
        sub._owns_cancel = False

    def _check_cancelled(self) -> None:
        """被取消了就按「用户中断」处理（抛 KeyboardInterrupt，走和 ESC 一样的收尾）。"""
        if self.ctx.cancelled.is_set():
            raise KeyboardInterrupt

    def _hook_info(self) -> dict[str, Any]:
        """Claude 钩子输入里的会话信息。transcript_path 给的是 heng-agent 的会话文件（格式和 Claude 的不同，
        但 Stop 钩子先看 last_assistant_message，实际的脚本都先用它）。"""
        session = getattr(self, "session", None)
        return {"session_id": session.id if session else "", "transcript_path": str(session.path) if session else "",
                "permission_mode": self.permissions.mode}  # fmt: skip

    def _spawn_subagent(self, role: AgentDef | None = None) -> tuple[Agent, Approver]:
        """子智能体：同一模型客户端、不存会话、不能再派子智能体。返回 (子 Agent, 它用的确认对话)。

        - 通用调研员 / 只读角色：只读工具、plan 权限，不问用户（可以并行）；
        - 带改文件 / 跑命令工具的角色：按主智能体的权限模式与规则，该问的通过主智能体的确认对话问用户；
          它改的文件记进主智能体这一问的检查点，/undo、/diff 连子智能体改的一起算。
        """
        settings = self.settings.model_copy(update={"max_rounds": min(self.settings.max_rounds, 20), "thinking": False})
        if role is None or role.read_only:
            names = role.tools if role else ("read_file", "glob", "grep")
            tools = [t for t in builtin_tools() if t.name in names]
            # 带上主智能体的规则：以前另建一个不带规则的权限对象，用户写的 deny read_file(.env) 对子智能体无效
            permissions = Permissions("plan", self.permissions.rules)
            sub = Agent(settings.model_copy(update={"permission_mode": "plan"}), self.client, tools=tools,
                        permissions=permissions, subagents=False)  # fmt: skip
            self._share_cancel(sub)
            return sub, lambda tool, args: "deny"
        tools = [t for t in builtin_tools() if t.name in role.tools]
        permissions = Permissions(self.permissions.mode, self.permissions.rules)
        sub = Agent(settings.model_copy(update={"permission_mode": self.permissions.mode}), self.client, tools=tools,
                    permissions=permissions, subagents=False)  # fmt: skip
        self._share_cancel(sub)
        sub.ctx.snapshot = self._snapshot  # 子智能体改的文件也记进主智能体这一问：/undo 能恢复
        approve = self._approve or (lambda tool, args: "deny")
        return sub, approve

    def _add_usage(self, usage: dict[str, int]) -> None:
        with self._usage_lock:
            for key in self.usage:
                self.usage[key] += usage.get(key, 0)

    def _execute_all(
        self, calls: list[ToolCall], on_event: Callable[[AgentEvent], None], approve: Approver
    ) -> list[dict[str, Any]]:
        """一轮里的工具调用。全是只读的 task（通用调研员或只读角色）时并行，结果按原顺序返回；
        其余情况按顺序一个个来：带改文件 / 跑命令工具的角色要问用户，并行的话确认框会互相打架。"""

        def read_only_task(call: ToolCall) -> bool:
            if call.name != "task":
                return False
            role = self.agent_defs.get(str(call.input.get("agent") or ""))
            return role is None or role.read_only

        if len(calls) < 2 or not all(read_only_task(c) for c in calls):
            return [self._execute(call, on_event, approve) for call in calls]
        with ThreadPoolExecutor(max_workers=min(len(calls), MAX_PARALLEL_SUBAGENTS)) as pool:
            futures = [pool.submit(self._execute, call, on_event, approve) for call in calls]
            return [f.result() for f in futures]

    # ---------- MCP 工具按需加载 ----------

    def _mcp_tools(self) -> list[Tool]:
        return [t for t in self.tools.values() if t.kind == "mcp"]

    def _load_mcp(self, names: list[str]) -> None:
        for name in names:
            if name not in self.loaded_mcp:
                self.loaded_mcp.append(name)

    def defer_mcp(self) -> bool:
        """要不要按需加载 MCP 工具：auto 时看它们的声明有多大——超过上下文的 10% 就按需加载。

        实测接上 6 个常用服务就有 361 个工具、约 29 万字符（十万 token）：每次请求都带会吃掉一半上下文。
        """
        mcp = self._mcp_tools()
        mode = self.settings.mcp_tool_search
        if not mcp or mode == "off":
            return False
        if mode == "on":
            return True
        chars = len(json.dumps([t.spec() for t in mcp], ensure_ascii=False))
        return chars / CHARS_PER_TOKEN > MCP_DEFER_RATIO * self.context_window

    def tool_specs(self) -> list[dict[str, Any]]:
        """这一轮发给模型的工具声明。

        按需加载时：内置工具 + tool_search + 已加载的 MCP 工具；否则全部（不含 tool_search）。
        """
        hidden = {"tool_search"} if not self.defer_mcp() else set()
        if self.permissions.mode != "plan":
            hidden.add(EXIT_PLAN)  # 交计划只在计划模式里有意义：别的模式里给了，模型会拿它来「请示」
        if not self.defer_mcp():
            return [t.spec() for t in self.tools.values() if t.name not in hidden]
        specs = [t.spec() for t in self.tools.values() if t.kind != "mcp" and t.name not in hidden]
        return specs + [self.tools[n].spec() for n in self.loaded_mcp if n in self.tools]

    def system_prompt(self) -> str:
        """发给模型的系统提示词：固定部分 + 当前权限模式（随 Shift+Tab、批准计划变）+ 按需加载 MCP 时的说明。

        权限模式以前写死在启动时生成的那段里，中途切了模式模型还以为是原来的；现在每轮按当前模式生成。
        """
        parts = [self.system, mode_section(self.permissions.mode)]
        if self.defer_mcp():
            counts: dict[str, int] = {}
            for tool in self._mcp_tools():
                server = tool.name.split("__")[1] if tool.name.count("__") >= 2 else "?"
                counts[server] = counts.get(server, 0) + 1
            servers = "、".join(f"{name}（{n} 个）" for name, n in counts.items())
            parts.append(f"# 外部工具（MCP）\n\n已接入：{servers}。这些工具太多，默认没有加载；"
                         "用户的任务可能用得上时，先用 tool_search 按关键词搜，搜到的下一步就能调用。")  # fmt: skip
        return "\n\n".join(parts)

    def _present_plan(self, tool: Tool, args: dict[str, Any], approve: Approver,
                      on_event: Callable[[AgentEvent], None]) -> ToolResult:  # fmt: skip
        """模型交出计划：请用户批准，批准就切到用户选的模式、让模型接着做，不批准就留在计划模式等用户说怎么改。"""
        if self.permissions.mode != "plan":
            return ToolResult("现在不是计划模式，不用请示：直接按计划做。")
        if not str(args.get("plan", "")).strip():
            return ToolResult("plan 是空的：把要改哪些文件、怎么改、怎么验证写清楚再交。", is_error=True)
        answer = approve(tool, args)
        mode = APPROVED_MODE.get(answer)
        if mode is None:
            return ToolResult("用户没有批准这个计划，还在计划模式。先停下，等用户说要怎么改；"
                              "不要再调 exit_plan_mode，也不要尝试改文件、跑命令。")  # fmt: skip
        self.permissions.mode = mode  # type: ignore[assignment]
        on_event(Notice(f"计划已批准，权限模式切到 {mode}：开始执行"))
        return ToolResult(f"用户批准了计划，权限模式已切到 {mode}（{MODE_NOTES[mode]}）。现在按计划开始做，"
                          "做完一步验证一步；计划之外的大改动先问用户。")  # fmt: skip

    def _snapshot(self, path: Path) -> None:
        """改文件前由工具调用：同一问里只记第一次改之前的内容；另记一份整个会话里「第一次改之前」的内容给 /diff。

        后一份不随压缩丢弃（压缩会删掉更早几问的撤销点），只在 /clear、切换会话时清掉。
        """
        key = str(path.resolve())
        original = path.read_bytes() if path.is_file() else None
        self.originals.setdefault(key, original)
        if not self.turns:
            return
        files = self.turns[-1]["files"]
        if key not in files:
            files[key] = original

    def undo(self) -> list[str] | None:
        """撤销最近一问：恢复它用 write_file / edit_file 改过的文件，并把这一问从对话历史里去掉。

        返回恢复了哪些文件（相对路径）；没有可撤销的返回 None。bash 命令造成的改动无法追踪，不在其内。
        """
        if not self.turns:
            return None
        turn = self.turns.pop()
        restored = []
        for key, original in turn["files"].items():
            path = Path(key)
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
            restored.append(display(path, self.settings.workdir))
        self.messages = self.messages[: turn["start"]]
        self._save()
        return sorted(restored)

    def rewind(self, turn_index: int) -> list[str]:
        """回退到第 turn_index 问之前（对话与代码一起）：从最近一问往前逐个撤销，文件按这个顺序恢复，
        同一个文件被多问改过时最后落到最早那一版。返回恢复过的文件。bash 造成的改动同样无法撤销。"""
        restored: set[str] = set()
        while len(self.turns) > turn_index:
            undone = self.undo()
            if undone is None:
                break
            restored.update(undone)
        return sorted(restored)

    def turn_titles(self) -> list[str]:
        """每一问的第一句话（/rewind 列表里显示）。"""
        titles = []
        for turn in self.turns:
            message = self.messages[turn["start"]] if turn["start"] < len(self.messages) else {}
            content = message.get("content") or []
            text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            titles.append(text.strip().splitlines()[0] if text.strip() else "（空）")
        return titles

    @property
    def context_window(self) -> int:
        return self.context_window_info[0]

    @property
    def context_window_info(self) -> tuple[int, str]:
        """(上下文长度, 来源)：配置 → 接口查询（界面在后台查好放进 fetched_windows）→ 按模型名推断 → 默认。"""
        profile = getattr(self.client, "profile", None)
        fetched = self.fetched_windows.get(window_key(profile)) if profile is not None else None
        return context_window.resolve(profile, fetched, self.settings.context_window)

    def compact(self, on_event: Callable[[AgentEvent], None], *, force: bool = False, instruction: str = "") -> bool:
        """输入超过上下文的 compact_ratio（或 force）时压缩历史。返回是否真的压缩了。instruction：用户对摘要的要求。"""
        window = self.context_window
        if not force and self.last_input_tokens <= self.settings.compact_ratio * window:
            return False
        before = len(self.messages)
        on_event(Notice(f"上下文已用 {self.last_input_tokens}/{window} token，正在压缩对话历史…"))
        # 摘要输入按字符控制在约一个上下文以内：中文大约一字一 token，英文更省
        messages, summary = compact_history(self.client, self.messages, max_chars=window, instruction=instruction)
        if not summary:
            on_event(Notice("历史太短，没有可压缩的。"))
            return False
        self.messages = messages
        self.last_input_tokens = 0
        if self.turns:
            # 旧的几问已经折进摘要，历史没法再按位置回退：只保留当前这一问的检查点
            current = self.turns[-1]
            current["start"] = len(messages) - (1 if messages[-1]["role"] == "user" else 0)
            current["start"] = min(current["start"], len(messages))
            self.turns = [current]
            on_event(Notice("压缩之后，更早的几问不能再 /undo 了（当前这一问仍可撤销）。"))
        self._save()
        on_event(Notice(f"已压缩：{before} 条消息 → {len(messages)} 条（旧历史换成了摘要）"))
        return True

    def _recover_overflow(self, on_event: Callable[[AgentEvent], None], attempt: int) -> bool:
        """上下文超出模型上限时的补救，最多两次：先强制压缩；压缩不了、或压缩后还超（压缩会原样保留末尾，
        超大的工具结果恰好在末尾时压了也没用）就把超长的工具结果截短。返回要不要重发。"""
        if attempt >= MAX_OVERFLOW_RECOVERIES:
            return False
        on_event(Notice("上下文超出了模型的上限，自动处理后重发…"))
        if attempt == 0:
            try:
                if self.compact(on_event, force=True):
                    return True
            except ModelError as exc:  # 做摘要的请求本身也可能超长：换截短的办法
                on_event(Notice(f"压缩没成（{exc}），改为截短过长的工具结果"))
        return self._shrink_tool_results(on_event)

    def _shrink_tool_results(self, on_event: Callable[[AgentEvent], None]) -> bool:
        """把历史里超长的工具结果截短（保留开头与结尾、中间注明）。返回有没有截。"""
        shrunk = 0
        for message in self.messages:
            if message["role"] != "user" or not isinstance(message["content"], list):
                continue
            for block in message["content"]:
                body = block.get("content")
                if block.get("type") != "tool_result" or not isinstance(body, str) or len(body) <= SHRINK_TOOL_RESULT:
                    continue
                if SHRINK_MARK in body:
                    continue  # 已经截过：截短后加了省略说明会略超长度，别再截一遍、假装又有进展
                block["content"] = clip(body, SHRINK_TOOL_RESULT) + SHRINK_MARK
                shrunk += 1
        if shrunk:
            on_event(Notice(f"截短了 {shrunk} 段过长的工具结果"))
            self._save()
        return shrunk > 0

    def _repair(self) -> None:
        """上一问中途被打断时，历史可能以「有工具调用、没有结果」结尾，模型服务会拒收：补上中断结果。"""
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return
        pending = [b for b in self.messages[-1]["content"] if b.get("type") == "tool_use"]
        if pending:
            results = [{"type": "tool_result", "tool_use_id": b["id"], "content": INTERRUPTED, "is_error": True}
                       for b in pending]  # fmt: skip
            self.messages.append({"role": "user", "content": results})

    def _execute(self, call: ToolCall, on_event: Callable[[AgentEvent], None], approve: Approver) -> dict[str, Any]:
        self._check_cancelled()  # 一轮里有好几个工具调用：取消后剩下的不再跑
        tool = self.tools.get(call.name)
        on_event(ToolStarted(call.id, call.name, call.input))
        denied = False
        if tool is None:
            result = ToolResult(f"没有叫 {call.name} 的工具，可用的有：{', '.join(self.tools)}", is_error=True)
        elif (pre := self.hooks.run("pre_tool", call.name, call.input)).blocked:
            # 钩子先于确认框（和 Claude Code 一样）：要被安全钩子拦下的调用，不该先问用户「允许吗」
            result = ToolResult(pre.message, is_error=True)
        elif call.name == EXIT_PLAN:  # 交计划：不走一般的「允许 / 拒绝」，由用户选按哪种模式执行
            result = self._present_plan(tool, call.input, approve, on_event)
        elif not self.permissions.check(tool, call.input, approve):
            result, denied = ToolResult(DENIED, is_error=True), True
        else:
            result = self._run_tool(tool, call)
            post = self.hooks.run("post_tool", call.name, call.input, result.content)
            extra = "\n\n".join(m for m in (pre.message, post.message, self._nested_memory(call)) if m)
            if extra:
                result = ToolResult(f"{result.content}\n\n{extra}", is_error=result.is_error)
        on_event(ToolFinished(call.id, call.name, result, denied=denied))
        return {"type": "tool_result", "tool_use_id": call.id, "content": result.content, "is_error": result.is_error}

    def _run_tool(self, tool: Tool, call: ToolCall) -> ToolResult:
        try:
            return tool.run(call.input, self.ctx)
        except AgentError as exc:
            return ToolResult(str(exc), is_error=True)
        except (KeyError, TypeError, ValueError) as exc:  # 模型给的参数缺字段或类型不对
            return ToolResult(f"参数有误：{type(exc).__name__}: {exc}", is_error=True)
        except OSError as exc:
            return ToolResult(f"文件或命令出错：{exc}", is_error=True)

    def _nested_memory(self, call: ToolCall) -> str:
        """模型第一次碰到某个子目录的文件时，把那里的 HENG.md（该目录的约定）附在工具结果后面，每份只附一次。"""
        raw = call.input.get("path")
        if not isinstance(raw, str) or not raw:
            return ""
        found = nested_memory(self.settings.workdir / raw, self.settings.workdir, self.seen_memory)
        return "\n\n".join(
            f"[{display(path, self.settings.workdir)} —— 在这个目录下工作时要遵守的约定]\n{text}"
            for path, text in found
        )

    def reload_system(self) -> None:
        """记忆文件改了（# 快速记忆、/memory edit）：重建系统提示词，下一问就生效。"""
        self.system = build_system_prompt(self.settings, self.skills) + self._session_note

    def set_workdir(self, path: Path) -> None:
        """换工作目录（切到 / 切出 git 工作树）。几处都拿着工作目录，要一起换：设置、工具的读写边界、
        钩子在哪儿跑、系统提示词里告诉模型的当前目录、子目录 HENG.md 的已附记录。"""
        self.settings = self.settings.model_copy(update={"workdir": path})
        self.ctx.workdir = path
        self.hooks.workdir = path
        self.seen_memory = set()
        self.skills = discover_skills(self.settings.home, path, claude_user=self.settings.claude_user_config)
        self.agent_defs = (
            discover_agents(self.settings.home, path, claude_user=self.settings.claude_user_config)
            if "task" in self.tools
            else {}
        )
        self.reload_system()

    def run(
        self,
        user_text: str,
        on_event: Callable[[AgentEvent], None],
        approve: Approver,
        images: list[dict[str, Any]] | None = None,
    ) -> TurnDone:
        """images：Anthropic 图片块（context.mention_images 给出），跟在文字后面一起发给模型。"""
        self._approve = approve
        if self._stop_depth == 0 and self._owns_cancel:
            self.ctx.cancelled.clear()  # 上一问的取消不算数（子智能体不清：标志是主智能体的）
            self._run_thread = threading.current_thread()
        if self._stop_depth == 0 and self.hooks.has("user_prompt_submit"):
            # user_prompt_submit 钩子：退出码 2 拦下这条消息；其余输出作为补充上下文附在后面
            outcome = self.hooks.run_event("user_prompt_submit", user_text)
            if outcome.blocked:
                on_event(Notice(f"这条消息被钩子拦下，没有发给模型：{outcome.message}"))
                done = TurnDone(usage=dict.fromkeys(USAGE_KEYS, 0), rounds=0)
                on_event(done)
                return done
            if outcome.message:
                user_text = f"{user_text}\n\n[钩子补充的上下文]\n{outcome.message}"
        self._repair()
        self.turns.append({"start": len(self.messages), "files": {}})
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}, *(images or [])]
        self.messages.append({"role": "user", "content": content})
        turn_usage = dict.fromkeys(USAGE_KEYS, 0)
        rounds = 0
        overflow_tries = 0  # 这一问里「上下文超长」补救了几次
        empty_retries = 0  # 这一问里因为空回复重新请求了几次
        while rounds < self.settings.max_rounds:
            rounds += 1
            self._check_cancelled()
            self.compact(on_event)  # 上一次请求已经快撑满上下文时，先压缩再发
            specs = self.tool_specs()  # 每轮重算：上一轮 tool_search 加载的工具这一轮要带上
            text: list[str] = []
            calls: list[ToolCall] = []
            thoughts: list[dict[str, Any]] = []  # 完整的思考块（带签名）：原样存进历史
            end = TurnEnd("end_turn")
            self._phase = "model"
            if hasattr(self.client, "thinking_budget"):  # 每次请求前按当前开关设：/think、/effort 可以随时切
                self.client.thinking_budget = self.thinking_budget if self.thinking else 0
                self.client.effort = self.effort
            first_output: float | None = None  # 第一段输出到达的时刻：速度从这里算，不含排队、读提示词的时间
            requested = time.monotonic()
            try:
                for event in self.client.stream(self.system_prompt(), self.messages, specs):
                    if first_output is None and not isinstance(event, TurnEnd):
                        first_output = time.monotonic()
                    if isinstance(event, TextDelta):
                        text.append(event.text)
                        on_event(event)
                    elif isinstance(event, ToolCall):
                        calls.append(event)
                    elif isinstance(event, ThinkingDelta):
                        on_event(event)
                    elif isinstance(event, ThinkingBlock):
                        thoughts.append(event.block)
                    else:
                        end = event
            except ModelError as exc:
                # 一下子超了上限（如工具返回了很大的结果）：发之前的自动压缩来不及触发，服务直接拒收。
                # 补救后重发这一轮（不算进轮数）；补救不了就照常报错
                if exc.overflow and not text and not calls and self._recover_overflow(on_event, overflow_tries):
                    overflow_tries += 1
                    rounds -= 1
                    continue
                raise
            except KeyboardInterrupt:
                # 回复到一半被打断：把已经出来的半段（或一句说明）记成助手消息，保持 user / assistant 交替，
                # 否则下一问会出现两条相连的用户消息，有的服务直接拒收
                partial = "".join(text).strip()
                note = f"{partial}\n\n（这次回复被用户中断）" if partial else "（这次回复被用户中断）"
                self.messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
                self._save()
                raise
            finally:
                self._phase = None
            output = end.usage.get("output_tokens", 0)
            finished = time.monotonic()
            spent = finished - first_output if first_output is not None else 0.0
            if spent < 0.2:
                # 有的中转站把整段回复攒齐了一次推过来（实测 80 段输出同一毫秒到达），从第一段算几乎是 0：
                # 改按整个请求的用时算（含排队、读提示词，偏慢些），总比不显示强
                spent = finished - requested
            if output >= 20 and spent >= 0.2:  # 太短的回复算出来的速度没意义（一两个 token、几毫秒）
                self.last_speed = output / spent
            for key in turn_usage:
                turn_usage[key] += end.usage.get(key, 0)
                self.usage[key] += end.usage.get(key, 0)
            # 按当时用的模型分开记：一个会话中途 /model、/provider 换了模型，/stats 也分得清、价钱算得对
            by_model = self.usage_by_model.setdefault(self.model_name, dict.fromkeys(USAGE_KEYS, 0))
            for key in USAGE_KEYS:
                by_model[key] = by_model.get(key, 0) + end.usage.get(key, 0)
            # 上下文占用 = 没命中 + 命中缓存 + 写入缓存：开了提示缓存后 input_tokens 只是末尾新增那一小段，
            # 只看它会严重低估，该自动压缩时不压缩，最后撑爆上下文
            self.last_input_tokens = sum(end.usage.get(k, 0) for k in ("input_tokens", "cache_read_tokens",
                                                                        "cache_write_tokens"))  # fmt: skip
            if not "".join(text).strip() and not calls and empty_retries < MAX_EMPTY_RETRIES:
                # 空回复（没文字、也没调工具，常见的是只有一个空的思考块）：实测某些中转站偶尔这样。交互里看着像
                # AI 没吭声，-p 脚本拿到空的 result 还以为成功了——重新请求一次（空回复不进历史、不算轮数；用量照记）
                empty_retries += 1
                rounds -= 1
                on_event(Notice("模型返回了空回复，自动重新请求一次…"))
                continue
            # 思考块必须放在最前面（服务要求的顺序），再是文字与工具调用
            content: list[dict[str, Any]] = list(thoughts)
            content += [{"type": "text", "text": "".join(text)}] if "".join(text) else []
            content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls]
            if not any(b["type"] in ("text", "tool_use") for b in content):
                content.append({"type": "text", "text": "（空回复）"})
            self.messages.append({"role": "assistant", "content": content})
            if not calls:  # 模型不再调工具：这一问结束
                break
            self._phase = "tool"
            try:
                results = self._execute_all(calls, on_event, approve)
            finally:
                self._phase = None
            extra = self.interjections() if self.interjections is not None else []
            for said in extra:  # 工具结果在前、插的话在后（服务要求 tool_result 排在这条消息的最前面）
                results.append({"type": "text", "text": said})
                on_event(UserInterjection(said))
            self.messages.append({"role": "user", "content": results})
            self._save()
        else:
            note = f"\n（一问之内已调用模型 {rounds} 轮，达到上限 max_rounds，先停下。可以让我继续。）"
            on_event(TextDelta(note))
        self._save()
        done = TurnDone(usage=turn_usage, rounds=rounds)
        on_event(done)
        return self._after_stop(done, on_event, approve)

    def _after_stop(self, done: TurnDone, on_event: Callable[[AgentEvent], None], approve: Approver) -> TurnDone:
        """stop 钩子：模型准备停下时跑。退出码 2 = 要求继续，把钩子输出当作新的一问交给模型（在历史里可见、
        可 /rewind）；最多连续 MAX_STOP_CONTINUES 次，免得钩子一直不满意而死循环。"""
        if not self.hooks.has("stop") or self._stop_depth >= MAX_STOP_CONTINUES:
            return done
        last = next((m for m in reversed(self.messages) if m.get("role") == "assistant"), {})
        reply = "".join(b.get("text", "") for b in last.get("content") or [] if b.get("type") == "text")
        outcome = self.hooks.run_event("stop", reply, stop_active=self._stop_depth > 0)
        if not outcome.blocked:
            if outcome.message:
                on_event(Notice(outcome.message))
            return done
        on_event(Notice(f"stop 钩子要求继续：{outcome.message[:200]}"))
        self._stop_depth += 1
        try:
            more = self.run(f"[stop 钩子要求继续]\n{outcome.message}", on_event, approve)
        finally:
            self._stop_depth -= 1
        usage = {k: done.usage.get(k, 0) + more.usage.get(k, 0) for k in USAGE_KEYS}
        return TurnDone(usage=usage, rounds=done.rounds + more.rounds)

    def _save(self) -> None:
        if self.session is not None:
            self.session.save(self.messages, self.usage, self.usage_by_model)

    @property
    def model_name(self) -> str:
        return str(getattr(getattr(self.client, "profile", None), "model", "") or "未知模型")

    def resume(self, session: SessionStore) -> int:
        """接着某个会话聊：载入历史，返回载入的消息条数。

        中途从别的会话切过来时，先清掉当前会话的撤销检查点、任务清单与用量——否则 /undo、/rewind 会去回退
        上一个会话里的改动。接上的会话本身没有检查点（当时没记改前内容），所以它之前的提问不能回退。
        """
        self.messages, usage = session.load()
        self.usage = dict.fromkeys(USAGE_KEYS, 0)
        self.usage.update(usage)
        self.usage_by_model = {m: dict(u) for m, u in session.usage_by_model.items()}
        self.turns = []
        self.originals = {}
        self.seen_memory = set()  # 新的历史里没有那些子目录约定，得重新附
        self.ctx.todos.clear()
        self.last_input_tokens = 0
        self.session = session
        return len(self.messages)
