from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from prediction_market_agent.core.config import Config
from prediction_market_agent.runtime.auth import AdminAuthStore, SESSION_COOKIE, _b64u, _unb64u
from prediction_market_agent.runtime.dashboard import create_app


def client_ecdh() -> tuple[ec.EllipticCurvePrivateKey, dict[str, str]]:
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    import base64

    def b64u(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return private, {
        "kty": "EC",
        "crv": "P-256",
        "x": b64u(numbers.x.to_bytes(32, "big")),
        "y": b64u(numbers.y.to_bytes(32, "big")),
    }


class AuthenticationTests(unittest.TestCase):
    def test_browser_and_server_derive_same_ecdh_session_key(self) -> None:
        client_private, client_jwk = client_ecdh()
        challenge, canonical, server_private, server_jwk = AdminAuthStore._ecdh_ceremony(
            client_jwk
        )
        server_key = AdminAuthStore._derive_session_key(canonical, server_private, challenge)
        _, server_public = AdminAuthStore._parse_public_jwk(server_jwk)
        shared = client_private.exchange(ec.ECDH(), server_public)
        browser_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=challenge,
            info=b"prediction-market-agent-session-v1",
        ).derive(shared)
        self.assertEqual(browser_key, server_key)

    def test_initial_registration_creates_admin_and_encrypted_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AdminAuthStore(Path(directory) / "auth.sqlite3", 72, 168)
            client_private, client_jwk = client_ecdh()
            options = store.registration_options(
                "http://localhost", adding=False, client_public_jwk=client_jwk
            )
            verification = SimpleNamespace(
                credential_id=b"credential-one",
                credential_public_key=b"cose-public-key",
                sign_count=0,
            )
            with patch(
                "prediction_market_agent.runtime.auth.verify_registration_response",
                return_value=verification,
            ):
                session = store.verify_registration(
                    options["ceremony_id"], {}, "Laptop", adding=False,
                    user_agent="Browser/1", source_address="127.0.0.1",
                )
            self.assertTrue(store.has_admin())
            self.assertIsNotNone(session)
            assert session is not None
            _, server_public = store._parse_public_jwk(options["server_public_key"])
            shared = client_private.exchange(ec.ECDH(), server_public)
            challenge = _unb64u(options["publicKey"]["challenge"])
            browser_key = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=challenge,
                info=b"prediction-market-agent-session-v1",
            ).derive(shared)
            self.assertEqual(session.encryption_key, browser_key)
            self.assertGreaterEqual(session.expires_at, int(time.time()) + 167 * 3600)
            self.assertEqual(store.sessions(session.session_id)[0]["user_agent"], "Browser/1")

    def test_replay_is_rejected_and_last_passkey_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AdminAuthStore(Path(directory) / "auth.sqlite3", 72, 168)
            connection = store._connect()
            connection.execute(
                "INSERT INTO admin_passkeys VALUES (?,?,?,?,?,?,?,NULL)",
                (b"one", "One", b"key", 0, "localhost", "http://localhost", int(time.time())),
            )
            connection.commit()
            connection.close()
            session = store.create_session(b"k" * 32, b"one", "Browser", "127.0.0.1")
            envelope = store.encrypt(session, {"hello": "world"}, aad="request")
            self.assertEqual(store.decrypt(session, envelope, aad="request"), {"hello": "world"})
            with self.assertRaisesRegex(ValueError, "already been used"):
                store.decrypt(session, envelope, aad="request")
            with self.assertRaisesRegex(ValueError, "at least one Passkey"):
                store.delete_credential(b"one".hex())

    def test_sessions_can_be_listed_and_kicked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AdminAuthStore(Path(directory) / "auth.sqlite3", 72, 168)
            connection = store._connect()
            connection.execute(
                "INSERT INTO admin_passkeys VALUES (?,?,?,?,?,?,?,NULL)",
                (b"one", "Phone", b"key", 0, "localhost", "http://localhost", int(time.time())),
            )
            connection.commit()
            connection.close()
            session = store.create_session(b"x" * 32, b"one", "Browser", "127.0.0.1")
            self.assertTrue(store.sessions(session.session_id)[0]["current"])
            self.assertEqual(store.kick_sessions([session.session_id]), 1)
            self.assertIsNone(store.session(session.token))

    def test_non_increasing_signature_counter_does_not_block_login(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AdminAuthStore(Path(directory) / "auth.sqlite3", 72, 168)
            connection = store._connect()
            connection.execute(
                "INSERT INTO admin_passkeys VALUES (?,?,?,?,?,?,?,NULL)",
                (b"one", "Synced Passkey", b"key", 9, "localhost", "http://localhost",
                 int(time.time())),
            )
            connection.commit()
            connection.close()
            _, client_jwk = client_ecdh()
            options = store.authentication_options("http://localhost", client_jwk)
            with patch(
                "prediction_market_agent.runtime.auth.verify_authentication_response",
                return_value=SimpleNamespace(new_sign_count=0),
            ) as verify:
                session = store.verify_authentication(
                    options["ceremony_id"], {"id": _b64u(b"one")},
                    user_agent="Browser", source_address="127.0.0.1",
                )
            self.assertIsNotNone(session)
            self.assertEqual(verify.call_args.kwargs["credential_current_sign_count"], 0)
            connection = store._connect()
            retained = connection.execute(
                "SELECT sign_count FROM admin_passkeys WHERE credential_id = ?", (b"one",)
            ).fetchone()[0]
            connection.close()
            self.assertEqual(retained, 9)

    def test_idle_timeout_slides_but_absolute_timeout_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AdminAuthStore(Path(directory) / "auth.sqlite3", 72, 168)
            connection = store._connect()
            connection.execute(
                "INSERT INTO admin_passkeys VALUES (?,?,?,?,?,?,?,NULL)",
                (b"one", "Phone", b"key", 0, "localhost", "http://localhost", int(time.time())),
            )
            connection.commit()
            connection.close()
            idle = store.create_session(b"i" * 32, b"one", "Browser", "127.0.0.1")
            connection = store._connect()
            connection.execute(
                "UPDATE admin_sessions SET last_request_at = ? WHERE session_id = ?",
                (int(time.time()) - 72 * 3600 - 1, idle.session_id),
            )
            connection.commit()
            connection.close()
            self.assertIsNone(store.session(idle.token))

            absolute = store.create_session(b"a" * 32, b"one", "Browser", "127.0.0.1")
            connection = store._connect()
            connection.execute(
                "UPDATE admin_sessions SET expires_at = ?,last_request_at = ? WHERE session_id = ?",
                (int(time.time()) - 1, int(time.time()), absolute.session_id),
            )
            connection.commit()
            connection.close()
            self.assertIsNone(store.session(absolute.token))

    def test_web_application_uses_configured_databases_before_any_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            decision_db = root / "chosen" / "decisions.sqlite3"
            auth_db = root / "chosen" / "auth.sqlite3"
            config = Config(
                working_directory=root,
                session_db=decision_db,
                auth_db=auth_db,
                management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json",
                decision_providers=("codex",),
                market_api_plugins=("binance",),
                decision_strategy_name="general_agent",
            )
            app = create_app(config)
            self.assertTrue(decision_db.exists())
            self.assertTrue(auth_db.exists())
            with TestClient(app, base_url="http://localhost") as client:
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertIn("初始化管理员", page.text)
                self.assertEqual(client.post("/api/secure", json={}).status_code, 401)

    def test_first_registration_origin_comes_from_request_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                working_directory=root,
                session_db=root / "decisions.sqlite3",
                auth_db=root / "auth.sqlite3",
                management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json",
                decision_providers=("codex",),
                market_api_plugins=("binance",),
                decision_strategy_name="general_agent",
            )
            _, public_jwk = client_ecdh()
            with TestClient(create_app(config), base_url="http://localhost") as client:
                response = client.post(
                    "/api/auth/register/options",
                    headers={"Origin": "https://attacker.invalid"},
                    json={"client_public_key": public_jwk},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["publicKey"]["rp"]["id"], "localhost")

    def test_secure_endpoint_encrypts_business_responses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                working_directory=root,
                session_db=root / "decisions.sqlite3",
                auth_db=root / "auth.sqlite3",
                management_file=root / "selection.json",
                plugin_directories_file=root / "directories.json",
                application_config_file=root / "application.json",
                decision_providers=("codex",),
                market_api_plugins=("binance",),
                decision_strategy_name="general_agent",
            )
            app = create_app(config)
            store = AdminAuthStore(config.auth_db, 72, 168)
            connection = store._connect()
            connection.execute(
                "INSERT INTO admin_passkeys VALUES (?,?,?,?,?,?,?,NULL)",
                (b"one", "Phone", b"key", 0, "localhost", "http://localhost", int(time.time())),
            )
            connection.commit()
            connection.close()
            session = store.create_session(b"z" * 32, b"one", "Browser", "127.0.0.1")
            request_envelope = store.encrypt(session, {"url": "/api/summary", "body": None},
                                             aad="POST /api/secure")
            with TestClient(app, base_url="http://localhost") as client:
                client.cookies.set(SESSION_COOKIE, session.token)
                response = client.post("/api/secure", json=request_envelope,
                                       headers={"X-Admin-CSRF": session.csrf_token})
            self.assertEqual(response.status_code, 200)
            encrypted = response.json()
            plaintext = AESGCM(session.encryption_key).decrypt(
                _unb64u(encrypted["nonce"]), _unb64u(encrypted["ciphertext"]),
                b"RESPONSE /api/secure",
            )
            self.assertIn("summary", json.loads(plaintext))


if __name__ == "__main__":
    unittest.main()
