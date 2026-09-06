[CmdletBinding()]
param(
    [string]$Python = 'D:\Software\anaconda3\envs\learn\python.exe',
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}
$previousConfig = $env:EASYLEARN_CONFIG
if ($ConfigPath) {
    $resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).ProviderPath
}
Push-Location -LiteralPath $projectRoot
try {
    if ($ConfigPath) {
        $env:EASYLEARN_CONFIG = $resolvedConfig
    }
    $arguments = @('-m', 'easylearn')
    if ($ConfigPath) {
        $arguments += @('--config', $resolvedConfig)
    }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "EasyLearn failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
    if ($ConfigPath) {
        $env:EASYLEARN_CONFIG = $previousConfig
    }
}
