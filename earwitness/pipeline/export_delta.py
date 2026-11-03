"""Nightly signed packages of new episode revisions for the hosted app
(the Mini side of docs/plans/2026-10-21-fresh-data-design.md rev 4a).

`export_delta(cfg)` runs the existing `corpus.export` over the corpus into
a work dir (media=False: no clip or speaker-transcript files) and splits its
records per episode: each package member is the export's own records for
one episode (fetch.json = its episodes.json row, transcript.json = its
transcript file, detections.json / names.json = its rows of the
rule-candidates / named-reads files, jev.json = a Jev run doc holding only
this episode, under a header frozen per episode in the ledger: it is
refreshed only when this episode's own Jev entries change, so another
episode or a code constant never changes it). The host joins them back into one file
per role. No media is ever packaged. Components are hashed one episode at a
time and dropped; the tar is streamed to a file.

Each episode also gets revision.json (8.1): the checked episode ID, the
fetch observation, the digest of every component, detection_status (13),
stage_status (the run's jev_status and name_status, 64 characters each) and
revision_seq. The export ledger keeps, per episode, the digests last
seen; any change of a digest, detection_status or stage_status makes a new
revision_seq (one counter that only grows). A package offers the revisions
not acknowledged by every configured target at the active epoch, oldest
first, within the host limits (7.2); removed items are never offered.

`export_receipt(cfg, path)` accepts a host receipt or a reconcile file only
when `ssh-keygen -Y verify -n earwitness-receipt-v1` passes against the
pinned signer file, the epoch fits and every db_identity is a configured
target (8.4, 8.5).

Removals (14, contract C2): every receipt and reconcile file carries the
host's removal list and its digest. A verified list is added to the Mini's
removal store (`removal_store`, apart from the export ledger), a union that
is never shrunk; together with the local `removal_list` file it is applied
before replay and before any package. A receipt that cannot be verified, or
whose list is missing or mismatched, is refused and pauses offering: each
night then sends only the status block (code REMOVALS_UNVERIFIED) until a
fully accepted receipt of the active epoch arrives whose list holds every
stored entry. The same pause follows any failure while a receipt is checked
or kept, a missing store (STORE_MISSING), a damaged one (STORE_UNREADABLE;
moved aside as <name>.damaged-<UTC>, its readable entries kept), and a store
with fewer entries than the ledger's high-water mark (STORE_BEHIND).

Offered packages are withdrawn before any pause or list is saved. When the
store cannot be written, the pause and the pending list go to the export
ledger's meta (removals_pause, removals_pending); they block offering the
same way until a store write succeeds and a complete current-epoch receipt
is accepted. A night that can save the pause in neither place offers
nothing and fails (refused:REMOVALS_PAUSE_UNSAVED).

export-receipt pauses the same way: it deletes the offered packages' files,
then marks them superseded and records the ledger pause in one transaction,
then saves the pause in the store. When neither the ledger nor the store
took the pause it returns refused:REMOVALS_PAUSE_UNSAVED (exit 3); the
upload script must then skip that night's export-delta, which would
otherwise offer the withdrawn revisions again.

Each packaged revision's show_key is kept in the export ledger (not in the
manifest), so a package is withdrawn when its show is removed or leaves the
inventory, whatever the corpus or inventory says later; a package whose
show cannot be resolved is withdrawn too.

First activation: no store exists yet, so the first night sends only the
status block; the host's receipt for it brings the list and offering starts
the next night.

Known gap: if the store and the export ledger roll back together to an
older, consistent copy, the Mini cannot tell; it may offer an item removed
since that copy until the next receipt. The host's importer still refuses
it (REMOVED). Second accepted gap: after refused:REMOVALS_PAUSE_UNSAVED
(store and ledger both unwritable) nothing on the Mini holds the pause;
the upload script skips that night's export, and later nights rely on the
host's REMOVED refusal until a pause can be saved.

Show renames: the export ledger keeps, grow-only, every show key each feed
identity and each episode was ever seen with (feed_shows, episode_shows),
and the removal store keeps a copy of that history, written atomically with
it (outside the entry count and the `complete` check). Each night the store
copy is added back to the ledger first, so an export-ledger loss or restore
keeps every link; a store copy that cannot be written raises the alert
STORE_HISTORY_UNSAVED and the night goes on (the ledger still holds it). An
episode is removed when its ID, or any of those keys, is on the removal set,
both when building and when withdrawing packages. A rename the Mini never
saw (the feed's first night already under the new key) is not linked on the
Mini. The host covers it: its inventory (feed_url -> show_key) is pinned by
the rights-basis manifest and cannot change during the pilot (S8R1-03), so
the host maps the feed to its original show_key and refuses the item
(REMOVED), whatever key the Mini used.

Package files are deleted best effort, `.done` first. A withdrawn package
whose file could not be deleted stays superseded in the ledger, never
offered, and is deleted again each night; export-delta and export-receipt
then return refused:WITHDRAW_INCOMPLETE (exit 3), on which the upload
script stops.

`export_tidy(cfg)` (CLI `export-tidy --config FILE`, the same config) runs,
under the ledger lock, exactly the withdrawal/tidy export-delta runs before
building: the union removal set with the show-key history, the local
removal file and the inventory; affected offered packages are withdrawn
(every offered package while paused) and undeletable files retried. It
builds nothing. The upload script runs it before uploading on a night it
skips export-delta. Results: {"code": "ok"} exit 0 when every offered
package is clean; "REMOVALS_UNVERIFIED" exit 2 when offering is paused
(`detail` gives the pause code); "refused:WITHDRAW_INCOMPLETE" exit 3 while
a withdrawn file remains; otherwise "refused:<CODE>" exit 3 (LEDGER_BUSY,
CONFIG_INVALID, INVENTORY_INVALID, REMOVAL_LIST_INVALID,
REMOVALS_PAUSE_UNSAVED) or "failed:<Type>" exit 1.

All three return (exit code, one JSON-serializable dict of codes and counts).
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from earwitness.pipeline import corpus as cr
from earwitness.pipeline import jev_budget

PACKAGE_NS = "earwitness-package-v1"
RECEIPT_NS = "earwitness-receipt-v1"
MANIFEST_FORMAT = "earwitness-package-manifest/1"
REVISION_FORMAT = "earwitness-episode-revision/1"
STATUS_FORMAT = "earwitness-pipeline-status/1"
DEFAULT_LEDGER = "~/ew-corpus-worker/export-ledger.sqlite3"
DEFAULT_JEV_BUDGET = jev_budget.DEFAULT_LEDGER
DEFAULT_JEV_PIN = jev_budget.DEFAULT_PIN
# The Mini's union of every verified host removal list (contract C2). It lives
# apart from the export ledger (beside the Jev pin, outside the corpus backup
# set) so a restore, loss or recreation of the ledger cannot drop a removal.
DEFAULT_REMOVAL_STORE = "~/.config/earwitness-mini-removals.json"
STORE_FORMAT = "earwitness-mini-removals/1"
MAX_REMOVALS = 50_000          # entries in one host list (C2); larger is refused
REMOVALS_UNVERIFIED = "REMOVALS_UNVERIFIED"   # offering stopped until a valid list
# Host limits (7.2), decimal units so the Mini stays under the host's figure.
LIMITS = {"max_tar_bytes": 300_000_000, "max_episodes": 1000, "max_members": 7001,
          "max_file_bytes": 8_000_000, "max_segments": 20_000}
KEEP_DAYS = 8
MAX_UNACKNOWLEDGED = 3
MIN_FREE_BYTES = 2_000_000_000  # free space the export work needs before it runs
MAX_RECEIPT_BYTES = 16 * 1024 * 1024   # one receipt or reconcile file
MAX_SIGNATURE_BYTES = 64 * 1024        # its ssh-keygen signature (a few hundred bytes)
EP_RE = re.compile(r"ep_[0-9a-f]{16}")
CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
PACKAGE_FILE_RE = re.compile(r"(p-[0-9a-f]{32})\.(tar|manifest\.json|manifest\.json\.sig|done)")
SUFFIXES = ("tar", "manifest.json", "manifest.json.sig", "done")
COMPONENTS = ("fetch", "transcript", "detections", "jev", "names")
STATUS_MEMBER = "status.json"
FETCH_DATE_BASIS = "fetch_observation.finished_at"
TABLE_STATUSES = ("complete", "jev_skipped_budget", "jev_failed", "transcript_only")
BUDGET_SKIP = cr.JEV_SKIPPED_BUDGET   # the fleet's jev_status for a spend-cap refusal
EXIT_OK, EXIT_FAILED, EXIT_BACKLOG, EXIT_REFUSED = 0, 1, 2, 3
_RECORD = tarfile.RECORDSIZE

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (
  revision_seq INTEGER PRIMARY KEY AUTOINCREMENT, episode_id TEXT NOT NULL,
  digests TEXT NOT NULL, detection_status TEXT NOT NULL, created_at TEXT NOT NULL,
  stage_status TEXT);
CREATE INDEX IF NOT EXISTS revisions_episode ON revisions (episode_id, revision_seq);
CREATE TABLE IF NOT EXISTS acks (
  db_identity TEXT NOT NULL, episode_id TEXT NOT NULL, revision_seq INTEGER NOT NULL,
  PRIMARY KEY (db_identity, episode_id));
CREATE TABLE IF NOT EXISTS refusals (
  db_identity TEXT NOT NULL, episode_id TEXT NOT NULL, revision_seq INTEGER NOT NULL,
  code TEXT NOT NULL, PRIMARY KEY (db_identity, episode_id, revision_seq));
CREATE TABLE IF NOT EXISTS packages (
  package_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL UNIQUE,
  sealed_at TEXT NOT NULL, restore_epoch INTEGER NOT NULL, revisions TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('offered','acknowledged','superseded','expired')),
  files_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY, file_sha256 TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
  restore_epoch INTEGER NOT NULL, package_digest TEXT, accepted_at TEXT NOT NULL,
  counts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, code TEXT NOT NULL, counts TEXT,
  reported INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jev_headers (episode_id TEXT PRIMARY KEY, header TEXT NOT NULL,
  entries_sha256 TEXT);
CREATE TABLE IF NOT EXISTS feed_shows (feed_identity TEXT NOT NULL, show_key TEXT NOT NULL,
  PRIMARY KEY (feed_identity, show_key));
CREATE TABLE IF NOT EXISTS episode_shows (episode_id TEXT NOT NULL, show_key TEXT NOT NULL,
  PRIMARY KEY (episode_id, show_key));
"""


