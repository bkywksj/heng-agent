"""测试用的假 MCP 服务：标准输入输出上逐行 JSON-RPC，真实协议流程。

工具：add（两数相加）、fail（返回 isError）。tools/list 分两页返回，用来测分页。
启动时若设了环境变量 FAKE_MCP_MARKER，就往那个文件里写一行，测试据此判断服务有没有被启动。
"""

from __future__ import annotations

import json
import os
import sys

if os.environ.get("FAKE_MCP_MARKER"):
    with open(os.environ["FAKE_MCP_MARKER"], "a", encoding="utf-8") as fh:
        fh.write("started\n")

TOOLS = [
    {"name": "add", "description": "两数相加", "inputSchema": {"type": "object", "properties": {
        "a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]}},
    {"name": "fail", "description": "总是失败", "inputSchema": {"type": "object", "properties": {}}},
]  # fmt: skip


def reply(rid: object, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        continue  # 通知（notifications/initialized）不回复
    print("这是一行日志，不是协议内容", file=sys.stdout, flush=True)  # 混在标准输出里的噪音，客户端要能跳过
    if method == "initialize":
        reply(rid, {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"}})  # fmt: skip
    elif method == "tools/list":
        page = 1 if params.get("cursor") == "p2" else 0
        reply(rid, {"tools": [TOOLS[page]], **({"nextCursor": "p2"} if page == 0 else {})})
    elif method == "tools/call":
        if params["name"] == "add":
            args = params["arguments"]
            reply(rid, {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]})
        else:
            reply(rid, {"content": [{"type": "text", "text": "出错了"}], "isError": True})
    else:
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "没有这个方法"}}) + "\n"
        )
        sys.stdout.flush()
