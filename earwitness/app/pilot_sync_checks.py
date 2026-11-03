"""Per-episode checks and importer inputs for the nightly sync (fresh-data
design revision 4a, sections 5.3, 5.4 and 13). Pure functions over the
bytes of one checked package; no IO, no database. scripts/pilot_sync.py
calls them after collection (6.3) and before any descriptor.

The package layout is the Mini exporter's (earwitness/pipeline/
export_delta.py, EXPORT1). An episode folder `ep_<16 hex>/` holds the
export's own records for that episode:

- `revision.json` (8.1): `format` earwitness-episode-revision/1,
  `producer`, `episode_id`, `revision_seq`, `detection_status`,
  `detector_ran` (true when the episode had a detections file, even with
  zero candidates),
  `fetch_date`, `fetch_date_basis`, `fetch_observation` (`id`,
  `finished_at`, `final_url`, `http_status`, `bytes`, `sha256`),
  `stage_status` (`jev`, `names`: the run's jev_status and name_status,
  contract C3) and `components` ("<name>.json" -> SHA-256 of every other
  file);
- `fetch.json`: the episode's row of the export's episodes file;
- `transcript.json`: the transcript;
- `detections.json`: its rows of the rule-candidates file (present, even
  `[]`, means the detector ran);
- `names.json`: its rows of the named-reads file;
- `jev.json`: the export's Jev run object holding only this episode;
- `shownotes.json` (allowed, not imported).

A failing episode is refused with one code and never imported under a
guessed show. Attribution fields (brand_spoken, brand_url, brand_code,
brand_raw) pass through unchanged; nothing is promoted.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re

from earwitness.pipeline.corpus import episode_id_for

REVISION_FORMAT = "earwitness-episode-revision/1"
EPISODE_RE = re.compile(r"ep_[0-9a-f]{16}")
MEMBER_RE = re.compile(r"ep_([0-9a-f]{16})/(revision|transcript|detections|jev|names|shownotes|fetch)\.json")
STATUS_MEMBER = "status.json"
MEDIA_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".mp4",
                  ".m4v", ".webm", ".mov", ".aif", ".aiff")
COMPONENTS = ("fetch", "transcript", "detections", "jev", "names", "shownotes")
# detection_status -> components it needs (13). Anything else (the
# exporter's jev_not_run, names_not_run, names_failed, ...) is quarantined.
STATUS_NEEDS = {"complete": ("fetch", "transcript", "detections", "jev", "names"),
                "jev_skipped_budget": ("fetch", "transcript", "detections"),
                "jev_failed": ("fetch", "transcript", "detections"),
                "transcript_only": ("fetch", "transcript")}
CLOCK_SKEW_S = 600
# Contract C3: revision.json `stage_status` {"jev", "names"}, the Mini's
# jev_status and name_status for the revision's run (each at most 64
# characters, null when absent). The exporter's budget-skip value:
BUDGET_SKIP = "jev_skipped_budget"
STAGE_MAX = 64
STAGE_ROW_FIELDS = (("jev", "jev_status"), ("names", "name_status"))
# 13: a component field recording one of these must equal the observation's
# (audio, observation ID) or the transcript's (its SHA-256) value.
BINDING_CODES = {"audio_sha256": "OBSERVATION_BINDING",
                 "fetch_observation_id": "OBSERVATION_BINDING",
                 "transcript_sha256": "TRANSCRIPT_BINDING"}
# The per-episode entry ID of the export's Jev run object (export_delta.py).
JEV_EPISODE_ID_FIELD = "episode_id"


class ItemRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_instant(text) -> datetime.datetime:
    if not isinstance(text, str) or not text:
        raise ValueError("no instant")
    value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("instant without a zone")
    return value.astimezone(datetime.timezone.utc)


def parse_day(text) -> datetime.date:
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text[:10]):
        raise ValueError("no date")
    return datetime.date.fromisoformat(text[:10])


def load_inventory(raw: bytes) -> dict:
    """shows-inventory.json -> {feed_identity: entry}. Raises ValueError."""
    doc = json.loads(raw.decode("utf-8"))
    shows = doc.get("shows") if isinstance(doc, dict) else None
    if not isinstance(shows, list):
        raise ValueError("inventory needs a shows list")
    out = {}
    for show in shows:
        if not isinstance(show, dict) or not isinstance(show.get("feed_identity"), str) \
                or not isinstance(show.get("show_key"), str):
            raise ValueError("every show needs feed_identity and show_key")
        if show["feed_identity"] in out:
            raise ValueError("a feed identity is listed twice")
        out[show["feed_identity"]] = show
    return out


def recompute_id(row: dict) -> str:
    """The runner's pinned episode ID (corpus.episode_id_for) from the
    feed identity and GUID, or feed and enclosure when there is no GUID."""
    feed = row.get("feed_url")
    if not isinstance(feed, str) or not feed:
        raise ItemRefused("ID_BASIS", "no feed identity")
    guid = row.get("guid")
    if isinstance(guid, str) and guid.strip():
        entry = {"guid": guid}
    elif isinstance(row.get("audio_url"), str) and row["audio_url"]:
        entry = {"enclosures": [{"href": row["audio_url"], "type": "audio/mpeg"}]}
    else:
        raise ItemRefused("ID_BASIS", "neither GUID nor enclosure")
    try:
        return episode_id_for(feed, entry)[0]
    except Exception as exc:  # AcquisitionError and friends: refuse, never guess
        raise ItemRefused("ID_BASIS", type(exc).__name__) from None


def _json(files: dict, name: str):
    try:
        return json.loads(files[name].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ItemRefused("COMPONENT_INVALID", f"{name} is not UTF-8 JSON") from None


def _prefix(candidate_id) -> str | None:
    return candidate_id.split(".", 1)[0] if isinstance(candidate_id, str) else None


def _rows(files: dict, name: str, checked: str, fetch_date: str) -> list:
    """detections.json or names.json: a list of rows of this episode."""
    rows = _json(files, name)
    if not isinstance(rows, list):
        raise ItemRefused("COMPONENT_INVALID", name)
    for r in rows:
        if not isinstance(r, dict) or r.get("ep_id") != checked \
                or _prefix(r.get("candidate_id")) != checked:
            raise ItemRefused("EPISODE_ID_MISMATCH", name)
        if r.get("fetch_date") not in (None, fetch_date):
            raise ItemRefused("FETCH_DATE_CONFLICT", name)
    return rows


def _stage_status(rev: dict, row: dict, status: str) -> None:
    """Contract C3: the stage evidence each detection_status needs, and no
    contradiction with the episode row's own jev_status/name_status."""
    stage = rev.get("stage_status")
    if stage is None and status == "transcript_only":
        return
    if not isinstance(stage, dict):
        raise ItemRefused("DETECTION_STATUS", "stage_status")
    vals = {}
    for key, _field in STAGE_ROW_FIELDS:
        v = stage.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > STAGE_MAX):
            raise ItemRefused("DETECTION_STATUS", "stage_status")
        vals[key] = v
    j, n = vals["jev"] or "", vals["names"] or ""
    if status == "complete":
        good = j.startswith("ok") and n.startswith("ok")
    elif status == BUDGET_SKIP:
        good = BUDGET_SKIP in (vals["jev"], vals["names"])
    elif status == "jev_failed":
        good = j.startswith("failed")
    else:
        good = True
    if not good:
        raise ItemRefused("DETECTION_STATUS", "stage_status")
    for key, field in STAGE_ROW_FIELDS:
        if field in row:
            v = row[field]
            if v is not None and not isinstance(v, str) or \
                    (v[:STAGE_MAX] if v is not None else None) != vals[key]:
                raise ItemRefused("DETECTION_STATUS", "contradiction")


