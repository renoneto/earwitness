"""Plan A excerpt view (design docs/plans/2026-10-22-plan-a-excerpt-view.md,
revision 4): the safe excerpt rule `pa1`, the two-source disclosure level,
both resolver paths, record and window routes, every reference form,
search matching on the safe excerpt only, and the read-route payload
contract (design 9 items 2-5, 6b, 8).

Red-capable command:
  PYTHONDONTWRITEBYTECODE=1 V/.venv-hosted/bin/python -B -m unittest tests.test_plan_a_excerpt
"""
from __future__ import annotations

import base64
import json
import re
import unittest

from earwitness.app import access, citations, customer, db, services
from tests.test_plan_a_fixture import (ADS, ENTRY, MARKERS, Workspace, leaked, member,
                                       safe_text, strings, token)

FORBIDDEN_KEYS = {"window", "segments", "context", "match_excerpt", "display_text",
                  "interval_text", "canonical_source_ref", "source_ref"}
AD_IDS = [ad[0] for ad in ADS]


def keys_in(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        out.update(value)
        for inner in value.values():
            keys_in(inner, out)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            keys_in(inner, out)
    return out


class _Base(unittest.TestCase):
    plan = "A"

    def setUp(self):
        self.ws = Workspace(self.plan)
        self.addCleanup(self.ws.close)
        self.con = self.ws.con

    def ref(self, evidence_id):
        return self.ws.ref("evidence", evidence_id)

    def record(self, ref, ctx=None):
        return services.customer_record(ctx or member(), self.con, ENTRY, ref)["record"]

    def window(self, ref, ctx=None, start_s=None, end_s=None, cursor=None):
        return services.customer_record_window(ctx or member(), self.con, ENTRY, ref,
                                               start_s, end_s, cursor)["record"]

    def search(self, mode, q="", ctx=None, **over):
        criteria = {"version": 2, "mode": mode, "q": q, **over}
        return services.customer_search(ctx or member(), self.con, ENTRY, criteria)

    def assertNoMarkers(self, payload, label=""):
        self.assertEqual(leaked(payload), [], label)

    def assertTextsSafe(self, payload, label=""):
        """Every record-bound text value equals its pin's safe excerpt."""
        for node in _dicts(payload):
            evidence = node.get("evidence") if isinstance(node.get("evidence"), dict) else {}
            rid = node.get("evidence_id") or node.get("record_id") or evidence.get("id")
            if rid not in AD_IDS:
                continue
            for key in ("text", "excerpt"):
                if key in node and node[key] not in (None, ""):
                    self.assertEqual(node[key], safe_text(rid), f"{label} {rid}.{key}")


def _dicts(value):
    if isinstance(value, dict):
        yield value
        for inner in value.values():
            yield from _dicts(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _dicts(inner)


# ---------------------------------------------------------------- rule pa1

class SafeExcerptRuleTest(unittest.TestCase):
    def rule(self, segments, a, b):
        return citations.safe_excerpt_from_segments(segments, a, b)

    def test_only_overlapping_segments_inside_the_context_bound_are_kept_whole(self):
        segs = [{"start": 0, "end": 5, "text": "before"},
                {"start": 25, "end": 38, "text": "first"},
                {"start": 38, "end": 50, "text": "second"},
                {"start": 50, "end": 60, "text": "after"}]
        self.assertEqual(self.rule(segs, 30, 45),
                         {"text": "first second", "start_s": 25, "end_s": 50})

    def test_a_segment_reaching_past_twenty_seconds_is_dropped_whole(self):
        segs = [{"start": 100, "end": 220, "text": "broad editorial then the ad"}]
        self.assertIsNone(self.rule(segs, 200, 210))
        segs = [{"start": 9.5, "end": 20, "text": "too early"},
                {"start": 20, "end": 50.5, "text": "too late"}]
        self.assertIsNone(self.rule(segs, 30, 30.25))
        edge = [{"start": 10, "end": 50, "text": "exactly at the bound"}]
        self.assertEqual(self.rule(edge, 30, 30)["text"], "exactly at the bound")

    def test_segments_join_in_time_order_and_untimed_segments_never_count(self):
        segs = [{"start": 38, "end": 50, "text": "second"}, {"text": "untimed words"},
                {"start": "x", "end": 40, "text": "bad bounds"},
                {"start": 25, "end": 38, "text": "first"}]
        self.assertEqual(self.rule(segs, 30, 45)["text"], "first second")
        self.assertIsNone(self.rule([{"text": "no times at all"}], 0, 10))
        self.assertIsNone(self.rule(segs, None, 45))

    def test_the_cut_is_at_or_before_character_600_at_a_word_boundary(self):
        words = " ".join(f"w{i:04d}" for i in range(200))  # 999 characters
        out = self.rule([{"start": 0, "end": 10, "text": words}], 1, 9)["text"]
        self.assertLessEqual(len(out), 600)
        self.assertTrue(words.startswith(out))
        self.assertTrue(words[len(out)] == " ")
        exact = "a" * 599 + " b"
        self.assertEqual(self.rule([{"start": 0, "end": 10, "text": exact}], 1, 9)["text"],
                         "a" * 599)

    def test_the_fixture_pins_give_the_hand_written_excerpts(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        for ev, ep, start, end, *_ in ADS:
            got = citations.safe_excerpt(ws.con, ws.tv[ep], start, end)
            self.assertEqual(got and got["text"], safe_text(ev), ev)


# ------------------------------------------------------- disclosure level

class LevelTest(_Base):
    def pins(self, evidence_id):
        state, _ = citations._parse_source_ref(self.ref(evidence_id))
        row = self.con.execute("SELECT kind, pins_json FROM source_states WHERE id=?",
                               (state,)).fetchone()
        return row["kind"], json.loads(row["pins_json"])

    def level(self, evidence_id, ctx=None, export=False):
        kind, pins = self.pins(evidence_id)
        return citations.disclosure_level(self.con, kind, pins, ctx or member(), export=export)

    def test_plan_a_is_excerpt_and_plan_b_is_full(self):
        self.assertEqual(self.level("ad-a1"), "excerpt")
        self.assertEqual(self.level("ad-a1", export=True), "excerpt")
        self.ws.plan("B")
        self.assertEqual(self.level("ad-a1"), "full")

    def test_a_credential_refusal_never_selects_the_excerpt(self):
        research_only = token(["research:read"])
        with self.assertRaises(access.AuthorizationError):
            self.level("ad-a1", research_only)
        record = self.record(self.ref("ad-a1"), research_only)
        self.assertNotEqual(record.get("disclosure"), "excerpt")
        self.assertNotIn("text", record)

    def test_a_transcript_capable_token_stays_at_excerpt_level(self):
        capable = token(["research:read", "transcripts:read"])
        record = customer.record(self.con, self.ref("ad-a1"), ctx=capable)
        self.assertEqual(record["disclosure"], "excerpt")
        self.assertEqual(record["text"], safe_text("ad-a1"))

    def test_a_research_only_token_in_plan_b_gets_no_transcript(self):
        self.ws.plan("B")
        record = customer.record(self.con, self.ref("ad-a1"), ctx=token(["research:read"]))
        self.assertNotIn("text", record)
        self.assertNotIn("canonical_source_ref", record)

    def assertRefused(self, ctx=None, export=False):
        with self.assertRaises(access.AuthorizationError) as raised:
            self.level("ad-a1", ctx, export=export)
        self.assertEqual(raised.exception.status, 403)
        with self.assertRaises(access.AuthorizationError):
            self.record(self.ref("ad-a1"), ctx)

    def test_an_excluded_transcript_source_is_refused(self):
        ctx = member(content_policy={"sources": ["detector:ad-a1", "episode:ep_a",
                                                 "episodes.json"]})
        self.assertRefused(ctx)

    def test_metadata_denied_on_t_with_transcript_denied_is_refused(self):
        self.ws.row("transcript:ep_a", "metadata", "deny")
        self.assertRefused()

    def test_excerpt_denied_on_t_with_excerpt_allowed_on_e_is_refused(self):
        self.ws.row("transcript:ep_a", "excerpt", "deny")
        self.assertRefused()

    def test_excerpt_denied_on_e_is_refused(self):
        self.ws.row("detector:ad-a1", "excerpt", "deny")
        self.assertRefused()

    def test_the_export_only_equivalents_refuse_export(self):
        for key in ("transcript:ep_a", "detector:ad-a1"):
            with self.subTest(source=key):
                self.ws.plan("A")
                self.ws.row(key, "excerpt", "allow", 0)
                self.assertEqual(self.level("ad-a1"), "excerpt")
                with self.assertRaises(access.AuthorizationError):
                    self.level("ad-a1", export=True)
        self.ws.plan("A")
        self.ws.row("transcript:ep_a", "metadata", "allow", 0)
        with self.assertRaises(access.AuthorizationError):
            self.level("ad-a1", export=True)

    def test_plan_b_transcript_export_refusal_is_not_an_excerpt_selection(self):
        # Plan B: transcript is viewable but not exportable. The allowing
        # transcript row means the refusal propagates (design 4 step 6).
        self.ws.plan("B")
        with self.assertRaises(access.AuthorizationError):
            self.level("ad-a1", export=True)

    def test_the_level_is_computed_per_request_never_cached(self):
        ref = self.ref("ad-a1")
        self.assertEqual(self.record(ref)["disclosure"], "excerpt")
        self.ws.plan("B")
        full = self.record(ref)
        self.assertNotIn("disclosure", full)
        self.assertIn("canonical_source_ref", full)
        self.ws.plan("A")
        self.assertEqual(self.record(ref)["disclosure"], "excerpt")
        # A new resolution pass on each request, too.
        pass_a = citations.ResolutionPass(self.con, member()).current(ref)
        self.ws.plan("B")
        pass_b = citations.ResolutionPass(self.con, member()).current(ref)
        self.assertEqual(pass_a.get("disclosure"), "excerpt")
        self.assertNotEqual(pass_b.get("disclosure"), "excerpt")

    def test_no_client_field_selects_the_level(self):
        ref = self.ref("ad-a1")
        forged = base64.urlsafe_b64encode(json.dumps(
            {"ref": ref, "disclosure": "full", "level": "full"}).encode()).decode()
        out = self.window(ref, cursor=forged)
        self.assertEqual(out["disclosure"], "excerpt")
        self.assertEqual(out["text"], safe_text("ad-a1"))
        with self.assertRaises(Exception):
            self.search("ads", "lumen", disclosure="full")


# ------------------------------------------------------- resolver paths

class ResolverPathsTest(_Base):
    def test_resolve_source_state_returns_the_excerpt_projection(self):
        for ev in AD_IDS:
            with self.subTest(evidence=ev):
                out = citations.resolve_source_state(self.con, self.ref(ev), ctx=member())
                self.assertEqual(out["disclosure"], "excerpt")
                self.assertEqual(out["text"], safe_text(ev))
                self.assertFalse(FORBIDDEN_KEYS & keys_in(out), FORBIDDEN_KEYS & keys_in(out))
                self.assertNotIn("excerpt", out.get("evidence") or {})
                self.assertNoMarkers(out)
        out = citations.resolve_source_state(self.con, self.ref("ad-a1"), ctx=member())
        self.assertEqual(out["excerpt_bounds"], {"start_s": 25.0, "end_s": 50.0})
        self.assertIsNone(citations.resolve_source_state(
            self.con, self.ref("ad-a2"), ctx=member())["excerpt_bounds"])

    def test_the_resolution_pass_returns_the_same_safe_text_and_never_rebuilds(self):
        resolution = citations.ResolutionPass(self.con, member())
        for ev in AD_IDS:
            with self.subTest(evidence=ev):
                ref = self.ref(ev)
                got = resolution.current(ref)
                self.assertEqual(got["disclosure"], "excerpt")
                self.assertEqual(got["text"], safe_text(ev))
                self.assertNoMarkers(got)
        ref = self.ref("ad-a1")
        windowed = resolution.current(f"{ref}:w:0.0:60.0")
        self.assertEqual(windowed["text"], safe_text("ad-a1"))

    def test_the_owner_context_resolves_at_excerpt_level_in_plan_a(self):
        out = citations.resolve_source_state(self.con, self.ref("ad-a1"))
        self.assertEqual(out["disclosure"], "excerpt")
        self.assertEqual(out["text"], safe_text("ad-a1"))

    def test_plan_b_resolvers_are_unchanged(self):
        self.ws.plan("B")
        out = citations.resolve_source_state(self.con, self.ref("ad-a1"), ctx=member())
        self.assertNotIn("disclosure", out)
        self.assertIn("canonical_source_ref", out)
        self.assertEqual(out["text"], "this episode is brought to you by Lumen lamps"
                                      " go to lumen dot com slash morning zebra")
        got = citations.ResolutionPass(self.con, member()).current(self.ref("ad-a2"))
        self.assertIn("qzoversized", got["text"])


# --------------------------------------------------- record routes, refs

class RecordRoutesTest(_Base):
    def projection(self, out):
        return {k: v for k, v in out.items() if k not in ("ref", "ad_break")}

    def test_every_ref_form_gives_the_same_excerpt_projection(self):
        ref = self.ref("ad-a1")
        sv = self.ws.sv["ad-a1"]
        base = self.projection(self.record(ref))
        self.assertEqual(base["disclosure"], "excerpt")
        self.assertEqual(base["text"], safe_text("ad-a1"))
        self.assertEqual(base["excerpt_bounds"], {"start_s": 25.0, "end_s": 50.0})
        forms = [ref, f"ev:ad-a1@v{sv}", f"{ref}:w:30.0:45.0", f"{ref}:w:31.0:32.0"]
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(self.projection(self.record(form)), base)
                self.assertEqual(self.projection(self.window(form)), base)
                self.assertEqual(self.projection(self.window(form, start_s=30.0, end_s=31.0)), base)
        cursor = base64.urlsafe_b64encode(json.dumps({"ref": ref, "si": 0, "co": 0}).encode()).decode()
        self.assertEqual(self.projection(self.window(ref, cursor=cursor)), base)

    def test_records_carry_no_window_and_no_unsafe_field(self):
        for ev in AD_IDS:
            for out in (self.record(self.ref(ev)), self.window(self.ref(ev))):
                with self.subTest(evidence=ev):
                    self.assertFalse(FORBIDDEN_KEYS & keys_in(out), FORBIDDEN_KEYS & keys_in(out))
                    self.assertNoMarkers(out)
                    self.assertEqual(out["text"], safe_text(ev))

    def test_excerpt_unavailable_ads_keep_their_metadata(self):
        for ev in ("ad-a2", "ad-a4"):
            out = self.window(self.ref(ev))
            self.assertIsNone(out["text"])
            self.assertIsNone(out["excerpt_bounds"])
            self.assertEqual(out["evidence"]["id"], ev)
            self.assertTrue(out["attribution"])
            self.assertTrue(out["episode"]["show_title"])

    def test_an_episode_ref_gives_metadata_and_its_ads_only(self):
        for form in ("ep:ep_a", self.ws.ref("episode", "ep_a"),
                     f"tx:ep_a@v{self.ws.tv['ep_a']}:2"):
            with self.subTest(form=form):
                out = self.window(form)
                self.assertEqual(out["disclosure"], "excerpt")
                self.assertIsNone(out.get("text"))
                self.assertNoMarkers(out)
                self.assertFalse(FORBIDDEN_KEYS & keys_in(out), FORBIDDEN_KEYS & keys_in(out))
                ads = {a["evidence"]["id"]: a for a in out["ads"]}
                self.assertEqual(set(ads), {"ad-a1", "ad-a2"})
                self.assertEqual(ads["ad-a1"]["text"], safe_text("ad-a1"))
                self.assertIsNone(ads["ad-a2"]["text"])
                self.assertEqual(out["episode"]["ep_title"], "Monday")

    def test_the_episode_page_lists_ads_with_excerpts(self):
        out = services.customer_episode_reads(member(), self.con, ENTRY, "ep_a")["episode"]
        self.assertNoMarkers(out)
        refs = [a["ref"] for a in out["ads"]]
        self.assertTrue(all(isinstance(r, str) and r.startswith("sr1:") for r in refs))
        self.assertEqual({a["evidence_id"]: a.get("text") for a in out["ads"]},
                         {"ad-a1": safe_text("ad-a1"), "ad-a2": None})


# ------------------------------------------------------------- search

QUERIES = ["lumen", "zebra", "brightly", "nimbus", "acme", "qzbound", '"zebra qzbound"',
           "qzlate", "qzother", "qzoversized", "qzoverother", "qzdetector", "qzstored",
           "qzstart", "qznotimed", "council", "closing words", ""]


class SearchTest(_Base):
    def found(self, mode, q):
        out = self.search(mode, q)
        return out, sorted(i.get("evidence_id") or i.get("episode_id") for i in out["items"])

    def test_ads_match_on_the_safe_excerpt_and_metadata_only(self):
        expect = {"lumen": ["ad-a1"], "zebra": ["ad-a1"], "brightly": ["ad-a3"],
                  "nimbus": ["ad-a5"], "acme": ["ad-a2"], "qzbound": [], '"zebra qzbound"': [],
                  "qzlate": [], "qzoversized": [], "qzdetector": [], "qzstored": [],
                  "qzstart": [], "council": [], "closing words": [], "zesty": ["ad-a4"]}
        for q, want in expect.items():
            with self.subTest(q=q):
                out, got = self.found("ads", q)
                self.assertEqual(got, want)
                self.assertEqual(out["total"], len(want))
                for item in out["items"]:
                    self.assertTrue(item["ref"].startswith("sr1:"))
                    self.assertEqual(item["text"], safe_text(item["evidence_id"]) or "")
                    self.assertFalse({"context", "match_excerpt"} & set(item))
                    self.assertNotIn("display_text", item.get("read") or {})

    def test_mentions_are_not_offered(self):
        for q in ("lumen", "qzstart", ""):
            out = self.search("mentions", q)
            self.assertEqual((out["items"], out["total"], out.get("plan_limited")), ([], 0, True))

    def test_episodes_match_metadata_and_their_ads_safe_excerpts(self):
        expect = {"lumen": ["ep_a"], "monday": ["ep_a"], "brightly": ["ep_b"],
                  "qzstart": [], "qzbound": [], "council": [], "qzlate": []}
        for q, want in expect.items():
            with self.subTest(q=q):
                out, got = self.found("episodes", q)
                self.assertEqual(got, want)
                self.assertNoMarkers({k: v for k, v in out.items() if k != "criteria"})

    def test_search_results_carry_no_marker(self):
        for mode in ("ads", "episodes", "mentions"):
            for q in ("", "lumen", "brightly"):
                out = self.search(mode, q)
                self.assertNoMarkers({k: v for k, v in out.items() if k != "criteria"}, (mode, q))
                self.assertTextsSafe(out["items"], (mode, q))

    def test_pages_and_summaries_stay_safe(self):
        first = self.search("ads", "", page=1)
        self.assertEqual(first["total"], 5)
        summary = services.customer_summary(member(), self.con, ENTRY,
                                            {"version": 2, "mode": "ads", "q": ""}, "brand")
        self.assertNoMarkers({k: v for k, v in summary.items() if k != "criteria"})

    def test_plan_b_search_is_unchanged(self):
        self.ws.plan("B")
        out, got = self.found("ads", "qzoversized")
        self.assertEqual(got, ["ad-a2"])
        self.assertNotIn("plan_limited", self.search("mentions", "qzstart"))
        self.assertEqual(self.found("mentions", "qzstart")[1], ["ep_a", "ep_d"])


class InferenceTest(unittest.TestCase):
    """Design 9 item 3: two fixtures identical except the text after
    character 600 and the oversized segment's outside-ad words give
    identical search answers."""

    def setUp(self):
        self.one = Workspace("A")
        self.two = Workspace("A", variant=True)
        self.addCleanup(self.one.close)
        self.addCleanup(self.two.close)

    def normal(self, ws, value):
        names = {r["id"]: f"{r['kind']}:{r['record_id']}" for r in ws.con.execute(
            "SELECT id, kind, record_id FROM source_states")}

        def walk(v):
            if isinstance(v, dict):
                # Payload digests identify stored versions; they are integrity
                # pins, not text, and differ with any payload byte.
                return {k: walk(x) for k, x in v.items()
                        if k not in ("revision", "view_revision", "criteria", "sha256", "created_at",
                                     "source_payload_digest", "transcript_payload_digest")}
            if isinstance(v, list):
                # Ties order by the random publication ids: compare as sets.
                return sorted((walk(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True))
            if isinstance(v, str) and v.startswith("sr1:"):
                state, _sep, rest = v[4:].partition(":")
                return names.get("sr1:" + state, v) + (":" + rest if rest else "")
            if isinstance(v, str) and v.startswith("brk:"):
                return "brk"  # a break id digests its publication ids
            if isinstance(v, str) and "sr1%3A" in v:
                # A clip's media src names its publication (REDESIGN S4).
                return re.sub(r"sr1%3A([0-9a-f]{32})",
                              lambda m: names.get("sr1:" + m.group(1), "sr1:?"), v)
            return v
        return walk(value)

    def test_every_search_mode_count_and_basis_is_identical(self):
        for mode in ("ads", "episodes", "mentions"):
            for q in QUERIES:
                with self.subTest(mode=mode, q=q):
                    crit = {"version": 2, "mode": mode, "q": q}
                    a = services.customer_search(member(), self.one.con, ENTRY, dict(crit))
                    b = services.customer_search(member(), self.two.con, ENTRY, dict(crit))
                    self.assertEqual(self.normal(self.one, a), self.normal(self.two, b))
        for q in QUERIES:
            crit = {"version": 2, "mode": "ads", "q": q}
            a = services.customer_summary(member(), self.one.con, ENTRY, dict(crit), "brand")
            b = services.customer_summary(member(), self.two.con, ENTRY, dict(crit), "brand")
            self.assertEqual(self.normal(self.one, a), self.normal(self.two, b), q)


# ---------------------------------------------- payload sweep (item 2)

class PayloadSweepTest(_Base):
    """Every other read route a member opens, walked for markers; record
    texts equal their pin's safe excerpt."""

    def test_read_routes_carry_no_marker(self):
        ctx = member()
        reads = {
            "overview": lambda: services.customer_overview(ctx, self.con, ENTRY),
            "brands": lambda: services.customer_brands(ctx, self.con, ENTRY),
            "brand": lambda: services.customer_brand_detail(ctx, self.con, ENTRY, "lumen"),
            "shows": lambda: services.customer_shows(ctx, self.con, ENTRY),
            "episodes": lambda: [services.customer_episode_reads(ctx, self.con, ENTRY, ep)
                                 for ep in ("ep_a", "ep_b", "ep_c", "ep_d")],
            "records": lambda: [self.window(self.ref(ev)) for ev in AD_IDS],
            "episode records": lambda: [self.window(f"ep:{ep}") for ep in ("ep_a", "ep_d")],
        }
        for label, read in reads.items():
            with self.subTest(route=label):
                out = read()
                self.assertNoMarkers(out, label)
                self.assertTextsSafe(out, label)
        shows = services.customer_shows(ctx, self.con, ENTRY)["shows"]
        for show in shows:
            out = services.customer_show_detail(ctx, self.con, ENTRY, show["id"])
            self.assertNoMarkers(out, show["id"])


# ------------------------------------------------------ positive (item 4)

class PositiveTest(_Base):
    def test_plan_a_gets_content(self):
        out = self.search("ads", "")
        self.assertEqual(out["total"], 5)
        self.assertEqual(self.record(self.ref("ad-a3"))["text"], safe_text("ad-a3"))
        ep = services.customer_episode_reads(member(), self.con, ENTRY, "ep_b")["episode"]
        self.assertEqual([a["evidence_id"] for a in ep["ads"]], ["ad-a3"])
        overview = services.customer_overview(member(), self.con, ENTRY)
        self.assertEqual(overview["coverage"]["episodes"], 4)
        self.assertNoMarkers(overview)


if __name__ == "__main__":
    unittest.main()
