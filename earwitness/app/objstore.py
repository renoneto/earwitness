"""Private tenant-scoped object storage with short-lived signed download links.

Stdlib only. This module is an interface plus a filesystem backend; no route
uses it yet (moving artifacts onto it is the L2b runtime port).

Keys
    A key is a tuple of segments (a bare ``str`` is one segment). Segments are
    chosen by the server, never copied from a request. A tenant id must match
    the workspace-id pattern ``[a-z0-9][a-z0-9-]{0,63}``. A segment is 1 to
    128 characters of lowercase ASCII ``[a-z0-9._-]`` and is never ``.`` or
    ``..``. A key has at most 16 segments and 512 characters in all (segments
    plus separators). Lowercase ASCII only means two names can never map to
    one folder on a case-insensitive or Unicode-normalising filesystem
    (APFS). ``InvalidKey`` is raised otherwise, including at ``signed_url``.

Isolation
    Objects live at ``<root>/tenants/<tenant>/<segment>/...``. Every operation
    takes the tenant, so one tenant cannot name another tenant's objects.
    Paths are canonicalised; any path that does not resolve to itself (a
    symlink anywhere along it) is refused with ``PathEscape``.

Signed links
    ``signed_url`` returns ``<prefix>/<tenant>/<seg>/...?exp=<unix>&sig=<hex>``.
    ``sig`` is HMAC-SHA256 over (tenant, key, expiry) using a server secret
    read from the environment variable named by ``secret_env`` (at least 32
    bytes). The store refuses to be built without that secret unless
    ``test_mode is True``, which uses a random per-instance secret. TTL is 1..900 seconds (``MAX_TTL_S``).
    ``verify_signed`` checks the signature (constant time), expiry, the TTL
    cap and that the link belongs to the tenant the caller authenticated as.
    There is no listing by link and no public listing.
"""
from __future__ import annotations

import abc
import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union
from urllib.parse import parse_qsl, quote, unquote, urlsplit

MAX_TTL_S = 900
MIN_SECRET_LEN = 32
DEFAULT_SECRET_ENV = "EARWITNESS_OBJSTORE_SECRET"
DEFAULT_URL_PREFIX = "/objects"
MAX_SEGMENT_LEN = 128
MAX_KEY_SEGMENTS = 16
MAX_KEY_BYTES = 512
OBJECT_MAGIC = b"EWOBJ1 "
_TENANT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SEGMENT_RE = re.compile(r"[a-z0-9._-]{1,%d}" % MAX_SEGMENT_LEN)

Key = Union[str, Sequence[str]]


class ObjectStoreError(Exception):
    pass


class InvalidKey(ObjectStoreError, ValueError):
    """A tenant id or key segment is not allowed."""


class PathEscape(InvalidKey):
    """The path resolves somewhere other than where its segments say."""


class InvalidTTL(ObjectStoreError, ValueError):
    pass


class ObjectNotFound(ObjectStoreError, KeyError):
    pass


class SecretMissing(ObjectStoreError):
    """No usable signing secret is configured."""


class LinkRefused(ObjectStoreError):
    """A signed link is missing, malformed, forged, expired or not theirs.

    ``reason`` is for logs only; callers show one generic refusal.
    """

    def __init__(self, reason: str):
        super().__init__("signed link refused")
        self.reason = reason


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str


@dataclass(frozen=True)
class SignedRef:
    tenant: str
    key: tuple
    expires_at: int


def _segment(value) -> str:
    if (not isinstance(value, str) or value in (".", "..")
            or not _SEGMENT_RE.fullmatch(value)):
        raise InvalidKey(f"key segment not allowed: {value!r}")
    return value


def _tenant(value) -> str:
    if not isinstance(value, str) or not _TENANT_RE.fullmatch(value):
        raise InvalidKey(f"tenant id not allowed: {value!r}")
    return value


def _key(value, *, allow_empty: bool = False) -> tuple:
    if isinstance(value, str):
        value = (value,)
    elif not isinstance(value, (tuple, list)):
        raise InvalidKey("key must be a string or a sequence of strings")
    segments = tuple(_segment(seg) for seg in value)
    if not segments and not allow_empty:
        raise InvalidKey("key is empty")
    if len(segments) > MAX_KEY_SEGMENTS:
        raise InvalidKey("key has too many segments")
    if sum(map(len, segments)) + max(len(segments) - 1, 0) > MAX_KEY_BYTES:
        raise InvalidKey("key is too long")
    return segments


