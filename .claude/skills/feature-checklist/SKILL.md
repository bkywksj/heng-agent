---
name: feature-checklist
description: |
  给 heng-agent 加功能时「要改哪几处」的清单，防止漏改。加斜杠命令、设置项、内置工具、事件类型、钩子事件、
  内置提示词命令、Claude 兼容映射、说明文件名时先看这里。
  触发场景：加命令 / 新命令 / 斜杠命令 / 加设置 / 新设置项 / settings / 加工具 / 新工具 / Tool / 加事件 /
  钩子事件 / 内置提示词 / prompts / 兼容映射 / Claude 工具名 / 漏改 / 改哪几处
  （与 test-development 区别：本技能管「改哪里」，那个管「怎么验证」）
---

# 加功能的改动清单

heng-agent 的很多功能要在好几个地方一起登记，漏一处就会出现「命令能用但补全里没有」「设置写进配置却报不认识」
「工具能调用但钩子认不出」这类问题。每类改动按下面的清单逐项打勾。

## 斜杠命令（如 /status）

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `ui/repl.py` | `Repl.BUILTIN` 集合加命令名（不带 /）——不加会被当成自定义命令去找、报「未知命令」 |
| 2 | `ui/repl.py` | `HELP` 文本里加一行，放进对的分组 |
| 3 | `ui/repl.py` | `handle_command` 里加 `elif cmd == "/名字":` 分支；实现长的写成方法或放进 `ui/info.py` 一类模块 |
| 4 | `ui/prompt.py` | `BUILTIN_HELP` 加说明（补全菜单里显示） |
| 5 | `ui/prompt.py` | 不带参数的加进 `NO_ARGS`（Enter 直接执行）；必须带参数的加进 `REQUIRES_ARGS`（Enter 只补全） |
| 6 | `ui/repl.py` | 有参数要补全的，在 `_arg_hints` 里加候选 |
| 7 | `ui/repl.py` | 有别名的加进 `ALIASES`（如 `/cost` → `/usage`） |
| 8 | README | 用户看得见的功能写进 README |

## 设置项（config.json 的 settings 段）

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `core/config.py` | `Settings` 加字段，写 `description`（`/config` 里显示） |
| 2 | `configfile.py` | `SETTINGS_KEYS` 加字段名——不加的话配置里写了也会被当成「不认识的设置」忽略 |
| 3 | 用到它的地方 | 从 `agent.settings` / `settings` 取；子智能体、`set_workdir` 这类「复制设置」的地方会自动带上 |
| 4 | README | 写进对应功能的说明 |

注意：配置里不认识的字段启动时只提示不拦（新版写的字段旧版也能启动），但 `/config set` 改配置时仍严格检查。

## 内置工具

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `tools/xxx.py` | 继承 `Tool`：`name`、`kind`、`description`、`input_schema`、`run()`；需要时加 `summary()`（界面上的一行摘要） |
| 2 | `tools/base.py` | `builtin_tools()` 列表里加上 |
| 3 | `permissions.py` | `kind` 决定要不要确认：read / edit / exec / net / mcp（见 `RULES`）。会改外部状态的不能写成 read |
| 4 | `permissions.py` | 参数里是路径、命令、网址的，确认 `Rules.subject` 能取到（权限规则按它匹配） |
| 5 | `ui/repl.py` | `TOOL_LABELS` 加显示名（工具调用那行的动词） |
| 6 | `core/compat.py` | Claude 有对应工具的：`HENG_TO_CLAUDE`（给 Claude 钩子看的名字）、`CLAUDE_TOOLS`（.claude/agents 的 tools 字段）、`CLAUDE_RULE_TOOLS`（Claude 权限规则）三张表都要对上 |
| 7 | 测试 | 正常路径 + 用户拒绝时什么都不发生 |

## 事件类型（agent 交给界面的事件）

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `core/types.py` | 加 dataclass，并加进 `AgentEvent`（或模型层的 `ModelEvent`）联合类型 |
| 2 | `ui/repl.py` | `_on_event` 里处理（交互界面怎么显示） |
| 3 | `cli.py` | `run_print_mode` 的 `on_event`：`stream-json` 要不要输出这种事件、`text` 模式怎么显示 |

## 钩子事件

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `hooks.py` | `Event` 字面量、`TOOL_EVENTS` 或 `SESSION_EVENTS`；`run` / `run_event` 里的语义 |
| 2 | `core/compat.py` | Claude 有对应事件的加进 `CLAUDE_EVENTS` |
| 3 | `hooks.py` | Claude 钩子的输入字段与 JSON 决定（`_run_claude_tool` / `_run_claude_event`） |
| 4 | `agent.py` | 在对的时机调用 |
| 5 | `ui/info.py` | `/hooks` 的事件显示名 |

## 内置提示词命令（如 /init、/commit）

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `prompts/名字.md` | 提示词正文，末尾 `$ARGUMENTS` |
| 2 | `commands.py` | `BUILTIN_PROMPTS` 登记 |
| 3 | `ui/repl.py` | `HELP` 加一行 |
| 4 | `pyproject.toml` | `package-data` 已经是 `prompts/*.md`，新文件自动打包——**别改成列文件名**（以前漏打包，装好就崩） |

## 说明文件名（HENG.md、AGENTS.md、CLAUDE.md……）

`core/compat.py` 的 `PROJECT_MEMORY_NAMES`（根目录读哪些）、`MEMORY_PRIORITY`（优先级从低到高）、
`NESTED_MEMORY_NAMES`（子目录）、`project_memory_file`（写到哪）四处要一致；`context.py` 的 `MEMORY_ORDER_NOTE`
写给模型的优先级说明也要跟着改。

## 做完之后

- `ruff format` + `ruff check` + 全部测试通过；
- 用户看得见的变化写进 README 与 CHANGELOG.md 的下一个版本；
- 涉及终端、键盘、编码的，按 test-development 技能在真实控制台里验证。
