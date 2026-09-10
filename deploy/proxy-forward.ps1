param([Parameter(Mandatory=$true)][string]$Snapshot,
      [string]$Bind = '0.0.0.0', [string]$ContainerHost = 'host.docker.internal', [string]$ReadyFile = '')
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Net.Security;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
public sealed class PredictionProxyRelay : IDisposable {
    readonly TcpListener listener;
    readonly Uri upstream;
    readonly string expected, replacement;
    readonly SemaphoreSlim slots = new SemaphoreSlim(32);
    volatile bool stopped;
    public int Port { get { return ((IPEndPoint)listener.LocalEndpoint).Port; } }
    public PredictionProxyRelay(string bind, string target, string secret) {
        upstream = new Uri(target);
        if (upstream.Scheme != "http" && upstream.Scheme != "https") throw new Exception("Invalid upstream");
        using(var check = new TcpClient()) {
            if(!check.ConnectAsync(upstream.Host, upstream.Port).Wait(3000)) throw new Exception("Upstream unavailable");
        }
        expected = "Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes("relay:" + secret));
        if(upstream.UserInfo.Length > 0) replacement = "Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes(Uri.UnescapeDataString(upstream.UserInfo)));
        listener = new TcpListener(IPAddress.Parse(bind), 0);
        listener.Start();
        Task.Run((Action)Accept);
    }
    void Accept() {
        while(!stopped) {
            TcpClient client;
            try { client = listener.AcceptTcpClient(); } catch { break; }
            if(!slots.Wait(0)) { client.Close(); continue; }
            Task.Run(() => { try { Handle(client); } catch { } finally { client.Close(); slots.Release(); } });
        }
    }
    void Handle(TcpClient client) {
        client.ReceiveTimeout = client.SendTimeout = 3000;
        var input = client.GetStream();
        var data = new MemoryStream();
        DateTime deadline = DateTime.UtcNow.AddSeconds(3);
        string header = "";
        while(data.Length < 32768 && DateTime.UtcNow < deadline) {
            int b = input.ReadByte(); if(b < 0) return; data.WriteByte((byte)b);
            if(b == 10) { header = Encoding.ASCII.GetString(data.ToArray()); if(header.EndsWith("\r\n\r\n")) break; }
        }
        if(!header.EndsWith("\r\n\r\n")) return;
        string[] lines = header.Split(new string[]{"\r\n"}, StringSplitOptions.None);
        int found = 0; bool matched = false;
        var rewritten = new StringBuilder();
        foreach(string line in lines) {
            if(line.StartsWith("Proxy-Authorization:", StringComparison.OrdinalIgnoreCase)) {
                found++;
                string candidate = line.Substring(line.IndexOf(':')+1).Trim();
                int diff = candidate.Length ^ expected.Length;
                for(int i=0;i<Math.Min(candidate.Length,expected.Length);i++) diff |= candidate[i] ^ expected[i];
                matched = diff == 0;
                if(replacement != null) rewritten.Append("Proxy-Authorization: ").Append(replacement).Append("\r\n");
            } else rewritten.Append(line).Append("\r\n");
        }
        if(found != 1 || !matched) {
            byte[] denied = Encoding.ASCII.GetBytes("HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=relay\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            input.Write(denied,0,denied.Length); return;
        }
        using(var remote = new TcpClient()) {
            if(!remote.ConnectAsync(upstream.Host,upstream.Port).Wait(5000)) return;
            remote.ReceiveTimeout = remote.SendTimeout = 5000;
            Stream output = remote.GetStream();
            if(upstream.Scheme == "https") { var tls = new SslStream(output); tls.AuthenticateAsClient(upstream.Host); output = tls; }
            using(output) {
                // Split produced a trailing empty entry; remove its extra CRLF.
                byte[] request = Encoding.ASCII.GetBytes(rewritten.ToString(0, rewritten.Length-2));
                output.Write(request,0,request.Length);
                var a = input.CopyToAsync(output);
                var b = output.CopyToAsync(input);
                Task.WhenAny(a,b).Wait(TimeSpan.FromHours(1));
            }
        }
    }
    public void Dispose() { stopped = true; listener.Stop(); }
}
'@
$relay = $null
try {
    $settings = Get-Content -Raw $Snapshot | ConvertFrom-Json
    $capture = $settings.captured_at
    $random = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($random) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($random).Replace('+','-').Replace('/','_').TrimEnd('=')
    $relay = New-Object PredictionProxyRelay($Bind, $settings.host_proxy, $token)
    $settings.proxy = "http://relay:$token@${ContainerHost}:$($relay.Port)"
    $settings.status = 'proxy'; $settings.error = ''; $settings.needs_forwarding = $false
    $settings.source += ' (host script forwarding)'
    $settings | Add-Member -NotePropertyName forwarded -NotePropertyValue $true -Force
    $temporary = $Snapshot + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($settings | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    Move-Item -Force $temporary $Snapshot
    if ($ReadyFile) { [IO.File]::WriteAllText($ReadyFile, 'ready') }
    Write-Host 'Host proxy forwarding ready (authenticated, upstream fixed).'
    $started = [DateTime]::UtcNow
    while ($true) {
        Start-Sleep -Seconds 1
        $current = Get-Content -Raw $Snapshot | ConvertFrom-Json
        if ($current.captured_at -ne $capture) { break }
        if ($current.container_id) {
            $state = & docker inspect --format '{{.State.Running}}' $current.container_id 2>$null
            if ($LASTEXITCODE -ne 0 -or $state -ne 'true') { break }
        } elseif (([DateTime]::UtcNow - $started).TotalSeconds -gt 300) { break }
    }
} catch {
    Write-Host 'Host proxy forwarding failed; check connectivity/binding and restart the launcher.'
    exit 1
} finally {
    if ($null -ne $relay) { $relay.Dispose() }
}
