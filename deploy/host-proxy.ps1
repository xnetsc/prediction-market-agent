# Uses the current user's Windows Internet Settings (shared by system-proxy browsers).
$ErrorActionPreference = 'Stop'
function Write-ProxyFrame([string]$Name, [string]$Value) {
    $Name + "`t" + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}
Write-ProxyFrame platform Windows
foreach ($pair in @(@('env_https', 'https_proxy'), @('env_http', 'http_proxy'), @('env_all', 'all_proxy'), @('env_no', 'no_proxy'))) {
    Write-ProxyFrame $pair[0] ([Environment]::GetEnvironmentVariable($pair[1]))
}
$settings = Get-ItemProperty 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Internet Settings' -ErrorAction SilentlyContinue
$values = @{
    enabled = [bool]$settings.ProxyEnable
    server = [string]$settings.ProxyServer
    bypass = [string]$settings.ProxyOverride
    pac = [string]$settings.AutoConfigURL
    auto_detect = [bool]$settings.AutoDetect
}
Write-ProxyFrame windows ($values | ConvertTo-Json -Compress)
Write-ProxyFrame complete yes
