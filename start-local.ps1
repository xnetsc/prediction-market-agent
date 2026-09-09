$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv/Scripts/python.exe")) {
  python -m venv .venv
}
& .venv/Scripts/python.exe -m pip install --index-url https://pypi.org/simple .
if (-not (Test-Path "config/application.json")) {
  & .venv/Scripts/prediction-market-agent.exe init
}
& .venv/Scripts/prediction-market-agent.exe serve
