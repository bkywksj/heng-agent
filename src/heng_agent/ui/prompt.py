"""输入框周边：斜杠命令补全、底部状态栏、权限模式列表与配色（输入框本身见 inputbox.py）。

状态栏常驻在输入框下方：权限模式（auto 用醒目的黄底标出——全部不问是危险模式）、服务商与模型、工作目录、上下文用量。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style

#: 内置斜杠命令的一句话说明（补全菜单里显示）
BUILTIN_HELP = {
    "help": "显示命令说明",
    "clear": "开始新会话",
    "mode": "切换权限模式 ask / accept-edits / plan / auto",
    "model": "切换模型名",
    "provider": "切换服务商",
    "compact": "压缩对话历史",
    "undo": "撤销上一问",
    "rewind": "回退到之前的某一问（对话与代码）",
    "resume": "选一个会话接着聊（可带关键词）",
    "rename": "给当前会话起名",
    "permissions": "持久的允许 / 拒绝规则",
    "tasks": "后台任务（/tasks kill 任务号 停止）",
    "think": "思考模式开关（on / off / 预算 / show）",
    "agents": "自定义子智能体角色（new 名字 新建）",
    "worktree": "git 工作树（new / switch / remove）",
    "terminal-setup": "让 Windows Terminal 的 Shift+Enter 能换行",
    "context": "上下文被什么占着",
    "diff": "这次会话改过的文件",
    "copy": "复制上一条回复",
    "export": "把会话导出成 Markdown",
    "memory": "读进来的说明文件（优先级、各占多少），edit 修改",
    "doctor": "检查运行环境（copy：连同日志复制下来，反馈问题用）",
    "todo": "查看任务清单",
    "skills": "查看技能",
    "mcp": "MCP 服务与工具（可带服务名）",
    "config": "查看与修改配置（provider / set / get / edit）",
    "cost": "本会话用量（同 /usage）",
    "usage": "本会话用量：输入输出、缓存命中率、花费、上下文",
    "status": "一屏看全当前状态",
    "stats": "历史统计：今天 / 近 7 天 / 近 30 天",
    "hooks": "生效的钩子",
    "exit": "退出",
}
MODES = ["ask", "accept-edits", "plan", "auto"]
MODE_LABEL = {
    "ask": "ask 每次问",
    "accept-edits": "accept-edits 改文件不问",
    "plan": "plan 先出计划",
    "auto": "auto 全部不问",
}

STYLE = Style.from_dict(
    {
        "bottom-toolbar": "noreverse bg:#1f2430 #c8ccd4",
        "frame-title": "nobold #5c6370",  # 边框上的按键提示：灰、不加粗，不抢输入的视线
        "flash": "#98c379",
        "todos": "#7f848e",
        "bar.mode": "bold #61afef",
        "bar.mode-auto": "bold bg:#e5c07b #1f2430",
        "bar.mode-plan": "bold #98c379",
        "bar.model": "#c678dd",
        "bar.dim": "#7f848e",
        "bar.ctx": "#56b6c2",
        "bar.ctx-hot": "bold #e06c75",
        "bar.background": "bold #e5c07b",
        "bar.thinking": "bold #c678dd",
        "bar.mcp": "#98c379",
        "bar.mcp-wait": "#e5c07b",
        "bar.mcp-fail": "bold #e06c75",
        # 补全菜单：深色底、选中项用强调色整行高亮、说明文字灰色，滚动条与底色协调
        "completion-menu": "bg:#21252b",
        "completion-menu.completion": "bg:#21252b #abb2bf",
        "completion-menu.completion.current": "bold bg:#3e4451 #61afef",
        "completion-menu.meta.completion": "bg:#21252b #5c6370",
        "completion-menu.meta.completion.current": "bg:#3e4451 #abb2bf",
        "completion-menu.multi-column-meta": "bg:#21252b #5c6370",
        "scrollbar.background": "bg:#2c313a",
        "scrollbar.button": "bg:#5c6370",
    }
)


#: 不带参数的命令：补全后按 Enter 直接执行，Tab 也不加空格
NO_ARGS = frozenset({"help", "clear", "undo", "todo", "cost", "skills", "rewind", "copy", "context", "diff",
                     "doctor", "exit", "quit", "terminal-setup", "usage", "status", "stats", "hooks", "todos",
                     "version", "bashes"})  # fmt: skip
#: 必须带参数的命令：Enter 只补全（加空格等你输参数），不执行
REQUIRES_ARGS = frozenset({"rename"})
#: 参数提示：(命令, 前面已输入的参数) → [(候选, 说明, 选它之后还要不要再输)]
ArgHints = Callable[[str, list[str]], list[tuple[str, str, bool]]]


class CmdCompletion(Completion):
    """命令或命令参数的补全项，带上「选它之后还要不要再输」：Enter 据此决定直接执行还是等你补参数。"""

    def __init__(
        self, text: str, start_position: int, display_meta: str, *, takes_more: bool, needs_more: bool
    ) -> None:
        super().__init__(text, start_position=start_position, display_meta=display_meta)
        self.takes_more = takes_more  # 后面还能跟参数（Tab 补全后自动加空格、接着提示）
        self.needs_more = needs_more  # 后面必须跟参数（Enter 不执行，补全后等你输）


class SlashCompleter(Completer):
    """行首的 / 命令补全：先补命令名（内置 + 自定义），命令后面再补它的参数（子命令、模式名、服务商……）。"""

    def __init__(self, custom: Callable[[], dict[str, str]], args: ArgHints | None = None) -> None:
        self.custom = custom  # 名字 → 说明；每次现取，/clear 之后新增的命令也能补全
        self.args = args

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or "\n" in text:
            return
        if " " not in text:
            word = text[1:]
            entries = {**self.custom(), **BUILTIN_HELP}  # 内置优先（同名的自定义命令不生效）
            for name, meta in sorted(entries.items()):
                if name.startswith(word):
                    yield CmdCompletion("/" + name, -len(text), meta,
                                        takes_more=name not in NO_ARGS, needs_more=name in REQUIRES_ARGS)  # fmt: skip
            return
        if self.args is None:
            return
        cmd, _, rest = text[1:].partition(" ")
        *prior, partial = rest.split(" ")
        for value, meta, more in self.args(cmd, [p for p in prior if p]):
            if value.startswith(partial):
                yield CmdCompletion(value, -len(partial), meta, takes_more=more, needs_more=more)


def human_tokens(n: int) -> str:
    """token 数按 k / M 显示：950 → 950、1741 → 1.7k、200000 → 200k、1000000 → 1M。"""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        value, unit = n / 1000, "k"
    else:
        value, unit = n / 1_000_000, "M"
    text = f"{value:.1f}".rstrip("0").rstrip(".") if value < 100 else f"{value:.0f}"
    return text + unit


def toolbar(mode: str, model: str, workdir: str, used: int, window: int, background: int = 0, *,
            thinking: bool = False, compact_ratio: float = 0.8,
            mcp: tuple[str, str] | None = None) -> FormattedText:  # fmt: skip
    pct = used / window * 100 if window else 0.0
    threshold = compact_ratio * 100
    mode_style = {"auto": "class:bar.mode-auto", "plan": "class:bar.mode-plan"}.get(mode, "class:bar.mode")
    ctx_style = "class:bar.ctx-hot" if pct >= threshold - 10 else "class:bar.ctx"
    short_dir = workdir if len(workdir) <= 48 else "…" + workdir[-47:]
    parts = [
        (mode_style, f" {MODE_LABEL.get(mode, mode)} "),
        ("", "  "),
        ("class:bar.model", model),
    ]
    if thinking:
        parts.append(("class:bar.thinking", "  ✻ 思考"))
    if mcp:  # MCP 连接情况：(样式, 文字)，见 mcp_view.toolbar_part
        parts.append(mcp)
    parts += [
        ("class:bar.dim", f"  {short_dir}  "),
        (ctx_style, f"上下文 {pct:.0f}%（{human_tokens(used)}/{human_tokens(window)}）"),
    ]
    if window and threshold - 20 <= pct < threshold:  # 快到了：提前说一声，免得在下一问里意外压缩
        parts.append((ctx_style, f" · 距自动压缩 {threshold - pct:.0f}%"))
    elif window and pct >= threshold:
        parts.append((ctx_style, " · 下一问前自动压缩"))
    if background:  # 还有后台任务在跑：显眼地标出来，免得忘了
        parts.append(("class:bar.background", f"  后台 {background}"))
    parts.append(("class:bar.dim", "   Shift+Tab 切换模式"))
    return FormattedText(parts)
