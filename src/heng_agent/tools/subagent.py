"""子智能体工具：把一件调研 / 探索的事交给一个全新的、只读的智能体去做，只把结论带回来。

好处：主对话不被大量搜索、读文件的过程挤满；同一轮发出多个 task 时并行执行（agent.py 负责并发）。

子智能体只有只读工具（读文件、找文件、搜内容、读技能），也不能再派子智能体：
- 并行的子智能体若能改文件、跑命令，确认对话会互相打架，同时写同一个文件也会冲突；
- 只读就不需要用户确认，可以安全地并发。
需要改代码的事，等子智能体带回结论后由主智能体来做。

每次派出都记一条 SubRun（SubRuns 登记）：界面据此画子智能体面板（任务、用时、token）、查看它做了什么。
run_in_background：派出去马上返回，主对话照常继续；做完时把这条记录交给 notify（界面在主智能体下一批工具
跑完时插进去，空闲时自动发起一问）。没有 notify（-p 非交互模式、测试）时照旧在前台跑完。
后台的子智能体有自己的取消标志：主对话按 ESC 不会把它们一起停掉（照 Claude Code）。
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from heng_agent.core.types import TextDelta, ToolResult, ToolStarted, TurnDone
from heng_agent.tools.base import Tool, ToolContext

SUBAGENT_PROMPT = """你是一个只读的调研子智能体，替主智能体完成下面这件事。你只能读文件、找文件、搜内容，
不能修改任何东西。做完后给出一份简洁、具体的结论：找到了什么（文件路径、函数名、行号）、得出什么判断。
结论会原样交给主智能体，它看不到你的中间过程，所以结论要能独立看懂。

