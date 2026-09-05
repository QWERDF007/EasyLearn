param(
    [ValidateSet('web', 'migrate')]
    [string]$Action = 'web',
    [string]$Python = 'E:\Softwares\Anaconda3\envs\learn\python.exe',
    [string]$BindAddress = '127.0.0.1',
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}
Push-Location -LiteralPath $projectRoot
try {
    if ($Action -eq 'migrate') {
        & $Python -m alembic upgrade head
    } else {
        & $Python -m uvicorn easylearn.main:create_app --factory --host $BindAddress --port $Port
    }
    if ($LASTEXITCODE -ne 0) {
        throw "EasyLearn $Action failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
