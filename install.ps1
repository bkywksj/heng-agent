# heng-agent 一键安装（Windows PowerShell 5.1 及以上）
#
#   国内：irm https://gitee.com/bkywksj/heng-agent/raw/master/install.ps1 | iex
#   国外：irm https://raw.githubusercontent.com/bkywksj/heng-agent/master/install.ps1 | iex
#
# 做的事：没有 uv 就先装 uv → 装（或升级）heng-agent → 把命令目录加进 PATH。
# pypi.org 连不上时自动改用清华镜像；也可以用环境变量 HENG_AGENT_INDEX 指定包索引地址。
# 测试用：HENG_AGENT_SPEC 指定要装的东西（如本地 wheel 路径），默认 heng-agent；HENG_AGENT_NO_MODIFY_PATH=1 不改 PATH。

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 默认不开 TLS 1.2，访问 pypi.org、astral.sh 这类现代网站的 HTTPS 都会失败
# （实测：明明连得上，却被判成「pypi.org 连不上」、装 uv 也会失败）
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$Mirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function Say([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }

function Test-Reachable([string]$Url) {
    try {
        Invoke-WebRequest -Uri $Url -Method Head -TimeoutSec 8 -UseBasicParsing | Out-Null
        return $true
    } catch {
        $script:ReachError = $_.Exception.Message  # 换镜像时把原因带上，用户排查网络问题用得上
        return $false
    }
}

Say "heng-agent 安装" 'Cyan'

# 1. uv（Python 的包管理工具，会自动准备好 Python，用户不用自己装 Python）
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "· 没找到 uv，先装 uv……"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Say "装 uv 失败：$($_.Exception.Message)" 'Red'
        Say "可以手动安装后再运行本脚本：https://docs.astral.sh/uv/getting-started/installation/" 'Yellow'
        Say "（有 Python 的话也可以：pip install uv -i $Mirror）" 'Yellow'
        exit 1
    }
    # 刚装的 uv 还不在这个窗口的 PATH 里
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say "uv 装好了，但这个窗口还找不到它：请关掉窗口重开一个，再运行一次本脚本。" 'Yellow'
        exit 1
    }
}
Say "· uv：$(uv --version)"

# 2. 包索引：默认 PyPI，连不上自动换清华镜像
$Spec = if ($env:HENG_AGENT_SPEC) { $env:HENG_AGENT_SPEC } else { 'heng-agent' }
$IndexArgs = @()
if ($env:HENG_AGENT_INDEX) {
    $IndexArgs = @('--index-url', $env:HENG_AGENT_INDEX)
    Say "· 用指定的包索引：$env:HENG_AGENT_INDEX"
} elseif (-not (Test-Reachable 'https://pypi.org/simple/pip/')) {
    $IndexArgs = @('--index-url', $Mirror)
    Say "· pypi.org 连不上（$script:ReachError），改用清华镜像"
}

# 3. 装或升级（--force：已装过就覆盖成最新版）
Say "· 安装 $Spec ……"
& uv tool install --force @IndexArgs $Spec
if ($LASTEXITCODE -ne 0) {
    Say "安装失败（退出码 $LASTEXITCODE）。可以把上面的报错发到 https://github.com/bkywksj/heng-agent/issues" 'Red'
    exit $LASTEXITCODE
}

# 4. 命令目录加进 PATH（已经在了就什么都不做；HENG_AGENT_NO_MODIFY_PATH=1 跳过，测试与 CI 用）
if (-not $env:HENG_AGENT_NO_MODIFY_PATH) { & uv tool update-shell 2>$null | Out-Null }

Say ""
Say "✓ 安装完成" 'Green'
$Bin = (& uv tool dir --bin 2>$null)  # 实际装到哪（可能被 UV_TOOL_BIN_DIR 改过），别写死
if (-not (Get-Command heng-agent -ErrorAction SilentlyContinue)) {
    Say "  新开一个终端窗口（让 PATH 生效），进入你的项目目录，输入：heng-agent" 'Cyan'
} else {
    Say "  进入你的项目目录，输入：heng-agent" 'Cyan'
}
Say "  第一次运行会引导你选服务商、填 API 密钥；以后升级再运行一次本脚本，或 uv tool upgrade heng-agent"
Say "  命令装在：$Bin"