# Columns added after ledgers existed (07bb91d, 63a6a66, S8R1-05): (table, column, type).
LEDGER_ADDITIONS = (("alerts", "reported", "INTEGER NOT NULL DEFAULT 0"),
                    ("jev_headers", "entries_sha256", "TEXT"),
                    ("revisions", "stage_status", "TEXT"))
STAGE_STATUS_CHARS = 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(obj) -> bytes:
    """The export's own serialization (chunked_stt.atomic_write_json)."""
    return json.dumps(obj).encode("utf-8")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Refused(Exception):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- config

def load_config(path) -> dict:
    """The export configuration (JSON). Paths expand `~`. Raises
    Refused("CONFIG_INVALID") on a missing or malformed field."""
    try:
        raw = json.loads(Path(path).read_text())
        ex = lambda p: Path(os.path.expanduser(str(p)))  # noqa: E731
        targets = raw["targets"]
        if not (isinstance(targets, list) and targets and all(
                isinstance(t, str) and t for t in targets) and len(set(targets)) == len(targets)):
            raise ValueError("targets")
        scope = raw["scope"]
        lo, hi = scope["pub_date_from"], scope["pub_date_to"]
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", lo) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", hi)
                and lo <= hi):
            raise ValueError("scope")
        limits = {**LIMITS, **(raw.get("limits") or {})}
        if set(limits) != set(LIMITS) or not all(
                isinstance(v, int) and v > 0 for v in limits.values()):
            raise ValueError("limits")
        ledger = ex(raw.get("ledger") or DEFAULT_LEDGER)
        cfg = {"corpus": ex(raw["corpus"]), "ledger": ledger, "outbox": ex(raw["outbox"]),
               "work_dir": ex(raw.get("work_dir") or ledger.parent / "export-work"),
               "inventory": ex(raw["inventory"]),
               "removal_list": ex(raw["removal_list"]) if raw.get("removal_list") else None,
               "scope": (lo, hi), "targets": targets,
               "producer": raw.get("producer") or "mini-1",
               "signing_key": ex(raw["signing_key"]),
               "receipt_allowed_signers": ex(raw["receipt_allowed_signers"]),
               "receipt_principal": raw["receipt_principal"],
               "initial_epoch": int(raw.get("initial_epoch", 1)),
               "jev_budget_ledger": ex(raw.get("jev_budget_ledger") or DEFAULT_JEV_BUDGET),
               "jev_budget_pin": ex(raw.get("jev_budget_pin") or DEFAULT_JEV_PIN),
               "removal_store": ex(raw.get("removal_store") or DEFAULT_REMOVAL_STORE),
               "limits": limits, "keep_days": int(raw.get("keep_days", KEEP_DAYS)),
               "max_unacknowledged": int(raw.get("max_unacknowledged", MAX_UNACKNOWLEDGED)),
               "min_free_bytes": int(raw.get("min_free_bytes", MIN_FREE_BYTES))}
        if cfg["removal_store"] == ledger:
            raise ValueError("removal_store")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused("CONFIG_INVALID") from exc
    return cfg


def _inventory(path) -> dict:
    """feed identity -> show_key, from shows-inventory.json (5.2). A
    duplicate feed identity or show key refuses the whole inventory."""
    try:
        doc = json.loads(Path(path).read_text())
        shows = doc["shows"] if isinstance(doc, dict) else doc
        out = {}
        for s in shows:
            fid, key = s["feed_identity"], s["show_key"]
            if not (isinstance(fid, str) and fid and isinstance(key, str) and key) \
                    or fid in out or key in out.values():
                raise ValueError("inventory entry")
            out[fid] = key
        return out
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused("INVENTORY_INVALID") from exc


def _removals(path) -> tuple:
    """(episode IDs, show keys) on the removal list (14); no file = none.
    Anything but lists of strings refuses the night (fails closed)."""
    if path is None:
        return frozenset(), frozenset()
    try:
        doc = json.loads(Path(path).read_text())
        if not isinstance(doc, dict):
            raise ValueError("removal list")
        out = []
        for key in ("episode_ids", "show_keys"):
            items = doc.get(key, [])
            if not (isinstance(items, list) and all(isinstance(x, str) for x in items)):
                raise ValueError(key)
            out.append(frozenset(items))
        return tuple(out)
    except (OSError, ValueError) as exc:
        raise Refused("REMOVAL_LIST_INVALID") from exc


# ---------------------------------------------------------------- host removals (C2)

def _read_store(path: Path) -> dict:
    """The Mini's removal store: the union of every verified host list and
    the pause code (None when offering may go on). No file pauses
    (STORE_MISSING): first activation, or a store lost with or without the
    ledger. A damaged file pauses (STORE_UNREADABLE) and keeps the entries
    still readable; `damaged` makes the next write move it aside first.
    It also mirrors the ledger's grow-only show-key history (feed_shows,
    episode_shows: id -> keys), kept out of the entry count and the
    `complete` check; a malformed history keeps what is readable."""
    out = {"episode_ids": set(), "show_keys": set(), "paused": None, "damaged": False,
           "feed_shows": {}, "episode_shows": {}}
    if not (path.exists() or path.is_symlink()):
        out["paused"] = "STORE_MISSING"
        return out
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        doc = None
    ok = isinstance(doc, dict) and doc.get("format") == STORE_FORMAT
    for key in ("episode_ids", "show_keys"):
        items = doc.get(key) if isinstance(doc, dict) else None
        if isinstance(items, list):
            out[key] = {x for x in items if isinstance(x, str)}   # what is still readable
            ok = ok and all(isinstance(x, str) for x in items)
        else:
            ok = False
    for key in HISTORY_KEYS:
        hist = doc.get(key, {}) if isinstance(doc, dict) else {}
        if not isinstance(hist, dict):
            ok, hist = False, {}
        for k, v in hist.items():
            if isinstance(v, list):
                out[key][k] = {x for x in v if isinstance(x, str)}
                ok = ok and len(out[key][k]) == len(v)
            else:
                ok = False
    paused = doc.get("paused") if isinstance(doc, dict) else None
    if ok and (paused is None or isinstance(paused, str)):
        out["paused"] = paused
    else:
        out["paused"], out["damaged"] = "STORE_UNREADABLE", True
    return out


def _store_entries(store: dict) -> int:
    return len(store["episode_ids"]) + len(store["show_keys"])


def _write_store(path: Path, store: dict) -> None:
    """Atomic, fsynced replace (temporary file, fsync, rename, directory
    fsync). A damaged store is first moved aside as <name>.damaged-<UTC>."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if store.get("damaged") and (path.exists() or path.is_symlink()):
        stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
        os.rename(path, path.with_name(f"{path.name}.damaged-{stamp}"))
        store["damaged"] = False
    _fsync_write(path, _canon({"format": STORE_FORMAT,
                               "episode_ids": sorted(store["episode_ids"]),
                               "show_keys": sorted(store["show_keys"]),
                               "paused": store["paused"],
                               **{key: {k: sorted(v) for k, v in store.get(key, {}).items()}
                                  for key in HISTORY_KEYS}}))
    _fsync_dir(path.parent)


REMOVAL_KEYS = ("episode_ids", "show_keys")
HISTORY_KEYS = ("feed_shows", "episode_shows")


def _merge_history(store: dict, history: dict | None) -> bool:
    """Add the ledger's show-key history to the store copy; True if it grew."""
    grew = False
    for key in HISTORY_KEYS:
        for k, keys in ((history or {}).get(key) or {}).items():
            have = store[key].setdefault(k, set())
            if not keys <= have:
                have |= keys
                grew = True
    return grew


