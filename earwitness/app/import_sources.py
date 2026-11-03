"""Supplied-corpus and upload import with validation, hashing, versions (stdlib)."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

from . import db

JEV_RUN_REL = "experiments/jev-ad-triage-20260921/results.json"
JEV_THRESHOLD = 0.80
DET_OUT_REL = "experiments/next-validation-detector/output/candidates_normalized.json"
# Named Jev spans from the corpus runner's name stage (candidate-shaped,
# "source": "jev+namer"). Optional: imported when present.
NAMED_OUT_REL = "experiments/named-reads/output/candidates_normalized.json"
EPISODES_REL = "data/episodes.json"
TRANSCRIPTS_DIR = "transcripts"
# Import-side ad rules (ADRULES-IMPORT): every load with named reads passes
# them through ad_rules.apply with DEFAULT_SWITCHES (_apply_ad_rules). The
# one off switch is this setting, read from the environment at each load
# (the service settings file /etc/earwitness/earwitness.env on the host):
# unset, empty or "on" = on; "off" = off, a load exactly as before the
# rules; any other value refuses the load. The rows the rules give are
# stored as their own source version under AD_RULES_SOURCE_KEY; the
# named-reads file's own version keeps the file as it is.
AD_RULES_ENV = "EARWITNESS_AD_RULES"
# DETBUILD: the boundary and address rules (ad_rules.DET_RULES: R6 split, R7
# end trim, R9 address route, R8 sign-off, R10 same ad, R11 said address) and
# the curated display list
# (earwitness/data/display-brands-det.json, which customer merges into the
# display list) load only behind this second setting: unset, empty or "off" =
# off (a load exactly as before it existed); "on" = on, with the rules on;
# any other value refuses the load. Read at each load.
DET_ENV = "EARWITNESS_AD_RULES_DET"
AD_RULES_SOURCE_KEY = "named-reads-ad-rules"
# DETBUILD-FIX2 (DBC-R2-01): what the display list merged at the last load
# with the rules on (customer.display_merge_state: "off" or the curated
# file's sha256), next to the rules cache index. A load with the rules off
# deletes it (customer merges nothing then); missing = "off".
AD_RULES_DISPLAY_META = "ad_rules_display_state"
# Incremental rules (ADI-R2-02): the meta row that indexes the stored rules
# rows by episode, so a load reuses an episode's rows from the previous
# load when nothing they depend on changed (_ad_rules_cache).
AD_RULES_CACHE_META = "ad_rules_cache"
# 2 (COLD b): the index also stores the checked key set and, per episode,
# the forms its rules run asked the checked list about (_asked_filter).
# 3 (DET-SPANS-2): and, per episode, how many input rows and how many added
# rows R10 left out of the stored rows.
AD_RULES_CACHE_FORMAT = 3
# CLIPGUARD R8 (CGS-R3-02): the meta row naming the rules version an R8-era
# load stored (_apply_ad_rules). The side-table rebuild trusts clip
# verdicts only from that version: a version a release before R8 stored
# may carry a named-reads file's raw clip_hidden through.
AD_RULES_R8_META = "ad_rules_r8_version"
# DET-SPANS-2 (ad_rules R10): the rows (record key, episode id) the last
# DET-on load left out as the same ad (a sorted JSON list of pairs; a row
# the load never stored has no evidence id yet). A later load that shows one
# of them (DET off, rules off, a row no longer marked) hands it to the alert
# carry as an old read, not news (seed_all). Deleted when empty and by a
# load where R10 did not run.
AD_RULES_SAME_AD_META = "ad_rules.same_ad_hidden"
# The report lists at most this many of the rows R10 left out
# (report["ad_rules"]["same_ad_rows"]: [candidate_id, same_ad_as] pairs).
SAME_AD_ROWS_CAP = 100
# The asked-forms filter: a Bloom filter, about 10 bits and 7 probes per
# form (about 1% false hits; a false hit only recomputes an episode).
_ASKED_BITS = 10
_ASKED_PROBES = 7


class ImportError(Exception):
    pass


def ad_rules_enabled(env=None) -> bool:
    """Whether loads apply the ad rules (AD_RULES_ENV in `env`, default the
    process environment). Raises ImportError on a value other than on/off,
    naming the setting and never the value (DBS-R2-05).
    Also refuses a bad value of the web's clip guard setting
    (ad_reads.clip_guard_setting, which never changes a load), so every
    host check that calls this one refuses it too (CLIPGUARD R8). The same
    for a bad value of DET_ENV (ad_rules_det_enabled)."""
    from . import ad_reads
    env = os.environ if env is None else env
    ad_rules_det_enabled(env)
    raw = env.get(AD_RULES_ENV)
    value = str(raw or "on").strip().lower() or "on"
    if value not in ("on", "off"):
        raise ImportError(f"{AD_RULES_ENV} must be on or off")
    try:
        ad_reads.clip_guard_setting(env)
    except ValueError as exc:
        raise ImportError(str(exc)) from None
    return value == "on"


def ad_rules_det_enabled(env=None) -> bool:
    """Whether loads turn on the DET rules (DET_ENV in `env`, default the
    process environment): unset, empty or off is False, on is True. Raises
    ImportError, naming the setting and not the value, on any other."""
    env = os.environ if env is None else env
    value = str(env.get(DET_ENV) or "").strip().lower()
    if value not in ("", "off", "on"):
        raise ImportError(f"{DET_ENV} must be on or off")
    return value == "on"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_dup_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ImportError(f"duplicate JSON key: {k!r}")
        obj[k] = v
    return obj


def load_json_strict(path: Path):
    try:
        return json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=_no_dup_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportError(f"{path}: unreadable JSON ({exc})")


def _finite_number(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImportError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ImportError(f"{name} must be finite")
    return float(value)


def _segments_text(transcript: dict) -> str:
    parts = []
    for s in transcript.get("segments", []) if isinstance(transcript, dict) else []:
        if isinstance(s, dict) and isinstance(s.get("text"), str):
            parts.append(s["text"])
    return " ".join(parts)


def canonical_transcript_text(transcript: dict) -> str:
    return " ".join(_segments_text(transcript).split())


def seed_all(repo: Path, con: sqlite3.Connection, actor: str = "seed",
             sources: dict | None = None) -> dict:
    """Validate everything first; then apply atomically. Idempotent replays
    reuse unchanged records and never reset reviews. `sources` optionally
    overrides the input locations (see seed_sources)."""
    rules_on = ad_rules_enabled()
    plan = _build_seed_plan(repo, sources)
    report = {"created": 0, "reused": 0, "rejected": 0, "unresolved": [],
              "episodes": 0, "source_files": 0, "jev_windows": 0, "detector_candidates": 0,
              "named_reads": 0}
    with db.transaction(con, write=True):  # caller-owned atomic transaction
        db.ensure_evidence_membership(con)  # servers seed without init_schema
        from . import corrections, lineage
        lineage.ensure_schema(con)
        human = lineage.human_work_ids(con)
        labels = lineage.human_work_ids(con, trackers=False)
        corrections.ensure_schema(con)
        # The episodes the workspace had before this load, with their pinned
        # transcript copies (alert carry, _rules_named_old_reads).
        had = dict(con.execute("SELECT id, transcript_version_id FROM episodes"))
        # The rows R10 left out at the last DET-on load (S03).
        prev_hidden = _stored_same_ad_hidden(con)
        # DETBUILD: the evidence the workspace had before this load, and the
        # namer's rows of the named-reads file it held (ADA-R2-02), for the
        # alert carry (_namer_same: _rules_named_old_reads, _rekeyed_old_reads,
        # _rules_changed_old_reads).
        before = {r[0] for r in con.execute("SELECT id FROM evidence")} if rules_on else set()
        before_named = _stored_named_rows(con) if rules_on and had else {}
        # DBC-R2-01: what the display list merged at the last rules-on load
        # and what it merges with this one. Only a load that changes it
        # re-keys old reads; the offer signatures alert members have under
        # the old merge are read now, before the load writes (alert_carry).
        merge = (_stored_merge_state(con), _customer().display_merge_state()) \
            if rules_on else ("off", "off")
        prior = None
        if merge[0] != merge[1] and con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='customer_alerts'").fetchone():
            # the old merge: nothing curated, or the curated file as it is
            # now; another file's cannot be recomputed (no refresh then)
            old = False if merge[0] == "off" else (
                True if merge[0] == _customer().curated_sha256() else None)
            if old is not None:
                from . import alert_carry
                prior = alert_carry.prior_offers(con, old)
        offers_before = _offer_fields(con) if prior else {}
        # DBS-R4-01: on such a load, the rule entries of the stored rules
        # rows, to find old reads whose rules changed (_rules_changed_old_reads).
        prev_rules = _rule_names(con) if merge[0] != merge[1] else {}
        # Episodes a corpus load holds: only the supplied kinds this path
        # writes (upload, transcript-import and feed come in by other paths;
        # a benchmark-unresolved episode is a transcript with no metadata
        # row, so a load without it is no takedown).
        loaded_before = {r[0] for r in con.execute(
            "SELECT id FROM episodes WHERE kind IN ('supplied', 'supplied-alias')")}
        # The R1b rows customers saw before it: a row it shows again is
        # carried like a row it wrote (ADI-R4-01).
        shown_rules = _rules_rows_shown(con) if rules_on else set()
        for sf in plan["source_files"]:
            row = con.execute("SELECT id FROM source_files WHERE sha256=? AND path=?",
                              (sf["sha256"], sf["path"])).fetchone()
            if row is None:
                con.execute("INSERT INTO source_files (kind, path, sha256, bytes, imported_at)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (sf["kind"], sf["path"], sf["sha256"], sf["bytes"], db.utcnow()))
                report["created"] += 1
            else:
                report["reused"] += 1
        report["source_files"] = len(plan["source_files"])
        held = _held_copies(con)
        for ver in plan["versions"]:
            cur = con.execute("SELECT id, sha256, is_active, version_no FROM source_versions"
                              " WHERE source_key=? AND sha256=?",
                              (ver["source_key"], ver["sha256"])).fetchone()
            pin = held.get(ver["source_key"])
            if cur and pin and cur["id"] != pin["id"] and cur["version_no"] < pin["version_no"]:
                # An older copy than the episode's current one: never re-pinned
                # or reactivated; its rows keep naming that copy.
                ver["db_id"] = cur["id"]
                plan["_kept_copies"][ver["source_key"][len("transcript:"):]] = pin["id"]
                report["reused"] += 1
                continue
            # Only real changes are written: an unchanged reload leaves the
            # version rows (and the research view state) untouched.
            con.execute("UPDATE source_versions SET is_active=0 WHERE source_key=? AND is_active=1"
                        " AND id IS NOT ?", (ver["source_key"], cur["id"] if cur else None))
            if cur:
                if not cur["is_active"]:
                    con.execute("UPDATE source_versions SET is_active=1, payload=? WHERE id=?",
                                (_version_payload(plan, ver), cur["id"]))
                ver["db_id"] = cur["id"]
                report["reused"] += 1
            else:
                version_no = con.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM"
                                         " source_versions WHERE source_key=?",
                                         (ver["source_key"],)).fetchone()[0]
                cur2 = con.execute(
                    "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                    " is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                    (ver["source_key"], version_no, ver["sha256"], _version_payload(plan, ver),
                     db.utcnow()))
                report["created"] += 1
                ver["db_id"] = cur2.lastrowid
        key_to_id = {}
        for ver in plan["versions"]:
            key_to_id[ver["source_key"]] = ver.get("db_id") or _version_id(con, ver["source_key"])
        for ep in plan["episodes"]:
            key, pin = f"transcript:{ep['id']}", held.get(f"transcript:{ep['id']}")
            if key not in key_to_id and pin:
                # The root offers no transcript for an episode that has one:
                # it keeps it, and its rows are identified by it.
                key_to_id[key] = pin["id"]
                plan["_transcript_digest"][ep["id"]] = pin["sha256"]
                plan["_kept_copies"][ep["id"]] = pin["id"]
        for ep in plan["episodes"]:
            if ep["id"] not in plan["_kept_copies"]:
                continue
            # Quotes, and the facts a copy gives its episode, from the kept copy.
            kept = _stored_transcript(con, plan["_kept_copies"][ep["id"]])
            # Membership in _extent also tells _rebuild_ad_reads to quote this
            # episode from its pinned copy (coverage skips kept copies).
            plan["_extent"][ep["id"]] = _segments_extent(kept.get("segments") or [])
            meta = _transcript_meta(kept)
            if ep["kind"] == "benchmark-unresolved":
                ep.update(show_title=meta["show_title"], ep_title=meta["ep_title"])
            if ep["id"] not in plan["_supplied_duration"]:
                ep["duration_min"] = meta["duration_min"]
        operator_aliases = _operator_aliases(con)
        for ep in plan["episodes"]:
            ep["transcript_version_id"] = plan["_kept_copies"].get(
                ep["id"], key_to_id.get(f"transcript:{ep['id']}"))
            if ep["id"] in operator_aliases:
                ep.update(operator_aliases[ep["id"]])
            _upsert_episode(con, ep, report, actor)
        report["episodes"] = len(plan["episodes"])
        # Takedown (Ask design 8.4, chat doc S4): an episode this load no
        # longer holds takes every Ask turn citing it, and the later turns
        # of its thread, with it, in this same transaction.
        gone = loaded_before - {ep["id"] for ep in plan["episodes"]}
        ask_removed = db.delete_ask_citing(con, gone)
        if ask_removed:
            report["ask_turns_removed"] = ask_removed
        # So does every meaning-alert match citing it (meaning alerts design 2).
        from . import meaning_schema
        meaning_removed = meaning_schema.delete_citing(con, gone)
        if meaning_removed:
            report["meaning_matches_removed"] = meaning_removed
        if plan["_kept_copies"]:
            report["kept_current_copy"] = len(plan["_kept_copies"])
        if plan["_named_loaded"]:
            # The ad rules, on the namer's rows and each episode's pinned
            # transcript copy (known only now), before the operator's rules.
            rules_version = _apply_ad_rules(con, plan, rules_on, report, labels)
            if rules_version is not None:
                key_to_id[AD_RULES_SOURCE_KEY] = rules_version
        # The operator's stoplist: the namer's corrected pick is the read's
        # machine brand from this load on (corrections module).
        corrections.apply_to_plan(con, plan)
        for ev in plan["evidence"]:
            ev["version_id"] = key_to_id.get(ev["version_key"])
        # Rows this load created or changed: the only evidence whose published
        # projection a load can move without moving its episode's.
        touched = plan["_touched"] = set()
        for ev in plan["evidence"]:
            transcript_id = key_to_id.get(f"transcript:{ev['episode']}")
            named = ev["id"].startswith("named:")
            spanned = named or ev["source"] == "jev"
            if spanned:
                # A named read or Jev window is identified by episode, span
                # and transcript version, never by its whole source file: a
                # Jev re-run keeps every unchanged window's row (and its
                # reviews); a named read's brand is a refreshable suggestion.
                content = _named_content(ev["id"], ev["episode"], ev["start"], ev["end"])
                identity = json.dumps([content, plan["_transcript_digest"].get(ev["episode"])])
            elif ev["source"] == "detector":
                # Identity comes from the candidate's evidence-defining
                # content and its episode's transcript, never from the whole
                # source file or volatile fields, so a re-run that changes
                # one episode keeps every other row (and its reviews).
                content = _detector_content(ev["id"], ev["episode"], ev["start"], ev["end"],
                                            ev["excerpt"], json.loads(ev["attrs"]))
                identity = json.dumps([content, plan["_transcript_digest"].get(ev["episode"])])
            else:
                content = None
                identity = json.dumps([ev["id"], next(v["sha256"] for v in plan["versions"]
                                                    if v["source_key"] == ev["version_key"]),
                                       plan["_transcript_digest"].get(ev["episode"])])
            eid = ev["id"].split(":")[0] + ":" + sha256_bytes(identity.encode())
            matches = []
            if content is not None:
                # Every row with equal content (same record, episode, transcript), in any
                # source version, incl. rows keyed before content identity existed. The
                # reviewed row wins, then the newest; the rest are duplicates.
                for row in con.execute(
                        "SELECT e.id, e.source_version_id, e.orig_start_s, e.orig_end_s,"
                        " e.excerpt, e.attrs FROM evidence e LEFT JOIN reviews r ON r.evidence_id=e.id"
                        " WHERE e.source_record_key=? AND e.episode_id=? AND e.transcript_version_id IS ?"
                        " ORDER BY r.evidence_id IS NULL, e.source_version_id DESC, e.created_at DESC",
                        (ev["id"], ev["episode"], transcript_id)).fetchall():
                    if content == (_named_content(ev["id"], ev["episode"], row["orig_start_s"],
                                                  row["orig_end_s"]) if spanned else
                                   _detector_content(ev["id"], ev["episode"], row["orig_start_s"],
                                                     row["orig_end_s"], row["excerpt"],
                                                     json.loads(row["attrs"]))):
                        matches.append(row)
            else:
                matches = con.execute("SELECT id, source_version_id FROM evidence WHERE"
                                      " source_record_key=? AND source_version_id=?"
                                      " AND transcript_version_id IS ?",
                                      (ev["id"], ev["version_id"], transcript_id)).fetchall()[:1]
            cur = matches[0] if matches else None
            if cur is None:
                con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                            " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                            " transcript_version_id, source_record_key)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                            (eid, ev["source"], ev["version_id"], ev["episode"],
                             ev["start"], ev["end"], ev["excerpt"], ev["attrs"], db.utcnow(),
                             transcript_id, ev["id"]))
                if ev["source"] == "detector":
                    attrs = json.loads(ev["attrs"])
                    for key, channel in (("brand_spoken", "spoken"), ("brand_url", "url"), ("brand_code", "code")):
                        brand = attrs.get(key)
                        if brand:
                            con.execute("INSERT INTO brand_assocs (evidence_id,brand,basis,origin,"
                                        "updated_at,channel) VALUES (?,?,'suggested','detector',?,?)",
                                        (eid, brand, db.utcnow(), channel))
                report["created"] += 1
                touched.add(eid)
            else:
                if content is not None:
                    cur = _set_membership(con, matches, ev["version_id"], human)
                    if named:
                        if _refresh_named_row(con, cur["id"], json.loads(ev["attrs"]),
                                              ev["excerpt"], actor):
                            report["named_refreshed"] = report.get("named_refreshed", 0) + 1
                            touched.add(cur["id"])
                    elif spanned:
                        pass  # a Jev window keeps the attrs it was first imported with
                    elif _refresh_display_attrs(con, cur["id"], json.loads(ev["attrs"]), actor):
                        report["display_refreshed"] = report.get("display_refreshed", 0) + 1
                        touched.add(cur["id"])
                report["reused"] += 1
            ev["imported_evidence_id"] = cur["id"] if cur is not None else eid
        # DET-SPANS-2 (ad_rules R10): a namer row this load leaves out as the
        # same ad as another row is hidden in the named-reads version, so a
        # row an earlier load showed leaves every list too. Its evidence row
        # (and any human work on it) stays; a load that loads it shows it
        # again (_set_membership).
        # The hide is scoped to the marked row's own episode and never takes
        # a row this load imported (DS2-S-r1-01).
        named_version = key_to_id.get("named-reads-output")
        imported = {ev["imported_evidence_id"] for ev in plan["evidence"]}
        hidden_now = set(plan.get("_same_ad_keys") or ())
        for key, ep_id in sorted(hidden_now):
            # `AND episode_id=?`: defence in depth behind the duplicate-id refusal.
            for row in con.execute("SELECT id, source_version_id FROM evidence"
                                   " WHERE source_record_key=? AND episode_id=?",
                                   (key, ep_id)).fetchall():
                if row["id"] in imported:   # defence in depth behind the duplicate-id refusal
                    continue
                if _is_visible(con, row, named_version):
                    _set_visible(con, row, named_version, False)
                    touched.add(row["id"])
        report["jev_windows"] = sum(1 for e in plan["evidence"] if e["id"].startswith("jev:"))
        if plan.get("jev_skipped"):
            report["jev_skipped"] = {"existing_jev_evidence": con.execute(
                "SELECT COUNT(*) FROM evidence WHERE source='jev'").fetchone()[0]}
        report["unresolved"] = [
            {"episode_id": ep["id"], "reason": "Benchmark transcript has no supplied metadata mapping"}
            for ep in plan["episodes"] if ep["kind"] == "benchmark-unresolved"]
        report["detector_candidates"] = sum(1 for e in plan["evidence"] if e["id"].startswith("det:"))
        report["named_reads"] = sum(1 for e in plan["evidence"] if e["id"].startswith("named:"))
        # One active row per stable key; human work on retired rows moves to
        # the active row it belongs to (lineage module docstring).
        # An imported Jev re-screen outlives a reseed (R4-01): carried before
        # reconcile, so its active rows (and their human work) are seen as
        # active, never orphaned (R5-02).
        from . import jev_rescreen
        carried = jev_rescreen.carry_forward(con, actor, rebuild=False)
        moved = lineage.reconcile(con, actor, human)
        touched |= moved["touched"]
        report["human_work"] = moved["report"]
        successor = dict(moved["pairs"])
        for ev in plan["evidence"]:
            while ev["imported_evidence_id"] in successor:
                ev["imported_evidence_id"] = successor[ev["imported_evidence_id"]]
        _rebuild_ad_reads(con, plan, report)
        if carried.get("carried") or carried.get("rows_active"):
            # The side tables after the carry, and the report counts them.
            from . import ad_reads as _ad_reads_mod
            report["ad_reads"] = _ad_reads_mod.rebuild_from_workspace(con)
            report["jev_rescreen_carried"] = {k: carried[k] for k in (
                "carried", "jev_run_version_id", "rows_active")}
        _record_seed_coverage(con, plan, key_to_id, actor)
        _publish_seed_sources(con, plan, actor)
        lineage.repoint(con, moved["pairs"])
        _reindex(con)
        from . import transcript_index
        report["transcript_index"] = transcript_index.index_versions(
            con, [ep.get("transcript_version_id") for ep in plan["episodes"]])
        if rules_on:
            # ADA-R1-02: the rules naming an old read is not news for alerts.
            same = _namer_same(plan, before_named)
            reads = _rules_named_old_reads(plan, had, touched, successor, shown_rules,
                                           before, same)
            # DBC-R2-01: the curated list re-keys old reads only on a load
            # that changes what the display list merges (the DET flip, its
            # switch-off, a new curated file), never on a routine load.
            listed = _rekeyed_old_reads(plan, had, successor, before, same) \
                if merge[0] != merge[1] else []
            rekeyed = [e for e in listed if e not in reads]
            if merge[0] != merge[1]:
                # DBS-R4-01: an old read whose rule entries this load changed
                # (the switch-off drops an R9 name or an R7 trim) is re-pointed
                # like one the rules wrote; on this load every read the rules
                # changed may also take the offer signature its attribution
                # change gives (refresh_offer, guarded by prior).
                reads = reads + [e for e in _rules_changed_old_reads(
                    plan, had, successor, before, same, prev_rules) if e not in reads]
                rekeyed = [e for e in listed if e not in reads]
            if (reads or listed) and con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='customer_alerts'").fetchone():
                from . import alert_carry
                via = _added_via(plan, reads)
                carried = alert_carry.carry_reads(
                    con, reads + rekeyed, dry_run=False, actor=actor,
                    origin={"origin": "import"}, repoint=True,
                    refresh_offer=_offer_unchanged(
                        con, listed + [e for e in reads if e not in via and e not in listed],
                        offers_before, successor),
                    seen_only=frozenset(e for e in reads + rekeyed if e not in via),
                    prior=prior, seen_via=via)
                report["alert_carry"] = {"reads": len(reads),
                                         "alerts_written": carried["written"]}
                if merge[0] != merge[1]:
                    report["alert_carry"]["rekeyed"] = len(rekeyed)
        if plan["_named_loaded"]:
            # DS2-S-r1-03: a row the last DET-on load hid and this load shows
            # again is an old read: carried into an alert that had seen its
            # episode's transcript copy, news to any other.
            reshown = _same_ad_reshown(con, prev_hidden, hidden_now, named_version)
            if reshown and con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='customer_alerts'").fetchone():
                from . import alert_carry
                alert_carry.carry_reads(
                    con, reshown, dry_run=False, actor=actor, origin={"origin": "import"},
                    repoint=True, seen_via={e: None for e in reshown})
            if reshown:
                report["same_ad_reshown"] = len(reshown)
            if hidden_now:
                _put_meta(con, AD_RULES_SAME_AD_META, json.dumps(sorted(hidden_now)))
            else:
                con.execute("DELETE FROM meta WHERE key=?", (AD_RULES_SAME_AD_META,))
        rev = db.bump_revision(con)
        db.audit(con, actor, "seed", "workspace", "seed", dict(report), rev)
    return report


def _stored_same_ad_hidden(con: sqlite3.Connection) -> list:
    """The (record key, episode id) pairs the last DET-on load left out as
    the same ad (AD_RULES_SAME_AD_META); [] when none or unreadable."""
    row = con.execute("SELECT value FROM meta WHERE key=?", (AD_RULES_SAME_AD_META,)).fetchone()
    try:
        pairs = json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        return []
    return [(p[0], p[1]) for p in pairs if isinstance(p, list) and len(p) == 2
            and all(isinstance(x, str) for x in p)] if isinstance(pairs, list) else []


def _same_ad_reshown(con: sqlite3.Connection, previous: list, hidden_now: set,
                     named_version) -> list:
    """The evidence ids of the rows R10 left out at the previous DET-on load
    (`previous`) that this load does not leave out and that are visible in
    the named-reads version now."""
    out: list = []
    for pair in previous:
        if pair in hidden_now:
            continue
        for row in con.execute("SELECT id, source_version_id FROM evidence"
                               " WHERE source_record_key=? AND episode_id=?", pair).fetchall():
            if row["id"] not in out and _is_visible(con, row, named_version):
                out.append(row["id"])
    return out


def _put_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _offer_fields(con: sqlite3.Connection) -> dict:
    """{evidence id: (offer, code, message)} from the attrs of every evidence
    row (all None for attrs that are not valid JSON)."""
    return {r[0]: tuple(r[1:]) for r in con.execute(
        "SELECT id, CASE WHEN json_valid(attrs) THEN json_extract(attrs, '$.offer') END,"
        " CASE WHEN json_valid(attrs) THEN json_extract(attrs, '$.code') END,"
        " CASE WHEN json_valid(attrs) THEN json_extract(attrs, '$.message') END"
        " FROM evidence")}


def _offer_unchanged(con: sqlite3.Connection, ids: list, before: dict,
                     successor: dict) -> frozenset:
    """The evidence ids (as this load leaves them) among `ids` whose offer
    fields now equal those of the id they had before the load (`before`,
    _offer_fields; successor maps an old id to its new one, in chains). An id
    with no entry before the load is not unchanged. The importer writes no
    offer fields from a named read today; this keeps an offer the load
    changed news if it ever does (alert_carry, refresh_offer)."""
    if not ids or not before:
        return frozenset()

    def final(eid):
        while eid in successor:
            eid = successor[eid]
        return eid
    was = {final(old): fields for old, fields in before.items()}
    now = _offer_fields(con)
    return frozenset(e for e in ids if e in was and e in now and now[e] == was[e])


def _rule_names_of(row) -> tuple:
    """The rules a stored rules row's entries name, in order, without R8's
    clip verdicts (which change no publication)."""
    from . import ad_rules
    prov = row.get("provenance") if isinstance(row, dict) else None
    return tuple(e.get("rule") for e in ad_rules._rules_list(prov) if isinstance(e, dict)
                 and not str(e.get("rule") or "").startswith("R8"))


def _rule_names(con: sqlite3.Connection) -> dict:
    """{record key: _rule_names_of} of the active rules rows (the last
    rules-on load's own output)."""
    from . import ad_reads
    rows = ad_reads.active_payload(con, AD_RULES_SOURCE_KEY)
    return {f"named:{r.get('candidate_id')}": _rule_names_of(r)
            for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}


def _rules_changed_old_reads(plan: dict, had: dict, successor: dict, before: set,
                             same: set, prev_rules: dict) -> list:
    """DBS-R4-01: the evidence ids (as this load leaves them) of old namer
    reads (the workspace had them, on an episode with the same pinned
    transcript copy, and the namer did not rename them) whose rule entries
    differ from those the last rules-on load stored (prev_rules)."""
    def final(eid):
        while eid in successor:
            eid = successor[eid]
        return eid
    rows = {f"named:{r.get('candidate_id')}": r for r in plan.get("_named_raw") or ()
            if isinstance(r, dict)}
    pinned = {ep["id"]: ep.get("transcript_version_id") for ep in plan["episodes"]}
    old = {final(e) for e in before}
    out: list = []
    for ev in plan["evidence"]:
        eid, row = ev.get("imported_evidence_id"), rows.get(ev["id"])
        if row is None or ev.get("version_key") == AD_RULES_SOURCE_KEY or eid not in old \
                or ev["id"] not in same or ev["episode"] not in had \
                or had[ev["episode"]] != pinned.get(ev["episode"]) or eid in out:
            continue
        if _rule_names_of(row) != prev_rules.get(ev["id"], ()):
            out.append(eid)
    return out


def _added_via(plan: dict, reads: list) -> dict:
    """{evidence id of a row the rules added, among reads: the evidence id of
    the read an R6 part was split from (its R6 entry's split_of), else None}
    (alert_carry.carry_reads seen_via, DBC-R3-01)."""
    eids = {ev["id"]: ev.get("imported_evidence_id") for ev in plan["evidence"]}
    rows = {f"named:{r.get('candidate_id')}": r for r in plan.get("_named_raw") or ()
            if isinstance(r, dict)}
    wanted = set(reads)
    out: dict = {}
    for ev in plan["evidence"]:
        eid = ev.get("imported_evidence_id")
        if ev.get("version_key") != AD_RULES_SOURCE_KEY or eid not in wanted:
            continue
        prov = (rows.get(ev["id"]) or {}).get("provenance")
        split = next((e.get("split_of") for e in (prov or {}).get("ad_rules") or ()
                      if isinstance(e, dict) and e.get("rule") == "R6" and e.get("split_of")),
                     None) if isinstance(prov, dict) else None
        out[eid] = eids.get(f"named:{split}") if split else None
    return out


def _customer():
    from . import customer
    return customer


def _stored_merge_state(con: sqlite3.Connection) -> str:
    """What the display list merged at the last load with the rules on
    (AD_RULES_DISPLAY_META; "off" when there is none)."""
    row = con.execute("SELECT value FROM meta WHERE key=?", (AD_RULES_DISPLAY_META,)).fetchone()
    return row[0] if row and row[0] else "off"


def _namer_same(plan: dict, before_named: dict) -> set:
    """The record keys of the file rows this load got (plan["_named_input"],
    before the rules) that equal the row the previous load stored
    (`before_named`, _stored_named_rows): a read the namer did not rename
    (DBS-R2-03) or change in any way (ADA-R2-02). Every carry route takes
    an old namer read only when it is in this set."""
    rows = {r.get("candidate_id"): r for r in plan.get("_named_input") or ()
            if isinstance(r, dict)}
    return {f"named:{cid}" for cid, row in rows.items()
            if cid in before_named and before_named[cid] == row}


def _rules_rows_shown(con: sqlite3.Connection) -> set:
    """The evidence ids an active AD_RULES_SOURCE_KEY version shows (its own
    pin unless retired, or importer membership): the R1b rows customers see."""
    return {r[0] for r in con.execute(
        "SELECT e.id FROM evidence e JOIN source_versions v ON v.id=e.source_version_id"
        " WHERE v.source_key=? AND v.is_active=1 AND NOT EXISTS (SELECT 1 FROM"
        " evidence_source_membership m WHERE m.evidence_id=e.id AND m.source_version_id=v.id"
        " AND m.member=0)"
        " UNION SELECT m.evidence_id FROM evidence_source_membership m JOIN source_versions v"
        " ON v.id=m.source_version_id WHERE v.source_key=? AND v.is_active=1 AND m.member=1",
        (AD_RULES_SOURCE_KEY, AD_RULES_SOURCE_KEY))}


def _stored_named_rows(con: sqlite3.Connection) -> dict:
    """The active named-reads version's rows by candidate_id (the namer's
    rows as stored, before any rule)."""
    from . import ad_reads
    rows = ad_reads.active_payload(con, ad_reads.NAMED_SOURCE_KEY)
    return {r.get("candidate_id"): r for r in rows or () if isinstance(r, dict)}


def _rules_named_old_reads(plan: dict, had: dict, touched: set, successor: dict,
                           shown_rules: set, before: set = frozenset(),
                           same: set = frozenset()) -> list:
    """The evidence ids (as this load leaves them) of the named reads the ad
    rules changed or added (an entry this load's apply added to
    provenance.ad_rules, or a row it added: plan["_rules_wrote"], never an
    entry a file row brought, DBS-R1-03) that this load
    wrote, or (an R1b row) showed again after a load with the rules off
    hid it (not in `shown_rules`, _rules_rows_shown before the load), on
    episodes the workspace had before it with the same pinned transcript
    copy. alert_carry.carry_reads takes them into alert baselines; a new
    episode's reads, or a changed transcript's, stay news. A namer row (not
    one the rules add) counts only when the workspace had it before this
    load (`before`, evidence ids, DBS-R2-07) and it is the row the previous
    load stored (`same`, _namer_same): the namer did not rename it
    (DBS-R2-03) or change it at all (ADA-R2-02). A rename, or a read the
    namer adds, is news even when a rule also wrote on the read."""
    def final(eid):
        while eid in successor:
            eid = successor[eid]
        return eid
    ruled = plan.get("_rules_wrote") or set()
    pinned = {ep["id"]: ep.get("transcript_version_id") for ep in plan["episodes"]}
    written = {final(e) for e in touched}
    shown = {final(e) for e in shown_rules}
    old = {final(e) for e in before}
    out: list = []
    for ev in plan["evidence"]:
        eid = ev.get("imported_evidence_id")
        added = ev.get("version_key") == AD_RULES_SOURCE_KEY
        shown_again = added and eid not in shown
        if ev["id"] in ruled and ev["episode"] in had and (eid in written or shown_again) \
                and had[ev["episode"]] == pinned.get(ev["episode"]) and eid not in out \
                and (added or (eid in old and ev["id"] in same)):
            out.append(eid)
    return out


def _rekeyed_old_reads(plan: dict, had: dict, successor: dict, before: set,
                       same: set) -> list:
    """DETBUILD (DBS-R1-01): the evidence ids (as this load leaves them) of
    the named reads that the curated display list re-keys, though no rule
    wrote them: reads the workspace had before this load (`before`, the
    evidence ids it held), on an episode it had with the same pinned
    transcript copy, that the namer did not rename or change (`same`,
    _namer_same, DBS-R2-03, ADA-R2-02), whose brand fields (brand, brand_raw, brand_spoken,
    brand_url, brand_code; letters and digits only) are keys or display
    names the curated file adds (customer.curated_keys, whatever the setting
    is). seed_all asks only on a load that changes what the display list
    merges (DBC-R2-01): such a read then starts (or stops) matching a brand
    alert, and its offer signature changes with its attribution brands.
    alert_carry.carry_reads takes it only into an alert that had seen it,
    and refreshes a member's offer only when the merge was the one change
    (seen_only, prior_merge). A new episode's reads, or a changed
    transcript's, stay news."""
    from . import ad_rules, customer
    keys = customer.curated_keys()
    if not keys:
        return []

    def final(eid):
        while eid in successor:
            eid = successor[eid]
        return eid
    rows = {f"named:{r.get('candidate_id')}": r for r in plan.get("_named_raw") or ()
            if isinstance(r, dict)}
    pinned = {ep["id"]: ep.get("transcript_version_id") for ep in plan["episodes"]}
    old = {final(e) for e in before}
    out: list = []
    for ev in plan["evidence"]:
        eid, row = ev.get("imported_evidence_id"), rows.get(ev["id"])
        if row is None or eid not in old or ev["id"] not in same or ev["episode"] not in had \
                or had[ev["episode"]] != pinned.get(ev["episode"]) or eid in out:
            continue
        if keys & {ad_rules.compact(row.get(f)) for f in (
                "brand", "brand_raw", "brand_spoken", "brand_url", "brand_code")}:
            out.append(eid)
    return out


def prepare_workspace(con: sqlite3.Connection, actor: str, repo: Path | None = None) -> dict:
    """What a load leaves, from the stored source versions (upgrade and
    restore of a workspace loaded before it existed, or by an older build):
    one active row per stable key with human work moved onto it
    (lineage.reconcile; the moved-to rows republished and refs repointed),
    the customer-ad side tables and the transcript index. Joins the
    caller's write transaction."""
    from . import ad_reads, citations, lineage, transcript_index
    moved = lineage.reconcile(con, actor)
    if db.has_v8_tables(con):
        digests: dict = {}
        for evidence_id in sorted(moved["touched"]):
            citations.publish_source_state(con, kind="evidence", record_id=evidence_id,
                                           actor=actor, digests=digests)
        lineage.repoint(con, moved["pairs"])
    out = ad_reads.rebuild_from_workspace(con, repo)
    out["human_work"] = moved["report"]
    transcript_index.ensure_schema(con)
    out["transcript_index"] = transcript_index.index_versions(
        con, [r[0] for r in con.execute("SELECT transcript_version_id FROM episodes")])
    return out


def ad_rules_checked(con: sqlite3.Connection, named: list):
    """(checked list, display list sha256, display digest, keys): the
    checked list the ad rules name brands from, customer.brand_checker's
    (the display list, corrections.known_keys and the advertiser list),
    with the export brand list and canon anchors this load's named reads
    give (corrections.known_keys reads them from the alias table, which the
    load rebuilds only after the rules), so a first load and a reload of
    the same rows check the same brands; the sha256 of the display list
    file it read; the digest of the display list's content (with that
    sha256, the incremental rules' load-wide part); and the sorted checked
    keys (the key set the index stores). The checked list is an _AskedList:
    it notes what the rules ask it, and gives the same list on other keys
    (with_keys). The only caller of the display list outside customer.py
    (test_display_brands)."""
    from . import ad_reads, ad_rules, corrections, customer
    rules = corrections.load(con)
    keys = set(corrections.known_keys(con, rules))
    # The advertiser list's own spellings (customer._advertiser_keys holds
    # their letters and digits): a rule writes the spelling it matched.
    try:
        doc = json.loads(customer._ADVERTISERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    for entry in doc.get("entries") or [] if isinstance(doc, dict) else []:
        if isinstance(entry, dict):
            keys.update(k for k in (entry.get("key"), entry.get("name"),
                                    *(entry.get("aliases") or [])) if isinstance(k, str) and k)
    aliases = corrections.alias_rows(rules, ad_reads.export_aliases(
        corrections.stoplist_reads(named, rules)))
    keys |= {raw for raw, row in aliases.items() if row[2] == "brand_list"}
    keys |= {row[1] for row in aliases.values() if row[1] and row[1] != row[0]}
    # Sorted: the first spelling of a key names it (CheckedList), the same
    # in every process.
    display, keys = customer._display_brands(), sorted(keys)
    # R11's own names beyond the display list: the advertiser list's keys and
    # names (not its aliases); load-wide, so in the digest
    own = advertiser_names(doc)
    digest = sha256_bytes(json.dumps({"display": display, "own_names": own}, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8"))
    return (_AskedList(lambda extra: ad_rules.CheckedList(display, extra, own), keys),
            customer._display_brands_sha256(), digest, keys)


def advertiser_names(doc) -> list:
    """The advertiser list's keys and names (letters and digits, sorted),
    never its aliases: the brands' own names R11 writes an address for
    (ad_rules.CheckedList own_names)."""
    from .ad_rules import compact
    out = set()
    for entry in doc.get("entries") or [] if isinstance(doc, dict) else []:
        if isinstance(entry, dict):
            out.update(compact(k) for k in (entry.get("key"), entry.get("name"))
                       if isinstance(k, str))
    out.discard("")
    return sorted(out)


class _AskedList:
    """A checked list (ad_rules.CheckedList on the load's display list and
    `keys`) that notes, while `asked` is a set, every form the rules ask it
    about: match(form), display_name(form) (R11), checked(key) (as letters
    and digits) and membership
    in `keys`. Those are the only ways ad_rules reads a checked list, and
    their answers on a form depend only on the display list (load-wide)
    and on whether a checked key has that form. So an episode whose rules
    asked about no form whose answer changed would run exactly as before
    (_ad_rules_stale)."""

    def __init__(self, make, keys: list):
        from .ad_rules import compact
        self.make, self.key_list, self.inner = make, keys, make(keys)
        self.asked: set | None = None
        self._compact = compact
        self.keys = _AskedKeys(self.inner.keys, self)

    def with_keys(self, keys: list):
        """The plain checked list on the same display list with `keys`."""
        return self.make(keys)

    def checked(self, key) -> bool:
        if self.asked is not None and isinstance(key, str):
            self.asked.add(self._compact(key))
        return self.inner.checked(key)

    def match(self, form: str):
        if self.asked is not None and isinstance(form, str):
            self.asked.add(form)
        return self.inner.match(form)

    def display_name(self, form: str) -> bool:
        if self.asked is not None and isinstance(form, str):
            self.asked.add(form)
        return self.inner.display_name(form)


class _AskedKeys(frozenset):
    """CheckedList.keys (self_promo.classify's known brands) noting each
    membership asked."""

    def __new__(cls, keys, owner):
        out = super().__new__(cls, keys)
        out._owner = owner
        return out

    def __contains__(self, key) -> bool:
        if self._owner.asked is not None and isinstance(key, str):
            self._owner.asked.add(key)
        return frozenset.__contains__(self, key)


def _apply_ad_rules(con: sqlite3.Connection, plan: dict, on: bool, report: dict,
                    human: set = frozenset()):
    """ADRULES-IMPORT, the one wiring point: the load's named reads through
    ad_rules.apply with DEFAULT_SWITCHES, one episode (and one decoded
    transcript copy, the one the episode now pins) at a time. The rows
    replace the plan's named rows and their evidence (new R1b rows join
    under the named-reads version); the named-reads file's version stays as
    planned. The rows are stored as the AD_RULES_SOURCE_KEY version, which
    ad_reads.loaded_named gives the side-table rebuild and the at-once rule
    refreshes. A new row (R1b, an R6 part, an R9 row) is no namer's: its evidence is pinned to that
    version, so it leaves customers' view as the namer's rows of an old file
    do, when a later load no longer gives it. Returns that version's id.
    Off: no rule runs, the version an earlier load stored is deactivated
    (the readers fall back to the file; its new rows leave), and the rules
    cache index is deleted, so the next load with the rules on is cold
    (ADI-R5-02: the index does not hold the episodes loaded while off).
    `human`: the evidence ids carrying human labels (lineage.human_work_ids
    with trackers=False: reviews, operator brands, observations, campaign
    links and checks, notes, saved items, citations, mistake reports; not
    alert baselines or events, not model names). A row R10 marks as the same
    ad is still loaded when its own read is one of them, or when the read it
    duplicates carries a person's contradicting label: a current `not_ad` or
    `unsure` review, or an operator brand association (origin not detector,
    basis not model) whose brand differs from the marked row's. An `ad`
    review, note, saved item, citation or campaign link on that read does
    not keep the duplicate."""
    if not on:
        con.execute("UPDATE source_versions SET is_active=0 WHERE source_key=? AND is_active=1",
                    (AD_RULES_SOURCE_KEY,))
        con.execute("DELETE FROM meta WHERE key=?", (AD_RULES_CACHE_META,))
        con.execute("DELETE FROM meta WHERE key=?", (AD_RULES_DISPLAY_META,))
        return None
    from . import ad_rules, customer
    # DET on: a file row that claims a rule's source (ad_rules.ADDED_SOURCES)
    # is not the namer's, only the rules add such rows. apply drops it too;
    # dropping it here keeps apply's output one row per input row (zip below).
    # DET off (DBC-R2-03): the load is as at base 19b18708, every file row
    # goes through apply and none is dropped (apply drops none either).
    det = ad_rules_det_enabled()
    raw = [row for row in plan["_named_raw"] if not (det and ad_rules.added(row))]
    plan["_named_input"] = raw   # the namer's rows, as _namer_same compares them
    checked, display_sha, switches, load_key = _ad_rules_load(con, raw)
    # DBC-R2-01: what the display list merges with this load (seed_all
    # compares it with the stored one before the load writes).
    stored = con.execute("SELECT value FROM meta WHERE key=?",
                         (AD_RULES_DISPLAY_META,)).fetchone()
    state = customer.display_merge_state()
    if stored is None:
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (AD_RULES_DISPLAY_META, state))
    elif stored[0] != state:
        con.execute("UPDATE meta SET value=? WHERE key=?", (state, AD_RULES_DISPLAY_META))
    at: dict = {}
    for i, row in enumerate(raw):
        at.setdefault(str(row.get("ep_id")), []).append(i)
    out, added = list(raw), {}
    eps = {ep["id"]: ep for ep in plan["episodes"]}
    versions = {e: ep.get("transcript_version_id") for e, ep in eps.items()}
    cache = _ad_rules_cache(con, load_key, checked)[0]
    index, reused = {}, 0
    # Human labels beat the mark (DS2-C-r1-08, DS2-S-r1-02, DS2-C-r2-01): a
    # marked row stays loaded, with its same_ad_as entry, when its own read
    # carries a human label (`own`), or when its same_ad_as read carries a
    # person's contradicting label (`judged`: a current not_ad or unsure
    # review; `brands`: an operator brand association that differs from the
    # marked row's shown brand).
    # An ad review, note, saved item, citation or campaign link on the
    # target keeps nothing (one ad, one row). An alert baseline member or
    # event is not a label: a hidden read leaves an alert silently (S03
    # carry). A model name (basis model) is no label either.
    own: set = set()
    judged: set = set()   # (record key, episode) of a read with a not_ad or unsure review
    brands: dict = {}     # (record key, episode) -> the brands a person gave a read
    if det:
        judged_ids = {r[0] for r in con.execute(
            "SELECT evidence_id FROM reviews WHERE judgment IN ('not_ad', 'unsure')")}
        brand_ids: dict = {}
        for evidence_id, brand in con.execute(
                "SELECT evidence_id, brand FROM brand_assocs"
                " WHERE origin<>'detector' AND basis<>'model'"):
            brand_ids.setdefault(evidence_id, set()).add(brand)
        if human or judged_ids or brand_ids:
            for r in con.execute("SELECT id, source_record_key, episode_id FROM evidence"
                                 " WHERE source_record_key LIKE 'named:%'"):
                if r[0] in human:
                    own.add((r[1], r[2]))
                if r[0] in judged_ids:
                    judged.add((r[1], r[2]))
                if r[0] in brand_ids:
                    brands.setdefault((r[1], r[2]), set()).update(brand_ids[r[0]])
    protected_eps = {e for _key, e in own | judged | set(brands)}
    dropped_stored = 0   # added rows R10 left out of a reused episode's stored rows
    for ep in sorted(set(at) | {e for e, v in versions.items() if v is not None}):
        idx = at.get(ep, [])
        version = versions.get(ep)
        key = _ad_rules_episode_key(con, [raw[i] for i in idx], version, eps.get(ep)) \
            if load_key is not None else None
        hit = cache.get(ep)
        if key is not None and hit is not None and hit[0] == key and \
                [r.get("candidate_id") for r in hit[1]] == [raw[i].get("candidate_id")
                                                            for i in idx] and \
                not (hit[4] and ep in protected_eps):
            rows, asked, stored_new = hit[1] + hit[2], hit[3], hit[4]
            reused += 1
            dropped_stored += stored_new
        else:
            stored_new = 0
            record = _stored_transcript(con, version) if version is not None else None
            checked.asked = set() if key is not None else None
            try:
                rows = ad_rules.apply([raw[i] for i in idx], {ep: record} if record else {},
                                      checked, switches=switches)
                asked = _asked_filter(checked.asked) if key is not None else None
            finally:
                checked.asked = None
        for i, row in zip(idx, rows):
            out[i] = row
        if len(rows) > len(idx):
            added[ep] = rows[len(idx):]
        if key is not None:
            index[ep] = [key, len(idx), len(rows) - len(idx), asked, 0, stored_new]
    # As one apply over every episode orders them: the rows, then the new
    # rows by episode.
    out += [row for ep in sorted(added) for row in added[ep]]
    # DET-SPANS-2 (ad_rules R10): a row the rules mark as the same ad as
    # another row is not loaded (one ad, one row). The stored rows and the
    # cache index hold only the loaded rows, and the index entry counts the
    # rows left out: an episode that had such an input row recomputes on
    # every load (its cached rows no longer match its input), every other
    # episode reuses as before, and its left-out added rows (dropped_stored)
    # still count in the report.
    same_ad = kept_human = 0
    plan["_same_ad_keys"] = set()
    # DBS-R1-08: one evidence row per id; a file row cannot take the id of a
    # row the rules add. DET on: any shared id refuses, whatever R10 marks
    # (DS2-S-r1-01: checked on every row, before the marked ones leave).
    # DET off (DBC-R2-03): the refusal holds only when a shared id is one of
    # the rows this apply added; file rows that share an id load as at base.
    ids = [row.get("candidate_id") for row in out if isinstance(row, dict)]
    if len(set(ids)) != len(ids):
        made = {row.get("candidate_id") for rows in added.values() for row in rows}
        seen: dict = {}
        for i in ids:
            seen[i] = seen.get(i, 0) + 1
        shared = {i for i, n in seen.items() if n > 1}
        if det or shared & made:
            raise ImportError("named reads: two rows share one candidate_id after the ad rules")
    n_out = len(out)   # the rows before R10 leaves any out
    dropped: list = []
    if det:
        n_in = len(raw)

        def marked(row) -> bool:
            if not (isinstance(row, dict) and row.get("same_ad_as")):
                return False
            ep = str(row.get("ep_id"))
            if (f"named:{row.get('candidate_id')}", ep) in own:
                return False
            target = (f"named:{row.get('same_ad_as')}", ep)
            if target in judged:
                return False
            given = brands.get(target)
            shown = ad_rules._shown(row, checked)
            # DS2-S-r3-06: a duplicate that shows no brand is not contradicted by a person's brand on its target
            if not given or not shown:
                return True
            return any(ad_rules._same_brand(b, shown, checked) for b in given)
        keep = [not marked(row) for row in out]
        kept_human = sum(1 for row, k in zip(out, keep) if k and isinstance(row, dict)
                         and row.get("same_ad_as"))
        same_ad = keep.count(False) + dropped_stored
        dropped = sorted(([row.get("candidate_id"), row.get("same_ad_as")]
                          for row, k in zip(out, keep) if not k),
                         key=lambda pair: [str(x) for x in pair])
        # The namer's rows left out: seed_all hides the rows an earlier
        # load showed (a rule-made row leaves with its rules version), in
        # their own episode.
        plan["_same_ad_keys"] = {(f"named:{row.get('candidate_id')}", str(row.get("ep_id")))
                                 for row, k in zip(out[:n_in], keep[:n_in]) if not k}
        gone_new: dict = {}
        kept_new: dict = {}
        for row, k in zip(out[n_in:], keep[n_in:]):
            by = kept_new if k else gone_new
            by[str(row.get("ep_id"))] = by.get(str(row.get("ep_id")), 0) + 1
        for ep, entry in index.items():
            entry[1] = sum(1 for i in at.get(ep, []) if keep[i])
            entry[4] = len(at.get(ep, [])) - entry[1]
            entry[2] = kept_new.get(ep, 0)
            entry[5] += gone_new.get(ep, 0)
        if not all(keep):
            out = [row for row, k in zip(out, keep) if k]
        rows_kept = sum(keep[:n_in])
    else:
        rows_kept = len(raw)
    # Each rule entry records the display list (its sha256) the checked
    # list used (lead, ADRULES-IMPORT guard sign-off).
    out = [_with_display_sha(row, display_sha) for row in out]
    # DBS-R1-03: the rows this apply wrote rule entries on (more entries than
    # the file row had) or added; a file row's own entry is not trusted.
    had_entries = {}
    for row in raw:
        had_entries[row.get("candidate_id")] = max(
            had_entries.get(row.get("candidate_id"), 0),
            len(ad_rules._rules_list(row.get("provenance"))))
    plan["_rules_wrote"] = {
        f"named:{row['candidate_id']}" for row in out
        if row.get("candidate_id") not in had_entries
        or len(ad_rules._rules_list(row.get("provenance"))) > had_entries[row["candidate_id"]]}
    plan["_named_raw"] = out
    plan["evidence"] = [ev for ev in plan["evidence"] if not ev["id"].startswith("named:")]
    _add_candidates(plan, out, "named", "named-reads-output")
    # Only rows this apply made (added: appended by apply, or replayed from
    # the rules cache) are rules-made (POL-S1-01): a file row claiming a
    # rule's source stays the namer's. DET on: every rule source's row
    # (with DET on the file's are dropped above). DET off: R1b only, as
    # at base.
    new_keys = {f"named:{row['candidate_id']}" for rows in added.values() for row in rows
                if (ad_rules.added(row) if det else row.get("source") == ad_rules.R1B_SOURCE)}
    for ev in plan["evidence"]:
        if ev["id"] in new_keys:
            ev["version_key"] = AD_RULES_SOURCE_KEY
    payload = json.dumps(out)
    digest = sha256_bytes(payload.encode("utf-8"))
    cur = con.execute("SELECT id, is_active FROM source_versions WHERE source_key=? AND sha256=?",
                      (AD_RULES_SOURCE_KEY, digest)).fetchone()
    con.execute("UPDATE source_versions SET is_active=0 WHERE source_key=? AND is_active=1"
                " AND id IS NOT ?", (AD_RULES_SOURCE_KEY, cur["id"] if cur else None))
    if cur is None:
        version_no = con.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM source_versions"
                                 " WHERE source_key=?", (AD_RULES_SOURCE_KEY,)).fetchone()[0]
        con.execute("INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                    " is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                    (AD_RULES_SOURCE_KEY, version_no, digest, payload, db.utcnow()))
    elif not cur["is_active"]:
        con.execute("UPDATE source_versions SET is_active=1 WHERE id=?", (cur["id"],))
    version_id = cur["id"] if cur is not None else con.execute(
        "SELECT id FROM source_versions WHERE source_key=? AND sha256=?",
        (AD_RULES_SOURCE_KEY, digest)).fetchone()[0]
    stamp = str(version_id)
    if switches.get("R8") and con.execute(
            "UPDATE meta SET value=? WHERE key=? AND value IS NOT ?",
            (stamp, AD_RULES_R8_META, stamp)).rowcount == 0:
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (AD_RULES_R8_META, stamp))
    if load_key is not None:
        _put_ad_rules_cache(con, {"format": AD_RULES_CACHE_FORMAT, "load": load_key,
                                  "keys": checked.key_list, "sha256": digest,
                                  "rows_in": rows_kept, "episodes": index})
    entries: dict = {}
    for row in out:
        for entry in (row.get("provenance") or {}).get("ad_rules") or ():
            entries[entry["rule"]] = entries.get(entry["rule"], 0) + 1
    report["ad_rules"] = {"rows_in": len(raw), "rows_out": len(out),
                          "new_rows": n_out - len(raw) + dropped_stored,
                          "rule_entries": entries,
                          **({"same_ad": same_ad, "same_ad_kept_human": kept_human,
                              "same_ad_rows": dropped[:SAME_AD_ROWS_CAP],
                              "same_ad_rows_total": len(dropped)} if det else {}),
                          "sha256": digest, "display_list_sha256": display_sha,
                          "episodes_reused": reused,
                          "episodes_applied": len(set(at) | {e for e, v in versions.items()
                                                             if v is not None}) - reused}
    return version_id


# Incremental rules (ADI-R2-02). An episode's rules rows are a pure
# function of its raw named rows (in order), its pinned transcript copy and
# its show identity (the episode-level cache key), and of the checked list,
# the switches and the rules code. The load-wide key holds the rules code,
# the Python version, the switches and the display list (content and
# sha256). A load reuses the rows the previous load stored (the
# AD_RULES_SOURCE_KEY version the AD_RULES_CACHE_META index names) for an
# episode whose keys are unchanged; it recomputes every other episode, and
# every episode when the load-wide key changed. COLD b: the checked keys
# are not load-wide. The index stores the key set and, per episode, the
# forms its rules run asked the checked list about; when the keys change,
# an episode that asked about an added or removed key's form recomputes
# (_ad_rules_stale), and every other episode reuses its rows. The index is
# written only when it changes; a load with the rules off deletes it, so
# the next load with the rules on is cold.

def _ad_rules_code_version():
    """sha256 of the rules code: the source of ad_rules.py, the pipeline
    modules it calls (brand_namer, self_promo), the gazetteer they read and
    this module (the wiring around apply), and the source of the two
    ad_reads functions the rules call (brand_key, and the fold it calls;
    not the rest of ad_reads.py) and the two customer functions behind the
    exact match (normalize_token, and the _strip_accents it calls; not the
    rest of customer.py), and the curated list R11 reads
    (ad_rules.SAID_REPAIRS_PATH). None when a source cannot be read: then
    no load uses or writes the cache."""
    import inspect
    from earwitness import gazetteer
    from earwitness.pipeline import brand_namer, self_promo
    from . import ad_reads, ad_rules, customer
    h = hashlib.sha256()
    for module in (ad_rules, brand_namer, self_promo, gazetteer, sys.modules[__name__]):
        try:
            data = Path(module.__file__).read_bytes()
        except (OSError, TypeError, AttributeError):
            return None
        h.update(module.__name__.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0")
        h.update(data)
    try:
        data = ad_rules.SAID_REPAIRS_PATH.read_bytes()
    except (OSError, TypeError, AttributeError):
        return None
    h.update(b"said-address-repairs\0" + str(len(data)).encode() + b"\0")
    h.update(data)
    for module, name in ((ad_reads, "brand_key"), (ad_reads, "fold"),
                         (customer, "normalize_token"), (customer, "_strip_accents")):
        try:
            data = inspect.getsource(getattr(module, name)).encode("utf-8")
        except (OSError, TypeError, AttributeError):
            return None
        h.update(f"{module.__name__}.{name}\0{len(data)}\0".encode("utf-8"))
        h.update(data)
    return h.hexdigest()


def _ad_rules_load(con: sqlite3.Connection, named: list):
    """(checked list, display list sha256, switches, load-wide key) of a
    load of the named-reads rows `named` (as parsed) into `con`; the key is
    None when it cannot be computed (the load then uses no cache)."""
    from . import ad_rules
    checked, display_sha, display_digest, _keys = ad_rules_checked(con, named)
    switches = dict(ad_rules.DEFAULT_SWITCHES)
    if ad_rules_det_enabled():
        switches.update({rule: True for rule in ad_rules.DET_RULES})
    return checked, display_sha, switches, _ad_rules_load_key(switches, display_digest,
                                                              display_sha)


def ad_rules_load_key(con: sqlite3.Connection, named: list):
    """The load-wide incremental-rules key that a load of the named-reads
    rows `named` (the file as parsed) into `con` would compute and store,
    or None when it cannot be computed. Read-only. The nightly estimate
    (scripts/pilot_sync.py) does not call it: it calls ad_rules_recompute,
    which checks this key and the checked-key change the way the load
    does."""
    return _ad_rules_load(con, named)[3]


def ad_rules_recompute(con: sqlite3.Connection, named: list):
    """How many of the stored episodes a load of the named-reads rows
    `named` (the file as parsed) into `con` would recompute because the
    checked keys changed, or because an input row of theirs was left out as
    the same ad (R10): 0 when the stored cache is warm for it; None when
    the load is cold (no usable stored cache, another load-wide key, a
    missing or unreadable key set) or the count cannot be computed.
    Read-only. The nightly estimate (scripts/pilot_sync.py) charges these
    episodes at the cold rate; the count is the load's own
    (_ad_rules_cache), except that it does not count an episode the load
    recomputes because a protected label appeared (protected_eps)."""
    checked, _sha, _switches, load_key = _ad_rules_load(con, named)
    stale = _ad_rules_cache(con, load_key, checked)[1]
    return None if stale is None else len(stale)


def ad_rules_stored_load_key(con: sqlite3.Connection):
    """The load-wide key the workspace's stored rules cache index names
    (the AD_RULES_CACHE_META row), when that index and the rows it indexes
    are stored; else None. Read-only."""
    row = con.execute("SELECT value FROM meta WHERE key=?", (AD_RULES_CACHE_META,)).fetchone()
    try:
        index = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None
    if not isinstance(index, dict) or index.get("format") != AD_RULES_CACHE_FORMAT or \
            not isinstance(index.get("load"), str) or not isinstance(index.get("sha256"), str):
        return None
    if con.execute("SELECT 1 FROM source_versions WHERE source_key=? AND sha256=?",
                   (AD_RULES_SOURCE_KEY, index["sha256"])).fetchone() is None:
        return None
    return index["load"]


def _ad_rules_load_key(switches: dict, display_digest: str, display_sha):
    code = _ad_rules_code_version()
    if code is None:
        return None
    return sha256_bytes(json.dumps({
        "format": AD_RULES_CACHE_FORMAT, "code": code, "python": list(sys.version_info[:3]),
        "switches": sorted(switches.items()), "display": display_digest,
        "display_list_sha256": display_sha}, separators=(",", ":")).encode("utf-8"))


def _ad_rules_episode_key(con: sqlite3.Connection, rows: list, version, episode):
    """The episode's cache key: its raw named rows in load order (their
    fields in file order too, which the stored rows keep), its pinned
    transcript copy (version id and digest) and its show identity. None
    (always recompute) when a row is not plain JSON."""
    copy = None
    if version is not None:
        found = con.execute("SELECT sha256 FROM source_versions WHERE id=?",
                            (version,)).fetchone()
        copy = [version, found[0] if found else None]
    show = (episode or {}).get("show_title")
    try:
        text = json.dumps([rows, copy, show], separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return sha256_bytes(text.encode("utf-8"))


def _ad_rules_cache(con: sqlite3.Connection, load_key, checked) -> tuple:
    """({episode: (key, its rules rows, its new rows, its asked filter)},
    stale) from the previous load's stored rows, when the index names this
    load-wide key, the stored version still has the rows it indexed and
    the stored key set is readable. `stale`: the indexed episodes the load
    may not reuse (their stored rows do not match the index, or their rules
    asked about a form the checked list `checked` answers differently from
    the stored key set's; _ad_rules_stale). Else ({}, None): every episode
    recomputes. Read-only: the load and the nightly estimate
    (ad_rules_recompute) both use it."""
    if load_key is None:
        return {}, None
    row = con.execute("SELECT value FROM meta WHERE key=?", (AD_RULES_CACHE_META,)).fetchone()
    try:
        index = json.loads(row[0]) if row else None
        if not isinstance(index, dict) or index.get("format") != AD_RULES_CACHE_FORMAT \
                or index.get("load") != load_key:
            return {}, None
        stored = con.execute("SELECT payload FROM source_versions WHERE source_key=?"
                             " AND sha256=?", (AD_RULES_SOURCE_KEY, index["sha256"])).fetchone()
        if stored is None or sha256_bytes(stored[0].encode("utf-8")) != index["sha256"]:
            return {}, None
        rows, rows_in = json.loads(stored[0]), index["rows_in"]
        if not isinstance(rows, list) or not isinstance(rows_in, int) or \
                not 0 <= rows_in <= len(rows):
            return {}, None
        changed = _ad_rules_changed(index.get("keys"), checked)
        if changed is None:
            return {}, None
        first: dict = {}
        new: dict = {}
        for i, r in enumerate(rows):
            (first if i < rows_in else new).setdefault(str(r.get("ep_id")), []).append(r)
        out, stale, hashed = {}, set(), [_asked_hash(f) for f in changed]
        for ep, entry in index["episodes"].items():
            key, n_first, n_new, asked, gone_first, gone_new = entry
            f, n = first.get(ep, []), new.get(ep, [])
            # an episode with an input row R10 left out cannot reuse its
            # rows (they no longer match its input): it recomputes
            if len(f) == n_first and len(n) == n_new and not gone_first \
                    and not _ad_rules_stale(asked, hashed):
                out[ep] = (key, f, n, asked, gone_new)
            else:
                stale.add(ep)
        return out, stale
    except (TypeError, ValueError, KeyError, AttributeError):
        return {}, None


def _ad_rules_changed(stored_keys, checked):
    """The forms the checked list `checked` answers differently from the
    same display list with the stored checked keys: those of the keys added
    or removed (as letters and digits) whose match() or membership in keys
    differs. None (cold) when the stored key set is missing, unreadable or
    not sorted (the load stores it sorted), or when one side has no keys at
    all (self_promo.classify reads that)."""
    if not isinstance(stored_keys, list) or not all(isinstance(k, str) for k in stored_keys) \
            or stored_keys != sorted(stored_keys):
        return None
    if stored_keys == checked.key_list:
        return set()
    before, after = checked.with_keys(stored_keys), checked.inner
    if bool(before.keys) != bool(after.keys):
        return None
    forms = {checked._compact(k) for k in set(stored_keys) ^ set(checked.key_list)}
    return {f for f in forms if before.match(f) != after.match(f)
            or (f in before.keys) != (f in after.keys)}


def _ad_rules_stale(asked, changed: list) -> bool:
    """Whether an episode whose rules run asked the forms in the filter
    `asked` (_asked_filter) may answer differently now: it asked about a
    changed form (`changed`: their _asked_hash), or its filter is
    unreadable."""
    if not changed:
        return False
    if not isinstance(asked, str):
        return True
    try:
        bits = base64.b64decode(asked, validate=True)
    except ValueError:
        return True
    return bool(bits) and any(_asked_has(bits, h) for h in changed)


def _asked_hash(form: str) -> tuple:
    d = hashlib.blake2b(form.encode("utf-8", "surrogatepass"), digest_size=16).digest()
    return int.from_bytes(d[:8], "little"), int.from_bytes(d[8:], "little") | 1


def _asked_positions(h: tuple, size: int):
    a, b = h
    return [(a + i * b) % size for i in range(_ASKED_PROBES)]


def _asked_filter(forms) -> str:
    """The forms an episode's rules run asked the checked list about, as a
    Bloom filter (base64; "" when it asked nothing)."""
    if not forms:
        return ""
    size = max(64, -(-len(forms) * _ASKED_BITS // 8) * 8)
    bits = bytearray(size // 8)
    for form in forms:
        for p in _asked_positions(_asked_hash(form), size):
            bits[p >> 3] |= 1 << (p & 7)
    return base64.b64encode(bytes(bits)).decode("ascii")


def _asked_has(bits: bytes, h: tuple) -> bool:
    return all(bits[p >> 3] >> (p & 7) & 1 for p in _asked_positions(h, len(bits) * 8))


def _put_ad_rules_cache(con: sqlite3.Connection, index: dict) -> None:
    value = json.dumps(index, sort_keys=True, separators=(",", ":"))
    row = con.execute("SELECT value FROM meta WHERE key=?", (AD_RULES_CACHE_META,)).fetchone()
    if row is None:
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (AD_RULES_CACHE_META, value))
    elif row[0] != value:
        con.execute("UPDATE meta SET value=? WHERE key=?", (value, AD_RULES_CACHE_META))


def _with_display_sha(row: dict, sha) -> dict:
    """`row` with display_list_sha256 on each provenance["ad_rules"] entry
    (a copy when it has entries; a row the rules left alone as it is)."""
    prov = row.get("provenance")
    entries = prov.get("ad_rules") if isinstance(prov, dict) else None
    if not entries:
        return row
    return dict(row, provenance=dict(prov, ad_rules=[
        dict(e, display_list_sha256=sha) if isinstance(e, dict) else e for e in entries]))


def _rebuild_ad_reads(con: sqlite3.Connection, plan: dict, report: dict) -> None:
    """Rebuild the customer-ad side tables from this load (ad_reads.rebuild):
    the brand alias table, supporting links by stable record key, per-read
    display facts, clips and screening status."""
    from . import ad_reads
    record_ids = {ev["id"]: ev["imported_evidence_id"] for ev in plan["evidence"]}
    windows = [{"key": ev["id"], "episode": ev["episode"], "start": ev["start"],
                "end": ev["end"], "excerpt": ev["excerpt"]}
               for ev in plan["evidence"] if ev["id"].startswith("jev:")]
    # Quotes come from the copy each episode now pins: the one this load
    # read, or the kept copy. Decoded from the stored version one episode
    # at a time, never held for the whole load.
    segments = ad_reads.StoredSegments(con, {
        ep["id"]: ep.get("transcript_version_id") for ep in plan["episodes"]
        if ep["id"] in plan["_extent"]})
    report["ad_reads"] = ad_reads.rebuild(
        con, episodes=plan.get("_episodes_raw") or [], named=plan.get("_named_raw") or [],
        det=plan.get("_det_raw") or [], jev_windows=windows, segments=segments,
        record_ids=record_ids, export_root=plan.get("_export_root"),
        jev_screened=plan.get("_jev_screened") or set(), clips=plan.get("_clips"))


def _publish_seed_sources(con: sqlite3.Connection, plan: dict, actor: str) -> dict:
    """Publish imported episodes/evidence in the importer's transaction.

    Runs after final component writes, before commit: new or changed
    episodes and created evidence rows get immutable source states.
    Unchanged reimports reuse existing IDs. Joins the caller's import
    transaction; no separate connection or commit.
    """
    from . import citations as _citations
    published = {"episodes": [], "evidence": []}
    if not db.has_v8_tables(con):
        return published
    from . import customer
    customer.rebuild_customer_show_map(con)
    # One payload-digest memo for the whole load (keyed by source version
    # id, an immutable identity): each source file is hashed once, not
    # once per record. Every row is published once.
    digests: dict = {}
    done: set = set()
    touched = plan.get("_touched")
    for ep in plan.get("episodes", []):
        if touched is None:
            _citations.publish_episode_sources(con, ep["id"], actor, digests)
        else:
            # An evidence projection carries its episode's: when the episode
            # publication stands, only rows this load touched, or never
            # published, can have moved (reviews publish themselves).
            before = con.execute("SELECT id FROM source_states WHERE kind='episode'"
                                 " AND record_id=? ORDER BY publication_order DESC LIMIT 1",
                                 (ep["id"],)).fetchone()
            ref = _citations.publish_source_state(con, kind="episode", record_id=ep["id"],
                                                  actor=actor, digests=digests)
            rows = [r[0] for r in con.execute(
                "SELECT e.id, EXISTS (SELECT 1 FROM source_states s WHERE s.kind='evidence'"
                " AND s.record_id=e.id) FROM evidence e WHERE e.episode_id=?", (ep["id"],))
                if before is None or before[0] != ref or r[0] in touched or not r[1]]
            for evidence_id in rows:
                _citations.publish_source_state(con, kind="evidence", record_id=evidence_id,
                                                actor=actor, digests=digests)
        published["episodes"].append(_citations.current_source_ref(con, "episode", ep["id"]))
        done.update(r[0] for r in con.execute("SELECT id FROM evidence WHERE episode_id=?",
                                              (ep["id"],)))
    for ev in plan.get("evidence", []):
        evidence_id = ev["imported_evidence_id"]
        if evidence_id in done:
            ref = _citations.current_source_ref(con, "evidence", evidence_id)
        else:
            ref = _citations.publish_source_state(
                con, kind="evidence", record_id=evidence_id, actor=actor, digests=digests)
            done.add(evidence_id)
        published["evidence"].append(ref)
    return published


def _transcript_extent_of(con: sqlite3.Connection, version_id) -> float:
    if not version_id:
        return 0.0
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    if row is None:
        return 0.0
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return 0.0
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    extent = 0.0
    for seg in segments:
        if isinstance(seg, dict) and isinstance(seg.get("end"), (int, float)):
            extent = max(extent, float(seg["end"]))
    return extent


def _segments_extent(segments) -> float:
    """The latest numeric segment end (0.0 without one)."""
    return max((float(s["end"]) for s in segments
                if isinstance(s, dict) and isinstance(s.get("end"), (int, float))), default=0.0)


def _version_payload(plan: dict, ver: dict) -> str:
    """A version row's payload. A transcript's is built when its row is
    written, from a fresh read through the load's reader (the pilot import
    re-checks the descriptor digest there); bytes that no longer have the
    planned digest refuse the load, which rolls back."""
    if "payload" in ver:
        return ver["payload"]
    path = ver["path"]
    try:
        data = plan["_read"](path)
    except OSError as exc:
        raise ImportError(f"{path}: unreadable ({exc})")
    if sha256_bytes(data) != ver["sha256"]:
        raise ImportError(f"{path}: changed since the load was planned")
    return json.dumps(_parse_strict(data, path))


def _bounded_seed_interval(extent: float, duration_min) -> tuple:
    """Requested/successful seed interval for one transcript extent.

    Supplied metadata rounds durations (one-decimal minutes) while a
    transcript segment can end slightly past that mark. The successful
    interval is bounded to the known request: coverage is never claimed
    outside the requested duration, and the request itself is never
    widened. Source content, pins, and duration_min stay untouched.
    """
    req = (float(duration_min) * 60.0
           if isinstance(duration_min, (int, float)) and duration_min > 0
           else extent)
    return req, min(extent, req)


def _record_seed_coverage(con: sqlite3.Connection, plan: dict, key_to_id: dict,
                          actor: str) -> None:
    """Record transcribe/screen attestations at the intake transaction.

    Intervals come from the actual imported transcript segments and the
    screening source versions that produced the imported evidence; the
    requested interval uses the episode's known duration when present.
    Partial transcripts therefore attest partial intervals, never full
    coverage. Successful ends are bounded to the request (see
    _bounded_seed_interval). An episode that kept its current copy
    (plan["_kept_copies"]) gets none: this load did not read that copy.
    Reruns record nothing new (_attest_once). Joins the caller's import
    transaction.
    """
    extents: dict = {}

    def extent_of(ep_id, tv_id):
        # The loaded transcript's extent (kept in the plan) without decoding
        # the stored payload again.
        if ep_id not in extents:
            extent = plan["_extent"].get(ep_id)
            extents[ep_id] = extent if extent is not None else _transcript_extent_of(con, tv_id)
        return extents[ep_id]
    kept = plan.get("_kept_copies") or {}
    for ep in plan["episodes"]:
        tv_id = key_to_id.get(f"transcript:{ep['id']}")
        if not tv_id or ep["id"] in kept:
            continue
        extent = extent_of(ep["id"], tv_id)
        if extent <= 0:
            continue
        req, succ = _bounded_seed_interval(extent, ep.get("duration_min"))
        _attest_once(con, ep["id"], "transcribe", req, succ, f"source-version:{tv_id}", actor)
    from . import ad_reads as _ad_reads
    # A transcript-tier episode without Jev results was never screened: no
    # attestation, whatever other rows an export carries for it.
    jev_done = plan.get("_jev_screened") or set()
    transcript_only = {str(e.get("ep_id")) for e in plan.get("_episodes_raw") or []
                       if isinstance(e, dict) and _ad_reads.transcript_only(e, jev_done)}
    screened: dict[str, int] = {}
    for ev in plan["evidence"]:
        if ev.get("episode") and ev.get("version_id"):
            screened.setdefault(ev["episode"], ev["version_id"])
    # An episode the Jev run screened (jev_status ok) proves screening even
    # with nothing flagged: "screened, no ads found" (validation plan A5).
    jev_version = key_to_id.get("jev-run")
    if jev_version:
        status = {str(e.get("ep_id")): e.get("jev_status") for e in plan.get("_episodes_raw") or []
                  if isinstance(e, dict)}
        for ep_id in sorted(plan.get("_jev_screened") or ()):
            if isinstance(status.get(ep_id), str) and status[ep_id].startswith("ok"):
                screened.setdefault(ep_id, jev_version)
    for ep_id, ver_id in screened.items():
        if ep_id in transcript_only or ep_id in kept:
            continue
        tv_id = key_to_id.get(f"transcript:{ep_id}")
        extent = extent_of(ep_id, tv_id) if tv_id else 0.0
        if extent <= 0:
            continue
        row = con.execute("SELECT duration_min FROM episodes WHERE id=?",
                          (ep_id,)).fetchone()
        dur = row["duration_min"] if row else None
        req, succ = _bounded_seed_interval(extent, dur)
        _attest_once(con, ep_id, "screen", req, succ, f"source-version:{ver_id}", actor)


SEED_PRODUCER = "seed-import"


def _attest_once(con: sqlite3.Connection, episode_id: str, stage: str, req: float,
                 succ: float, source_pin: str, actor: str):
    """Record one seed attestation unless it is already the proof on file: a
    success row of this producer for the same episode, stage, transcript
    copy, source pin and intervals, with no failure for that stage and copy
    recorded after it (a later failure is superseded by recording again).
    A rerun that proves nothing new writes nothing."""
    from . import coverage as _coverage
    pin = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                      (episode_id,)).fetchone()
    copy = pin[0] if pin is not None else None
    same = con.execute(
        "SELECT id FROM coverage_attestations WHERE workspace_id='local' AND episode_id=?"
        " AND stage=? AND source_identity IS ? AND producer=? AND status='success'"
        " AND source_pin=? AND req_start_s=0 AND req_end_s=? AND succ_start_s=0"
        " AND succ_end_s=? ORDER BY id DESC LIMIT 1",
        (episode_id, stage, copy, SEED_PRODUCER, source_pin, float(req), float(succ))).fetchone()
    if same is not None and con.execute(
            "SELECT 1 FROM coverage_attestations WHERE workspace_id='local' AND episode_id=?"
            " AND stage=? AND source_identity IS ? AND status!='success' AND id>? LIMIT 1",
            (episode_id, stage, copy, same[0])).fetchone() is None:
        return None
    return _coverage.record_attestation(
        con, episode_id, stage, (0.0, req), (0.0, succ), source_pin=source_pin,
        producer=SEED_PRODUCER, actor=actor)


def _held_copies(con: sqlite3.Connection) -> dict:
    """transcript:<episode> -> the episode's current copy (id, version_no,
    sha256). A load never replaces it with an older copy: a copy the
    workspace already holds at a lower version_no (version_no is the
    creation order of an episode's copies, MAX + 1 at insert). A copy it
    has never held is newer and becomes current, as does a held copy with
    a higher version_no than the current one."""
    return {row["source_key"]: row for row in con.execute(
        "SELECT sv.id, sv.version_no, sv.sha256, sv.source_key FROM episodes e"
        " JOIN source_versions sv ON sv.id=e.transcript_version_id"
        " AND sv.source_key='transcript:'||e.id")}


def _stored_transcript(con: sqlite3.Connection, version_id) -> dict:
    row = con.execute("SELECT payload FROM source_versions WHERE id=?", (version_id,)).fetchone()
    try:
        payload = json.loads(row["payload"]) if row else {}
    except (TypeError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _transcript_meta(t: dict) -> dict:
    """What a transcript copy says about its episode: titles (a benchmark
    without a metadata row) and duration (a supplied row without one)."""
    dur = t.get("duration_min")
    if not isinstance(dur, (int, float)) or dur <= 0:
        ends = [s.get("end", 0) for s in t.get("segments") or []
                if isinstance(s, dict) and isinstance(s.get("end"), (int, float))]
        dur = round(max(ends) / 60, 2) if ends else None
    return {"show_title": t.get("show") if isinstance(t.get("show"), str) else None,
            "ep_title": t.get("ep_title") if isinstance(t.get("ep_title"), str) else None,
            "duration_min": dur}


def _operator_aliases(con: sqlite3.Connection) -> dict:
    """Aliases an operator recorded with `record-alias` (episode_alias):
    their audit row is the source of truth, so a reimport keeps alias_of
    and kind (and with them the alias show mapping the show map keeps
    for an alias) instead of resetting them to the plan's. Latest valid row
    per episode wins. A row is valid when its after holds alias_of and kind
    (an alias), or holds the key alias_of with a null value (an un-alias:
    the episode stays un-aliased, alias_of NULL, with its after kind when
    it has one, even where the plan would alias it). Any other row is
    skipped (alias_of without kind, a detail without episode or after, a
    detail that is not JSON or not an object): it never un-aliases and the
    episode's earlier valid row stays. (No command writes an un-alias row
    yet; an operator undoing an alias resets the row and writes one.)"""
    from .episode_alias import AUDIT_ACTION
    out = {}
    for row in con.execute("SELECT target_id, detail FROM audit WHERE action=?"
                           " AND target_type='episode' ORDER BY id", (AUDIT_ACTION,)):
        try:
            after = (json.loads(row["detail"]).get("episode") or {}).get("after") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        if not isinstance(after, dict):
            continue
        if after.get("alias_of") and after.get("kind"):
            out[row["target_id"]] = {"alias_of": after["alias_of"], "kind": after["kind"]}
        elif "alias_of" in after and after["alias_of"] is None:
            out[row["target_id"]] = dict({"alias_of": None},
                                         **({"kind": after["kind"]} if after.get("kind") else {}))
    return out


def _upsert_episode(con: sqlite3.Connection, ep: dict, report: dict,
                    actor: str = "seed") -> None:
    cur = con.execute("SELECT * FROM episodes WHERE id=?", (ep["id"],)).fetchone()
    prior_pin = cur["transcript_version_id"] if cur is not None else None
    if cur is None:
        con.execute("INSERT INTO episodes (id, kind, alias_of, show_title, ep_title, category,"
                    " pub_date, duration_min, fetch_date, audio_url, media_id,"
                    " transcript_version_id, revision, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (ep["id"], ep["kind"], ep.get("alias_of"), ep.get("show_title"),
                     ep.get("ep_title"), ep.get("category"), ep.get("pub_date"),
                     ep.get("duration_min"), ep.get("fetch_date"), ep.get("audio_url"),
                     ep.get("media_id"), ep.get("transcript_version_id"), db.utcnow()))
        report["created"] += 1
    else:
        values = {key: ep.get(key) for key in
                  ("show_title", "ep_title", "category", "pub_date", "duration_min", "fetch_date", "alias_of")}
        values["audio_url"] = cur["audio_url"] if cur["media_edited"] else ep.get("audio_url")
        values["transcript_version_id"] = ep.get("transcript_version_id")
        if any(cur[key] != value for key, value in values.items()):
            if any(cur[key] != values[key] for key in ("audio_url", "transcript_version_id")):
                values["alignment"] = "unknown"
            con.execute("UPDATE episodes SET " + ",".join(f"{key}=?" for key in values)
                        + ",revision=revision+1,updated_at=? WHERE id=?",
                        [*values.values(), db.utcnow(), ep["id"]])
            if db.has_history_tables(con):
                db.record_episode_snapshot(con, ep["id"], actor)
        report["reused"] += 1


_DETECTOR_IDENTITY_ATTRS = ("brand_spoken", "brand_url", "brand_code", "offer_codes", "urls", "anchor")
# Display-only attrs of named reads (corpus brand canonicalization, the
# self-promo filter, speaker diarization's voice signal, the namer's
# Option A product and network): never identity, refreshed on reused rows
# without human work (see below).
_DISPLAY_ATTRS = ("brand_canonical", "canon_reason", "self_promo", "self_promo_reason",
                  "dominant_speaker", "voice_share_outside_ads", "voice_ad_only",
                  "product", "network")


def _refresh_display_attrs(con: sqlite3.Connection, evidence_id: str, incoming: dict,
                           actor: str) -> bool:
    """Bring a reused row's display attrs (_DISPLAY_ATTRS) up
    to date. Rows carrying human work keep the attrs they were reviewed
    with, so their evidence revision (which published refs pin) never moves
    for a display change; their canonical brand is in the active
    named-reads source version. Other rows get the new values, a revision
    bump and an evidence snapshot. Returns True when the row changed."""
    row = con.execute("SELECT attrs FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    attrs = json.loads(row["attrs"]) if row else {}
    new = {k: v for k, v in attrs.items() if k not in _DISPLAY_ATTRS}
    new.update({k: incoming[k] for k in _DISPLAY_ATTRS if incoming.get(k) is not None})
    if row is None or new == attrs or _has_row_edits(con, evidence_id):
        return False
    con.execute("UPDATE evidence SET attrs=?, revision=revision+1 WHERE id=?",
                (json.dumps(new), evidence_id))
    if db.has_history_tables(con):
        db.record_evidence_snapshot(con, evidence_id, actor)
    return True


# Attrs of a named read refreshed on reuse (rows without human work): the
# namer's brand suggestion and its evidence, never part of its identity.
_NAMED_REFRESH_ATTRS = ("brand_spoken", "brand_url", "brand_code", "offer_codes", "urls",
                        "anchor", "confidence", "confidence_score", "needs_review", "source")


def _named_content(record_id, episode, start, end) -> str:
    """Identity of a named read: record key (episode + span), episode and
    span; the transcript digest joins it in the caller."""
    return json.dumps([record_id, episode, start, end])


def _refresh_named_row(con: sqlite3.Connection, evidence_id: str, incoming: dict,
                       excerpt: str, actor: str) -> bool:
    """Bring a reused named read up to date with the namer's current
    suggestion: brand attrs, display attrs, excerpt and the machine brand
    associations. Rows carrying human work keep everything they were
    reviewed with (their current suggestion lives in ad_reads). The
    machine association change bumps assoc_revision with an
    `import-refresh` history snapshot, which _has_row_edits does not
    count as human work. Returns True when the row changed."""
    if _has_row_edits(con, evidence_id):
        return False
    row = con.execute("SELECT attrs, excerpt FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        return False
    attrs = json.loads(row["attrs"] or "{}")
    new = {k: v for k, v in attrs.items() if k not in _DISPLAY_ATTRS}
    new.update({k: incoming.get(k) for k in _NAMED_REFRESH_ATTRS if k in incoming})
    new.update({k: incoming[k] for k in _DISPLAY_ATTRS if incoming.get(k) is not None})
    # The ad rules' record (provenance["ad_rules"]) follows the load, and so
    # leaves with the rules switched off; the rest of the provenance stays
    # as first imported.
    # The _without_r8 call is redundant: the incoming rows already went through
    # candidate_attrs/_without_r8. It is kept as a second safeguard.
    was, now = attrs.get("provenance"), _without_r8(incoming.get("provenance"))
    was_rules = was.get("ad_rules") if isinstance(was, dict) else None
    now_rules = now.get("ad_rules") if isinstance(now, dict) else None
    if was_rules != now_rules:
        prov = dict(was) if isinstance(was, dict) else {}
        if now_rules:
            prov["ad_rules"] = now_rules
        else:
            prov.pop("ad_rules", None)
        new["provenance"] = prov
    changed = False
    if new != attrs or excerpt != row["excerpt"]:
        con.execute("UPDATE evidence SET attrs=?, excerpt=?, revision=revision+1 WHERE id=?",
                    (json.dumps(new), excerpt, evidence_id))
        if db.has_history_tables(con):
            db.record_evidence_snapshot(con, evidence_id, actor)
        changed = True
    return _sync_machine_assocs(con, evidence_id, incoming, actor) or changed


def _sync_machine_assocs(con: sqlite3.Connection, evidence_id: str, incoming: dict,
                         actor: str) -> bool:
    """Make the row's machine (detector) brand associations the incoming
    brand attrs, as an import-refresh association revision."""
    wanted = sorted({(incoming.get(key), channel) for key, channel in
                     (("brand_spoken", "spoken"), ("brand_url", "url"), ("brand_code", "code"))
                     if incoming.get(key)})
    have = sorted((r["brand"], r["channel"]) for r in con.execute(
        "SELECT brand, channel FROM brand_assocs WHERE evidence_id=? AND origin='detector'",
        (evidence_id,)))
    if wanted == have or not db.has_history_tables(con):
        return False
    con.execute("UPDATE evidence SET assoc_revision=assoc_revision+1 WHERE id=?", (evidence_id,))
    con.execute("DELETE FROM brand_assocs WHERE evidence_id=? AND origin='detector'",
                (evidence_id,))
    for brand, channel in wanted:
        con.execute("INSERT INTO brand_assocs (evidence_id,brand,basis,origin,updated_at,channel)"
                    " VALUES (?,?,'suggested','detector',?,?)",
                    (evidence_id, brand, db.utcnow(), channel))
    db.record_association_snapshot(con, evidence_id, actor, origin=IMPORT_REFRESH_ORIGIN)
    return True


def refresh_machine_assocs(con: sqlite3.Connection, evidence_id: str, incoming: dict,
                           actor: str) -> bool:
    """An operator rule changed the namer's pick of a read (corrections):
    its machine brand associations follow at once, unless a person worked
    on the row (then it keeps what it was reviewed with, as on a load)."""
    if _has_row_edits(con, evidence_id):
        return False
    return _sync_machine_assocs(con, evidence_id, incoming, actor)


IMPORT_REFRESH_ORIGIN = "import-refresh"
# An Undo that put back a read nobody had worked on writes its review and
# association revisions with this origin. Like an import refresh it is not
# human work, so later imports refresh the read again (AD-03).
UNDO_UNTOUCHED_ORIGIN = "admin-undo-untouched"
MACHINE_ORIGINS_SQL = f"('{IMPORT_REFRESH_ORIGIN}','{UNDO_UNTOUCHED_ORIGIN}')"


def human_review_sql(evidence: str) -> str:
    """SQL: the read `evidence` has a review a person made (a review whose
    current revision an untouched Undo wrote is not one)."""
    return (f"SELECT 1 FROM reviews r WHERE r.evidence_id={evidence} AND NOT EXISTS"
            " (SELECT 1 FROM review_history h WHERE h.evidence_id=r.evidence_id"
            " AND h.review_revision=r.revision"
            f" AND h.history_origin='{UNDO_UNTOUCHED_ORIGIN}')")


def _detector_content(record_id, episode, start, end, excerpt, attrs: dict) -> str:
    """Evidence-defining detector content as canonical JSON. Excludes volatile
    fields (fetch date, provenance, confidence, titles) that change per run."""
    return json.dumps([record_id, episode, start, end, excerpt,
                       {k: attrs.get(k) for k in _DETECTOR_IDENTITY_ATTRS}], sort_keys=True)


def _has_row_edits(con: sqlite3.Connection, evidence_id: str) -> bool:
    """True when a person (or agent) edited this row's own state or pinned
    it: a review, a non-detector association, a campaign attachment or
    check, an observation, or a published ref attached to a placement or
    target. A reload never refreshes such a row (its evidence revision
    stays where the work saw it). Notes, saved captures and tracker events
    pin immutable publications and do not freeze the row; they are human
    work for lineage (lineage.human_work_ids), which decides which row of a
    stable key stays and where retired rows' work goes."""
    # assoc_revision > 0 is human work unless the current association
    # revision was written by an import refresh (_refresh_named_row) or by
    # an Undo that put back an untouched read (corrections.undo); a review
    # is human work unless such an Undo wrote its current revision.
    history = db.has_history_tables(con)
    refreshed = (" AND NOT EXISTS (SELECT 1 FROM association_history h WHERE h.evidence_id=:e"
                 " AND h.association_revision=evidence.assoc_revision"
                 f" AND h.history_origin IN {MACHINE_ORIGINS_SQL})"
                 if history else "")
    reviewed = human_review_sql(":e") if history else "SELECT 1 FROM reviews WHERE evidence_id=:e"
    sql = (f"SELECT EXISTS ({reviewed})"
           # PODS: model rows are display-only
           " OR EXISTS (SELECT 1 FROM brand_assocs WHERE evidence_id=:e AND origin<>'detector'"
           " AND basis<>'model')"
           " OR EXISTS (SELECT 1 FROM evidence WHERE id=:e AND ((assoc_revision>0" + refreshed + ")"
           " OR observation_revision_id IS NOT NULL))"
           " OR EXISTS (SELECT 1 FROM campaign_evidence WHERE evidence_id=:e)"
           " OR EXISTS (SELECT 1 FROM campaign_checks WHERE evidence_id=:e)")
    if db.has_v8_tables(con):
        refs = "SELECT id FROM source_states WHERE kind='evidence' AND record_id=:e"
        sql += (f" OR EXISTS (SELECT 1 FROM placement_evidence WHERE ref IN ({refs}))"
                f" OR EXISTS (SELECT 1 FROM target_evidence WHERE ref IN ({refs}))")
    return bool(con.execute(sql, {"e": evidence_id}).fetchone()[0])


def _is_visible(con: sqlite3.Connection, row, version_id) -> bool:
    member = con.execute("SELECT member FROM evidence_source_membership WHERE evidence_id=?"
                         " AND source_version_id=?", (row["id"], version_id)).fetchone()
    return bool(member["member"]) if member else row["source_version_id"] == version_id


def _set_visible(con: sqlite3.Connection, row, version_id, visible: bool) -> None:
    """Show or hide one row in `version_id` without touching its (immutable) source pin."""
    if visible == (row["source_version_id"] == version_id):
        con.execute("DELETE FROM evidence_source_membership WHERE evidence_id=?"
                    " AND source_version_id=?", (row["id"], version_id))
    else:
        con.execute("INSERT OR REPLACE INTO evidence_source_membership VALUES (?, ?, ?)",
                    (row["id"], version_id, int(visible)))


def _set_membership(con: sqlite3.Connection, matches: list, version_id,
                    human: set) -> sqlite3.Row:
    """Choose the one visible row among rows with equal content and return
    it: a row carrying human work (`human`, lineage.human_work_ids) first,
    then the row already visible, then the newest; the rest are retired as
    duplicates (old re-key bug) and lineage.reconcile merges their human
    work onto the visible row.
    Reused rows keep the non-identity attrs (confidence, fetch date,
    provenance) they were first imported with; refreshing them would bump the
    evidence revision that published refs pin. The current candidate stays in
    the active detector-output source version."""
    if len(matches) == 1:  # the usual reload: one row, kept visible
        if not _is_visible(con, matches[0], version_id):
            _set_visible(con, matches[0], version_id, True)
        return matches[0]
    ranked = sorted(matches, key=lambda r: (r["id"] not in human,
                                            not _is_visible(con, r, version_id)))
    for row in ranked:
        _set_visible(con, row, version_id, row is ranked[0])
    return ranked[0]


def seed_sources(root: Path, episodes=None, transcripts=None, candidates=None,
                 jev_results=None, no_jev: bool = False, named_reads=None,
                 no_named_reads: bool = False) -> dict:
    """Input locations for one seed run: the repo layout under `root`, with
    optional explicit paths. no_jev treats the Jev run as empty. Named reads
    come from `named_reads`, else from NAMED_OUT_REL under `root` when that
    file exists (absent = none, as before); no_named_reads skips them.

    A caller may also set "transcripts" to a mapping {episode id: file}
    (files named otherwise than <episode id>.json) and "read" to a function
    path -> bytes: every input file is then read through it when planned,
    and a transcript again when its version row is written; each read is
    hashed and parsed from its own bytes (the pilot import checks each
    file's digest there)."""
    named = None if no_named_reads else Path(named_reads) if named_reads else root / NAMED_OUT_REL
    return {"episodes": Path(episodes) if episodes else root / EPISODES_REL,
            "transcripts": Path(transcripts) if transcripts else root / TRANSCRIPTS_DIR,
            "candidates": Path(candidates) if candidates else root / DET_OUT_REL,
            "jev": None if no_jev else Path(jev_results) if jev_results else root / JEV_RUN_REL,
            "named": named if named_reads or (named and named.is_file()) else None}


def _parse_strict(data: bytes, path):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_dup_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportError(f"{path}: unreadable JSON ({exc})")


def _build_seed_plan(repo: Path, sources: dict | None = None) -> dict:
    src = sources or seed_sources(repo)
    src.setdefault("named", None)
    read = src.get("read") or (lambda path: Path(path).read_bytes())
    blobs: dict = {}

    def raw(p) -> bytes:
        """Each input file is read once; its digest and its parse use these bytes."""
        key = str(p)
        if key not in blobs:
            try:
                blobs[key] = read(p)
            except OSError as exc:
                raise ImportError(f"{p}: unreadable ({exc})")
        return blobs[key]

    def load(p):
        return _parse_strict(raw(p), p)
    episodes_raw = load(src["episodes"])
    if not isinstance(episodes_raw, list) or not episodes_raw:
        raise ImportError("data/episodes.json must be a non-empty list")
    run = load(src["jev"]) if src["jev"] else {"episodes": []}
    det = load(src["candidates"])
    if not isinstance(det, list):
        raise ImportError("detector output must be a list")
    plan: dict = {"source_files": [], "versions": [], "episodes": [], "evidence": [],
                  "_kept_copies": {}, "_read": read,
                  "_episodes_raw": episodes_raw, "_det_raw": det, "_named_raw": [],
                  "_named_loaded": False,
                  # A caller that brings its clips as admitted items (the pilot
                  # import, always with a "clips" list) and any load on a pilot
                  # host (EARWITNESS_PILOT_LAYOUT set) never has a folder
                  # scanned; a local seed scans its export's clips folder.
                  "_export_root": None if "clips" in src or os.environ.get("EARWITNESS_PILOT_LAYOUT")
                  else Path(src["episodes"]).resolve().parent.parent,
                  "_clips": src.get("clips"),
                  "_jev_screened": {str(ep.get("episode_id") or ep.get("ep_id"))
                                    for ep in run.get("episodes", []) or []
                                    if isinstance(ep, dict) and ep.get("answers") is not None}}

    def add_file(kind: str, rel: str, p: Path) -> str:
        data = raw(p)
        digest = sha256_bytes(data)
        # Explicit sources record where the file actually came from.
        plan["source_files"].append({"kind": kind, "path": rel if sources is None else str(p.resolve()),
                                     "sha256": digest,
                                    "bytes": len(data)})
        return digest

    ep_digest = add_file("episodes_json", EPISODES_REL, src["episodes"])
    plan["versions"].append({"source_key": "episodes.json", "sha256": ep_digest,
                             "payload": json.dumps(episodes_raw)})
    episodes = []
    for i, e in enumerate(episodes_raw):
        if not isinstance(e, dict) or not e.get("ep_id"):
            raise ImportError(f"episodes.json row {i} lacks ep_id")
        dur = e.get("duration_min")
        episodes.append({
            "id": str(e["ep_id"]), "kind": "supplied",
            "show_title": e.get("show_title"), "ep_title": e.get("ep_title"),
            "category": e.get("category"), "pub_date": e.get("pub_date"),
            "duration_min": float(dur) if isinstance(dur, (int, float)) else None,
            "fetch_date": e.get("fetch_date"), "audio_url": e.get("audio_url"),
            "media_id": None, "transcript_version_id": None,
        })
    # Durations the metadata supplies (the rest come from a transcript copy).
    plan["_supplied_duration"] = {e["id"] for e in episodes if e["duration_min"] is not None}
    # Supplied transcripts; bench_0/s000e0 exact-text alias preserved.
    tsrc = src["transcripts"]
    tfiles = sorted((str(k), Path(v)) for k, v in tsrc.items()) if isinstance(tsrc, dict) \
        else [(p.stem, p) for p in sorted(tsrc.glob("*.json"))]
    # Only small facts per transcript stay in the plan (IMPORTMEM): its
    # digest, metadata, extent and the Jev excerpts it gives. Its payload
    # is re-read when its version row is written (_version_payload); its
    # quotes come from the stored copy (_rebuild_ad_reads).
    jev_eps: dict = {}
    for ep in run.get("episodes", []) or [] if isinstance(run, dict) else []:
        if isinstance(ep, dict):
            jev_eps.setdefault(ep.get("episode_id") or ep.get("ep_id"), []).append(ep)
    jev_excerpts: dict = {}
    texts: dict = {}  # canonical text of the bench_0/s000e0 alias pair only
    tids: list = []
    plan["_extent"] = {}
    plan["_tmeta"] = {}
    digest_by_key = {}
    for ep_id, p in tfiles:
        digest = add_file("transcript", f"{TRANSCRIPTS_DIR}/{p.name}", p)
        digest_by_key[ep_id] = digest
        t = load(p)
        blobs.pop(str(p), None)  # parsed; the bytes are not needed again
        if not isinstance(t, dict) or not isinstance(t.get("segments"), list):
            raise ImportError(f"{p.name}: malformed transcript")
        _validate_segments(t["segments"], p.name)
        tids.append(ep_id)
        if ep_id in ("bench_0", "s000e0"):
            texts[ep_id] = canonical_transcript_text(t)
        plan["_extent"][ep_id] = _segments_extent(t["segments"])
        plan["_tmeta"][ep_id] = _transcript_meta(t)
        for ep in jev_eps.get(ep_id, ()):
            _jev_excerpts(ep, t["segments"], jev_excerpts)
        plan["versions"].append({"source_key": f"transcript:{ep_id}", "sha256": digest,
                                 "path": p})
        del t
    alias_of = {}
    if texts.get("bench_0") and texts.get("bench_0") == texts.get("s000e0"):
        alias_of["bench_0"] = "s000e0"
    by_id = {e["id"]: e for e in episodes}
    for ep_id in tids:
        tm = plan["_tmeta"].get(ep_id, {})
        if ep_id not in by_id:
            # Benchmark identity comes from the transcript file itself; a
            # missing metadata row must not discard known show/episode titles.
            by_id[ep_id] = {"id": ep_id, "kind": "benchmark-unresolved",
                            "show_title": tm.get("show_title"), "ep_title": tm.get("ep_title"),
                            "category": None, "pub_date": None,
                            "duration_min": tm.get("duration_min"), "fetch_date": None,
                            "audio_url": None, "media_id": None,
                            "transcript_version_id": None}
        else:
            # Real durations from transcripts repair metadata gaps on reimport;
            # supplied metadata values always win when present.
            if by_id[ep_id].get("duration_min") is None:
                by_id[ep_id]["duration_min"] = tm.get("duration_min")
    for ep_id, target in alias_of.items():
        if ep_id in by_id:
            by_id[ep_id]["alias_of"] = target
            by_id[ep_id]["kind"] = "supplied-alias"
            # Exact-transcript alias shares the target's supplied enclosure
            # stream. Bounded: reuse the stored URL string only; never fetch,
            # probe, or download remote audio at seed time.
            if by_id[ep_id].get("audio_url") is None:
                by_id[ep_id]["audio_url"] = (by_id.get(target) or {}).get("audio_url")
    plan["episodes"] = list(by_id.values())
    # Link transcript version digests (db ids resolved at apply time via sha).
    plan["_transcript_digest"] = digest_by_key
    # Jev windows at threshold.
    plan["jev_skipped"] = not src["jev"]
    if src["jev"]:
        jev_digest = add_file("jev_run", JEV_RUN_REL, src["jev"])
        plan["versions"].append({"source_key": "jev-run", "sha256": jev_digest,
                                 "payload": json.dumps(run)})
    for ep in run.get("episodes", []) or []:
        ep_id = ep.get("episode_id") or ep.get("ep_id")
        for wid, w in sorted((ep.get("answers") or {}).items()):
            if float(w.get("noul", 0)) < JEV_THRESHOLD:
                continue
            start = _finite_number(w.get("start_s"), "jev window start")
            end = _finite_number(w.get("end_s"), "jev window end")
            if not (end > start and start >= 0):
                raise ImportError("jev window has nonsensical bounds")
            excerpt = jev_excerpts.get((id(ep), wid), "")
            plan["evidence"].append({
                "id": f"jev:{ep_id}:{wid}", "source": "jev", "version_key": "jev-run",
                "episode": ep_id, "start": start, "end": end,
                "excerpt": excerpt,
                "attrs": json.dumps({"window_id": wid, "noul": float(w.get("noul")),
                                     "model": run.get("model_returned", "jev-1.13.0"),
                                     "raw_window": w,
                                     "run_id": run.get("run_id")}),
            })
    # Experimental detector candidates (separate source; never merged).
    det_digest = add_file("detector_output", DET_OUT_REL, src["candidates"])
    plan["versions"].append({"source_key": "detector-output", "sha256": det_digest,
                             "payload": json.dumps(det)})
    _add_candidates(plan, det, "det", "detector-output")
    # Named Jev reads: detector-shaped candidates under their own source key
    # and record prefix, imported with the detector's identity rules.
    if src["named"]:
        named = load(src["named"])
        if not isinstance(named, list):
            raise ImportError("named reads must be a list")
        named_digest = add_file("named_reads_output", NAMED_OUT_REL, src["named"])
        plan["versions"].append({"source_key": "named-reads-output", "sha256": named_digest,
                                 "payload": json.dumps(named)})
        _add_candidates(plan, named, "named", "named-reads-output")
        plan["_named_raw"] = named
        plan["_named_loaded"] = True
    return plan


def _jev_window_excerpt(segments, start: float, end: float) -> str:
    return " ".join(
        str(s.get("text", "")) for s in segments
        if isinstance(s, dict) and s.get("start", float("inf")) < end
        and s.get("end", float("-inf")) > start).strip()[:600]


def _jev_excerpts(ep: dict, segments, out: dict) -> None:
    """The excerpts of one Jev run episode's windows at the threshold, from
    its transcript's segments while they are in hand: out[(id(ep), window
    id)]. A window the plan refuses later is skipped here."""
    try:
        answers = sorted((ep.get("answers") or {}).items())
    except (AttributeError, TypeError):
        return
    for wid, w in answers:
        try:
            if float(w.get("noul", 0)) < JEV_THRESHOLD:
                continue
            start = _finite_number(w.get("start_s"), "jev window start")
            end = _finite_number(w.get("end_s"), "jev window end")
        except (AttributeError, TypeError, ValueError, ImportError):
            continue
        if end > start and start >= 0:
            out[(id(ep), wid)] = _jev_window_excerpt(segments, start, end)


def _without_r8(prov):
    """`prov` without R8's entries (CG-R2-01): R8's verdict and its signal
    text (transcript words from the clip window, outside the read) stay in
    the rules output (the clean list), never in evidence attrs, so search
    never matches them and attrs and revisions are as before R8."""
    rules = prov.get("ad_rules") if isinstance(prov, dict) else None
    if not isinstance(rules, list):
        return prov
    kept = [e for e in rules if not (isinstance(e, dict) and e.get("rule") == "R8")]
    if len(kept) == len(rules):
        return prov
    out = {k: v for k, v in prov.items() if k != "ad_rules"}
    if kept:
        out["ad_rules"] = kept
    # A row whose provenance was literally {} before the rules now also gets
    # None (no demo row has either).
    return out or None


def candidate_attrs(c: dict, prefix: str) -> dict:
    """The evidence attrs a load writes for one detector-shaped candidate
    (a named read when prefix is "named"), without R8's entries
    (_without_r8)."""
    attrs = {"candidate_id": c.get("candidate_id"),
             "brand_spoken": c.get("brand_spoken"),
             "brand_url": c.get("brand_url"),
             "brand_code": c.get("brand_code"),
             "offer_codes": c.get("offer_codes") or [],
             "urls": c.get("urls") or [],
             "anchor": c.get("anchor"),
             "confidence": c.get("confidence"),
             "fetch_date": c.get("fetch_date"), "provenance": _without_r8(c.get("provenance"))}
    if prefix != "det":  # detector attrs stay byte-identical
        attrs.update(source=c.get("source"), needs_review=c.get("needs_review"),
                     confidence_score=c.get("confidence_score"))
        attrs.update({k: c[k] for k in _DISPLAY_ATTRS if c.get(k) is not None})
    return attrs


def _add_candidates(plan: dict, cands: list, prefix: str, version_key: str) -> None:
    for c in cands:
        if not isinstance(c, dict) or not c.get("candidate_id"):
            raise ImportError("detector candidate lacks candidate_id")
        ep_id = c.get("ep_id")
        start = c.get("start_s")
        end = c.get("end_s")
        attrs = candidate_attrs(c, prefix)
        plan["evidence"].append({
            "id": f"{prefix}:{c['candidate_id']}", "source": "detector",
            "version_key": version_key, "episode": ep_id,
            "start": float(start) if isinstance(start, (int, float)) else None,
            "end": float(end) if isinstance(end, (int, float)) else None,
            "excerpt": str(c.get("excerpt", ""))[:2000],
            "attrs": json.dumps(attrs),
        })


def _version_id(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT id FROM source_versions WHERE source_key=? AND is_active=1",
                      (key,)).fetchone()
    return row["id"] if row else None


def _validate_segments(segments, name):
    if not isinstance(segments, list):
        raise ImportError(f"{name}: segments must be a list")
    for s in segments:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            raise ImportError(f"{name}: each segment needs text and numeric bounds")
        start = _finite_number(s.get("start"), f"{name} segment start")
        end = _finite_number(s.get("end"), f"{name} segment end")
        if start < 0 or end <= start:
            raise ImportError(f"{name}: require 0 <= segment start < end")


@db.atomic
def import_upload_transcript(con: sqlite3.Connection, payload: dict, actor: str = "user",
                             origin_note: str = "") -> dict:
    """Compatible local transcript JSON upload. IDs live in the upload:
    namespace and can never overwrite supplied episodes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ImportError("upload must be an object with a segments list")
    import re
    if not origin_note.strip():
        raise ImportError("origin_note is required")
    raw_id = str(payload.get("ep_id") or payload.get("id") or "untitled").removeprefix("upload:")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", raw_id):
        raise ImportError("episode id must use 1–64 letters, numbers, dots, underscores, or hyphens")
    ep_id = f"upload:{raw_id}"
    _validate_segments(payload["segments"], "upload")
    digest = sha256_bytes(json.dumps(payload, sort_keys=True).encode())
    existing = con.execute("SELECT id FROM source_versions WHERE source_key=? AND sha256=?",
                           (f"transcript:{ep_id}", digest)).fetchone()
    current = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?", (ep_id,)).fetchone()
    if existing and current and current["transcript_version_id"] == existing["id"]:
        return {"episode_id": ep_id, "segments": len(payload["segments"]), "reused": True}
    with db.transaction(con, write=True):
        con.execute("INSERT OR IGNORE INTO source_files (kind, path, sha256, bytes, imported_at)"
                    " VALUES ('upload', ?, ?, ?, ?)",
                    (ep_id, digest, len(json.dumps(payload)), db.utcnow()))
        version = con.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM source_versions"
                              " WHERE source_key=?", (f"transcript:{ep_id}",)).fetchone()[0]
        con.execute("UPDATE source_versions SET is_active=0 WHERE source_key=?",
                    (f"transcript:{ep_id}",))
        if existing:
            transcript_id = existing["id"]
            con.execute("UPDATE source_versions SET is_active=1 WHERE id=?", (transcript_id,))
        else:
            cur = con.execute("INSERT INTO source_versions (source_key, version_no, sha256,"
                              " payload, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                              (f"transcript:{ep_id}", version, digest, json.dumps(payload), db.utcnow()))
            transcript_id = cur.lastrowid
        prior = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                            (ep_id,)).fetchone()
        prior_pin = prior["transcript_version_id"] if prior is not None else None
        # fetch_date is the stated source date (the pilot import sets it
        # from its descriptor); an upload without it keeps NULL.
        con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, category,"
                    " transcript_version_id, duration_min, fetch_date, revision, updated_at)"
                    " VALUES (?, 'upload', ?, ?, ?, ?, ?, ?, 1, ?)"
                    " ON CONFLICT(id) DO UPDATE SET transcript_version_id=excluded.transcript_version_id,"
                    " duration_min=excluded.duration_min, revision=episodes.revision+1,"
                    " show_title=excluded.show_title,ep_title=excluded.ep_title,category=excluded.category,"
                    " fetch_date=excluded.fetch_date,"
                    " alignment='unknown', updated_at=excluded.updated_at",
                    (ep_id, payload.get("show_title") or payload.get("show"),
                     payload.get("ep_title"), payload.get("category"), transcript_id,
                     max((s["end"] for s in payload["segments"]), default=0) / 60,
                     payload["fetch_date"] if isinstance(payload.get("fetch_date"), str)
                     and payload["fetch_date"] else None,
                     db.utcnow()))
        if db.has_history_tables(con):
            db.record_episode_snapshot(con, ep_id, actor)
        if db.has_v8_tables(con):
            from . import citations as _citations_upload
            from . import customer
            customer.rebuild_customer_show_map(con)
            _citations_upload.publish_episode_sources(con, ep_id, actor)
        _reindex(con)
        rev = db.bump_revision(con)
        db.audit(con, actor, "upload_import", "episode", ep_id, {"origin_note": origin_note}, rev)
    return {"episode_id": ep_id, "segments": len(payload["segments"])}


def _reindex(con: sqlite3.Connection) -> None:
    """Bring the evidence full-text index up to date: entries whose text
    changed, or whose row is gone, are replaced; an unchanged reload writes
    nothing."""
    if not db.fts_available(con):
        return
    rows = con.execute("SELECT e.id, e.excerpt, ep.show_title, ep.ep_title,"
                       " COALESCE(GROUP_CONCAT(a.brand, ' '), '') AS brands"
                       " FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
                       # PODS: model rows are display-only
                       " LEFT JOIN brand_assocs a ON a.evidence_id=e.id AND a.basis<>'model'"
                       " GROUP BY e.id").fetchall()
    wanted = {r["id"]: " ".join(x for x in [r["excerpt"], r["show_title"], r["ep_title"],
                                            r["brands"]] if x) for r in rows}
    kept: set = set()
    stale = []
    for rowid, evidence_id, text in con.execute("SELECT rowid, evidence_id, text FROM evidence_fts"):
        if evidence_id in kept or wanted.get(evidence_id) != text:
            stale.append((rowid,))
        else:
            kept.add(evidence_id)
    con.executemany("DELETE FROM evidence_fts WHERE rowid=?", stale)
    con.executemany("INSERT INTO evidence_fts (evidence_id, text) VALUES (?, ?)",
                    [(k, v) for k, v in wanted.items() if k not in kept])
