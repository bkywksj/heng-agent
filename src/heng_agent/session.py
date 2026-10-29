"""会话保存与恢复：每个工作目录一个子目录，每个会话一个 JSON 文件。

多实例：会话 id = 秒级时间 + 随机后缀，同一目录同一秒启动两个实例也不会撞名互相覆盖；
每个实例只写自己的文件，所以不需要加锁。整份写：先写临时文件再改名，写到一半中断不会留下坏文件。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from heng_agent.core.errors import AgentError
from heng_agent.core.types import Message

#: 会话文件的格式版本：以后改格式时据此做兼容（旧文件没有这个字段，按 0 看）
SESSION_FORMAT = 1


def _project_dir(home: Path, workdir: Path) -> Path:
    resolved = str(workdir.resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
    return home / "sessions" / f"{workdir.resolve().name}-{digest}"


def new_session_id() -> str:
    return f"{_dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class SessionInfo:
    id: str
    updated: float
    messages: int
    first_question: str
    title: str = ""  # /rename 起的名字（没起就是空的）


class SessionStore:
    def __init__(self, home: Path, workdir: Path, session_id: str | None = None) -> None:
        self.dir = _project_dir(home, workdir)
        self.id = session_id or new_session_id()
        self.workdir = workdir
        self.title = ""  # 会话名：存在会话文件里，/resume 列表里显示、可以按它搜
        self.usage_by_model: dict[str, dict[str, int]] = {}  # 按模型分的用量（载入旧会话时从文件读出）
        # 会话 id 是调用方明确指定的（--session-id、--resume <id>）：/clear 时 id 不变、旧对话另存——
        # 智码这类调用方按 id 记着这个会话（标签页 ↔ id），换了 id 它下次打开就接到 /clear 之前的旧对话上
        self.pinned = False

    @property
    def path(self) -> Path:
        return self.dir / f"{self.id}.json"

    def archive(self) -> str | None:
        """把当前对话另存成一个新 id（文件改名），自己的 id 不变、留给接下来的新对话。没存过盘返回 None。"""
        if not self.path.exists():
            return None
        archived = new_session_id()
        self.path.replace(self.dir / f"{archived}.json")
        self.title = ""
        self.usage_by_model = {}
        return archived

    def save(
        self, messages: list[Message], usage: dict[str, int], usage_by_model: dict[str, dict[str, int]] | None = None
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"format": SESSION_FORMAT, "workdir": str(self.workdir), "messages": messages,
                                "usage": usage}  # fmt: skip
        if usage_by_model:
            data["usage_by_model"] = usage_by_model  # /stats 按模型统计、按各自的价格算钱
        if self.title:
            data["title"] = self.title
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def load(self) -> tuple[list[Message], dict[str, int]]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.title = str(data.get("title") or "")
        self.usage_by_model = dict(data.get("usage_by_model") or {})
        return data["messages"], data.get("usage", {})

    @classmethod
    def all(cls, home: Path, workdir: Path) -> list[SessionInfo]:
        """这个工作目录的全部会话，按最近更新排在前面。"""
        folder = _project_dir(home, workdir)
        infos = []
        for path in folder.glob("*.json") if folder.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # 另一个实例正写到一半等情况：跳过，不影响列出其余
            messages = data.get("messages", [])
            first = next((_text_of(m) for m in messages if m.get("role") == "user" and _text_of(m)), "")
            infos.append(
                SessionInfo(path.stem, path.stat().st_mtime, len(messages), first, str(data.get("title") or ""))
            )
        return sorted(infos, key=lambda s: s.updated, reverse=True)

    @classmethod
    def latest(cls, home: Path, workdir: Path) -> SessionStore | None:
        sessions = cls.all(home, workdir)
        return cls(home, workdir, sessions[0].id) if sessions else None

    @classmethod
    def find(cls, home: Path, workdir: Path, prefix: str) -> SessionStore:
        """按 id 或 id 前缀找会话；前缀对上多个时报错让用户写长一点。"""
        matches = [s.id for s in cls.all(home, workdir) if s.id.startswith(prefix)]
        if not matches:
            raise AgentError(f"这个目录没有 id 以「{prefix}」开头的会话（heng-agent --sessions 查看）")
        if len(matches) > 1 and prefix not in matches:
            raise AgentError(f"「{prefix}」对上了 {len(matches)} 个会话，写长一点：{', '.join(matches[:5])}")
        return cls(home, workdir, prefix if prefix in matches else matches[0])


def _text_of(message: Message) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")
