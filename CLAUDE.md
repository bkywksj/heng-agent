# CLAUDE.md - heng-agent 项目

## 语言与编码

- 和用户交流、代码注释、文档、提交信息一律中文。
- 所有文件 UTF-8 无 BOM、LF 换行。Windows 下跑 Python 加 `PYTHONIOENCODING=utf-8`。
- 长脚本写成文件再执行，不在 shell 里内联多行 Python（heredoc 会吃掉反斜杠）。

## 项目定位

heng-agent 是类似 Claude Code 的**终端编码智能体**：读代码、改代码、跑命令，直到任务完成。
模型只通过 HTTP（Anthropic Messages 协议）接入，默认连 `heng serve`；开发阶段由它的 proxy 后端转发到 Claude，
以后换成 Heng 自己的模型只改配置。**本仓库不依赖 heng 的代码**，两边只通过 API 对接。

设计书：`docs/design/00-总体设计.md`。阶段与进度见设计书第 6 节。

## 🔴 分层规则

```
cli                 命令行入口
ui                  终端界面、斜杠命令、权限确认对话
agent               对话循环：模型 → 工具 → 结果喂回
context | session | permissions | compaction | commands | hooks | mcp
tools               内置工具（含 todo、web_fetch、skill、task 子智能体）与路径安全
model               模型客户端（协议、流式解析、服务商配置档）
core                消息与事件的数据类型、配置、错误、信任记录
```

上层只能引用下层，同一行 `|` 两边互不引用。`agent` 不认识具体的界面：权限确认、输出显示都通过回调注入，
这样同一个循环既能跑交互界面，也能跑 `-p` 非交互模式和测试。

## 🔴 安全底线

- 文件读写只能在工作目录内（`tools/paths.py` 统一校验），越界一律拒绝。
- 写文件、改文件、跑命令默认都要用户确认；只读工具（读文件、找文件、搜内容）不用确认。
- API 密钥只来自环境变量或用户目录的 `~/.heng-agent/config.json`（不在项目目录），不写进日志、会话文件或报错信息；
  `--list-providers` 只显示「已设置 / 未设置」。
- 新增会改动外部状态的工具，必须声明对的 `kind`（edit / exec / net / mcp，只读才是 read），并加测试证明「用户拒绝时什么都不发生」。

## 测试

