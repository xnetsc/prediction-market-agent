from __future__ import annotations

import base64
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from prediction_market_agent.plugins.providers._client_control import (
    CREDENTIAL_BUNDLE_KIND,
    CREDENTIAL_FILES,
    ClientControl,
)


class _Configuration:
    """Minimal stand-in for the plugin's private configuration."""

    def __init__(self, values: dict[str, str]):
        self._values = values

    def load(self) -> dict[str, str]:
        return dict(self._values)


def _control(root: Path, name: str) -> ClientControl:
    prefix = name.upper()
    configuration = _Configuration(
        {
            f"{prefix}_AUTH_DIRECTORY": f"credentials/{name}",
            f"{prefix}_CLIENT_DIRECTORY": f"clients/{name}",
            f"{prefix}_HTTP_PROXY": "DIRECT",
            f"{prefix}_HOST_PROXY_FILE": ".deployment/host-proxy.json",
        }
    )
    control = ClientControl(name, f"@example/{name}", configuration, root)
    # The session-restoring paths are all this test drives; nothing here starts the real client.
    control.inspect = lambda: None
    control.cancel = lambda: None
    return control


class CredentialPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write_session(self, name: str, payload: dict) -> Path:
        control = _control(self.root, name)
        path = control.directory("AUTH") / CREDENTIAL_FILES[name][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_export_carries_only_the_session_files(self) -> None:
        self._write_session("codex", {"token": "live-token"})
        home = _control(self.root, "codex").directory("AUTH")
        (home / ".codex" / "history.jsonl").write_text("unrelated local history\n", encoding="utf-8")
        (home / ".codex" / "logs.sqlite").write_bytes(b"\x00" * 32)

        bundle = _control(self.root, "codex").export_credentials()
        self.assertEqual(bundle["kind"], CREDENTIAL_BUNDLE_KIND)
        self.assertEqual(bundle["client"], "codex")
        self.assertEqual(list(bundle["files"]), [".codex/auth.json"])
        self.assertIn("live login tokens", bundle["warning"])

    def test_export_refuses_when_nothing_is_signed_in(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _control(self.root, "claude").export_credentials()
        self.assertIn("登录", str(caught.exception))

    def test_a_bundle_restores_the_session_into_a_fresh_home(self) -> None:
        self._write_session("codex", {"token": "live-token"})
        bundle = _control(self.root, "codex").export_credentials()

        fresh = Path(tempfile.mkdtemp(dir=self.temp.name))
        target = _control(fresh, "codex")
        target.import_credentials(json.dumps(bundle))

        restored = target.directory("AUTH") / ".codex" / "auth.json"
        self.assertEqual(json.loads(restored.read_text()), {"token": "live-token"})
        self.assertEqual(
            stat.S_IMODE(restored.stat().st_mode), 0o600, "tokens must not be world readable"
        )
        self.assertEqual(stat.S_IMODE(restored.parent.stat().st_mode), 0o700)

    def test_a_bundle_cannot_be_imported_into_a_different_client(self) -> None:
        self._write_session("codex", {"token": "live-token"})
        bundle = _control(self.root, "codex").export_credentials()
        with self.assertRaises(ValueError) as caught:
            _control(self.root, "claude").import_credentials(json.dumps(bundle))
        self.assertIn("codex", str(caught.exception))

    def test_entries_outside_the_allowlist_are_refused(self) -> None:
        bundle = {
            "kind": CREDENTIAL_BUNDLE_KIND,
            "version": 1,
            "client": "codex",
            "files": {"../../escape.json": base64.b64encode(b"{}").decode("ascii")},
        }
        with self.assertRaises(ValueError) as caught:
            _control(self.root, "codex").import_credentials(json.dumps(bundle))
        self.assertIn("不接受", str(caught.exception))
        self.assertFalse((self.root.parent / "escape.json").exists())

    def test_unrecognized_or_broken_files_are_refused(self) -> None:
        control = _control(self.root, "codex")
        for payload, expected in (
            ("", "请选择"),
            ("not json", "JSON"),
            (json.dumps({"kind": "something-else"}), "本机器人导出"),
            (
                json.dumps({"kind": CREDENTIAL_BUNDLE_KIND, "version": 99, "client": "codex"}),
                "版本",
            ),
            (
                json.dumps({"kind": CREDENTIAL_BUNDLE_KIND, "version": 1, "client": "codex"}),
                "不包含",
            ),
            (
                json.dumps(
                    {
                        "kind": CREDENTIAL_BUNDLE_KIND,
                        "version": 1,
                        "client": "codex",
                        "files": {".codex/auth.json": "not base64!!"},
                    }
                ),
                "解码",
            ),
        ):
            with self.subTest(payload=payload[:40]):
                with self.assertRaises(ValueError) as caught:
                    control.import_credentials(payload)
                self.assertIn(expected, str(caught.exception))

    def test_the_actions_are_offered_and_export_is_gated_on_being_signed_in(self) -> None:
        control = _control(self.root, "codex")
        control.state.update(state="login_required")
        actions = {item["id"]: item for item in self._actions(control)}
        self.assertTrue(actions["export_credentials"]["disabled"])
        self.assertIn("令牌", actions["export_credentials"]["confirm"])
        self.assertEqual(
            [field["type"] for field in actions["import_credentials"]["fields"]], ["file"]
        )
        control.state.update(state="authenticated")
        self.assertFalse(
            {item["id"]: item for item in self._actions(control)}["export_credentials"]["disabled"]
        )

    @staticmethod
    def _actions(control: ClientControl) -> list[dict]:
        control.proxy_settings = lambda: {"display": "直连", "source": "test", "proxy": "", "no_proxy": ""}
        return control.snapshot()["actions"]


if __name__ == "__main__":
    unittest.main()