def _ledger_removals(con) -> tuple:
    """(pause code or None, pending list) kept in the export ledger's meta
    when the store could not be written."""
    pause = _meta(con, "removals_pause")
    pending = {k: set() for k in REMOVAL_KEYS}
    raw = _meta(con, "removals_pending")
    if raw:
        try:
            doc = json.loads(raw)
            pending = {k: {x for x in doc[k] if isinstance(x, str)} for k in REMOVAL_KEYS}
        except (ValueError, KeyError, TypeError):
            pause = pause or "LEDGER_PENDING_UNREADABLE"
    return pause, pending


def _ledger_pause_rows(con, code: str, removals: dict | None) -> None:
    """Inside the caller's transaction: the pause (the first code stays) and
    `removals` added to the pending list."""
    con.execute("INSERT OR IGNORE INTO meta VALUES ('removals_pause', ?)", (code,))
    if removals:
        _, pending = _ledger_removals(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('removals_pending', ?)",
                    (_canon({k: sorted(pending[k] | removals[k])
                             for k in REMOVAL_KEYS}).decode(),))


def _ledger_pause(cfg, code: str, removals: dict | None = None) -> None:
    """The fallback when the store cannot be written: record the pause (the
    first code stays) and add `removals` to the pending list in the export
    ledger. Both block offering like a store pause until a store write and
    a complete current-epoch receipt clear them."""
    con = open_ledger(cfg["ledger"])
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            _ledger_pause_rows(con, code, removals)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def _persist_pause(cfg, code: str, removals: dict | None = None,
                   history: dict | None = None) -> str:
    """Save a pause, with `removals` and the show-key `history` added: in the
    store (a missing or damaged one rewritten, the damaged file moved aside),
    else in the export ledger. Neither: Refused(REMOVALS_PAUSE_UNSAVED), so
    nothing is offered."""
    path = cfg["removal_store"]
    try:
        store = _read_store(path)
        _merge_history(store, history)
        for k in REMOVAL_KEYS:
            store[k] |= (removals or {}).get(k, set())
        store["paused"] = store["paused"] or code
        _write_store(path, store)
        return "store"
    except Exception:
        pass
    try:
        _ledger_pause(cfg, code, removals)
        return "ledger"
    except Exception as exc:
        raise Refused("REMOVALS_PAUSE_UNSAVED") from exc


def _verified_removals(doc: dict) -> dict:
    """Contract C2: the receipt's removal list, sorted unique strings, at
    most MAX_REMOVALS entries, whose canonical digest is removal_list_sha256.
    Anything else refuses the receipt (RECEIPT_INVALID, removal_list)."""
    rl, sha = doc.get("removal_list"), doc.get("removal_list_sha256")
    keys = ("episode_ids", "show_keys")
    try:
        if not (isinstance(rl, dict) and set(rl) == set(keys)
                and all(isinstance(rl[k], list) for k in keys)
                and sum(len(rl[k]) for k in keys) <= MAX_REMOVALS):
            raise ValueError("shape")
        for k in keys:
            if not (all(isinstance(x, str) for x in rl[k]) and rl[k] == sorted(set(rl[k]))):
                raise ValueError(k)
        if not (isinstance(sha, str) and sha == _sha(_canon(rl))):
            raise ValueError("digest")
    except (ValueError, TypeError) as exc:
        raise Refused("RECEIPT_INVALID", "removal_list") from exc
    return {k: set(rl[k]) for k in keys}


def _same_volume(a: Path, b: Path) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


# ---------------------------------------------------------------- ledger

