$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $Root "data\docvec-gui.pids.json"

function Stop-TrackedProcess {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force
    }
}

if (Test-Path $PidFile) {
    $state = Get-Content -Raw $PidFile | ConvertFrom-Json
    Stop-TrackedProcess ([int]$state.AppPid)
    Stop-TrackedProcess ([int]$state.WebPid)
    Stop-TrackedProcess ([int]$state.ApiPid)
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}
