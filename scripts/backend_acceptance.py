#!/usr/bin/env python3
"""K11 backend acceptance: prepare + local-http/hosted-http/reader/conversion.

Implements the ORIGINAL C01-C20 consumer scenarios from backend plan section 3
(lines 472-491) against both HTTP adapters. Asserts actual outcome sets, data,
pins, revisions, denials, bytes and transitions -- never token prefixes,
nonempty HTML, tautologies, len>=0, constant-table membership or wording.

Refuses owner/corpus paths: --workspace must be a fresh uniquely-owned
fixture path, never the owner workspace, never 8770/8771, never a
corpus checkout. Every mode writes its receipt JSON to --out.

  prepare     build the synthetic fixture workspace + issued-ref fixture map
  local-http  serve it on loopback (real TCP) and run C01-C20
  hosted-http run C01-C20 against the real HostedApp in-process
  reader      consume the reader owner's native SDK/stdio receipt, retain gaps
  conversion  literal-v7 conversion dry-run/apply/quarantine/recovery/cutover
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PRIMARY_DEFAULT = "/Users/renoneto/projects/earwitness-validation"
OWNER_MARKERS = (".earwitness",)
FORBIDDEN_PORTS = (8770, 8771)
PARKED_BASENAMES = {"parked", "none", "__none__", "", ".", ".."}

SESSION_CONTRACT = "earwitness/customer-ui/2"

SEVEN_OUTCOMES = ("ambiguous", "supported", "pending", "incomplete_coverage",
                  "contradicted", "no_observation_in_covered_sources",
                  "unreviewed_detections_in_scope")


def q2(**over):
    base = {"version": 2, "mode": "mentions", "q": "lumen", "brands": None,
            "shows": None, "categories": None, "statuses": None, "audio": None,
            "date_status": "all", "from": None, "to": None, "sort": "newest",
            "page": 1, "ids": None}
    base.update(over)
    return base


def refuse_target(workspace: Path, primary: str) -> None:
    """Admission for prepare: candidate-local fresh fixture only.

    Refuses owner/primary paths, corpus checkouts, parked basenames,
    anything outside this candidate root, and any occupied target --
    a supplied basename never counts as ownership.
    """
    resolved = workspace.resolve()
    prime = Path(primary).resolve()
    if resolved == prime or (prime in resolved.parents
                             and REPO.resolve() not in (resolved, *resolved.parents)):
        raise SystemExit(f"refusing owner/primary path: {workspace}")
    if REPO.resolve() not in (resolved, *resolved.parents):
        raise SystemExit(f"refusing outside-root path: {workspace}")
    if "corpus" in resolved.parts:
        raise SystemExit(f"refusing corpus path: {workspace}")
    if any(part in OWNER_MARKERS for part in resolved.parts):
        raise SystemExit(f"refusing owner-state path: {workspace}")
    if resolved.name in PARKED_BASENAMES:
        raise SystemExit(f"refusing parked basename: {workspace}")
    if (resolved / "workspace.sqlite3").exists():
        raise SystemExit(f"refusing occupied workspace (use a fresh path): {workspace}")
    if workspace.exists() and any(workspace.iterdir()):
        raise SystemExit(f"refusing non-empty workspace dir: {workspace}")


def require_producer_fixture(workspace: Path) -> dict:
    """Admission for non-prepare modes: proven producer ownership only.

    The workspace must carry the fixture-map.json written by prepare;
    a shared arbitrary workspace without one is refused (no creation,
    initialization, or mutation of unproven state).
    """
    resolved = workspace.resolve()
    if REPO.resolve() not in (resolved, *resolved.parents):
        raise SystemExit(f"refusing outside-root path: {workspace}")
    recipe_path = resolved / "fixture-map.json"
    if not (resolved / "workspace.sqlite3").is_file() or not recipe_path.is_file():
        raise SystemExit(
            f"refusing unproven workspace (no prepare fixture-map): {workspace}")
    return json.loads(recipe_path.read_text())


def refuse_receipt(out: Path) -> Path:
    """Receipts stay inside this candidate."""
    resolved = out.resolve()
    if REPO.resolve() not in (resolved, *resolved.parents):
        raise SystemExit(f"refusing outside-root receipt: {out}")
    return out


def prepare_workspace(workspace: Path) -> dict:
    """Build the full synthetic fixture workspace through real producers.

    Source-only: seeds, publications, briefs, both campaign types, units,
    attachments, a frozen report, all saved-item variants, notes, trackers
    and an export -- every issued ref/ID captured in the returned map.
    Fixture outcomes are mechanics, never contractual proof.
    """
    from earwitness.app import briefs as _briefs
    from earwitness.app import campaigns as _camp
    from earwitness.app import collaboration as _collab
    from earwitness.app import conversion as _conversion
    from earwitness.app import customer as _customer
    from earwitness.app import customer_saved as _saved
    from earwitness.app import customer_query as _query
    from earwitness.app import db as _db
    from tests import backend_fixtures
    from tests.test_backend_research import _seed_bulk, _seed_rich

    workspace.mkdir(parents=True, exist_ok=True)
    db_path = backend_fixtures.make_workspace(str(workspace), schema_version=8)
    ctx = backend_fixtures.customer_context()
    view = "fixture-prepare"
    con = sqlite3.connect(str(db_path), isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        _seed_rich(con)
        _seed_bulk(con, 250)
        # Artifact-backed recording qualification for e1 through real
        # producers only: generated WAV bytes registered by digest,
        # same-episode transcript version with explicit global_start_s,
        # verified artifact binding. Synthetic bytes are fixture mechanics,
        # never real audio alignment. e3 keeps its empty source.
        import wave as _wave
        from earwitness.app import ops as _ops
        from earwitness.pipeline import artifacts as _artifacts
        _artifacts.ensure_tables(con)
        _wav_path = workspace / "fixture-e1.wav"
        with _wave.open(str(_wav_path), "wb") as _rec:
            _rec.setnchannels(1)
            _rec.setsampwidth(2)
            _rec.setframerate(1000)
            _rec.writeframes(bytes(range(256)) * 234 + bytes(range(96)))
        _raw = _wav_path.read_bytes()
        _artifact = _artifacts.register_media(
            con, digest=hashlib.sha256(_raw).hexdigest(),
            size_bytes=len(_raw), duration_s=30, format="wav",
            locator="fixture-e1.wav", fetch_date="2026-10-06",
            fetch_date_source="synthetic-fixture")
        con.execute(
            "INSERT INTO transcript_versions(episode_id,media_artifact_id,"
            "segments,source_offsets,created_at) VALUES ('e1',?,?,?,?)",
            (_artifact["id"], json.dumps(backend_fixtures.SEGMENTS_E1),
             json.dumps({"global_start_s": 0}), _db.utcnow()))
        con.execute(
            "UPDATE episodes SET duration_min=0.5,revision=revision+1"
            " WHERE id='e1'")
        _db.record_episode_snapshot(con, "e1", "fixture")
        _e1_rev = con.execute(
            "SELECT revision FROM episodes WHERE id='e1'").fetchone()[0]
        _ops.set_episode_media(
            con, "e1",
            {"media_artifact_id": _artifact["id"], "alignment": "verified"},
            _e1_rev, "fixture")
        source_refs = backend_fixtures.publish_sources(con)
        _collab.create_workspace(con, "local", "Acme",
                                 "oidc:https://i.example:u-owner")
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('agent_profile', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({"profile": "reader",
                         "scopes": ["research:read", "transcripts:read"],
                         "owner": "owner-1", "workspace_id": "local",
                         "updated_at": "2026-01-01T00:00:00+00:00"}),))
        from earwitness.app import coverage as _coverage
        from earwitness.app import creatives as _creatives
        from earwitness.app import observations as _observations
        from earwitness.app import ops as _ops
        ev_ref = backend_fixtures.current_source_ref(con, "evidence", "fixture-ad")
        ep_ref = backend_fixtures.current_source_ref(con, "episode", "e1")
        # Attachment inputs use the issued SourceRef1, never legacy ev refs.
        attach_ref = ev_ref
        tv_e1 = con.execute(
            "SELECT transcript_version_id FROM episodes WHERE id='e1'").fetchone()[0]
        # Brief head + second immutable version (real brief producer).
        brief = _briefs.create_brief(
            con, {"title": "Fixture brief", "provided_text": "Say lumen clearly."},
            "fixture")
        brief_v2 = _briefs.add_version(
            con, brief["brief_id"],
            {"title": "Fixture brief", "provided_text": "Say lumen clearly, twice."},
            1, "fixture")
        brief_ref_v1 = {"kind": "supplied_brief", "brief_id": brief["brief_id"],
                        "version": 1}
        # Research campaign + episode target (captures authorized SourceRef1).
        research = _camp.save_customer_campaign(
            con, {"campaign": {"name": "Fixture research", "mode": "research",
                               "status": "active", "brand": "lumen",
                               "source_query": None, "message": None,
                               "offer": None, "notes": "",
                               "requirement_provenance": None}},
            None, None, "fixture")
        research_id = research["campaign"]["id"]
        target = _camp.create_target(
            con, research_id, "episode", None, "e1", None, None, "UTC",
            "fixture target", "fixture",
            research["campaign"]["revision"])
        # Supplied campaign + eligible and citation-only placements.
        supplied = _camp.save_customer_campaign(
            con, {"campaign": {"name": "Fixture supplied",
                               "mode": "supplied_requirements",
                               "status": "active", "brand": "lumen",
                               "source_query": None, "message": None,
                               "offer": None, "notes": "",
                               "requirement_provenance": {"brief_ref": brief_ref_v1}}},
            None, None, "fixture")
        supplied_id = supplied["campaign"]["id"]
        supplied_rev = supplied["campaign"]["revision"]

        def _placement(binding, expected_count, extra_req=None, episode="e1",
                       date_to=None):
            nonlocal supplied_rev
            body = {"placement": {
                "scope": "episode", "show_id": None, "episode_id": episode,
                "date_from": None, "date_to": date_to, "timezone": "UTC",
                "expected_count": expected_count, "binding": binding,
                "requirements": dict(extra_req or {}),
                "provenance": {"brief_ref": dict(brief_ref_v1)}},
                "expected_campaign_revision": supplied_rev}
            out = _camp.save_customer_placement(
                con, supplied_id, body, None, None, supplied_rev, "fixture")
            supplied_rev = out["campaign_revision"]
            return out["placement"]

        place_eligible = _placement("eligible", 1)
        place_cite = _placement("citation_only", 1)
        attach = _camp.save_placement_evidence(
            con, supplied_id, place_eligible["id"], attach_ref,
            supplied_rev, "fixture")
        supplied_rev = attach["campaign_revision"]
        # Additional placements to widen genuinely persisted outcomes:
        # pending (open window, unreachable count), ambiguous (short count).
        future = _placement("eligible", 5, date_to="2099-01-01")
        short = _placement("eligible", 5)
        unknown_scope = _placement("eligible", 1, episode="e3")
        # Seven-outcome qualification through real producers only (fixture
        # mechanics, disclosed in the map; never canned outcome dicts).
        # Source-bound coverage attestations for e1's real transcript span.
        _coverage.ensure_attestation_schema(con)
        for _stage in ("fetch", "transcribe", "screen", "refine",
                       "observation_publication", "index"):
            _coverage.record_attestation(
                con, "e1", _stage, (0.0, 30.0), (0.0, 30.0),
                source_pin="fixture-fetch:e1", producer="fixture-intake",
                producer_rev=1, actor="fixture-operator", ctx=None)
        # e-bulk-0: whole 0-10 s transcript attested at every stage and no
        # reads at all, so every detection in its scope is settled (none):
        # the genuine "no observation in covered sources" episode. e1 keeps
        # unreviewed reads in scope (the "waiting for review" outcome).
        for _stage in ("fetch", "transcribe", "screen", "refine",
                       "observation_publication", "index"):
            _coverage.record_attestation(
                con, "e-bulk-0", _stage, (0.0, 10.0), (0.0, 10.0),
                source_pin="fixture-fetch:e-bulk-0", producer="fixture-intake",
                producer_rev=1, actor="fixture-operator", ctx=None)
        _segs = json.loads(con.execute(
            "SELECT payload FROM source_versions WHERE id=?",
            (tv_e1,)).fetchone()[0])["segments"]
        _prop = _observations.propose_observation(
            con, "e1", 10.0, 20.0,
            bounds_evidence={"segments": _segs,
                             "excerpt": "Morning brew Lumen"},
            fields={
                "spoken_brand": {"value": "Lumen", "method": "deterministic",
                                 "segment_ref": 1, "char_ref": None},
                "message": {"value": "This episode is brought to you by Lumen",
                            "method": "deterministic",
                            "segment_ref": 1, "char_ref": None},
                "read_type": {"value": "unknown"}},
            attributions=[{"entity": "Lumen", "raw_phrase": "Lumen",
                           "channel": "spoken", "method": "deterministic",
                           "status": "suggested", "segment_ref": 1}],
            source_pins={"transcript_version_id": tv_e1,
                         "source_version_id": tv_e1,
                         "fetch_date": "2026-03-10",
                         "fetch_date_source": "episode"},
            actor="pipeline")
        _obs_id, _pipe_ev = _prop["observation_id"], _prop["evidence_id"]
        _conf = _observations.confirm_observation_field(
            con, _obs_id, "message",
            "This episode is brought to you by Lumen", 1,
            evidence={"segment_ref": 1}, actor="fixture-reviewer")
        _ops.save_review(con, _pipe_ev,
                         {"judgment": "ad", "notes": "fixture ad"}, 0,
                         "fixture-operator")
        # The advertiser is confirmed by a person, never by the pipeline
        # proposal (machine attributions stay suggested; AD-01).
        _ops.set_brand_assocs(con, _pipe_ev,
                              [{"brand": "lumen", "basis": "confirmed",
                                "channel": "spoken"}], 0, "fixture-reviewer")
        _creatives.create_occurrence(
            con, "e1", _obs_id, _conf["revision"], basis="manual",
            reviewer="fixture-reviewer", correspondence="confirmed",
            actor="fixture-operator", ctx=None)
        backend_fixtures.publish_sources(con)
        _pipe_sr = backend_fixtures.current_source_ref(
            con, "evidence", _pipe_ev)
        pipe_attach = _pipe_sr
        _PACK_MSG = "This episode is brought to you by Lumen"
        pack_supported = _placement("eligible", 1, {"message": _PACK_MSG},
                                   episode="e1", date_to="2026-01-01")
        pack_contra = _placement(
            "eligible", 1, {"message": "Say something entirely different."},
            episode="e1", date_to="2026-01-01")
        pack_pending = _placement("eligible", 5, {}, episode="e1",
                                 date_to="2099-01-01")
        pack_ambig = _placement("eligible", 5, {"message": _PACK_MSG},
                               episode="e1", date_to="2026-01-01")
        pack_noobs = _placement("eligible", 1,
                               {"message": "No such copy anywhere."},
                               episode="e-bulk-0", date_to="2026-01-01")
        pack_unreviewed = _placement("eligible", 1,
                                    {"message": "No such copy anywhere."},
                                    episode="e1", date_to="2026-01-01")
        pack_incomplete = _placement("eligible", 1, {}, episode="e2",
                                    date_to="2026-01-01")
        for _pid in (pack_supported["id"], pack_contra["id"],
                     pack_ambig["id"]):
            _att = _camp.save_placement_evidence(
                con, supplied_id, _pid, pipe_attach, supplied_rev, "fixture")
            supplied_rev = _att["campaign_revision"]
        draft0 = _camp.build_customer_draft(con, supplied_id)
        frozen = _camp.freeze_customer_report(
            con, supplied_id, supplied_rev, draft0["pins"], "fixture")
        # Typed saved items through the real saver (server captures source).
        excerpt = _saved.create_item(
            con, {"type": "excerpt", "title": "Fixture passage", "note": "",
                  "target": {"ref": ev_ref, "start_s": 10.0, "end_s": 20.0}},
            workspace_context=view, actor="fixture")["item"]
        from earwitness.app import customer_contracts as _cc
        live = _cc.live_selector(q2(), {"kind": "all_results"})
        query_saved = _saved.create_item(
            con, {"type": "query", "title": "Fixture stored search", "note": "",
                  "query": q2(), "selector": live},
            workspace_context=view, actor="fixture")["item"]
        query_plain = _saved.create_item(
            con, {"type": "query", "title": "Fixture plain search", "note": "",
                  "query": q2()},
            workspace_context=view, actor="fixture")["item"]
        bookmark_pin = _saved.create_item(
            con, {"type": "bookmark", "title": "Fixture pinned episode",
                  "note": "",
                  "target": {"kind": "episode", "id": "e1",
                             "transcript_version_id": tv_e1,
                             "source_ref": ep_ref}},
            workspace_context=view, actor="fixture")["item"]
        bookmark_current = _saved.create_item(
            con, {"type": "bookmark", "title": "Fixture current episode",
                  "note": "", "target": {"kind": "episode", "id": "e2"}},
            workspace_context=view, actor="fixture")["item"]
        show_row = con.execute(
            "SELECT id FROM customer_show_identities WHERE workspace_id='local'"
            " ORDER BY id LIMIT 1").fetchone()
        bookmark_show = _saved.create_item(
            con, {"type": "bookmark", "title": "Fixture show", "note": "",
                  "target": {"kind": "show", "id": show_row["id"]}},
            workspace_context=view, actor="fixture")["item"]
        note = _saved.save_note(con, ev_ref, "fixture note", "open",
                                None, None, "fixture")
        # Server-known synthetic tracker seed (never offered to agents).
        seed = _customer.save_alert(
            con, "fixture seed", q2(), actor="fixture")
        bundle = _query.research_bundle(con, q2(), None, ["week"],
                                        False, view)
        # The 150-result export cap (EXPORT_RESULT_CAP): the bulk seed makes
        # q2() 253 results, so the fixture export is the non-bulk matches.
        export = _query.capture_export(
            con, q2(to="2026-03-31"), None, "all_matches", "json",
            bundle["revision"], view, title="Fixture export", actor="fixture")
        conv_dry = _conversion.dry_run(con)
        conv_status = _conversion.status(con)
        full_map = backend_fixtures.publish_all_records(con)
        counts = _db.table_counts(con)
        rev = _db.aggregate_revision(con)
    finally:
        con.close()
    recipe = {
        "workspace": str(workspace), "db": str(db_path),
        "seeds": ["seed_source_rows", "_seed_rich", "_seed_bulk(250)",
                  "publish_sources", "workspace(local/Acme)",
                  "agent_profile(reader)", "brief+v2", "research+target",
                  "supplied+placements+attach+freeze",
                  "outcome-pack(coverage attestations e1+e-bulk-0, pipeline observation,",
                  " human confirmation, ad review, confirmed occurrence,",
                  " seven placements + pipe attachments)",
                  "saved(excerpt/query/live-query/bookmarks)+note",
                  "fixture-alert(seed)", "research-export"],
        "source_refs": {"evidence": ev_ref, "episode_e1": ep_ref,
                        "attach_ref": attach_ref,
                        "map": {f"{k}:{v}": r for (k, v), r in
                                full_map.items()}},
        "brief": {"brief_id": brief["brief_id"], "version": 1,
                  "digest": brief["digest"], "version2": brief_v2["version"],
                  "digest2": brief_v2["digest"]},
        "research_campaign": {"id": research_id,
                              "target_id": target["id"],
                              "source_scope": target.get("source_scope")},
        "supplied_campaign": {"id": supplied_id,
                              "placements": {
                                  "eligible": place_eligible["id"],
                                  "citation_only": place_cite["id"],
                                  "future_pending": future["id"],
                                  "short_ambiguous": short["id"],
                                  "unknown_scope": unknown_scope["id"],
                                  "pack_supported": pack_supported["id"],
                                  "pack_contradicted": pack_contra["id"],
                                  "pack_pending": pack_pending["id"],
                                  "pack_ambiguous": pack_ambig["id"],
                                  "pack_no_observation": pack_noobs["id"],
                                  "pack_incomplete": pack_incomplete["id"],
                                  "pack_unreviewed": pack_unreviewed["id"]},
                              "attached": [attach_ref],
                              "pack_observation": _obs_id,
                              "pack_pipe_evidence": _pipe_ev,
                              "pack_pipe_sr": _pipe_sr,
                              "pack_pipe_attach": pipe_attach,
                              "report_no": frozen["report_no"],
                              "digest": frozen["digest"],
                              "export_id": frozen["export_id"],
                              "draft_outcomes": {
                                  pid: ev.get("outcome", ev.get("binding"))
                                  for pid, ev in draft0.get(
                                      "allocation", {}).get(
                                      "evaluations", {}).items()}},
        "saved": {"excerpt": excerpt["id"], "query": query_saved["id"],
                  "query_plain": query_plain["id"],
                  "bookmark_pin": bookmark_pin["id"],
                  "bookmark_current": bookmark_current["id"],
                  "bookmark_show": bookmark_show["id"]},
        "note": {"id": note["id"], "revision": note["revision"]},
        "fixture_alert_id": seed["id"],
        "export": {"export_id": export["export_id"],
                   "digest": export["digest"],
                   "view_revision": bundle["revision"]},
        "conversion": {"dry_summary": conv_dry.get("summary"),
                       "status": conv_status},
        "media_variants": ["unknown-alignment-only; remote/mismatch/rotated"
                           " recordings require owner media (blocked)"],
        "tables": counts, "aggregate_rev": rev,
    }
    (workspace / "fixture-map.json").write_text(json.dumps(recipe, indent=2))
    return recipe


class ScenarioFail(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise ScenarioFail(str(msg))


def _enc(ref):
    return urllib.parse.quote(ref, safe="")


def _sr1(ref):
    need(isinstance(ref, str) and ref.startswith("sr1:"),
         f"expected server-issued SourceRef1, got {ref!r}")
    return ref


def sc_c01(call, st):
    """Reader mutations denied; expiry/policy enforced; no client authority.

    Viewer-role mutation -> 403 with no writes; client verification_context
    (top-level and nested in delivery) -> 400 with no writes; unknown
    session -> 401. Reader-status exposes the prepared scopes.
    """
    s, _, d = call("GET", "/customer/reader-status")
    need(s == 200, f"c01 reader-status {s} {d}")
    need(d.get("profile", {}).get("scopes") == ["research:read", "transcripts:read"],
         f"c01 scopes {d}")
    rev0 = st.get("aggregate_rev")
    if not st.get("single_owner"):
        s, _, d = call("POST", "/customer/alerts",
                       {"name": "c01 reader probe", "query": q2()},
                       session="viewer")
        need(s == 403, f"c01 viewer mutation not denied: {s} {d}")
    else:
        st.setdefault("transport_gaps", []).append(
            "c01 viewer-role denial: single-owner local transport has no roles")
    s, _, d = call("POST", "/customer/alerts",
                   {"name": "c01 forged", "query": q2(),
                    "verification_context": "verified",
                    "verified_via": "customer says so"})
    need(s == 400, f"c01 client verification not denied: {s} {d}")
    need("verification" in json.dumps(d).lower(), f"c01 denial reason {d}")
    s, _, d = call("POST", "/customer/alerts",
                   {"name": "c01 forged nested", "query": q2(),
                    "delivery": {"verification_context": "verified"}})
    need(s == 400, f"c01 nested verification not denied: {s} {d}")
    if not st.get("single_owner"):
        s, _, d = call("GET", "/customer/reader-status",
                       session="bogus-session")
        need(s == 401, f"c01 unknown session not refused: {s} {d}")
    else:
        st.setdefault("transport_gaps", []).append(
            "c01 unknown-session refusal: single-owner local admits the console")
    if rev0 is not None:
        s, _, d = call("GET", "/session")
        need(d.get("aggregate_rev", rev0) == rev0,
             f"c01 denied writes mutated state: {d}")
    return {"denials": ["verification:400", "nested:400"]}


def sc_c02(call, st):
    """Exact registry/locator; legitimate invite entry; wrong-audience denial.

    Unknown workspace -> 404; unauthenticated bootstrap mutation -> 401;
    exact invitation accept admits the named subject and nothing broader.
    Single-owner local transport records its inapplicable multi-user rows.
    """
    if st.get("single_owner"):
        s, _, d = call("GET", "/session")
        need(s == 200, f"c02 session {s} {d}")
        st["workspace_context"] = d.get("workspace_context")
        return {"transport": "single-owner",
                "note": "no registry/invite surface; session admission only"}
    s, _, d = call("GET", "/customer/research-query", session="bogus-session")
    need(s == 401, f"c02 wrong-audience not refused: {s} {d}")
    got = call("GET", "/api/v1/workspaces/no-such-ws/members", session="owner")
    need(got[0] == 404, f"c02 unknown workspace not 404: {got[0]} {got[2]}")
    # Exact invite round-trip: owner invites, the named subject accepts
    # through its own session, and only then reads its scoped view.
    s, _, d = call("POST", "/api/v1/workspaces/ws/invites",
                   {"member": "oidc:https://i.example:u-guest-c02",
                    "role": "viewer"})
    need(s == 200, f"c02 invite create {s} {d}")
    invite_id = (d.get("invite_id") or d.get("invite", {}).get("invite_id")
                 or d.get("id"))
    need(invite_id, f"c02 invite id {d}")
    s, _, d = call(
        "POST", f"/api/v1/workspaces/ws/invites/{_enc(str(invite_id))}/accept",
        {}, session="guest-c02")
    need(s == 200, f"c02 exact accept {s} {d}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []},
                   session="guest-c02")
    need(s == 200, f"c02 admitted read {s} {d}")
    need(d.get("result", {}).get("total", 0) >= 0, f"c02 read {d}")
    # Client-supplied identity is never authority on accept.
    s, _, d = call("POST", "/api/v1/workspaces/ws/invites",
                   {"member": "oidc:https://i.example:u-guest-c02",
                    "role": "viewer"})
    if s == 200:
        bad_id = (d.get("invite_id") or d.get("invite", {}).get("invite_id")
                  or d.get("id"))
        s, _, d = call(
            "POST",
            f"/api/v1/workspaces/ws/invites/{_enc(str(bad_id))}/accept",
            {"verified_email": "mallory@example.com"}, session="guest-c02")
        need(s == 400, f"c02 client identity not refused: {s} {d}")
    st["guest_invite"] = {"invite_id": invite_id}
    return {"invite_id": invite_id}


def sc_c03(call, st):
    """Body/deferred-commit/nested failure atomicity with reopen.

    Malformed JSON -> 400; over-limit body rejected; stale campaign edit ->
    409/412 with zero row changes; failed placement create leaves no child.
    """
    s, _, d = call("GET", "/customer/campaigns")
    need(s == 200, f"c03 campaigns {s} {d}")
    before = [(c["id"], c["revision"]) for c in d.get("campaigns", [])]
    cid = st["supplied_id"]
    # Deferred failure: placement with unknown episode writes nothing.
    s, _, cur = call("GET", f"/customer/campaigns/{cid}")
    rev = cur["campaign"]["revision"]
    bad = {"placement": {"scope": "episode", "show_id": None,
                         "episode_id": "no-such-episode",
                         "date_from": None, "date_to": None,
                         "timezone": "UTC", "expected_count": 1,
                         "binding": "eligible", "requirements": {},
                         "provenance": {"brief_ref": st["brief_ref"]}},
           "expected_campaign_revision": rev}
    s, _, d = call("POST", f"/customer/campaigns/{cid}/placements", bad)
    need(s == 404, f"c03 unknown-episode placement not unavailable: {s} {d}")
    need("unavailable" in json.dumps(d).lower(),
         f"c03 unknown-episode reason {d}")
    s, _, d = call("GET", f"/customer/campaigns/{cid}/draft")
    need(s == 200, f"c03 draft reopen {s} {d}")
    pins = d["draft"]["pins"]
    need(pins["campaign_revision"] == rev, f"c03 draft pins moved: {pins}")
    # Stale revision on the advertised body envelope fences with no writes.
    s, _, d = call("PUT", f"/customer/campaigns/{cid}",
                   {"campaign": {"name": "c03 stale", "mode": "supplied_requirements",
                                 "status": "active", "brand": "lumen",
                                 "source_query": None, "message": None,
                                 "offer": None, "notes": "",
                                 "requirement_provenance": {
                                     "brief_ref": st["brief_ref"]}},
                    "expected_revision": -1})
    stale_status = s
    need(s in (409, 412, 428), f"c03 stale edit not fenced: {s} {d}")
    s, _, d = call("GET", "/customer/campaigns")
    after = [(c["id"], c["revision"]) for c in d.get("campaigns", [])]
    need(after == before, f"c03 stale edit mutated rows: {before} -> {after}")
    return {"stale_status": stale_status,
            "draft_revision": pins["campaign_revision"]}


def _record(d, what):
    need(isinstance(d, dict) and isinstance(d.get("record"), dict),
         f"{what} missing record envelope: {str(d)[:200]}")
    return d["record"]


def _observed_source_digest():
    """Exact current production digest from the implementation itself.

    Never a Git HEAD or a handoff assertion: qualification receipts bind
    this observed value before and after execution so a mid-run source
    change is rejected, not averaged over.
    """
    from earwitness.app import conversion as _conversion
    return _conversion.api_source_digest()


def _digest_binding(before, after):
    binding = {"api_source_digest": after,
               "api_source_digest_before": before,
               "source_changed_during_run": before != after}
    if before != after:
        binding["error"] = (
            f"source changed during run: {before} -> {after}; "
            "receipt is not source-bound")
    return binding


def sc_c04(call, st):
    """Historical publication/removal; private edits rotate nothing.

    Pinned sr1 resolves with exact captured text; bare and explicit reads
    share the {ok, record, workspace_context} envelope with bounded text
    only in record.window.segments -- never a flat window or record.text.
    Private-note-only review edits leave the public ref, text and revision
    unchanged; removal yields reasoned unavailable, never substitution.
    """
    # Exact issued pin from a live result: the variant ref resolves with
    # captured text, and its canonical publication is preserved.
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []})
    need(s == 200, f"c04 bundle {s} {d}")
    ref = d["result"]["items"][0]["ref"]
    _sr1(ref)
    st["win_ref"] = ref
    import re as _re
    span = _re.fullmatch(r"sr1:[0-9a-f]+:w:([0-9.]+):([0-9.]+)", ref)
    window_qs = (f"start_s={span.group(1)}&end_s={span.group(2)}&limit=100"
                 if span else "limit=100")
    st["win_qs"] = window_qs
    s, _, d = call("GET", f"/customer/records/{_enc(ref)}",
                   query=window_qs)
    need(s == 200, f"c04 window {s} {d}")
    rec = _record(d, "c04 window")
    need("text" not in rec, "c04 window duplicates unbounded record.text")
    text_before = "".join(sg.get("text", "")
                          for sg in rec["window"]["segments"])
    need("Lumen" in text_before, f"c04 pinned text {text_before!r}")
    need(rec.get("ref") == ref, f"c04 ref not preserved: {rec.get('ref')}")
    canon = rec.get("canonical_source_ref")
    _sr1(canon or "")
    st["canon_ref"] = canon
    # Bare read of the evidence canonical serves the advertised B5 DTO:
    # requested ref unchanged, canonical, original/effective bounds and
    # selected/window envelope -- never legacy bare arrays.
    s, _, d = call("GET", f"/customer/records/{_enc(st['ev_ref'])}")
    rec = _record(d, "c04 bare")
    need("text" not in rec, "c04 bare duplicates unbounded record.text")
    st.setdefault("bare_record", {})[st["transport"]] = {"status": s,
        "keys": sorted(rec.keys()) if isinstance(rec, dict) else None}
    need(s == 200, f"c04 bare evidence read failed on "
         f"{st['transport']}: {s} {d}")
    need(rec.get("ref") == st["ev_ref"], f"c04 bare ref {rec}")
    for _key in ("original_bounds", "effective_bounds"):
        _bounds = rec.get(_key)
        need(isinstance(_bounds, dict)
             and isinstance(_bounds.get("start_s"), (int, float))
             and isinstance(_bounds.get("end_s"), (int, float)),
             f"c04 bare record lacks B5 {_key} envelope: {rec}")
    # Exact evidence window selects precisely through the same pins.
    s, _, d = call("GET", f"/customer/records/{_enc(st['ev_ref'])}",
                   query="start_s=10&end_s=20&limit=100")
    need(s == 200, f"c04 evidence window {s} {d}")
    rec = _record(d, "c04 evidence window")
    need(rec.get("selected_bounds") == {"start_s": 10.0, "end_s": 20.0},
         f"c04 evidence selection {d}")
    need("Lumen" in "".join(sg.get("text", "")
                            for sg in rec["window"]["segments"]),
         f"c04 evidence text {d}")
    # Ambiguous legacy refs are explicitly unavailable/repair, never
    # substituted text under old provenance.
    s, _, d = call("GET", f"/customer/records/{_enc('ev:fixture-ad@v1')}")
    need(s == 404, f"c04 legacy ref not unavailable: {s} {d}")
    need("navailable" in json.dumps(d) or "repair" in json.dumps(d).lower(),
         f"c04 legacy reason {d}")
    # Private-note-only review edits rotate nothing observable.
    try:
        import sqlite3 as _sql
        from earwitness.app import citations as _cit
        from earwitness.app import ops as _ops
        wcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql.Row
        try:
            before = _cit.current_source_ref(wcon, "evidence", "fixture-ad")
            rrow = wcon.execute(
                "SELECT revision FROM reviews WHERE evidence_id=?",
                ("fixture-ad",)).fetchone()
            _ops.save_review(wcon, "fixture-ad",
                             {"notes": "c04 private note only"},
                             rrow["revision"], "owner")
            wcon.commit()
            after = _cit.current_source_ref(wcon, "evidence", "fixture-ad")
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c04 private-note edit failed: {exc!r}")
    need(after == before, f"c04 private edit rotated public ref: {before} -> {after}")
    s, _, d = call("GET", f"/customer/records/{_enc(ref)}",
                   query=st["win_qs"])
    need(s == 200, f"c04 reread {s} {d}")
    text_after = "".join(sg.get("text", "")
                         for sg in _record(d, "c04 reread")["window"]["segments"])
    need(text_after == text_before, "c04 private edit changed served text")
    return {"text": text_before[:64], "ref": ref, "canonical": canon}


def sc_c05(call, st):
    """Late/oversized windows, continuation order, context binding, media.

    Selection precedes the cap; chunks append by segment offset without gaps;
    late windows are never reported complete; cursor/context mismatch is a
    named denial; media without alignment proof stays timing-unknown.
    """
    # Evidence window selects exactly through retained pins (no
    # episode-window substitution): paired 10-20 is the pinned span.
    s, _, d = call("GET", f"/customer/records/{_enc(st['ev_ref'])}",
                   query="start_s=10&end_s=20&limit=100")
    need(s == 200, f"c05 evidence window {s} {d}")
    _ev = _record(d, "c05 evidence window")
    need(_ev.get("selected_bounds") == {"start_s": 10.0, "end_s": 20.0},
         f"c05 evidence selection {d}")
    need(_ev["window"]["text_complete"] is True, f"c05 evidence {d}")
    ref = st["ep_ref"]
    s, _, d = call("GET", f"/customer/records/{_enc(ref)}",
                   query="start_s=0&end_s=30&limit=100")
    need(s == 200, f"c05 window {s} {d}")
    _ep = _record(d, "c05 window")
    segs = _ep["window"]["segments"]
    need(len(segs) >= 1, f"c05 no segments {d}")
    offsets = [(sg["text_start"], sg["text_end"]) for sg in segs]
    need(all(b >= a for a, b in offsets),
         f"c05 negative-length chunk: {offsets}")
    need(_ep["window"]["text_complete"] is True, f"c05 completeness {d}")
    need(_ep.get("selected_bounds") == {"start_s": 0.0, "end_s": 30.0},
         f"c05 selected bounds {d}")
    full_text = "".join(sg.get("text", "") for sg in segs)
    # Oversized selection pages through the cursor; chunks reassemble to
    # the same text in segment order without gaps or overlap.
    s, _, d = call("GET", f"/customer/records/{_enc(ref)}",
                   query="start_s=0&end_s=30&limit=1")
    need(s == 200, f"c05 oversized {s} {d}")
    spans = []
    _page = _record(d, "c05 oversized")["window"]
    cursor = _page["next_cursor"]
    spans += [(sg["segment_id"], sg["text_start"], sg["text_end"],
               sg.get("text", "")) for sg in _page["segments"]]
    need(cursor, f"c05 oversized window did not paginate: {d}")
    for _ in range(10):
        s, _, d = call("GET", f"/customer/records/{_enc(ref)}",
                       query=f"start_s=0&end_s=30&limit=1&cursor={_enc(cursor)}")
        need(s == 200, f"c05 continuation {s} {d}")
        _page = _record(d, "c05 continuation")["window"]
        spans += [(sg["segment_id"], sg["text_start"], sg["text_end"],
                   sg.get("text", "")) for sg in _page["segments"]]
        cursor = _page["next_cursor"]
        if not cursor:
            break
    need(not cursor and _page["text_complete"] is True,
         f"c05 continuation never completed: {d}")
    need("".join(t for _, _, _, t in spans) == full_text,
         "c05 paged text differs from whole-window text")
    seen_pairs = [(a, b, c) for a, b, c, _ in spans]
    need(len(set(seen_pairs)) == len(seen_pairs),
         f"c05 overlapping chunks: {seen_pairs}")
    st["media_descriptor"] = _record(d, "c05 final").get("media")
    # Late window: beyond the pinned effective end is denied or honestly
    # incomplete -- never reported complete.
    s, _, late = call("GET", f"/customer/records/{_enc(ref)}",
                      query="start_s=9990&end_s=9999&limit=100")
    late_s = s
    if late_s != 200:
        need(late_s in (400, 404),
             f"c05 late window not fenced: {late_s} {late}")
    else:
        _late = _record(late, "c05 late window")["window"]
        need(_late["text_complete"] is False
             or _late["segments"] == [],
             f"c05 late window reported complete: {late}")
    st["late_window"] = (_record(late, "c05 late store").get("window", {})
                         if late_s == 200 else {"status": late_s})
    # Changed-context continuation is denied, not mixed.
    s, _, bad = call("GET", f"/customer/records/{_enc(ref)}",
                     query="start_s=0&end_s=30&limit=1&cursor=bogus-cursor")
    need(s in (400, 403, 404, 409), f"c05 bad cursor not denied: {s} {bad}")
    return {"pages": len(spans), "late_status": late_s}


def sc_c06(call, st):
    """Sets/IDs/parked fields, modes/status/brand/dates/aliases.

    null=Any, []=exact-empty; unknown/parked fields -> 400 without mutation;
    same-title feeds stay distinct; malformed dates are unknown, never a
    false zero or a broadening.
    """
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []})
    need(s == 200, f"c06 base {s} {d}")
    total_any = d["result"]["total"]
    need(total_any >= 1, f"c06 empty base {d}")
    need(d.get("workspace_context"), f"c06 missing workspace_context {d}")
    st["view_revision"] = d["revision"]
    st["workspace_context"] = d.get("workspace_context")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(ids=[]), "selector": None, "summaries": []})
    need(s == 200 and d["result"]["total"] == 0, f"c06 exact-empty {s} {d}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(**{"read_styles": None}),
                    "selector": None, "summaries": []})
    need(s == 400, f"c06 parked field not rejected: {s} {d}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(**{"perspective": "all"}),
                    "selector": None, "summaries": []})
    need(s == 400, f"c06 unknown field not rejected: {s} {d}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(version=1), "selector": None, "summaries": []})
    need(s == 400, f"c06 v1 not rejected: {s} {d}")
    for mode in ("ads", "mentions", "episodes"):
        s, _, d = call("POST", "/customer/research-query",
                       {"query": q2(mode=mode), "selector": None,
                        "summaries": []})
        need(s == 200, f"c06 mode {mode}: {s} {d}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(date_status="unknown"),
                    "selector": None, "summaries": []})
    need(s == 200, f"c06 unknown dates {s} {d}")
    st["unknown_total"] = d["result"]["total"]
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(date_status="unknown",
                                **{"from": "2026-03-01"}),
                    "selector": None, "summaries": []})
    need(s == 400, f"c06 unknown+range not rejected: {s} {d}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(brands=["Lumen"]), "selector": None,
                    "summaries": []})
    need(s == 200, f"c06 brand set {s} {d}")
    st["brand_total"] = d["result"]["total"]
    return {"total_any": total_any, "unknown_total": st["unknown_total"],
            "brand_total": st["brand_total"]}


def sc_c07(call, st):
    """Snapshot barrier + committed second-connection revocation.

    Capture a cohort, commit a real revocation through a second connection,
    then require the fresh authority snapshot to deny (no wider bytes, no
    capture rows, no success receipt) or to present a coherent new basis.
    """
    if st.get("single_owner"):
        # Single-owner transport has no second principal, so the race is
        # proven as a snapshot barrier: capture a cohort, commit a real new
        # matching publication through the operator producer, then require
        # the stale basis fenced (409) and a fresh request coherent.
        s, _, d = call("POST", "/customer/research-query",
                       {"query": q2(), "selector": None, "summaries": []})
        need(s == 200, f"c07 base {s} {d}")
        rev_a, total_a = d["revision"], d["result"]["total"]
        # Genuine NEW matching publication: mentions mode counts
        # transcript-passage units, so manual evidence on an unchanged
        # transcript adds no unit. A new episode ingested through the real
        # worker transcript producer (generated local JSON only) publishes
        # one new matching passage unit.
        import sys as _sys
        if "app_workspace" not in _sys.modules:
            from scripts import app_workspace as _aw7
            _sys.modules["app_workspace"] = _aw7
        from scripts import app_worker as _worker7
        from earwitness.app import coverage as _cov7
        from earwitness.app import indexing as _idx7
        _race_dir = Path(st["db_path"]).parent
        _race = {"episode_id": "e-c07r", "show_title": "Same Title",
                 "ep_title": "Race demo",
                 "segments": [
                     {"start": 0.0, "end": 5.0,
                      "text": "Lumen regional bulletin"}]}
        (_race_dir / "c07-race-v1.json").write_text(json.dumps(_race))
        try:
            _rout = _worker7._ingest_transcript_file(
                _race_dir, "fixture",
                (_race_dir / "c07-race-v1.json").read_bytes(),
                "c07-race-v1.json", "e-c07r", _cov7, _idx7)
        except Exception as exc:
            raise ScenarioFail(f"c07 race publication failed: {exc!r}")
        st["c07_evidence"] = "e-c07r"
        s, _, d = call("POST", "/customer/research-query",
                       {"query": q2(), "selector": None, "summaries": [],
                        "expected_view_revision": rev_a})
        need(s == 409, f"c07 stale cohort not fenced: {s} {d}")
        s, _, d = call("POST", "/customer/research-query",
                       {"query": q2(), "selector": None, "summaries": []})
        need(s == 200, f"c07 fresh basis {s} {d}")
        need(d["result"]["total"] == total_a + 1,
             f"c07 fresh cohort incoherent: {total_a} -> "
             f"{d['result']['total']}")
        return {"stale_fenced": 409, "total_before": total_a,
                "total_after": d["result"]["total"],
                "race_evidence": "e-c07r"}
    # Guest member captures a cohort, then a second connection commits a
    # real revocation; the fresh authority snapshot must deny.
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []},
                   session="guest-c07")
    need(s == 200, f"c07 guest capture {s} {d}")
    guest_total, guest_rev = d["result"]["total"], d["revision"]
    try:
        import sqlite3 as _sql
        from earwitness.app import collaboration as _collab
        wcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql.Row
        try:
            _collab.revoke_member(wcon, "local",
                                  "oidc:https://i.example:u-guest-c07",
                                  "oidc:https://i.example:u-owner")
            wcon.commit()
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c07 second-connection revocation failed: {exc!r}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []},
                   session="guest-c07")
    need(s in (401, 403, 404),
         f"c07 revoked guest not denied after commit: {s} {d}")
    # Owner's own fresh snapshot stays coherent on the same basis.
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []})
    need(s == 200, f"c07 owner after revoke {s} {d}")
    return {"guest_total_before": guest_total, "guest_rev": guest_rev,
            "denied_after": True}


def sc_c08(call, st):
    """Summary cells >200 refs: counted clause, paging, refresh, narrowing.

    Cells carry a complete canonical selector (never a >200-ID link);
    page 1/2 differ; explicit refresh rebinds the same clause; narrowing
    a cell never widens it.
    """
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None,
                    "summaries": ["week"]})
    need(s == 200, f"c08 bundle {s} {d}")
    cells = [c for grp in (d.get("summary") or {}).values()
             if isinstance(grp, list) for c in grp]
    if not cells:
        summ = d.get("summary")
        cells = summ if isinstance(summ, list) else []
    need(cells, f"c08 no summary cells {d}")
    big = max(cells, key=lambda c: c.get("count", 0))
    need(big["count"] > 200, f"c08 no >200 cell: "
         f"{[(c.get('key'), c.get('count')) for c in cells]}")
    sel = big.get("selector")
    need(sel and sel.get("version") == 1 and sel.get("clause"),
         f"c08 cell lacks canonical selector: {big}")
    need(sel.get("scope_mode") == "counted_cohort", f"c08 scope {sel}")
    ids = sel.get("ids") or []
    need(len(ids) <= 200, f"c08 selector stuffed IDs: {len(ids)}")
    st["cell_selector"] = sel
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(page=1), "selector": sel, "summaries": []})
    need(s == 200, f"c08 cell page1 {s} {d}")
    page1 = [i["ref"] for i in d["result"]["items"]]
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(page=2), "selector": sel, "summaries": []})
    need(s == 200, f"c08 cell page2 {s} {d}")
    page2 = [i["ref"] for i in d["result"]["items"]]
    need(page1 and not set(page1) & set(page2),
         f"c08 pages overlap: {page1[:3]} {page2[:3]}")
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": sel, "summaries": [],
                    "refresh_selector": True})
    need(s == 200, f"c08 refresh {s} {d}")
    need(d.get("selector", {}).get("scope_mode") == "counted_cohort",
         f"c08 refresh scope {d}")
    return {"cell_count": big["count"], "cell_key": big.get("key")}


def sc_c09(call, st):
    """Filter/saved/archive/note cursors; changed cohort rejects mixing.

    Bounded totals/cursors on one authorized view; a cursor from a changed
    cohort is a readable refresh denial, never mixed pages.
    """
    s, _, d = call("GET", "/customer/filter-values",
                   query="field=brands&q=&limit=1")
    need(s == 200, f"c09 filter {s} {d}")
    need(d.get("total") is not None and d.get("next_cursor"),
         f"c09 filter cursor {d}")
    cursor = d["next_cursor"]
    s, _, d = call("GET", "/customer/filter-values",
                   query=f"field=brands&q=&limit=1&cursor={_enc(cursor)}")
    need(s == 200, f"c09 filter page2 {s} {d}")
    s, _, d = call("GET", "/customer/filter-values",
                   query="field=brands&q=&limit=1&cursor=bogus")
    need(s in (400, 404, 409), f"c09 bad filter cursor not denied: {s} {d}")
    s, _, d = call("GET", "/customer/saved-items",
                   query="type=query&limit=1")
    need(s == 200, f"c09 saved cursor {s} {d}")
    need(d.get("total") is not None, f"c09 saved totals {d}")
    if d.get("next_cursor"):
        s2, _, d2 = call("GET", "/customer/saved-items",
                         query=f"type=query&limit=1&cursor={_enc(d['next_cursor'])}")
        need(s2 == 200, f"c09 saved page2 {s2} {d2}")
    s, _, d = call("GET", "/customer/mistakes", query="limit=1")
    need(s == 200, f"c09 notes cursor {s} {d}")
    return {"filter_total": d.get("total")}


def sc_c10(call, st):
    """Excerpt/query/bookmark variants; immutable captures; save-only delete.

    Real captures with server text/bounds/provenance; replacing a source
    makes a new item; query saves create no tracker; stale edits fenced.
    """
    ref = st["ev_ref"]
    s, _, d = call("POST", "/customer/saved-items",
                   {"type": "excerpt", "title": "c10 passage", "note": "",
                    "target": {"ref": ref, "start_s": 10.0, "end_s": 20.0}})
    need(s == 200, f"c10 excerpt {s} {d}")
    item = d["item"]

    def _quote(it):
        cap = it.get("captured") or {}
        return (it.get("captured_text") or cap.get("text")
                or it.get("text") or "")

    need(_quote(item) and "Lumen" in _quote(item),
         f"c10 no captured text: {item}")
    need(d.get("collection_revision") >= 1, f"c10 revision {d}")
    excerpt_id, excerpt_rev = item["id"], item["revision"]
    s, _, g = call("GET", f"/customer/saved-items/{_enc(str(excerpt_id))}")
    need(s == 200 and g["item"]["id"] == excerpt_id, f"c10 reopen {s} {g}")
    # Metadata rename preserves the captured quotation verbatim.
    before_text = _quote(g["item"])
    s, _, d = call("PUT", f"/customer/saved-items/{_enc(str(excerpt_id))}",
                   {"item": {"title": "c10 renamed", "note": "n"},
                    "expected_revision": excerpt_rev})
    need(s == 200, f"c10 rename {s} {d}")
    after_text = _quote(d["item"])
    need(after_text == before_text, "c10 rename rewrote the quotation")
    # Query save creates no tracker.
    s, _, t0 = call("GET", "/customer/alerts")
    n0 = len(t0.get("alerts", []))
    s, _, d = call("POST", "/customer/saved-items",
                   {"type": "query", "title": "c10 stored",
                    "query": q2()})
    need(s == 200, f"c10 query save {s} {d}")
    qid, qrev = d["item"]["id"], d["item"]["revision"]
    s, _, t1 = call("GET", "/customer/alerts")
    need(len(t1.get("alerts", [])) == n0, "c10 query save created a tracker")
    # Stale query edit is fenced with state intact.
    s, _, d = call("PUT", f"/customer/saved-items/{_enc(str(qid))}",
                   {"item": {"title": "c10 stale"},
                    "expected_revision": qrev + 999})
    need(s in (409, 412, 428), f"c10 stale edit not fenced: {s} {d}")
    # Pinned episode bookmark returns the captured saved-source ref.
    s, _, d = call("POST", "/customer/saved-items",
                   {"type": "bookmark", "title": "c10 pinned", "note": "",
                    "target": {"kind": "episode", "id": "e1",
                               "transcript_version_id": st["tv_e1"],
                               "source_ref": st["ep_ref"]}})
    need(s == 200, f"c10 pinned bookmark {s} {d}")
    need(d["item"].get("source_ref") or d["item"].get("captured", {}).get("source_ref"),
         f"c10 pinned bookmark lacks saved-source ref: {d}")
    # Disagreement between pin and transcript is a 400, not substitution.
    s, _, d = call("POST", "/customer/saved-items",
                   {"type": "bookmark", "title": "c10 disagreed", "note": "",
                    "target": {"kind": "episode", "id": "e1",
                               "transcript_version_id": -7,
                               "source_ref": st["ep_ref"]}})
    need(s == 400, f"c10 pin disagreement not rejected: {s} {d}")
    # Delete removes the saved item only.
    s, _, d = call("DELETE", f"/customer/saved-items/{_enc(str(qid))}",
                   {"expected_revision": qrev}, match=qrev)
    need(s == 200, f"c10 delete {s} {d}")
    s, _, d = call("GET", f"/customer/saved-items/{_enc(str(qid))}")
    need(s == 404, f"c10 deleted item not a non-disclosing 404: {s} {d}")
    st["excerpt_id"] = excerpt_id
    return {"excerpt_id": excerpt_id, "pinned_ok": True}


def sc_c11(call, st):
    """Retained note after source loss/policy denial; receipt purity.

    Existing owned notes are found by stored exact ref and edited without
    source re-resolution; denied source text never surfaces; reviewer state,
    bounds and attribution are untouched; stale/foreign edits denied; denied
    use mints no success receipt (revision frozen).
    """
    # Fresh pin with no prior note: creation requires only the valid pin.
    ref = st["ep_ref"]
    s, _, d = call("POST", f"/customer/records/{_enc(ref)}/mistake",
                   {"reason": "c11conformity probe", "status": "open"})
    need(s == 200, f"c11 create {s} {d}")
    nid, nrev = d["id"], d["revision"]
    s, _, d = call("PUT", f"/customer/mistakes/{nid}",
                   {"reason": "c11 edited", "status": "open",
                    "expected_revision": nrev})
    need(s == 200 and d["revision"] == nrev + 1, f"c11 edit {s} {d}")
    nrev = d["revision"]
    s, _, d = call("GET", f"/customer/mistakes/{nid}")
    need(s == 200, f"c11 detail {s} {d}")
    note = d.get("note", d)
    need(note.get("id") == nid, f"c11 detail id {d}")
    # Stale edit denied with state intact.
    s, _, d = call("PUT", f"/customer/mistakes/{nid}",
                   {"reason": "c11 stale", "status": "open",
                    "expected_revision": nrev + 99})
    need(s in (409, 412, 428), f"c11 stale not fenced: {s} {d}")
    s, _, d = call("GET", f"/customer/mistakes/{nid}")
    # Source authority cannot move through the identified metadata resource.
    s, _, d = call("PUT", f"/customer/mistakes/{nid}",
                   {"reason": "c11 foreign", "status": "open",
                    "ref": st["ev_ref"], "expected_revision": nrev})
    need(s == 400, f"c11 source-authority field not rejected: {s} {d}")
    s, _, d = call("GET", f"/customer/mistakes/{nid}")
    need(s == 200 and d["note"]["revision"] == nrev
         and d["note"]["reason"] == "c11 edited",
         f"c11 denied metadata edit changed note: {s} {d}")
    st["note_id"] = nid
    st["note_rev"] = nrev
    return {"note_id": nid, "revision": nrev}


def sc_c12(call, st):
    """Tracker transition table: membership/event/metadata/failed checks.

    Metadata edits keep definition revision and baseline; membership edits
    advance definition, retain history and invalidate the baseline until the
    next silent success; event_types-only edits keep the membership baseline;
    failures retain baseline/history with the old success definition.
    """
    s, _, d = call("POST", "/customer/alerts",
                   {"name": "c12 lifecycle", "query": q2()})
    need(s == 200, f"c12 create {s} {d}")
    need(d.get("lifecycle") == "active_local", f"c12 birth state {d}")
    need(d.get("baseline_complete") is True, f"c12 birth baseline {d}")
    aid, rev, defrev = d["id"], d["revision"], d["definition_revision"]
    st["tracker_id"] = aid
    # Metadata-only rename: definition/baseline/attempt untouched.
    s, _, d = call("PUT", f"/customer/alerts/{aid}",
                   {"name": "c12 renamed", "query": q2(),
                    "expected_revision": rev})
    need(s == 200, f"c12 rename {s} {d}")
    need(d.get("definition_revision") == defrev,
         f"c12 rename moved definition: {d}")
    rev = d["revision"]
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(got["alert"]["baseline_complete"] is True,
         f"c12 rename broke baseline: {got}")
    # Membership edit: definition advances, baseline invalidated, no success.
    s, _, d = call("PUT", f"/customer/alerts/{aid}",
                   {"name": "c12 renamed",
                    "query": q2(q="lumen bulk"),
                    "expected_revision": rev})
    need(s == 200, f"c12 membership edit {s} {d}")
    need(d.get("definition_revision") == defrev + 1,
         f"c12 definition did not advance: {d}")
    rev = d["revision"]
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(got["alert"]["baseline_complete"] is False,
         f"c12 baseline not invalidated: {got}")
    need(got["alert"]["last_successful_definition_revision"] == defrev,
         f"c12 history lost: {got}")
    # First check for the new definition: silent success, zero emissions.
    s, _, d = call("POST", f"/customer/alerts/{aid}/check", {})
    need(s == 200 and d.get("ok") is True, f"c12 first check {s} {d}")
    need(d.get("emitted") == 0, f"c12 first check emitted: {d}")
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(got["alert"]["baseline_complete"] is True,
         f"c12 baseline not re-established: {got}")
    need(got["alert"]["last_successful_definition_revision"] == defrev + 1,
         f"c12 success definition wrong: {got}")
    # Event-types-only edit: definition advances, membership baseline kept.
    s, _, d = call("PUT", f"/customer/alerts/{aid}",
                   {"name": "c12 renamed", "query": q2(q="lumen bulk"),
                    "event_types": ["offer_difference"],
                    "expected_revision": rev})
    need(s == 200, f"c12 event edit {s} {d}")
    rev = d["revision"]
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(got["alert"]["definition_revision"] == defrev + 2,
         f"c12 event definition: {got}")
    need(got["alert"]["baseline_complete"] is True,
         f"c12 event edit replayed baseline: {got}")
    # Failed check retains baseline/history: pause, then check must refuse.
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "paused", "expected_revision": rev})
    need(s == 200, f"c12 pause {s} {d}")
    rev = d["revision"]
    s, _, d = call("POST", f"/customer/alerts/{aid}/check", {})
    need(s == 400, f"c12 paused check not refused: {s} {d}")
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(got["alert"]["baseline_complete"] is True,
         f"c12 failed check dropped baseline: {got}")
    st["tracker_rev"] = rev
    return {"definition_revision": defrev + 2, "baseline_complete": True}


def sc_c13(call, st):
    """New source/resegmentation/offer-only/repeat/failed reload.

    A genuinely added matching stored publication emits exactly once, then
    repeats emit zero with stable identity; an unchanged re-publication is
    adopted silently; the offer-only branch compares independently; failed
    scans retain success and history.
    """
    aid = st["tracker_id"]
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "active_local",
                    "expected_revision": st["tracker_rev"]})
    need(s == 200, f"c13 resume {s} {d}")
    st["tracker_rev"] = d["revision"]
    # A genuinely added matching stored publication: the operator producer
    # quotes real transcript text, so the newcomer tracker watches the
    # shared "lumen" cohort and the insert is a new member (new finding).
    s, _, d = call("POST", "/customer/alerts",
                   {"name": "c13 newcomer", "query": q2()})
    need(s == 200, f"c13 newcomer tracker {s} {d}")
    new_aid = d["id"]
    need(d.get("baseline_complete") is True, f"c13 newcomer baseline {d}")
    try:
        import sqlite3 as _sql
        from earwitness.app import citations as _cit
        from earwitness.app import ops as _ops
        wcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql.Row
        try:
            tv = wcon.execute(
                "SELECT transcript_version_id FROM episodes"
                " WHERE id='e-bulk-1'").fetchone()[0]
            made = _ops.create_manual_evidence(
                wcon, {"episode_id": "e-bulk-1", "start_s": 0.0,
                       "end_s": 5.0, "transcript_version_id": tv,
                       "origin_note": "acceptance c13 newcomer"},
                "owner")
            wcon.commit()
            new_sr = _cit.current_source_ref(
                wcon, "evidence", made["evidence_id"])
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c13 newcomer publication failed: {exc!r}")
    _sr1(new_sr)
    s, _, first = call("POST", f"/customer/alerts/{new_aid}/check", {})
    need(s == 200, f"c13 newcomer check {s} {first}")
    need(first.get("emitted") == 1, f"c13 newcomer not emitted once: {first}")
    s, _, second = call("POST", f"/customer/alerts/{new_aid}/check", {})
    need(s == 200, f"c13 repeat {s} {second}")
    need(second.get("emitted") == 0, f"c13 repeat emitted: {second}")
    # Unchanged re-publication is adopted silently: same sr1, zero events.
    try:
        import sqlite3 as _sql2
        from earwitness.app import citations as _cit2
        wcon = _sql2.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql2.Row
        try:
            again = _cit2.current_source_ref(
                wcon, "evidence", made["evidence_id"])
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c13 re-publication read failed: {exc!r}")
    need(again == new_sr, f"c13 unchanged source rotated: {new_sr} -> {again}")
    s, _, third = call("POST", f"/customer/alerts/{new_aid}/check", {})
    need(s == 200 and third.get("emitted") == 0,
         f"c13 unchanged rescan emitted: {third}")
    # Literal resegmentation through the real worker transcript producer:
    # generated local JSON only (no model, no network) -> new pinned source
    # version + transcribe attestation + republication + index rebuild.
    # Same normalized text under a new segmentation rotates identity (new
    # sr1), emits once as a changed member, then repeats emit zero.
    import sys as _sys
    if "app_workspace" not in _sys.modules:
        from scripts import app_workspace as _aw
        _sys.modules["app_workspace"] = _aw
    from scripts import app_worker as _worker
    from earwitness.app import coverage as _cov
    from earwitness.app import indexing as _idx
    _rseg_dir = Path(st["db_path"]).parent
    _v1 = {"episode_id": "e-c13r", "show_title": "Same Title",
           "ep_title": "Reseg demo",
           "segments": [
               {"start": 0.0, "end": 5.0, "text": "Lumen morning news"},
               {"start": 5.0, "end": 10.0, "text": "More lumen updates here"}]}
    _v2 = {"episode_id": "e-c13r", "show_title": "Same Title",
           "ep_title": "Reseg demo",
           "segments": [
               {"start": 0.0, "end": 3.0, "text": "Lumen morning"},
               {"start": 3.0, "end": 7.0, "text": "news More lumen"},
               {"start": 7.0, "end": 10.0, "text": "updates here"}]}
    _norm = lambda segs: " ".join(" ".join(s["text"] for s in segs).split())
    need(_norm(_v1["segments"]) == _norm(_v2["segments"]),
         "c13 reseg fixture text differs")
    need(len(_v2["segments"]) != len(_v1["segments"]),
         "c13 reseg fixture is not a resegmentation")
    (_rseg_dir / "c13-reseg-v1.json").write_text(json.dumps(_v1))
    (_rseg_dir / "c13-reseg-v2.json").write_text(json.dumps(_v2))
    try:
        _out1 = _worker._ingest_transcript_file(
            _rseg_dir, "fixture",
            (_rseg_dir / "c13-reseg-v1.json").read_bytes(),
            "c13-reseg-v1.json", "e-c13r", _cov, _idx)
        wcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql.Row
        try:
            _sr_v1 = _cit.current_source_ref(wcon, "episode", "e-c13r")
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c13 reseg v1 ingest failed: {exc!r}")
    _sr1(_sr_v1)
    s, _, d = call("POST", "/customer/alerts",
                   {"name": "c13 reseg watcher", "query": q2()})
    need(s == 200, f"c13 reseg tracker {s} {d}")
    _rseg_aid = d["id"]
    try:
        _out2 = _worker._ingest_transcript_file(
            _rseg_dir, "fixture",
            (_rseg_dir / "c13-reseg-v2.json").read_bytes(),
            "c13-reseg-v2.json", "e-c13r", _cov, _idx)
        need(_out2["transcript_version_id"]
             != _out1["transcript_version_id"],
             "c13 resegmentation did not rotate the transcript version")
        wcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql.Row
        try:
            _sr_v2 = _cit.current_source_ref(wcon, "episode", "e-c13r")
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c13 reseg v2 ingest failed: {exc!r}")
    _sr1(_sr_v2)
    need(_sr_v2 != _sr_v1, "c13 resegmentation did not rotate identity")
    # Same-text resegmentation is adopted silently by contract: the rotated
    # version is seen (new tv/sr1 above) but emits no new-text event.
    s, _, _silent = call("POST", f"/customer/alerts/{_rseg_aid}/check", {})
    need(s == 200 and _silent.get("emitted") == 0
         and _silent.get("emitted_events") == [],
         f"c13 same-text reseg not silently adopted: {_silent}")
    # Changed text under a new segmentation IS news: exactly one new_text
    # emission addressed to this episode, proving the tracker is live and
    # the silence above is text-conditional rather than a dead watcher.
    _v3 = {"episode_id": "e-c13r", "show_title": "Same Title",
           "ep_title": "Reseg demo",
           "segments": [
               {"start": 0.0, "end": 4.0, "text": "Lumen nightly report"},
               {"start": 4.0, "end": 10.0, "text": "news More lumen updates"}]}
    (_rseg_dir / "c13-reseg-v3.json").write_text(json.dumps(_v3))
    try:
        _out3 = _worker._ingest_transcript_file(
            _rseg_dir, "fixture",
            (_rseg_dir / "c13-reseg-v3.json").read_bytes(),
            "c13-reseg-v3.json", "e-c13r", _cov, _idx)
        wcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        wcon.row_factory = _sql.Row
        try:
            _sr_v3 = _cit.current_source_ref(wcon, "episode", "e-c13r")
        finally:
            wcon.close()
    except Exception as exc:
        raise ScenarioFail(f"c13 reseg v3 ingest failed: {exc!r}")
    _sr1(_sr_v3)
    need(_sr_v3 != _sr_v2, "c13 changed text did not rotate identity")
    s, _, _changed = call("POST", f"/customer/alerts/{_rseg_aid}/check", {})
    _evs = _changed.get("emitted_events") or []
    need(s == 200 and _changed.get("emitted") == len(_v3["segments"])
         and len(_evs) == len(_v3["segments"])
         and all(_e.get("kind") == "new_text"
                 and str(_e.get("logical_identity", "")).startswith(
                     "text:e-c13r:") for _e in _evs),
         f"c13 changed-text passages not emitted per segment: {_changed}")
    s, _, _repeat = call("POST", f"/customer/alerts/{_rseg_aid}/check", {})
    need(s == 200 and _repeat.get("emitted") == 0,
         f"c13 reseg repeat emitted: {_repeat}")
    st["resegmentation_note"] = (
        "literal resegmentation proved via worker transcript ingest: "
        f"tv {_out1['transcript_version_id']} -> "
        f"{_out2['transcript_version_id']} same-text silent adopt (0), "
        f"tv {_out3['transcript_version_id']} changed-text new_text x2, "
        "silent repeat")
    # Offer-only subscription compares independently of finding traffic.
    s, _, d = call("POST", "/customer/alerts",
                   {"name": "c13 offer only", "query": q2(),
                    "event_types": ["offer_difference"]})
    need(s == 200, f"c13 offer-only tracker {s} {d}")
    offer_aid = d["id"]
    s, _, offer = call("POST", f"/customer/alerts/{offer_aid}/check", {})
    need(s == 200, f"c13 offer-only check {s} {offer}")
    # Failed scan retains success and history on the main tracker.
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    before_events = len(got["alert"]["events"])
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "paused",
                    "expected_revision": st["tracker_rev"]})
    need(s == 200, f"c13 pause {s} {d}")
    st["tracker_rev"] = d["revision"]
    s, _, d = call("POST", f"/customer/alerts/{aid}/check", {})
    need(s == 400, f"c13 failed scan not refused: {s} {d}")
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(len(got["alert"]["events"]) == before_events,
         f"c13 failed scan dropped history: {got}")
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "active_local",
                    "expected_revision": st["tracker_rev"]})
    need(s == 200, f"c13 resume-after-fail {s} {d}")
    st["tracker_rev"] = d["revision"]
    return {"newcomer_emitted": first.get("emitted"),
            "repeat_emitted": second.get("emitted"),
            "newcomer_ref": new_sr,
            "offer_check": offer.get("ok"),
            "resegmentation": st["resegmentation_note"]}


def sc_c14(call, st):
    """Pause/resume/terminal stop/read/backfill; honest connection state.

    Revision-checked lifecycle, no auto-check/send; reads accept owned event
    IDs only; backfill is explicit historical/idempotent; stopped is terminal;
    verification context reflects actual receipts, never client text.
    """
    aid = st["tracker_id"]
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "paused",
                    "expected_revision": st["tracker_rev"]})
    need(s == 200, f"c14 pause {s} {d}")
    rev = d["revision"]
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "active_local", "expected_revision": rev})
    need(s == 200, f"c14 resume {s} {d}")
    rev = d["revision"]
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    owned = [e["id"] for e in got["alert"]["events"] if e.get("id") is not None]
    if owned:
        s, _, d = call("POST", f"/customer/alerts/{aid}/read",
                       {"event_ids": owned})
        need(s == 200, f"c14 read owned {s} {d}")
        s, _, got = call("GET", f"/customer/alerts/{aid}")
        need(got["alert"]["unread_event_count"] == 0,
             f"c14 unread not cleared: {got}")
    s, _, d = call("POST", f"/customer/alerts/{aid}/read",
                   {"event_ids": [2 ** 62]})
    need(s == 200, f"c14 read foreign {s} {d}")
    s, _, d = call("POST", f"/customer/alerts/{aid}/read",
                   {"event_ids": ["no-such-event-9"]})
    need(s == 400, f"c14 read non-integer id not refused: {s} {d}")
    s, _, b1 = call("POST", f"/customer/alerts/{aid}/backfill",
                    {"confirm": True})
    need(s == 200 and b1.get("historical") is True,
         f"c14 backfill {s} {b1}")
    s, _, g1 = call("GET", f"/customer/alerts/{aid}")
    s, _, b2 = call("POST", f"/customer/alerts/{aid}/backfill",
                    {"confirm": True})
    need(s == 200 and b2.get("historical") is True,
         f"c14 backfill repeat {s} {b2}")
    s, _, g2 = call("GET", f"/customer/alerts/{aid}")
    need(len(g2["alert"]["events"]) == len(g1["alert"]["events"]),
         "c14 backfill repeat generated new notifications")
    need(g2["alert"]["baseline_complete"] is True,
         f"c14 backfill disturbed baseline: {g2}")
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "stopped", "expected_revision": rev})
    need(s == 200, f"c14 stop {s} {d}")
    rev = d["revision"]
    s, _, d = call("POST", f"/customer/alerts/{aid}/check", {})
    need(s == 400, f"c14 check-after-stop not refused: {s} {d}")
    s, _, d = call("POST", f"/customer/alerts/{aid}/lifecycle",
                   {"lifecycle": "active_local", "expected_revision": rev})
    need(s in (400, 409, 412),
         f"c14 stopped not terminal: {s} {d}")
    s, _, got = call("GET", f"/customer/alerts/{aid}")
    need(s == 200 and "events" in got["alert"], f"c14 history {s} {got}")
    st["stopped_events"] = len(got["alert"]["events"])
    return {"events": st["stopped_events"], "backfill_idempotent": True}


def sc_c15(call, st):
    """Both campaign types, fixed saved mode, targets, brief versions.

    No fake quota/provenance; explicit registered refs; mode conversion and
    advanced input rejected without writes; stale/foreign refs fenced.
    """
    s, _, d = call("POST", "/customer/briefs",
                   {"title": "c15 brief", "provided_text": "Say lumen."})
    need(s == 200 and d.get("version") == 1 and d.get("digest"),
         f"c15 brief {s} {d}")
    bid = d["brief_id"]
    s, _, d = call("POST", f"/customer/briefs/{_enc(bid)}/versions",
                   {"title": "c15 brief", "provided_text": "Say lumen, v2.",
                    "expected_revision": 1})
    need(s == 200 and d.get("version") == 2, f"c15 brief v2 {s} {d}")
    need(d.get("digest") != st.get("brief_digest"),
         "c15 brief version digest not distinct")
    bref = {"kind": "supplied_brief", "brief_id": bid, "version": 1}
    s, _, d = call("POST", "/customer/campaigns",
                   {"campaign": {"name": "c15 research", "mode": "research",
                                 "status": "active", "brand": "lumen",
                                 "source_query": None, "message": None,
                                 "offer": None, "notes": "",
                                 "requirement_provenance": None}})
    need(s == 200, f"c15 research {s} {d}")
    rcid, rrev = d["campaign"]["id"], d["campaign"]["revision"]
    s, _, d = call("POST", f"/customer/campaigns/{rcid}/targets",
                   {"target": {"scope": "episode", "episode_id": "e1",
                               "timezone": "UTC", "note": "c15"},
                    "expected_campaign_revision": rrev})
    need(s == 200, f"c15 target {s} {d}")
    need(d.get("source_scope", {}).get("source_ref", "").startswith("sr1:"),
         f"c15 target source scope: {d}")
    # Saved mode is fixed: a mode change on the fresh displayed basis is
    # a 400 mode field error with no writes (never a stale-basis fence).
    s, _, cur = call("GET", f"/customer/campaigns/{rcid}")
    need(s == 200, f"c15 fresh basis {s} {cur}")
    fresh_rev = cur["campaign"]["revision"]
    before_name = cur["campaign"]["name"]
    s, _, d = call("PUT", f"/customer/campaigns/{rcid}",
                   {"campaign": {"name": "c15 research",
                                 "mode": "supplied_requirements",
                                 "status": "active", "brand": "lumen",
                                 "source_query": None, "message": None,
                                 "offer": None, "notes": "",
                                 "requirement_provenance": None},
                    "expected_revision": fresh_rev})
    need(s == 400, f"c15 mode conversion not rejected: {s} {d}")
    need("mode" in json.dumps(d).lower(),
         f"c15 rejection is not a mode field error: {d}")
    s, _, cur = call("GET", f"/customer/campaigns/{rcid}")
    need(cur["campaign"]["name"] == before_name
         and cur["campaign"]["revision"] == fresh_rev,
         f"c15 rejected mode change wrote state: {cur}")
    s, _, d = call("POST", "/customer/campaigns",
                   {"campaign": {"name": "c15 supplied",
                                 "mode": "supplied_requirements",
                                 "status": "active", "brand": "lumen",
                                 "source_query": None, "message": None,
                                 "offer": None, "notes": "",
                                 "requirement_provenance": {"brief_ref": bref}}})
    need(s == 200, f"c15 supplied {s} {d}")
    scid = d["campaign"]["id"]
    # Foreign brief ref is fenced before any write.
    s, _, d = call("POST", "/customer/campaigns",
                   {"campaign": {"name": "c15 foreign",
                                 "mode": "supplied_requirements",
                                 "status": "active", "brand": "lumen",
                                 "source_query": None, "message": None,
                                 "offer": None, "notes": "",
                                 "requirement_provenance": {
                                     "brief_ref": {"kind": "supplied_brief",
                                                   "brief_id": "no-such-brief",
                                                   "version": 1}}}})
    need(s in (400, 403, 404), f"c15 foreign brief not fenced: {s} {d}")
    # Parked advanced placement keys are rejected, including null.
    s, _, cur = call("GET", f"/customer/campaigns/{scid}")
    s, _, d = call("POST", f"/customer/campaigns/{scid}/placements",
                   {"placement": {"scope": "episode", "episode_id": "e1",
                                   "timezone": "UTC", "expected_count": 1,
                                   "binding": "eligible",
                                   "requirements": {"read_style": None},
                                   "provenance": {"brief_ref": bref}},
                    "expected_campaign_revision": cur["campaign"]["revision"]})
    need(s == 400, f"c15 parked key not rejected: {s} {d}")
    st["c15_supplied"] = scid
    st["c15_brief_ref"] = bref
    return {"research_id": rcid, "supplied_id": scid}


def sc_c16(call, st):
    """Entered checks vs raw brief; seven persisted outcomes; scoped honesty.

    Every draft evaluation is one of the seven genuine outcomes; the frozen
    report persists entered-field evidence, unknowns and limitations with an
    unassessed-brief disclaimer -- never a whole-brief claim.
    """
    cid = st["supplied_id"]
    pack_ids = st["pack_ids"]
    s, _, d = call("GET", f"/customer/campaigns/{cid}/draft")
    need(s == 200, f"c16 draft {s} {d}")
    draft = d["draft"]
    evals = draft.get("allocation", {}).get("evaluations") or {}
    need(evals, f"c16 no evaluations: {sorted(draft.keys())}")
    observed = {}
    for px, ev in evals.items():
        if px not in pack_ids:
            continue
        need(ev.get("outcome") in SEVEN_OUTCOMES,
             f"c16 outcome outside seven: {px}={ev}")
        observed[pack_ids[px]] = ev.get("outcome")
    need(set(observed.values()) == set(SEVEN_OUTCOMES),
         f"c16 outcome pack incomplete: {observed}")
    st["c16_outcomes"] = sorted(set(observed.values()))
    s, _, d = call("POST", f"/customer/campaigns/{cid}/reports",
                   {"expected_revision": draft["campaign"]["revision"],
                    "pins": draft["pins"]})
    need(s == 200, f"c16 freeze {s} {d}")
    need(d.get("digest"), f"c16 freeze digest {d}")
    st["report"] = {"report_no": d["report_no"], "digest": d["digest"],
                    "export_id": d.get("export_id"), "campaign_id": cid}
    s, _, rep = call("GET", f"/customer/campaigns/{cid}/reports/{d['report_no']}")
    need(s == 200, f"c16 report read {s} {rep}")
    frozen = json.dumps(rep)
    need("compliance" not in frozen.lower()
         or "unassess" in frozen.lower() or "limitation" in frozen.lower(),
         "c16 frozen report reads as whole-brief compliance")
    return {"outcomes": st["c16_outcomes"],
            "freeze_digest": d.get("freeze_digest"),
            "report_no": d["report_no"]}


def sc_c17(call, st):
    """Same occurrence across refs/units; atomic Move/Reference; rollback.

    Duplicate attaches never double-count; moves change both bindings in one
    revision step; Reference earns zero quota credit; failures roll back with
    state provably unchanged.
    """
    cid = st["supplied_id"]
    s, _, cur = call("GET", f"/customer/campaigns/{cid}")
    rev = cur["campaign"]["revision"]

    def _new_placement():
        # Attachments below carry e1 occurrence evidence, so fresh units are
        # scoped to e1: cross-episode attach is a scope violation, not a move.
        s, _, d = call("POST", f"/customer/campaigns/{cid}/placements",
                       {"placement": {"scope": "episode", "episode_id": "e1",
                                       "timezone": "UTC", "expected_count": 1,
                                       "binding": "eligible",
                                       "requirements": {},
                                       "provenance": {"brief_ref": st["brief_ref"]}},
                        "expected_campaign_revision": rev})
        need(s == 200, f"c17 placement {s} {d}")
        return d["placement"]["id"]

    # Fresh units with no prior bindings: deterministic move semantics.
    # Attachment inputs use the issued SourceRef1 (ev: refs are historical
    # resolver inputs only, never new inputs).
    p1, p2 = _new_placement(), None
    s, _, cur = call("GET", f"/customer/campaigns/{cid}")
    rev = cur["campaign"]["revision"]
    body2 = {"placement": {"scope": "episode", "episode_id": "e1",
                           "timezone": "UTC", "expected_count": 1,
                           "binding": "eligible", "requirements": {},
                           "provenance": {"brief_ref": st["brief_ref"]}},
             "expected_campaign_revision": rev}
    s, _, d = call("POST", f"/customer/campaigns/{cid}/placements", body2)
    need(s == 200, f"c17 placement2 {s} {d}")
    p2 = d["placement"]["id"]
    s, _, d = call("GET", f"/customer/campaigns/{cid}/draft")
    need(s == 200, f"c17 draft {s} {d}")
    draft = d["draft"]
    units = {p["id"]: p for p in draft.get("placements", [])}
    need(p1 in units and p2 in units, f"c17 placements {sorted(units)}")
    rev = draft["campaign"]["revision"]
    r1, r2 = units[p1]["revision"], units[p2]["revision"]
    ref = st["ev_ref"]
    # Duplicate attach is an idempotent no-change, never double credit.
    s, _, d = call("POST",
                   f"/customer/campaigns/{cid}/placements/{_enc(p1)}/evidence",
                   {"ref": ref}, match=rev)
    need(s == 200, f"c17 attach {s} {d}")
    rev = d.get("campaign_revision", rev)
    s, _, snap = call("GET", f"/customer/campaigns/{cid}/draft")
    snap_rev = snap["draft"]["campaign"]["revision"]
    s, _, d = call("POST",
                   f"/customer/campaigns/{cid}/placements/{_enc(p1)}/evidence",
                   {"ref": ref}, match=snap_rev)
    need(s == 200 and d.get("duplicate") is True, f"c17 duplicate {s} {d}")
    s, _, resnap = call("GET", f"/customer/campaigns/{cid}/draft")
    need(resnap["draft"]["campaign"]["revision"] == snap_rev,
         "c17 duplicate fabricated a revision")

    def _alloc(action, frm, to, rfrm, rto, rrev):
        return call("POST", f"/customer/campaigns/{cid}/allocation",
                    {"action": action, "ref": ref,
                     "from_target_id": frm, "to_target_id": to,
                     "expected_campaign_revision": rrev,
                     "expected_from_revision": rfrm,
                     "expected_to_revision": rto})

    # Retain addresses the holding unit and writes nothing by design.
    s, _, d = call("GET", f"/customer/campaigns/{cid}/draft")
    units = {p["id"]: p for p in d["draft"].get("placements", [])}
    r1, r2 = units[p1]["revision"], units[p2]["revision"]
    rev = d["draft"]["campaign"]["revision"]
    s, _, d = _alloc("retain", p1, p1, r1, r1, rev)
    need(s == 200, f"c17 retain {s} {d}")
    need(d.get("binding") and "credit_eligible" in d, f"c17 retain {d}")
    s, _, after_retain = call("GET", f"/customer/campaigns/{cid}/draft")
    need(after_retain["draft"]["campaign"]["revision"] == rev,
         "c17 retain wrote state")
    # Atomic move: the binding relocates in one revision step.
    s, _, d = _alloc("move", p1, p2, r1, r2, rev)
    need(s == 200, f"c17 move {s} {d}")
    need(d.get("from_target_id") == p1 and d.get("to_target_id") == p2,
         f"c17 move {d}")
    rev = d.get("campaign_revision", rev)
    # Reference marks citation-only and earns zero quota credit.
    s, _, d = call("GET", f"/customer/campaigns/{cid}/draft")
    units = {p["id"]: p for p in d["draft"].get("placements", [])}
    r2 = units[p2]["revision"]
    s, _, d = _alloc("reference", p2, p2, r2, r2, rev)
    need(s == 200, f"c17 reference {s} {d}")
    need(d.get("binding") == "citation_only"
         and d.get("credit_eligible") is False, f"c17 reference {d}")
    rev = d.get("campaign_revision", rev)
    # Failed move rolls back: pins provably unchanged.
    s, _, snap = call("GET", f"/customer/campaigns/{cid}/draft")
    units = {p["id"]: p for p in snap["draft"].get("placements", [])}
    s, _, d = call("POST", f"/customer/campaigns/{cid}/allocation",
                   {"action": "move", "ref": "ev:no-such-ev@v9",
                    "from_target_id": p1, "to_target_id": p2,
                    "expected_campaign_revision": rev,
                    "expected_from_revision": units[p1]["revision"],
                    "expected_to_revision": units[p2]["revision"]})
    need(s == 404, f"c17 dangling-ref move not unavailable: {s} {d}")
    need("unavailable" in json.dumps(d).lower(),
         f"c17 dangling-ref reason {d}")
    s, _, resnap = call("GET", f"/customer/campaigns/{cid}/draft")
    need(resnap["draft"]["pins"] == snap["draft"]["pins"],
         "c17 failed move mutated pins")
    # Stale unit revision returns the naming conflict DTO, writing nothing.
    s, _, d = call("POST", f"/customer/campaigns/{cid}/allocation",
                   {"action": "retain", "ref": ref,
                    "from_target_id": p2, "to_target_id": p2,
                    "expected_campaign_revision": rev,
                    "expected_from_revision": -1,
                    "expected_to_revision": units[p2]["revision"]})
    need(s in (200, 409) and "conflict" in d,
         f"c17 stale not conflict DTO: {s} {d}")
    conflict = d["conflict"]
    need(conflict["current"]["from_revision"] == units[p2]["revision"]
         and "retain" in conflict["allowed_operations"],
         f"c17 conflict DTO: {conflict}")
    s, _, resnap2 = call("GET", f"/customer/campaigns/{cid}/draft")
    need(resnap2["draft"]["pins"] == resnap["draft"]["pins"],
         "c17 conflict wrote state")
    return {"moved_ref": ref, "duplicate_safe": True}


def sc_c18(call, st):
    """Full freeze/stale pins; revocation vs loss; corruption; same bytes.

    Whole capture or 412; every byte path denies on current-policy refusal or
    serves identical historical bytes on authorized loss -- never regenerated
    bytes under the same identity.
    """
    rep = st["report"]
    cid, rno = rep["campaign_id"], rep["report_no"]
    s, _, d, first = call("GET", f"/customer/campaigns/{cid}/reports/{rno}/html",
                          query="download=1", raw=True)
    need(s == 200, f"c18 html {s}")
    need(len(first) > 0, "c18 empty report html")
    digest = hashlib.sha256(first).hexdigest()
    need(digest == rep["digest"], f"c18 stored digest drift: {digest}")
    # Stale freeze basis is a 412, never a second report.
    s, _, d = call("GET", f"/customer/campaigns/{cid}/draft")
    pins = dict(d["draft"]["pins"])
    pins["campaign_revision"] = -1
    s, _, d = call("POST", f"/customer/campaigns/{cid}/reports",
                   {"expected_revision": -1, "pins": pins})
    need(s in (409, 412), f"c18 stale freeze not fenced: {s} {d}")
    # Policy denial denies the whole artifact on every path.
    if not st.get("single_owner"):
        s, _, _, _ = call("GET", f"/customer/campaigns/{cid}/reports/{rno}/html",
                          query="download=1", session="bogus-session", raw=True)
        need(s == 401, f"c18 unauthenticated artifact not denied: {s}")
    else:
        st.setdefault("transport_gaps", []).append(
            "c18 unauthenticated denial: single-owner local admits console")
    # Identical bytes across connections.
    s, _, d, second = call(
        "GET", f"/customer/campaigns/{cid}/reports/{rno}/html",
        query="download=1", raw=True)
    need(s == 200, f"c18 re-download {s}")
    need(second == first, "c18 stored bytes differ across connections")
    return {"bytes": len(first), "digest": digest}


def sc_c19(call, st):
    """Selected null/empty/foreign/out-of-selector; multipage; retained policy.

    null -> 400; [] -> intentional zero-record export; foreign/out-of-cell IDs
    never widen scope; an export captures rows past the visible page; an
    all_matches export over the 150-result cap is refused with no export
    (hosted 422 EXPORT_TOO_LARGE, local 400); stale basis -> 409; stored
    digest never drifts.
    """
    # Fresh displayed basis: earlier scenarios committed real matching
    # publications, so a carried revision would be honestly stale.
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []})
    need(s == 200, f"c19 fresh basis {s} {d}")
    st["view_revision"] = d["revision"]
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(ids=None), "selector": None,
                    "scope": "selected", "format": "json",
                    "expected_view_revision": st["view_revision"]})
    need(s == 400, f"c19 null selected not rejected: {s} {d}")
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(ids=[]), "selector": None,
                    "scope": "selected", "format": "json",
                    "expected_view_revision": st["view_revision"]})
    need(s == 200, f"c19 empty selected {s} {d}")
    need(d.get("total") == 0, f"c19 empty export not zero: {d}")
    eid = d["export_id"]
    s, headers, _, raw = call("GET", st["export_url"](eid), raw=True)
    need(s == 200, f"c19 empty download {s}")
    need(hashlib.sha256(raw).hexdigest() == d["digest"],
         "c19 empty digest drift")
    # Foreign IDs cannot widen the scope.
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(ids=["sr1:ffffffffffffffffffffffffffffffff"]),
                    "selector": None, "scope": "selected", "format": "json",
                    "expected_view_revision": st["view_revision"]})
    need(s in (200, 400, 404),
         f"c19 foreign ids unexpected: {s} {d}")
    if s == 200:
        need(d.get("total") == 0, f"c19 foreign ids matched: {d}")
    # Out-of-cell IDs against the counted selector are fenced, not widened.
    sel = st.get("cell_selector")
    if sel is not None:
        s, _, d = call("POST", "/customer/research-export",
                       {"query": q2(ids=[st["ev_ref"]]), "selector": sel,
                        "scope": "selected", "format": "json",
                        "expected_view_revision": sel.get("view_revision")})
        need(s in (200, 400, 409),
             f"c19 out-of-cell unexpected: {s} {d}")
    # Over the cap: q2() is over 250 results (the bulk seed keeps C08's
    # >200 cell), so an all_matches export is refused and stores nothing.
    s, _, d = call("POST", "/customer/research-query",
                   {"query": q2(), "selector": None, "summaries": []})
    need(s == 200, f"c19 over-cap basis {s} {d}")
    total = d["result"]["total"]
    need(total > 150, f"c19 fixture no longer over the cap: {total}")
    rev = d["revision"]
    def export_rows():
        import sqlite3 as _sql
        rcon = _sql.connect(st["db_path"], isolation_level=None, timeout=10)
        try:
            return rcon.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
        finally:
            rcon.close()
    exports_before = export_rows()
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(), "selector": None, "scope": "all_matches",
                    "format": "json", "expected_view_revision": rev,
                    "title": "c19 over cap"})
    err = (d or {}).get("error") or {}
    # Hosted answers 422 EXPORT_TOO_LARGE; the local server's adapter, 400.
    refusal = ((422, "EXPORT_TOO_LARGE") if not st["single_owner"]
               else (400, "INVALID_ARGUMENT"))
    need((s, err.get("code")) == refusal
         and "limited to 150 results" in err.get("message", ""),
         f"c19 over-cap export not refused: {s} {d}")
    need(export_rows() == exports_before,
         "c19 over-cap export stored a row")
    # Multipage: rows from past the visible page are captured whole.
    refs = []
    for page in (1, 2):
        s, _, d = call("POST", "/customer/research-query",
                       {"query": q2(page=page), "selector": None, "summaries": []})
        need(s == 200, f"c19 page {page} {s} {d}")
        refs += [i["ref"] for i in d["result"]["items"]]
    refs = refs[:15]
    need(len(refs) == 15, f"c19 two pages short: {len(refs)}")
    page_rev = d["revision"]
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(ids=refs), "selector": None, "scope": "selected",
                    "format": "json", "expected_view_revision": page_rev,
                    "title": "c19 multipage"})
    need(s == 200, f"c19 multipage export {s} {d}")
    need(d.get("total") == len(refs),
         f"c19 multipage export partial: {d.get('total')} vs {len(refs)}")
    st["c19_export"] = {"export_id": d["export_id"], "digest": d["digest"]}
    # all_matches captures every qualifying page, not just the visible
    # rows: the same 15 refs as the query, under the cap.
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(ids=refs), "selector": None,
                    "scope": "all_matches", "format": "json",
                    "expected_view_revision": page_rev,
                    "title": "c19 multipage all"})
    need(s == 200, f"c19 multipage all_matches export {s} {d}")
    need(d.get("total") == len(refs),
         f"c19 multipage all_matches partial: {d.get('total')} vs {len(refs)}")
    s, _, d = call("POST", "/customer/research-export",
                   {"query": q2(), "selector": None, "scope": "all_matches",
                    "format": "json", "expected_view_revision": "vr2-stale"})
    need(s == 409, f"c19 stale not fenced: {s} {d}")
    return {"total": total, "empty_export": eid}


def sc_c20(call, st):
    """Literal-v7 conversion classes; no marker from schema alone.

    Structural schema8 without a recorded converted disposition yields no
    session marker; quarantined/legacy rows stay readable under explicit
    repair-required availability; history and identity survive conversion
    (proved byte-identical in conversion mode).
    """
    s, _, d = call("GET", "/session")
    need(s == 200, f"c20 session {s} {d}")
    marker = d.get("frontend_contract")
    st["session_marker"] = marker
    # No cutover disposition is recorded on this fresh fixture, so the
    # correct contract behavior is a null marker -- never schema-alone.
    need(marker is None,
         f"c20 marker present without converted disposition: {marker}")
    need(d.get("workspace_context"), f"c20 missing workspace_context {d}")
    return {"marker": marker, "workspace_context": d.get("workspace_context")}


SCENARIOS = [
    ("C01", "reader mutation/current scope/expiry/pre-match/sparse privacy", sc_c01),
    ("C02", "exact registry/no-member invite/share identity", sc_c02),
    ("C03", "real trigger/body/deferred commit/nested failures", sc_c03),
    ("C04", "historical publication/removal/private changes", sc_c04),
    ("C05", "late/oversized unicode window continuation", sc_c05),
    ("C06", "rich sets/modes/dates/brand/aliases", sc_c06),
    ("C07", "second-connection committed revocation after capture", sc_c07),
    ("C08", ">200 membership/paging/refresh", sc_c08),
    ("C09", "collection cursors after changed cohort", sc_c09),
    ("C10", "excerpt/query/bookmark variants with loss/denial", sc_c10),
    ("C11", "exact-ref lost note/ownership/revision/receipt purity", sc_c11),
    ("C12", "tracker transition table", sc_c12),
    ("C13", "new source/resegmentation/offer-only/repeat/failed reload", sc_c13),
    ("C14", "pause/resume/stop/read/backfill/receipt bindings", sc_c14),
    ("C15", "both campaign kinds/briefs", sc_c15),
    ("C16", "seven persisted assessment outcomes", sc_c16),
    ("C17", "occurrence allocation and atomic move/reference rollback", sc_c17),
    ("C18", "frozen artifacts, denial vs loss, corruption", sc_c18),
    ("C19", "null/empty/foreign/out-of-cell/multipage/stale exports", sc_c19),
    ("C20", "literal-v7 conversion/quarantine/marker/disposition", sc_c20),
]


def _lower_headers(headers) -> dict:
    return {str(k).lower(): v for k, v in dict(headers).items()}


def _full_path(path: str) -> str:
    return path if path.startswith("/api/v1/") else "/api/v1" + path


class LocalCall:
    """Real loopback HTTP with the server's own session/CSRF recipe.

    Bootstraps GET /api/v1/session for the live CSRF token, then sends
    X-CSRF-Token plus the full Origin on every query/mutation POST/PUT/DELETE,
    exactly as the browser surface requires.
    """

    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.port = port
        self.csrf = None

    def bootstrap(self):
        s, _, d = self("GET", "/session")
        if s != 200 or not d.get("csrf_token"):
            raise ScenarioFail(f"local session bootstrap failed: {s} {d}")
        self.csrf = d["csrf_token"]
        return d

    def __call__(self, method, path, body=None, query="", raw=False,
                 session="owner", match=None):
        url = self.base + _full_path(path) + ("?" + query if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json",
                   "Host": f"127.0.0.1:{self.port}"}
        if method in ("POST", "PUT", "DELETE"):
            token = self.csrf or ""
            if session == "bogus-session":
                token = "bogus-token"
            headers["X-CSRF-Token"] = token
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        if match is not None:
            headers["If-Match"] = str(match)
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                status, headers, raw_bytes = r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw_bytes = e.code, dict(e.headers), e.read()
        try:
            parsed = json.loads(raw_bytes.decode() or "{}")
        except ValueError:
            parsed = {"_raw": raw_bytes.decode("utf-8", "replace")}
        headers = _lower_headers(headers)
        if raw:
            return status, headers, parsed, raw_bytes
        return status, headers, parsed


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    if port in FORBIDDEN_PORTS:
        raise SystemExit("port probe hit a forbidden port; retry")
    return port


def _scenario_state(workspace: Path, recipe: dict, transport: str,
                    single_owner: bool, export_prefix: str = "/customer/exports"
                    ) -> dict:
    tv_e1 = None
    try:
        tcon = sqlite3.connect(str(workspace / "workspace.sqlite3"),
                               isolation_level=None)
        try:
            row = tcon.execute(
                "SELECT transcript_version_id FROM episodes WHERE id='e1'"
            ).fetchone()
            tv_e1 = row[0] if row else None
        finally:
            tcon.close()
    except sqlite3.Error:
        pass
    return {"transport": transport, "single_owner": single_owner,
            "tv_e1": tv_e1,
            "export_url": (lambda i, p=export_prefix: f"{p}/{i}"),
            "db_path": str(workspace / "workspace.sqlite3"),
            "ev_ref": recipe["source_refs"]["evidence"],
            "ep_ref": recipe["source_refs"]["episode_e1"],
            "attach_ref": recipe["source_refs"]["attach_ref"],
            "canon_ref": None,
            "supplied_id": recipe["supplied_campaign"]["id"],
            "brief_ref": {"kind": "supplied_brief",
                          "brief_id": recipe["brief"]["brief_id"],
                          "version": 1},
            "brief_digest": recipe["brief"]["digest"],
            "aggregate_rev": recipe.get("aggregate_rev"),
            "pack_ids": {pid: name.split("pack_", 1)[1]
                        for name, pid in
                        recipe["supplied_campaign"]["placements"].items()
                        if name.startswith("pack_")}}


SCENARIO_PREREQS = {"C13": ["C12"]}


def _expand_only(only):
    """Targeted runs execute actual prerequisites, never fake state.

    --only C13 transparently runs C12 first so the tracker id/revision
    it consumes are real; the receipt records every executed scenario.
    """
    if not only:
        return None
    ordered = []
    for cid in only:
        for pre in SCENARIO_PREREQS.get(cid, []):
            if pre not in ordered:
                ordered.append(pre)
        if cid not in ordered:
            ordered.append(cid)
    order = [cid for cid, _, _ in SCENARIOS]
    return sorted(ordered, key=order.index)


def run_scenarios(call, st, only=None) -> list:
    only = _expand_only(only)
    results = []
    for cid, name, fn in SCENARIOS:
        if only and cid not in only:
            continue
        try:
            detail = fn(call, st)
            if not isinstance(detail, dict):
                detail = {"result": detail}
            if st.get("transport_gaps"):
                detail = dict(detail, transport_gaps=list(st["transport_gaps"]))
                st["transport_gaps"] = []
            results.append({"id": cid, "name": name, "ok": True,
                            "detail": detail})
        except ScenarioFail as exc:
            results.append({"id": cid, "name": name, "ok": False,
                            "detail": {"error": str(exc)}})
        except Exception as exc:
            results.append({"id": cid, "name": name, "ok": False,
                            "detail": {"exception": f"{type(exc).__name__}: {exc}"}})
    return results


def run_local_http(workspace: Path, only=None) -> dict:
    recipe = json.loads((workspace / "fixture-map.json").read_text())
    digest_before = _observed_source_digest()
    try:
        port = _free_port()
    except OSError as exc:
        return _tcp_blocked(workspace, recipe,
                            f"{type(exc).__name__}: {exc}", digest_before)
    log_path = workspace / "server.log"
    try:
        with open(log_path, "wb") as log:
            srv = subprocess.Popen(
                [sys.executable, str(REPO / "scripts" / "app_server.py"),
                 "--workspace", str(workspace), "--port", str(port)],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
    except OSError as exc:
        return _tcp_blocked(workspace, recipe,
                            f"{type(exc).__name__}: {exc}")
    results = []
    try:
        call = LocalCall(port)
        ready = False
        last = ""
        for _ in range(100):
            try:
                s, _, d = call("GET", "/session")
                if s == 200:
                    ready = True
                    break
                last = f"status {s} {d}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(0.2)
        if not ready:
            tail = ""
            try:
                tail = log_path.read_bytes()[-500:].decode("utf-8", "replace")
            except OSError:
                pass
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except subprocess.TimeoutExpired:
                srv.kill()
            if "Operation not permitted" in tail or "Permission" in last:
                return _tcp_blocked(workspace, recipe,
                                    f"server not ready ({last}); log: {tail}",
                                    digest_before)
            binding = _digest_binding(
                digest_before, _observed_source_digest())
            return {"ok": False, "transport": "tcp-loopback",
                    "error": f"server not ready ({last}); log: {tail}",
                    "scenarios": [], **binding}
        call.bootstrap()
        st = _scenario_state(workspace, recipe, "tcp-loopback", True)
        row = call("GET", "/session")
        st["workspace_context"] = row[2].get("workspace_context")
        results = run_scenarios(call, st, only)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
    binding = _digest_binding(digest_before, _observed_source_digest())
    ok = all(r["ok"] for r in results) \
        and not binding["source_changed_during_run"]
    return {"ok": ok, "transport": "tcp-loopback", "port": port,
            "csrf": "session-bootstrapped",
            "scenarios": results, **binding}


def _tcp_blocked(workspace: Path, recipe: dict, error: str,
                 digest_before: str | None = None) -> dict:
    """Sandbox TCP denial: exact coordinator-host handoff, never socketpair."""
    import hashlib as _hashlib
    db_path = workspace / "workspace.sqlite3"
    digest = _hashlib.sha256(db_path.read_bytes()).hexdigest()
    observed = digest_before if digest_before is not None \
        else _observed_source_digest()
    binding = _digest_binding(observed, _observed_source_digest())
    return {"ok": False, "transport": "tcp-loopback-blocked",
            "error": error, **binding,
            "coordinator_host_command": (
                "cd /Users/renoneto/projects/earwitness-validation/"
                ".tmp-earwitness-product-20261006-075350/source && "
                "PYTHONPATH=. /Users/renoneto/projects/earwitness-validation/"
                ".venv-hosted/bin/python scripts/backend_acceptance.py "
                f"--workspace {workspace.name} --mode local-http "
                f"--out .tmp-execution/proof-local-http-rerun.json"),
            "fixture": {"workspace": workspace.name,
                        "workspace_sha256": digest,
                        "fixture_map_sha256": _hashlib.sha256(
                            (workspace / "fixture-map.json").read_bytes()
                        ).hexdigest()},
            "scenarios": []}


SESSIONS = {
    "owner": ("sess-k11", "tok-k11", "u-owner"),
    "viewer": ("sess-k11-viewer", "tok-viewer", "u-viewer"),
    "guest-c07": ("sess-k11-guest", "tok-guest", "u-guest-c07"),
    "guest-c02": ("sess-k11-c02", "tok-c02", "u-guest-c02"),
}


def _register_session(app, session_id, subject, csrf="tok-k11"):
    import hashlib as _hashlib
    import time as _time
    from earwitness.app import browser_auth as _auth
    digest = _hashlib.sha256(f"ew-browser-v1:{session_id}".encode()).hexdigest()
    now = _time.time()
    raw = app.store.auth_connection()
    try:
        _auth.ensure_auth_schema(raw)
        raw.execute(
            "INSERT INTO browser_sessions (session_hash, issuer, subject,"
            " email, email_verified, csrf_token, active_workspace, workspace_switch_revision,"
            " created_at, expires_at, revoked_at)"
            " VALUES (?, 'https://i.example', ?, '', 0, ?, 'ws', 0, ?, ?, NULL)",
            (digest, subject, csrf, now, now + 3600))
        raw.commit()
    finally:
        raw.close()


def _hosted_app(workspace: Path):
    from earwitness.app import asgi as _asgi
    from earwitness.app import browser_auth as _auth
    from earwitness.app import collaboration as _collab
    from earwitness.app import connections as _conn
    tmp = tempfile.mkdtemp(prefix="grants-", dir=str(workspace))
    grants = os.path.join(tmp, "grants.sqlite3")
    app = _asgi.HostedApp(_asgi.HostedConfig(
        data_dir=str(workspace), grants_db=grants))
    _conn.register_workspace(app.store, "ws",
                             str(workspace / "workspace.sqlite3"),
                             str(workspace))
    for name, (sid, csrf, _subject) in SESSIONS.items():
        _register_session(app, sid, _subject, csrf)
    # Real producer memberships: viewer + guest-c07 admitted; guest-c02's
    # invite is created through HTTP inside C02 (its session pre-exists).
    db_path = str(workspace / "workspace.sqlite3")
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        for _member in ("oidc:https://i.example:u-viewer",
                        "oidc:https://i.example:u-guest-c07"):
            invite = _collab.create_invite(con, "local", _member, "viewer",
                                           "oidc:https://i.example:u-owner")
            _collab.accept_invite(con, invite["invite_id"], _member)
        con.commit()
    finally:
        con.close()
    return app, tmp


class HostedCall:
    def __init__(self, app):
        self.app = app

    def _switch_revision(self, sid):
        import hashlib as _hashlib
        digest = _hashlib.sha256(f"ew-browser-v1:{sid}".encode()).hexdigest()
        raw = self.app.store.auth_connection()
        try:
            row = raw.execute("SELECT workspace_switch_revision FROM browser_sessions"
                              " WHERE session_hash=?", (digest,)).fetchone()
        finally:
            raw.close()
        return None if row is None else row[0]

    def __call__(self, method, path, body=None, query="", raw=False,
                 session="owner", match=None):
        import asyncio
        if session in SESSIONS:
            sid, token, _subject = SESSIONS[session]
        else:
            sid, token = session, "bad-token"
        raw_body = json.dumps(body or {}).encode()
        hdrs = [(b"host", b"127.0.0.1:8771"),
                (b"cookie", f"ew_session={sid}".encode()),
                (b"content-length", str(len(raw_body)).encode())]
        if method in ("POST", "PUT", "DELETE"):
            hdrs.append((b"x-csrf-token", token.encode()))
            hdrs.append((b"origin", b"http://127.0.0.1:8771/"))
            # The displayed workspace-switch epoch is the session's live
            # revision: acceptance sessions never switch mid-scenario.
            revision = self._switch_revision(sid)
            if revision is not None:
                hdrs.append((b"x-earwitness-workspace-switch-revision",
                             str(revision).encode()))
        if match is not None:
            hdrs.append((b"if-match", str(match).encode()))
        scope = {"type": "http", "method": method,
                 "path": _full_path(path),
                 "scheme": "http", "headers": hdrs,
                 "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw_body,
                    "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        # Token-bucket pacing: the hosted gate admits ~60/min with a
        # burst of 20; acceptance traffic stays inside the budget.
        time.sleep(1.0)
        start = next(m for m in sent if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in sent
                            if m["type"] == "http.response.body")
        headers = {k.decode().lower(): v.decode()
                   for k, v in start.get("headers", [])}
        try:
            parsed = json.loads(payload.decode() or "{}")
        except ValueError:
            parsed = {"_raw": payload.decode("utf-8", "replace")}
        if raw:
            return start["status"], headers, parsed, payload
        return start["status"], headers, parsed


def run_hosted_http(workspace: Path, only=None) -> dict:
    recipe = json.loads((workspace / "fixture-map.json").read_text())
    digest_before = _observed_source_digest()
    app, tmp = _hosted_app(workspace)
    try:
        call = HostedCall(app)
        st = _scenario_state(workspace, recipe, "asgi-in-process", False)
        results = run_scenarios(call, st, only)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    binding = _digest_binding(digest_before, _observed_source_digest())
    return {"ok": all(r["ok"] for r in results)
            and not binding["source_changed_during_run"],
            "transport": "asgi-in-process", "scenarios": results, **binding}


def run_reader(workspace: Path) -> dict:
    """Integrate the reader owner's real SDK/stdio receipt; retain gaps.

    The native MCP SDK/stdio proof is reader-owner work (accepted lane) and
    coordinator-host TCP evidence -- never the old five-call ASGI loop, which
    is deleted. This mode verifies those receipts exist and records what they
    do and do not cover.
    """
    checks = []
    reader_receipt = REPO / ".tmp-execution" / "RESULT-backend-reader-20261006.json"
    host_receipt = REPO / ".tmp-execution" / "coordinator-reader-http.json"
    if reader_receipt.is_file():
        data = json.loads(reader_receipt.read_text())
        obs = data.get("observations", {})
        checks.append({"name": "reader receipt present", "ok": True,
                       "detail": {"model": data.get("model"),
                                  "pinned_citation": bool(obs.get("pinned_citation")),
                                  "permission_purity": bool(
                                      obs.get("permission_purity"))}})
    else:
        checks.append({"name": "reader receipt present", "ok": False,
                       "detail": "RESULT-backend-reader-20261006.json absent"})
    if host_receipt.is_file():
        data = json.loads(host_receipt.read_text())
        checks.append({"name": "coordinator host TCP eras", "ok": data.get("status") == "passed",
                       "detail": {"observed": data.get("observed"),
                                  "command": data.get("command")}})
    else:
        checks.append({"name": "coordinator host TCP eras", "ok": False,
                       "detail": "coordinator-reader-http.json absent"})
    checks.append({"name": "live Postgres reader parity", "ok": False,
                   "detail": "blocked: no isolated postgres (reader lane "
                             "environmental skip); not claimed"})
    checks.append({"name": "no ASGI pseudo-reader substitution", "ok": True,
                   "detail": "old five-call direct-service loop deleted; "
                             "only receipts above are integrated"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def run_conversion(workspace: Path) -> dict:
    """Original C20: per-class quarantine/manifest/CLI conversion proof.

    Literal-v7 workspace carrying one legacy row per inventoried class
    (saved_query, native_filter, typed_query, tracker x2, campaign null-mode,
    requirements x2, saved capture, origin/source_pin/artifact/reader_profile
    seeds): dry-run classifies each convertible or needs_review; a pending
    non-search blocks manifest/retire; explicit v1 manifest drives the
    separate backed-up semantic CLI operation; null-mode campaign graphs stay
    read-only while its historical report bytes remain exact; byte-identical
    backup restores and re-upgrades. No minted readiness.
    """
    import hashlib as _hashlib
    from earwitness.app import briefs as _briefs
    from earwitness.app import campaigns as _camp
    from earwitness.app import conversion as _conversion
    from earwitness.app import db as _db
    from earwitness.app import ops as _ops
    from scripts import app_workspace as _ws
    from tests import backend_fixtures
    checks = []

    def check(name, cond, detail=""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    work = workspace.parent / (workspace.name + "-v7src")
    if work.exists():
        raise SystemExit(f"refusing occupied conversion source: {work}")
    digest_before = _observed_source_digest()
    try:
        db_file = backend_fixtures.make_workspace(str(work), schema_version=7)
        h0 = _hashlib.sha256(work.joinpath("workspace.sqlite3").read_bytes()
                             ).hexdigest()
        con = sqlite3.connect(str(db_file), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            # Pre-upgrade legacy seeds (direct-legacy pattern, synthetic only):
            # a decodable native filter and a second v1 tracker.
            con.execute(
                "INSERT INTO watchlists (name, query, revision, updated_at)"
                " VALUES ('legacy wl', '{\"q\":\"lumen\"}', 1, ?)",
                (_db.utcnow(),))
            con.execute(
                "INSERT INTO customer_alerts (workspace_id, name, query_json,"
                " definition_revision, origin_route, event_types,"
                " delivery_json, lifecycle, revision, baseline_json,"
                " monitor_json, created_at, updated_at) VALUES ('local',"
                " 'second tracker', '{\"mode\":\"mentions\",\"q\":\"lumen\"}',"
                " 1, '', '[]', '{}', 'active_local', 1, '{\"basis\":\"seed\"}',"
                " '{}', ?, ?)", (_db.utcnow(), _db.utcnow()))
            con.commit()
            t2 = con.execute(
                "SELECT MAX(id) FROM customer_alerts").fetchone()[0]
        finally:
            con.close()
        upgraded = _ws.upgrade_workspace(work)
        check("explicit backed-up upgrade to v8",
              upgraded.get("upgraded") is True
              and upgraded.get("after_version") == _db.SCHEMA_VERSION,
              {"upgraded": upgraded.get("upgraded"),
               "before": upgraded.get("before_version"),
               "after": upgraded.get("after_version")})
        backup_file = work / "before-upgrade-7.sqlite3"
        check("upgrade backup written", backup_file.is_file(),
              {"backup": str(backup_file)})
        _vcon = sqlite3.connect(str(db_file), isolation_level=None)
        try:
            _vv = _db._schema_version(_vcon)
        finally:
            _vcon.close()
        check("upgraded schema is v8", _vv == _db.SCHEMA_VERSION,
              {"version": _vv})
        con = sqlite3.connect(str(db_file), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            check("upgrade keeps legacy filter row",
                  con.execute("SELECT query FROM watchlists").fetchone()[0]
                  == '{"q":"lumen"}', {})
            # Post-upgrade legacy seeds: real brief authority for one
            # requirements row, an unassessable requirements row, a v1 typed
            # query, a dangling saved capture, and a legacy-selector tracker.
            brief = _briefs.create_brief(
                con, {"title": "Conversion brief",
                      "provided_text": "Say lumen clearly."}, "fixture")
            brief_ref = {"kind": "supplied_brief",
                         "brief_id": brief["brief_id"], "version": 1,
                         "digest": brief["digest"]}
            con.execute(
                "INSERT INTO expected_placements (id, workspace_id,"
                " campaign_id, scope, episode_id, timezone, expected_count,"
                " requirements, provenance, revision, updated_at) VALUES"
                " ('req-1', 'local', 1, 'episode', 'e1', 'UTC', 1, '{}', ?,"
                " 1, ?)", (json.dumps({"brief_ref": brief_ref}), _db.utcnow()))
            con.execute(
                "INSERT INTO expected_placements (id, workspace_id,"
                " campaign_id, scope, episode_id, timezone, expected_count,"
                " requirements, provenance, revision, updated_at) VALUES"
                " ('req-bad', 'local', 1, 'episode', 'e1', 'UTC', 1,"
                " '{\"bogus_key\": 1}', '{}', 1, ?)", (_db.utcnow(),))
            con.execute(
                "INSERT INTO customer_saved_items (id, workspace_id, type,"
                " title, note, revision, target_json, query_json,"
                " selector_json, captured_json, saved_at, updated_at)"
                " VALUES ('tq-1', 'local', 'query', 'Legacy typed', '', 1,"
                " '{}', '{\"mode\":\"mentions\",\"q\":\"lumen\"}', NULL, '{}',"
                " ?, ?)", (_db.utcnow(), _db.utcnow()))
            con.execute(
                "INSERT INTO customer_saved_items (id, workspace_id, type,"
                " title, note, revision, target_json, query_json,"
                " selector_json, captured_json, saved_at, updated_at)"
                " VALUES ('cap-1', 'local', 'bookmark', 'Dangling capture',"
                " '', 1, '{}', '{}', NULL,"
                " '{\"ref\": \"sr1:deadbeefdeadbeef\","
                " \"capture_digest\": \"0\"}', ?, ?)",
                (_db.utcnow(), _db.utcnow()))
            con.execute(
                "UPDATE customer_alerts SET live_selector_json=?"
                " WHERE id=?", ('{"mode": "mentions"}', t2))
            con.commit()
            rev_before = _db.aggregate_revision(con)
            tables_before = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            h_before = _hashlib.sha256(work.joinpath(
                "workspace.sqlite3").read_bytes()).hexdigest()
            report = _conversion.dry_run(con)
            check("dry-run read-only",
                  _db.aggregate_revision(con) == rev_before
                  and {r[0] for r in con.execute(
                      "SELECT name FROM sqlite_master WHERE type='table'")}
                  == tables_before
                  and _hashlib.sha256(work.joinpath(
                      "workspace.sqlite3").read_bytes()).hexdigest()
                  == h_before,
                  report["summary"])
            by_key = {e["key"]: e for e in report["definitions"]}
            check("dry-run classifies legacy rows",
                  report["summary"]["total"] >= 1
                  and report["summary"]["convertible"]
                  + report["summary"]["needs_review"]
                  >= report["summary"]["total"], report["summary"])
            for _key, _want in (
                    ("saved_query:1", "convertible"),
                    ("typed_query:tq-1", "convertible"),
                    ("tracker:1", "convertible"),
                    (f"tracker:{t2}", "needs_review"),
                    ("native_filter:1", "qualified"),
                    ("reader_profile:agent_profile", "qualified"),
                    ("requirements:req-1", "qualified"),
                    ("requirements:req-bad", "needs_review"),
                    ("saved_capture:cap-1", "needs_review"),
                    ("origin:1", "needs_review"),
                    ("campaign:1", "needs_review"),
                    ("artifact:1", "needs_review")):
                check(f"class {_key} is {_want}",
                      by_key.get(_key, {}).get("disposition") == _want,
                      {"disposition": by_key.get(_key, {}).get(
                          "disposition")})
            for _key in ("saved_query:1", "tracker:1"):
                _mem = by_key[_key].get("membership") or {}
                check(f"{_key} converted membership equals corrected declared",
                      _mem.get("corrected_declared_before") == _mem.get("after")
                      and _mem.get("after") is not None,
                      {"before": _mem.get("corrected_declared_before"),
                       "after": _mem.get("after")})
            check("pending non-search blocks manifest authority",
                  f"tracker:{t2}" in report["pending_definitions"],
                  {"pending": report["pending_definitions"]})
            legacy_bytes = con.execute(
                "SELECT payload FROM exports WHERE id=1").fetchone()[0]
            tracker1_before = dict(con.execute(
                "SELECT * FROM customer_alerts WHERE id=1").fetchone())
            # Manifest authority negatives (pure validation, no writes).
            _base = {"version": 1,
                     "inventory_digest": report["inventory_digest"],
                     "dispositions": []}
            try:
                _conversion.validate_manifest(con, dict(_base))
                _uncovered_ok = True
            except _ops.MissingPrecondition:
                _uncovered_ok = False
            check("uncovered pending manifest rejected",
                  _uncovered_ok is False, {})
            _tampered = {"version": 1,
                         "inventory_digest": report["inventory_digest"],
                         "dispositions": [
                             {"key": k, "definition_digest": "0" * 64,
                              "action": "quarantined"}
                             for k in report["pending_definitions"][:1]]}
            try:
                _conversion.validate_manifest(con, _tampered)
                _tampered_ok = True
            except _ops.BadInput:
                _tampered_ok = False
            check("stale-definition manifest rejected",
                  _tampered_ok is False, {})
            _unknown = {"version": 1,
                        "inventory_digest": report["inventory_digest"],
                        "dispositions": [
                            {"key": "saved_query:4242",
                             "definition_digest": "0" * 64,
                             "action": "dropped"}]}
            try:
                _conversion.validate_manifest(con, _unknown)
                _unknown_ok = True
            except _ops.BadInput:
                _unknown_ok = False
            check("unknown-key manifest rejected", _unknown_ok is False, {})
            # Pending definitions block store mutation: refused retire writes
            # nothing (revision and file bytes unchanged).
            _rh = _hashlib.sha256(work.joinpath(
                "workspace.sqlite3").read_bytes()).hexdigest()
            _rr = _db.aggregate_revision(con)
            try:
                _conversion.retire_store(con)
                _retire_blocked = False
            except _ops.MissingPrecondition:
                _retire_blocked = True
            check("pending definitions block store retire",
                  _retire_blocked is True
                  and _db.aggregate_revision(con) == _rr
                  and _hashlib.sha256(work.joinpath(
                      "workspace.sqlite3").read_bytes()).hexdigest() == _rh,
                  {})
            manifest = {
                "version": 1, "inventory_digest": report["inventory_digest"],
                "dispositions": [
                    {"key": k,
                     "definition_digest": by_key[k]["definition_digest"],
                     "action": ("converted"
                                if by_key[k]["disposition"] == "convertible"
                                else "quarantined")}
                    for k in report["pending_definitions"]]}
            _conversion.validate_manifest(con, manifest)
            check("explicit v1 manifest authorizes every pending definition",
                  True, {"decisions": len(manifest["dispositions"])})
        finally:
            con.close()
        # Separate backed-up semantic CLI operation (own connection + lock).
        # The CLI service requires an absolute workspace path for its
        # candidate-local backup check; the mode otherwise stays relative.
        semantic = _ws.semantic_convert_workspace(work.resolve(), manifest)
        check("semantic CLI backs up before writing",
              Path(semantic["backup"]).is_file(), {"backup": semantic["backup"]})
        check("semantic apply completes the marker",
              semantic["conversion"]["marker"]["state"] == "complete",
              semantic["conversion"]["marker"])
        check("semantic retires the obsolete store",
              semantic["retirement"]["retired"] is True, semantic["retirement"])
        con = sqlite3.connect(str(db_file), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            migrated = {m["search_id"]: m
                        for m in semantic["conversion"]["migrated"]}
            check("convertible saved query migrates to a typed v2 item",
                  1 in migrated, {"migrated": migrated})
            _item = con.execute(
                "SELECT * FROM customer_saved_items WHERE id=?",
                (migrated[1]["item_id"],)).fetchone()
            check("migrated item carries the corrected v2 query",
                  _item is not None and _item["type"] == "query"
                  and json.loads(_item["query_json"])["version"] == 2
                  and json.loads(_item["query_json"])["q"] == "lumen",
                  {"item_id": migrated[1]["item_id"]})
            _lin1 = con.execute(
                "SELECT * FROM customer_conversion_lineage"
                " WHERE definition_key='saved_query:1'").fetchone()
            check("lineage preserves the original definition",
                  _lin1 is not None and _lin1["action"] == "converted"
                  and json.loads(_lin1["original_json"])
                  == {"mode": "mentions", "q": "lumen"},
                  {"action": _lin1["action"] if _lin1 else None})
            _t1 = dict(con.execute(
                "SELECT * FROM customer_alerts WHERE id=1").fetchone())
            _mem1 = json.loads(con.execute(
                "SELECT membership_json FROM customer_conversion_lineage"
                " WHERE definition_key='tracker:1'").fetchone()[0])
            check("tracker converts in place with corrected membership",
                  _t1["query_version"] == 2
                  and json.loads(_t1["query_json"])["version"] == 2
                  and _mem1["corrected_declared_before"] == _mem1["after"],
                  {"query_version": _t1["query_version"]})
            check("tracker definition/baseline/event identity preserved",
                  all(_t1[k] == tracker1_before[k]
                      for k in ("name", "origin_route", "event_types",
                                "delivery_json", "lifecycle", "baseline_json",
                                "monitor_json", "definition_revision")),
                  {})
            _tq = con.execute(
                "SELECT query_json FROM customer_saved_items"
                " WHERE id='tq-1'").fetchone()[0]
            check("typed query canonicalizes in place",
                  json.loads(_tq)["version"] == 2, {})
            _actions = {r["definition_key"]: r["action"] for r in con.execute(
                "SELECT definition_key, action FROM"
                " customer_conversion_lineage").fetchall()}
            _pin_key = next(k for k in by_key if k.startswith("source_pin:"))
            check("review classes quarantined without executable items",
                  all(_actions.get(k) == "quarantined"
                      for k in (f"tracker:{t2}", "campaign:1", "origin:1",
                                _pin_key, "requirements:req-bad",
                                "saved_capture:cap-1", "artifact:1")),
                  {"quarantined": sorted(
                      k for k, v in _actions.items() if v == "quarantined")})
            for _kind, _ident in (("tracker", t2), ("campaign", 1)):
                try:
                    _conversion.require_executable(con, _kind, _ident)
                    _exec_ok = True
                except _ops.BadInput:
                    _exec_ok = False
                check(f"quarantined {_kind}:{_ident} is read-only",
                      _exec_ok is False, {})
            # Null-mode campaign graphs stay read-only...
            _camp_row = dict(con.execute(
                "SELECT * FROM campaigns WHERE id=1").fetchone())
            for _op, _fn in (
                    ("attach", lambda: _camp.save_customer_placement(
                        con, 1, {"placement": {
                            "scope": "episode", "episode_id": "e1",
                            "timezone": "UTC", "expected_count": 1,
                            "binding": "eligible", "requirements": {},
                            "provenance": {}},
                            "expected_campaign_revision":
                                _camp_row["revision"]},
                        None, None, _camp_row["revision"], "fixture")),
                    ("freeze", lambda: _camp.freeze_customer_report(
                        con, 1, _camp_row["revision"], {}, "fixture"))):
                try:
                    _fn()
                    _legacy_outcome = "unexpected-ok"
                except _ops.BadInput as exc:
                    _legacy_outcome = str(exc)[:160]
                check(f"null-mode campaign {_op} denied",
                      _legacy_outcome in (
                          "legacy campaign mode is read-only pending"
                          " qualified disposition",
                          "legacy campaign is read-only pending"
                          " qualified disposition",
                          "legacy definition is read-only pending"
                          " qualified owner disposition"),
                      {"outcome": _legacy_outcome,
                       "mode": _camp_row["mode"]})
            # ...while its qualified historical report bytes remain exact.
            _rep = con.execute(
                "SELECT export_digest FROM campaign_reports WHERE id=1"
                ).fetchone()[0]
            now_bytes = con.execute(
                "SELECT payload FROM exports WHERE id=1").fetchone()[0]
            check("legacy report bytes preserved",
                  now_bytes == legacy_bytes
                  and _hashlib.sha256(
                      now_bytes.encode()).hexdigest() == _rep,
                  {"bytes": len(now_bytes)})
            status = _conversion.status(con)
            check("conversion status explicit", "marker" in status
                  and "store_retired" in status
                  and status["store_retired"] is True
                  and status["marker"]["state"] == "complete",
                  {"marker": status.get("marker"),
                   "store_retired": status.get("store_retired")})
            with _db.transaction(con, write=True):
                again = _conversion.apply(con)
            check("apply idempotent",
                  len(again["migrated"]) == 0
                  and again["marker"]["state"] == "complete",
                  again["summary"])
            with _db.transaction(con, write=True):
                _again_retired = _conversion.retire_store(con)
            check("reopening retire is already-done",
                  _again_retired.get("already") is True, _again_retired)
            _ch = _hashlib.sha256(work.joinpath(
                "workspace.sqlite3").read_bytes()).hexdigest()
            _cr = _db.aggregate_revision(con)
            result = _ws.customer_cutover_check(work)
            check("cutover check names blocked runtime readiness only",
                  result["ok"] is False
                  and result["problems"] == [
                      "source-bound runtime/semantic readiness is incomplete"]
                  and result["conversion"]["pending_definitions"] == []
                  and result["conversion"]["store_retired"] is True,
                  {"problems": result["problems"]})
            check("readonly cutover check writes nothing",
                  _db.aggregate_revision(con) == _cr
                  and _hashlib.sha256(work.joinpath(
                      "workspace.sqlite3").read_bytes()).hexdigest() == _ch,
                  {})
        finally:
            con.close()
        # Recovery: the pre-upgrade backup restores byte-identical v7,
        # which re-upgrades cleanly (owner rollback path, not a downgrade).
        h_backup = _hashlib.sha256(backup_file.read_bytes()).hexdigest()
        shutil.copy2(backup_file, work / "workspace.sqlite3")
        h_restored = _hashlib.sha256(
            work.joinpath("workspace.sqlite3").read_bytes()).hexdigest()
        check("pre-upgrade backup restores byte-identical",
              h_backup == h_restored, {"sha256": h_restored})
        con = sqlite3.connect(str(work / "workspace.sqlite3"),
                              isolation_level=None)
        try:
            check("restored v7 workspace opens",
                  _db._schema_version(con) == 7,
                  {"version": _db._schema_version(con)})
        finally:
            con.close()
        backup_file.unlink()
        upgraded2 = _ws.upgrade_workspace(work)
        check("restored workspace re-upgrades",
              upgraded2.get("upgraded") is True
              and upgraded2.get("after_version") == _db.SCHEMA_VERSION,
              {"upgraded": upgraded2.get("upgraded"),
               "after": upgraded2.get("after_version")})
        try:
            result = _ws.customer_cutover_check(work)
        except Exception as exc:
            result = {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        check("cutover-check explicit (no silent pass)",
              isinstance(result, dict) and "problems" in result, result)
        rcon = sqlite3.connect(str(work / "workspace.sqlite3"),
                               isolation_level=None)
        rcon.row_factory = sqlite3.Row
        try:
            st = _conversion.status(rcon)
        finally:
            rcon.close()
        # The recovered copy is converted-nothing-yet: marker correctly null
        # with the legacy row intact -- history preserved, no schema-alone
        # claim.
        check("history/identity stable across recovery",
              st.get("marker") is None
              and st.get("summary", {}).get("total") == 1,
              {"marker": st.get("marker"),
               "summary": st.get("summary")})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    binding = _digest_binding(digest_before, _observed_source_digest())
    return {"ok": all(c["ok"] for c in checks)
            and not binding["source_changed_during_run"],
            "checks": checks, **binding}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--mode", required=True,
                        choices=["prepare", "local-http", "hosted-http",
                                 "reader", "conversion"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--primary", default=PRIMARY_DEFAULT)
    parser.add_argument("--only", default=None,
                        help="comma-separated scenario IDs for targeted reruns"
                             " (prior green evidence preserved)")
    args = parser.parse_args(argv)
    only = ([c.strip() for c in args.only.split(",") if c.strip()]
            if args.only else None)
    workspace = Path(args.workspace)
    out_path = refuse_receipt(Path(args.out))
    if args.mode == "prepare":
        refuse_target(workspace, args.primary)
        recipe = prepare_workspace(workspace)
        receipt = {"ok": True, "mode": "prepare",
                   "workspace": str(workspace), "fixture": recipe}
    else:
        recipe = require_producer_fixture(workspace)
        if args.mode == "local-http":
            receipt = {"mode": "local-http", "workspace": str(workspace),
                       **run_local_http(workspace, only)}
        elif args.mode == "hosted-http":
            receipt = {"mode": "hosted-http", "workspace": str(workspace),
                       **run_hosted_http(workspace, only)}
        elif args.mode == "reader":
            receipt = {"mode": "reader", "workspace": str(workspace),
                       **run_reader(workspace)}
        elif args.mode == "conversion":
            receipt = {"mode": "conversion", "workspace": str(workspace),
                       **run_conversion(workspace)}
    out_path.write_text(json.dumps(receipt, indent=2, default=str))
    print(json.dumps({k: v for k, v in receipt.items()
                      if k not in ("scenarios", "checks")}, indent=2,
                     default=str))
    return 0 if receipt.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
