"""Explicit one-time conversion and non-executable quarantine (K10).

The existing lineage store accounts for every retained executable definition.
Original definitions, history/profile bytes and frozen payloads are not
rewritten to manufacture qualification. Native ResearchFilterV1 stays distinct.
The one recorded exception: a schema-7 tracker baseline is translated to
source publications (translate_tracker_baselines) so the first check after
conversion compares like with like; each translation is in the receipt.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
from collections import OrderedDict

from . import citations, customer_contracts as contracts, customer_query as query, media
from . import customer_saved as saved, db, ops, search, exports, access

MARKER_KEY = "customer_conversion"
# Local-only UI readiness receipt (validation plan A1): written by `seed`
# (and `upgrade`) on a converted workspace, read by the local server's
# /session. The hosted gate (readiness: C01-C20 API qualification) never
# reads it.
LOCAL_RECEIPT_KEY = "local_ui_readiness"
LOCAL_UI_CONTRACT = "earwitness/customer-ui/2"
DISPOSITION_KEY = "customer_conversion_dispositions"
FINAL_ACTIONS = frozenset({"converted", "qualified", "quarantined", "dropped"})
READ_SCOPES = frozenset({"research:read", "transcripts:read", "watchlists:read", "campaigns:read", "notes:read"})


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _legacy_rows(con):
    if "customer_saved_searches" not in _tables(con):
        return []
    return [dict(row) for row in con.execute("SELECT * FROM customer_saved_searches WHERE workspace_id='local' ORDER BY id")]


def _lineage(con):
    if "customer_conversion_lineage" not in _tables(con):
        return {}
    return {r["definition_key"]: dict(r) for r in con.execute(
        "SELECT * FROM customer_conversion_lineage WHERE workspace_id='local'") if r["definition_key"]}


def _inventory(con):
    entries = []
    def add(kind, identity, original, **fields):
        entries.append({"key": f"{kind}:{identity}", "kind": kind, "id": identity,
                        "original": original, "definition_digest": _digest(original), **fields})
    for row in _legacy_rows(con):
        add("saved_query", row["id"], row["query_json"], name=row["name"])
    tables = _tables(con)
    if "customer_saved_items" in tables:
        for row in con.execute("SELECT * FROM customer_saved_items WHERE workspace_id='local'"):
            if row["type"] == "query":
                add("typed_query", row["id"], {"query": row["query_json"], "selector": row["selector_json"]})
            elif row["captured_json"] not in ("{}", ""):
                add("saved_capture", row["id"], {"ref": row["target_json"], "capture_digest": _digest(row["captured_json"])})
    if "customer_alerts" in tables:
        for row in con.execute("SELECT * FROM customer_alerts WHERE workspace_id='local'"):
            add("tracker", row["id"], {"query": row["query_json"], "selector": row["live_selector_json"] if "live_selector_json" in row.keys() else None})
            if row["origin_route"]:
                add("origin", row["id"], row["origin_route"])
    if "watchlists" in tables:
        for row in con.execute("SELECT id, query FROM watchlists"):
            add("native_filter", row["id"], row["query"])
    row = con.execute("SELECT value FROM meta WHERE key='agent_profile'").fetchone()
    if row:
        add("reader_profile", "agent_profile", row[0])
    for table, key, ref in (("customer_mistake_reports", "id", "ref"),
                            ("placement_evidence", "placement_id", "ref"),
                            ("target_evidence", "target_id", "ref")):
        if table in tables:
            for row in con.execute(f"SELECT {key}, {ref} FROM {table} WHERE workspace_id='local'"):
                add("source_pin", f"{table}/{row[0]}/{_digest(row[1])[:16]}", row[1],
                    table=table, identity_column=key, row_identity=row[0])
    if "campaigns" in tables:
        for row in con.execute("SELECT * FROM campaigns"):
            add("campaign", row["id"], {"mode": row["mode"] if "mode" in row.keys() else None,
                                       "brief": row["brief_ref_json"] if "brief_ref_json" in row.keys() else "{}"})
    if "expected_placements" in tables:
        for row in con.execute("SELECT id, requirements, provenance FROM expected_placements"):
            add("requirements", row["id"], {"requirements": row["requirements"], "provenance": row["provenance"]})
    if "exports" in tables:
        for row in con.execute("SELECT * FROM exports"):
            add("artifact", row["id"], {"digest": row["digest"] if "digest" in row.keys() else None,
                                       "payload_digest": hashlib.sha256(row["payload"].encode()).hexdigest()})
    return entries


def _query_definition(con, raw, selector=None):
    original = json.loads(raw) if isinstance(raw, str) else raw
    if original.get("version") == 2:
        canonical, corrections = contracts.normalize_query(original), []
    else:
        canonical, corrections = contracts.upcast_query_v1(original)
    if canonical["ids"]:
        refs = []
        for ref in canonical["ids"]:
            qualified = ref if ref.startswith("sr1:") else citations.qualify_legacy_source_ref(con, ref)
            citations.resolve_source_state(con, qualified, media=False)
            refs.append(qualified)
        canonical["ids"] = refs
    live = json.loads(selector) if isinstance(selector, str) and selector else selector
    if live == {}:
        live = None
    if live is not None:
        checked = contracts.validate_selector(live, current_revision=None, workspace_context=None)
        if checked["scope_mode"] != "live_definition":
            live = contracts.live_selector(checked["base_query"], checked["clause"], checked["count_units"])
    effective, clause, units = query.tracker_effective(canonical, live)
    members, info = query.match_effective(con, effective, clause, units, with_revision=False)
    # The owner-view revision is unused here, but its failures still classify
    # the definition: computed once per pass, re-raised per definition.
    query.shared_view_revision(con)
    if info["total_relation"] != "exact" or info["truncation_reason"]:
        raise ops.BadInput("conversion membership is incomplete")
    membership = sorted(i["ref"] for i in members)
    return {"query": dict(canonical, page=1), "selector": live}, corrections, membership


def _declared_membership(con, raw, selector=None):
    """Conversion-only evaluation of the original corrected declared scope."""
    from . import customer
    original = json.loads(raw) if isinstance(raw, str) else raw
    live = json.loads(selector) if isinstance(selector, str) and selector else selector
    if original.get("version", 1) == 2:
        declared = contracts.normalize_query(original)
        effective, clause, units = query.tracker_effective(declared, live or None)
        rows, info = query._match_query2(con, effective)
        rows, _ = query._apply_clause(con, effective, clause, rows)
    else:
        if live:
            raise ops.BadInput("original legacy selector meaning requires review")
        criteria = {"version": 1, "mode": original.get("mode", "ads"),
                    "q": original.get("q", ""), "brand": original.get("brand") or "",
                    "show": original.get("show") or "", "from": "", "to": "",
                    "status": "", "audio": "", "sort": original.get("sort", "newest"),
                    "page": 1, "ids": None}
        result = customer._run_match(con, criteria)
        rows, info = result["items"], result
        selected = original.get("ids")
        if selected in ("", "__none__") or selected == []:
            rows = []
        elif selected is not None:
            rows = customer._apply_ids(con, rows, selected)
        dated = []
        for item in rows:
            day = contracts.publication_day(item.get("pub_date"))
            if original.get("from") or original.get("to"):
                if day is None or original.get("from") and day < original["from"][:10] \
                        or original.get("to") and day > original["to"][:10]:
                    continue
            if original.get("audio") and item.get("audio") != original["audio"]:
                continue
            if original.get("status"):
                if criteria["mode"] == "episodes":
                    evidence = customer._run_match(con, dict(criteria, mode="ads", q=""))["items"]
                    states = {row.get("status") for row in evidence if row["episode_id"] == item["episode_id"]}
                    if item.get("matching_passages"):
                        states.add("mention")
                    if original["status"] not in states:
                        continue
                else:
                    status = item.get("status")
                    if status == "confirmed" and criteria["brand"] and not any(
                            assoc.get("basis") == "confirmed" and customer._brand_key(assoc.get("brand")) == customer._brand_key(criteria["brand"])
                            for assoc in item.get("attribution", [])):
                        status = "finding"
                    if status != original["status"]:
                        continue
            dated.append(item)
        rows = dated
    if info["total_relation"] != "exact" or info["truncation_reason"]:
        raise ops.BadInput("original declared membership is incomplete")
    return sorted(item["ref"] for item in rows)


_UNREADABLE = {
    "saved_query": "legacy saved search definition is unreadable",
    "typed_query": "legacy saved query definition is unreadable",
    "tracker": "legacy tracker definition is unreadable",
    "native_filter": "legacy watchlist filter is unreadable",
    "reader_profile": "legacy reader profile is unreadable",
    "origin": "legacy origin is unreadable",
    "source_pin": "legacy source reference is unreadable",
    "campaign": "legacy campaign definition is unreadable",
    "requirements": "legacy placement requirements are unreadable",
    "saved_capture": "legacy saved capture is unreadable",
    "artifact": "legacy artifact is unreadable",
}


def _stored_object(raw, unreadable):
    """Decode a stored JSON object; anything else is a plain reason."""
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        raise ops.BadInput(unreadable)
    if not isinstance(value, dict):
        raise ops.BadInput(unreadable)
    return value


def _registered_brief_ref(reference, subject):
    """A complete brief projection, or a plain reason about `subject`."""
    if not reference:
        raise ops.BadInput(f"{subject} has no registered brief")
    if isinstance(reference, str):
        raise ops.BadInput(f"{subject}'s brief is free text, not a registered brief")
    if not isinstance(reference, dict) or not {"brief_id", "version", "digest"} <= set(reference):
        raise ops.BadInput(f"{subject}'s brief reference is incomplete")
    brief_id, version, digest = reference["brief_id"], reference["version"], reference["digest"]
    version_ok = (isinstance(version, int) and not isinstance(version, bool)) \
        or (isinstance(version, str) and version.isascii() and version.isdigit())
    if not (isinstance(brief_id, str) and brief_id and isinstance(digest, str) and version_ok):
        raise ops.BadInput(f"{subject}'s brief reference is incomplete")
    return reference


def _withheld(con, entry, disclosures, refusal) -> None:
    """An old artifact, saved capture or source pin that a rights or
    disclosure rule refuses (the Plan A old-artifact rule, design 6, or a
    source's own deny) stays stored and unchanged and is refused on use
    (403); it is not pending conversion work. Readiness means intact and
    converted, never currently allowed: the caller first runs the
    integrity checks the refusal skipped, and a digest mismatch or a
    broken capture still counts. The note reuses the refusal's own words,
    and is the plan words only where the plan rule alone refuses it (the
    plan rule can raise first while a source's deny refuses too)."""
    if citations.plan_refusal(con, disclosures):
        note = citations.PLAN_REFUSAL
    elif str(refusal) == citations.PLAN_REFUSAL:
        note = str(access.DisclosureRefused())
    else:
        note = str(refusal)
    entry["notes"] = [note]


def _capture_intact(con, captured) -> None:
    """The rights-free part of citations.validate_capture's live check of
    an sr1 evidence capture, which a refusal skips: the cited publication
    and its pinned versions' policy identities are retained. The same
    lookups and errors as validate_capture, full_disclosure and
    pa1_disclosure."""
    ref = captured.get("source_ref") or captured.get("ref")
    if captured.get("source_kind", captured.get("kind")) != "evidence" \
            or not (isinstance(ref, str) and ref.startswith("sr1:")):
        return
    state_id, _window = citations._parse_source_ref(ref)
    row = con.execute("SELECT kind, pins_json, bounds_json FROM source_states WHERE id=?",
                      (state_id,)).fetchone()
    if row is None:
        raise citations.RevisionUnavailable("capture publication is unretained")
    pins = json.loads(row["pins_json"])
    if any(isinstance(loc, dict) and loc.get("rule") == citations.PA1
           for loc in captured.get("disclosure") or []):
        citations.pa1_disclosure(con, row["kind"], pins, json.loads(row["bounds_json"]))
        return
    for version in (citations._excerpt_policy_version(row["kind"], pins),
                    pins.get("transcript_version_id")):
        if version is not None and citations._version_source_key(con, version) is None:
            raise citations.RevisionUnavailable("capture policy identity is unretained")


def _pin_intact(con, ref) -> None:
    """The rights-free checks of citations.resolve_source_state(media=False)
    that a refusal skips: the pinned payload digests, the pinned components
    (resolved as _source_qualification does) and, where the read is at
    full level, the issued window inside the pinned bounds. The publication
    row exists: resolve_source_state checks that before any rights check."""
    state_id, window = citations._parse_source_ref(ref)
    row = con.execute("SELECT kind, record_id, pins_json, bounds_json FROM source_states"
                      " WHERE id=?", (state_id,)).fetchone()
    pins = json.loads(row["pins_json"])
    citations._validate_payload_pins(con, pins)
    resolver = citations._resolve_pinned_evidence if row["kind"] == "evidence" \
        else citations._resolve_pinned_episode
    resolver(con, row["record_id"], pins, media=False)
    # An excerpt-level read ignores the window (resolve_source_state), so
    # a window is checked only where the owner's read with no deny is at
    # full level: disclosure_level's own rule (FDK-C-01).
    t_version = pins.get("transcript_version_id")
    if t_version is not None:
        excerpt = citations.transcript_policy_denied(con, citations._policy_source(con, t_version))
    else:
        e_version = citations._excerpt_policy_version(row["kind"], pins)
        excerpt = citations.untranscribed_policy_denied(
            con, citations._policy_source(con, e_version) if e_version is not None else None,
            pins["episode_id"])
    if not excerpt:
        citations._selected_span(json.loads(row["bounds_json"]), window)


def _classify(con, entry):
    entry = dict(entry, canonical=None, notes=[], membership=None,
                 classification="unchanged_equivalent", disposition="qualified")
    kind, original = entry["kind"], entry["original"]
    try:
        if kind == "saved_query" and (not isinstance(entry.get("name"), str) or not entry["name"].strip()):
            raise ops.BadInput("legacy query title requires review")
        if kind in ("saved_query", "typed_query", "tracker"):
            raw = original if kind == "saved_query" else original["query"]
            selector = None if kind == "saved_query" else original["selector"]
            # Membership only: no presentation reads (customer.MEMBERSHIP_ONLY).
            from . import customer
            only = customer.MEMBERSHIP_ONLY.set(True)
            try:
                before = _declared_membership(con, raw, selector)
                canonical, notes, membership = _query_definition(con, raw, selector)
            finally:
                customer.MEMBERSHIP_ONLY.reset(only)
            entry.update(canonical=canonical, notes=notes,
                         membership={"corrected_declared_before": before, "after": membership})
            if before != membership:
                raise ops.BadInput("original and converted declared memberships differ; reviewed correction required")
            version = json.loads(raw).get("version")
            if version != 2 or kind == "saved_query":
                entry["disposition"] = "convertible"
            if notes:
                entry["classification"] = "documented_correctness_correction"
        elif kind == "native_filter":
            decoded, reasons = search.decode_legacy_filter(json.loads(original))
            if reasons:
                raise ops.BadInput("; ".join(reasons))
            entry["canonical"] = decoded
        elif kind == "reader_profile":
            profile = json.loads(original)
            if profile.get("profile") != "reader" or not set(profile.get("scopes", [])) <= READ_SCOPES \
                    or not profile.get("owner") or not profile.get("workspace_id"):
                raise ops.BadInput("reader profile authority requires review")
            entry["canonical"] = profile
        elif kind == "origin":
            if "/searches" in original or "/saved-searches" in original:
                raise ops.BadInput("retired customer origin is historical only; no runtime replay")
            entry["canonical"] = {"origin": original, "authority": False}
        elif kind == "source_pin":
            ref = original if original.startswith("sr1:") else citations.qualify_legacy_source_ref(con, original)
            try:
                citations.resolve_source_state(con, ref, media=False)
            except access.AuthorizationError as exc:
                _pin_intact(con, ref)
                _withheld(con, entry, [], exc)
            entry["canonical"] = {"ref": ref}
        elif kind == "campaign":
            from . import campaigns, briefs
            if original["mode"] not in campaigns.CAMPAIGN_MODES:
                raise ops.BadInput("legacy campaign mode requires owner disposition")
            if original["mode"] == "supplied_requirements":
                brief = original["brief"]
                try:
                    projection = json.loads(brief) if isinstance(brief, (str, bytes)) else brief
                except ValueError:
                    projection = str(brief)
                projection = _registered_brief_ref(projection, "the campaign")
                version = briefs.get_version(con, projection["brief_id"], projection["version"])
                if version["digest"] != projection["digest"]:
                    raise ops.BadInput("legacy brief authority differs")
            entry["canonical"] = original
        elif kind == "requirements":
            from . import campaigns, briefs
            requirements = _stored_object(original["requirements"],
                                          "the placement's stored requirements are unreadable")
            if set(requirements) - (set(campaigns.SUPPORTED_REQUIREMENT_KEYS) | {"priority"}):
                raise ops.BadInput("legacy advanced requirements are unassessed and read-only")
            provenance = _stored_object(original["provenance"],
                                        "the placement's stored provenance is unreadable")
            projection = _registered_brief_ref(provenance.get("brief_ref"), "the placement")
            version = briefs.get_version(con, projection["brief_id"], projection["version"])
            if version["digest"] != projection["digest"]:
                raise ops.BadInput("legacy placement brief differs")
            entry["canonical"] = original
        elif kind == "saved_capture":
            row = con.execute("SELECT captured_json FROM customer_saved_items WHERE id=?", (entry["id"],)).fetchone()
            captured = json.loads(row[0])
            try:
                citations.validate_capture(con, captured)
            except access.AuthorizationError as exc:
                # The capture digest is checked before any rights check.
                _capture_intact(con, captured)
                _withheld(con, entry, captured.get("disclosure") or [], exc)
            entry["canonical"] = original
        elif kind == "artifact":
            try:
                exports.load_export_bytes(con, entry["id"], export=False)
            except access.AuthorizationError as exc:
                # The payload and capture digests are checked before any
                # rights check; the linked reports and the capture's live
                # check are run here.
                row = con.execute("SELECT payload, captured_json FROM exports WHERE id=?",
                                  (entry["id"],)).fetchone()
                captured = json.loads(row["captured_json"])
                exports.verify_linked_reports(con, entry["id"], hashlib.sha256(
                    row["payload"].encode("utf-8")).hexdigest(), captured)
                _capture_intact(con, captured)
                _withheld(con, entry, exports.stored_disclosure(con, entry["id"]), exc)
            entry["canonical"] = original
    except (contracts.QueryRejected, citations.CitationError, ops.BadInput,
            exports.DigestMismatch, access.AuthorizationError) as exc:
        # The project's own exceptions carry customer-safe sentences.
        entry.update(disposition="needs_review", classification="repair_required", notes=[str(exc)])
    except (ValueError, TypeError, KeyError, AttributeError):
        # Raw Python errors never reach a customer; record a plain reason.
        entry.update(disposition="needs_review", classification="repair_required",
                     notes=[_UNREADABLE.get(kind, "legacy definition is unreadable")])
    return entry


def dry_run(con):
    """Pure inventory, corrected-declared membership and current disposition."""
    lineage = _lineage(con)
    definitions = []
    with query.shared_scans(con):
        classified = [_classify(con, raw) for raw in _inventory(con)]
    for entry in classified:
        retained = lineage.get(entry["key"])
        if retained and retained["definition_digest"] == entry["definition_digest"] \
                and retained["action"] in FINAL_ACTIONS:
            entry["disposition"] = retained["action"]
            entry["item_id"] = retained["item_id"]
        definitions.append(entry)
    searches = [entry for entry in definitions if entry["kind"] == "saved_query"]
    summary = {"total": len(searches), "convertible": sum(e["disposition"] == "convertible" for e in searches),
               "needs_review": sum(e["disposition"] == "needs_review" for e in searches),
               "disposed": sum(e["disposition"] in FINAL_ACTIONS for e in searches)}
    pending = [entry["key"] for entry in definitions if entry["disposition"] not in FINAL_ACTIONS]
    return {"searches": searches, "definitions": definitions, "summary": summary,
            "pending_definitions": pending, "inventory_digest": _digest(definitions)}


def _retain(con, entry, action, actor, item_id=None):
    existing = con.execute("SELECT search_id FROM customer_conversion_lineage WHERE definition_key=?", (entry["key"],)).fetchone()
    identity = existing[0] if existing else entry["id"] if entry["kind"] == "saved_query" else \
        con.execute("SELECT MIN(COALESCE(search_id,0))-1 FROM customer_conversion_lineage").fetchone()[0]
    if identity is None:
        identity = -1
    con.execute("INSERT INTO customer_conversion_lineage (search_id,workspace_id,action,item_id,original_json,"
                "notes_json,definition_key,definition_digest,canonical_json,membership_json,actor,converted_at)"
                " VALUES (?,'local',?,?,?,?,?,?,?,?,?,?) ON CONFLICT(search_id) DO UPDATE SET"
                " action=excluded.action,item_id=excluded.item_id,definition_digest=excluded.definition_digest,"
                " canonical_json=excluded.canonical_json,membership_json=excluded.membership_json,notes_json=excluded.notes_json",
                (identity, action, item_id,
                 entry["original"] if isinstance(entry["original"], str) else _json(entry["original"]),
                 _json(entry["notes"]), entry["key"], entry["definition_digest"],
                 _json(entry["canonical"]), _json(entry["membership"]), actor, db.utcnow()))


def _write_marker(con, report):
    marker = {"state": "complete" if not report["pending_definitions"] else "blocked",
              "total": len(report["definitions"]), "disposed": len(report["definitions"]) - len(report["pending_definitions"]),
              "pending": len(report["pending_definitions"]), "inventory_digest": report["inventory_digest"], "updated_at": db.utcnow()}
    con.execute("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (MARKER_KEY, _json(marker)))
    return marker


def validate_manifest(con, manifest):
    report = dry_run(con)
    if not isinstance(manifest, dict) or set(manifest) != {"version", "inventory_digest", "dispositions"} \
            or manifest["version"] != 1 or manifest["inventory_digest"] != report["inventory_digest"] \
            or not isinstance(manifest["dispositions"], list):
        raise ops.BadInput("reviewed manifest has stale or invalid inventory authority")
    definitions = {entry["key"]: entry for entry in report["definitions"]}
    decisions = {}
    for decision in manifest["dispositions"]:
        if not isinstance(decision, dict) or set(decision) - {"key", "definition_digest", "action", "mapping"}:
            raise ops.BadInput("invalid disposition")
        key = decision.get("key")
        entry = definitions.get(key)
        if entry is None or key in decisions or decision.get("definition_digest") != entry["definition_digest"]:
            raise ops.BadInput("unknown, duplicate or stale definition disposition")
        action = decision.get("action")
        if action not in FINAL_ACTIONS:
            raise ops.BadInput("unknown disposition action")
        mapping = decision.get("mapping")
        if mapping is not None:
            expected = {"mode", "brief"} if entry["kind"] == "campaign" else \
                {"requirements", "provenance"} if entry["kind"] == "requirements" else set()
            if action != "qualified" or not isinstance(mapping, dict) or not expected or set(mapping) != expected:
                raise ops.BadInput("mapping requires an exact qualified campaign or requirement definition")
            if entry["kind"] == "requirements":
                from . import campaigns
                fields = json.loads(mapping["requirements"])
                fields.pop("priority", None)
                typed = {key: {"min": value[0], "max": value[1]} if key in ("duration_s", "position_s")
                         and isinstance(value, list) and len(value) == 2 else value for key, value in fields.items()}
                campaigns._check_requirements(typed)
            mapped = _classify(con, dict(entry, original=mapping))
            if mapped["disposition"] != "qualified":
                raise ops.BadInput("mapping has no qualified normal authority")
        elif action == "converted" and entry["disposition"] != "convertible" \
                or action == "qualified" and entry["disposition"] not in ("qualified", "converted") \
                or action == "dropped" and entry["kind"] != "saved_query":
            raise ops.BadInput("disposition cannot invent executable authority")
        decisions[key] = decision
    if set(report["pending_definitions"]) - decisions.keys():
        raise ops.MissingPrecondition("every pending definition requires reviewed disposition")
    return decisions


def _publication_member(member):
    """A tracker baseline member in the source-publication form."""
    transition = member.get("transition") if isinstance(member, dict) else None
    return isinstance(transition, dict) and "source_ref" in transition


def _legacy_transition(con, logical, member):
    """The schema-7 tracker transition of one member, on the current rows.

    Same components the schema-7 check compared: episode revision and
    transcript version, plus evidence/source/association revisions and the
    review revision for a finding.
    """
    dep = {}
    ep = con.execute("SELECT revision, transcript_version_id FROM episodes WHERE id=?",
                     (member.get("episode_id"),)).fetchone()
    if ep:
        dep["episode_revision"] = ep["revision"]
        dep["transcript_version"] = ep["transcript_version_id"]
    if logical.startswith("finding:"):
        evidence_id = logical[len("finding:"):]
        ev = con.execute("SELECT revision, source_version_id, assoc_revision FROM evidence WHERE id=?",
                         (evidence_id,)).fetchone()
        if ev:
            dep["evidence_revision"] = ev["revision"]
            dep["source_version"] = ev["source_version_id"]
            dep["assoc_revision"] = ev["assoc_revision"]
        review = con.execute("SELECT revision FROM reviews WHERE evidence_id=?",
                             (evidence_id,)).fetchone()
        if review:
            dep["review_revision"] = review["revision"]
    return dep


def _current_members(con, row):
    """Current tracker snapshot members, or the reason none is available."""
    from . import customer
    query = json.loads(row["query_json"])
    if not isinstance(query, dict) or query.get("version") != contracts.QUERY_VERSION:
        return None, "tracker definition is not converted; its baseline stays as recorded"
    live = json.loads(row["live_selector_json"] or "null") if "live_selector_json" in row.keys() else None
    try:
        snapshot = customer._alert_snapshot(con, query, live or None, json.loads(row["event_types"]))
    except (ops.BadInput, search.FilterRejected, citations.CitationError) as exc:
        return None, f"current tracker scan is unavailable: {exc}"
    return snapshot["members"], None


def _translate_baseline(con, row, current, reason):
    """Map schema-7 baseline members onto source publications.

    A member whose schema-7 transition still equals the one recomputed on
    the current rows is unchanged: it adopts the current publication, so
    the first check emits nothing for it. Any other member keeps a
    transition no publication equals, so the first check reports it.
    Nothing is dropped: a real change still emits.
    """
    before = row["baseline_json"] or "{}"
    baseline = json.loads(before)
    members = baseline.get("members") or {}
    legacy = sorted(logical for logical, member in members.items() if not _publication_member(member))
    if not legacy:
        return None
    record = {"alert_id": row["id"], "members": len(members), "legacy_members": len(legacy),
              "adopted": [], "changed_since_baseline": [], "unresolved": [],
              "legacy_audio_revision_mapped": []}
    if current is None:
        record.update(state="untranslated", reason=reason)
        return record
    from . import media
    bindings = media.legacy_audio_registrations(con)
    translated = dict(members)
    for logical in legacy:
        old = members[logical]
        unchanged = False
        if isinstance(old, dict):
            stored = old.get("transition") or {}
            recomputed = _legacy_transition(con, logical, old)
            bound = bindings.get(old.get("episode_id"))
            if bound and recomputed.get("episode_revision") == bound.get("episode_revision_after") \
                    and isinstance(stored, dict) \
                    and stored.get("episode_revision") == bound.get("episode_revision_before"):
                # The upgrade's own legacy-audio binding is the only episode
                # revision since the baseline: not a change to report.
                recomputed = dict(recomputed, episode_revision=bound["episode_revision_before"])
                if recomputed == stored:
                    record["legacy_audio_revision_mapped"].append(logical)
            unchanged = recomputed == stored
        ref = None
        if unchanged:
            now = current.get(logical)
            if now is not None:
                ref = now["ref"] if now.get("kind") == old.get("kind") else None
            elif logical.startswith(("finding:", "episode:")):
                kind, record_id = logical.split(":", 1)
                try:
                    ref = citations.current_source_ref(con, "evidence" if kind == "finding" else "episode", record_id)
                except citations.CitationError:
                    ref = None
        if ref is not None:
            translated[logical] = dict(old, ref=ref, transition={"source_ref": ref})
            record["adopted"].append(logical)
        else:
            translated[logical] = dict(old if isinstance(old, dict) else {},
                                       transition={"source_ref": None, "legacy_transition":
                                                   old.get("transition") if isinstance(old, dict) else None})
            record["changed_since_baseline" if not unchanged else "unresolved"].append(logical)
    baseline["members"] = translated
    after = json.dumps(baseline, sort_keys=True)
    con.execute("UPDATE customer_alerts SET baseline_json=? WHERE id=? AND workspace_id='local'",
                (after, row["id"]))
    record.update(state="translated",
                  baseline_digest_before=hashlib.sha256(before.encode()).hexdigest(),
                  baseline_digest_after=hashlib.sha256(after.encode()).hexdigest())
    return record


def translate_tracker_baselines(con):
    """Translate every schema-7 tracker baseline; record each translation."""
    rows = con.execute("SELECT * FROM customer_alerts WHERE workspace_id='local' ORDER BY id").fetchall() \
        if "customer_alerts" in _tables(con) else []
    pending = [row for row in rows if any(not _publication_member(member) for member in
                                          (json.loads(row["baseline_json"] or "{}").get("members") or {}).values())]
    with query.shared_scans(con):
        currents = {row["id"]: _current_members(con, row) for row in pending}
    records = []
    for row in pending:
        record = _translate_baseline(con, row, *currents[row["id"]])
        if record is None:
            continue
        records.append(record)
        lineage = con.execute("SELECT search_id, notes_json, membership_json FROM customer_conversion_lineage"
                              " WHERE definition_key=?", (f"tracker:{row['id']}",)).fetchone()
        if lineage is not None:
            notes = json.loads(lineage["notes_json"] or "[]")
            note = ("legacy tracker baseline " + (
                f"translated to source publications: {len(record['adopted'])} unchanged,"
                f" {len(record['changed_since_baseline'])} changed since the recorded baseline,"
                f" {len(record['unresolved'])} unresolved" if record["state"] == "translated"
                else "kept as recorded: " + record["reason"]))
            if note not in notes:
                notes.append(note)
            membership = json.loads(lineage["membership_json"] or "{}") or {}
            membership["baseline_translation"] = record
            con.execute("UPDATE customer_conversion_lineage SET notes_json=?, membership_json=? WHERE search_id=?",
                        (_json(notes), _json(membership), lineage["search_id"]))
    return records


@db.atomic
def apply(con, actor="owner", *, manifest=None):
    from . import customer
    decisions = validate_manifest(con, manifest) if manifest is not None else {}
    customer.rebuild_customer_show_map(con)
    """Convert qualified definitions or preserve them in enforced quarantine."""
    for row in con.execute("SELECT id FROM episodes").fetchall():
        citations.publish_episode_sources(con, row["id"], actor)
    migrated = []
    retained = _lineage(con)
    for entry in dry_run(con)["definitions"]:
        if entry["key"] in retained and entry["disposition"] in FINAL_ACTIONS and not decisions.get(entry["key"], {}).get("mapping"):
            continue
        action = "quarantined" if entry["disposition"] == "needs_review" else "qualified"
        decision = decisions.get(entry["key"])
        if decision is not None:
            action = decision["action"]
            if decision.get("mapping") is not None:
                mapping = decision["mapping"]
                if entry["kind"] == "campaign":
                    con.execute("UPDATE campaigns SET mode=?,brief_ref_json=? WHERE id=?",
                                (mapping["mode"], mapping["brief"], entry["id"]))
                else:
                    con.execute("UPDATE expected_placements SET requirements=?,provenance=? WHERE id=?",
                                (mapping["requirements"], mapping["provenance"], entry["id"]))
                entry["notes"].append("explicit reviewed mode/provenance mapping")
                entry["canonical"] = mapping
                entry["definition_digest"] = _digest(mapping)
        item_id = None
        if entry["disposition"] == "convertible" and action not in ("quarantined", "dropped"):
            action = "converted"
            if entry["kind"] == "saved_query":
                created = saved.create_item(con, {"type": "query", "title": entry["name"][:120],
                                                  **entry["canonical"]}, workspace_context=None, actor=actor)
                item_id = created["item"]["id"]
                migrated.append({"search_id": entry["id"], "item_id": item_id})
            elif entry["kind"] == "tracker":
                canonical = entry["canonical"]
                con.execute("UPDATE customer_alerts SET query_json=?,query_version=2,live_selector_json=? WHERE id=?",
                            (_json(canonical["query"]), _json(canonical["selector"] or {}), entry["id"]))
                entry["definition_digest"] = _digest(
                    {"query": _json(canonical["query"]), "selector": _json(canonical["selector"] or {})})
            elif entry["kind"] == "typed_query":
                canonical = entry["canonical"]
                selector = _json(canonical["selector"]) if canonical["selector"] is not None else None
                con.execute("UPDATE customer_saved_items SET query_json=?,selector_json=? WHERE id=?",
                            (_json(canonical["query"]), selector, entry["id"]))
                entry["definition_digest"] = _digest({"query": _json(canonical["query"]), "selector": selector})
        if entry["kind"] == "source_pin" and action == "qualified" and entry["canonical"]["ref"] != entry["original"]:
            con.execute(f"UPDATE {entry['table']} SET ref=? WHERE {entry['identity_column']}=? AND ref=?"
                        " AND workspace_id='local'",
                        (entry["canonical"]["ref"], entry["row_identity"], entry["original"]))
            entry["definition_digest"] = _digest(entry["canonical"]["ref"])
            entry["key"] = f"source_pin:{entry['table']}/{entry['row_identity']}/{entry['definition_digest'][:16]}"
        _retain(con, entry, action, actor, item_id)
    translations = translate_tracker_baselines(con)
    report = dry_run(con)
    marker = _write_marker(con, report)
    return {"migrated": migrated, "baseline_translations": translations, **report, "marker": marker}


def status(con):
    report = dry_run(con)
    return {**report, "marker": _marker(con), "store_retired": "customer_saved_searches" not in _tables(con)}


@db.atomic
def mark_dropped(con, search_id, actor="owner", reason=""):
    row = next((row for row in _legacy_rows(con) if row["id"] == search_id), None)
    if row is None:
        raise ops.BadInput("unknown saved query disposition")
    retained = _lineage(con).get(f"saved_query:{search_id}")
    if retained is not None and retained["definition_digest"] == _digest(row["query_json"]) \
            and retained["action"] in ("converted", "qualified", "dropped"):
        raise ops.BadInput("saved query already has a final disposition")
    entry = _classify(con, {"key": f"saved_query:{search_id}", "kind": "saved_query", "id": search_id,
                            "name": row["name"], "original": row["query_json"],
                            "definition_digest": _digest(row["query_json"])})
    entry["notes"].append("owner disposition: " + reason)
    _retain(con, entry, "dropped", actor)
    marker = _write_marker(con, dry_run(con))
    return {"search_id": search_id, "action": "dropped", "marker": marker, "aggregate_rev": db.aggregate_revision(con)}


@db.atomic
def retire_store(con, actor="owner"):
    report = status(con)
    if report["summary"]["disposed"] != report["summary"]["total"]:
        raise ops.MissingPrecondition("saved queries await conversion or explicit quarantine")
    if report["store_retired"]:
        return {"retired": True, "already": True}
    con.execute("DROP TABLE customer_saved_searches")
    revision = db.bump_revision(con)
    db.audit(con, actor, "customer_conversion_retire", "customer_saved_search", "store", {}, revision)
    _write_marker(con, dry_run(con))
    return {"retired": True, "already": False, "aggregate_rev": revision}


def require_executable(con, kind, identity):
    key = f"{kind}:{identity}"
    lineage = _lineage(con).get(key)
    if lineage and lineage["action"] in ("quarantined", "dropped"):
        raise ops.BadInput("legacy definition is read-only pending qualified owner disposition")


def _research_input_state(con):
    """The database part of a memo key for a computation that reads only
    research tables (db.RESEARCH_TABLES) and the schema: the research data
    token (moved by every real row change of a research table, by any
    writer; rolled back with its write) and the schema digest. None, so
    nothing is reused, when a research trigger or any view-state guard is
    missing or altered. Writes outside the research tables (an export, a
    saved item, a campaign edit) and rewrites to equal values leave it
    unchanged (READINESS-CACHE)."""
    if db.view_state_key(con) is None:
        return None
    token = db.research_state_token(con)
    if token is None:
        return None
    return "research:" + _digest([token, query._schema_digest(con)])


def source_qualification(con):
    """Pure exact-history qualification; never repairs pins on admission.

    Remembered per (workspace database, research state) through the view
    revision's memo (customer_query.view_state_memo; DEMO2 pass 9 keyed it
    on the whole view state, so an export made the next session check
    requalify every publication). Its inputs are rows of research tables
    only (source_states, source_versions, evidence, episodes, the
    evidence/review/association/episode history tables,
    observation_revisions, field_confirmations, media_artifacts,
    transcript_versions and the retention rows check_rights reads; pinned
    by tests.test_readiness_cache), the schema, and the pinned recording
    file and retention clock that media.recording_proof notes. A real
    change to any research row, a schema change, another database or a new
    process computes it again; a reuse is a copy equal to a fresh
    computation."""
    def compute():
        with citations.provenance_memo():
            result = _source_qualification(con)
        return result["qualified"], tuple(result["unavailable"])
    qualified, unavailable = query.view_state_memo(con, None, "source-qualification", compute,
                                                   state=_research_input_state)
    return {"qualified": qualified, "unavailable": list(unavailable)}


def _source_qualification(con):
    problems = []
    digests = {}  # payload digests by version id, for this pure read only
    for row in con.execute("SELECT id,kind,record_id,pins_json FROM source_states WHERE workspace_id='local'"):
        try:
            pins = json.loads(row["pins_json"])
            citations._validate_payload_pins(con, pins, digests)
            resolver = citations._resolve_pinned_evidence if row["kind"] == "evidence" else citations._resolve_pinned_episode
            resolver(con, row["record_id"], pins)
        except citations.CitationError:
            problems.append(row["id"])
    for row in con.execute("SELECT id FROM episodes"):
        try:
            citations.current_source_ref(con, "episode", row["id"])
        except citations.CitationError:
            problems.append("episode:" + row["id"])
    for row in con.execute("SELECT id FROM evidence"):
        try:
            citations.current_source_ref(con, "evidence", row["id"])
        except citations.CitationError:
            problems.append("evidence:" + row["id"])
    return {"qualified": not problems, "unavailable": problems}


_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _digest_inputs():
    """The files api_source_digest hashes. deploy/Dockerfile.web copies
    every one of them, so the image and a source checkout of the same
    commit compute the same digest."""
    root = _SOURCE_ROOT
    paths = sorted((root / "earwitness" / "app").glob("*.py"))
    paths += sorted((root / "earwitness" / "agents").glob("*.py"))
    paths += [root / "earwitness" / "pipeline" / name for name in ("artifacts.py", "jobs.py")]
    paths += [root / "scripts" / name for name in
              ("app_server.py", "app_workspace.py", "app_mcp.py", "app_worker.py")]
    return paths


def api_source_digest():
    """Bind qualification to the actual candidate implementation, not schema."""
    return _digest({str(path.relative_to(_SOURCE_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in _digest_inputs()})


def _native_query2(entry) -> bool:
    """A saved query item or tracker whose stored definition is already
    Query2 (version 2): v8 code authored it, natively or by conversion, and
    no conversion applies to it. The stored version is the same
    discriminator _classify and _query_definition use; legacy forms (no
    version, or 1) stay conversion candidates. Never the retired
    saved_query store, which only ever held legacy definitions."""
    if entry["kind"] not in ("typed_query", "tracker"):
        return False
    try:
        stored = json.loads(entry["original"]["query"])
    except (ValueError, TypeError):
        return False
    return isinstance(stored, dict) and stored.get("version") == contracts.QUERY_VERSION


def pending_definitions(con):
    """Definitions that still await conversion or explicit disposition.

    Two kinds of definition are never classified here, so a session read
    never scans the corpus for them:
    - one whose retained lineage has the same definition digest and a
      final action: dry_run overrides its classified disposition with the
      lineage action, so its pending state never depends on classifying it;
    - a saved query item or tracker stored as Query2 (_native_query2):
      every saved query and tracker created after cutover. Conversion
      never applies to it; dry_run still classifies it, and can report it
      needs_review when its own membership check fails (status and the
      cutover check show that), but it is not a pending conversion.
    Every other definition (any legacy-form query, tracker or saved
    search, and the other definition kinds) is classified in one shared
    pass and inventory order, exactly as dry_run classifies it.

    Apart from native Query2 definitions, the list equals dry_run's
    whenever dry_run returns. A crash that only classifying a skipped
    definition would hit (an uncaught exception, or classification output
    that cannot be encoded into inventory_digest) does not fail this call;
    dry_run, status and the cutover check still surface it. When an
    unencodable classification of another definition fails here, the
    error's position counts within the classified definitions, not the
    whole inventory.

    Each definition's classification is remembered on its own
    (READINESS-CACHE; see _CLASSIFIED), so a session check classifies only
    definitions that are new or whose inputs changed: an export classifies
    only its own artifact. The inventory and lineage are read on every
    call, outside the memo: customer_alerts, which the view state does not
    guard, is read only there, and each definition is in its own key.
    A failure is never remembered; a reuse equals a fresh computation.
    """
    if not con.in_transaction:
        # The definitions, the memo key and the classification: one snapshot.
        with db.transaction(con, write=False):
            return pending_definitions(con)
    lineage = _lineage(con)
    unresolved = []
    for raw in _inventory(con):
        retained = lineage.get(raw["key"])
        if retained and retained["definition_digest"] == raw["definition_digest"] \
                and retained["action"] in FINAL_ACTIONS:
            continue
        if _native_query2(raw):
            continue
        unresolved.append(raw)
    if not unresolved:
        return []
    pending = _classified_pending(con, unresolved)
    return [raw["key"] for raw, still in zip(unresolved, pending) if still]


# Cross-request memo of definition classifications (READINESS-CACHE).
#
# _classify(con, raw) is a pure function of the definition (raw: its
# definition_digest covers what the inventory read, plus a saved search's
# name), the database rows it reads, the recording file and retention
# clock media.recording_proof notes, and code. The rows it reads (an
# authorizer test in tests.test_readiness_cache pins the tables, and the
# own-row columns, on fixtures where artifacts, a saved capture, a
# supplied campaign and its placement classify on their success paths and
# the other kinds classify too; a read added on a path no fixture takes
# is not caught):
#   - research tables: keyed by the research state (_research_input_state:
#     research data token and schema digest);
#   - _CLASSIFY_TABLES, the guarded tables outside the research tables a
#     match or a brief check reads: keyed by a digest of their rows;
#   - an artifact's export row and linked campaign reports: keyed per
#     definition by _own_rows (the payload is left out: the definition
#     digest holds its hash);
#   - a saved capture's item row: only its id and captured_json, both in
#     the definition digest;
#   - unguarded derived indexes, as DEMO2 pass 10 keyed them: the
#     transcript index (its version table's digest, the segments derived
#     from those versions) and ad_reads_fts (external content of the
#     research table ad_reads, written only with its rows or by DDL);
#   - sqlite_master (schema digest), json_each (a function) and the
#     research token row.
# The key is (database path, definition key, definition digest, name,
# own-row digest) -> (shared state, pending, noted outside inputs). A
# definition whose key is unchanged has one entry, replaced when the
# state moves; an entry for a changed definition stays until LRU eviction.
# Definitions not
# found, or found under another state or with changed outside inputs,
# are classified in one shared scan pass, exactly as before; the pass's
# noted outside inputs are kept with each entry it stores. Nothing is
# stored when the pass raises, notes a transient (volatile) input, or runs
# inside a caller's shared_scans pass (whose matches were not noted
# here). Each process keeps its own memo; nothing is persisted.
_CLASSIFIED: OrderedDict = OrderedDict()
_CLASSIFIED_LIMIT = 4096
_CLASSIFIED_LOCK = threading.Lock()
_CLASSIFY_TABLES = ("ad_rules", "evidence_lineage", "provided_brief_versions")


def _classification_state(con):
    """Everything outside the definitions that classification reads, or
    None when the research state cannot be keyed (nothing is reused)."""
    research = _research_input_state(con)
    if research is None:
        return None
    digest = hashlib.sha256(research.encode())
    digest.update(b"\0" + str(query._transcript_index_state(con)).encode())
    tables = _tables(con)
    for table in _CLASSIFY_TABLES:
        digest.update(b"\1" + table.encode())
        if table in tables:
            _digest_rows(digest, con.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
    return digest.hexdigest()


def _digest_rows(digest, rows):
    """Add every value of rows to digest, typed and delimited."""
    for row in rows:
        digest.update(b"\2")
        for value in row:
            if isinstance(value, str):
                digest.update(b"s%d:" % len(value) + value.encode("utf-8", "surrogatepass"))
            elif isinstance(value, bytes):
                digest.update(b"b%d:" % len(value) + value)
            else:
                digest.update(b"v" + repr(value).encode() + b";")


def _own_rows(con, raw):
    """Digest of an artifact's own rows that its classification reads
    beyond the inventory's columns: every export column but the payload
    (the definition digest holds its hash) and its linked campaign
    reports. None for other kinds."""
    if raw["kind"] != "artifact":
        return None
    columns = ", ".join(f'"{r[1]}"' for r in con.execute("PRAGMA table_info(exports)")
                        if r[1] != "payload")
    digest = hashlib.sha256()
    _digest_rows(digest, con.execute(f"SELECT {columns} FROM exports WHERE id=?", (raw["id"],)))
    if "campaign_reports" in _tables(con):
        digest.update(b"\1")
        _digest_rows(digest, con.execute("SELECT * FROM campaign_reports WHERE export_id=?"
                                         " ORDER BY rowid", (raw["id"],)))
    return digest.hexdigest()


def _classified_pending(con, unresolved):
    """Whether each unresolved definition is still pending, in order."""
    state = _classification_state(con)
    if state is None:
        return _classify_pending(con, unresolved)
    database = next((row[2] for row in con.execute("PRAGMA database_list")
                     if row[1] == "main"), "")
    keys = [(database, raw["key"], raw["definition_digest"], raw.get("name"),
             _own_rows(con, raw)) for raw in unresolved]
    known = {}
    with _CLASSIFIED_LOCK:
        for key in keys:
            entry = _CLASSIFIED.get(key)
            if entry is not None and entry[0] == state:
                _CLASSIFIED.move_to_end(key)
                known[key] = entry
    # Entries stored by one pass share its noted inputs: check each list
    # once per call, not once per entry (the entries keep the ids alive).
    unchanged = {}
    for entry in known.values():
        if id(entry[2]) not in unchanged:
            unchanged[id(entry[2])] = media.proof_inputs_unchanged(entry[2])
    known = {key: entry[1] for key, entry in known.items() if unchanged[id(entry[2])]}
    missing = [(key, raw) for key, raw in zip(keys, unresolved) if key not in known]
    if missing:
        shared = query._SHARED_SCANS.get()
        outer_pass = shared is not None and shared[0] is con and shared[2]
        noted: list = []
        noting = media._PROOF_INPUTS.set(noted)
        try:
            computed = _classify_pending(con, [raw for _key, raw in missing])
        finally:
            media._PROOF_INPUTS.reset(noting)
        fresh = {key: still for (key, _raw), still in zip(missing, computed)}
        if not outer_pass and not any(item[0] == "volatile" for item in noted):
            kept = tuple(noted)
            with _CLASSIFIED_LOCK:
                for key, still in fresh.items():
                    _CLASSIFIED[key] = (state, still, kept)
                    _CLASSIFIED.move_to_end(key)
                while len(_CLASSIFIED) > _CLASSIFIED_LIMIT:
                    _CLASSIFIED.popitem(last=False)
        known.update(fresh)
    return [known[key] for key in keys]


def _classify_pending(con, unresolved):
    """Whether each definition is still pending, in order, from one shared
    scan pass."""
    with query.shared_scans(con):
        classified = [_classify(con, raw) for raw in unresolved]
    # dry_run digests every classified definition (inventory_digest); one
    # that cannot be encoded fails readiness here as it fails dry_run.
    _digest(classified)
    return [entry["disposition"] not in FINAL_ACTIONS for entry in classified]


def _marker(con):
    row = con.execute("SELECT value FROM meta WHERE key=?", (MARKER_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def readiness(con):
    """Schema + enforced disposition + exact source + source-bound API proof.

    Enforced disposition is pending_definitions(con): definitions with a
    final lineage disposition, and saved queries and trackers already
    stored as Query2, are not classified per read (see its documented
    deviations), so a session read does not rescan the corpus.

    The API proof is the source-bound C01-C20 qualification, except for a
    workspace the product created at the current schema
    (record_product_creation): it passes on its own creation marker.
    Every other check applies to both. See docs/hosting/workspace-readiness.md.
    """
    if db._schema_version(con) != db.SCHEMA_VERSION:
        return {"ready": False, "reason": "schema"}
    pending = pending_definitions(con)
    marker = _marker(con) or {}
    store_retired = "customer_saved_searches" not in _tables(con)
    source = source_qualification(con)
    ready = bool(store_retired and marker.get("state") == "complete"
                 and not pending and source["qualified"]
                 and (_product_created(con, marker) or _api_qualified(marker)))
    return {"ready": ready, "conversion": marker, "source": source,
            "store_retired": store_retired}


@db.atomic
def record_local_readiness(con, actor="seed"):
    """Write the local-only UI readiness receipt when this workspace has
    nothing left to convert (validation plan A1). A workspace whose old
    saved-search store holds no rows has nothing to convert: the store is
    retired and conversion is recorded complete. A workspace with legacy
    definitions keeps the explicit conversion path (upgrade --manifest):
    no receipt is written and an existing one is removed. Returns the
    receipt or None. Never touches the hosted API qualification."""
    if db._schema_version(con) != db.SCHEMA_VERSION:
        return None
    if "customer_saved_searches" in _tables(con) and not _legacy_rows(con):
        con.execute("DROP TABLE customer_saved_searches")
        db.audit(con, actor, "customer_conversion_retire", "customer_saved_search", "store",
                 {"reason": "empty store"}, db.bump_revision(con))
    report = dry_run(con)
    if report["pending_definitions"] or "customer_saved_searches" in _tables(con):
        con.execute("DELETE FROM meta WHERE key=?", (LOCAL_RECEIPT_KEY,))
        return None
    # The conversion marker is written only to complete a conversion; a
    # complete marker (and any hosted api_qualification on it) is left as
    # it is. Local readiness is the separate receipt below, so a local seed
    # never clears hosted qualification; data changes reach the hosted gate
    # only through its own rules (readiness).
    previous = _marker(con) or {}
    if previous.get("state") != "complete":
        marker = _write_marker(con, report)
        if "api_qualification" in previous:
            con.execute("UPDATE meta SET value=? WHERE key=?",
                        (_json(dict(marker, api_qualification=previous["api_qualification"])),
                         MARKER_KEY))
    receipt = {"contract": LOCAL_UI_CONTRACT, "schema": db.SCHEMA_VERSION,
               "written_by": actor, "written_at": db.utcnow()}
    con.execute("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (LOCAL_RECEIPT_KEY, _json(receipt)))
    return receipt


def local_readiness(con):
    """The local UI may open: a receipt from seed/upgrade, the current
    schema, the old store retired, conversion complete and no pending
    definition. Reads a few meta rows and the definition inventory; never
    re-checks sources (seed published them in its own transaction)."""
    if db._schema_version(con) != db.SCHEMA_VERSION:
        return {"ready": False, "reason": "schema"}
    row = con.execute("SELECT value FROM meta WHERE key=?", (LOCAL_RECEIPT_KEY,)).fetchone()
    receipt = json.loads(row[0]) if row else None
    if not receipt or receipt.get("contract") != LOCAL_UI_CONTRACT:
        return {"ready": False, "reason": "no local receipt"}
    if "customer_saved_searches" in _tables(con):
        return {"ready": False, "reason": "store"}
    if (_marker(con) or {}).get("state") != "complete":
        return {"ready": False, "reason": "conversion"}
    if pending_definitions(con):
        return {"ready": False, "reason": "pending definitions"}
    return {"ready": True, "receipt": receipt}


def check_api_receipts(local_receipt, hosted_receipt):
    """Both receipts must be complete C01-C20 runs on this source digest.
    Returns the digest; raises MissingPrecondition otherwise."""
    digest = api_source_digest()
    for mode, receipt in (("local-http", local_receipt), ("hosted-http", hosted_receipt)):
        scenarios = receipt.get("scenarios", []) if isinstance(receipt, dict) else None
        if not isinstance(scenarios, list) or receipt.get("mode") != mode or receipt.get("ok") is not True or \
                receipt.get("api_source_digest") != digest or \
                {row.get("id") for row in scenarios} != {f"C{i:02}" for i in range(1, 21)} or \
                len(scenarios) != 20 or any(row.get("ok") is not True for row in scenarios):
            raise ops.MissingPrecondition("complete source-bound original runtime qualification required")
    return digest


@db.atomic
def record_api_qualification(con, *, local_receipt, hosted_receipt):
    """Explicit operator proof publication, never a customer endpoint
    (scripts/app_hosting.py requalify-workspaces, or a coordinator tool).

    Both receipts must be genuine completed original C01–C20 executions on
    this source digest. Independent source/UI/owner gates remain separate.
    """
    digest = check_api_receipts(local_receipt, hosted_receipt)
    state = status(con)
    if not state["store_retired"] or state["pending_definitions"] or not source_qualification(con)["qualified"]:
        raise ops.MissingPrecondition("conversion, quarantine and source qualification required")
    marker = dict(state["marker"] or {}, api_qualification={
        "contract": "earwitness/customer-ui/2", "source_digest": digest,
        "local_receipt_digest": _digest(local_receipt), "hosted_receipt_digest": _digest(hosted_receipt)})
    con.execute("UPDATE meta SET value=? WHERE key=?", (_json(marker), MARKER_KEY))
    return readiness(con)


PRODUCT_ORIGIN = "created_by_product"


def _product_created(con, marker):
    """The creation marker, for this schema and this database's identity.
    Checked before _api_qualified, so a product-created workspace never
    computes the source digest."""
    return marker.get("origin") == PRODUCT_ORIGIN and marker.get("schema") == db.SCHEMA_VERSION \
        and marker.get("workspace_id") is not None and marker.get("workspace_id") == db.workspace_identity(con)


def _api_qualified(marker):
    """Source-bound API proof. Fails closed (not ready, logged) when a
    digest input cannot be read, instead of failing the session read."""
    qualified = marker.get("api_qualification") or {}
    try:
        digest = api_source_digest()
    except OSError as exc:
        from . import oplog
        oplog.log_event("readiness.api_source_digest_unavailable", level="warning",
                        error_type=type(exc).__name__, outcome="not ready")
        return False
    return qualified.get("source_digest") == digest \
        and qualified.get("contract") == "earwitness/customer-ui/2"


def record_product_creation(con, actor):
    """Mark a workspace the product just created (services.
    collab_create_workspace) ready by construction, in the caller's
    transaction. It is at the current schema and holds no legacy
    definition, so nothing can be converted: the empty old saved-search
    store is retired and the conversion marker records complete with
    origin created_by_product. No API qualification is written; readiness
    accepts this marker in its place. Refuses anything that is not a
    fresh, empty store."""
    identity = db.workspace_identity(con)
    if db._schema_version(con) != db.SCHEMA_VERSION or _marker(con) is not None or not identity:
        raise ops.MissingPrecondition("product creation marker needs a new workspace at the current schema")
    if "customer_saved_searches" in _tables(con):
        if _legacy_rows(con):
            raise ops.MissingPrecondition("a new workspace holds no legacy saved searches")
        con.execute("DROP TABLE customer_saved_searches")
        db.audit(con, actor, "customer_conversion_retire", "customer_saved_search", "store",
                 {"reason": "created by product"}, db.bump_revision(con))
    report = dry_run(con)
    if report["pending_definitions"]:
        raise ops.MissingPrecondition("a new workspace has no definition to convert")
    marker = _write_marker(con, report)
    marker = dict(marker, origin=PRODUCT_ORIGIN, schema=db.SCHEMA_VERSION, created_by=actor,
                  workspace_id=identity)
    con.execute("UPDATE meta SET value=? WHERE key=?", (_json(marker), MARKER_KEY))
    return marker
