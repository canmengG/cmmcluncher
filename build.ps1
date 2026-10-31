[CmdletBinding()]
param(
    [switch]$NoZip,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root

$PythonExe = $Python
if (-not $PythonExe) {
    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    } else {
        $PythonExe = "python"
    }
}
if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "找不到 Python：$PythonExe。请安装 Python 3.13，或传入 -Python <python.exe>。"
}

$Icon = Join-Path $Root "assets\icon.ico"
$Assets = Join-Path $Root "assets"
$Entry = Join-Path $Root "launcher.py"
$Version = (& $PythonExe -c "from mc_launcher import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $Version) { throw "无法读取 APP_VERSION" }
Write-Host "Version: $Version" -ForegroundColor Cyan
Write-Host "Python: $PythonExe" -ForegroundColor Cyan

Write-Host "Building MinecraftLauncher.exe ..." -ForegroundColor Cyan
& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name MinecraftLauncher `
    --icon $Icon `
    --add-data "$Assets;assets" `
    $Entry

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$AppDir = Join-Path $Root "dist\MinecraftLauncher"
$Exe = Join-Path $AppDir "MinecraftLauncher.exe"
if (-not (Test-Path $Exe)) {
    throw "PyInstaller 没有生成 dist\MinecraftLauncher\MinecraftLauncher.exe"
}
Write-Host "Built: $Exe" -ForegroundColor Green

if (-not $NoZip) {
    $ReleaseDir = Join-Path $Root "release"
    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
    $Zip = Join-Path $ReleaseDir "MinecraftLauncher-$Version-win64.zip"
    if (Test-Path $Zip) {
        Remove-Item -LiteralPath $Zip -Force
    }
    Compress-Archive -Path $AppDir, (Join-Path $Root "README.md"), (Join-Path $Root "screenshots") -DestinationPath $Zip -CompressionLevel Optimal
    Write-Host "Packaged: $Zip" -ForegroundColor Green
}
