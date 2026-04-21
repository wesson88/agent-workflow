# 一键后台启动所有服务（无挂起窗口，日志写入 logs/ 目录）
$root = $PSScriptRoot
$logsDir = Join-Path $root "logs"
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir | Out-Null }

$backendLog  = Join-Path $logsDir "backend.log"
$frontendLog = Join-Path $logsDir "frontend.log"
$pidFile     = Join-Path $logsDir "pids.txt"

Write-Host ""
Write-Host "  多Agent 智能会议聊天系统" -ForegroundColor Magenta
Write-Host "  ================================" -ForegroundColor Magenta
Write-Host ""

# ── 启动后端 ──────────────────────────────────────────────────
Write-Host "  [1/2] 启动后端..." -ForegroundColor Cyan
$backendCmd = @"
Set-Location '$root\backend'
if (-not (Test-Path '.venv')) { python -m venv .venv }
& '.venv\Scripts\pip.exe' install -r requirements.txt -q
& '.venv\Scripts\python.exe' -m uvicorn main:app --host 0.0.0.0 --port 8765 --reload
"@
$backendProc = Start-Process powershell `
    -ArgumentList "-NoProfile", "-Command", $backendCmd `
    -WindowStyle Hidden `
    -RedirectStandardOutput $backendLog `
    -RedirectStandardError  "$logsDir\backend_err.log" `
    -PassThru
Write-Host "     PID: $($backendProc.Id)  日志: logs\backend.log" -ForegroundColor DarkGray

Start-Sleep -Seconds 4

# ── 启动前端 ──────────────────────────────────────────────────
Write-Host "  [2/2] 启动前端..." -ForegroundColor Cyan
$frontendCmd = @"
Set-Location '$root\frontend'
npm run dev
"@
$frontendProc = Start-Process powershell `
    -ArgumentList "-NoProfile", "-Command", $frontendCmd `
    -WindowStyle Hidden `
    -RedirectStandardOutput $frontendLog `
    -RedirectStandardError  "$logsDir\frontend_err.log" `
    -PassThru
Write-Host "     PID: $($frontendProc.Id)  日志: logs\frontend.log" -ForegroundColor DarkGray

# 保存 PID 供 stop_all.ps1 使用
"$($backendProc.Id)`n$($frontendProc.Id)" | Set-Content $pidFile

Start-Sleep -Seconds 4

Write-Host ""
Write-Host "  ✅ 服务已在后台启动！" -ForegroundColor Green
Write-Host "     前端:   http://localhost:5173" -ForegroundColor White
Write-Host "     后端:   http://localhost:8765" -ForegroundColor White
Write-Host "     API文档: http://localhost:8765/docs" -ForegroundColor White
Write-Host "     日志目录: $logsDir" -ForegroundColor DarkGray
Write-Host "     停止服务: .\stop_all.ps1" -ForegroundColor DarkGray
Write-Host ""

# 打开浏览器
Start-Process "http://localhost:5173"
