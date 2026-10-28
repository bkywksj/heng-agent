# heng-agent

类似 Claude Code 的终端编码智能体：用中文派活，它读代码、改代码、跑命令，直到任务完成。
支持 Anthropic 与 OpenAI 两种协议，内置 DeepSeek、Kimi、GLM、通义千问、OpenAI、OpenRouter、Ollama 与 heng 自己的模型，
也能接任意兼容中转站。

## 安装

一条命令装好（没有 uv 会先自动装 uv；pypi.org 连不上会自动改用清华镜像；已装过就升级到最新版）。
有新版本时欢迎页会提示，运行 `heng-agent upgrade` 升级（每天最多后台查一次，可用 `settings.update_check` 关掉）：

```powershell
# Windows（PowerShell）
irm https://gitee.com/bkywksj/heng-agent/raw/master/install.ps1 | iex
```

```bash
# macOS / Linux
curl -LsSf https://gitee.com/bkywksj/heng-agent/raw/master/install.sh | sh
```

（在国外把地址换成 `https://raw.githubusercontent.com/bkywksj/heng-agent/master/install.ps1`、`…/install.sh`。）

已经有 [uv](https://docs.astral.sh/uv/)（Python 的包管理工具，会自动准备好 Python）的话，也可以直接：

```bash
uv tool install heng-agent      # 装成全局命令，任何目录都能敲 heng-agent
heng-agent upgrade              # 升级（Windows 上会开个新窗口、等 heng-agent 退出后再升级）
uv tool uninstall heng-agent    # 卸载
```

从源码装：`git clone` 本仓库后，在仓库目录里 `uv tool install .`。
装完如果提示「not on your PATH」，运行一次 `uv tool update-shell` 再重开终端。

### 平台支持

主要在 Windows（Windows Terminal）上开发与测试；macOS、Linux 上核心功能（对话、工具、MCP、钩子、会话……）
都能用，以下几项暂时只在 Windows 上有：

| 功能 | Windows | macOS / Linux |
|---|---|---|
| AI 回复时按 ESC 中断、Ctrl+B 转后台 | ✅ | 暂不支持，用 Ctrl+C 中断 |
| Alt+V 粘贴剪贴板里的图片、文件 | ✅ | 暂不支持，用 `@文件路径` |
| Shift+Enter 换行 | ✅ | 取决于终端；Ctrl+J、Alt+Enter 处处可用 |

## 最快上手（以 DeepSeek 为例）

```bash
export DEEPSEEK_API_KEY=你的密钥                # PowerShell：$env:DEEPSEEK_API_KEY="..."
cd 你的项目目录
heng-agent --provider deepseek
```

## 服务商

`heng-agent --list-providers` 列出全部配置档、默认用哪个、密钥是否已设置（只显示已设置 / 未设置，不显示密钥）。

| 名字 | 协议 | 密钥环境变量 | 说明 |
|---|---|---|---|
| `heng` | anthropic | `HENG_AGENT_API_KEY` | 本机 heng serve（自己的模型或 proxy 转发），**默认** |
| `anthropic` | anthropic | `ANTHROPIC_API_KEY` | Anthropic 官方 |
| `deepseek` | anthropic | `DEEPSEEK_API_KEY` | DeepSeek |
| `kimi` | anthropic | `MOONSHOT_API_KEY` | Kimi（月之暗面） |
| `glm` | anthropic | `ZHIPU_API_KEY` | 智谱 GLM |
| `qwen` | openai | `DASHSCOPE_API_KEY` | 通义千问（阿里云百炼） |
| `openai` | openai | `OPENAI_API_KEY` | OpenAI |
| `openrouter` | openai | `OPENROUTER_API_KEY` | 一个密钥用多家模型 |
| `ollama` | openai | 不需要 | 本机 Ollama |

预设里的模型名只是建议值（各家改名很频繁），用 `--model` 临时换，或在配置文件里改。
界面里随时 `/provider deepseek` 切换服务商（对话历史保留），`/model 名字` 换模型。

### 配置文件 `~/.heng-agent/config.json`

自己的中转站、改默认服务商、改预设的模型名都写在这里：

```json
{
  "default": "deepseek",
  "profiles": {
    "deepseek": {"preset": "deepseek", "model": "deepseek-flash"},
    "永夜": {
      "protocol": "anthropic",
      "base_url": "https://你的中转站地址",
      "model": "claude-opus-4-8",
      "api_key": "sk-..."
    }
  }
}
```

- `preset`：继承一个内置预设，只写要改的字段。
- 密钥二选一：`api_key_env`（读环境变量，推荐）或 `api_key`（写在这个文件里；文件只在你的用户目录，不会进任何项目）。
- `auth`：密钥放哪个请求头，`auto`（默认，两种都带）/ `x-api-key` / `bearer`。中转站报 401 时试试改这个。

**不支持 Claude 订阅账号登录（OAuth）**：Anthropic 的条款只允许订阅账号的登录凭据用在它自己的客户端里，
第三方工具使用有封号风险。用 Claude 请用 API 密钥（`anthropic` 预设）或中转站。

## 接自己的模型（heng）

```bash
# 终端 1：在 heng 模型仓库的目录启动模型服务
.venv/Scripts/heng.exe serve configs/serve/heng-mini.yaml          # 自己训练的模型
# 或 configs/serve/proxy-claude.yaml（需 HENG_UPSTREAM_API_KEY），把请求转发到 Claude

# 终端 2：在项目目录
heng-agent --provider heng --model heng-mini-0.1
```

## 常用参数

| 参数 | 作用 |
|---|---|
| `--provider 名字` | 用哪个服务商配置档 |
| `--model` / `--base-url` | 临时覆盖模型名 / 地址 |
| `--resume` | 接着这个目录上次的会话聊 |
| `-p "任务"` | 非交互：执行完这一个任务就退出（需要确认的操作一律拒绝） |
| `--permission-mode ask` | 默认：改文件、跑命令前都问你 |
| `--permission-mode accept-edits` | 改文件不问，跑命令还问 |
| `--permission-mode plan` | 只读：只看不改 |
| `--permission-mode auto` | 全部不问，直接执行（只在 CI、一次性沙箱等可信环境用；仍不能读写工作目录以外） |
| `--workdir` | 指定工作目录（默认当前目录） |

## 界面里

- 输入框：**Enter 发送，Shift+Enter 换行**（其他终端里用 Ctrl+J / Alt+Enter），`/` 补全命令，`@` 补全文件路径，
  Shift+Tab 切换权限模式（边框颜色跟着变）。底部状态栏：权限模式 · 服务商与模型 · 目录 · 上下文用量。
- **拖入文件 / 粘贴截图**：消息里出现的文件绝对路径（拖入文件、**智码终端里 Ctrl+V 粘贴的截图**都是这样进来的）
  发送时自动变成附件；工作目录以外的文件先复制到 `.heng-agent/attachments/`（自带 `.gitignore`，不进仓库）。
  在 Windows Terminal 里粘贴截图用 **Alt+V**：Windows Terminal 自己处理 Ctrl+V 且只粘文字，截图到不了程序
  （Claude Code 在 Windows 上也是 Alt+V）；把 Ctrl+V 交给程序的终端里 Ctrl+V 也能直接粘截图。
- **/config**：在对话里查看与修改配置，改完立即生效。`/config provider` 引导式添加服务商（自动取模型列表让你挑、
  密钥输入不回显）；`/config set profiles.yongye.model claude-opus-5`、`/config set settings.permission_mode plan`；
  `/config edit` 用记事本打开配置文件。改错了自动恢复原配置。加 `--project` 改项目配置。
- 配置文件的 `settings` 段可以写启动默认值：`permission_mode`、`max_tokens`、`max_rounds`、`bash_timeout`、
  `context_window`、`compact_ratio`、`image_preview`（命令行参数优先）。
- **图片缩略图**：附图发送后，在 Windows Terminal（1.22 起）里直接画出缩略图（Sixel）。`image_preview`：
  `auto`（默认，只在 Windows Terminal 里画）/ `on` / `off`；不支持 Sixel 的终端会显示成一屏乱码，所以默认不在别处画。
- **会话**：`/resume` 弹出这个目录的会话列表选一个接着聊（`/resume 关键词` 按会话名、第一句话筛选）；`/rename 名字`
  给会话起名；启动时 `heng-agent -c` 直接接最近的会话。
- **模型**：`/model` 向服务商取可用模型、方向键选，当前会话立即生效，可以顺手存成默认。
- **权限规则**：确认框里选「以后都允许」，规则写进用户配置；`/permissions` 查看与增删，例如
  `/permissions allow bash(npm test)`、`/permissions deny bash(git push*)`。写法 `工具名` 或 `工具名(匹配内容)`，可用 `*`。
  **拒绝规则优先于一切**（auto 模式下也拒绝）；**bash 组合命令（`&&` `;` `|` 重定向、`$(...)` 等）不适用允许规则**，
  照常询问，免得 `bash(git status*)` 放过 `git status && rm -rf …`；**项目配置只能加拒绝规则**，别人的仓库没法给自己放开命令。
- **提示缓存**（Anthropic 协议默认开启）：系统提示词与对话历史命中缓存后按 0.1 倍计价，长对话省钱明显；`/cost`
  显示命中率。不想用就在配置档里写 `"prompt_cache": false`。
- **输入框快捷键**：Ctrl+R 搜历史输入（Enter 采用、接着改）；Ctrl+G 用外部编辑器写长消息（`$EDITOR`，没设用记事本；
  VS Code 写 `code --wait`）；Ctrl+T 显示 / 隐藏输入框上方的任务清单。
- **会话工具**：`/context` 看上下文被什么占着；`/diff` 看这次会话改过的文件；`/copy` 复制上一条回复；
  `/export [路径]` 导出 Markdown；`/compact 要求` 按你的要求压缩（如「只保留数据库迁移相关的」）。
- **任务完成提醒**：任务跑超过 `notify_after` 秒（默认 30，0 关闭）结束时响铃，Windows Terminal 会闪任务栏。
- **记忆**：输入以 `#` 开头的一句话（如 `# 这个项目用 pnpm`）不发给模型，选记进项目或用户的 HENG.md，立即生效；
  `/memory` 查看、`/memory edit project|user` 用编辑器改。大项目的子目录可以有自己的 HENG.md（如 `frontend/HENG.md`），
  模型第一次读写那个目录的文件时自动附上，每份只附一次。
- **内置命令**：`/commit [要求]` 按仓库风格写提交信息并提交（不 push、不改历史）；`/review [关注点]` 审查没提交的改动，
  按严重程度列问题。在 `.heng-agent/commands/` 放同名的 `commit.md` / `review.md` 就能按自己团队的规矩改写。
- **`/doctor`**：检查配置、服务商连接（会真的连一次）、密钥、终端、git、记忆、钩子、MCP、会话目录。
- **后台任务**：开发服务器、watch 这类不会自己结束的命令，模型会用 `run_in_background` 放到后台跑，之后用 `bash_output`
  看新输出、`kill_task` 停掉。前台命令跑着时按 **Ctrl+B** 也能把它转到后台（进程不停、已有输出不丢）。
  `/tasks` 查看、`/tasks kill bg1` 停止；状态栏会显示「后台 N」；退出 heng-agent（含 `-p` 结束）时停掉全部后台任务。
- **思考模式**：`/think` 切换（`/think on|off`、`/think 16000` 设预算、`/think show` 看最近一次思考），也可在配置
  `settings.thinking` / `settings.thinking_budget` 里常开。思考过程不刷屏：等待动画里显示想了多少字，结束后折叠成一行。
  Anthropic 协议下思考块（带签名）原样存进历史、调工具后原样传回（服务要求）；OpenAI 协议（DeepSeek 推理模型等）
  是否思考由模型决定，`reasoning_content` 只显示、不进历史（传回去会报错）。
- **自定义子智能体角色**：在 `.heng-agent/agents/名字.md`（项目）或 `~/.heng-agent/agents/`（用户）定义「测试员」「审查员」
  这类角色——开头写 `description`（模型据此挑）与 `tools`（不写就是只读），正文是它的提示词。模型用 `task` 工具派它们。
  只读角色不用确认、可以并行；带 `write_file` / `edit_file` / `bash` 的角色按你的权限模式确认、不并行，改的文件能 `/undo`。
  `/agents` 查看，`/agents new 名字` 建模板并打开编辑器，存好马上生效。
- **git 工作树隔离**：想试个有风险的改动、或让两件事互不干扰时，`heng-agent -w 名字` 启动，或会话里 `/worktree new 名字`，
  就在 `.heng-agent/worktrees/名字`（分支 `heng/名字`）里干活，不碰主分支和你没提交的改动（这个目录自动被 git 忽略）。
  `/worktree` 列出、`/worktree switch 名字|main` 切换、`/worktree remove 名字` 删除（分支没合并就保留）。
  改完在工作树里 `/commit`，回主目录 `git merge heng/名字`——合并可能冲突，不自动做。

## 兼容 Claude Code 与 AGENTS.md

从别的编码工具转过来，项目里现成的配置直接能用，不用迁移：

| 配置 | 读哪里 | 说明 |
|---|---|---|
| 项目说明 | 根目录的 `HENG.md`、`AGENTS.md`、`CLAUDE.md`、`CLAUDE.local.md`；子目录里的同名文件 | 都读；内容一样的（如 CLAUDE.md 链到 AGENTS.md）只算一份；CLAUDE.md 的 `@路径` 引用会展开 |
| 用户说明 | `~/.heng-agent/HENG.md`、`~/.claude/CLAUDE.md` | |
| 技能 | `.claude/skills/`、`~/.claude/skills/` | 格式一样；同名时 `.heng-agent/` 的优先 |
| 子智能体 | `.claude/agents/`、`~/.claude/agents/` | Claude 的工具名（Read、Bash……）自动对应；不写 tools 时照 Claude 的规则继承全部工具 |
| MCP | 项目根目录的 `.mcp.json` | 支持 `${变量}`、`${变量:-默认值}`；项目里的服务照样要确认信任才启动 |
| 自定义命令 | `.claude/commands/`（可分子目录）、`~/.claude/commands/` | 支持 `$ARGUMENTS`、`$1`…`$9`、`argument-hint`、``!`命令` ``（先跑命令把输出拼进去：用户级的直接跑，项目里的先问你，脚本模式只在 auto 时跑） |
| 权限规则 | `~/.claude/settings.json`；项目 `.claude/settings.json`、`settings.local.json` | 规则自动翻译（`Bash(npm test:*)` → `bash(npm test*)` 等）；项目里的只采用拒绝规则；它的 `defaultMode` 不跟，权限模式由 heng-agent 自己定 |
| 钩子 | 同上几个 `settings.json` 的 `hooks` | 支持 PreToolUse、PostToolUse、UserPromptSubmit、Stop、SessionStart：照 Claude 的约定从标准输入传 JSON（工具名报 `Bash`、`Edit`……，文件参数是绝对路径的 `file_path`），设 `CLAUDE_PROJECT_DIR`，认退出码 2 与标准输出的 JSON 决定（`decision: block`、`permissionDecision: deny`、`additionalContext`）；项目里的要确认信任 |

拒绝规则对组合命令逐段检查（`cd x && 危险命令` 也拦得住），文件路径换写法（`./.env`、`E:\项目\.env`）也拦得住。

欢迎页会显示「兼容加载：……」，`/doctor` 也列出来。

几份说明并存时：系统提示词里按优先级从低到高排列——用户级在前、项目级在后，同一级里
`CLAUDE.md` < `AGENTS.md` < `CLAUDE.local.md` < `HENG.md`，并明确告诉模型「内容冲突时以后面的为准」；子目录的说明比根目录的更具体。
读到了写给 Claude Code 的说明时，还会附一段工具名对照（Bash → bash、Edit → edit_file……）。`/memory` 按这个顺序列出每份说明
各占多少 token、`#` 快速记忆写进哪个文件，合计超过 1 万 token 时提醒精简（如 CLAUDE.md 里只写一行 `@AGENTS.md`）。

`/init` 默认生成 `AGENTS.md`（跨工具的开放标准，别的工具也读得到），
项目里已有说明文件就更新现有的。不想读 Claude Code 的用户级配置（`~/.claude` 里的），在配置里设
`settings.claude_user_config` 为 `false`；项目里的照读。

有些 Claude 钩子是专给 Claude Code 自己用的集成（如智码的会话记录器），在 heng-agent 里跑会出错或记错，
用 `settings.claude_hooks_skip` 跳过：命令里含其中任一段文字的 Claude 钩子不跑，如 `["aicoder-claude-session-recorder"]`。

## 开发版与正式版

开发 heng-agent 的同时自己也在用它，两边互不干扰：

| | 正式版 | 开发版 |
|---|---|---|
| 命令 | `heng-agent` | `heng-dev`（`scripts/install_dev_command.py` 装一次；或直接跑仓库里的 `.venv/Scripts/heng-agent`） |
| 代码 | 从打了标签的版本装进环境，装好就不变 | 直接跑仓库源码，改完立即生效 |
| 数据 | `~/.heng-agent` | `~/.heng-agent-dev`（第一次运行从正式版复制一份配置，之后各改各的） |
| 标记 | — | 欢迎页品红边框与「开发版」徽标、状态栏徽标、窗口标题，版本号带 git 位置 |

是不是开发版看代码在哪儿就知道（仓库的 `src/` 下就是开发版），也可以用环境变量 `HENG_AGENT_CHANNEL=dev|release` 强制；
`HENG_AGENT_HOME` 或 `--home` 可以直接指定数据目录。配置里有当前版本不认识的设置（新版写的）时只提示、不拦启动。

发布：改 `pyproject.toml` 与 `src/heng_agent/__init__.py` 里的版本号 → 在 CHANGELOG.md 记下改动 → 提交、打标签 `v版本号` →
从标签构建安装包装成正式版（见 CHANGELOG.md 开头的「发布步骤」）。

## 脚本模式（-p）

```bash
heng-agent -p "把 cart.py 的运费改成 15 元" --allowed-tools "edit_file,bash(python -m pytest*)"
cat error.log | heng-agent -p "分析这段错误"           # 管道内容附在问题后面
echo "解释一下 README" | heng-agent -p                 # 只写 -p：管道内容就是问题
heng-agent -p "统计代码行数" --output-format json        # 结束时输出一个 JSON：result、session_id、usage……
heng-agent -p "跑测试" --output-format stream-json      # 每个事件一行 JSON，给别的程序实时展示进度
```

`-p` 没人可问，需要确认的操作默认都拒绝：用 `--allowed-tools` 放行（规则写法同 `/permissions`，逗号分隔），
`--disallowed-tools` 额外禁止；或用 `--permission-mode accept-edits` / `auto`。

- 执行中按 **ESC**（或 Ctrl+C）中断当前任务：等模型时立刻断开（不会被当成网络抖动去重试），跑命令时连同子进程一起停掉；
  不退出程序。确认框里用 ↑↓ / y / a / n 选，**ESC 等于拒绝**。
- **ESC**：输入框里有字就清空；空输入框里**双击 ESC**（或 `/rewind`）选一问，对话与它改过的文件一起回到它之前，
  那一问的原话放回输入框，改了重发。Ctrl+L 清屏；空输入框里连按两次 Ctrl+C 退出。
- 要改文件时会显示红绿对比，回答 `y` 这次允许、`a` 本会话一直允许、`n` 拒绝。
- 斜杠命令：`/help` `/clear`（新会话）`/mode`（权限模式）`/provider`（服务商）`/model` `/compact`（压缩历史）
  `/undo`（撤销上一问）`/todo`（任务清单）`/cost`（累计用量与金额）`/exit`。
- **`@路径`**：输入里写 `@src/app.py`，文件内容直接附上。**`!命令`**：自己直接跑命令（不经过模型），
  输出会附在你下一句话里告诉模型。
- **撤销**：`/undo` 把上一问用 write_file / edit_file 改过的文件恢复原样（新建的删掉），对话也退回去。
  **bash 命令造成的改动（删文件、git 操作、构建产物）无法撤销**。
- **花费**：在配置档里填 `price_in` / `price_out`（每百万 token 的价格，按服务商控制台填）和 `currency`，
  `/cost` 就按金额显示。预设不带价格：各家常调价，写死容易显示错的数。
- **读网页**（`web_fetch` 工具）：除 `auto` 模式外都会先问你，网址完整显示——模型可能把代码内容拼进网址带出去。
- 回复按 Markdown 渲染（代码块、列表、表格），边生成边成形。
- **自动重试**：限流（429）、过载（529）、服务端错误、网络中断时自动重试，最多 4 次，界面上会提示。
- **自动压缩**：对话快撑满模型上下文（超过 80%）时，把旧历史总结成摘要继续干。上下文长度在配置档里用
  `context_window` 设（默认 128K，anthropic 200K，ollama 32K）。

## 多开与配置隔离

可以同时开多个 heng-agent，互不干扰：

- **各实例独立**：`--provider`、`/provider`、`/mode`、「本会话一直允许」都只在各自进程的内存里；配置文件只读不写。
- **会话不会互相覆盖**：会话 id 带随机后缀，同一目录同一秒开两个也各写各的。
  `heng-agent --sessions` 列出这个目录的全部会话，`--resume` 接最近的，`--resume <id 或前缀>` 接指定的。
- **同一个文件别让两个实例同时改**：后写的会覆盖先写的（Claude Code 也一样）。多开时让它们处理不同的文件或目录。

配置分三层，后面的覆盖前面的：

| 层 | 位置 | 用途 |
|---|---|---|
| 内置预设 | 程序自带 | deepseek、kimi、glm… |
| 用户配置 | `~/.heng-agent/config.json` | 你的中转站、密钥、默认服务商 |
| 项目配置 | `项目根目录/.heng-agent/config.json` | 这个项目默认用哪个服务商、哪个模型 |

项目配置例子（项目 A 默认用 DeepSeek 的便宜模型，密钥仍在用户配置或环境变量里）：

```json
{"default": "ds-fast", "profiles": {"ds-fast": {"preset": "deepseek", "model": "deepseek-flash"}}}
```

`preset` 也能引用用户配置里定义的配置档（比如带密钥的中转站），项目里只写要改的字段。
**项目配置里不许写 `api_key`**：项目目录可能被提交进仓库，写了会直接报错。

**完全独立的一套配置**（不同账号、不同客户）：用环境变量 `HENG_AGENT_HOME` 或 `--home` 指到另一个目录，
配置、会话、输入历史都分开：

```bash
HENG_AGENT_HOME=~/.heng-agent-客户A heng-agent
```

## 技能、自定义命令、钩子

**技能**：某类任务的做法说明，模型用得上时自己去读。放在 `.heng-agent/skills/<名字>/SKILL.md`（项目）
或 `~/.heng-agent/skills/<名字>/SKILL.md`（所有项目通用），同名时项目的优先：

```markdown
---
name: test-style
description: 本项目写单元测试的规矩（新增或修改测试前必读）
---
- 每个测试方法第一行写中文 docstring，说明测的是什么
- 边界值至少测「刚好等于」和「差一点」
```

系统提示词里只列名字和 description，模型觉得对得上才读全文，所以装再多也不挤占上下文。技能目录里可以放模板、
清单等其他文件，模型按需读。`/skills` 查看已有技能。

**自定义命令**：把常用提示词存成 `.heng-agent/commands/<名字>.md`（或 `~/.heng-agent/commands/`），输入 `/名字 参数`
就展开发给模型。正文里的 `$ARGUMENTS` 换成参数，没写就把参数接在末尾。`-p "/名字 参数"` 同样可用。
内置命令（`/help`、`/undo`……）优先，同名的自定义命令不生效。

**钩子**：工具执行前后自动跑的命令，写在 `config.json`：

```json
{
  "hooks": {
    "pre_tool":  [{"match": "bash", "command": "python scripts/check_cmd.py"}],
    "post_tool": [{"match": "edit_file|write_file", "command": "ruff format \"$HENG_FILE\""}]
  }
}
```

- `match` 按工具名正则匹配；环境变量 `HENG_TOOL`、`HENG_TOOL_INPUT`（参数 JSON）、`HENG_FILE`（改的是哪个文件）。
- `pre_tool` 退出码 **2** 拦下这次调用，输出作为原因告诉模型；`post_tool` 的输出附在结果后面给模型
  （格式化改了什么、检查报了什么错）。
- 另有三个不针对工具的事件（`match` 可省，环境变量 `HENG_PROMPT` 是用户这句话 / 模型最后的回复）：
  - `user_prompt_submit`：消息发给模型前跑。输出作为补充上下文附在消息后面（如当前分支、最近的报错）；
    退出码 **2** 拦下这条消息不发（如防止把密钥发出去）。
  - `stop`：模型准备停下时跑。退出码 **2** 要求模型继续，输出交给模型（如「测试还没过，接着修」），最多连续 3 次。
  - `session_start`：启动时跑，输出附进系统提示词（如注入项目当前状态）。
- **项目里的钩子要你确认信任才会运行**：克隆来的仓库可能带着恶意钩子。交互模式第一次遇到会把命令完整列出来问你；
  钩子内容一改就要重新确认；`-p` 模式下没信任过的项目钩子直接跳过。用户目录里的钩子是你自己写的，直接生效。

## 子智能体、图片、MCP

**子智能体**（`task` 工具）：模型可以把调研 / 探索类的事交给子智能体去做（在大项目里找相关代码、梳理调用关系……），
子智能体独立搜索阅读，只把结论带回来，主对话不会被中间过程挤满。同一轮发出多个会**并行执行**（最多 4 个）。
子智能体**只读**（读文件、找文件、搜内容），不能改文件、跑命令，也不能再派子智能体；要改代码由主智能体来做。

**图片**：输入里 `@截图.png` 就把图片附上（png / jpg / gif / webp，单张 ≤ 5 MB）。**能不能看图取决于模型**：
Claude、GPT 等支持；DeepSeek 等纯文本模型不支持。

**MCP**：接入外部工具服务，在 `config.json` 里配置。两种连接方式：`command` 启动一个程序（stdio），
`url` 连一个已经在跑的服务（http，即 MCP 的 Streamable HTTP）：

```json
{
  "mcp_servers": {
    "sigil": {"command": "sigil-mcp", "args": ["--stdio"], "env": {"FOO": "bar"}, "timeout": 60},
    "ai-workstation": {"url": "https://ai-workstation-mcp.agilefr.com/mcp", "headers": {"Authorization": "Bearer 令牌"}}
  }
}
```

- 也认 `mcpServers` 这个写法（Claude Code、Kimi 用的），它们的配置可以直接贴过来。老式 SSE 传输不支持。
- 启动时每个服务一行：`MCP server "aicoder" connected · 90 tools (stdio)`，连不上的红色显示原因。
- **工具多时按需加载**：接几个大服务很容易上百个工具（实测 6 个常用服务 361 个、声明约 10 万 token），每次请求都带上
  会占掉一半上下文、每问都花这么多钱。所以 MCP 工具声明超过上下文的 10% 时，先不发声明，只给模型一个 `tool_search`：
  它按关键词搜，搜到的工具立即加载、下一步就能调用（实测每次请求从约 10 万 token 降到约 3 千）。
  配置 `settings.mcp_tool_search`：`auto`（默认）/ `on` / `off`。
- 工具注册成 `mcp__服务名__工具名`；`/mcp` 查看已接入的工具。
- 外部工具可能写数据库、发消息，**除 `auto` 模式外调用前都会问你**（`plan` 模式也一样）。
- **项目里配置的 MCP 服务要你确认信任才会启动**，和钩子一样：配置一改（包括换了 url）就要重新确认，`-p` 模式下没信任过的直接跳过。
- 启动失败的服务跳过并提示，不影响其他服务；退出时关掉 stdio 服务的进程、结束 http 服务的会话。

## 项目记忆

在项目根目录放一个 `HENG.md`，写上项目规矩（技术栈、命名约定、提交规范……），每次启动自动读入。
所有项目通用的写在 `~/.heng-agent/HENG.md`。

## 开发

```bash
uv venv --python D:/software/dev/python/python.exe .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
```

设计见 `docs/design/00-总体设计.md`，开发约定见 `CLAUDE.md`。
