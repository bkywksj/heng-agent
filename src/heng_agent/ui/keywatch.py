"""任务运行时监听 ESC：按下就中断当前任务（和 Claude Code 一样）。

为什么要单独一个线程：模型回复、工具执行期间输入框是关着的，没有谁在读键盘。这里在后台用 msvcrt
逐个读按键，读到 ESC 就回调（由界面去取消网络请求、打断主线程）。

注意两点：
- **问用户的时候必须暂停**（paused()）：否则这个线程会把用户在确认框里敲的字抢走；
- 方向键等在 VT 输入模式下是 `ESC [ A` 这样的序列：ESC 后面紧跟 `[` / `O` 的不算按了 ESC。
运行期间敲的其他字会被丢掉（Claude Code 会排队成下一条消息，这里先不做）。只在 Windows 上工作。
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

POLL_SECONDS = 0.03
SEQUENCE_WAIT = 0.05  # ESC 之后这么久内还有字符跟上来，就当成方向键之类的转义序列


class EscWatcher:
    def __init__(self, on_escape: Callable[[], None], on_detach: Callable[[], None] | None = None) -> None:
        """on_escape：按了 ESC（中断任务，监听随之结束）；on_detach：按了 Ctrl+B（前台命令转后台，监听继续）。"""
        self.on_escape = on_escape
        self.on_detach = on_detach
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def start(self) -> None:
        if not self.supported or self._thread is not None:
            return
        self._stop.clear()
        self._paused.clear()
        self._thread = threading.Thread(target=self._run, name="esc-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None

    @contextmanager
    def paused(self) -> Iterator[None]:
        """问用户（确认框、y/n）期间暂停监听，免得把用户的按键抢走。"""
        self._paused.set()
        try:
            yield
        finally:
            self._paused.clear()

    def _run(self) -> None:
        import msvcrt

        while not self._stop.is_set():
            if self._paused.is_set() or not msvcrt.kbhit():
                time.sleep(POLL_SECONDS)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # 功能键 / 方向键的前缀：再读一个字符丢掉
                msvcrt.getwch()
                continue
            if ch == "\x02" and self.on_detach is not None:  # Ctrl+B：任务还要接着跑，监听不停
                self.on_detach()
                continue
            if ch != "\x1b":
                continue
            time.sleep(SEQUENCE_WAIT)
            if msvcrt.kbhit():  # ESC 后面紧跟着字符：是转义序列（方向键等），整段丢掉
                while msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            self.on_escape()
            return  # 一次任务只需要中断一次
