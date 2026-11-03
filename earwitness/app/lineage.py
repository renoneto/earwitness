"""Human work follows a read (review of 534fc73, items 2 and 4).

A stable record key (`named:<id>`, `det:<id>`, `jev:<ep>:<win>`) has
exactly one active row. Human work found on any other row is moved to the
active row it belongs to:

- duplicate: a retired row of the same key, episode and span (copies an
  older importer made) merges onto the active row of that key;
- carried over: a retired row whose span or transcript changed (a re-name,
  a re-transcription) goes to the active row of the same kind (and, for a
  second-brand read `<span>.b-<brand key>`, the same brand key) in the same
  episode whose span overlaps it most by IoU >= 0.5. When several retired
  rows overlap one active row, a row with a review or an operator brand
  change (even a removal) wins over one that is only in a tracker
  baseline, then the largest
  overlap; a row that loses tries its next-best read, and a row left with
  none is reported (overlap_taken).

Moved: reviews (the latest judgment stays; the other is in review history
and in evidence_lineage.old_judgment) with the winning review's operator
brand associations (differing judgments or brands are reported as
conflicts; namer suggestions a person's brand change removed stay
removed on the new row), campaign links and checks, and, once the active row is
published, notes, saved captures' targets, citations and tracker members
and events (repoint), so a carried-over read does not fire a tracker
again. Human work includes a tracker's silent baseline members.

Notes and citations pinned to an older publication of the retired row are
re-pointed to the new row's current publication: the alternative is a link
to a read that no longer shows anywhere. The frozen capture of a saved
item keeps its own pin; only its target moves.

Each move is one evidence_lineage row (old row -> new row). A retired row
that lost an overlap to another (overlap_taken) gets a final lineage row
without moving anything, so an unchanged reload never moves it later. A
row that becomes active again loses its lineage, so its latest work carries
when it is retired again. A retired row with human work and no successor is
reported as orphaned on every load; its work stays on it, never dropped.
"""
from __future__ import annotations

import json
import sqlite3

from . import db

IOU_MIN = 0.5
SCHEMA = ("CREATE TABLE IF NOT EXISTS evidence_lineage ("
          " old_id TEXT PRIMARY KEY,"
          " new_id TEXT NOT NULL,"
          " reason TEXT NOT NULL,"
          " overlap REAL,"
          " old_judgment TEXT,"
          " created_at TEXT NOT NULL)")
# The order of a review a move copied: the copy keeps its source's place in
# time (review_history sequence), so "latest review wins" compares when the
# judgment was made, not when it was copied, even within one second.
ORDER_SCHEMA = ("CREATE TABLE IF NOT EXISTS evidence_review_order ("
                " evidence_id TEXT PRIMARY KEY,"
                " review_revision INTEGER NOT NULL,"
                " seq INTEGER NOT NULL)")
ORIGIN = "lineage"
_REPORT_KEYS = 20


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(SCHEMA)
    con.execute(ORDER_SCHEMA)
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    db.ensure_view_state_guards(con)


def _tables(con) -> frozenset:
    return db._table_names(con)


def _state_id(ref) -> str | None:
    """The publication id of a SourceRef1 (`sr1:<id>`, maybe with a window)."""
    if not isinstance(ref, str) or not ref.startswith("sr1:"):
        return None
    return ":".join(ref.split(":")[:2])


def _ref_records(con, refs) -> dict[str, str]:
    """publication id -> evidence id, for the evidence publications among refs."""
    ids = sorted({s for s in map(_state_id, refs) if s})
    out = {}
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        out.update(con.execute("SELECT id, record_id FROM source_states WHERE kind='evidence'"
                               " AND id IN (%s)" % ",".join("?" * len(chunk)), chunk).fetchall())
    return out


def _saved_refs(con) -> list[tuple[str, str, str]]:
    """(item id, target ref, captured source ref) of every saved item."""
    out = []
    if "customer_saved_items" not in _tables(con):
        return out
    for row in con.execute("SELECT id, target_json, captured_json FROM customer_saved_items"):
        try:
            target = json.loads(row[1] or "{}")
            captured = json.loads(row[2] or "{}")
        except (ValueError, TypeError):
            continue
        out.append((row[0], target.get("ref") if isinstance(target, dict) else None,
                    captured.get("source_ref") if isinstance(captured, dict) else None))
    return out


