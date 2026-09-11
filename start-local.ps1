$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. "$PSScriptRoot/deploy/ensure-docker.ps1"
Ensure-Docker
$image = if ($env:PREDICTION_AGENT_IMAGE) { $env:PREDICTION_AGENT_IMAGE } else { 'ghcr.io/xnetsc/prediction-market-agent:latest' }
# Pull through the registry preflight when a host interpreter is available, so a machine that can
# only reach the registry through a proxy still works without touching Docker's own configuration.
$python = @('python', 'python3', 'py') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if ($python) {
    & $python "$PSScriptRoot/deploy/registry-pull.py" $image
    if ($LASTEXITCODE -ne 0) { throw "Could not obtain $image" }
} else {
    Write-Warning 'No Python on PATH; pulling directly without the registry preflight.'
    Invoke-Docker compose -f "$PSScriptRoot/compose.yaml" pull
}
& "$PSScriptRoot/deploy/prepare-host-proxy.ps1" -Image $image
Invoke-Docker compose -f "$PSScriptRoot/compose.yaml" up -d --no-build --wait --wait-timeout 180
$robot = (Invoke-Docker compose -f "$PSScriptRoot/compose.yaml" ps -q robot).Trim()
if ($robot -notmatch '^[a-f0-9]+$') { throw "Cannot identify the running robot container" }
& "$PSScriptRoot/deploy/prepare-host-proxy.ps1" -Image $image -AttachContainer $robot
$dataDirectory = Join-Path $PSScriptRoot "runtime-data"
New-Item -ItemType Directory -Force $dataDirectory | Out-Null
$pidFile = Join-Path $dataDirectory "local-callbacks-$robot.pid"
$existing = if (Test-Path $pidFile) { Get-Process -Id ([int](Get-Content $pidFile)) -ErrorAction SilentlyContinue } else { $null }
if (-not $existing) {
    $readyFile = Join-Path $dataDirectory "local-callbacks-$robot.ready"
    Set-Content $readyFile '' -NoNewline
    $shellExecutable = (Get-Process -Id $PID).Path
    $worker = Start-Process $shellExecutable -ArgumentList @('-NoProfile', '-File', "`"$PSScriptRoot/deploy/local-callbacks.ps1`"", '-Container', $robot, '-ReadyFile', "`"$readyFile`"") `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput "$dataDirectory/local-callbacks.log" `
        -RedirectStandardError "$dataDirectory/local-callbacks.errors.log"
    Set-Content $pidFile $worker.Id
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Get-Item $readyFile).Length -gt 0) { break }
        if ($worker.HasExited -or $attempt -eq 19) { throw "Local callback forwarding failed to start. See runtime-data/local-callbacks.errors.log" }
        Start-Sleep -Seconds 1
    }
}
$port = if ($env:PREDICTION_AGENT_PORT) { $env:PREDICTION_AGENT_PORT } else { "8765" }
Write-Host "Robot is ready: http://127.0.0.1:$port"
Write-Host "Local client callback forwarding is managed automatically. Log: runtime-data/local-callbacks.log"
