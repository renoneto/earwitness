"""Consistent, inspectable evidence and campaign exports (stdlib only)."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import sqlite3

from . import campaigns, db, ops

RESEARCH_FORMATS = ("csv", "json", "html")


class DigestMismatch(ops.BadInput):
    """Stored export bytes fail their sha256 digest; refused, never served."""


def _export_columns(con: sqlite3.Connection) -> set:
    return {r["name"] for r in con.execute("PRAGMA table_info(exports)")}


def render_capture(rows: list, fmt: str, title: str = "EarWitness research export",
                   limitations: str = "") -> str:
    """Render persisted research-capture bytes. Audio is never bundled.
    A limitations line, when given, is the JSON `limitations` value, a
    CSV `limitations` column (filled on the first row; with no rows, one
    row with only that cell) and a paragraph under the printable page's
    heading."""
    if fmt not in RESEARCH_FORMATS:
        raise ops.BadInput("format must be csv, json, or html")
    # A held brand (under review) is blanked by _customer_row; the CSV's
    # web address/code column reads the flag from the row as given.
    held = [_held(r) for r in rows]
    rows = [_customer_row({k: v for k, v in dict(r).items() if k != "audio"}) for r in rows]
    if fmt == "json":
        return json.dumps({"title": title, "exported_at": db.utcnow(),
                           "item_count": len(rows), "items": rows,
                           **({"limitations": limitations} if limitations else {})},
                          indent=2, ensure_ascii=False, default=str)
    if fmt == "csv":
        # M7: what a reader needs first, then the rest, internal IDs last.
        keys = []
        for row in rows:
            for key in row:
                if key not in keys and key not in CSV_LEAD and key not in CSV_IDS \
                        and key not in _CSV_FOLDED and key != "audio":
                    keys.append(key)
        ids = [k for k in CSV_IDS if k == "ref" or any(k in row for row in rows)]
        note = ["limitations"] if limitations else []
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(list(CSV_LEAD) + keys + ids + note)
        for i, row in enumerate(rows):
            writer.writerow([_csv_cell(_lead(key, row, held[i])) for key in CSV_LEAD]
                            + [_csv_cell(_plain(key, row.get(key))) for key in keys + ids]
                            + [_csv_cell(limitations if i == 0 else "") for _ in note])
        if note and not rows:
            writer.writerow([""] * (len(CSV_LEAD) + len(keys) + len(ids))
                            + [_csv_cell(limitations)])
        return buf.getvalue()
    parts = [html.escape(title, quote=True)]
    note = "<p>%s</p>" % html.escape(limitations, quote=True) if limitations else ""
    body = ["<html><head><meta charset=\"utf-8\"><title>%s</title></head><body>" % parts[0],
            "<h1>%s</h1>%s<p>%d items</p><table>" % (parts[0], note, len(rows))]
    for row in rows:
        cells = "".join("<td>%s</td>" % html.escape(str(row.get(k, "")), quote=True)
                        for k in ("ref",) if k in row)
        rest = "".join("<td>%s</td>" % html.escape(_plain(k, v), quote=True)
                       for k, v in row.items() if k not in ("ref", "audio"))
        body.append("<tr>%s%s</tr>" % (cells, rest))
    body.append("</table></body></html>")
    return "\n".join(body)


# -- Readable cells for CSV and the printable page ---------------------------
# A capture row carries structured values (show, read, attribution, ...).
# Spreadsheets get plain text, never Python dict/list repr. JSON keeps the
# full structure. Nothing is derived: a missing value is an empty cell, a
# name under review stays hidden, spoken-brand and URL/code attribution
# stay in their own words.

def _clock(value):
    v = int(float(value) + 0.5)
    h, m, sec = v // 3600, v // 60 % 60, v % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _range(span):
    if not isinstance(span, (list, tuple)) or len(span) < 2:
        return ""
    try:
        return f"{_clock(span[0])}\u2013{_clock(span[1])}"
    except (TypeError, ValueError):
        return ""


# PLAIN-ADS (DEP7): a customer file says what was found, never its review
# state. The stored record keeps status, review and basis; a new export
# names the kind (ad / mention), drops the review judgment, keeps each
# attribution's brand and channel without its basis, and a held ad (its
# brand waits for review) reads "Brand not identified".
HELD_LABEL = "Brand not identified"
_READ_REVIEW_KEYS = ("needs_review", "display_basis", "confidence", "confidence_score")
_BRAND_REVIEW_KEYS = ("confirmed", "under_review", "suggested", "display_basis")


# PLAIN-ADS-FIX1 (PA-R1-01): the kind comes from the record type, so an
# Episodes row is an episode, never an ad. A row without a known record
# type falls back to its status: a mention, an ad status, else blank.
_KIND_BY_RECORD = {"evidence": "ad", "transcript_passage": "mention", "episode": "episode"}
_AD_STATUSES = ("confirmed", "finding", "unresolved", "rejected")


def _record_kind_word(row: dict) -> str:
    kind = _KIND_BY_RECORD.get(row.get("record_kind"))
    if kind:
        return kind
    status = row.get("status")
    return "mention" if status == "mention" else "ad" if status in _AD_STATUSES else ""


def _customer_row(row: dict) -> dict:
    out = {}
    read = row.get("read") if isinstance(row.get("read"), dict) else {}
    brand = read.get("brand") if isinstance(read.get("brand"), dict) else {}
    for key, value in row.items():
        if key == "status":
            out["kind"] = _record_kind_word(row)
        elif key == "review":
            continue
        elif key == "show" and isinstance(value, dict):
            # The show identity's own resolution flag is internal too.
            out[key] = {k: v for k, v in value.items() if k != "unresolved"}
        elif key == "attribution" and isinstance(value, list):
            # PODS: model rows are display-only
            out[key] = [_customer_assoc(a, brand) for a in value
                        if not (isinstance(a, dict) and a.get("basis") == "model")]
        elif key == "read" and isinstance(value, dict):
            out[key] = _customer_read(value)
        else:
            out[key] = value
    return out


def _customer_assoc(assoc, brand: dict):
    """An attribution without its basis, named in the read's display
    spelling when it is the read's own brand (never a held one)."""
    if not isinstance(assoc, dict):
        return assoc
    out = {k: v for k, v in assoc.items() if k != "basis"}
    if not out.get("brand_display") and brand.get("display") and not brand.get("under_review") \
            and str(brand.get("key") or "").lower() == str(out.get("brand") or "").lower():
        out["brand_display"] = brand["display"]
    return out


