"""Canonical research bundle pipeline (K04): Query2 matching, summaries, selectors.

One canonical parser/match pipeline shared by results, counts, charts and
export scope evaluation. Post-pagination filtering does not exist: set
predicates apply before pagination inside research_bundle. Reads run in
one caller-owned read transaction so rows, counts, summaries and the
view revision come from a single authorized cohort.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import sqlite3
import threading
import time
import weakref
from collections import OrderedDict
from datetime import date, timedelta

from earwitness.app import ad_reads as _ad
from earwitness.app import ad_breaks as _breaks
from earwitness.app import customer as _customer
from earwitness.app import customer_contracts as _contracts
from earwitness.app import db as _db
from earwitness.app import media as _media
from earwitness.app import search as _search

PER_PAGE = 10
FILTER_VALUE_LIMIT = 50
EXPORT_ID_LIMIT = 200
# A complete export (all matches, or a summary cell's scope) holds at most
# this many results, counted as the research view counts result.total.
# app/views.js mirrors it (tests/test_export_cap pins both).
EXPORT_RESULT_CAP = 150
EXPORT_CAP_MESSAGE = (f"Exports are limited to {EXPORT_RESULT_CAP} results. Narrow the"
                      " search or select the results you need.")
# The Ads tab counts ads; an ads export's results are pieces of evidence.
EXPORT_CAP_ADS_MESSAGE = (f"Exports are limited to {EXPORT_RESULT_CAP} pieces of evidence."
                          " Narrow the search or select the ads you need.")

_WEEKDAY_MONDAY = 0


# Cross-request memo of view revisions.
#
# _view_revision_uncached(con, ctx) is a pure function of:
#   1. rows of the workspace database: episodes, evidence, reviews,
#      brand_assocs, the four *_history tables, source_versions,
#      transcript_versions, source_states, customer_episode_shows,
#      customer_show_identities, media_artifacts, p01p04_retention,
#      source_content_policy (what an authorizer sees it read), and the
#      schema (a table read under try/except may be absent);
#   2. the AccessContext fields its policy checks and digest read
#      (_ctx_memo_identity), plus the default owner context when None;
#   3. outside the database, only through media.recording_proof: the
#      recording file's location and signature (its bytes are trusted by
#      signature, as media._PROOF_CACHE already does) and the side of a
#      retention deadline the clock is on;
#   4. code and module constants, fixed for the life of a process.
# Its only side effect is adding to ctx.disclosures (and ctx.recordings),
# which admission and release re-check, so a reuse replays them exactly.
#
# The memo key is (database path, db.view_state_key, context identity).
# Other computations with the same kinds of input share this memo through
# view_state_memo, under (tag, context identity) in place of the identity.
# view_state_key is read in the same snapshot as the computation: a token
# that every row write to any ordinary table replaces through SQLite
# triggers (any writer: application, CLI, another process; see
# db.ensure_view_state_guards for why no hand-bumped revision qualifies),
# plus a digest of the whole schema. It is None, and nothing is reused,
# whenever a guard is missing. File and clock inputs are re-checked on
# every reuse (media.proof_inputs_unchanged). Each process keeps its own
# memo; nothing is shared or persisted across processes, and every
# process re-reads the key from the database on every call.
#
# An entry holds a frozen copy of the disclosures (hundreds of KB at
# thousands of episodes), and an entry for a superseded state is never
# reused, so storing one replaces any other state's entry for the same
# database, tag and context: one entry per (database, tag, context). The
# count cap is only a backstop across many contexts and databases. A
# session check's source qualification (_UNCAPPED_TAGS: one small entry
# per database, warmed at start-up) is not counted or dropped by it, so
# many workspaces' research reads never drop a warmed check before its
# first use (DEMO2-R9-03).
_VIEW_MEMO: OrderedDict = OrderedDict()
# READ-CACHE: room for the 8 classes a database keeps (_CLASS_LIMIT), 36
# entries each: a class's warm-up and a judge walk keep 33 after the merge
# with lr-app-speed (PFC-R1-07, DET copy), plus a few to spare.
_VIEW_MEMO_LIMIT = 288
_UNCAPPED_TAGS = frozenset({"source-qualification"})
_VIEW_MEMO_LOCK = threading.Lock()


def _ctx_memo_identity(ctx):
    """Every context field the view computation reads (scopes, content
    policy, credential and share authority via permits/_sparse_view, the
    policy identity in the digest) plus the principal and workspace, so
    one principal's view is never reused for another; None when ctx
    cannot be keyed (then the view is computed every time)."""
    if ctx is None:
        return ("default-owner",)
    from .access import AccessContext
    if type(ctx) is not AccessContext:
        return None
    try:
        return ("ctx", ctx.policy_identity, ctx.policy_revision, ctx.workspace_id,
                ctx.principal_id, ctx.principal_kind, ctx.effective_principal,
                ctx.authority_kind, ctx.share_grant_id, ctx.credential_kind,
                tuple(sorted(ctx.scopes)),
                json.dumps(ctx.content_policy, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError):
        return None


def _view_revision(con: sqlite3.Connection, ctx=None) -> str:
    """The research view revision for ctx, computed once per database
    state, context and outside input state (see _VIEW_MEMO above); equal
    to _view_revision_uncached(con, ctx) in value and side effects."""
    return view_state_memo(con, ctx, None, lambda: _view_revision_uncached(con, ctx))


def view_state_memo(con: sqlite3.Connection, ctx, tag, compute, *, state=None,
                    keep: int | None = None):
    """compute() once per (database, view state, tag, context identity),
    with the _VIEW_MEMO rules above: compute must be a pure function of
    the database's guarded rows and schema, the context fields
    _ctx_memo_identity keys, the recording file/clock inputs it notes
    through media.recording_proof, and code. A reuse replays its effects
    on ctx (disclosures, recordings) and equals a fresh computation; the
    caller must not mutate the returned value. tag None is the view
    revision itself; any other tag names another computation sharing the
    key (DEMO2 pass 9: conversion.source_qualification, filter values).

    state, when given, replaces db.view_state_key as the database part of
    the key: state(con) must change whenever any row or schema input of
    compute changes, and return None when it cannot tell (nothing is then
    reused). conversion.source_qualification keys on the research state
    (READINESS-CACHE).

    keep, when given, makes tag a tuple whose last item varies (a page):
    the entries whose tags differ only there share a budget of keep for
    one database and context, the least recently used dropped first
    (PBA-R2-02)."""
    identity = _ctx_memo_identity(ctx)
    if identity is None:
        return compute()
    if tag is not None:
        identity = (tag, identity)
    state_of = state or _db.view_state_key
    if not con.in_transaction:
        # The key and the computation must read one snapshot.
        with _db.transaction(con, write=False):
            return _memoized(con, ctx, identity, compute, state_of, keep)
    return _memoized(con, ctx, identity, compute, state_of, keep)


# READ-CACHE (2026-10-25): the heavy aggregate reads (Overview, Search,
# saved items, Brands, brand detail, summary, Shows, the Alerts list and
# the ad index) are remembered per disclosure class, not per principal.
# The class is every context field that can change what a caller sees
# (workspace, role, admitted role, scopes, content policy, credential,
# share authority, policy identity and revision) plus the workspace plan
# (customer.excerpt_plan). A miss computes under a class context whose
# principal is "class:<digest>", so the result cannot depend on who
# asked; disclosures and recordings are replayed on the real context.
# The field audit (launch-readiness ws-app/read-cache-design.md) found no
# per-user field in these results except the saved-items revision and
# cursor, which customer_saved computes per call after the cache.
CLASS_PREFIX = "class:"
_CLASS_LIMIT = 8            # classes kept per database, least recently used dropped
# SPEED-FIX1 (SPC-R1-02), SPEED-FIX2 (SPC-C-01), SPEED-FIX3 (SC2-01),
# SPEED-FIX4 (SF3-03): the most entries one class keeps, every budget
# full: Search 20 + the warm-up's searches 24 (customer.SEARCH_WARM_KEEP)
# + research queries 16 + the warmed ones 8 + brand details, weekly
# summaries and brand identities 32 each + the warmed Brand pages' own
# brand details and weekly summaries 12 each (customer.BRAND_WARM_KEEP)
# + 11 single reads (with lr-app-nogap-perf merged) = 199. The warm
# budgets count full: tracked-brand changes fill them past one fill's
# requests. Measured on a DET copy with real Brand pages
# (launch-readiness logs/speed-r1/fix3/brand-walk-head-40.jsonl): the
# class's brand-detail and summary budgets full (32 each) after 40 other
# Brand pages, 0 warmed entries dropped. The global cap is _CLASS_LIMIT
# such classes, so the class limit, not the global cap, drops a class's
# entries first.
# RS1-OVERVIEW: + campaigns.OUTCOMES_KEEP (16) Overview campaign outcomes.
CLASS_ENTRIES = 199 + 16
_VIEW_MEMO_LIMIT = max(_VIEW_MEMO_LIMIT, _CLASS_LIMIT * CLASS_ENTRIES)
_SINGLE_FLIGHT_WAIT_S = 120.0
_INFLIGHT: dict = {}        # key -> _Flight of the one computing call
# (database, class digest) -> (class fields, last use time), most
# recently used last: the classes a live session (not warm-up) read a
# remembered route of in the last _USE_WINDOW_S seconds. Warm-up refills
# only these (asgi.ReadWarmer; RC-R1-01), at most _CLASS_LIMIT per
# database. A refill does not end the use (RC-R2-02): a second change
# with no read between still refills the class.
_CLASS_USED: OrderedDict = OrderedDict()
_USE_WINDOW_S = 30 * 60.0
_clock = time.monotonic
# database -> class digest of the last live session that passed a write
# gate: warm-up refills that class first (RC-SCR-R1-01).
_WRITER: dict = {}
_MISS = object()


class _Flight(threading.Event):
    """The one computation of a key; error is what it raised, if anything:
    its waiters raise that error too instead of computing again (RC-R1-02)."""
    error = None


def disclosure_class(con: sqlite3.Connection, ctx):
    """(digest, fields) of ctx's disclosure class, or None when ctx cannot
    be keyed (then nothing is reused)."""
    if ctx is None or _ctx_memo_identity(ctx) is None:
        return None
    try:
        fields = {"workspace_id": ctx.workspace_id, "principal_kind": ctx.principal_kind,
                  "admitted_role": ctx.admitted_role, "scopes": sorted(ctx.scopes),
                  "content_policy": ctx.content_policy, "credential_kind": ctx.credential_kind,
                  "authority_kind": ctx.authority_kind, "share_grant_id": ctx.share_grant_id,
                  "policy_identity": ctx.policy_identity, "policy_revision": ctx.policy_revision,
                  "plan": _customer.excerpt_plan(con)}
        raw = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()[:32], fields


def class_context(digest: str, fields: dict):
    """A fresh principal-free context of one disclosure class."""
    from .access import AccessContext
    ctx = AccessContext.create(
        principal_id=CLASS_PREFIX + digest, principal_kind=fields["principal_kind"],
        workspace_id=fields["workspace_id"], scopes=fields["scopes"],
        policy_revision=fields["policy_revision"], authority_kind=fields["authority_kind"],
        share_grant_id=fields["share_grant_id"], credential_kind=fields["credential_kind"],
        policy_identity=fields["policy_identity"], content_policy=fields["content_policy"],
        request_id="class-" + digest[:8])
    ctx.admitted_role = fields["admitted_role"]
    return ctx


def class_memo(con: sqlite3.Connection, ctx, tag, compute, *, state=None,
               keep: int | None = None, default: bool = False, warm_keep: int | None = None):
    """compute(class context) once per (database, state, tag, disclosure
    class of ctx), under the view_state_memo rules, with single-flight:
    concurrent misses on one key wait for the one computation. compute
    must read nothing of the context but the class fields (READ-CACHE
    above); its disclosures and recordings are replayed on ctx. ctx None
    computes compute(None) afresh, or (default) keeps the default-owner
    memo of view_state_memo. A context that cannot be keyed computes
    compute(ctx) afresh. The caller must not mutate the returned value.

    warm_keep (with keep): the requests the read warm-up made have a
    budget of their own, warm_keep per class (_warm_tag), so a class's
    other requests never drop them (SPEED-FIX2)."""
    if ctx is None:
        if default:
            return view_state_memo(con, None, tag, lambda: compute(None), state=state, keep=keep)
        return compute(None)
    if not con.in_transaction:
        # The plan, the key and the computation read one snapshot.
        with _db.transaction(con, write=False):
            return class_memo(con, ctx, tag, compute, state=state, keep=keep, default=default,
                              warm_keep=warm_keep)
    found = disclosure_class(con, ctx)
    if found is None:
        return compute(ctx)
    digest, fields = found
    live = not str(ctx.principal_id or "").startswith(CLASS_PREFIX)
    plain = (tag, keep)
    if keep is not None and warm_keep is not None:
        tag, keep = _warm_tag(con, tag, digest, live, warm_keep, keep)

    def run():
        member = class_context(digest, fields)
        value = compute(member)
        ctx.disclosures.update(member.disclosures)
        ctx.recordings.update(member.recordings)
        return value
    return _memoized(con, ctx, (tag, ("class", digest)), run, state or _db.view_state_key,
                     keep, single_flight=True, klass=(digest, fields, live, plain))


# SPEED-FIX2 (code review SPC-C-01, security review SPS-C-01), SPEED-FIX3
# (SC2-01, SC2-04), SPEED-FIX4 (SF3-01): the requests the read warm-up
# made per (database, class), then per (group, pass): Overview's search
# and each warmed Brand page's newest and oldest lists ("search"), its
# brand ("brand-detail") and its weekly summary ("summary"). The
# five-task pass ("tasks", WARM_TASKS set) and the first-pages fills
# ("pages") keep lists of their own, each at most half the group's warm
# budget, most recently made last: tracked-brand changes, which refill
# the first pages only, never trim the five-task requests. Only the
# warm-up (a class principal, never a live session) adds to a list, so no
# request can flood it; at most _CLASS_LIMIT classes per database, the
# least recently warmed dropped with its warm entries.
_WARM_TAGS: OrderedDict = OrderedDict()
_WARM_PASSES = ("tasks", "pages")
WARM_TASKS: contextvars.ContextVar = contextvars.ContextVar("ew_warm_tasks", default=False)


def _warm_tag(con: sqlite3.Connection, tag, digest: str, live: bool, warm_keep: int,
              keep: int):
    """(tag, keep) of one keep-bounded request of class digest: a request
    the warm-up made for the class goes to its group's warm twin ("search"
    -> "search-warm") with a budget of warm_keep, for the warm-up and
    every later caller alike; any other stays in its group with keep. A
    request trimmed off the class's lists takes its warm entry back to its
    group (_unwarm)."""
    database = next((row[2] for row in con.execute("PRAGMA database_list")
                     if row[1] == "main"), "")
    name = (database, digest)
    with _VIEW_MEMO_LOCK:
        lists = _WARM_TAGS.get(name, {})
        if live:
            if not any(tag in lists.get((tag[0], p), ()) for p in _WARM_PASSES):
                return tag, keep
        else:
            lists = _WARM_TAGS.setdefault(name, {})
            _WARM_TAGS.move_to_end(name)
            made = lists.setdefault((tag[0], _WARM_PASSES[0] if WARM_TASKS.get()
                                     else _WARM_PASSES[1]), OrderedDict())
            made[tag] = None
            made.move_to_end(tag)
            while len(made) > warm_keep // len(_WARM_PASSES):
                old = made.popitem(last=False)[0]
                if not any(old in lists.get((tag[0], p), ()) for p in _WARM_PASSES):
                    _unwarm(database, digest, old, keep)
            for gone in [n for n in _WARM_TAGS if n[0] == database][:-_CLASS_LIMIT]:
                del _WARM_TAGS[gone]
                for key in [k for k in _VIEW_MEMO if k[0] == database
                            and _class_of(k[2]) == gone[1] and isinstance(k[2][0], tuple)
                            and str(k[2][0][0]).endswith("-warm")]:
                    del _VIEW_MEMO[key]
    return (tag[0] + "-warm",) + tuple(tag[1:]), warm_keep


def _unlisted(database: str, identity, keep, klass):
    """SPEED-FIX5 (SF4-03): (identity, keep) to store a computed value
    under: a warm request trimmed off its class's lists while it was
    computed goes to its ordinary group (klass[3]: its tag and keep), where
    the next live read (SPEED-FIX6: and its flight's waiters) looks. Holds
    _VIEW_MEMO_LOCK."""
    if klass is None or not str(identity[0][0]).endswith("-warm"):
        return identity, keep
    tag, plain_keep = klass[3]
    lists = _WARM_TAGS.get((database, klass[0]), {})
    if any(tag in lists.get((tag[0], p), ()) for p in _WARM_PASSES):
        return identity, keep
    return (tag, identity[1]), plain_keep


def _unwarm(database: str, digest: str, tag, keep: int) -> None:
    """SPEED-FIX4 (SF3-01): tag is off the class's warm lists, so its live
    reads look in its own group again: the class's warm entry of it moves
    there and the group keeps keep entries. SPEED-FIX5: an entry of the
    group at the warm entry's state stays (the warm one is dropped); one
    at an older state is dropped (SF4-01). The moved entry goes in as the
    group's oldest, dropped first, so a burst of trims never drops a
    member's recent reads (SF4-02). Holds _VIEW_MEMO_LOCK."""
    who = ("class", digest)
    twin = ((tag[0] + "-warm",) + tuple(tag[1:]), who)
    for key in [k for k in _VIEW_MEMO if k[0] == database and k[2] == twin]:
        entry = _VIEW_MEMO.pop(key)
        plain = [k for k in _VIEW_MEMO if k[0] == database and k[2] == (tag, who)]
        if any(k[1] == key[1] for k in plain):
            continue
        for stale in plain:
            del _VIEW_MEMO[stale]
        moved = (database, key[1], (tag, who))
        _VIEW_MEMO[moved] = entry
        _VIEW_MEMO.move_to_end(moved, last=False)
        siblings = [k for k in _VIEW_MEMO if k[0] == database and isinstance(k[2][0], tuple)
                    and k[2][0][:-1] == tag[:-1] and k[2][1] == who]
        for stale in siblings[:-keep]:  # oldest use first
            del _VIEW_MEMO[stale]


# READ-CACHE-2 (2026-10-27): a saved-item write (Track, untrack, a saved
# item or its note, a personal note) moved the one view-state token, so
# every research read and every tracked brand's identity was computed
# again for every class in use (RC-R2-01: 4.5-7.6 s Overview after a
# Track, 2-3x with an owner or viewer tab open). The research reads key on
# research_view_state instead: the research data token (db.RESEARCH_TABLES)
# plus every row of every other guarded table, except the tables below,
# which no research read reads (pinned per read, role and plan by
# tests.test_read_cache2). A table added later is in the key by default.
RESEARCH_VIEW_SKIPPED = frozenset({
    # Saved items, tracked brands, bookmarks and personal notes: the writes
    # this key exists to leave out.
    "customer_saved_items", "customer_mistake_reports",
    # meta's guarded rows change with no research input; a saved write
    # bumps its unguarded aggregate_rev.
    "meta",
    # Bulk, and read by no research read.
    "source_files",
    # Every stored export's whole file (CSV, HTML, JSON): hashing it cost
    # every lookup ~70 ms at 50 x 300 KB, without limit (RC2-SEC-R1-02).
    "exports",
    # Frozen campaign reports (report JSON); read only by the report routes.
    "campaign_reports",
})

# Guarded tables keyed on (row count, last rowid) instead of every row
# (READ-CACHE-2-FIX2, lead U3-1). coverage_attestations: the episode page's
# screened state reads it since SCREENED; hashing its rows cost every
# lookup ~11-16 ms at 2,838 rows and grew with history. It is append-only
# (coverage.record_attestation INSERTs; id AUTOINCREMENT is never reused),
# so an insert moves the last rowid and a delete the count. The only other
# write, the account-removal SQL, rewrites actor, which no read reads
# (both pinned by tests.test_read_cache2).
RESEARCH_VIEW_APPEND_ONLY = frozenset({"coverage_attestations"})

# Tables keyed on what SQLite holds outside a large BLOB (ART-KEY, gate
# 2.6, lead ruling 2026-11-01): COUNT(*) and MAX(rowid), every named index
# read as a covering index, and the columns before the BLOB plus its
# length. A column stored after a BLOB sits behind that row's overflow
# chain, so reading any of them (ART-PERF read them all) read every cover:
# 0.104 s with 796 covers on the host. show_artwork: research reads read
# show_identity_id, source_version_id (before image_bytes),
# length(image_bytes), status (idx_show_artwork_status) and mime
# (customer.artwork_urls). Every app and loader write that changes one of
# them moves one of these parts (pinned by tests.test_art_key); mime
# changes only with the bytes, and a validated image's mime is always on
# the allowlist the read checks.
# DOCUMENTED LIMIT: a raw-SQL UPDATE that changes only mime, or another
# column after image_bytes, leaves the key stale until the next art write.
# No app or loader path writes that; ops must not edit show_artwork by
# hand outside the loader.
# table -> (BLOB column, indexes read whole). A table missing a named
# index or the column falls back to the full-row read below.
RESEARCH_VIEW_BEFORE_BLOB = {
    "show_artwork": ("image_bytes",
                     ("sqlite_autoindex_show_artwork_1", "idx_show_artwork_status")),
}


def _before_blob_rows(con: sqlite3.Connection, table: str) -> list | None:
    """RESEARCH_VIEW_BEFORE_BLOB's parts of table as repr strings, or None
    when its column or an index is missing."""
    def q(name):
        return '"' + name.replace('"', '""') + '"'
    blob, indexes = RESEARCH_VIEW_BEFORE_BLOB[table]
    names = [r[1] for r in con.execute(f"PRAGMA table_info({q(table)})")]
    if blob not in names:
        return None
    out = [repr(tuple(con.execute(
        f"SELECT (SELECT COUNT(*) FROM {q(table)}), (SELECT MAX(rowid) FROM {q(table)})"
    ).fetchone()))]
    try:
        for index in indexes:
            keyed = [r[2] for r in con.execute(f"PRAGMA index_info({q(index)})")]
            if not keyed or None in keyed:
                return None
            order = ", ".join(q(c) for c in keyed)
            out.append("index " + index)
            out.extend(repr(tuple(row)) for row in con.execute(
                f"SELECT {order}, rowid FROM {q(table)} INDEXED BY {q(index)}"
                f" ORDER BY {order}, rowid"))
        before = [q(c) for c in names[:names.index(blob)]]
        out.append("rows")
        out.extend(repr(tuple(row)) for row in con.execute(
            f"SELECT rowid, {', '.join(before + [f'length({q(blob)})'])}"
            f" FROM {q(table)} ORDER BY rowid"))
    except sqlite3.OperationalError:   # no such index, or WITHOUT ROWID
        return None
    return out


def research_view_state(con: sqlite3.Connection) -> str | None:
    """The database part of a research read's memo key, read in the
    caller's snapshot: the research data token, the schema, and every row
    of every guarded table outside db.RESEARCH_TABLES and
    RESEARCH_VIEW_SKIPPED (RESEARCH_VIEW_APPEND_ONLY tables: their row
    count and last rowid; RESEARCH_VIEW_BEFORE_BLOB tables: what SQLite
    holds outside the BLOB, see its limit). A saved-item write leaves it unchanged; any
    other guarded write moves it. None (nothing reused) when a view-state
    guard is missing; the whole view state when the research triggers are
    missing."""
    return _research_digest(con, RESEARCH_VIEW_SKIPPED, b"research-view", "research:")


# RS1-OVERVIEW: the campaign and placement tables, named. A campaign write
# (Confirm, Reject or undo, Add or remove a placement or its evidence, a
# brief or code edit, a frozen report, a target) writes only these. The
# reads keyed on campaign_free_state never read them: the show list's
# screened set and the shared ad rows read none; one campaign's Overview
# outcomes read only that campaign's rows, which its own key digests
# (campaigns._campaign_rows_digest). Pinned by tests.test_rs1_overview.
CAMPAIGN_TABLES = frozenset({
    "campaigns", "campaign_checks", "campaign_evidence", "campaign_history",
    "campaign_reports", "campaign_targets", "expected_placements",
    "model_brief_checks", "placement_episode_reviews", "placement_evaluations",
    "placement_evidence", "provided_brief_versions", "provided_briefs",
})


def campaign_free_state(con: sqlite3.Connection):
    """research_view_state without CAMPAIGN_TABLES, plus the transcript
    index state (the screened set and the outcomes read the index's
    version table, which no guard covers): every other guarded write still
    moves it. None (nothing reused) when a view-state guard is missing; the
    whole view state when the research triggers are missing."""
    narrow = _research_digest(con, RESEARCH_VIEW_SKIPPED | CAMPAIGN_TABLES,
                              b"research-view-no-campaigns", "research-nc:")
    if narrow is None:
        return None
    return (narrow, _transcript_index_state(con))


def _research_digest(con: sqlite3.Connection, skipped, label: bytes, prefix: str) -> str | None:
    """research_view_state's digest, leaving out the guarded tables in
    skipped (with the research tables, which the token covers)."""
    import hashlib
    view = _db.view_state_key(con)
    if view is None:
        return None
    token = _db.research_state_token(con)
    if token is None:
        return "view:" + view
    rows = _db._schema_rows(con)
    digest = hashlib.sha256(label + b"\0" + token.encode())
    for entry in rows:
        digest.update(b"\0" + repr(entry).encode())
    research = set(_db.RESEARCH_TABLES)
    for table in _db._view_state_guarded_tables(rows):
        if table in research or table in skipped:
            continue
        digest.update(b"\1" + table.encode())
        quoted = '"' + table.replace('"', '""') + '"'
        if table in RESEARCH_VIEW_APPEND_ONLY:
            # Two subqueries: one statement with both aggregates is a full
            # scan; apart, each takes SQLite's shortcut (RC2F2-R1-03).
            digest.update(b"\0" + repr(tuple(con.execute(
                f"SELECT (SELECT COUNT(*) FROM {quoted}), (SELECT MAX(rowid) FROM {quoted})"
            ).fetchone())).encode())
            continue
        columns = "*"
        if table in RESEARCH_VIEW_BEFORE_BLOB:
            summary = _before_blob_rows(con, table)
            if summary is not None:
                for row in summary:
                    digest.update(b"\0" + row.encode())
                continue
            # Fallback: every row, the BLOB by its length.
            blob = RESEARCH_VIEW_BEFORE_BLOB[table][0]
            columns = ", ".join(
                ("length({})" if r[1] == blob else "{}").format(
                    '"' + r[1].replace('"', '""') + '"')
                for r in con.execute(f"PRAGMA table_info({quoted})"))
        try:
            found = [repr(tuple(row)) for row in con.execute(
                f"SELECT {columns} FROM {quoted} ORDER BY rowid")]
        except sqlite3.OperationalError:   # WITHOUT ROWID
            found = sorted(repr(tuple(row)) for row in con.execute(
                f"SELECT {columns} FROM {quoted}"))
        for row in found:
            digest.update(b"\0" + row.encode())
    return prefix + digest.hexdigest()


def classes_in_use(database: str) -> list:
    """(digest, fields) of every class a live session read a remembered
    route of in database in the last _USE_WINDOW_S seconds: the last
    writer's class first, then the others, most recently used first;
    older marks are dropped."""
    since = _clock() - _USE_WINDOW_S
    with _VIEW_MEMO_LOCK:
        for name in [name for name, (_f, at) in _CLASS_USED.items() if at < since]:
            del _CLASS_USED[name]
        writer = _WRITER.get(database)
        found = [(digest, dict(fields))
                 for (db_path, digest), (fields, _at) in reversed(_CLASS_USED.items())
                 if db_path == database]
    return sorted(found, key=lambda item: item[0] != writer)


# The writer notes of the services writer call running in this context
# (writer_scope), applied when it returns; None outside one.
_PENDING_WRITER: contextvars.ContextVar = contextvars.ContextVar("ew_pending_writer",
                                                                 default=None)


def note_writer(con: sqlite3.Connection, ctx) -> None:
    """A live session of ctx's class passed a write gate: its class is in
    use and refills first after the change (RC-SCR-R1-01), so the
    writer's own reload does not wait behind other classes' refills.
    Inside a writer_scope the note waits for the write: it is applied
    only if the call returns and changed rows on con (RC-R3-02)."""
    if ctx is None or str(ctx.principal_id or "").startswith(CLASS_PREFIX):
        return
    found = disclosure_class(con, ctx)
    if found is None:
        return
    database = next((row[2] for row in con.execute("PRAGMA database_list")
                     if row[1] == "main"), "")
    pending = _PENDING_WRITER.get()
    if pending is not None:
        pending.append((con, con.total_changes, database, found))
        return
    _mark_writer(database, found)


def _mark_writer(database: str, found) -> None:
    _note_use(database, *found)
    with _VIEW_MEMO_LOCK:
        _WRITER[database] = found[0]


@contextlib.contextmanager
def writer_scope():
    """Around one services writer call: the writer notes its gate takes
    (note_writer) are applied when the call returns, each only if its
    connection changed rows since the gate. A write refused after the gate
    (bad input, a revision conflict) raises, and a read-only preflight
    changes nothing: neither marks its class (RC-R3-02)."""
    pending: list = []
    token = _PENDING_WRITER.set(pending)
    try:
        yield
    finally:
        _PENDING_WRITER.reset(token)
    for con, before, database, found in pending:
        try:
            changed = con.total_changes != before
        except sqlite3.ProgrammingError:   # a closed read-only snapshot
            changed = False
        if changed:
            _mark_writer(database, found)


def _note_use(database: str, digest: str, fields: dict) -> None:
    with _VIEW_MEMO_LOCK:
        _CLASS_USED[(database, digest)] = (fields, _clock())
        _CLASS_USED.move_to_end((database, digest))
        mine = [name for name in _CLASS_USED if name[0] == database]
        for name in mine[:-_CLASS_LIMIT]:
            del _CLASS_USED[name]


def _class_of(identity):
    """The class digest a memo identity belongs to: a class_memo entry, or
    an entry a class computation keyed on its class context; else None."""
    who = identity[1] if len(identity) == 2 and isinstance(identity[1], tuple) else identity
    if not who:
        return None
    if who[0] == "class":
        return who[1]
    if who[0] == "ctx" and len(who) > 4 and isinstance(who[4], str) \
            and who[4].startswith(CLASS_PREFIX):
        return who[4][len(CLASS_PREFIX):]
    return None


def _bound_classes(database: str) -> None:
    """At most _CLASS_LIMIT classes per database: the least recently used
    class loses all of its entries together. Holds _VIEW_MEMO_LOCK."""
    order: OrderedDict = OrderedDict()
    for key in _VIEW_MEMO:   # least recently used first
        if key[0] == database:
            digest = _class_of(key[2])
            if digest is not None:
                order[digest] = None
                order.move_to_end(digest)
    dropped = set(list(order)[:-_CLASS_LIMIT])
    if dropped:
        for key in [k for k in _VIEW_MEMO if k[0] == database and _class_of(k[2]) in dropped]:
            del _VIEW_MEMO[key]


def _memo_hit(key, ctx):
    with _VIEW_MEMO_LOCK:
        entry = _VIEW_MEMO.get(key)
        if entry is not None:
            _VIEW_MEMO.move_to_end(key)
    if entry is not None and _media.proof_inputs_unchanged(entry[3]):
        value, disclosures, recordings, noted = entry
        _pass_up(noted)
        if ctx is not None:
            ctx.disclosures.update(disclosures)
            ctx.recordings.update(recordings)
        return value
    return _MISS


def _pass_up(noted) -> None:
    """SPEED-FIX1 (SPC-R1-01): a computation that uses a memo value, reused
    or fresh, rests on that value's file and clock inputs too: they go to
    the enclosing memo's list (media._PROOF_INPUTS), so its entry is
    re-checked on them (the research query's revision is a nested memo)."""
    outer = _media._PROOF_INPUTS.get()
    if outer is not None and noted:
        outer.extend(noted)


# SPEED-FIX3 (SC2-05): a memo entry holds its disclosure set, about 2,700
# tuples (about 0.5 MB) on DET whatever its value, so it was about half of
# every entry. Equal sets are held once (by hash, weakly: a set no entry
# holds goes with its last entry), and each disclosure tuple once across
# sets. The tuples held are bounded by the distinct disclosures read, at
# most _HELD_ITEMS_LIMIT before the table starts over.
_HELD_SETS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_HELD_ITEMS: dict = {}
_HELD_ITEMS_LIMIT = 200000


def _held_disclosures(disclosures) -> frozenset:
    """The one held frozenset equal to disclosures. Holds _VIEW_MEMO_LOCK."""
    frozen = frozenset(disclosures)
    held = _HELD_SETS.get(hash(frozen))
    if held is not None and held == frozen:
        return held
    if len(_HELD_ITEMS) > _HELD_ITEMS_LIMIT:
        _HELD_ITEMS.clear()
    held = frozenset(_HELD_ITEMS.setdefault(item, item) for item in frozen)
    _HELD_SETS[hash(held)] = held
    return held


# The state function of the _memoized call whose _fill runs (_fill_with).
_STATE_OF: contextvars.ContextVar = contextvars.ContextVar("ew_memo_state_of", default=None)


def _fill_with(state_of, *args):
    """_fill, with state_of (its key's state function) for the
    newer-entry check."""
    token = _STATE_OF.set(state_of)
    try:
        return _fill(*args)
    finally:
        _STATE_OF.reset(token)


def _is_current(state, now) -> bool:
    """Whether a memo state is the database's current state now: equal, or
    now with a mark on top (a tuple whose first item is now)."""
    return state == now or (isinstance(state, tuple) and len(state) == 2 and state[0] == now)


def _current_state(database: str, state_of):
    """state_of read on a fresh read-only snapshot of database (not the
    caller's), or None when it cannot be read."""
    if not database:
        return None
    try:
        other = _db.open_existing(database, readonly=True)
    except Exception:  # noqa: BLE001 - cannot tell: the caller keeps the old rule
        return None
    try:
        with _db.transaction(other, write=False):
            return state_of(other)
    except Exception:  # noqa: BLE001
        return None
    finally:
        other.close()


def _memoized(con: sqlite3.Connection, ctx, identity, compute, state_of=None, keep=None,
              *, single_flight=False, klass=None):
    state = (state_of or _db.view_state_key)(con)
    if state is None:
        return compute()
    database = next((row[2] for row in con.execute("PRAGMA database_list")
                     if row[1] == "main"), "")
    if _customer.model_names_on() and _model_rows_present(con, database, state):
        # PODS: reads named by Muse Spark are a different view of the
        # same data; the two never share an entry.
        state = (state, "model-names")
    key = (database, state, identity)
    if klass is not None and klass[2]:
        _note_use(database, klass[0], klass[1])
    value = _memo_hit(key, ctx)
    if value is not _MISS:
        return value
    if not single_flight:
        return _fill_with(state_of, con, ctx, database, state, key, identity, compute, keep,
                          klass)
    with _VIEW_MEMO_LOCK:
        flight = _INFLIGHT.get(key)
        mine = flight is None
        if mine:
            flight = _INFLIGHT[key] = _Flight()
    if not mine:
        flight.wait(_SINGLE_FLIGHT_WAIT_S)
        value = _memo_hit(key, ctx)
        if value is _MISS:
            # SPEED-FIX6 (SF5-02): the flight's request was trimmed off its
            # class's lists while it ran, so it stored under its ordinary
            # key (_unlisted): look there too before computing again.
            with _VIEW_MEMO_LOCK:
                moved = _unlisted(database, identity, keep, klass)[0]
            if moved != identity:
                value = _memo_hit((database, state, moved), ctx)
        if value is not _MISS:
            return value
        if flight.error is not None:
            # The one computation failed: its waiters fail with it; nothing
            # was remembered, so the next read computes afresh.
            raise flight.error
        # Not kept (volatile inputs) or still running after the wait: compute.
        return _fill_with(state_of, con, ctx, database, state, key, identity, compute, keep,
                          klass)
    try:
        return _fill_with(state_of, con, ctx, database, state, key, identity, compute, keep,
                          klass)
    except Exception as exc:
        flight.error = exc
        raise
    finally:
        with _VIEW_MEMO_LOCK:
            _INFLIGHT.pop(key, None)
        flight.set()


_MODEL_ROWS: OrderedDict = OrderedDict()  # (database, state) -> any model row (PODS)


def _model_rows_present(con: sqlite3.Connection, database: str, state) -> bool:
    """Whether the data state holds a model-named brand row
    (customer.MODEL_BASIS), read once per (database, state): without one
    the model-names view is the plain view and shares its entries."""
    key = (database, state)
    with _VIEW_MEMO_LOCK:
        if key in _MODEL_ROWS:
            return _MODEL_ROWS[key]
    found = con.execute("SELECT 1 FROM brand_assocs WHERE basis=? LIMIT 1",
                        (_customer.MODEL_BASIS,)).fetchone() is not None
    with _VIEW_MEMO_LOCK:
        _MODEL_ROWS[key] = found
        while len(_MODEL_ROWS) > 64:
            _MODEL_ROWS.popitem(last=False)
    return found


def _plain_state(state):
    """A memo state without the PODS model-names mark (_memoized): an entry
    with and one without it are of the same data state, so neither makes
    the other stale."""
    if isinstance(state, tuple) and len(state) == 2 and state[1] == "model-names":
        return state[0]
    return state


def _fill(con, ctx, database, state, key, identity, compute, keep, klass):
    noted: list = []
    disclosures, recordings = set(), {}
    noting = _media._PROOF_INPUTS.set(noted)
    if ctx is not None:
        kept_disclosures, kept_recordings = ctx.disclosures, ctx.recordings
        ctx.disclosures, ctx.recordings = set(), {}
    try:
        value = compute()
    finally:
        _media._PROOF_INPUTS.reset(noting)
        _pass_up(noted)
        if ctx is not None:
            disclosures, recordings = ctx.disclosures, ctx.recordings
            kept_disclosures.update(disclosures)
            kept_recordings.update(recordings)
            ctx.disclosures, ctx.recordings = kept_disclosures, kept_recordings
    shared = _SHARED_SCANS.get()
    if shared is not None and shared[0] is con and shared[2]:
        # Inside a shared_scans pass the owner-view matches may come from
        # the pass memo, whose file/clock inputs were not noted here.
        return value
    if any(entry[0] == "volatile" for entry in noted):
        return value  # rests on a transient failure the next call retries
    with _VIEW_MEMO_LOCK:
        identity, keep = _unlisted(database, identity, keep, klass)
        newer = any(k[0] == database and k[2] == identity and k[1] != state for k in _VIEW_MEMO)
    if newer:
        # SPC-R1-05: another state's entry is kept. Replace it only when
        # this value's state is the database's state now: a fill that read
        # an older snapshot (a long warm-up) and finishes after a request
        # stored the newer entry keeps that entry and stores nothing.
        now = _current_state(database, _STATE_OF.get() or _db.view_state_key)
        if now is not None and not _is_current(state, now):
            return value
    noted = list(dict.fromkeys(noted))   # nested uses repeat their inputs
    with _VIEW_MEMO_LOCK:
        identity, keep = _unlisted(database, identity, keep, klass)
        key = (database, state, identity)
        for stale in [k for k in _VIEW_MEMO
                      if k[0] == database and k[2] == identity
                      and _plain_state(k[1]) != _plain_state(state)]:
            del _VIEW_MEMO[stale]
        _VIEW_MEMO[key] = (value, _held_disclosures(disclosures), dict(recordings), tuple(noted))
        _VIEW_MEMO.move_to_end(key)
        if keep is not None:
            group, who = identity[0][:-1], identity[1]
            siblings = [k for k in _VIEW_MEMO if k[0] == database and isinstance(k[2][0], tuple)
                        and k[2][0][:-1] == group and k[2][1] == who]
            for stale in siblings[:-keep]:  # oldest use first
                del _VIEW_MEMO[stale]
        if klass is not None or _class_of(identity) is not None:
            _bound_classes(database)
        capped = [k for k in _VIEW_MEMO if _capped(k)]
        for stale in capped[:max(0, len(capped) - _VIEW_MEMO_LIMIT)]:
            del _VIEW_MEMO[stale]   # least recently used first
    return value


def _capped(key) -> bool:
    """Whether a memo entry counts against _VIEW_MEMO_LIMIT."""
    identity = key[2]
    return not (len(identity) == 2 and isinstance(identity[0], str)
                and identity[0] in _UNCAPPED_TAGS)


def _schema_digest(con: sqlite3.Connection) -> str:
    import hashlib
    digest = hashlib.sha256()
    for entry in _db._schema_rows(con):
        digest.update(b"\0" + repr(entry).encode())
    return digest.hexdigest()


def _recording_states(con: sqlite3.Connection, ctx=None) -> list:
    """Playback proof outcome of every episode with a registered recording:
    the one research input outside the database (file bytes and the
    retention clock, noted for the memo by media.recording_proof). An
    episode whose audio ctx is refused is `refused`, with no proof or file
    work: its rows show no recording (CLIPCORE-R3-01)."""
    out, memo = [], {}
    for row in con.execute("SELECT * FROM episodes WHERE media_artifact_id IS NOT NULL"
                           " ORDER BY id"):
        ep = dict(row)
        if not _customer._audio_allowed(con, ctx, ep["id"], memo):
            out.append([ep["id"], "refused"])
            continue
        identity = _media.recording_identity(con, ep)
        proof = _media.recording_proof(con, ep, identity)
        out.append([ep["id"], None if proof is None else
                    [proof["verified_duration_s"] is not None, proof["alignment"],
                     proof["source_offset_s"]]])
    return out


def _visibility_identity(ctx) -> dict:
    """Every context field that decides what research results a caller
    sees (AccessContext.permits / authorize_disclosure / the sparse view),
    without the principal itself: two callers who see the same results
    share a revision."""
    return {"scopes": sorted(ctx.scopes), "content_policy": ctx.content_policy,
            "credential_kind": ctx.credential_kind, "authority_kind": ctx.authority_kind,
            "principal_kind": ctx.principal_kind, "share_grant_id": ctx.share_grant_id}


def _view_revision_uncached(con: sqlite3.Connection, ctx=None) -> str:
    # Without a ctx the matches run as _run_match's own default owner, the
    # same context built here, so a shared_scans pass can share them.
    owner = ctx
    if owner is None:
        from .access import AccessContext
        owner = AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                     workspace_id="local", credential_kind="local_operator")
    from . import citations
    # The revision comes from the research data token (db.research_state_token:
    # replaced on every real change of a research input row) and the
    # caller's visibility, not from a rescan of every result.
    token = _db.research_state_token(con)
    if token is not None:
        return citations._state_digest("view", owner.policy_identity, {
            "research_state": token, "visibility": _visibility_identity(owner),
            "schema": _schema_digest(con), "recordings": _recording_states(con, owner)})
    direct = _customer.DIRECT_SCAN.set(True)
    # The revision digests whole items, whoever asks (MEMBERSHIP_ONLY).
    whole = _customer.MEMBERSHIP_ONLY.set(False)
    try:
        return _view_revision_rescan(con, ctx, owner)
    finally:
        _customer.MEMBERSHIP_ONLY.reset(whole)
        _customer.DIRECT_SCAN.reset(direct)


