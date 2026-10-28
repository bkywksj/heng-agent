"""回答过程中输入框一直在：智能体在后台线程跑，主线程留给输入框（Repl 继承的 mixin）。

以前一问开始输入框就关掉，回答期间敲的字直接丢掉（keywatch 只认 ESC / Ctrl+B）。现在：
- 智能体在后台线程里跑（ACP 模式本来就这样跑，取消靠 ctx.cancelled 与关掉连接，不靠给主线程发中断）；
- 主线程一直显示输入框：回答期间照样能打字，Enter 把这句话排进队列，这一问结束后按顺序发出；
- 输出：写完的段落、工具调用等照旧用 rich 排版，但先写进 _AboveWriter，再由输入框打印到它上方
  （prompt_toolkit 的 run_in_terminal：收起输入框 → 打印 → 画回来）；还没写完的那段回复、转圈与计时
  画在输入框里「正在进行」那块（不能用 rich 的 Live / Status：它们自己移动光标重画，会和输入框打架）；
- 要你确认时：后台线程把请求交给主线程，输入框收起、照旧弹确认框（框里敲了一半的字保留），答完再画回来；
- ESC / Ctrl+C 打断这一问，Ctrl+B 把前台命令转后台，Shift+Tab 照样切权限模式（下一次确认时生效）。
没有原生控制台的终端（mintty、管道输入、测试）不走这里：照旧是以前的做法（Repl._live_turn_ok）。

注意：本模块只能从 render 导入，不能导入 repl（repl 继承它，会循环引用）。
"""

from __future__ import annotations

import io
import threading
from typing import TYPE_CHECKING, Any

from prompt_toolkit.formatted_text import ANSI, AnyFormattedText
from rich.console import Console, Group
from rich.text import Text

from heng_agent.tools.base import Tool
from heng_agent.ui.inputbox import TurnHooks, TurnSignal

if TYPE_CHECKING:
    from heng_agent.ui.inputbox import InputBox

#: 「正在进行」那块最多画几行：还没写完的那段回复很长时只显示最后这些行（写完的段落会打印到上方）
LIVE_MAX_LINES = 12
#: 回答中也马上执行的命令：只看状态、或改下一次请求才生效的设置（同 Claude Code：/status、/usage、/tasks、/effort……）。
#: 别的命令（会动对话、切服务商、要你选东西的）排队，这一问结束后执行
INSTANT_COMMANDS = frozenset({"status", "usage", "cost", "stats", "tasks", "bashes", "context", "todos",
                              "effort", "think", "btw"})  # fmt: skip


class _AboveWriter(io.TextIOBase):
    """rich 往这里写：攒起来，由输入框在它上方打印（run_in_terminal）。输入框没画出来时就一直攒着。"""

    def __init__(self, box: InputBox, real: Console) -> None:
        self.box, self.real = box, real
        self._chunks: list[str] = []
        self._lock = threading.Lock()
        self._scheduled = False

    def write(self, s: str) -> int:
        with self._lock:
            self._chunks.append(s)
        return len(s)

    def flush(self) -> None:
        # 合并：已经约好一次打印就不再约；那一次打印时把攒着的全部打出来
        with self._lock:
            if self._scheduled or not self._chunks:
                return
            self._scheduled = True
        if not self.box.print_above(self.drain):
            with self._lock:
                self._scheduled = False  # 输入框没画出来：先攒着，画出来之前（before_draw）或结束时再打

    def drain(self) -> None:
        """把攒着的直接写到真正的终端上（在界面线程里、输入框收起时调，或者输入框没画出来时调）。"""
        with self._lock:
            text, self._chunks, self._scheduled = "".join(self._chunks), [], False
        if text:
            # 经真正的控制台再打一遍（颜色从 ANSI 里还原）：它记录的对话（/export、测试）里也就有这些内容
            self.real.print(Text.from_ansi(text), end="", soft_wrap=True)

    def isatty(self) -> bool:
        return True