def _customer_read(read: dict) -> dict:
    out = {k: v for k, v in read.items() if k not in _READ_REVIEW_KEYS}
    brand = read.get("brand")
    if isinstance(brand, dict) and brand.get("under_review"):
        # A held brand: nothing the read said is written (it could name it).
        out.pop("said", None)
    if isinstance(brand, dict):
        out["brand"] = {"key": None, "display": None} if brand.get("under_review") \
            else {k: v for k, v in brand.items() if k not in _BRAND_REVIEW_KEYS}
    # PA-R4-02: the namer's repair notes ("repair:ranger->granger") name a
    # detector spelling; a customer file carries none (JSON is the only
    # format that writes candidates).
    if isinstance(read.get("candidates"), list):
        out["candidates"] = [dict(c, evidence=[t for t in c["evidence"]
                                               if not str(t).startswith("repair:")])
                             if isinstance(c, dict) and isinstance(c.get("evidence"), list) else c
                             for c in read["candidates"]]
    return out


def _brand_label(brand):
    brand = brand if isinstance(brand, dict) else {}
    if brand.get("under_review"):
        return HELD_LABEL
    return " \u00b7 ".join(str(brand[k]) for k in ("display", "product", "network") if brand.get(k))


def _brand_words(read):
    """The brand as the page shows it: its name, else "Brand not identified"."""
    if not isinstance(read, dict):
        return ""
    brand = read.get("brand") if isinstance(read.get("brand"), dict) else {}
    return _brand_label(brand) or HELD_LABEL


def _address_parts(said):
    """The web addresses and codes a read said: URL/code attribution only,
    never the spoken brand."""
    said = said if isinstance(said, dict) else {}
    parts = []
    for address in said.get("web_addresses") or []:
        parts.append(f"web address {address}" + (f" (names {said['address_brand']})" if said.get("address_brand") else ""))
    for code in said.get("promo_codes") or []:
        parts.append(f"code {code}" + (f" (names {said['code_brand']})" if said.get("code_brand") else ""))
    return parts


def _address_words(said):
    return ", ".join(_address_parts(said))


def _said_words(said):
    said = said if isinstance(said, dict) else {}
    parts = _address_parts(said)
    if said.get("spoken_brand"):
        parts.append(f"spoken brand {said['spoken_brand']}")
    return ", ".join(parts)


def _read_words(read):
    parts = [read.get("source_label") or read.get("kind") or ""]
    parts.append(_brand_words(read))
    parts.append(_range(read.get("display_span")))
    if read.get("self_promo"):
        parts.append("self-promotion")
    brand = read.get("brand") if isinstance(read.get("brand"), dict) else {}
    said = "" if brand.get("under_review") else _said_words(read.get("said"))
    if said:
        parts.append("said: " + said)
    return "; ".join(p for p in parts if p)


def _generic(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_generic(v)}" for k, v in value.items() if v not in (None, "", [], {}))
    if isinstance(value, (list, tuple)):
        return "; ".join(_generic(v) for v in value)
    return str(value)


def _plain(key, value):
    if value is None:
        return ""
    if key == "show" and isinstance(value, dict):
        return str(value.get("display_title") or "")
    if key == "read" and isinstance(value, dict):
        return _read_words(value)
    if key == "quotation_span":
        return _range(value)
    if key == "attribution" and isinstance(value, list):
        return "; ".join(
            (f"{a.get('brand_display') or a.get('brand')}"
             + (f" ({a['channel']})" if a.get("channel") else ""))
            if isinstance(a, dict) else str(a) for a in value
            # PODS: model rows are display-only
            if not (isinstance(a, dict) and a.get("basis") == "model"))
    if key == "provenance" and isinstance(value, dict):
        return (f"{value.get('source_key')} v{value.get('version_no')}"
                f" saved {value.get('created_at')}, sha256 {value.get('sha256')}")
    if isinstance(value, (dict, list, tuple)):
        return _generic(value)
    return str(value)


# M7 research CSV layout (new captures only; stored bytes are served as
# they are). Lead columns, in order; the row keys they fold in are not
# repeated (quotation_span was a second time range); internal IDs last.
CSV_LEAD = ("brand", "show", "episode", "published", "time_range", "text",
            "web_address_or_code", "clip", "evidence_link")
CSV_IDS = ("ref", "record_kind", "evidence_id", "episode_id", "source_version_id",
           "transcript_version_id", "sort_ref", "ad_id", "brand_keys", "id",
           "related_evidence")
_CSV_FOLDED = ("show_title", "ep_title", "pub_date", "quotation_span", "audio_local")
# The customer evidence page (app/views.js '#/evidence/' + ref), relative:
# the file names no host.
EVIDENCE_LINK = "/app/#/evidence/"


def _held(row) -> bool:
    read = row.get("read") if isinstance(row.get("read"), dict) else {}
    brand = read.get("brand") if isinstance(read.get("brand"), dict) else {}
    return bool(brand.get("under_review"))


def _lead(key, row, held=False):
    if key == "brand":
        return _derived("brand", row) if isinstance(row.get("read"), dict) \
            else _plain("brand", row.get("brand"))
    if key == "show":
        return _plain("show", row.get("show")) or _plain("show_title", row.get("show_title"))
    if key == "episode":
        return _plain("ep_title", row.get("ep_title"))
    if key == "published":
        return _plain("pub_date", row.get("pub_date"))
    if key == "time_range":
        return _derived("time_range", row)
    if key == "text":
        return _plain("text", row.get("text"))
    if key == "web_address_or_code":
        read = row.get("read") if isinstance(row.get("read"), dict) else {}
        return "" if held or _held(row) else _address_words(read.get("said"))
    if key == "clip":
        # M7: an ad plays its own evidence clip (read.has_clip, the page's
        # player) as well as a pinned episode recording (audio_local). Only
        # this new CSV column counts it; the research read is unchanged.
        if "audio_local" not in row:
            return ""
        read = row.get("read") if isinstance(row.get("read"), dict) else {}
        return "yes" if row["audio_local"] or read.get("has_clip") is True else "no"
    if key == "evidence_link":
        ref = row.get("ref")
        if row.get("record_kind") != "evidence" or not isinstance(ref, str) or not ref:
            return ""
        from urllib.parse import quote
        return EVIDENCE_LINK + quote(ref, safe="")
    return ""


def _derived(key, row):
    read = row.get("read")
    if key == "brand":
        return _brand_words(read)
    span = read.get("display_span") if isinstance(read, dict) and read.get("display_span") else row.get("quotation_span")
    return _range(span)


