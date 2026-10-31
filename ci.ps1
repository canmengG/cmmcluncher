# Local parity with .github/workflows/ci.yml.
# GitHub Actions still has to be verified after the repository is pushed.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "== Python ==" -ForegroundColor Cyan
py --version

Write-Host "== Unit tests ==" -ForegroundColor Cyan
py -m unittest discover -v
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "== Byte-compile ==" -ForegroundColor Cyan
py -m compileall -q mc_launcher tests
exit $LASTEXITCODE
