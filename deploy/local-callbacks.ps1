param([Parameter(Mandatory=$true)][string]$Container, [int]$ApiPort = 8765, [string]$ReadyFile = "")
$ErrorActionPreference = "Stop"
. "$PSScriptRoot/ensure-docker.ps1"
$details = (Invoke-Docker inspect $Container | ConvertFrom-Json)[0]
$robot = $details.Id
if ($robot -notmatch '^[a-f0-9]{64}$') { throw "Invalid container ID" }
$network = @($details.NetworkSettings.Networks.PSObject.Properties)[0]
$target = $network.Value.IPAddress
$prefix = "prediction-callback-" + $robot.Substring(0, 12)
$label = "org.prediction-agent.callback-owner=$robot"
$transport = Join-Path $PSScriptRoot "local-callbacks.py"
try {
    # Feed the deployment script through stdin; no host Python is required.
    Get-Content -Raw $transport | & docker exec -i $robot python -u - watch --api-port $ApiPort | ForEach-Object {
        if ($_ -eq 'READY 0 0') {
            if ($ReadyFile) { Set-Content $ReadyFile 'ready' }
            return
        }
        if ($_ -notmatch '^(ADD|REMOVE) ([0-9]+) ([0-9]+)$') { throw "Invalid callback event" }
        $action, $port, $lifetime = $Matches[1], [int]$Matches[2], [int]$Matches[3]
        if ($port -lt 1024 -or $port -gt 65535) { throw "Invalid callback port" }
        $name = "$prefix-$port"
        if ($action -eq "ADD") {
            $mapping = & docker port $robot "$port/tcp" 2>$null
            if ($mapping) { return }
            try {
                Invoke-Docker run -d --rm --name $name --label $label --network $network.Name `
                    -p "127.0.0.1:${port}:$port" --read-only --cap-drop ALL --security-opt no-new-privileges `
                    --memory 64m --pids-limit 32 --entrypoint python `
                    --mount "type=bind,source=$transport,target=/transport.py,readonly" `
                    $details.Image /transport.py tunnel --target $target --port $port --lifetime $lifetime | Out-Null
                Write-Host "Published local callback port $port"
            } catch {
                Write-Warning "Callback port $port could not be published; check for a local port conflict and retry login."
            }
        } else {
            $owner = & docker inspect --format '{{index .Config.Labels "org.prediction-agent.callback-owner"}}' $name 2>$null
            if ($owner -eq $robot) {
                & docker stop -t 5 $name 2>$null | Out-Null
                if ($LASTEXITCODE -ne 0) { & docker rm $name | Out-Null }
            }
        }
    }
} finally {
    & docker ps -aq --filter "label=$label" | ForEach-Object { & docker rm -f $_ | Out-Null }
}
