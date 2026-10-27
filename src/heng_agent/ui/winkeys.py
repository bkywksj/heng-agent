"""Windows 上让 Shift+Enter 换行——默认就能用，不用用户去配终端。

终端里 Enter 与 Shift+Enter 通常发的是同一个字符，程序分不出来。但 Windows 控制台给程序的「原始按键事件」
里带着修饰键状态——Windows Terminal 走 win32-input-mode 把完整的按键信息（含 Shift、Ctrl、Alt）传给控制台，
就是为了这个。

坑在 prompt_toolkit 默认开 VT 输入模式：那样控制台先把按键翻成字符再给程序，Shift+Enter 与 Enter 都变成 \\r，
Shift 信息半路就丢了（实测在 Windows Terminal 里按 Shift+Enter 直接发送了）。所以这里做两件事：
1. 让 prompt_toolkit 不用 VT 输入模式，改用读原始按键事件的 ConsoleInputReader（粘贴识别、Shift+Tab、Alt 组合键、
   中文与 emoji 它都支持）；
2. 在它把按键事件翻译成按键的地方补一下：Enter 按下且 Shift 也按着时，交出 Ctrl+J（输入框里绑定为换行）。
VT 读取器也顺手补上（万一别处开了它，控制台又恰好留着修饰键状态时也能用）。

prompt_toolkit 会把输入读取器建一次就缓存起来，所以要在程序一启动就调（cli.main 里），
赶在任何一个提示、选择器之前。只在 Windows 上生效；其他系统什么都不做（那里 Ctrl+J / Alt+Enter 照样能换行）。
重复调用没有副作用。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

VK_RETURN = 0x0D
SHIFT_PRESSED = 0x0010
_patched = False


def is_shift_enter(ev: Any) -> bool:
    """这条原始按键事件是不是「Shift 按着时按下 Enter」。"""
    return bool(ev.KeyDown) and ev.VirtualKeyCode == VK_RETURN and bool(ev.ControlKeyState & SHIFT_PRESSED)


def enable_shift_enter() -> bool:
    """给 prompt_toolkit 的两种 Windows 输入读取器打补丁；返回是否打上了。"""
    global _patched
    if _patched or sys.platform != "win32":
        return _patched
    try:
        from prompt_toolkit.input import win32
        from prompt_toolkit.key_binding.key_processor import KeyPress
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.win32_types import KEY_EVENT_RECORD  # 不在 prompt_toolkit.input 下：写错过，补丁静默失效
    except ImportError:
        return False

    # VT 读取器（Windows 10 起默认）：逐条交出字符，Shift+Enter 改交 "\n"（解析成 Ctrl+J）
    original_get_keys = win32.Vt100ConsoleInputReader._get_keys

    def _get_keys(self: Any, read: Any, input_records: Any) -> Iterator[str]:
        for i in range(read.value):
            ir = input_records[i]
            if ir.EventType in win32.EventTypes:
                ev = getattr(ir.Event, win32.EventTypes[ir.EventType])
                if isinstance(ev, KEY_EVENT_RECORD) and is_shift_enter(ev):
                    yield "\n"
                    continue
            yield from original_get_keys(self, _One(), [ir])

    # 老式读取器：把这条事件翻译出的按键改成 Ctrl+J
    original_event = win32.ConsoleInputReader._event_to_key_presses

    def _event_to_key_presses(self: Any, ev: Any) -> list[Any]:
        if is_shift_enter(ev):
            return [KeyPress(Keys.ControlJ, "\n")]
        return original_event(self, ev)

    win32.Vt100ConsoleInputReader._get_keys = _get_keys  # type: ignore[method-assign]
    win32.ConsoleInputReader._event_to_key_presses = _event_to_key_presses  # type: ignore[method-assign]
    # 不用 VT 输入模式：Win32Input 建立时问这个函数，返回 False 就改用读原始按键事件的 ConsoleInputReader，
    # 也不会把控制台切到 VT 输入模式——Shift 信息一路保留到程序这里
    win32._is_win_vt100_input_enabled = lambda: False  # type: ignore[attr-defined]
    _patched = True
    return True


class _One:
    """把单条事件交回原始的 _get_keys：它按 read.value 取前几条。"""

    value = 1
