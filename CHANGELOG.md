# 更新日志

## 发布步骤

1. 改版本号：`pyproject.toml` 的 `version` 与 `src/heng_agent/__init__.py` 的 `__version__`（测试会检查两处一致）；
2. 在本文件最上面记下这个版本的改动（GitHub Release 的说明就取这一节）；
3. 跑测试与代码检查都通过后提交，打标签 `v版本号`，分支与标签推送到 GitHub 与 Gitee；
4. 推到 GitHub 的标签会触发自动发布（`.github/workflows/release.yml`）：三个平台跑完测试 → 核对标签与版本号 →
   构建 → 发布到 PyPI → 建 GitHub Release。在 GitHub 的 Actions 页面看结果；
5. 本机的正式版升级：关掉所有 heng-agent 窗口后运行 `heng-agent upgrade`（或重新运行一键安装脚本）；
   `heng-agent --version` 应显示新版本号（不带 -dev）。

## 0.3.1（2026-10-28）

修好 Windows 一键安装的两个问题；有了 logo。

- **修复**：
  - Windows 一键安装开头报「The term '﻿#' is not recognized」：安装脚本带了 BOM，`irm | iex` 时粘在第一行开头；
  - Windows 一键安装装好后在「改 PATH」那一步中断，看不到「安装完成」、还冒一段像报错的红字。
- **logo**：H 的横杠换成命令行提示符「›」，青到蓝渐变（`docs/brand/`，含给网站、智码用的各种尺寸）；
  欢迎页左上角换成两行方块字「HENG」，正式版青色、开发版品红。

## 0.3.0（2026-10-28）

第一个发布到 PyPI 的版本：一条命令就能装；能在编辑器、桌面应用里用（ACP）。

- **安装与升级**：一键安装脚本（Windows 的 `install.ps1`、macOS / Linux 的 `install.sh`）——没有 uv 自动装，
  pypi.org 连不上自动改用清华镜像，已装过就升级；`heng-agent upgrade` 升级（Windows 上有别的 heng-agent 在运行时
  先让你关掉，再在新窗口里等自己退出后升级——运行中硬升级会把安装弄坏）；每天最多后台查一次新版本，欢迎页提示。
- **第一次运行的引导**：自动找电脑上现成的配置（环境变量里各家的密钥、Claude Code 里配的中转站）一键用上，
  没有就选服务商、填密钥；配好后测一下连接，失败说清楚原因（密钥不对、余额不足、连不上）。
- **内置技能**：debugging（排查问题）、writing-tests（写测试）、refactoring（安全重构）、codebase-tour（读懂项目）、
  security-check（安全检查）；用户或项目里放同名技能可以替换。`/skills` 按来源分组显示。
- **ACP 模式**（`heng-agent --acp`）：智码、Zed、JetBrains 等支持 Agent Client Protocol 的客户端可以在自己的界面里驱动
  heng-agent——流式回答与工具调用、权限确认在客户端弹窗（改文件时带改动对比）、取消、会话列表与恢复、
  模型 / 权限模式 / 思考三个下拉框。对接说明见 `docs/integration/acp.md`。
- **用环境变量给服务商**：`HENG_AGENT_BASE_URL`、`HENG_AGENT_API_KEY`、`HENG_AGENT_MODEL`……只在这次运行里用、
  不写进配置文件，终端、`-p`、`--acp` 都认，方便别的程序启动 heng-agent 时直接给。
- **开源**：Apache-2.0，GitHub（bkywksj/heng-agent）为主仓库、Gitee 镜像；CI 在 Windows / macOS / Linux 上跑全部测试。
- **修复**：
  - 技能、子智能体的多行描述（`description: |`）以前只读到一个「|」，模型看不到技能是干什么的——
    Claude Code 写法的技能（如 ruoyi-plus-uniapp 的 60 个）在系统提示词里全没有说明；
  - 标准输入不是终端（脚本、管道）时，方向键选择器会把「输入结束」当成取消；
  - 子智能体能吞掉主任务的取消；只读的子智能体取消不了；
  - 国内连不上 PyPI 时查新版本拿到的是旧数据。

## 0.2.0（2026-10-27）

第一个正式版。0.1.0 之后的主要改动：

- **交互**：输入框、权限确认、Shift+Enter 换行（默认可用）、命令补全、思考模式（/think）、后台任务（Ctrl+B、/tasks）、
  /rewind、/resume、/diff、/context、/export、/copy、任务完成提醒。
- **上下文**：自动压缩；一下子超出模型上限时自动压缩 / 截短工具结果后重发；状态栏显示距自动压缩还有多少；
  模型返回空回复时自动重试一次。
- **扩展**：技能、自定义命令、自定义子智能体角色（/agents）、钩子、MCP（stdio / http、工具多时按需加载、/mcp 逐级查看与重连）、
  git 工作树隔离（-w、/worktree）、/init 生成 AGENTS.md。
- **兼容 Claude Code 与 AGENTS.md**：说明文件（AGENTS.md、CLAUDE.md、@ 引用、优先级）、.claude/skills、.claude/agents、
  .claude/commands（$1、!`命令`）、权限规则、钩子（标准输入 JSON、JSON 决定）、.mcp.json。
- **安全**：拒绝规则对组合命令逐段检查、文件路径换写法也拦得住；项目里的钩子、MCP、命令里的 shell 都要确认信任。
- **查看状态**：/status、/usage、/stats、/hooks、/memory、/doctor。
- **脚本模式**：-p（text / json / stream-json 输出、管道输入、--allowed-tools）。
- **开发版与正式版分开**：命令、数据目录、标记各一套；配置里不认识的设置只提示不拦启动；会话文件带格式版本号。