def human_work_ids(con: sqlite3.Connection, *, trackers: bool = True) -> set[str]:
    """Evidence ids carrying human (or agent) work: a review, an operator
    brand association or association edit, an observation, a campaign link
    or check, a note, a saved capture, a citation (placement or target
    evidence) or a tracker member event. `trackers=False` leaves out the
    tracker part (alert events and silent alert baseline members): what an
    alert knows is not a person's label."""
    from .import_sources import MACHINE_ORIGINS_SQL, human_review_sql
    tables = _tables(con)
    ids: set[str] = set()
    history = db.has_history_tables(con)
    queries = [("SELECT e.id FROM evidence e WHERE EXISTS (" + human_review_sql("e.id") + ")")
               if history else "SELECT evidence_id FROM reviews",
               # PODS: model rows are display-only
               "SELECT evidence_id FROM brand_assocs WHERE origin<>'detector' AND basis<>'model'",
               "SELECT id FROM evidence WHERE observation_revision_id IS NOT NULL",
               "SELECT evidence_id FROM campaign_evidence",
               "SELECT evidence_id FROM campaign_checks WHERE evidence_id IS NOT NULL"]
    if history:
        queries.append("SELECT e.id FROM evidence e WHERE e.assoc_revision>0 AND NOT EXISTS"
                       " (SELECT 1 FROM association_history h WHERE h.evidence_id=e.id"
                       " AND h.association_revision=e.assoc_revision"
                       f" AND h.history_origin IN {MACHINE_ORIGINS_SQL})")
    else:
        queries.append("SELECT id FROM evidence WHERE assoc_revision>0")
    for sql in queries:
        ids.update(r[0] for r in con.execute(sql))
    refs = []
    for table in ("placement_evidence", "target_evidence", "customer_mistake_reports"):
        if table in tables:
            refs += [r[0] for r in con.execute(f"SELECT ref FROM {table}")]
    for _item, target, captured in _saved_refs(con):
        refs += [target, captured]
    if "source_states" in tables:
        ids.update(_ref_records(con, refs).values())
    if trackers and "customer_alert_events" in tables:
        ids.update(r[0][len("finding:"):] for r in con.execute(
            "SELECT logical_identity FROM customer_alert_events"
            " WHERE logical_identity LIKE 'finding:%'"))
    if trackers and "customer_alerts" in tables:
        # Silent baseline members: a tracker already knows the read, so a
        # span or transcript change must carry it, never re-fire.
        for (raw,) in con.execute("SELECT baseline_json FROM customer_alerts"):
            try:
                members = (json.loads(raw or "{}") or {}).get("members")
            except (ValueError, TypeError, AttributeError):
                continue
            if isinstance(members, dict):
                ids.update(k[len("finding:"):] for k in members if k.startswith("finding:"))
    return ids


def _active_rows(con) -> list[dict]:
    from .ad_reads import active_where
    where, args = active_where(con)
    return [dict(r) for r in con.execute(
        "SELECT e.id, e.source_record_key AS rkey, e.episode_id, e.orig_start_s AS start,"
        " e.orig_end_s AS end, e.source_version_id, e.created_at FROM evidence e"
        f" JOIN episodes ep ON ep.id=e.episode_id WHERE {where}"
        " AND e.source_record_key IS NOT NULL", args)]


def _active_version(con, version_id):
    row = con.execute("SELECT b.id FROM source_versions a JOIN source_versions b"
                      " ON b.source_key=a.source_key AND b.is_active=1 WHERE a.id=?",
                      (version_id,)).fetchone()
    return row[0] if row else None


def _retire(con, row: dict) -> None:
    """Hide one active row in its source's active version (membership)."""
    version = _active_version(con, row["source_version_id"])
    if version is None:
        return
    if row["source_version_id"] == version:
        con.execute("INSERT OR REPLACE INTO evidence_source_membership VALUES (?, ?, 0)",
                    (row["id"], version))
    else:
        con.execute("DELETE FROM evidence_source_membership WHERE evidence_id=?"
                    " AND source_version_id=?", (row["id"], version))


