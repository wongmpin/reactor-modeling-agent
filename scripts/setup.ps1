$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3.11 or 3.12 is required.' }
$version = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($version -notin @('3.11', '3.12')) { throw "Python 3.11 or 3.12 required; found $version." }

if (-not (Test-Path '.venv')) { python -m venv .venv }
$venvPython = Join-Path $repo '.venv\Scripts\python.exe'
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e '.[windows,test]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; setup is incomplete.' }
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
New-Item -ItemType Directory -Force -Path 'data\cases','data\runs','logs' | Out-Null
Write-Host 'Setup complete. Configure DEEPSEEK_API_KEY in .env, then run scripts/start.ps1.'
