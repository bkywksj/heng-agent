# 本文件由 scripts/install.src.ps1 生成（scripts/build_install.py），改那个文件，别直接改这里
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
        $script:ReachError = $_.Exception.Message
        return $false
    }
}

Say "heng-agent $([char]0x5B89)$([char]0x88C5)" 'Cyan'

# 1. uv（Python 的包管理工具，会自动准备好 Python，用户不用自己装 Python）
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "$([char]0x00B7) $([char]0x6CA1)$([char]0x627E)$([char]0x5230) uv$([char]0xFF0C)$([char]0x5148)$([char]0x88C5) uv$([char]0x2026)$([char]0x2026)"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Say "$([char]0x88C5) uv $([char]0x5931)$([char]0x8D25)$([char]0xFF1A)$($_.Exception.Message)" 'Red'
        Say "$([char]0x53EF)$([char]0x4EE5)$([char]0x624B)$([char]0x52A8)$([char]0x5B89)$([char]0x88C5)$([char]0x540E)$([char]0x518D)$([char]0x8FD0)$([char]0x884C)$([char]0x672C)$([char]0x811A)$([char]0x672C)$([char]0xFF1A)https://docs.astral.sh/uv/getting-started/installation/" 'Yellow'
        Say "$([char]0xFF08)$([char]0x6709) Python $([char]0x7684)$([char]0x8BDD)$([char]0x4E5F)$([char]0x53EF)$([char]0x4EE5)$([char]0xFF1A)pip install uv -i $Mirror$([char]0xFF09)" 'Yellow'
        exit 1
    }
    # 刚装的 uv 还不在这个窗口的 PATH 里
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say "uv $([char]0x88C5)$([char]0x597D)$([char]0x4E86)$([char]0xFF0C)$([char]0x4F46)$([char]0x8FD9)$([char]0x4E2A)$([char]0x7A97)$([char]0x53E3)$([char]0x8FD8)$([char]0x627E)$([char]0x4E0D)$([char]0x5230)$([char]0x5B83)$([char]0xFF1A)$([char]0x8BF7)$([char]0x5173)$([char]0x6389)$([char]0x7A97)$([char]0x53E3)$([char]0x91CD)$([char]0x5F00)$([char]0x4E00)$([char]0x4E2A)$([char]0xFF0C)$([char]0x518D)$([char]0x8FD0)$([char]0x884C)$([char]0x4E00)$([char]0x6B21)$([char]0x672C)$([char]0x811A)$([char]0x672C)$([char]0x3002)" 'Yellow'
        exit 1
    }
}
Say "$([char]0x00B7) uv$([char]0xFF1A)$(uv --version)"

# 2. 包索引：默认 PyPI，连不上自动换清华镜像
$Spec = if ($env:HENG_AGENT_SPEC) { $env:HENG_AGENT_SPEC } else { 'heng-agent' }
$IndexArgs = @()
if ($env:HENG_AGENT_INDEX) {
    $IndexArgs = @('--index-url', $env:HENG_AGENT_INDEX)
    Say "$([char]0x00B7) $([char]0x7528)$([char]0x6307)$([char]0x5B9A)$([char]0x7684)$([char]0x5305)$([char]0x7D22)$([char]0x5F15)$([char]0xFF1A)$env:HENG_AGENT_INDEX"
} elseif (-not (Test-Reachable 'https://pypi.org/simple/pip/')) {
    $IndexArgs = @('--index-url', $Mirror)
    Say "$([char]0x00B7) pypi.org $([char]0x8FDE)$([char]0x4E0D)$([char]0x4E0A)$([char]0xFF08)$script:ReachError$([char]0xFF09)$([char]0xFF0C)$([char]0x6539)$([char]0x7528)$([char]0x6E05)$([char]0x534E)$([char]0x955C)$([char]0x50CF)"
}

