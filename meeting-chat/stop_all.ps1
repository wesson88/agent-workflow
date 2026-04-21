# stop_all.ps1 - Stop all background services
$root    = $PSScriptRoot
$pidFile = Join-Path $root "logs\pids.txt"

Write-Host ""
Write-Host "  [stop_all] Stopping Meeting-Chat services..." -ForegroundColor Magenta
Write-Host ""

if (Test-Path $pidFile) {
    Get-Content $pidFile | Where-Object { $_ -match '^\d+$' } | ForEach-Object {
        $p = [int]$_
        $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "  Stopping PID $p ($($proc.Name))..." -ForegroundColor Yellow
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
            Get-CimInstance Win32_Process |
                Where-Object { $_.ParentProcessId -eq $p } |
                ForEach-Object {
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                }
        }
    }
    Remove-Item $pidFile -Force
} else {
    Write-Host "  No PID file found, killing by port..." -ForegroundColor Yellow
}

@(8765, 5173, 5174) | ForEach-Object {
    $port = $_
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $conn | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
            Write-Host "  Killing port $port (PID $_)..." -ForegroundColor Yellow
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host ""
Write-Host "  [stop_all] Done. All services stopped." -ForegroundColor Green
Write-Host ""
