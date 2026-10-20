import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Union
import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from jose import jwt
from core.config import settings

# New hashes are argon2id with argon2-cffi's default cost. Hashes written
# before argon2 became the default are bcrypt and are still verified.
_argon2 = PasswordHasher()
# bcrypt only reads the first 72 bytes of a password; bcrypt >= 5 refuses longer
# input instead of truncating, so cut it the way earlier versions did.
_BCRYPT_MAX_BYTES = 72
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week

def session_lifetime(remember: bool = False) -> timedelta:
    """Lifetime of a user login token; longer when the user ticked "Stay logged in"."""
    if remember:
        return timedelta(days=max(1, settings.session_remember_days))
    return timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)


def create_access_token(
    subject: Union[str, Any],
    expires_delta: timedelta = None,
    extra_claims: dict = None,
) -> str:
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {"exp": expire, "sub": str(subject)}
    if extra_claims:
        to_encode.update(extra_claims)
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)
    return encoded_jwt

def verify_password(plain_password: str, hashed_password: str) -> bool:
    if hashed_password.startswith("$argon2"):
        try:
            return _argon2.verify(hashed_password, plain_password)
        except (VerificationError, InvalidHashError):
            return False
    if hashed_password.startswith(_BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed_password.encode("utf-8"))
        except ValueError:
            return False
    return False

def get_password_hash(password: str) -> str:
    return _argon2.hash(password)


def hash_opaque_token(token: str) -> str:
    """SHA-256 hex digest for at-rest storage of high-entropy bearer secrets
    (device codes, refresh tokens). These are already 256-bit random strings,
    so a slow password KDF buys nothing - a fast digest that never stores the
    raw value is what matters. Looked up by exact hash match, never by
    comparing a decrypted value."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_opaque_token() -> str:
    """URL-safe, ~256 bits of entropy."""
    return secrets.token_urlsafe(32)