def store_capture(con: sqlite3.Connection, rows: list, fmt: str, scope: str,
                  count_units: str, captured: dict, title: str,
                  actor: str = "owner", ctx=None) -> dict:
    """Persist research-capture bytes with a sha256 digest and pinned basis."""
    return insert_capture(con, prepare_capture(con, rows, fmt, scope, count_units,
                                               captured, title, ctx=ctx), actor)


def prepare_capture(con: sqlite3.Connection, rows: list, fmt: str, scope: str,
                    count_units: str, captured: dict, title: str, ctx=None) -> dict:
    """store_capture's reads and rendering, with no write: each row's
    disclosure (excerpt-only rows keep their own pinned excerpt), the
    pinned basis and its digest, the file bytes and their sha256."""
    from . import citations
    captured = dict(captured)
    if any(not row.get("ref") for row in rows):
        from .access import AuthorizationError
        raise AuthorizationError("export capture requires all embedded source fields", 403)
    kept, disclosure, narrowed, facts = [], [], False, {}
    for row in rows:
        locators, full = export_disclosure(con, row["ref"], ctx)
        narrowed = narrowed or not full
        if locators is None:
            continue
        if not full:
            # An ad row's text, context and match_excerpt are transcript over
            # its interval, and the read's own words over its display span:
            # the row keeps only its own pinned excerpt.
            row = {k: v for k, v in row.items() if k != "match_excerpt"}
            row.update(text=cap_excerpt(_pinned_excerpt(con, row["ref"])), context="")
            if isinstance(row.get("read"), dict):
                row["read"] = {k: v for k, v in row["read"].items() if k != "display_text"}
        elif "text" in row and any(loc.get("rule") == citations.PA1 for loc in locators):
            # An excerpt-level (Plan A) row's text is its safe excerpt.
            row = dict(row, text=cap_excerpt(row["text"]))
        row = _with_fetch_date(con, row, facts)
        kept.append(row)
        disclosure.extend(locators)
    withheld = len(rows) - len(kept)
    rows = kept
    captured["disclosure"] = disclosure
    # DEMO2-SCR-R1-01: Saved › Reports says when a file is empty because
    # rows containing transcript text were left out.
    captured["withheld"] = withheld
    captured["capture_digest"] = citations.capture_digest(captured)
    payload = render_capture(rows, fmt, title, limitations=(
        transcript_notice(withheld) if narrowed or _excerpt_only(con) else ""))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {"rows": rows, "withheld": withheld, "captured": captured, "payload": payload,
            "digest": digest, "format": fmt, "scope": scope, "count_units": count_units,
            "title": title}


def insert_capture(con: sqlite3.Connection, prepared: dict, actor: str = "owner") -> dict:
    """store_capture's write: the export row, the revision and its audit."""
    rows, withheld, captured = prepared["rows"], prepared["withheld"], prepared["captured"]
    payload, digest, fmt = prepared["payload"], prepared["digest"], prepared["format"]
    scope, count_units, title = prepared["scope"], prepared["count_units"], prepared["title"]
    now = db.utcnow()
    agg = db.aggregate_revision(con)
    cols = _export_columns(con)
    data = {"created_at": now, "format": fmt,
            "filter": json.dumps(captured.get("query", {}), sort_keys=True,
                                 default=str),
            "aggregate_rev": agg, "item_count": len(rows), "payload": payload}
    if "workspace_id" in cols:
        data["workspace_id"] = "local"
    if "title" in cols:
        data["title"] = title[:160]
    if "digest" in cols:
        data["digest"] = digest
    if "count_units" in cols:
        data["count_units"] = count_units
    if "captured_json" in cols:
        data["captured_json"] = json.dumps(captured, sort_keys=True, default=str)
    names = ", ".join(data)
    cur = con.execute(f"INSERT INTO exports ({names})"
                      f" VALUES ({', '.join('?' * len(data))})", tuple(data.values()))
    rev = db.bump_revision(con)
    db.audit(con, actor, "research_export", "export", str(cur.lastrowid),
             {"format": fmt, "items": len(rows), "digest": digest,
              "scope": scope}, rev)
    return {"export_id": cur.lastrowid, "format": fmt, "item_count": len(rows),
            "aggregate_rev": agg, "digest": digest, "title": title,
            "payload": payload, "revision": rev, "rows": rows, "withheld": withheld}


def transcript_notice(withheld: int) -> str:
    """The export file's notice under an excerpt-only export rule."""
    left = ("No results containing transcript text were left out." if withheld == 0 else
            "1 result containing transcript text was left out." if withheld == 1 else
            f"{withheld} results containing transcript text were left out.")
    return ("This workspace allows ad excerpts to be exported, but not transcript text. "
            f"{left} Ad results include excerpts only.")


def _excerpt_only(con: sqlite3.Connection) -> bool:
    """No source lets transcript text be exported, some refuse it, and some
    let excerpts be exported: then an export that narrowed no row (one with
    no rows) still says so."""
    return con.execute(
        "SELECT EXISTS (SELECT 1 FROM source_content_policy WHERE workspace_id='local'"
        " AND content_category='transcript' AND export_eligible=0)"
        " AND NOT EXISTS (SELECT 1 FROM source_content_policy WHERE workspace_id='local'"
        " AND content_category='transcript' AND export_eligible=1)"
        " AND EXISTS (SELECT 1 FROM source_content_policy WHERE workspace_id='local'"
        " AND content_category='excerpt' AND export_eligible=1)").fetchone()[0] == 1


TRANSCRIPT_EXPORT_REFUSED = "transcript_export"


def _as_excerpt(locators: list) -> list:
    """The locators with `transcript` export asked as `excerpt` export."""
    return [dict(loc, fields=list(dict.fromkeys(
        "excerpt" if f == "transcript" else f for f in loc["fields"])))
        for loc in locators]


def transcript_refusal(con: sqlite3.Connection, disclosures: list, ctx=None) -> bool:
    """Whether export of these retained locators is refused only because
    transcript export is not granted: refused as they are, allowed with
    transcript asked as excerpt, for a caller whose own grant permits
    transcript. Records nothing on ctx."""
    from . import citations

    def check(locators):
        return lambda probe: citations.authorize_retained(
            con, {"disclosure": locators}, probe, export=True)
    # The caller's own grant refusing transcript is not the record's rule.
    if ctx is not None and not ctx.permits("transcript"):
        return False
    if not any("transcript" in loc.get("fields", ()) for loc in disclosures) \
            or citations.export_permitted(ctx, check(disclosures)):
        return False
    return citations.export_permitted(ctx, check(_as_excerpt(disclosures)))


