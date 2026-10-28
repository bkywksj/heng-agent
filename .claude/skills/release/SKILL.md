---
name: release
description: |
  发布 heng-agent 新版本、把正式版装到本机、装 heng-dev 开发版命令时使用。
  触发场景：发布 / 发版 / 新版本 / 打标签 / tag / 版本号 / CHANGELOG / 更新日志 / 装正式版 / 重装 / uv tool install /
  heng-dev / 全局命令坏了 / 升级
---

# 发布与安装

## 两个版本

| | 正式版 `heng-agent` | 开发版 `heng-dev` |
|---|---|---|
| 代码 | 从标签构建的 wheel，装在 uv 的工具目录（site-packages） | 仓库源码，改完即生效 |
| 数据 | `~/.heng-agent` | `~/.heng-agent-dev` |
| 版本号 | `0.2.0` | `0.2.0-dev（v0.2.0-3-gxxxx-dirty）` |

判断不用配置：代码在仓库 `src/` 下就是开发版（`core/version.py`）。

## 发布步骤

1. **先确认没有别的会话在改**：`git status`、`git log --oneline -5`；
2. **改版本号两处**：`pyproject.toml` 的 `version`、`src/heng_agent/__init__.py` 的 `__version__`（有测试检查两处一致）。
   版本号规则：修 bug 加最后一位（0.2.1），加功能加中间一位（0.3.0）；
3. **写 CHANGELOG.md**：最上面加一节，按「交互 / 扩展 / 兼容 / 安全 / 修复」分类写用户看得见的变化；
4. **检查**：`ruff format`、`ruff check`、全部测试通过；
5. **提交并打标签**：逐个 `git add`，提交信息「发布 x.y.z」；`git tag -a vx.y.z -m "…"`；
6. **推送**：Sigil 的 `git_push`，分支与标签各推一次、两个远程：`origin`（GitHub，凭据 `github1`）与 `gitee`（Gitee 镜像，凭据 `gitee`）。
   **推到 GitHub 的标签会触发自动发布**（`.github/workflows/release.yml`）：三个平台跑完测试 → 核对标签与版本号 →
   构建 → 发布到 PyPI → 建 GitHub Release（说明取自 CHANGELOG.md 这个版本的那一节）；
7. **构建并装成正式版**（在干净的标签副本里构建，别在工作目录——那样会把没提交的改动也装进去）：

   ```bash
   git worktree add --detach ../heng-agent-release vx.y.z
   cd ../heng-agent-release && uv build --wheel
   uv tool install --force dist/heng_agent-x.y.z-py3-none-any.whl
   cd - && git worktree remove --force ../heng-agent-release
   ```

8. **验证**：

   ```bash
   heng-agent --version      # 不带 -dev
   heng-dev --version        # 带 -dev
   ```

   再用 uv 工具目录里的 Python 确认：代码在 site-packages、`prompts/` 里的内置提示词都在（以前漏打包，装好就崩）。

## heng-dev 命令

仓库搬了位置、换了电脑、或 `heng-dev` 报找不到路径时，重新装一次：

```bash
.venv/Scripts/python.exe scripts/install_dev_command.py
```

它写 `~/.local/bin/heng-dev.cmd`（cmd / PowerShell）与 `heng-dev`（Git Bash）。.cmd 开头会切到 UTF-8 再读中文路径、
跑完切回——**不要改成按固定编码写**（GBK 与 UTF-8 终端里总有一种报「找不到路径」）。

## 仓库

| 仓库 | 地址 | 说明 |
|---|---|---|
| 主仓库（公开） | https://github.com/bkywksj/heng-agent | 本地 `E:\my\AI模型\heng-agent` 的 `origin` |
| 镜像 | https://gitee.com/bkywksj/heng-agent | 本地的 `gitee`，给国内用户；每次推送两边都推 |
| 开发历史归档（私有） | gitee.com/bkywksj/heng-agent-archive | 本地 `heng-agent-archive`，0.2.0 之前的完整历史，不再更新 |

公开仓库从「0.2.0 首次开源」一个提交开始，和归档的历史不相关（不能互相推送、合并）。

## 第一次发布到 PyPI

用「受信任发布」，不建也不存 API 令牌。第一次发布前由用户在 PyPI 网站上登记一次：

1. 登录 https://pypi.org → 账号的 **Publishing** → **Add a new pending publisher** → 选 GitHub；
2. 填：PyPI 项目名 `heng-agent`、Owner `bkywksj`、Repository `heng-agent`、Workflow `release.yml`、
   Environment `pypi`；
3. 之后推 `v` 开头的标签就会自动发布；第一次发布成功后，这个「待定」发布者自动转成正式的。

发布失败时看 GitHub Actions 的「发布」工作流：版本号与标签不一致会在「构建」一步的注释里说明。

## 出了问题

| 现象 | 原因与处理 |
|---|---|
| `heng-agent` 报 `ModuleNotFoundError: heng_agent` | 工具环境坏了：**多半是 Windows 上有 heng-agent 开着时重装 / 升级**——它加载的文件被占用，uv 装到一半失败（os error 32），把环境删了一半。关掉所有 heng-agent 窗口，按第 7 步重装 |
| 正式版启动提示「配置里有不认识的设置」 | 开发版或新版写了新字段；只是提示，不影响使用。要清掉就删 `~/.heng-agent/config.json` 里那一项 |
| `heng-agent --version` 带 `-dev` | 装成了可编辑安装或从工作目录装的；按第 7 步重装 |
| 两个窗口分不清哪个是开发版 | 开发版欢迎页是品红边框带「开发版」徽标，状态栏也有徽标 |