def _view_revision_rescan(con: sqlite3.Connection, ctx, owner) -> str:
    """The view revision as the digest of every result the caller sees
    (workspaces whose research data token is not installed)."""
    from . import citations
    criteria = _v1_criteria(_contracts.normalize_query({"version": 2, "mode": "ads"}))
    # The three scans read the same pinned transcripts: decode each once
    # (once per pass, inside a shared pass).
    decoded = pass_transcripts(con)
    if decoded is None:
        decoded = {}
    # Each scan already encoded its items canonically for its own revision;
    # the view digest hashes those bytes instead of encoding them again.
    encoded: list = []
    ads = _customer._run_match(con, criteria, ctx=ctx, transcripts=decoded, encoded=encoded)["items"]
    criteria["mode"] = "mentions"
    transcripts = _customer._run_match(con, criteria, ctx=ctx, transcripts=decoded,
                                       encoded=encoded)["items"]
    criteria["mode"] = "episodes"
    episodes = _customer._run_match(con, criteria, ctx=ctx, transcripts=decoded,
                                    encoded=encoded)["items"]
    spans = [citations._projection_span("view", "research", canonical)
             if canonical is not None else None for canonical in encoded]
    if len(spans) == 3 and None not in spans:
        try:
            return citations._state_digest_of_lists(
                "view", owner.policy_identity,
                [(canonical, *span) for canonical, span in zip(encoded, spans)])
        except (TypeError, ValueError):
            pass  # an unencodable record_id fails below exactly as before
    return citations._state_digest("view", owner.policy_identity, [ads, transcripts, episodes])


