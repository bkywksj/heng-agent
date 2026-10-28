"""输入框周边：斜杠命令补全、底部状态栏、权限模式列表与配色（输入框本身见 inputbox.py）。

状态栏常驻在输入框下方：权限模式（auto 用醒目的黄底标出——全部不问是危险模式）、服务商与模型、工作目录、上下文用量。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style
from rich.cells import cell_len

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
        "bottom-toolbar": "noreverse #c8ccd4",  # 不加底色：两行状态栏带底色在深色终端里太突兀（用户反馈）
        "frame-title": "nobold #5c6370",  # 边框上的按键提示：灰、不加粗，不抢输入的视线
        "flash": "#98c379",
        "todos": "#7f848e",
        "bar.mode": "bold #61afef",
        "bar.mode-edits": "bold #e5c07b",
        "bar.mode-auto": "bold #e06c75",
        "bar.mode-plan": "bold #98c379",
        "bar.project": "bold #e5c07b",
        "bar.branch": "#98c379",
        "bar.sep": "#4b5263",
        "bar.ctx-warn": "#e5c07b",
        "bar.model": "#c678dd",
        "bar.dim": "#7f848e",
        "bar.ctx": "#98c379",
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


#: 第二行权限模式前的图标：箭头越多放得越开，plan 是暂停号（先出计划、不动手）。
#: 都用单宽字符：emoji 在有的终端里宽度不一，对不齐
MODE_ICON = {"ask": "▸", "accept-edits": "▸▸", "plan": "‖", "auto": "▸▸▸"}
MODE_STYLE = {"ask": "class:bar.mode", "accept-edits": "class:bar.mode-edits", "plan": "class:bar.mode-plan",
              "auto": "class:bar.mode-auto"}  # fmt: skip
CTX_CELLS = 8  # 上下文进度条的格数
SEP = ("class:bar.sep", " │ ")


def context_bar(used: int, window: int, threshold: float) -> tuple[str, str]:
    """(样式, 进度条)：一半以下绿，过半黄，离自动压缩不到 10% 红。"""
    pct = used / window * 100 if window else 0.0
    filled = min(CTX_CELLS, round(pct / 100 * CTX_CELLS))
    if pct >= threshold - 10:
        style = "class:bar.ctx-hot"
    elif pct >= 50:
        style = "class:bar.ctx-warn"
    else:
        style = "class:bar.ctx"
    return style, "█" * filled + "░" * (CTX_CELLS - filled)


def toolbar(mode: str, model: str, workdir: str, used: int, window: int, background: int = 0, *,
            thinking: bool = False, compact_ratio: float = 0.8, mcp: tuple[str, str] | None = None,
            branch: str | None = None, dirty: bool = False, speed: float | None = None, width: int | None = None,
            custom: list[tuple[str, str]] | None = None) -> FormattedText:  # fmt: skip
    """输入框下面的两行状态栏。

    第一行是信息：项目名、git 分支、模型、MCP、上一次的输出速度、上下文进度条；第二行是权限模式与提醒。
    权限模式决定改文件、跑命令要不要问，单独一行最醒目（以前全挤在一行，窄窗口里后半截直接被截掉）。
    width：终端宽度。第一行放不下时按重要程度依次去掉：速度 → 分支 → 服务商名 → token 数 → 进度条，不截断。
    custom：用户的状态栏命令（status_line 设置）的输出，有的话替换第一行。
    """
    pct = used / window * 100 if window else 0.0
    threshold = compact_ratio * 100
    line2 = mode_line(mode, background=background, pct=pct, threshold=threshold, window=window, width=width)
    if custom is not None and mcp:  # 第一行换成了用户的输出：MCP 连接情况挪到第二行，不然连接中、连不上都看不到
        line2.append((mcp[0], " · " + mcp[1].strip()))
    line1 = custom if custom is not None else info_line(
        model, workdir, used, window, thinking=thinking, threshold=threshold, mcp=mcp, branch=branch, dirty=dirty,
        speed=speed, width=width,
    )  # fmt: skip
    return FormattedText([*line1, ("", "\n"), *line2])


def info_line(model: str, workdir: str, used: int, window: int, *, thinking: bool, threshold: float,
              mcp: tuple[str, str] | None, branch: str | None, dirty: bool, speed: float | None,
              width: int | None) -> list[tuple[str, str]]:  # fmt: skip
    pct = used / window * 100 if window else 0.0
    name = Path(workdir).name or workdir  # 完整路径欢迎页里有；这里只放项目名，省出地方
    ctx_style, bar = context_bar(used, window, threshold)
    short_model = model.rsplit(" · ", 1)[-1]  # 「yongye · claude-sonnet-5」窄了只留模型名
    # 依次去掉的东西：每一步在前一步的基础上再少一样
    steps = [set(), {"speed"}, {"speed", "branch"}, {"speed", "branch", "provider"},
             {"speed", "branch", "provider", "tokens"}, {"speed", "branch", "provider", "tokens", "bar"}]  # fmt: skip
    parts: list[tuple[str, str]] = []
    for drop in steps:
        parts = [("class:bar.project", f" ▣ {name}")]
        if branch and "branch" not in drop:
            parts.append(("class:bar.branch", f"  {branch}{'*' if dirty else ''}"))
        parts += [SEP, ("class:bar.model", short_model if "provider" in drop else model)]
        if thinking:
            parts.append(("class:bar.thinking", " ✻思考"))
        if mcp:  # MCP 连接情况：(样式, 文字)，见 mcp_view.toolbar_part
            parts += [SEP, (mcp[0], mcp[1].strip())]
        if speed and "speed" not in drop:
            parts += [SEP, ("class:bar.dim", f"{speed:.0f}t/s")]
        ctx = "上下文" if "bar" in drop else f"上下文 {bar}"
        ctx += f" {pct:.0f}%" + ("" if "tokens" in drop else f" {human_tokens(used)}/{human_tokens(window)}")
        parts += [SEP, (ctx_style, ctx)]
        if width is None or cell_len("".join(t for _, t in parts)) < width:
            break
    return parts


def mode_line(mode: str, *, background: int, pct: float, threshold: float, window: int,
              width: int | None) -> list[tuple[str, str]]:  # fmt: skip
    icon = MODE_ICON.get(mode, "▸")
    parts: list[tuple[str, str]] = [(MODE_STYLE.get(mode, "class:bar.mode"), f" {icon} {MODE_LABEL.get(mode, mode)}")]
    hint = ("class:bar.dim", "  (Shift+Tab 切换)")
    extras: list[tuple[str, str]] = []
    if background:  # 还有后台任务在跑：显眼地标出来，免得忘了
        extras.append(("class:bar.background", f" · 后台任务 {background}"))
    ctx_style = "class:bar.ctx-hot" if pct >= threshold - 10 else "class:bar.ctx-warn"
    if window and threshold - 20 <= pct < threshold:  # 快到了：提前说一声，免得在下一问里意外压缩
        extras.append((ctx_style, f" · 距自动压缩 {threshold - pct:.0f}%"))
    elif window and pct >= threshold:
        extras.append((ctx_style, " · 下一问前自动压缩"))
    line = [*parts, hint, *extras]
    if width is not None and cell_len("".join(t for _, t in line)) >= width:
        line = [*parts, ("class:bar.dim", "  (Shift+Tab)"), *extras]  # 窄了提示缩短，提醒照留
    return line
