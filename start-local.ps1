$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. "$PSScriptRoot/deploy/ensure-docker.ps1"
Ensure-Docker
Invoke-Docker compose -f "$PSScriptRoot/compose.yaml" pull
Invoke-Docker compose -f "$PSScriptRoot/compose.yaml" up -d --no-build --wait --wait-timeout 180
$port = if ($env:PREDICTION_AGENT_PORT) { $env:PREDICTION_AGENT_PORT } else { "8765" }
Write-Host "Robot is ready: http://127.0.0.1:$port"
