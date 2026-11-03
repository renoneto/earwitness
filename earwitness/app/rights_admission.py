"""Pilot rights admission (hosted runtime design, section 5.2; task B2).

A pilot workspace has at most one admission record, for one source
collection. The record is signed by Reno, never changes while the pilot
runs, and bounds three things:

- the source permissions of the workspace (the permission ceiling);
- the features that widen the audience (sharing, agent access, invites);
- the dates: serving ends at the approved pilot end, and every copy may be
  kept until pilot end + d.

This module holds the rules. `scripts/app_rights.py` approves a record and
checks a proposed permission set; `health.check_config` calls
`startup_problems` in production; the hosted app calls `feature_enabled`
at two choke points; the pilot import adapter (task B5) calls `check`.
Standard library only; signatures are checked with the `ssh-keygen` binary.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from urllib.parse import quote

FEATURES = ("invites", "sharing", "agent_access", "ask", "meaning_alerts",
            "brief_check", "brief_check_live")
# The use a feature exposes, per category (5.2.2). `ask` and
# `meaning_alerts` send text and passages to a model provider: their use,
# model_read, is granted by the workspace's model-processing addendum, never
# by the rights record.
FEATURE_USE = {"invites": "member_invite", "sharing": "share_view",
               "agent_access": "agent_read", "ask": "model_read",
               "meaning_alerts": "model_read", "brief_check_live": "model_read"}
# Features never implied by an unset EARWITNESS_PILOT_FEATURES (Ask design
# 8.1): on only when listed, in feature_enabled and in the import check.
# `brief_check` (WIN-2) shows imported Muse Spark brief checks in the campaign
# draft; the host makes no model call for it, so it needs no FEATURE_USE.
# `brief_check_live` (LIVE-BRIEF-CHECK) sends a placement's ads, with 15 s of
# transcript around each, and the campaign's brief to the model when a
# member presses "Check with Muse Spark": Ask's addendum check, at Ask's level.
# `brief_check_live` may be switched on for the judges' pilot once the new
# public row 60 (Judges' access page), approved by Reno 2026-10-29, is live on
# the host; until then it stays OFF. The Nov 2 install keeps it off by default.
MODEL_FEATURES = ("ask", "meaning_alerts", "brief_check", "brief_check_live")
CATEGORIES = ("metadata", "excerpt", "transcript")
# Audio clips of detected ads (clip design 5.1): members may play them and,
# with invites on, invitees may too. No other use pairs with audio, and audio
# is outside the agent and share coverage loop over CATEGORIES.
AUDIO = "audio"
AUDIO_USES = ("member_view", "member_invite")
POLICY_CATEGORIES = CATEGORIES + (AUDIO,)
USES = ("member_view", "member_invite", "share_view", "agent_read", "export",
        "acquire_transcript", "store_transcript", "acquire_metadata")
BASES = ("customer_owned", "license", "consent", "public_evaluation")
# public_evaluation: public podcast episodes used only to evaluate
# EarWitness (Reno's decision A, 2026-10-21). It never serves a paying
# customer: see basis_allows_paid_use.
PAID_BASES = ("customer_owned", "license", "consent")
NAMESPACE = "earwitness-rights-admission"

SIGNED_FIELDS = ("admission_id", "workspace_identity", "collection_id",
                 "origin", "basis", "basis_reference_digest", "territory",
                 "valid_from", "valid_until", "retention_until", "pilot_end",
                 "deletion_days", "uses", "approved_at")
GENERATED_FIELDS = ("approved_by", "approval_file_sha256", "record_sha256")
COLUMNS = SIGNED_FIELDS + GENERATED_FIELDS

# Created by `app_rights.py approve` on the stopped control store.
DDL = """
CREATE TABLE IF NOT EXISTS pilot_rights_admissions (
  admission_id TEXT PRIMARY KEY,
  workspace_identity TEXT NOT NULL UNIQUE,
  collection_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('customer_owned','license','consent','public_evaluation')),
  basis_reference_digest TEXT NOT NULL,
  territory TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
  retention_until TEXT NOT NULL,
  pilot_end TEXT NOT NULL,
  deletion_days INTEGER NOT NULL,
  uses TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approval_file_sha256 TEXT NOT NULL,
  record_sha256 TEXT NOT NULL)
"""

# Formats (5.2.1 step 5). ASCII classes only: \d would accept other digits.
_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_IDENTITY = re.compile(r"ws-[0-9a-f]{16}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                      r"(?:\.[0-9]{1,6})?Z")

_INVITE_POST = re.compile(
    r"/api/v1/workspaces/[^/]+/invites(?:/[^/]+/(?:deliver|send|accept))?")
# L25's invitee preflight (GET) reads an invitation: the same switch.
_INVITE_GET = re.compile(r"/api/v1/workspaces/[^/]+/invites/[^/]+/preflight")
# Every Ask route (any method) is behind the `ask` switch.
ASK_PATH = "/api/v1/customer/ask"
# Every meaning-alert route (any method) is behind the `meaning_alerts` switch.
MEANING_PATH = "/api/v1/customer/meaning-alerts"
# Every live brief-check route (any method) is behind `brief_check_live`.
BRIEF_CHECK_PATH = "/api/v1/customer/brief-check"
# The approved public and in-app wording pins (App owns the file). With
# meaning_alerts on, start-up needs every meaning_alerts pair in its page.
PUBLIC_ROWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_rows",
                                "public_rows.json")


class RightsRefused(Exception):
    """A rights rule refused the operation; `code` names the rule."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ---------------------------------------------------------------- features

def parse_features(raw):
    """EARWITNESS_PILOT_FEATURES -> None (unset) or a tuple of names.

    Unknown names are kept so the start-up check can refuse them."""
    if raw is None:
        return None
    return tuple(name.strip() for name in raw.split(",") if name.strip())


def feature_enabled(config, name: str) -> bool:
    """The one predicate. Unset (None) means every feature except the
    model features (MODEL_FEATURES), which only a configuration outside
    production can have: production refuses to start without the
    variable."""
    features = getattr(config, "pilot_features", None)
    if features is None:
        return name not in MODEL_FEATURES
    return name in features


def pilot_feature(method: str, path: str):
    """The feature a collaboration request needs, or None (5.2.2)."""
    if path.startswith("/api/v1/shared/") or (
            method == "POST" and path == "/api/v1/shares"):
        return "sharing"
    if (method == "POST" and _INVITE_POST.fullmatch(path)) or (
            method == "GET" and _INVITE_GET.fullmatch(path)):
        return "invites"
    if path == ASK_PATH or path.startswith(ASK_PATH + "/"):
        return "ask"
    if path == MEANING_PATH or path.startswith(MEANING_PATH + "/"):
        return "meaning_alerts"
    if path == BRIEF_CHECK_PATH or path.startswith(BRIEF_CHECK_PATH + "/"):
        return "brief_check_live"
    return None


# ------------------------------------------------------------------- dates

def parse_instant(text) -> _dt.datetime:
    """A UTC ISO 8601 time ending in Z."""
    if not isinstance(text, str) or not _INSTANT.fullmatch(text):
        raise ValueError("not a UTC ISO 8601 time ending in Z")
    return _dt.datetime.fromisoformat(text[:-1]).replace(tzinfo=_dt.timezone.utc)


_DEPLOYED_END = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                           r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})")


def parse_deployed_end(text) -> _dt.datetime:
    """The deployed end (layout pilot_end, EARWITNESS_PILOT_END): ISO 8601
    with Z or an explicit offset, normalised to a UTC instant."""
    if not isinstance(text, str) or not _DEPLOYED_END.fullmatch(text):
        raise ValueError("not an ISO 8601 time with Z or an explicit offset")
    value = text[:-1] + "+00:00" if text.endswith("Z") else text
    return _dt.datetime.fromisoformat(value).astimezone(_dt.timezone.utc)


