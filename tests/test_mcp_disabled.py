"""停用的 MCP 服务不加载："disabled": true（Kimi、Cursor、智码设置页的写法）或 "enabled": false。"""

from __future__ import annotations

import json
from pathlib import Path

from heng_agent.mcp import load_servers


def test_disabled_servers_are_skipped(tmp_path: Path) -> None:
    home = tmp_path / "h"
    work = tmp_path / "w"
    home.mkdir()
    work.mkdir()
    config = {
        "mcp_servers": {
            "on": {"command": "on-mcp"},
            "off": {"command": "off-mcp", "disabled": True},
            "off2": {"type": "http", "url": "http://127.0.0.1:1/mcp", "enabled": False},
            "explicit_on": {"command": "x", "disabled": False},
        }
    }
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    user, _ = load_servers(home, work)
    assert sorted(s.name for s in user) == ["explicit_on", "on"]
