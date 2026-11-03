"""Plan A excerpt view: the shared rights fixture (design 9 item 1).

A file-backed workspace built from real domain rows (source versions,
episodes, evidence, reviews, brand associations, publications) and a
source_content_policy that is Plan A ("transcript: deny" on every source,
metadata and excerpt allowed for viewing and export) or Plan B
(transcript viewable, not exportable). Independent of the purchase seat
code. Markers (qz...) sit only outside the safe excerpts:

- qzstart: the first words of a transcript, in no ad's segments;
- qzbound: across a segment boundary ("zebra qzbound"), the second half
  in a segment that does not overlap the ad;
- qzoversized: inside a 120-second segment around a 10-second ad, within
  its first 600 characters but outside the ad;
- qzdetector: in a detector-supplied stored excerpt;
- qzlate: after character 600 of an in-bounds excerpt;
- qzstored: in every stored evidence excerpt;
- qznotimed: the only words of an episode with no timed segment over its
  ad's span.

`untranscribed=True` adds UNTRANSCRIBED: a detector ad (`ad-n1`, brand
Glimmer) on an episode with no transcript (`ep_n`), so its evidence row
has no transcript version; its stored excerpt holds qzdetector and
qzstored. Such a row has no safe excerpt.

`variant=True` builds the twin fixture: identical except the text after
character 600 and the oversized segment's outside-ad words.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from earwitness.app import access, citations, customer, db
from earwitness.app import ad_reads as _ad

MARKERS = ("qzstart", "qzbound", "qzoversized", "qzdetector", "qzlate", "qzstored", "qznotimed")
VARIANT_MARKERS = ("qzother", "qzoverother")
ENTRY = {"row_namespace": "local", "workspace_id": "local", "is_owner": True}

FILLER = ("the host reads a long description of the product and its many features"
          " for listeners who stayed through the whole break today")


def _late_segments(variant: bool) -> list:
    """In-bounds segments around ad A5 [98, 122] whose joined text runs past
    600 characters; the marker sits after character 600."""
    segs, at = [], 96.0
    for i in range(6):
        text = f"nimbus part {i} " + FILLER
        segs.append({"start": at, "end": at + 4.0, "text": text})
        at += 4.0
    late = "qzother" if variant else "qzlate"
    segs.append({"start": at, "end": at + 4.0, "text": f"{late} closing words of the nimbus read"})
    return segs


def transcripts(variant: bool = False) -> dict:
    over = "qzoverother" if variant else "qzoversized"
    return {
        "ep_a": [
            {"start": 0.0, "end": 8.0, "text": "qzstart welcome to the morning show"},
            {"start": 8.0, "end": 25.0, "text": "we talk about the weather today"},
            {"start": 25.0, "end": 38.0, "text": "this episode is brought to you by Lumen lamps"},
            {"start": 38.0, "end": 50.0, "text": "go to lumen dot com slash morning zebra"},
            {"start": 50.0, "end": 90.0, "text": "qzbound returns after the break with news"},
            {"start": 100.0, "end": 220.0,
             "text": f"{over} long editorial conversation about the city council"
                     " and then a short read sponsored by Acme tools"},
        ],
        "ep_b": [
            {"start": 0.0, "end": 10.0, "text": "intro words for the evening"},
            {"start": 10.0, "end": 22.0, "text": "today's show is sponsored by Brightly"},
            {"start": 22.0, "end": 30.0, "text": "visit brightly dot com for more"},
        ],
        # No timed segment for its ad's span: no safe excerpt.
        "ep_c": [{"start": 0.0, "end": 4.0, "text": "qznotimed Zesty sauce is great"}],
        "ep_d": [{"start": 0.0, "end": 96.0, "text": "qzstart editorial opening of the day"}]
                + _late_segments(variant),
    }


EPISODES = [
    {"ep_id": "ep_a", "show_title": "Morning Show", "feed_url": "https://feeds.example/a.xml",
     "ep_title": "Monday", "pub_date": "2026-09-28", "category": "News"},
    {"ep_id": "ep_b", "show_title": "Evening Show", "feed_url": "https://feeds.example/b.xml",
     "ep_title": "Tuesday", "pub_date": "2026-09-29", "category": "News"},
    {"ep_id": "ep_c", "show_title": "Noon Show", "feed_url": "https://feeds.example/c.xml",
     "ep_title": "Wednesday", "pub_date": "2026-09-30", "category": "News"},
    {"ep_id": "ep_d", "show_title": "Night Show", "feed_url": "https://feeds.example/d.xml",
     "ep_title": "Thursday", "pub_date": "2026-10-01", "category": "News"},
]

# (evidence id, episode, start, end, brand, stored excerpt)
ADS = [
    ("ad-a1", "ep_a", 30.0, 45.0, "Lumen", "qzstored Lumen lamps read"),
    ("ad-a2", "ep_a", 200.0, 210.0, "Acme", "qzstored qzoversized Acme tools"),
    ("ad-a3", "ep_b", 10.0, 22.0, "Brightly", "qzdetector qzstored Brightly sponsorship read"),
    ("ad-a4", "ep_c", 5.0, 15.0, "Zesty", "qzstored Zesty sauce"),
    ("ad-a5", "ep_d", 98.0, 122.0, "Nimbus", "qzstored qzlate Nimbus"),
]


# A detector ad on an episode with no transcript: no transcript version.
UNTRANSCRIBED_EPISODE = {"ep_id": "ep_n", "show_title": "Late Show",
                         "feed_url": "https://feeds.example/n.xml", "ep_title": "Friday",
                         "pub_date": "2026-10-02", "category": "News"}
UNTRANSCRIBED = ("ad-n1", "ep_n", 40.0, 55.0, "Glimmer", "qzdetector qzstored Glimmer read")


def safe_text(evidence_id: str, variant: bool = False) -> str | None:
    """The expected pa1 text of each ad, written out by hand."""
    if evidence_id == "ad-a1":
        return ("this episode is brought to you by Lumen lamps"
                " go to lumen dot com slash morning zebra")
    if evidence_id == "ad-a3":
        return "today's show is sponsored by Brightly"
    if evidence_id == "ad-a5":
        joined = " ".join(s["text"] for s in _late_segments(variant))
        cut = joined[:600]
        if not joined[600].isspace():
            cut = cut[:cut.rfind(" ")]
        return cut.rstrip()
    return None  # a2: oversized segment; a4: no timed segments


def _add_version(con, key, payload) -> int:
    blob = json.dumps(payload, sort_keys=True)
    cur = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload, is_active, created_at)"
        " VALUES (?, (SELECT COALESCE(MAX(version_no),0)+1 FROM source_versions WHERE source_key=?),"
        " ?, ?, 1, ?)", (key, key, hashlib.sha256(blob.encode()).hexdigest(), blob, db.utcnow()))
    return cur.lastrowid


POLICY = {
    "A": {"metadata": ("allow", 1), "excerpt": ("allow", 1), "transcript": ("deny", 0),
          "audio": ("allow", 0)},
    "B": {"metadata": ("allow", 1), "excerpt": ("allow", 1), "transcript": ("allow", 0),
          "audio": ("allow", 0)},
}


def set_plan(con, plan: str) -> None:
    """Every source key and episode gets one row per category (the rows the
    rights import writes); Plan A denies transcript everywhere."""
    keys = {r[0] for r in con.execute("SELECT DISTINCT source_key FROM source_versions")}
    keys |= {"episode:" + r[0] for r in con.execute("SELECT id FROM episodes")}
    for key in sorted(keys):
        for category, (view, export) in POLICY[plan].items():
            con.execute("INSERT INTO source_content_policy (workspace_id, source_key,"
                        " content_category, agent_disclosure, export_eligible, updated_at)"
                        " VALUES ('local', ?, ?, ?, ?, ?) ON CONFLICT(workspace_id, source_key,"
                        " content_category) DO UPDATE SET agent_disclosure=excluded.agent_disclosure,"
                        " export_eligible=excluded.export_eligible",
                        (key, category, view, export, db.utcnow()))


def set_row(con, key, category, view, export=None):
    con.execute("UPDATE source_content_policy SET agent_disclosure=?,"
                " export_eligible=COALESCE(?, export_eligible)"
                " WHERE source_key=? AND content_category=?", (view, export, key, category))


class Workspace:
    """One file-backed fixture workspace. clips: registered clip rows."""

    def __init__(self, plan: str = "A", *, variant: bool = False,
                 untranscribed: bool = False):
        self.dir = tempfile.TemporaryDirectory(prefix="ew-plan-a-")
        self.root = Path(self.dir.name)
        self.path = self.root / "workspace.sqlite3"
        self.variant = variant
        self.untranscribed = untranscribed
        con = sqlite3.connect(self.path, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        db.init_schema(con)
        self.con = con
        self.tv: dict = {}
        self.sv: dict = {}
        self.clips: dict = {}
        con.execute("BEGIN IMMEDIATE")
        self._seed()
        set_plan(con, plan)
        con.execute("COMMIT")

    def close(self):
        self.con.close()
        self.dir.cleanup()

    # -- seed -----------------------------------------------------------
    def _seed(self):
        con, now = self.con, db.utcnow()
        _add_version(con, "episodes.json",
                     EPISODES + ([UNTRANSCRIBED_EPISODE] if self.untranscribed else []))
        for meta in EPISODES:
            ep = meta["ep_id"]
            tv = _add_version(con, f"transcript:{ep}", {"segments": transcripts(self.variant)[ep]})
            self.tv[ep] = tv
            con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
                        " fetch_date, transcript_version_id, revision, updated_at)"
                        " VALUES (?, 'supplied', ?, ?, ?, ?, '2026-10-11', ?, 1, ?)",
                        (ep, meta["show_title"], meta["ep_title"], meta["category"],
                         meta["pub_date"], tv, now))
        for ev, ep, start, end, brand, stored in ADS:
            stored = self.stored(stored)
            sv = _add_version(con, f"detector:{ev}", {"origin": "synthetic", "excerpt": stored})
            self.sv[ev] = sv
            con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                        " transcript_version_id, source_record_key, assoc_revision, record_kind)"
                        " VALUES (?, 'detector', ?, ?, ?, ?, ?, '{}', 1, ?, ?, ?, 0, 'candidate')",
                        (ev, sv, ep, start, end, stored, now, self.tv[ep], ev))
            con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                        " VALUES (?, 'finding', 1, ?)", (ev, now))
            con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision,"
                        " updated_at) VALUES (?, ?, 'spoken', 'test', 1, ?)", (ev, brand, now))
        if self.untranscribed:
            self._seed_untranscribed()
        self._seed_reads()
        from tests import backend_fixtures
        backend_fixtures.publish_all_records(con)

    def _seed_untranscribed(self):
        con, now = self.con, db.utcnow()
        meta = UNTRANSCRIBED_EPISODE
        con.execute("INSERT INTO episodes (id, kind, show_title, ep_title, category, pub_date,"
                    " fetch_date, transcript_version_id, revision, updated_at)"
                    " VALUES (?, 'supplied', ?, ?, ?, ?, '2026-10-11', NULL, 1, ?)",
                    (meta["ep_id"], meta["show_title"], meta["ep_title"], meta["category"],
                     meta["pub_date"], now))
        ev, ep, start, end, brand, stored = UNTRANSCRIBED
        sv = _add_version(con, f"detector:{ev}", {"origin": "synthetic", "excerpt": stored})
        self.sv[ev] = sv
        con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                    " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                    " transcript_version_id, source_record_key, assoc_revision, record_kind)"
                    " VALUES (?, 'detector', ?, ?, ?, ?, ?, '{}', 1, ?, NULL, ?, 0, 'candidate')",
                    (ev, sv, ep, start, end, stored, now, ev))
        con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                    " VALUES (?, 'finding', 1, ?)", (ev, now))
        con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision,"
                    " updated_at) VALUES (?, ?, 'spoken', 'test', 1, ?)", (ev, brand, now))

    def _clip(self, clip_id, ep, start, end, version=None):
        # Frame-only MP3 bytes for the clip's length, as a load admits them
        # (CLIP-START cuts a hosted clip on its frames; CST-R1-01).
        from tests.test_clip_name import frames
        data = frames(end - start)
        path = self.root / f"{clip_id}.mp3"
        path.write_bytes(data)
        self.con.execute("INSERT INTO ad_clips (id, episode_id, rel, path, sha256, bytes, start_s,"
                         " end_s, transcript_version_id) VALUES (?,?,?,?,?,?,?,?,?)",
                         (clip_id, ep, f"clips/{ep}/{clip_id}.mp3", str(path),
                          hashlib.sha256(data).hexdigest(), len(data), start, end,
                          self.tv[ep] if version is None else version))
        self.clips[clip_id] = data

    def _seed_reads(self):
        """Side tables as a load writes them: each read's quote and folded
        search text are its full interval text (never shown at excerpt
        level), plus registered clips."""
        con = self.con
        _ad.ensure_schema(con)
        # a1: a clip inside the padding (5 s before, 3 s after).
        self._clip("clip-good", "ep_a", 25.0, 48.0)
        # a2: on another transcript version.
        self._clip("clip-version", "ep_a", 196.0, 212.0, version=self.tv["ep_b"])
        # a3: a broad clip that contains the ad.
        self._clip("clip-broad", "ep_b", 0.0, 30.0)
        # a5: a clip of another passage linked to the read.
        self._clip("clip-unrelated", "ep_d", 300.0, 320.0)
        links = {"ad-a1": "clip-good", "ad-a2": "clip-version", "ad-a3": "clip-broad",
                 "ad-a5": "clip-unrelated"}
        for ev, ep, start, end, brand, stored in ADS:
            stored = self.stored(stored)
            full = " ".join(s.get("text", "") for s in transcripts(self.variant)[ep]
                            if s.get("start", float("inf")) < end
                            and s.get("end", float("-inf")) > start)
            con.execute("INSERT INTO ad_reads (evidence_id, record_key, episode_id, kind,"
                        " evidence_revision, start_s, end_s, display_start_s, display_end_s,"
                        " clip_id, clip_offset_s, brand_suggested, quote, search_folded)"
                        " VALUES (?, ?, ?, 'rule', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (ev, ev, ep, start, end, start, end, links.get(ev),
                         5.0 if links.get(ev) else None, brand, full,
                         customer.normalize_token(full + " " + stored)))
        if self.untranscribed:
            ev, ep, start, end, brand, stored = UNTRANSCRIBED
            con.execute("INSERT INTO ad_reads (evidence_id, record_key, episode_id, kind,"
                        " evidence_revision, start_s, end_s, display_start_s, display_end_s,"
                        " brand_suggested, quote, search_folded)"
                        " VALUES (?, ?, ?, 'rule', 1, ?, ?, ?, ?, ?, '', ?)",
                        (ev, ev, ep, start, end, start, end, brand,
                         customer.normalize_token(stored)))
        if _ad.fts_present(con):
            con.execute(f"INSERT INTO {_ad.FTS} ({_ad.FTS}) VALUES ('rebuild')")
        _ad._set_meta(con, _ad.BUILT, "1")

    # -- helpers --------------------------------------------------------
    def stored(self, text):
        """A stored excerpt holds the hidden words of its own fixture."""
        if not self.variant:
            return text
        return text.replace("qzlate", "qzother").replace("qzoversized", "qzoverother")

    def add_ad(self, ev, ep, start, end, brand, stored):
        """A new ad read after setup (a producer write that publishes)."""
        con, now = self.con, db.utcnow()
        sv = _add_version(con, f"detector:{ev}", {"origin": "synthetic", "excerpt": stored})
        self.sv[ev] = sv
        con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                    " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                    " transcript_version_id, source_record_key, assoc_revision, record_kind)"
                    " VALUES (?, 'detector', ?, ?, ?, ?, ?, '{}', 1, ?, ?, ?, 0, 'candidate')",
                    (ev, sv, ep, start, end, stored, now, self.tv[ep], ev))
        con.execute("INSERT INTO reviews (evidence_id, judgment, revision, updated_at)"
                    " VALUES (?, 'finding', 1, ?)", (ev, now))
        con.execute("INSERT INTO brand_assocs (evidence_id, brand, basis, origin, revision,"
                    " updated_at) VALUES (?, ?, 'spoken', 'test', 1, ?)", (ev, brand, now))
        full = " ".join(s.get("text", "") for s in transcripts(self.variant)[ep]
                        if s.get("start", float("inf")) < end and s.get("end", float("-inf")) > start)
        con.execute("INSERT INTO ad_reads (evidence_id, record_key, episode_id, kind,"
                    " evidence_revision, start_s, end_s, display_start_s, display_end_s,"
                    " brand_suggested, quote, search_folded)"
                    " VALUES (?, ?, ?, 'rule', 1, ?, ?, ?, ?, ?, ?, ?)",
                    (ev, ev, ep, start, end, start, end, brand, full,
                     customer.normalize_token(full + " " + stored)))
        if _ad.fts_present(con):
            con.execute(f"INSERT INTO {_ad.FTS} ({_ad.FTS}) VALUES ('rebuild')")
        keys = [f"detector:{ev}"]
        for key in keys:
            for category in ("metadata", "excerpt", "transcript", "audio"):
                row = con.execute("SELECT agent_disclosure, export_eligible FROM"
                                  " source_content_policy WHERE source_key=? AND"
                                  " content_category=?", (f"detector:ad-a1", category)).fetchone()
                con.execute("INSERT OR REPLACE INTO source_content_policy (workspace_id,"
                            " source_key, content_category, agent_disclosure, export_eligible,"
                            " updated_at) VALUES ('local', ?, ?, ?, ?, ?)",
                            (key, category, row[0], row[1], now))
        citations.publish_source_state(con, kind="evidence", record_id=ev, actor="fixture")

    def ref(self, kind, record_id):
        return citations.current_source_ref(self.con, kind, record_id)

    def write(self, sql, args=()):
        self.con.execute(sql, args)

    def plan(self, plan):
        set_plan(self.con, plan)

    def row(self, key, category, view, export=None):
        set_row(self.con, key, category, view, export)


def member(**over):
    fields = dict(principal_id="owner-1", principal_kind="owner", workspace_id="local",
                  credential_kind="browser_session", scopes=())
    fields.update(over)
    return access.AccessContext.create(**fields)


def token(scopes, **over):
    fields = dict(principal_id="owner-1", principal_kind="owner", workspace_id="local",
                  credential_kind="reader_bearer", scopes=tuple(scopes))
    fields.update(over)
    return access.AccessContext.create(**fields)


def strings(value, path="$"):
    """Every (path, string) in a JSON-like value, keys included."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield f"{path}.<key>", str(key)
            yield from strings(inner, f"{path}.{key}")
    elif isinstance(value, (list, tuple, set)):
        for index, inner in enumerate(value):
            yield from strings(inner, f"{path}[{index}]")
    elif isinstance(value, bytes):
        yield path, value.decode("utf-8", "replace")
    elif isinstance(value, str):
        yield path, value


