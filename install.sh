#!/bin/sh
# heng-agent 一键安装（macOS / Linux）
#
#   国内：curl -LsSf https://gitee.com/bkywksj/heng-agent/raw/master/install.sh | sh
#   国外：curl -LsSf https://raw.githubusercontent.com/bkywksj/heng-agent/master/install.sh | sh
#
# 做的事：没有 uv 就先装 uv → 装（或升级）heng-agent → 把命令目录加进 PATH。
# pypi.org 连不上时自动改用清华镜像；也可以用环境变量 HENG_AGENT_INDEX 指定包索引地址。
# 测试用：HENG_AGENT_SPEC 指定要装的东西（如本地 wheel 路径），默认 heng-agent；HENG_AGENT_NO_MODIFY_PATH=1 不改 PATH。
set -eu

MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
SPEC="${HENG_AGENT_SPEC:-heng-agent}"
# 原来的 PATH：第 1 步为了找到刚装的 uv 会把 ~/.local/bin 加进本脚本的 PATH，第 4 步要拿原来的判断「加没加过」
ORIG_PATH="$PATH"

say() { printf '%s\n' "$*"; }

say "heng-agent 安装"

# 1. uv（Python 的包管理工具，会自动准备好 Python，用户不用自己装 Python）
#    UV_NO_MODIFY_PATH=1：uv 自己的安装器默认把 PATH 写进它认得的**所有** shell 的配置——没有的还新建
#    （只用 bash 的机器上会凭空多出 ~/.zshrc、~/.config/fish/）。PATH 统一交给第 4 步，只改当前在用的 shell。
if ! command -v uv >/dev/null 2>&1; then
    say "· 没找到 uv，先装 uv……"
    if ! curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh; then
        say "装 uv 失败。可以手动安装后再运行本脚本：https://docs.astral.sh/uv/getting-started/installation/"
        say "（有 Python 的话也可以：pip install uv -i $MIRROR）"
        exit 1
    fi
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"  # 刚装的 uv 还不在这个 shell 的 PATH 里
    export PATH
    if ! command -v uv >/dev/null 2>&1; then
        say "uv 装好了，但这个终端还找不到它：请重开一个终端，再运行一次本脚本。"
        exit 1
    fi
fi
say "· uv：$(uv --version)"

# 2. 包索引：默认 PyPI，连不上自动换清华镜像
if [ -n "${HENG_AGENT_INDEX:-}" ]; then
    set -- --index-url "$HENG_AGENT_INDEX"
    say "· 用指定的包索引：$HENG_AGENT_INDEX"
elif ! curl -fsS -o /dev/null --max-time 8 https://pypi.org/simple/pip/; then
    set -- --index-url "$MIRROR"
    say "· pypi.org 连不上，改用清华镜像"
else
    set --
fi

# 3. 装或升级（--force：已装过就覆盖成最新版）
say "· 安装 $SPEC ……"
uv tool install --force "$@" "$SPEC"

# 4. 命令目录加进当前 shell 的 PATH（已经在了就什么都不做；HENG_AGENT_NO_MODIFY_PATH=1 跳过，测试与 CI 用）。
#    用原来的 PATH 跑：第 1 步往本脚本的 PATH 里加过 ~/.local/bin，uv 看到「已经在 PATH 里」就不写配置——
#    新开的终端会找不到 heng-agent（Ubuntu 自带的 .profile 碰巧会加 ~/.local/bin，macOS 的 zsh 就不会）。
if [ -z "${HENG_AGENT_NO_MODIFY_PATH:-}" ]; then
    UV_BIN="$(command -v uv)"
    PATH="$ORIG_PATH" "$UV_BIN" tool update-shell >/dev/null 2>&1 || true
fi

say ""
say "✓ 安装完成"
if command -v heng-agent >/dev/null 2>&1; then
    say "  进入你的项目目录，输入：heng-agent"
else
    say "  重开一个终端（让 PATH 生效），进入你的项目目录，输入：heng-agent"
fi
say "  第一次运行会引导你选服务商、填 API 密钥；以后升级运行 heng-agent upgrade（或再运行一次本脚本）"
