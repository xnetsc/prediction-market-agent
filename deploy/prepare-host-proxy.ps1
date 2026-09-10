param([Parameter(Mandatory=$true)][string]$Image, [string]$AttachContainer = '')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/ensure-docker.ps1"
$privateDirectory = Join-Path (Split-Path $PSScriptRoot) 'runtime-data/.deployment'
New-Item -ItemType Directory -Force $privateDirectory | Out-Null
function Invoke-ProxyPrepare {
    $testUrl = if ($env:PREDICTION_AGENT_PROXY_TEST_URL) { $env:PREDICTION_AGENT_PROXY_TEST_URL } else { 'https://api.ipify.org?format=json' }
    $dockerArguments = @('run', '--rm', '-i', '--add-host', 'host.proxy.internal:host-gateway', '--entrypoint', 'python',
        '--mount', "type=bind,source=$privateDirectory,target=/deployment",
        '--mount', "type=bind,source=$PSScriptRoot/host-proxy.py,target=/host-proxy.py,readonly",
        $Image, '/host-proxy.py', '--output', '/deployment/host-proxy.json', '--test-url', $testUrl) + $args
    $input | & docker @dockerArguments
    if ($LASTEXITCODE -ne 0) { throw 'Host proxy capture failed; application not started' }
}
if ($AttachContainer) { Invoke-ProxyPrepare --attach-container $AttachContainer; return }
$capture = & "$PSScriptRoot/host-proxy.ps1" | Invoke-ProxyPrepare
$capture | Write-Host
if ($capture -contains 'Forwarding required: yes') {
    $ready = Join-Path $privateDirectory 'proxy-forward.ready'
    Set-Content $ready '' -NoNewline
    $executable = (Get-Process -Id $PID).Path
    $arguments = @('-NoProfile', '-File', ('"' + "$PSScriptRoot/proxy-forward.ps1" + '"'),
        '-Snapshot', ('"' + "$privateDirectory/host-proxy.json" + '"'), '-ReadyFile', ('"' + $ready + '"'))
    $options = @{ ArgumentList = $arguments; PassThru = $true; WindowStyle = 'Hidden'
        RedirectStandardOutput = "$privateDirectory/proxy-forward.log"
        RedirectStandardError = "$privateDirectory/proxy-forward.errors.log" }
    $worker = Start-Process $executable @options
    Set-Content (Join-Path $privateDirectory 'proxy-forward.pid') $worker.Id
    for ($attempt=0; $attempt -lt 15; $attempt++) {
        if ((Get-Item $ready).Length -gt 0) { break }
        if ($worker.HasExited -or $attempt -eq 14) {
            Write-Warning 'Host forwarding failed; inspect runtime-data/.deployment/proxy-forward.errors.log. HOST clients remain unavailable.'
            break
        }
        Start-Sleep -Seconds 1
    }
}
$verification = @(Invoke-ProxyPrepare --verify)
$verification | Write-Host
if ($verification -contains 'Proxy validation failed: yes') {
    $action = $env:PREDICTION_AGENT_PROXY_FAILURE_ACTION
    if (-not $action) {
        if (-not [Environment]::UserInteractive) {
            throw 'Detected proxy failed container HTTPS validation; set PREDICTION_AGENT_PROXY_FAILURE_ACTION=direct to explicitly continue without it'
        }
        Write-Host '检测到了系统代理，但容器无法通过它完成 HTTPS 请求。'
        $action = Read-Host '输入 1 不使用代理并继续，输入 2 退出 [1/2]'
    }
    if ($action -in @('1', 'direct', 'DIRECT')) {
        Invoke-ProxyPrepare --force-direct | Write-Host
        Write-Host '已按用户选择改为直连。'
    } else {
        throw '代理不可用，已退出且未启动机器人'
    }
}
