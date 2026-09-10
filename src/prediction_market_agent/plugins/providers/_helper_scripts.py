"""Render one-flow terminal scripts without distributing native executables."""
from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path


def render_script(platform: str, config: dict) -> str:
    root = Path(__file__).parent
    encoded = base64.b64encode(json.dumps(config).encode()).decode()
    if platform == "bash":
        template = (root / "login_scripts/helper.sh").read_text(encoding="utf-8")
        source = base64.b64encode((root / "_login_helper.py").read_bytes()).decode()
        return template.replace("@@CONFIG@@", encoded).replace("@@PYTHON@@", source)
    if platform == "powershell":
        return (root / "login_scripts/helper.ps1").read_text(encoding="utf-8").replace("@@CONFIG@@", encoded)
    raise ValueError("Unsupported helper shell")


def command_for(url: str, token: str, platform: str, digest: str) -> str:
    header = "Authorization: Bearer " + token
    if platform == "bash":
        verify = ("import hashlib,sys; data=sys.stdin.buffer.read(); "
                  "sys.exit('Script checksum mismatch; restart login') if hashlib.sha256(data).hexdigest() != "
                  + repr(digest) + " else sys.stdout.buffer.write(data)")
        # No script bytes enter shell expansion or text substitution. Nothing is emitted until verified.
        return ("(set -o pipefail; command -v python3 >/dev/null || { echo 'Python 3 is required'; exit 1; }; "
                "curl --fail --silent --show-error --max-time 45 --header "
                + shlex.quote(header) + " " + shlex.quote(url)
                + " | python3 -c " + shlex.quote(verify) + " | bash)")
    if platform == "powershell":
        quote = lambda s: "'" + s.replace("'", "''") + "'"
        return ("& { $ErrorActionPreference = 'Stop'; $script = Invoke-WebRequest -UseBasicParsing "
                "-MaximumRedirection 0 -TimeoutSec 45 -Headers @{ Authorization = "
                + quote("Bearer " + token) + " } -Uri " + quote(url)
                + "; $buffer = New-Object IO.MemoryStream; $script.RawContentStream.CopyTo($buffer); "
                "$bytes = $buffer.ToArray(); $buffer.Dispose(); $sha = [Security.Cryptography.SHA256]::Create(); "
                "try { $hash = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() } "
                "finally { $sha.Dispose() }; if ($hash -ne " + quote(digest) + ") { throw 'Script checksum mismatch; restart login' }; "
                "$utf8 = New-Object Text.UTF8Encoding($false, $true); & ([scriptblock]::Create($utf8.GetString($bytes))) }")
    raise ValueError("Unsupported helper shell")