# DETBUILD (DBC-R3-02, DBS-R4-02): the record keys of the rows a DET rule
# adds, an R6 split part "<id>.split-<ms>-<ms>" and an R9 row
# "<ep>.adrules-r9.<ms>-<ms>". Each is a kind of its own: a part's work
# carries over only onto another part, an R9 row's only onto another R9
# row, never onto a read of another ad (a part lies inside its keeper,
# which is the other brand's ad). When the DET switch-off retires such a
# row, its work stays on it and shows again when the switch is back on.
DET_ROW_KINDS = ((".split-", "named-r6"), (".adrules-r9.", "named-r9"))


def kind_of(record_key: str) -> tuple[str, str | None]:
    """(kind prefix, second-brand key or None) of a stable record key; an R6
    part is kind "named-r6" and an R9 row "named-r9" (DET_ROW_KINDS)."""
    prefix = record_key.split(":", 1)[0]
    if prefix == "named":
        for marker, kind in DET_ROW_KINDS:
            if marker in record_key:
                return kind, None
    brand = None
    if prefix == "named" and ".b-" in record_key:
        brand = record_key.rsplit(".b-", 1)[1]
    return prefix, brand


def _iou(a_start, a_end, b_start, b_end) -> float:
    if None in (a_start, a_end, b_start, b_end):
        return 0.0
    inter = min(a_end, b_end) - max(a_start, b_start)
    union = max(a_end, b_end) - min(a_start, b_start)
    return inter / union if inter > 0 and union > 0 else 0.0


def reconcile(con: sqlite3.Connection, actor: str, human: set | None = None) -> dict:
    """Make every stable key's active row unique and move human work onto
    active rows (module docstring). Joins the caller's transaction; call
    when the load's evidence rows are final, before publishing. Returns
    {"pairs": [(old, new)], "touched": {new ids}, "report": {...}}; call
    repoint(con, pairs) after the touched rows are published."""
    ensure_schema(con)
    human = human_work_ids(con) if human is None else human
    active = _active_rows(con)
    # A row that is active again (an export reverted) is no longer retired:
    # its lineage goes, so its latest work carries when it is retired again.
    ids = [r["id"] for r in active]
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        con.execute("DELETE FROM evidence_lineage WHERE old_id IN (%s)"
                    % ",".join("?" * len(chunk)), chunk)
    done = {r[0] for r in con.execute("SELECT old_id FROM evidence_lineage")}
    pairs: list[tuple] = []
    touched: set[str] = set()
    counts = {"merged": 0, "carried_over": 0, "overlap_taken": 0, "orphaned": 0}
    merged_keys: list[str] = []
    conflicts: list[str] = []
    # 1. Exactly one active row per stable key: human work first, then newest.
    groups: dict[tuple, list] = {}
    for row in active:
        groups.setdefault((row["rkey"], row["episode_id"]), []).append(row)
    retired: set[str] = set()
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: (r["id"] in human, r["created_at"] or "", r["id"]), reverse=True)
        for row in rows[1:]:
            _retire(con, row)
            retired.add(row["id"])
    active = [r for r in active if r["id"] not in retired]
    by_key = {(r["rkey"], r["episode_id"]): r for r in active}
    by_episode: dict[str, list] = {}
    for row in active:
        by_episode.setdefault(row["episode_id"], []).append(row)
    active_ids = {r["id"] for r in active}
    # 2. Retired rows with human work and no lineage yet.
    old_rows = []
    pending = sorted(human - active_ids - done)
    for at in range(0, len(pending), 500):
        chunk = pending[at:at + 500]
        old_rows += [dict(r) for r in con.execute(
            "SELECT id, source_record_key AS rkey, episode_id, orig_start_s AS start,"
            " orig_end_s AS end FROM evidence WHERE source_record_key IS NOT NULL"
            " AND id IN (%s)" % ",".join("?" * len(chunk)), chunk)]
    spans = []
    for old in old_rows:
        twin = by_key.get((old["rkey"], old["episode_id"]))
        if twin is not None and (twin["start"], twin["end"]) == (old["start"], old["end"]):
            touched |= _move(con, old["id"], twin["id"], actor, "duplicate", 1.0, pairs,
                             conflicts, old["rkey"])
            counts["merged"] += 1
            merged_keys.append(old["rkey"])
            continue
        kind = kind_of(old["rkey"])
        for new in by_episode.get(old["episode_id"], []):
            if kind_of(new["rkey"]) != kind:
                continue
            overlap = _iou(old["start"], old["end"], new["start"], new["end"])
            if overlap >= IOU_MIN:
                spans.append((overlap, old["id"], new["id"], old["rkey"]))
    # 3. Carry-over: retired rows with a review or an operator brand change first
    # (a tracker's baseline alone never decides which read wins, so counts
    # do not depend on whether a tracker exists), then the largest overlap;
    # one old row per new row and back. A row that loses a read tries its
    # next-best one.
    edited = _edited_ids(con, {s[1] for s in spans})
    spans.sort(key=lambda s: (s[1] not in edited, -s[0], s[1], s[2]))
    taken_old, taken_new, not_carried = set(), set(), []
    contested: dict[str, tuple] = {}
    for overlap, old_id, new_id, rkey in spans:
        if old_id in taken_old:
            continue
        if new_id in taken_new:
            contested.setdefault(old_id, (new_id, overlap, rkey))
            continue
        taken_old.add(old_id)
        taken_new.add(new_id)
        touched |= _move(con, old_id, new_id, actor, "carried_over", overlap, pairs,
                         conflicts, rkey)
        counts["carried_over"] += 1
    for old_id, (new_id, overlap, rkey) in contested.items():
        if old_id in taken_old:
            continue
        # Final: every read it overlaps went to another retired read. Its
        # work stays on it and it never carries later (no flip on reload).
        _record(con, old_id, new_id, "overlap_taken", overlap)
        taken_old.add(old_id)
        not_carried.append(rkey)
        counts["overlap_taken"] += 1
    orphans = sorted({o["rkey"] for o in old_rows
                      if o["id"] not in taken_old and o["id"] not in {p[0] for p in pairs}})
    counts["orphaned"] = len(orphans)
    report = dict(counts)
    if orphans:
        report["orphaned_keys"] = orphans[:_REPORT_KEYS]
    if not_carried:
        report["overlap_taken_keys"] = sorted(set(not_carried))[:_REPORT_KEYS]
    if merged_keys:
        report["merged_keys"] = sorted(set(merged_keys))[:_REPORT_KEYS]
    if conflicts:
        report["conflicts"] = len(conflicts)
        report["conflict_keys"] = sorted(set(conflicts))[:_REPORT_KEYS]
    return {"pairs": pairs, "touched": touched, "report": report}


