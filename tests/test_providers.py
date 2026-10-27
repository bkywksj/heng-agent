"""服务商配置档：预设继承、用户配置文件、密钥来源与请求头方式、列表输出不泄露密钥。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heng_agent import cli
from heng_agent.core.errors import AgentError
from heng_agent.model.providers import PRESETS, Profile, load_profiles


def _write_config(home: Path, data: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_presets_cover_both_protocols() -> None:
    assert {"heng", "anthropic", "deepseek", "kimi", "glm", "qwen", "openai", "openrouter", "ollama"} <= set(PRESETS)
    assert {p.protocol for p in PRESETS.values()} == {"anthropic", "openai"}


def test_config_inherits_preset_and_overrides_fields(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "default": "ds",
            "profiles": {
                "ds": {"preset": "deepseek", "model": "deepseek-flash"},
                "永夜": {"protocol": "anthropic", "base_url": "https://relay.example", "model": "claude-opus-4-8",
                         "api_key": "sk-relay"},
            },
        },
    )  # fmt: skip
    profiles, default = load_profiles(tmp_path)
    assert default == "ds"
    ds = profiles["ds"]
    assert ds.base_url == "https://api.deepseek.com/anthropic" and ds.model == "deepseek-flash"  # 继承地址，覆盖模型
    assert profiles["永夜"].resolve_key() == "sk-relay" and "heng" in profiles  # 内置预设仍可用


def test_bad_config_gives_clear_errors(tmp_path: Path) -> None:
    _write_config(tmp_path, {"profiles": {"x": {"preset": "nope"}}})
    with pytest.raises(AgentError, match="preset"):
        load_profiles(tmp_path)
    _write_config(tmp_path, {"profiles": {"x": {"protocol": "grpc", "base_url": "u", "model": "m"}}})
    with pytest.raises(AgentError, match="配置档 x 有误"):
        load_profiles(tmp_path)
    _write_config(tmp_path, {"default": "missing"})
    with pytest.raises(AgentError, match="默认配置档"):
        load_profiles(tmp_path)


def test_key_from_env_and_auth_header_styles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "sk-env")
    auto = Profile(protocol="anthropic", base_url="u", model="m", api_key_env="MY_KEY")
    assert auto.auth_headers() == {"x-api-key": "sk-env", "authorization": "Bearer sk-env"}
    assert auto.model_copy(update={"auth": "bearer"}).auth_headers() == {"authorization": "Bearer sk-env"}
    monkeypatch.delenv("MY_KEY")
    assert auto.auth_headers() == {}  # 没有密钥就不带请求头（如本机 Ollama）


def test_list_providers_never_prints_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HENG_AGENT_HOME", raising=False)  # 用「家目录下的默认数据目录」
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "release")  # 按正式版算：~/.heng-agent
    _write_config(tmp_path / ".heng-agent", {"profiles": {"relay": {"preset": "anthropic", "api_key": "sk-SECRET"}}})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ALSO-SECRET")
    assert cli.main(["--list-providers"]) == 0
    out = capsys.readouterr().out
    assert "relay" in out and "已写在配置文件" in out and "DEEPSEEK_API_KEY 已设置" in out
    assert "SECRET" not in out


def test_unknown_provider_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HENG_AGENT_HOME", raising=False)  # 用「家目录下的默认数据目录」
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "release")  # 按正式版算：~/.heng-agent
    assert cli.main(["--provider", "nope", "-p", "hi"]) == 1
    assert "没有叫「nope」" in capsys.readouterr().err
