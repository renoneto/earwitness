"""Provider-issued agent access tokens: JWT verification (L10).

The hosted resource server accepts two bearer kinds. Locally issued
opaque grants resolve first (connections.GrantsStore.resolve_token).
Only an unknown bearer reaches this verifier, which checks a provider
JWT access token: an allow-listed asymmetric signature against the
configured JWKS, an RFC 9068 ``typ`` (``at+jwt``; switchable), the exact
issuer, the MCP resource URI as audience, expiry and not-before with
connections.CLOCK_SKEW_S, finite non-bool time claims, and a capped
lifetime. ``alg: none``,
symmetric algorithms, and weak, non-signing, symmetric or private JWKS
keys are refused.

A verified token is not access by itself: connections.resolve_claims
then requires the private grant binding (``egrant``), checks that the
token's principal and client own that grant, intersects scopes, and
loads the workspace from the server-side grant.

Off unless configured: create_app installs it only when both the
issuer and a JWKS file are set. The JWKS is read once from a local
file; nothing here makes network calls.
"""
from __future__ import annotations

import base64
import json

from earwitness.app import connections as _conn
from earwitness.app.browser_auth import ALLOWED_ALGS

CLOCK_SKEW_S = _conn.CLOCK_SKEW_S
MAX_LIFETIME_S = 900
MIN_RSA_BITS = 2048
ACCESS_TOKEN_TYPES = ("at+jwt", "application/at+jwt")
PRIVATE_PARAMS = ("d", "p", "q", "dp", "dq", "qi")


def _deny() -> _conn.GrantError:
    # One indistinguishable answer for every failure: no reason leaks.
    return _conn.GrantError("UNAUTHORIZED", "invalid token", 401)


def _header(token: str) -> dict:
    try:
        segment = token.split(".")[0]
        padded = segment + "=" * (-len(segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, RecursionError):
        raise _deny()
    if not isinstance(header, dict) or not isinstance(header.get("alg"), str):
        raise _deny()
    return header


def load_jwks(path: str):
    """Import a local JWKS file as a joserfc KeySet of public
    signature keys. Symmetric ('oct') and private keys are refused:
    either would be a misconfiguration (a shared secret or private
    material in verifier config). So are RSA keys under 2048 bits and
    keys whose ``use`` is not ``sig``."""
    from joserfc.jwk import KeySet
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list) or not keys:
        raise ValueError("JWKS has no keys")
    for key in keys:
        if not isinstance(key, dict) or key.get("kty") not in ("RSA", "EC"):
            raise ValueError("JWKS keys must be RSA or EC public keys")
        if any(name in key for name in PRIVATE_PARAMS):
            raise ValueError("JWKS must not contain private key material")
        if key.get("use", "sig") != "sig":
            raise ValueError("JWKS keys must be signature keys")
        if key["kty"] == "RSA" and _rsa_bits(key) < MIN_RSA_BITS:
            raise ValueError("JWKS RSA keys must be at least 2048 bits")
    return KeySet.import_key_set({"keys": keys})


def _rsa_bits(key: dict) -> int:
    modulus = str(key.get("n", ""))
    raw = base64.urlsafe_b64decode(modulus + "=" * (-len(modulus) % 4))
    return int.from_bytes(raw, "big").bit_length()


class ProviderTokenVerifier:
    """Callable: token -> verified claims, or raises GrantError 401."""

    def __init__(self, *, issuer: str, audience: str, key_set, clock,
                 max_lifetime_s: int = MAX_LIFETIME_S,
                 require_at_jwt: bool = True):
        if not issuer or not audience:
            raise ValueError("provider tokens need an issuer and an audience")
        self.issuer = issuer
        self.audience = audience
        self.key_set = key_set
        self.clock = clock
        self.max_lifetime_s = max_lifetime_s
        self.require_at_jwt = require_at_jwt

    def __call__(self, token: str) -> dict:
        from joserfc import jwt
        from joserfc.jwt import JWTClaimsRegistry
        if not isinstance(token, str) or token.count(".") != 2:
            raise _deny()
        header = _header(token)
        if header["alg"] not in ALLOWED_ALGS:
            raise _deny()
        if self.require_at_jwt and (
                not isinstance(header.get("typ"), str)
                or header["typ"].lower() not in ACCESS_TOKEN_TYPES):
            raise _deny()
        try:
            decoded = jwt.decode(token, self.key_set,
                                 algorithms=sorted(ALLOWED_ALGS))
            JWTClaimsRegistry(
                now=lambda: int(self.clock()), leeway=CLOCK_SKEW_S,
                iss={"essential": True, "value": self.issuer},
                aud={"essential": True, "value": self.audience},
                exp={"essential": True},
                nbf={"essential": False},
            ).validate(decoded.claims)
        except Exception:
            raise _deny()
        claims = decoded.claims
        if not all(_conn.finite_time(claims[name])
                   for name in _conn.TIME_CLAIMS if name in claims):
            raise _deny()
        start = claims["iat"] if "iat" in claims else self.clock()
        if claims["exp"] - start > self.max_lifetime_s:
            raise _deny()
        return claims


def from_config(config):
    """The verifier for a HostedConfig, or None when provider tokens
    are not configured (issuer and JWKS file both required). A JWKS
    without an MCP resource URI fails loudly rather than verifying
    against an empty audience."""
    if not (config.issuer and config.jwks_file):
        return None
    if not config.resource_uri:
        raise ValueError("provider tokens need EARWITNESS_MCP_RESOURCE_URI")
    return ProviderTokenVerifier(
        issuer=config.issuer, audience=config.resource_uri,
        key_set=load_jwks(config.jwks_file), clock=config.now,
        max_lifetime_s=config.max_token_lifetime_s,
        require_at_jwt=config.require_at_jwt)