def _copy_rows(con, table: str, key: str, old: str, new: str, skip=("id",)) -> None:
    columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})") if r[1] not in skip]
    select = ", ".join("?" if c == key else c for c in columns)
    con.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) SELECT {select}"
                f" FROM {table} WHERE {key}=?", (new, old))


def _edited_ids(con, ids) -> set:
    """Rows among ids with a review, an operator brand association, or a
    brand change a person or agent made (the current association revision
    was not written by an import refresh or an untouched Undo), such as
    removing every brand."""
    from .import_sources import MACHINE_ORIGINS_SQL, human_review_sql
    history = db.has_history_tables(con)
    refreshed = (" AND NOT EXISTS (SELECT 1 FROM association_history h"
                 " WHERE h.evidence_id=evidence.id AND h.association_revision=evidence.assoc_revision"
                 f" AND h.history_origin IN {MACHINE_ORIGINS_SQL})"
                 if history else "")
    reviewed = (("SELECT id FROM evidence WHERE EXISTS (" + human_review_sql("evidence.id")
                 + ") AND id") if history else "SELECT evidence_id FROM reviews WHERE evidence_id")
    ids = sorted(ids)
    out = set()
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        marks = ",".join("?" * len(chunk))
        out.update(r[0] for r in con.execute(
            f"{reviewed} IN ({marks}) UNION"
            # PODS: model rows are display-only
            f" SELECT evidence_id FROM brand_assocs WHERE origin<>'detector'"
            f" AND basis<>'model' AND evidence_id IN ({marks}) UNION"
            f" SELECT id FROM evidence WHERE assoc_revision>0{refreshed} AND id IN ({marks})",
            chunk * 3))
    return out


def _later(a: tuple, b: tuple) -> bool:
    """Review order a is later than b: by review history sequence when both
    have one (a clock that went back cannot reorder judgments), else by
    updated_at."""
    if a[1] and b[1]:
        return a[1] > b[1]
    return a[0] > b[0]


