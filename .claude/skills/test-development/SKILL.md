---
name: test-development
description: |
  heng-agent 的测试怎么写、怎么在真实环境里验证。写单元测试、用假模型跑智能体循环、真实控制台验证键盘与终端功能、
  真实模型与真实配置实测时使用。
  触发场景：写测试 / 加测试 / 测试失败 / pytest / FakeModel / 假模型 / 实测 / 真实环境验证 / 真实控制台 /
  真实模型 / 回归测试 / 测试隔离 / conftest
  （与 feature-checklist 区别：那个管「改哪里」，本技能管「怎么证明改对了」）
---

# 测试与实测

## 跑测试

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe format src tests && .venv/Scripts/ruff.exe check src tests
```

## 单元测试的几个约定

**假模型**：`tests/fake_model.py` 的 `FakeModel(script)`，script 是「每轮回复什么」的列表：
字符串 = 说一段话，`("tool", 名字, 参数)` = 调工具，`("think", 内容)` = 一段带签名的思考。
`model.calls` 记着每次收到的 messages，可以断言「工具结果有没有喂回给模型」。不花 API 费用。

**隔离**：`tests/conftest.py` 把 `HENG_AGENT_HOME` 与 `CLAUDE_CONFIG_DIR` 指到临时目录。
测试从源码跑、会被当成开发版，不隔离就会写进真实的 `~/.heng-agent-dev`、读到本机真实的 `~/.claude`。
要测「家目录下的默认数据目录」时，`monkeypatch` 掉 `Path.home`、删掉 `HENG_AGENT_HOME`、设 `HENG_AGENT_CHANNEL=release`。

**界面输出**：`Console(record=True, width=…, force_terminal=False)`，然后 **`export_text()` 只调一次存进变量**
——它默认清空已记录的内容，第二次调用拿到空串（踩过两次）。长路径会折行，断言别依赖「第几行」。

**中文对齐**：比较屏幕上的列用 `rich.cells.cell_len`，不用 `len` / `str.index`（一个汉字占两格）。

**选择器**：测试里 `monkeypatch.setattr(repl, "_choose", lambda title, options: "值")`。

## 必须在真实环境里验证的

管道喂按键的单元测试**走不到** Windows 控制台那条路——Shift+Enter 补丁因导入路径写错静默失效了很久，测试一直是绿的。
下面这类改动，单元测试之外还要实测：

| 改动 | 怎么实测 |
|---|---|
| 键盘、输入框、Windows 控制台 | 写临时脚本在**新的控制台窗口**里跑：用 `WriteConsoleInputW` 写入原始按键事件（带 Shift 等修饰键状态），结果写文件再读。注意它绕过了 Windows Terminal 的翻译，最后一步仍要用户亲手按 |
| 批处理、编码、代码页 | 在 GBK（936）与 UTF-8（65001）两种代码页下都跑：`cmd /c "chcp 936 >nul & 命令"` |
| 兼容 Claude Code 的配置 | 读本机真实的 `~/.claude`（只读），看兼容加载的数量对不对；有链接（junction）的目录要解析前后都认 |
| Claude 钩子 | 用真实的钩子脚本跑，**用 plan 模式**：钩子在权限检查之前跑，就算钩子没拦住，plan 也不让命令真执行 |
| 真实模型的协议细节 | 小请求、少轮数；看会话文件（开发版在 `~/.heng-agent-dev/sessions`）里每轮实际存了什么 |
| 打包发布 | 看 wheel 里有没有 `prompts/*.md` 等数据文件；装好后 `--version`、从安装位置 import 看是不是 site-packages |

## 实测脚本的规矩

- 写成仓库根目录（或 `tests/` 下，要用 `fake_model` 时）的 `scratch_*.py`，**跑完就删**，不进测试集、不提交；
- **不要对付费模型发故意超长、超大的请求**「试试会不会报错」：百万 token 级的模型会真处理、真收钱（踩过）；
- 危险命令只交给权限判断或钩子判断，**不真执行**；命令文字里别出现「按进程名结束进程」的字面写法（智码守卫会拦），
  要用就拼接字符串；
- 只读用户的真实配置，不改；要改的话在开发版的数据目录里改。

## 跨平台（CI 在 Windows / macOS / Linux 上都跑）

本地只有 Windows，别的平台的问题靠 CI 发现。**CI 不在推送时自动跑**（省 Actions 配额）：改动大、发版前要先验证时，
用 Sigil 的 `github_workflow_dispatch` 手动触发 `ci.yml`（推 v 标签发布时 release.yml 也会跑同一套测试）。
CI 失败时每个失败的测试会写成 GitHub 注释，公开仓库不用凭据就能读：
`https://api.github.com/repos/bkywksj/heng-agent/check-runs/<任务 id>/annotations`（任务 id 从
`/actions/runs/<运行 id>/jobs` 取；完整日志要管理员权限才能下载）。每个步骤最多显示 10 条错误注释。

写测试时避开只在 Windows 上成立的写法（详见 CLAUDE.md「绝对禁止」）：模拟输入的管道上下文要留住；
测「选择器用不了」这类路径要明确指定、别依赖环境；盘符路径、Git Bash 行为的用例只在 Windows 上跑。

## 测试失败时

- 先看是测试写错了还是代码错了：断言的文字被格式化折行、`export_text` 调了两次、中文宽度——这些是测试的问题；
- 行为是有意改的（如「不认识的设置」从报错改成提示），就改测试的期望，并在提交信息里写明为什么改；
- 别为了让测试变绿去删断言。
