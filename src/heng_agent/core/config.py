"""配置：命令行参数 > 用户配置文件（~/.heng-agent/config.json）> 默认值。

模型相关的设置（协议、地址、模型名、密钥）都在服务商配置档里（model/providers.py），这里只记用哪个配置档，
以及命令行上临时覆盖的地址与模型名。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from heng_agent.core.version import default_home

PermissionMode = Literal["ask", "accept-edits", "plan", "auto"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, description="服务商配置档名；None 用配置文件里的 default（缺省 heng）")
    base_url: str | None = Field(default=None, description="临时覆盖配置档的地址")
    model: str | None = Field(default=None, description="临时覆盖配置档的模型名")
    max_tokens: int = Field(default=8192, gt=0)
    permission_mode: PermissionMode = "ask"
    workdir: Path = Field(default_factory=Path.cwd, description="工作目录：文件读写与命令都限定在这里")
    max_rounds: int = Field(default=50, gt=0, description="一问之内最多调多少轮模型，防止工具调用死循环")
    bash_timeout: float = Field(default=120.0, gt=0, description="单条命令超时（秒）")
    context_window: int = Field(default=128_000, gt=0, description="模型客户端没有配置档时用的上下文长度")
    compact_ratio: float = Field(default=0.8, gt=0, le=1, description="输入超过上下文的这个比例就自动压缩历史")
    image_preview: Literal["auto", "on", "off"] = Field(
        default="auto", description="附图时在终端里画缩略图（Sixel）：auto 只在 Windows Terminal 里画"
    )
    notify_after: float = Field(
        default=30.0, ge=0, description="任务跑超过这么多秒，结束时响铃提醒（Windows Terminal 会闪任务栏）；0 关闭"
    )
    mcp_tool_search: Literal["auto", "on", "off"] = Field(
        default="auto",
        description="MCP 工具按需加载：auto 在声明超过上下文 10% 时启用（先不发声明，模型用 tool_search 搜到再加载）",
    )
    thinking: bool = Field(default=False, description="思考模式：先深度思考再回答（Anthropic 协议；/think 随时切换）")
    thinking_budget: int = Field(default=8000, ge=1024, description="思考预算（token），Anthropic 要求至少 1024")
    claude_user_config: bool = Field(
        default=True,
        description="读 Claude Code 的用户级配置（~/.claude 的 CLAUDE.md、技能、子智能体）；项目里的不受影响",
    )
    update_check: bool = Field(
        default=True, description="每天最多一次在后台查有没有新版本，有就在欢迎页提示（开发版不查）"
    )
    claude_hooks_skip: list[str] = Field(
        default_factory=list,
        description="不跑的 Claude Code 钩子：命令里含其中任一段文字就跳过（专给 Claude Code 自己用的集成钩子，"
        "如会话记录器）",
    )
    home: Path = Field(
        default_factory=default_home,
        description="用户级目录：配置、会话、历史、用户记忆。正式版 ~/.heng-agent、"
        "开发版 ~/.heng-agent-dev（见 core.version）；"
        "HENG_AGENT_HOME 或 --home 指到别处即一套完全独立的配置",
    )