# 3. 装或升级（--force：已装过就覆盖成最新版）
# Windows 上 heng-agent 运行时文件被占用：硬装会失败一半、把安装弄坏（之后一启动就 ModuleNotFoundError）。先让用户关掉
$Running = @(Get-Process -Name 'heng-agent' -ErrorAction SilentlyContinue)
if ($Running.Count -gt 0) {
    Say "$([char]0x8FD8)$([char]0x6709) $($Running.Count) $([char]0x4E2A) heng-agent $([char]0x5728)$([char]0x8FD0)$([char]0x884C)$([char]0xFF08)$([char]0x8FDB)$([char]0x7A0B)$([char]0x53F7) $(($Running | ForEach-Object Id) -join ', ')$([char]0xFF09)$([char]0xFF1A)$([char]0x5148)$([char]0x5173)$([char]0x6389)$([char]0x5B83)$([char]0x4EEC)$([char]0x518D)$([char]0x8FD0)$([char]0x884C)$([char]0x672C)$([char]0x811A)$([char]0x672C)$([char]0x3002)" 'Yellow'
    Say "$([char]0xFF08)$([char]0x7A0B)$([char]0x5E8F)$([char]0x8FD0)$([char]0x884C)$([char]0x65F6)$([char]0x6587)$([char]0x4EF6)$([char]0x88AB)$([char]0x5360)$([char]0x7528)$([char]0xFF0C)$([char]0x8FD9)$([char]0x65F6)$([char]0x5B89)$([char]0x88C5)$([char]0x4F1A)$([char]0x5931)$([char]0x8D25)$([char]0xFF0C)$([char]0x8FD8)$([char]0x4F1A)$([char]0x628A)$([char]0x5DF2)$([char]0x6709)$([char]0x7684)$([char]0x5B89)$([char]0x88C5)$([char]0x5F04)$([char]0x574F)$([char]0xFF09)" 'Yellow'
    exit 1
}
Say "$([char]0x00B7) $([char]0x5B89)$([char]0x88C5) $Spec $([char]0x2026)$([char]0x2026)"
& uv tool install --force @IndexArgs $Spec
if ($LASTEXITCODE -ne 0) {
    Say "$([char]0x5B89)$([char]0x88C5)$([char]0x5931)$([char]0x8D25)$([char]0xFF08)$([char]0x9000)$([char]0x51FA)$([char]0x7801) $LASTEXITCODE$([char]0xFF09)$([char]0x3002)$([char]0x53EF)$([char]0x4EE5)$([char]0x628A)$([char]0x4E0A)$([char]0x9762)$([char]0x7684)$([char]0x62A5)$([char]0x9519)$([char]0x53D1)$([char]0x5230) https://github.com/bkywksj/heng-agent/issues" 'Red'
    exit $LASTEXITCODE
}

# 4. 命令目录加进 PATH（已经在了就什么都不做；HENG_AGENT_NO_MODIFY_PATH=1 跳过，测试与 CI 用）
if (-not $env:HENG_AGENT_NO_MODIFY_PATH) { & uv tool update-shell 2>$null | Out-Null }

Say ""
Say "$([char]0x2713) $([char]0x5B89)$([char]0x88C5)$([char]0x5B8C)$([char]0x6210)" 'Green'
$Bin = (& uv tool dir --bin 2>$null)
if (-not (Get-Command heng-agent -ErrorAction SilentlyContinue)) {
    Say "  $([char]0x65B0)$([char]0x5F00)$([char]0x4E00)$([char]0x4E2A)$([char]0x7EC8)$([char]0x7AEF)$([char]0x7A97)$([char]0x53E3)$([char]0xFF08)$([char]0x8BA9) PATH $([char]0x751F)$([char]0x6548)$([char]0xFF09)$([char]0xFF0C)$([char]0x8FDB)$([char]0x5165)$([char]0x4F60)$([char]0x7684)$([char]0x9879)$([char]0x76EE)$([char]0x76EE)$([char]0x5F55)$([char]0xFF0C)$([char]0x8F93)$([char]0x5165)$([char]0xFF1A)heng-agent" 'Cyan'
} else {
    Say "  $([char]0x8FDB)$([char]0x5165)$([char]0x4F60)$([char]0x7684)$([char]0x9879)$([char]0x76EE)$([char]0x76EE)$([char]0x5F55)$([char]0xFF0C)$([char]0x8F93)$([char]0x5165)$([char]0xFF1A)heng-agent" 'Cyan'
}
Say "  $([char]0x7B2C)$([char]0x4E00)$([char]0x6B21)$([char]0x8FD0)$([char]0x884C)$([char]0x4F1A)$([char]0x5F15)$([char]0x5BFC)$([char]0x4F60)$([char]0x9009)$([char]0x670D)$([char]0x52A1)$([char]0x5546)$([char]0x3001)$([char]0x586B) API $([char]0x5BC6)$([char]0x94A5)$([char]0xFF1B)$([char]0x4EE5)$([char]0x540E)$([char]0x5347)$([char]0x7EA7)$([char]0x518D)$([char]0x8FD0)$([char]0x884C)$([char]0x4E00)$([char]0x6B21)$([char]0x672C)$([char]0x811A)$([char]0x672C)$([char]0xFF0C)$([char]0x6216) uv tool upgrade heng-agent"
Say "  $([char]0x547D)$([char]0x4EE4)$([char]0x88C5)$([char]0x5728)$([char]0xFF1A)$Bin"