def _fail(action: str, exc: OSError) -> ObjectStoreError:
    """A generic error: no path, no root, no OS cause chain."""
    return ObjectStoreError(
        f"cannot {action}: {errno.errorcode.get(exc.errno, 'os error')}")


def _content_type(value) -> str:
    ok = (isinstance(value, str) and len(value) <= 127
          and value.count("/") == 1 and value.isascii()
          and all(c.isalnum() or c in "/!#$&^_.+-" for c in value)
          and not value.startswith("/") and not value.endswith("/"))
    if not ok:
        raise ValueError(f"invalid content type: {value!r}")
    return value


class ObjectStore(abc.ABC):
    """Tenant-scoped blob storage. Signing is shared; storage is per backend.

    put(tenant, key, data, content_type)   store bytes, overwriting in-tenant
    get(tenant, key) -> StoredObject       raises ObjectNotFound
    delete(tenant, key) -> bool            True if an object was removed
    list(tenant, prefix) -> [key, ...]     sorted key tuples under the prefix
                                           (a segment-level prefix, ``()`` for
                                           all); never crosses tenants
    signed_url(tenant, key, ttl_s) -> str  download link, ttl 1..MAX_TTL_S
    verify_signed(url, tenant) -> SignedRef
                                           raises LinkRefused; ``tenant`` is the
                                           tenant the caller authenticated as
    """

    def __init__(self, *, secret_env: str = DEFAULT_SECRET_ENV,
                 test_mode: bool = False,
                 environ: Optional[Mapping[str, str]] = None,
                 clock: Callable[[], float] = time.time,
                 url_prefix: str = DEFAULT_URL_PREFIX):
        env = os.environ if environ is None else environ
        raw = (env.get(secret_env) or "").strip()
        if not raw:
            if test_mode is not True:
                raise SecretMissing(
                    f"signing secret not set: export {secret_env}")
            raw = secrets.token_hex(32)
        secret = raw.encode("utf-8")
        if len(secret) < MIN_SECRET_LEN:
            raise SecretMissing(
                f"{secret_env} must be at least {MIN_SECRET_LEN} bytes")
        if not isinstance(url_prefix, str) or not url_prefix.strip("/"):
            raise ValueError("url_prefix must not be empty")
        self._secret = secret
        self._clock = clock
        self._prefix = "/" + url_prefix.strip("/")

    @abc.abstractmethod
    def put(self, tenant: str, key: Key, data: bytes,
            content_type: str) -> None: ...

    @abc.abstractmethod
    def get(self, tenant: str, key: Key) -> StoredObject: ...

    @abc.abstractmethod
    def delete(self, tenant: str, key: Key) -> bool: ...

    @abc.abstractmethod
    def list(self, tenant: str, prefix: Key = ()) -> list: ...

    # -- signed links ---------------------------------------------------

    def _sign(self, tenant: str, key: tuple, expires_at: int) -> str:
        msg = json.dumps(["ew-objstore-v1", tenant, list(key), expires_at],
                         separators=(",", ":"), ensure_ascii=True)
        return hmac.new(self._secret, msg.encode("ascii"),
                        hashlib.sha256).hexdigest()

    def signed_url(self, tenant: str, key: Key, ttl_s: float) -> str:
        tenant, key = _tenant(tenant), _key(key)
        if (isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))
                or not math.isfinite(ttl_s) or ttl_s <= 0
                or ttl_s > MAX_TTL_S):
            raise InvalidTTL(f"ttl_s must be in (0, {MAX_TTL_S}]")
        expires_at = int(self._clock()) + math.ceil(ttl_s)
        path = "/".join(quote(s, safe="") for s in (tenant, *key))
        return (f"{self._prefix}/{path}?exp={expires_at}"
                f"&sig={self._sign(tenant, key, expires_at)}")

    def verify_signed(self, url: str, tenant: str) -> SignedRef:
        """Return the tenant and key a link grants, or raise LinkRefused."""
        if not isinstance(url, str) or not isinstance(tenant, str):
            raise LinkRefused("malformed")
        try:
            parts = urlsplit(url)
            query = parse_qsl(parts.query, keep_blank_values=True)
        except ValueError:
            raise LinkRefused("malformed") from None
        names = [k for k, _ in query]
        if len(set(names)) != len(names):
            raise LinkRefused("malformed")
        q = dict(query)
        if not q.get("sig") or not q.get("exp"):
            raise LinkRefused("unsigned")
        head = self._prefix + "/"
        if not parts.path.startswith(head):
            raise LinkRefused("malformed")
        try:
            raw = [unquote(s) for s in parts.path[len(head):].split("/")]
            link_tenant, link_key = _tenant(raw[0]), _key(raw[1:])
        except InvalidKey:
            raise LinkRefused("malformed") from None
        exp_text = q["exp"]
        if not (exp_text.isascii() and exp_text.isdigit()) or len(exp_text) > 12:
            raise LinkRefused("malformed")
        expires_at = int(exp_text)
        expected = self._sign(link_tenant, link_key, expires_at)
        if not hmac.compare_digest(expected.encode("ascii"),
                                   q["sig"].encode("utf-8")):
            raise LinkRefused("signature")
        now = self._clock()
        if now >= expires_at:
            raise LinkRefused("expired")
        if expires_at - now > MAX_TTL_S:
            raise LinkRefused("ttl")
        if link_tenant != tenant:
            raise LinkRefused("tenant")
        return SignedRef(link_tenant, link_key, expires_at)


