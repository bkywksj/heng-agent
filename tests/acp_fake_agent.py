"""测试用：以 --acp 模式启动 heng-agent，但模型换成按脚本回答的假模型（不花钱、结果确定）。

由 tests/test_acp.py 当成子进程启动，和真实的客户端一样走标准输入输出。
脚本从环境变量 HENG_FAKE_SCRIPT 读（JSON）：每轮一个列表，字符串 = 说一段话，["tool", 名字, 参数] = 调工具。
每次提问前故意 print 一行到标准输出：验证零星输出不会把协议搅乱。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel
from heng_agent import acp_server
from heng_agent.agent import Agent

rounds = [[tuple(item) if isinstance(item, list) else item for item in r]
          for r in json.loads(os.environ.get("HENG_FAKE_SCRIPT", "[]"))]  # fmt: skip
model = FakeModel(rounds)
acp_server.make_client = lambda profile, max_tokens: model  # type: ignore[assignment]

original_run = Agent.run


def noisy_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
    print("这是一行零星的输出，不该进协议")
    return original_run(self, *args, **kwargs)


Agent.run = noisy_run  # type: ignore[method-assign]
raise SystemExit(acp_server.run())
