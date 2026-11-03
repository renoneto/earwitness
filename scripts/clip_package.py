"""Demo audio clips: the Mini cutter (design
docs/plans/2026-10-22-demo-audio-clips-design.md, sections 3.1 and 3.2).

Runs on the Mini, read-only against the corpus.

  plan --corpus C --named NAMED.json --episodes EPISODES.json --out PLAN.json
      One clip per (feed, brand) pair. Pair key: feed:<feed_url> (or
      ep:<ep_id> without one) plus ad_reads.brand_key(brand_spoken or
      brand); titles are never used. Reads: confidence "high", numeric
      start_s < end_s. The winner is the newest usable read of the pair
      (pub_date, ep_id, start_s, descending), falling back to the next one.
      Usable: span at most 90 s; the window [max(0, start - 5), end + 3]
      lies inside one Mini clip (C/clips/<ep>/index.json); that clip hashes
      to its index sha256; the index audio_sha256 equals the transcript's
      provenance.audio_sha256 (C/transcripts/<ep>.json). A pair with no
      usable read reports the reason of its newest read: span_over_90,
      no_containing_clip, index_digest_mismatch or audio_mismatch.
      Writes only --out.

  cut --plan PLAN.json --out DIR [--limit N]
      Cuts each planned window into DIR/clip-<ep>-<start_ms>-<end_ms>.mp3
      by copying the Mini clip's frames (-c:a copy; design revision 9: no
      decode, resample or re-encode): MPEG audio frames only (no tag, no
      Xing frame). Each output is walked frame by frame; it is refused (and
      removed) when it is not frames only, has a frame that is not MPEG-1
      Layer III, 64 kbps, 44,100 Hz, mono (frame_format_mismatch), is over
      99 s, is more than 0.5 s off its window, or is over 819,200 bytes.
      DIR/cut.json lists every kept clip with its exact bytes, sha256 and
      seconds (the descriptor's max_bytes) and every refusal with its
      reason, plus each clip's source, source offset, source_sha256 and
      audio_sha256. A rerun reuses a file only when the earlier cut.json
      lists it at the same source, source offset, source_sha256 and
      audio_sha256 with the file's own sha256, and it passes every check
      above; otherwise it cuts again. cut deletes every clip-*.mp3 (and
      clip-*.mp3.tmp) in DIR that it does not list, so DIR must hold only
      this cutter's clips. Pairs whose windows
      give one file name share that file: it is cut once and has one row
      listing every pair and candidate_id ("pairs", "candidate_ids"; "pair"
      and "candidate_id" are the first). --limit counts files. An episode ID
      the host name parser refuses is refused (episode_id_invalid).

Standard library plus ffmpeg (cut only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_READ_S = 90.0
LEAD_S = 5.0
TAIL_S = 3.0
MAX_CLIP_S = 99.0
WINDOW_TOLERANCE_S = 0.5
MAX_CLIP_BYTES = 819_200
# The host clip-name parser's episode ID group (design 4.2). Merge note
# (CLIPUI-R4-02): a copy of the literal in lr-clips scripts/clip_name.py
# _NAME (?P<ep>...); once lr-clips and lr-clips-ui merge, import it instead.
EPISODE_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
REASONS = ("span_over_90", "no_containing_clip", "index_digest_mismatch", "audio_mismatch")


# -- MP3 frame walk ------------------------------------------------------------

_BITRATES = {1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
             2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_INFO_TAGS = (b"Xing", b"Info", b"VBRI")


def _header(data: bytes, pos: int):
    """(kbps, sample rate, samples, frame bytes) of the MPEG-1/2 Layer III
    frame header at pos, else None. MPEG-2.5 is refused, like the host."""
    if pos + 4 > len(data):
        return None
    h = int.from_bytes(data[pos:pos + 4], "big")
    version, layer = (h >> 19) & 3, (h >> 17) & 3
    br_i, sr_i, pad = (h >> 12) & 15, (h >> 10) & 3, (h >> 9) & 1
    if (h >> 21) != 0x7FF or version not in (2, 3) or layer != 1 or br_i in (0, 15) or sr_i == 3:
        return None
    sr = _RATES[version][sr_i]
    kbps = _BITRATES[1 if version == 3 else 2][br_i]
    per = 1152 if version == 3 else 576
    return kbps, sr, per, (per // 8) * kbps * 1000 // sr + pad


def mp3_seconds(data: bytes) -> float | None:
    """Seconds of an MPEG-1/2 Layer III stream that is frames only, from byte
    0 to the end; None if any byte is not part of a whole valid frame (a tag,
    junk, a truncated frame, first or last), the sample rate changes, or the
    first frame is a Xing/Info/VBRI frame.

    It accepts a subset of what the host's clip_name.mp3_seconds accepts
    (design 4.3): the host counts a Xing frame as audio; the cutter refuses
    it and a sample-rate change too. Merge note (CLIPUI-R1-05): once lr-clips
    and lr-clips-ui merge, call scripts/clip_name.mp3_seconds here and keep
    only these two extra refusals."""
    pos, samples, rate = 0, 0, None
    while pos < len(data):
        frame = _header(data, pos)
        if frame is None:
            return None
        _, sr, per, size = frame
        if pos + size > len(data):
            return None
        if rate is None:
            if any(t in data[pos:pos + size] for t in _INFO_TAGS):
                return None
            rate = sr
        elif sr != rate:
            return None
        pos += size
        samples += per
    return samples / rate if rate else None


# -- plan ----------------------------------------------------------------------

def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sha_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class _Corpus:
    """Read-only, cached views of the corpus's clip indexes, clip digests and
    transcript audio digests."""

    def __init__(self, root: Path):
        self.root = root
        self._index, self._digest, self._audio = {}, {}, {}

    def index(self, ep: str) -> dict:
        if ep not in self._index:
            doc = _load_json(self.root / "clips" / ep / "index.json")
            self._index[ep] = doc if isinstance(doc, dict) else {}
        return self._index[ep]

    def digest(self, rel: str) -> str | None:
        if rel not in self._digest:
            self._digest[rel] = _sha_file(self.root / rel)
        return self._digest[rel]

    def transcript_audio(self, ep: str) -> str | None:
        if ep not in self._audio:
            doc = _load_json(self.root / "transcripts" / f"{ep}.json")
            prov = doc.get("provenance") if isinstance(doc, dict) else None
            self._audio[ep] = prov.get("audio_sha256") if isinstance(prov, dict) else None
        return self._audio[ep]


def _usable(corpus: _Corpus, read: dict):
    """(clip entry, None) for a usable read, else (None, reason)."""
    ep, start, end = read["ep_id"], read["start_s"], read["end_s"]
    if end - start > MAX_READ_S:
        return None, "span_over_90"
    ws, we = max(0.0, start - LEAD_S), end + TAIL_S
    index = corpus.index(ep)
    containing = []
    for c in index.get("clips") or ():
        cs, ce = _num(c.get("start_s")), _num(c.get("end_s"))
        name = c.get("file")
        if cs is None or ce is None or not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
            continue
        if cs <= ws and ce >= we:
            containing.append((ce - cs, cs, name, c.get("sha256")))
    if not containing:
        return None, "no_containing_clip"
    for _, cs, name, want in sorted(containing):
        rel = f"clips/{ep}/{name}"
        if want and corpus.digest(rel) == want:
            break
    else:
        return None, "index_digest_mismatch"
    audio = index.get("audio_sha256")
    if not audio or audio != corpus.transcript_audio(ep):
        return None, "audio_mismatch"
    return {"episode_id": ep, "candidate_id": read.get("candidate_id"),
            "read_start_s": round(start, 3), "read_end_s": round(end, 3),
            "start_s": round(ws, 3), "end_s": round(we, 3),
            "source": rel, "source_offset_s": round(ws - cs, 3), "source_sha256": want,
            "audio_sha256": audio}, None


def plan(corpus_root: Path, named: list, episodes: list) -> dict:
    from earwitness.app.ad_reads import brand_key
    eps = {}
    for e in episodes:
        if isinstance(e, dict) and (e.get("ep_id") or e.get("id")):
            eps[e.get("ep_id") or e.get("id")] = e
    pairs: dict[str, list] = {}
    for r in named:
        if not isinstance(r, dict) or r.get("confidence") != "high":
            continue
        start, end, ep = _num(r.get("start_s")), _num(r.get("end_s")), r.get("ep_id")
        if start is None or end is None or not start < end or not isinstance(ep, str) or not ep:
            continue
        e = eps.get(ep, {})
        feed = e.get("feed_url")
        key = (f"feed:{feed}" if feed else f"ep:{ep}") + "|" + brand_key(r.get("brand_spoken") or r.get("brand"))
        pub = e.get("pub_date") or r.get("pub_date") or ""
        pairs.setdefault(key, []).append((str(pub), ep, start, {**r, "start_s": start, "end_s": end}, e))
    corpus = _Corpus(corpus_root)
    clips, no_source, by_reason = [], [], {}
    for key in sorted(pairs):
        first_reason = None
        for _, _, _, read, e in sorted(pairs[key], key=lambda t: t[:3], reverse=True):
            entry, reason = _usable(corpus, read)
            if entry:
                clips.append({"pair": key, **entry, "fetch_date": e.get("fetch_date") or read.get("fetch_date")})
                break
            first_reason = first_reason or reason
        else:
            no_source.append({"pair": key, "reason": first_reason})
            by_reason[first_reason] = by_reason.get(first_reason, 0) + 1
    return {"schema": "earwitness/clip-plan/1", "corpus": str(corpus_root.resolve()),
            "params": {"max_read_s": MAX_READ_S, "lead_s": LEAD_S, "tail_s": TAIL_S},
            "counts": {"pairs": len(pairs), "with_clip": len(clips), "no_usable_source": len(no_source),
                       "by_reason": by_reason},
            "clips": clips, "no_source": no_source}


# -- cut -----------------------------------------------------------------------

def clip_file(entry: dict) -> str:
    return f"clip-{entry['episode_id']}-{round(entry['start_s'] * 1000)}-{round(entry['end_s'] * 1000)}.mp3"


def _mini_format(data: bytes) -> bool:
    """True when every frame of a frames-only stream is the Mini's: MPEG-1
    Layer III, 64 kbps, 44,100 Hz, mono (design 3.2, revision 9)."""
    pos = 0
    while pos < len(data):
        kbps, sr, _, size = _header(data, pos)
        h = int.from_bytes(data[pos:pos + 4], "big")
        if (h >> 19) & 3 != 3 or kbps != 64 or sr != 44100 or (h >> 6) & 3 != 3:
            return False
        pos += size
    return True


def _check(data: bytes, window: float):
    """(seconds, None) for a clip the host will admit, else (None, reason)."""
    secs = mp3_seconds(data)
    if secs is None:
        return None, "not_frames_only"
    if not _mini_format(data):
        return None, "frame_format_mismatch"
    if secs > MAX_CLIP_S or abs(secs - window) > WINDOW_TOLERANCE_S:
        return None, "clip_duration"
    if len(data) > MAX_CLIP_BYTES:
        return None, "clip_too_large"
    return secs, None


def _ffmpeg(src: Path, offset: float, dur: float, out: Path) -> bool:
    """Design 3.2: copy the source's frames for the window; no re-encode."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}",
           "-i", str(src), "-map", "0:a:0", "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0",
           "-c:a", "copy", "-write_xing", "0", "-f", "mp3", str(out)]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _reusable(final: Path, prior: dict | None, entry: dict, window: float):
    """The bytes of an earlier cut that provably matches this source and
    window (CLIPUI-R1-01), else None."""
    if not prior or not final.is_file():
        return None
    if any(prior.get(k) != entry.get(k) for k in ("source", "source_offset_s", "source_sha256", "audio_sha256")):
        return None
    data = final.read_bytes()
    if hashlib.sha256(data).hexdigest() != prior.get("sha256"):
        return None
    return None if _check(data, window)[1] else data


