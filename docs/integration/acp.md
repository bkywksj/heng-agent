# 接入 heng-agent（ACP）

给要把 heng-agent 接进自己界面的客户端（智码、编辑器插件……）。heng-agent 用
[Agent Client Protocol](https://agentclientprotocol.com)（ACP，协议版本 1）通信：客户端启动 `heng-agent --acp`，
双方在它的标准输入输出上逐行收发 JSON-RPC 2.0 消息。下面的示例消息都取自真实的通信记录。

只想在终端里用 heng-agent 的，不需要这份文档：终端标签页里直接运行 `heng-agent` 即可（服务商同样可以用下面的环境变量给）。

## 1. 启动

```
heng-agent --acp
```

| 环境变量 | 说明 |
|---|---|
| `HENG_AGENT_BASE_URL` | 服务地址。设了它才启用，设了就是这次运行的默认服务商（在模型下拉框里叫 `env`） |
| `HENG_AGENT_API_KEY` | 密钥 |
| `HENG_AGENT_MODEL` | 模型名（设了地址就必填，否则启动报错） |
| `HENG_AGENT_PROTOCOL` | `anthropic`（默认）或 `openai` |
| `HENG_AGENT_AUTH` | `auto`（默认）、`x-api-key` 或 `bearer` |
| `HENG_AGENT_HOME` | heng-agent 的数据目录（配置、会话），默认 `~/.heng-agent` |

- 环境变量给的服务商只在这次运行里用，不写进用户的配置文件；不设的话用用户在 heng-agent 里配好的默认服务商。
- **标准输出只有协议消息**，日志与报错在标准错误：客户端可以把标准错误记进自己的日志，别当协议解析。
- 一个进程可以同时开多个会话（不同的工作目录也行）。建议一个窗口 / 项目一个进程，关窗口时结束进程。

## 2. 一次完整的对话

```
客户端                                  heng-agent
  │── initialize ──────────────────────────▶│
  │◀─────────────── 能力、版本 ─────────────│
  │── session/new（cwd）───────────────────▶│
  │◀──── sessionId、模式、三个下拉框 ───────│
  │── session/prompt（用户的话）───────────▶│
  │◀── session/update：文字片段 ────────────│  （流式，多条）
  │◀── session/update：tool_call ───────────│  开始调用工具
  │◀── session/request_permission ──────────│  需要用户确认时
  │── 用户的选择 ──────────────────────────▶│
  │◀── session/update：tool_call_update ────│  工具结束
  │◀── session/update：文字片段 ────────────│
  │◀──────────── stopReason ───────────────│  这一问结束
```

### initialize

```json
→ {"jsonrpc": "2.0", "id": 1, "method": "initialize",
   "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "zhima", "version": "1.0"}}}
← {"jsonrpc": "2.0", "id": 1, "result": {
     "protocolVersion": 1,
     "agentCapabilities": {"loadSession": true,
                           "promptCapabilities": {"image": true, "embeddedContext": true},
                           "sessionCapabilities": {"list": {}}},
     "authMethods": [],
     "agentInfo": {"name": "heng-agent", "title": "heng-agent", "version": "0.2.0"}}}
```

不需要 `authenticate`：服务商与密钥用 heng-agent 自己的配置或上面的环境变量。

### session/new

```json
→ {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "E:\\项目\\shop", "mcpServers": []}}
← {"jsonrpc": "2.0", "id": 2, "result": {
     "sessionId": "20261028-143548-95be5f",
     "modes": {"currentModeId": "ask", "availableModes": [{"id": "ask", "name": "每次问", "description": "改文件、跑命令前都问你"}, …]},
     "configOptions": [
       {"id": "model", "name": "模型", "category": "model", "type": "select", "currentValue": "env",
        "options": [{"value": "env", "name": "env · claude-sonnet-5", "description": "环境变量 HENG_AGENT_BASE_URL（这次运行用）"}, …]},
       {"id": "mode", "name": "权限模式", "category": "mode", "type": "select", "currentValue": "ask", "options": […]},
       {"id": "thinking", "name": "思考", "category": "thought_level", "type": "select", "currentValue": "off", "options": […]}]}}
```

- `cwd` 是项目目录（绝对路径），heng-agent 的读写都限制在这个目录里，项目说明（AGENTS.md / CLAUDE.md）、技能、权限规则也按它找。
- `sessionId` 就是 heng-agent 的会话编号：之后能用 `session/load` 恢复，用户在终端里 `heng-agent --resume` 也接得上。
- `mcpServers` 目前忽略：MCP 服务用 heng-agent 自己的配置。

### session/prompt 与推送

```json
→ {"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
   "params": {"sessionId": "…", "prompt": [{"type": "text", "text": "看看这个目录"}]}}
```

`prompt` 里可以有：`text`、`image`（`data` 为 base64、`mimeType`）、`resource`（附带的文件内容，`resource.uri` + `resource.text`）、
`resource_link`（只给地址）。

这一问进行中，heng-agent 不断发 `session/update` 通知（`params.update.sessionUpdate` 区分类型）：

| sessionUpdate | 含义 | 界面怎么显示 |
|---|---|---|
| `agent_message_chunk` | 回答的一段文字（`content.text`，Markdown） | 依次拼到同一条消息里。调完工具后的新一段话开头已带空行 |
| `agent_thought_chunk` | 思考过程，或过程提示（自动压缩、跳过了没信任的钩子等，以「⋯」开头） | 折叠显示 |
| `tool_call` | 开始调用工具 | 一行工具卡片：`title` 如「执行：echo hello」，`kind` 决定图标 |
| `tool_call_update` | 工具结束：`status` 为 `completed` 或 `failed`，`content` 是结果（最多 4000 字） | 更新同一张卡片（按 `toolCallId` 对应） |
| `plan` | 任务清单（`entries`：`content`、`status` pending / in_progress / completed） | 进度列表，每次都是完整清单 |
| `user_message_chunk` | 只在 `session/load` 重放历史时出现：之前用户说的话 | 用户消息 |
| `config_option_update` | 配置项变了（如用旧接口切了模式） | 刷新三个下拉框 |

```json
← {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "…", "update": {
     "sessionUpdate": "tool_call", "toolCallId": "toolu_1_1", "title": "执行：echo hello",
     "kind": "execute", "status": "in_progress", "rawInput": {"command": "echo hello"}}}}

← {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "…", "update": {
     "sessionUpdate": "tool_call_update", "toolCallId": "toolu_1_1", "status": "completed",
     "content": [{"type": "content", "content": {"type": "text", "text": "退出码 0\nhello"}}]}}}
```

工具类型（`kind`）：`read` 读文件、`edit` 写或改文件、`execute` 跑命令、`search` 找文件与搜索、`fetch` 抓网页、
`think` 任务清单与子任务、`other` 其余（MCP 工具等）。

这一问结束时，`session/prompt` 的响应给出结束原因：

```json
← {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}
```

| stopReason | 含义 |
|---|---|
| `end_turn` | 正常结束（模型出错时也是它：错误会以「⚠ 出错了：……」作为一段回答文字发出来，会话照样可以继续） |
| `cancelled` | 被 `session/cancel` 取消 |
| `max_turn_requests` | 一问里调工具的轮数到了上限 |

## 3. 权限确认

当前权限模式要求确认时（默认的「每次问」：改文件、跑命令都要问），heng-agent **先**发这个工具的 `tool_call`，**再**发请求：

```json
← {"jsonrpc": "2.0", "id": 0, "method": "session/request_permission", "params": {
     "sessionId": "…",
     "toolCall": {"toolCallId": "toolu_1_1", "title": "执行：echo hello", "kind": "execute",
                  "rawInput": {"command": "echo hello"}},
     "options": [{"optionId": "once",   "name": "允许这一次",   "kind": "allow_once"},
                 {"optionId": "always", "name": "本会话都允许", "kind": "allow_always"},
                 {"optionId": "deny",   "name": "拒绝",         "kind": "reject_once"}]}}

→ {"jsonrpc": "2.0", "id": 0, "result": {"outcome": {"outcome": "selected", "optionId": "once"}}}
```

- 改文件（`kind` 为 `edit`）时 `toolCall.content` 带改动对比：
  `{"type": "diff", "path": "cart.py", "oldText": "FREE_SHIPPING = 99", "newText": "FREE_SHIPPING = 88"}`
  （新建文件时 `oldText` 为空）。确认框里直接显示它，用户不用猜要改什么。
- 用户关掉确认框、不选：回 `{"outcome": {"outcome": "cancelled"}}`，按**拒绝**处理（工具不执行，卡片状态 `failed`）。
- 进程或连接出问题导致拿不到回答时，heng-agent 一律按拒绝处理，绝不在没问到的情况下执行。
- 等用户确认期间这一问是暂停的；用户迟迟不选也不会超时。

## 4. 取消

```json
→ {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "…"}}
```

正在等模型就断开请求；正在跑命令就结束整个进程树；之后的工具不再执行。`session/prompt` 很快以 `"stopReason": "cancelled"` 返回
（实测 30 秒的命令几秒内停下）。已经输出的半段回答会留在会话里。

## 5. 下拉框：模型、权限模式、思考

`session/new`、`session/load` 的响应里有 `configOptions`（见上）。用户切换时：

```json
→ {"jsonrpc": "2.0", "id": 4, "method": "session/set_config_option",
   "params": {"sessionId": "…", "configId": "mode", "value": "plan"}}
← {"jsonrpc": "2.0", "id": 4, "result": {"configOptions": [ …三个下拉框的最新状态… ]}}
```

| configId | 取值 |
|---|---|
| `model` | 列出的服务商名（只列用户配好、能用的，加上环境变量给的 `env`）。切换立即生效，下一问就用新的 |
| `mode` | `ask` 每次问 / `accept-edits` 改文件不问 / `plan` 只读 / `auto` 全部放行 |
| `thinking` | `off` / `on` |

旧的 `session/set_mode`（`modeId` 同上）也支持，切换后 heng-agent 会发 `config_option_update` 让客户端刷新。
不认识的值返回 JSON-RPC 错误（`-32602`）。

## 6. 会话列表与恢复

```json
→ {"jsonrpc": "2.0", "id": 5, "method": "session/list", "params": {"cwd": "E:\\项目\\shop"}}
← {"jsonrpc": "2.0", "id": 5, "result": {"sessions": [
     {"sessionId": "20261028-143548-95be5f", "cwd": "E:\\项目\\shop", "title": "看看这个目录", "updatedAt": "2026-10-28T06:35:48+00:00"}]}}

→ {"jsonrpc": "2.0", "id": 6, "method": "session/load",
   "params": {"sessionId": "20261028-143548-95be5f", "cwd": "E:\\项目\\shop", "mcpServers": []}}
```

`session/load` 返回前，会用 `user_message_chunk` / `agent_message_chunk` 把之前的对话重放一遍（只重放文字），客户端照常显示即可。
列表按最近更新排序；`title` 是用户起的名字，没起就是第一个问题。

## 7. 注意

- **项目里没被信任的钩子与 MCP 服务不加载**（ACP 模式下没法弹信任确认），第一次提问时会以「⋯」开头的过程提示说明。
  用户在终端里对这个项目运行一次 `heng-agent`、确认信任后就会加载。
- 用户级的配置（服务商、MCP、钩子、权限规则、技能、AGENTS.md / CLAUDE.md）都照常生效，和终端里用一样。
- heng-agent 自己读写文件、跑命令，不用客户端的 `fs/*`、`terminal/*` 能力；客户端不实现它们也没关系。

## 8. 不花钱地调界面

开发客户端界面时可以用假模型（按脚本回答，不调真实的模型）：在 heng-agent 仓库里运行 `tests/acp_fake_agent.py` 代替
`heng-agent --acp`，用环境变量 `HENG_FAKE_SCRIPT` 给脚本——每一轮一个列表，字符串是说一段话，`["tool", 工具名, 参数]` 是调一个工具：

```bash
HENG_FAKE_SCRIPT='[["我先跑一下", ["tool", "bash", {"command": "echo hello"}]], ["跑完了"]]' \
  python tests/acp_fake_agent.py
```

工具会真的执行（在 `cwd` 里），权限确认也照常发出，适合调确认框、工具卡片、取消等交互。

## 9. 版本

ACP 实现在 `src/heng_agent/acp_server.py`，基于官方 SDK `agent-client-protocol` 0.12 系列。协议版本变化、
新增能力会写进 CHANGELOG.md。