def parse_date(text) -> _dt.date:
    if not isinstance(text, str) or not _DATE.fullmatch(text):
        raise ValueError("not a YYYY-MM-DD date")
    return _dt.date.fromisoformat(text)


def _now_dt(now) -> _dt.datetime:
    if isinstance(now, _dt.datetime):
        return now.astimezone(_dt.timezone.utc)
    return _dt.datetime.fromtimestamp(float(now), _dt.timezone.utc)


def date_problem(record: dict, pilot_end: str, deletion_days: int):
    """Check 6 / approval step 4. A date field covers its whole UTC day:
    valid_until must be on or after the pilot end's day, and
    retention_until on or after the day of pilot_end + d."""
    try:
        end = parse_deployed_end(pilot_end)
        if parse_instant(record["pilot_end"]) != end:
            return "PILOT_END_MISMATCH"
    except ValueError:
        return "PILOT_END_MISMATCH"
    if parse_date(record["valid_until"]) < end.date():
        return "LAPSES_DURING_PILOT"
    deadline = end + _dt.timedelta(days=int(deletion_days))
    if parse_date(record["retention_until"]) < deadline.date():
        return "RETENTION_SHORT"
    return None


def policy_problems(policy: dict, layout: dict | None = None) -> list:
    """L5p 3.4 date rules: a <= c, c + R <= d, a + L + R_log <= d.

    The same rules as `pilot_inventory.py policy-check` (L5p T1), with
    the layout's log store; `scripts/app_rights.py` runs that command
    instead when it exists."""
    names = ("a", "c", "d", "L", "R", "R_log")
    values = {}
    for name in names:
        value = policy.get(name) if isinstance(policy, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return [f"policy {name} must be a whole number of days"]
        values[name] = value
    a, c, d, log, r, r_log = (values[n] for n in names)
    problems = []
    if a > c:
        problems.append("a > c")
    if c + r > d:
        problems.append("c + R > d")
    if a + log + r_log > d:
        problems.append("a + L + R_log > d")
    if d < 1:
        problems.append("d must be at least one day")
    store = (layout or {}).get("log_store")
    if isinstance(store, dict):
        if isinstance(store.get("retention_days"), int) and store["retention_days"] > log:
            problems.append("the log store keeps logs longer than L")
        if store.get("snapshotted") is True and r_log == 0:
            problems.append("the log volume is snapshotted: R_log must be its snapshot lifetime")
    return problems


# ------------------------------------------------------- record and digest

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_sha256(fields: dict) -> str:
    """SHA-256 of the canonical JSON of the 14 signed fields plus
    approved_by and approval_file_sha256 (never record_sha256 itself)."""
    obj = {k: fields[k] for k in SIGNED_FIELDS}
    obj["approved_by"] = fields["approved_by"]
    obj["approval_file_sha256"] = fields["approval_file_sha256"]
    return sha256(canonical(obj))


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise RightsRefused("FIELD_SET", "duplicate key in approval file")
        out[key] = value
    return out


def load_approval(raw: bytes) -> dict:
    """The approval file: one UTF-8 JSON object with exactly the 14 signed
    keys and the right value types, every value in its format."""
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except RightsRefused:
        raise
    except (UnicodeDecodeError, ValueError):
        raise RightsRefused("FIELD_FORMAT", "approval file is not UTF-8 JSON") from None
    if not isinstance(obj, dict) or set(obj) != set(SIGNED_FIELDS):
        raise RightsRefused("FIELD_SET", "approval file must hold exactly the 14 signed keys")
    bad = field_format_problems(obj)
    if bad:
        raise RightsRefused("FIELD_FORMAT", "bad format: " + ", ".join(bad))
    return obj


def field_format_problems(fields: dict) -> list:
    """Names of the fields that fail their format (step 5). A limited
    guard: it cannot tell a neutral code from a name written as one
    lowercase word; Reno's review before signing covers that."""
    bad = []

    def check(name, ok):
        if not ok:
            bad.append(name)

    for name in ("admission_id", "collection_id", "origin"):
        value = fields.get(name)
        check(name, isinstance(value, str) and bool(_CODE.fullmatch(value)))
    value = fields.get("workspace_identity")
    check("workspace_identity", isinstance(value, str) and bool(_IDENTITY.fullmatch(value)))
    check("basis", fields.get("basis") in BASES)
    value = fields.get("basis_reference_digest")
    check("basis_reference_digest", isinstance(value, str) and bool(_HEX64.fullmatch(value)))
    for name in ("valid_from", "valid_until", "retention_until"):
        try:
            parse_date(fields.get(name))
        except ValueError:
            bad.append(name)
    for name in ("pilot_end", "approved_at"):
        try:
            parse_instant(fields.get(name))
        except ValueError:
            bad.append(name)
    territory = fields.get("territory")
    check("territory", isinstance(territory, list) and bool(territory) and (
        territory == ["WORLD"] or (
            all(isinstance(t, str) and _COUNTRY.fullmatch(t) for t in territory)
            and len(set(territory)) == len(territory))))
    uses = fields.get("uses")
    pairs_ok = isinstance(uses, list) and all(
        isinstance(p, list) and len(p) == 2 and p[0] in USES and (
            p[1] in CATEGORIES or (p[1] == AUDIO and p[0] in AUDIO_USES))
        for p in uses)
    check("uses", pairs_ok and len({tuple(p) for p in uses}) == len(uses))
    days = fields.get("deletion_days")
    check("deletion_days", isinstance(days, int) and not isinstance(days, bool) and days > 0)
    return bad


def build_record(raw: bytes, principal: str) -> dict:
    """The record a verified approval file produces: the 14 signed values
    and the 3 generated ones. A pure function of its inputs."""
    record = dict(load_approval(raw))
    record["approved_by"] = principal
    record["approval_file_sha256"] = sha256(raw)
    record["record_sha256"] = record_sha256(record)
    return record


def to_row(record: dict) -> dict:
    row = dict(record)
    row["territory"] = canonical(record["territory"]).decode("utf-8")
    row["uses"] = canonical(record["uses"]).decode("utf-8")
    return row


def row_fields(row) -> dict:
    """A stored row as a record (territory and uses parsed)."""
    fields = {k: row[k] for k in COLUMNS}
    fields["territory"] = json.loads(row["territory"])
    fields["uses"] = json.loads(row["uses"])
    return fields


def row_digest_ok(row) -> bool:
    """record_sha256 recomputed from the row alone equals the stored one."""
    try:
        return record_sha256(row_fields(row)) == row["record_sha256"]
    except (TypeError, ValueError, KeyError):
        return False


def row_equals_file(row, raw: bytes, principal: str) -> bool:
    """5.2.1 "Row equals file": each signed value equals the file's,
    approved_by is the principal verified now, approval_file_sha256 is the
    file's digest and record_sha256 matches the row."""
    try:
        fields = load_approval(raw)
        stored = row_fields(row)
    except (RightsRefused, TypeError, ValueError, KeyError):
        return False
    return (all(stored[k] == fields[k] for k in SIGNED_FIELDS)
            and stored["approved_by"] == principal
            and stored["approval_file_sha256"] == sha256(raw)
            and row_digest_ok(row))


# ------------------------------------------------------------- signatures

def verify_signature(data_path: str, sig_path: str, allowed_signers: str,
                     namespace: str = NAMESPACE) -> str:
    """Return the principal that signed `data_path` in `namespace` (the
    rights namespace by default), or refuse (SIGNATURE_INVALID)."""
    keygen = shutil.which("ssh-keygen")
    if not keygen or not all(os.path.isfile(p) for p in
                             (data_path, sig_path, allowed_signers)):
        raise RightsRefused("SIGNATURE_INVALID", "signature or signer list missing")
    found = subprocess.run(
        [keygen, "-Y", "find-principals", "-f", allowed_signers, "-s", sig_path],
        capture_output=True, text=True, timeout=30)
    principals = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    if found.returncode != 0 or len(principals) != 1:
        raise RightsRefused("SIGNATURE_INVALID", "no single allowed signer")
    with open(data_path, "rb") as handle:
        verified = subprocess.run(
            [keygen, "-Y", "verify", "-f", allowed_signers, "-I", principals[0],
             "-n", namespace, "-s", sig_path],
            stdin=handle, capture_output=True, timeout=30)
    if verified.returncode != 0:
        raise RightsRefused("SIGNATURE_INVALID", "signature does not verify")
    return principals[0]


def bundle_paths(protected_dir: str, workspace_identity: str,
                 approval_sha: str) -> tuple:
    """Where an approval file and its signature live in the protected
    bundle. Named by digest, so a kept file is never overwritten."""
    folder = os.path.join(protected_dir, "rights", workspace_identity)
    data = os.path.join(folder, approval_sha + ".json")
    return data, data + ".sig"


def allowed_signers_path(protected_dir: str) -> str:
    return os.path.join(protected_dir, "allowed_signers")


def bundle_available(protected_dir: str) -> bool:
    """The protected bundle is mounted: its folder and signer list exist."""
    return os.path.isdir(protected_dir) and os.path.isfile(allowed_signers_path(protected_dir))


# ------------------------------------------- model-processing addendum (Ask)
#
# Ask sends questions and passages to a model provider, a new processor of
# workspace content (Ask design 8.1). The rights record never changes, so a
# separate addendum, signed by Reno in its own namespace, grants
# `model_read`: one row per workspace in `pilot_model_addenda`, never
# changed, bound to the workspace's record (admission_id), with a bundle
# copy beside the record's. Start-up and the import check refuse a row
# whose digest fails (ADDENDUM_TAMPERED); the import check also compares it
# with its signed file. Audio is never model-readable (a format error).

MODEL_USE = "model_read"
ADDENDUM_NAMESPACE = "earwitness-model-addendum"
PROCESSORS = ("meta-model-api",)
ADDENDUM_SIGNED_FIELDS = ("addendum_id", "workspace_identity", "admission_id",
                          "processor", "uses", "basis_note", "approved_at")
ADDENDUM_GENERATED = ("approved_by", "approval_file_sha256", "addendum_sha256")
ADDENDUM_COLUMNS = ADDENDUM_SIGNED_FIELDS + ADDENDUM_GENERATED
BASIS_NOTE_MAX = 500
# C0 and C1 controls, and the Unicode line and paragraph separators.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f  ]")

# Part of every control store (connections.GrantsStore), so approving an
# addendum never moves the pinned control schema.
ADDENDUM_DDL = """
CREATE TABLE IF NOT EXISTS pilot_model_addenda (
  addendum_id TEXT PRIMARY KEY,
  workspace_identity TEXT NOT NULL UNIQUE,
  admission_id TEXT NOT NULL,
  processor TEXT NOT NULL,
  uses TEXT NOT NULL,
  basis_note TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approval_file_sha256 TEXT NOT NULL,
  addendum_sha256 TEXT NOT NULL)
"""


def addendum_sha256(fields: dict) -> str:
    """SHA-256 of the canonical JSON of the 7 signed fields plus approved_by
    and approval_file_sha256 (never addendum_sha256 itself)."""
    obj = {k: fields[k] for k in ADDENDUM_SIGNED_FIELDS}
    obj["approved_by"] = fields["approved_by"]
    obj["approval_file_sha256"] = fields["approval_file_sha256"]
    return sha256(canonical(obj))


def addendum_format_problems(fields: dict) -> list:
    """Names of the addendum fields that fail their format."""
    bad = []
    for name in ("addendum_id", "admission_id"):
        value = fields.get(name)
        if not (isinstance(value, str) and _CODE.fullmatch(value)):
            bad.append(name)
    value = fields.get("workspace_identity")
    if not (isinstance(value, str) and _IDENTITY.fullmatch(value)):
        bad.append("workspace_identity")
    if fields.get("processor") not in PROCESSORS:
        bad.append("processor")
    uses = fields.get("uses")
    # Only [model_read, metadata|excerpt|transcript]; [model_read, audio]
    # and any other use are refused here.
    if not (isinstance(uses, list) and uses and all(
            isinstance(p, list) and len(p) == 2 and p[0] == MODEL_USE and p[1] in CATEGORIES
            for p in uses) and len({tuple(p) for p in uses}) == len(uses)):
        bad.append("uses")
    note = fields.get("basis_note")
    if not (isinstance(note, str) and note.strip() and len(note) <= BASIS_NOTE_MAX
            and not _CONTROL.search(note)):
        bad.append("basis_note")
    try:
        parse_instant(fields.get("approved_at"))
    except ValueError:
        bad.append("approved_at")
    return bad


def load_addendum(raw: bytes) -> dict:
    """The addendum approval file: one UTF-8 JSON object with exactly the 7
    signed keys, every value in its format."""
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except RightsRefused:
        raise
    except (UnicodeDecodeError, ValueError):
        raise RightsRefused("FIELD_FORMAT", "addendum file is not UTF-8 JSON") from None
    if not isinstance(obj, dict) or set(obj) != set(ADDENDUM_SIGNED_FIELDS):
        raise RightsRefused("FIELD_SET", "addendum file must hold exactly the 7 signed keys")
    bad = addendum_format_problems(obj)
    if bad:
        raise RightsRefused("FIELD_FORMAT", "bad format: " + ", ".join(bad))
    return obj


def build_addendum(raw: bytes, principal: str) -> dict:
    record = dict(load_addendum(raw))
    record["approved_by"] = principal
    record["approval_file_sha256"] = sha256(raw)
    record["addendum_sha256"] = addendum_sha256(record)
    return record


def addendum_to_row(record: dict) -> dict:
    row = dict(record)
    row["uses"] = canonical(record["uses"]).decode("utf-8")
    return row


def addendum_row_fields(row) -> dict:
    fields = {k: row[k] for k in ADDENDUM_COLUMNS}
    fields["uses"] = json.loads(row["uses"])
    return fields


def addendum_row_digest_ok(row) -> bool:
    """addendum_sha256 recomputed from the row alone equals the stored one,
    and the row's values are still in their formats."""
    try:
        fields = addendum_row_fields(row)
        return (addendum_sha256(fields) == row["addendum_sha256"]
                and not addendum_format_problems(fields))
    except (TypeError, ValueError, KeyError):
        return False


def addendum_row_equals_file(row, raw: bytes, principal: str) -> bool:
    try:
        fields = load_addendum(raw)
        stored = addendum_row_fields(row)
    except (RightsRefused, TypeError, ValueError, KeyError):
        return False
    return (all(stored[k] == fields[k] for k in ADDENDUM_SIGNED_FIELDS)
            and stored["approved_by"] == principal
            and stored["approval_file_sha256"] == sha256(raw)
            and addendum_row_digest_ok(row))


def addendum_bundle_paths(protected_dir: str, workspace_identity: str,
                          approval_sha: str) -> tuple:
    """Beside the record's files: rights/<identity>/addendum-<sha>.json."""
    folder = os.path.join(protected_dir, "rights", workspace_identity)
    data = os.path.join(folder, "addendum-" + approval_sha + ".json")
    return data, data + ".sig"


def addendum_matches_bundle(row, protected_dir: str) -> bool:
    """The addendum row equals its signed file in the mounted bundle, the
    signature verified now in the addendum namespace."""
    try:
        data_path, sig_path = addendum_bundle_paths(
            protected_dir, row["workspace_identity"], row["approval_file_sha256"])
        principal = verify_signature(data_path, sig_path, allowed_signers_path(protected_dir),
                                     namespace=ADDENDUM_NAMESPACE)
        with open(data_path, "rb") as handle:
            raw = handle.read()
    except (RightsRefused, OSError, TypeError, KeyError):
        return False
    return addendum_row_equals_file(row, raw, principal)


def addendum_in(con, workspace_identity):
    if not _has_table(con, "pilot_model_addenda"):
        return None
    row = con.execute("SELECT * FROM pilot_model_addenda WHERE workspace_identity=?",
                      (workspace_identity,)).fetchone()
    return None if row is None else dict(row)


def read_addenda(control_db: str) -> list:
    if not control_db or not os.path.isfile(control_db):
        return []
    con = _open_ro(control_db)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM pilot_model_addenda")] \
            if _has_table(con, "pilot_model_addenda") else []
    finally:
        con.close()


def addendum_bound(addendum, record_row) -> bool:
    """The addendum's digest holds and it names its workspace's record."""
    return (addendum_row_digest_ok(addendum) and record_row is not None
            and addendum.get("admission_id") == record_row.get("admission_id"))


def model_uses(addendum) -> set:
    return {tuple(p) for p in json.loads(addendum["uses"])}


def viewed_categories(record) -> list:
    return [c for c in CATEGORIES if ("member_view", c) in uses_of(record)]


def model_read_scope(config, workspace_identity):
    """{"transcripts": bool} for a workspace whose valid addendum lets the
    model read at least metadata and excerpts, else None (no addendum, a
    tampered or unbound one, no valid record, or any read error).
    transcripts: the addendum grants model_read on transcript and the
    record lets members view it."""
    control_db = getattr(config, "grants_db", "") or ""
    try:
        if not os.path.isfile(control_db):
            return None
        con = _open_ro(control_db)
        try:
            record_row = record_in(con, workspace_identity)
            addendum = addendum_in(con, workspace_identity)
        finally:
            con.close()
        if addendum is None or record_row is None or not row_digest_ok(record_row) \
                or not addendum_bound(addendum, record_row):
            return None
        granted = model_uses(addendum)
        if not {(MODEL_USE, "metadata"), (MODEL_USE, "excerpt")} <= granted:
            return None
        record = row_fields(record_row)
    except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
        return None
    return {"transcripts": (MODEL_USE, "transcript") in granted
            and "transcript" in viewed_categories(record)}


# ------------------------------------------------- permissions (ceiling)

def uses_of(record) -> set:
    return {tuple(p) for p in record["uses"]}


def ceiling(record, category: str) -> tuple:
    """(agent_disclosure, export_eligible) the record allows at most.
    Audio is never export-eligible."""
    uses = uses_of(record)
    if category == AUDIO:
        return ("allow" if ("member_view", AUDIO) in uses else "deny"), 0
    if category not in CATEGORIES:
        return "deny", 0
    view = ("member_view", category) in uses
    return ("allow" if view else "deny",
            1 if view and ("export", category) in uses else 0)


def disclosure_keys(con) -> set:
    """K: every source_versions key plus episode:<id> for every episode."""
    keys = {r[0] for r in con.execute("SELECT DISTINCT source_key FROM source_versions")}
    keys |= {"episode:" + r[0] for r in con.execute("SELECT id FROM episodes")}
    return keys


def policy_rows(con) -> list:
    """The rows authorize_disclosure reads (workspace 'local')."""
    return [{"source_key": r[0], "content_category": r[1],
             "agent_disclosure": r[2], "export_eligible": r[3]}
            for r in con.execute(
                "SELECT source_key, content_category, agent_disclosure,"
                " export_eligible FROM source_content_policy"
                " WHERE workspace_id='local'")]


def set_problems(record, keys, rows) -> list:
    """(code, detail) pairs for a permission set against a record."""
    keys = set(keys)
    if record is None:
        return [("NO_RECORD", "workspace has content and no record")] if keys else []
    problems = []
    present = {(r["source_key"], r["content_category"]) for r in rows}
    for key in sorted(keys):
        for category in POLICY_CATEGORIES:
            if (key, category) not in present:
                problems.append(("PERMISSION_INCOMPLETE", f"{key} {category}"))
    for row in rows:
        allow, export = ceiling(record, row["content_category"])
        # The same tests authorize_disclosure applies (access.py).
        if (row["agent_disclosure"] == "allow" and allow != "allow") or (
                row["export_eligible"] and not export):
            problems.append(("PERMISSION_EXCEEDS_RECORD",
                             f"{row['source_key']} {row['content_category']}"))
    return problems


def permission_problems(record, con) -> list:
    """5.2.2 permission ceiling check on one workspace database."""
    return set_problems(record, disclosure_keys(con), policy_rows(con))


# --------------------------------------------------------------- paid use

def basis_allows_paid_use(record) -> bool:
    """The one paid-use predicate: may this record's workspace serve a
    paying customer? False for public_evaluation and for any unknown
    basis. Every path that establishes or serves paid access calls it
    (through `paid_use`): the in-app checkout, the billing portal, the
    billing webhook, the entitlement gate and billing status. The
    purchase seat reserve (purchases.reserve refuses a workspace that is
    not paid-capable). BY3's seat allocation must also call it before it
    chooses a pool workspace."""
    return record.get("basis") in PAID_BASES


EVALUATION_ONLY = ("evaluation_only: the workspace's rights record is for evaluation only"
                   " and never serves paid use")
PAID_USE_UNREADABLE = "rights_unreadable: the workspace's rights record could not be read"
RECORD_TAMPERED = ("record_tampered: the workspace's rights record changed after approval"
                   " and never serves paid use")


def paid_use(con, workspace_identity) -> tuple:
    """(allowed, reason) for paid use of one workspace, read from an open
    control-store connection. No record: allowed (approval refuses an
    evaluation-only record for a workspace that is already paid, see
    workspace_paid_problem). A record whose digest fails: refused
    (RECORD_TAMPERED). Otherwise basis_allows_paid_use. A failed read:
    refused (PAID_USE_UNREADABLE)."""
    try:
        record = record_in(con, workspace_identity)
    except (sqlite3.Error, TypeError, ValueError):
        return False, PAID_USE_UNREADABLE
    if record is None:
        return True, ""
    if not row_digest_ok(record):
        # A row changed after approval (say public_evaluation edited to
        # license) is not trusted for paid use.
        return False, RECORD_TAMPERED
    if basis_allows_paid_use(record):
        return True, ""
    return False, EVALUATION_ONLY


# Subscription states that end paid access for good (billing.STRIPE_STATUSES).
_ENDED = ("", "unknown", "canceled", "incomplete_expired")
# billing._write_verified and billing._reconcile_gone journal a subscription's
# verified state as these reasons on its reconciled billing_events row.
_ENDED_REASONS = ("verified provider status canceled",
                  "verified provider status incomplete_expired")
_GONE_REASON = "verified subscription is gone"


def _subscription_state(con, subscription_id: str) -> str:
    """'ended', 'live' or 'unknown' for one verified subscription, from
    its last reconciled event: ended only when that event recorded an
    end, 'unknown' when no reconciled event exists."""
    row = con.execute("SELECT reason FROM billing_events WHERE subscription_id=?"
                      " AND status='reconciled' ORDER BY rowid DESC LIMIT 1",
                      (subscription_id,)).fetchone()
    if row is None:
        return "unknown"
    reason = row[0] or ""
    return "ended" if reason in _ENDED_REASONS or reason.startswith(_GONE_REASON) else "live"


def workspace_paid_problem(db_path: str):
    """None when the workspace holds no paid subscription, else the
    refusal code: WORKSPACE_PAID (the entitlement row, or any verified
    subscription's last reconciled state, is not ended),
    PAID_STATE_UNREADABLE (its billing state could not be read, or a
    verified subscription has no recorded state). Approval
    of a record that does not allow paid use runs this first."""
    try:
        con = _open_ro(db_path)
        try:
            statuses = [r[0] for r in con.execute("SELECT status FROM billing_entitlement")] \
                if _has_table(con, "billing_entitlement") else []
            # The entitlement row holds only the last-written subscription;
            # every verified subscription is checked on its own (RB1-R2-01).
            states = {_subscription_state(con, r[0]) for r in con.execute(
                "SELECT subscription_id FROM billing_subscriptions")} \
                if _has_table(con, "billing_subscriptions") else set()
        finally:
            con.close()
    except sqlite3.Error:
        return "PAID_STATE_UNREADABLE"
    if "live" in states or any((status or "") not in _ENDED for status in statuses):
        return "WORKSPACE_PAID"
    if "unknown" in states:
        # The stored state cannot show this subscription ended: fail closed.
        return "PAID_STATE_UNREADABLE"
    return None


def record_in(con, workspace_identity):
    """The rights row of one workspace from an open control-store
    connection, or None (no row, or no table yet)."""
    if not _has_table(con, "pilot_rights_admissions"):
        return None
    row = con.execute("SELECT * FROM pilot_rights_admissions WHERE workspace_identity=?",
                      (workspace_identity,)).fetchone()
    return None if row is None else dict(row)


def table_current(con) -> bool:
    """The control store's rights table has this build's definition (a
    store made before public_evaluation keeps its old basis CHECK, since
    CREATE TABLE IF NOT EXISTS never changes a table)."""
    def table_sql(c):
        row = c.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                        " AND name='pilot_rights_admissions'").fetchone()
        return None if row is None else row[0]
    fresh = sqlite3.connect(":memory:")
    try:
        fresh.executescript(DDL)
        return table_sql(con) == table_sql(fresh)
    finally:
        fresh.close()


