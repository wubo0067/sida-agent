<#
.SYNOPSIS
    sida-agent 一键启动脚本（PowerShell 版）。

.DESCRIPTION
    封装 `uv run python main.py ...` 的常用命令，启动前做前置检查：
      1. uv 已安装
      2. .env 存在（缺失仅告警，必填项见 README 3.2）
      3. uv sync 同步依赖（可用 -SkipSync 跳过）

    子命令之后的参数原样透传给 main.py，完整参数见
    `uv run python main.py --help`。

.EXAMPLE
    .\start.ps1 serve
    .\start.ps1 serve --host 0.0.0.0 --port 6173
    .\start.ps1 chat --session s-xxxx
    .\start.ps1 ask --query "讲解可变电路的分析思路"
    .\start.ps1 build --pdf "L:/vivi/.../教材.pdf" --start-page 1 --end-page 10 --subject physics --yes
    .\start.ps1 list-books
    .\start.ps1 list-sessions
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, HelpMessage = '子命令: serve|chat|ask|build|list-books|list-sessions|help')]
    [string]$Command,

    [switch]$SkipSync,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok([string]$m) { Write-Host "    $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "    [warn] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "    [error] $m" -ForegroundColor Red }

$HelpText = @'
sida-agent 一键启动脚本（PowerShell）

用法:
  .\start.ps1 <子命令> [main.py 参数...] [-SkipSync]

子命令:
  serve           启动 FastAPI HTTP 服务（默认 127.0.0.1:6173 /docs）
  chat            多轮对话 REPL（会话落盘，Ctrl+C 退出）
  ask             单轮问答
  build           建库（PDF 提取 + 结构化抽取）
  list-books      列出已导入教材
  list-sessions   列出对话会话
  help            显示本帮助

常用示例:
  .\start.ps1 serve
  .\start.ps1 serve --host 0.0.0.0 --port 6173
  .\start.ps1 serve --reload
  .\start.ps1 chat
  .\start.ps1 chat --session s-xxxx
  .\start.ps1 ask --query "讲解可变电路的分析思路"
  .\start.ps1 build --pdf "L:/vivi/.../教材.pdf" --start-page 1 --end-page 10 --subject physics --yes
  .\start.ps1 list-books
  .\start.ps1 list-sessions

-SkipSync  跳过 uv sync 依赖同步（依赖未变时启动更快）。
子命令之后的参数原样透传给 `uv run python main.py`，
完整参数见 `uv run python main.py --help`。
'@

# ---------- 0. 帮助 / 无参数 ----------
if (-not $Command -or $Command -in 'help', '-h', '--help') {
    Write-Host $HelpText
    exit 0
}

# ---------- 1. 前置检查 ----------
$UvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $UvCmd) {
    Fail '未检测到 uv，请先安装: https://docs.astral.sh/uv/'
    exit 1
}
Step "uv: $($UvCmd.Source)"

$EnvFile = Join-Path $PSScriptRoot '.env'
if (-not (Test-Path -LiteralPath $EnvFile)) {
    Warn '.env 不存在，请先创建（必填项见 README 3.2: VISION_*/REASONING_*/EMBEDDING_*）。'
}
else {
    Ok '.env 已存在'
}

if (-not $SkipSync) {
    Step '同步依赖 (uv sync)...'
    uv sync
    if ($LASTEXITCODE -ne 0) {
        Fail "uv sync 失败 (exit=$LASTEXITCODE)，请检查网络或 uv.lock。"
        exit 1
    }
    Ok '依赖同步完成'
}
else {
    Ok '跳过依赖同步 (-SkipSync)'
}

# ---------- 2. 子命令 → main.py 参数 ----------
$UvArgs = @('run', 'python', 'main.py')
switch ($Command) {
    'serve' { $UvArgs += '--stage', 'serve' }
    'chat' { $UvArgs += '--stage', 'chat' }
    'ask' { $UvArgs += '--stage', 'ask' }
    'build' { $UvArgs += '--stage', 'build' }
    'list-books' { $UvArgs += '--list-books' }
    'list-sessions' { $UvArgs += '--stage', 'chat', '--list' }
    default {
        Fail "未知子命令: $Command"
        Write-Host ''
        Write-Host $HelpText
        exit 1
    }
}
if ($ExtraArgs) { $UvArgs += $ExtraArgs }

Step "执行: uv $($UvArgs -join ' ')"
uv @UvArgs
exit $LASTEXITCODE
