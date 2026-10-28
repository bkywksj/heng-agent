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


# ---------- /doctor copy：反馈问题时，检查结果连同最近的日志一次复制走 ----------

CHECKS = [("ok", "配置文件", "无误"), ("fail", "服务商连接", "连不上 https://api.example.com")]


def test_doctor_report_has_checks_and_logs_without_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from heng_agent.ui.doctor import report

    fake_home = tmp_path / "Users" / "zhangsan"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    data = fake_home / ".heng-agent"
    logs.setup(data)
    logs.log.warning("这一问出错：连不上 %s", fake_home / "proj")
    text = report(CHECKS, data)  # type: ignore[arg-type]
    assert "✓ 配置文件：无误" in text and "✗ 服务商连接：连不上 https://api.example.com" in text
    assert "这一问出错" in text and "heng-agent " in text and "Python" in text
    assert "zhangsan" not in text and "~" in text  # 用户目录换成 ~，反馈里不带出本机用户名


def _doctor_repl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Repl, Console]:
    from heng_agent.ui import repl as repl_module  # run_checks 在 repl.py 的 /doctor 分支里调

    monkeypatch.setattr(repl_module, "run_checks", lambda repl: CHECKS)  # 不真去连服务商
    work = tmp_path / "w"
    work.mkdir()
    console = Console(record=True, width=160, force_terminal=False)
    return Repl(Agent(Settings(workdir=work, home=tmp_path / "h"), FakeModel([])), console=console), console


def test_doctor_copy_puts_report_on_clipboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from heng_agent.ui import repl_commands as repl_module  # /doctor copy 在 repl_commands 里

    copied: list[str] = []
    monkeypatch.setattr(repl_module, "copy_text", lambda text: (copied.append(text), (True, ""))[1])
    repl, console = _doctor_repl(tmp_path, monkeypatch)
    repl.handle_command("/doctor copy")
    assert copied and "服务商连接" in copied[0] and "## 最近的日志" in copied[0]
    assert "已复制诊断信息" in console.export_text()


def test_doctor_copy_falls_back_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """剪贴板用不了（Linux 上没装 xclip 之类）：存成文件，告诉用户在哪。"""
    from heng_agent.ui import repl_commands as repl_module  # /doctor copy 在 repl_commands 里

    monkeypatch.setattr(repl_module, "copy_text", lambda text: (False, "找不到剪贴板工具"))
    repl, console = _doctor_repl(tmp_path, monkeypatch)
    repl.handle_command("/doctor copy")
    saved = list((tmp_path / "h" / "logs").glob("doctor-*.txt"))
    assert len(saved) == 1 and "服务商连接" in saved[0].read_text(encoding="utf-8")
    shown = console.export_text().replace("\n", "")
    assert "没能复制（找不到剪贴板工具）" in shown and "doctor-" in shown


def test_plain_doctor_does_not_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from heng_agent.ui import repl_commands as repl_module  # /doctor copy 在 repl_commands 里

    monkeypatch.setattr(repl_module, "copy_text", lambda text: pytest.fail("不带 copy 不该复制"))
    repl, _ = _doctor_repl(tmp_path, monkeypatch)
    repl.handle_command("/doctor")


def test_clipboard_text_has_no_bom(monkeypatch: pytest.MonkeyPatch) -> None:
    """以前往 clip 写 utf-16（自带 BOM），复制下来的内容开头多一个看不见的字符，贴进 issue 也带着（读剪贴板发现）。"""
    from heng_agent.ui import session_tools

    sent: list[bytes] = []
    monkeypatch.setattr(session_tools.sys, "platform", "win32")
    monkeypatch.setattr(session_tools.subprocess, "run", lambda argv, input, **kw: sent.append(input))
    assert session_tools.copy_text("诊断信息")[0]
    assert sent[0] == "诊断信息".encode("utf-16-le")  # 不带 BOM（\xff\xfe）
