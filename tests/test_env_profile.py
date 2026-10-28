"""调用方（如智码）用环境变量 HENG_AGENT_BASE_URL 等给服务商：只在这次运行里用，设了就是默认。"""

from __future__ import annotations

from pathlib import Path

import pytest

from heng_agent import cli
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.model.providers import ENV_PROFILE, config_path, env_profile, load_profiles
from heng_agent.ui.onboarding import needs_onboarding

RELAY = {"HENG_AGENT_BASE_URL": "https://cc.example.cn", "HENG_AGENT_API_KEY": "sk-relay",
         "HENG_AGENT_MODEL": "claude-sonnet-5"}  # fmt: skip


def test_env_profile_parsing() -> None:
    assert env_profile({}) is None  # 没设地址：不启用
    profile = env_profile({**RELAY, "HENG_AGENT_AUTH": "bearer"})
    assert profile is not None and profile.base_url == "https://cc.example.cn" and profile.model == "claude-sonnet-5"
    assert profile.api_key == "sk-relay" and profile.protocol == "anthropic" and profile.auth == "bearer"
    with pytest.raises(AgentError, match="HENG_AGENT_MODEL"):
        env_profile({"HENG_AGENT_BASE_URL": "https://x.cn"})  # 没给模型名：明确报错，不去猜
    with pytest.raises(AgentError, match="HENG_AGENT_PROTOCOL"):
        env_profile({**RELAY, "HENG_AGENT_PROTOCOL": "grpc"})


def test_env_profile_becomes_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work = tmp_path / "h", tmp_path / "w"
    work.mkdir()
    for key, value in RELAY.items():
        monkeypatch.setenv(key, value)
    profiles, default = load_profiles(home, work)
    assert default == ENV_PROFILE and profiles[ENV_PROFILE].model == "claude-sonnet-5"
    assert not config_path(home).exists()  # 不写进配置文件
    assert not needs_onboarding(home)  # 调用方给了服务商：不弹第一次用的引导
    name, _ = cli.resolve_profile(Settings(home=home, workdir=work, provider="deepseek"))
    assert name == "deepseek"  # 命令行的 --provider 仍然优先
