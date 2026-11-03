"""Decision 3 (2026-10-16): record bench_3 as an alias of s003e0 with the
existing alias mechanism (bench_0 -> s000e0 is a supplied alias: alias_of
plus kind 'supplied-alias', the seed's exact-transcript rule).

An operator-run, idempotent, auditable step: a dry run prints the change
and writes nothing; a run records alias_of + kind exactly as bench_0 has
them, maps the alias to its target's show (as the show map does for
bench_0), snapshots the episode and writes one audit row; a second run
is a no-op. It refuses a missing target, a target that is itself an
alias, and a twin whose transcript differs from every stored copy of the
target. Customer views then fold the twin (no duplicate read, no twin
show).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from earwitness.app import customer, customer_contracts, db, episode_alias

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import app_workspace  # noqa: E402

WORDS = [f"word{i}" for i in range(400)]


def _segments(words, step=10):
    """40 ten-second segments of ten words each."""
    return [{"start": float(i), "end": float(i + step), "text": " ".join(words[i:i + step])}
            for i in range(0, len(words), step)]


def _transcript(con, ep_id, segments, version_no=1, active=1):
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"transcript:{ep_id}", version_no, f"sh-{ep_id}-{version_no}",
         json.dumps({"segments": segments}), active, db.utcnow()))
    return cur.lastrowid


def _episode(con, ep_id, kind, tv, show="God Damn", title="401: Chestnuts",
             pub_date="2026-09-21", duration=69.6, alias_of=None):
    con.execute(
        "INSERT INTO episodes (id, kind, alias_of, show_title, ep_title, category,"
        " pub_date, duration_min, fetch_date, transcript_version_id, revision, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'true crime', ?, ?, '2026-09-21', ?, 1, ?)",
        (ep_id, kind, alias_of, show, title, pub_date, duration, tv, db.utcnow()))


def _evidence(con, ev_id, episode_id, start, end):
    tv = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?",
                     (episode_id,)).fetchone()[0]
    con.execute(
        "INSERT INTO evidence (id, source, source_version_id, episode_id,"
        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
        " transcript_version_id, source_record_key, assoc_revision,"
        " record_kind) VALUES (?, 'jev', 1, ?, ?, ?, 'acme offer', '{}', 1, ?,"
        " ?, ?, 0, 'candidate')",
        (ev_id, episode_id, start, end, db.utcnow(), tv, f"jev:{episode_id}:{ev_id}"))
    con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin,"
                " revision, updated_at) VALUES (?, 'acme', 'suggested', 'test', 1, ?)",
                (ev_id, db.utcnow()))


def seed(con):
    """s003e0 (feed-resolved show) and bench_3, an unresolved benchmark
    twin of the same audio whose transcript is another run (1 word of 400
    differs). s000e0 + its supplied alias bench_0. bench_9: same titles and
    duration as s003e0 but a different transcript."""
    con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload,"
        " is_active, created_at) VALUES ('episodes.json', 1, 'sh-eps', ?, 1, ?)",
        (json.dumps([{"ep_id": "s003e0", "show_title": "God Damn",
                      "feed_url": "https://godamn.example/rss"},
                     {"ep_id": "s000e0", "show_title": "Shop",
                      "feed_url": "https://shop.example/rss"}]), db.utcnow()))
    _episode(con, "s003e0", "supplied", _transcript(con, "s003e0", _segments(WORDS)))
    twin_words = list(WORDS)
    twin_words[123] = "different"
    _episode(con, "bench_3", "benchmark-unresolved",
             _transcript(con, "bench_3", _segments(twin_words)), pub_date=None)
    other = [f"other{i}" for i in range(400)]
    _episode(con, "bench_9", "benchmark-unresolved",
             _transcript(con, "bench_9", _segments(other)), pub_date=None)
    shop = _segments(WORDS[:200])
    _episode(con, "s000e0", "supplied", _transcript(con, "s000e0", shop),
             show="Shop", title="Side hustle", duration=22.8)
    _episode(con, "bench_0", "supplied-alias", _transcript(con, "bench_0", shop),
             show="Shop", title="Side hustle", duration=22.8, pub_date=None,
             alias_of="s000e0")
    _evidence(con, "c-1", "s003e0", 60.0, 120.0)
    _evidence(con, "c-2", "s003e0", 120.0, 176.0)
    _evidence(con, "t-1", "bench_3", 60.0, 120.0)
    _evidence(con, "t-2", "bench_3", 120.0, 176.0)
    _evidence(con, "o-1", "bench_9", 60.0, 120.0)
    con.commit()
    from tests.backend_fixtures import publish_all_records
    publish_all_records(con)
    customer.rebuild_customer_show_map(con)
    con.commit()


def _dump(con) -> str:
    return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()


def _q(mode, **extra):
    return customer_contracts.upcast_query_v1(dict({"mode": mode}, **extra))[0]


def _mapping(con, ep_id):
    row = con.execute("SELECT show_identity_id, basis FROM customer_episode_shows"
                      " WHERE workspace_id='local' AND episode_id=?", (ep_id,)).fetchone()
    return tuple(row) if row else None


class RecordAliasTests(unittest.TestCase):
    def setUp(self):
        from tests.backend_fixtures import make_file_workspace
        self.con = make_file_workspace()
        seed(self.con)

    def tearDown(self):
        self.con.close()

    def test_dry_run_writes_nothing_and_prints_the_change(self):
        before = _dump(self.con)
        result = episode_alias.record(self.con, "bench_3", "s003e0", actor="test", dry_run=True)
        self.assertEqual(_dump(self.con), before)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["episode"]["before"],
                         {"kind": "benchmark-unresolved", "alias_of": None})
        self.assertEqual(result["episode"]["after"],
                         {"kind": "supplied-alias", "alias_of": "s003e0"})
        self.assertEqual(result["show_map"]["after"],
                         {"show_identity_id": _mapping(self.con, "s003e0")[0], "basis": "alias_of"})
        self.assertEqual(result["basis"]["rule"], "same-audio-transcript")
        self.assertGreaterEqual(result["basis"]["word_match"], episode_alias.MIN_WORD_MATCH)
        self.assertEqual(sorted(p["alias_read"] for p in result["reads"]), ["t-1", "t-2"])

    def test_run_records_alias_as_bench_0_has_it_with_audit(self):
        rev = self.con.execute("SELECT revision FROM episodes WHERE id='bench_3'").fetchone()[0]
        result = episode_alias.record(self.con, "bench_3", "s003e0", actor="operator:test")
        self.assertTrue(result["changed"])
        twin = dict(self.con.execute("SELECT * FROM episodes WHERE id='bench_3'").fetchone())
        ref = dict(self.con.execute("SELECT * FROM episodes WHERE id='bench_0'").fetchone())
        self.assertEqual((twin["kind"], twin["alias_of"]), ("supplied-alias", "s003e0"))
        self.assertEqual(twin["kind"], ref["kind"])
        self.assertEqual(twin["revision"], rev + 1)
        # Same mapping shape as bench_0: the target's show, basis alias_of.
        self.assertEqual(_mapping(self.con, "bench_3"),
                         (_mapping(self.con, "s003e0")[0], "alias_of"))
        self.assertEqual(_mapping(self.con, "bench_0")[1], "alias_of")
        hist = self.con.execute(
            "SELECT kind, alias_of, actor FROM episode_history WHERE episode_id='bench_3'"
            " ORDER BY episode_revision DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(hist), ("supplied-alias", "s003e0", "operator:test"))
        audits = self.con.execute(
            "SELECT actor, target_type, target_id, detail FROM audit"
            " WHERE action='episode_alias'").fetchall()
        self.assertEqual(len(audits), 1)
        self.assertEqual(tuple(audits[0])[:3], ("operator:test", "episode", "bench_3"))
        detail = json.loads(audits[0]["detail"])
        self.assertEqual(detail["episode"]["after"]["alias_of"], "s003e0")
        self.assertEqual(detail["basis"]["rule"], "same-audio-transcript")
        self.assertIn("idempotency_key", detail)
        # Stored transcript, evidence and media are untouched.
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM evidence WHERE episode_id='bench_3'").fetchone()[0], 2)
        # A later show-map rebuild keeps the recorded mapping.
        customer.rebuild_customer_show_map(self.con)
        self.assertEqual(_mapping(self.con, "bench_3")[1], "alias_of")

    def test_second_run_is_a_noop(self):
        episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        before = _dump(self.con)
        again = episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        self.assertFalse(again["changed"])
        self.assertEqual(_dump(self.con), before)

    def test_refusals_write_nothing(self):
        before = _dump(self.con)
        cases = {
            ("bench_3", "s999e0"): "target",          # missing target
            ("bench_3", "bench_0"): "alias",          # target is itself an alias
            ("bench_9", "s003e0"): "transcript",      # twin transcript differs
            ("bench_3", "bench_3"): "itself",
            ("bench_0", "s003e0"): "already",         # alias of another episode
        }
        for (twin, target), word in cases.items():
            with self.subTest(twin=twin, target=target):
                with self.assertRaises(episode_alias.AliasRefused) as caught:
                    episode_alias.record(self.con, twin, target, actor="test")
                self.assertIn(word, str(caught.exception))
        self.assertEqual(_dump(self.con), before)

    def test_refuses_when_duration_differs(self):
        self.con.execute("UPDATE episodes SET duration_min=58.2 WHERE id='bench_3'")
        self.con.commit()
        with self.assertRaises(episode_alias.AliasRefused) as caught:
            episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        self.assertIn("duration", str(caught.exception))

    def test_customer_views_fold_the_twin(self):
        ads = customer.query(self.con, _q("ads"))
        self.assertIn("t-1", {i["evidence_id"] for i in ads["items"]})
        titles = [s["display_title"] for s in customer.customer_shows(self.con)]
        self.assertEqual(titles.count("God Damn"), 3)  # s003e0's show, two unresolved twins
        cov = customer.overview(self.con)["coverage"]
        episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        after = customer.query(self.con, _q("ads"))
        ids = {i["evidence_id"] for i in after["items"]}
        self.assertTrue({"c-1", "c-2"} <= ids)
        self.assertFalse({"t-1", "t-2"} & ids)
        self.assertEqual(after["total"], ads["total"] - 2)
        shows = customer.customer_shows(self.con, "God Damn")
        # Only s003e0's show and bench_9's (not aliased) remain; the
        # unresolved bench_3 twin show is gone.
        self.assertEqual(sorted(s["id"] for s in shows),
                         sorted({_mapping(self.con, "s003e0")[0], _mapping(self.con, "bench_9")[0]}))
        canonical = next(s for s in shows if s["id"] == _mapping(self.con, "s003e0")[0])
        self.assertEqual(canonical["episodes"], 1)
        after_cov = customer.overview(self.con)["coverage"]
        self.assertEqual(after_cov["episodes"], cov["episodes"] - 1)
        self.assertEqual(after_cov["shows"], cov["shows"] - 1)
        detail = customer.show_detail(self.con, _mapping(self.con, "s003e0")[0])
        # One episode (the canonical one) carrying its two reads once.
        self.assertEqual([(e["id"], e["ads"]) for e in detail["episodes"]], [("s003e0", 2)])


    def test_refuses_swapped_arguments(self):
        """R1-07: the canonical episode is the supplied / feed-resolved one;
        recording it as an alias of its benchmark twin is refused, by kind
        and by show identity independently. The right direction records."""
        def refused(*needles):
            before = _dump(self.con)
            with self.assertRaises(episode_alias.AliasRefused) as caught:
                episode_alias.record(self.con, "s003e0", "bench_3", actor="test")
            for needle in ("canonical", *needles):
                self.assertIn(needle, str(caught.exception))
            self.assertEqual(_dump(self.con), before)

        # Both at once (the owner data shape).
        refused()
        # Kind alone: s003e0 is 'supplied', bench_3 is not (feed basis removed).
        self.con.execute("UPDATE customer_episode_shows SET basis='unresolved'"
                         " WHERE episode_id='s003e0'")
        self.con.commit()
        refused("supplied")
        # Show identity alone: s003e0 has a feed_url show, bench_3 has none.
        self.con.execute("UPDATE customer_episode_shows SET basis='feed_url'"
                         " WHERE episode_id='s003e0'")
        self.con.execute("UPDATE episodes SET kind='benchmark-unresolved' WHERE id='s003e0'")
        self.con.commit()
        refused("feed_url")
        self.con.execute("UPDATE episodes SET kind='supplied' WHERE id='s003e0'")
        self.con.commit()
        # The right direction still records.
        self.assertTrue(episode_alias.record(self.con, "bench_3", "s003e0", actor="test")["changed"])

    def test_refuses_missing_alias_and_chain(self):
        before = _dump(self.con)
        with self.assertRaises(episode_alias.AliasRefused) as caught:
            episode_alias.record(self.con, "s998e0", "s003e0", actor="test")
        self.assertIn("s998e0 does not exist", str(caught.exception))
        # s000e0 is bench_0's target: aliases do not chain; the refusal
        # names the dependent alias.
        with self.assertRaises(episode_alias.AliasRefused) as caught:
            episode_alias.record(self.con, "s000e0", "s003e0", actor="test")
        self.assertIn("chain", str(caught.exception))
        self.assertIn("bench_0", str(caught.exception))
        self.assertEqual(_dump(self.con), before)

    def test_refuses_when_title_differs(self):
        for field, value in (("ep_title", "402: Walnuts"), ("show_title", "God Darn")):
            with self.subTest(field=field):
                self.con.execute(f"UPDATE episodes SET {field}=? WHERE id='bench_3'", (value,))
                self.con.commit()
                with self.assertRaises(episode_alias.AliasRefused) as caught:
                    episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
                self.assertIn(f"{field} differs", str(caught.exception))
                original = "401: Chestnuts" if field == "ep_title" else "God Damn"
                self.con.execute(f"UPDATE episodes SET {field}=? WHERE id='bench_3'", (original,))
                self.con.commit()

    def test_refuses_when_reads_differ(self):
        self.con.execute("UPDATE evidence SET orig_start_s=300, orig_end_s=330 WHERE id='t-2'")
        self.con.commit()
        before = _dump(self.con)
        with self.assertRaises(episode_alias.AliasRefused) as caught:
            episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        self.assertIn("reads differ", str(caught.exception))
        self.assertIn("t-2", str(caught.exception))
        self.assertEqual(_dump(self.con), before)

    def test_dry_run_lists_dependents(self):
        """R1-17: the operator sees what references the alias episode
        before recording it: show-map rows on its show, campaigns,
        placements and attachments, alerts, saved items and searches."""
        from earwitness.app import citations
        con, now = self.con, db.utcnow()
        show = _mapping(con, "bench_3")[0]
        ev_ref = citations.current_source_ref(con, "evidence", "t-1")
        con.execute("INSERT INTO campaigns (id, name, target_shows, updated_at)"
                    " VALUES (7, 'Acme Q4', ?, ?)", (json.dumps([show]), now))
        con.execute("INSERT INTO campaigns (id, name, updated_at) VALUES (8, 'Other', ?)", (now,))
        con.execute("INSERT INTO campaign_evidence (campaign_id, evidence_id) VALUES (8, 't-2')")
        con.execute("INSERT INTO expected_placements (id, campaign_id, scope, episode_id,"
                    " updated_at) VALUES ('pl-1', 7, 'episode', 'bench_3', ?)", (now,))
        con.execute("INSERT INTO expected_placements (id, campaign_id, scope, show_id,"
                    " updated_at) VALUES ('pl-2', 7, 'show_window', ?, ?)", (show, now))
        con.execute("INSERT INTO expected_placements (id, campaign_id, scope, episode_id,"
                    " updated_at) VALUES ('pl-3', 7, 'episode', 's003e0', ?)", (now,))
        con.execute("INSERT INTO placement_evidence (campaign_id, placement_id, ref,"
                    " attached_at) VALUES (7, 'pl-3', ?, ?)", (ev_ref + ":w:60:90", now))
        con.execute("INSERT INTO campaign_targets (id, campaign_id, scope, episode_id,"
                    " created_at, updated_at) VALUES ('tg-1', 7, 'episode', 'bench_3', ?, ?)",
                    (now, now))
        con.execute("INSERT INTO customer_alerts (name, query_json, created_at, updated_at)"
                    " VALUES ('God Damn reads', ?, ?, ?)",
                    (json.dumps({"mode": "ads", "show": [show]}), now, now))
        con.execute("INSERT INTO customer_alerts (name, query_json, created_at, updated_at)"
                    " VALUES ('unrelated', ?, ?, ?)",
                    (json.dumps({"mode": "ads", "show": [_mapping(con, "s003e0")[0]]}), now, now))
        con.execute("INSERT INTO customer_saved_items (id, type, target_json, saved_at,"
                    " updated_at) VALUES ('si-1', 'bookmark', ?, ?, ?)",
                    (json.dumps({"kind": "episode", "id": "bench_3"}), now, now))
        con.execute("INSERT INTO customer_saved_items (id, type, target_json, saved_at,"
                    " updated_at) VALUES ('si-2', 'excerpt', ?, ?, ?)",
                    (json.dumps({"ref": ev_ref, "start_s": None, "end_s": None}), now, now))
        con.execute("INSERT INTO customer_saved_items (id, type, target_json, saved_at,"
                    " updated_at) VALUES ('si-3', 'bookmark', ?, ?, ?)",
                    (json.dumps({"kind": "episode", "id": "s003e0"}), now, now))
        con.execute("INSERT INTO customer_saved_searches (name, query_json, created_at,"
                    " updated_at) VALUES ('gd', ?, ?, ?)",
                    (json.dumps({"show": [show]}), now, now))
        con.commit()
        before = _dump(con)
        result = episode_alias.record(con, "bench_3", "s003e0", actor="test", dry_run=True)
        self.assertEqual(_dump(con), before)
        deps = result["dependents"]
        self.assertEqual(deps["aliased_episodes"], [])
        self.assertEqual(deps["show_map"], [{"episode": "bench_3", "show_identity_id": show,
                                             "basis": _mapping(con, "bench_3")[1]}])
        self.assertEqual(deps["campaigns"], [7, 8])
        self.assertEqual(deps["placements"], ["pl-1", "pl-2", "pl-3"])
        self.assertEqual(deps["campaign_targets"], ["tg-1"])
        self.assertEqual([a["name"] for a in deps["alerts"]], ["God Damn reads"])
        self.assertEqual(deps["saved_items"], ["si-1", "si-2"])
        self.assertEqual([s["name"] for s in deps["saved_searches"]], ["gd"])

    def test_dry_run_dependents_empty_when_none(self):
        deps = episode_alias.record(self.con, "bench_3", "s003e0", actor="test",
                                    dry_run=True)["dependents"]
        self.assertEqual(deps["show_map"], [{"episode": "bench_3",
                                             "show_identity_id": _mapping(self.con, "bench_3")[0],
                                             "basis": "unresolved"}])
        self.assertEqual({k: v for k, v in deps.items() if k != "show_map"},
                         {"aliased_episodes": [], "campaigns": [], "placements": [],
                          "campaign_targets": [], "alerts": [], "saved_items": [],
                          "saved_searches": []})

    def test_unalias_audit_row_drops_the_operator_alias(self):
        """R1-16: latest episode_alias row per episode wins, an un-alias
        (alias_of null) included: the reimport then uses the seed plan."""
        from earwitness.app import import_sources
        episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        self.assertEqual(import_sources._operator_aliases(self.con)["bench_3"],
                         {"alias_of": "s003e0", "kind": "supplied-alias"})
        rev = db.bump_revision(self.con)
        db.audit(self.con, "test", episode_alias.AUDIT_ACTION, "episode", "bench_3",
                 {"episode": {"before": {"kind": "supplied-alias", "alias_of": "s003e0"},
                              "after": {"kind": "benchmark-unresolved", "alias_of": None}}}, rev)
        self.con.commit()
        self.assertEqual(import_sources._operator_aliases(self.con)["bench_3"],
                         {"alias_of": None, "kind": "benchmark-unresolved"})
        # A later re-record wins again.
        self.con.execute("UPDATE episodes SET kind='benchmark-unresolved', alias_of=NULL"
                         " WHERE id='bench_3'")
        self.con.commit()
        episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        self.assertIn("bench_3", import_sources._operator_aliases(self.con))

    def _row(self, ep_id, detail):
        rev = db.bump_revision(self.con)
        db.audit(self.con, "test", episode_alias.AUDIT_ACTION, "episode", ep_id, detail, rev)
        self.con.commit()

    def test_malformed_rows_are_skipped_and_never_unalias(self):
        """R2-02: only an after that holds the key alias_of with a null
        value is an un-alias. alias_of without kind, a detail without
        episode or after, and a detail that is not an object are skipped:
        the earlier valid row for the episode stays."""
        from earwitness.app import import_sources
        episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        want = {"alias_of": "s003e0", "kind": "supplied-alias"}
        self.assertEqual(import_sources._operator_aliases(self.con)["bench_3"], want)
        self._row("bench_3", {"episode": {"after": {"alias_of": "s003e0"}}})
        self._row("bench_3", {"episode": {"after": {"kind": "benchmark-unresolved"}}})
        self._row("bench_3", {"other": 1})
        self._row("bench_3", {"episode": {"before": {"alias_of": "s003e0"}}})
        self._row("bench_3", {"episode": {"after": {}}})
        self._row("bench_3", {"episode": {"after": None}})
        self._row("bench_3", {"episode": "x"})
        # Valid JSON that is not an object (the audit table refuses non-JSON).
        for raw in ("null", "[1]", '"x"'):
            rev = db.bump_revision(self.con)
            self.con.execute(
                "INSERT INTO audit (at, actor, action, target_type, target_id, detail,"
                " aggregate_rev) VALUES (?, 'test', ?, 'episode', 'bench_3', ?, ?)",
                (db.utcnow(), episode_alias.AUDIT_ACTION, raw, rev))
        self.con.commit()
        self.assertEqual(import_sources._operator_aliases(self.con)["bench_3"], want)
        # With no earlier valid row, a malformed row leaves the episode out.
        self._row("bench_9", {"episode": {"after": {"alias_of": "s003e0"}}})
        self.assertNotIn("bench_9", import_sources._operator_aliases(self.con))

    def test_explicit_null_alias_of_unaliases_with_or_without_kind(self):
        from earwitness.app import import_sources
        episode_alias.record(self.con, "bench_3", "s003e0", actor="test")
        self._row("bench_3", {"episode": {"after": {"alias_of": None}}})
        self.assertEqual(import_sources._operator_aliases(self.con)["bench_3"],
                         {"alias_of": None})


class RecordAliasCommandTests(unittest.TestCase):
    """The operator command: scripts/app_workspace.py record-alias."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix=".alias-cli-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.ws, True)
        con = db.connect(self.ws)
        db.init_schema(con)
        seed(con)
        con.close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = app_workspace.main(["--workspace", str(self.ws), "record-alias", *argv])
        return rc, out.getvalue(), err.getvalue()

    def _row(self):
        con = db.connect(self.ws)
        try:
            return tuple(con.execute(
                "SELECT kind, alias_of, revision FROM episodes WHERE id='bench_3'").fetchone())
        finally:
            con.close()

    def test_dry_run_then_run_then_noop(self):
        before = self._row()
        rc, out, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(self._row(), before)
        self.assertEqual(list(self.ws.glob("before-record-alias-*.sqlite3")), [])
        rc, out, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0")
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertTrue(result["changed"])
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(self._row()[:2], ("supplied-alias", "s003e0"))
        rc, out, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0")
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["changed"])
        self.assertNotIn("backup", json.loads(out))

    def _audit_actors(self):
        con = db.connect(self.ws)
        try:
            return [r[0] for r in con.execute(
                "SELECT actor FROM audit WHERE action='episode_alias' ORDER BY id")]
        finally:
            con.close()

    def test_actor_default_and_option(self):
        """R1-27: --actor names the operator; default 'operator'."""
        rc, _, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0")
        self.assertEqual(rc, 0)
        self.assertEqual(self._audit_actors(), ["operator"])
        con = db.connect(self.ws)
        con.execute("UPDATE episodes SET kind='benchmark-unresolved', alias_of=NULL"
                    " WHERE id='bench_3'")
        con.commit()
        con.close()
        rc, _, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0", "--actor", "reno")
        self.assertEqual(rc, 0)
        self.assertEqual(self._audit_actors(), ["operator", "reno"])

    def test_backup_name_has_alias_target_and_time_and_never_overwrites(self):
        import re
        rc, out, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0")
        self.assertEqual(rc, 0)
        first = Path(json.loads(out)["backup"])
        self.assertRegex(first.name,
                         r"^before-record-alias-bench_3-to-s003e0-\d{8}T\d{6}\d*Z\.sqlite3$")
        first_bytes = first.read_bytes()
        con = db.connect(self.ws)
        con.execute("UPDATE episodes SET kind='benchmark-unresolved', alias_of=NULL"
                    " WHERE id='bench_3'")
        con.commit()
        con.close()
        rc, out, _ = self.run_cli("--alias", "bench_3", "--target", "s003e0")
        self.assertEqual(rc, 0)
        second = Path(json.loads(out)["backup"])
        self.assertNotEqual(second, first)
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(len(list(self.ws.glob("before-record-alias-*.sqlite3"))), 2)
        self.assertTrue(re.search(r"bench_3-to-s003e0", second.name))

    def test_refusal_exits_nonzero(self):
        rc, _, err = self.run_cli("--alias", "bench_9", "--target", "s003e0")
        self.assertEqual(rc, 1)
        self.assertIn("transcript", err)

    def test_refuses_while_server_runs(self):
        import os
        (self.ws / "server.pid").write_text(str(os.getpid()))
        rc, _, err = self.run_cli("--alias", "bench_3", "--target", "s003e0")
        self.assertEqual(rc, 1)
        self.assertIn("server", err)


