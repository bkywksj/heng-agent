"""上下文：系统提示词 = 基本守则 + 运行环境 + 说明文件（HENG.md、AGENTS.md、CLAUDE.md……，见 core.compat）+ 技能清单。"""

from __future__ import annotations

import base64
import datetime as _dt
import platform
import re
from pathlib import Path
from typing import Any

from heng_agent.core.compat import MEMORY_PRIORITY, NESTED_MEMORY_NAMES, claude_home, project_memory_file
from heng_agent.core.config import Settings
from heng_agent.core.errors import AgentError
from heng_agent.tools.paths import display, resolve_in_workdir
from heng_agent.tools.shell import shell_argv
from heng_agent.tools.skill import Skill

MEMORY_FILE = "HENG.md"
MAX_MEMORY_CHARS = 20_000  # 记忆文件太长会挤占上下文：超出部分截掉并提示
MEMORY_WARN_TOKENS = 10_000  # 说明文件合计超过这么多 token：/memory、/doctor 提醒精简（每次对话都带着）

BASE_PROMPT = """你是 heng-agent，一个在用户电脑上工作的编码智能体。用中文和用户交流。

工作方式：
- 先弄清现状再动手：用 glob / grep / read_file 找到并读懂相关代码，不要凭猜测修改。
- 改文件优先用 edit_file 做精确替换；只有新建文件或需要整体重写时才用 write_file。
- bash 命令本来就在工作目录里执行，不用先 cd；文件路径尽量写相对工作目录的路径。
- 三步以上的任务先用 todo 工具列出步骤，做一项勾一项。
- 改完要验证：能跑测试就跑测试，能编译就编译，把结果如实告诉用户。没验证过的不要说「已修好」。
- 一次只做用户要求的事，不顺手改无关代码；拿不准需求时先问。
- 用户拒绝了某个操作，就说明情况并换一种做法，不要原样重试。
- 回答简洁：说清做了什么、结果如何、还剩什么。"""


#: CLAUDE.md 的 @路径 引用：前面不能紧贴字母数字（邮箱 a@b.com 不算）、不能在反引号里
IMPORT = re.compile(r"(?<![\w`@])@((?:~|\.{1,2})?[/\\]?[\w\-.\u4e00-\u9fff][\w\-./\\\u4e00-\u9fff]*)")
MAX_IMPORT_DEPTH = 5  # 和 Claude Code 一样最多嵌套 5 层


def expand_imports(text: str, base: Path, seen: set[Path], depth: int = 0) -> str:
    """展开 @路径 引用（Claude Code 的 CLAUDE.md 语法）：文件存在才展开，内容接在后面。

    代码块里的不动；同一个文件只展开一次（seen），互相引用也不会死循环；最多 5 层。
    """
    if depth >= MAX_IMPORT_DEPTH:
        return text
    imported: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in IMPORT.finditer(line):
            raw = match.group(1).rstrip(".,;:，。；：)）")
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = base / target
            try:
                real = target.resolve()
            except OSError:
                continue
            if real in seen or not real.is_file():
                continue
            seen.add(real)
            body = real.read_text(encoding="utf-8-sig", errors="replace").strip()
            body = expand_imports(body, real.parent, seen, depth + 1)
            imported.append(f"\n\n（以下引用自 {raw}）\n{body}")
    return text + "".join(imported)


def memory_sources(settings: Settings) -> list[tuple[Path, str]]:
    """常驻的说明文件，按优先级从低到高排（系统提示词里也是这个顺序，冲突时以后面的为准）：
    用户级在前、项目级在后；同一级里 CLAUDE.md < AGENTS.md < CLAUDE.local.md < HENG.md（见 core.compat）。"""
    sources: list[tuple[Path, str]] = []
    if settings.claude_user_config:
        sources.append((claude_home() / "CLAUDE.md", "用户记忆（Claude Code）"))
    sources.append((settings.home / MEMORY_FILE, "用户记忆"))
    return sources + [(settings.workdir / name, f"项目说明 {name}") for name in MEMORY_PRIORITY]


