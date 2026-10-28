"""带边框的输入框：在当前位置临时渲染一个小界面（不是全屏，终端原有的滚动与复制不受影响），提交后擦掉。

- 边框颜色跟着权限模式变（ask 蓝、accept-edits 紫、plan 绿、auto 黄），边框标题上是按键提示；
- 多行输入：Enter 发送，Ctrl+J（或 Alt+Enter）换行；
- 补全：行首 `/` 补命令，任意位置的 `@` 补文件路径；
- 框下面是状态栏；Shift+Tab 切换权限模式；
- Ctrl+C：框里有字就清空；空框里 2 秒内连按两次才退出；Ctrl+D 在空框里退出；Ctrl+L 清屏；
- ESC：框里有字就清空；空框里双击返回 REWIND，由界面弹出回退列表；
- Ctrl+R 搜历史输入；Ctrl+G 用外部编辑器写长消息；Ctrl+T 显示 / 隐藏输入框上方的任务清单。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, merge_completers
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions, is_searching
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.history import History
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings
from prompt_toolkit.key_binding.defaults import load_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, FloatContainer, HSplit, Layout, VerticalAlign, VSplit, Window
from prompt_toolkit.layout.containers import Float
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output
from prompt_toolkit.search import accept_search
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import SearchToolbar

from heng_agent.tools.paths import SKIP_DIRS
from heng_agent.ui.prompt import CmdCompletion
from heng_agent.ui.winkeys import enable_shift_enter

FRAME_COLORS = {"ask": "#61afef", "accept-edits": "#c678dd", "plan": "#98c379", "auto": "#e5c07b"}
TITLE = " Shift+Enter 换行 · / 命令 · @ 文件 · Alt+V 粘贴图片 "
MAX_LINES = 10  # 输入框最多长到多少行（更长的内容在框内滚动）
MENU_ROWS = 8  # 补全菜单最多显示几行
MAX_PATH_COMPLETIONS = 40
EXIT_CONFIRM_SECONDS = 2.0  # 空框里连按两次 Ctrl+C 的时间窗
DOUBLE_ESC_SECONDS = 1.0  # 空框里双击 ESC 的时间窗
REWIND = "/rewind"  # 双击 ESC 时 prompt() 返回这个，由界面弹出回退列表
ESC_TIMEOUT = 0.1  # 单独 ESC 的确认等待（秒）
KEY_SEQUENCE_TIMEOUT = 0.2  # 多键绑定（Alt+V = ESC v）等下一个键的时间；终端里这些组合键是整段到达的
MAX_TODO_LINES = 12  # 输入框上方任务清单最多占几行


class PathCompleter(Completer):
    """光标前最后一个词以 @ 开头时，补全工作目录里的文件与目录（跳过 .git、node_modules 等）。"""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        word = document.get_word_before_cursor(WORD=True)
        if not word.startswith("@"):
            return
        partial = word[1:].replace("\\", "/")
        folder, _, prefix = partial.rpartition("/")
        base = (self.workdir / folder).resolve() if folder else self.workdir
        try:
            if base != self.workdir.resolve() and self.workdir.resolve() not in base.parents:
                return  # 不补全工作目录以外的路径
            entries = sorted(os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        shown = 0
        for entry in entries:
            if entry.name in SKIP_DIRS or not entry.name.lower().startswith(prefix.lower()):
                continue
            rel = f"{folder}/{entry.name}" if folder else entry.name
            suffix = "/" if entry.is_dir() else ""
            yield Completion(f"@{rel}{suffix}", start_position=-len(word), display=entry.name + suffix,
                             display_meta="目录" if suffix else "")  # fmt: skip
            shown += 1
            if shown >= MAX_PATH_COMPLETIONS:
                return


TURN_TITLE = " 回答中：Enter 排队发送 · ESC 中断 · Ctrl+B 转后台 "


@dataclass
class TurnHooks:
    """一问正在进行时输入框要用的东西（Repl 提供）。

    live：输入框上方「正在进行」那块的内容（转圈与计时、还没写完的那段回复），每 0.1 秒重画一次；
    queued：排着队的话（普通的话在这批工具跑完时插进这一问，命令等这一问结束再执行）；
    interrupt：ESC / Ctrl+C 打断这一问；detach：Ctrl+B 前台命令转后台；
    before_draw：每次画出输入框之前调（Repl 把攒着、还没打到屏幕上的输出先打出来）。
    """

    live: Callable[[], AnyFormattedText]
    queued: Callable[[], list[str]]
    interrupt: Callable[[str], None]
    detach: Callable[[], None]
    before_draw: Callable[[], None]
    take_queued: Callable[[], list[str]] = lambda: []  # ↑ 把排着队的话全部取回输入框里改


@dataclass(frozen=True)
class TurnSignal:
    """别的线程让输入框收起来时 prompt 的返回值：kind 是 done（这一问结束了）或 approval（要你确认），
    draft 是框里已经敲了、还没发的字（接着画输入框时放回去）。"""

    kind: str
    draft: str


class InputBox:
    def __init__(
        self,
        *,
        history: History,
        commands: Completer,
        workdir: Path,
        toolbar: Callable[[], AnyFormattedText],
        get_mode: Callable[[], str],
        set_mode: Callable[[str], None],
        modes: list[str],
        style: Style,
        input: Input | None = None,  # 测试里用管道输入
        output: Output | None = None,
        on_paste: Callable[[str], str | None] | None = None,
        on_clipboard: Callable[[], tuple[str, str]] | None = None,
        on_ctrl_v: Callable[[], tuple[str, str]] | None = None,
        on_editor: Callable[[str], str | None] | None = None,
        todos: Callable[[], str] | None = None,
        agents: Callable[[], list[tuple[int, str]]] | None = None,
        on_view_agent: Callable[[int], None] | None = None,
    ) -> None:
        """on_paste：粘贴的文字是文件路径时返回替换文字（拖入文件就是这样），否则 None；
        on_clipboard：Alt+V 时调用，返回 (要插入的文字, 显示在状态栏下的说明)；
        on_ctrl_v：Ctrl+V 时调用（同上，另带文字兜底）。Windows Terminal 会自己处理 Ctrl+V，到不了这里；
        把 Ctrl+V 交给程序的终端里它就能直接粘截图。
        agents：正在跑的子智能体 [(编号, 一行说明)]，有就在状态栏下面画面板；on_view_agent：选中一个按 Enter。"""
        self.history = history
        self.completer = merge_completers([commands, PathCompleter(workdir)])
        self.toolbar = toolbar
        self.app: Application[str] | None = None  # 正在显示的输入框（prompt 期间），refresh 用
        self.get_mode, self.set_mode, self.modes = get_mode, set_mode, modes
        self.style = style
        self.input, self.output = input, output
        self.on_paste, self.on_clipboard, self.on_ctrl_v = on_paste, on_clipboard, on_ctrl_v
        self.on_editor = on_editor  # Ctrl+G：用外部编辑器写长消息，返回改好的文字（None = 没改）
        self.todos = todos  # 当前任务清单的文字（空 = 没有），显示在输入框上方
        self.show_todos = True  # Ctrl+T 切换
        self.agents = agents or (lambda: [])
        self.on_view_agent = on_view_agent
        #: 子智能体面板里选中的那一行（None = 没在选；在输入框最后一行按 ↓ 进入，见 _bindings）
        self.agent_pick: int | None = None
        self.flash = ""  # 一次性提示（如「已粘贴图片」），显示在状态栏下面
        self._last_ctrl_c = -1e9  # 空框里上一次按 Ctrl+C 的时间（连按两次才退出）
        self._last_esc = -1e9  # 上一次按 ESC 的时间（空框里双击 ESC 回退）
        #: 一问正在进行时由 Repl 设上（None = 平常的输入框）。见 TurnHooks
        self.turn: TurnHooks | None = None
        self._signals: list[str] = []  # 别的线程要求收起输入框（输入框还没画出来时先记着，画出来立刻收）
        self._signal_lock = threading.Lock()
        self._buffer: Buffer | None = None  # 正在用的输入区（收起时取出没发的字）

    def _bindings(self, buffer: Buffer) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter", filter=is_searching)
        def _accept_search(event: KeyPressEvent) -> None:
            # Ctrl+R 搜到后按 Enter：采用这条，光标放到行尾（内置做法把光标留在匹配处，接着打字会插到前面去）
            accept_search()
            buffer.cursor_position = len(buffer.text)

        @kb.add("enter", filter=~is_searching)  # Ctrl+R 搜索中的 Enter 是「采用搜到的」，由上面处理，不直接发送
        def _submit(event: KeyPressEvent) -> None:
            # Enter = 执行。菜单里选着的（或没选、但第一项是命令）是命令：不必带参数就直接执行，
            # 必须带参数的只补全、加空格等你输；@ 文件之类的补全只在选中时才采用（没选就是想发送当前文字）
            state = buffer.complete_state
            if state is not None:
                comp = state.current_completion
                if comp is None and state.completions and isinstance(state.completions[0], CmdCompletion):
                    comp = state.completions[0]
                if comp is not None:
                    buffer.apply_completion(comp)
                    if isinstance(comp, CmdCompletion) and comp.needs_more:
                        buffer.insert_text(" ")
                        return
                    if not isinstance(comp, CmdCompletion):
                        return  # @ 文件等：只是填进去，接着写
            event.app.exit(result=buffer.text)

        @kb.add("tab")
        def _tab(event: KeyPressEvent) -> None:
            # Tab = 只补全不执行；命令后面还能跟参数就加个空格，接着提示参数
            state = buffer.complete_state
            if state is None or not state.completions:
                buffer.start_completion(select_first=True)
                return
            comp = state.current_completion or state.completions[0]
            buffer.apply_completion(comp)
            if isinstance(comp, CmdCompletion) and comp.takes_more:
                buffer.insert_text(" ")

        @kb.add("c-j")
        @kb.add("escape", "enter")
        def _newline(event: KeyPressEvent) -> None:
            buffer.insert_text("\n")

        @kb.add("s-tab")
        def _cycle(event: KeyPressEvent) -> None:
            current = self.get_mode()
            nxt = (
                self.modes[(self.modes.index(current) + 1) % len(self.modes)]
                if current in self.modes
                else self.modes[0]
            )
            self.set_mode(nxt)
            event.app.invalidate()

        @kb.add("up", filter=Condition(lambda: self.turn is not None and bool(self.turn.queued())
                                     and buffer.document.cursor_position_row == 0))  # fmt: skip
        def _take_back(event: KeyPressEvent) -> None:
            # 回答中、光标在第一行：把排着队的话全部取回来改（和 Claude Code 一样，一条一行，放在已经敲的字前面）。
            # 改完 Enter 重新排进去；清空就是不发了
            if self.turn is None:
                return
            taken = self.turn.take_queued()
            if taken:
                text = "\n".join(taken) + ("\n" + buffer.text if buffer.text else "")
                buffer.document = Document(text, len(text))

        picking = Condition(lambda: self.agent_pick is not None)

        def at_bottom() -> bool:
            # 光标在最后一行、而且不在翻历史（翻到一半时 ↓ 是回到更新的历史）
            doc = buffer.document
            return (
                doc.cursor_position_row == doc.line_count - 1 and buffer.working_index == len(buffer._working_lines) - 1
            )

        @kb.add("down", filter=~picking & Condition(lambda: bool(self.agents()) and at_bottom()))
        def _enter_agents(event: KeyPressEvent) -> None:
            self.agent_pick = 0  # 进入子智能体面板，选中第一个（照 Claude Code：↓ 选子智能体）

        @kb.add("down", filter=picking)
        def _agent_next(event: KeyPressEvent) -> None:
            rows = self.agents()
            self.agent_pick = min((self.agent_pick or 0) + 1, max(len(rows) - 1, 0))

        @kb.add("up", filter=picking)
        def _agent_prev(event: KeyPressEvent) -> None:
            self.agent_pick = None if not self.agent_pick else self.agent_pick - 1  # 第一行再按 ↑ 回到输入框

        @kb.add("enter", filter=picking)
        def _agent_view(event: KeyPressEvent) -> None:
            rows = self.agents()
            if rows and self.on_view_agent is not None:
                self.on_view_agent(rows[min(self.agent_pick or 0, len(rows) - 1)][0])
            self.agent_pick = None

        @kb.add("escape", filter=picking, eager=True)
        def _agent_leave(event: KeyPressEvent) -> None:
            self.agent_pick = None

        @kb.add("c-b", filter=Condition(lambda: self.turn is not None))
        def _detach(event: KeyPressEvent) -> None:
            if self.turn is not None:
                self.turn.detach()

        def clear_saving_draft() -> None:
            """清空输入框，但先把草稿存进历史：清掉了还能按 ↑ 找回来（Claude Code 也是这样，以前清了就没了）。"""
            if buffer.text.strip():
                buffer.append_to_history()
            buffer.reset()

        @kb.add("c-c")
        def _interrupt(event: KeyPressEvent) -> None:
            if self.turn is not None:  # 回答中：打断这一问（框里有草稿也打断，草稿留着不发——和 Claude Code 一样）
                self.turn.interrupt("ctrl-c")
                return
            if buffer.text:
                clear_saving_draft()  # 框里有字：先清空（↑ 能找回）
                self.flash = "已清空（↑ 找回）· 再按一次 Ctrl+C 退出"
                self._last_ctrl_c = time.monotonic()
                return
            # 空框：2 秒内连按两次才退出——中断任务时多按了一下、清空后顺手又按，都不该丢掉整个会话
            now = time.monotonic()
            if now - self._last_ctrl_c <= EXIT_CONFIRM_SECONDS:
                event.app.exit(exception=KeyboardInterrupt())
            else:
                self._last_ctrl_c = now
                self.flash = "再按一次 Ctrl+C 退出"

        # 不用 eager（Alt+V、方向键都以 ESC 开头）；搜索中的 ESC 是取消搜索；在子智能体面板里选的时候 ESC 是退出选择
        @kb.add("escape", filter=~is_searching & ~picking)
        def _escape(event: KeyPressEvent) -> None:
            now = time.monotonic()
            if self.turn is not None:  # 回答中：打断这一问；框里的草稿留着不发，排队的话接着发（同 Claude Code）
                self.turn.interrupt("esc")
                return
            double = now - self._last_esc <= DOUBLE_ESC_SECONDS
            self._last_esc = now
            if buffer.text:
                # 有字：双击才清空（单击清空太容易误删一大段），清空前存进历史、↑ 能找回
                if double:
                    clear_saving_draft()
                    self.flash = "已清空（↑ 找回）"
                    self._last_esc = -1e9  # 清空后马上再按两下才是「回退」，不接着算
                else:
                    self.flash = "再按一次 ESC 清空（↑ 找回）"
            elif double:
                event.app.exit(result=REWIND)  # 空框里双击 ESC：回退到之前的某一问
            else:
                self.flash = "再按一次 ESC 回退到之前的某一问"

        @kb.add("escape", "escape", filter=~is_searching & ~picking)
        def _double_escape(event: KeyPressEvent) -> None:
            # 两下 ESC 隔得比 KEY_SEQUENCE_TIMEOUT 还近时，prompt_toolkit 把它们当成一个「ESC ESC」组合键，
            # 没有绑定的话两下都被丢掉——快速双击什么也不发生（真实终端里实测）。按「刚按过一下」处理第二下
            if self.turn is not None:
                self.turn.interrupt("esc")
                return
            self._last_esc = time.monotonic()
            _escape(event)

        @kb.add("c-t")
        def _toggle_todos(event: KeyPressEvent) -> None:
            self.show_todos = not self.show_todos
            if not (self.todos and self.todos()):
                self.flash = "还没有任务清单"

        @kb.add("c-g")
        async def _editor(event: KeyPressEvent) -> None:
            # 用外部编辑器写长消息：run_in_terminal 先把输入框收起来，编辑器关掉后再画回来
            if self.on_editor is None:
                return
            edited = await run_in_terminal(lambda: self.on_editor(buffer.text), in_executor=True)  # type: ignore[misc]
            if edited is not None:
                buffer.document = Document(edited, len(edited))

        @kb.add("c-d")
        def _eof(event: KeyPressEvent) -> None:
            if not buffer.text and self.turn is None:  # 回答中不退出
                event.app.exit(exception=EOFError())

        @kb.add("c-l")
        def _clear_screen(event: KeyPressEvent) -> None:
            # 清屏：只清屏幕显示，对话历史与输入框里的字都保留（和大多数终端程序习惯一致）
            event.app.renderer.clear()

        @kb.add(Keys.BracketedPaste)
        def _paste(event: KeyPressEvent) -> None:
            # 拖入文件 = 终端粘贴它的路径：是路径就换成 @，否则按普通文字粘贴（换行统一成 \n）
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            converted = self.on_paste(data) if self.on_paste else None
            buffer.insert_text(converted if converted is not None else data)
            if converted is not None:
                self.flash = "已把粘贴的文件路径换成 @ 引用"

        @kb.add("escape", "v")
        def _clipboard(event: KeyPressEvent) -> None:
            if self.on_clipboard is None:
                return
            text, note = self.on_clipboard()
            if text:
                buffer.insert_text(text)
            self.flash = note

        @kb.add("c-v")
        def _ctrl_v(event: KeyPressEvent) -> None:
            if self.on_ctrl_v is None:
                return
            text, note = self.on_ctrl_v()
            if text:
                buffer.insert_text(text.replace("\r\n", "\n"))
            self.flash = note

        return kb

    def build_layout(self, buffer: Buffer) -> tuple[FloatContainer, Window]:
        """圆角边框输入框 + 状态栏 + 补全菜单。返回 (整体, 输入区)。单独成方法便于测试高度。"""

        def body_height() -> Dimension:
            """输入区高度 = 实际显示的行数（含自动折行），1 到 MAX_LINES 行，**固定值**。

            不能只给 min/max：非全屏模式下 prompt_toolkit 会把光标下方剩余的行都分给界面，
            能伸长的部分（边框竖线、输入区）会被一路拉到窗口底部。
            """
            width = max(10, get_app().output.get_size().columns - 6)  # 减去两侧竖线、留白与提示符
            rows = sum(max(1, -(-get_cwidth(line) // width)) for line in buffer.document.lines)
            return Dimension.exact(min(max(1, rows), MAX_LINES))

        search = SearchToolbar(
            text_if_not_searching="", forward_search_prompt="向后搜：", backward_search_prompt="搜索历史："
        )
        body = Window(
            BufferControl(buffer=buffer, search_buffer_control=search.control),
            height=body_height,
            wrap_lines=True,
            get_line_prefix=lambda line, wrap: [("class:prompt", "› " if line == 0 and not wrap else "  ")],
        )

        def bar(char: str) -> Window:
            return Window(width=1, height=body_height, char=char)  # 竖线和输入区一样高，不会被拉长

        top = VSplit(
            [
                Window(width=1, height=1, char="╭"),
                Window(width=1, height=1, char="─"),
                Window(
                    FormattedTextControl(lambda: [("class:frame-title", TITLE if self.turn is None else TURN_TITLE)]),
                    height=1,
                    dont_extend_width=True,
                ),
                Window(height=1, char="─"),
                Window(width=1, height=1, char="╮"),
            ]
        )
        middle = VSplit([bar("│"), bar(" "), body, bar(" "), bar("│")])
        bottom = VSplit(
            [Window(width=1, height=1, char="╰"), Window(height=1, char="─"), Window(width=1, height=1, char="╯")]
        )
        # 边框颜色跟着权限模式变：HSplit 的 style 可以是函数，里面的边框字符继承它
        frame = HSplit([top, middle, bottom], style=lambda: f"fg:{FRAME_COLORS.get(self.get_mode(), '#61afef')}")
        # 状态栏两行（信息 + 权限模式），用户的状态栏命令可能输出更多行：高度跟着内容走
        status = Window(FormattedTextControl(self.toolbar), dont_extend_height=True, style="class:bottom-toolbar")

        def todo_text() -> str:
            return (self.todos() if self.todos else "").rstrip()

        todo_box = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: [("class:todos", " " + todo_text().replace("\n", "\n "))]),
                height=lambda: Dimension.exact(min(len(todo_text().splitlines()), MAX_TODO_LINES)),
            ),
            filter=Condition(lambda: self.show_todos and bool(todo_text())),
        )
        turn_active = Condition(lambda: self.turn is not None)
        # 正在进行：转圈与计时、还没写完的那段回复（写完的段落已经打印到上方，见 Repl._run_turn_live）
        # 至少留一行：一问刚开始时这块是空的（高 0），转圈一出现就长成 1 行，界面整体变高的那一下画错了位置，
        # 转圈画进了输入框里（真实终端里实测）。一开始就占住一行，高度不再从 0 跳起来
        live = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: self.turn.live() if self.turn else ""),
                dont_extend_height=True,
                height=lambda: Dimension(min=1),
            ),
            filter=turn_active,
        )

        def queued_text() -> list[tuple[str, str]]:
            items = self.turn.queued() if self.turn else []
            rows = []
            for q in items:
                when = "结束后执行" if q.lstrip()[:1] in ("/", "!", "#") else "工具跑完后插进这一问"
                rows.append(f" ⧗ 排队（{when}）：{q.splitlines()[0][:80]}")
            rows.append(" ↑ 取回来改")
            return [("class:queued", "\n".join(rows))]

        queued = ConditionalContainer(
            Window(FormattedTextControl(queued_text), dont_extend_height=True),
            filter=Condition(lambda: bool(self.turn and self.turn.queued())),
        )
        # 回答中在「正在进行」与输入框之间空一行：不空的话正在写的那段紧贴着输入框的上边框，看着挤（用户反馈）
        gap = ConditionalContainer(Window(height=Dimension.exact(1)), filter=turn_active)
        flash = ConditionalContainer(
            Window(FormattedTextControl(lambda: [("class:flash", f" {self.flash}")]), height=1),
            filter=Condition(lambda: bool(self.flash)),
        )

        def agents_text() -> list[tuple[str, str]]:
            rows = self.agents()
            if not rows:
                self.agent_pick = None  # 都做完了：退出选择
                return []
            pick = self.agent_pick
            hint = "  ↑↓ 选择 · Enter 查看 · ESC 返回" if pick is not None else "  ↓ 选中查看"
            out: list[tuple[str, str]] = [("", "\n"), ("class:agents.main", " ● main"), ("class:agents.hint", hint)]
            for i, (_, line) in enumerate(rows):
                style = "class:agents.pick" if pick is not None and i == min(pick, len(rows) - 1) else "class:agents"
                out += [("", "\n"), (style, f" {'❯' if style.endswith('pick') else '○'} {line}")]
            return out

        agents = ConditionalContainer(
            Window(FormattedTextControl(agents_text), dont_extend_height=True),
            filter=Condition(lambda: bool(self.agents())),
        )
        # 补全菜单弹出时在下方留出空间：输入框只有一行高，不留的话菜单会被截掉（PromptSession 也是这么做的）
        menu_space = ConditionalContainer(Window(height=Dimension.exact(MENU_ROWS)), filter=has_completions)
        root = FloatContainer(
            # 顶部对齐：光标下方多出来的空间留在最下面，不分给输入框
            content=HSplit(
                [live, queued, gap, todo_box, frame, search, status, agents, flash, menu_space],
                align=VerticalAlign.TOP,
            ),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=MENU_ROWS, scroll_offset=1))],
        )
        return root, body

    def signal(self, kind: str, before: Callable[[], None] | None = None) -> None:
        """别的线程要求收起输入框（这一问结束了、要你确认）：prompt 返回 TurnSignal。线程安全。

        before：收起之前在界面线程里调（把攒着的输出打出来，免得收起后才看到）。输入框此刻没画出来
        （刚收起、正在确认框里）就先记着，下次画出来时立刻收。
        """
        with self._signal_lock:
            self._signals.append(kind)
        app = self.app
        loop = app.loop if app is not None else None
        if app is not None and loop is not None and app.is_running:
            loop.call_soon_threadsafe(lambda: self._exit_for_signal(app, before))

    def _exit_for_signal(self, app: Application[Any], before: Callable[[], None] | None) -> None:
        with self._signal_lock:
            if not self._signals or app.future is None or app.future.done():
                return
            kind = self._signals.pop(0)
        draft = self._buffer.text if self._buffer is not None else ""

        async def finish() -> None:
            if before is not None:
                await run_in_terminal(before)
            if app.future is not None and not app.future.done():
                app.exit(result=TurnSignal(kind, draft))  # type: ignore[arg-type]

        app.create_background_task(finish())

    def refresh(self) -> None:
        """叫正在显示的输入框重画（状态栏内容变了时）。可以在别的线程里调：invalidate 是线程安全的。"""
        app = self.app
        if app is not None:
            app.invalidate()

    def print_above(self, func: Callable[[], object]) -> bool:
        """输入框开着时，在界面线程里把输入框收起、调 func 打印、再画回来（输出留在输入框上方）。

        可以在别的线程里调：通过事件循环转到界面线程执行，func 运行时主线程正停在输入框里、没有别的事在做。
        返回是否已安排；输入框没开着（模型正在回答、或在别的提问里）返回 False，由调用方另找时机打印。
        """
        app = self.app
        loop = app.loop if app is not None else None
        if app is None or loop is None or not app.is_running:
            return False

        def schedule() -> None:
            with set_app(app):  # run_in_terminal 用 get_app() 找界面：事件循环的回调里要先设好
                run_in_terminal(func)

        loop.call_soon_threadsafe(schedule)
        return True

    def prompt(self, default: str = "") -> str:
        """default：预填进输入框的文字（/rewind 之后把那一问放回来编辑重发）。"""
        enable_shift_enter()  # Windows 上让 Shift+Enter 换行（重复调用无副作用）
        self.flash = ""
        buffer = Buffer(history=self.history, completer=self.completer, complete_while_typing=True, multiline=True)
        if default:
            buffer.document = Document(default, len(default))
        root, body = self.build_layout(buffer)
        app: Application[str] = Application(
            layout=Layout(root, focused_element=body),
            key_bindings=merge_key_bindings([load_key_bindings(), self._bindings(buffer)]),
            style=self.style,
            full_screen=False,
            erase_when_done=True,  # 提交后擦掉输入框，由调用方把这句话按对话格式打印出来
            input=self.input,
            output=self.output,
        )
        # 单独的 ESC 要等这么久才确认（区分 Alt 组合键、方向键这些 ESC 开头的序列）：默认 0.5 秒按下去像没反应；
        # 那些序列都是一次整段到达的，0.1 秒足够区分
        app.ttimeoutlen = ESC_TIMEOUT
        # 另一个等待：有 Alt+V、Alt+Enter 这些「ESC 开头」的组合键绑定时，单独的 ESC 还要再等 timeoutlen（默认 1 秒）
        # 看后面跟不跟键——两次 ESC 各被拖近 1 秒，间隔超出双击窗口，双击 ESC 就认不出来（实测卡住）
        app.timeoutlen = KEY_SEQUENCE_TIMEOUT
        # 计时、转圈、流式文字要动起来；平时有后台子智能体在跑时面板上的用时也要动
        app.refresh_interval = 0.1 if self.turn is not None else 0.5
        self._buffer = buffer
        self.agent_pick = None
        buffer.on_text_changed += lambda _: setattr(self, "agent_pick", None)  # 接着打字：退出面板选择
        # 历史是异步加载的：PromptSession 会自己触发，这里手搭的 Application 要主动做，否则上箭头取不到历史
        self.app = app
        try:
            text = app.run(pre_run=lambda: self._pre_run(app, buffer))
        finally:
            self.app = None
        if isinstance(text, TurnSignal):
            return text  # type: ignore[return-value]
        if text.strip():
            buffer.append_to_history()
        return text

    def _pre_run(self, app: Application[Any], buffer: Buffer) -> None:
        buffer.load_history_if_not_yet_loaded()
        if self.turn is not None:
            self.turn.before_draw()
        with self._signal_lock:
            pending = bool(self._signals)
        if pending:  # 收起期间来了信号（如确认完之后这一问正好结束）：一画出来就收
            self._exit_for_signal(app, None)
