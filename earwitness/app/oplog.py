"""Structured operational logs and the error-reporting hook.

One JSON object per line on the `earwitness.ops` logger. Every line
carries the current request ID and job ID when they are bound. Field
values pass through `redact` first: secret-shaped keys and values, and
transcript text keys, never reach a log line or an error report.

The error-reporting hook is vendor neutral and off by default. With no
sink set, `ErrorReporter.report` returns at once: it imports nothing,
opens nothing and makes no outbound call.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
import time
import uuid

LOGGER = logging.getLogger("earwitness.ops")

_REQUEST_ID: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_request_id", default=None)
_JOB_ID: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_job_id", default=None)

REDACTED = "[redacted]"
REDACTED_TEXT = "[redacted-text]"
MAX_STRING = 300
MAX_DEPTH = 6

# Keys whose values are credentials, whatever they hold.
_SECRET_KEY = re.compile(
    r"token|secret|passw|authoriz|cookie|api[_-]?key|bearer|csrf|session|"
    r"signature|credential|verifier|private[_-]?key", re.I)
# Keys whose values are transcript or query text.
_SECRET_KEY_EXACT = re.compile(
    r"^(code|auth_code|authorization_code|oauth_code|state)$", re.I)
_TEXT_KEY_EXACT = re.compile(r"^(evidence|lines?)$", re.I)
_TEXT_KEY = re.compile(
    r"transcript|(^|[_.\-])(text|segments?|snippets?|quote|excerpt|utterance|"
    r"words|body|content|query|prompt|notes?|q)($|[_.\-])", re.I)
# Values that look like credentials: bearer headers and long random runs
# (the app's tokens are 32+ byte url-safe strings).
_BEARER_VALUE = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;\"']+")
# OAuth values inside URLs, forms and JSON text.
_PARAM_VALUE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|token|code|client_secret|"
    r"password|state)=[^&\s\"']*")
_JSON_VALUE = re.compile(
    r"(?i)([\"'](?:access_token|refresh_token|id_token|token|code|client_secret|"
    r"password)[\"']\s*:\s*)[\"'][^\"']*[\"']")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9_\-]{24,}")
_REQUEST_ID_OK = re.compile(r"[A-Za-z0-9._\-]{8,64}")
# Correlation ids clients commonly send (UUID, W3C trace id). Never the
# app's tokens, which are url-safe base64; logged as sent for this field.
_TRACE_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32}")

# Stripe event and subscription ids (billing webhook correlation), kept
# only in their own field and only in this exact shape (Stripe suffixes
# are 24 characters today; app tokens are 43+, so longer stays redacted).
_PROVIDER_IDS = {"event_id": re.compile(r"evt_[A-Za-z0-9]{1,32}"),
                 "subscription_id": re.compile(r"sub_[A-Za-z0-9]{1,32}")}


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def valid_request_id(value) -> bool:
    return isinstance(value, str) and bool(_REQUEST_ID_OK.fullmatch(value))


def scrub_text(value: str) -> str:
    value = _BEARER_VALUE.sub(lambda m: m.group(1) + " " + REDACTED, value)
    value = _PARAM_VALUE.sub(lambda m: m.group(1) + "=" + REDACTED, value)
    value = _JSON_VALUE.sub(lambda m: m.group(1) + '"' + REDACTED + '"', value)
    value = _TOKEN_RUN.sub(REDACTED, value)
    if len(value) > MAX_STRING:
        value = value[:MAX_STRING] + "...[truncated]"
    return value


def redact(value, key: str = "", _depth: int = 0):
    """Return a copy of `value` that is safe to log."""
    if key == "inbound_request_id" and isinstance(value, str) \
            and _TRACE_ID.fullmatch(value):
        return value
    if key in _PROVIDER_IDS and isinstance(value, str) \
            and _PROVIDER_IDS[key].fullmatch(value):
        return value
    if key:
        if _SECRET_KEY.search(key) or _SECRET_KEY_EXACT.match(key):
            return REDACTED
        if _TEXT_KEY.search(key) or _TEXT_KEY_EXACT.match(key):
            return REDACTED_TEXT
    if _depth >= MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k), _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v, key, _depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (bytes, bytearray)):
        return "[bytes]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_text(str(value))


def error_location(exc: BaseException) -> str:
    """file:function:line of the innermost frame; no values."""
    tb = exc.__traceback__
    where = ""
    while tb is not None:
        code = tb.tb_frame.f_code
        where = f"{code.co_filename.rsplit('/', 1)[-1]}:{code.co_name}:{tb.tb_lineno}"
        tb = tb.tb_next
    return where


def current_request_id():
    return _REQUEST_ID.get()


def current_job_id():
    return _JOB_ID.get()


def bind_request(request_id: str):
    return _REQUEST_ID.set(request_id)


def reset_request(token) -> None:
    _REQUEST_ID.reset(token)


@contextlib.contextmanager
def job_context(job_id: str):
    token = _JOB_ID.set(str(job_id))
    try:
        yield
    finally:
        _JOB_ID.reset(token)


def log_event(event: str, level: str = "info", **fields) -> dict:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "level": level, "event": event,
              "request_id": current_request_id(), "job_id": current_job_id()}
    record.update(redact(fields))
    LOGGER.log(getattr(logging, level.upper(), logging.INFO),
               json.dumps(record, sort_keys=True, default=str))
    return record


def configure_logging(stream=None):
    """Send the ops logger to `stream` (default stderr), one JSON line each.
    Idempotent: a second call returns the handler already installed."""
    for handler in LOGGER.handlers:
        if getattr(handler, "_earwitness_ops", False):
            return handler
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._earwitness_ops = True
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    return handler


class ErrorReporter:
    """Vendor-neutral error-reporting hook. Off unless a sink is given.

    The sink is any callable taking one redacted event dict. It is never
    handed an exception message, a traceback with values, or request
    content: only the error type, the code location, the request and job
    IDs, and redacted context fields.
    """

    def __init__(self, sink=None):
        self._sink = sink

    @property
    def enabled(self) -> bool:
        return self._sink is not None

    def report(self, exc: BaseException, **context) -> bool:
        if self._sink is None:
            return False
        event = {"error_type": type(exc).__name__,
                 "where": error_location(exc),
                 "request_id": current_request_id(),
                 "job_id": current_job_id(),
                 "context": redact(context)}
        try:
            self._sink(event)
        except Exception:  # noqa: BLE001 - a failing sink must not hide the error
            log_event("error_report.failed", level="warning")
            return False
        return True


def load_sink(path: str):
    """Resolve `module:callable` for EARWITNESS_ERROR_SINK. Called only when
    the setting is non-empty."""
    import importlib
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError("EARWITNESS_ERROR_SINK must look like module:callable")
    sink = getattr(importlib.import_module(module_name), attr)
    if not callable(sink):
        raise ValueError("EARWITNESS_ERROR_SINK is not callable")
    return sink