def cut(doc: dict, out: Path, limit: int | None = None) -> dict:
    """Cut the planned clips into out. Afterwards out holds exactly the clip
    files cut.json lists: a stale clip (another source or plan, a format the
    checks refuse, or beyond --limit) is cut again or removed."""
    corpus = Path(doc["corpus"])
    out.mkdir(parents=True, exist_ok=True)
    before = _load_json(out / "cut.json")
    prior = {x.get("file"): x for x in before.get("clips") or ()
             if isinstance(x, dict)} if isinstance(before, dict) else {}
    kept, refused = [], []
    # Pairs that win one window share one file (CLIPUI-R4-01): cut it once and
    # write one row listing every pair and candidate_id; --limit counts files.
    groups: dict[str, list] = {}
    for entry in doc.get("clips") or []:
        groups.setdefault(clip_file(entry), []).append(entry)
    names = list(groups)[:limit] if limit is not None else list(groups)
    for name in names:
        entry = groups[name][0]
        window = entry["end_s"] - entry["start_s"]
        base = {k: entry.get(k) for k in ("pair", "episode_id", "candidate_id", "start_s", "end_s", "source",
                                          "source_offset_s", "source_sha256", "audio_sha256", "fetch_date")}
        base["pairs"] = [e.get("pair") for e in groups[name]]
        base["candidate_ids"] = [e.get("candidate_id") for e in groups[name]]
        if not isinstance(entry.get("episode_id"), str) or not EPISODE_ID_RE.fullmatch(entry["episode_id"]):
            refused.append({**base, "reason": "episode_id_invalid"})
            continue
        final, tmp = out / name, out / (name + ".tmp")
        data, reason = _reusable(final, prior.get(name), entry, window), None
        if data is None:
            if _sha_file(corpus / entry["source"]) != entry["source_sha256"]:
                reason = "source_digest_mismatch"
            elif not _ffmpeg(corpus / entry["source"], entry["source_offset_s"], window, tmp):
                reason = "ffmpeg_failed"
            else:
                data = tmp.read_bytes()
                reason = _check(data, window)[1]
                if not reason:
                    os.replace(tmp, final)
            if reason:
                for p in (tmp, final):
                    p.unlink(missing_ok=True)
                refused.append({**base, "reason": reason})
                continue
        secs = _check(data, window)[0]
        kept.append({"file": name, **base, "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest(), "seconds": round(secs, 4)})
    listed = {x["file"] for x in kept}
    for p in out.iterdir():
        if p.name.startswith("clip-") and p.name not in listed and (p.name.endswith(".mp3") or p.name.endswith(".mp3.tmp")):
            p.unlink()
    result = {"schema": "earwitness/clip-cut/1", "clips": kept, "refused": refused}
    _write_json(out / "cut.json", result)
    return result


# -- CLI -----------------------------------------------------------------------

def _write_json(path: Path, doc) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--corpus", required=True, type=Path)
    p.add_argument("--named", required=True, type=Path)
    p.add_argument("--episodes", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    c = sub.add_parser("cut")
    c.add_argument("--plan", required=True, type=Path)
    c.add_argument("--out", required=True, type=Path)
    c.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        named, episodes = json.loads(a.named.read_text()), json.loads(a.episodes.read_text())
        doc = plan(a.corpus, named, episodes)
        _write_json(a.out, doc)
        print(json.dumps(doc["counts"], sort_keys=True))
    else:
        if a.limit is not None and a.limit < 0:
            ap.error("--limit must be 0 or more")
        doc = cut(json.loads(a.plan.read_text()), a.out, a.limit)
        print(json.dumps({"kept": len(doc["clips"]), "refused": len(doc["refused"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
