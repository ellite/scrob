import base64
import hashlib
import hmac
import json
import os
import time
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

import jwt

from core.config import settings
from core.security import ALGORITHM, create_access_token, get_password_hash, verify_password

# Hashes written by earlier versions, kept verbatim so existing accounts keep
# signing in: argon2id as produced by passlib, bcrypt as produced by the bcrypt
# library (the format accounts created before argon2 became the default have).
ARGON2_REDPILL = "$argon2id$v=19$m=65536,t=3,p=4$di5lLEUIAaAUYoxRKkUIAQ$Iim/mdT0p2zfncMZ9FW1g7dqQikvqja4qve6UKFs5XU"
ARGON2_UNICODE = "$argon2id$v=19$m=65536,t=3,p=4$CsHYG8MYQ2jN2TtnzFmL0Q$vx65Q2m9ZYkqI+Aauem3JLft6qE672b/3yNyxc2LTvg"
BCRYPT_2A_REDPILL = "$2a$04$HF03KXdPYARp9NvZmXi0hOh149EHtwJJDCYttBF2L660kZ/drXZaK"
BCRYPT_2B_REDPILL = "$2b$04$7f/F.6jOGetwDxrXZKvWxOWxKBK5Hhf.7wsc7dGg./yxLsHqbJ7nG"
BCRYPT_UNICODE = "$2b$04$6/mexNIz6LpR9gTYvRaSTOv1VzGHW3hhA7X0CfAberlXCwe6wF87m"
BCRYPT_100_X = "$2b$04$/s6E0xdqcDZjzT3Dfa1wuuLjZ0Run9BAnUaSKnmJIDTwYyOktOTom"
UNICODE_PASSWORD = "pässwörd-🔑"


class PasswordHashTests(unittest.TestCase):
    def test_new_hashes_are_argon2id_and_verify(self) -> None:
        hashed = get_password_hash("redpill")

        self.assertTrue(hashed.startswith("$argon2id$"))
        self.assertTrue(verify_password("redpill", hashed))
        self.assertFalse(verify_password("bluepill", hashed))

    def test_hashes_are_salted(self) -> None:
        self.assertNotEqual(get_password_hash("redpill"), get_password_hash("redpill"))

    def test_existing_argon2_hashes_still_verify(self) -> None:
        self.assertTrue(verify_password("redpill", ARGON2_REDPILL))
        self.assertFalse(verify_password("bluepill", ARGON2_REDPILL))
        self.assertTrue(verify_password(UNICODE_PASSWORD, ARGON2_UNICODE))

    def test_existing_bcrypt_hashes_still_verify(self) -> None:
        for hashed in (BCRYPT_2A_REDPILL, BCRYPT_2B_REDPILL):
            self.assertTrue(verify_password("redpill", hashed))
            self.assertFalse(verify_password("bluepill", hashed))
        self.assertTrue(verify_password(UNICODE_PASSWORD, BCRYPT_UNICODE))

    def test_bcrypt_ignores_characters_past_72_bytes_without_raising(self) -> None:
        self.assertTrue(verify_password("x" * 100, BCRYPT_100_X))
        self.assertTrue(verify_password("x" * 72 + "anything", BCRYPT_100_X))
        self.assertFalse(verify_password("x" * 71, BCRYPT_100_X))

    def test_unrecognised_or_damaged_hashes_never_verify(self) -> None:
        for hashed in ("", "plaintext", "$argon2id$not-a-real-hash", "$2b$04$short"):
            self.assertFalse(verify_password("redpill", hashed))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class SessionTokenCompatibilityTests(unittest.TestCase):
    def test_token_in_the_format_python_jose_issued_still_verifies(self) -> None:
        # Sessions issued before the move to PyJWT must survive the upgrade:
        # compact JSON header/payload, HS256, signed with the app secret.
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = _b64(json.dumps({"exp": int(time.time()) + 600, "sub": "41"}, separators=(",", ":")).encode())
        signature = _b64(hmac.new(settings.secret_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())

        claims = jwt.decode(f"{header}.{payload}.{signature}", settings.secret_key, algorithms=[ALGORITHM])

        self.assertEqual(claims["sub"], "41")

    def test_tokens_we_issue_round_trip_with_extra_claims(self) -> None:
        token = create_access_token(7, extra_claims={"rem": True, "type": "2fa_pending"})

        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])

        self.assertEqual((claims["sub"], claims["rem"], claims["type"]), ("7", True, "2fa_pending"))

    def test_expired_and_tampered_tokens_are_rejected(self) -> None:
        expired = jwt.encode({"sub": "1", "exp": int(time.time()) - 5}, settings.secret_key, algorithm=ALGORITHM)
        tampered = create_access_token(7)[:-3] + "AAA"

        for token in (expired, tampered):
            with self.subTest(token=token[-6:]), self.assertRaises(jwt.PyJWTError):
                jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])

    def test_token_with_a_different_algorithm_than_expected_is_rejected(self) -> None:
        other = jwt.encode({"sub": "1"}, "k" * 64, algorithm="HS512")

        with self.assertRaises(jwt.PyJWTError):
            jwt.decode(other, settings.secret_key, algorithms=[ALGORITHM])


if __name__ == "__main__":
    unittest.main()