def load_memories(settings: Settings) -> list[tuple[Path, str]]:
    """读出全部说明文件，返回 [(路径, 写进系统提示词的一段)]。

    去重：指向同一个文件的（常见的做法是把 CLAUDE.md 链到 AGENTS.md）、内容一样的只算一份，免得占两份上下文。
    """
    loaded: list[tuple[Path, str]] = []
    seen_files: set[Path] = set()
    seen_text: set[str] = set()
    for path, label in memory_sources(settings):
        if not path.is_file():
            continue
        real = path.resolve()
        text = real.read_text(encoding="utf-8-sig", errors="replace").strip()
        if real in seen_files or not text or text in seen_text:
            continue
        seen_files.add(real)
        seen_text.add(text)
        text = expand_imports(text, real.parent, seen_files)
        if len(text) > MAX_MEMORY_CHARS:
            text = text[:MAX_MEMORY_CHARS] + f"\n…（{label}超过 {MAX_MEMORY_CHARS} 字，后面截掉了）"
        loaded.append((path, f"# {label}（{path}）\n\n{text}"))
    return loaded


def build_system_prompt(settings: Settings, skills: dict[str, Skill] | None = None) -> str:
    env = (
        f"# 运行环境\n\n- 工作目录：{settings.workdir}\n- 系统：{platform.system()} {platform.release()}\n"
        f"- 命令行：{Path(shell_argv('')[0]).name}\n- 今天：{_dt.date.today().isoformat()}\n"
        f"- 权限模式：{settings.permission_mode}"
    )
    parts = [BASE_PROMPT, env]
    memories = load_memories(settings)
    preamble = memory_preamble([path for path, _ in memories])
    if preamble:
        parts.append(preamble)
    parts += [block for _, block in memories]
    if skills:
        listing = "\n".join(f"- {s.name}：{skill_summary(s.description)}" for s in skills.values())
        parts.append(f"# 可用技能\n\n任务和下面某个技能对得上时，先用 skill 工具读它的完整说明再动手：\n{listing}")
    return "\n\n".join(parts)


SKILL_SUMMARY_CHARS = 250


def skill_summary(description: str) -> str:
    """技能清单里的一行：多行描述压成一行、太长截断。清单只用来让模型挑技能，真用时 skill 工具会读完整说明；
    多行原样放进来会把清单格式搅乱，几十个长描述（如 ruoyi-plus-uniapp 的 60 个技能）也白占每次对话的上下文。"""
    text = " ".join(description.split())
    return text if len(text) <= SKILL_SUMMARY_CHARS else text[: SKILL_SUMMARY_CHARS - 1] + "…"


MAX_MENTION_BYTES = 200_000
_MENTION = re.compile(r"(?<!\S)@(\S+)")


def estimate_tokens(text: str) -> int:
    """粗估 token 数（给用户看大小用）：汉字等大约一字一个，其余大约四个字符一个。"""
    wide = sum(1 for ch in text if ord(ch) > 0x2E80)
    return wide + (len(text) - wide) // 4


MEMORY_ORDER_NOTE = (
    "下面几份说明按优先级从低到高排列：用户级在前、项目级在后；同一级里 CLAUDE.md < AGENTS.md < "
    "CLAUDE.local.md（个人本地的补充）< HENG.md（专门写给你的）。内容冲突时以后面的为准。"
    "读到某个子目录的文件时会附上那里的说明，它比根目录的更具体，在那个目录里以它为准。"
)
TOOL_GLOSSARY = (
    "其中有些说明是写给 Claude Code 等其他编码工具的，提到的工具对应如下：Bash / PowerShell → bash，"
    "Read → read_file，Edit / MultiEdit → edit_file，Write → write_file，Glob → glob，Grep → grep，"
    "WebFetch → web_fetch，Task / Agent → task，TodoWrite → todo。提到那些工具特有的功能（它们的斜杠命令、"
    "设置项、界面操作）时用等价的办法做；没有等价的就忽略，不要因此停下来问。"
)


def memory_preamble(paths: list[Path]) -> str:
    """说明文件前面的一段。有两份以上才写优先级（多份说法冲突时以谁为准）；读到了写给别的工具的说明
    （CLAUDE.md、AGENTS.md）才加工具名对照——只有一份 HENG.md 时这些都用不上，不白占上下文。"""
    notes = [MEMORY_ORDER_NOTE] if len(paths) > 1 else []
    if any(p.name != MEMORY_FILE for p in paths):
        notes.append(TOOL_GLOSSARY)
    return "# 项目说明与记忆\n\n" + "\n".join(notes) if notes else ""