class TurnMixin:
    """由 Repl 继承。用到的属性（console、agent、_box、_queued……）都在 Repl.__init__ 里建好。"""

    console: Console
    agent: Any
    _box: InputBox | None

    def _live_turn_ok(self) -> bool:
        """能不能用「回答中输入框一直在」：要有正在用的带边框输入框（真实的 Windows 控制台、Windows Terminal 等）。"""
        return self._box is not None and not getattr(self, "_plain_input", True)

    def _run_turn_live(self, text: str, images: list[Any] | None) -> None:
        """跑一问：智能体在后台线程，主线程显示输入框。出错照旧抛出来（KeyboardInterrupt = 被打断），由调用方收尾。"""
        box = self._box
        assert box is not None
        real = self.console
        writer = _AboveWriter(box, real)
        # 和真正的控制台同样的宽度与颜色，只是写到 writer 里
        self.console = Console(file=writer, force_terminal=True, color_system=real.color_system or "standard",
                               width=real.width, legacy_windows=False, highlight=False)  # type: ignore[arg-type]  # fmt: skip
        self._turn_real_console = real
        self._turn_live_render: Any = None  # 还没写完的那段回复（rich 可渲染对象）
        self._turn_activity: Any = None  # 转圈旁边的文字（render.Ticker）
        self._turn_approval: tuple[Tool, dict[str, Any], threading.Event, list[Any]] | None = None
        self._turn_approval_lock = threading.Lock()
        outcome: dict[str, BaseException] = {}

        def work() -> None:
            try:
                self.agent.run(text, self.on_event, self._approve_from_worker, images=images)  # type: ignore[attr-defined]
            except BaseException as exc:  # 被打断（KeyboardInterrupt）也在这里接住，交给主线程按原来的方式收尾
                outcome["error"] = exc
            finally:
                box.signal("done", before=writer.drain)

        self._queue_lock = threading.Lock()
        box.turn = TurnHooks(live=self._turn_live_text, queued=lambda: list(self._queued),
                             interrupt=self._turn_interrupt, detach=self.agent.ctx.detach.set, before_draw=writer.drain,
                             take_queued=self._take_queued)  # type: ignore[attr-defined]  # fmt: skip
        self.agent.interjections = self._take_interjections  # 每批工具跑完时，排着队的普通的话插进这一问
        # 主循环在这之前已经用 rich 的 Status 转起了「思考中」：它有自己的线程，一直往光标所在处画——光标此刻在
        # 输入框里，于是「思考中」画进了输入框、盖住 ›，直到模型回来第一段才停（真实终端里实测，不是一闪而过）。
        # 先停掉它，再把转圈交给输入框上方那一行
        self._unspin()  # type: ignore[attr-defined]
        self._spin("思考中…")  # type: ignore[attr-defined]
        worker = threading.Thread(target=work, name="heng-turn", daemon=True)
        worker.start()
        draft = ""
        try:
            while True:
                result = box.prompt(default=draft)
                if isinstance(result, TurnSignal):
                    draft = result.draft
                    if result.kind == "done":
                        break
                    if result.kind == "approval":
                        self._serve_approval(real, writer)
                    continue
                draft = ""
                if not result.strip():
                    continue
                name = result.strip()[1:].split(" ", 1)[0] if result.strip().startswith("/") else ""
                if name in INSTANT_COMMANDS:  # 只看状态、改下次请求的设置：马上执行，不排队
                    self.echo_user(result, restart_timer=False)  # type: ignore[attr-defined]
                    self.handle_command(result.strip())  # type: ignore[attr-defined]
                    self._block_start = False  # type: ignore[attr-defined]  # 命令打印了东西：下一块前要空一行
                    continue
                with self._queue_lock:
                    self._queued.append(result)  # type: ignore[attr-defined]
        finally:
            worker.join()
            self.agent.interjections = None
            box.turn = None
            self.console = real
            writer.drain()  # 还有没打出来的（输入框收起后才写的）直接打
            self._turn_live_render = self._turn_activity = None
        self._prefill = draft  # type: ignore[attr-defined]  # 框里敲了一半的字：下次输入框里接着写
        if "error" in outcome:
            raise outcome["error"]

    # ---------- 后台线程 → 主线程 ----------

    def _approve_from_worker(self, tool: Tool, args: dict[str, Any]) -> Any:
        """后台线程要确认：交给主线程（输入框收起、弹确认框），等它答完。一次只问一个。"""
        with self._turn_approval_lock:
            done, answer = threading.Event(), [None]
            self._turn_approval = (tool, args, done, answer)
            box = self._box
            assert box is not None
            writer = self.console.file
            box.signal("approval", before=getattr(writer, "drain", None))
            done.wait()
            return answer[0]

    def _serve_approval(self, real: Console, writer: _AboveWriter) -> None:
        """主线程：输入框已经收起，用真正的控制台照旧弹确认框。确认期间别的线程（并行的子智能体）的输出也直接打。"""
        request = self._turn_approval
        if request is None:
            return
        tool, args, done, answer = request
        writer.drain()
        capture, self.console = self.console, real
        try:
            answer[0] = self.approve(tool, args)  # type: ignore[attr-defined]
        except (KeyboardInterrupt, EOFError):
            answer[0] = "deny"
        finally:
            self.console = capture
            self._turn_approval = None
            done.set()

    def _take_interjections(self) -> list[str]:
        """后台线程（智能体）在每批工具跑完时调：把排着队的普通的话取走、插进这一问。命令（/ ! #）留着，结束后执行。"""
        with self._queue_lock:
            said = [q for q in self._queued if q.lstrip()[:1] not in ("/", "!", "#")]  # type: ignore[attr-defined]
            self._queued[:] = [q for q in self._queued if q not in said]  # type: ignore[attr-defined]
        return said

    def _take_queued(self) -> list[str]:
        """输入框里按 ↑：排着队的全部取回来改（界面线程调）。"""
        with self._queue_lock:
            taken, self._queued[:] = list(self._queued), []  # type: ignore[attr-defined]
        return taken

    def _turn_interrupt(self, how: str) -> None:
        """ESC / Ctrl+C：打断这一问（智能体在后台线程，取消靠标志与关掉连接）。"""
        self._esc_pressed = how == "esc"  # type: ignore[attr-defined]
        self.agent.cancel()
        self._turn_activity = None

    # ---------- 「正在进行」那块 ----------

    def _turn_live_text(self) -> AnyFormattedText:
        """输入框上方那块：还没写完的那段回复（最后几行）+ 转圈与计时。每 0.1 秒重画时调。"""
        parts: list[Any] = []
        if self._turn_live_render is not None:
            parts.append(self._turn_live_render)
        if self._turn_activity is not None:
            parts.append(_Spinner(self._turn_activity))
        if not parts:
            return ""
        real = self._turn_real_console
        out = io.StringIO()
        Console(file=out, force_terminal=True, color_system=real.color_system or "standard", width=real.width,
                legacy_windows=False, highlight=False).print(Group(*parts))  # fmt: skip
        lines = out.getvalue().rstrip("\n").split("\n")[-LIVE_MAX_LINES:]
        if not getattr(self, "_block_start", True):  # 和上方已经打印的内容隔开一行，同 Repl._gap（不然贴着工具结果）
            lines.insert(0, "")
        return ANSI("\n".join(lines))


class _Spinner:
    """转圈的点 + 文字：rich 的 Status 自己会重画，这里只要一帧（输入框每 0.1 秒重画一次，按时间取帧）。"""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: Any) -> None:
        self.text = text

    def __rich__(self) -> Any:
        import time

        from rich.table import Table

        frame = self.FRAMES[int(time.monotonic() * 10) % len(self.FRAMES)]
        grid = Table.grid(padding=(0, 1))
        grid.add_row(f"[green]{frame}[/]", self.text)
        return grid
