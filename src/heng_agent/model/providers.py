"""模型服务商：配置档（Profile）、内置预设、用户配置文件。

一个配置档 = 协议 + 地址 + 模型名 + 密钥来源 + 密钥请求头方式。和智码的 API Profile 是同一个思路。

用户配置文件 ~/.heng-agent/config.json（只在用户目录，不在项目目录，不进仓库）：

    {
      "default": "deepseek",
      "profiles": {
        "deepseek": {"preset": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"},
        "永夜": {"protocol": "anthropic", "base_url": "https://cc.example.cn", "model": "claude-opus-4-8",
                 "api_key": "sk-..."}
      }
    }

配置档可以用 preset 继承内置预设，只写要改的字段。密钥二选一：api_key_env（推荐，读环境变量）或 api_key（写在用户
配置文件里）。密钥永远不进会话文件、日志、报错信息。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from heng_agent.core.errors import AgentError
from heng_agent.core.net import check_proxy

Protocol = Literal["anthropic", "openai"]
AuthStyle = Literal["auto", "x-api-key", "bearer"]
CACHE_READ_RATE = 0.1  # 读缓存按输入价的倍数（Anthropic 定价）
CACHE_WRITE_RATE = 1.25  # 写缓存（5 分钟有效）按输入价的倍数


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Protocol
    base_url: str
    model: str = Field(description="模型名；预设只给建议值，各家改名很频繁，以服务商文档为准")
    api_key_env: str | None = Field(default=None, description="从这个环境变量读密钥（推荐）")
    api_key: str | None = Field(default=None, repr=False, description="直接写在用户配置文件里的密钥")
    # 密钥放哪个请求头：x-api-key（Anthropic 官方、DeepSeek）、bearer（Kimi、GLM、OpenAI 系）、auto 两个都带
    auth: AuthStyle = "auto"
    note: str = ""
    context_window: int = Field(
        default=128_000, gt=0, description="模型上下文长度（token）；输入超过它的 80% 时自动压缩对话历史"
    )
    # 价格：每百万 token 多少钱。预设不带价格（各家常调价、分模型与时段），
    # 在配置文件里按控制台的价格填，/cost 就会显示金额
    price_in: float | None = Field(default=None, ge=0, description="输入价格：每百万 token")
    price_out: float | None = Field(default=None, ge=0, description="输出价格：每百万 token")
    currency: str = "元"
    prompt_cache: bool = Field(
        default=True, description="Anthropic 协议开启提示缓存：系统提示词与历史只按 0.1 倍计价（写入时 1.25 倍）"
    )
    proxy: str | None = Field(
        default=None,
        description="这个服务商的请求走的代理（写法同 settings.proxy：代理地址或 direct）；不写就用 settings.proxy。"
        "例如国外的走代理、国内的写 direct 直连",
    )

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, value: str | None) -> str | None:
        return check_proxy(value)

    def cost(self, usage: dict[str, int]) -> float | None:
        """按配置的价格算花费；没配价格返回 None。

        开了提示缓存时 input_tokens 只是没命中的部分：读缓存按输入价 0.1 倍、写缓存按 1.25 倍另算（Anthropic 定价）。
        """
        if self.price_in is None or self.price_out is None:
            return None
        tokens_in = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_tokens", 0) * CACHE_READ_RATE
            + usage.get("cache_write_tokens", 0) * CACHE_WRITE_RATE
        )
        return (tokens_in * self.price_in + usage.get("output_tokens", 0) * self.price_out) / 1e6

    def resolve_key(self) -> str | None:
        """现取现用，不缓存：避免密钥被序列化进会话或日志。"""
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def auth_headers(self) -> dict[str, str]:
        key = self.resolve_key()
        if not key:
            return {}
        headers: dict[str, str] = {}
        if self.auth in ("auto", "x-api-key"):
            headers["x-api-key"] = key
        if self.auth in ("auto", "bearer"):
            headers["authorization"] = f"Bearer {key}"
        return headers


#: 内置预设（2026-10 核对过各家文档的地址；模型名只是建议值）
PRESETS: dict[str, Profile] = {
    "heng": Profile(protocol="anthropic", base_url="http://127.0.0.1:8765", model="heng-claude",
                    api_key_env="HENG_AGENT_API_KEY", note="本机 heng serve（自己的模型，或 proxy 转发）"),
    "anthropic": Profile(protocol="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5-5",
                         context_window=200_000,
                         api_key_env="ANTHROPIC_API_KEY", auth="x-api-key", note="Anthropic 官方 API"),
    "deepseek": Profile(protocol="anthropic", base_url="https://api.deepseek.com/anthropic", model="deepseek-v4-pro",
                        api_key_env="DEEPSEEK_API_KEY", note="DeepSeek（Anthropic 兼容接口）"),
    "kimi": Profile(protocol="anthropic", base_url="https://api.moonshot.cn/anthropic", model="kimi-k2.7-code",
                    api_key_env="MOONSHOT_API_KEY", auth="bearer", note="Kimi（Anthropic 兼容接口）"),
    "glm": Profile(protocol="anthropic", base_url="https://open.bigmodel.cn/api/anthropic", model="glm-5.2",
                   api_key_env="ZHIPU_API_KEY", auth="bearer", note="智谱 GLM（Anthropic 兼容接口）"),
    "qwen": Profile(protocol="openai", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen3-coder-plus", api_key_env="DASHSCOPE_API_KEY", auth="bearer",
                    note="通义千问（阿里云百炼，OpenAI 兼容接口）"),
    "openai": Profile(protocol="openai", base_url="https://api.openai.com/v1", model="gpt-5.1",
                      api_key_env="OPENAI_API_KEY", auth="bearer", note="OpenAI"),
    "openrouter": Profile(protocol="openai", base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4-pro",
                          api_key_env="OPENROUTER_API_KEY", auth="bearer", note="OpenRouter（一个密钥用多家模型）"),
    "ollama": Profile(protocol="openai", base_url="http://127.0.0.1:11434/v1", model="qwen3-coder",
                      context_window=32_768,
                      auth="bearer", note="本机 Ollama（不需要密钥）"),
}  # fmt: skip

DEFAULT_PROFILE = "heng"


def config_path(home: Path) -> Path:
    return home / "config.json"


PROJECT_CONFIG = ".heng-agent/config.json"


def _apply_config(path: Path, profiles: dict[str, Profile], default: str, *, project: bool) -> tuple[str, str | None]:
    """把一份配置文件叠加到 profiles 上，返回 (新的 default, 这份配置 settings 段里的 proxy)。

    preset 可以引用任何已加载的配置档（内置预设，或上一层配置文件里定义的），只写要改的字段。
    项目配置文件不许出现 api_key：项目目录可能被提交进仓库，密钥写在那里会泄露。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path} 不是合法 JSON：{exc}") from exc
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    if project and ("proxy" in settings or any("proxy" in dict(p) for p in (data.get("profiles") or {}).values())):
        # 和 api_key 同理：项目配置跟着仓库走，别人的仓库能借它把你的模型请求（连同请求头里的密钥）转到他的代理上
        raise AgentError(
            f"{path} 写了代理（proxy）：项目配置跟着仓库走，不能替你决定请求发到哪；代理写在用户目录的配置里"
        )
    try:
        proxy = check_proxy(settings.get("proxy"))
    except ValueError as exc:
        raise AgentError(f"{path} 的 settings.proxy 有误：{exc}") from exc
    for name, raw in (data.get("profiles") or {}).items():
        raw = dict(raw)
        if project and "api_key" in raw:
            raise AgentError(
                f"{path} 的配置档 {name} 写了 api_key：项目目录可能被提交进仓库，密钥只能放环境变量（api_key_env）"
                "或用户目录的配置文件"
            )
        base = raw.pop("preset", None)
        if base is not None and base not in profiles:
            raise AgentError(f"配置档 {name} 的 preset「{base}」不存在，可选：{', '.join(profiles)}")
        merged = {**(profiles[base].model_dump() if base else {}), **raw}
        try:
            profiles[name] = Profile(**merged)
        except ValueError as exc:
            raise AgentError(f"配置档 {name} 有误：{exc}") from exc
    return str(data.get("default", default)), proxy


