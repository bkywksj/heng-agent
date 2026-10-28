"""工具本身：文件读写改的边界情况、搜索跳过目录与条数上限、命令的退出码与超时、路径越界。"""

from __future__ import annotations

import codecs
import sys
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


@pytest.mark.parametrize("raw", [
    "../x", "a/../../x", "/etc/passwd",
    # 只在 Windows 上是绝对路径；Linux / macOS 上它是个普通的相对路径（叫 C: 的子目录），本来就不该拒
    pytest.param("C:/Windows/x", marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows 的盘符路径")),
])  # fmt: skip
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


# ---------- 链接逃逸：grep / glob 遍历时不能顺着链接读到工作目录外 ----------

SECRET = "TOP-SECRET-0xC0FFEE"


def _escape_setup(tmp_path: Path) -> tuple[ToolContext, Path]:
    """工作目录与「外面」是兄弟目录；外面放一个装着密钥的文件。"""
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (outside / "id_rsa").write_text(f"key = {SECRET}\n", encoding="utf-8")
    (work / "ok.txt").write_text("hello\n", encoding="utf-8")
    return ToolContext(workdir=work, bash_timeout=30), outside


def _link_dir(link: Path, target: Path) -> None:
    """目录链接：Windows 上建目录联接（junction，不要管理员权限，是现实里最容易被利用的），其余系统建符号链接。"""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_grep_glob_do_not_follow_dir_link_outside(tmp_path: Path) -> None:
    """以前：工作目录里一个指向外部的目录链接，grep 就能把外面的文件内容读出来（os.walk 会钻进 Windows 的目录联接）。"""
    ctx, outside = _escape_setup(tmp_path)
    _link_dir(ctx.workdir / "linked", outside)
    found = Grep().run({"pattern": "TOP-SECRET"}, ctx).content
    assert SECRET not in found
    assert "id_rsa" not in Glob().run({"pattern": "**/*"}, ctx).content
    assert "ok.txt" in Glob().run({"pattern": "**/*"}, ctx).content  # 工作目录里的照常找得到


def test_grep_glob_do_not_follow_file_link_outside(tmp_path: Path) -> None:
    ctx, outside = _escape_setup(tmp_path)
    try:
        (ctx.workdir / "notes.txt").symlink_to(outside / "id_rsa")
    except OSError:
        pytest.skip("这台机器建不了文件符号链接（Windows 要开发者模式）")
    assert SECRET not in Grep().run({"pattern": "TOP-SECRET"}, ctx).content
    with pytest.raises(AgentError, match="工作目录"):  # 直接点名这个文件：路径校验本来就拒
        Grep().run({"pattern": "TOP", "path": "notes.txt"}, ctx)
    assert "notes.txt" not in Glob().run({"pattern": "*.txt"}, ctx).content


def test_links_inside_workdir_still_work(tmp_path: Path) -> None:
    """指向工作目录里面的链接不受影响。"""
    ctx, _ = _escape_setup(tmp_path)
    (ctx.workdir / "src").mkdir()
    (ctx.workdir / "src" / "a.py").write_text("needle = 1\n", encoding="utf-8")
    _link_dir(ctx.workdir / "alias", ctx.workdir / "src")
    assert "needle" in Grep().run({"pattern": "needle"}, ctx).content


# ---------- Windows 上找 bash：别用 System32 里的 WSL 启动器 ----------

win_only = pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 上有 WSL 启动器与 Git Bash 之分")


def _fake_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture
def fake_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """假的 Windows 目录：System32 里有 WSL 的 bash.exe；PATH 只有它（从 PowerShell / cmd 启动时常见）。"""
    windows = tmp_path / "Windows"
    _fake_exe(windows / "System32" / "bash.exe")
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    monkeypatch.setenv("PATH", str(windows / "System32"))
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / "none"))
    return tmp_path


@win_only
def test_wsl_bash_is_skipped_for_git_bash_on_path(fake_windows: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """以前直接用 PATH 里第一个 bash：从 PowerShell 启动时是 System32\bash.exe（WSL 启动器），
    没装 Linux 发行版就每条命令都报 execvpe(/bin/bash) failed（真实终端里实测撞到）。"""
    git_bash = _fake_exe(fake_windows / "Git" / "usr" / "bin" / "bash.exe")
    monkeypatch.setenv("PATH", f"{fake_windows / 'Windows' / 'System32'};{git_bash.parent}")
    from heng_agent.tools.shell import shell_argv

    assert Path(shell_argv("echo hi")[0]) == git_bash


@win_only
def test_git_bash_found_from_git_exe(fake_windows: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r"""PATH 里只有 git（Git 安装时默认只把 cmd\git.exe 加进 PATH），从它推出 Git\bin\bash.exe。"""
    git_exe = _fake_exe(fake_windows / "Git" / "cmd" / "git.exe")
    bash = _fake_exe(fake_windows / "Git" / "bin" / "bash.exe")
    monkeypatch.setenv("PATH", f"{fake_windows / 'Windows' / 'System32'};{git_exe.parent}")
    from heng_agent.tools.shell import shell_argv

    assert Path(shell_argv("echo hi")[0]) == bash


@win_only
def test_no_git_bash_falls_back_to_cmd(fake_windows: Path) -> None:
    from heng_agent.tools.shell import shell_argv

    assert shell_argv("echo hi") == ["cmd", "/c", "echo hi"]  # 不用 WSL 启动器
