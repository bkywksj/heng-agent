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
6. **推送**：Sigil 的 `git_push`，分支与标签各推一次、两个远程（`origin` 用 gitee 凭据、`gitcode` 用 gitcode 凭据）；
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

## 出了问题

| 现象 | 原因与处理 |
|---|---|
| `heng-agent` 报 `ModuleNotFoundError: heng_agent` | 工具环境坏了（重装被打断等）。按第 7 步从标签重装 |
| 正式版启动提示「配置里有不认识的设置」 | 开发版或新版写了新字段；只是提示，不影响使用。要清掉就删 `~/.heng-agent/config.json` 里那一项 |
| `heng-agent --version` 带 `-dev` | 装成了可编辑安装或从工作目录装的；按第 7 步重装 |
| 两个窗口分不清哪个是开发版 | 开发版欢迎页是品红边框带「开发版」徽标，状态栏也有徽标 |
