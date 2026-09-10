$ErrorActionPreference = "Stop"

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed with exit code $LASTEXITCODE" }
}

function Find-DockerDesktop {
    foreach ($root in @("$env:ProgramFiles/Docker/Docker", "$env:LOCALAPPDATA/Programs/DockerDesktop")) {
        if (Test-Path "$root/Docker Desktop.exe") { return $root }
    }
    return $null
}

function Ensure-Docker {
    $desktop = Find-DockerDesktop
    if ($desktop) { $env:PATH = "$desktop/resources/bin;$env:PATH" }
    $dockerFound = [bool](Get-Command docker -ErrorAction SilentlyContinue)
    $engineReady = $false
    if ($dockerFound) {
        & docker info *> $null
        $engineReady = $LASTEXITCODE -eq 0
    }
    if (-not $dockerFound -or (-not $engineReady -and -not $desktop)) {
        Write-Host "Docker not found. Downloading its official installer…"
        $architecture = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
        $installer = Join-Path ([IO.Path]::GetTempPath()) ("docker-" + [guid]::NewGuid() + ".exe")
        Invoke-WebRequest "https://desktop.docker.com/win/main/$architecture/Docker%20Desktop%20Installer.exe" -OutFile $installer
        $signature = Get-AuthenticodeSignature $installer
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Docker") {
            throw "The Docker installer does not have a valid Docker signature."
        }
        $process = Start-Process $installer -ArgumentList "install" -Wait -PassThru
        if ($process.ExitCode -eq 3010) { throw "Docker installed. Restart Windows and rerun start-local.ps1." }
        if ($process.ExitCode -ne 0) { throw "Docker installation failed: $($process.ExitCode)" }
        $desktop = Find-DockerDesktop
        if (-not $desktop) { throw "Docker installation did not produce Docker Desktop." }
        $env:PATH = "$desktop/resources/bin;$env:PATH"
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        $desktop = Find-DockerDesktop
        if (-not $desktop) { throw "Docker CLI exists but no running engine or Docker Desktop installation was found." }
        Write-Host "Starting Docker Desktop. Complete first-run setup or any requested WSL restart."
        Start-Process "$desktop/Docker Desktop.exe"
    }
    for ($attempt = 0; $attempt -lt 150; $attempt++) {
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            Invoke-Docker compose version
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Docker is not ready. Complete Desktop setup or restart Windows, then rerun this script."
}