# ---------------------------------------------------------- stores (read)

def _open_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:" + quote(os.path.abspath(path), safe="/") + "?mode=ro",
                          uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has_table(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def read_control(control_db: str) -> tuple:
    """(registry rows, every rights row) from the control store, read-only.
    A store that does not exist yet has neither."""
    if not control_db or not os.path.isfile(control_db):
        return [], []
    con = _open_ro(control_db)
    try:
        registry = [dict(r) for r in con.execute(
            "SELECT workspace_id, db_path, db_identity FROM workspace_registry"
        )] if _has_table(con, "workspace_registry") else []
        records = [dict(r) for r in con.execute(
            "SELECT * FROM pilot_rights_admissions")] \
            if _has_table(con, "pilot_rights_admissions") else []
    finally:
        con.close()
    return registry, records


def record_for(control_db: str, workspace_identity: str):
    _, records = read_control(control_db)
    for row in records:
        if row["workspace_identity"] == workspace_identity:
            return row
    return None


# ------------------------------------------------------- start-up check

def startup_problems(config) -> list:
    """5.2.2 start-up check, run by health.check_config in production.
    Each problem names the rule; the app does not start on any."""
    problems = []
    end = None
    try:
        end = parse_deployed_end(getattr(config, "pilot_end", "") or "")
    except ValueError:
        problems.append("EARWITNESS_PILOT_END must be set to the approved end,"
                        " an ISO 8601 time with Z or an explicit offset")
    if end is not None and _now_dt(config.now()) >= end:
        problems.append("EARWITNESS_PILOT_END has passed: the pilot has ended"
                        " and the service must not start (PILOT_ENDED)")
    features = getattr(config, "pilot_features", None)
    if features is None:
        problems.append("EARWITNESS_PILOT_FEATURES must be set (empty means no feature)")
        features = ()
    elif any(name not in FEATURES for name in features):
        problems.append("EARWITNESS_PILOT_FEATURES holds an unknown feature name"
                        " (allowed: " + ", ".join(FEATURES) + ")")
    enabled = [name for name in FEATURES if name in features]
    if "agent_access" in enabled and (getattr(config, "issuer", "") or "") \
            and (getattr(config, "jwks_file", "") or ""):
        # Provider tokens (issuer and JWKS both set, agent_verify.from_config)
        # are not under the personal-token route and scope table (agent
        # tokens review AGT-R4-03).
        problems.append("EARWITNESS_OAUTH_ISSUER with EARWITNESS_OAUTH_JWKS_FILE (provider"
                        " tokens) must stay unset while agent_access is on"
                        " (AGENT_ACCESS_WITH_ISSUER)")
    try:
        registry, records = read_control(getattr(config, "grants_db", "") or "")
        addenda = read_addenda(getattr(config, "grants_db", "") or "")
    except sqlite3.Error:
        return problems + ["EARWITNESS_GRANTS_DB: pilot rights records could not be read"]
    # Every kept row is checked for tampering; only rows of live
    # workspaces are judged against the deployment.
    for row in records:
        if not row_digest_ok(row):
            problems.append("pilot rights record changed after approval"
                            " (RECORD_TAMPERED)")
    by_identity = {row["workspace_identity"]: row for row in records}
    # An addendum must keep its digest and name its workspace's record
    # (Ask design 8.1), whatever the switch says.
    for row in addenda:
        if not addendum_bound(row, by_identity.get(row.get("workspace_identity"))):
            problems.append("pilot model-processing addendum changed after approval"
                            " (ADDENDUM_TAMPERED)")
    addendum_by_identity = {row.get("workspace_identity"): row for row in addenda}
    if "meaning_alerts" in enabled:
        problems.extend(public_rows_problems(getattr(config, "app_dir", "") or ""))
    for entry in registry:
        name = entry["workspace_id"]
        row = by_identity.get(entry["db_identity"])
        record = None
        if row is not None:
            try:
                record = row_fields(row)
            except (TypeError, ValueError):
                record = None  # reported above as RECORD_TAMPERED
        if record is not None:
            if end is None or _instant_or_none(row["pilot_end"]) != end:
                problems.append(f"workspace {name}: record pilot_end differs from"
                                " EARWITNESS_PILOT_END (PILOT_END_MISMATCH)")
            viewed = [c for c in CATEGORIES if ("member_view", c) in uses_of(record)]
            for feature in enabled:
                if feature in MODEL_FEATURES:
                    continue  # granted by the addendum, checked below
                if any((FEATURE_USE[feature], c) not in uses_of(record) for c in viewed):
                    problems.append(f"workspace {name}: feature {feature} is enabled"
                                    " and the record does not cover it (NOT_COVERED)")
            if "invites" in enabled and _audio_invite_missing(record):
                problems.append(f"workspace {name}: feature invites is enabled and the"
                                " record lets members play audio without"
                                " [member_invite, audio] (NOT_COVERED)")
        if "ask" in enabled:
            problems.extend(f"workspace {name}: {p}" for p in _model_problems(
                record, addendum_by_identity.get(entry["db_identity"])))
        elif "meaning_alerts" in enabled:
            # Meaning alerts run on Plan A too (ad excerpts only): the
            # addendum is needed; an excerpt-level record is not refused.
            problems.extend(f"workspace {name}: {p}" for p in _model_problems(
                record, addendum_by_identity.get(entry["db_identity"]),
                feature="meaning_alerts"))
        if "brief_check_live" in enabled:
            problems.extend(f"workspace {name}: {p}" for p in _model_problems(
                record, addendum_by_identity.get(entry["db_identity"]),
                feature="brief_check_live"))
        try:
            con = _open_ro(entry["db_path"])
            try:
                found = permission_problems(record, con)
                pinned = recording_pinned(con)
                denied = [f for f in ("ask", "brief_check_live") if f in enabled] \
                    if _transcript_denied(con) else []
            finally:
                con.close()
        except sqlite3.Error:
            problems.append(f"workspace {name}: database could not be read")
            continue
        for feature in denied:
            problems.append(f"workspace {name}: feature {feature} is enabled and a transcript"
                            " permission row does not allow transcripts: the workspace is"
                            " excerpt-level (ASK_EXCERPT_LEVEL)")
        if pinned:
            problems.append(f"workspace {name}: a recording is pinned; the hosted"
                            " pilot is clip-only (RECORDING_PINNED)")
        for code in sorted({code for code, _ in found}):
            problems.append(f"workspace {name}: source permissions fail the"
                            f" record ({code})")
    return problems


# A recording's registry id (pipeline/artifacts.register_media). Clip ids,
# digests and paths never take this shape.
_MEDIA_ID = re.compile(r"\bmed_[0-9a-f]{12}\b")
# JSON keys whose non-null value identifies a full recording: the pin, the
# playback proof (citations), and the media digest that transcription and
# creatives.map_refetch keep (CLIPCORE-SEC-R8-02). A clip's `sha256` and its
# source `provenance.audio_sha256` are not recordings and are not listed.
_MEDIA_KEYS = ("recording", "recording_id", "recording_digest", "media_artifact_id",
               "media_digest")
# Member free text (CLIPCORE-R7-01, owner option 1; R8-02): columns whose
# value a member types through a hosted customer service (notes, names,
# labels, campaign text, brief text, search queries, ad-report notes,
# workspace names). Plain-text columns are prose and are not scanned;
# member JSON columns (*.query_json) skip the id match but keep the
# media-key and unreadable-JSON rules. Every other TEXT/BLOB column is
# scanned in full. A column belongs here only when every writer of it is a
# guarded hosted writer, which also checks the value it stores
# (services._refuse_stored, CLIPCORE-SEC-R11-01).
MEMBER_TEXT = frozenset({
    "customer_mistake_reports.reason",                       # save_note
    "customer_saved_items.title", "customer_saved_items.note",
    "customer_saved_items.query_json",                       # save/edit item
    "customer_alerts.name", "customer_alerts.query_json",    # save_alert
    "campaigns.name", "campaigns.brand", "campaigns.message", "campaigns.offer",
    "campaigns.notes", "campaigns.manual_conclusion",
    "campaigns.manual_reason",                               # save_campaign
    "campaign_history.name", "campaign_history.brand", "campaign_history.message",
    "campaign_history.offer", "campaign_history.notes",
    "campaign_history.manual_conclusion", "campaign_history.manual_reason",
    "campaign_targets.note",                                 # save_target
    "campaign_checks.manual_reason",
    "provided_brief_versions.title",
    "provided_brief_versions.provided_text",                 # save_brief
    "customer_saved_searches.name",
    "customer_saved_searches.query_json",                    # retired store
    "ad_reports.note",                                       # report_ad
    "workspaces.name",                                       # create_workspace
})
# Pin columns whose value must be a JSON object.
_PIN_OBJECTS = {("source_states", "pins_json"), ("campaign_reports", "pins_json"),
                ("campaign_reports", "report_json")}


def recording_pinned(con) -> bool:
    """The clip-only boundary (CLIPCORE-R5-01, SEC-R6-01, SEC-R7-01/02).

    Frozen reports and exports keep recording pins unfiltered, so a hosted
    workspace may hold no recording anywhere. Besides the current episode
    and transcript bindings, every TEXT or BLOB value in every table is
    read, except member free text (MEMBER_TEXT plain-text columns): it is
    pinned when it names a recording id (`med_` + 12 hex; not checked in
    member JSON columns), or
    parses as JSON with a non-null media key (_MEDIA_KEYS) at any depth. A value in
    a JSON column (name ends in _json or holds "pins", or default '{}' or
    '[]') that does not parse counts as pinned, and so does a pin column
    that is not a JSON object."""
    if con.execute("SELECT 1 FROM episodes WHERE media_artifact_id IS NOT NULL LIMIT 1"
                   ).fetchone() or con.execute(
            "SELECT 1 FROM transcript_versions WHERE media_artifact_id IS NOT NULL LIMIT 1"
            ).fetchone():
        return True
    tables = {}
    for name, (json_column, must_object) in text_columns(con).items():
        table, column = name.split(".", 1)
        tables.setdefault(table, []).append(
            (column, json_column, must_object, name in MEMBER_TEXT))
    for table, columns in tables.items():
        # One pass per table; the WHERE is a superset of what _value_pinned
        # flags, evaluated in C.
        tests = [_candidate_test(column, json_column, must_object)
                 for column, json_column, must_object, _m in columns]
        names = ", ".join(f'"{column}"' for column, _j, _o, _m in columns)
        for row in con.execute(f'SELECT {names} FROM "{table}" WHERE '
                               + " OR ".join(f"({t})" for t in tests)):
            if any(_value_pinned(value, json_column, must_object, member)
                   for value, (_c, json_column, must_object, member) in zip(row, columns)):
                return True
    return False


RECORDING_ID_REFUSAL = "This text can't include a recording reference."
RECORDING_COPY_REFUSAL = ("This would copy a recording ID into a report, export,"
                          " history or email. Remove it from the text first.")
# Member input is matched more widely than the scan: in any case and with no
# closing boundary, so a writer that lowercases or truncates the text can
# never turn accepted input into a bare id (CLIPCORE-SEC-R10-03).
_MEMBER_INPUT_ID = re.compile(r"\bmed_[0-9a-f]{12}", re.IGNORECASE)


def has_recording_id(value) -> bool:
    """Member input a hosted writer refuses (owner option 2; CLIPCORE-R8-01/02,
    SEC-R10-01..03): a string holding a recording id in any case, or text a
    fully scanned column would count as a recording (JSON with a non-null
    media key, or too deep to read); a dict with a non-null media key; any
    key or value of a dict or list. Writers store member text in scanned
    columns and frozen reports and exports copy it, so refused input can
    never make start-up refuse. The walk is iterative, so input nested
    deeper than the recursion limit is checked, not an internal error
    (CLIPCORE-SEC-R13-02). A stored value that is not a str is checked as
    stored_text renders it; bytes that are not UTF-8 count as holding an
    id (CLIPCORE-SEC-R14-01)."""
    stack = [value]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if (_MEMBER_INPUT_ID.search(value) is not None
                    or _value_pinned(value, json_column=False, must_object=False)):
                return True
        elif isinstance(value, dict):
            if any(value.get(key) is not None for key in _MEDIA_KEYS):
                return True
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
        elif value is None or isinstance(value, (bool, int, float)):
            continue  # can never spell an id
        else:
            try:
                stack.append(stored_text(value))
            except UnicodeDecodeError:
                return True  # stored bytes that are not UTF-8 fail closed
    return False


def stored_text(value) -> str:
    """A stored value as the text a copy renders (CLIPCORE-SEC-R14-01):
    SQLite keeps a BLOB in a TEXT column, so bytes are decoded as UTF-8
    (UnicodeDecodeError when they are not; callers refuse) and any other
    non-str value is its str() form. None is ""."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8")
    return value if isinstance(value, str) else str(value)


def stored_recording_id(con, table: str, columns, where: str, params=()) -> bool:
    """has_recording_id on member text as a writer stored it, read back
    before commit: after the writer's strip, cut or lowercasing
    (CLIPCORE-SEC-R11-01, R9-01). Columns the table lacks are skipped."""
    have = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
    names = [column for column in columns if column in have]
    if not names:
        return False
    listed = ", ".join(f'"{column}"' for column in names)
    for row in con.execute(f'SELECT {listed} FROM "{table}" WHERE {where}', params):
        if has_recording_id(list(row)):
            return True
    return False


def member_recording_ids(con) -> set:
    """Recording ids typed into member free text that start-up does not
    read (stored before the writer guard, or by a local writer). A frozen
    report or export copies that text into columns it reads in full, so
    the copy must not hold one of these (CLIPCORE-R8-N1, SEC-R10-03)."""
    found = set()
    columns = {}
    for name in MEMBER_TEXT:
        table, column = name.split(".", 1)
        columns.setdefault(table, []).append(column)
    present = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, names in columns.items():
        if table not in present:
            continue
        have = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        for column in names:
            if column not in have:
                continue
            for (value,) in con.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE instr("{column}", \'med_\') > 0'):
                try:
                    value = stored_text(value)
                except UnicodeDecodeError:
                    # Replacing bad bytes never hides an ASCII id.
                    value = bytes(value).decode("utf-8", "replace")
                found.update(_MEDIA_ID.findall(value))
    return found


def text_columns(con) -> dict:
    """{"table.column": (json_column, must_object)} for every TEXT, CHAR,
    CLOB, BLOB or untyped column. A virtual table's rows live in its shadow
    tables, which are listed."""
    found = {}
    for (table,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'").fetchall():
        found.update(_table_text_columns(con, table))
    return found


def _table_text_columns(con, table: str) -> list:
    found = []
    for _cid, column, kind, _notnull, default, _pk in con.execute(
            f'PRAGMA table_info("{table}")').fetchall():
        kind = (kind or "").upper()
        if kind and not any(t in kind for t in ("TEXT", "CHAR", "CLOB", "BLOB")):
            continue
        found.append((f"{table}.{column}", (
            column.endswith("_json") or "pins" in column or default in ("'{}'", "'[]'"),
            (table, column) in _PIN_OBJECTS)))
    return found


def _candidate_test(column: str, json_column: bool, must_object: bool) -> str:
    """A recording id needs `med_`; a decoded media key or an escaped id
    needs `recording`, `media_` or a \\u escape; unreadable JSON fails
    json_valid, except a NUL, after which json_valid ignores the rest."""
    c = f'"{column}"'
    if must_object:
        return f"{c} IS NOT NULL"
    test = (f"instr({c}, 'med_') > 0 OR instr({c}, 'recording') > 0"
            f" OR instr({c}, 'media_') > 0 OR instr({c}, '\\u') > 0")
    if json_column:
        # json_valid accepts invalid UTF-8 in a BLOB, so every BLOB is read.
        test += (f" OR NOT json_valid({c}) OR instr(CAST({c} AS BLOB), x'00') > 0"
                 f" OR typeof({c}) = 'blob'")
    return test


def _value_pinned(value, json_column: bool, must_object: bool,
                  member: bool = False) -> bool:
    """member: a MEMBER_TEXT column. Member free text is prose, never a pin
    (CLIPCORE-R8-02): it is not matched for ids or parsed as JSON. A member
    JSON column (query_json) keeps the media-key and unreadable-JSON rules."""
    if member and not json_column:
        return False
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            if json_column:
                return True  # undecodable JSON fails closed (CLIPCORE-SEC-R9-01)
            value = value.decode("utf-8", "replace")
    elif not isinstance(value, str):  # NULL or a number
        return False
    if not member and _MEDIA_ID.search(value):
        return True
    if not json_column and value.lstrip()[:1] not in ("{", "["):
        return False
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return json_column  # unreadable JSON fails closed; free text does not
    try:
        return (must_object and not isinstance(parsed, dict)) or _names_recording(
            parsed, ids=not member)
    except RecursionError:
        return True  # too deep to inspect: fails closed (CLIPCORE-R7-N1)


def _names_recording(value, ids: bool = True) -> bool:
    """A non-null media key, or (ids) a recording id, anywhere in a JSON value."""
    if isinstance(value, dict):
        return (any(value.get(key) is not None for key in _MEDIA_KEYS)
                or any(_names_recording(v, ids) for v in value.values()))
    if isinstance(value, list):
        return any(_names_recording(v, ids) for v in value)
    return ids and isinstance(value, str) and _MEDIA_ID.search(value) is not None


def public_rows_problems(app_dir: str) -> list:
    """The meaning-alert start-up gate (meaning alerts design 3, approved):
    PUBLIC_ROWS_FILE exists with a non-empty `meaning_alerts` list, and
    every [page, text] pair under it names a page directly under
    <app_dir>/public that holds the text byte for byte."""
    refusal = ["feature meaning_alerts is enabled and the approved public rows are not all"
               " published (PUBLIC_ROWS)"]
    try:
        with open(PUBLIC_ROWS_FILE, "rb") as handle:
            rows = json.loads(handle.read().decode("utf-8")).get("meaning_alerts")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return refusal
    public = os.path.join(app_dir, "public")
    if not app_dir or not isinstance(rows, list) or not rows or not os.path.isdir(public):
        return refusal
    for pair in rows:
        if not (isinstance(pair, list) and len(pair) == 2
                and all(isinstance(v, str) and v for v in pair)):
            return refusal
        page, text = pair
        if os.path.basename(page) != page or page in (".", ".."):
            return refusal
        try:
            with open(os.path.join(public, page), "rb") as handle:
                if text.encode("utf-8") not in handle.read():
                    return refusal
        except OSError:
            return refusal
    return []


def _model_problems(record, addendum, *, feature: str = "ask") -> list:
    """Start-up problems of one live workspace with a model feature on (Ask
    design 8.1, chat doc S7): an approved addendum covering every text
    category the record lets members view and, for `ask`, a full-transcript
    (not excerpt-level) record; meaning alerts check ad excerpts on an
    excerpt-level record. Tampering is reported for every row above."""
    if addendum is None:
        return [f"feature {feature} is enabled and the workspace has no approved"
                " model-processing addendum (NO_ADDENDUM)"]
    out = []
    if record is not None:
        try:
            granted = model_uses(addendum)
        except (TypeError, ValueError):
            granted = set()  # unreadable: reported as ADDENDUM_TAMPERED
        if any((MODEL_USE, c) not in granted for c in viewed_categories(record)):
            out.append(f"feature {feature} is enabled and the model-processing addendum"
                       " does not cover it (NOT_COVERED)")
        if feature in ("ask", "brief_check_live") \
                and "transcript" not in viewed_categories(record):
            out.append(f"feature {feature} is enabled and the record does not let members view"
                       " transcripts: the workspace is excerpt-level (ASK_EXCERPT_LEVEL)")
    return out


def _transcript_denied(con) -> bool:
    """A transcript permission row that does not allow: the workspace reads
    at excerpt level there (plan-a.md). The same test as
    customer.excerpt_plan, kept here so start-up needs no customer import;
    change both together."""
    return con.execute("SELECT 1 FROM source_content_policy WHERE workspace_id='local'"
                       " AND content_category='transcript' AND agent_disclosure <> 'allow'"
                       " LIMIT 1").fetchone() is not None


def _audio_invite_missing(record) -> bool:
    """Members may play audio, and the record does not extend it to invitees."""
    granted = uses_of(record)
    return ("member_view", AUDIO) in granted and ("member_invite", AUDIO) not in granted


def _instant_or_none(text):
    try:
        return parse_instant(text)
    except ValueError:
        return None


# ------------------------------------------------------------ import check

def check(workspace: dict, collection: str, items, now, *, layout: dict,
          env=None) -> dict:
    """5.2.3 import checks, in order; raise RightsRefused with the first
    failing code, else return the record. B5 calls this before any write.

    workspace: {"identity": ws-…, "territory": "US"}; items: a non-empty
    list of dicts with "origin" and a non-empty "categories" list;
    layout: the L5p layout (control_db, protected_dir, pilot_end). The
    deployed end and feature list are read here, from the layout and the
    environment (env, default os.environ): EARWITNESS_PILOT_END, when set,
    must be the layout's instant; EARWITNESS_GRANTS_DB, when set, must be
    the layout's control_db; EARWITNESS_PILOT_LAYOUT, when set, names the
    installed layout file, whose control_db, protected_dir and pilot_end
    the given layout must equal; an unset EARWITNESS_PILOT_FEATURES means
    every feature except the model features (MODEL_FEATURES: ask), which
    are never implied."""
    env = os.environ if env is None else env
    if not isinstance(layout, dict) or not all(
            isinstance(layout.get(k), str) and layout.get(k)
            for k in ("control_db", "protected_dir", "pilot_end")):
        raise RightsRefused("INVALID_INPUT", "layout needs control_db, protected_dir and pilot_end")
    deployed_layout = env.get("EARWITNESS_PILOT_LAYOUT")
    if deployed_layout and not _same_layout(layout, deployed_layout):
        # The host installs one layout file; the caller's layout must be it,
        # so the signer list (protected_dir) cannot come from elsewhere.
        raise RightsRefused("LAYOUT_MISMATCH", "layout differs from EARWITNESS_PILOT_LAYOUT")
    deployed_store = env.get("EARWITNESS_GRANTS_DB")
    if deployed_store and os.path.realpath(deployed_store) != os.path.realpath(layout["control_db"]):
        # The signer list and the store come from the layout; on a host
        # the layout must name the deployed control store.
        raise RightsRefused("LAYOUT_MISMATCH", "layout control_db is not EARWITNESS_GRANTS_DB")
    items = list(items) if isinstance(items, (list, tuple)) else None
    if not items or any(
            not isinstance(item, dict) or not isinstance(item.get("categories"), list)
            or not item["categories"]
            or any(c not in POLICY_CATEGORIES for c in item["categories"]) for item in items):
        raise RightsRefused("INVALID_INPUT", "every item needs a non-empty list of known categories")
    features = parse_features(env.get("EARWITNESS_PILOT_FEATURES"))
    if features is not None and any(f not in FEATURES for f in features):
        raise RightsRefused("INVALID_INPUT", "EARWITNESS_PILOT_FEATURES holds an unknown name")
    try:
        end = parse_deployed_end(layout["pilot_end"])
        deployed = env.get("EARWITNESS_PILOT_END")
        if deployed is not None and parse_deployed_end(deployed) != end:
            raise ValueError("deployed end differs from the layout")
    except (KeyError, TypeError, ValueError):
        raise RightsRefused("PILOT_END_MISMATCH") from None
    row = record_for(layout["control_db"], workspace["identity"])
    if row is None:
        raise RightsRefused("NO_RECORD")
    try:
        record = row_fields(row)
    except (TypeError, ValueError):
        raise RightsRefused("RECORD_TAMPERED") from None
    if record["collection_id"] != collection:
        raise RightsRefused("COLLECTION_MISMATCH")
    if any(item.get("origin") != record["origin"] for item in items):
        raise RightsRefused("ORIGIN_MISMATCH")
    if record["territory"] != ["WORLD"] and workspace.get("territory") not in record["territory"]:
        raise RightsRefused("TERRITORY")
    moment = _now_dt(now)
    if parse_date(record["valid_from"]) > moment.date():
        raise RightsRefused("NOT_YET_VALID")
    if moment >= end:
        raise RightsRefused("PILOT_ENDED")
    problem = date_problem(record, layout["pilot_end"], record["deletion_days"])
    if problem:
        raise RightsRefused(problem)
    # Check 7: the text categories the items carry and every text category
    # the record lets members view need the base, import and feature uses.
    # Audio is checked apart (clip design 5.1): member view only, plus
    # member_invite when invites are on; never agent_read or share_view.
    granted = uses_of(record)
    present = {c for item in items for c in item["categories"]}
    covered = (present - {AUDIO}) | {c for c in CATEGORIES if ("member_view", c) in granted}
    # Unset means every feature but the model ones, which are never implied.
    enabled = [f for f in FEATURES if f not in MODEL_FEATURES] if features is None \
        else [f for f in FEATURES if f in features]
    needed = {("member_view", c) for c in covered}
    needed |= {(FEATURE_USE[f], c) for f in enabled if f not in MODEL_FEATURES
               for c in covered}
    if covered & {"transcript", "excerpt"}:
        needed |= {("acquire_transcript", "transcript"), ("store_transcript", "transcript")}
    if "metadata" in covered:
        needed.add(("acquire_metadata", "metadata"))
    if AUDIO in present:
        needed.add(("member_view", AUDIO))
    if not needed <= granted or ("invites" in enabled and _audio_invite_missing(record)):
        raise RightsRefused("NOT_COVERED")
    if not row_matches_bundle(row, layout["protected_dir"]):
        raise RightsRefused("RECORD_TAMPERED")
    if "ask" in enabled or "brief_check_live" in enabled:
        _check_addendum(row, record, layout)
    elif "meaning_alerts" in enabled:
        _check_addendum(row, record, layout, excerpt_ok=True)
    return record


def _check_addendum(row, record, layout, *, excerpt_ok: bool = False) -> None:
    """With `ask` on: the workspace's addendum covers every text category
    the record lets members view (NOT_COVERED), the record is not
    excerpt-level (ASK_EXCERPT_LEVEL; meaning alerts alone pass excerpt_ok),
    and the addendum keeps its digest,
    names this record and equals its signed file (ADDENDUM_TAMPERED)."""
    con = _open_ro(layout["control_db"])
    try:
        addendum = addendum_in(con, row["workspace_identity"])
    finally:
        con.close()
    if addendum is None:
        raise RightsRefused("NOT_COVERED", "no approved model-processing addendum")
    if not addendum_bound(addendum, row) or \
            not addendum_matches_bundle(addendum, layout["protected_dir"]):
        raise RightsRefused("ADDENDUM_TAMPERED")
    viewed = viewed_categories(record)
    if any((MODEL_USE, c) not in model_uses(addendum) for c in viewed):
        raise RightsRefused("NOT_COVERED", "the addendum does not cover a viewed category")
    if "transcript" not in viewed and not excerpt_ok:
        raise RightsRefused("ASK_EXCERPT_LEVEL")


def _same_layout(layout: dict, path: str) -> bool:
    """The given layout names the deployed layout file's control store,
    protected bundle and end (paths resolved, end as an instant)."""
    try:
        with open(path, "rb") as handle:
            deployed = json.loads(handle.read().decode("utf-8"))
        return (os.path.realpath(deployed["control_db"]) == os.path.realpath(layout["control_db"])
                and os.path.realpath(deployed["protected_dir"])
                == os.path.realpath(layout["protected_dir"])
                and parse_deployed_end(deployed["pilot_end"])
                == parse_deployed_end(layout["pilot_end"]))
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return False


def row_matches_bundle(row, protected_dir: str) -> bool:
    """Check 8: the row equals its signed approval file in the mounted
    protected bundle, the signature verified now."""
    data_path, sig_path = bundle_paths(protected_dir, row["workspace_identity"],
                                       row["approval_file_sha256"])
    try:
        principal = verify_signature(data_path, sig_path,
                                     allowed_signers_path(protected_dir))
        with open(data_path, "rb") as handle:
            raw = handle.read()
    except (RightsRefused, OSError):
        return False
    return row_equals_file(row, raw, principal)
