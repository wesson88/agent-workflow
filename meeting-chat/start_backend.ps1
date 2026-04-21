# 启动后端服务
Write-Host "🚀 启动多Agent会议聊天后端..." -ForegroundColor Cyan

$backendDir = Join-Path $PSScriptRoot "backend"
Set-Location $backendDir

# 检查虚拟环境
if (-not (Test-Path ".venv")) {
    Write-Host "📦 创建虚拟环境..." -ForegroundColor Yellow
    python -m venv .venv
}

# 激活并安装依赖
Write-Host "📦 安装依赖..." -ForegroundColor Yellow
& ".venv\Scripts\pip.exe" install -r requirements.txt -q

Write-Host "✅ 后端启动于 http://localhost:8765" -ForegroundColor Green
Write-Host "📡 WebSocket: ws://localhost:8765/ws/{meeting_id}" -ForegroundColor Green
Write-Host ""

& ".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8765 --reload
