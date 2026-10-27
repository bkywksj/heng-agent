# 更新日志

## 发布步骤

1. 改版本号：`pyproject.toml` 的 `version` 与 `src/heng_agent/__init__.py` 的 `__version__`（测试会检查两处一致）；
2. 在本文件最上面记下这个版本的改动；
3. 跑测试与代码检查都通过后提交，打标签 `v版本号` 并推送；
4. 从标签构建安装包、装成正式版（不要直接从工作目录装——那样连没提交的改动也会装进去）：

   ```bash
   git worktree add ../heng-agent-release v版本号        # 干净的标签副本
   cd ../heng-agent-release && uv build --wheel           # 生成 dist/heng_agent-版本号-py3-none-any.whl
   uv tool install --force dist/heng_agent-版本号-py3-none-any.whl
   cd - && git worktree remove ../heng-agent-release
   ```

5. `heng-agent --version` 应显示新版本号（不带 -dev）。

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
