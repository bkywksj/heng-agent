"""下一句建议：答完一问后灰字显示在空的输入框里，Tab 采纳（ui/suggest.py、InputBox.suggestion）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from heng_agent.core.types import TextDelta, ToolCall, TurnEnd
from heng_agent.model.providers import Profile
from heng_agent.ui import suggest
from heng_agent.ui.inputbox import InputBox
from heng_agent.ui.prompt import MODES, STYLE, SlashCompleter

TALK = [{"role": "user", "content": [{"type": "text", "text": "跑一下测试"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "测试全过了"}]}]  # fmt: skip


class Says:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, []

    def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
        self.calls.append((messages, tools))
        yield TextDelta(self.text)
        yield TurnEnd("end_turn", {"input_tokens": 1, "output_tokens": 1})


CACHED = Profile(protocol="anthropic", base_url="https://x", model="m", api_key="k")
NO_CACHE = Profile(protocol="openai", base_url="https://x", model="m", api_key="k")
TOOLS = [{"name": "read_file", "description": "读文件", "input_schema": {"type": "object"}}]


def test_cached_plan_keeps_the_main_prefix() -> None:
    """吃缓存时请求前缀和主对话一模一样：同一个系统提示词、同样的工具声明、整段对话，只在末尾加一条要求。
    以前不带工具：缓存按 工具 → 系统提示词 → 消息 比前缀，开头就对不上，整段对话每一问都按全价再发一遍。"""
    request = suggest.plan(CACHED, "主对话的系统提示词", TALK, TOOLS)
    assert request is not None and request.cached
    assert request.system == "主对话的系统提示词" and request.tools == TOOLS
    assert request.messages[:2] == TALK and request.messages[-1]["content"][0]["text"] == suggest.INSTRUCTION
    client = Says("「提交一下」")
    assert suggest.ask(client, request) == "提交一下"  # 去掉引号
    assert client.calls[0] == (request.messages, TOOLS)


def test_uncached_plan_sends_only_recent_turns() -> None:
    """没有缓存可吃（OpenAI 协议、关了缓存）：只带最近几问压成的一段文字、短系统提示词、不带工具。"""
    long_talk: list[Any] = []
    for i in range(10):
        long_talk += [{"role": "user", "content": [{"type": "text", "text": f"第{i}问"}]},
                      {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "read_file",
                                                         "input": {"path": "a.py"}}]},
                      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                    "content": "x" * 5000}]},
                      {"role": "assistant", "content": [{"type": "text", "text": f"第{i}答"}]}]  # fmt: skip
    request = suggest.plan(NO_CACHE, "很长的系统提示词" * 1000, long_talk, TOOLS)
    assert request is not None and not request.cached
    assert request.system == suggest.BRIEF_SYSTEM and request.tools == [] and len(request.messages) == 1
    text = request.messages[0]["content"][0]["text"]
    assert "第6问" not in text and "用户：第7问" in text and "助手：第9答" in text  # 最近 3 问
    assert "[助手调用工具 read_file" in text and "x" * 301 not in text  # 工具结果截短
    assert len(text) < suggest.BRIEF_CHARS + len(suggest.INSTRUCTION) + 10
    no_cache = CACHED.model_copy(update={"prompt_cache": False})
    assert not suggest.plan(no_cache, "", TALK, TOOLS).cached  # type: ignore[union-attr]


def test_tool_call_answer_is_dropped() -> None:
    """带着工具声明，模型偶尔还是去调工具：整个丢掉，工具不执行。"""

    class Calls(Says):
        def stream(self, system: str, messages: list[Any], tools: list[Any]) -> Any:
            yield TextDelta("我先看看")
            yield ToolCall("t1", "read_file", {"path": "a.py"})
            yield TurnEnd("tool_use", {})

    assert suggest.ask(Calls(""), suggest.plan(CACHED, "", TALK, TOOLS)) == ""  # type: ignore[arg-type]


def test_bad_answers_are_dropped() -> None:
    assert suggest.clean("") == ""
    assert suggest.clean("（没有明显的下一步）") == ""
    assert suggest.clean("第一行\n第二行") == ""
    assert suggest.clean("很长" * 40) == ""
    assert suggest.plan(CACHED, "", TALK[:1], []) is None  # 上一条不是助手的回复（出错、被打断）：不问


def _box(pipe: Any, tmp_path: Path) -> InputBox:
    return InputBox(history=InMemoryHistory(), commands=SlashCompleter(lambda: {}), workdir=tmp_path,
                    toolbar=lambda: "", get_mode=lambda: "ask", set_mode=lambda m: None, modes=MODES, style=STYLE,
                    input=pipe, output=DummyOutput())  # fmt: skip


def test_tab_accepts_the_suggestion(tmp_path: Path) -> None:
    with create_pipe_input() as pipe:
        box = _box(pipe, tmp_path)
        box.suggestion = "提交一下"
        pipe.send_text("\t，写清楚原因\r")  # Tab 采纳后还能接着改
        assert box.prompt() == "提交一下，写清楚原因"
    assert box.suggestion == ""


def test_right_arrow_also_accepts(tmp_path: Path) -> None:
    """→ 也能采纳（同 Claude Code）。"""
    with create_pipe_input() as pipe:
        box = _box(pipe, tmp_path)
        box.suggestion = "提交一下"
        pipe.send_text("\x1b[C\r")
        assert box.prompt() == "提交一下"


def test_typing_dismisses_the_suggestion(tmp_path: Path) -> None:
    with create_pipe_input() as pipe:
        box = _box(pipe, tmp_path)
        box.suggestion = "提交一下"
        pipe.send_text("别的\t\r")  # 先打字：建议作废，Tab 照常是补全
        assert box.prompt().startswith("别的")
    assert box.suggestion == ""


def test_hint_is_drawn_only_in_an_empty_box(tmp_path: Path) -> None:
    from prompt_toolkit.document import Document
    from prompt_toolkit.layout.processors import TransformationInput

    from heng_agent.ui.inputbox import _SuggestionHint

    with create_pipe_input() as pipe:
        box = _box(pipe, tmp_path)
    box.suggestion = "提交一下"
    hint = _SuggestionHint(box)

    def shown(text: str) -> str:
        ti = TransformationInput(None, Document(text), 0, lambda i: i, [("", text)], 80, 1)  # type: ignore[arg-type]
        return "".join(t for _, t, *_ in hint.apply_transformation(ti).fragments)

    assert shown("") == "提交一下   Tab / → 采纳" and shown("在打字") == "在打字"
    box.turn = object()  # type: ignore[assignment]  # 回答中不显示
    assert shown("") == ""


def test_repl_fetches_after_a_turn_and_respects_the_setting(tmp_path: Path, monkeypatch: Any) -> None:
    import time

    from fake_model import FakeModel
    from heng_agent.agent import Agent
    from heng_agent.core.config import Settings
    from heng_agent.model.providers import Profile
    from heng_agent.ui import repl as repl_module
    from heng_agent.ui.repl import Repl

    client = FakeModel([])
    client.profile = Profile(protocol="anthropic", base_url="https://x", model="m", api_key="k")  # type: ignore[attr-defined]
    made: list[Says] = []
    monkeypatch.setattr(repl_module, "make_client", lambda p, n: made.append(Says("提交一下")) or made[-1])
    with create_pipe_input() as pipe:
        repl = Repl(Agent(Settings(workdir=tmp_path, home=tmp_path / "h"), client))
        repl._box, repl._plain_input = _box(pipe, tmp_path), False
        repl.agent.messages = list(TALK)
        repl._suggest_next()
        deadline = time.monotonic() + 3
        while not repl._box.suggestion and time.monotonic() < deadline:
            time.sleep(0.02)
        assert repl._box.suggestion == "提交一下"
        assert made[0].calls[0][1] == repl.agent.tool_specs()  # 照主对话的前缀：工具声明一样
        repl.agent.settings = repl.agent.settings.model_copy(update={"prompt_suggestions": False})
        repl._suggest_next()  # 关掉了：清掉旧的，不再问
        assert repl._box.suggestion == "" and len(made) == 1
