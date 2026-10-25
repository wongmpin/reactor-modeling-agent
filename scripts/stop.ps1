$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
foreach ($serviceName in @('streamlit','worker','mcp')) {
$pidFile = Join-Path $repo "data\$serviceName.pid"
if (Test-Path $pidFile) {
    $appPid = [int](Get-Content $pidFile -Raw)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$appPid" -ErrorAction SilentlyContinue
    $expectedPython = Join-Path $repo '.venv\Scripts\python.exe'
    if ($process -and $process.ExecutablePath -eq $expectedPython -and (($serviceName -eq 'streamlit' -and $process.CommandLine -like '*streamlit*reactor_agent/ui.py*') -or ($serviceName -ne 'streamlit' -and $process.CommandLine -like "*reactor_agent.cli*$serviceName*"))) {
        # Windows venv launchers spawn a second Python process. Stop only this app's descendants.
        $allProcesses = @(Get-CimInstance Win32_Process)
        $ownedIds = [System.Collections.Generic.List[int]]::new()
        $ownedIds.Add($appPid)
        for ($index = 0; $index -lt $ownedIds.Count; $index++) {
            foreach ($child in $allProcesses) {
                if ($child.ParentProcessId -eq $ownedIds[$index] -and -not $ownedIds.Contains([int]$child.ProcessId)) {
                    $ownedIds.Add([int]$child.ProcessId)
                }
            }
        }
        for ($index = $ownedIds.Count - 1; $index -ge 0; $index--) {
            Stop-Process -Id $ownedIds[$index] -Force -ErrorAction SilentlyContinue
        }
    } elseif ($process) {
        Write-Warning 'Saved PID no longer identifies this project Streamlit process; it was not stopped.'
    }
    Remove-Item -LiteralPath $pidFile -Force
}
 }
Write-Host 'Stopped project Streamlit, queue consumer and MCP processes. HYSYS application remains open.'
