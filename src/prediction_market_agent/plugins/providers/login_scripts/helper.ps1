# One-flow Windows PowerShell 5.1+ helper; no execution-policy changes or native downloads.
& {
    $ErrorActionPreference = 'Stop'
    $config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@CONFIG@@')) | ConvertFrom-Json
    $utf8 = [Text.Encoding]::UTF8
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $sha = [Security.Cryptography.SHA512]::Create()
    $keys = $sha.ComputeHash($utf8.GetBytes("prediction-login-v1" + [char]0 + $config.secret))
    $sha.Dispose()
    $encryptionKey = [byte[]]$keys[0..31]
    $macKey = [byte[]]$keys[32..63]
    $listener = $null
    $done = $false
    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    $deadline = [Math]::Min([double]$config.expires_at, [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + 900)

    function Get-Mac([byte[]]$data) {
        $hmac = New-Object Security.Cryptography.HMACSHA256
        try { $hmac.Key = $macKey; return ,$hmac.ComputeHash($data) } finally { $hmac.Dispose() }
    }
    function Seal-Message($payload) {
        $iv = New-Object byte[] 16
        $rng.GetBytes($iv)
        $aes = [Security.Cryptography.Aes]::Create()
        try {
            $aes.Key = $encryptionKey; $aes.IV = $iv
            $aes.Mode = [Security.Cryptography.CipherMode]::CBC
            $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
            $plain = $utf8.GetBytes(($payload | ConvertTo-Json -Compress))
            $transform = $aes.CreateEncryptor()
            try { $cipher = $transform.TransformFinalBlock($plain, 0, $plain.Length) } finally { $transform.Dispose() }
            $tag = Get-Mac ([byte[]]($utf8.GetBytes('helper-request') + $iv + $cipher))
            return @{ algorithm = 'AES-CBC-HMAC-SHA256'; nonce = [Convert]::ToBase64String($iv)
                ciphertext = [Convert]::ToBase64String($cipher); mac = [Convert]::ToBase64String($tag) }
        } finally { $aes.Dispose() }
    }
    function Open-Message($envelope) {
        if ($envelope.algorithm -ne 'AES-CBC-HMAC-SHA256') { throw 'Unexpected encryption protocol' }
        $iv = [Convert]::FromBase64String($envelope.nonce)
        $cipher = [Convert]::FromBase64String($envelope.ciphertext)
        $tag = [Convert]::FromBase64String($envelope.mac)
        if ($iv.Length -ne 16 -or $tag.Length -ne 32) { throw 'Invalid envelope' }
        $expected = Get-Mac ([byte[]]($utf8.GetBytes('helper-response') + $iv + $cipher))
        $different = 0
        for ($i = 0; $i -lt 32; $i++) { $different = $different -bor ($tag[$i] -bxor $expected[$i]) }
        if ($different -ne 0 -or -not $seen.Add([string]$envelope.nonce)) { throw 'Response authentication failed' }
        $aes = [Security.Cryptography.Aes]::Create()
        try {
            $aes.Key = $encryptionKey; $aes.IV = $iv
            $aes.Mode = [Security.Cryptography.CipherMode]::CBC
            $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
            $transform = $aes.CreateDecryptor()
            try { $plain = $transform.TransformFinalBlock($cipher, 0, $cipher.Length) } finally { $transform.Dispose() }
            return ($utf8.GetString($plain) | ConvertFrom-Json)
        } finally { $aes.Dispose() }
    }
    function Exchange-Message($payload) {
        if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge $deadline) { throw 'Login expired' }
        $request = [Net.HttpWebRequest]::Create($config.endpoint)
        $request.Method = 'POST'; $request.ContentType = 'application/json'
        $request.Headers['Authorization'] = 'Bearer ' + $config.secret
        $request.AllowAutoRedirect = $false
        $request.Timeout = 40000; $request.ReadWriteTimeout = 40000
        $body = $utf8.GetBytes(((Seal-Message $payload) | ConvertTo-Json -Compress))
        $request.ContentLength = $body.Length
        $stream = $request.GetRequestStream()
        try { $stream.Write($body, 0, $body.Length) } finally { $stream.Dispose() }
        $response = $request.GetResponse()
        try {
            if ([int]$response.StatusCode -ne 200) { throw 'Pairing rejected' }
            $reader = New-Object IO.StreamReader($response.GetResponseStream())
            try {
                $buffer = New-Object char[] 65537
                $count = $reader.ReadBlock($buffer, 0, $buffer.Length)
                if ($count -ge $buffer.Length) { throw 'Oversized response' }
                $json = -join $buffer[0..($count - 1)]
                return Open-Message ($json | ConvertFrom-Json)
            } finally { $reader.Dispose() }
        } finally { $response.Dispose() }
    }
    function Reply-Local($stream, [int]$status, [string]$body) {
        $bytes = $utf8.GetBytes($body)
        $head = $utf8.GetBytes("HTTP/1.1 $status Result\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: $($bytes.Length)\r\n\r\n".Replace('\r', "`r").Replace('\n', "`n"))
        $stream.Write($head, 0, $head.Length); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush()
    }
    try {
        $endpoint = [Uri]$config.endpoint
        if (($endpoint.Scheme -ne 'https' -and -not ($endpoint.Scheme -eq 'http' -and $endpoint.Host -in @('localhost', '127.0.0.1'))) -or $endpoint.UserInfo -or $endpoint.Fragment -or $endpoint.Query -or -not $endpoint.AbsolutePath.StartsWith('/api/plugin-helper/')) {
            throw 'Invalid management endpoint; remote access requires HTTPS'
        }
        Write-Host ('Login relay connects only to: ' + $endpoint.Authority)
        do {
            $state = Exchange-Message @{ action = 'poll' }
            if ($state.completed) { return }
            if (-not $state.redirect_uri) { Start-Sleep -Seconds 1 }
        } while (-not $state.redirect_uri)
        $redirect = [string]$state.redirect_uri
        $callback = [Uri]$redirect
        if ($callback.Scheme -ne 'http' -or $callback.Host -notin @('localhost', '127.0.0.1') -or $callback.Port -lt 1 -or $callback.UserInfo -or $callback.Query -or $callback.Fragment -or $callback.AbsolutePath -notin @('/callback', '/auth/callback')) {
            throw 'Invalid callback address'
        }
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $callback.Port)
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        $null = Exchange-Message @{ action = 'ready'; redirect_uri = $redirect }
        Write-Host 'Ready. Return to the web wizard and open the official login page. Keep this terminal open.'
        $lastPoll = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        while (-not $done -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -lt $deadline) {
            if ($listener.Pending()) {
                $client = $listener.AcceptTcpClient()
                try {
                    $client.ReceiveTimeout = 3000; $client.SendTimeout = 3000
                    $stream = $client.GetStream()
                    # Bound the entire header, including clients that never send a newline.
                    $bytes = New-Object 'System.Collections.Generic.List[byte]'
                    $headerDeadline = [DateTime]::UtcNow.AddSeconds(3)
                    while ($bytes.Count -lt 20000 -and [DateTime]::UtcNow -lt $headerDeadline) {
                        $next = $stream.ReadByte()
                        if ($next -lt 0) { break }
                        $bytes.Add([byte]$next)
                        if ($bytes.Count -ge 4 -and ($utf8.GetString($bytes.GetRange($bytes.Count - 4, 4).ToArray())) -eq "`r`n`r`n") { break }
                    }
                    $header = $utf8.GetString($bytes.ToArray())
                    $lines = $header -split "`r`n"
                    $first = $lines[0] -split ' '
                    $hostLine = @($lines | Where-Object { $_ -match '^Host:' })
                    $hostValue = if ($hostLine.Count -eq 1) { $hostLine[0].Substring(5).Trim() } else { '' }
                    if ($bytes.Count -ge 20000 -or -not $header.EndsWith("`r`n`r`n") -or $first.Length -ne 3 -or $first[0] -ne 'GET' -or $hostValue -notin @("localhost:$($callback.Port)", "127.0.0.1:$($callback.Port)")) {
                        Reply-Local $stream 400 'Invalid callback request'
                    } else {
                        $parts = $first[1] -split '\?', 2
                        if ($parts[0] -ne $callback.AbsolutePath -or $parts.Length -ne 2 -or $parts[1].Length -gt 16384) {
                            Reply-Local $stream 404 'Unknown callback route'
                        } else {
                            $null = Exchange-Message @{ action = 'callback'; query = $parts[1] }
                            $done = $true
                            Reply-Local $stream 200 'Authorization delivered. Return to the web wizard to check login status.'
                        }
                    }
                } finally { $client.Close() }
            } else { Start-Sleep -Milliseconds 100 }
            if (-not $done -and [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $lastPoll) {
                $state = Exchange-Message @{ action = 'poll' }
                if ($state.completed -or $state.redirect_uri -ne $redirect) { throw 'Login flow ended' }
                $lastPoll = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            }
        }
        if (-not $done) { throw 'Login expired' }
        Write-Host 'Callback delivered. Confirm login success in the web wizard.'
    } catch {
        # Never print exception URLs, raw request headers, pairing tokens or OAuth query strings.
        Write-Host 'Login failed or expired. Check the network/port and restart in the web wizard.'
        throw 'Login helper stopped'
    } finally {
        if ($null -ne $listener) { $listener.Stop() }
        $rng.Dispose()
    }
}
