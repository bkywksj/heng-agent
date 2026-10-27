"""多实例与配置隔离：同一目录并发跑多个实例、按 id 续聊、HENG_AGENT_HOME 独立配置、项目级配置与密钥禁令。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from fake_model import FakeModel
from heng_agent import cli
from heng_agent.agent import Agent
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.model.providers import load_profiles
from heng_agent.session import SessionStore


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_same_second_sessions_do_not_collide(tmp_path: Path) -> None:
    # 旧实现用秒级时间当 id：同一秒启动的两个实例会写同一个文件、互相覆盖
    ids = {SessionStore(tmp_path, tmp_path).id for _ in range(50)}
    assert len(ids) == 50


def test_concurrent_instances_in_same_dir_keep_their_own_sessions(tmp_path: Path) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    settings = Settings(workdir=work, home=tmp_path / "home")
    errors: list[BaseException] = []

    def instance(i: int) -> None:
        try:
            agent = Agent(settings, FakeModel([[f"实例{i}的回答"]]), session=SessionStore(settings.home, work))
            agent.run(f"实例{i}的问题", lambda e: None, lambda t, a: "deny")
        except BaseException as exc:  # 线程里的异常要带回主线程才会让测试失败
            errors.append(exc)

    threads = [threading.Thread(target=instance, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    sessions = SessionStore.all(settings.home, work)
    assert sorted(s.first_question for s in sessions) == sorted(f"实例{i}的问题" for i in range(8))
    assert all(s.messages == 2 for s in sessions)  # 每个会话完整：一问一答，没有被别的实例覆盖


def test_resume_by_id_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home, work = tmp_path / "home", tmp_path / "proj"
    work.mkdir()
    first, second = (
        SessionStore(home, work, "20261026-100000-aaaaaa"),
        SessionStore(home, work, "20261026-110000-bbbbbb"),
    )
    first.save([{"role": "user", "content": [{"type": "text", "text": "第一个会话"}]}], {})
    second.save([{"role": "user", "content": [{"type": "text", "text": "第二个会话"}]}], {})
    assert SessionStore.find(home, work, "20261026-10").id == first.id
    with pytest.raises(AgentError, match="对上了 2 个"):
        SessionStore.find(home, work, "20261026")
    with pytest.raises(AgentError, match="没有 id"):
        SessionStore.find(home, work, "zzz")

    monkeypatch.setenv("HENG_AGENT_HOME", str(home))
    assert cli.main(["--sessions", "--workdir", str(work)]) == 0
    listing = capsys.readouterr().out
    assert "第一个会话" in listing and "第二个会话" in listing
    seen = FakeModel([["接上了"]])
    monkeypatch.setattr(cli, "make_client", lambda profile, max_tokens: seen)
    assert cli.main(["-p", "继续", "--workdir", str(work), "--resume", "20261026-10"]) == 0
    assert seen.calls[0][0]["content"][0]["text"] == "第一个会话"  # 接的是指定的那个，不是最近的


def test_home_env_gives_fully_separate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a / "config.json", {"default": "deepseek"})
    _write(b / "config.json", {"default": "kimi"})
    monkeypatch.setenv("HENG_AGENT_HOME", str(a))
    assert Settings().home == a and load_profiles(Settings().home)[1] == "deepseek"
    monkeypatch.setenv("HENG_AGENT_HOME", str(b))
    assert load_profiles(Settings().home)[1] == "kimi"
    assert Settings(home=a).home == a  # --home 优先于环境变量


def test_project_config_overrides_and_inherits_user_profile(tmp_path: Path) -> None:
    home, work = tmp_path / "home", tmp_path / "proj"
    _write(
        home / "config.json",
        {
            "default": "deepseek",
            "profiles": {
                "永夜": {
                    "protocol": "anthropic",
                    "base_url": "https://relay.example",
                    "model": "m1",
                    "api_key": "sk-user",
                }
            },
        },
    )
    _write(work / ".heng-agent/config.json", {"default": "relay-fast", "profiles": {
        "relay-fast": {"preset": "永夜", "model": "m2"}}})  # fmt: skip
    profiles, default = load_profiles(home, work)
    fast = profiles["relay-fast"]
    assert default == "relay-fast" and fast.model == "m2"
    assert fast.base_url == "https://relay.example" and fast.resolve_key() == "sk-user"  # 密钥留在用户目录
    assert load_profiles(home, tmp_path)[1] == "deepseek"  # 别的项目不受影响


def test_project_config_may_not_hold_keys(tmp_path: Path) -> None:
    work = tmp_path / "proj"
    _write(work / ".heng-agent/config.json", {"profiles": {"x": {"preset": "deepseek", "api_key": "sk-leak"}}})
    with pytest.raises(AgentError, match="api_key"):
        load_profiles(tmp_path / "home", work)