def _v1_criteria(query: dict) -> dict:
    """Derive the V1 text/mode/sort core; set predicates apply after."""
    single_brand = query["brands"][0] if query["brands"] is not None \
        and len(query["brands"]) == 1 else ""
    single_show = query["shows"][0] if query["shows"] is not None \
        and len(query["shows"]) == 1 else ""
    criteria = {"version": 1, "mode": query["mode"], "q": query["q"],
                "brand": single_brand, "show": single_show,
                "from": query["from"] or "", "to": query["to"] or "",
                "status": "", "audio": "", "sort": query["sort"],
                "page": 1, "ids": None}
    # The episode scope (customer._episode_scope) also takes the date
    # partition and a multi-value shows set, so coverage counts the real
    # scope; the set predicates below still filter every item.
    if query.get("date_status", "all") != "all":
        criteria["date_status"] = query["date_status"]
    if query["shows"] is not None and len(query["shows"]) != 1:
        criteria["shows"] = list(query["shows"])
    if query.get("self_promo"):
        criteria["self_promo"] = query["self_promo"]
    return criteria


def _item_brand_keys(item: dict, aliases: dict) -> set[str]:
    """Canonical brand keys of one item's own attribution."""
    if item.get("brand_keys") is not None:
        return set(item["brand_keys"])
    return {_ad.assoc_key(aliases, a) for a in (item.get("attribution") or [])
            if a.get("brand")}


