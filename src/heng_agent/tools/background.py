"""后台任务：开发服务器、watch 这类不会自己结束的命令放到后台跑，对话照常进行。

- bash 带 run_in_background=true 直接在后台启动；前台正在跑的命令按 Ctrl+B 也能转到后台（进程与已有输出一并交过来）；
- bash_output 看某个任务的新输出与状态，kill_task 停掉它（连同整棵进程树）；
- 退出 heng-agent 时停掉全部后台任务，不留在后台偷偷跑。
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field

MAX_BUFFER_BYTES = 1_000_000  # 每个任务最多留这么多输出，超出丢最早的：开发服务器跑一天也不会吃光内存


class OutputBuffer:
    """线程安全的输出缓存：读线程往里追加，记着读到哪了，bash_output 每次只拿新的部分。"""

    def __init__(self) -> None:
        self._data = bytearray()
        self._dropped = 0  # 因超出上限丢掉的字节数（读位置要跟着平移）
        self._read = 0  # 已经交给模型的位置（按全程字节计）
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            self._data += chunk
            extra = len(self._data) - MAX_BUFFER_BYTES
            if extra > 0:
                del self._data[:extra]
                self._dropped += extra

    def text(self) -> str:
        with self._lock:
            return bytes(self._data).decode("utf-8", errors="replace")

    def read_new(self) -> tuple[str, bool]:
        """返回 (上次读到之后的新输出, 中间是否有被丢掉的部分)。"""
        with self._lock:
            start = max(self._read - self._dropped, 0)
            lost = self._read < self._dropped
            new = bytes(self._data[start:]).decode("utf-8", errors="replace")
            self._read = self._dropped + len(self._data)
            return new, lost

    def last_line(self) -> str:
        lines = [ln for ln in self.text().splitlines() if ln.strip()]
        return lines[-1] if lines else ""


def drain(proc: subprocess.Popen[bytes], buffer: OutputBuffer) -> threading.Thread:
    """起一个读线程把进程输出不断搬进缓存；进程结束、管道关闭后线程自然结束。"""

    def loop() -> None:
        assert proc.stdout is not None
        # read1：有多少读多少（至少 1 字节就返回）。不能用 read(4096)：带缓冲的管道上它会一直等到凑满 4096 字节
        # 或进程结束——后台的开发服务器每次只打几个字，启动日志要等好几分钟才看得到（实测 3 秒一个字都读不到）
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):  # type: ignore[union-attr]
            buffer.append(chunk)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


@dataclass
class BackgroundTask:
    id: str
    command: str
    proc: subprocess.Popen[bytes]
    buffer: OutputBuffer
    started: float = field(default_factory=time.time)

    @property
    def code(self) -> int | None:
        return self.proc.poll()

    def status(self) -> str:
        code = self.code
        if code is None:
            return f"运行中（{_elapsed(self.started)}）"
        return f"已结束，退出码 {code}"


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, BackgroundTask] = {}
        self._next = 0
        self._lock = threading.Lock()

    def _new_id(self) -> str:
        with self._lock:
            self._next += 1
            return f"bg{self._next}"

    def adopt(self, command: str, proc: subprocess.Popen[bytes], buffer: OutputBuffer) -> BackgroundTask:
        """接管一个已经在跑的进程（bash 后台启动，或前台命令按 Ctrl+B 转过来的）。"""
        task = BackgroundTask(self._new_id(), command, proc, buffer)
        self.tasks[task.id] = task
        return task

    def get(self, task_id: str) -> BackgroundTask | None:
        return self.tasks.get(task_id.strip())

    def running(self) -> list[BackgroundTask]:
        return [t for t in self.tasks.values() if t.code is None]

    def kill(self, task_id: str) -> BackgroundTask | None:
        from heng_agent.tools.shell import kill_tree  # 避免与 shell 模块互相导入

        task = self.get(task_id)
        if task is not None:
            kill_tree(task.proc)
        return task

    def kill_all(self) -> int:
        """退出时调用：停掉还在跑的全部后台任务，返回停了几个。"""
        alive = self.running()
        for task in alive:
            self.kill(task.id)
        return len(alive)


def _elapsed(since: float) -> str:
    seconds = int(time.time() - since)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


def describe(task: BackgroundTask, *, limit: int = 0) -> str:
    """一行概况（/tasks 用）。"""
    last = task.buffer.last_line()
    if limit and len(last) > limit:
        last = last[:limit] + "…"
    return f"{task.id}  {task.status()}  {task.command}" + (f"\n      最后一行：{last}" if last else "")