def leaked(value, markers=MARKERS + VARIANT_MARKERS) -> list:
    """(path, marker) of every marker found anywhere in value."""
    return [(path, m) for path, text in strings(value) for m in markers if m in text.lower()]


class FixtureSanityTest(unittest.TestCase):
    def test_the_fixture_holds_every_marker_in_its_source_rows(self):
        ws = Workspace("A")
        self.addCleanup(ws.close)
        payloads = " ".join(r[0] for r in ws.con.execute("SELECT payload FROM source_versions"))
        excerpts = " ".join(r[0] for r in ws.con.execute("SELECT excerpt FROM evidence"))
        for marker in MARKERS:
            self.assertIn(marker, (payloads + excerpts).lower(), marker)
        joined = " ".join(s["text"] for s in _late_segments(False))
        self.assertGreater(joined.index("qzlate"), 600)
        self.assertEqual(len(safe_text("ad-a5")) <= 600, True)
        policy = {(r[0], r[1]): r[2] for r in ws.con.execute(
            "SELECT source_key, content_category, agent_disclosure FROM source_content_policy")}
        self.assertEqual(policy[("transcript:ep_a", "transcript")], "deny")
        self.assertEqual(policy[("detector:ad-a1", "excerpt")], "allow")


if __name__ == "__main__":
    unittest.main()
