"""P1 功能：任务清单、撤销、@文件、!命令、网页读取、费用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent.agent import Agent
from heng_agent.context import expand_mentions
from heng_agent.core.config import Settings
from heng_agent.model.providers import Profile
from heng_agent.permissions import Permissions
from heng_agent.tools.base import ToolContext
from heng_agent.tools.todo import Todo
from heng_agent.tools.web import WebFetch, html_to_text
from heng_agent.ui.repl import Repl


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    work = tmp_path / "proj"
    work.mkdir()
    return Settings(workdir=work, home=tmp_path / "home")


def _yes(tool: Any, args: dict[str, Any]) -> Any:
    return "once"


# ---------- 任务清单 ----------


def test_todo_tool_updates_context_and_validates(tmp_path: Path) -> None:
    ctx = ToolContext(workdir=tmp_path)
    todo = Todo()
    res = todo.run({"todos": [{"content": "读代码", "status": "done"}, {"content": "改 bug", "status": "in_progress"},
                              {"content": "跑测试", "status": "pending"}]}, ctx)  # fmt: skip
    assert res.content.splitlines() == ["任务清单（1/3 完成）", "☑ 读代码", "▶ 改 bug", "☐ 跑测试"]
    assert len(ctx.todos) == 3
    two = {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]}
    assert todo.run(two, ctx).is_error and len(ctx.todos) == 3  # 校验失败不改状态
    assert todo.run({"todos": [{"content": "a", "status": "maybe"}]}, ctx).is_error  # doing 等同义词会被认，maybe 不会


# ---------- 撤销 ----------


def test_undo_restores_files_and_rolls_back_history(settings: Settings) -> None:
    old = settings.workdir / "app.py"
    old.write_text("x = 1\n", encoding="utf-8")
    model = FakeModel(
        [
            ["第一问的回答"],
            [
                ("tool", "edit_file", {"path": "app.py", "old_string": "x = 1", "new_string": "x = 2"}),
                ("tool", "edit_file", {"path": "app.py", "old_string": "x = 2", "new_string": "x = 3"}),
                ("tool", "write_file", {"path": "new/added.py", "content": "y = 1"}),
            ],
            ["改好了"],
        ]
    )
    agent = Agent(settings, model)
    agent.run("第一问", lambda e: None, _yes)
    agent.run("改一下", lambda e: None, _yes)
    assert old.read_text(encoding="utf-8") == "x = 3\n" and (settings.workdir / "new/added.py").exists()
    restored = agent.undo()
    assert restored == ["app.py", "new/added.py"]
    assert old.read_text(encoding="utf-8") == "x = 1\n"  # 恢复到这一问之前（不是只退一次修改）
    assert not (settings.workdir / "new/added.py").exists()  # 这一问新建的文件被删掉
    assert len(agent.messages) == 2 and agent.messages[0]["content"][0]["text"] == "第一问"
    assert agent.undo() == [] and agent.messages == []  # 再撤一问：没改文件，只退历史
    assert agent.undo() is None


# ---------- @文件 ----------


def test_expand_mentions(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "big.txt").write_bytes(b"a" * 300_000)
    text, attached = expand_mentions(
        "看看 @src/app.py，和 @src/app.py 以及 @big.txt 还有 @nobody 跟 a@b.com @../x", tmp_path
    )
    assert attached == ["src/app.py", "big.txt"]  # 去重；不存在、越界、邮箱都不附
    assert '<file path="src/app.py">' in text and "print('hi')" in text and "只附上前" in text
    assert expand_mentions("没有提到文件", tmp_path) == ("没有提到文件", [])


# ---------- !命令 与界面 ----------


def _repl(settings: Settings, model: Any) -> tuple[Repl, Console]:
    console = Console(record=True, width=120, force_terminal=False)
    return Repl(Agent(settings, model), console=console), console


def test_bang_command_output_goes_into_next_question(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel([["看到了"]])
    repl, console = _repl(settings, model)
    lines = iter(["!echo 来自终端", "刚才的输出是什么？", "/exit"])
    monkeypatch.setattr(repl, "read", lambda: next(lines))
    repl.loop()
    sent = model.calls[0][-1]["content"][0]["text"]
    assert "我在终端执行了 `echo 来自终端`" in sent and "来自终端" in sent and sent.endswith("刚才的输出是什么？")
    assert "退出码 0" in console.export_text()


def test_undo_todo_and_cost_commands(settings: Settings) -> None:
    repl, console = _repl(settings, FakeModel([]))
    repl.handle_command("/undo")
    repl.handle_command("/todo")
    repl.handle_command("/cost")
    repl.agent.client.profile = Profile(protocol="anthropic", base_url="u", model="m", price_in=2, price_out=8)  # type: ignore[attr-defined]
    repl.agent.usage = {"input_tokens": 1_000_000, "output_tokens": 500_000}
    repl.handle_command("/cost")
    text = console.export_text()
    assert "没有可以撤销的" in text and "还没有任务清单" in text and "price_in" in text
    assert "约 6.0000 元" in text  # 100 万输入 × 2 + 50 万输出 × 8 = 6 元


# ---------- 网页 ----------


def test_html_to_text_and_scheme_check(tmp_path: Path) -> None:
    page = "<html><head><title>t</title><style>x{}</style></head><body><h1>标题</h1><p>第一段&amp;更多</p>" \
           "<script>alert(1)</script><ul><li>项</li></ul></body></html>"  # fmt: skip
    assert html_to_text(page) == "标题\n\n第一段&更多\n\n项"  # 块级元素之间留空行，脚本与样式去掉
    res = WebFetch().run({"url": "file:///etc/passwd"}, ToolContext(workdir=tmp_path))
    assert res.is_error and "http" in res.content


def test_web_fetch_needs_confirmation_except_auto() -> None:
    tool = WebFetch()
    for mode in ("ask", "accept-edits", "plan"):
        assert Permissions(mode).decide(tool) == "ask"  # type: ignore[arg-type]  # 可能把代码拼进网址带出去
    assert Permissions("auto").decide(tool) == "allow"


def test_todo_accepts_common_status_synonyms(tmp_path: Path) -> None:
    # Claude 爱写 completed：直接认，不报错让模型白白重试一次
    ctx = ToolContext(workdir=tmp_path)
    res = Todo().run({"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "In-Progress"},
                                {"content": "c", "status": "todo"}]}, ctx)  # fmt: skip
    assert not res.is_error and [t["status"] for t in ctx.todos] == ["done", "in_progress", "pending"]