- `tests/fake_model.py` 是脚本化的假模型：预先写好每一轮回复什么、调哪些工具。循环和工具的测试都用它，不花 API 费用。
- 提交前：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q` 与 `.venv/Scripts/ruff.exe check src tests`、
  `.venv/Scripts/ruff.exe format --check src tests` 全部通过。

## 🔴 Git

- 远程仓库一律私有。推送只走 Sigil（Gitee `origin`、GitCode `gitcode`），不用普通 `git push`。
- 逐个文件 `git add`，不用 `git add -A`；功能与注释改动分开提交。
- 提交信息用中文，写清「为什么」：修 bug 写根因与怎么发现的，新功能写用户看得见的变化。

## 🔴 开发版与正式版

- 开发、试功能一律用 `heng-dev`（数据在 `~/.heng-agent-dev`）；`heng-agent` 是从标签装的正式版，用户日常在用，
  不要拿它试、不要改 `~/.heng-agent` 里的东西（会话、配置、信任记录）。
- 测试不碰真实数据：`tests/conftest.py` 把 `HENG_AGENT_HOME`、`CLAUDE_CONFIG_DIR` 指到临时目录，新测试别绕开它。
- 不要从工作目录 `uv tool install`（会把没提交的改动装成正式版）；发布走 release 技能的步骤。

## 🔴 多会话并发

同一个仓库可能有别的会话在改（实际发生过：另一个会话已经实现并推送了 /status，这边差点重写一遍）：

- 动手前先 `git status` 与 `git log --oneline -5`：有没提交的改动、或出现了不认识的提交，先弄清楚再写；
- 只提交自己这次改的文件（逐个 `git add`），别把别人没提交的改动带进去；
- 要并行做两件事，各开一个 git 工作树（`heng-dev -w 名字`）。

## 绝对禁止（都是踩过的坑）

| ❌ 禁止 | 为什么 | ✅ 正确 |
|---|---|---|
| 只用管道喂按键的测试验证键盘与终端功能 | Shift+Enter 补丁因导入路径写错静默失效了很久，管道测试走不到 Windows 控制台那条路 | 还要在真实控制台里验证（test-development 技能） |
| `except ImportError: return False` 之类静默失败 | 同上：失败了没人知道 | 失败要提示用户，或加回归测试断言它生效 |
| 一个测试里多次调 `console.export_text()` | 它默认清空已记录的内容，第二次拿到空串 | 取一次存变量 |
| 用字符个数比较中文对齐 | 一个汉字占两格 | `rich.cells.cell_len` |
| `write_text("…\r\n")` 不加 `newline=""` | Python 又把 `\n` 换成 `\r\n`，变成 `\r\r\n` | 自己写 `\r\n` 时加 `newline=""` |
| 给 cmd 批处理按固定编码写中文路径 | cmd 按当前控制台代码页读，GBK / UTF-8 终端总有一种乱码 | 开头切 `chcp 65001`、跑完切回（见 `scripts/install_dev_command.py`） |
| 对付费模型发故意超长的请求「试试会不会报错」 | 百万 token 级的模型会真处理、真收钱 | 报错写法查文档，或用假客户端 |
| 测试、脚本里写「按进程名结束进程」的字面命令 | 智码守卫会拦下整条命令 | 换个例子（如 `git push --force`），或拼接字符串 |
| 很长的 heredoc / `python -c` | 会被截断或转义出错 | 先写成文件再运行 |
| `git push` / `fetch` / `pull` | 会弹凭据窗卡住 | 推送走 Sigil |
| Windows 上 heng-agent 开着时重装、升级它 | 加载的文件被占用，uv 装到一半失败，把环境删了一半，之后一启动就 `ModuleNotFoundError`（实测；也是全局命令以前莫名坏掉的原因） | 先关掉所有 heng-agent；`heng-agent upgrade` 会检查并在新窗口里等自己退出后再升级 |
| 只按单行 `键: 值` 解析 frontmatter | `description: |` 只读到一个「|」，续行里带冒号的还被拆成乱字段——ruoyi-plus-uniapp 的 60 个技能在系统提示词里全没有说明（实测） | 用 `tools/skill.py` 的 `parse_frontmatter`（认多行文本、列表、续行），别自己再写一个 |
| 直接改 `install.ps1`，或给它加 BOM | 带 BOM 时 `irm \| iex` 第一行报「The term '﻿#' is not recognized」；不带 BOM 又有中文时 PowerShell 5.1 下载到本地运行按 GBK 读乱（都实测过） | 改 `scripts/install.src.ps1`，运行 `scripts/build_install.py` 生成（中文写成字符编码、不带 BOM），有测试检查两者一致 |
| PowerShell 5.1 脚本里 `$ErrorActionPreference = 'Stop'` 时重定向外部程序的标准错误（`2>$null`） | 外部程序往标准错误写的每一行都算错误，脚本当场中断（实测：`uv tool update-shell` 提示一句「已在 PATH」，安装脚本就停了，看不到「安装完成」） | 这类调用前后临时设 `$ErrorActionPreference = 'Continue'` |
| `ruff format` 后拿旧文字精确替换 | 格式化会把长行拆开，对不上 | 先看当前原文再改 |
| `pipe = create_pipe_input().__enter__()`（不留上下文对象） | 函数一返回上下文对象被回收、管道随之关掉；Windows 的模拟输入关了还能写，Linux / macOS 是真管道，报 Bad file descriptor 或读到输入结束 | 把上下文对象存起来（见 `tests/test_config_ui.py` 的 `_OPEN_PIPES`），或用 `with` |
| 测试的结果依赖「当前环境有没有终端、选择器能不能开」 | Windows CI 碰巧没有控制台走了一条路，Linux 上走了另一条；选择器读到输入结束会当成取消、抛 KeyboardInterrupt 让整轮测试中断 | 测哪条路就明确指定（monkeypatch `_choose`、`_stdin_is_terminal`） |
| 用 `C:/…` 当「工作目录外的路径」、测 Git Bash 行为却不限平台 | 盘符路径只在 Windows 上是绝对路径；Git Bash 只在 Windows 上有 | 只在 Windows 上成立的用例加 `skipif(sys.platform != "win32")` |

## 技能索引（`.claude/skills/`，Claude Code、智码与 heng-dev 都读）

| 技能 | 什么时候用 |
|---|---|
| feature-checklist | 加斜杠命令、设置项、工具、事件、钩子事件、内置提示词、兼容映射——要改哪几处 |
| test-development | 写测试、真实环境实测、哪些功能必须在真实控制台里验证 |
| release | 发布新版本、装正式版、装 heng-dev |
| exp-sediment | 会话结束时把踩坑与决定沉淀下来（`/exp`） |
