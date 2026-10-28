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
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from prompt_toolkit.application import Application, get_app, run_in_terminal
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
    ) -> None:
        """on_paste：粘贴的文字是文件路径时返回替换文字（拖入文件就是这样），否则 None；
        on_clipboard：Alt+V 时调用，返回 (要插入的文字, 显示在状态栏下的说明)；
        on_ctrl_v：Ctrl+V 时调用（同上，另带文字兜底）。Windows Terminal 会自己处理 Ctrl+V，到不了这里；
        把 Ctrl+V 交给程序的终端里它就能直接粘截图。"""
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
        self.flash = ""  # 一次性提示（如「已粘贴图片」），显示在状态栏下面
        self._last_ctrl_c = -1e9  # 空框里上一次按 Ctrl+C 的时间（连按两次才退出）
        self._last_esc = -1e9  # 上一次按 ESC 的时间（空框里双击 ESC 回退）

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

        @kb.add("c-c")
        def _interrupt(event: KeyPressEvent) -> None:
            if buffer.text:
                buffer.reset()  # 框里有字：先清空
                return
            # 空框：2 秒内连按两次才退出——中断任务时多按了一下、清空后顺手又按，都不该丢掉整个会话
            now = time.monotonic()
            if now - self._last_ctrl_c <= EXIT_CONFIRM_SECONDS:
                event.app.exit(exception=KeyboardInterrupt())
            else:
                self._last_ctrl_c = now
                self.flash = "再按一次 Ctrl+C 退出"

        @kb.add("escape", filter=~is_searching)  # 不用 eager（Alt+V、方向键都以 ESC 开头）；搜索中的 ESC 是取消搜索
        def _escape(event: KeyPressEvent) -> None:
            now = time.monotonic()
            if buffer.text:
                buffer.reset()  # 有字：ESC 清空
                self._last_esc = now
            elif now - self._last_esc <= DOUBLE_ESC_SECONDS:
                event.app.exit(result=REWIND)  # 空框里双击 ESC：回退到之前的某一问
            else:
                self._last_esc = now
                self.flash = "再按一次 ESC 回退到之前的某一问"

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
            if not buffer.text:
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
                Window(FormattedTextControl([("class:frame-title", TITLE)]), height=1, dont_extend_width=True),
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
        status = Window(FormattedTextControl(self.toolbar), height=1, style="class:bottom-toolbar")

        def todo_text() -> str:
            return (self.todos() if self.todos else "").rstrip()

        todo_box = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: [("class:todos", " " + todo_text().replace("\n", "\n "))]),
                height=lambda: Dimension.exact(min(len(todo_text().splitlines()), MAX_TODO_LINES)),
            ),
            filter=Condition(lambda: self.show_todos and bool(todo_text())),
        )
        flash = ConditionalContainer(
            Window(FormattedTextControl(lambda: [("class:flash", f" {self.flash}")]), height=1),
            filter=Condition(lambda: bool(self.flash)),
        )
        # 补全菜单弹出时在下方留出空间：输入框只有一行高，不留的话菜单会被截掉（PromptSession 也是这么做的）
        menu_space = ConditionalContainer(Window(height=Dimension.exact(MENU_ROWS)), filter=has_completions)
        root = FloatContainer(
            # 顶部对齐：光标下方多出来的空间留在最下面，不分给输入框
            content=HSplit([todo_box, frame, search, status, flash, menu_space], align=VerticalAlign.TOP),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=MENU_ROWS, scroll_offset=1))],
        )
        return root, body

    def refresh(self) -> None:
        """叫正在显示的输入框重画（状态栏内容变了时）。可以在别的线程里调：invalidate 是线程安全的。"""
        app = self.app
        if app is not None:
            app.invalidate()

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
        # 历史是异步加载的：PromptSession 会自己触发，这里手搭的 Application 要主动做，否则上箭头取不到历史
        self.app = app
        try:
            text = app.run(pre_run=buffer.load_history_if_not_yet_loaded)
        finally:
            self.app = None
        if text.strip():
            buffer.append_to_history()
        return text
