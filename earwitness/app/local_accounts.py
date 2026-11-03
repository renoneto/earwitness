"""Local email and password accounts for the pilot (PW design rev 4).

The operator creates each login on the host (scripts/app_accounts.py);
there is no self sign-up and no outside sign-in service. A local account
is the pair (local issuer, derived subject) in `local_accounts`; its
principal keeps the existing `oidc:<issuer>:<subject>` form, with the
app's public origin as the issuer. A row grants nothing by itself: the
admission file decides who may sign in.

- Hash format `scrypt-v1` only: hashlib.scrypt, N=2^15, r=8, p=1, a
  16-byte salt, a 32-byte key, maxmem 64 MiB, stored as
  `scrypt-v1$<salt>$<key>` (unpadded base64url). Any other value fails
  after one hash against a dummy record made at start-up, so an unknown
  account, a missing password and a malformed record cost the same work.
- One email rule (`canonical`) for the command, login lookup, the pair
  limiter key and the subject. Its key equals collaboration.identity_key.
- The subject is a plain hash of the issuer and the email key: the same
  email always gives the same principal, with no extra secret to keep.

No password, hash or salt is ever logged or written to a result file.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import sqlite3
from urllib.parse import urlsplit

SCHEME = "scrypt-v1"
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
_HASH_RE = re.compile(r"scrypt-v1\$([A-Za-z0-9_-]{22})\$([A-Za-z0-9_-]{43})")

# Generated passwords: 20 symbols from 55 (about 115 bits), shown in
# groups of 5 joined by hyphens; the hyphens are part of the password.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
GENERATED_SYMBOLS = 20
# Chosen passwords (change password).
NEW_MIN_CHARS = 12
NEW_MAX_CHARS = 128
NEW_MAX_BYTES = 512
# Any password presented at sign-in, before any hash.
PRESENTED_MAX_BYTES = 1024

EMAIL_MAX = 254
_ASCII_WS = " \t\n\r\x0b\x0c"
SUBJECT_PREFIX = "acct-"
_SUBJECT_TAG = "earwitness-local-subject-v1"

# The dummy record, made once at start-up: an unknown account, a row with
# no password and a malformed record each run one scrypt-v1 against it.
_DUMMY_SALT = secrets.token_bytes(SALT_BYTES)
_DUMMY_KEY = secrets.token_bytes(KEY_BYTES)


class EmailError(ValueError):
    """The email does not follow the one email rule (3.3)."""


class DuplicateAccount(ValueError):
    """An account with this email or principal already exists."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str, size: int) -> bytes | None:
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None
    # Canonical only: a text whose spare bits are set decodes to the same
    # bytes as another text; it is not the stored form.
    if len(data) != size or _b64(data) != text:
        return None
    return data


def _scrypt(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                          p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=KEY_BYTES)


def password_bytes(password) -> bytes | None:
    """UTF-8 bytes of a presented password, or None when it is not a
    string or cannot be encoded (a lone surrogate)."""
    if not isinstance(password, str):
        return None
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _parse(stored) -> tuple[bytes, bytes] | None:
    if not isinstance(stored, str):
        return None
    match = _HASH_RE.fullmatch(stored)
    if match is None:
        return None
    salt = _unb64(match.group(1), SALT_BYTES)
    key = _unb64(match.group(2), KEY_BYTES)
    if salt is None or key is None:
        return None
    return salt, key


def valid_hash(stored) -> bool:
    """True only for a well-formed scrypt-v1 record."""
    return _parse(stored) is not None


def hash_password(password: str) -> str:
    raw = password_bytes(password)
    if raw is None:
        raise ValueError("password must be a UTF-8 string")
    salt = secrets.token_bytes(SALT_BYTES)
    return f"{SCHEME}${_b64(salt)}${_b64(_scrypt(raw, salt))}"


def verify(stored, password: str) -> bool:
    """One scrypt-v1 computation, against the stored record or, when it is
    missing or malformed, against the dummy record (always a failure)."""
    raw = password_bytes(password)
    if raw is None:
        raise ValueError("password must be a UTF-8 string")
    parsed = _parse(stored)
    salt, key = parsed if parsed is not None else (_DUMMY_SALT, _DUMMY_KEY)
    matched = hmac.compare_digest(_scrypt(raw, salt), key)
    return matched and parsed is not None


def generate_password() -> str:
    symbols = "".join(secrets.choice(ALPHABET) for _ in range(GENERATED_SYMBOLS))
    return "-".join(symbols[i:i + 5] for i in range(0, GENERATED_SYMBOLS, 5))


