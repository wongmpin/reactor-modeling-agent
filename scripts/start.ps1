$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Virtual environment missing; run scripts/setup.ps1 first.' }
$version = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($version -notin @('3.11', '3.12')) { throw "The project environment must use Python 3.11 or 3.12; found $version." }
& $python -c "import streamlit,pydantic,langgraph,langgraph.checkpoint.sqlite,mcp,openai,pint,win32com" 2>$null
if ($LASTEXITCODE -ne 0) { throw 'One or more required packages are missing. Run scripts/setup.ps1.' }
$envFile = Join-Path $repo '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$' -and $matches[2] -notmatch '^\s*#') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"').Trim("'"), 'Process')
        }
    }
}
if (-not $env:DEEPSEEK_API_KEY) { Write-Warning 'DEEPSEEK_API_KEY is not set. Natural-language model execution will pause.' }
$hysysExe = 'C:\Program Files\AspenTech\Aspen HYSYS V15.0\AspenHysys.exe'
if ((Test-Path $hysysExe) -and (Test-Path 'Registry::HKEY_CLASSES_ROOT\HYSYS.Application\CLSID')) {
    Write-Host 'HYSYS V15 installation and COM registration detected.'
} else {
    Write-Warning 'HYSYS V15 executable or COM registration was not found in the default location.'
}
New-Item -ItemType Directory -Force -Path (Join-Path $repo 'logs'),(Join-Path $repo 'data') | Out-Null
foreach ($savedService in @('streamlit','worker','mcp')) {
    if (Test-Path (Join-Path $repo "data\$savedService.pid")) { throw 'A saved session exists. Run scripts/stop.ps1 before restarting.' }
}
$workerProcess = Start-Process -FilePath $python -ArgumentList @('-m','reactor_agent.cli','worker') -WorkingDirectory $repo -PassThru -WindowStyle Hidden -RedirectStandardOutput 'logs\worker.out.log' -RedirectStandardError 'logs\worker.err.log'
Set-Content -Path 'data\worker.pid' -Value $workerProcess.Id
$mcpProcess = Start-Process -FilePath $python -ArgumentList @('-m','reactor_agent.cli','mcp') -WorkingDirectory $repo -PassThru -WindowStyle Hidden -RedirectStandardOutput 'logs\mcp.out.log' -RedirectStandardError 'logs\mcp.err.log'
Set-Content -Path 'data\mcp.pid' -Value $mcpProcess.Id
$port = 8501
if ($env:REACTOR_AGENT_UI_PORT) { $port = [int]$env:REACTOR_AGENT_UI_PORT }
$attempt = 0
while ($attempt -lt 20) {
    $existing = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $existing) { break }
    $port++
    $attempt++
}
if ($attempt -ge 20) { throw 'No available Streamlit port found in the configured range.' }
$process = Start-Process -FilePath $python -ArgumentList @('-m','streamlit','run','reactor_agent/ui.py','--server.address=127.0.0.1',"--server.port=$port",'--server.headless=true','--server.showEmailPrompt=false','--browser.gatherUsageStats=false') -WorkingDirectory $repo -PassThru -WindowStyle Hidden -RedirectStandardOutput 'logs\streamlit.out.log' -RedirectStandardError 'logs\streamlit.err.log'
Set-Content -Path 'data\streamlit.pid' -Value $process.Id
Write-Host "Streamlit started (PID $($process.Id)): http://127.0.0.1:$port"
Write-Host 'Logs: logs\streamlit.out.log and logs\streamlit.err.log. Run scripts/stop.ps1 to stop.'