def open_ledger(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(LEDGER_SCHEMA)
    for table, column, kind in LEDGER_ADDITIONS:  # ledgers made before these columns
        if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return con


class _Locked:
    """One export-delta / export-receipt at a time (flock beside the ledger)."""

    def __init__(self, ledger: Path):
        ledger.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(str(ledger) + ".lock")

    def __enter__(self):
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close()
            raise Refused("LEDGER_BUSY")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _active_epoch(con, cfg) -> int:
    value = _meta(con, "active_epoch")
    if value is None:
        con.execute("INSERT OR IGNORE INTO meta VALUES ('active_epoch', ?)",
                    (str(cfg["initial_epoch"]),))
        return cfg["initial_epoch"]
    return int(value)


def _current_revisions(con) -> dict:
    """episode_id -> (revision_seq, digests dict, detection_status, stage_status
    dict or None) of the latest revision."""
    out = {}
    for seq, ep_id, digests, status, stage in con.execute(
            "SELECT revision_seq, episode_id, digests, detection_status, stage_status"
            " FROM revisions ORDER BY revision_seq"):
        out[ep_id] = (seq, json.loads(digests), status, json.loads(stage) if stage else None)
    return out


def _alert(con, now: datetime, code: str, counts=None) -> None:
    con.execute("INSERT INTO alerts (at, code, counts) VALUES (?,?,?)",
                (_iso(now), code, json.dumps(counts or {}, sort_keys=True)))


# ---------------------------------------------------------------- per-episode content

def detection_status(has_detections: bool, has_jev: bool, has_names: bool,
                     jev_status, name_status) -> str:
    """Section 13. `complete` needs a detections file, Jev ok and names ok;
    a budget skip or a failure keeps the rule detections; anything not run
    gets an off-table status, which the host quarantines (never a pass)."""
    if not has_detections:
        return "transcript_only"
    j, n = str(jev_status or ""), str(name_status or "")
    if BUDGET_SKIP in (j, n):
        return BUDGET_SKIP
    if j.startswith("ok") and n.startswith("ok") and has_jev and has_names:
        return "complete"
    if j.startswith("failed"):
        return "jev_failed"
    if not j.startswith("ok"):
        return "jev_not_run"
    return "names_failed" if n.startswith("failed") else "names_not_run"


def stage_status(jev_status, name_status) -> dict:
    """Contract C3: the run's jev_status and name_status as the revision
    carries them, each cut to 64 characters, None when absent."""
    cut = lambda v: None if v is None else str(v)[:STAGE_STATUS_CHARS]  # noqa: E731
    return {"jev": cut(jev_status), "names": cut(name_status)}


def _read_corpus(cfg, inventory: dict) -> tuple:
    """One read of the corpus queue: the full-tier done episodes of
    inventory shows inside the publication scope (with their fetch
    observation), the exclusion counts, and the done episodes whose files
    would stop the existing export (episode_id -> skip code)."""
    lo, hi = cfg["scope"]
    dirs = cr.corpus_dirs(cfg["corpus"])
    con = cr.connect(cfg["corpus"])
    try:
        cr.ensure_schema(con)
        stage_sets = {t: cr.tier_stages(con, t) for t in cr.TIERS}
        rows = [dict(r) for r in con.execute(
            "SELECT e.episode_id, e.feed_url, e.pub_date, e.tier, e.jev_status, e.name_status,"
            " e.fetch_observation_id, o.id AS obs_id, o.finished_at, o.final_url,"
            " o.http_status, o.bytes AS obs_bytes, o.sha256 AS obs_sha256"
            " FROM episodes e LEFT JOIN fetch_observations o"
            " ON o.id = e.fetch_observation_id WHERE e.stage='done'"
            " ORDER BY e.pub_date, o.finished_at, e.episode_id")]
    finally:
        con.close()
    bad = {}
    for r in rows:  # what cr.export reads without a guard
        ep_id = r["episode_id"]
        ran = stage_sets.get(r["tier"])
        if not (dirs["transcripts"] / f"{ep_id}.json").is_file():
            bad[ep_id] = "MISSING_TRANSCRIPT"
        elif ran is None or "detect" in ran:
            det = dirs["detections"] / f"{ep_id}.json"
            if not det.is_file():
                bad[ep_id] = "MISSING_DETECTIONS"
            elif not isinstance(cr._read_json(det), list):
                bad[ep_id] = "BAD_DETECTIONS"
    counts = {"out_of_inventory": 0, "out_of_scope": 0, "bad_episode_id": 0}
    keep = []
    for r in rows:
        if r["tier"] != "full":
            continue
        if r["feed_url"] not in inventory:
            counts["out_of_inventory"] += 1
        elif not (r["pub_date"] and lo <= str(r["pub_date"])[:10] <= hi):
            counts["out_of_scope"] += 1
        elif not EP_RE.fullmatch(r["episode_id"]):
            counts["bad_episode_id"] += 1
        else:
            r["show_key"] = inventory[r["feed_url"]]
            r["detector_ran"] = (dirs["detections"] / f"{r['episode_id']}.json").is_file()
            keep.append(r)
    return keep, counts, bad


def _record_shows(con, inventory: dict, rows: list, store: dict) -> tuple:
    """Grow-only show history in the export ledger (inside the caller's
    transaction): every show key each feed identity and each episode was
    ever seen with, so a removed show key still removes its feed and
    episodes after an inventory rename. The store's copy is added first, so
    a lost or restored ledger gets it back. (feed -> keys, episode -> keys)."""
    for table, key in (("feed_shows", "feed_shows"), ("episode_shows", "episode_shows")):
        con.executemany(f"INSERT OR IGNORE INTO {table} VALUES (?,?)",
                        [(i, k) for i, keys in store[key].items() for k in keys])
    con.executemany("INSERT OR IGNORE INTO feed_shows VALUES (?,?)", sorted(inventory.items()))
    feeds = {}
    for f, k in con.execute("SELECT feed_identity, show_key FROM feed_shows"):
        feeds.setdefault(f, set()).add(k)
    con.executemany("INSERT OR IGNORE INTO episode_shows VALUES (?,?)",
                    [(r["episode_id"], k) for r in rows for k in feeds.get(r["feed_url"], ())])
    eps = {}
    for e, k in con.execute("SELECT episode_id, show_key FROM episode_shows"):
        eps.setdefault(e, set()).add(k)
    return feeds, eps


def _name_statuses(cfg) -> dict:
    """episode_id -> name_status of done episodes, read after the export."""
    con = cr.connect(cfg["corpus"])
    try:
        return {e: n for e, n in con.execute(
            "SELECT episode_id, name_status FROM episodes WHERE stage='done'")}
    finally:
        con.close()


def _by_episode(rows, wanted, key="ep_id") -> dict:
    out = {}
    for r in rows:
        if r.get(key) in wanted:
            out.setdefault(r[key], []).append(r)
    return out


def _read_list(path) -> list:
    return json.loads(path.read_text()) if path.is_file() else []


class _Export:
    """The work export's records, indexed for the episodes in question only;
    `components` builds one episode's member bytes on demand."""

    def __init__(self, out_dir: Path, wanted: set, jev_headers: dict):
        self.out = out_dir
        self.episodes = {e["ep_id"]: e for e in json.loads(
            (out_dir / "data" / "episodes.json").read_text()) if e["ep_id"] in wanted}
        self.cands = _by_episode(_read_list(out_dir / cr.EXPORT_CANDIDATES_REL), wanted)
        self.named = _by_episode(_read_list(out_dir / cr.EXPORT_NAMED_CANDIDATES_REL), wanted)
        jev_path = out_dir / cr.EXPORT_JEV_RESULTS_REL
        run = json.loads(jev_path.read_text()) if jev_path.is_file() else {}
        self.jev = _by_episode(run.get("episodes") or [], wanted, "episode_id")
        self.headers = jev_headers          # episode_id -> frozen header (from the ledger)
        self.new_headers = {}

    def jev_header(self, ep_id: str, entries: list) -> dict:
        """This episode's own Jev run header, frozen with the digest of the
        entries it was taken for; refreshed only when those entries change
        (a re-screen or backfill), never by another episode or a constant."""
        entries_sha = _sha(_canon(entries))
        frozen = self.headers.get(ep_id)
        if frozen is None or frozen[1] != entries_sha:
            now = {k: v for k, v in cr.jev_results_doc(entries).items() if k != "episodes"}
            self.headers[ep_id] = self.new_headers[ep_id] = frozen = (now, entries_sha)
        return frozen[0]

    def components(self, r) -> dict:
        ep_id = r["episode_id"]
        c = {"fetch": _dump(self.episodes[ep_id]),
             "transcript": (self.out / "transcripts" / f"{ep_id}.json").read_bytes()}
        if r["detector_ran"]:   # the detector ran (zero rows allowed)
            c["detections"] = _dump(self.cands.get(ep_id, []))
        if ep_id in self.jev:
            c["jev"] = _dump({**self.jev_header(ep_id, self.jev[ep_id]),
                              "episodes": self.jev[ep_id]})
        if ep_id in self.named or str(r["name_status"] or "").startswith("ok"):
            c["names"] = _dump(self.named.get(ep_id, []))
        return c


def _ids_agree(ep_id: str, c: dict) -> bool:
    """5.4 on the Mini: every member names this episode."""
    try:
        if json.loads(c["fetch"]).get("ep_id") != ep_id:
            return False
        t = json.loads(c["transcript"])
        if t.get("ep_id") != ep_id or t.get("id", ep_id) != ep_id:
            return False
        for comp in ("detections", "names"):
            for row in json.loads(c.get(comp, b"[]")):
                if row.get("ep_id") != ep_id \
                        or not str(row.get("candidate_id", "")).startswith(ep_id + "."):
                    return False
        if "jev" in c and any(e.get("episode_id") != ep_id
                              for e in json.loads(c["jev"])["episodes"]):
            return False
    except (ValueError, AttributeError, KeyError, TypeError):
        return False
    return True


def _fetch_observation(r) -> dict | None:
    if r["obs_id"] is None:
        return None
    return {"id": r["obs_id"], "finished_at": r["finished_at"], "final_url": r["final_url"],
            "http_status": r["http_status"], "bytes": r["obs_bytes"], "sha256": r["obs_sha256"]}


def _revision_doc(cfg, r, seq: int, digests: dict, status: str, stages: dict) -> bytes:
    fetched = r["finished_at"] or ""
    return _dump({
        "format": REVISION_FORMAT, "producer": cfg["producer"], "episode_id": r["episode_id"],
        "revision_seq": seq, "detection_status": status, "stage_status": stages,
        "detector_ran": r["detector_ran"],
        "fetch_date": fetched[:10] or None,
        "fetch_date_basis": FETCH_DATE_BASIS if fetched else None,
        "fetch_observation": _fetch_observation(r),
        "components": {f"{k}.json": v for k, v in sorted(digests.items())}})


def _oversize(cfg, comps: dict) -> bool:
    lim = cfg["limits"]
    if any(len(b) > lim["max_file_bytes"] for b in comps.values()):
        return True
    try:
        segs = json.loads(comps["transcript"]).get("segments") or []
    except (ValueError, AttributeError):
        return True
    return len(segs) > lim["max_segments"]


# ---------------------------------------------------------------- status block (12.1)

def _reason_code(err) -> str:
    m = re.match(r"[A-Za-z_][A-Za-z0-9_.-]{0,40}", str(err or ""))
    return m.group(0) if m else "unknown"


def _jev_spend(ledger: Path, pin: Path) -> dict:
    """Today's and this month's Jev spend (10.1) through the budget module's
    own check and totals, JevBudget(ledger, pin).status(). Recovery mode
    (10.2: missing or older ledger, pin mismatch, broken chain) reports its
    code and `unknown` figures, never a zero. Committed spend counts open
    reservations and `unknown` rows at their maximum. Codes and amounts only."""
    unknown = {"day_usd": "unknown", "month_usd": "unknown"}
    try:
        s = jev_budget.JevBudget(ledger, pin).status()
        if s.get("mode") == "recovery":
            return {**unknown, "source": "jev_budget", "mode": "recovery",
                    "code": str(s.get("code") or "unknown")[:64],
                    "reason": str(s.get("reason") or "unknown")[:64]}
        return {"source": "jev_budget", "mode": "normal",
                "day_usd": s["day"]["committed_usd"], "month_usd": s["month"]["committed_usd"],
                "day_exhausted": s["day"]["exhausted"],
                "month_exhausted": s["month"]["exhausted"],
                "reserved": {"day": s["day"]["reserved"], "month": s["month"]["reserved"]},
                "unknown": {"day": s["day"]["unknown"], "month": s["month"]["unknown"]},
                "caps_usd": s["caps_usd"]}
    except Exception:  # never fail the nightly package on the status block
        return {**unknown, "source": "unreadable"}


def status_block(cfg, now: datetime, export_counts: dict, alerts: dict,
                 dead_listed=lambda feed_url, ep_id: True) -> dict:
    con = cr.connect(cfg["corpus"])
    try:
        queue = {}
        for tier, stage, state, n in con.execute(
                "SELECT tier, stage, state, COUNT(*) FROM episodes GROUP BY 1, 2, 3"):
            queue.setdefault(tier, {}).setdefault(stage, {})[state] = n
        dead = [{"episode_id": r[0], "dead_stage": r[1], "reason": _reason_code(r[2])}
                for r in con.execute("SELECT episode_id, dead_stage, last_error, feed_url"
                                     " FROM episodes WHERE stage='dead' ORDER BY episode_id")
                if dead_listed(r[3], r[0])]
        by_reason = {}
        for d in dead:
            k = f"{d['dead_stage']}:{d['reason']}"
            by_reason[k] = by_reason.get(k, 0) + 1
        retry = con.execute("SELECT COUNT(*), MIN(updated_at) FROM episodes WHERE"
                            " stage NOT IN ('done','dead') AND state='ready' AND attempts > 0"
                            ).fetchone()
        last = con.execute("SELECT command, started_at, finished_at, report FROM discovery_log"
                           " ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    oldest_wait = None
    if retry[1]:
        try:
            oldest_wait = int((now - datetime.fromisoformat(retry[1])).total_seconds())
        except ValueError:
            oldest_wait = None
    discovery = None
    if last:
        try:
            rep = json.loads(last[3] or "null")
        except ValueError:
            rep = None
        discovery = {"command": last[0], "started_at": last[1], "finished_at": last[2],
                     "counts": {k: v for k, v in (rep or {}).items()
                                if isinstance(v, (int, float)) and not isinstance(v, bool)}
                     if isinstance(rep, dict) else {}}
    return {"format": STATUS_FORMAT, "producer": cfg["producer"], "generated_at": _iso(now),
            "queue": queue,
            "dead": {"count": len(dead), "by_reason": by_reason, "episodes": dead[:1000]},
            "waiting_retries": {"count": retry[0], "oldest_updated_at": retry[1],
                                "oldest_wait_s": oldest_wait},
            "jev_spend": _jev_spend(cfg["jev_budget_ledger"], cfg["jev_budget_pin"]),
            "last_discovery": discovery,
            "free_disk_bytes": shutil.disk_usage(cfg["corpus"]).free,
            "alerts": alerts,
            "export": export_counts}


# ---------------------------------------------------------------- package files

def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime, info.type = size, 0o644, 0, tarfile.REGTYPE
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _tar_size(sizes) -> int:
    body = sum(512 + -(-s // 512) * 512 for s in sizes) + 1024
    return -(-body // _RECORD) * _RECORD


def _fsync_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sign(key: Path, path: Path, ns: str) -> None:
    r = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", ns, "-f", str(key), str(path)],
                       stdin=subprocess.DEVNULL, capture_output=True)
    if r.returncode != 0 or not Path(str(path) + ".sig").is_file():
        raise Refused("SIGN_FAILED")


def _read_bounded(path: Path, limit: int, code: str) -> bytes:
    """At most `limit` bytes of `path`: refused (`code`) before any read when
    its size is larger, and never more than limit + 1 bytes read whatever
    the size claims (a file growing or lying about its size)."""
    # never block on a FIFO or follow a link; a regular file only (OSError otherwise)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        if st.st_size > limit:
            raise Refused(code)
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise Refused(code)
    return data


def _verify(allowed: Path, principal: str, data: bytes, sig: bytes, ns: str,
            tmp_dir: Path) -> bool:
    """ssh-keygen -Y verify of `data` (stdin) against the bounded signature
    bytes, written to a private file first so ssh-keygen reads only those."""
    if not allowed.is_file():
        return False
    fd, sig_path = tempfile.mkstemp(prefix=".receipt-", suffix=".sig", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(sig)
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", principal,
                            "-n", ns, "-s", sig_path], input=data, capture_output=True)
    finally:
        os.unlink(sig_path)
    return r.returncode == 0


def _delete_package_files(outbox: Path, pid: str) -> int:
    """Best effort, `.done` first so the set stops being complete at once;
    the number of files that could not be deleted."""
    failed = 0
    for s in ("done",) + SUFFIXES[:3]:
        try:
            (outbox / f"{pid}.{s}").unlink(missing_ok=True)
        except OSError:
            failed += 1
    return failed


class _TarBuilder:
    """Streams regular members into <outbox>/.building/package.tar (USTAR,
    uncompressed, fixed owner and time) and records each member's size and
    digest for the manifest."""

    def __init__(self, outbox: Path):
        self.outbox = outbox
        self.build = outbox / ".building"
        shutil.rmtree(self.build, ignore_errors=True)
        self.build.mkdir(parents=True)
        self.path = self.build / "package.tar"
        self.fh = open(self.path, "wb")
        self.tf = tarfile.open(fileobj=self.fh, mode="w", format=tarfile.USTAR_FORMAT)
        self.members = []

    def add(self, name: str, data: bytes) -> None:
        self.tf.addfile(_tar_info(name, len(data)), io.BytesIO(data))
        self.members.append({"name": name, "size": len(data), "sha256": _sha(data)})

    def seal(self, cfg, revisions: list, epoch: int, now: datetime) -> str:
        """Close the tar, write and sign the manifest, move the three files
        into the outbox (each atomically) and return the package ID; `.done`
        comes later."""
        self.tf.close()
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()
        size = self.path.stat().st_size
        if size > cfg["limits"]["max_tar_bytes"]:
            raise Refused("PACKAGE_TOO_LARGE")
        manifest = _canon({
            "format": MANIFEST_FORMAT, "producer": cfg["producer"], "sealed_at": _iso(now),
            "restore_epoch": epoch, "tar": {"size": size, "sha256": _file_sha(self.path)},
            "members": self.members, "revisions": revisions})
        pid = "p-" + _sha(manifest)[:32]
        man = self.build / "manifest.json"
        _fsync_write(man, manifest)
        _sign(cfg["signing_key"], man, PACKAGE_NS)
        os.replace(self.path, self.outbox / f"{pid}.tar")
        os.replace(man, self.outbox / f"{pid}.manifest.json")
        os.replace(Path(str(man) + ".sig"), self.outbox / f"{pid}.manifest.json.sig")
        _fsync_dir(self.outbox)
        shutil.rmtree(self.build, ignore_errors=True)
        return pid

    def abort(self) -> None:
        try:
            self.tf.close()
            self.fh.close()
        except Exception:
            pass
        shutil.rmtree(self.build, ignore_errors=True)


def _write_done(outbox: Path, pid: str) -> None:
    _fsync_write(outbox / f"{pid}.done", b"")
    _fsync_dir(outbox)


# ---------------------------------------------------------------- export-delta

def _delete_offered_files(con, cfg) -> dict:
    """Delete the files of every offered package (no ledger write), so they
    are withdrawn from the outbox even when nothing can be saved; package ID
    -> files that could not be deleted."""
    pids = [r[0] for r in con.execute("SELECT package_id FROM packages WHERE state='offered'")]
    return {pid: _delete_package_files(cfg["outbox"], pid) for pid in pids}


def _sweep_package_files(cfg) -> int:
    """No ledger to name the offered packages: delete every package file in
    the outbox, best effort; the number that could not be deleted."""
    failed = 0
    try:
        names = [p for p in cfg["outbox"].iterdir() if PACKAGE_FILE_RE.fullmatch(p.name)]
    except FileNotFoundError:
        return 0
    except OSError:
        return 1
    for p in names:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            failed += 1
    return failed


def _ledger_withdraw_pause(con, cfg, code: str, removals: dict | None,
                           deleted: dict | None = None) -> None:
    """One transaction: offered packages marked superseded and the pause
    (with `removals` pending) recorded together, so no crash can leave a
    withdrawal without its pause."""
    con.execute("BEGIN IMMEDIATE")
    try:
        _withdraw_offered(con, cfg, deleted)
        _ledger_pause_rows(con, code, removals)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _withdraw_and_pause_now(cfg, code: str, removals: dict | None) -> tuple:
    """A receipt that pauses offering withdraws every offered package before
    anything else is saved: their files go first, best effort (every package
    file in the outbox when the ledger cannot even be read), then the
    supersede mark and the ledger pause in one transaction, whatever the
    deletes did. (True when that transaction committed, files not deleted)."""
    try:
        con = open_ledger(cfg["ledger"])
    except Exception:
        con = None
    try:
        deleted = None
        if con is not None:
            try:
                deleted = _delete_offered_files(con, cfg)
            except Exception:
                deleted = None
        undeleted = sum(deleted.values()) if deleted is not None else _sweep_package_files(cfg)
        if con is None:
            return False, undeleted
        try:
            _ledger_withdraw_pause(con, cfg, code, removals, deleted)
            return True, undeleted
        except Exception:
            return False, undeleted
    finally:
        if con is not None:
            con.close()


def _withdraw_offered(con, cfg, deleted: dict | None = None) -> tuple:
    """Withdraw every offered package: files deleted first (unless `deleted`
    already says how that went), then marked superseded in any case; a
    package with a file left is marked files_deleted=0 so tidy retries.
    Their revisions are offered again as current revisions, minus removals.
    (packages withdrawn, files not deleted)."""
    if deleted is None:
        deleted = _delete_offered_files(con, cfg)
    for pid, failed in deleted.items():
        con.execute("UPDATE packages SET state='superseded', files_deleted=? WHERE"
                    " package_id=? AND state='offered'", (0 if failed else 1, pid))
    return len(deleted), sum(deleted.values())


def _tidy_outbox(con, cfg, now: datetime, removed_eps: frozenset, removed_shows: frozenset,
                 shows_now: frozenset, ep_shows: dict, withdraw_all: bool = False) -> dict:
    """Expire packages past the store, withdraw offered packages holding a
    removed item (every offered package with `withdraw_all`), delete
    orphans, finish a missing `.done` of a complete set. A packaged
    revision's show is the one the ledger stored with it; a row without one
    (from before show_key was stored), or a show no longer in the inventory
    (`shows_now`), counts as removed (fail closed). Any show key the episode
    ever had (`ep_shows`, renames included) that is removed removes it.
    Deletes are best effort: a withdrawn or expired package with a file
    left keeps files_deleted=0 and is retried; `undeleted_files` counts them."""
    def removed(r) -> bool:
        show = r.get("show_key")   # None is never in the inventory
        shows = {show} | ep_shows.get(r["episode_id"], set())
        return (r["episode_id"] in removed_eps or bool(shows & removed_shows)
                or show not in shows_now)

    outbox = cfg["outbox"]
    outbox.mkdir(parents=True, exist_ok=True)
    cutoff = now - timedelta(days=cfg["keep_days"])
    counts = {"expired_packages": 0, "withdrawn_packages": 0, "undeleted_files": 0}
    known = {}
    for pid, sealed, state, revs, deleted in con.execute(
            "SELECT package_id, sealed_at, state, revisions, files_deleted FROM packages"
            ).fetchall():
        old = datetime.fromisoformat(sealed.replace("Z", "+00:00")) < cutoff
        has_removed = withdraw_all or any(removed(r) for r in json.loads(revs))
        new_state = state
        if state == "offered" and old:
            new_state = "expired"
            counts["expired_packages"] += 1
        elif state == "offered" and has_removed:
            new_state = "superseded"
            counts["withdrawn_packages"] += 1
        retry = state in ("superseded", "expired")
        if (old or has_removed or new_state != state or retry) and not deleted:
            failed = _delete_package_files(outbox, pid)
            counts["undeleted_files"] += failed
            con.execute("UPDATE packages SET state=?, files_deleted=? WHERE package_id=?",
                        (new_state, 0 if failed else 1, pid))
        elif new_state != state:
            con.execute("UPDATE packages SET state=? WHERE package_id=?", (new_state, pid))
        elif state == "offered" and not (outbox / f"{pid}.done").exists() and all(
                (outbox / f"{pid}.{s}").is_file() for s in SUFFIXES[:3]):
            _write_done(outbox, pid)
        known[pid] = new_state
    for p in outbox.iterdir():
        m = PACKAGE_FILE_RE.fullmatch(p.name)
        if p.is_file() and (m is None and p.name.endswith(".part")
                            or m is not None and m.group(1) not in known):
            try:
                p.unlink()
            except OSError:
                counts["undeleted_files"] += 1
    return counts


def export_delta(cfg) -> tuple:
    now = _now()
    res = {"cmd": "export-delta", "code": "ok", "package_id": None, "offered": [],
           "alerts": [], "counts": {}}
    try:
        with _Locked(cfg["ledger"]):
            return _export_delta(cfg, now, res)
    except Refused as exc:
        res["code"] = f"refused:{exc.code}"
        if exc.detail:
            res["detail"] = exc.detail
        return EXIT_REFUSED, res
    except Exception as exc:  # one JSON line on every path; no payload text
        res["code"] = f"failed:{type(exc).__name__}"
        return EXIT_FAILED, res


def _skip(counts: dict, code: str) -> None:
    counts["skipped"][code] = counts["skipped"].get(code, 0) + 1


def _tidy_phase(cfg, now, con, inventory: dict) -> dict:
    """What export-delta does before building, and all export-tidy does:
    the union removal set (local file, store, ledger pending), the show-key
    history, the pause; withdraw affected offered packages (everything
    offered when paused), retry undeletable files, save the pause and the
    store copy of the history. Refused(WITHDRAW_INCOMPLETE) while a
    withdrawn file remains."""
    local_eps, local_shows = _removals(cfg["removal_list"])
    store = _read_store(cfg["removal_store"])      # host removals: before replay and build
    ledger_pause, pending = _ledger_removals(con)   # the store could not be written
    removed_eps = frozenset(local_eps | store["episode_ids"] | pending["episode_ids"])
    removed_shows = frozenset(local_shows | store["show_keys"] | pending["show_keys"])
    paused = store["paused"] or ledger_pause
    if paused is None and _store_entries(store) < int(_meta(con, "removal_store_entries", 0)):
        paused = "STORE_BEHIND"            # the store lost removals the ledger saw
    if paused is not None:
        _delete_offered_files(con, cfg)    # withdrawn before anything is saved
    rows, counts, bad = _read_corpus(cfg, inventory)
    counts["skipped"] = {}
    show_of = {r["episode_id"]: r["show_key"] for r in rows}
    con.execute("BEGIN IMMEDIATE")
    feed_shows, ep_shows = _record_shows(con, inventory, rows, store)
    counts.update(_tidy_outbox(con, cfg, now, removed_eps, removed_shows,
                               frozenset(inventory.values()), ep_shows,
                               withdraw_all=paused is not None))
    con.execute("COMMIT")
    history = {"feed_shows": feed_shows, "episode_shows": ep_shows}
    if paused is not None:                 # store, else ledger, else no package
        counts["pause_saved_in"] = _persist_pause(cfg, paused, pending, history)
    elif _merge_history(store, history):   # the store's copy of the history
        try:
            fresh = _read_store(cfg["removal_store"])
            if fresh["paused"] is not None:
                raise ValueError("paused meanwhile")
            _merge_history(fresh, history)
            _write_store(cfg["removal_store"], fresh)
        except Exception:
            # the ledger still holds it; the alert says the store copy lags
            _alert(con, now, "STORE_HISTORY_UNSAVED")
    if counts["undeleted_files"]:
        # a withdrawn package still has files in the outbox: no package
        # tonight, and the uploader stops; tidy retries the deletes
        raise Refused("WITHDRAW_INCOMPLETE")
    return {"paused": paused, "removed_eps": removed_eps, "removed_shows": removed_shows,
            "rows": rows, "counts": counts, "bad": bad, "show_of": show_of,
            "feed_shows": feed_shows, "ep_shows": ep_shows}


def export_tidy(cfg) -> tuple:
    """export-tidy: the withdrawal/tidy of export-delta alone (before an
    upload with no new build); builds nothing. ok (0); REMOVALS_UNVERIFIED (2)
    when offering is paused (everything offered withdrawn);
    refused:WITHDRAW_INCOMPLETE (3) while a withdrawn file remains; other
    refused:<CODE> (3) or failed:<Type> (1)."""
    now = _now()
    res = {"cmd": "export-tidy", "code": "ok", "offered": [], "counts": {}}
    try:
        with _Locked(cfg["ledger"]):
            inventory = _inventory(cfg["inventory"])
            con = open_ledger(cfg["ledger"])
            try:
                _active_epoch(con, cfg)
                t = _tidy_phase(cfg, now, con, inventory)
                res["offered"] = [r[0] for r in con.execute(
                    "SELECT package_id FROM packages WHERE state='offered' ORDER BY sealed_at")]
            finally:
                con.close()
        res["counts"] = {k: v for k, v in t["counts"].items() if k != "skipped"}
        if t["paused"] is not None:
            res["code"] = REMOVALS_UNVERIFIED
            res["detail"] = t["paused"]
            return EXIT_BACKLOG, res
        return EXIT_OK, res
    except Refused as exc:
        res["code"] = f"refused:{exc.code}"
        return EXIT_REFUSED, res
    except Exception as exc:  # one JSON line on every path; no payload text
        res["code"] = f"failed:{type(exc).__name__}"
        return EXIT_FAILED, res


def _export_delta(cfg, now, res) -> tuple:
    inventory = _inventory(cfg["inventory"])
    cfg["work_dir"].mkdir(parents=True, exist_ok=True)
    if not _same_volume(cfg["work_dir"], cfg["corpus"]):  # transcripts are hard links
        raise Refused("WORK_DIR_VOLUME")
    con = open_ledger(cfg["ledger"])
    try:
        epoch = _active_epoch(con, cfg)
        t = _tidy_phase(cfg, now, con, inventory)
        paused, removed_eps, removed_shows = t["paused"], t["removed_eps"], t["removed_shows"]
        rows, counts, bad = t["rows"], t["counts"], t["bad"]
        feed_shows, ep_shows, show_of = t["feed_shows"], t["ep_shows"], t["show_of"]

        def is_removed(ep_id: str, feed: str) -> bool:
            # the episode, or any show key it or its feed ever had
            return ep_id in removed_eps or bool(
                (ep_shows.get(ep_id, set()) | feed_shows.get(feed, set())) & removed_shows)
        offered = [r[0] for r in con.execute(
            "SELECT package_id FROM packages WHERE state='offered' ORDER BY sealed_at")]
        res["offered"] = offered
        counts["unacknowledged_packages"] = len(offered)
        res["counts"] = counts
        if len(offered) >= cfg["max_unacknowledged"]:
            _alert(con, now, "INBOX_BACKLOG", counts)
            res["code"] = "INBOX_BACKLOG"
            res["alerts"] = ["INBOX_BACKLOG"]
            return EXIT_BACKLOG, res

        live = [r for r in rows if not is_removed(r["episode_id"], r["feed_url"])]
        counts["removed"] = len(rows) - len(live)
        current = _current_revisions(con)
        acks = {(t, e): s for t, e, s in con.execute(
            "SELECT db_identity, episode_id, revision_seq FROM acks")}
        refused = {(t, e, s) for t, e, s in con.execute(
            "SELECT db_identity, episode_id, revision_seq FROM refusals")}
        pending = set()
        for (revs,) in con.execute("SELECT revisions FROM packages WHERE state='offered'"):
            pending |= {(x["episode_id"], x["revision_seq"]) for x in json.loads(revs)}

        todo, exp = [], None
        if paused is not None:
            # no verified removal list: offer nothing until one arrives (C2);
            # the status block still goes, so its receipt can bring the list
            _alert(con, now, REMOVALS_UNVERIFIED, {"reason": paused})
            res["code"] = REMOVALS_UNVERIFIED
        elif _free_bytes(cfg["work_dir"]) < cfg["min_free_bytes"]:
            _alert(con, now, "LOW_DISK")       # no export tonight; the status block still goes
            res["code"] = "LOW_DISK"
        else:
            for r in live:
                if r["episode_id"] in bad:
                    _skip(counts, bad[r["episode_id"]])
            try:
                cr.export(cfg["corpus"], cfg["work_dir"] / "export", exclude=frozenset(bad),
                          media=False)
                headers = {e: (json.loads(h), sha) for e, h, sha in con.execute(
                    "SELECT episode_id, header, entries_sha256 FROM jev_headers")}
                exp = _Export(cfg["work_dir"] / "export",
                              {r["episode_id"] for r in live if r["episode_id"] not in bad},
                              headers)
            except Exception:
                _alert(con, now, "EXPORT_FAILED")  # status block only, never a guess
                res["code"] = "EXPORT_FAILED"
        new = eligible = 0
        if exp is not None:
            names_now = _name_statuses(cfg)   # the fetch row carries no name_status
            con.execute("BEGIN IMMEDIATE")
            for r in live:
                ep_id = r["episode_id"]
                row = exp.episodes.get(ep_id)
                if ep_id in bad or row is None:
                    continue
                # jev_status, name_status and the observation exactly as the
                # export saw them
                if row.get("jev_status") != r["jev_status"] \
                        or names_now.get(ep_id) != r["name_status"] \
                        or row.get("fetch_observation_id") != r["fetch_observation_id"]:
                    _skip(counts, "CHANGED_DURING_EXPORT")
                    continue
                try:
                    c = exp.components(r)
                except (OSError, ValueError, KeyError, TypeError):
                    _skip(counts, "READ_ERROR")
                    continue
                digests = {k: _sha(v) for k, v in c.items()}
                del c
                status = detection_status(r["detector_ran"], "jev" in digests,
                                          "names" in digests, r["jev_status"], r["name_status"])
                stages = stage_status(r["jev_status"], r["name_status"])
                eligible += 1
                r["ready"] = True
                cur = current.get(ep_id)
                # any change of what revision.json states is a new revision_seq
                if cur is None or cur[1] != digests or cur[2] != status or cur[3] != stages:
                    seq = con.execute(
                        "INSERT INTO revisions (episode_id, digests, detection_status,"
                        " created_at, stage_status) VALUES (?,?,?,?,?)",
                        (ep_id, json.dumps(digests, sort_keys=True), status, _iso(now),
                         json.dumps(stages, sort_keys=True))).lastrowid
                    current[ep_id] = (seq, digests, status, stages)
                    new += 1
            for ep_id, (h, sha) in exp.new_headers.items():
                con.execute("INSERT OR REPLACE INTO jev_headers (episode_id, header,"
                            " entries_sha256) VALUES (?,?,?)", (ep_id, json.dumps(h), sha))
            con.execute("COMMIT")
            for r in live:
                ep_id = r["episode_id"]
                if not r.get("ready"):
                    continue
                seq = current[ep_id][0]
                if (ep_id, seq) in pending:
                    continue
                if any(acks.get((t, ep_id), 0) < seq and (t, ep_id, seq) not in refused
                       for t in cfg["targets"]):
                    todo.append((seq, r))
            todo.sort(key=lambda x: x[0])
        counts["eligible"] = eligible
        counts["new_revisions"] = new

        lim = cfg["limits"]
        unreported = {}
        for code, n in con.execute("SELECT code, COUNT(*) FROM alerts WHERE reported=0"
                                   " GROUP BY code"):
            unreported[code] = n
        dead_listed = lambda feed, e: feed in inventory and not is_removed(e, feed)  # noqa: E731
        # room for the status block: its size with these counts, plus slack
        reserve = len(_dump(status_block(cfg, now, {**counts, "offered_revisions": 0,
                                                    "held_by_limits": 0}, unreported,
                                         dead_listed))) + 4096
        tar = _TarBuilder(cfg["outbox"])
        try:
            revisions, sizes, n_members = [], [], 0
            held = 0
            for seq, r in todo:
                ep_id = r["episode_id"]
                try:
                    c = exp.components(r)
                except (OSError, ValueError, KeyError, TypeError):
                    _skip(counts, "READ_ERROR")
                    continue
                digests, status, stages = current[ep_id][1:]
                if {k: _sha(v) for k, v in c.items()} != digests:
                    _skip(counts, "CHANGED_DURING_EXPORT")
                    continue
                if not _ids_agree(ep_id, c):
                    _skip(counts, "ID_MISMATCH")   # never packaged
                    continue
                if _oversize(cfg, c):
                    _skip(counts, "OVERSIZE")
                    continue
                mine = [(f"{ep_id}/revision.json",
                         _revision_doc(cfg, r, seq, digests, status, stages))]
                mine += [(f"{ep_id}/{k}.json", c[k]) for k in COMPONENTS if k in c]
                del c
                if not (len(revisions) < lim["max_episodes"]
                        and n_members + len(mine) + 1 <= lim["max_members"]
                        and _tar_size(sizes + [len(d) for _, d in mine] + [reserve])
                        <= lim["max_tar_bytes"]):
                    held += 1
                    continue
                for name, data in mine:
                    tar.add(name, data)
                sizes += [len(d) for _, d in mine]
                n_members += len(mine)
                revisions.append({"episode_id": ep_id, "revision_seq": seq,
                                  "detection_status": status})
            counts.update(offered_revisions=len(revisions), held_by_limits=held)
            tar.add(STATUS_MEMBER, _dump(status_block(cfg, now, dict(counts), unreported,
                                                      dead_listed)))
            pid = tar.seal(cfg, revisions, epoch, now)
        except BaseException:
            tar.abort()
            raise
        manifest_sha = _file_sha(cfg["outbox"] / f"{pid}.manifest.json")
        con.execute("BEGIN IMMEDIATE")
        # the ledger keeps each packaged revision's show, so withdrawal never
        # depends on tomorrow's inventory or corpus rows
        con.execute("INSERT INTO packages (package_id, manifest_sha256, sealed_at, restore_epoch,"
                    " revisions, state) VALUES (?,?,?,?,?, 'offered')",
                    (pid, manifest_sha, _iso(now), epoch, json.dumps(
                        [{**x, "show_key": show_of[x["episode_id"]]} for x in revisions])))
        con.execute("UPDATE alerts SET reported=1 WHERE reported=0")
        con.execute("COMMIT")
        _write_done(cfg["outbox"], pid)
        res["package_id"] = pid
        res["offered"] = offered + [pid]
        res["alerts"] = sorted(unreported)
        counts["unacknowledged_packages"] = len(res["offered"])
        return (EXIT_OK if res["code"] == "ok" else EXIT_BACKLOG), res
    finally:
        con.close()


# ---------------------------------------------------------------- export-receipt

def _rev_list(items, with_code=False) -> list:
    out = []
    for x in items or []:
        if not (isinstance(x, dict) and isinstance(x.get("episode_id"), str)
                and EP_RE.fullmatch(x["episode_id"]) and isinstance(x.get("revision_seq"), int)
                and not isinstance(x["revision_seq"], bool)):
            raise Refused("RECEIPT_INVALID")
        if with_code and not (isinstance(x.get("code"), str) and CODE_RE.fullmatch(x["code"])):
            raise Refused("RECEIPT_INVALID")
        out.append(x)
    return out


def export_receipt(cfg, path) -> tuple:
    path = Path(path)
    res = {"cmd": "export-receipt", "code": "ok", "kind": None, "alerts": [], "counts": {}}
    try:
        with _Locked(cfg["ledger"]):
            return _export_receipt(cfg, path, res)
    except Refused as exc:
        res["code"] = f"refused:{exc.code}"
        if exc.detail:
            res["detail"] = exc.detail
        return EXIT_REFUSED, res
    except Exception as exc:
        res["code"] = f"failed:{type(exc).__name__}"
        return EXIT_FAILED, res


def _verified_receipt(cfg, path: Path) -> tuple:
    """(data, doc, removals) of a receipt whose signature, shape and removal
    list check; Refused otherwise."""
    try:
        data = _read_bounded(path, MAX_RECEIPT_BYTES, "RECEIPT_TOO_LARGE")
    except OSError as exc:
        raise Refused("RECEIPT_UNREADABLE") from exc
    try:
        sig = _read_bounded(Path(str(path) + ".sig"), MAX_SIGNATURE_BYTES,
                            "SIGNATURE_TOO_LARGE")
    except OSError as exc:
        raise Refused("BAD_SIGNATURE") from exc
    if not _verify(cfg["receipt_allowed_signers"], cfg["receipt_principal"], data, sig,
                   RECEIPT_NS, cfg["ledger"].parent):
        raise Refused("BAD_SIGNATURE")
    try:
        doc = json.loads(data)
        kind, epoch, targets = doc["kind"], doc["restore_epoch"], doc["targets"]
        if kind not in ("receipt", "reconcile") or not isinstance(epoch, int) \
                or isinstance(epoch, bool) or not isinstance(targets, list) \
                or not all(isinstance(t, dict) and isinstance(t.get("db_identity"), str)
                           for t in targets):
            raise ValueError("shape")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Refused("RECEIPT_INVALID") from exc
    return data, doc, _verified_removals(doc)


def _keep_removals(cfg, con, removals: dict) -> dict:
    """Contract C2: add a verified host list to the store (a union, never
    shrunk); new removals withdraw every offered package at once, before
    any replay. A store with fewer entries than the ledger last saw was
    lost or rolled back: offering pauses until a fully accepted receipt.
    The ledger's mark only grows. `complete`: this list holds the whole
    store, so a fully accepted receipt carrying it may resume offering."""
    store = _read_store(cfg["removal_store"])
    mark = int(_meta(con, "removal_store_entries", 0))
    if store["paused"] is None and _store_entries(store) < mark:
        store["paused"] = "STORE_BEHIND"
    ledger_pause, pending = _ledger_removals(con)
    grew = False
    for key in REMOVAL_KEYS:
        store[key] |= pending[key]
        new = removals[key] - store[key]
        if new:
            store[key] |= new
            grew = True
    # withdraw before anything is saved: files first, then the ledger
    deleted = _delete_offered_files(con, cfg) if grew else {}
    con.execute("BEGIN IMMEDIATE")
    if deleted:
        _withdraw_offered(con, cfg, deleted)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('removal_store_entries', ?)",
                (str(max(mark, _store_entries(store))),))
    con.execute("COMMIT")
    _write_store(cfg["removal_store"], store)   # failure: the caller saves pause and list
    complete = all(store[k] <= removals[k] for k in REMOVAL_KEYS)
    return {"store": store, "withdrawn": len(deleted), "complete": complete,
            "ledger_paused": ledger_pause is not None,
            "undeleted": sum(deleted.values())}


def _export_receipt(cfg, path: Path, res) -> tuple:
    con = removals = None
    try:
        data, doc, removals = _verified_receipt(cfg, path)
        res["kind"] = doc["kind"]
        con = open_ledger(cfg["ledger"])
        kept = _keep_removals(cfg, con, removals)   # whatever happens to the rest
    except Exception as exc:
        # unverifiable, mismatched or not kept, for any reason: nothing is
        # offered until a valid list arrives (C2)
        if con is not None:
            con.close()
        res["alerts"] = [REMOVALS_UNVERIFIED]
        code = exc.code if isinstance(exc, Refused) else "RECEIPT_CHECK_FAILED"
        in_ledger, undeleted = _withdraw_and_pause_now(cfg, code, removals)
        try:
            _persist_pause(cfg, code, removals)   # the store, else the ledger again
        except Refused:
            if not in_ledger:
                raise       # refused:REMOVALS_PAUSE_UNSAVED: the uploader skips tonight
        if undeleted:       # a withdrawn package file is still in the outbox: stop
            raise Refused("WITHDRAW_INCOMPLETE") from exc
        raise
    kind, epoch, targets = doc["kind"], doc["restore_epoch"], doc["targets"]
    try:
        res["counts"] = {"removals_withdrawn_packages": kept["withdrawn"]}
        if not targets:
            raise Refused("EMPTY_TARGETS")
        if any(t["db_identity"] not in cfg["targets"] for t in targets):
            raise Refused("UNKNOWN_TARGET")
        if len({t["db_identity"] for t in targets}) != len(targets):
            raise Refused("RECEIPT_INVALID")
        active = _active_epoch(con, cfg)
        if kind == "receipt" and epoch != active or kind == "reconcile" and epoch <= active:
            raise Refused("WRONG_EPOCH")
        file_sha = _sha(data)
        if con.execute("SELECT 1 FROM receipts WHERE file_sha256=?", (file_sha,)).fetchone():
            res["code"] = "duplicate"
            return EXIT_OK, res
        known = {e: s for e, s in con.execute(
            "SELECT episode_id, MAX(revision_seq) FROM revisions GROUP BY episode_id")}
        counts = {"targets": len(targets), "accepted": 0, "stale": 0, "refused": 0,
                  "deferred": 0, "unknown_revisions": 0}
        now = _iso(_now())
        con.execute("BEGIN IMMEDIATE")
        try:
            if kind == "receipt":
                digest = doc.get("package_digest")
                if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise Refused("RECEIPT_INVALID")
                parsed = [(t["db_identity"], _rev_list(t.get("accepted")),
                           _rev_list(t.get("stale")), _rev_list(t.get("refused"), True),
                           _rev_list(t.get("deferred"))) for t in targets]
                for target, accepted, stale, refused, deferred in parsed:
                    # accepted and stale: the target holds this revision or a later one
                    for x in accepted + stale:
                        if not 0 < x["revision_seq"] <= known.get(x["episode_id"], 0):
                            counts["unknown_revisions"] += 1
                            continue
                        con.execute(
                            "INSERT INTO acks VALUES (?,?,?) ON CONFLICT (db_identity,"
                            " episode_id) DO UPDATE SET revision_seq ="
                            " MAX(revision_seq, excluded.revision_seq)",
                            (target, x["episode_id"], x["revision_seq"]))
                    for x in refused:  # not offered again at this revision
                        con.execute("INSERT OR REPLACE INTO refusals VALUES (?,?,?,?)",
                                    (target, x["episode_id"], x["revision_seq"], x["code"]))
                    counts["accepted"] += len(accepted)
                    counts["stale"] += len(stale)
                    counts["refused"] += len(refused)
                    counts["deferred"] += len(deferred)  # stays unacknowledged: offered again
                counts["package_acknowledged"] = con.execute(
                    "UPDATE packages SET state='acknowledged' WHERE manifest_sha256=?"
                    " AND state='offered'", (digest,)).rowcount
            else:
                listing = [(t["db_identity"], _rev_list(t.get("revisions"))) for t in targets]
                con.execute("DELETE FROM acks")
                con.execute("DELETE FROM refusals")
                for target, revs in listing:
                    for x in revs:
                        if not 0 < x["revision_seq"] <= known.get(x["episode_id"], 0):
                            counts["unknown_revisions"] += 1   # never acked
                            continue
                        con.execute(
                            "INSERT INTO acks VALUES (?,?,?) ON CONFLICT (db_identity,"
                            " episode_id) DO UPDATE SET revision_seq ="
                            " MAX(revision_seq, excluded.revision_seq)",
                            (target, x["episode_id"], x["revision_seq"]))
                        counts["accepted"] += 1
                counts["withdrawn_packages"], left = _withdraw_offered(con, cfg)
                kept["undeleted"] += left
                con.execute("INSERT OR REPLACE INTO meta VALUES ('active_epoch', ?)",
                            (str(epoch),))
                digest = None
            if counts["unknown_revisions"]:
                _alert(con, _now(), "UNKNOWN_REVISIONS", counts)
                res["alerts"] = ["UNKNOWN_REVISIONS"]
            con.execute("INSERT INTO receipts (file_sha256, kind, restore_epoch, package_digest,"
                        " accepted_at, counts) VALUES (?,?,?,?,?,?)",
                        (file_sha, kind, epoch, digest, now, json.dumps(counts, sort_keys=True)))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        store = kept["store"]
        if (store["paused"] is not None or kept["ledger_paused"]) and kept["complete"]:
            # a fully accepted receipt of the active epoch whose list holds the
            # whole store: offering resumes once the store is written (the
            # mark stays; a store still below it pauses again as STORE_BEHIND)
            store["paused"] = None
            _write_store(cfg["removal_store"], store)
            con.execute("DELETE FROM meta WHERE key IN ('removals_pause', 'removals_pending')")
            counts["removals_resumed"] = 1
        counts["removals_withdrawn_packages"] = kept["withdrawn"]
        res["counts"] = counts
        if kept["undeleted"]:   # receipt applied, but a withdrawn package file remains
            raise Refused("WITHDRAW_INCOMPLETE")
        return EXIT_OK, res
    finally:
        con.close()