def _review_order(con, evidence_id: str, review) -> tuple:
    """When a row's current review was made: (updated_at, sequence). The
    sequence is the review_history row of that write, or, for a review a
    move copied, its source's (evidence_review_order); 0 when unknown.
    Compare with _later."""
    if review is None:
        return ("", -1)
    row = con.execute("SELECT review_revision, seq FROM evidence_review_order"
                      " WHERE evidence_id=?", (evidence_id,)).fetchone()
    if row is not None and row[0] == review["revision"]:
        seq = row[1]
    elif db.has_history_tables(con):
        seq = con.execute("SELECT MAX(rowid) FROM review_history WHERE evidence_id=?"
                          " AND review_revision=?", (evidence_id, review["revision"])).fetchone()[0]
    else:
        seq = None
    return (review["updated_at"] or "", seq or 0)


def _human_assocs(con, evidence_id: str) -> set:
    return {tuple(r) for r in con.execute(
        "SELECT brand, basis, origin, channel FROM brand_assocs"
        # PODS: model rows are display-only
        " WHERE evidence_id=? AND origin<>'detector' AND basis<>'model'", (evidence_id,))}


def _attrs(con, evidence_id: str) -> dict:
    row = con.execute("SELECT attrs FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    try:
        attrs = json.loads((row[0] if row else None) or "{}")
    except (ValueError, TypeError):
        attrs = {}
    return attrs if isinstance(attrs, dict) else {}


def _brand_keys(aliases: dict, attrs: dict, brand, channel) -> set:
    """A brand's spelling and canonical keys: the alias table's, and the
    row's brand_canonical for its spoken brand."""
    from .ad_reads import brand_key, canonical
    keys = {brand_key(brand), canonical(aliases, brand)}
    if channel == "spoken" and attrs.get("brand_canonical") and brand_key(brand) == brand_key(
            attrs.get("brand_spoken") or attrs.get("brand")):
        keys.add(brand_key(attrs["brand_canonical"]))
    return keys - {""}


def _drop_removed_suggestions(con, old: str, new: str, actor: str, history: bool,
                              touched: set) -> None:
    """The namer's suggestions a person's brand change removed from the old
    row stay removed on the new row, which the load gave the namer's
    suggestions again. A suggestion is removed when neither its spelling
    nor its canonical key is on any of the old row's brands (the namer's or
    a person's); the new row's suggestions that match it by spelling or
    canonical key go (a reload may also respell it)."""
    from .ad_reads import alias_map
    from .import_sources import MACHINE_ORIGINS_SQL
    row = con.execute("SELECT assoc_revision FROM evidence WHERE id=?", (old,)).fetchone()
    if row is None or not row[0] or (history and con.execute(
            "SELECT 1 FROM association_history WHERE evidence_id=? AND association_revision=?"
            f" AND history_origin IN {MACHINE_ORIGINS_SQL}", (old, row[0])).fetchone()):
        return  # no person or agent changed the old row's brands
    aliases = alias_map(con)
    old_attrs, new_attrs = _attrs(con, old), _attrs(con, new)
    on_old = set()
    for brand, channel in con.execute(
            # PODS: model rows are display-only
            "SELECT brand, channel FROM brand_assocs WHERE evidence_id=? AND basis<>'model'",
            (old,)):
        on_old |= _brand_keys(aliases, old_attrs, brand, channel)
    removed = set()
    for attr, channel in (("brand_spoken", "spoken"), ("brand_url", "url"), ("brand_code", "code")):
        if old_attrs.get(attr):
            keys = _brand_keys(aliases, old_attrs, old_attrs[attr], channel)
            if not keys & on_old:
                removed |= keys
    drop = [r[0] for r in con.execute(
        "SELECT id, brand, channel FROM brand_assocs WHERE evidence_id=? AND origin='detector'",
        (new,)) if _brand_keys(aliases, new_attrs, r[1], r[2]) & removed]
    if not drop:
        return
    con.executemany("DELETE FROM brand_assocs WHERE id=?", [(i,) for i in drop])
    con.execute("UPDATE evidence SET assoc_revision=assoc_revision+1 WHERE id=?", (new,))
    if history:
        db.record_association_snapshot(con, new, actor, origin=ORIGIN)
    touched.add(new)


def _record(con, old: str, new: str, reason: str, overlap, judgment=None) -> None:
    if judgment is None:
        row = con.execute("SELECT judgment FROM reviews WHERE evidence_id=?", (old,)).fetchone()
        judgment = row[0] if row else None
    con.execute("INSERT OR REPLACE INTO evidence_lineage (old_id, new_id, reason, overlap,"
                " old_judgment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (old, new, reason, overlap, judgment, db.utcnow()))


def _move(con, old: str, new: str, actor: str, reason: str, overlap: float,
          pairs: list, conflicts: list, rkey: str) -> set:
    """Move one retired row's row-keyed human work onto the active row. The
    later review wins, and its brand associations go with it; differing
    judgments or operator brands are reported as conflicts."""
    history = db.has_history_tables(con)
    touched = set()
    old_review = con.execute("SELECT * FROM reviews WHERE evidence_id=?", (old,)).fetchone()
    new_review = con.execute("SELECT * FROM reviews WHERE evidence_id=?", (new,)).fetchone()
    old_brands, new_brands = _human_assocs(con, old), _human_assocs(con, new)
    if (old_review is not None and new_review is not None
            and old_review["judgment"] != new_review["judgment"]) \
            or (old_brands and new_brands and old_brands != new_brands):
        conflicts.append(rkey)
    old_wins = old_review is not None and (
        new_review is None
        or _later(_review_order(con, old, old_review), _review_order(con, new, new_review)))
    if old_wins:
        seq = _review_order(con, old, old_review)[1]
        columns = [k for k in old_review.keys() if k not in ("evidence_id", "revision")]
        if new_review is None:
            con.execute(f"INSERT INTO reviews (evidence_id, revision, {', '.join(columns)})"
                        f" VALUES (?, 1, {', '.join('?' * len(columns))})",
                        (new, *[old_review[c] for c in columns]))
        else:
            con.execute(f"UPDATE reviews SET {', '.join(c + '=?' for c in columns)},"
                        " revision=revision+1 WHERE evidence_id=?",
                        (*[old_review[c] for c in columns], new))
        revision = con.execute("SELECT revision FROM reviews WHERE evidence_id=?",
                               (new,)).fetchone()[0]
        con.execute("INSERT OR REPLACE INTO evidence_review_order VALUES (?, ?, ?)",
                    (new, revision, seq))
        if history:
            db.record_review_snapshot(con, new, actor, origin=ORIGIN)
        touched.add(new)
    # Operator brands: the winning review's, else the only side that has any.
    if old_brands and old_brands != new_brands and (old_wins or not new_brands):
        # PODS: model rows are display-only
        con.execute("DELETE FROM brand_assocs WHERE evidence_id=? AND origin<>'detector'"
                    " AND basis<>'model'", (new,))
        columns = [r[1] for r in con.execute("PRAGMA table_info(brand_assocs)")
                   if r[1] not in ("id",)]
        select = ", ".join("?" if c == "evidence_id" else c for c in columns)
        con.execute(f"INSERT INTO brand_assocs ({', '.join(columns)}) SELECT {select}"
                    " FROM brand_assocs WHERE evidence_id=? AND origin<>'detector'"
                    " AND basis<>'model'", (new, old))
        con.execute("UPDATE evidence SET assoc_revision=assoc_revision+1 WHERE id=?", (new,))
        if history:
            db.record_association_snapshot(con, new, actor, origin=ORIGIN)
        touched.add(new)
    if old_wins or not new_brands:
        _drop_removed_suggestions(con, old, new, actor, history, touched)
    if con.execute("SELECT 1 FROM campaign_evidence WHERE evidence_id=?", (old,)).fetchone():
        _copy_rows(con, "campaign_evidence", "evidence_id", old, new, skip=())
        con.execute("DELETE FROM campaign_evidence WHERE evidence_id=?", (old,))
        touched.add(new)
    if con.execute("UPDATE campaign_checks SET evidence_id=? WHERE evidence_id=?",
                   (new, old)).rowcount:
        touched.add(new)
    _record(con, old, new, reason, overlap, old_review["judgment"] if old_review else None)
    pairs.append((old, new))
    return touched


def repoint(con: sqlite3.Connection, pairs: list[tuple]) -> int:
    """Point ref-keyed human work of each old row at its new row's current
    publication: notes, saved captures' targets (the frozen capture keeps
    its own pin), citations on placements and targets, and tracker members
    and events (so the new row is not news to a tracker that had the old
    one). Joins the caller's transaction; call after publishing."""
    if not pairs:
        return 0
    from .citations import current_source_ref
    tables = _tables(con)
    new_ref: dict[str, str] = {}
    by_state: dict[str, str] = {}
    old_to_new = dict(pairs)
    for old, new in pairs:
        if new not in new_ref:
            new_ref[new] = current_source_ref(con, "evidence", new)
        for (state,) in con.execute("SELECT id FROM source_states WHERE kind='evidence'"
                                    " AND record_id=?", (old,)):
            by_state[state] = new
    moved = 0

    def swap(ref):
        state = _state_id(ref)
        if state not in by_state:
            return None
        return new_ref[by_state[state]] + ref[len(state):]
    if "customer_mistake_reports" in tables:
        for row_id, ref in con.execute("SELECT id, ref FROM customer_mistake_reports").fetchall():
            if swap(ref):
                con.execute("UPDATE customer_mistake_reports SET ref=? WHERE id=?", (swap(ref), row_id))
                moved += 1
    for table, key in (("placement_evidence", "placement_id"), ("target_evidence", "target_id")):
        if table not in tables:
            continue
        columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        for row in con.execute(f"SELECT * FROM {table}").fetchall():
            ref = swap(row["ref"])
            if not ref:
                continue
            values = [ref if c == "ref" else row[c] for c in columns]
            con.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(columns)})"
                        f" VALUES ({', '.join('?' * len(columns))})", values)
            con.execute(f"DELETE FROM {table} WHERE workspace_id=? AND {key}=? AND ref=?",
                        (row["workspace_id"], row[key], row["ref"]))
            moved += 1
    for item, target_ref, _captured in _saved_refs(con):
        ref = swap(target_ref)
        if ref:
            target = json.loads(con.execute("SELECT target_json FROM customer_saved_items"
                                            " WHERE id=?", (item,)).fetchone()[0])
            target["ref"] = ref
            con.execute("UPDATE customer_saved_items SET target_json=? WHERE id=?",
                        (json.dumps(target, sort_keys=True), item))
            moved += 1
    if "customer_alert_events" in tables:
        for old, new in pairs:
            moved += con.execute("UPDATE OR IGNORE customer_alert_events SET logical_identity=?"
                                 " WHERE logical_identity=?",
                                 (f"finding:{new}", f"finding:{old}")).rowcount
            # An event the update could not move duplicates one already there.
            con.execute("DELETE FROM customer_alert_events WHERE logical_identity=?",
                        (f"finding:{old}",))
        # One event per tracker, definition, kind and read: copies that both
        # fired leave the earliest event (unread if any copy was unread).
        for new in {n for _o, n in pairs}:
            groups: dict[tuple, list] = {}
            for row in con.execute("SELECT id, alert_id, definition_revision, kind, read_state"
                                   " FROM customer_alert_events WHERE logical_identity=?"
                                   " ORDER BY id", (f"finding:{new}",)).fetchall():
                groups.setdefault((row[1], row[2], row[3]), []).append(row)
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                con.execute("UPDATE customer_alert_events SET read_state=? WHERE id=?",
                            (min(r[4] for r in rows), rows[0][0]))
                con.executemany("DELETE FROM customer_alert_events WHERE id=?",
                                [(r[0],) for r in rows[1:]])
    if "customer_alerts" in tables:
        for row in con.execute("SELECT id, baseline_json FROM customer_alerts").fetchall():
            try:
                baseline = json.loads(row["baseline_json"] or "{}")
            except (ValueError, TypeError):
                continue
            members = baseline.get("members") if isinstance(baseline, dict) else None
            if not isinstance(members, dict):
                continue
            changed = False
            for old, new in old_to_new.items():
                member = members.pop(f"finding:{old}", None)
                if member is None:
                    continue
                changed = True
                if f"finding:{new}" not in members and isinstance(member, dict):
                    member = dict(member, ref=new_ref[new])
                    if isinstance(member.get("transition"), dict):
                        member["transition"] = dict(member["transition"], source_ref=new_ref[new])
                    members[f"finding:{new}"] = member
            if changed:
                con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=?",
                            (json.dumps(baseline, sort_keys=True), row["id"]))
                moved += 1
    return moved