def refuse_export(con: sqlite3.Connection, disclosures: list, ctx, exc) -> None:
    """Re-raise an export refusal; its error details name the transcript
    rule when that rule alone refused (the app explains it)."""
    from .access import AuthorizationError, DisclosureRefused
    err = type(exc)(str(exc), getattr(exc, "status", 403)) \
        if type(exc) in (AuthorizationError, DisclosureRefused) \
        else AuthorizationError(str(exc), getattr(exc, "status", 403))
    from . import citations
    if citations.plan_refusal(con, disclosures, ctx, export=True):
        # Plan A (design 6): an artifact made before excerpt level.
        err.details = {"reason": "plan"}
    elif transcript_refusal(con, disclosures, ctx):
        err.details = {"reason": TRANSCRIPT_EXPORT_REFUSED}
    raise err from exc


def stored_disclosure(con: sqlite3.Connection, export_id) -> list:
    row = con.execute("SELECT captured_json FROM exports WHERE id=?", (export_id,)).fetchone()
    return (json.loads(row["captured_json"] or "{}").get("disclosure") or []) if row else []


def _publication(con: sqlite3.Connection, ref: str) -> dict:
    from . import citations
    if not ref.startswith("sr1:"):
        ref = citations.qualify_legacy_source_ref(con, ref)
    state_id, _window = citations._parse_source_ref(ref)
    return dict(con.execute("SELECT kind, record_id, pins_json, bounds_json FROM source_states"
                            " WHERE id=?",
                            (state_id,)).fetchone())


# UX decision D7: an ad excerpt in a new export or frozen report is short.
# Applied only when a capture is built: stored evidence keeps its length,
# and stored export and report bytes are served and verified as they are.
EXCERPT_CAP = 200


def cap_excerpt(text):
    """text cut at a word boundary to at most EXCERPT_CAP characters,
    ending in "…" when cut. Shorter text (and None) is unchanged. A pa1
    row or capture keeps its excerpt_bounds: they name the source passage
    the excerpt came from, not the span of the shortened text."""
    if not isinstance(text, str) or len(text) <= EXCERPT_CAP:
        return text
    cut = text[:EXCERPT_CAP - 1]
    if not text[EXCERPT_CAP - 1].isspace():
        space = max((i for i, c in enumerate(cut) if c.isspace()), default=-1)
        cut = cut[:space] if space > 0 else cut
    return cut.rstrip() + "\u2026"


def _with_fetch_date(con: sqlite3.Connection, row: dict, facts: dict) -> dict:
    """The row with the fetch date its evidence citation carries
    (citations._cited_fetch_date: the cited version's own fetch_date and
    basis when it records them, else the episode's fetch_date), with its
    source and basis, so every new capture keeps fetch_date and its
    provenance. A row that already has a fetch_date keeps it; facts
    memoizes one export's (transcript version, episode) pairs."""
    from . import citations
    if "fetch_date" in row:
        return row
    key = (row.get("transcript_version_id"), row.get("episode_id"))
    if key not in facts:
        ep = con.execute("SELECT fetch_date FROM episodes WHERE id=?",
                         (key[1],)).fetchone() if key[1] is not None else None
        facts[key] = citations._cited_fetch_date(con, key[0], ep[0] if ep else None)
    cited = facts[key]
    return dict(row, fetch_date=cited.get("fetch_date"),
                fetch_date_source=cited.get("fetch_date_source"),
                fetch_date_basis=cited.get("fetch_date_basis"))


def _pinned_excerpt(con: sqlite3.Connection, ref: str) -> str:
    """The excerpt of the evidence revision ref pins ("" if none): its
    stored excerpt, cut to the pinned reviewed bounds when they narrow the
    read (campaigns._reviewed_excerpt, T2-EXPORT). An export row and a
    frozen report (campaigns._excerpt_capture) both take it from here."""
    from . import citations
    state = _publication(con, ref)
    pins = json.loads(state["pins_json"])
    ev = citations._component(con, "evidence_history", "evidence_id", state["record_id"],
                              "evidence_revision", pins["evidence_revision"])
    return campaigns._reviewed_excerpt(
        con, {"bounds": json.loads(state["bounds_json"]), "projection": pins},
        ev.get("excerpt") or "")


def export_disclosure(con: sqlite3.Connection, ref: str, ctx=None) -> tuple:
    """(locators, full) an export of ref must hold; locators is None when
    the row is left out.

    A ref whose every category may be exported keeps its full locators.
    When `transcript` export is refused, the transcript locator asks for
    `excerpt` export instead; if that holds, an ad (evidence) row is
    exported, since its text is the ad excerpt, and an episode or
    transcript passage row is left out, since its text is transcript.
    Anything still refused refuses the export (fail closed), so a record
    without excerpt export refuses as before.
    """
    locators, full = export_locators(con, ref, ctx)
    if full or _publication(con, ref)["kind"] == "evidence":
        return locators, full
    return None, False


def export_locators(con: sqlite3.Connection, ref: str, ctx=None) -> tuple:
    """(locators, full) of export_disclosure before the row decision: the
    full locators when every category may be exported, else the locators
    with `transcript` asked as `excerpt`, authorized for export. Anything
    still refused raises. Frozen campaign reports use the same rule
    (campaigns._captured_attachment)."""
    from . import citations
    from .access import AuthorizationError
    try:
        return citations.captured_disclosure(con, ref, ctx, export=True), True
    except AuthorizationError:
        locators = citations.captured_disclosure(con, ref, ctx)
        if not any("transcript" in loc["fields"] for loc in locators):
            raise
        without = _as_excerpt(locators)
        citations.authorize_retained(con, {"disclosure": without}, ctx, export=True)
    return without, False


def archive_list(con: sqlite3.Connection) -> list:
    """Export metadata without payload bytes, newest last."""
    cols = _export_columns(con)
    want = ["id", "created_at", "format", "aggregate_rev", "item_count"]
    for extra in ("workspace_id", "title", "digest", "count_units"):
        if extra in cols:
            want.append(extra)
    rows = con.execute("SELECT %s FROM exports ORDER BY id" % ", ".join(want)).fetchall()
    return [dict(r) for r in rows]


ARCHIVE_LIMIT = 20
ARCHIVE_KINDS = ("report", "export")


def _archive_revision(con: sqlite3.Connection) -> int:
    try:
        return db.aggregate_revision(con)
    except sqlite3.Error:
        return 0