def _episode_brand_keys(con: sqlite3.Connection, ctx=None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    aliases = _ad.alias_map(con)
    query = _contracts.normalize_query({"version": 2, "mode": "ads"})
    for row in _customer._light_match(con, _v1_criteria(query), ctx,
                                      transcripts=pass_transcripts(con))["rows"]:
        out.setdefault(row["episode_id"], set()).update(_item_brand_keys(row, aliases))
    return out


def _episode_categories(con: sqlite3.Connection, ctx=None) -> dict[str, str]:
    return {row["id"]: row.get("category") or ""
            for row in _customer.permitted_episodes(con, ctx)}


def _item_brands(item: dict, brand_map: dict[str, set[str]],
                 aliases: dict | None = None) -> set[str]:
    """An ad counts under its own canonical brand only; a passage or an
    episode under the brands of its episode's ads (gap 9)."""
    keys = _item_brand_keys(item, aliases or {})
    if item.get("record_kind") != "evidence":
        keys |= brand_map.get(item.get("episode_id") or "", set())
    keys.discard("")
    return keys


def _item_show_id(item: dict) -> str | None:
    show = item.get("show")
    if isinstance(show, dict) and show.get("show_identity_id") \
            and not _customer.show_mapping_disputed(show):
        return show["show_identity_id"]
    return None


def _episode_status_sets(con: sqlite3.Connection,
                         episode_ids: set[str]) -> dict[str, set[str]]:
    """Confirmed/finding/unresolved status per episode from evidence rows."""
    out: dict[str, set[str]] = {}
    if not episode_ids:
        return out
    try:
        rows = con.execute("SELECT id, episode_id FROM evidence").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        if row["episode_id"] not in episode_ids:
            continue
        try:
            status, _ = _customer._evidence_status(con, row["id"])
        except sqlite3.Error:
            continue
        if status != "rejected":
            out.setdefault(row["episode_id"], set()).add(status)
    return out


def _apply_set_predicates(con: sqlite3.Connection, query: dict,
                          items: list[dict], ctx=None, *, light: bool = False,
                          transcripts: dict | None = None, episodes=None) -> list[dict]:
    """Set predicates over result items, or (light) over light rows.
    episodes: the items' episode scope (customer._light_match), when the
    caller restricted its match; the per-episode reference reads only it."""
    brand_map: dict[str, set[str]] | None = None
    categories: dict[str, str] | None = None
    status_map: dict[str, set[str]] | None = None
    if query["categories"] is not None:
        categories = _episode_categories(con, ctx)
    aliases = _customer.customer_aliases(con)
    if (query["brands"] is not None and len(query["brands"]) != 1) or \
            (query["statuses"] is not None and query["mode"] == "episodes"):
        reference = _customer._light_match(
            con, _v1_criteria(dict(query, mode="ads", q="")), ctx,
            transcripts=transcripts, episodes=episodes)["rows"] if light else \
            (_customer._run_match(con, _v1_criteria(dict(query, mode="ads", q="")), ctx=ctx)
             if episodes is None else _customer._run_match_scan(
                con, _v1_criteria(dict(query, mode="ads", q="")), ctx=ctx,
                episodes=episodes))["items"]
        brand_map, status_map = {}, {}
        for evidence in reference:
            episode = evidence["episode_id"]
            associations = evidence.get("attribution", [])
            brands = _ad.match_keys(_item_brand_keys(evidence, aliases), _ad.item_heard(evidence))
            brand_map.setdefault(episode, set()).update(brands)
            status = evidence.get("status")
            if query["brands"] is not None and status == "confirmed":
                wanted = _ad.wanted_keys(aliases, query["brands"])
                confirmed = _ad.match_keys({_ad.assoc_key(aliases, a) for a in associations
                                            if a.get("basis") == "confirmed" and a.get("brand")})
                if not confirmed & wanted:
                    status = "finding"
            status_map.setdefault(episode, set()).add(status)
    if query["shows"] is not None and len(query["shows"]) != 1:
        for show_id in query["shows"]:
            _customer.resolve_customer_show(con, show_id)
    out = []
    for item in items:
        if query["mode"] == "ads" and query["brands"] is not None and item.get("status") == "confirmed":
            wanted_brands = _ad.wanted_keys(aliases, query["brands"])
            confirmed = _ad.match_keys({_ad.assoc_key(aliases, a) for a in item.get("attribution", [])
                                        if a.get("basis") == "confirmed" and a.get("brand")})
            if not confirmed & wanted_brands:
                item = dict(item, status="finding")
        if query["brands"] is not None and len(query["brands"]) != 1:
            wanted = _ad.wanted_keys(aliases, query["brands"])
            if query["brands"] == []:
                continue
            if not (_ad.match_keys(_item_brands(item, brand_map or {}, aliases), _ad.item_heard(item))
                    & wanted):
                continue
        if query["shows"] is not None and len(query["shows"]) != 1:
            if query["shows"] == []:
                continue
            if _item_show_id(item) not in set(query["shows"]):
                continue
        if query["categories"] is not None:
            if query["categories"] == []:
                continue
            cat = (categories or {}).get(item.get("episode_id") or "", "")
            if cat not in set(query["categories"]):
                continue
        if query["statuses"] is not None:
            if query["statuses"] == []:
                continue
            wanted = set(query["statuses"])
            if query["mode"] == "episodes":
                got = (status_map or {}).get(item.get("episode_id") or "", set())
                if item.get("matching_passages"):
                    got = set(got) | {"mention"}
            else:
                got = {item.get("status")} if item.get("status") else set()
            if not (got & wanted):
                continue
        if query["audio"] is not None:
            if query["audio"] == []:
                continue
            if item.get("audio") not in set(query["audio"]):
                continue
        day = _contracts.publication_day(item.get("pub_date") or "")
        if query["date_status"] == "known" and day is None:
            continue
        if query["date_status"] == "unknown" and day is not None:
            continue
        if (query["from"] or query["to"]) and day is None:
            continue
        if query["from"] and day is not None and day < query["from"][:10]:
            continue
        if query["to"] and day is not None and day > query["to"][:10]:
            continue
        out.append(item)
    if query["ids"] is not None:
        if query["ids"] == []:
            return []
        out = _customer._apply_light_ids(con, out, query["ids"], transcripts or {}) if light \
            else _customer._apply_ids(con, out, query["ids"])
    if query.get("has_clip"):
        # REDESIGN S9: only the ads with a clip ctx can play (S6's rule: a
        # read of the ad with has_clip), every matching read of them; a
        # read in no ad has none.
        ads = _breaks.scope_index(con, ctx)
        out = [item for item in out if any(
            r.get("has_clip") for r in (ads.get(_breaks.evidence_id(item)) or {}).get("reads", ()))]
    return out


def _week_key(day: str) -> str:
    monday = date.fromisoformat(day) - timedelta(
        days=date.fromisoformat(day).weekday())
    return monday.isoformat()


def _cell_members(con: sqlite3.Connection, items: list[dict], group_by: str,
                  key: str, brand_map: dict[str, set[str]],
                  categories: dict[str, str]) -> list[dict]:
    aliases = _ad.alias_map(con)
    members = []
    for item in items:
        keys: set[str] = set()
        if group_by == "week":
            day = _contracts.publication_day(item.get("pub_date") or "")
            keys.add(_week_key(day) if day else "unknown")
        elif group_by == "category":
            keys.add(categories.get(item.get("episode_id") or "", "")
                     or "unknown")
        elif group_by == "brand":
            keys = _item_brands(item, brand_map, aliases) or {"unknown"}
        elif group_by == "show":
            keys.add(_item_show_id(item) or "unknown")
        elif group_by == "status":
            keys.add(item.get("status") or "unknown")
        if key in keys:
            members.append(item)
    return members


def _summarize(con: sqlite3.Connection, items: list[dict], group_by: str,
               count_units: str, revision: str,
               workspace_context: str, ctx=None,
               breaks: dict | None = None, brand_keys: set | None = None) -> list[dict]:
    """Summary cells over items. breaks (ads mode: the scope's
    ad_breaks index) adds each cell's distinct ad breaks; `count` stays
    the cell's results, the unit its selector is bound to. brand_keys
    (_named_brand_keys: the search words name a listed brand) adds
    brand_breaks, the cell's ads named that brand, brand_break_total's
    rule (REDESIGN S5: the bars sum to the head's count)."""
    # Ads count under their own brand; passages and episodes need the
    # brands of their episode's ads.
    brand_map = _episode_brand_keys(con, ctx) if group_by == "brand" and any(
        i.get("record_kind") != "evidence" for i in items) else {}
    categories = _episode_categories(con, ctx)
    aliases = _customer.customer_aliases(con)
    buckets: dict[str, list[dict]] = {}
    for item in items:
        keys: set[str] = set()
        if group_by == "week":
            day = _contracts.publication_day(item.get("pub_date") or "")
            keys.add(_week_key(day) if day else "unknown")
        elif group_by == "category":
            keys.add(categories.get(item.get("episode_id") or "", "")
                     or "unknown")
        elif group_by == "brand":
            keys = _item_brands(item, brand_map, aliases) or {"unknown"}
        elif group_by == "show":
            keys.add(_item_show_id(item) or "unknown")
        elif group_by == "status":
            keys.add(item.get("status") or "unknown")
        for key in keys:
            buckets.setdefault(key, []).append(item)
    cells = []
    for key in sorted(buckets):
        members = buckets[key]
        count = sum(_customer.light_weight(m) for m in members)
        cell = {"key": key, "label": _cell_label(
            con, group_by, key, members, aliases),
            "count": count, "count_units": count_units,
            "coverage": {"matched_in_scope": count},
            "selector": None}
        if breaks is not None:
            cell["breaks"] = _breaks.count(breaks, (_breaks.evidence_id(m) for m in members))
            if brand_keys is not None:
                named = {brk["id"] for brk in (breaks.get(_breaks.evidence_id(m)) for m in members)
                         if brk is not None and brand_keys & {b["key"] for b in brk["brands"]}}
                cell["brand_breaks"] = len(named)
        if group_by == "week" and key != "unknown":
            end = date.fromisoformat(key) + timedelta(days=6)
            cell["interval"] = {"from": key, "to": end.isoformat()}
        cells.append(cell)
    return cells


def _cell_label(con: sqlite3.Connection, group_by: str, key: str,
                members: list[dict], aliases: dict | None = None) -> str:
    if key == "unknown":
        return "Unknown" if group_by != "week" else "Unknown date"
    if group_by == "brand":
        return _ad.brand_display(con, key, aliases)[0]
    if group_by == "show":
        try:
            row = con.execute(
                "SELECT display_title FROM customer_show_identities"
                " WHERE id=?", (key,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and row["display_title"]:
            return row["display_title"]
        return key
    if group_by == "week":
        end = date.fromisoformat(key) + timedelta(days=6)
        return f"Week of {key} – {end.isoformat()}"
    return key


_SHARED_SCANS: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_shared_scans", default=None)


@contextlib.contextmanager
def shared_scans(con: sqlite3.Connection):
    """Within one pure read pass over con, an identical owner-view Query2
    match scans once. The memo lives only for the with-block and only for
    that connection; callers must not write inside it."""
    token = _SHARED_SCANS.set((con, {}, True))
    try:
        yield
    finally:
        _SHARED_SCANS.reset(token)


@contextlib.contextmanager
def shared_view_revisions(con: sqlite3.Connection):
    """Within one pure read pass over con, the view revision for one
    caller context is computed once (shared_view_revision); matches still
    scan per caller. Same lifetime and no-write rule as shared_scans."""
    token = _SHARED_SCANS.set((con, {}, False))
    try:
        yield
    finally:
        _SHARED_SCANS.reset(token)


def pass_transcripts(con: sqlite3.Connection) -> dict | None:
    """The decoded-transcript memo (by version id) of the shared_scans or
    shared_view_revisions pass over con, or None outside one."""
    shared = _SHARED_SCANS.get()
    if shared is None or shared[0] is not con:
        return None
    return shared[1].setdefault(("transcripts",), {})


def shared_view_revision(con: sqlite3.Connection, ctx=None) -> str:
    """The view revision for ctx, computed once per shared_scans or
    shared_view_revisions pass. A failure is kept and re-raised to every
    caller, exactly as each unshared _view_revision(con, ctx) call would
    have raised it. A ctx is matched by identity (its disclosures are a
    set, so a repeated computation would add nothing)."""
    shared = _SHARED_SCANS.get()
    if shared is None or shared[0] is not con:
        return _view_revision(con, ctx)
    memo = shared[1]
    # query keys are JSON strings; None holds the owner-view revision.
    key = None if ctx is None else ("view_revision", id(ctx))
    entry = memo.get(key)
    if entry is None or entry[2] is not ctx:
        try:
            entry = (True, _view_revision(con, ctx), ctx)
        except Exception as exc:
            entry = (False, exc, ctx)
        memo[key] = entry
    ok, value, _ctx = entry
    if not ok:
        raise value
    return value


def _match_query2(con: sqlite3.Connection, query: dict,
                  repo=None, ctx=None) -> tuple[list[dict], dict]:
    """Run the canonical match, apply set predicates, return (items, info)."""
    shared = _SHARED_SCANS.get()
    if shared is not None and shared[0] is con and shared[2] and repo is None and ctx is None:
        key = json.dumps(query, sort_keys=True)
        if _customer.MEMBERSHIP_ONLY.get():
            key = ("membership_only", key)
        if _customer.model_names_on():
            key = ("model-names", key)  # PODS: never shared with the plain view
        if key not in shared[1]:
            shared[1][key] = _match_query2_scan(con, query)
        items, info = shared[1][key]
        return list(items), dict(info)
    return _match_query2_scan(con, query, repo, ctx=ctx)


def _match_query2_light(con: sqlite3.Connection, query: dict, repo=None, ctx=None
                        ) -> tuple[list[dict], dict]:
    """_match_query2 over light rows (customer._light_match): the same
    matching, set predicates and order, with no publication resolution;
    customer._materialize builds the result items of any slice. Passage
    groups stand for all of their passages (customer.light_weight). info
    adds by_id and the pass's decoded-transcript memo for _materialize."""
    shared = _SHARED_SCANS.get()
    key = None
    if shared is not None and shared[0] is con and shared[2] and repo is None and ctx is None:
        key = ("light", json.dumps(query, sort_keys=True))
        if _customer.model_names_on():
            key = ("model-names",) + key  # PODS: never shared with the plain view
        if key in shared[1]:
            rows, info = shared[1][key]
            return list(rows), dict(info)
    transcripts = pass_transcripts(con)
    if transcripts is None:
        transcripts = {}
    light = _customer._light_match(con, _v1_criteria(query), ctx, transcripts=transcripts)
    rows = _apply_set_predicates(con, query, light["rows"], ctx=ctx, light=True,
                                 transcripts=transcripts)
    rows = _customer._order_light(rows, query["sort"])
    info = {"total_relation": "lower_bound" if light["partial"] else "exact",
            "truncation_reason": light["reason"],
            "episodes_in_scope": light["coverage"]["episodes_in_scope"],
            "sources_included": light["coverage"]["sources_included"],
            "excluded_undated": light["coverage"]["excluded_undated"],
            "by_id": light["by_id"], "transcripts": transcripts}
    if key is not None:
        shared[1][key] = (rows, info)
    return list(rows), dict(info)


def _match_query2_scan(con: sqlite3.Connection, query: dict,
                       repo=None, ctx=None, *, episodes=None) -> tuple[list[dict], dict]:
    """episodes, when given, restricts the match to those episodes and
    builds only the items of query's ids: every item kept is the item of
    the unrestricted match (the ids cut the rest below in any case)."""
    raw = _customer._run_match(con, _v1_criteria(query), repo, ctx=ctx) if episodes is None \
        else _customer._run_match_scan(con, dict(_v1_criteria(query), ids=query["ids"]),
                                       repo, ctx, episodes=episodes)
    items = _apply_set_predicates(con, query, raw["items"], ctx=ctx, episodes=episodes)
    items = _customer._order_items(items, query["sort"])
    info = {"total_relation": raw["total_relation"],
            "truncation_reason": raw["truncation_reason"],
            "episodes_in_scope": raw["coverage"]["episodes_in_scope"],
            "sources_included": raw["coverage"]["sources_included"],
            "excluded_undated": raw["coverage"]["excluded_undated"]}
    return items, info


def tracker_effective(query: dict, live: dict | None
                      ) -> tuple[dict, dict, str]:
    """Narrow a tracker query by its live-definition selector, if any.

    Returns (effective_query, clause, count_units). The selector base
    narrows, never widens; a mode conflict is rejected. A null selector
    matches the whole query with the all-results clause.
    """
    query = _contracts.normalize_query(query)
    if live is None:
        return query, {"kind": "all_results"}, "distinct_results"
    live = _contracts.validate_selector(
        live, current_revision=None, workspace_context=None)
    if live["scope_mode"] != "live_definition":
        raise _contracts.QueryRejected(
            "trackers keep a live_definition selector")
    effective = _contracts.narrow_for_request(live["base_query"], query)
    return effective, live["clause"], live["count_units"]


def match_effective(con: sqlite3.Connection, effective: dict, clause: dict,
                    count_units: str, repo=None, ctx=None,
                    *, with_revision: bool = True, light: bool = False) -> tuple[list[dict], dict]:
    """Run one effective tracker scope: all matches plus revision/coverage.

    An explicit exact-empty id set short-circuits before scanning: []
    is an intentional zero-record scope, never all matches. Carries the
    completeness signals trackers use to refuse partial baselines
    (total_relation/truncation_reason). The view revision rescans the
    whole workspace three times; membership-only callers pass
    with_revision=False and get no "revision" key.
    """
    if effective.get("ids") == []:
        return [], {**({"revision": shared_view_revision(con, ctx)} if with_revision else {}),
                    "coverage": {"episodes_in_scope": 0,
                                 "sources_included": 0,
                                 "excluded_undated": 0},
                    "count_units": count_units,
                    "effective": effective,
                    "total_relation": "exact",
                    "truncation_reason": None}
    if light:
        items, info = _match_query2_light(con, effective, repo, ctx=ctx)
    else:
        items, info = _match_query2(con, effective, repo, ctx=ctx)
        items = _customer._apply_ids(con, items, effective.get("ids"))
    scoped, count_units = _apply_clause(con, effective, clause, items, ctx)
    coverage = _cell_coverage(con, effective, clause, ctx) or {
        "episodes_in_scope": info["episodes_in_scope"],
        "sources_included": info["sources_included"],
        "excluded_undated": info["excluded_undated"]}
    return scoped, {**({"revision": shared_view_revision(con, ctx)} if with_revision else {}),
                   **({"by_id": info["by_id"], "transcripts": info["transcripts"]} if light else {}),
                   "coverage": coverage,
                   "count_units": count_units,
                   "effective": effective,
                   "total_relation": info["total_relation"],
                   "truncation_reason": info["truncation_reason"]}


def match_for_tracker(con: sqlite3.Connection, query: dict,
                      live: dict | None = None,
                      repo=None) -> tuple[list[dict], dict]:
    """Canonical tracker membership: effective Query2 plus live clause.

    Returns all matched items (no pagination) plus revision/coverage and
    the completeness signals trackers use to refuse partial baselines.
    """
    effective, clause, count_units = tracker_effective(query, live)
    return match_effective(con, effective, clause, count_units, repo)


def _counts_occurrences(item: dict) -> bool:
    """A read behind the confirmed occurrences: a confirmed evidence record
    with a confirmed attribution."""
    return item.get("record_kind") == "evidence" and item.get("status") == "confirmed" \
        and any(a.get("basis") == "confirmed" for a in item.get("attribution") or [])


def _apply_clause(con: sqlite3.Connection, query: dict, clause: dict,
                  items: list[dict], ctx=None) -> tuple[list[dict], str]:
    kind = clause["kind"]
    if kind == "all_results":
        return items, "distinct_results"
    if kind == "summary_cell":
        members = _cell_members(
            con, items, clause["group_by"], clause["key"],
            _episode_brand_keys(con, ctx), _episode_categories(con, ctx))
        return members, "distinct_results"
    if kind == "confirmed_occurrences":
        if query["mode"] != "ads":
            raise _contracts.QueryRejected(
                "confirmed_occurrences clauses use a confirmed-ad base_query")
        seen: dict[tuple[str, str], dict] = {}
        aliases = _ad.alias_map(con)
        for item in filter(_counts_occurrences, items):
            for assoc in (item.get("attribution") or []):
                if assoc.get("basis") != "confirmed":
                    continue
                token = (item.get("episode_id") or "",
                         _ad.assoc_key(aliases, assoc))
                if token not in seen:
                    seen[token] = item
        ordered = sorted(seen.values(), key=lambda i: (i.get("sort_ref") or ""))
        return ordered, "confirmed_occurrences"
    raise _contracts.QueryRejected(f"invalid selector clause kind: {kind!r}")


def _cell_scope_query(query: dict, clause: dict) -> dict | None:
    """The episode scope of one week-chart cell as a Query2: its week
    inside the query's date bounds, or the undated episodes for the
    unknown cell. None for any other clause."""
    if clause.get("kind") != "summary_cell" or clause.get("group_by") != "week":
        return None
    key = clause.get("key")
    if key == "unknown":
        return dict(query, date_status="unknown")
    try:
        monday = date.fromisoformat(key)
    except (TypeError, ValueError):
        return None
    lo, hi = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    if query["from"] and query["from"][:10] > lo:
        lo = query["from"][:10]
    if query["to"] and query["to"][:10] < hi:
        hi = query["to"][:10]
    return dict(query, date_status="known", **{"from": lo, "to": hi})


def _cell_coverage(con: sqlite3.Connection, query: dict, clause: dict,
                   ctx=None) -> dict | None:
    """The coverage of a week or unknown-date chart cell: the episodes of
    its scope (_cell_scope_query), not the whole query's. None for any
    other clause or an exact-empty id set."""
    cell_query = _cell_scope_query(query, clause) if query.get("ids") != [] else None
    if cell_query is None:
        return None
    cell_eps, _aliases, cell_excluded = _customer._episode_scope(
        con, _v1_criteria(cell_query), ctx if ctx is not None else _customer._owner_ctx())
    return {"episodes_in_scope": len(cell_eps), "sources_included": len(cell_eps),
            "excluded_undated": cell_excluded}


@_customer.model_names(True)  # PODS: a customer display endpoint
def research_bundle(con: sqlite3.Connection, raw_query: dict,
                    raw_selector: dict | None, summaries: list[str] | None,
                    refresh_selector: bool, workspace_context: str,
                    repo=None, ctx=None, expected_view_revision=None,
                    rank: bool = True, recent: bool = False,
                    card_quote: bool | None = None) -> dict:
    """Evaluate one authorized read cohort: query+selector+result+summary.

    The caller owns the read transaction; rows, counts, summaries and the
    revision below all come from that single cohort. summaries holds
    week/category/brand/show/status names. refresh_selector re-evaluates
    the same base/clause in the current view and returns a newly bound
    counted selector with all new counts/rows together. rank (the Search
    page's list) orders ads by customer.search_rank; rank=False keeps the
    date order (summaries, the seed's reads); with recent (customer.query's
    "Recent ads" reads), same-day ties go the same way on every copy
    (customer.recent_order).
    """
    query = _contracts.normalize_query(raw_query)
    # card_quote (default rank): the Search page's card quote (CARD-QUOTE,
    # REDESIGN S7); S7-BRAND gives it to customer.query's Recent ads too.
    quote = rank if card_quote is None else card_quote
    if summaries is None:
        summaries = []
    for name in summaries:
        if name not in _contracts.SUMMARY_VALUES:
            raise _contracts.QueryRejected(f"invalid summary value: {name!r}")
    revision = _view_revision(con, ctx)
    if expected_view_revision is not None:
        if not isinstance(expected_view_revision, str) or not expected_view_revision:
            raise _contracts.QueryRejected("expected_view_revision must be a nonempty string")
        if expected_view_revision != revision:
            raise _contracts.SelectorConflict("research view changed; refresh the displayed cohort")
    selector = None
    effective = query
    clause: dict = {"kind": "all_results"}
    count_units = "distinct_results"
    if raw_selector is not None:
        selector = _contracts.validate_selector(
            raw_selector, current_revision=revision,
            workspace_context=workspace_context, refresh=refresh_selector)
        effective = _contracts.narrow_for_request(
            selector["base_query"], query)
        clause = selector["clause"]
        count_units = selector["count_units"]
    elif refresh_selector:
        raise _contracts.QueryRejected(
            "refresh_selector needs a selector")
    if effective.get("ids") == []:
        # Explicit exact-empty selection: zero records, never a scan
        # (same short-circuit as match_effective).
        items, info = [], {"total_relation": "exact", "truncation_reason": None,
                           "episodes_in_scope": 0, "sources_included": 0,
                           "excluded_undated": 0, "by_id": {}, "transcripts": {}}
    else:
        # Light rows: filter, order, count and summarize every match, then
        # build and bind only the page's items.
        items, info = _match_query2_light(con, effective, repo, ctx=ctx)
    if rank and effective["mode"] == "ads":
        # SEARCH-RANK: ads with a shown brand first, then by date; counts
        # and rows unchanged. Exports keep the date order (prepare_export).
        items = _customer.search_rank(con, items, ctx)
    elif recent and effective["mode"] == "ads":
        items = _customer.recent_order(items, effective["sort"], _customer.recent_avoid_reads(con))
    scoped, count_units = _apply_clause(con, effective, clause, items, ctx)
    weight = _customer.light_weight
    total = sum(weight(i) for i in scoped)
    dated = sum(weight(i) for i in scoped
                if _contracts.publication_day(i.get("pub_date") or ""))
    breaks = None
    if effective["mode"] == "ads":
        breaks = _breaks.scope_index(con, ctx) if scoped else {}
    break_fields: dict = {}
    # The listed brand the search words name (SEARCH-CLARITY), for the
    # head's brand_break_total and the cells' brand_breaks (REDESIGN S5).
    brand_keys = _named_brand_keys(con, effective.get("q"), ctx) if breaks is not None else None
    if breaks is None:
        page_items, _capped = _customer._materialize(
            con, scoped, ctx, repo, transcripts=info["transcripts"], by_id=info["by_id"],
            limit=PER_PAGE, offset=(query["page"] - 1) * PER_PAGE,
            match_q=query.get("q") or None, card_quote=quote)
    else:
        page_items, break_fields = _ads_page_by_break(
            con, scoped, breaks, query, ctx, repo, info, card_quote=quote,
            brand_keys=brand_keys, roll=rank)
    summary: dict[str, list[dict]] = {}
    for name in summaries:
        cells = _summarize(con, scoped, name, count_units, revision,
                           workspace_context, ctx, breaks=breaks, brand_keys=brand_keys)
        for cell in cells:
            cell["selector"] = {
                "version": 1, "base_query": effective,
                "clause": {"kind": "summary_cell", "group_by": name,
                           "key": cell["key"]},
                "count_units": count_units, "scope_mode": "counted_cohort",
                "view_revision": revision,
                "workspace_context": workspace_context}
        summary[name] = cells
    if selector is not None and (
            refresh_selector or selector["scope_mode"] == "live_definition"):
        selector = {"version": 1, "base_query": selector["base_query"],
                    "clause": clause, "count_units": count_units,
                    "scope_mode": "counted_cohort",
                    "view_revision": revision,
                    "workspace_context": workspace_context}
    # A week or unknown-date cell narrows the episodes in scope too.
    coverage = _cell_coverage(con, effective, clause, ctx) or {
        "episodes_in_scope": info["episodes_in_scope"],
        "sources_included": info["sources_included"],
        "excluded_undated": info["excluded_undated"]}
    if page_items:
        # HEARD-FIX: `brand.suggested`, the namer's raw heard name, leaves
        # the response unless it may be shown. New dicts: items are shared
        # with the memoized light rows.
        _aliases = _customer.customer_aliases(con)
        _checked = _customer._alias_checker(con, _aliases)
        page_items = [_customer._customer_item(i, _aliases, _checked) for i in page_items]
    result = {"items": page_items, "page": query["page"],
              "per_page": PER_PAGE, "total": total,
              "dated_total": dated,
              "undated_total": total - dated,
              "total_relation": info["total_relation"],
              "truncation_reason": info["truncation_reason"],
              "count_units": count_units,
              # REDESIGN S10 (ads mode: _ads_page_by_break's, from its ads).
              **(_result_reach(con, scoped, None) if breaks is None else {}),
              **break_fields}
    if _customer.excerpt_workspace(con, ctx):
        # Plan A (design 5): ads and episodes work on safe excerpts; the
        # mentions tab (transcript passages) is not offered. Server-derived,
        # so a zero-result or mentions answer still says why.
        result["excerpt_only"] = True
        if effective["mode"] == "mentions":
            result.update(items=[], total=0, dated_total=0, undated_total=0,
                          episode_total=0, show_total=0, plan_limited=True)
    from earwitness.app import exports as _exports
    if _exports._excerpt_only(con):
        # DEMO2-SCR-R1-01: this workspace exports ad excerpts only, so an
        # All mentions or Episodes file holds no rows; the export dialog
        # says so before the file is made.
        result["export_excerpts_only"] = True
    return {"query": query, "selector": selector, "result": result,
            "summary": summary, "revision": revision,
            "workspace_context": workspace_context, "coverage": coverage}


# SPEED-R1 (2026-10-29): POST /research-query took ~1.9 s on every Search
# open (cards and list), uncached. It is remembered like Search
# (customer.query): per research data state, transcript index state,
# disclosure class and request. The caller's workspace_context is the only
# per-person value in the answer (the class audit of READ-CACHE): the class
# computation runs with _CONTEXT_SLOT in its place and each caller's own
# context is put back on a copy. A selector names a context: one that
# names the caller's own keys on the slot; any other computes afresh (and
# is refused, as before).
_CONTEXT_SLOT = "\0workspace-context\0"
RESEARCH_QUERY_KEEP = 16    # research queries last used per class
# SPEED-FIX1 (SPS-R1-02): the longest remembered request, as its key's
# JSON; a longer one (a padded selector, a huge clause key) is computed
# afresh and kept nowhere. The Search page's own requests are under 2 KB.
RESEARCH_QUERY_KEY_LIMIT = 8192


def research_query_state(con: sqlite3.Connection):
    """The memo state of a research query: research_view_state plus the
    transcript index state, for every mode (stricter than Search: the
    index is outside the view state, db.VIEW_STATE_UNREAD)."""
    view = research_view_state(con)
    return None if view is None else (view, _transcript_index_state(con))


def research_data_state(con: sqlite3.Connection):
    """What a research change moves (SPEED-FIX1, SPS-R1-01): the research
    data token (db.research_state_token: an import, the nightly window, a
    reseed, a correction, a review, an owner's ad review, a content policy)
    and the transcript index state. Not a campaign, brief, comment, share
    or invite (those move research_view_state only). None when the
    research triggers are missing."""
    token = _db.research_state_token(con)
    return None if token is None else (token, _transcript_index_state(con))


def _keyed_selector(raw):
    """The fields of a selector research_bundle reads (customer_contracts.
    validate_selector), normalised: unknown fields dropped, the base query
    and clause checked. validate_selector gives the same answer on it as on
    raw. QueryRejected when raw is not keyable (research_bundle then
    reports its own error)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _contracts.QueryRejected("selector must be an object or null")
    return {"version": raw.get("version"),
            "base_query": _contracts.normalize_query(raw.get("base_query")),
            "clause": _contracts._check_clause(raw.get("clause")),
            "count_units": raw.get("count_units", "distinct_results"),
            "scope_mode": raw.get("scope_mode"), "view_revision": raw.get("view_revision"),
            "workspace_context": raw.get("workspace_context")}


def research_query_tag(raw_query, raw_selector, summaries, refresh_selector: bool,
                       workspace_context: str, expected_view_revision=None, rank: bool = True):
    """(tag JSON, normalized query, keyed selector, summaries) of one
    research query request, or None when it is not remembered: input that
    does not key, a selector naming another context, or a key over
    RESEARCH_QUERY_KEY_LIMIT. The selector's context is the class slot;
    summaries are de-duplicated (research_bundle answers the same)."""
    try:
        query = _contracts.normalize_query(raw_query)
        selector = _keyed_selector(raw_selector)
        if not isinstance(summaries, list) or any(
                not isinstance(name, str) or name not in _contracts.SUMMARY_VALUES
                for name in summaries):
            return None
        summaries = list(dict.fromkeys(summaries))
        if selector is not None and selector["workspace_context"] is not None:
            if selector["workspace_context"] != workspace_context:
                return None
            selector["workspace_context"] = _CONTEXT_SLOT
        tag = json.dumps([query, selector, summaries, refresh_selector, expected_view_revision,
                          rank], sort_keys=True, separators=(",", ":"))
    except (_contracts.QueryRejected, TypeError, ValueError):
        return None
    if len(tag) > RESEARCH_QUERY_KEY_LIMIT:
        return None
    return tag, query, selector, summaries


def research_bundle_memo(con: sqlite3.Connection, raw_query, raw_selector, summaries,
                         refresh_selector: bool, workspace_context: str, repo=None, ctx=None,
                         expected_view_revision=None, rank: bool = True,
                         pinned=frozenset()) -> dict:
    """research_bundle, remembered per (database, research_query_state,
    disclosure class, request: research_query_tag) under class_memo
    (single flight, at most RESEARCH_QUERY_KEEP per class). Equal to
    research_bundle in value and errors. With repo, without ctx, or with a
    request research_query_tag does not remember, it is research_bundle
    itself. A request whose tag is in pinned (the read warm-up's,
    services.FIVE_TASK_READS) has a budget of its own, so a class's other
    searches never drop it (SPEED-FIX1). The caller must not mutate the
    result."""
    def plain():
        return research_bundle(con, raw_query, raw_selector, summaries, refresh_selector,
                               workspace_context, repo, ctx=ctx,
                               expected_view_revision=expected_view_revision, rank=rank)
    if repo is not None or ctx is None or not workspace_context:
        return plain()
    found = research_query_tag(raw_query, raw_selector, summaries, refresh_selector,
                               workspace_context, expected_view_revision, rank)
    if found is None:
        return plain()
    tag, query, selector, summaries = found
    group = "research-query-warm" if tag in pinned else "research-query"
    cached = class_memo(
        con, ctx, (group, tag),
        lambda c: research_bundle(con, query, selector, list(summaries), refresh_selector,
                                  _CONTEXT_SLOT, None, ctx=c,
                                  expected_view_revision=expected_view_revision, rank=rank),
        state=research_query_state, keep=RESEARCH_QUERY_KEEP)
    return _with_context(cached, workspace_context)


def _with_context(bundle: dict, workspace_context: str) -> dict:
    """bundle with the caller's workspace_context in every place the class
    computation put _CONTEXT_SLOT (the bundle, its selector, each summary
    cell's selector); a copy where changed, the shared rest untouched."""
    def own(selector):
        if isinstance(selector, dict) and selector.get("workspace_context") == _CONTEXT_SLOT:
            return dict(selector, workspace_context=workspace_context)
        return selector
    out = dict(bundle)
    if out.get("workspace_context") == _CONTEXT_SLOT:
        out["workspace_context"] = workspace_context
    out["selector"] = own(out.get("selector"))
    out["summary"] = {name: [dict(cell, selector=own(cell["selector"])) if "selector" in cell
                             else cell for cell in cells]
                      for name, cells in (out.get("summary") or {}).items()}
    return out


def _named_brand_keys(con: sqlite3.Connection, q, ctx) -> set | None:
    """The keys of the caller's Brands list row the search words name, else
    None. The Search page's brand helper rule (views.js brandJump): the
    words, outer spaces and case aside and with no quotes, equal the row's
    key, display name or one of its merged keys; the first such row in
    the list's order. Held brands are never in the list."""
    text = (q or "").strip() if isinstance(q, str) else ""
    if not text or '"' in text:
        return None
    needle = text.lower()
    for row in _customer.brands_directory(con, "", ctx):
        keys = [k for k in [row.get("key"), *(row.get("keys") or [])] if k]
        if any(str(name).lower() == needle for name in [*keys, row.get("display")] if name):
            return set(keys)
    return None


def _result_reach(con: sqlite3.Connection, scoped: list[dict], real) -> dict:
    """REDESIGN S10: the distinct episodes and shows of what a result's
    head counts, over every page: real (ads mode, _ads_page_by_break's
    (ad, rows) pairs) its ads' episodes, a read in no ad being none; else
    the rows' episodes. An alias episode is its target. A show is the
    Shows page's (_all_show_members: a disputed mapping is in no show)."""
    alias_of = dict(con.execute("SELECT id, alias_of FROM episodes WHERE alias_of IS NOT NULL"))
    found = ({brk["episode_id"] for brk, _rows in real} if real is not None
             else {row.get("episode_id") for row in scoped})
    episodes = {alias_of.get(e, e) for e in found if e}
    show_of = {e: show for show, eps in _customer._all_show_members(con).items() for e in eps}
    return {"episode_total": len(episodes),
            "show_total": len({show_of[e] for e in episodes if e in show_of})}


def _ads_page_by_break(con: sqlite3.Connection, scoped: list[dict], breaks: dict,
                       query: dict, ctx, repo, info: dict,
                       card_quote: bool = False,
                       brand_keys: set | None = None,
                       roll: bool | None = None) -> tuple[list[dict], dict]:
    """One Search Ads page by ad break (D-1): page N holds breaks
    (N-1)*PER_PAGE .. N*PER_PAGE-1, ordered by each break's first matching
    read in the current sort (on the Search page, card_quote, a roll's
    ads kept together on one page: _roll_order, _roll_pages), with every matching read of those
    breaks, contiguous per break. A matching read outside the customer ad scope
    (self-promotion) is a page unit of its own, in no break. Returns the
    page items (each with ad_break_id) and the result's break fields.
    brand_keys (_named_brand_keys) adds brand_break_total: the result's
    ads named that brand (SEARCH-CLARITY), over these same caller rows; a
    held read names no brand, so it never makes an ad count."""
    # roll (default card_quote): the Search page's order. S7-BRAND quotes
    # the Recent ads read as Search (quote) but keeps its date order
    # (roll=False): from here card_quote is the order, quote the quote.
    quote = card_quote
    if roll is not None:
        card_quote = roll
    units: dict = {}
    for row in scoped:
        brk = breaks.get(_breaks.evidence_id(row))
        key = brk["id"] if brk is not None else ("solo", id(row))
        units.setdefault(key, (brk, []))[1].append(row)
    real = [(brk, rows) for brk, rows in units.values() if brk is not None]
    break_dated = sum(1 for brk, rows in real
                      if _contracts.publication_day(rows[0].get("pub_date") or ""))
    offset = (query["page"] - 1) * PER_PAGE
    order = (_roll_pages(_roll_order(list(units.values())), PER_PAGE) if card_quote
             else list(units.values()))
    page = order[offset:offset + PER_PAGE]
    rows = [row for _brk, members in page for row in members]
    items, _capped = _customer._materialize(
        con, rows, ctx, repo, transcripts=info["transcripts"], by_id=info["by_id"],
        match_q=query.get("q") or None, card_quote=quote)
    for item in items:
        brk = breaks.get(item.get("evidence_id"))
        item["ad_break_id"] = brk["id"] if brk is not None else None
    ad_breaks = []
    for brk, members in page:
        if brk is None:
            continue
        ad_breaks.append({
            "id": brk["id"], "episode_id": brk["episode_id"],
            "transcript_version_id": brk["transcript_version_id"],
            "on_current_transcript": brk["on_current_transcript"],
            "span": brk["span"], "read_count": len(brk["reads"]),
            "matching_read_count": len(members),
            "refs": [i.get("ref") or i.get("id") for i in items
                     if i["ad_break_id"] == brk["id"]],
            "brands": [dict(b) for b in brk["brands"]],
            "group_id": brk["group_id"], "group_ad_count": brk["group_ad_count"],
            "pub_date": members[0].get("pub_date") or ""})
    fields = {"break_total": len(real), "break_dated_total": break_dated,
              "break_undated_total": len(real) - break_dated,
              "ad_breaks": ad_breaks, **_result_reach(con, scoped, real)}

    def clip(brk):
        # REDESIGN S6: an ad with a clip the caller can play (a read of the
        # ad with has_clip, the Shows page's rule, _show_ad_counts).
        return any(r.get("has_clip") for r in brk["reads"])
    fields["ads_with_clips"] = sum(1 for brk, _rows in real if clip(brk))
    if brand_keys is not None:
        fields["brand_break_total"] = sum(
            1 for brk, _rows in real if brand_keys & {b["key"] for b in brk["brands"]})
        fields["brand_ads_with_clips"] = sum(
            1 for brk, _rows in real if brand_keys & {b["key"] for b in brk["brands"]} and clip(brk))
    return items, fields


def _roll_order(units: list) -> list:
    """The Search page's ads with each roll kept together (NOGAP-AB rows
    21/154, M4 roll card): the ads of one overlap group (group_ad_count
    >= 2) follow the group's first ad, in play order (ad_breaks order), so
    the page can show them under one "Ad break" box. Every other unit
    keeps its place; counts and members are unchanged."""
    members: dict = {}
    for unit in units:
        if _roll_key(unit[0]) is not None:
            members.setdefault(_roll_key(unit[0]), []).append(unit)
    out, placed = [], set()
    for unit in units:
        key = _roll_key(unit[0])
        if key is None:
            out.append(unit)
        elif key not in placed:
            placed.add(key)
            out.extend(sorted(members[key], key=lambda u: _breaks._sort_key(u[0])))
    return out


def _roll_key(brk):
    """The overlap group of a roll's ad (group_ad_count >= 2), else None."""
    return brk["group_id"] if brk is not None and brk["group_ad_count"] >= 2 else None


def _roll_pages(units: list, per_page: int) -> list:
    """_roll_order's units with no roll split by a page break (NOGAP-AB
    FIX3, AB-R1-04: Insurify + T-Mobile ended p167 and opened p168, so
    neither page showed its box). Every page still holds per_page ads, so
    page N is still ads (N-1)*per_page+1 .. N*per_page and the page count,
    page links and "Showing" line are unchanged. A roll that would run
    past a page end starts the next page; the next ads that are in no
    roll fill this page in its place. Where no later ad can fill the page
    (the result's last ads), the roll ends its page instead, and as many
    of the page's own single ads go back to wait. Every ad is shown once.
    Limits (FIX4, AB3-05): a roll longer than a page keeps today's order;
    at the result's end, a roll with too few single ads on its page to
    move back keeps its place and crosses the page end, so with two rolls
    waiting there one can split on the second-to-last page (pages of 3:
    "A A B | B C C | C"); and with several rolls waiting, an ad can move
    more places than a roll's size (three 10-ad rolls waiting move the
    page's single ads back 3 pages)."""
    blocks: list = []
    for unit in units:
        key = _roll_key(unit[0])
        if key is not None and blocks and _roll_key(blocks[-1][0][0]) == key:
            blocks[-1].append(unit)
        else:
            blocks.append([unit])
    out: list = []
    held: list = []
    boxed: set = set()
    nxt = 0
    while nxt < len(blocks) or held:
        room = per_page - len(out) % per_page
        fit = next((n for n, block in enumerate(held) if len(block) <= room), None)
        if fit is not None:
            block = held.pop(fit)
        elif nxt < len(blocks):
            block = blocks[nxt]
            nxt += 1
            if room < len(block) <= per_page:
                held.append(block)
                continue
        else:
            block = held.pop(0)
            page = range(len(out) - (per_page - room), len(out))
            singles = [n for n in page if id(out[n]) not in boxed][room - len(block):]
            if len(singles) == len(block) - room:
                held[:0] = [[out[n]] for n in singles]
                for n in reversed(singles):
                    del out[n]
        if len(block) > 1:
            boxed.update(map(id, block))
        out.extend(block)
    return out


def _filter_value_entries(con, ctx=None) -> dict:
    """Every value of brands, shows and categories for ctx, ordered, before
    any q narrowing or paging: one ads and one episodes match shared by the
    three fields (DEMO2 pass 9). filter_values remembers it per database,
    research query state and disclosure class (class_memo, SPEED-R1)."""
    items = []
    ads: list = []
    for mode in ("ads", "episodes"):
        query = _contracts.normalize_query({"version": 2, "mode": mode})
        matched, _info = _match_query2_light(con, query, ctx=ctx)
        items.extend(matched)
        if mode == "ads":
            ads = matched
    aliases = _customer.customer_aliases(con)
    categories = {row["id"]: row["category"] for row in con.execute("SELECT id, category FROM episodes")}
    values: dict = {"brands": {}, "shows": {}, "categories": {}}
    for item in items:
        for key in _item_brand_keys(item, aliases):
            values["brands"][key] = None
        show = item.get("show") or {}
        identity = _item_show_id(item)
        if identity:
            values["shows"][identity] = show.get("display_title") or item.get("show_title") or identity
        category = categories.get(item.get("episode_id"))
        if category is not None:
            values["categories"][category] = category or "Unknown"
    # One display lookup per brand, not per ad (Sol R1-02).
    values["brands"] = {key: _ad.brand_display(con, key, aliases)[0] for key in values["brands"]}
    out = {field: [{"value": key, "label": label} for key, label in sorted(found.items())]
           for field, found in values.items()}
    # Brands: most-heard names first, then by name (UX-09); a confirmed flag,
    # never the per-value counts (accepted difference 3). Read from the
    # ads matched above, as customer.brands_directory counts them
    # (records: distinct ad refs per attributed brand key; confirmed: any
    # confirmed one), without rebuilding the directory (Sol R1-02).
    records: dict[str, set] = {}
    confirmed: set[str] = set()
    for item in ads:
        for assoc in item.get("attribution") or []:
            if not assoc.get("brand"):
                continue
            key = _ad.assoc_key(aliases, assoc)
            records.setdefault(key, set()).add(item.get("ref") or item["id"])
            if item.get("status") == "confirmed":
                confirmed.add(key)
    for entry in out["brands"]:
        entry["confirmed"] = entry["value"] in confirmed
    out["brands"].sort(key=lambda e: (-len(records.get(e["value"], ())),
                                      e["label"].casefold(), e["value"]))
    return out


def _transcript_index_state(con) -> str | None:
    """Digest of the transcript index's version table: the one input of
    the values computation outside the guarded tables (db.VIEW_STATE_UNREAD;
    passage groups read which versions are indexed). None without an index."""
    import hashlib
    from . import transcript_index as _tindex
    if not _tindex.available(con):
        return None
    digest = hashlib.sha256()
    for row in con.execute(f"SELECT version_id, segments FROM {_tindex.VERSIONS}"
                           " ORDER BY version_id"):
        digest.update(f"{row[0]}:{row[1]};".encode())
    return digest.hexdigest()


def filter_values(con, field, q="", cursor=None, limit=50, ctx=None):
    import json
    if field not in ("brands", "shows", "categories") or not isinstance(q, str):
        raise _contracts.QueryRejected("invalid filter field or query")
    if not con.in_transaction:
        # The memo key (view state, index state) and the values: one snapshot.
        with _db.transaction(con, write=False):
            return filter_values(con, field, q, cursor, limit, ctx)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= FILTER_VALUE_LIMIT:
        raise _contracts.QueryRejected("invalid filter limit")
    revision = _view_revision(con, ctx)
    offset = 0
    if cursor:
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        except (ValueError, TypeError):
            raise _contracts.QueryRejected("malformed filter cursor")
        if state.get("field") != field or state.get("q") != q:
            raise _contracts.QueryRejected("filter cursor does not match input")
        if state.get("revision") != revision:
            raise _contracts.SelectorConflict("filter values changed; refresh")
        offset = state.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise _contracts.QueryRejected("malformed filter cursor")
    needle = q.strip().casefold()
    # SPEED-R1: once per disclosure class and research query state (the
    # research data and the transcript index), not per person and view
    # state: the first call took 2.4 s for every person, and again after
    # any saved-item write.
    known = class_memo(con, ctx, ("filter-values",), lambda c: _filter_value_entries(con, c),
                       state=research_query_state, default=True)
    entries = [dict(entry) for entry in known[field]
               if not needle or needle in entry["value"].casefold()
               or needle in entry["label"].casefold()]
    more = offset + limit < len(entries)
    next_cursor = base64.urlsafe_b64encode(json.dumps(
        {"field": field, "q": q, "revision": revision, "offset": offset + limit}).encode()).decode() if more else None
    return {"field": field, "items": entries[offset:offset + limit], "total": len(entries),
            "total_relation": "exact", "next_cursor": next_cursor, "revision": revision,
            "limits": {"max_values_per_field": _contracts.SET_MAX, "max_ids": EXPORT_ID_LIMIT}}


def _selected_episodes(con: sqlite3.Connection, ids) -> set[str]:
    """The episodes an explicit id set addresses, each with its canonical
    target (ad_breaks.index_for_reads): an alias read folds into its
    target's episode, or stands alone when the viewer may not see it."""
    episodes = set(_customer._episode_ids_for_ids(con, ids) or ())
    canonical = _breaks.canonical_map(con)
    return episodes | {canonical[e] for e in episodes if e in canonical}


def capture_export(con: sqlite3.Connection, raw_query: dict,
                   raw_selector: dict | None, scope: str, fmt: str,
                   expected_view_revision: str,
                   workspace_context: str | None = None, repo=None,
                   title: str | None = None, actor: str = "owner", ctx=None) -> dict:
    """prepare_export then store_export on one connection and transaction."""
    return store_export(con, prepare_export(
        con, raw_query, raw_selector, scope, fmt, expected_view_revision,
        workspace_context, repo, title=title, ctx=ctx), actor=actor, ctx=ctx)


def prepare_export(con: sqlite3.Connection, raw_query: dict,
                   raw_selector: dict | None, scope: str, fmt: str,
                   expected_view_revision: str,
                   workspace_context: str | None = None, repo=None,
                   title: str | None = None, ctx=None) -> dict:
    """Evaluate an export scope and render its file, with no write
    (store_export persists it; K09).

    scope is all_matches (every qualifying page) or selected (the
    explicit ids set intersected with the active query/selector).
    A changed or partial basis is rejected, never captured. The
    rendered bytes persist to the archive with a sha256 digest and the
    pinned query/selector/revision basis.
    """
    if scope not in ("all_matches", "selected"):
        raise _contracts.QueryRejected(
            "scope must be all_matches or selected")
    if fmt not in ("csv", "json", "html"):
        raise _contracts.QueryRejected("format must be csv, json, or html")
    query = _contracts.normalize_query(raw_query)
    if scope == "selected":
        if query["ids"] is None:
            raise _contracts.QueryRejected(
                "selected exports need an explicit ids set")
        if len(query["ids"]) > EXPORT_ID_LIMIT:
            raise _contracts.QueryRejected(
                f"selected exports cap at {EXPORT_ID_LIMIT} refs")
    revision = _view_revision(con, ctx)
    if expected_view_revision != revision:
        raise _contracts.SelectorConflict(
            "export view changed; refresh before capturing")
    selector = None
    effective = query
    clause: dict = {"kind": "all_results"}
    count_units = "distinct_results"
    if raw_selector is not None:
        selector = _contracts.validate_selector(
            raw_selector, current_revision=revision,
            workspace_context=workspace_context)
        effective = _contracts.narrow_for_request(
            selector["base_query"], query)
        clause = selector["clause"]
        count_units = selector["count_units"]
    plan_limited = effective["mode"] == "mentions" and _customer.excerpt_workspace(con, ctx)
    if scope == "all_matches" and effective.get("ids") != [] and not plan_limited:
        # The cap, before the export's own match and rendering: the light
        # rows and clause of research_bundle, so the count is its
        # result.total (a Plan A mentions export holds none).
        light, _light_info = _match_query2_light(con, effective, repo, ctx=ctx)
        counted, _units = _apply_clause(con, effective, clause, light, ctx)
        if sum(_customer.light_weight(row) for row in counted) > EXPORT_RESULT_CAP:
            raise _contracts.ExportTooLarge(
                EXPORT_CAP_ADS_MESSAGE if effective["mode"] == "ads" else EXPORT_CAP_MESSAGE)
    if effective.get("ids") == []:
        scoped = []
    else:
        if scope == "selected" and effective["mode"] == "ads":
            # Only the selected ads' episodes: each of their items is the
            # item of the whole-workspace match, and every other item is
            # cut by the ids below (retime 130b8e80: one selected ad built
            # every ad of the corpus).
            items, _info = _match_query2_scan(
                con, effective, repo, ctx=ctx,
                episodes=_selected_episodes(con, query["ids"]))
        else:
            items, _info = _match_query2(con, effective, repo, ctx=ctx)
        if _info["total_relation"] != "exact" or _info["truncation_reason"]:
            raise _contracts.SelectorConflict("partial query cannot be captured as a complete export")
        if scope == "selected":
            qualified = [_customer._canonical_ref_for_input(con, ref) for ref in query["ids"]]
            if any(ref is None for ref in qualified):
                raise _contracts.QueryRejected("selected source is unknown or unavailable")
            from earwitness.app import exports as _exports
            for ref in qualified:
                _exports.export_disclosure(con, ref, ctx)
            items = _customer._apply_ids(con, items, query["ids"])
        scoped, count_units = _apply_clause(con, effective, clause, items, ctx)
    if plan_limited:
        scoped = []  # transcript passages are not offered at excerpt level
    # Decision 1: every row names its ad (one row per evidence record still).
    index = _breaks.index_for_reads(
        con, ctx, [_breaks.evidence_id(row) for row in scoped]) if scoped else {}
    scoped = [dict(row, ad_id=(index.get(_breaks.evidence_id(row)) or {}).get("id"))
              for row in scoped]
    from earwitness.app import exports as _exports
    prepared = _exports.prepare_capture(
        con, scoped, fmt, scope, count_units,
        {"query": effective, "selector": selector,
         "view_revision": revision, "scope": scope},
        title or "EarWitness research export", ctx=ctx)
    return dict(prepared, view_revision=revision, query=effective, selector=selector,
                plan_limited=plan_limited)


def store_export(con: sqlite3.Connection, prepared: dict, actor: str = "owner",
                 ctx=None) -> dict:
    """Store a prepared export. It may have been prepared on another
    connection's snapshot (services.prepare_research_export): the view it
    was computed in must still be the current view, else it is refused as
    a changed basis, never stored."""
    from earwitness.app import exports as _exports
    revision = prepared["view_revision"]
    if _view_revision(con, ctx) != revision:
        raise _contracts.SelectorConflict(
            "export view changed; refresh before capturing")
    stored = _exports.insert_capture(con, prepared, actor)
    effective, selector = prepared["query"], prepared["selector"]
    count_units, plan_limited = prepared["count_units"], prepared["plan_limited"]
    # Rows the record does not let leave as text are left out (exports.export_disclosure).
    scoped = stored["rows"]
    # Plan A (design 5): a mentions export says why it holds nothing.
    plan = {"plan_limited": True} if plan_limited else {}
    # DEMO2-SCR-R1-01: an empty file says why (the workspace's export rule).
    if _exports._excerpt_only(con):
        plan["export_excerpts_only"] = True
    return {**plan, "rows": scoped, "total": len(scoped), "withheld": stored["withheld"],
            "count_units": count_units, "view_revision": revision,
            "query": effective, "selector": selector,
            "export_id": stored["export_id"], "digest": stored["digest"],
            "title": stored["title"], "payload": stored["payload"],
            "item_count": stored["item_count"],
            "ad_count": len({row["ad_id"] for row in scoped if row["ad_id"]}),
            "aggregate_rev": stored["aggregate_rev"]}


@_customer.model_names(True)  # PODS: a customer display endpoint
def compare_bundle(con: sqlite3.Connection, brands: list, period_a: dict | None,
                   period_b: dict | None, raw_query: dict | None,
                   raw_selector: dict | None, workspace_context: str,
                   repo=None, ctx=None) -> dict:
    """Two-brand (up to ten) comparison on one cohort: each cell counts
    every ad of the brand the caller can see in the period, as Search and
    the Brand page do (PLAIN-ADS). Ads held for brand review match no
    brand, so they count toward none. Each cell's selector opens the
    brand's Search for that period.

    Without repo or selector, remembered per research state, disclosure
    class and arguments (class_memo, as Search; the 8 Compares last
    used): two judges' Compares held both read threads for seconds
    (PBA-R4-02). The caller's workspace context (per person) is laid over
    the remembered body. A selector is checked against the caller's own
    context, so a Compare with one is computed each time. A refused
    request is never remembered. Callers must not mutate the result."""
    try:
        tag = ("compare", json.dumps([brands, period_a, period_b, raw_query], sort_keys=True))
    except (TypeError, ValueError):
        tag = None
    if repo is not None or raw_selector is not None or tag is None:
        return _compare_bundle(con, brands, period_a, period_b, raw_query, raw_selector,
                               workspace_context, repo, ctx)
    shared = class_memo(con, ctx, tag, lambda c: _compare_bundle(
        con, brands, period_a, period_b, raw_query, None, "", None, c),
        state=research_view_state, keep=8)
    cells = [{name: dict(cell, selector=dict(cell["selector"],
                                            workspace_context=workspace_context))
              if isinstance(cell, dict) and "selector" in cell else cell
              for name, cell in row.items()} for row in shared["cells"]]
    return dict(shared, cells=cells, workspace_context=workspace_context)


def _compare_bundle(con, brands, period_a, period_b, raw_query, raw_selector,
                    workspace_context, repo=None, ctx=None) -> dict:
    if not isinstance(brands, list) or not 1 <= len(brands) <= 10:
        raise _contracts.QueryRejected("brands must be a list of 1..10 values")
    if len(set(brands)) != len(brands):
        raise _contracts.QueryRejected("brands has a repeated value")
    for brand in brands:
        if not isinstance(brand, str) or not brand:
            raise _contracts.QueryRejected("brands entries must be nonempty strings")
    periods = {}
    for name, period in (("period_a", period_a), ("period_b", period_b)):
        if period is None:
            periods[name] = None
            continue
        if not isinstance(period, dict):
            raise _contracts.QueryRejected(f"{name} must be an object")
        frm, to = period.get("from"), period.get("to")
        for label, value in (("from", frm), ("to", to)):
            if not isinstance(value, str) \
                    or _contracts.publication_day(value) != value[:10]:
                raise _contracts.QueryRejected(
                    f"{name}.{label} must be a valid ISO date")
        if to[:10] < frm[:10]:
            raise _contracts.QueryRejected(f"{name}.to must not precede from")
        periods[name] = {"from": frm[:10], "to": to[:10]}
    base = _contracts.normalize_query(raw_query) if raw_query is not None \
        else {"version": 2, "mode": "ads", "q": "", "brands": None,
              "shows": None, "categories": None, "statuses": None,
              "audio": None, "date_status": "all", "from": None, "to": None,
              "sort": "newest", "page": 1, "ids": None}
    revision = _view_revision(con, ctx)
    selector = None
    if raw_selector is not None:
        selector = _contracts.validate_selector(
            raw_selector, current_revision=revision,
            workspace_context=workspace_context)
        base = _contracts.narrow_for_request(selector["base_query"], base)
    if base["mode"] != "ads":
        raise _contracts.QueryRejected("comparison scopes to ads")
    base = dict(base)
    cells = []
    scale = 0
    ad_index = _breaks.scope_index(con, ctx)
    for brand in brands:
        scoped_query = dict(base)
        scoped_query["brands"] = [brand] if base["brands"] is None or \
            brand in base["brands"] else []
        row = {"brand": brand}
        counted: list = []  # PODS: model_only
        for pname in ("period_a", "period_b"):
            if pname == "period_b" and periods["period_b"] is None:
                continue
            cell_query = dict(scoped_query)
            period = periods[pname]
            if period is not None:
                lower = max(filter(None, (cell_query["from"], period["from"])))
                upper = min(filter(None, (cell_query["to"], period["to"])))
                if lower > upper:
                    cell_query["ids"] = []
                else:
                    cell_query["from"], cell_query["to"] = lower, upper
            if cell_query.get("ids") == []:
                items = []
            else:
                items, _info = _match_query2_light(con, cell_query, repo, ctx=ctx)
            if selector is not None:
                items, _original_units = _apply_clause(con, cell_query, selector["clause"], items, ctx)
            counted.extend(items)
            refs = [i["ref"] for i in items[:200]]
            reach = _customer._brand_reach(items)
            ads = _breaks.count(ad_index, map(_breaks.evidence_id, items))
            key = pname[-1]
            row[key] = {
                "count": len(items),
                # Distinct ads among the brand's results (one ad counts
                # once however much evidence it has).
                "ads": ads,
                "shows": reach["shows"], "last_heard": reach["last_heard"],
                "records_total": len(items),
                "coverage": {"matched_in_scope": len(items)},
                "count_units": "distinct_results",
                "selector": {
                    "version": 1, "base_query": cell_query,
                    "clause": {"kind": "all_results"},
                    "count_units": "distinct_results",
                    "scope_mode": "counted_cohort",
                    "view_revision": revision,
                    "workspace_context": workspace_context},
                "refs": refs, "refs_complete": len(items) <= 200}
            scale = max(scale, ads)
        if counted and all(_customer._model_keys(i) for i in counted):
            row["model_only"] = True  # PODS: every ad counted here is model-named
        cells.append(row)
    return {"brands": brands, "period_a": periods["period_a"],
            "period_b": periods["period_b"], "cells": cells,
            "shared_scale": scale, "revision": revision,
            "workspace_context": workspace_context}