def load_profiles(home: Path, workdir: Path | None = None) -> tuple[dict[str, Profile], str]:
    """内置预设 → 用户配置（home/config.json）→ 项目配置（workdir/.heng-agent/config.json），后者覆盖前者；
    最后是环境变量 HENG_AGENT_BASE_URL 等给的一次性服务商（见 env_profile），有就设为默认。

    多实例互不干扰：配置文件只读不写；每个实例的 --provider、/provider 切换都只在自己的内存里。

    没写 proxy 的配置档用 settings.proxy（项目配置覆盖用户配置）：这样建客户端、取模型列表的各处只看 profile.proxy。
    """
    profiles = dict(PRESETS)
    default = DEFAULT_PROFILE
    layers = [(config_path(home), False)]
    if workdir is not None:
        layers.append((workdir / PROJECT_CONFIG, True))
    settings_proxy: str | None = None
    for path, project in layers:
        if path.is_file():
            default, proxy = _apply_config(path, profiles, default, project=project)
            settings_proxy = proxy or settings_proxy
    from_env = env_profile()
    if from_env is not None:
        profiles[ENV_PROFILE], default = from_env, ENV_PROFILE
    if settings_proxy:
        profiles = {n: p if p.proxy else p.model_copy(update={"proxy": settings_proxy}) for n, p in profiles.items()}
    if default not in profiles:
        raise AgentError(f"默认配置档「{default}」不存在，可选：{', '.join(profiles)}")
    return profiles, default


