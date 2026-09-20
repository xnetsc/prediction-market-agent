from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from prediction_market_agent.agent.decision import DecisionProviderError


def subprocess_environment(proxy: str, no_proxy: str = "localhost,127.0.0.1,::1") -> dict[str, str]:
    """Pass runtime necessities without leaking unrelated plugin configuration."""
    exact = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TERM",
        "COLORTERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
    }
    env = {
        name: value
        for name, value in os.environ.items()
        if name in exact or name.startswith("LC_")
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if proxy:
            env[name] = proxy
        else:
            env.pop(name, None)
    env["NO_PROXY"] = env["no_proxy"] = no_proxy
    return env


def resolve_executable(configured: str) -> str:
    if "/" in configured:
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise DecisionProviderError(f"Executable is unavailable: {configured}")
    resolved = shutil.which(configured)
    if not resolved:
        raise DecisionProviderError(f"Executable is not on PATH: {configured}")
    return resolved


def subprocess_output_text(value: str | bytes | None) -> str:
    """Normalize TimeoutExpired output; Python may return bytes despite text=True."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def configured_client_homes(working_directory: Path) -> dict[str, Path]:
    """Resolve both clients' private data roots from their plugin configurations."""
    result: dict[str, Path] = {}
    for client, hidden in (("codex", ".codex"), ("claude", ".claude")):
        path = working_directory / "config" / "plugins" / f"{client}.json"
        values: dict[str, object] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                values = loaded
        except (OSError, ValueError, TypeError):
            pass
        configured = Path(
            str(values.get(f"{client.upper()}_AUTH_DIRECTORY") or f"credentials/{client}")
        ).expanduser()
        auth_home = configured if configured.is_absolute() else working_directory / configured
        result[client.upper()] = (auth_home.resolve() / hidden).resolve()
    return result


def client_subprocess_environment(
    client: str,
    private_home: Path,
    proxy: str,
    no_proxy: str = "localhost,127.0.0.1,::1",
) -> dict[str, str]:
    """Use the selected client's configured private HOME, including for OpenRouter runs."""
    name = client.strip().upper()
    if name not in {"CODEX", "CLAUDE"}:
        raise ValueError("client must be CODEX or CLAUDE")
    private_home = private_home.resolve()
    auth_home = private_home.parent
    auth_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = subprocess_environment(proxy, no_proxy)
    env["HOME"] = str(auth_home)
    env.pop("CODEX_HOME", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env["CODEX_HOME" if name == "CODEX" else "CLAUDE_CONFIG_DIR"] = str(
        private_home
    )
    return env


def shared_agent_history_instruction(
    database: Path | None,
    client_homes: dict[str, Path] | None = None,
) -> str:
    """Tell either official CLI where both clients' full records live.

    These are pointers, not application-produced summaries.  The active CLI decides what to read,
    how much to retain, and whether an older record is relevant to the current round.
    """
    if database is None:
        return ""
    configured = client_homes or {
        "CODEX": Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
        "CLAUDE": Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        ),
    }

    def roots(client: str, environment_name: str, hidden: str) -> tuple[Path, ...]:
        primary = Path(configured[client]).resolve()
        legacy = Path(os.environ.get(environment_name, str(Path.home() / hidden))).resolve()
        return (primary, legacy) if legacy != primary and legacy.exists() else (primary,)

    codex_roots = roots("CODEX", "CODEX_HOME", ".codex")
    claude_roots = roots("CLAUDE", "CLAUDE_CONFIG_DIR", ".claude")
    codex_locations = ", ".join(str(path) for path in codex_roots)
    claude_locations = ", ".join(str(path) for path in claude_roots)
    return (
        "\n\nSHARED_AGENT_HISTORY:\n"
        f"This is a new round. The provider-neutral application history is the read-only SQLite "
        f"database at {database.resolve()}. Relevant tables are decision_ledger (rounds and "
        "outcomes), provider_turns (model inputs/outputs), agent_steps (business tool calls and "
        "results), execution_actions, operator_instructions, topic_observations, and "
        "discovery_selections. The complete private Codex data roots are "
        f"{codex_locations}; inspect their sessions, archived sessions, and client index/state "
        "databases when present. The complete private Claude data roots are "
        f"{claude_locations}; inspect their projects, sessions, and history index when present. "
        "You may use your own shell and file tools to read "
        "records from either client. Decide yourself which files and rows matter and how much "
        "context to retain. Do not modify any history database or private conversation record."
    )