FIXTURE = REPO / "tests" / "fixtures" / "export-contract"


class SeedReimportKeepsOperatorAliasTests(unittest.TestCase):
    """A seed reimport keeps an alias an operator recorded (its
    episode_alias audit row is the source of truth): alias_of, kind and
    the show mapping stay, the twin show stays closed. Without such an
    audit row the reimport behaves as before (bench_0 path included)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".alias-seed-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "ws"
        self.export = self.tmp / "export"
        shutil.copytree(FIXTURE, self.export)
        tdir = self.export / "transcripts"
        canon = json.loads((tdir / "ep_fx1.json").read_text())
        eps = {e["ep_id"]: e for e in json.loads((self.export / "data/episodes.json").read_text())}
        twin = dict(canon, ep_id="bench_fx1", show=eps["ep_fx1"]["show_title"],
                    ep_title=eps["ep_fx1"]["ep_title"],
                    segments=[dict(s) for s in canon["segments"]])
        twin["segments"][3]["text"] = twin["segments"][3]["text"].replace("Host", "Hast")
        (tdir / "bench_fx1.json").write_text(json.dumps(twin))
        # The seed's exact-text alias pair (bench_0 -> s000e0).
        other = json.loads((tdir / "ep_fx2.json").read_text())
        for ep_id in ("s000e0", "bench_0"):
            (tdir / f"{ep_id}.json").write_text(json.dumps(dict(other, ep_id=ep_id)))

    def seed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = app_workspace.main(["--workspace", str(self.ws), "seed",
                                     "--source-root", str(self.export)])
        self.assertEqual(rc or 0, 0, out.getvalue())

    def record(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = app_workspace.main(["--workspace", str(self.ws), "record-alias",
                                     "--alias", "bench_fx1", "--target", "ep_fx1"])
        self.assertEqual(rc, 0)
        return json.loads(out.getvalue())

    def state(self):
        con = db.connect(self.ws)
        try:
            rows = {r["id"]: (r["kind"], r["alias_of"]) for r in con.execute(
                "SELECT id, kind, alias_of FROM episodes WHERE id IN"
                " ('bench_fx1', 'bench_0', 'ep_fx1')")}
            maps = {e: _mapping(con, e) for e in ("bench_fx1", "bench_0", "ep_fx1")}
            counts = (customer.query(con, _q("ads"))["total"],
                      customer.query(con, _q("mentions"))["total"],
                      len(customer.customer_shows(con)),
                      customer.overview(con)["coverage"])
            return rows, maps, counts
        finally:
            con.close()

    def test_reimport_keeps_the_recorded_alias(self):
        self.seed()
        self.assertTrue(self.record()["changed"])
        after = self.state()
        self.assertEqual(after[0]["bench_fx1"], ("supplied-alias", "ep_fx1"))
        self.assertEqual(after[1]["bench_fx1"], (after[1]["ep_fx1"][0], "alias_of"))
        self.seed()
        self.assertEqual(self.state(), after)
        self.assertFalse(self.record()["changed"])

    def test_reimport_after_unalias_row_uses_the_plan(self):
        """R1-16: an un-alias (episode row reset plus its episode_alias row
        with no alias_of) is the latest word: the reimport keeps it instead
        of restoring the earlier alias."""
        self.seed()
        self.record()
        con = db.connect(self.ws)
        con.execute("UPDATE episodes SET kind='benchmark-unresolved', alias_of=NULL"
                    " WHERE id='bench_fx1'")
        rev = db.bump_revision(con)
        db.audit(con, "test", "episode_alias", "episode", "bench_fx1",
                 {"episode": {"before": {"kind": "supplied-alias", "alias_of": "ep_fx1"},
                              "after": {"kind": "benchmark-unresolved", "alias_of": None}}}, rev)
        con.commit()
        con.close()
        self.seed()
        self.assertEqual(self.state()[0]["bench_fx1"], ("benchmark-unresolved", None))

    def test_reimport_without_audit_row_is_unchanged(self):
        self.seed()
        before = self.state()
        self.assertEqual(before[0]["bench_0"], ("supplied-alias", "s000e0"))
        self.assertEqual(before[0]["bench_fx1"], ("benchmark-unresolved", None))
        # An alias written without the operator step (no audit row) is
        # still reset by the reimport, as before.
        con = db.connect(self.ws)
        con.execute("UPDATE episodes SET alias_of='ep_fx1' WHERE id='bench_fx1'")
        con.close()
        self.seed()
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
