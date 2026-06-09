param(
    [switch]$Fake,
    [switch]$Native,
    [switch]$NoOpen,
    [int]$ApiPort = 8765,
    [int]$WebPort = 4173
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$DataDir = Join-Path $Root "data"
$LogDir = Join-Path $DataDir "logs"
$PidFile = Join-Path $DataDir "docvec-gui.pids.json"

New-Item -ItemType Directory -Force -Path $DataDir, $LogDir | Out-Null

if (-not $env:DOCVEC_OLLAMA_BATCH_SIZE) {
    $env:DOCVEC_OLLAMA_BATCH_SIZE = "32"
}

function Stop-ExistingProcess {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return
    }

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $ProcessId -Force
    }
}

function Stop-PreviousDocVec {
    if (-not (Test-Path $PidFile)) {
        return
    }

    try {
        $state = Get-Content -Raw $PidFile | ConvertFrom-Json
        Stop-ExistingProcess ([int]$state.AppPid)
        Stop-ExistingProcess ([int]$state.WebPid)
        Stop-ExistingProcess ([int]$state.ApiPid)
    }
    finally {
        Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
    }
}

function Wait-Http {
    param([string]$Uri)

    for ($i = 0; $i -lt 30; $i++) {
        & curl.exe -fsS $Uri *> $null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    return $false
}

function Find-Browser {
    $candidates = @(
        Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe",
        Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe",
        Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe",
        Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    return $null
}

Stop-PreviousDocVec

$dbPath = Join-Path $DataDir "docvec.sqlite"
$vectorPath = Join-Path $DataDir "vectors.tvim"
$apiArgs = @(
    "serve",
    "--host", "127.0.0.1",
    "--port", "$ApiPort",
    "--db-path", $dbPath,
    "--vector-path", $vectorPath
)

if ($Fake) {
    $apiArgs += "--fake"
}

$api = Start-Process `
    -FilePath "docvec" `
    -ArgumentList $apiArgs `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "api.out.log") `
    -RedirectStandardError (Join-Path $LogDir "api.err.log") `
    -PassThru

if ($Native) {
    $nativeExe = Join-Path $Root "src-ui\src-tauri\target\release\docvec-desktop.exe"
    if (-not (Test-Path $nativeExe)) {
        throw "Native DocVec executable not found: $nativeExe. Run 'npm run tauri:build' from src-ui first."
    }

    if (-not (Wait-Http "http://127.0.0.1:$ApiPort/status")) {
        throw "DocVec API did not become ready on http://127.0.0.1:$ApiPort/status"
    }

    $app = $null
    if (-not $NoOpen) {
        $app = Start-Process `
            -FilePath $nativeExe `
            -WorkingDirectory $Root `
            -PassThru
    }

    [PSCustomObject]@{
        ApiPid = $api.Id
        WebPid = 0
        AppPid = if ($app) { $app.Id } else { 0 }
        ApiPort = $ApiPort
        WebPort = 0
        Fake = [bool]$Fake
        Mode = "native"
        StartedAt = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

    return
}

$viteBin = Join-Path $Root "src-ui\node_modules\vite\bin\vite.js"
$web = Start-Process `
    -FilePath "node.exe" `
    -ArgumentList @($viteBin, "preview", "--host", "127.0.0.1", "--port", "$WebPort") `
    -WorkingDirectory (Join-Path $Root "src-ui") `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "web.out.log") `
    -RedirectStandardError (Join-Path $LogDir "web.err.log") `
    -PassThru

[PSCustomObject]@{
    ApiPid = $api.Id
    WebPid = $web.Id
    AppPid = 0
    ApiPort = $ApiPort
    WebPort = $WebPort
    Fake = [bool]$Fake
    Mode = "web"
    StartedAt = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

Wait-Http "http://127.0.0.1:$ApiPort/status" | Out-Null
Wait-Http "http://127.0.0.1:$WebPort" | Out-Null

$url = "http://127.0.0.1:$WebPort"
if ($NoOpen) {
    return
}

$browser = Find-Browser
if ($browser) {
    Start-Process -FilePath $browser -ArgumentList @("--app=$url")
}
else {
    Start-Process $url
}
