"""运行日志：数据目录下的 logs/heng-agent.log（正式版 ~/.heng-agent、开发版 ~/.heng-agent-dev，各写各的）。

以前出了问题只剩终端上那几行，用户没有东西可以附进反馈里。现在记下：启动信息、每轮出错、模型请求重试、
MCP 连接失败、意料之外的异常（带完整调用栈）。/doctor copy 会把最近的日志一起复制出来。

🔴 只记「发生了什么」，不记密钥、请求头、对话内容：日志会被用户原样贴进反馈。写日志失败（没权限、磁盘满）
不影响程序本身，只在启动时说一句。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOGGER = "heng_agent"
LOG_FILE = "heng-agent.log"
MAX_BYTES = 1_000_000  # 1 MB 一个文件
BACKUPS = 3  # 另留 3 份旧的：总共不超过 4 MB

log = logging.getLogger(LOGGER)


def log_path(home: Path) -> Path:
    return home / "logs" / LOG_FILE


def setup(home: Path) -> str | None:
    """把 heng_agent 的日志写进数据目录；返回一句失败说明（成功返回 None）。重复调用只装一次。"""
    path = log_path(home)
    for handler in list(log.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            if Path(handler.baseFilename) == path:
                return None
            # 一个进程只写一个日志文件：换了数据目录（测试里每次是不同的临时目录）就关掉旧的，
            # 不然句柄一直开着，Windows 上那个目录删不掉
            log.removeHandler(handler)
            handler.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    except OSError as exc:
        return f"日志文件写不了（{path}：{exc}），这次不记日志"
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False  # 不往根日志器传：别的库配置了根日志器时，不把我们的记录打到终端上
    return None


def current() -> Path | None:
    """正在写的日志文件（还没 setup 或写不了时为 None）：出错提示里告诉用户去哪看。"""
    for handler in log.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return Path(handler.baseFilename)
    return None


def tail(home: Path, lines: int = 60) -> list[str]:
    """最近的日志（最多 lines 行）；没有日志返回空列表。/doctor copy 附在诊断信息后面。"""
    path = log_path(home)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
