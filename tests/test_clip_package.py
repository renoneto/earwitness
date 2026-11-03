"""Demo audio clips, build item 6 (Mini cutter): design
docs/plans/2026-10-22-demo-audio-clips-design.md sections 3.1 and 3.2.

`plan` picks one clip per (feed, brand) pair: the newest usable
high-confidence named read of at most 90 s, falling back to the next one,
and reports pairs without a usable source by reason. `cut` copies the Mini
clip's MPEG-1 Layer III frames (64 kbps, 44.1 kHz, mono; design revision 9:
no re-encode) and writes frames only (no tag, no Xing frame) of at most 99 s.
`plan` writes nothing but its --out file. Fixtures only: no Mini, no corpus
copy.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "clip_package.py"
FFMPEG = shutil.which("ffmpeg")

from scripts import clip_package as cp  # noqa: E402

AUDIO = "a" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Corpus:
    """A fixture corpus: episodes, named reads, transcripts and Mini clips."""

    def __init__(self, root: Path):
        self.root = root
        self.episodes: list[dict] = []
        self.reads: list[dict] = []

    def episode(self, ep, *, feed="https://f.example/a.xml", pub="2026-10-01",
                title="Quiet Hours", audio=AUDIO, transcript_audio=None):
        self.episodes.append({"ep_id": ep, "feed_url": feed, "pub_date": pub,
                              "show_title": title, "audio_sha256": audio})
        t = self.root / "transcripts"
        t.mkdir(parents=True, exist_ok=True)
        (t / f"{ep}.json").write_text(json.dumps(
            {"ep_id": ep, "provenance": {"audio_sha256": transcript_audio or audio}, "segments": []}))
        return ep

    def clip(self, ep, start_s, end_s, data=b"mini-clip-bytes", *, index_sha=None, audio=AUDIO):
        d = self.root / "clips" / ep
        d.mkdir(parents=True, exist_ok=True)
        name = f"{round(start_s * 1000)}-{round(end_s * 1000)}.mp3"
        (d / name).write_bytes(data)
        idx = d / "index.json"
        doc = json.loads(idx.read_text()) if idx.exists() else {
            "schema_version": 2, "episode_id": ep, "status": "ok", "audio_sha256": audio, "clips": []}
        doc["clips"].append({"file": name, "start_s": start_s, "end_s": end_s,
                             "start_ms": round(start_s * 1000), "end_ms": round(end_s * 1000),
                             "sha256": index_sha or sha(data), "bytes": len(data)})
        idx.write_text(json.dumps(doc))
        return d / name

    def read(self, ep, start_s, end_s, brand="Northwind", *, spoken=None, confidence="high"):
        cid = f"{ep}.named.{round(start_s * 1000)}-{round(end_s * 1000)}"
        self.reads.append({"candidate_id": cid, "ep_id": ep, "start_s": start_s, "end_s": end_s,
                           "brand": brand, "brand_spoken": spoken, "confidence": confidence})
        return cid

    def write(self):
        (self.root / "named.json").write_text(json.dumps(self.reads))
        (self.root / "episodes.json").write_text(json.dumps(self.episodes))

    def plan(self) -> dict:
        self.write()
        out = self.root / "plan.json"
        cp.main(["plan", "--corpus", str(self.root), "--named", str(self.root / "named.json"),
                 "--episodes", str(self.root / "episodes.json"), "--out", str(out)])
        return json.loads(out.read_text())


class PlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.c = Corpus(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_pairs_use_feed_identity_and_spoken_brand_never_titles(self):
        c = self.c
        # Same feed, two show titles: one pair. Same title, another feed: its own pair.
        for ep, feed, title in [("ep_a", "https://f.example/1.xml", "Quiet Hours"),
                                ("ep_b", "https://f.example/1.xml", "Quiet Hours (Rebrand)"),
                                ("ep_c", "https://f.example/2.xml", "Quiet Hours"),
                                ("ep_d", None, "Quiet Hours")]:
            c.episode(ep, feed=feed, title=title)
            c.clip(ep, 0, 200)
        c.read("ep_a", 20, 60, "Northwind")
        c.read("ep_b", 20, 60, "nw-typo", spoken="  NORTHWIND ")
        c.read("ep_c", 20, 60, "Northwind")
        c.read("ep_d", 20, 60, "Northwind")
        c.read("ep_a", 100, 130, "Acme")
        plan = c.plan()
        pairs = sorted(x["pair"] for x in plan["clips"])
        self.assertEqual(pairs, ["ep:ep_d|northwind", "feed:https://f.example/1.xml|acme",
                                 "feed:https://f.example/1.xml|northwind",
                                 "feed:https://f.example/2.xml|northwind"])
        self.assertEqual(plan["counts"], {"pairs": 4, "with_clip": 4, "no_usable_source": 0, "by_reason": {}})

    def test_newest_usable_read_wins_and_falls_back_to_the_next(self):
        c = self.c
        for ep, pub in [("ep_old", "2026-10-01"), ("ep_mid", "2026-10-08"), ("ep_new", "2026-10-09")]:
            c.episode(ep, pub=pub)
        c.clip("ep_old", 0, 200)
        c.clip("ep_mid", 0, 200)
        c.clip("ep_new", 0, 200, index_sha="0" * 64)  # newest: its clip fails its digest
        c.read("ep_old", 30, 60)
        mid = c.read("ep_mid", 30, 60)
        c.read("ep_new", 30, 60)
        [x] = self.c.plan()["clips"]
        self.assertEqual(x["candidate_id"], mid)
        self.assertEqual((x["start_s"], x["end_s"]), (25, 63), "5 s before the read, 3 s after")
        self.assertEqual(x["source"], "clips/ep_mid/0-200000.mp3")
        self.assertEqual(x["source_offset_s"], 25)

    def test_newest_usable_read_wins_when_every_read_is_usable(self):
        c = self.c
        for ep, pub in [("ep_old", "2026-10-01"), ("ep_new", "2026-10-09")]:
            c.episode(ep, pub=pub)
            c.clip(ep, 0, 200)
        c.read("ep_old", 30, 60)
        c.read("ep_new", 140, 150)
        new2 = c.read("ep_new", 150, 160)  # same day and episode: the later start wins
        [x] = c.plan()["clips"]
        self.assertEqual(x["candidate_id"], new2)

    def test_reads_over_90_s_are_span_over_90_and_90_s_is_eligible(self):
        c = self.c
        c.episode("ep_a", feed="https://f.example/1.xml")
        c.episode("ep_b", feed="https://f.example/2.xml")
        c.clip("ep_a", 0, 300)
        c.clip("ep_b", 0, 300)
        c.read("ep_a", 10, 101)          # 91 s
        ok = c.read("ep_b", 10, 100)     # 90 s
        c.read("ep_b", 120, 130, "Low", confidence="medium")  # not high: no pair at all
        plan = c.plan()
        self.assertEqual([x["candidate_id"] for x in plan["clips"]], [ok])
        self.assertEqual(plan["counts"], {"pairs": 2, "with_clip": 1, "no_usable_source": 1,
                                          "by_reason": {"span_over_90": 1}})
        self.assertEqual(plan["no_source"], [{"pair": "feed:https://f.example/1.xml|northwind",
                                              "reason": "span_over_90"}])

    def test_no_usable_source_reasons(self):
        c = self.c
        for ep in ("ep_digest", "ep_short", "ep_audio", "ep_none"):
            c.episode(ep, feed=f"https://f.example/{ep}.xml",
                      transcript_audio=("b" * 64 if ep == "ep_audio" else None))
            c.read(ep, 30, 60)
        c.clip("ep_digest", 0, 200, index_sha="0" * 64)
        c.clip("ep_short", 0, 62.9)   # the window ends at 63: not contained
        c.clip("ep_audio", 0, 200)
        plan = c.plan()
        self.assertEqual(plan["clips"], [])
        got = {x["pair"].split("|")[0]: x["reason"] for x in plan["no_source"]}
        self.assertEqual(got, {"feed:https://f.example/ep_digest.xml": "index_digest_mismatch",
                               "feed:https://f.example/ep_short.xml": "no_containing_clip",
                               "feed:https://f.example/ep_audio.xml": "audio_mismatch",
                               "feed:https://f.example/ep_none.xml": "no_containing_clip"})
        self.assertEqual(plan["counts"]["by_reason"], {"index_digest_mismatch": 1, "no_containing_clip": 2,
                                                       "audio_mismatch": 1})

    def test_plan_writes_only_its_out_file(self):
        c = self.c
        c.episode("ep_a")
        c.clip("ep_a", 0, 200)
        c.read("ep_a", 30, 60)
        c.write()

        def tree():
            return {str(p.relative_to(c.root)): (p.stat().st_size, p.stat().st_mtime_ns, sha(p.read_bytes()))
                    for p in sorted(c.root.rglob("*")) if p.is_file()}
        before = tree()
        out = c.root / "out" / "plan.json"
        out.parent.mkdir()
        r = subprocess.run([sys.executable, str(SCRIPT), "plan", "--corpus", str(c.root),
                            "--named", str(c.root / "named.json"), "--episodes", str(c.root / "episodes.json"),
                            "--out", str(out)], cwd=c.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = tree()
        self.assertEqual(set(after) - set(before), {"out/plan.json"})
        self.assertEqual({k: after[k] for k in before}, before, "nothing else changed")


def _source_mp3(path: Path, seconds: float, *, rate="64k", ar=44100, ac=1):
    """A Mini-like clip: 64 kbps, 44.1 kHz mono MP3 with ffmpeg's default ID3v2
    tag and Xing/Info frame. rate, ar and ac make an off-format source."""
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-ar", str(ar),
                    "-ac", str(ac), "-c:a", "libmp3lame", "-b:a", rate, "-metadata", "title=t", str(path)], check=True)


MINI_FORMAT = {("MPEG-1", "Layer III", 64, 44100, "mono")}


def _source_frames(source: bytes) -> bytes:
    """The audio frames of a _source_mp3 file, after asserting that it starts
    with an ID3v2 tag and then a Xing/Info frame."""
    assert source.startswith(b"ID3"), "the fixture source carries an ID3v2 tag"
    at = 10 + int.from_bytes(bytes(b & 0x7F for b in source[6:10]), "big")
    first = cp._header(source, at)
    assert first and any(t in source[at:at + first[3]] for t in (b"Xing", b"Info")), "and a Xing/Info frame"
    return source[at + first[3]:]


def _frame_formats(data: bytes) -> set:
    """Every frame's (version, layer, kbps, Hz, mono or not), walked
    independently of the cutter's own parser. Fails on a non-frame byte."""
    kbps1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    kbps2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    seen, pos = set(), 0
    while pos < len(data):
        b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
        assert data[pos] == 0xFF and b1 >> 5 == 7 and (b1 >> 1) & 3 == 1, f"not a Layer III frame at {pos}"
        v1 = (b1 >> 3) & 3 == 3
        kbps = (kbps1 if v1 else kbps2)[b2 >> 4]
        hz = ((44100, 48000, 32000) if v1 else (22050, 24000, 16000))[(b2 >> 2) & 3]
        seen.add(("MPEG-1" if v1 else "MPEG-2", "Layer III", kbps, hz, "mono" if b3 >> 6 == 3 else "stereo"))
        pos += (144 if v1 else 72) * kbps * 1000 // hz + ((b2 >> 1) & 1)
    assert pos == len(data), "the last frame is cut off"
    return seen


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class CutTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.c = Corpus(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _cut(self, plan_path: Path, *extra) -> dict:
        out = self.root / "cut"
        cp.main(["cut", "--plan", str(plan_path), "--out", str(out), *extra])
        return json.loads((out / "cut.json").read_text())

    def test_cut_copies_the_mini_frames_with_no_tag_and_no_xing(self):
        c = self.c
        c.episode("ep_a")
        src = self.root / "src.mp3"
        _source_mp3(src, 120)
        source = src.read_bytes()
        self.assertEqual(_frame_formats(_source_frames(source)), MINI_FORMAT)
        c.clip("ep_a", 0, 120, source)
        c.read("ep_a", 10, 100)     # 90 s read: window 5..103, 98 s
        c.plan()
        doc = self._cut(self.root / "plan.json")
        self.assertEqual(doc["refused"], [])
        [x] = doc["clips"]
        out = self.root / "cut" / x["file"]
        data = out.read_bytes()
        self.assertEqual(x["file"], "clip-ep_a-5000-103000.mp3")
        self.assertEqual((x["bytes"], x["sha256"]), (len(data), sha(data)))
        self.assertLessEqual(x["bytes"], 819_200)
        self.assertEqual(_frame_formats(data), MINI_FORMAT, "every frame is 64 kbps, 44.1 kHz, mono")
        self.assertIn(data, source, "the source's own frames, copied: no re-encode")
        self.assertFalse(data.startswith(b"ID3"), "no ID3v2 tag")
        self.assertNotEqual(data[-128:-125], b"TAG", "no ID3v1 tag")
        for marker in (b"Xing", b"Info", b"VBRI", b"LAME", b"Lavf", b"Lavc"):
            self.assertNotIn(marker, data[:2048], marker)
        secs = cp.mp3_seconds(data)
        self.assertLessEqual(secs, 99.0)
        self.assertLess(abs(secs - 98.0), 0.5)
        self.assertAlmostEqual(x["seconds"], secs, places=3)
        probe = shutil.which("ffprobe")
        if probe:
            got = float(subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                        str(out)], capture_output=True, text=True, check=True).stdout)
            self.assertLess(abs(got - secs), 0.1, "ffprobe agrees with the frame count")

    def test_frame_walk_refuses_tags_xing_and_junk(self):
        src = self.root / "src.mp3"
        _source_mp3(src, 5)
        tagged = src.read_bytes()
        self.assertIsNone(cp.mp3_seconds(tagged), "ID3v2 tag and Xing frame")
        self.assertIsNone(cp.mp3_seconds(b""))
        self.assertIsNone(cp.mp3_seconds(b"\x00" * 1000))

    def test_cut_refuses_a_window_over_99_s_and_writes_no_file(self):
        c = self.c
        c.episode("ep_a")
        src = self.root / "src.mp3"
        _source_mp3(src, 130)
        c.clip("ep_a", 0, 130, src.read_bytes())
        c.read("ep_a", 10, 100)
        plan = c.plan()
        plan["clips"][0].update(start_s=5, end_s=105)   # a hand-edited 100 s window
        p = self.root / "plan-edited.json"
        p.write_text(json.dumps(plan))
        doc = self._cut(p)
        self.assertEqual(doc["clips"], [])
        self.assertEqual([r["reason"] for r in doc["refused"]], ["clip_duration"])
        self.assertEqual(sorted(os.listdir(self.root / "cut")), ["cut.json"])

    def test_a_source_off_the_mini_format_is_refused(self):
        # Revision 9: the cut copies frames, so an off-format source gives an
        # off-format clip. Each is refused on its own and leaves no file.
        c = self.c
        sources = {"ep_32k": dict(rate="32k"), "ep_22k": dict(ar=22050), "ep_stereo": dict(ac=2)}
        for ep, fmt in sources.items():
            src = self.root / f"{ep}.mp3"
            _source_mp3(src, 60, **fmt)
            self.assertNotEqual(_frame_formats(_source_frames(src.read_bytes())), MINI_FORMAT, ep)
            c.episode(ep, feed=f"https://f.example/{ep}.xml")
            c.clip(ep, 0, 60, src.read_bytes())
            c.read(ep, 10, 40)
        self.assertEqual(len(c.plan()["clips"]), 3)
        doc = self._cut(self.root / "plan.json")
        self.assertEqual(doc["clips"], [])
        self.assertEqual(sorted((r["episode_id"], r["reason"]) for r in doc["refused"]),
                         [(ep, "frame_format_mismatch") for ep in sorted(sources)])
        self.assertEqual(sorted(os.listdir(self.root / "cut")), ["cut.json"])

    def test_cut_limit_and_rerun_reuse(self):
        c = self.c
        src = self.root / "src.mp3"
        _source_mp3(src, 60)
        for i in range(3):
            c.episode(f"ep_{i}", feed=f"https://f.example/{i}.xml")
            c.clip(f"ep_{i}", 0, 60, src.read_bytes())
            c.read(f"ep_{i}", 10, 20)
        c.plan()
        first = self._cut(self.root / "plan.json", "--limit", "2")
        self.assertEqual(len(first["clips"]), 2)
        again = self._cut(self.root / "plan.json", "--limit", "2")
        self.assertEqual(again["clips"], first["clips"], "a rerun gives the same files")


