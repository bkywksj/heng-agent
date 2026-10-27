"""区分开发版与正式版：两套命令、两套数据，互不干扰。

- 正式版：从打了标签的版本装进环境（代码在 site-packages），命令 `heng-agent`，数据在 ~/.heng-agent；
- 开发版：直接跑仓库里的源码（代码在 仓库/src/heng_agent，旁边有 pyproject.toml 与 .git），改完立即生效，
  数据在 ~/.heng-agent-dev——开发时改配置、试新会话格式，不会把日常用的正式版弄坏（以前两边共用 ~/.heng-agent，
  开发版写进一个新设置字段，旧的正式版就启动不了）。

判断不用任何配置，看代码在哪儿就知道；要强制可以设环境变量 HENG_AGENT_CHANNEL=dev / release。
数据目录还可以用 HENG_AGENT_HOME 或 --home 直接指定（最优先）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from heng_agent import __version__

_PACKAGE_DIR = Path(__file__).resolve().parent.parent  # .../heng_agent


@lru_cache(maxsize=1)
def source_root() -> Path | None:
    """从源码仓库直接运行时返回仓库根目录；装好的正式版返回 None。"""
    root = _PACKAGE_DIR.parent.parent
    if _PACKAGE_DIR.parent.name == "src" and (root / "pyproject.toml").is_file() and (root / ".git").exists():
        return root
    return None


def is_dev() -> bool:
    channel = os.environ.get("HENG_AGENT_CHANNEL", "").lower()
    if channel in ("dev", "release"):
        return channel == "dev"
    return source_root() is not None


@lru_cache(maxsize=1)
def _git_describe() -> str:
    root = source_root()
    if root is None:
        return ""
    try:
        done = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=root, capture_output=True,
                              text=True, timeout=3, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def version_label() -> str:
    """显示用的版本：正式版就是版本号；开发版带上 git 位置，如 0.2.0-dev（v0.2.0-3-g238954f-dirty）。"""
    if not is_dev():
        return __version__
    where = _git_describe()
    return f"{__version__}-dev（{where}）" if where else f"{__version__}-dev"


def default_home() -> Path:
    """用户级目录（配置、会话、历史、信任记录）：HENG_AGENT_HOME 最优先，否则开发版与正式版各用各的。"""
    custom = os.environ.get("HENG_AGENT_HOME")
    if custom:
        return Path(custom)
    return Path.home() / (".heng-agent-dev" if is_dev() else ".heng-agent")


def prepare_dev_home(home: Path) -> str:
    """开发版第一次运行：从正式版复制一份 config.json（服务商地址、密钥、MCP 服务），省得再配一遍；
    之后两边各改各的。会话、历史、信任记录不复制——开发时从干净的状态开始。返回给用户看的说明（没做事就空串）。"""
    prod = Path.home() / ".heng-agent" / "config.json"
    if not is_dev() or home != Path.home() / ".heng-agent-dev" or home.exists() or not prod.is_file():
        return ""
    home.mkdir(parents=True)
    shutil.copy2(prod, home / "config.json")
    return f"开发版第一次运行：从正式版复制了配置到 {home}（之后两边各改各的，互不影响）"
