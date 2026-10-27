"""方向键选择器：确认对话、/rewind、/resume、/model 共用。

↑↓（或 j / k）移动、Enter 选定；每项可以有快捷字母直接选；ESC 取消（返回 None）。
在当前位置临时画一块（不是全屏），选完擦掉，由调用方把结果打印成一行。
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style

STYLE = Style.from_dict(
    {
        "title": "bold",
        "current": "bold #61afef",
        "key": "#e5c07b",
        "hint": "#5c6370",
        "detail": "#7f848e",
    }
)


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    key: str = ""  # 快捷字母（可选）
    detail: str = ""  # 灰色补充说明（可选）


def choose(
    title: str,
    options: list[Option],
    *,
    default: int = 0,
    max_visible: int = 12,
    input: Input | None = None,  # 测试里用管道输入
    output: Output | None = None,
) -> str | None:
    """弹出选择器，返回选中项的 value；ESC / Ctrl+C 取消返回 None。"""
    if not options:
        return None
    state = {"index": max(0, min(default, len(options) - 1))}

    def render() -> FormattedText:
        index = state["index"]
        top = min(max(0, index - max_visible // 2), max(0, len(options) - max_visible))
        parts: list[tuple[str, str]] = [("class:title", f" {title}\n")]
        for i in range(top, min(len(options), top + max_visible)):
            opt = options[i]
            pointer, style = ("❯ ", "class:current") if i == index else ("  ", "")
            key = f"[{opt.key}] " if opt.key else ""
            parts += [(style, f" {pointer}"), ("class:key", key), (style, opt.label)]
            if opt.detail:
                parts.append(("class:detail", f"  {opt.detail}"))
            parts.append(("", "\n"))
        if len(options) > max_visible:
            parts.append(("class:hint", f"   （共 {len(options)} 项）\n"))
        parts.append(("class:hint", " ↑↓ 选择 · Enter 确定 · ESC 取消"))
        return FormattedText(parts)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    def _up(event: KeyPressEvent) -> None:
        state["index"] = (state["index"] - 1) % len(options)

    @kb.add("down")
    @kb.add("j")
    def _down(event: KeyPressEvent) -> None:
        state["index"] = (state["index"] + 1) % len(options)

    @kb.add("enter")
    def _enter(event: KeyPressEvent) -> None:
        event.app.exit(result=options[state["index"]].value)

    @kb.add("escape")  # 不用 eager：方向键在 VT 输入里是 ESC [ A，让 prompt_toolkit 先判断完整序列
    @kb.add("c-c")
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    for opt in options:
        if opt.key:

            @kb.add(opt.key.lower())
            def _pick(event: KeyPressEvent, value: str = opt.value) -> None:
                event.app.exit(result=value)

    app: Application[str | None] = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(render), dont_extend_height=True)])),
        key_bindings=kb,
        style=STYLE,
        full_screen=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
    app.ttimeoutlen = 0.1  # 单独 ESC 别等默认的 0.5 秒
    return app.run()


def ask_line(message: str, *, password: bool = False, input: Input | None = None,
             output: Output | None = None) -> str:  # fmt: skip
    """一行输入（网址、名字、密钥……）：ESC 或 Ctrl+C 取消（抛 KeyboardInterrupt，由调用方说「已取消」）；
    password=True 时输入不显示。"""
    from prompt_toolkit import PromptSession

    kb = KeyBindings()

    @kb.add("escape")
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    session: PromptSession[str] = PromptSession(key_bindings=kb, is_password=password, input=input, output=output)
    session.app.ttimeoutlen = 0.1
    return session.prompt(message)
