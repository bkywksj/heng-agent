"""后台任务：run_in_background、bash_output 只取新输出、kill_task、Ctrl+B 前台转后台、退出时全部停掉。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.tools.background import MAX_BUFFER_BYTES, OutputBuffer
from heng_agent.tools.base import ToolContext
from heng_agent.tools.shell import Bash, BashOutput, KillTask
from heng_agent.ui.repl import Repl

PY = Path(sys.executable).as_posix()
#: 假的开发服务器：先说启动完成，然后每 0.2 秒打一行，一直不退出（写成脚本文件，免得在 bash -c 里层层转义引号）
SERVER_CODE = "import time\nprint('server ready')\nfor i in range(600):\n    print(f'tick {i}')\n    time.sleep(0.2)\n"


def _server(tmp_path: Path) -> str:
    script = tmp_path / "server.py"
    script.write_text(SERVER_CODE, encoding="utf-8")
    return f'"{PY}" -u "{script.as_posix()}"'


def _wait_for(predicate, seconds: float = 10.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_output_buffer_reads_only_new_and_caps() -> None:
    buf = OutputBuffer()
    buf.append(b"a\nb\n")
    assert buf.read_new() == ("a\nb\n", False)
    assert buf.read_new() == ("", False)  # 没有新输出
    buf.append(b"c\n")
    assert buf.read_new() == ("c\n", False)
    buf.append(b"x" * (MAX_BUFFER_BYTES + 10))  # 一下子超出上限：最早的被丢掉（含 10 字节没读过的）
    new, lost = buf.read_new()
    assert lost is True and len(new) == MAX_BUFFER_BYTES  # 告诉模型中间有一部分没看到
    buf.append(b"z")
    assert buf.read_new() == ("z", False)


def test_background_task_lifecycle(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    started = Bash().run({"command": _server(tmp_path), "run_in_background": True}, ctx)
    assert "已在后台启动 bg1" in started.content
    task = ctx.tasks.get("bg1")
    assert task is not None and _wait_for(lambda: "server ready" in task.buffer.text())
    first = BashOutput().run({"task_id": "bg1"}, ctx).content
    assert "运行中" in first and "server ready" in first
    assert _wait_for(lambda: "tick 3" in task.buffer.text())
    second = BashOutput().run({"task_id": "bg1"}, ctx).content
    assert "server ready" not in second and "tick" in second  # 第二次只拿新输出
    assert "已停止 bg1" in KillTask().run({"task_id": "bg1"}, ctx).content
    assert task.code is not None and "已结束" in task.status()  # 进程真的停了
    assert BashOutput().run({"task_id": "bg9"}, ctx).is_error


def test_ctrl_b_moves_foreground_command_to_background(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    threading.Timer(1.0, ctx.detach.set).start()  # 跑了 1 秒后用户按 Ctrl+B
    began = time.monotonic()
    result = Bash().run({"command": _server(tmp_path), "timeout": 60}, ctx)
    assert time.monotonic() - began < 5  # 没有等命令跑完（它要跑两分钟）
    assert "Ctrl+B" in result.content and "bg1" in result.content and "server ready" in result.content
    task = ctx.tasks.get("bg1")
    assert task is not None and task.code is None  # 进程没被杀，转到后台继续跑
    assert _wait_for(lambda: "tick" in BashOutput().run({"task_id": "bg1"}, ctx).content)  # 后续输出照收
    assert ctx.tasks.kill_all() == 1 and task.code is not None


def test_stale_ctrl_b_is_ignored(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    ctx.detach.set()  # 上一条命令时按的 Ctrl+B
    result = Bash().run({"command": f'"{PY}" -c "print(1)"'}, ctx)
    assert result.content.startswith("退出码 0") and not ctx.tasks.tasks  # 新命令照常在前台跑完


def test_tasks_command_and_close(tmp_path: Path) -> None:
    console = Console(record=True, width=160, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), FakeModel([])), console=console)
    repl.handle_command("/tasks")
    server = _server(tmp_path)
    Bash().run({"command": server, "run_in_background": True}, repl.agent.ctx)
    Bash().run({"command": server, "run_in_background": True}, repl.agent.ctx)
    assert len(repl.agent.ctx.tasks.running()) == 2
    repl.handle_command("/tasks")
    repl.handle_command("/tasks kill bg1")
    assert len(repl.agent.ctx.tasks.running()) == 1
    repl.close()  # 退出：剩下的也停掉
    assert repl.agent.ctx.tasks.running() == []
    text = console.export_text()
    assert "没有后台任务" in text and "bg2" in text and "已停止 bg1" in text and "已停止 1 个后台任务" in text


def test_cloudflare_522_is_retried() -> None:
    """中转站常架在 Cloudflare 后面：522（连不上源站）是临时的，等一下重试多半能成（实测遇到过）。"""
    import httpx

    from heng_agent.model.client import make_client
    from heng_agent.model.providers import Profile

    hits: list[int] = []
    start = '{"type": "message_start", "message": {"usage": {"input_tokens": 1}}}'
    ok = f'data: {start}\n\ndata: {{"type": "message_stop"}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(522, text="origin timeout") if len(hits) == 1 else httpx.Response(200, text=ok)

    client = make_client(Profile(protocol="anthropic", base_url="http://x", model="m"), 10,
                         transport=httpx.MockTransport(handler))  # fmt: skip
    client.sleep = lambda s: None
    list(client.stream("", [{"role": "user", "content": "hi"}], []))
    assert len(hits) == 2  # 第一次 522，重试一次就成了