def memory_files(settings: Settings) -> dict[str, Path]:
    """往里写的两份记忆（# 快速记忆、/memory edit）：项目的写进现有的说明文件（AGENTS.md / HENG.md / CLAUDE.md），
    都没有就新建 AGENTS.md；用户的写 ~/.heng-agent/HENG.md。"""
    return {"project": project_memory_file(settings.workdir), "user": settings.home / MEMORY_FILE}


def append_memory(path: Path, text: str) -> None:
    """`#` 快速记忆：在记忆文件末尾加一条；文件不存在就新建（带个标题）。"""
    line = text.strip().lstrip("#").strip()
    if not line:
        raise AgentError("要记的内容是空的")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else "# 记忆\n"
    sep = "" if existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{sep}- {line}\n", encoding="utf-8")


def nested_memory(file_path: Path, workdir: Path, seen: set[str]) -> list[tuple[Path, str]]:
    """子目录的 HENG.md：模型第一次碰到某目录下的文件时，把从工作目录到这个文件之间各级目录的 HENG.md 附上。

    工作目录本身那份已在系统提示词里；每份只附一次（seen 记着）；工作目录以外的不管。
    """
    root = workdir.resolve()
    try:
        target = file_path.resolve()
    except OSError:
        return []
    if root not in target.parents:
        return []
    found = []
    folder = target.parent if not target.is_dir() else target
    chain = []
    while folder != root and root in folder.parents:
        chain.append(folder)
        folder = folder.parent
    for directory in reversed(chain):  # 外层目录的约定在前
        for name in NESTED_MEMORY_NAMES:  # 子目录里的 HENG.md / AGENTS.md / CLAUDE.md 都认
            memory = directory / name
            key = str(memory)
            if key in seen or not memory.is_file():
                continue
            seen.add(key)
            text = memory.read_text(encoding="utf-8-sig", errors="replace").strip()
            if not text or f"内容:{hash(text)}" in seen:  # CLAUDE.md 链到 AGENTS.md 之类：内容一样只附一份
                continue
            seen.add(f"内容:{hash(text)}")
            found.append((memory, text[:MAX_MEMORY_CHARS]))
    return found


def expand_mentions(text: str, workdir: Path) -> tuple[str, list[str]]:
    """把输入里的 @路径 替换成文件内容附在末尾，省一次 read_file。返回 (展开后的文字, 附上的文件)。

    路径同样要在工作目录内；不存在或不是文件的 @xxx 原样保留（可能只是在提某个人或邮箱）。
    """
    attached: list[str] = []
    blocks: list[str] = []
    for raw in dict.fromkeys(_MENTION.findall(text)):  # 去重但保持顺序
        try:
            path = resolve_in_workdir(raw.rstrip("，。；,.;:：)）"), workdir)
        except AgentError:
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            continue  # 二进制文件不附
        body = data[:MAX_MENTION_BYTES].decode("utf-8-sig", errors="replace")
        if len(data) > MAX_MENTION_BYTES:
            body += f"\n…（文件共 {len(data)} 字节，只附上前 {MAX_MENTION_BYTES} 字节）"
        rel = display(path, workdir)
        attached.append(rel)
        blocks.append(f'<file path="{rel}">\n{body}\n</file>')
    if not blocks:
        return text, []
    return text + "\n\n用户附上的文件：\n" + "\n".join(blocks), attached


IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"
}  # fmt: skip
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def mention_images(text: str, workdir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """输入里 @ 到的图片（png/jpg/gif/webp，单张 ≤ 5 MB）变成 Anthropic 图片块。返回 (图片块, 附上的文件)。

    能不能看图取决于模型：DeepSeek 等纯文本模型会拒收或忽略。
    """
    images: list[dict[str, Any]] = []
    attached: list[str] = []
    for raw in dict.fromkeys(_MENTION.findall(text)):
        try:
            path = resolve_in_workdir(raw.rstrip("，。；,.;:：)）"), workdir)
        except AgentError:
            continue
        media = IMAGE_TYPES.get(path.suffix.lower())
        if media is None or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            continue
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        images.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        attached.append(display(path, workdir))
    return images, attached
