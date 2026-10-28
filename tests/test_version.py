"""开发版与正式版分开：自动判断、版本号、数据目录、开发版第一次运行复制配置。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heng_agent import __version__
from heng_agent.core import version


def test_running_from_source_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HENG_AGENT_CHANNEL", raising=False)
    assert version.source_root() is not None and version.is_dev()  # 测试就是从仓库源码跑的
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "release")
    assert not version.is_dev() and version.version_label() == __version__  # 可以强制按正式版
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "dev")
    assert version.version_label().startswith(f"{__version__}-dev")  # 开发版带上 git 位置


def test_default_home_per_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HENG_AGENT_HOME", raising=False)
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "release")
    assert version.default_home() == tmp_path / ".heng-agent"
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "dev")
    assert version.default_home() == tmp_path / ".heng-agent-dev"  # 开发版的数据和正式版分开
    monkeypatch.setenv("HENG_AGENT_HOME", str(tmp_path / "别处"))
    assert version.default_home() == tmp_path / "别处"  # HENG_AGENT_HOME 最优先


def test_dev_home_copies_config_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "dev")
    prod = tmp_path / ".heng-agent"
    (prod / "sessions").mkdir(parents=True)
    (prod / "config.json").write_text(json.dumps({"default": "yongye"}), encoding="utf-8")
    dev = tmp_path / ".heng-agent-dev"
    assert "复制了配置" in version.prepare_dev_home(dev)
    assert json.loads((dev / "config.json").read_text(encoding="utf-8")) == {"default": "yongye"}
    assert not (dev / "sessions").exists()  # 会话、历史不复制：开发从干净的状态开始
    (dev / "config.json").write_text("{}", encoding="utf-8")
    assert version.prepare_dev_home(dev) == ""  # 只在第一次：之后两边各改各的
    assert (dev / "config.json").read_text(encoding="utf-8") == "{}"
    monkeypatch.setenv("HENG_AGENT_CHANNEL", "release")
    assert version.prepare_dev_home(tmp_path / ".heng-agent-dev2") == ""  # 正式版不做这件事


def test_version_matches_pyproject() -> None:
    import tomllib

    pyproject = version.source_root() / "pyproject.toml"  # type: ignore[operator]
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"] == __version__  # 两处要一起改


def test_uv_lock_matches_version() -> None:
    """发版时改了版本号要跟着 uv lock：以前没人记得，v0.3.2 标签里 uv.lock 记的还是 0.2.0。"""
    import tomllib

    lock = tomllib.loads((version.source_root() / "uv.lock").read_text(encoding="utf-8"))  # type: ignore[operator]
    ours = next(p for p in lock["package"] if p["name"] == "heng-agent")
    assert ours["version"] == __version__, "改了版本号后运行 uv lock，把 uv.lock 一起提交"


def test_changelog_has_current_version_on_top() -> None:
    """CHANGELOG 最新一节要是当前版本：漏写的话 GitHub Release 的说明会悄悄变成「见 CHANGELOG.md」。"""
    import re

    text = (version.source_root() / "CHANGELOG.md").read_text(encoding="utf-8")  # type: ignore[operator]
    newest = re.search(r"^## (\d+\.\d+\.\d+)", text, re.MULTILINE)
    assert newest is not None and newest.group(1) == __version__, "改了版本号后在 CHANGELOG.md 最上面写这个版本的一节"
