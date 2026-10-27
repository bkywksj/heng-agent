"""信任记录：项目里的钩子、MCP 服务会在用户电脑上执行程序，用户确认信任后才启用。

记录在 ~/.heng-agent/trusted_hooks.json：键是「项目路径 + 种类」，值是被信任时那份配置内容的指纹。
配置一改指纹就对不上，需要重新确认——防止已信任的项目被一次 git pull 悄悄换掉要执行的命令。
"""

from __future__ import annotations

import json
from pathlib import Path


def _path(home: Path) -> Path:
    return home / "trusted_hooks.json"


def _load(home: Path) -> dict[str, str]:
    path = _path(home)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except json.JSONDecodeError:
        return {}  # 文件坏了就当什么都没信任过：宁可多问一次


def is_trusted(home: Path, key: str, fp: str) -> bool:
    return _load(home).get(key) == fp


def trust(home: Path, key: str, fp: str) -> None:
    data = _load(home)
    data[key] = fp
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
