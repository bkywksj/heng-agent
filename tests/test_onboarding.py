"""第一次运行的引导：检测现成的配置、选服务商、测连接、给上手提示；脚本模式没配置时提示一句。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from fake_model import FakeModel
from heng_agent import cli
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.errors import ModelError
from heng_agent.model.providers import config_path
from heng_agent.ui import config_cmd, onboarding
from heng_agent.ui.repl import Repl


def test_needs_onboarding(tmp_path: Path) -> None:
    assert onboarding.needs_onboarding(tmp_path)  # 没有用户配置：第一次用
    config_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    config_path(tmp_path).write_text("{}", encoding="utf-8")
    assert not onboarding.needs_onboarding(tmp_path)


def test_detect_env_keys_and_claude_code(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://cc.example.cn/",
                                            "ANTHROPIC_AUTH_TOKEN": "sk-relay-1234567890abcd",
                                            "ANTHROPIC_MODEL": "claude-sonnet-5"}}), encoding="utf-8")  # fmt: skip
    relay = {"ANTHROPIC_BASE_URL": "https://cc.example.cn", "ANTHROPIC_AUTH_TOKEN": "sk-relay-1234567890abcd"}
    environ = {"DEEPSEEK_API_KEY": "sk-ds", "HENG_AGENT_API_KEY": "x", **relay}
    found = onboarding.detect(environ, settings)
    claude = [f for f in found if f.kind == "claude"]
    assert len(claude) == 1  # 配置文件与环境变量里是同一个服务：只算一次
    assert claude[0].base_url == "https://cc.example.cn" and claude[0].model == "claude-sonnet-5"
    assert claude[0].auth == "bearer"  # AUTH_TOKEN 是 Bearer 认证
    assert [f.preset for f in found if f.kind == "env"] == ["deepseek"]  # 本机 heng 的不算
    api_key_style = onboarding.detect({"ANTHROPIC_BASE_URL": "https://x.cn", "ANTHROPIC_API_KEY": "k"}, tmp_path / "无")
    assert api_key_style[0].auth == "x-api-key"
    assert onboarding.detect({}, tmp_path / "无") == []


def test_mask_and_explain() -> None:
    assert onboarding.mask("sk-relay-1234567890abcd") == "sk-rel…abcd"  # 不显示完整密钥
    assert "密钥不对" in onboarding.explain("模型服务返回 401：invalid api key")
    assert "余额不足" in onboarding.explain("模型服务返回 402：Insufficient Balance")
    assert "连不上" in onboarding.explain("连不上模型服务：connection refused")


def _repl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: list[str], model: Any) -> tuple[Repl, Console]:
    console = Console(record=True, width=160, force_terminal=False)
    home = tmp_path / "home"
    repl = Repl(Agent(Settings(workdir=tmp_path, home=home), FakeModel([])), console=console, onboarding=True)
    picks = iter(answers)
    monkeypatch.setattr(repl, "_choose", lambda title, options, default=0: next(picks))
    monkeypatch.setattr(config_cmd, "make_client", lambda profile, max_tokens: model)  # 配好后换上的「客户端」
    return repl, console


def test_later_saves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "detect", lambda environ, path: [])
    repl, console = _repl(tmp_path, monkeypatch, ["later"], FakeModel([]))
    onboarding.run(repl)
    assert not config_path(repl.agent.settings.home).exists()
    assert "/config provider" in console.export_text()


def test_use_detected_key_tests_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-secret")
    monkeypatch.setattr(onboarding, "claude_home", lambda: tmp_path / "没有")
    model = FakeModel([["好的"]])
    repl, console = _repl(tmp_path, monkeypatch, ["0"], model)
    onboarding.run(repl)
    data = json.loads(config_path(repl.agent.settings.home).read_text(encoding="utf-8"))
    assert data["default"] == "deepseek" and data["profiles"]["deepseek"] == {"preset": "deepseek"}
    assert "sk-ds-secret" not in json.dumps(data)  # 环境变量里的密钥不抄进配置文件
    shown = console.export_text()
    assert "✓ 连接成功" in shown and "好的" in shown and "/init" in shown  # 测了连接、给了上手提示
    assert onboarding.TEST_PROMPT in str(model.calls[0])


class BrokenClient:
    def stream(self, *args: Any) -> Iterator[Any]:
        raise ModelError("模型服务返回 401：invalid api key")
        yield


def test_failed_connection_explains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-bad")
    monkeypatch.setattr(onboarding, "claude_home", lambda: tmp_path / "没有")
    repl, console = _repl(tmp_path, monkeypatch, ["0", "keep"], BrokenClient())
    onboarding.run(repl)
    shown = console.export_text()
    assert "✗ 连接失败" in shown and "密钥不对" in shown and "/doctor" in shown


def test_print_mode_hints_when_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: FakeModel([["好"]]))
    assert cli.main(["--workdir", str(tmp_path), "-p", "你好"]) == 0
    assert "还没配置模型服务" in capsys.readouterr().err