def _bindings(doc, expect: dict, name: str) -> None:
    """Every field of `doc` (at any depth) named in BINDING_CODES and not
    null must equal the expected value. Components that record none of
    them are bound only by the signed revision (section 4's limit)."""
    stack = [doc]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, x in v.items():
                if k in expect and x is not None and x != expect[k]:
                    raise ItemRefused(BINDING_CODES[k], name)
                if isinstance(x, (dict, list)):
                    stack.append(x)
        elif isinstance(v, list):
            stack.extend(x for x in v if isinstance(x, (dict, list)))


def check_episode(folder_id: str, files: dict, listed: dict, *, inventory: dict, scope: dict,
                  received_at: datetime.datetime, max_segments: int,
                  producer: str | None = None) -> dict:
    """Check one episode folder (bytes by component name) and return what
    the importer needs. `listed` is the manifest's revision entry. Raises
    ItemRefused with one code."""
    if "revision" not in files:
        raise ItemRefused("COMPONENT_MISSING", "revision.json")
    rev = _json(files, "revision")
    if not isinstance(rev, dict) or rev.get("format") != REVISION_FORMAT:
        raise ItemRefused("REVISION_INVALID", "format")
    if producer is not None and rev.get("producer") != producer:
        raise ItemRefused("REVISION_INVALID", "producer")
    seq = rev.get("revision_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ItemRefused("REVISION_INVALID", "revision_seq")
    if (rev.get("episode_id"), seq, rev.get("detection_status")) != \
            (listed.get("episode_id"), listed.get("revision_seq"), listed.get("detection_status")):
        raise ItemRefused("REVISION_UNLISTED", "revision.json differs from the manifest")
    # Component digests are bound by the signed revision.
    components = rev.get("components")
    allowed = {f"{c}.json" for c in COMPONENTS}
    if not isinstance(components, dict) or set(components) - allowed:
        raise ItemRefused("REVISION_INVALID", "components")
    present = {f"{name}.json" for name in files if name != "revision"}
    if present != set(components):
        raise ItemRefused("COMPONENT_UNBOUND", "files and components differ")
    for name in present:
        if sha256(files[name[:-5]]) != components[name]:
            raise ItemRefused("COMPONENT_DIGEST", name)
    status = rev.get("detection_status")
    needs = STATUS_NEEDS.get(status)
    if needs is None:
        raise ItemRefused("DETECTION_STATUS", str(status)[:40])
    for name in needs:
        if name not in files:
            raise ItemRefused("COMPONENT_MISSING", name)
    # 13: `detector_ran` is read from revision.json; it must agree with the
    # detections file, and every status but transcript_only needs it true.
    ran = rev.get("detector_ran")
    if not isinstance(ran, bool) or ran != ("detections" in files) or \
            (status != "transcript_only" and not ran):
        raise ItemRefused("DETECTION_STATUS", "detector_ran")
    row = _json(files, "fetch")
    if not isinstance(row, dict):
        raise ItemRefused("COMPONENT_INVALID", "fetch")
    _stage_status(rev, row, status)
    # 5.3 step 2 and 5.4: the checked ID is the recomputed one.
    checked = recompute_id(row)
    if folder_id != checked or rev.get("episode_id") != checked or row.get("ep_id") != checked:
        raise ItemRefused("EPISODE_ID_MISMATCH", "folder, revision or episode row")
    # 5.3 step 1: inventory membership; the show_key is the inventory's.
    show = inventory.get(row["feed_url"])
    if show is None:
        raise ItemRefused("SHOW_NOT_IN_INVENTORY")
    # 13: the fetch observation.
    obs = rev.get("fetch_observation")
    if not isinstance(obs, dict) or not isinstance(obs.get("id"), int) \
            or isinstance(obs.get("id"), bool) or not isinstance(obs.get("sha256"), str):
        raise ItemRefused("OBSERVATION_INVALID")
    try:
        finished = parse_instant(obs.get("finished_at"))
    except ValueError:
        raise ItemRefused("OBSERVATION_INVALID", "finished_at") from None
    if (finished - received_at).total_seconds() > CLOCK_SKEW_S:
        raise ItemRefused("OBSERVATION_FUTURE")
    fetch_date = finished.date().isoformat()
    if rev.get("fetch_date") != fetch_date or row.get("fetch_date") != fetch_date:
        raise ItemRefused("FETCH_DATE_CONFLICT", "revision or episode row")
    if row.get("fetch_observation_id") != obs["id"] or \
            row.get("audio_sha256") not in (None, obs["sha256"]):
        raise ItemRefused("OBSERVATION_BINDING", "episode row")
    if row.get("fetched_at") is not None:
        try:
            if parse_instant(row["fetched_at"]) != finished:
                raise ValueError
        except ValueError:
            raise ItemRefused("OBSERVATION_BINDING", "fetched_at") from None
    # 5.3 step 3: publication scope.
    try:
        pub = parse_day(row.get("pub_date"))
    except ValueError:
        raise ItemRefused("PUB_DATE_INVALID") from None
    if pub < scope["pub_date_from"] or pub > scope["pub_date_to"]:
        raise ItemRefused("OUT_OF_SCOPE")
    if (pub - finished.date()).days > 1:
        raise ItemRefused("PUB_AFTER_FETCH")
    # 5.4 across components, and 13's cross-file binding.
    tr = _json(files, "transcript")
    if not isinstance(tr, dict) or not isinstance(tr.get("segments"), list):
        raise ItemRefused("COMPONENT_INVALID", "transcript")
    if tr.get("ep_id") != checked or ("id" in tr and tr["id"] != checked):
        raise ItemRefused("EPISODE_ID_MISMATCH", "transcript")
    if len(tr["segments"]) > max_segments:
        raise ItemRefused("COMPONENT_TOO_LARGE", "segments")
    # 13: both transcript provenance bindings are required and must match.
    prov = tr.get("provenance") if isinstance(tr.get("provenance"), dict) else {}
    if prov.get("fetch_observation_id") is None or prov.get("audio_sha256") is None:
        raise ItemRefused("PROVENANCE_MISSING", "transcript provenance")
    if prov["fetch_observation_id"] != obs["id"] or prov["audio_sha256"] != obs["sha256"]:
        raise ItemRefused("OBSERVATION_BINDING", "transcript provenance")
    # 13: where any component records the audio or transcript digest (or the
    # observation ID), it must match.
    expect = {"audio_sha256": obs["sha256"], "fetch_observation_id": obs["id"],
              "transcript_sha256": sha256(files["transcript"])}
    for name in COMPONENTS:
        if name in files:
            _bindings(row if name == "fetch" else tr if name == "transcript"
                      else _json(files, name), expect, name)
    if "fetch_date" in tr and tr["fetch_date"] != fetch_date:
        raise ItemRefused("FETCH_DATE_CONFLICT", "transcript")
    candidates = _rows(files, "detections", checked, fetch_date) if "detections" in files else []
    reads = _rows(files, "names", checked, fetch_date) if "names" in files else []
    jev = None
    if "jev" in files:
        jev = _json(files, "jev")
        eps = jev.get("episodes") if isinstance(jev, dict) else None
        if not isinstance(eps, list) or not eps:
            raise ItemRefused("COMPONENT_INVALID", "jev")
        if any(not isinstance(e, dict) or e.get(JEV_EPISODE_ID_FIELD) != checked for e in eps):
            raise ItemRefused("EPISODE_ID_MISMATCH", "jev")
    if status != "complete":  # only what the status covers is imported
        jev, reads = None, []
        if status == "transcript_only":
            candidates = []
    episode = dict(row, ep_id=checked, fetch_date=fetch_date, fetched_at=finished.isoformat(),
                   fetch_observation_id=obs["id"], detection_status=status, clips=[])
    transcript = {k: v for k, v in tr.items() if k != "id"}
    transcript.update(ep_id=checked, fetch_date=fetch_date)
    return {"episode_id": checked, "revision_seq": seq, "revision_id": sha256(files["revision"]),
            "show_key": show["show_key"], "detection_status": status, "fetch_date": fetch_date,
            "episode": episode, "transcript": transcript, "candidates": candidates,
            "jev": jev, "reads": reads}


# The workspace's active source version of each role B5 replaces
# (import_sources._build_seed_plan stores the whole parsed file as payload).
ROLE_SOURCE_KEYS = {"episodes": "episodes.json", "candidates": "detector-output",
                    "jev": "jev-run", "named": "named-reads-output"}


class MergeRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# S8R11-01: DEMO2's show keys (its shows.json) are "<title slug>-<tag>",
# with tag = sha1(feed_url)[:8]; the slug is not recomputable here.
_DEMO2_KEY_RE = re.compile(r".+-([0-9a-f]{8})")


def feed_show_ids(feed_url) -> set:
    """Every show identity a removal list may name a feed's show by, from
    the feed alone (no inventory: DEMO2 snapshot shows outside the nightly
    inventory included): the app's customer show identity (the formula of
    customer._show_id_for_feed, over the trimmed URL as the app keys it) and
    the DEMO2 key's feed tag as "#<tag>". Empty without a usable feed."""
    if not isinstance(feed_url, str) or not feed_url.strip():
        return set()
    out = set()
    for feed in {feed_url, feed_url.strip()}:
        out.add("cs-" + hashlib.sha1(f"feed\0{feed}".encode()).hexdigest()[:12])
        out.add("#" + hashlib.sha1(feed.encode()).hexdigest()[:8])
    return out


def removal_show_ids(shows) -> set:
    """The removal list's show keys, plus "#<tag>" for each DEMO2-form key,
    to intersect with feed_show_ids and footprint show keys."""
    out = set(shows)
    for key in shows:
        m = _DEMO2_KEY_RE.fullmatch(key)
        if m:
            out.add("#" + m.group(1))
    return out


def episode_shows(rows, keys: dict) -> dict:
    """ep_id -> the identities of its show, from episodes role rows: the
    installed (or previous) inventory key of its feed (`keys`: feed identity
    -> show key) and feed_show_ids. An episode without a usable feed has none."""
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("ep_id"), str):
            continue
        ids = feed_show_ids(row.get("feed_url"))
        if ids and keys.get(row["feed_url"]) is not None:
            ids.add(keys[row["feed_url"]])
        out.setdefault(row["ep_id"], set()).update(ids)
    return out


