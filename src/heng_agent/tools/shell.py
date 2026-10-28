"""命令工具：在工作目录里跑一条 shell 命令。

Windows 上优先用 Git Bash（模型写的命令大多是 bash 语法），找不到才退回 cmd。
输出太长时保留头尾、截掉中间：报错信息通常在最后，开头往往是命令回显与进度。

超时与 Ctrl+C 都要杀掉**整棵进程树**：`bash -c "python -m pytest"` 里真正干活的是 bash 的子进程，
只杀 bash 的话测试还在后台跑，而且它还握着输出管道，读输出会一直等到它自己结束——超时形同虚设。
按 PID 杀（Windows 用 taskkill /T，其他系统用进程组），从不按进程名杀。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from heng_agent.core.types import ToolResult
from heng_agent.tools.background import OutputBuffer, drain
from heng_agent.tools.base import Tool, ToolContext

MAX_OUTPUT_CHARS = 30_000
WAIT_SLICE = 0.1  # 等进程时每段多长：越短中断越及时
IS_WINDOWS = sys.platform == "win32"


def _is_wsl_launcher(path: Path) -> bool:
    """System32 / SysWOW64 里的 bash.exe 是 WSL 的启动器、WindowsApps 里的是商店别名——都不是 Git Bash。"""
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")).resolve()
    resolved = path.resolve()
    return resolved.is_relative_to(system_root) or "WindowsApps" in resolved.parts


def windows_bash() -> str | None:
    """Windows 上找 Git Bash：PATH 里的 bash（跳过 WSL 启动器）→ 从 git.exe 的位置推 → 常见安装位置。

    以前直接用 PATH 里第一个 bash：从 PowerShell / cmd 启动时 Git 的 bin 目录多半不在 PATH 里，
    找到的是 System32\\bash.exe（WSL 启动器）——没装 Linux 发行版就每条命令都报
    「execvpe(/bin/bash) failed」，装了也是在 Linux 里跑、路径全不对（真实终端里实测撞到）。
    """
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / "bash.exe"
        if folder and candidate.is_file() and not _is_wsl_launcher(candidate):
            return str(candidate)
    git = shutil.which("git")
    if git:  # Git 安装时默认只把 <Git>\cmd 加进 PATH：git.exe 在 cmd\ 或 mingw64\bin\ 下，bash 在 <Git>\bin\
        for root in Path(git).resolve().parents[:3]:
            for rel in (Path("bin") / "bash.exe", Path("usr") / "bin" / "bash.exe"):
                if (root / rel).is_file():
                    return str(root / rel)
    for base in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(base)
        for sub in (Path("Git"), Path("Programs") / "Git"):
            candidate = Path(root) / sub / "bin" / "bash.exe" if root else None
            if candidate is not None and candidate.is_file():
                return str(candidate)
    return None


def shell_argv(command: str) -> list[str]:
    if IS_WINDOWS:
        bash = windows_bash()
        return [bash, "-c", command] if bash else ["cmd", "/c", command]
    bash = shutil.which("bash")
    return [bash, "-c", command] if bash else ["/bin/sh", "-c", command]


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 3], text[-(limit * 2 // 3) :]
    return f"{head}\n…（中间省略 {len(text) - len(head) - len(tail)} 个字符）…\n{tail}"


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """按 PID 杀掉进程及其全部子孙进程。"""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    proc.wait()


class Detached(Exception):
    """前台命令转到后台：带着还在跑的进程与已有输出，交给后台任务管理。"""

    def __init__(self, proc: subprocess.Popen[bytes], buffer: OutputBuffer) -> None:
        super().__init__("转到后台")
        self.proc, self.buffer = proc, buffer


def spawn(argv: list[str], cwd: Any, env: dict[str, str] | None) -> subprocess.Popen[bytes]:
    """启动进程：独立进程组（中断、超时时能整棵杀掉），标准输出与错误合并，不读标准输入。"""
    extra: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
    )
    return subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **extra
    )


def run_command(
    argv: list[str], cwd: Any, timeout: float, env: dict[str, str] | None, detach: threading.Event | None = None,
    cancelled: threading.Event | None = None,
) -> tuple[int | None, str]:  # fmt: skip
    """跑命令，返回 (退出码, 合并输出)。超时返回 (None, 已有输出)；被 Ctrl+C 打断时先杀进程树再把中断抛出去；
    detach 被置上（用户按 Ctrl+B）时抛 Detached，进程不杀、输出接着收，交给后台任务。

    输出由后台线程读进 OutputBuffer：主线程只等进程结束、超时或转后台。
    """
    proc = spawn(argv, cwd, env)
    buffer = OutputBuffer()
    reader = drain(proc, buffer)
    if detach is not None:
        detach.clear()  # 上一条命令时按的 Ctrl+B 不算数
    # 用 poll() + sleep 分小段等，不用 proc.wait(timeout)：Windows 上中断落在 wait 里时，Python 会再等子进程一下，
    # 那段时间用 0.1 秒切片算出来是负数，被当成超大值传给 WaitForSingleObject，等于一直等到命令自己结束——
    # 实测 ESC 后 30 秒才停。poll + sleep 实测 0.01 秒内收到中断，再去杀进程树
    deadline = time.monotonic() + timeout
    code: int | None = None
    try:
        while (code := proc.poll()) is None:
            if cancelled is not None and cancelled.is_set():
                raise KeyboardInterrupt  # 不在主线程时的中断（--acp）：下面统一杀进程树
            if detach is not None and detach.is_set():
                detach.clear()
                raise Detached(proc, buffer)
            if time.monotonic() >= deadline:
                kill_tree(proc)
                code = None
                break
            time.sleep(WAIT_SLICE)
    except KeyboardInterrupt:
        kill_tree(proc)
        raise
    reader.join(timeout=5)
    return code, buffer.text()


class Bash(Tool):
    name = "bash"
    kind = "exec"
    description = (
        "在工作目录里执行一条 shell 命令（Windows 上是 Git Bash），返回退出码与合并后的输出。"
        "适合跑测试、构建、git 只读查询。不要用它读写文件（用 read_file / write_file / edit_file）。"
        "开发服务器、watch 这类不会自己结束的命令，传 run_in_background=true 放到后台，"
        "之后用 bash_output 看输出、kill_task 停掉。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number", "description": "超时秒数，默认 120（后台任务没有超时）"},
            "run_in_background": {"type": "boolean", "description": "放到后台跑，立即返回任务 id"},
        },
        "required": ["command"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        suffix = "  （后台）" if args.get("run_in_background") else ""
        return str(args.get("command", "")) + suffix

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if args.get("run_in_background"):
            proc = spawn(shell_argv(command), ctx.workdir, env)
            buffer = OutputBuffer()
            drain(proc, buffer)
            task = ctx.tasks.adopt(command, proc, buffer)
            return ToolResult(f"已在后台启动 {task.id}（PID {proc.pid}）。用 bash_output 看输出，kill_task 停止。")
        timeout = float(args.get("timeout") or ctx.bash_timeout)
        try:
            code, output = run_command(shell_argv(command), ctx.workdir, timeout, env, ctx.detach, ctx.cancelled)
        except Detached as moved:
            task = ctx.tasks.adopt(command, moved.proc, moved.buffer)
            so_far = clip(moved.buffer.read_new()[0]).rstrip()
            note = f"用户按 Ctrl+B 把这条命令转到了后台：任务 {task.id}，还在跑。用 bash_output 看后续输出。"
            return ToolResult(f"{note}\n到目前的输出：\n{so_far}" if so_far else note)
        output = clip(output).rstrip()
        if code is None:
            return ToolResult(f"命令超时（{timeout:g} 秒），已终止整个进程树。已有输出：\n{output}", is_error=True)
        return ToolResult(f"退出码 {code}\n{output}" if output else f"退出码 {code}", is_error=code != 0)


class BashOutput(Tool):
    name = "bash_output"
    kind = "read"
    description = "查看后台任务的状态与新输出（上次查看之后的部分）。"
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "如 bg1"}},
        "required": ["task_id"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("task_id", ""))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = ctx.tasks.get(str(args["task_id"]))
        if task is None:
            known = "、".join(ctx.tasks.tasks) or "无"
            return ToolResult(f"没有后台任务 {args['task_id']}（现有：{known}）", is_error=True)
        new, lost = task.buffer.read_new()
        head = f"{task.id}：{task.status()}" + ("（更早的输出太多，已丢弃一部分）" if lost else "")
        body = clip(new).rstrip()
        return ToolResult(f"{head}\n{body}" if body else f"{head}\n（没有新输出）")


class KillTask(Tool):
    name = "kill_task"
    kind = "exec"
    description = "停止一个后台任务（连同它启动的全部子进程）。"
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "如 bg1"}},
        "required": ["task_id"],
    }

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("task_id", ""))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = ctx.tasks.kill(str(args["task_id"]))
        if task is None:
            return ToolResult(f"没有后台任务 {args['task_id']}", is_error=True)
        return ToolResult(f"已停止 {task.id}：{task.command}")
