<#
.SYNOPSIS
    PaperMind 一键启动脚本（规划 §7 / §25.3：无需手工配环境）。

.DESCRIPTION
    1) 检查 conda 环境（默认 scenemind）与关键依赖；
    2) 确认 MySQL 可达（不可达时提示改用 SQLite）；
    3) 初始化业务库（幂等）与内置模型；
    4) 启动 FastAPI 后端（默认 http://127.0.0.1:8710）。

.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -EnvName py312 -Port 8800
    .\scripts\start.ps1 -SkipInit -NoBrowser
#>
param(
    [string]$EnvName = "scenemind",
    [int]$Port = 8710,
    [string]$HostAddr = "127.0.0.1",
    [switch]$SkipInit,
    [switch]$NoBrowser,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  PaperMind · 智能论文阅读与知识管理系统" -ForegroundColor White
Write-Host "  --------------------------------------" -ForegroundColor DarkGray
Write-Ok "项目目录：$root"

# ---------------------------------------------------------------- 1. Python
Write-Step "检查 conda 环境：$EnvName"
$condaExe = (Get-Command conda -ErrorAction SilentlyContinue).Source
if (-not $condaExe) {
    Write-Warn2 "未找到 conda，尝试使用系统 python"
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "找不到 conda 或 python，请先安装 Miniconda/Anaconda" }
} else {
    $envJson = conda env list --json | ConvertFrom-Json
    $envPath = $envJson.envs | Where-Object { (Split-Path $_ -Leaf) -eq $EnvName } | Select-Object -First 1
    if (-not $envPath) {
        Write-Warn2 "conda 环境 $EnvName 不存在，可用环境："
        $envJson.envs | ForEach-Object { Write-Host "      - $(Split-Path $_ -Leaf)" }
        throw "请先创建环境：conda create -n $EnvName python=3.12"
    }
    $python = Join-Path $envPath "python.exe"
    if (-not (Test-Path $python)) { $python = Join-Path $envPath "python" }
}
Write-Ok "Python：$python"
$pyVer = & $python -c "import sys;print(sys.version.split()[0])"
Write-Ok "版本：$pyVer"

# ---------------------------------------------------------------- 2. 依赖
Write-Step "检查关键依赖"
$missing = & $python -c @"
import importlib, sys
need = ['fastapi','uvicorn','pydantic','pydantic_settings','sqlalchemy','pymysql',
        'fitz','PIL','httpx','sse_starlette','openai']
miss = [m for m in need if importlib.util.find_spec(m) is None]
print(' '.join(miss))
"@
if ($missing.Trim()) {
    Write-Warn2 "缺失依赖：$missing"
    Write-Warn2 "安装：& '$python' -m pip install -r requirements.txt"
    $ans = Read-Host "现在自动安装？（需要联网，y/N）"
    if ($ans -eq "y" -or $ans -eq "Y") {
        & $python -m pip install -r requirements.txt
    } else {
        throw "依赖不完整，已中止"
    }
} else {
    Write-Ok "关键依赖齐全"
}
$opt = & $python -c @"
import importlib
print(' '.join(m for m in ['lancedb','jieba','torch','sentence_transformers']
               if importlib.util.find_spec(m) is not None))
"@
Write-Ok "可选依赖：$(if ($opt.Trim()) { $opt } else { '（无，检索/本地模型将降级）' })"

# ---------------------------------------------------------------- 3. 数据库
if (-not $SkipInit) {
    Write-Step "初始化业务库（幂等）"
    & $python "cli/pr.py" init
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "初始化失败。若本机没有 MySQL，请在 .env 中设置 PR_DB_BACKEND=sqlite 后重试。"
        throw "数据库初始化失败"
    }
}

# ---------------------------------------------------------------- 4. 启动
Write-Step "启动后端 http://${HostAddr}:${Port}  （API 文档：/docs）"
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($u)
        Start-Sleep -Seconds 3
        Start-Process $u
    } -ArgumentList "http://${HostAddr}:${Port}/docs" | Out-Null
}
$args = @("cli/pr.py", "serve", "--host", $HostAddr, "--port", "$Port")
if ($Reload) { $args += "--reload" }
& $python @args
