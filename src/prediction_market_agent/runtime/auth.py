from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


ADMIN_USER_ID = b"prediction-market-agent-admin"
SESSION_COOKIE = "prediction_agent_admin"


def _b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64u(value: str) -> bytes:
    return base64url_to_bytes(value)


@dataclass(frozen=True)
class AdminSession:
    token: str
    csrf_token: str
    expires_at: int
    session_id: str
    encryption_key: bytes


class AdminAuthStore:
    """Single-admin Passkeys, ECDH-bound sessions, and replay protection."""

    def __init__(self, path: Path, idle_session_hours: int, absolute_session_hours: int):
        self.path = path.expanduser().resolve()
        self.idle_session_seconds = int(idle_session_hours) * 3600
        self.absolute_session_seconds = int(absolute_session_hours) * 3600
        if self.absolute_session_seconds < self.idle_session_seconds:
            raise ValueError("Absolute session lifetime cannot be shorter than idle lifetime")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_passkeys (
                credential_id BLOB PRIMARY KEY,
                name TEXT NOT NULL,
                public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL,
                rp_id TEXT NOT NULL,
                origin TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS auth_challenges (
                ceremony_id TEXT PRIMARY KEY,
                purpose TEXT NOT NULL,
                challenge BLOB NOT NULL,
                rp_id TEXT NOT NULL,
                origin TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                client_public_key BLOB,
                server_private_key BLOB
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL,
                encryption_key BLOB NOT NULL,
                credential_id BLOB NOT NULL,
                user_agent TEXT NOT NULL,
                source_address TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_request_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY (credential_id) REFERENCES admin_passkeys(credential_id)
            );
            CREATE TABLE IF NOT EXISTS session_nonces (
                session_id TEXT NOT NULL,
                nonce BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (session_id, nonce)
            );
            """
        )
        connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def request_identity(origin: str) -> tuple[str, str]:
        parsed = urlparse(origin)
        host = parsed.hostname or ""
        secure = parsed.scheme == "https" or (
            parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
        )
        if not secure or not host or parsed.username or parsed.password:
            raise ValueError("Passkey requires HTTPS, or an HTTP loopback origin for local use")
        canonical = f"{parsed.scheme}://{parsed.netloc}"
        if canonical != origin.rstrip("/"):
            raise ValueError("Origin must contain only scheme, host, and optional port")
        return canonical, host

    @classmethod
    def _parse_public_jwk(
        cls, value: dict[str, Any]
    ) -> tuple[bytes, ec.EllipticCurvePublicKey]:
        if not isinstance(value, dict) or value.get("kty") != "EC" or value.get("crv") != "P-256":
            raise ValueError("ECDH public key must be a P-256 EC JWK")
        try:
            x = _unb64u(str(value["x"]))
            y = _unb64u(str(value["y"]))
            public = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            ).public_key()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("ECDH public key is invalid") from error
        canonical = json.dumps(
            {"crv": "P-256", "kty": "EC", "x": _b64u(x), "y": _b64u(y)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return canonical, public

    @classmethod
    def _ecdh_ceremony(
        cls, client_jwk: dict[str, Any]
    ) -> tuple[bytes, bytes, bytes, dict[str, str]]:
        canonical, _ = cls._parse_public_jwk(client_jwk)
        private = ec.generate_private_key(ec.SECP256R1())
        private_value = private.private_numbers().private_value.to_bytes(32, "big")
        numbers = private.public_key().public_numbers()
        server_jwk = {
            "kty": "EC", "crv": "P-256",
            "x": _b64u(numbers.x.to_bytes(32, "big")),
            "y": _b64u(numbers.y.to_bytes(32, "big")),
        }
        server_canonical = json.dumps(server_jwk, sort_keys=True, separators=(",", ":")).encode()
        digest = hashes.Hash(hashes.SHA256())
        digest.update(secrets.token_bytes(32))
        digest.update(canonical)
        digest.update(server_canonical)
        return digest.finalize(), canonical, private_value, server_jwk

    @classmethod
    def _derive_session_key(
        cls, client_canonical: bytes, server_private_value: bytes, challenge: bytes
    ) -> bytes:
        _, client_public = cls._parse_public_jwk(json.loads(client_canonical))
        private = ec.derive_private_key(int.from_bytes(server_private_value, "big"), ec.SECP256R1())
        shared = private.exchange(ec.ECDH(), client_public)
        return HKDF(
            algorithm=hashes.SHA256(), length=32, salt=challenge,
            info=b"prediction-market-agent-session-v1",
        ).derive(shared)

    def _cleanup(self, connection: sqlite3.Connection) -> None:
        now = int(time.time())
        connection.execute("DELETE FROM auth_challenges WHERE expires_at < ?", (now,))
        connection.execute(
            "DELETE FROM admin_sessions WHERE expires_at < ? OR last_request_at + ? < ?",
            (now, self.idle_session_seconds, now),
        )
        connection.execute(
            "DELETE FROM session_nonces WHERE session_id NOT IN (SELECT session_id FROM admin_sessions)"
        )

    def has_admin(self) -> bool:
        connection = self._connect()
        count = connection.execute("SELECT COUNT(*) FROM admin_passkeys").fetchone()[0]
        connection.close()
        return bool(count)

    def registration_options(
        self, origin: str, *, adding: bool,
        client_public_jwk: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        origin, rp_id = self.request_identity(origin)
        connection = self._connect()
        self._cleanup(connection)
        rows = connection.execute(
            "SELECT credential_id,rp_id,origin FROM admin_passkeys ORDER BY created_at"
        ).fetchall()
        if adding:
            if not rows:
                connection.close()
                raise ValueError("The admin account has not been initialized")
            if (rp_id, origin) != (rows[0][1], rows[0][2]):
                connection.close()
                raise ValueError("New Passkeys must use the original admin origin")
            challenge = secrets.token_bytes(32)
            client_public = server_private = None
            server_jwk = None
        else:
            if rows:
                connection.close()
                raise ValueError("The admin account is already initialized")
            if client_public_jwk is None:
                connection.close()
                raise ValueError("Initial registration requires an ECDH public key")
            challenge, client_public, server_private, server_jwk = self._ecdh_ceremony(client_public_jwk)
        ceremony_id = secrets.token_urlsafe(24)
        options = generate_registration_options(
            rp_id=rp_id, rp_name="Prediction Market Agent", user_id=ADMIN_USER_ID,
            user_name="admin", user_display_name="Administrator", challenge=challenge,
            exclude_credentials=[PublicKeyCredentialDescriptor(id=row[0]) for row in rows],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        connection.execute(
            "INSERT INTO auth_challenges "
            "(ceremony_id,purpose,challenge,rp_id,origin,expires_at,client_public_key,server_private_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ceremony_id, "add" if adding else "register", challenge, rp_id, origin,
             int(time.time()) + 300, client_public, server_private),
        )
        connection.commit()
        connection.close()
        result: dict[str, Any] = {
            "ceremony_id": ceremony_id,
            "publicKey": json.loads(options_to_json(options)),
        }
        if server_jwk:
            result["server_public_key"] = server_jwk
        return result

    def verify_registration(
        self, ceremony_id: str, credential: dict[str, Any], name: str, *, adding: bool,
        user_agent: str = "", source_address: str = "",
    ) -> AdminSession | None:
        connection = self._connect()
        row = connection.execute(
            "SELECT purpose,challenge,rp_id,origin,expires_at,client_public_key,server_private_key "
            "FROM auth_challenges WHERE ceremony_id = ?", (ceremony_id,),
        ).fetchone()
        connection.execute("DELETE FROM auth_challenges WHERE ceremony_id = ?", (ceremony_id,))
        connection.commit()
        expected = "add" if adding else "register"
        if not row or row[4] < int(time.time()) or row[0] != expected:
            connection.close()
            raise ValueError("Registration challenge is missing, expired, or has the wrong purpose")
        count = connection.execute("SELECT COUNT(*) FROM admin_passkeys").fetchone()[0]
        if (adding and not count) or (not adding and count):
            connection.close()
            raise ValueError("Admin initialization state changed during registration")
        verification = verify_registration_response(
            credential=credential, expected_challenge=row[1], expected_rp_id=row[2],
            expected_origin=row[3], require_user_verification=True,
        )
        label = name.strip() or f"Passkey {count + 1}"
        connection.execute(
            "INSERT INTO admin_passkeys VALUES (?,?,?,?,?,?,?,NULL)",
            (verification.credential_id, label, verification.credential_public_key,
             verification.sign_count, row[2], row[3], int(time.time())),
        )
        connection.commit()
        connection.close()
        if adding:
            return None
        if row[5] is None or row[6] is None:
            raise ValueError("Initial registration did not include ECDH parameters")
        key = self._derive_session_key(row[5], row[6], row[1])
        return self.create_session(key, verification.credential_id, user_agent, source_address)

    def authentication_options(
        self, origin: str, client_public_jwk: dict[str, Any]
    ) -> dict[str, Any]:
        origin, _ = self.request_identity(origin)
        connection = self._connect()
        self._cleanup(connection)
        rows = connection.execute(
            "SELECT credential_id,rp_id,origin FROM admin_passkeys ORDER BY created_at"
        ).fetchall()
        if not rows:
            connection.close()
            raise ValueError("The admin account has not been initialized")
        if origin != rows[0][2]:
            connection.close()
            raise ValueError("Login origin does not match the registered admin origin")
        challenge, client_public, server_private, server_jwk = self._ecdh_ceremony(client_public_jwk)
        ceremony_id = secrets.token_urlsafe(24)
        options = generate_authentication_options(
            rp_id=rows[0][1], challenge=challenge,
            allow_credentials=[PublicKeyCredentialDescriptor(id=row[0]) for row in rows],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        connection.execute(
            "INSERT INTO auth_challenges "
            "(ceremony_id,purpose,challenge,rp_id,origin,expires_at,client_public_key,server_private_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ceremony_id, "login", challenge, rows[0][1], rows[0][2],
             int(time.time()) + 300, client_public, server_private),
        )
        connection.commit()
        connection.close()
        return {"ceremony_id": ceremony_id,
                "publicKey": json.loads(options_to_json(options)),
                "server_public_key": server_jwk}

    def verify_authentication(
        self, ceremony_id: str, credential: dict[str, Any], *,
        user_agent: str = "", source_address: str = "",
    ) -> AdminSession:
        credential_id = _unb64u(str(credential.get("id", "")))
        connection = self._connect()
        challenge = connection.execute(
            "SELECT challenge,rp_id,origin,expires_at,client_public_key,server_private_key "
            "FROM auth_challenges WHERE ceremony_id = ? AND purpose = 'login'",
            (ceremony_id,),
        ).fetchone()
        connection.execute("DELETE FROM auth_challenges WHERE ceremony_id = ?", (ceremony_id,))
        key = connection.execute(
            "SELECT public_key,sign_count FROM admin_passkeys WHERE credential_id = ?",
            (credential_id,),
        ).fetchone()
        connection.commit()
        if not challenge or challenge[3] < int(time.time()) or not key:
            connection.close()
            raise ValueError("Authentication challenge or Passkey is invalid")
        verification = verify_authentication_response(
            credential=credential, expected_challenge=challenge[0], expected_rp_id=challenge[1],
            expected_origin=challenge[2], credential_public_key=key[0],
            # Counter values are optional authenticator telemetry. Supplying the
            # stored high-water mark would make the dependency reject valid zero,
            # reset, or non-increasing counters before application policy applies.
            credential_current_sign_count=0, require_user_verification=True,
        )
        observed_sign_count = max(0, int(verification.new_sign_count))
        retained_sign_count = max(int(key[1]), observed_sign_count)
        connection.execute(
            "UPDATE admin_passkeys SET sign_count = ?,last_used_at = ? WHERE credential_id = ?",
            (retained_sign_count, int(time.time()), credential_id),
        )
        connection.commit()
        connection.close()
        if challenge[4] is None or challenge[5] is None:
            raise ValueError("Authentication did not include ECDH parameters")
        session_key = self._derive_session_key(challenge[4], challenge[5], challenge[0])
        return self.create_session(session_key, credential_id, user_agent, source_address)

    def create_session(
        self, encryption_key: bytes, credential_id: bytes,
        user_agent: str, source_address: str,
    ) -> AdminSession:
        if len(encryption_key) != 32:
            raise ValueError("Session encryption key must contain 32 bytes")
        now = int(time.time())
        token = secrets.token_urlsafe(40)
        session = AdminSession(token, secrets.token_urlsafe(24), now + self.absolute_session_seconds,
                               secrets.token_urlsafe(18), encryption_key)
        connection = self._connect()
        connection.execute(
            "INSERT INTO admin_sessions "
            "(token_hash,session_id,csrf_token,encryption_key,credential_id,user_agent,source_address,created_at,last_request_at,expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self._token_hash(token), session.session_id, session.csrf_token,
             session.encryption_key, credential_id, user_agent[:1000], source_address[:200],
             now, now, session.expires_at),
        )
        connection.commit()
        connection.close()
        return session

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token: str, *, touch: bool = True) -> AdminSession | None:
        if not token:
            return None
        connection = self._connect()
        self._cleanup(connection)
        token_hash = self._token_hash(token)
        row = connection.execute(
            "SELECT session_id,csrf_token,encryption_key,expires_at FROM admin_sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row and touch:
            connection.execute("UPDATE admin_sessions SET last_request_at = ? WHERE token_hash = ?",
                               (int(time.time()), token_hash))
        connection.commit()
        connection.close()
        return None if not row else AdminSession(token, row[1], row[3], row[0], row[2])

    def logout(self, token: str) -> None:
        connection = self._connect()
        connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (self._token_hash(token),))
        connection.commit()
        connection.close()

    def encrypt(self, session: AdminSession, value: Any, *, aad: str) -> dict[str, str]:
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        ciphertext = AESGCM(session.encryption_key).encrypt(nonce, plaintext, aad.encode())
        return {"nonce": _b64u(nonce), "ciphertext": _b64u(ciphertext)}

    def decrypt(self, session: AdminSession, envelope: dict[str, Any], *, aad: str) -> Any:
        try:
            nonce = _unb64u(str(envelope["nonce"]))
            ciphertext = _unb64u(str(envelope["ciphertext"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Encrypted request envelope is invalid") from error
        if len(nonce) != 12:
            raise ValueError("Encrypted request nonce is invalid")
        connection = self._connect()
        try:
            connection.execute("INSERT INTO session_nonces VALUES (?,?,?)",
                               (session.session_id, nonce, int(time.time())))
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.close()
            raise ValueError("Encrypted request nonce has already been used") from error
        connection.close()
        try:
            plaintext = AESGCM(session.encryption_key).decrypt(nonce, ciphertext, aad.encode())
            return json.loads(plaintext)
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Encrypted request could not be authenticated") from error

    def credentials(self) -> list[dict[str, Any]]:
        connection = self._connect()
        rows = connection.execute(
            "SELECT hex(credential_id),name,rp_id,origin,created_at,last_used_at FROM admin_passkeys ORDER BY created_at"
        ).fetchall()
        connection.close()
        return [{"id": row[0].lower(), "name": row[1], "rp_id": row[2],
                 "origin": row[3], "created_at": row[4], "last_used_at": row[5]}
                for row in rows]

    def sessions(self, current_session_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        self._cleanup(connection)
        rows = connection.execute(
            "SELECT s.session_id,p.name,s.user_agent,s.source_address,s.created_at,"
            "s.last_request_at,s.expires_at FROM admin_sessions s "
            "JOIN admin_passkeys p ON p.credential_id=s.credential_id ORDER BY s.last_request_at DESC"
        ).fetchall()
        connection.commit()
        connection.close()
        return [{"id": row[0], "passkey_name": row[1], "user_agent": row[2],
                 "source_address": row[3], "login_at": row[4],
                 "last_request_at": row[5], "expires_at": row[6],
                 "idle_expires_at": min(row[5] + self.idle_session_seconds, row[6]),
                 "current": row[0] == current_session_id} for row in rows]

    def kick_sessions(self, session_ids: list[str]) -> int:
        if not isinstance(session_ids, list) or not session_ids or not all(
            isinstance(value, str) and value for value in session_ids
        ):
            raise ValueError("Session ids must be a non-empty string list")
        connection = self._connect()
        placeholders = ",".join("?" for _ in session_ids)
        cursor = connection.execute(
            f"DELETE FROM admin_sessions WHERE session_id IN ({placeholders})", session_ids
        )
        connection.commit()
        connection.close()
        return cursor.rowcount

    def rename_credential(self, credential_hex: str, name: str) -> None:
        label = name.strip()
        if not label:
            raise ValueError("Passkey name cannot be empty")
        try:
            credential_id = bytes.fromhex(credential_hex)
        except ValueError as error:
            raise ValueError("Passkey id is invalid") from error
        connection = self._connect()
        cursor = connection.execute("UPDATE admin_passkeys SET name = ? WHERE credential_id = ?",
                                    (label, credential_id))
        connection.commit()
        connection.close()
        if cursor.rowcount != 1:
            raise ValueError("Passkey was not found")

    def delete_credential(self, credential_hex: str) -> None:
        try:
            credential_id = bytes.fromhex(credential_hex)
        except ValueError as error:
            raise ValueError("Passkey id is invalid") from error
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT COUNT(*) FROM admin_passkeys").fetchone()[0] <= 1:
            connection.rollback()
            connection.close()
            raise ValueError("The admin account must keep at least one Passkey")
        connection.execute("DELETE FROM admin_sessions WHERE credential_id = ?", (credential_id,))
        cursor = connection.execute("DELETE FROM admin_passkeys WHERE credential_id = ?", (credential_id,))
        if cursor.rowcount != 1:
            connection.rollback()
            connection.close()
            raise ValueError("Passkey was not found")
        connection.commit()
        connection.close()