ENV_PROFILE = "env"


def env_profile(environ: dict[str, str] | None = None) -> Profile | None:
    """调用方（如智码）启动 heng-agent 时用环境变量给的服务商：只在这次运行里用，不写进配置文件。

    设了 HENG_AGENT_BASE_URL 才启用；HENG_AGENT_API_KEY 密钥、HENG_AGENT_MODEL 模型名（必填）、
    HENG_AGENT_PROTOCOL anthropic（默认）/ openai、HENG_AGENT_AUTH auto（默认）/ x-api-key / bearer。

    用 heng-agent 自己的变量名、不读 ANTHROPIC_BASE_URL 这类：不少人为了别的工具全局设着 ANTHROPIC_API_KEY，
    一看到就用会悄悄绕开用户在 heng-agent 里配好的默认服务商。专用的名字只在有意设置时生效。
    """
    env = os.environ if environ is None else environ
    base_url = env.get("HENG_AGENT_BASE_URL", "").strip()
    if not base_url:
        return None
    model = env.get("HENG_AGENT_MODEL", "").strip()
    protocol = env.get("HENG_AGENT_PROTOCOL", "anthropic").strip() or "anthropic"
    auth = env.get("HENG_AGENT_AUTH", "auto").strip() or "auto"
    if not model:
        raise AgentError("设了 HENG_AGENT_BASE_URL 就要同时设 HENG_AGENT_MODEL（用哪个模型）")
    if protocol not in ("anthropic", "openai"):
        raise AgentError(f"HENG_AGENT_PROTOCOL 只能是 anthropic 或 openai，现在是「{protocol}」")
    if auth not in ("auto", "x-api-key", "bearer"):
        raise AgentError(f"HENG_AGENT_AUTH 只能是 auto、x-api-key 或 bearer，现在是「{auth}」")
    return Profile(protocol=protocol, base_url=base_url, model=model, auth=auth,  # type: ignore[arg-type]
                   api_key=env.get("HENG_AGENT_API_KEY", "").strip() or None,
                   note="环境变量 HENG_AGENT_BASE_URL（这次运行用）")  # fmt: skip
