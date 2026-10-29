"""自定义斜杠命令：把常用提示词存成 .md 文件，输入 /名字 就展开成一句话发给模型。

位置（优先级从低到高，后读的覆盖同名）：内置 → ~/.claude/commands → ~/.heng-agent/commands →
项目 .claude/commands → 项目 .heng-agent/commands。Claude Code 的命令可以放在子目录里分组，命令名取文件名。

正文的写法（与 Claude Code 一致）：
- `$ARGUMENTS`：命令后面跟的全部文字；`$1`…`$9`：按空格分开的第几个（没有就留空）。
  两种都没写时，后面的文字接在末尾。
- ``!`命令` ``：发送前先在本机跑这条命令，把输出拼进去（如 ``!`git status` ``）。用户级的命令是自己写的，直接跑；
  项目里的跟着仓库来，由界面先问用户（脚本模式只在 auto 时跑）——等于仓库里的文件能让你的电脑跑命令。
- `@文件`：展开后和用户输入一样处理，文件内容会附上。
frontmatter 的 description 显示在 /help 里，argument-hint 在补全时提示该填什么；allowed-tools、model 忽略。
内置命令（/help、/undo……）优先，同名的自定义命令不会生效。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from heng_agent.core.compat import claude_home
from heng_agent.tools.shell import clip, run_command, shell_argv
from heng_agent.tools.skill import parse_frontmatter

#: ``!`命令` ``：发送前先跑的 shell 命令
SHELL_SNIPPET = re.compile(r"!`([^`\n]+)`")
_POSITIONAL = re.compile(r"\$([1-9])")


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    body: str
    path: Path
    hint: str = ""  # argument-hint：补全时提示参数怎么填
    project: bool = False  # 跟着仓库来的（项目级）：里面的 shell 命令要先问用户

    @property
    def shell_snippets(self) -> list[str]:
        return SHELL_SNIPPET.findall(self.body)

    def expand(self, arguments: str, run_shell: Callable[[str], str] | None = None) -> str:
        """展开成发给模型的话。先填参数再跑 shell：命令里可以用参数（``!`git show $1` ``），
        命令输出里碰巧有 $1 之类的也不会被误换。run_shell 为 None 时不跑，原处注明「没有运行」。"""
        positional = arguments.split()
        body = self.body
        used = "$ARGUMENTS" in body or bool(_POSITIONAL.search(body))
        body = body.replace("$ARGUMENTS", arguments)
        body = _POSITIONAL.sub(lambda m: positional[int(m.group(1)) - 1] if int(m.group(1)) <= len(positional) else "",
                               body)  # fmt: skip

        def substitute(match: re.Match[str]) -> str:
            command = match.group(1)
            if run_shell is None:
                return f"（没有运行：{command}）"
            fence = "```"
            return f"\n{fence}\n$ {command}\n{run_shell(command).rstrip()}\n{fence}\n"

        body = SHELL_SNIPPET.sub(substitute, body)
        if used or not arguments:
            return body.rstrip()
        return f"{body}\n\n{arguments}".rstrip()


_BUILTIN = Path("<内置>")


def _builtin(name: str, description: str) -> Command:
    """内置提示词放在包里的 prompts/<名字>.md（比写在代码里好读好改）；装好之后从包里读。"""
    body = (resources.files("heng_agent") / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return Command(name, description, body.strip(), _BUILTIN)


#: 内置的提示词命令：用户或项目里同名的 .md 会覆盖它们（可以按自己团队的规矩重写）
BUILTIN_PROMPTS = {
    "init": _builtin("init", "分析项目，生成或更新项目说明（默认 AGENTS.md，已有说明就更新现有的）"),
    "commit": _builtin("commit", "按仓库风格写提交信息并提交（不 push）"),
    "review": _builtin("review", "审查没提交的改动（只评审不修改）"),
}


def discover_commands(home: Path, workdir: Path, *, claude_user: bool = True) -> dict[str, Command]:
    commands: dict[str, Command] = dict(BUILTIN_PROMPTS)  # 内置提示词命令在最底层，用户、项目同名的覆盖它们
    # (目录, 是不是 Claude Code 的（可以分子目录）, 是不是项目级)；后读的覆盖同名
    roots = [(claude_home() / "commands", True, False)] if claude_user else []
    roots += [(home / "commands", False, False), (workdir / ".claude" / "commands", True, True),
              (workdir / ".heng-agent" / "commands", False, True)]  # fmt: skip
    for root, nested, project in roots:
        if not root.is_dir():
            continue
        for md in sorted(root.rglob("*.md") if nested else root.glob("*.md")):
            meta, body = parse_frontmatter(md.read_text(encoding="utf-8-sig", errors="replace"))
            first = next((line.strip("# ").strip() for line in body.splitlines() if line.strip()), "")
            commands[md.stem] = Command(md.stem, meta.get("description") or first[:60], body.strip(), md,
                                        hint=meta.get("argument-hint", ""), project=project)  # fmt: skip
    return commands


def expand_command(
    text: str, commands: dict[str, Command], run_shell: Callable[[str], str] | None = None
) -> str | None:
    """`/名字 参数` 是自定义命令就返回展开后的文字，否则返回 None。run_shell：怎么跑 ``!`命令` ``（None = 不跑）。"""
    if not text.startswith("/"):
        return None
    name, _, arguments = text[1:].partition(" ")
    command = commands.get(name)
    return command.expand(arguments.strip(), run_shell) if command else None


def shell_runner(workdir: Path, timeout: float) -> Callable[[str], str]:
    """跑命令里 ``!`命令` `` 的函数：在工作目录跑，返回输出（太长截断；失败或超时注明）。界面与脚本模式共用。"""

    def run(snippet: str) -> str:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        code, output = run_command(shell_argv(snippet), workdir, timeout, env)
        status = "（超时）" if code is None else (f"（退出码 {code}）" if code else "")
        return clip(output, 10_000).rstrip() + status

    return run
