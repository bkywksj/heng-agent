"""错误类型。消息里不能带密钥：模型服务的报错原文可能回显请求头，转出来前要过滤。"""

from __future__ import annotations

import re

#: 各家「上下文超长」的报错写法：Anthropic「prompt is too long: 210000 tokens > 200000 maximum」；
#: OpenAI / DeepSeek「context_length_exceeded」「maximum context length is …」；Kimi「exceeded model token limit」；
#: 通义「Range of input length should be …」；其他「context window」「too many tokens」「input is too long」
CONTEXT_OVERFLOW = re.compile(
    r"prompt is too long|context[_ ]length|maximum context|context window|too many tokens|token limit"
    r"|input (?:is )?too long|range of input length|上下文.{0,6}(?:超|过长|上限)",
    re.IGNORECASE,
)


class AgentError(Exception):
    """heng-agent 自己能解释清楚的错误：直接把消息给用户看。"""


class ModelError(AgentError):
    """模型服务返回错误或连不上。

    retryable：限流、过载、5xx、连接失败这类「等一下再试多半能成」的错误；retry_after 是服务要求的等待秒数。
    """

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after

    @property
    def overflow(self) -> bool:
        """是不是「上下文超出模型上限」：这种错压缩历史后重发就能成。在错误本身上判断，不管它从哪儿抛出来
        （HTTP 状态码、流里的 error 事件）。限流的报错也常提到 token，但它可重试，不算——否则限流会被当成超长去压缩。"""
        return not self.retryable and CONTEXT_OVERFLOW.search(str(self)) is not None