class FilesystemObjectStore(ObjectStore):
    """Objects under ``<root>/tenants/<tenant>/<key...>``.

    Each object is one file: ``EWOBJ1 <content-type>\\n`` then the data. It is
    written to ``<root>/tmp`` and published with one atomic rename, so bytes
    and content type always change together and a partial write is never
    readable. A symlink anywhere on an object's path is refused, so nothing
    outside the tenant directory can be read, written or deleted. The check is
    made just before use; the root must not be writable by untrusted local
    users. Errors never carry the root path.
    """

    def __init__(self, root, **kwargs):
        super().__init__(**kwargs)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = Path(os.path.realpath(self.root))
        (self.root / "tmp").mkdir(exist_ok=True)

    def __repr__(self):
        return f"FilesystemObjectStore(root={str(self.root)!r})"

    def _path(self, tenant: str, key: tuple) -> Path:
        lexical = self.root.joinpath("tenants", tenant, *key)
        if Path(os.path.realpath(lexical)) != lexical:
            raise PathEscape("path resolves outside its tenant directory")
        return lexical

    def put(self, tenant, key, data, content_type):
        tenant, key = _tenant(tenant), _key(key)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        header = OBJECT_MAGIC + _content_type(content_type).encode() + b"\n"
        path = self._path(tenant, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root / "tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(header)
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise _fail("store object", exc) from None

    @staticmethod
    def _read(path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def get(self, tenant, key):
        tenant, key = _tenant(tenant), _key(key)
        path = self._path(tenant, key)
        try:
            raw = self._read(path)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise ObjectNotFound(key) from None
        except OSError as exc:
            raise _fail("read object", exc) from None
        head, sep, data = raw.partition(b"\n")
        try:
            if not (sep and head.startswith(OBJECT_MAGIC)):
                raise ValueError
            content_type = _content_type(
                head[len(OBJECT_MAGIC):].decode("ascii"))
        except ValueError:
            raise ObjectStoreError("corrupt object") from None
        return StoredObject(data, content_type)

    def delete(self, tenant, key):
        tenant, key = _tenant(tenant), _key(key)
        path = self._path(tenant, key)
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return False
            os.unlink(path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as exc:
            raise _fail("delete object", exc) from None
        return True

    @staticmethod
    def _is_object(path: str) -> bool:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                return fh.read(len(OBJECT_MAGIC)) == OBJECT_MAGIC
        except OSError:
            return False

    def list(self, tenant, prefix=()):
        tenant, prefix = _tenant(tenant), _key(prefix, allow_empty=True)
        try:
            base = self._path(tenant, prefix)
        except PathEscape:
            return []
        found = []

        def walk(directory: Path, trail: tuple):
            if len(trail) >= MAX_KEY_SEGMENTS:
                return
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except (FileNotFoundError, NotADirectoryError):
                return
            for entry in entries:
                name = entry.name
                if not _SEGMENT_RE.fullmatch(name) or name in (".", ".."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), trail + (name,))
                elif (entry.is_file(follow_symlinks=False)
                        and self._is_object(entry.path)):
                    found.append(trail + (name,))

        try:
            walk(base, prefix)
        except OSError as exc:
            raise _fail("list objects", exc) from None
        return sorted(found)