def new_password_problem(new, *, current: str, email_key: str) -> str | None:
    """Why a chosen password is refused, or None when it is accepted."""
    raw = password_bytes(new)
    if raw is None or not NEW_MIN_CHARS <= len(new) <= NEW_MAX_CHARS \
            or len(raw) > NEW_MAX_BYTES:
        return (f"new password must be {NEW_MIN_CHARS} to {NEW_MAX_CHARS}"
                " characters")
    if new == current:
        return "new password must differ from the current password"
    if new.lower() == str(email_key or "").lower():
        return "new password must differ from the email address"
    return None


# ------------------------------------------------------------------ email

def canonical(raw) -> tuple[str, str]:
    """(key, display) under the one email rule (3.3), or EmailError.

    Strip ASCII whitespace at both ends; refuse non-ASCII, any inner
    whitespace, control characters, ':', anything but exactly one '@', an
    empty local part or domain, and more than 254 characters. The key is
    the lowercased result; the display is the stripped spelling."""
    if not isinstance(raw, str):
        raise EmailError("email must be text")
    text = raw.strip(_ASCII_WS)
    if not text or len(text) > EMAIL_MAX or not text.isascii():
        raise EmailError("email is empty, too long or not ASCII")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        raise EmailError("email has whitespace or a control character")
    if ":" in text or text.count("@") != 1:
        raise EmailError("email needs exactly one '@' and no ':'")
    local, _, domain = text.partition("@")
    if not local or not domain:
        raise EmailError("email has an empty local part or domain")
    return text.lower(), text


def lookup_key(raw) -> str:
    """The key used at sign-in: the canonical key, or for a value that
    fails the rule its stripped, lowercased text (unknown-account path)."""
    try:
        return canonical(raw)[0]
    except EmailError:
        return raw.strip(_ASCII_WS).lower() if isinstance(raw, str) else ""


# ------------------------------------------------------- issuer and subject

def local_issuer(public_base_url: str) -> str:
    """The local issuer: the origin of EARWITNESS_PUBLIC_BASE_URL."""
    try:
        parts = urlsplit(str(public_base_url or "").strip())
        port = parts.port
    except ValueError:
        raise ValueError("EARWITNESS_PUBLIC_BASE_URL is not a URL") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host or "@" in parts.netloc \
            or parts.query or parts.fragment:
        raise ValueError("EARWITNESS_PUBLIC_BASE_URL must be an http(s) origin")
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def subject(issuer: str, key: str) -> str:
    digest = hashlib.sha256(f"{_SUBJECT_TAG}\0{issuer}\0{key}".encode()).hexdigest()
    return SUBJECT_PREFIX + digest[:32]


def principal(issuer: str, email: str) -> str:
    from .browser_auth import principal_identity
    return principal_identity(issuer, subject(issuer, canonical(email)[0]))


def split_principal(issuer: str, value: str) -> str | None:
    """The subject of a local principal under `issuer`, or None."""
    prefix = f"oidc:{issuer}:"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    sub = value[len(prefix):]
    if not re.fullmatch(r"acct-[0-9a-f]{32}", sub):
        return None
    return sub


# ------------------------------------------------------------------ store

def insert_account(con: sqlite3.Connection, issuer: str, email: str,
                   password_hash: str | None, now: float) -> dict:
    """Insert one account; never replaces. DuplicateAccount on a taken
    email (any issuer) or principal."""
    key, display = canonical(email)
    sub = subject(issuer, key)
    try:
        con.execute("INSERT INTO local_accounts (issuer, subject, email,"
                    " password_hash, password_set_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (issuer, sub, display, password_hash,
                     now if password_hash is not None else None, now))
    except sqlite3.IntegrityError:
        raise DuplicateAccount("an account with this email already exists") from None
    from .browser_auth import principal_identity
    return {"issuer": issuer, "subject": sub, "email": display,
            "principal": principal_identity(issuer, sub)}


def find_by_key(con: sqlite3.Connection, issuer: str, key: str):
    return con.execute("SELECT * FROM local_accounts WHERE issuer=? AND"
                       " lower(email)=?", (issuer, key)).fetchone()


def find_by_principal(con: sqlite3.Connection, issuer: str, value: str):
    sub = split_principal(issuer, value)
    if sub is None:
        return None
    return con.execute("SELECT * FROM local_accounts WHERE issuer=? AND"
                       " subject=?", (issuer, sub)).fetchone()