def footprint(merged: dict, items: list, keys: dict) -> dict:
    """Every episode and show a delivery writes (8.3 recovery step 1): the
    episodes of every cumulative role file and transcript, and every
    identity of their shows (episode_shows). `shows_complete` is False when
    some episode's show is unknown (no episodes row with a usable feed)."""
    eps = {i["episode_id"] for i in items}
    for role in ("episodes", "candidates", "named"):
        eps |= {r.get("ep_id") for r in merged.get(role) or [] if isinstance(r, dict)}
    eps |= {e.get(JEV_EPISODE_ID_FIELD) for e in (merged.get("jev") or {}).get("episodes", [])
            if isinstance(e, dict)}
    by_ep = episode_shows(merged.get("episodes"), keys)
    shows = {i["show_key"] for i in items}.union(*by_ep.values())
    return {"episode_ids": sorted(e for e in eps if isinstance(e, str)), "show_keys": sorted(shows),
            "shows_complete": all(isinstance(e, str) and by_ep.get(e) for e in eps)}


def merge_roles(current: dict, items: list, drop: set) -> dict:
    """Cumulative role files for one target: the workspace's current file
    for each role (None when absent), with every row of the episodes in
    this package (`items`) or in `drop` (removed) taken out, and the
    package's rows added. Rows are matched by their native per-episode ID.
    Raises MergeRefused("MERGE_SOURCE_INVALID") on a malformed current file."""
    replaced = {i["episode_id"] for i in items} | set(drop)

    def rows(role, field="ep_id"):
        cur = current.get(role)
        if cur is None:
            return []
        if not isinstance(cur, list) or not all(isinstance(r, dict) for r in cur):
            raise MergeRefused("MERGE_SOURCE_INVALID")
        return [r for r in cur if r.get(field) not in replaced]

    def order(rs):
        return sorted(rs, key=lambda r: (str(r.get("ep_id")), r.get("start_s") or 0,
                                         str(r.get("candidate_id"))))
    out = {"episodes": sorted(rows("episodes") + [i["episode"] for i in items],
                              key=lambda r: str(r.get("ep_id"))),
           "candidates": order(rows("candidates") + [c for i in items for c in i["candidates"]])}
    cur_jev = current.get("jev")
    if cur_jev is not None and not (isinstance(cur_jev, dict) and
                                    isinstance(cur_jev.get("episodes"), list)):
        raise MergeRefused("MERGE_SOURCE_INVALID")
    new_jev = [i["jev"] for i in items if i["jev"] is not None]
    if cur_jev is not None or new_jev:
        kept = [e for e in (cur_jev or {}).get("episodes", [])
                if not (isinstance(e, dict) and e.get(JEV_EPISODE_ID_FIELD) in replaced)]
        added = [e for j in new_jev for e in j["episodes"]]
        top = cur_jev if cur_jev is not None else new_jev[0]
        out["jev"] = dict(top, episodes=sorted(kept + added,
                                               key=lambda e: str(e.get(JEV_EPISODE_ID_FIELD))))
    if current.get("named") is not None or new_jev:
        out["named"] = order(rows("named") + [r for i in items for r in i["reads"]])
    return out