def _frames_mp3(path: Path, seconds: float, *, rate="64k", ar=44100, ac=1, xing=False, freq=440):
    """A tag-free MPEG stream (frames only unless xing); Mini format by default."""
    cmd = [FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"sine=frequency={freq}:sample_rate=44100:duration={seconds}", "-map_metadata", "-1",
           "-id3v2_version", "0", "-write_id3v1", "0", "-ac", str(ac), "-ar", str(ar), "-c:a", "libmp3lame",
           "-b:a", rate] + ([] if xing else ["-write_xing", "0"]) + ["-f", "mp3", str(path)]
    subprocess.run(cmd, check=True)
    return path.read_bytes()


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RefusalTest(unittest.TestCase):
    """CLIPUI-R1-03 and R1-05: each refusal rule is pinned on its own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_tag_free_stream_with_a_xing_frame_is_refused(self):
        clean = _frames_mp3(self.root / "a.mp3", 3)
        self.assertIsNotNone(cp.mp3_seconds(clean))
        xing = _frames_mp3(self.root / "b.mp3", 3, xing=True)
        self.assertFalse(xing.startswith(b"ID3"))
        self.assertEqual(xing[:2], clean[:2], "starts with a frame header, not a tag")
        self.assertIsNone(cp.mp3_seconds(xing))

    def test_a_truncated_frame_is_refused_first_or_last(self):
        clean = _frames_mp3(self.root / "a.mp3", 3)
        self.assertIsNone(cp.mp3_seconds(clean[:10]), "first frame cut off")
        self.assertIsNone(cp.mp3_seconds(clean[:-10]), "last frame cut off")

    def test_mpeg_2_5_and_a_sample_rate_change_are_refused(self):
        self.assertIsNone(cp.mp3_seconds(_frames_mp3(self.root / "a.mp3", 3, rate="16k", ar=11025)), "MPEG-2.5")
        mixed = _frames_mp3(self.root / "b.mp3", 2) + _frames_mp3(self.root / "c.mp3", 2, ar=24000)
        self.assertIsNone(cp.mp3_seconds(mixed))

    def test_check_holds_the_window_tolerance_and_the_byte_cap(self):
        data = _frames_mp3(self.root / "a.mp3", 10)
        secs = cp.mp3_seconds(data)
        self.assertEqual(cp._check(data, secs + 0.4)[1], None)
        self.assertEqual(cp._check(data, secs - 0.4)[1], None)
        self.assertEqual(cp._check(data, secs + 0.6)[1], "clip_duration")
        self.assertEqual(cp._check(data, secs - 0.6)[1], "clip_duration")
        self.assertEqual(cp._check(data, secs + 1)[1], "clip_duration")

    def test_check_refuses_any_frame_off_the_mini_format(self):
        mini = _frames_mp3(self.root / "a.mp3", 4)
        self.assertEqual(_frame_formats(mini), MINI_FORMAT)
        self.assertEqual(cp._check(mini, cp.mp3_seconds(mini))[1], None)
        off = {"old 24 kbps cut": dict(rate="24k", ar=22050), "32 kbps": dict(rate="32k"),
               "22.05 kHz": dict(ar=22050), "stereo": dict(ac=2)}
        for label, fmt in off.items():
            data = _frames_mp3(self.root / "b.mp3", 4, **fmt)
            self.assertNotEqual(_frame_formats(data), MINI_FORMAT, label)
            self.assertIsNotNone(cp.mp3_seconds(data), label)
            self.assertEqual(cp._check(data, cp.mp3_seconds(data))[1], "frame_format_mismatch", label)
        # Every frame counts, not only the first: Mini frames, then 32 kbps
        # or stereo frames at the same 44.1 kHz (the frame walk accepts both).
        for label, fmt in (("32 kbps tail", dict(rate="32k")), ("stereo tail", dict(ac=2))):
            mixed = mini + _frames_mp3(self.root / "c.mp3", 1, **fmt)
            self.assertIsNotNone(cp.mp3_seconds(mixed), label)
            self.assertEqual(cp._check(mixed, cp.mp3_seconds(mixed))[1], "frame_format_mismatch", label)

    def test_the_byte_cap_is_819_200(self):
        # Revision 9: 64 kbps clips. 70 s is about 560,000 bytes: over the old
        # 524,288 cap, under the new one.
        mid = _frames_mp3(self.root / "a.mp3", 70, rate="64k", ar=44100)
        self.assertGreater(len(mid), 524_288)
        self.assertLess(len(mid), 819_200)
        self.assertEqual(cp._check(mid, cp.mp3_seconds(mid)), (cp.mp3_seconds(mid), None))
        # 99 s at 64 kbps is about 792,000 bytes, so the duration rule refuses
        # first; lift it to pin the byte cap on its own, one frame either side.
        long = _frames_mp3(self.root / "b.mp3", 105, rate="64k", ar=44100)
        self.assertEqual(cp._check(long, cp.mp3_seconds(long))[1], "clip_duration")
        ends, pos = [], 0
        while pos < len(long):
            pos += cp._header(long, pos)[3]
            ends.append(pos)
        fit = max(e for e in ends if e <= 819_200)
        over = min(e for e in ends if e > 819_200)
        from unittest import mock
        with mock.patch.object(cp, "MAX_CLIP_S", 200):
            for n, want in ((fit, None), (over, "clip_too_large"), (len(long), "clip_too_large")):
                part = long[:n]
                self.assertEqual(cp._check(part, cp.mp3_seconds(part))[1], want, n)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RecutTest(unittest.TestCase):
    """CLIPUI-R1-01 and R1-03: a rerun keeps only what it can prove, and
    cut.json describes exactly the clip files present."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.c = Corpus(self.root)
        self.src = self.root / "src.mp3"
        _source_mp3(self.src, 60)
        for i in range(3):
            self.c.episode(f"ep_{i}", feed=f"https://f.example/{i}.xml")
            self.c.clip(f"ep_{i}", 0, 60, self.src.read_bytes())
            self.c.read(f"ep_{i}", 10, 20)
        self.c.plan()
        self.out = self.root / "cut"

    def tearDown(self):
        self._tmp.cleanup()

    def cut(self, *extra):
        cp.main(["cut", "--plan", str(self.root / "plan.json"), "--out", str(self.out), *extra])
        return json.loads((self.out / "cut.json").read_text())

    def assert_describes_folder(self, doc):
        files = sorted(p.name for p in self.out.iterdir() if p.name != "cut.json")
        self.assertEqual(files, sorted(x["file"] for x in doc["clips"]))
        for x in doc["clips"]:
            data = (self.out / x["file"]).read_bytes()
            self.assertEqual((x["bytes"], x["sha256"]), (len(data), sha(data)))

    def test_a_rerun_over_an_old_24k_cut_recuts_every_clip(self):
        # An earlier cutter's 24 kbps, 22,050 Hz files, listed with their own
        # digests: the old (rate, 22050) reuse rule is gone, and the frame
        # check refuses them, so every clip is cut again from the source.
        first = self.cut()
        old = json.loads((self.out / "cut.json").read_text())
        old["rate"] = "24k"
        for x in old["clips"]:
            data = _frames_mp3(self.root / "old.mp3", x["seconds"], rate="24k", ar=22050)
            self.assertIsNotNone(cp.mp3_seconds(data), "frames only: only the frame check can refuse it")
            (self.out / x["file"]).write_bytes(data)
            x.update(bytes=len(data), sha256=sha(data))
        (self.out / "cut.json").write_text(json.dumps(old))
        again = self.cut()
        self.assertEqual(again["clips"], first["clips"])
        for x in again["clips"]:
            self.assertEqual(_frame_formats((self.out / x["file"]).read_bytes()), MINI_FORMAT, x["file"])
        self.assert_describes_folder(again)

    def test_a_replan_from_another_source_recuts_the_clip(self):
        first = self.cut()
        subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=880:sample_rate=44100:duration=60", "-ac", "1", "-c:a", "libmp3lame",
                        "-b:a", "64k", str(self.root / "other.mp3")], check=True)
        new = (self.root / "other.mp3").read_bytes()
        (self.root / "clips" / "ep_0" / "0-60000.mp3").write_bytes(new)
        idx = self.root / "clips" / "ep_0" / "index.json"
        doc = json.loads(idx.read_text())
        doc["clips"][0]["sha256"] = sha(new)
        idx.write_text(json.dumps(doc))
        self.c.plan()
        again = self.cut()
        a = {x["episode_id"]: x for x in first["clips"]}
        b = {x["episode_id"]: x for x in again["clips"]}
        self.assertEqual(b["ep_0"]["source_sha256"], sha(new))
        self.assertNotEqual(b["ep_0"]["sha256"], a["ep_0"]["sha256"], "cut again from the new source")
        self.assertEqual(b["ep_1"]["sha256"], a["ep_1"]["sha256"])
        self.assert_describes_folder(again)

    def test_a_replan_at_another_source_offset_recuts_the_clip(self):
        # CLIPUI-R2-01 (reviewer's C-off): same Mini bytes, index start moved 0 -> 2 s,
        # so the offset goes 5.0 -> 3.0 s; the old file must not be kept.
        first = self.cut()
        idx = self.root / "clips" / "ep_0" / "index.json"
        doc = json.loads(idx.read_text())
        doc["clips"][0].update(start_s=2, end_s=62, start_ms=2000, end_ms=62000)
        idx.write_text(json.dumps(doc))
        plan = self.c.plan()
        self.assertEqual({x["episode_id"]: x["source_offset_s"] for x in plan["clips"]}["ep_0"], 3)
        again = self.cut()
        a = {x["episode_id"]: x for x in first["clips"]}
        b = {x["episode_id"]: x for x in again["clips"]}
        self.assertNotEqual(b["ep_0"]["sha256"], a["ep_0"]["sha256"], "cut again at the new offset")
        self.assertEqual((b["ep_0"]["source"], b["ep_0"]["source_offset_s"]), ("clips/ep_0/0-60000.mp3", 3))
        self.assertEqual(a["ep_0"]["source_offset_s"], 5)
        self.assertEqual(b["ep_1"]["sha256"], a["ep_1"]["sha256"])
        self.assert_describes_folder(again)

    def test_a_kept_clip_replaced_by_another_valid_clip_is_cut_again(self):
        # CLIPUI-R2-02: same rate and length, other audio: only the file digest tells.
        first = {x["episode_id"]: x for x in self.cut()["clips"]}
        other = _frames_mp3(self.root / "other.mp3", 18, freq=880)
        self.assertEqual(cp._check(other, 18)[1], None)
        (self.out / first["ep_0"]["file"]).write_bytes(other)
        again = {x["episode_id"]: x for x in self.cut()["clips"]}
        self.assertEqual(again["ep_0"]["sha256"], first["ep_0"]["sha256"])
        self.assertNotEqual(again["ep_0"]["sha256"], sha(other))

    def test_an_identical_rerun_reuses_every_clip_without_ffmpeg(self):
        # CLIPUI-R3-01: reuse is real: no ffmpeg call, same inode and mtime.
        from unittest import mock
        first = self.cut()
        stats = {x["file"]: (self.out / x["file"]).stat() for x in first["clips"]}
        with mock.patch.object(cp, "_ffmpeg", wraps=cp._ffmpeg) as ff:
            again = self.cut()
        self.assertEqual(ff.call_count, 0)
        self.assertEqual(again["clips"], first["clips"])
        for name, st in stats.items():
            now = (self.out / name).stat()
            self.assertEqual((now.st_ino, now.st_mtime_ns), (st.st_ino, st.st_mtime_ns), name)

    def test_a_smaller_rerun_removes_clips_it_no_longer_lists(self):
        self.cut()
        doc = self.cut("--limit", "1")
        self.assertEqual(len(doc["clips"]), 1)
        self.assert_describes_folder(doc)

    def test_a_source_changed_after_plan_is_refused(self):
        (self.root / "clips" / "ep_1" / "0-60000.mp3").write_bytes(b"changed after plan")
        doc = self.cut()
        self.assertEqual([(r["episode_id"], r["reason"]) for r in doc["refused"]], [("ep_1", "source_digest_mismatch")])
        self.assert_describes_folder(doc)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class SharedWindowTest(unittest.TestCase):
    """CLIPUI-R4-01: two pairs that win one window share one file and one row.
    CLIPUI-R4-02: an episode ID the host name parser refuses is refused here."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.c = Corpus(self.root)
        src = self.root / "src.mp3"
        _source_mp3(src, 60)
        self.c.episode("ep_1")
        self.c.clip("ep_1", 0, 60, src.read_bytes())
        self.acme = self.c.read("ep_1", 10, 40, "Acme")
        self.c.reads[-1]["candidate_id"] = self.acme = self.acme + ".acme"
        self.nw = self.c.read("ep_1", 10, 40, "Northwind")
        self.c.reads[-1]["candidate_id"] = self.nw = self.nw + ".northwind"
        self.plan = self.c.plan()
        self.out = self.root / "cut"

    def tearDown(self):
        self._tmp.cleanup()

    def cut(self, plan=None, *extra):
        path = self.root / "plan.json"
        if plan is not None:
            path = self.root / "plan-edited.json"
            path.write_text(json.dumps(plan))
        cp.main(["cut", "--plan", str(path), "--out", str(self.out), *extra])
        return json.loads((self.out / "cut.json").read_text())

    def test_two_pairs_over_one_window_give_one_file_and_one_row(self):
        self.assertEqual([cp.clip_file(x) for x in self.plan["clips"]],
                         ["clip-ep_1-5000-43000.mp3"] * 2, "the reviewer's probe: two pairs, one window")
        from unittest import mock
        with mock.patch.object(cp, "_ffmpeg", wraps=cp._ffmpeg) as ff:
            doc = self.cut()
        self.assertEqual(ff.call_count, 1, "the shared file is cut once")
        [row] = doc["clips"]
        self.assertEqual(row["file"], "clip-ep_1-5000-43000.mp3")
        self.assertEqual(row["pairs"], ["feed:https://f.example/a.xml|acme", "feed:https://f.example/a.xml|northwind"])
        self.assertEqual(row["candidate_ids"], [self.acme, self.nw])
        self.assertEqual((row["pair"], row["candidate_id"]), ("feed:https://f.example/a.xml|acme", self.acme))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["clip-ep_1-5000-43000.mp3", "cut.json"])

    def test_a_failed_shared_cut_is_one_refused_row_and_no_file(self):
        from unittest import mock
        with mock.patch.object(cp, "_ffmpeg", return_value=False):
            doc = self.cut()
        self.assertEqual(doc["clips"], [])
        [row] = doc["refused"]
        self.assertEqual((row["reason"], row["candidate_ids"]), ("ffmpeg_failed", [self.acme, self.nw]))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["cut.json"])

    def test_limit_counts_files(self):
        plan = json.loads(json.dumps(self.plan))
        other = dict(plan["clips"][0], pair="feed:https://f.example/b.xml|acme", start_s=20, end_s=43,
                     source_offset_s=20, candidate_id="x")
        plan["clips"].append(other)
        # Three plan entries, two files: the first two pairs share one window.
        # --limit 2 keeps both files; counting pairs would keep only the first (R5-02).
        doc = self.cut(plan, "--limit", "2")
        self.assertEqual([r["file"] for r in doc["clips"]],
                         ["clip-ep_1-5000-43000.mp3", "clip-ep_1-20000-43000.mp3"])
        self.assertEqual([len(r["pairs"]) for r in doc["clips"]], [2, 1])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["clip-ep_1-20000-43000.mp3", "clip-ep_1-5000-43000.mp3", "cut.json"])

    def test_an_episode_id_the_host_refuses_is_refused_before_cutting(self):
        from unittest import mock
        for bad in ("ep 1", "ep/1", "e" * 65, ""):
            plan = json.loads(json.dumps(self.plan))
            for x in plan["clips"]:
                x["episode_id"] = bad
            with mock.patch.object(cp, "_ffmpeg", wraps=cp._ffmpeg) as ff:
                doc = self.cut(plan)
            self.assertEqual(ff.call_count, 0, bad)
            self.assertEqual(doc["clips"], [], bad)
            self.assertEqual([r["reason"] for r in doc["refused"]], ["episode_id_invalid"], bad)
            self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["cut.json"], bad)


if __name__ == "__main__":
    unittest.main()
