"""运行日志：写在数据目录、不记密钥；意料之外的错误记下调用栈、给用户日志位置，交互会话不因此崩掉。"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent import cli
from heng_agent.agent import Agent
from heng_agent.core import logs
from heng_agent.core.config import Settings
from heng_agent.ui.repl import Repl


def _files() -> list[logging.Handler]:
    """日志器上的文件 handler（pytest 等可能另挂别的 handler，不算）。"""
    return [h for h in logs.log.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def test_setup_writes_into_data_dir_once(tmp_path: Path) -> None:
    home = tmp_path / "h"
    assert logs.setup(home) is None and logs.setup(home) is None  # 重复调用只装一次
    assert len(_files()) == 1
    logs.log.info("第一条")
    assert logs.current() == home / "logs" / "heng-agent.log"
    assert logs.tail(home)[-1].endswith("第一条")
    other = tmp_path / "h2"
    logs.setup(other)  # 换了数据目录：关掉旧的、只写新的
    assert len(_files()) == 1 and logs.current() == other / "logs" / "heng-agent.log"


def test_setup_failure_is_reported_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "h"
    blocker.write_text("这里是个文件，建不了 logs 目录", encoding="utf-8")
    note = logs.setup(blocker)
    assert note is not None and "日志文件写不了" in note and logs.current() is None


def test_startup_is_logged_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-test-DO-NOT-LOG-1234567890"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    assert cli.main(["--list-providers", "--workdir", str(tmp_path)]) == 0
    home = Path(Settings().home)
    text = "\n".join(logs.tail(home))
    assert "启动" in text and "Python" in text
    assert secret not in text  # 日志会被原样贴进反馈：不能有密钥


def test_unexpected_error_points_to_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """以前意料之外的异常直接甩一屏堆栈；现在给一句话和日志位置，堆栈进日志。"""
    logs.setup(tmp_path / "h")

    def boom(argv: list[str] | None = None) -> int:
        raise RuntimeError("出了个 bug")

    monkeypatch.setattr(cli, "_main", boom)
    monkeypatch.setattr(cli, "is_dev", lambda: False)  # 正式版：终端上不打调用栈
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "出错了：RuntimeError: 出了个 bug" in err and "heng-agent.log" in err and "Traceback" not in err
    text = "\n".join(logs.tail(tmp_path / "h"))
    assert "Traceback" in text and "出了个 bug" in text


def test_repl_survives_unexpected_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """交互模式里一问出了意料之外的错误：以前整个程序崩掉、对话也没了；现在这一问作废，会话照常。"""
    logs.setup(tmp_path / "h")
    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=160, force_terminal=False)
    repl = Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([["好的"]])), console=console)
    original = repl.agent.run
    calls = iter([RuntimeError("坏了"), None])

    def run(*args: object, **kwargs: object) -> None:
        exc = next(calls)
        if exc is not None:
            raise exc
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repl.agent, "run", run)
    lines = iter(["第一问", "第二问", "/exit"])
    monkeypatch.setattr(repl, "read", lambda: next(lines))
    assert repl.loop() == 0  # 没崩，走到了 /exit
    shown = console.export_text()
    flat = shown.replace("\n", "")  # 提示带着日志路径，比较长，会被折行
    assert "内部错误：RuntimeError: 坏了" in flat and "heng-agent.log" in flat
    assert "好的" in shown  # 第二问照常回答
    assert "Traceback" in "\n".join(logs.tail(tmp_path / "h"))
