# 启动前端开发服务器
Write-Host "🚀 启动多Agent会议聊天前端..." -ForegroundColor Cyan

$frontendDir = Join-Path $PSScriptRoot "frontend"
Set-Location $frontendDir

if (-not (Test-Path "node_modules")) {
    Write-Host "📦 安装前端依赖..." -ForegroundColor Yellow
    npm install
}

Write-Host "✅ 前端启动于 http://localhost:5173" -ForegroundColor Green
npm run dev