def _encode_archive_cursor(key: list, offset: int, revision: int) -> str:
    import base64
    raw = json.dumps({"k": key, "o": offset, "r": revision},
                     sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_archive_cursor(cursor: str, key: list, revision: int) -> int:
    import base64
    try:
        payload = json.loads(base64.urlsafe_b64decode(
            cursor.encode()).decode())
    except (ValueError, TypeError):
        raise ops.BadInput("malformed archive cursor")
    if not isinstance(payload, dict) or payload.get("k") != key \
            or payload.get("r") != revision:
        raise ops.BadInput("archive changed; refresh the list")
    offset = payload.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise ops.BadInput("malformed archive cursor")
    return offset


def archive_collection(con: sqlite3.Connection, *, kind: str | None = None,
                       campaign_id: int | None = None, q: str = "",
                       cursor: str | None = None,
                       limit: int = ARCHIVE_LIMIT, ctx=None) -> dict:
    """B4 retained archive: reports plus exports as one read projection.

    Deterministic capture-time/ID ordering; literal kind/campaign/title
    filters; cursor binds the filter cohort and a changed cohort reads
    as a refresh denial, never mixed pages. No payload bytes, no
    regeneration: rows project stored exports/campaign_reports only.
    """
    if kind is not None and kind not in ARCHIVE_KINDS:
        raise ops.BadInput(f"invalid archive kind: {kind!r}")
    if campaign_id is not None:
        try:
            campaign_id = int(campaign_id)
        except (TypeError, ValueError):
            raise ops.BadInput("campaign_id must be an integer")
    if not isinstance(q, str):
        raise ops.BadInput("q must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) \
            or limit < 1 or limit > ARCHIVE_LIMIT:
        raise ops.BadInput(f"limit must be 1..{ARCHIVE_LIMIT}")
    cols = _export_columns(con)
    needle = q.strip().casefold()
    items: list[dict] = []
    exports_withheld = {"plan": 0, "access": 0, "rights": 0}
    if kind is None or kind == "export":
        want = ["id", "created_at", "format", "aggregate_rev", "item_count"]
        for extra in ("workspace_id", "title", "digest", "count_units",
                      "captured_json"):
            if extra in cols:
                want.append(extra)
        scope = " WHERE workspace_id='local'" if "workspace_id" in cols else ""
        for row in con.execute(
                "SELECT %s FROM exports%s" % (", ".join(want), scope)
                ).fetchall():
            row = dict(row)
            from . import citations, access
            try:
                load_export_bytes(con, row["id"], ctx, export=False)
            except access.AuthorizationError as exc:
                # DPC-06 (UX DENY-WORDS (4), 34c544385): a withheld export
                # (a report's own file too: the archive lists it as its own
                # row) is counted under its first reason, plan, access or
                # rights, never described; its title too may name what is
                # withheld, so no filter but the kind applies to it.
                reason = citations.withheld_reason(con, exc, stored_disclosure(con, row["id"]), ctx)
                if reason is not None:
                    exports_withheld[reason] += 1
                continue
            except (DigestMismatch, citations.CitationError):
                continue
            exportable = citations.export_permitted(
                ctx, lambda probe_ctx: load_export_bytes(con, row["id"], probe_ctx))
            title = row.get("title") or f"export-{row['id']}"
            if needle and needle not in title.casefold():
                continue
            captured = {}
            if row.get("captured_json"):
                try:
                    captured = json.loads(row["captured_json"])
                except (ValueError, TypeError):
                    captured = {}
            stale, reasons = _export_staleness(con, captured, ctx)
            items.append({
                "id": row["id"], "kind": "export", "title": title,
                "campaign_id": None, "report_no": None,
                "captured_at": row["created_at"],
                "pins": {"view_revision": captured.get("view_revision"),
                         "scope": captured.get("scope")},
                "count": row["item_count"],
                # Rows left out as transcript text (None: stored before
                # this was kept).
                "withheld": captured.get("withheld"),
                "count_units": row.get("count_units") or "",
                "format": row["format"], "digest": row.get("digest") or "",
                "download_path": f"/customer/exports/{row['id']}",
                "exportable": exportable,
                "stale": stale, "stale_reasons": reasons,
                "limitations": [LIMITATIONS],
            })
    withheld_reports: list[dict] = []
    # DENY-WORDS (ADC-C-06/07, lead 10-30): reports the plan withholds and
    # reports a grant's source list leaves out are counted, never described
    # (the archive lists what the member can open; it never refuses whole).
    counted = {"plan": [], "access": []}
    if kind is None or kind == "report":
        for row in con.execute(
                "SELECT r.id, r.campaign_id, r.report_no, r.created_at,"
                " r.pins_json, r.export_id, r.export_digest,"
                " r.campaign_revision, c.name AS campaign_name"
                " FROM campaign_reports r LEFT JOIN campaigns c"
                " ON c.id=r.campaign_id"
                " WHERE r.workspace_id='local'").fetchall():
            row = dict(row)
            from . import campaigns, citations, access
            try:
                exportable = campaigns.report_exportable(
                    con, row["campaign_id"], row["report_no"], ctx)
            except access.AuthorizationError as exc:
                # The report history's order: the plan refusal first; a
                # report a source's own policy withholds keeps its row, its
                # number only (ADS-R1-02, placed below), uncounted; one the
                # grant's source list leaves out is counted. Any other
                # refusal is left out, as before. Only a refused row's
                # stored JSON is read, for its locators (R1-02: a listed
                # row is built without it).
                frozen = con.execute("SELECT report_json FROM campaign_reports WHERE id=?",
                                     (row["id"],)).fetchone()[0]
                # DPC-02: a campaign the caller cannot read is no filter on
                # what is withheld (its name would answer the title filter).
                row["campaign_readable"] = citations.export_permitted(
                    ctx, lambda probe: citations.authorize_resource(
                        con, "campaign", row["campaign_id"], probe))
                reason = citations.withheld_reason(con, exc, campaigns._report_disclosure(
                    con, row["export_id"], json.loads(frozen or "{}")), ctx)
                if reason == "rights":
                    withheld_reports.append(row)
                elif reason is not None:
                    counted[reason].append(row)
                continue
            except (ops.BadInput, DigestMismatch, citations.CitationError):
                continue
            if campaign_id is not None and row["campaign_id"] != campaign_id:
                continue
            title = row["campaign_name"] or \
                f"campaign-{row['campaign_id']}-report-{row['report_no']}"
            if needle and needle not in title.casefold():
                continue
            try:
                pins = json.loads(row["pins_json"] or "{}")
            except (ValueError, TypeError):
                pins = {}
            stale, reasons = _report_staleness(con, row, pins)
            items.append({
                "id": row["id"], "kind": "report", "title": title,
                "campaign_id": row["campaign_id"],
                "report_no": row["report_no"],
                "captured_at": row["created_at"], "pins": pins,
                # R1-02: listing metadata only; the stored report JSON is
                # never read here (a frozen report is one HTML document).
                "count": None, "count_units": "", "format": "html",
                "digest": row["export_digest"] or "",
                "download_path": f"/customer/campaigns/{row['campaign_id']}"
                                 f"/reports/{row['report_no']}/html",
                "exportable": exportable,
                "stale": stale, "stale_reasons": reasons,
                "limitations": [],
            })
    items.sort(key=lambda i: (i["captured_at"], i["kind"], i["id"]))
    _place_withheld_reports(items, withheld_reports, campaign_id, needle)
    plan_withheld, access_withheld = (
        len(list(filter(_report_filter(campaign_id, needle), counted[k])))
        for k in ("plan", "access"))
    from . import citations
    revision = citations._state_digest("archive", getattr(ctx, "policy_identity", ""), items)
    key = [kind, campaign_id, q]
    offset = _decode_archive_cursor(cursor, key, revision) if cursor else 0
    total = len(items)
    page = items[offset:offset + limit]
    next_cursor = _encode_archive_cursor(key, offset + limit, revision) \
        if total > offset + limit else None
    return {"items": page, "total": total, "total_relation": "exact",
            "next_cursor": next_cursor, "archive_revision": revision,
            "plan_withheld": plan_withheld, "access_withheld": access_withheld,
            "exports_withheld": exports_withheld}


def _report_filter(campaign_id, needle: str):
    """The campaign and title filters, as a report row of its campaign
    meets them (its title is the campaign's name). DPC-02: a withheld
    report of a campaign the caller cannot read meets every filter, so
    the counts never answer a guess at its name or id."""
    def keep(row):
        if not row.get("campaign_readable", True):
            return True
        title = row["campaign_name"] or f"campaign-{row['campaign_id']}-report-{row['report_no']}"
        return (campaign_id is None or row["campaign_id"] == campaign_id) \
            and not (needle and needle not in title.casefold())
    return keep


def _place_withheld_reports(items: list, rows: list, campaign_id, needle: str) -> None:
    """ADS-R1-02: each report a source's own policy withholds, as its
    number only ({"kind", "report_no", "rights_withheld"}: no title, date,
    id, link or count), where its number already puts it: right after its
    campaign's report before it, else right before the one after it; a
    campaign with no other report shown puts it last. The campaign and
    title filters apply to it as to its campaign's other reports."""
    keep = _report_filter(campaign_id, needle)
    keys = [(i["campaign_id"], i["report_no"]) if i["kind"] == "report" else None for i in items]
    for row in sorted(filter(keep, rows), key=lambda r: (r["campaign_id"], r["report_no"])):
        cid, n = row["campaign_id"], row["report_no"]
        before = [x for x, k in enumerate(keys) if k and k[0] == cid and k[1] < n]
        after = [x for x, k in enumerate(keys) if k and k[0] == cid and k[1] > n]
        at = before[-1] + 1 if before else after[0] if after else len(items)
        items.insert(at, {"kind": "report", "report_no": n, "rights_withheld": True})
        keys.insert(at, (cid, n))


def _export_staleness(con: sqlite3.Connection,
                      captured: dict, ctx=None) -> tuple[bool, list]:
    """An export is stale when its pinned view moved underneath it.

    The pin is the Query2 research view revision taken at capture
    (customer_query._view_revision), so it is compared with that same
    scheme for the caller's context, never with another revision kind."""
    basis = captured.get("view_revision")
    if not basis:
        return False, []
    try:
        from . import customer_query as _customer_query
        current = _customer_query._view_revision(con, ctx)
    except sqlite3.Error:
        return False, []
    if current != basis:
        return True, ["research view changed since capture"]
    return False, []


def _report_staleness(con: sqlite3.Connection, row: dict,
                      pins: dict) -> tuple[bool, list]:
    """A report is stale when its campaign moved past the frozen pins."""
    live = con.execute("SELECT revision FROM campaigns WHERE id=?",
                       (row["campaign_id"],)).fetchone()
    if live is None:
        return True, ["campaign removed after capture"]
    frozen = pins.get("campaign_revision")
    if frozen is None:
        frozen = row.get("campaign_revision")
    from . import campaigns
    # Archive / Restore changes the status only: not a change (R1-08).
    if frozen is not None and int(live["revision"]) != int(frozen) \
            and not campaigns.status_only_changes(con, row["campaign_id"], frozen, live["revision"]):
        return True, ["campaign changed since freeze"]
    return False, []


def verify_linked_reports(con: sqlite3.Connection, export_id: int, digest: str,
                          captured: dict) -> None:
    """Every frozen report linked to this export still matches its bytes."""
    linked = con.execute("SELECT report_json, export_digest FROM campaign_reports"
                         " WHERE export_id=? AND workspace_id='local'", (export_id,)).fetchall()
    for report in linked:
        json_digest = hashlib.sha256(report["report_json"].encode("utf-8")).hexdigest()
        if digest != report["export_digest"] or \
                captured.get("report_json_digest") != json_digest:
            raise DigestMismatch("linked report integrity mismatch")


def load_export_bytes(con: sqlite3.Connection, export_id: int, ctx=None, *,
                      export: bool = True) -> dict:
    """Serve stored bytes only when the digest still matches.

    Unknown ids read as unknown; a digest mismatch refuses the stored
    bytes instead of serving tampered content (disclosure gate). Releasing
    the bytes rechecks current export permission for every retained
    locator; export=False is for read-permission callers: the archive
    listing and conversion's artifact qualification, which verify an
    entry without releasing it.
    """
    try:
        wanted = int(export_id)
    except (TypeError, ValueError):
        raise ops.NotFound("unknown export")
    row = con.execute("SELECT * FROM exports WHERE id=?", (wanted,)).fetchone()
    if row is None:
        raise ops.NotFound("unknown export")
    stored = dict(row)
    if stored.get("workspace_id", "local") != "local":
        raise ops.NotFound("unknown export")
    payload = stored["payload"]
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if not stored.get("digest") or stored["digest"] != digest:
        raise DigestMismatch("export digest unqualified or mismatched; stored bytes refused")
    from . import citations
    captured = json.loads(stored["captured_json"])
    from .access import AuthorizationError
    try:
        citations.validate_capture(con, captured, ctx, export=export)
    except AuthorizationError as exc:
        if not export:
            raise
        refuse_export(con, captured.get("disclosure") or [], ctx, exc)
    verify_linked_reports(con, wanted, digest, captured)
    ctype = {"csv": "text/csv; charset=utf-8",
             "html": "text/html; charset=utf-8"}.get(
                 stored["format"], "application/json; charset=utf-8")
    return {"export_id": stored["id"], "format": stored["format"],
            "payload": payload, "digest": digest, "content_type": ctype,
            "item_count": stored["item_count"]}

LIMITATIONS = ("Assisted research, not independent evaluation or contractual verification. "
               "Model intervals are not verified ad durations. Audio is not bundled; "
               "remote audio may differ from archived transcripts. Local app links are not shareable.")


def _rows_for_filter(con, filt):
    filt = ops.normalize_filter({**filt, "page": 1, "per_page": ops.PER_PAGE_MAX})
    items = []
    while True:
        result = ops.search_evidence(con, filt)
        items.extend(result["items"])
        if len(items) >= result["total"] or not result["items"]:
            return items
        filt["page"] += 1


def _csv_cell(value):
    text = "" if value is None else str(value)
    if text.lstrip()[:1] in ("=", "+", "-", "@") or text[:1] in ("\t", "\r", "\n"):
        return "'" + text
    return text


def _original(con, item):
    if not item.get("source_version_id"):
        return item["attrs"]
    row = con.execute("SELECT payload FROM source_versions WHERE id=?", (item["source_version_id"],)).fetchone()
    payload = json.loads(row[0])
    if item["source"] == "detector" and isinstance(payload, list):
        return next((c for c in payload if c.get("candidate_id") == item["attrs"].get("candidate_id")), item["attrs"])
    return item["attrs"]


def to_csv(bundle):
    out = io.StringIO()
    columns = ["record_type", "id", "episode", "source", "start_s", "end_s", "judgment",
               "text", "notes", "exported_at", "aggregate_revision", "limitations", "complete_record_json"]
    writer = csv.writer(out)
    writer.writerow(columns)
    records = [("evidence", item) for item in bundle["items"]]
    if bundle.get("campaign"):
        records.insert(0, ("campaign", bundle["campaign"]))
        records.extend(("check", c) for c in bundle["campaign"]["checks"])
    if not records:
        records = [("metadata", {"filter": bundle["filter"]})]
    for kind, record in records:
        review = record.get("review", {})
        values = [kind, record.get("id"), record.get("episode_id"), record.get("source"),
                  record.get("orig_start_s"), record.get("orig_end_s"), review.get("judgment"),
                  record.get("text", ""), review.get("notes", record.get("notes", "")),
                  bundle["exported_at"], bundle["aggregate_rev"], LIMITATIONS,
                  json.dumps(record, ensure_ascii=False)]
        writer.writerow([_csv_cell(value) for value in values])
    return out.getvalue()


def _text(value):
    if value is None:
        return "unknown"
    text = str(value).strip()
    return html.escape(text) if text else "unknown"


def _row(label, value):
    return '<tr><th>' + html.escape(label) + '</th><td>' + _text(value) + '</td></tr>'


def _brief_section(campaign):
    rows = [
        _row("Name", campaign.get("name")),
        _row("Status", campaign.get("status")),
        _row("Brand", campaign.get("brand")),
        _row("Starts", campaign.get("date_from")),
        _row("Ends", campaign.get("date_to")),
        _row("Target shows", ", ".join(campaign.get("target_shows") or []) or None),
        _row("Expected message", campaign.get("message")),
        _row("Expected offer", campaign.get("offer")),
        _row("Notes", campaign.get("notes")),
        _row("Separate manual conclusion", campaign.get("manual_conclusion")),
        _row("Manual reason", campaign.get("manual_reason")),
    ]
    return ('<section><h2>Campaign brief</h2><table>' + ''.join(rows) + '</table>'
            '<p>Synthetic briefs stay labeled and are never contractual verification.</p></section>')


def _evidence_section(items):
    body = ['<section><h2>Attached evidence</h2>']
    if not items:
        body.append('<p>No evidence attached. This is not evidence that the campaign is absent.</p>')
    else:
        body.append('<table><thead><tr><th>Evidence</th><th>Episode</th>'
                    '<th>Interval (s)</th><th>Judgment</th><th>Excerpt</th></tr></thead><tbody>')
        for item in items:
            review = item.get("review") or {}
            body.append('<tr><td>' + _text(item.get("id")) + '</td><td>'
                        + _text(item.get("ep_title") or item.get("episode_id")) + '</td><td>'
                        + _text(item.get("orig_start_s")) + '–' + _text(item.get("orig_end_s"))
                        + '</td><td>' + _text(review.get("judgment")) + '</td><td>'
                        + _text(item.get("text") or item.get("excerpt")) + '</td></tr>')
        body.append('</tbody></table>')
    body.append('</section>')
    return ''.join(body)


def _checks_section(checks):
    body = ['<section><h2>Checks and manual assessments</h2>']
    if not checks:
        body.append('<p>No checks yet. Missing evidence stays unresolved.</p>')
    for check in checks or []:
        result = check.get("result") or {}
        try:
            params = json.dumps(check.get("params"), ensure_ascii=False)
        except (TypeError, ValueError):
            params = str(check.get("params"))
        body.append('<div><h3>' + _text(check.get("check_type")) + '</h3>'
                    '<p>Computed: ' + _text(result.get("outcome")) + ' — '
                    + _text(result.get("reason")) + '</p>'
                    '<p>Evidence: ' + _text(check.get("evidence_id"))
                    + ' · Parameters: ' + html.escape(params) + '</p>'
                    '<p>Manual: ' + _text(check.get("manual_verdict"))
                    + ' ' + _text(check.get("manual_reason")) + '</p></div>')
    body.append('</section>')
    return ''.join(body)


def _placements_section(entries):
    body = ['<section><h2>Expected placements and evaluations</h2>']
    if not entries:
        body.append('<p>No expected placements yet.</p>')
    for entry in entries or []:
        placement = entry.get("placement") or {}
        try:
            detail = json.dumps({"scope": placement.get("scope"),
                                 "show_id": placement.get("show_id"),
                                 "episode_id": placement.get("episode_id"),
                                 "date_from": placement.get("date_from"),
                                 "date_to": placement.get("date_to"),
                                 "timezone": placement.get("timezone"),
                                 "expected_count": placement.get("expected_count"),
                                 "requirements": placement.get("requirements"),
                                 "provenance": placement.get("provenance")},
                                ensure_ascii=False)
        except (TypeError, ValueError):
            detail = "{}"
        frozen = entry.get("latest_evaluation")
        if frozen:
            try:
                reasons = json.loads(frozen.get("reasons") or "[]")
            except (TypeError, ValueError):
                reasons = [frozen.get("reasons")]
            try:
                eligible = (json.loads(frozen.get("evidence_pins") or "{}").get("eligible") or [])
            except (TypeError, ValueError):
                eligible = []
            frozen_text = ('<p>Latest frozen evaluation: ' + _text(frozen.get("outcome"))
                           + ' (' + _text(frozen.get("created_at"))
                           + ', campaign revision ' + _text(frozen.get("campaign_revision")) + ')</p>'
                           '<p>Eligible occurrences: '
                           + (', '.join(_text(oid) for oid in eligible) or 'none') + '</p><ul>'
                           + ''.join('<li>' + _text(reason) + '</li>' for reason in reasons) + '</ul>')
        else:
            frozen_text = '<p>No frozen evaluation yet.</p>'
        body.append('<div><h3>' + _text(placement.get("id")) + '</h3>'
                    '<p>' + html.escape(detail) + '</p>' + frozen_text + '</div>')
    body.append('</section>')
    return ''.join(body)


def _campaign_html(bundle, title, campaign):
    # Standalone share pack: readable sections only. No local app links
    # (#/review/...) and no audio payload — the text carries the evidence.
    parts = [_brief_section(campaign),
             _evidence_section(bundle["items"]),
             _checks_section(campaign.get("checks")),
             _placements_section(bundle.get("placements")),
             '<section><h2>Limitations</h2><p>' + html.escape(LIMITATIONS) + '</p></section>']
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>' + html.escape(title)
            + '</title><style>' + campaigns.REPORT_PRINT_CSS + '</style><h1>'
            + html.escape(title) + '</h1><p>' + html.escape(bundle["exported_at"])
            + '</p><p>Workspace revision ' + str(bundle["aggregate_rev"]) + '</p>'
            + ''.join(parts) + '</html>')


def to_html(bundle, title):
    def pre(value):
        return '<pre>' + html.escape(json.dumps(value, indent=2, ensure_ascii=False)) + '</pre>'
    campaign = bundle.get("campaign")
    if campaign and bundle.get("placements") is not None:
        return _campaign_html(bundle, title, campaign)
    parts = []
    if campaign:
        parts.append('<section><h2>Campaign brief, checks, and manual assessments</h2>' + pre(campaign) + '</section>')
    for item in bundle["items"]:
        parts.append('<section><h2>' + html.escape(item.get("ep_title") or item["id"]) + '</h2>'
                     + '<p>' + html.escape(item["text"]) + '</p>' + pre(item) + '</section>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>' + html.escape(title)
            + '</title><style>' + campaigns.REPORT_PRINT_CSS + '</style><h1>'
            + html.escape(title) + '</h1><p>' + html.escape(bundle["exported_at"] + '. ' + LIMITATIONS)
            + '</p><p>Workspace revision ' + str(bundle["aggregate_rev"]) + '</p>' + ''.join(parts) + '</html>')


@db.atomic
def build_export(con, filt, fmt, explicit_ids=None, title="EarWitness export", actor="owner", campaign_id=None):
    if not isinstance(filt, dict):
        raise ops.BadInput("filter must be an object")
    if fmt not in ("csv", "json", "html"):
        raise ops.BadInput("format must be csv, json, or html")
    campaign = ops.campaign_detail(con, campaign_id) if campaign_id is not None else None
    if campaign_id is not None and campaign is None:
        raise ops.BadInput("unknown campaign")
    if campaign:
        explicit_ids = [e["id"] for e in campaign["evidence"]]
    if explicit_ids is not None:
        if not isinstance(explicit_ids, list) or len(explicit_ids) > 2000 or any(not isinstance(eid, str) for eid in explicit_ids):
            raise ops.BadInput("evidence_ids must be a list of at most 2000 string IDs")
        ids = list(dict.fromkeys(explicit_ids))
    else:
        ids = [item["id"] for item in _rows_for_filter(con, filt)]
    items = []
    for eid in ids:
        item = ops.evidence_detail(con, eid)
        if item is None:
            raise ops.BadInput(f"unknown evidence: {eid}")
        item["text"] = ops.evidence_text(con, item)
        item["original_record"] = _original(con, item)
        # Operator entrypoint: review lives under /app/operator/ since the
        # customer/operator split; stored historical exports keep their bytes.
        item["app_link"] = "/app/operator/#/review/" + eid
        items.append(item)
    bundle = {"filter": ops.normalize_filter(filt), "filter_version": ops.FILTER_VERSION,
              "aggregate_rev": db.aggregate_revision(con), "exported_at": db.utcnow(),
              "limitations": LIMITATIONS, "items": items, "campaign": campaign}
    if campaign and fmt == "html":
        # Share pack only: placements + latest frozen history via the
        # existing campaigns.py helpers (no new routes). CSV/JSON bundles
        # stay untouched. Local app links are stripped — the pack must be
        # readable standalone, without local #/review/ hrefs.
        entries = []
        for placement in campaigns.list_expected(con, campaign_id):
            history = campaigns.evaluation_history(con, placement["id"])
            entries.append({"placement": placement,
                            "latest_evaluation": dict(history[-1]) if history else None})
        bundle["placements"] = entries
        for item in items:
            item.pop("app_link", None)
    payload = to_csv(bundle) if fmt == "csv" else to_html(bundle, title) if fmt == "html" else json.dumps(bundle, indent=2, ensure_ascii=False)
    cur = con.execute("INSERT INTO exports (created_at,format,filter,aggregate_rev,item_count,payload)"
                      " VALUES (?,?,?,?,?,?)", (bundle["exported_at"], fmt, json.dumps(bundle["filter"]),
                                               bundle["aggregate_rev"], len(items), payload))
    if "digest" in _export_columns(con):
        con.execute("UPDATE exports SET digest=?, title=? WHERE id=?",
                    (hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                     title[:160], cur.lastrowid))
    rev = db.bump_revision(con)
    db.audit(con, actor, "export", "export", str(cur.lastrowid), {"format": fmt, "items": len(items)}, rev)
    return {"export_id": cur.lastrowid, "format": fmt, "item_count": len(items),
            "aggregate_rev": bundle["aggregate_rev"], "payload": payload}
