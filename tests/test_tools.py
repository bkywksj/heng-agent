"""工具本身：文件读写改的边界情况、搜索跳过目录与条数上限、命令的退出码与超时、路径越界。"""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from heng_agent.core.errors import AgentError
from heng_agent.tools.base import ToolContext
from heng_agent.tools.files import EditFile, ReadFile, WriteFile
from heng_agent.tools.paths import resolve_in_workdir
from heng_agent.tools.search import Glob, Grep
from heng_agent.tools.shell import Bash, clip


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workdir=tmp_path, bash_timeout=30)


def test_read_with_offset_and_limit(ctx: ToolContext) -> None:
    (ctx.workdir / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
    out = ReadFile().run({"path": "f.txt", "offset": 4, "limit": 2}, ctx).content
    assert out.startswith("4\tline4\n5\tline5") and "offset=6" in out


def test_edit_keeps_crlf_and_bom(ctx: ToolContext) -> None:
    path = ctx.workdir / "win.txt"
    path.write_bytes(codecs.BOM_UTF8 + b"a = 1\r\nb = 2\r\n")
    res = EditFile().run({"path": "win.txt", "old_string": "a = 1\nb = 2", "new_string": "a = 10\nb = 20"}, ctx)
    assert not res.is_error
    assert path.read_bytes() == codecs.BOM_UTF8 + b"a = 10\r\nb = 20\r\n"


def test_edit_requires_unique_match(ctx: ToolContext) -> None:
    (ctx.workdir / "d.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    edit = EditFile()
    res = edit.run({"path": "d.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert res.is_error and "出现了 2 次" in res.content
    res = edit.run({"path": "d.py", "old_string": "x = 1", "new_string": "x = 2", "replace_all": True}, ctx)
    assert not res.is_error and (ctx.workdir / "d.py").read_text(encoding="utf-8") == "x = 2\nx = 2\n"
    assert "没找到" in edit.run({"path": "d.py", "old_string": "zzz", "new_string": "y"}, ctx).content


def test_write_creates_parents_and_keeps_existing_bom(ctx: ToolContext) -> None:
    WriteFile().run({"path": "sub/dir/new.txt", "content": "你好"}, ctx)
    assert (ctx.workdir / "sub/dir/new.txt").read_bytes() == "你好".encode()
    bom_file = ctx.workdir / "bom.txt"
    bom_file.write_bytes(codecs.BOM_UTF8 + b"old")
    WriteFile().run({"path": "bom.txt", "content": "new"}, ctx)
    assert bom_file.read_bytes() == codecs.BOM_UTF8 + b"new"


def test_binary_file_is_not_read_as_text(ctx: ToolContext) -> None:
    (ctx.workdir / "x.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(AgentError, match="二进制"):
        ReadFile().run({"path": "x.bin"}, ctx)


def test_glob_and_grep_skip_dependency_dirs(ctx: ToolContext) -> None:
    (ctx.workdir / "src").mkdir()
    (ctx.workdir / "src/app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (ctx.workdir / "node_modules/pkg").mkdir(parents=True)
    (ctx.workdir / "node_modules/pkg/index.py").write_text("def hello(): pass\n", encoding="utf-8")
    (ctx.workdir / "top.py").write_text("hello = 1\n", encoding="utf-8")
    found = Glob().run({"pattern": "**/*.py"}, ctx).content.splitlines()
    assert sorted(found) == ["src/app.py", "top.py"]  # 「**/x」也匹配根目录；node_modules 被跳过
    hits = Grep().run({"pattern": r"def hello", "glob": "*.py"}, ctx).content
    assert hits == "src/app.py:1: def hello():"
    assert Grep().run({"pattern": "("}, ctx).is_error  # 正则写错给出错误而不是崩溃


def test_bash_exit_code_output_and_timeout(ctx: ToolContext) -> None:
    ok = Bash().run({"command": "echo 你好"}, ctx)
    assert not ok.is_error and ok.content.startswith("退出码 0") and "你好" in ok.content
    bad = Bash().run({"command": "exit 3"}, ctx)
    assert bad.is_error and bad.content.startswith("退出码 3")
    slow = Bash().run({"command": "sleep 5", "timeout": 0.5}, ctx)
    assert slow.is_error and "超时" in slow.content


def test_clip_keeps_head_and_tail() -> None:
    text = "头" * 50 + "中" * 1000 + "尾" * 100
    out = clip(text, limit=300)
    assert out.startswith("头") and out.endswith("尾" * 100) and "省略" in out


@pytest.mark.parametrize("raw", ["../x", "a/../../x", "/etc/passwd", "C:/Windows/x"])
def test_paths_outside_workdir_are_refused(ctx: ToolContext, raw: str) -> None:
    with pytest.raises(AgentError, match="工作目录"):
        resolve_in_workdir(raw, ctx.workdir)


def test_timeout_kills_whole_process_tree(ctx: ToolContext) -> None:
    # bash 的子进程（python）3 秒后写标记文件；超时 1 秒必须连它一起杀掉：
    # 只杀 bash 的话，孙进程还会写出文件，而且握着输出管道让调用一直等到它结束
    import sys
    import time

    py = Path(sys.executable).as_posix()
    cmd = f"\"{py}\" -c \"import time, pathlib; time.sleep(3); pathlib.Path('marker.txt').write_text('x')\""
    started = time.monotonic()
    res = Bash().run({"command": cmd, "timeout": 1}, ctx)
    assert res.is_error and "整个进程树" in res.content
    assert time.monotonic() - started < 2.5
    time.sleep(3)
    assert not (ctx.workdir / "marker.txt").exists()