任务：
"""
MAX_RESULT_CHARS = 20_000
#: 每个子智能体最多记多少条过程（查看时用）
MAX_LOG = 300


@dataclass
class SubRun:
    """一次派出的子智能体：给面板与查看用。agent 是子 Agent（取它的 usage 算 token）。"""

    id: int
    description: str
    role: str
    background: bool
    agent: Any = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    result: str = ""
    rounds: int = 0
    error: bool = False
    #: 过程：("tool", "glob pattern=*") / ("text", "……")，按发生顺序
    log: list[tuple[str, str]] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.finished is None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def tokens(self) -> int:
        usage = getattr(self.agent, "usage", None) or {}
        return sum(int(v) for k, v in usage.items() if k in ("input_tokens", "output_tokens"))

    def add_text(self, text: str) -> None:
        if self.log and self.log[-1][0] == "text":
            self.log[-1] = ("text", self.log[-1][1] + text)
        elif len(self.log) < MAX_LOG:
            self.log.append(("text", text))

    def add_tool(self, line: str) -> None:
        if len(self.log) < MAX_LOG:
            self.log.append(("tool", line))


class SubRuns:
    """主智能体派出过的子智能体（线程安全：后台的在别的线程里更新）。"""

    def __init__(self) -> None:
        self._runs: list[SubRun] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def add(self, description: str, role: str, background: bool) -> SubRun:
        with self._lock:
            run = SubRun(next(self._ids), description, role, background)
            self._runs.append(run)
        return run

    def running(self) -> list[SubRun]:
        with self._lock:
            return [r for r in self._runs if r.running]

    def all(self) -> list[SubRun]:
        with self._lock:
            return list(self._runs)

    def get(self, run_id: int) -> SubRun | None:
        with self._lock:
            return next((r for r in self._runs if r.id == run_id), None)


class SubAgentTool(Tool):
    name = "task"
    kind = "read"  # 子智能体只读，不需要确认
    description = (
        "派一个只读的子智能体去完成一件调研 / 探索任务（在大项目里找相关代码、梳理调用关系、对比几处实现……），"
        "它独立搜索阅读，只把结论带回来，主对话不会被中间过程挤满。同一轮里发出多个 task 会并行执行。"
        "子智能体不能修改文件或跑命令；要改代码，等结论回来后自己改。"
        "run_in_background=true 把它放到后台：马上返回，你可以接着做别的，它做完时结论会自动交给你"
        "（适合不急着用结论的调研；下一步就要用结论的别放后台）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "三五个字概括这件事（显示给用户）"},
            "prompt": {"type": "string", "description": "交给子智能体的完整任务说明：要找什么、要回答什么问题"},
            "run_in_background": {"type": "boolean", "description": "放到后台跑，做完自动把结论交给你（默认 false）"},
        },
        "required": ["description", "prompt"],
    }

    def __init__(
        self,
        spawn: Callable[..., tuple[Any, Any]],
        on_usage: Callable[[dict[str, int]], None] | None = None,
        agents: dict[str, Any] | None = None,
        runs: SubRuns | None = None,
        notify: Callable[[], Callable[[SubRun], None] | None] | None = None,
    ) -> None:
        """spawn(角色或 None) 返回 (子 Agent, 确认对话)；on_usage 把子智能体的用量记进主 Agent；
        agents：自定义角色（名字 → AgentDef），写进工具说明里让模型挑。

        用量用回调交回而不是存在工具对象上：多个子智能体并行时会互相覆盖。
        """
        self.spawn = spawn
        self.on_usage = on_usage
        self.runs = runs if runs is not None else SubRuns()
        #: 取「后台做完时通知谁」：交互界面开着时才有（主智能体的 background_notify），每次调用时现取
        self.notify = notify or (lambda: None)
        self.agents = dict(agents or {})
        if self.agents:
            listing = "\n".join(
                f"- {a.name}：{a.description}（工具：{', '.join(a.tools)}{'；只读' if a.read_only else ''}）"
                for a in self.agents.values()
            )
            self.description = (  # type: ignore[misc]
                f"{type(self).description}\n可以用 agent 参数派专门的角色（不写就是通用的只读调研员）：\n{listing}\n"
                "带改文件 / 跑命令工具的角色会按权限模式向用户确认，而且不并行。"
            )
            self.input_schema = {  # type: ignore[misc]
                **type(self).input_schema,
                "properties": {
                    **type(self).input_schema["properties"],
                    "agent": {"type": "string", "enum": list(self.agents), "description": "派哪个角色（可省）"},
                },
            }

    def summary(self, args: dict[str, Any]) -> str:
        agent = args.get("agent")
        return f"{agent}：{args.get('description', '')}" if agent else str(args.get("description", ""))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("agent") or "")
        role = self.agents.get(name) if name else None
        if name and role is None:
            return ToolResult(f"没有叫 {name} 的角色，有这些：{', '.join(self.agents) or '无'}", is_error=True)
        notify = self.notify()
        # 只读的才能放后台：能改文件、跑命令的和主对话同时跑，确认框会抢着弹、还可能同时改同一个文件
        background = bool(args.get("run_in_background")) and notify is not None and (role is None or role.read_only)
        run = self.runs.add(str(args.get("description", "")), name or "通用调研员", background)
        sub, approve = self.spawn(role, own_cancel=background)
        run.agent = sub
        if not background:
            return self._finish(run, sub, approve, role, args)
        assert notify is not None

        def work() -> None:
            try:
                self._finish(run, sub, approve, role, args)
            except BaseException as exc:  # 被停止、网络错误……都算这个子智能体失败：结论里写明，照样通知
                run.result = f"子智能体没做完：{type(exc).__name__}: {exc}" if str(exc) else "子智能体被停止了"
                run.error = True
                run.finished = run.finished or time.monotonic()
            notify(run)

        threading.Thread(target=work, name=f"heng-subagent-{run.id}", daemon=True).start()
        return ToolResult(f"已派到后台（子智能体 #{run.id}：{run.description}）。它做完时结论会自动交给你，"
                          "这期间可以接着做别的事；别重复派同一件事。")  # fmt: skip

    def _finish(self, run: SubRun, sub: Any, approve: Any, role: Any, args: dict[str, Any]) -> ToolResult:
        text: list[str] = []

        def on_event(event: Any) -> None:
            if isinstance(event, TextDelta):
                text.append(event.text)
                run.add_text(event.text)
            elif isinstance(event, ToolStarted):
                tool = getattr(sub, "tools", {}).get(event.name)
                summary = tool.summary(event.input) if tool is not None else ""
                run.add_tool(f"{event.name} {summary}".strip())

        intro = f"{role.prompt}\n\n任务：\n" if role else SUBAGENT_PROMPT
        try:
            done: TurnDone = sub.run(intro + str(args["prompt"]), on_event, approve)
        except BaseException:
            run.finished = run.finished or time.monotonic()
            raise
        answer = "".join(text).strip() or "（子智能体没有给出结论）"
        if len(answer) > MAX_RESULT_CHARS:
            answer = answer[:MAX_RESULT_CHARS] + "\n…（结论太长，后面截掉了）"
        if self.on_usage is not None:
            self.on_usage(done.usage)
        run.result, run.rounds, run.finished = answer, done.rounds, time.monotonic()
        return ToolResult(f"{answer}\n\n（子智能体用了 {done.rounds} 轮）")
