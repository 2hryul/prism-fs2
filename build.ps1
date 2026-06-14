# build.ps1 -- prism-fs release build (PyInstaller onedir full bundle)
# Windows 11 / PowerShell. Output name injected from VERSION file.
# ASCII-only on purpose: PowerShell 5.1 mis-parses non-ASCII .ps1 without a BOM.
# Output: dist\setup\setup_v{VERSION}\setup_v{VERSION}.exe (+ _internal\, storage\)
#
# Usage:  .\build.ps1
# Note:   run the server on :8021 first so live tests gate too (recommended).

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# 1) Load VERSION -> inject name
$version = (Get-Content (Join-Path $root "VERSION") -Raw).Trim()
$name = "setup_v$version"
$env:PRISM_VERSION = $version
Write-Host "==> prism-fs build v$version (onedir full bundle)" -ForegroundColor Cyan

# 1b) Build python -- prefer CPU-torch build venv (slim bundle ~1.7GB vs ~6.8GB).
# Falls back to system python if the venv is absent (then bundle stays large).
$buildPy = Join-Path $root ".venv-build\Scripts\python.exe"
if (Test-Path $buildPy) {
    Write-Host "    build env: .venv-build (CPU torch)" -ForegroundColor Green
} else {
    $buildPy = "python"
    Write-Host "    build env: system python (no .venv-build -- bundle will be large)" -ForegroundColor Yellow
}

# 2) Gate -- unit tests must pass (run on system python: full dev deps incl. playwright)
Write-Host "==> [gate] pytest" -ForegroundColor Cyan
python -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed -- build aborted" }

# 2b) External https must be 0 (closed-network regression gate)
$ext = Select-String -Path "src\static\index.html" -Pattern "https://" -SimpleMatch |
       Where-Object { $_.Line -notmatch "w3\.org|json-schema\.org" }
if ($ext) { $ext; throw "external https reference found -- build aborted (closed-net)" }
Write-Host "    external https = 0 OK" -ForegroundColor Green

# 3) Build deps + model (PyInstaller from build env; model export via system python)
& $buildPy -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "==> install PyInstaller"; & $buildPy -m pip install pyinstaller }
if (-not (Test-Path "src\models\ko-sroberta")) {
    Write-Host "==> export ko-sroberta model (first run)"
    $env:HF_HUB_OFFLINE = "1"
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('jhgan/ko-sroberta-multitask').save('src/models/ko-sroberta')"
}

# 4) Clean previous output, then build
if (Test-Path "dist")  { Remove-Item "dist"  -Recurse -Force }
if (Test-Path "build\work") { Remove-Item "build\work" -Recurse -Force }
Write-Host "==> PyInstaller (several minutes)" -ForegroundColor Cyan
& $buildPy -m PyInstaller prism_fs.spec --noconfirm --distpath "dist\setup" --workpath "build\work"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# 5) Ship demo data (storage) next to exe (persistent).
# Exclude: .env (secret) + xbrl/ + source/ (raw DART artifacts -- not used by
# search/viewer, ~840MB. Re-collectable via DART or import zip if ever needed).
$target = "dist\setup\$name"
if (Test-Path "src\storage") {
    Write-Host "==> bundling storage (demo data, excluding xbrl/source raw)" -ForegroundColor Cyan
    robocopy "src\storage" (Join-Path $target "storage") /E /XD xbrl source /XF .env /NFL /NDL /NJH /NJS /NP | Out-Null
    # robocopy: 0-7 = success(1 = files copied). Reset so it isn't read as build failure.
    if ($LASTEXITCODE -ge 8) { throw "robocopy storage failed (code $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

# 6) Report
$exe = Join-Path $target "$name.exe"
if (Test-Path $exe) {
    $sizeGB = [math]::Round(((Get-ChildItem $target -Recurse | Measure-Object Length -Sum).Sum / 1GB), 2)
    Write-Host "==> done: $exe  (total $sizeGB GB)" -ForegroundColor Green
} else {
    throw "build artifact (.exe) missing: $exe"
}
