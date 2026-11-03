"""P13 coverage-aware campaign placement tracking (disjoint leaf).

Extends current campaigns (briefs/checks in ops.py); never replaces
them. Expected placements + deterministic allocation + seven-state
evaluation with precedence, staleness, and frozen evidence packages.
Reuses the existing typed checks in ops.evaluate_check without
promoting unknowns.

Rules honored:
- Expected placement: show + optional exact episode + inclusive date
  range with IANA timezone + positive expected count + optional
  duration/position/read-type/message/offer requirements + allocation
  priority + requirement provenance (real-brief ref or explicit
  synthetic flag).
- Known feed instants converted to requirement TZ before inclusive
  matching; uncertain date precision -> ambiguous (never
  midnight/UTC invention).
- Eligible = resolved occurrence + current reviewed_ad + reviewed
  advertiser attribution + all specified checks; unverified required
  fields = unknown (never satisfied).
- Precedence: ambiguous -> supported -> pending -> incomplete_coverage
  -> contradicted -> unreviewed_detections_in_scope (complete coverage,
  reads in scope still waiting for review) -> no_observation_in_covered_sources.
  Insufficient-but-nonzero after close with complete coverage =
  ambiguous + INSUFFICIENT_SUPPORTED_COUNT.
- One occurrence once per campaign unless explicit owner reuse
  override + rationale. Allocation order: owner priority ->
  episode-scoped before show-window -> earliest window end/start ->
  stable expectation ID; occurrences by publication instant ->
  verified interval start -> stable ID. Repeated DAI fetches never
  count as multiple placements.
- Position/duration need matched source + human-verified
  alignment/bounds + full-episode duration (source_matched !=
  verified; partial clips/last-timestamps insufficient).
- Brief/source/attribution/correspondence/review changes -> stale +
  recompute; previous snapshots retained as history.
- Manual assessments stay separate (author/reason/evidence).
- Synthetic briefs stay labeled (never contractual verification).
"""
from __future__ import annotations

import collections
import copy
import functools
import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db as _db
from . import ops as _ops


def _model_names_off(fn):
    """PODS: model rows are display-only. Campaign verdicts, outcomes,
    coverage, drafts and frozen reports run with model names off, whatever
    customer display endpoint called them. customer is imported lazily
    (import cycle)."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from . import customer as _cust
        with _cust.model_names(False):
            return fn(*args, **kwargs)
    return wrapped

OUTCOMES = ("ambiguous", "supported", "pending", "incomplete_coverage",
            "contradicted", "no_observation_in_covered_sources",
            "unreviewed_detections_in_scope")
# Complete coverage and no eligible occurrence: "no match" only when no
# unreviewed detection sits in the placement's scope (hard rule 1).
NO_MATCH_OUTCOMES = ("no_observation_in_covered_sources",
                     "unreviewed_detections_in_scope")
INSUFFICIENT_SUPPORTED_COUNT = "INSUFFICIENT_SUPPORTED_COUNT"


class Reason(str):
    """An evaluation reason: the human text, unchanged, plus a stable
    machine ``code`` and the optional requirement/check ``field`` it is
    about. Behaves as the plain string everywhere (equality, hashing,
    json.dumps), so frozen digests and stored reasons are byte-identical;
    the codes ride alongside for display."""
    __slots__ = ("code", "field")

    def __new__(cls, text, code="other", field=None):
        obj = super().__new__(cls, text)
        obj.code = code
        obj.field = field
        return obj

    def __reduce__(self):
        return (Reason, (str(self), self.code, self.field))


def _prefixed(prefix, reason):
    """f"{prefix}: {reason}" keeping the reason's code and field."""
    return Reason(f"{prefix}: {reason}", getattr(reason, "code", "other"),
                  getattr(reason, "field", None))


def reason_code_rows(reasons) -> list:
    """Display codes for a reasons list: same order, same length."""
    return [{"code": getattr(r, "code", "other"),
             "field": getattr(r, "field", None)} for r in (reasons or [])]
SYNTHETIC_FLAG = "synthetic"


def _now_iso() -> str:
    return _db.utcnow()


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise _ops.BadInput(f"unknown IANA timezone: {name!r}")


def _to_tz_date(pub_instant_iso: str, tzname: str) -> str:
    """Convert a known feed instant to the requirement TZ; return YYYY-MM-DD."""
    dt = datetime.fromisoformat(str(pub_instant_iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("naive instant")
    return dt.astimezone(_tz(tzname)).date().isoformat()


def _definition_problems(con, camp):
    """Reuse conversion classification without writing or blessing legacy data."""
    from . import conversion
    entries = [{"kind": "campaign", "original": {"mode": camp.get("mode"),
               "brief": camp.get("brief_ref_json") or "{}"}}]
    entries.extend({"kind": "requirements", "original": {
        "requirements": row["requirements"], "provenance": row["provenance"]}}
        for row in con.execute("SELECT requirements,provenance FROM expected_placements WHERE campaign_id=?",
                               (camp["id"],)))
    notes = [note for entry in entries for classified in [conversion._classify(con, entry)]
             if classified["disposition"] == "needs_review" for note in classified["notes"]]
    return list(dict.fromkeys(notes))


def validate_provenance(con: sqlite3.Connection, provenance: dict) -> dict:
    prov = dict(provenance or {})
    reference = prov.get("brief_ref")
    if isinstance(reference, dict):
        from . import briefs
        if set(reference) == {"kind", "brief_id", "version"}:
            projection = briefs.resolve_ref(con, reference)
        else:
            if not {"brief_id", "version", "digest"} <= set(reference):
                raise _ops.BadInput("registered brief projection is incomplete")
            projection = briefs.resolve_ref(con, {"kind": "supplied_brief",
                "brief_id": reference["brief_id"], "version": reference["version"]})
            if projection["digest"] != reference["digest"]:
                raise _ops.BadInput("registered brief projection digest differs")
        prov["brief_ref"] = projection
        prov[SYNTHETIC_FLAG] = projection["qualification"]["synthetic"]
        prov["contractual"] = False
        return prov
    if prov.get(SYNTHETIC_FLAG) is True or str(prov.get("brief_type") or "") == "synthetic":
        prov[SYNTHETIC_FLAG] = True
        prov["contractual"] = False
        return prov
    if not prov.get("brief_ref"):
        raise _ops.BadInput("requirement provenance needs a real-brief ref"
                            " or an explicit synthetic flag")
    prov["contractual"] = False  # research outcome, never auto legal verdict
    return prov


@_db.atomic
def save_expected_placement(con: sqlite3.Connection, campaign_id: int,
                            scope: str = "show_window", show_id: str = "",
                            episode_id: str | None = None,
                            date_from: str | None = None,
                            date_to: str | None = None,
                            timezone_name: str = "UTC",
                            expected_count: int = 1,
                            requirements: dict | None = None,
                            provenance: dict | None = None,
                            priority: int = 0, actor: str = "owner",
                            expected_revision=None) -> dict:
    if scope not in ("episode", "show_window"):
        raise _ops.BadInput(f"unknown placement scope: {scope!r}")
    if scope == "episode" and not episode_id:
        raise _ops.BadInput("episode scope requires an exact episode id")
    if not isinstance(expected_count, int) or isinstance(expected_count, bool) \
            or expected_count <= 0:
        raise _ops.BadInput("expected_count must be a positive integer")
    if date_from and date_to and date_to < date_from:
        raise _ops.BadInput("date_to must not precede date_from")
    _tz(timezone_name)  # validates IANA name
    prov = validate_provenance(con, provenance or {})
    reqs = dict(requirements or {})
    for key in ("duration_s", "position_s"):
        if key in reqs:
            bounds = reqs[key]
            if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2):
                raise _ops.BadInput(f"{key} requirement must be [min_s, max_s]")
    camp = con.execute("SELECT revision FROM campaigns WHERE id=?",
                       (campaign_id,)).fetchone()
    if camp is None:
        raise _ops.BadInput("unknown campaign")
    _refuse_archived(con, campaign_id)
    pid = _new_id("exp")
    con.execute("INSERT INTO expected_placements (id, workspace_id, campaign_id, scope,"
                " show_id, episode_id, date_from, date_to, timezone, expected_count,"
                " requirements, provenance, revision, updated_at)"
                " VALUES (?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (pid, campaign_id, scope, str(show_id or ""), episode_id,
                 date_from, date_to, timezone_name, expected_count,
                 json.dumps({**reqs, "priority": int(priority)}),
                 json.dumps(prov), _now_iso()))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "placement_save", "expected_placement", pid,
              {"campaign": campaign_id, "synthetic": bool(prov.get(SYNTHETIC_FLAG))},
              rev)
    return {"placement_id": pid, "revision": 1, "synthetic": bool(prov.get(SYNTHETIC_FLAG)),
            "aggregate_rev": rev}


@_db.atomic
def update_expected_placement(con: sqlite3.Connection, campaign_id: int, placement_id: str,
                              body: dict, expected_revision, actor: str = "owner") -> dict:
    """Edit an existing placement with CID/PID ownership check and revision.

    Typed fields only (no JSON editor): scope, show_id, episode_id,
    date_from/date_to, timezone, expected_count, requirements, provenance,
    priority. Requirements/provenance replace wholesale when supplied.
    """
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    if not isinstance(body, dict):
        raise _ops.BadInput("placement update must be an object")
    row = con.execute("SELECT * FROM expected_placements WHERE id=? AND campaign_id=?",
                      (placement_id, campaign_id)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown placement for this campaign")
    current = dict(row)
    if int(expected_revision) != current["revision"]:
        raise _ops.Conflict(current["revision"])
    _refuse_archived(con, campaign_id)
    allowed = {"scope", "show_id", "episode_id", "date_from", "date_to",
               "timezone", "expected_count", "requirements", "provenance", "priority"}
    unknown = set(body) - allowed
    if unknown:
        raise _ops.BadInput(f"unknown placement fields: {sorted(unknown)}")
    updated = dict(current)
    if "scope" in body:
        if body["scope"] not in ("episode", "show_window"):
            raise _ops.BadInput("unknown placement scope")
        updated["scope"] = body["scope"]
    if "show_id" in body:
        if not isinstance(body["show_id"], str):
            raise _ops.BadInput("show_id must be a string")
        updated["show_id"] = body["show_id"]
    if "episode_id" in body:
        if body["episode_id"] is not None and not isinstance(body["episode_id"], str):
            raise _ops.BadInput("episode_id must be a string or null")
        updated["episode_id"] = body["episode_id"]
    if updated["scope"] == "episode" and not updated["episode_id"]:
        raise _ops.BadInput("episode scope requires an exact episode id")
    for key in ("date_from", "date_to"):
        if key in body:
            if body[key] not in (None, "") and not isinstance(body[key], str):
                raise _ops.BadInput(f"{key} must be an ISO date, empty, or null")
            updated[key] = body[key] or None
    if updated["date_from"] and updated["date_to"] \
            and updated["date_to"] < updated["date_from"]:
        raise _ops.BadInput("date_to must not precede date_from")
    if "timezone" in body:
        _tz(body["timezone"])
        updated["timezone"] = body["timezone"]
    if "expected_count" in body:
        if not isinstance(body["expected_count"], int) \
                or isinstance(body["expected_count"], bool) \
                or body["expected_count"] <= 0:
            raise _ops.BadInput("expected_count must be a positive integer")
        updated["expected_count"] = body["expected_count"]
    if "requirements" in body:
        if not isinstance(body["requirements"], dict):
            raise _ops.BadInput("requirements must be an object")
        reqs = dict(body["requirements"])
        for key in ("duration_s", "position_s"):
            if key in reqs and reqs[key] is not None:
                bounds = reqs[key]
                if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                    raise _ops.BadInput(f"{key} requirement must be [min_s, max_s]")
        priority = reqs.pop("priority", None)
        if priority is not None:
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise _ops.BadInput("priority must be an integer")
        else:
            try:
                priority = (json.loads(current["requirements"] or "{}") or {}
                            ).get("priority", 0)
            except (ValueError, TypeError):
                priority = 0
        reqs["priority"] = int(priority)
        updated["requirements"] = json.dumps(reqs)
    if "provenance" in body:
        if not isinstance(body["provenance"], dict):
            raise _ops.BadInput("provenance must be an object")
        updated["provenance"] = json.dumps(validate_provenance(con, body["provenance"]))
    if "priority" in body:
        if not isinstance(body["priority"], int) or isinstance(body["priority"], bool):
            raise _ops.BadInput("priority must be an integer")
        try:
            reqs = json.loads(updated["requirements"] if isinstance(
                updated["requirements"], str) else json.dumps(updated["requirements"]))
        except (ValueError, TypeError):
            reqs = {}
        reqs["priority"] = int(body["priority"])
        updated["requirements"] = json.dumps(reqs)
    con.execute("UPDATE expected_placements SET scope=?, show_id=?, episode_id=?,"
                " date_from=?, date_to=?, timezone=?, expected_count=?, requirements=?,"
                " provenance=?, revision=revision+1, updated_at=? WHERE id=? AND campaign_id=?",
                (updated["scope"], updated["show_id"], updated["episode_id"],
                 updated["date_from"], updated["date_to"], updated["timezone"],
                 updated["expected_count"], updated["requirements"]
                 if isinstance(updated["requirements"], str)
                 else json.dumps(updated["requirements"]),
                 updated["provenance"] if isinstance(updated["provenance"], str)
                 else json.dumps(updated["provenance"]),
                 _now_iso(), placement_id, campaign_id))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "placement_update", "expected_placement", placement_id,
              {"campaign": campaign_id}, rev)
    return {"placement_id": placement_id, "revision": current["revision"] + 1,
            "aggregate_rev": rev}


def placement_binding(provenance) -> str:
    """Explicit placement binding; operator rows default to eligible.

    eligible placements allocate through the current allocator;
    citation_only placements are planned and never allocate. The
    binding is explicit at customer creation and stored server-side in
    the provenance record; operator rows without one default to the
    current allocator. Unknown bindings are rejected, never coerced.
    """
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance or "{}")
        except (ValueError, TypeError):
            provenance = {}
    if not isinstance(provenance, dict):
        provenance = {}
    binding = provenance.get("binding", "eligible")
    if binding not in PLACEMENT_BINDINGS:
        raise _ops.BadInput(f"invalid placement binding: {binding!r}")
    return binding


def _stored_json_object(raw) -> dict:
    """Stored JSON object; undecodable or non-object legacy values read as empty.

    Legacy rows can hold free text. Conversion classification reports them
    as unreadable in plain words; readers here must not raise decode errors.
    """
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def list_expected(con: sqlite3.Connection, campaign_id: int) -> list:
    rows = con.execute("SELECT * FROM expected_placements WHERE campaign_id=?"
                       " ORDER BY id", (campaign_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["requirements"] = _stored_json_object(d.get("requirements"))
        d["provenance"] = _stored_json_object(d.get("provenance"))
        d["binding"] = placement_binding(d["provenance"])
        out.append(d)
    return out


# -- eligibility -----------------------------------------------------------

def _occurrence_eligible(occ: dict, placement: dict, checks_run=None) -> tuple[bool, list]:
    """Eligible = resolved occurrence + current reviewed_ad + reviewed
    advertiser attribution + every specified check. Unverified required
    fields are unknown (never satisfied). Returns (eligible, gaps)."""
    gaps = []
    if occ.get("correspondence") not in ("confirmed", "ambiguous-separate-ok") \
            or occ.get("ambiguous"):
        gaps.append(Reason("correspondence ambiguous", "correspondence_ambiguous")
                    if occ.get("correspondence") == "ambiguous"
                    else Reason("unresolved correspondence",
                                "unresolved_correspondence"))
    if occ.get("review_status") != "reviewed_ad":
        gaps.append(Reason("not currently reviewed_ad", "not_reviewed_ad"))
    if not occ.get("advertiser_confirmed"):
        gaps.append(Reason("advertiser attribution unconfirmed",
                           "attribution_unconfirmed"))
    reqs = placement.get("requirements") or {}
    # Duration/position prerequisites: matched source + human-verified
    # alignment/bounds + full-episode duration. source_matched != verified;
    # partial clips / last-timestamps are insufficient.
    for key in ("duration_s", "position_s"):
        if key in reqs and reqs[key] is not None:
            if not occ.get("source_matched"):
                gaps.append(Reason(f"{key}: source identity unmatched",
                                   "requirement_unverified", key))
            elif not occ.get("alignment_verified"):
                gaps.append(Reason(f"{key}: audio alignment unverified",
                                   "requirement_unverified", key))
            elif not occ.get("bounds_verified"):
                gaps.append(Reason(f"{key}: boundaries unverified",
                                   "requirement_unverified", key))
            elif not occ.get("full_episode_duration_s"):
                gaps.append(Reason(f"{key}: full-episode duration unavailable"
                                   " (partial clips/last-timestamps insufficient)",
                                   "requirement_unverified", key))
            else:
                val = occ.get("duration_s") if key == "duration_s" else occ.get("position_s")
                if val is None:
                    gaps.append(Reason(f"{key}: unverified required field is unknown",
                                       "requirement_unverified", key))
                else:
                    lo, hi = reqs[key]
                    if not ((lo is None or lo <= val) and (hi is None or val <= hi)):
                        gaps.append(Reason(f"{key}: value {val} outside [{lo}, {hi}]",
                                           "requirement_mismatch", key))
    for key in ("read_type", "message", "offer", "code"):
        if key in reqs and reqs[key] not in (None, ""):
            got = (occ.get("fields") or {}).get(key)
            verified = key in (occ.get("verified_fields") or []) \
                or bool(occ.get("human_verified_copy"))
            # Missing/unverified copy is unknown before any string
            # comparison, matching the comparison matrix: no copy-field
            # verification authority exists, so absent or unverified
            # observed copy is never a mismatch. Only a verified
            # conflicting value mismatches; copy is never invented.
            if got in (None, ""):
                gaps.append(Reason(f"{key}: unverified required field is unknown",
                                       "requirement_unverified", key))
            elif not verified:
                gaps.append(Reason(f"{key}: unverified required field is unknown",
                                       "requirement_unverified", key))
            elif got != reqs[key]:
                gaps.append(Reason(f"{key}: mismatch",
                                       "requirement_mismatch", key))
    if checks_run:
        for name, outcome in checks_run.items():
            if outcome == "unknown":
                gaps.append(Reason(f"check {name}: unknown (never satisfied)",
                                   "check_unknown", name))
            elif outcome == "fail":
                gaps.append(Reason(f"check {name}: fail", "check_failed", name))
    return (not gaps, gaps)


def _in_window(occ: dict, placement: dict) -> tuple[bool, str | None]:
    """Inclusive date matching in the requirement TZ.

    Returns (included, ambiguity_reason). Unknown/uncertain date
    precision that could change inclusion -> ambiguous, never
    midnight/UTC invention.
    """
    tzname = placement.get("timezone") or "UTC"
    date_from = placement.get("date_from")
    date_to = placement.get("date_to")
    if placement.get("scope") == "episode":
        if occ.get("episode_id") != placement.get("episode_id"):
            return False, None
        return True, None
    if occ.get("date_precision") in ("unknown", "uncertain", "date_only_uncertain"):
        return False, Reason("uncertain date precision", "date_uncertain")
    pub = occ.get("published_at") or occ.get("pub_date")
    if not pub:
        if date_from or date_to:
            return False, Reason("unknown publication date with bounded window",
                         "date_uncertain")
        return True, None
    try:
        if len(str(pub)) == 10 and ("T" not in str(pub)):
            # Date-only known instant: only usable when the window fully
            # contains the whole date regardless of TZ shift; else ambiguous.
            if occ.get("date_precision", "date") != "date":
                return False, Reason("uncertain date precision", "date_uncertain")
            from . import customer_contracts as _contracts
            day = _contracts.publication_day(str(pub))
            if day is None:
                # A malformed date is unknown, never compared as text.
                return False, Reason("malformed publication date", "date_uncertain")
            if date_from and day < date_from:
                return False, None
            if date_to and day > date_to:
                return False, None
            # Borderline equality with a non-UTC TZ could shift the day.
            if tzname != "UTC" and day in (date_from or "", date_to or ""):
                return False, Reason("uncertain date precision at TZ boundary",
                                 "date_uncertain")
            return True, None
        day = _to_tz_date(str(pub), tzname)
    except (ValueError, TypeError):
        return False, Reason("uncertain date precision", "date_uncertain")
    if date_from and day < date_from:
        return False, None
    if date_to and day > date_to:
        return False, None
    return True, None


def occurrence_sort_key(occ: dict) -> tuple:
    return (str(occ.get("published_at") or occ.get("pub_date") or ""),
            float(occ.get("verified_start_s")
                  if occ.get("verified_start_s") is not None else 1e18),
            str(occ.get("id") or ""))


# -- evaluation ------------------------------------------------------------

def _ad_of(occ: dict):
    """The ad an occurrence belongs to (decision 1: placement checks count
    distinct ads). An occurrence with no ad_id is an ad of its own."""
    return occ.get("ad_id") or ("occurrence", occ.get("id"))


def evaluate_one(placement: dict, occurrences: list, coverage: dict,
                 now_iso: str, checks_run=None) -> dict:
    """Seven-state evaluation with precedence for one expected placement.
    expected_count counts distinct ads (_ad_of): several eligible pieces of
    evidence of one ad are one delivery; each stays in eligible_ids."""
    reasons: list = []
    ambiguities: list = []
    eligible: list = []
    seen_eligible: set = set()
    for occ in (occurrences or []):
        included, amb = _in_window(occ, placement)
        if amb:
            ambiguities.append(_prefixed(occ.get("id"), amb))
            continue
        if not included:
            continue
        # One persisted occurrence attached through several refs is one
        # delivery, never several: count each resolved identity once.
        # Gaps from every record still surface as reasons below.
        ok, gaps = _occurrence_eligible(occ, placement, checks_run)
        if ok:
            if occ.get("id") in seen_eligible:
                reasons.append(Reason(
                    f"{occ.get('id')}: same occurrence attached"
                    " through another reference counts once",
                    "duplicate_reference"))
            else:
                seen_eligible.add(occ.get("id"))
                eligible.append(occ)
        else:
            reasons.extend(_prefixed(occ.get("id"), g) for g in gaps)
        if occ.get("correspondence") == "ambiguous":
            ambiguities.append(Reason(f"{occ.get('id')}: correspondence ambiguous",
                                     "correspondence_ambiguous"))
        if occ.get("has_unreviewed_proposal"):
            ambiguities.append(Reason(f"{occ.get('id')}: relevant unreviewed proposal",
                                     "unreviewed_proposal"))
    expected = int(placement.get("expected_count") or 1)
    window_open = placement.get("scope") == "show_window" and not placement.get("date_to")
    if placement.get("date_to"):
        try:
            window_open = str(placement["date_to"]) >= (now_iso or "")[:10]
        except TypeError:
            window_open = False
    # Omitted coverage is unknown, never complete.
    coverage_state = (coverage or {}).get("state", "unknown")
    missing = (coverage or {}).get("missing") or []
    failed = (coverage or {}).get("failed") or []
    contradicted = list((coverage or {}).get("contradicted_by") or [])
    if not contradicted and coverage_state == "complete":
        # _placement_coverage never supplies contradicted_by, which made
        # contradicted unreachable through real drafts. Derive it from
        # eligible authority with known verified conflicting requirements:
        # linked occurrence, current ad review, and verified bounds/copy
        # that observably violate the placement. Unknowns never promote.
        contradicted = [
            o.get("evidence_id") for o in (occurrences or [])
            if o.get("evidence_id")
            and _verified_requirement_conflict(o, placement)]
    if ambiguities:
        outcome = "ambiguous"
        reasons = ambiguities + reasons
    elif len({_ad_of(o) for o in eligible}) >= expected:
        outcome = "supported"
        if missing or failed:
            reasons.append(Reason("supported with remaining source gaps disclosed",
                                  "supported_with_gaps"))
    elif window_open and not (missing or failed):
        outcome = "pending"
    elif missing or failed:
        # incomplete coverage outranks no-match, unless ambiguity already won.
        # Gaps read as episode titles; exact ids travel separately in
        # coverage_gap_ids for provenance details, never as primary text.
        outcome = "incomplete_coverage"
        labels = (coverage or {}).get("episode_labels") or {}
        reasons = [Reason(f"missing transcripts: "
                          f"{[labels.get(e, e) for e in missing]}",
                          "missing_transcripts" if missing
                          else "no_missing_transcripts"),
                   Reason(f"failed fetches: "
                          f"{[labels.get(e, e) for e in failed]}",
                          "failed_fetches" if failed
                          else "no_failed_fetches")] + reasons
    elif len(eligible) > 0 and not window_open and coverage_state == "complete":
        outcome = "ambiguous"
        reasons = [Reason(INSUFFICIENT_SUPPORTED_COUNT,
                           "insufficient_count")] + reasons
    elif contradicted and coverage_state == "complete":
        outcome = "contradicted"
        reasons = [Reason(f"contradicted by verified evidence: {contradicted}",
                          "contradicted")] + reasons
    elif coverage_state == "complete" and (coverage or {}).get("waiting_for_review"):
        # The count itself stays out of the reason (and so out of the
        # allocation pin): only a change of outcome stales a report (R1-21).
        outcome = "unreviewed_detections_in_scope"
        reasons = [Reason("no reviewed match yet; reads in scope wait for review",
                          "unreviewed_detections_in_scope")] + reasons
    elif coverage_state == "complete":
        outcome = "no_observation_in_covered_sources"
    else:
        outcome = "incomplete_coverage"
    return {"placement_id": placement.get("id"), "outcome": outcome,
            "reasons": reasons, "eligible_ids": sorted(o.get("id") for o in eligible),
            "coverage_state": coverage_state,
            "coverage_gap_ids": {"missing": list(missing),
                                 "failed": list(failed)}}


def _verified_requirement_conflict(occ: dict, placement: dict) -> bool:
    """Known verified conflict between one occurrence and one placement.

    Gates are eligible authority (linked occurrence, current ad review);
    the conflict itself must be observed and verified: timing outside a
    required range with verified bounds, or a required copy value that is
    human-verified yet different. Missing observed values never promote
    an unknown into a contradiction.
    """
    if occ.get("correspondence") != "confirmed" \
            or occ.get("review_status") != "reviewed_ad" \
            or not occ.get("advertiser_confirmed") or not occ.get("source_matched"):
        return False
    reqs = placement.get("requirements") or {}
    fields = occ.get("fields") or {}
    if occ.get("timing_verified"):
        for key in ("duration_s", "position_s"):
            req = reqs.get(key)
            got = fields.get(key)
            if got is None:
                got = occ.get(key)
            if (isinstance(req, (list, tuple)) and len(req) == 2
                    and isinstance(got, (int, float))
                    and not ((req[0] is None or req[0] <= got) and
                             (req[1] is None or got <= req[1]))):
                return True
    verified_fields = set(occ.get("verified_fields") or ())
    for key in ("message", "offer", "code"):
        if (key in verified_fields and key in reqs and reqs[key] not in (None, "")
                and fields.get(key) not in (None, "")
                and fields[key] != reqs[key]):
            return True
    return False


def _validate_reuse_overrides(placements: list, occurrences: list,
                              reuse_overrides: dict | None,
                              checks_run=None) -> tuple[dict, dict]:
    """Validate each explicit reuse pair BEFORE evaluation.

    An override authorizes only its recorded (occurrence, placement) pair
    with a nonempty rationale, and only when the occurrence exists and
    satisfies that target's eligibility and window scope. Invalid pairs
    (unknown occurrence, unknown placement, empty rationale, ineligible
    or out-of-window occurrence) are rejected and reported; they never
    enter any eligible set. Returns (valid_by_placement, invalid).
    """
    by_id = {p.get("id"): p for p in placements}
    occ_by_id = {o.get("id"): o for o in (occurrences or [])}
    valid: dict[str, list] = {}
    invalid: dict = {}
    for oid, rule in (reuse_overrides or {}).items():
        target_id = (rule or {}).get("placement_id") if isinstance(rule, dict) else None
        rationale = (rule or {}).get("rationale") if isinstance(rule, dict) else ""
        placement = by_id.get(target_id)
        occ = occ_by_id.get(oid)
        if placement is None or occ is None or not rationale:
            invalid[oid] = {"placement_id": target_id,
                            "reason": "unknown placement, unknown occurrence,"
                                      " or empty rationale"}
            continue
        ok, gaps = _occurrence_eligible(occ, placement, checks_run)
        included, amb = _in_window(occ, placement)
        if not ok or not included or amb:
            invalid[oid] = {"placement_id": target_id,
                            "reason": "; ".join(gaps) if gaps
                            else (amb or "outside placement window")}
            continue
        valid.setdefault(target_id, []).append(oid)
    return valid, invalid


def allocate_campaign(placements: list, occurrences: list, coverage: dict,
                      now_iso: str, reuse_overrides: dict | None = None,
                      checks_run=None, eligible_by_placement: dict | None = None,
                      coverage_by_placement: dict | None = None) -> dict:
    """Deterministic allocation across a campaign.

    Order placements by owner priority -> episode-scoped before
    show-window -> earliest window end/start -> stable ID. Order
    occurrences by publication instant -> verified interval start ->
    stable ID. Each occurrence used once; an explicit owner reuse
    override + rationale permits one recorded pair to count twice, and
    only for that pair. Original reservations are retained: reuse never
    moves an occurrence, so a third placement cannot inherit another
    pair's permission. Exposes eligible-but-allocated competition
    instead of declaring advertiser failure. A supported placement claims
    its first expected_count ads, every piece of evidence of each, so one
    ad never supports two placements (decision 1). Per-placement eligible
    attachment sets and coverage may be passed separately; placement
    scopes without their own entry fall back to the campaign-wide pool.
    """
    ordered = sorted(placements, key=lambda p: (
        -int((p.get("requirements") or {}).get("priority", 0)),
        0 if p.get("scope") == "episode" else 1,
        str(p.get("date_to") or "9999"), str(p.get("date_from") or ""),
        str(p.get("id") or "")))
    valid_reuse, invalid_reuse = _validate_reuse_overrides(
        placements, occurrences, reuse_overrides, checks_run)
    used: dict = {}
    used_ads: set = set()
    results: dict = {}

    def taken(occ: dict) -> bool:
        return occ.get("id") in used or bool(occ.get("ad_id")) and occ["ad_id"] in used_ads
    for placement in ordered:
        pid = placement.get("id")
        pool = (eligible_by_placement or {}).get(pid, occurrences)
        cov = (coverage_by_placement or {}).get(pid, coverage)
        # Permission applies before the outcome is computed: this
        # placement's validated reuse occurrences rejoin its pool even
        # when already allocated elsewhere, provided they are inside
        # this placement's attachment scope. The original reservation in
        # `used` is retained; reuse counts twice without moving.
        pool_by_id = {o.get("id"): o for o in pool}
        fresh_ids = {o.get("id") for o in pool if not taken(o)}
        extra = [pool_by_id[oid] for oid in valid_reuse.get(pid, [])
                 if oid in pool_by_id and oid not in fresh_ids]
        fresh = [o for o in pool if not taken(o)] + extra
        first_pass = evaluate_one(placement, fresh, cov, now_iso, checks_run)
        # Competition: eligible-but-already-allocated support stays visible.
        allocated_elsewhere = []
        for occ in occurrences:
            if taken(occ) and occ.get("id") not in first_pass["eligible_ids"]:
                ok, _ = _occurrence_eligible(occ, placement, checks_run)
                included, amb = _in_window(occ, placement)
                if ok and included and not amb:
                    allocated_elsewhere.append(occ.get("id"))
        if allocated_elsewhere and first_pass["outcome"] in (
                *NO_MATCH_OUTCOMES, "contradicted"):
            first_pass = {**first_pass, "outcome": "ambiguous",
                          "reasons": [Reason(
                              f"allocation competition: {sorted(set(allocated_elsewhere))}"
                              " already allocated; identity/evidence uncertain",
                              "allocation_competition")] +
                          first_pass["reasons"]}
        if first_pass["outcome"] in NO_MATCH_OUTCOMES:
            # The fresh pool excludes identities allocated to earlier
            # placements, which erases authoritative conflicts: a used
            # occurrence that observably violates this placement still
            # contradicts it, and its unknowns stay unknown. Re-derive
            # against the full attached pool so allocation never reports
            # a false no-observation; the verdict keeps no eligible ids.
            full = evaluate_one(placement, pool, cov, now_iso, checks_run)
            if full["outcome"] not in NO_MATCH_OUTCOMES:
                for o in pool:
                    if taken(o) \
                            and o.get("id") not in allocated_elsewhere:
                        allocated_elsewhere.append(o.get("id"))
                used_here = sorted({o.get("id") for o in pool if taken(o)})
                note = Reason(f"allocation competition: {used_here} already"
                              " allocated; attached identity retained for"
                              " contradiction/unknown reasons",
                              "allocation_retained")
                first_pass = {**full,
                              "eligible_ids": list(first_pass["eligible_ids"]),
                              "reasons": list(full["reasons"]) + [note]}
        # Claim only unused occurrences for supported counts (stable
        # order). Reuse-permitted occurrences already allocated elsewhere
        # count toward support without moving their reservation.
        if first_pass["outcome"] == "supported":
            fresh_by_id = {o.get("id"): o for o in fresh}
            ads: list = []
            for oid in sorted(first_pass["eligible_ids"]):
                ad = _ad_of(fresh_by_id.get(oid) or {"id": oid})
                if ad not in ads:
                    ads.append(ad)
            take = set(ads[:int(placement.get("expected_count") or 1)])
            for occ in list(fresh) + list(occurrences):
                if _ad_of(occ) in take and occ.get("id") not in used:
                    used[occ.get("id")] = pid
            used_ads.update(ad for ad in take if isinstance(ad, str))
        first_pass["allocated_elsewhere"] = sorted(set(allocated_elsewhere))
        # Occurrence ordering note for stability checks.
        first_pass["occurrence_order"] = [o.get("id") for o in sorted(
            occurrences, key=occurrence_sort_key)]
        results[pid] = first_pass
    # Input-reorder stability: allocation depends only on sorted keys.
    return {"evaluations": results, "used": used,
            "invalid_overrides": invalid_reuse}


@_db.atomic
def freeze_evaluation(con: sqlite3.Connection, campaign_id: int,
                      evaluation: dict, pins: dict, actor: str = "owner") -> dict:
    """Retain an evaluation snapshot as history (never overwrite)."""
    _refuse_archived(con, campaign_id)
    ids = []
    for placement_id, ev in (evaluation.get("evaluations") or {}).items():
        cur = con.execute("INSERT INTO placement_evaluations (workspace_id, placement_id,"
                          " campaign_revision, outcome, reasons, evidence_pins,"
                          " created_at) VALUES ('local', ?, ?, ?, ?, ?, ?)",
                          (placement_id,
                           int(pins.get("campaign_revision", 0)),
                           ev.get("outcome") or "unknown",
                           json.dumps(ev.get("reasons") or []),
                           json.dumps({"pins": pins,
                                       "eligible": ev.get("eligible_ids") or []}),
                           _now_iso()))
        ids.append(cur.lastrowid)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "placement_evaluate", "campaign", str(campaign_id),
              {"frozen_rows": ids}, rev)
    return {"frozen_ids": ids, "aggregate_rev": rev}


def evaluation_history(con: sqlite3.Connection, placement_id: str) -> list:
    return [dict(r) for r in con.execute(
        "SELECT * FROM placement_evaluations WHERE placement_id=? ORDER BY id",
        (placement_id,)).fetchall()]


@_db.atomic
def save_manual_assessment(con: sqlite3.Connection, placement_id: str, author: str,
                           verdict: str, reason: str, evidence: list,
                           actor: str = "owner") -> dict:
    """Manual assessments stay separate from computed evaluations."""
    if verdict not in ("supported", "not_supported", "inconclusive"):
        raise _ops.BadInput("manual verdict must be supported/not_supported/inconclusive")
    if not reason or not str(reason).strip():
        raise _ops.BadInput("a manual assessment requires a reason")
    owner = con.execute("SELECT campaign_id FROM expected_placements WHERE id=?",
                        (placement_id,)).fetchone()
    if owner is not None:
        _refuse_archived(con, owner[0])
    con.execute("CREATE TABLE IF NOT EXISTS manual_assessments ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, placement_id TEXT NOT NULL,"
                " author TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL DEFAULT '',"
                " reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]',"
                " created_at TEXT NOT NULL)")
    cur = con.execute("INSERT INTO manual_assessments (placement_id, author, verdict,"
                      " reason, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                      (placement_id, author, verdict, str(reason)[:2000],
                       json.dumps(evidence or []), _now_iso()))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "manual_assess", "expected_placement", placement_id,
              {"author": author, "verdict": verdict}, rev)
    return {"assessment_id": cur.lastrowid, "aggregate_rev": rev}


def mark_stale(current_pins: dict, frozen_pins: dict) -> dict:
    """Brief/source/attribution/correspondence/review changes -> stale."""
    watched = ("brief_revision", "source_revision", "attribution_revision",
               "correspondence_revision", "review_revision", "coverage_revision")
    changed = [k for k in watched if current_pins.get(k) != frozen_pins.get(k)]
    return {"stale": bool(changed), "changed": changed}


# -- Customer campaign domain (Product-9 §4.5) --------------------------------------
#
# Server-owned evaluation: the browser cannot supply its own pass status,
# coverage, attribution, clock, or revision pins. Eligible occurrences are
# built from persisted per-placement attachments plus existing
# observation/review/source records. Campaign-level attachments stay an
# unassigned pool until explicitly allocated to a placement.

def _touch_campaign(con: sqlite3.Connection, campaign_id: int, actor: str,
                    action: str, detail: dict) -> dict:
    """Advance the owning campaign revision, snapshot, and audit atomically.

    Follows the established campaign-changed convention: the campaign row
    revision increments, a history snapshot is recorded when history
    tables exist, and the aggregate revision advances. Callers return the
    new campaign_revision so the next write checks against current state.

    The snapshot copies each check's manual reason, member text start-up
    does not scan, into the fully scanned checks_json; an older reason
    holding a recording id is refused before the copy (CLIPCORE-SEC-R13-01).
    """
    history = _db.has_history_tables(con)
    if history:
        from . import rights_admission as _rights
        # Every reason, any stored type: has_recording_id decodes a BLOB
        # and refuses one that is not UTF-8 (CLIPCORE-SEC-R14-01).
        reasons = [r[0] for r in con.execute(
            "SELECT manual_reason FROM campaign_checks WHERE campaign_id=?", (campaign_id,))]
        if _rights.has_recording_id(reasons):
            raise _ops.BadInput(_rights.RECORDING_COPY_REFUSAL)
    con.execute("UPDATE campaigns SET revision=revision+1, updated_at=? WHERE id=?",
                (_now_iso(), campaign_id))
    if history:
        _db.record_campaign_snapshot(con, campaign_id, actor)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, action, "campaign", str(campaign_id), detail, rev)
    revision = con.execute("SELECT revision FROM campaigns WHERE id=?",
                           (campaign_id,)).fetchone()[0]
    return {"campaign_revision": revision, "aggregate_rev": rev}


def _placement_row(con: sqlite3.Connection, campaign_id: int, placement_id: str) -> dict:
    from . import conversion
    conversion.require_executable(con, "requirements", placement_id)
    row = con.execute("SELECT * FROM expected_placements WHERE id=? AND campaign_id=?",
                      (placement_id, campaign_id)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown placement for this campaign")
    d = dict(row)
    d["requirements"] = json.loads(d.get("requirements") or "{}")
    d["provenance"] = json.loads(d.get("provenance") or "{}")
    return d


def _norm_brand(brand: str) -> str:
    import unicodedata as _ud
    return "".join(ch for ch in _ud.normalize("NFKD", (brand or "").strip())
                   if not _ud.combining(ch)).casefold()


def _occurrence_from_evidence(con: sqlite3.Connection, ev_id: str,
                              campaign_brand: str,
                              attachment_ref: str, ctx=None) -> dict | None:
    """Server-built occurrence from persisted evidence/review/observation rows.

    Every predicate comes from its own recorded authority; missing
    authority stays unknown and the record stays ineligible with explicit
    gaps. No resolved occurrence, correspondence, timing, copy-field, or
    publication fact is inferred from review bounds, transcript existence,
    or candidate state:
    - identity is the linked persisted occurrence row id when the
      evidence's observation links to exactly one occurrence; otherwise
      the attached evidence row itself (a derived handle, never a claim
      of a separately resolved occurrence). Multiple distinct linked
      occurrences stay ambiguous and never collapse to one id;
    - correspondence comes only from a persisted occurrence linked to the
      evidence's observation; otherwise unresolved;
    - advertiser confirmation requires a confirmed association for the
      campaign brand itself (evidence-level or observation-level);
    - timing values come only from reviewer-recorded effective bounds and
      count as verified only with the full chain (bounds recorded and
      complete, episode alignment verified, transcript version match,
      associated audio, known full-episode duration);
    - copy fields are observed; a field counts verified only with a
      current human field confirmation whose value matches the observed
      value with current source pins, otherwise it stays unknown;
    - publication keeps its recorded date precision; no midnight is
      invented from a date-only field.
    """
    from . import citations
    import math
    resolved = citations.resolve_source_state(con, attachment_ref, ctx=ctx)
    if "pins" not in resolved:
        from .access import AuthorizationError
        raise AuthorizationError("occurrence source fields are not disclosable", 403)
    if resolved["kind"] != "evidence" or resolved["record_id"] != ev_id:
        raise _ops.BadInput("occurrence needs an exact evidence publication")
    pins = resolved["pins"]
    ev = citations._component(con, "evidence_history", "evidence_id", ev_id,
                              "evidence_revision", pins["evidence_revision"])
    review = citations._component(con, "review_history", "evidence_id", ev_id,
                                  "review_revision", pins["review_revision"])
    ep = citations._component(con, "episode_history", "episode_id", ev["episode_id"],
                              "episode_revision", pins["episode_revision"])
    attrs = json.loads(ev["attrs"] or "{}")
    live = con.execute("SELECT source_version_id,transcript_version_id,episode_id"
                       " FROM evidence WHERE id=?", (ev_id,)).fetchone()
    operator_source_current = live is not None and \
        live["source_version_id"] == pins["source_version_id"] and \
        live["transcript_version_id"] == pins["transcript_version_id"] and \
        live["episode_id"] == pins["episode_id"]
    current_review = con.execute("SELECT judgment,eff_start_s,eff_end_s,bounds_verified"
                                 " FROM reviews WHERE evidence_id=?", (ev_id,)).fetchone()
    current_review = dict(current_review) if current_review is not None and operator_source_current else {}
    judgment = current_review.get("judgment")
    obs = resolved.get("observation")
    obs_id = obs["observation_id"] if obs else None
    current_observation = con.execute(
        "SELECT status,source_version_id FROM ad_observations WHERE id=?", (obs_id,)).fetchone() \
        if obs_id else None
    if current_observation is not None:
        if current_observation["source_version_id"] != pins["source_version_id"]:
            operator_source_current = False
            judgment = None
        elif current_observation["status"] == "reviewed_not_ad":
            judgment = "not_ad"
        elif current_observation["status"] == "reviewed_ad" and judgment is None:
            judgment = "ad"
    correspondence = "unresolved"
    occurrence_revision = None
    occurrence_id = None
    if obs_id:
        linked = [dict(r) for r in con.execute(
            "SELECT o.id, o.correspondence, o.revision FROM occurrences o"
            " JOIN occurrence_observations m ON m.occurrence_id=o.id"
            " WHERE m.observation_id=?", (obs_id,)).fetchall()]
        if linked:
            ids = {r["id"] for r in linked}
            if len(ids) == 1:
                # One persisted occurrence: its row id is the delivery
                # identity across every referencing ref and placement.
                occurrence_id = next(iter(ids))
            else:
                # Multiple distinct occurrences linked to one observation:
                # the occurrence stays ambiguous, never collapsed to one.
                correspondence = "ambiguous"
            if correspondence != "ambiguous":
                values = {r["correspondence"] for r in linked}
                correspondence = next(iter(values)) if len(values) == 1 else "ambiguous"
            occurrence_revision = max(r["revision"] for r in linked)
    if judgment == "unsure" or attrs.get("correspondence") == "ambiguous":
        correspondence = "ambiguous"
    brand_key = _norm_brand(campaign_brand)
    current_assocs = con.execute("SELECT brand,basis,channel FROM brand_assocs"
                                " WHERE evidence_id=?", (ev_id,)).fetchall() \
        if operator_source_current else []
    current_attributions = con.execute("SELECT entity,status FROM observation_attributions"
                                      " WHERE observation_id=?", (obs_id,)).fetchall() \
        if obs_id and operator_source_current else []
    advertiser_confirmed = (
        any(a["basis"] == "confirmed" and _norm_brand(a["brand"]) == brand_key
            for a in current_assocs)
        or any(a["status"] == "confirmed" and _norm_brand(a["entity"]) == brand_key
               for a in current_attributions))
    eff_start, eff_end = review.get("eff_start_s"), review.get("eff_end_s")
    bounds_complete = (bool(review.get("bounds_verified")) and operator_source_current
                       and bool(current_review.get("bounds_verified"))
                       and current_review.get("eff_start_s") == eff_start
                       and current_review.get("eff_end_s") == eff_end
                       and isinstance(eff_start, (int, float))
                       and isinstance(eff_end, (int, float)))
    transcript_match = (ep.get("transcript_version_id") is not None
                        and ep.get("transcript_version_id") == ev.get("transcript_version_id"))
    media = resolved.get("media") or {}
    aligned = ep.get("alignment") == "verified" and transcript_match \
        and media.get("timing") == "verified" and bool(media.get("recording_id"))
    audio = media.get("playback") in ("local", "remote")
    full_duration = None
    if isinstance(ep.get("duration_min"), (int, float)):
        full_duration = float(ep["duration_min"]) * 60.0
    full_recording = full_duration is not None and media.get("duration_s") is not None \
        and media.get("source_offset_s") == 0.0 \
        and math.isclose(media["duration_s"], full_duration, abs_tol=0.1)
    timing_verified = bool(bounds_complete and aligned and full_recording
                           and full_duration is not None)
    duration_s = (eff_end - eff_start) if bounds_complete else None
    position_s = eff_start if bounds_complete else None
    fields = {k: attrs.get(k) for k in ("read_type", "message", "offer", "code")
              if attrs.get(k) not in (None, "")}
    if obs:
        fields.update({key: value.get("value") for key, value in obs["fields"].items()
                       if key in ("read_type", "message", "offer", "code") and isinstance(value, dict)})
    from . import observations
    # Only human confirmations carry authority: a machine row a pin
    # captured never becomes the observed value.
    field_confirmations = {field: confirmation for field, confirmation
                           in (obs.get("confirmations") or {}).items()
                           if not observations._is_machine(confirmation.get("actor"))} if obs else {}
    active_confirmations = observations.current_field_confirmations(con, obs_id) \
        if obs_id and operator_source_current else {}
    verified_fields = sorted(
        field for field, confirmation in field_confirmations.items()
        if field in active_confirmations
        and active_confirmations[field]["id"] == confirmation["id"]
        and active_confirmations[field]["value"] == confirmation["value"])
    for field, confirmation in field_confirmations.items():
        fields[field] = confirmation["value"]
    human_verified_copy = bool(fields) and all(
        f in verified_fields for f in fields)
    operator_qualification = {
        "source_current": operator_source_current,
        "judgment": judgment, "advertiser_confirmed": advertiser_confirmed,
        "bounds_verified": bounds_complete, "verified_fields": verified_fields}
    return {"id": occurrence_id if occurrence_id is not None else ev_id,
            "evidence_id": ev_id,
            "attachment_ref": attachment_ref,
            "episode_id": ev["episode_id"],
            "published_at": None,
            "pub_date": ep.get("pub_date") or "",
            "date_precision": "date" if ep.get("pub_date") else "unknown",
            "correspondence": correspondence,
            "review_status": "reviewed_ad" if judgment == "ad" else (
                "reviewed_not_ad" if judgment == "not_ad" else "unreviewed"),
            "advertiser_confirmed": advertiser_confirmed,
            "source_matched": operator_source_current and transcript_match,
            "alignment_verified": aligned,
            "bounds_verified": bounds_complete,
            "timing_verified": timing_verified,
            "verified_start_s": eff_start if timing_verified else None,
            "full_episode_duration_s": media["duration_s"] if full_recording else None,
            "duration_s": duration_s,
            "position_s": position_s,
            "fields": fields,
            "verified_fields": sorted(verified_fields),
            "field_confirmations": field_confirmations,
            "human_verified_copy": human_verified_copy,
            "evidence_revision": ev.get("revision"),
            "review_revision": review.get("revision"),
            "review_judgment": judgment,
            "observation_id": obs_id,
            "occurrence_id": occurrence_id,
            "observation_revision": (obs or {}).get("revision"),
            "occurrence_revision": occurrence_revision,
            "episode_revision": ep.get("revision"),
            "transcript_version_id": ep.get("transcript_version_id"),
            "operator_qualification": operator_qualification,
            "media_state": "available" if audio else (
                "mismatch" if ep.get("alignment") == "mismatch" else "unavailable")}


def _placement_scope_episodes(con: sqlite3.Connection, placement: dict, ctx=None) -> tuple[list[str], str | None]:
    """Episode ids inside a placement window plus an ambiguity reason.

    Scopes resolve only through actual episodes and provenance: exact
    episode ids, persisted customer show identities, or legacy agent show
    ids. An empty or free-text title scope never broadens to all episodes;
    it yields ambiguity and can never produce supported outcomes.
    """
    from . import customer
    admitted = {row["id"]: row for row in customer.permitted_episodes(con, ctx)}
    if placement.get("scope") == "episode":
        ep_id = placement.get("episode_id")
        if ep_id not in admitted:
            return [], "episode scope is unavailable"
        return [ep_id], None
    show_id = (placement.get("show_id") or "").strip()
    date_from, date_to = placement.get("date_from"), placement.get("date_to")
    eps = list(admitted.values())
    if show_id.startswith("cs-"):
        try:
            from . import customer as _customer
            wanted = set(_customer.resolve_customer_show(con, show_id))
        except Exception:
            return [], "customer show scope does not resolve"
        eps = [e for e in eps if e["id"] in wanted]
    elif show_id.startswith("sh-"):
        try:
            from . import search as _search_mod
            wanted = set(_search_mod.resolve_show_id(con, show_id))
        except Exception:
            return [], "legacy agent show scope is ambiguous"
        eps = [e for e in eps if e["id"] in wanted]
    else:
        return [], ("unresolvable show scope: title guessing never establishes"
                    " coverage or eligibility")
    from . import customer_contracts as _contracts
    out = []
    for e in eps:
        # Undated means no valid ISO date (the customer date_status rule):
        # a malformed or literal "unknown" date is never in a window.
        day = _contracts.publication_day((e.get("pub_date") or "").strip())
        if day is None:
            continue
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        out.append(e["id"])
    return sorted(out), None


def _transcript_extent(con: sqlite3.Connection, version_id: int | None) -> float:
    if version_id is None:
        return 0.0
    row = con.execute("SELECT payload FROM source_versions WHERE id=?",
                      (version_id,)).fetchone()
    if row is None:
        return 0.0
    return _payload_extent(row["payload"])


def _payload_extent(payload_text) -> float:
    try:
        payload = json.loads(payload_text)
    except (ValueError, TypeError):
        return 0.0
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    extent = 0.0
    for seg in segments:
        if isinstance(seg, dict) and isinstance(seg.get("end"), (int, float)):
            extent = max(extent, float(seg["end"]))
    return extent


def read_settled(review_judgment, observation_status, brand_confirmed: bool) -> bool:
    """One rule for a settled read, whichever table holds its review (R1-05).

    Settled: a person said it is not an ad (evidence review not_ad or
    observation reviewed_not_ad), the observation was replaced
    (superseded), or a person said it is an ad (evidence review ad or
    observation reviewed_ad) and a person confirmed its brand (a confirmed
    brand association or observation attribution). Anything else still
    waits for review: no decision yet, "unsure", or an ad whose brand
    nobody confirmed."""
    if review_judgment == "not_ad" or observation_status in ("reviewed_not_ad", "superseded"):
        return True
    is_ad = review_judgment == "ad" or observation_status == "reviewed_ad"
    return is_ad and bool(brand_confirmed)


# RS1-MEMO: the shared ad rows below (_build_ad_rows) are read with no
# context, so they are the same for every viewer of one database state:
# they are remembered per database file and research view state
# (customer_query.view_state_memo under AD_ROWS_TAG, context None), as the
# research campaign's source-query match is (_research_scope_match). Their
# inputs: guarded rows only (evidence, reviews, episodes, source_versions,
# brand_assocs, ad_reads, ad_read_links, ad_brand_aliases, ad_reads_meta,
# ad_rules, evidence_lineage: research tables move the research token, the
# others are hashed into research_view_state; none is a skipped table);
# the committed brand lists (code); the model-names switch (in the key, and
# _memoized splits model-named states); and the display list's curated
# merge switch (in the key). Nothing reads ctx, role, plan, content policy,
# saved items or the clip guard: every per-viewer rule (permitted episodes,
# disclosure, has_clip) runs after, per request, outside the memo. None
# (nothing remembered) without a view-state guard or a database file.
AD_ROWS_TAG = "draft-ad-rows"
AD_ROWS_FIELDS = ("rows", "aliases", "ad_ids", "ad_ids_waiting")


def _db_file(con: sqlite3.Connection) -> str:
    """The main database's file path; "" for an in-memory or temporary
    database, which has no identity a memo could key on."""
    return next((row[2] for row in con.execute("PRAGMA database_list")
                 if row[1] == "main"), "")


def _ad_rows_state(con: sqlite3.Connection):
    """The memo state of the shared ad rows: the campaign-free research
    state of a database file (customer_query.campaign_free_state: they
    read no campaign table, RS1-OVERVIEW); None (never remembered) for an
    in-memory or temporary database, or without guards."""
    from . import customer_query
    if not _db_file(con):
        return None
    return customer_query.campaign_free_state(con)


def _build_ad_rows(con: sqlite3.Connection) -> dict:
    """{"rows": episode id -> rows, "aliases": alias map}: the workspace's
    customer ad rows and, in a named-reads workspace, its unjudged
    candidates. "ad_ids" and "ad_ids_waiting" are added on first use
    (_ad_ids), derived from these rows only."""
    from . import customer
    from . import ad_reads as _ad
    by_episode: dict = {}
    rows = customer._customer_ad_rows(con, None, self_promo="exclude")
    if _ad.named_mode(con):
        rows = rows + _unjudged_candidates(con)
    for ev in rows:
        by_episode.setdefault(ev["episode_id"], []).append(ev)
    return {"rows": by_episode, "aliases": _ad.alias_map(con)}


def _ad_rows_by_episode(con: sqlite3.Connection, cache: dict) -> dict:
    """The workspace's customer ad rows (customer._customer_ad_rows, the rows
    every customer surface counts) and, in a named-reads workspace, the
    unjudged candidates customers do not see (_unjudged_candidates),
    grouped by episode: shared through cache by every placement of a
    draft and by the whole Overview (R1-20), and remembered across
    requests per database and research view state (RS1-MEMO). Callers
    must not mutate them."""
    if "rows" not in cache:
        from . import customer, customer_query
        tag = (AD_ROWS_TAG, customer.model_names_on(), customer._display_det_on())
        shared = customer_query.view_state_memo(
            con, None, tag, lambda: _build_ad_rows(con), state=_ad_rows_state)
        cache["ad_rows"] = shared
        cache["rows"] = shared["rows"]
        cache["aliases"] = shared["aliases"]
    return cache["rows"]


def _unjudged_candidates(con: sqlite3.Connection) -> list:
    """Named-reads workspace (R3-02): the active rule candidates (det:) and
    Jev windows (jev:, the re-screen's too) no review settled (no decision
    or "unsure") that support no named read (ad_read_links). Customer
    surfaces never list them (customer._customer_ad_rows), but they still
    wait for review. One query, plus one batched read of their brand
    associations; rows shaped like _customer_ad_rows' for _waiting_for_review."""
    from . import customer
    where, args = customer.customer_row_where(con)
    rows = [dict(r) for r in con.execute(
        "SELECT e.id, e.episode_id, e.source_version_id, e.transcript_version_id, e.attrs,"
        " e.orig_start_s, e.orig_end_s,"
        " r.judgment AS r_judgment, ar.brand_suggested AS ar_brand_suggested"
        " FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
        " LEFT JOIN reviews r ON r.evidence_id=e.id"
        " LEFT JOIN ad_reads ar ON ar.evidence_id=e.id"
        f" WHERE {where} AND (e.source_record_key LIKE 'det:%' OR e.source_record_key LIKE 'jev:%')"
        " AND (r.judgment IS NULL OR r.judgment NOT IN ('ad', 'not_ad'))"
        " AND NOT EXISTS (SELECT 1 FROM ad_read_links l WHERE l.support_key=e.source_record_key)"
        " ORDER BY e.id", args)]
    assocs: dict = {}
    ids = [r["id"] for r in rows]
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        # PODS: model rows are display-only
        for a in con.execute("SELECT evidence_id, brand, basis, channel FROM brand_assocs"
                             " WHERE evidence_id IN (%s) AND basis<>'model' ORDER BY id"
                             % ",".join("?" * len(chunk)), chunk):
            assocs.setdefault(a["evidence_id"], []).append(
                {"brand": a["brand"], "basis": a["basis"], "channel": a["channel"]})
    for r in rows:
        try:
            attrs = json.loads(r.pop("attrs") or "{}")
        except (ValueError, TypeError):
            attrs = {}
        r["_attrs"] = attrs if isinstance(attrs, dict) else {}
        r["_judgment"] = r.pop("r_judgment")
        r["_assocs"] = assocs.get(r["id"], [])
        r["_unjudged"] = True
    return rows


def _ad_ids(con: sqlite3.Connection, cache: dict, waiting: bool = False) -> dict:
    """Evidence id -> ad id over the shared ad rows (_ad_rows_by_episode,
    one pass per cache, R1-20), by the customer ad rule (ad_breaks).
    waiting=False: the customer ad rows only, the ads customers see, which
    placement checks count. waiting=True: with the unjudged candidates too,
    so reads still waiting for review group into ads (_waiting_for_review)."""
    key = "ad_ids_waiting" if waiting else "ad_ids"
    if key not in cache:
        from . import ad_breaks
        by_episode = _ad_rows_by_episode(con, cache)
        shared = cache["ad_rows"]
        if key not in shared:
            # A pure function of the shared rows (RS1-MEMO): filled once
            # into the remembered bundle; a race fills it twice, equal.
            rows = [ev for evs in by_episode.values() for ev in evs
                    if waiting or not ev.get("_unjudged")]
            shared[key] = {eid: ad["id"]
                           for eid, ad in ad_breaks.index_of_ad_rows(con, rows).items()}
        cache[key] = shared[key]
    return cache[key]


_URL_CHANNELS = ("url", "code")


def _host_stem(entity: str) -> str:
    """A web address's or code's host stem: no scheme, `www.`, path or TLD
    ("https://www.Shopify.com/unsol" -> "shopify"); a code is itself."""
    s = (entity or "").strip().casefold()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0].removeprefix("www.")
    head, dot, _tld = s.rpartition(".")
    return head if dot else s


def _compact(key: str) -> str:
    return re.sub(r"[\W_]", "", key or "")


def _waiting_for_review(con: sqlite3.Connection, episode_ids: list, brand, ctx,
                        cache: dict | None = None) -> int | None:
    """The number of ads that hold a read still waiting for review
    (_waiting_reads); None when any of it is hidden from ctx."""
    reads = _waiting_reads(con, episode_ids, brand, ctx, cache)
    return None if reads is None else len({ad for ad, _ev in reads.values()})


def _waiting_reads(con: sqlite3.Connection, episode_ids: list, brand, ctx,
                   cache: dict | None = None, named: dict | None = None) -> dict | None:
    """{evidence id: (ad id, row)} for every read that waits: ads (decision 1:
    _ad_ids, unjudged candidates included) holding a
    read that still waits for review: reads on the scope episodes' current
    transcript copies that still wait
    for review (read_settled) and could be the placement's: unnamed, or
    naming its brand (R1-04). A web address or code names its brand by its
    host stem (_host_stem, same alias rules); one that resolves to no known
    brand names nothing (R3-01), and so does a machine suggestion of no
    known brand. A read naming only other brands never makes
    the placement uncertain; a brand under review counts as unnamed (its
    guess stays hidden). Display is untouched: spoken and URL/code names
    are only matched here, never merged. Earlier-copy and no-copy reads stay outside, as
    everywhere else; self-promotion is not an ad.

    None (unknown, never zero) when an episode or a read in scope is hidden
    from ctx, so hidden data never reads as "no match" (R1-19).

    named, when given, is filled with {evidence id: row} for every read on
    those current copies, waiting or settled, that names the brand (the
    brand's reads, FUNC-2 F2-SCR-R1-01); an unnamed read waits but is not
    one of them."""
    from . import customer
    from . import ad_reads as _ad
    from . import citations
    wanted_ids = set(episode_ids)
    episodes = customer.permitted_episodes_by_id(con, ctx, wanted_ids)
    if {e["id"] for e in episodes} != wanted_ids:
        return None
    current = {e["id"]: e.get("transcript_version_id") for e in episodes}
    cache = {} if cache is None else cache
    by_episode = _ad_rows_by_episode(con, cache)
    aliases = cache["aliases"]
    rows = [ev for eid, tv in current.items() for ev in by_episode.get(eid, [])
            if tv is not None and ev.get("transcript_version_id") == tv]
    if ctx is not None:
        for ev in rows:
            source = citations._version_provenance(
                con, ev["source_version_id"] or ev["transcript_version_id"])
            if source is None or not ctx.disclosure_allowed(con, source["source_key"], "metadata"):
                return None
    observations: dict = {}
    attributions: dict = {}
    ids = [ev["id"] for ev in rows]
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        for r in con.execute(
                "SELECT e.id AS evidence_id, o.id AS observation_id, o.status FROM evidence e"
                " JOIN ad_observations o ON o.id=json_extract(e.attrs, '$.observation_id')"
                " WHERE e.id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            observations[r["evidence_id"]] = (r["observation_id"], r["status"])
    obs_ids = sorted({o for o, _status in observations.values()})
    for at in range(0, len(obs_ids), 500):
        chunk = obs_ids[at:at + 500]
        for r in con.execute("SELECT observation_id, entity, status, channel"
                             " FROM observation_attributions"
                             " WHERE observation_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            attributions.setdefault(r["observation_id"], []).append(
                (r["entity"], r["status"], r["channel"]))
    wanted = {k for k in (_ad.canonical(aliases, brand), _norm_brand(brand)) if k} \
        if isinstance(brand, str) and brand.strip() else set()
    wanted_compact = {_compact(k) for k in wanted} - {""}

    def any_known(keys: list) -> bool:
        from . import corrections
        if "known" not in cache:
            cache["known"] = ({}, corrections.load(con))
        known, rules = cache["known"]
        for k in keys:
            if k not in known:
                known[k] = corrections.known_brand(con, k, rules)
        return any(known[k] for k in keys)

    def names_brand(name: str, url: bool, confirmed: bool):
        """True: names the placement's brand; False: another brand; None:
        names nothing — a web address or code that resolves to no known
        brand (R3-01), or a machine suggestion (not confirmed by a person)
        that names no known brand (uncertain stays uncertain)."""
        if _ad.canonical(aliases, name) in wanted or _norm_brand(name) in wanted:
            return True
        if url:
            stem = _host_stem(name)
            keys = [k for k in (_ad.canonical(aliases, stem), _ad.brand_key(stem)) if k]
            if any(_compact(k) in wanted_compact for k in keys):
                return True
            return False if any_known(keys) else None
        if confirmed:
            return False
        keys = [k for k in (_ad.canonical(aliases, name), _ad.brand_key(name)) if k]
        return False if any_known(keys) else None

    ad_of = _ad_ids(con, cache, waiting=True)
    waiting: dict = {}
    for ev in rows:
        obs_id, obs_status = observations.get(ev["id"], (None, None))
        attrs = [a for a in attributions.get(obs_id, []) if a[1] != "rejected"]
        confirmed = any(a.get("basis") == "confirmed" for a in ev.get("_assocs") or []) \
            or any(st == "confirmed" for _e, st, _ch in attrs)
        settled = read_settled(ev.get("_judgment"), obs_status, confirmed)
        if settled and named is None:
            continue
        if ev.get("_brand_hidden"):
            names = []
        else:
            # PODS: model rows are display-only
            names = [(a.get("brand"), a.get("channel") in _URL_CHANNELS,
                      a.get("basis") == "confirmed") for a in ev.get("_assocs") or []
                     if a.get("basis") != "model"]
            names.append((ev.get("ar_brand_suggested")
                          or (ev.get("_attrs") or {}).get("brand_spoken"), False, False))
            names.extend((e, ch in _URL_CHANNELS, st == "confirmed") for e, st, ch in attrs)
        hits = [names_brand(x, url, conf) for x, url, conf in names
                if isinstance(x, str) and x.strip()]
        if named is not None and True in hits:
            named[ev["id"]] = ev
        if settled:
            continue
        if True in hits or all(h is None for h in hits):
            # A settled read beside it never settles the ad: it still waits.
            waiting[ev["id"]] = (ad_of.get(ev["id"]) or ev["id"], ev)
    return waiting


def _placement_coverage(con: sqlite3.Connection, placement: dict, ctx=None,
                        brand=None, reads_cache: dict | None = None) -> dict:
    """Per-placement coverage from the one screened / fully checked
    definition (coverage.episode_screen_state, DET-PLAN item 6).

    A whole-episode scope is complete for an episode only when it is fully
    checked: screened (the latest screen attestation on the current
    transcript copy covers [0, transcript end], and the screening record
    says screened), the transcript reaches the stored duration within
    coverage.FULLY_CHECKED_TOLERANCE_S, the copy is indexed, and no latest
    fetch or refine attestation failed. A bounded scope (any other
    bounds_basis) needs the same, except that the screen proof must cover
    the requested bounds instead of the transcript reaching the duration.
    Screening rests on the recorded screen attestation: a review,
    candidate row, or observation window is never whole-episode screening
    evidence, and no migration backfills it. Only proof bound to the
    episode's current transcript identity counts; proof recorded against a
    replaced source is reported as a stale-source exclusion.

    episode_checks carries each listed episode's episode_screen_state
    entry as plain fields; the display is built from them.

    Complete coverage also counts the ads in scope still waiting for
    review (_waiting_for_review, for the campaign's brand); when that count
    is unknown to ctx the coverage is unknown, never complete.
    """
    if ctx is None:
        from .access import AccessContext
        ctx = AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                   workspace_id="local", credential_kind="local_operator")
    from . import coverage as _coverage
    _coverage._require_attestation_schema(con)
    ep_ids, ambiguity = _placement_scope_episodes(con, placement, ctx)
    if ambiguity:
        return {"state": "unknown", "missing": [], "failed": [],
                "ambiguity": ambiguity, "episodes": ep_ids, "episode_labels": {}}
    if not ep_ids:
        return {"state": "unknown", "missing": [], "failed": [],
                "ambiguity": "no included episodes means unknown coverage",
                "episodes": [], "episode_labels": {}}
    titles = {}
    checks: dict[str, dict] = {}
    exclusions: list[str] = []
    complete: set[str] = set()
    touched: set[str] = set()
    from . import citations
    source_scope = placement.get("source_scope") or (placement.get("provenance") or {}).get("source_scope") or {}
    pinned = citations.resolve_source_state(con, source_scope["source_ref"], ctx=ctx,
                                            include_text=False) if source_scope else None
    whole_episode = not source_scope or source_scope.get("bounds_basis") == "full_effective_source"
    from . import ad_reads as _ad
    if reads_cache is not None:
        memo = reads_cache.setdefault("screening_display", {})
        need = [e for e in dict.fromkeys(ep_ids) if e not in memo]
        if need:
            got = _ad.screening_display_many(con, need, run_cache=reads_cache)
            memo.update({e: got.get(e) for e in need})
        screening = {e: memo[e] for e in ep_ids if memo.get(e) is not None}
    else:
        screening = None
    states, extents = _coverage._episode_screen_states(con, ep_ids, screening)
    reason_text = {"no_transcript": "no transcript", "not_screened": "not screened",
                   "no_duration": "no known episode duration",
                   "not_indexed": "transcript copy is not indexed",
                   "fetch_failed": "latest fetch attestation failed",
                   "refine_failed": "latest refine attestation failed"}
    transcribed_hours = 0.0
    for eid in ep_ids:
        ep = con.execute("SELECT ep_title, transcript_version_id, duration_min"
                         " FROM episodes WHERE id=?", (eid,)).fetchone()
        tv = ep["transcript_version_id"] if ep else None
        if ctx is not None and not ctx.disclosure_allowed(con, f"transcript:{eid}" if tv else f"episode:{eid}", "metadata"):
            continue
        if tv is not None and ctx is not None and not ctx.disclosure_allowed(con, f"transcript:{eid}", "transcript"):
            continue
        if pinned and ((pinned.get("pins") or {}).get("transcript_version_id") != tv):
            return {"state": "unknown", "missing": [], "failed": [],
                    "ambiguity": "captured source scope is stale; displayed scope revision must change",
                    "episodes": ep_ids, "episode_labels": {}}
        if ep and ep["ep_title"]:
            titles[eid] = ep["ep_title"]
        check = states.get(eid)
        if check is None:
            exclusions.append(f"{eid}: unknown episode")
            continue
        checks[eid] = check
        extent = extents[eid] if eid in extents else _transcript_extent(con, tv)
        duration = (float(ep["duration_min"]) * 60.0
                    if isinstance(ep["duration_min"], (int, float))
                    and not isinstance(ep["duration_min"], bool)
                    and float(ep["duration_min"]) > 0 else None)
        req_start = float((source_scope.get("bounds") or {}).get("start_s", 0.0) or 0.0)
        req_end = (source_scope.get("bounds") or {}).get("end_s") if source_scope else duration or extent
        if not req_end or req_end <= 0:
            exclusions.append(f"{eid}: no known episode duration or transcript")
            continue
        if tv is not None or "fetch_failed" in check["not_fully_checked"]:
            touched.add(eid)
        reasons = list(check["not_fully_checked"])
        if not whole_episode:
            # A bounded scope: the screen proof must cover the bounds; the
            # transcript need not reach the stored duration.
            reasons = [r for r in reasons if r not in ("transcript_short", "no_duration")]
            if check["screened"]:
                union, _failure = _coverage.attestation_union(
                    con, eid, _coverage.STAGE_GROUPS["screening"], source_identity=tv,
                    exclude_producers=_coverage._screen_gate(con, eid, tv).get("screening", ()))
                if not _coverage.covers_to_end(union, req_start, float(req_end)):
                    reasons.append("outside_screened_span")
                    exclusions.append(f"{eid}: screen proof does not cover the requested"
                                      f" {req_start:.1f}-{float(req_end):.1f}s")
        # Same comparison as coverage.episode_screen_state's transcript_short.
        if extent and extent < float(req_end) - (_coverage.FULLY_CHECKED_TOLERANCE_S + _coverage.EPS
                                                 if whole_episode else _coverage.EPS):
            if whole_episode and "transcript_short" not in reasons:
                reasons.append("transcript_short")
            exclusions.append(
                f"{eid}: transcript covers {extent:.1f}s of requested {float(req_end):.1f}s")
        exclusions.extend(f"{eid}: {reason_text[r]}" for r in reasons if r in reason_text)
        if not reasons and check["screened"]:
            complete.add(eid)
        if _coverage.has_superseded_attestations(con, eid, tv):
            exclusions.append(
                f"{eid}: recorded proof predates the current source"
                " (transcript replaced); no current attestation")
        transcribed_hours += _coverage._union_hours(
            [(max(req_start, s), min(float(req_end), e)) for s, e in _coverage.attestation_union(
                con, eid, _coverage.STAGE_GROUPS["transcription"], source_identity=tv)[0]],
            float(req_end))
    exclusions = sorted(set(exclusions))
    scope = set(ep_ids)
    if scope <= complete:
        waiting = _waiting_for_review(con, sorted(scope), brand, ctx, reads_cache)
        if waiting is None:
            return {"state": "unknown", "missing": [], "failed": [],
                    "ambiguity": "some reads in scope are not visible here,"
                                 " so a match can't be ruled out",
                    "episodes": sorted(scope), "episode_labels": titles}
        return {"state": "complete", "missing": [], "failed": [],
                "episodes": sorted(scope),
                "episode_labels": titles,
                "episode_checks": checks,
                "waiting_for_review": waiting,
                "searchable_hours": round(transcribed_hours, 6)}
    if scope & (complete | touched):
        return {"state": "incomplete",
                "missing": sorted(scope - complete), "failed": [],
                "episodes": sorted(scope),
                "episode_labels": titles,
                "episode_checks": checks,
                "exclusions": exclusions}
    return {"state": "unknown", "missing": [], "failed": [],
            "ambiguity": "; ".join(exclusions) or "no covered sources in scope",
            "episodes": sorted(scope), "episode_labels": titles}


_VERDICT_MISMATCH_FIELDS = ("code", "url", "offer", "message", "duration_s", "position_s")
EPISODE_REVIEW_DECISIONS = ("confirm", "reject", "undo")
EPISODE_REVIEW_CODE_MAX = 40


def _episode_reviews(con: sqlite3.Connection, campaign_id, placement_id) -> dict:
    """{episode id: {"revision": int, "decisions": {evidence id: row}}}."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                   " AND name='placement_episode_reviews'").fetchone() is None:
        return {}
    out: dict = {}
    for r in con.execute(
            "SELECT * FROM placement_episode_reviews WHERE workspace_id='local'"
            " AND campaign_id=? AND placement_id=? ORDER BY revision, id",
            (campaign_id, placement_id)):
        ep = out.setdefault(r["episode_id"], {"revision": 0, "decisions": {}})
        ep["revision"] = max(ep["revision"], int(r["revision"]))
        if r["decision"] == "undo":
            ep["decisions"].pop(r["evidence_id"], None)
        else:
            ep["decisions"][r["evidence_id"]] = dict(r)
    return out


def _code_key(code) -> str:
    """Codes compare upper-cased with non-alphanumerics removed."""
    return "".join(ch for ch in code if ch.isalnum()).upper() if isinstance(code, str) else ""


# PREFS-SHARED's customer.MEMBER_PREFS_ACTION (lr-app-prefs 33335d9d): each
# hosted member's own Settings values, display name included, are the
# detail of their latest audit row with this action (actor = principal).
MEMBER_PREFS_ACTION = "member_preferences_save"
# The judge review's audit action: outside the customer_/placement_ actions
# Overview activity reads, so review clicks never use up its 40 rows.
EPISODE_REVIEW_ACTION = "episode_verdict_review"


def _reviewer_name(con: sqlite3.Connection, principal: str, cache: dict) -> str | None:
    """A decider's display name: the member's own Settings display name
    from their latest PREFS-SHARED row (MEMBER_PREFS_ACTION, actor = the
    member principal). None when they recorded none: never the principal,
    its account subject or an email (security r1 F2-SEC-R1-01/03)."""
    names = cache.setdefault("verdict_reviewer_names", {})
    if principal not in names:
        name = None
        row = con.execute("SELECT detail FROM audit WHERE action=?"
                          " AND target_type='settings' AND actor=? ORDER BY id DESC LIMIT 1",
                          (MEMBER_PREFS_ACTION, principal)).fetchone()
        if row is not None:
            try:
                detail = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                detail = {}
            value = detail.get("display_name") if isinstance(detail, dict) else None
            name = value.strip() if isinstance(value, str) and value.strip() else None
        names[principal] = name
    return names[principal]


def _review_view(con, row: dict | None, cache: dict) -> dict | None:
    if row is None:
        return None
    return {"decision": "confirmed" if row["decision"] == "confirm" else "rejected",
            "by_name": _reviewer_name(con, row["decided_by"], cache),
            "at": row["decided_at"], "code": row["code"]}


def _evidence_ref(con: sqlite3.Connection, evidence_id, ctx, cache: dict):
    """The source ref that opens one evidence row (customer.record takes
    an sr1: ref, never a raw evidence id), under the capture disclosure
    rule the placement's other evidence refs use
    (citations.captured_disclosure). None when there is no id, no
    publication, or this viewer may not disclose it."""
    if not evidence_id:
        return None
    refs = cache.setdefault("verdict_evidence_refs", {})
    if evidence_id not in refs:
        from . import access, citations
        try:
            ref = citations.current_source_ref(con, "evidence", evidence_id)
            citations.captured_disclosure(con, ref, ctx)
        except (citations.CitationError, access.AuthorizationError):
            ref = None
        refs[evidence_id] = ref
    return refs[evidence_id]


def _first_text(attrs: dict, key: str):
    value = attrs.get(key)
    if not isinstance(value, list):
        return None
    return next((v.strip() for v in value if isinstance(v, str) and v.strip()), None)


# Words that follow "code" in speech and are never the code ("enter code
# below", "use code at checkout"): CODE-HEARD r1 F3.
_NOT_A_CODE = frozenset((
    "a above and at below checkout for from here in is it now of on online or"
    " that the there this to today when with you your").split())


# How far a transcript segment may run past the read's shown span and
# still count toward its spoken-code hint (_spoken_code).
SPAN_EDGE_S = 0.5


def _segments_inside(segs: list, lo, hi) -> list:
    """The transcript segments that lie wholly inside [lo, hi], with the
    SPAN_EDGE_S tolerance: the segments carry no word timings, so one that
    crosses either edge (or runs across the whole span) is left out, the
    safe direction (_spoken_code, _reviewed_excerpt)."""
    return [s for s in segs if _timed_segment(s)
            and s["start"] >= lo - SPAN_EDGE_S and s["end"] <= hi + SPAN_EDGE_S]


def _finite(value) -> bool:
    """A plain finite number: int or float, not bool, NaN or inf."""
    return type(value) in (int, float) and math.isfinite(value)


def _timed_segment(s) -> bool:
    """T2 r1 F1/F2: a segment whose start and end are finite plain numbers
    with start <= end. Any other (no end, null, string, NaN, reversed) can
    run past any bound and is never kept."""
    return isinstance(s, dict) and _finite(s.get("start")) and _finite(s.get("end")) \
        and s["start"] <= s["end"]


def _spoken_code(con: sqlite3.Connection, ev: dict, lo, hi, cache: dict):
    """CODE-HEARD: the first promo code said in the read's shown span
    ("use code X", "promo code X": ad_rules._CODE_RE), for a read whose
    attrs carry none (the namer writes offer_codes [] on named reads). A
    hint only ("code sounds like"), never a verified code. The segments
    carry no word timings, so a segment counts only when it lies wholly
    inside the span (SPAN_EDGE_S tolerance): one that crosses either
    edge, or runs across the whole read, never lends it a neighbour's
    code (r1 F2, r2 R2-2). A code in such a segment is missed, the safe
    direction (r2 R2-3). The segments are
    source_versions rows, a research table: the memo keys move with them
    (customer_query.campaign_free_state)."""
    from . import ad_rules, observations
    vid = ev.get("transcript_version_id") or ev.get("source_version_id")
    if vid is None or lo is None or hi is None:
        return None
    segs = cache.setdefault("verdict_segments", {})
    if vid not in segs:
        segs[vid] = observations._segments_of(con, vid)
    text = " ".join(s.get("text") or "" for s in _segments_inside(segs[vid], lo, hi))
    return next((m.group(1).upper() for m in ad_rules._CODE_RE.finditer(text)
                 if m.group(1).lower() not in _NOT_A_CODE), None)


def _verdict_reads(con: sqlite3.Connection, eid: str, ids: list, rows: dict, ctx,
                   cache: dict) -> dict | None:
    """{evidence id: {"machine_code", "machine_url", "has_clip", "display_span",
    "start"}} for the given reads of one episode. rows holds the waiting
    rows; any other id (a decided read) is read from the shared ad rows or
    the evidence table. None when one of them is hidden from ctx."""
    from . import citations, customer
    shared = {r["id"]: r for r in _ad_rows_by_episode(con, cache).get(eid, [])}
    rows = dict(rows)
    missing = [i for i in ids if i not in rows and i not in shared]
    for at in range(0, len(missing), 500):
        chunk = missing[at:at + 500]
        for r in con.execute(
                "SELECT e.id, e.episode_id, e.source_version_id, e.transcript_version_id,"
                " e.orig_start_s, e.orig_end_s, r.judgment AS _judgment,"
                " r.eff_start_s AS r_eff_start_s, r.eff_end_s AS r_eff_end_s"
                " FROM evidence e LEFT JOIN reviews r ON r.evidence_id=e.id"
                " WHERE e.id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            if r["episode_id"] == eid:
                rows[r["id"]] = dict(r)
    for i in ids:
        if i not in rows and i in shared:
            rows[i] = shared[i]
    attrs: dict = {}
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        for rid, raw in con.execute("SELECT id, attrs FROM evidence WHERE id IN (%s)"
                                    % ",".join("?" * len(chunk)), chunk):
            try:
                parsed = json.loads(raw or "{}")
            except (ValueError, TypeError):
                parsed = {}
            attrs[rid] = parsed if isinstance(parsed, dict) else {}
    memo = cache.setdefault("verdict_clip_memo", {})
    if "verdict_customer_aliases" not in cache:
        cache["verdict_customer_aliases"] = customer.customer_aliases(con)
    if "verdict_brand_checked" not in cache:
        cache["verdict_brand_checked"] = customer._alias_checker(
            con, cache["verdict_customer_aliases"])
    out = {}
    for i in ids:
        ev = rows.get(i)
        if ev is None:
            continue
        source = citations._version_provenance(
            con, ev.get("source_version_id") or ev.get("transcript_version_id"))
        if source is None or not ctx.disclosure_allowed(con, source["source_key"], "metadata"):
            return None
        a = attrs.get(i, {})
        if "_brand_keys" not in ev:  # an unjudged candidate or an evidence row
            ev = dict(ev, _brand_keys=[])
        # The read as a customer card shows it (customer._read_block): its
        # brand, its display span (customer._display_span) and has_clip
        # (customer._clip_playable with that span, CLIP-STOP), one rule.
        card = customer._read_block(con, ev, cache["verdict_customer_aliases"], memo, ctx=ctx)
        lo, hi = card["display_span"]
        read_code = _first_text(a, "offer_codes")
        out[i] = {"brand": customer._customer_brand(
                      card["brand"], cache["verdict_customer_aliases"],
                      cache["verdict_brand_checked"]),
                  "machine_code": read_code or _spoken_code(con, ev, lo, hi, cache),
                  "read_code": read_code,
                  "machine_url": _first_text(a, "urls"),
                  "has_clip": bool(card["has_clip"]),
                  "display_span": [lo, hi] if lo is not None and hi is not None else None,
                  "start": lo if lo is not None else float("inf")}
    return out


def _episode_verdicts(con: sqlite3.Connection, placement: dict, occurrences: list,
                      coverage: dict, brand, ctx=None, reads_cache: dict | None = None) -> dict:
    """Verdict per episode in one placement's scope (FUNC-2 B, STEP0-PLAN
    with Addenda A and B).

    Additive and read-only: evaluate_one, the allocation, the pins and
    frozen reports are untouched. Precedence: an operator-verified heard
    read (heard_right: _occurrence_eligible; heard_wrong:
    _verified_requirement_conflict; basis "verified") > a judge-confirmed
    read (placement_episode_reviews; basis "judge_review") > an undecided
    read this viewer can see (heard_unconfirmed) > reads wait but none is
    listed (waiting_review) > not_heard on a complete episode; else
    not_checked. Complete means complete under SCREENED
    (_placement_coverage). An episode or read hidden from the viewer is
    not_checked "not_visible", never not_heard. not_heard means only that
    the fully checked episode holds no read of the brand, never that the
    ad did not air. A code counts only as the human-verified field or the
    code a judge confirmed. A scope problem (coverage ambiguity) gives no
    episodes."""
    coverage = coverage or {}
    if coverage.get("ambiguity") or coverage.get("state") not in ("complete", "incomplete"):
        return {"episodes": [], "summary": None}
    if ctx is None:
        from .access import AccessContext
        ctx = AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                   workspace_id="local", credential_kind="local_operator")
    from . import customer
    from . import customer_contracts as _contracts
    cache = {} if reads_cache is None else reads_cache
    scope = list(coverage.get("episodes") or [])
    complete = set(scope) - set(coverage.get("missing") or []) - set(coverage.get("failed") or [])
    rows = {e["id"]: e for e in customer.permitted_episodes_by_id(con, ctx, scope)}
    reviews = _episode_reviews(con, placement.get("campaign_id"), placement.get("id"))
    reqs = placement.get("requirements") or {}
    brief_code = reqs.get("code") if reqs.get("code") not in (None, "") else None
    entries = []
    for eid in scope:
        ep = rows.get(eid) or {}
        entry = {"episode_id": eid, "show": ep.get("show_title") or "",
                 "episode_title": ep.get("ep_title") or "",
                 "published": _contracts.publication_day((ep.get("pub_date") or "").strip()),
                 "verdict": "not_checked", "mismatch": [], "said_code": None,
                 "brief_code": brief_code, "evidence_id": None, "has_clip": False,
                 "display_span": None, "waiting_for_review": 0, "not_checked_reason": None,
                 "reads": [], "review": None, "review_revision": 0, "basis": None,
                 "machine_code": None, "machine_url": None, "evidence_ref": None}
        entries.append(entry)
        tv = ep.get("transcript_version_id")
        visible = bool(ep) and ctx.disclosure_allowed(
            con, f"transcript:{eid}" if tv else f"episode:{eid}", "metadata") and (
            tv is None or ctx.disclosure_allowed(con, f"transcript:{eid}", "transcript"))
        named: dict = {}
        waiting = _waiting_reads(con, [eid], brand, ctx, cache, named) if visible else None
        if waiting is None:
            entry["not_checked_reason"] = "not_visible"
            continue
        if eid not in complete:
            entry["not_checked_reason"] = "no_transcript" if tv is None else "not_fully_checked"
        decided = (reviews.get(eid) or {}).get("decisions") or {}
        # Only the brand's reads are listed and decide the verdict
        # (F2-SCR-R1-01): another brand's read or an unnamed one is never
        # listed, and a decision stored on one never counts.
        ids = sorted(i for i in set(waiting) | set(decided) if i in named)
        views = _verdict_reads(con, eid, ids, named, ctx, cache)
        if views is None:
            entry["verdict"], entry["not_checked_reason"] = "not_checked", "not_visible"
            continue
        entry["waiting_for_review"] = len({ad for ad, _row in waiting.values()})
        entry["review_revision"] = (reviews.get(eid) or {}).get("revision", 0)
        listed = sorted(views, key=lambda i: (views[i]["start"], i))
        entry["reads"] = [{"evidence_id": i, "evidence_ref": _evidence_ref(con, i, ctx, cache),
                           "brand": views[i]["brand"],
                           "machine_code": views[i]["machine_code"],
                           "machine_url": views[i]["machine_url"], "brief_code": brief_code,
                           "has_clip": views[i]["has_clip"],
                           "display_span": views[i]["display_span"],
                           "review": _review_view(con, decided.get(i), cache)} for i in listed]
        here = sorted((o for o in occurrences or [] if o and o.get("episode_id") == eid),
                      key=occurrence_sort_key)
        chosen = next((o for o in here if _occurrence_eligible(o, placement)[0]), None)
        read = None
        if chosen is not None:
            entry["verdict"], entry["basis"] = "heard_right", "verified"
        else:
            chosen = next((o for o in here if _verified_requirement_conflict(o, placement)), None)
            if chosen is not None:
                entry["verdict"], entry["basis"] = "heard_wrong", "verified"
                entry["mismatch"] = [
                    {"field": r["field"], "said": r["observed"], "brief": r["expected"]}
                    for r in _compare_requirements(placement, chosen,
                                                   chosen.get("attachment_ref"), con)
                    if r["state"] == "mismatch" and r["field"] in _VERDICT_MISMATCH_FIELDS]
        if chosen is not None:
            entry["evidence_id"] = chosen.get("evidence_id")
            if "code" in (chosen.get("verified_fields") or []):
                entry["said_code"] = (chosen.get("fields") or {}).get("code")
            view = (_verdict_reads(con, eid, [entry["evidence_id"]], {}, ctx, cache) or {}) \
                .get(entry["evidence_id"])
            if view is not None:
                entry["display_span"], entry["has_clip"] = view["display_span"], view["has_clip"]
        else:
            judged = []
            for r in entry["reads"]:
                if (r["review"] or {}).get("decision") != "confirmed":
                    continue
                # The code saved with the Confirm: typed, or the one the row
                # showed (save_episode_review, r1 F1). A row saved before
                # that (code NULL) keeps the read's own code only, never a
                # spoken hint no judge saw.
                said = r["review"]["code"]
                if said is None:
                    said = views[r["evidence_id"]]["read_code"]
                right = brief_code is None or (
                    bool(_code_key(said)) and _code_key(said) == _code_key(brief_code))
                judged.append((not right, r, said))
            undecided = [r for r in entry["reads"] if r["review"] is None]
            if judged:
                wrong, read, said = min(judged, key=lambda j: j[0])
                entry["verdict"] = "heard_wrong" if wrong else "heard_right"
                entry["basis"], entry["said_code"] = "judge_review", said
                entry["review"] = read["review"]
                if wrong:
                    entry["mismatch"] = [{"field": "code", "said": said, "brief": brief_code}]
            elif undecided:
                entry["verdict"], read = "heard_unconfirmed", undecided[0]
            elif eid not in complete:
                entry["verdict"] = "not_checked"
            elif set(waiting) - set(views):
                # Reads wait that are not the brand's by name (unnamed): they
                # could still be its read, so never not_heard.
                entry["verdict"] = "waiting_review"
            else:
                entry["verdict"] = "not_heard"
                rejected = [decided[r["evidence_id"]] for r in entry["reads"] if r["review"]]
                if rejected:
                    entry["review"] = _review_view(
                        con, max(rejected, key=lambda d: d["revision"]), cache)
            if read is not None:
                entry.update(evidence_id=read["evidence_id"], machine_code=read["machine_code"],
                             machine_url=read["machine_url"], has_clip=read["has_clip"],
                             display_span=read["display_span"])
    for entry in entries:
        entry["evidence_ref"] = _evidence_ref(con, entry["evidence_id"], ctx, cache)
    entries.sort(key=lambda e: e["episode_id"])
    entries.sort(key=lambda e: e["published"] or "", reverse=True)
    return {"episodes": entries, "summary": _verdict_summary(entries)}


def _verdict_summary(entries: list) -> dict:
    """The counts of one placement's episode verdicts."""
    count = {v: sum(e["verdict"] == v for e in entries)
             for v in ("heard_right", "heard_wrong", "heard_unconfirmed", "not_heard",
                       "waiting_review", "not_checked")}
    heard = count["heard_right"] + count["heard_wrong"]
    return {"heard": heard, "heard_right": count["heard_right"],
            "heard_wrong": count["heard_wrong"], "not_heard": count["not_heard"],
            "checked": heard + count["not_heard"],
            "waiting_review": count["waiting_review"],
            "not_checked": count["not_checked"],
            "unconfirmed": count["heard_unconfirmed"]}


@_model_names_off
def placement_episode_verdicts(con: sqlite3.Connection, campaign_id: int,
                               placement_id: str, ctx=None) -> dict:
    """One placement's episode_verdicts entry as the viewer, without the
    rest of the draft (the review route's response)."""
    camp = _checked_campaign(con, campaign_id, ctx)
    placement = next((p for p in list_expected(con, campaign_id) if p["id"] == placement_id),
                     None)
    if placement is None:
        raise _ops.BadInput("unknown placement for this campaign")
    brand = camp.get("brand") or ""
    cache: dict = {}
    occs, _problems = _occurrences_for_placement(con, campaign_id, placement_id, brand, ctx)
    cov = _placement_coverage(con, placement, ctx, brand, cache)
    return _episode_verdicts(con, placement, occs, cov, brand, ctx, cache)


def _shown_hint(read: dict):
    """The code a read's row shows as "code sounds like" (app/views.js
    unconfirmedLine, the server's _unconfirmed_text): its machine_code, on
    an undecided read of a brief with a code; else None (a rejected or
    confirmed row, or a brief without a code, shows no code)."""
    if read.get("review") is not None or not read.get("brief_code"):
        return None
    return read.get("machine_code")


@_db.atomic
@_model_names_off
def save_episode_review(con: sqlite3.Connection, campaign_id: int, placement_id: str,
                        episode_id: str, evidence_id: str, decision: str, expected_revision,
                        code=None, actor: str = "owner", ctx=None,
                        shown_code=None) -> dict:
    """A judge's confirm, reject or undo on one listed read of one episode
    of one placement (FUNC-2 Addendum A). Appends one
    placement_episode_reviews row and one audit row; writes nothing else
    (no operator review, brand association, field confirmation,
    attachment, pin or report). The read must be one of the episode's
    `reads` as this viewer sees them. Returns the placement's updated
    verdicts."""
    if decision not in EPISODE_REVIEW_DECISIONS:
        raise _ops.BadInput("decision must be confirm, reject or undo")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise _ops.BadInput("evidence_id must be a nonempty string")
    if shown_code is not None and (decision != "confirm" or not isinstance(shown_code, str)
                                   or len(shown_code) > EPISODE_REVIEW_CODE_MAX):
        raise _ops.BadInput(f"shown_code is a text of at most {EPISODE_REVIEW_CODE_MAX}"
                            " characters, on confirm only")
    if code is not None and (decision != "confirm" or not isinstance(code, str)
                             or not code.strip() or len(code) > EPISODE_REVIEW_CODE_MAX):
        raise _ops.BadInput(f"code is a nonempty text of at most {EPISODE_REVIEW_CODE_MAX}"
                            " characters, on confirm only")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise _ops.BadInput("expected revision must be an integer")
    _refuse_archived(con, campaign_id)
    verdicts = placement_episode_verdicts(con, campaign_id, placement_id, ctx)
    entry = next((e for e in verdicts["episodes"] if e["episode_id"] == episode_id), None)
    if entry is None:
        raise _ops.BadInput("unknown episode for this placement")
    if expected_revision != entry["review_revision"]:
        raise _ops.EpisodeReviewConflict(entry["review_revision"])
    read = next((r for r in entry["reads"] if r["evidence_id"] == evidence_id), None)
    if read is None:
        raise _ops.BadInput("not a read of this episode")
    if decision == "undo" and read["review"] is None:
        raise _ops.EpisodeReviewConflict(entry["review_revision"])
    from_hint = False
    if decision == "confirm" and code is None and shown_code is not None:
        # A blank Code heard box confirms the code the row SHOWED, sent by
        # the page ("" when it showed none). It is saved only when it is
        # still what the row would show now; otherwise the page is stale (a
        # transcript or bounds edit since it loaded) and must repaint (r2
        # R2-1). A read's own offer_codes stay unsaved (code NULL), read
        # from the read as before. No shown_code (an older page): NULL,
        # never the live hint.
        if shown_code != (_shown_hint(read) or ""):
            raise _ops.EpisodeReviewConflict(entry["review_revision"])
        row = con.execute("SELECT attrs FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        try:
            attrs = json.loads(row[0] or "{}") if row is not None else {}
        except (TypeError, ValueError):
            attrs = {}
        if shown_code and _first_text(attrs if isinstance(attrs, dict) else {},
                                      "offer_codes") is None:
            code, from_hint = shown_code, True
    current = read["review"]
    if current is not None and decision != "undo" \
            and current["decision"] == ("confirmed" if decision == "confirm" else "rejected") \
            and current["code"] == code:
        return verdicts  # the decision stands already: no row, audit or revision (F2-SEC-R1-04)
    revision = entry["review_revision"] + 1
    con.execute("INSERT INTO placement_episode_reviews (workspace_id, campaign_id,"
                " placement_id, episode_id, evidence_id, decision, code, decided_by,"
                " decided_at, revision) VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, placement_id, episode_id, evidence_id, decision, code,
                 actor, _now_iso(), revision))
    rev = _db.bump_revision(con)
    _db.audit(con, actor, EPISODE_REVIEW_ACTION, "placement", placement_id,
              {"campaign": campaign_id, "episode": episode_id, "evidence": evidence_id,
               "decision": decision, "code": code, "review_revision": revision,
               **({"from_hint": True} if from_hint else {})}, rev)
    return placement_episode_verdicts(con, campaign_id, placement_id, ctx)


def _confirmation_record(con, conf: dict) -> dict:
    """A pinned field confirmation plus its review time and observation
    revision, read by id from the immutable confirmation row. The pinned
    projection never copies them: adding them there changes digests."""
    row = con.execute("SELECT created_at, observation_revision FROM field_confirmations"
                      " WHERE id=?", (conf.get("id"),)).fetchone() if con is not None else None
    if row is None:
        return conf
    return dict(conf, reviewed_at=row["created_at"],
                observation_revision=row["observation_revision"])


def _compare_requirements(placement: dict, occ: dict | None,
                          evidence_ref: str | None, con=None) -> list[dict]:
    """Structured requirement comparisons: expected vs observed per field.

    A missing/unverified field is unknown before string comparison; a
    verified conflicting value is a mismatch. Timing verification follows
    the persisted chain (reviewer-recorded effective bounds, episode
    alignment, transcript match, associated audio, known full duration).
    Copy fields verify only through current human field confirmations:
    the confirmed value is the observed value, bound to the current
    observation revision and source pins with human provenance;
    unconfirmed fields stay unknown. Never assembles a pass from fields
    across different records: occ is one record, and the comparison
    carries that record's exact reference.
    """
    reqs = dict(placement.get("requirements") or {})
    reqs.pop("priority", None)
    rows = []
    prov = placement.get("provenance") or {}
    for field in ("read_type", "message", "offer", "code", "duration_s", "position_s"):
        if field not in reqs or reqs[field] in (None, ""):
            continue
        expected = reqs[field]
        if occ is None:
            rows.append({"field": field, "expected": expected, "observed": None,
                         "state": "unknown", "evidence_ref": None,
                         "reason": "no eligible occurrence", "provenance": prov})
            continue
        conf: dict = {}
        if field in ("duration_s", "position_s"):
            observed = occ.get(field)
            verified = bool(occ.get("timing_verified"))
            unverified_reason = ("timing unverified: needs reviewer-recorded bounds,"
                                 " verified alignment, transcript match, audio,"
                                 " and known full-episode duration")
        else:
            observed = (occ.get("fields") or {}).get(field)
            verified = field in (occ.get("verified_fields") or [])
            conf = (occ.get("field_confirmations") or {}).get(field) or {}
            if verified:
                unverified_reason = ""
            elif observed is not None and conf:
                unverified_reason = ("confirmation does not match the current"
                                     " observed value or pins; re-confirm")
            else:
                unverified_reason = ("no copy-field verification recorded;"
                                     " observed values stay unknown")
        if observed is None or not verified:
            state, reason = "unknown", unverified_reason if observed is not None \
                else "missing required field"
        elif isinstance(expected, list) and len(expected) == 2 and isinstance(observed, (int, float)):
            state = "supported" if (expected[0] is None or expected[0] <= observed) and \
                (expected[1] is None or observed <= expected[1]) else "mismatch"
            reason = "" if state == "supported" else f"value {observed} outside [{expected[0]}, {expected[1]}]"
        elif observed == expected:
            state, reason = "supported", ""
        else:
            state, reason = "mismatch", "verified conflicting value"
        confirmation = None
        if verified:
            conf = _confirmation_record(con, conf)
            confirmation = {"actor": conf.get("actor"),
                            "reviewed_at": conf.get("reviewed_at"),
                            "observation_revision": conf.get("observation_revision"),
                            "source_version_id": conf.get("source_version_id"),
                            "transcript_version_id": conf.get("transcript_version_id")}
        rows.append({"field": field, "expected": expected, "observed": observed,
                     "state": state, "evidence_ref": evidence_ref,
                     "reason": reason, "provenance": prov,
                     "confirmation": confirmation})
    return rows


@_db.atomic
def save_placement_evidence(con: sqlite3.Connection, campaign_id: int, placement_id: str,
                            ref: str, expected_revision, actor: str = "owner") -> dict:
    """Attach an exact versioned reference to one placement (CID/PID checked).

    Suggestions never attach automatically; each explicit pair is stored.
    """
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    camp = _campaign_exists(con, campaign_id)
    if camp.get("mode") != "supplied_requirements":
        raise _ops.BadInput("placement attachments require supplied-requirement mode")
    if int(expected_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    _refuse_archived(con, camp)
    _placement_row(con, campaign_id, placement_id)
    if not isinstance(ref, str) or not ref:
        raise _ops.BadInput("ref must be a nonempty versioned reference")
    ref = _qualify_attachment_ref(con, ref)
    from . import citations
    resolved = citations.resolve_source_state(con, ref, media=False)
    citations.require_window_in_bounds(con, ref)
    placement = _placement_row(con, campaign_id, placement_id)
    if not _target_allows_ref(con, dict(placement), ref):
        raise _ops.BadInput("evidence is outside the captured placement scope")
    cur = con.execute("INSERT OR IGNORE INTO placement_evidence (workspace_id, campaign_id,"
                      " placement_id, ref, attached_at, attached_by)"
                      " VALUES ('local', ?, ?, ?, ?, ?)",
                      (campaign_id, placement_id, ref, _now_iso(), actor))
    if not cur.rowcount:
        # Exact duplicate attach is an idempotent no-change: the owning
        # campaign revision, snapshot, and audit stay exactly as they are.
        # No change revision is fabricated for a duplicate.
        return {"placement_id": placement_id, "ref": ref,
                "campaign_revision": camp["revision"],
                "aggregate_rev": _db.aggregate_revision(con),
                "duplicate": True}
    changed = _touch_campaign(con, campaign_id, actor, "placement_evidence_attach",
                              {"placement": placement_id, "ref": ref})
    return {"placement_id": placement_id, "ref": ref,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


@_db.atomic
def remove_placement_evidence(con: sqlite3.Connection, campaign_id: int, placement_id: str,
                              ref: str, expected_revision, actor: str = "owner") -> dict:
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    camp = _campaign_exists(con, campaign_id)
    if camp.get("mode") != "supplied_requirements":
        raise _ops.BadInput("placement attachments require supplied-requirement mode")
    if int(expected_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    _refuse_archived(con, camp)
    _placement_row(con, campaign_id, placement_id)
    cur = con.execute("DELETE FROM placement_evidence WHERE workspace_id='local'"
                      " AND campaign_id=? AND placement_id=? AND ref=?",
                      (campaign_id, placement_id, ref))
    if not cur.rowcount:
        raise _ops.BadInput("attachment not found for this placement")
    changed = _touch_campaign(con, campaign_id, actor, "placement_evidence_detach",
                              {"placement": placement_id, "ref": ref})
    return {"placement_id": placement_id, "ref": ref,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


def placement_attachments(con: sqlite3.Connection, campaign_id: int,
                          placement_id: str) -> list[str]:
    _placement_row(con, campaign_id, placement_id)
    return [r["ref"] for r in con.execute(
        "SELECT ref FROM placement_evidence WHERE workspace_id='local'"
        " AND campaign_id=? AND placement_id=? ORDER BY ref",
        (campaign_id, placement_id)).fetchall()]


def _attached_evidence_id(con, ref: str) -> str | None:
    """Evidence id a stored attachment ref names: the publication's record
    for sr1 refs, the named id for legacy ev refs. A pure lookup: no
    disclosure or pin resolution."""
    from . import citations
    legacy = re.fullmatch(r"ev:(.+)@v\d+", ref)
    if legacy:
        return legacy.group(1)
    try:
        state_id = citations._parse_source_ref(ref)[0]
    except citations.CitationError:
        return None
    row = con.execute("SELECT kind, record_id FROM source_states WHERE id=?",
                      (state_id,)).fetchone()
    return row["record_id"] if row is not None and row["kind"] == "evidence" else None


def _parse_attachment_ref(con, ref, ctx=None):
    from . import citations
    try:
        ref = ref if ref.startswith("sr1:") else citations.qualify_legacy_source_ref(con, ref)
        resolved = citations.resolve_source_state(con, ref, ctx=ctx)
        if "pins" not in resolved:
            from .access import AuthorizationError
            raise AuthorizationError("attachment source fields are not disclosable", 403)
    except citations.CitationError:
        return None
    if resolved["kind"] != "evidence":
        return None
    return resolved["record_id"], resolved["pins"]["source_version_id"]


def _occurrences_for_placement(con: sqlite3.Connection, campaign_id: int,
                               placement_id: str, campaign_brand: str, ctx=None
                               ) -> tuple[list[dict], list[str]]:
    """Occurrence pool from this placement's exact persisted attachments.

    Each attachment reference is retained and validated: the evidence row
    must exist and the pinned version must be current or retained. No
    silent substitution of a replacement version ever occurs.
    """
    refs = [row["ref"] for row in con.execute(
        "SELECT ref FROM placement_evidence WHERE workspace_id='local'"
        " AND campaign_id=? AND placement_id=? AND binding='eligible' ORDER BY ref",
        (campaign_id, placement_id))]
    occs, problems = [], []
    for ref in refs:
        parsed = _parse_attachment_ref(con, ref, ctx)
        if parsed is None:
            problems.append("unqualified attachment")
            continue
        ev_id, _version = parsed
        occs.append(_occurrence_from_evidence(con, ev_id, campaign_brand, ref, ctx))
    return occs, problems


def _attachment_pin(con: sqlite3.Connection, ref: str, occ: dict | None, ctx=None) -> dict:
    """Exact source identity plus current operator qualification. Personal
    notes are not pinned: they are no part of a report (PRIVACY-FEED-FIX1)."""
    from . import citations
    resolved = citations.resolve_source_state(con, ref, ctx=ctx)
    if "pins" not in resolved:
        raise _ops.BadInput("attachment pins require full field authority")
    if occ is None:
        return {"ref": ref, "source_pins": resolved["pins"]}
    # PODS: model rows are display-only
    assocs = [a for a in resolved["assocs"] if a.get("basis") != "model"]
    assoc_digest = hashlib.sha256(json.dumps(
        sorted([a.get("brand"), a.get("basis"), a.get("channel")] for a in assocs),
        sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"ref": ref, "source_pins": resolved["pins"],
            "evidence_revision": occ.get("evidence_revision"),
            "source_version": occ.get("transcript_version_id"),
            "review_revision": occ.get("review_revision"),
            "review_judgment": occ.get("review_judgment"),
            "assoc_digest": assoc_digest,
            "observation_revision": occ.get("observation_revision"),
            "occurrence_revision": occ.get("occurrence_revision"),
            "correspondence": occ.get("correspondence"),
            "episode_revision": occ.get("episode_revision"),
            "transcript_version": occ.get("transcript_version_id"),
            "media_state": occ.get("media_state"),
            "operator_qualification": occ.get("operator_qualification")}


def _comparison_source(con: sqlite3.Connection, placement: dict,
                       occs: list[dict], eligible_ids: list) -> tuple[dict | None, list[dict]]:
    """Comparison record and rows for one placement.

    The eligible record compares when one exists. Otherwise a same-record
    decisive verified mismatch still shows with its actual expected and
    observed values, even though that record cannot satisfy the placement.
    Passes are never assembled from fields across different records.
    """
    by_id = {o["id"]: o for o in occs}
    ordered = sorted(occs, key=occurrence_sort_key)
    eligible = [by_id[oid] for oid in sorted(eligible_ids) if oid in by_id]
    if eligible:
        chosen = eligible[0]
        return chosen, _compare_requirements(placement, chosen,
                                             chosen.get("attachment_ref"), con)
    for occ in ordered:
        rows = _compare_requirements(placement, occ, occ.get("attachment_ref"), con)
        if any(r["state"] == "mismatch" for r in rows):
            return None, rows
    if ordered:
        first = ordered[0]
        return None, _compare_requirements(placement, first, first.get("attachment_ref"), con)
    return None, _compare_requirements(placement, None, None, con)


def campaign_brand_display(con: sqlite3.Connection, brand) -> str | None:
    """The display name of a campaign's stored brand key, the way the
    brand directory names it (VL-25); None when the campaign has none."""
    if not isinstance(brand, str) or not brand.strip():
        return None
    from . import ad_reads as _ad
    aliases = _ad.alias_map(con)
    return _ad.brand_display(con, _ad.canonical(aliases, brand), aliases)[0]


def _draft_show_titles(con: sqlite3.Connection, units: list, ctx=None) -> dict:
    """{show_id: display title} for the customer shows the live draft's
    placements and targets point at (VL-06). A show is named only when
    one of its episodes is disclosable to ctx; anything else stays
    unnamed. Never part of a frozen payload."""
    from . import customer
    wanted = sorted({u.get("show_id") for u in units
                     if isinstance(u.get("show_id"), str) and u["show_id"].startswith("cs-")})
    if not wanted:
        return {}
    allowed = {row["id"] for row in customer.permitted_episodes(con, ctx)}
    out = {}
    for show_id in wanted:
        row = con.execute("SELECT display_title FROM customer_show_identities"
                          " WHERE workspace_id='local' AND id=?", (show_id,)).fetchone()
        if row is None or not row["display_title"]:
            continue
        if allowed.intersection(customer._show_member_ids(con, show_id)):
            out[show_id] = row["display_title"]
    return out


def _coverage_pin(cov: dict) -> str:
    """Coverage pin of one unit. The waiting-for-review count stays out
    (R1-21): a review that leaves the result as it is never stales a
    frozen report; a review that changes the outcome still does, through
    the allocation pin."""
    return hashlib.sha256(json.dumps(
        {k: v for k, v in (cov or {}).items() if k != "waiting_for_review"},
        sort_keys=True, default=str).encode()).hexdigest()


def _checked_campaign(con: sqlite3.Connection, campaign_id: int, ctx=None) -> dict:
    """The campaign row a viewer may read, refused while its definition
    needs legacy review (draft and Overview alike)."""
    from . import citations
    citations.authorize_resource(con, "campaign", campaign_id, ctx)
    camp = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if camp is None:
        raise _ops.BadInput("unknown campaign")
    problems = _definition_problems(con, dict(camp))
    if problems:
        raise _ops.BadInput("legacy_review_required: " + "; ".join(problems))
    return dict(camp)


def _evaluate_placements(con: sqlite3.Connection, campaign_id: int, placements: list,
                         brand: str, now: str, ctx, reads_cache: dict,
                         reuse_overrides: dict | None = None,
                         pins: dict | None = None) -> tuple[dict, dict, dict, dict]:
    """Occurrences, coverage and the allocation of every placement, as the
    viewer: (eligible_by_placement, coverage_by_placement, problems,
    allocation). With pins, also records the draft's per-placement pins;
    without (Overview), no pin is resolved."""
    eligible_by_placement, coverage_by_placement, problems_by = {}, {}, {}
    ad_ids = _ad_ids(con, reads_cache)
    for p in placements:
        occs, problems = _occurrences_for_placement(con, campaign_id, p["id"], brand, ctx)
        for o in occs:
            o["ad_id"] = ad_ids.get(o["evidence_id"])
        if pins is not None and p.get("binding", "eligible") == "eligible":
            # The occurrence -> ad grouping the counts used: a later
            # regroup stales a frozen report (_stale_reasons).
            groups = pins["ad_groups"][p["id"]] = {}
            for o in occs:
                groups.setdefault(o["id"], o["ad_id"])
        eligible_by_placement[p["id"]] = occs
        cov = _placement_coverage(con, p, ctx, brand, reads_cache)
        coverage_by_placement[p["id"]] = cov
        refs = sorted(placement_attachments(con, campaign_id, p["id"]))
        by_ref = {o.get("attachment_ref"): o for o in occs}
        for ref in refs:
            _captured_attachment(con, ref, ctx)
            if pins is not None:
                pins["attachment_state"][ref] = _attachment_pin(con, ref, by_ref.get(ref), ctx)
        if pins is not None:
            pins["placements"][p["id"]] = p.get("revision", 1)
            pins["attachments"][p["id"]] = refs
            pins["coverage"][p["id"]] = _coverage_pin(cov)[:16]
        problems_by[p["id"]] = problems
    allocatable = [p for p in placements
                   if p.get("binding", "eligible") == "eligible"]
    allocation = allocate_campaign(allocatable, [o for occs in eligible_by_placement.values()
                                                for o in occs],
                                   {"state": "unknown"}, now, reuse_overrides, None,
                                   eligible_by_placement, coverage_by_placement)
    allocation.setdefault("citations", {})
    for p in placements:
        if p.get("binding", "eligible") != "eligible" \
                and p["id"] not in allocation["evaluations"]:
            # A binding label, not an outcome: citation-only placements
            # are never evaluated for credit, so they live outside the
            # seven-outcome evaluations map.
            allocation["citations"][p["id"]] = {
                "binding": p.get("binding"),
                "reasons": [Reason("citation_only placements never allocate",
                                    "citation_only")],
                "eligible_ids": [], "allocated_ids": []}
    for p in placements:
        cov = coverage_by_placement.get(p["id"]) or {}
        if cov.get("ambiguity"):
            # Ambiguous scopes can never produce supported outcomes.
            ev = allocation["evaluations"].get(p["id"]) or {}
            allocation["evaluations"][p["id"]] = {
                **ev, "outcome": "ambiguous",
                "reasons": [Reason(f"scope ambiguity: {cov['ambiguity']}",
                                   "scope_ambiguity")] + (ev.get("reasons") or [])}
    return eligible_by_placement, coverage_by_placement, problems_by, allocation


@_model_names_off
def placement_outcomes(con: sqlite3.Connection, campaign_id: int, ctx=None,
                       reads_cache: dict | None = None) -> dict:
    """What Overview needs of a campaign, without building its draft: the
    placements, their coverage, the allocation outcomes and the
    per-episode verdicts, computed as the viewer exactly as
    build_customer_draft computes them. No pins,
    report state, research scope or brief versions are resolved."""
    camp = _checked_campaign(con, campaign_id, ctx)
    placements = list_expected(con, campaign_id)
    reads_cache = {} if reads_cache is None else reads_cache
    brand = camp.get("brand") or ""
    eligible, coverage, _, allocation = _evaluate_placements(
        con, campaign_id, placements, brand, _now_iso(), ctx, reads_cache)
    # WIN3: the campaign page's per-episode verdicts, built by the same
    # call as build_customer_draft, so Overview counts what it counts.
    episode_verdicts = {p["id"]: _episode_verdicts(con, p, eligible[p["id"]], coverage[p["id"]],
                                                   brand, ctx, reads_cache)
                        for p in placements}
    return {"placements": placements, "allocation": allocation,
            "coverage_by_placement": coverage, "episode_verdicts": episode_verdicts}


# RS1-OVERVIEW: Overview's per-campaign outcomes (placement_outcomes) are
# remembered per database file, campaign-free research state
# (customer_query.campaign_free_state), this campaign's own rows
# (_campaign_rows_digest), disclosure class (customer_query.class_memo),
# the viewer's permitted episodes (permitted_digest) and the UTC day (an
# open placement window is judged against today); the last four are the
# entry's state, so each (class, campaign) keeps one entry. A write to one campaign
# moves only its own digest: Overview then rebuilds that campaign alone.
# The outcomes read campaign tables only by this campaign's id and its
# brief (pinned by tests.test_rs1_overview's SQL trace). Each class keeps
# OUTCOMES_KEEP of them; callers get a copy.
OUTCOMES_TAG = "overview-outcomes"
OUTCOMES_KEEP = 16


def _campaign_rows_digest(con: sqlite3.Connection, campaign_id: int) -> str:
    """Digest of every CAMPAIGN_TABLES row of one campaign: its campaigns
    row, every row with its campaign_id, and every row of a campaign table
    with no campaign_id column (the briefs, whichever campaign uses them)."""
    from . import customer_query
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    digest = hashlib.sha256(str(campaign_id).encode())
    for table in sorted(customer_query.CAMPAIGN_TABLES & names):
        quoted = '"' + table.replace('"', '""') + '"'
        columns = {r[1] for r in con.execute(f"PRAGMA table_info({quoted})")}
        if table == "campaigns":
            where, args = " WHERE id=?", [campaign_id]
        elif "campaign_id" in columns:
            where, args = " WHERE campaign_id=?", [campaign_id]
        else:
            where, args = "", []
        digest.update(b"\1" + table.encode())
        rows = sorted(repr(tuple(row)) for row in con.execute(
            f"SELECT * FROM {quoted}{where}", args))
        for row in rows:
            digest.update(b"\0" + row.encode())
    return digest.hexdigest()


def permitted_digest(con: sqlite3.Connection, ctx) -> str:
    """Digest of the episode ids ctx may see (customer.permitted_episodes),
    part of the outcomes' key."""
    from . import customer
    ids = sorted(str(r["id"]) for r in customer.permitted_episodes(con, ctx))
    return hashlib.sha256("\0".join(ids).encode()).hexdigest()


def _outcomes_state(campaign_id: int, day: str, permitted: str):
    """The outcomes' memo state: the campaign-free research state, this
    campaign's own rows, the UTC day and the viewer's permitted episodes.
    The day and the permitted digest are state, not tag (review r1
    RRO-01): an entry of another day or digest is then the same identity's
    older state, which _fill replaces, so OUTCOMES_KEEP bounds a class."""
    def state(con):
        from . import customer_query
        free = customer_query.campaign_free_state(con)
        if free is None:
            return None
        return (free, _campaign_rows_digest(con, campaign_id), day, permitted)
    return state


def _overview_fields(outcomes: dict) -> dict:
    """Only what Overview reads of placement_outcomes (review r1 RRO-02):
    placements, allocation, coverage, and per placement its episode
    summary and whether it lists any episode. The episode rows (reviewer
    display names from the unguarded audit table, refs) are not kept."""
    return {"placements": outcomes["placements"], "allocation": outcomes["allocation"],
            "coverage_by_placement": outcomes["coverage_by_placement"],
            "episode_verdicts": {
                pid: {"summary": (v or {}).get("summary"),
                      "episodes": bool((v or {}).get("episodes"))}
                for pid, v in (outcomes.get("episode_verdicts") or {}).items()}}


def overview_outcomes(con: sqlite3.Connection, campaign_id: int, ctx, reads_cache: dict,
                      permitted: str) -> dict:
    """Overview's fields of placement_outcomes (_overview_fields),
    remembered (RS1-OVERVIEW above); a fresh copy each call. No database
    file, no context or no guard: computed afresh."""
    from . import customer_query
    if not _db_file(con):
        return _overview_fields(placement_outcomes(con, campaign_id, ctx=ctx,
                                                   reads_cache=reads_cache))
    return copy.deepcopy(customer_query.class_memo(
        con, ctx, (OUTCOMES_TAG, campaign_id),
        lambda c: _overview_fields(placement_outcomes(con, campaign_id, ctx=c,
                                                      reads_cache=reads_cache)),
        state=_outcomes_state(campaign_id, _now_iso()[:10], permitted), keep=OUTCOMES_KEEP))


@_model_names_off
def build_customer_draft(con: sqlite3.Connection, campaign_id: int,
                         reuse_overrides: dict | None = None, ctx=None,
                         reads_cache: dict | None = None) -> dict:
    """Server-built complete campaign draft: requirements, attachments,
    suggestions, coverage, and current pins.

    An explicit owner reuse override + rationale is validated and honored
    here, never silently discarded; invalid pairs are reported on the
    allocation. The exposed per-placement eligible ids are the allocation
    verdict, not the raw attachment pool. reads_cache shares one pass over
    the ad rows between drafts built together (Overview)."""
    from . import citations, access
    camp = _checked_campaign(con, campaign_id, ctx)
    reads_cache = {} if reads_cache is None else reads_cache
    placements = list_expected(con, campaign_id)
    try:
        prov = json.loads(camp.get("provenance") or "{}")
    except (ValueError, TypeError):
        prov = {}
    synthetic = bool(prov.get("synthetic")) or str(prov.get("brief_type") or "") == "synthetic"
    pool = [r["evidence_id"] for r in con.execute(
        "SELECT evidence_id FROM campaign_evidence WHERE campaign_id=?", (campaign_id,)).fetchall()]
    permitted_pool = []
    for evidence_id in pool:
        ref = citations.current_source_ref(con, "evidence", evidence_id)
        try:
            _captured_attachment(con, ref, ctx)
        except access.AuthorizationError:
            continue
        permitted_pool.append(evidence_id)
    pool = permitted_pool
    attached_everywhere = {r["ref"] for r in con.execute(
        "SELECT ref FROM placement_evidence WHERE workspace_id='local' AND campaign_id=?",
        (campaign_id,)).fetchall()}
    # Unassigned pool: campaign-level attachments with no per-placement
    # allocation stay suggestions until explicitly attached. Attached
    # refs compare by the evidence they resolve to, never by prefix.
    attached_ids = {_attached_evidence_id(con, a) for a in attached_everywhere}
    suggestions = [e for e in pool if e not in attached_ids]
    now = _now_iso()
    brand = camp.get("brand") or ""
    pins: dict = {"campaign_revision": camp["revision"], "placements": {},
                  "attachments": {}, "attachment_state": {}, "coverage": {},
                  "ad_groups": {}}
    eligible_by_placement, coverage_by_placement, problems_by, allocation = \
        _evaluate_placements(con, campaign_id, placements, brand, now, ctx,
                             reads_cache, reuse_overrides, pins)
    comparisons = {}
    for p in placements:
        ev = allocation["evaluations"].get(p["id"]) or {}
        _, rows = _comparison_source(con, p, eligible_by_placement[p["id"]],
                                     ev.get("eligible_ids") or [])
        problems = problems_by[p["id"]]
        comparisons[p["id"]] = rows + ([{
            "field": "(attachment)", "expected": None, "observed": None,
            "state": "unknown", "evidence_ref": None, "reason": "; ".join(problems),
            "provenance": p.get("provenance") or {}}] if problems else [])
    episode_verdicts = {p["id"]: _episode_verdicts(con, p, eligible_by_placement[p["id"]],
                                                   coverage_by_placement[p["id"]], brand, ctx,
                                                   reads_cache)
                        for p in placements}
    pins["brief_digest"] = _brief_digest(camp)
    pool_refs = {}
    for ev_id in pool:
        row = con.execute("SELECT source_version_id FROM evidence WHERE id=?",
                          (ev_id,)).fetchone()
        if row is not None:
            from . import citations
            pool_refs[ev_id] = citations.current_source_ref(con, "evidence", ev_id)
    pins["pool_refs"] = pool_refs
    # Source-derived scope titles are captured and bound by coverage-source pins.
    episode_titles: dict = {}
    for cov in coverage_by_placement.values():
        episode_titles.update((cov or {}).get("episode_labels") or {})
    unresolved = [p.get("episode_id") for p in placements
                  if p.get("scope") == "episode" and p.get("episode_id")
                  and p["episode_id"] not in episode_titles]
    for eid in sorted(set(unresolved)):
        from . import customer
        row = next((row for row in customer.permitted_episodes(con, ctx) if row["id"] == eid), None)
        if row and row["ep_title"]:
            episode_titles[eid] = row["ep_title"]
    synthetic_placements = sorted(
        p["id"] for p in placements
        if isinstance(p.get("provenance"), dict)
        and p["provenance"].get("synthetic") is True)
    try:
        brief_ref = json.loads(camp.get("brief_ref_json") or "{}")
    except (ValueError, TypeError):
        brief_ref = {}
    targets = [dict(r) for r in con.execute(
        "SELECT id, scope, show_id, episode_id, date_from, date_to,"
        " timezone, note, source_scope_json, revision FROM campaign_targets"
        " WHERE workspace_id='local' AND campaign_id=? ORDER BY created_at, id",
        (campaign_id,)).fetchall()] \
        if "mode" in {r["name"] for r in con.execute(
            "PRAGMA table_info(campaigns)").fetchall()} else []
    for target in targets:
        try:
            target["source_scope"] = json.loads(
                target.pop("source_scope_json") or "{}")
        except (ValueError, TypeError):
            target["source_scope"] = {}
        target["evidence"] = [r["ref"] for r in con.execute(
            "SELECT ref FROM target_evidence WHERE target_id=? AND workspace_id='local' ORDER BY ref",
            (target["id"],))]
        pins["attachments"][target["id"]] = target["evidence"]
        for ref in target["evidence"]:
            _captured_attachment(con, ref, ctx)
        coverage_by_placement[target["id"]] = _placement_coverage(con, target, ctx, brand,
                                                                  reads_cache)
        pins["coverage"][target["id"]] = _coverage_pin(coverage_by_placement[target["id"]])
    pins["targets"] = {t["id"]: t.get("revision", 1) for t in targets}
    pins["source_scopes"] = {t["id"]: t.get("source_scope") or {}
                             for t in targets}
    from . import citations, briefs, customer_query
    pins["coverage_sources"] = {}
    coverage_episodes = {eid for coverage in coverage_by_placement.values()
                         for eid in coverage.get("episodes", [])} | set(episode_titles)
    for eid in sorted(coverage_episodes):
        source_ref = citations.current_source_ref(con, "episode", eid)
        source = citations.resolve_source_state(con, source_ref, ctx=ctx, include_text=False)
        if "pins" not in source:
            raise access.AuthorizationError("coverage source fields are not disclosable", 403)
        pins["coverage_sources"][eid] = source_ref
    for placement in placements:
        source_scope = (placement.get("provenance") or {}).get("source_scope") or {}
        pins["source_scopes"][placement["id"]] = source_scope
    pins["source_scope_state"] = {}
    for unit_id, scope in pins["source_scopes"].items():
        if scope:
            source = citations.resolve_source_state(con, scope["source_ref"], ctx=ctx)
            if "pins" not in source:
                raise _ops.BadInput("campaign source scope requires full field authority")
            # The excerpt projection (Plan A) names no canonical ref: the
            # record's current publication is the same pin.
            pins["source_scope_state"][unit_id] = source.get("canonical_source_ref") \
                or citations.current_source_ref(con, source["kind"], source["record_id"])
    pins["brief_versions"] = {}
    brief_models = {}
    for unit_id, projection in [("campaign", brief_ref)] + [
            (p["id"], (p.get("provenance") or {}).get("brief_ref")) for p in placements]:
        if projection and projection.get("brief_id"):
            version = briefs.get_version(con, projection["brief_id"], projection["version"], ctx=ctx)
            if version["digest"] != projection["digest"]:
                raise _ops.BadInput("registered brief digest changed")
            pins["brief_versions"][unit_id] = {
                "brief_id": version["brief_id"], "version": version["version"], "digest": version["digest"]}
            brief_models[unit_id] = version
    research_scope = None
    if camp.get("mode") == "research" and brief_ref.get("source_query") is not None:
        # The normalized query and the view revision a Search bundle of it
        # would report, without building one (DEMO2 pass 5).
        from . import customer_contracts
        research_scope = {"query": customer_contracts.normalize_query(brief_ref["source_query"]),
                          "revision": customer_query._view_revision(con, ctx)}
        query = research_scope["query"]
        rows, info = _research_scope_match(con, query, ctx)
        if info["total_relation"] != "exact" or info["truncation_reason"]:
            raise _ops.BadInput("research source query is incomplete")
        pins["attachments"]["source_query"] = [row["ref"] for row in rows]
        research_scope = {"query": query, "revision": research_scope["revision"],
                          "refs": pins["attachments"]["source_query"]}
    pins["research_scope"] = research_scope
    pins["allocation"] = allocation
    assessment_scope = None
    if camp.get("mode") == "supplied_requirements":
        assessment_scope = {"basis": "entered_structured_requirements_only", "by_unit": {},
                            "unassessed_brief_conditions": True,
                            "limitations": ["Only entered supported checks are assessed; no whole-brief or legal compliance claim."]}
        for placement in placements:
            entered = sorted(k for k, value in (placement.get("requirements") or {}).items()
                             if k in {"message", "offer", "code", "duration_s", "position_s"} and value not in (None, ""))
            rows = comparisons[placement["id"]]
            unknown = sorted(r["field"] for r in rows if r["field"] in entered and r["state"] == "unknown")
            assessment_scope["by_unit"][placement["id"]] = {
                "entered_fields": entered, "assessed_fields": sorted({r["field"] for r in rows if r["field"] in entered and r["state"] != "unknown"}),
                "unknown_fields": unknown}
    mode = camp.get("mode")
    if mode is None:
        availability: dict = {"availability": "legacy_review_required",
                              "reasons": ["campaign predates typed modes"],
                              "allowed_actions": ["read", "stored_reports"]}
    else:
        availability = {"availability": "active",
                        "allowed_actions": customer_allowed_actions(camp)}
    return {"campaign": {**camp, "brand_display": campaign_brand_display(con, camp.get("brand"))},
            "mode": mode,
            "requirement_provenance": brief_ref or None,
            "placements": placements, "targets": targets,
            "show_titles": _draft_show_titles(con, placements + targets, ctx),
            "synthetic": synthetic,
            "synthetic_placements": synthetic_placements,
            "attachment_pool": sorted(pool), "pool_refs": pool_refs,
            "episode_titles": episode_titles,
            "suggestions": sorted(suggestions),
            "eligible_by_placement": {
                k: list((allocation["evaluations"].get(k) or {}).get("eligible_ids") or [])
                for k in eligible_by_placement},
            "coverage_by_placement": coverage_by_placement,
            "episode_verdicts": episode_verdicts,
            "comparisons": comparisons, "allocation": allocation,
            "assessment_scope": assessment_scope, "brief_versions": brief_models,
            "research_scope": research_scope,
            "reason_codes": {
                "evaluations": {k: reason_code_rows(v.get("reasons"))
                                for k, v in allocation["evaluations"].items()},
                "citations": {k: reason_code_rows(v.get("reasons"))
                              for k, v in allocation["citations"].items()}},
            "pins": pins, "built_at": now, **availability}


def _research_scope_match(con: sqlite3.Connection, query: dict, ctx=None):
    """customer_query._match_query2 of a research campaign's source query,
    remembered per research state, disclosure class and query, as Search
    is (customer.query; mention and episode queries add the transcript
    index state): the full match was half of a draft's 7-8 s first read
    and ran again on every read (CAMPAIGN-DRAFT-SLOW). Callers must not
    mutate the result."""
    from . import customer, customer_query
    tag = ("draft-scope", json.dumps(query, sort_keys=True))
    return customer_query.class_memo(
        con, ctx, tag, lambda c: customer_query._match_query2(con, query, ctx=c),
        state=customer._research_state(query.get("mode")), keep=4)


def _stale_reasons(current: dict, frozen: dict, status_only: bool = False) -> list[str]:
    """Staleness across brief, attachments and their pinned state,
    placements, and coverage. Unpinned inputs (unrelated settings, UI
    state) never stale a report.

    Only what changed is named (UAT-098): a unit (placement or target)
    added or removed is told once, by "placements" or "targets", not again
    by its own new per-unit entries (an empty source list, its coverage,
    scope or brief pin). A changed campaign revision that nothing else
    explains still stales the report, as "campaign", unless every step of
    it changed the status only (status_only, R1-08)."""
    if not isinstance(frozen, dict) or not frozen:
        return ["pins unavailable"]
    def without_notes(pins):
        # Pins stored before PRIVACY-FEED-FIX1 hold the freezer's own notes
        # ("notes", each attachment's "note_versions"); notes never stale.
        pins = {k: v for k, v in pins.items() if k != "notes"}
        if isinstance(pins.get("attachment_state"), dict):
            pins["attachment_state"] = {
                ref: {k: v for k, v in state.items() if k != "note_versions"}
                if isinstance(state, dict) else state
                for ref, state in pins["attachment_state"].items()}
        return pins
    current, frozen = without_notes(current), without_notes(frozen)
    def units(pins):
        return set(pins.get("placements") or {}) | set(pins.get("targets") or {})
    changed_units = units(current) ^ units(frozen)
    def kept(pins, key):
        value = pins.get(key)
        if not isinstance(value, dict):
            return value
        return {k: v for k, v in value.items() if k not in changed_units}
    def sources(pins):
        return {k: v for k, v in (pins.get("attachments") or {}).items() if v}
    reasons = []
    if current.get("brief_digest") != frozen.get("brief_digest"):
        reasons.append("brief")
    if sources(current) != sources(frozen):
        reasons.append("attachments")
    if current.get("attachment_state") != frozen.get("attachment_state"):
        cur_state = current.get("attachment_state") or {}
        old_state = frozen.get("attachment_state") or {}
        changed = sorted(set(cur_state) | set(old_state))
        detail = [r for r in changed if cur_state.get(r) != old_state.get(r)]
        reasons.append("attachments:" + ",".join(detail[:5]) if detail else "attachments")
    if current.get("placements") != frozen.get("placements"):
        reasons.append("placements")
    for dependency in ("coverage", "source_scopes", "source_scope_state", "brief_versions"):
        if kept(current, dependency) != kept(frozen, dependency):
            reasons.append(dependency)
    cur_eps, old_eps = current.get("coverage_sources"), frozen.get("coverage_sources")
    if changed_units and isinstance(cur_eps, dict) and isinstance(old_eps, dict):
        # Episodes that only joined or left with a unit are that unit's change.
        cur_eps = {k: v for k, v in cur_eps.items() if k in old_eps}
        old_eps = {k: v for k, v in old_eps.items() if k in cur_eps}
    if cur_eps != old_eps:
        reasons.append("coverage_sources")
    for dependency in ("targets", "allocation", "pool_refs"):
        if current.get(dependency) != frozen.get(dependency):
            reasons.append(dependency)
    def research_scope(pins):
        # T2: the query and its refs, not the view revision, which every
        # guarded workspace write moves.
        scope = pins.get("research_scope")
        return {k: v for k, v in scope.items() if k != "revision"} \
            if isinstance(scope, dict) else scope
    if research_scope(current) != research_scope(frozen):
        reasons.append("research_scope")
    # Decision 1: an occurrence whose ad changed (a regroup) stales the
    # report. Pins frozen before the mapping existed have none to compare;
    # added or removed attachments are "attachments", not a regroup.
    cur_groups, old_groups = kept(current, "ad_groups"), kept(frozen, "ad_groups")
    if isinstance(cur_groups, dict) and isinstance(old_groups, dict) and any(
            isinstance(cur_groups[u], dict) and isinstance(old_groups[u], dict)
            and any(cur_groups[u][o] != old_groups[u][o]
                    for o in cur_groups[u].keys() & old_groups[u].keys())
            for u in cur_groups.keys() & old_groups.keys()):
        reasons.append("ad_groups")
    if not reasons and current.get("campaign_revision") != frozen.get("campaign_revision") \
            and not status_only:
        reasons.append("campaign")
    return sorted(set(reasons))


# Snapshot columns that are not campaign content: Archive / Restore
# changes the status only, and every write advances the revision.
_STATUS_ONLY_IGNORED = frozenset({"campaign_revision", "revision", "status",
                                  "history_origin", "actor", "created_at"})


def status_only_changes(con: sqlite3.Connection, campaign_id: int,
                        since_revision, to_revision) -> bool:
    """True when every campaign revision after since_revision up to
    to_revision changed the status and nothing else (Archive / Restore,
    R1-08), read from the retained history snapshots. A missing snapshot,
    or a revision that left the status alone, is a real change. The
    revision itself still advances, so the write conflict rule holds."""
    try:
        since, to = int(since_revision), int(to_revision)
    except (TypeError, ValueError):
        return False
    if to <= since or not _db.has_history_tables(con):
        return False
    rows = {r["campaign_revision"]: r for r in con.execute(
        "SELECT * FROM campaign_history WHERE workspace_id='local'"
        " AND campaign_id=? AND campaign_revision BETWEEN ? AND ?",
        (campaign_id, since, to)).fetchall()}
    def content(row):
        return {k: row[k] for k in row.keys() if k not in _STATUS_ONLY_IGNORED}
    for rev in range(since + 1, to + 1):
        before, after = rows.get(rev - 1), rows.get(rev)
        if before is None or after is None or before["status"] == after["status"] \
                or content(before) != content(after):
            return False
    return True


def _report_stale_reasons(con: sqlite3.Connection, campaign_id: int,
                          current: dict, frozen: dict) -> list[str]:
    """_stale_reasons for one stored report: a campaign revision moved
    only by Archive / Restore never stales it (the dialog promises
    reports stay as they are)."""
    status_only = isinstance(frozen, dict) and status_only_changes(
        con, campaign_id, frozen.get("campaign_revision"), current.get("campaign_revision"))
    return _stale_reasons(current, frozen, status_only=status_only)


def _brief_digest(camp: dict) -> str:
    import hashlib as _hl
    return _hl.sha256(json.dumps(
        {"name": camp.get("name"), "brand": camp.get("brand"),
         "message": camp.get("message"), "offer": camp.get("offer"),
         "notes": camp.get("notes"), "target_shows": camp.get("target_shows")},
        sort_keys=True, default=str).encode()).hexdigest()[:24]


def _captured_attachment(con, ref, ctx=None, *, export=False):
    """One source as a report holds it. export=True (a frozen report): the
    CSV export rule (exports.export_locators). When transcript export is
    not granted the capture holds no transcript text: an ad keeps its own
    pinned excerpt, any other source its metadata only."""
    from . import citations
    from . import customer as _customer
    resolved = citations.resolve_source_state(con, ref, ctx=ctx)
    if export:
        from . import exports as _exports
        locators, full = _exports.export_locators(con, ref, ctx)
        if not full:
            return _excerpt_capture(con, ref, ctx, resolved, locators)
    excerpt = resolved.get("disclosure") == "excerpt"
    metadata_only = resolved.get("kind") == "episode" \
        and ((resolved.get("pins") or {}).get("transcript_version_id") is None or excerpt)
    if "pins" not in resolved or (not isinstance(resolved.get("text"), str)
                                  and not metadata_only and not excerpt):
        raise _ops.BadInput("full capture requires a qualified disclosed source")
    # Excerpt level (Plan A design 5): the capture is the excerpt projection
    # (its safe excerpt, or none) with pa1 locators on both sources.
    # The display-only truncated flag is not part of the captured bytes.
    captured = dict({k: v for k, v in resolved.items() if k != "truncated"},
                    disclosure=citations.captured_disclosure(con, ref, ctx),
                    capture_scope="metadata_only" if metadata_only else
                    "pa1_excerpt" if excerpt else "source_text")
    if isinstance(captured.get("assocs"), list):
        # PODS: model rows are display-only
        captured["assocs"] = [a for a in captured["assocs"] if a.get("basis") != "model"]
    if export and captured["capture_scope"] == "pa1_excerpt":
        # A frozen report's safe excerpt, cut as a CSV row's is (D7).
        from . import exports as _exports
        captured["text"] = _exports.cap_excerpt(captured["text"])
    if _customer.pinned_brand_under_review(con, resolved):
        # The customer never gets a brand still under review (decision 10).
        captured["assocs"] = []
    captured["capture_digest"] = citations.capture_digest(captured)
    return captured


def _excerpt_capture(con, ref, ctx, resolved, locators):
    """A frozen report's capture of ref under an excerpt-only export rule:
    the excerpt projection's identity and metadata (no window, segments,
    context or interval text), with the ad's pinned excerpt, cut to its
    pinned reviewed bounds (exports._pinned_excerpt, _reviewed_excerpt), as
    its text exactly as a CSV or JSON row keeps it (exports.prepare_capture)."""
    from . import citations, exports as _exports
    from . import customer as _customer
    if not ref.startswith("sr1:"):
        ref = citations.qualify_legacy_source_ref(con, ref)
    state_id, _window = citations._parse_source_ref(ref)
    publication = citations._resolve_publication(con, state_id, ref, ctx, media=False)
    narrowed = citations.excerpt_projection(con, publication, ref)
    narrowed.pop("truncated", None)
    evidence = publication["kind"] == "evidence"
    narrowed.update(text=_exports.cap_excerpt(_exports._pinned_excerpt(con, ref))
                    if evidence else None,
                    excerpt_bounds=None)
    captured = dict(narrowed, disclosure=locators,
                    capture_scope="export_excerpt" if evidence else "metadata_only")
    if isinstance(captured.get("assocs"), list):
        # PODS: model rows are display-only
        captured["assocs"] = [a for a in captured["assocs"] if a.get("basis") != "model"]
    if _customer.pinned_brand_under_review(con, resolved):
        captured["assocs"] = []
    captured["capture_digest"] = citations.capture_digest(captured)
    return captured


def _reviewed_excerpt(con, publication: dict, stored: str) -> str:
    """T2-EXCERPT: the excerpt a frozen report and an export row
    (exports._pinned_excerpt, T2-EXPORT) show for one pinned ad
    publication. The stored excerpt was cut at detection over the original
    bounds; a span fix (a review revision) narrows the read but never the
    stored excerpt, which then opens or closes on a neighbouring ad.

    When the pinned review bounds (the publication's own bounds_json, never
    live state) cut the original bounds to a narrower overlap, the excerpt
    is rebuilt from the pinned transcript version's segments that lie
    wholly inside that overlap (_segments_inside, the CODE-HEARD rule) and
    wholly inside the original bounds (no tolerance there): only text the
    original read already covered, so the result never widens disclosure.
    No such segments (or none retained): "" — the stored text is known to
    run past the reviewed bounds, so an empty excerpt is the safe
    direction. Reviewed bounds equal to, wider than, or wholly outside the
    original keep the stored excerpt unchanged, and so do non-finite
    bounds; a segment counts only with finite timings (_timed_segment)."""
    bounds = publication.get("bounds") or {}
    try:
        olo, ohi = (float(v) for v in bounds["original"])
        rlo, rhi = (float(v) for v in bounds["effective"])
    except (KeyError, TypeError, ValueError):
        return stored
    if not all(math.isfinite(v) for v in (olo, ohi, rlo, rhi)):
        return stored
    lo, hi = max(rlo, olo), min(rhi, ohi)
    if lo >= hi or (lo == olo and hi == ohi):
        return stored
    from . import observations
    vid = (publication.get("projection") or {}).get("transcript_version_id")
    kept = [s for s in _segments_inside(observations._segments_of(con, vid), lo, hi)
            if s["start"] >= olo and s["end"] <= ohi]
    return " ".join(str(s.get("text") or "").strip() for s in kept).strip()


# Design v3 (Exhibit) print look for standalone HTML exports and frozen
# reports. Self-contained (these files carry no external references and
# are served under a sandbox CSP); presentation only, never content.
REPORT_PRINT_CSS = (
    "body{font:15px/1.5 'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif;"
    "background:#fff;color:#1a1c20;max-width:900px;margin:24px auto;padding:0 16px}"
    "h1,h2,h3{font-family:'IBM Plex Serif',Georgia,serif;font-weight:600;margin:16px 0 8px}"
    "h1{font-size:28px;line-height:34px;border-bottom:1.5px solid #1a1c20;padding-bottom:8px}"
    "h2{font-size:19px;line-height:26px}h3{font-size:15px;line-height:22px}"
    "section{border-top:1px solid #1a1c20;margin-top:24px}"
    "table{border-collapse:collapse;margin:12px 0}"
    "th,td{border:1px solid #c9c2b3;padding:4px 8px;text-align:left;vertical-align:top}"
    "th{font-weight:600}"
    "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.45 'IBM Plex Mono',ui-monospace,Menlo,monospace}"
    "blockquote{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0;padding:6px 0;"
    "font:16px/1.6 'IBM Plex Serif',Georgia,serif;border-top:1px solid #1a1c20;border-bottom:1px solid #1a1c20}"
    "summary{font-weight:600}"
    "@page{size:auto;margin:16mm 14mm 18mm}"
    "@media print{body{margin:0;max-width:none;color:#000;font-size:10.5pt}"
    "h1,h2,h3{break-after:avoid;color:#000}a{color:#000}"
    "details>*{display:block}}")


def _frozen_episodes(frozen: dict, pid: str):
    """One placement's frozen episode verdicts, or None when it has none
    to show."""
    v = (frozen.get("episode_verdicts") or {}).get(pid)
    if isinstance(v, dict) and isinstance(v.get("episodes"), list) and v["episodes"] \
            and v.get("summary"):
        return v
    return None


def _summary_num(summary: dict, key: str) -> int:
    value = (summary or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _episode_linkage(summary: dict) -> str:
    """The linkage word the episode counts give one placement."""
    num = lambda key: _summary_num(summary, key)
    if num("heard") > 0:
        return "possible"
    if num("unconfirmed") > 0 or num("waiting_review") > 0:
        return "unreviewed"
    if num("checked") > 0 and num("not_checked") == 0:
        return "none"
    return "insufficient"


def _plural(n, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


def _episode_summary_text(summary: dict) -> str:
    """One placement's episode counts in words (heard, never delivered)."""
    num = lambda key: _summary_num(summary, key)
    unconfirmed, waiting, left = num("unconfirmed"), num("waiting_review"), num("not_checked")
    checked = num("checked") + unconfirmed + waiting
    return (f"Confirmed heard in {num('heard')} of {checked} checked episode{'' if checked == 1 else 's'}"
            + (f" · {_plural(unconfirmed, 'episode')} to confirm" if unconfirmed else "")
            + (f" · {_plural(waiting, 'episode')} waiting for review" if waiting else "")
            + (f" · {_plural(left, 'episode')} not checked" if left else ""))


def _mismatch_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "–".join(_mismatch_value(item) for item in value)
    if value is None:
        return ""
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return str(int(value))
    return str(value)


_MISMATCH_FIELD = {"offer": "offer", "message": "message", "duration_s": "length",
                   "position_s": "position"}


def _mismatch_clause(m: dict) -> str:
    said, brief = _mismatch_value(m.get("said")), _mismatch_value(m.get("brief"))
    field = m.get("field")
    if field == "code":
        if m.get("said") is None or m.get("said") == "":
            return f"no code heard (brief: {brief})"
        return f"wrong code: said {said}, brief says {brief}"
    if field == "url":
        return f"wrong URL: said {said}, brief says {brief}"
    return (f"doesn't match the brief: {_MISMATCH_FIELD.get(field, field)}"
            f" said {said}, brief says {brief}")


def _unconfirmed_text(read: dict) -> str:
    if not read.get("brief_code"):
        return "Heard · not confirmed"
    if read.get("machine_code"):
        return (f"Heard, code sounds like {read['machine_code']}"
                f" (brief: {read['brief_code']}) · not confirmed")
    return f"Heard, no code heard (brief: {read['brief_code']}) · not confirmed"


def _episode_verdict_text(ep: dict) -> str:
    """One episode's verdict in words."""
    verdict = ep.get("verdict")
    if verdict == "heard_right":
        return "Heard, right code" if ep.get("brief_code") else "Heard"
    if verdict == "heard_wrong":
        ms = sorted(ep.get("mismatch") or [],
                    key=lambda m: 0 if m.get("field") in ("code", "url") else 1)
        return ("Heard, " + "; ".join(_mismatch_clause(m) for m in ms) if ms
                else "Heard, doesn't match the brief")
    if verdict == "heard_unconfirmed":
        return _unconfirmed_text(ep)
    return {"waiting_review": "Waiting for review", "not_heard": "Not heard in this episode",
            "not_checked": "Not checked yet"}.get(verdict, "")


def _episode_review_text(ep: dict) -> str:
    """The judge's decision on an episode, with its time and never a name."""
    review = ep.get("review") or {}
    word = {"confirmed": "Confirmed", "rejected": "Rejected"}.get(review.get("decision"))
    if word is None:
        return ""
    return word + " · " + _report_utc_time(review["at"]) if review.get("at") else word


def _report_verdict(frozen: dict, episodes: bool = True) -> str:
    """One verdict line: a placement "has a result" only with complete
    coverage (a possible ad or a clean no-match). Ads waiting for review
    are no result yet and are named apart, never as a possible ad. Never
    verified, delivered or ran; citations are never counted. Ad
    vocabulary from campaign-report/4 (decision 1). With frozen episode
    verdicts a placement reads by its episodes (campaign-report/4.2)."""
    from . import customer
    placements = frozen.get("placements") or []
    cites = (frozen.get("allocation") or {}).get("citations") or {}
    covs = frozen.get("coverage_by_placement") or {}
    evs = frozen.get("evaluations") or {}
    counted = [p for p in placements if p["id"] not in cites]
    cite_n = len(placements) - len(counted)
    tail = (f" {cite_n} more {'is a citation' if cite_n == 1 else 'are citations'} only, never counted."
            if cite_n else "")
    if not counted:
        # B-8: with nothing counted there is no "more"; say what they are.
        if cite_n == 1:
            return "No placement is counted for credit. This placement is a citation only, never counted."
        if cite_n:
            return (f"No placement is counted for credit. All {cite_n} placements are"
                    " citations only, never counted.")
        return "No placement is counted for credit yet."
    n = {"possible": 0, "none": 0, "unreviewed": 0, "insufficient": 0}
    short = []
    for p in counted:
        cov = covs.get(p["id"])
        v = _frozen_episodes(frozen, p["id"]) if episodes else None
        if v:
            linkage = _episode_linkage(v["summary"])
        else:
            linkage, _label = customer.placement_linkage_label(
                (cov or {}).get("state"), (evs.get(p["id"]) or {}).get("outcome"), cov)
        n[linkage] += 1
        if linkage == "insufficient":
            short.append(cov)
    results = n["possible"] + n["none"]
    line = (f"{results} of {len(counted)} counted placement"
            f"{' has' if len(counted) == 1 else 's have'} a result")
    waiting = f"{n['unreviewed']} with ads waiting for review" if n["unreviewed"] else ""
    if not results and waiting:
        line += " yet — " + waiting
        if n["insufficient"]:
            line += f"; {n['insufficient']} without enough data"
        line += "."
    elif not results:
        if all(c and c.get("state") == "incomplete" for c in short):
            line += " yet — not every episode in scope has been fully checked for ads yet."
        elif not any(c and c.get("state") == "complete" for c in short):
            line += " yet — coverage isn't complete yet."
        else:
            line += " yet."
    else:
        parts = []
        if n["possible"]:
            parts.append(f"{n['possible']} with an ad found")
        if n["none"]:
            parts.append(f"{n['none']} with no matching ad in the available transcript")
        line += ": " + " and ".join(parts)
        if waiting:
            line += "; " + waiting
        if n["insufficient"]:
            line += f"; {n['insufficient']} without enough data"
        line += "."
    return line + tail


def _report_summary_rows(frozen: dict, episodes: bool = True) -> list:
    """The Summary's line per placement as (unit id, label, rest): linkage
    (by episodes when the report froze them), coverage, brief checks
    assessed of entered."""
    from . import customer
    cites = (frozen.get("allocation") or {}).get("citations") or {}
    covs = frozen.get("coverage_by_placement") or {}
    evs = frozen.get("evaluations") or {}
    by_unit = (frozen.get("assessment_scope") or {}).get("by_unit") or {}
    rows = []
    titles = _report_titles(frozen)
    for p in sorted(frozen.get("placements") or [],
                    key=lambda u: _window_order_key(u, _report_placement_label(u, titles))):
        cov = covs.get(p["id"]) or {}
        v = _frozen_episodes(frozen, p["id"]) if episodes else None
        if p["id"] in cites:
            linkage = "citation only, never counted"
        elif v:
            linkage = _episode_summary_text(v["summary"])
        else:
            _key, linkage = customer.placement_linkage_label(
                cov.get("state"), (evs.get(p["id"]) or {}).get("outcome"), cov)
        state = cov.get("state")
        # MKT-02: incomplete reads as the app's chip, "not fully checked".
        coverage_phrase = {"complete": "coverage complete",
                           "incomplete": "not fully checked"}.get(state, "coverage not checked")
        scope = by_unit.get(p["id"]) or {}
        checks = (f"{len(scope.get('assessed_fields') or [])}/"
                  f"{len(scope.get('entered_fields') or [])} assessed")
        rows.append((p["id"], _report_placement_label(p, titles),
                     f"{linkage}; {coverage_phrase}; brief checks: {checks}"))
    labels = _numbered_labels([label for _unit_id, label, _rest in rows])
    return [(unit_id, label, rest) for label, (unit_id, _old, rest) in zip(labels, rows)]


def _report_summary_html(frozen: dict, text) -> str:
    """Plain Summary for new freezes (campaign-report/3 on): the verdict,
    one line per placement (linkage, coverage, brief checks assessed of
    entered), and the synthetic notice. Research snapshots give no pass,
    fail or delivery credit. The full model dumps follow in the technical
    record."""
    parts = ["<section><h2>Summary</h2>"]
    if frozen.get("mode") == "research":
        count = len(frozen.get("targets") or [])
        parts.append(f"<p>Research snapshot: {count} research target{'' if count == 1 else 's'};"
                     " scope and evidence only, no pass, fail or delivery credit.</p>")
    else:
        parts.append("<p>" + text(_report_verdict(frozen)) + "</p>")
        items = ["<li>" + text(label) + " — " + text(rest) + "</li>"
                 for _unit_id, label, rest in _report_summary_rows(frozen)]
        if items:
            parts.append("<ul>" + "".join(items) + "</ul>")
    if frozen.get("synthetic") or frozen.get("synthetic_placements"):
        parts.append("<p>Brief type: synthetic — not contractual verification.</p>")
    parts.append("</section>")
    return "".join(parts)


def _window_order_key(unit: dict, label: str) -> tuple:
    """Placements read in window order: dated windows by start date, then
    by name; undated ones after them."""
    start = str(unit.get("date_from") or "")
    return (0 if start else 1, start, label)


def _numbered_labels(labels: list[str]) -> list[str]:
    """Show windows with the same dates would print identical lines (R-01):
    number only the labels that clash, in order."""
    clashes = collections.Counter(labels)
    seen = collections.Counter()
    out = []
    for label in labels:
        if clashes[label] > 1:
            seen[label] += 1
            label = label.replace("Show window", f"Show window {seen[label]}", 1)
        out.append(label)
    return out


def _report_utc_time(value) -> str:
    """A stored ISO time read in UTC: "Oct 16, 2026, 5:10 AM UTC" (frozen
    reports stay UTC, decision 14). A value without a readable time and
    zone is shown as stored, never guessed."""
    import datetime as _dt
    try:
        moment = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return str(value or "")
    if moment.tzinfo is None:
        return str(value)
    moment = moment.astimezone(_dt.timezone.utc)
    return (f"{moment.strftime('%b')} {moment.day}, {moment.year},"
            f" {moment.hour % 12 or 12}:{moment.minute:02d}"
            f" {'AM' if moment.hour < 12 else 'PM'} UTC")


def _render_report_html(frozen: dict) -> str:
    """Render the complete immutable research/supplied model, with no live
    reads (campaign-report/4): readable text first, then every model dump
    under one Technical record heading."""
    import html
    def text(value):
        return html.escape(str(value), quote=True)
    def model(value):
        return "<pre>" + text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)) + "</pre>"
    def label(key):
        words = key.replace("_", " ")
        return words[:1].upper() + words[1:]
    campaign = frozen["campaign"]
    parts = ["<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
             "<title>" + text(campaign["name"]) + "</title>",
             "<style>" + REPORT_PRINT_CSS + "</style></head><body>",
             "<h1>" + text(campaign["name"]) + "</h1>",
             "<p>Report " + text(frozen["report_no"]) + " captured "
             + text(_report_utc_time(frozen["captured_at"])) + ".</p>",
             "<p>Evidence captured at the recorded time; later corrections do not rewrite this file."
             " A recipient file cannot expire or observe future corrections.</p>",
             "<p>Research snapshots record scope and evidence only, with no pass, fail or delivery"
             " credit. Supplied assessments cover only entered supported requirements; unknown or"
             " unassessed conditions are not passed. No whole-brief or legal claim.</p>",
             _report_summary_html(frozen, text)]
    units = {unit["id"]: unit for unit in frozen["placements"] + frozen["targets"]}
    # A show window is named by its dates, as in the Summary (UAT-097);
    # the raw show id stays in the technical record.
    unit_of = lambda unit_id: units.get(unit_id) or {"scope": "source_query", "id": unit_id}
    titles = _report_titles(frozen)
    label_of = lambda unit_id: _report_placement_label(unit_of(unit_id), titles)
    unit_ids = sorted(frozen["captured"], key=lambda u: _window_order_key(unit_of(u), label_of(u)))
    headings = dict(zip(unit_ids, _numbered_labels([label_of(unit_id) for unit_id in unit_ids])))
    with_episodes = any(_frozen_episodes(frozen, p["id"]) for p in frozen["placements"])
    for unit_id in unit_ids:
        captures = frozen["captured"][unit_id]
        parts.append("<section><h2>" + text(headings[unit_id]) + "</h2>")
        v = _frozen_episodes(frozen, unit_id) if with_episodes else None
        if v:
            rows = []
            for ep in v["episodes"]:
                published = ep.get("published")
                name = " · ".join(x for x in [_report_short_date(published) if published else "",
                                              ep.get("show"),
                                              ep.get("episode_title") or ep.get("episode_id")] if x)
                review = _episode_review_text(ep)
                rows.append("<li>" + text(name) + " — " + text(_episode_verdict_text(ep))
                            + ("<br>" + text(review) if review else "") + "</li>")
            parts.append("<p>" + text(_episode_summary_text(v["summary"])) + "</p><ol>"
                         + "".join(rows) + "</ol>")
        if not captures:
            parts.append("<p>No captured evidence.</p>")
        for number, capture in enumerate(captures, 1):
            parts.append("<h3>Captured passage " + text(number) + "</h3><blockquote>"
                         + text(capture["text"]) + "</blockquote>")
        parts.append("</section>")
    parts.append("<section><h2>Technical record</h2>"
                 "<p>The exact data this report was frozen from.</p>")
    old_rest = {}
    if with_episodes:
        parts.append("<p>" + text(_report_verdict(frozen, episodes=False)) + "</p>")
        old_rest = {uid: rest for uid, _label, rest in _report_summary_rows(frozen, episodes=False)}
    for key in ("mode", "campaign", "assessment_scope", "brief_versions", "research_scope", "scope_sources"):
        parts.append("<h3>" + text(label(key)) + "</h3>" + model(frozen.get(key)))
    for unit_id in unit_ids:
        parts.append("<h3>" + text(headings[unit_id]) + "</h3>")
        v = _frozen_episodes(frozen, unit_id) if with_episodes else None
        if v:
            parts.append("<p>" + text(old_rest[unit_id]) + "</p>")
        unit_model = {"unit": unit_of(unit_id),
                      "evaluation": frozen["evaluations"].get(unit_id),
                      "comparisons": frozen["comparisons"].get(unit_id),
                      "coverage": frozen["coverage_by_placement"].get(unit_id)}
        if v:
            unit_model["episode_verdicts"] = v
        parts.append(model(unit_model))
        for number, capture in enumerate(frozen["captured"][unit_id], 1):
            parts.append("<details><summary>Captured passage " + text(number)
                         + ": exact source and provenance</summary>" + model(capture) + "</details>")
    for key in ("allocation", "pins", "synthetic", "synthetic_placements", "limitation"):
        parts.append("<h3>" + text(label(key)) + "</h3>" + model(frozen.get(key)))
    parts.append("</section>")
    return "\n".join(parts) + "</body></html>\n"


def _report_short_date(value: str) -> str:
    try:
        import datetime as _dt
        day = _dt.date.fromisoformat(str(value)[:10])
        return f"{day.strftime('%b')} {day.day}"
    except (ValueError, TypeError):
        return str(value or "")


def _report_window(placement: dict) -> str:
    start, end = placement.get("date_from") or "", placement.get("date_to") or ""
    if not start and not end:
        return ""
    if start and end and start[:4] == end[:4] and len(start) >= 10:
        return (f"{_report_short_date(start)}–{_report_short_date(end)},"
                f" {start[:4]}")
    left = f"{_report_short_date(start)}, {start[:4]}" if start else "start"
    right = f"{_report_short_date(end)}, {end[:4]}" if end else "latest"
    return f"{left}–{right}"


def _report_titles(frozen: dict) -> dict:
    """Episode titles for a report's headings: the draft's titles, then
    any title the report already captured in its sources' episode metadata
    (a research target's episode has no draft title). Never a live read."""
    titles = dict(frozen.get("episode_titles") or {})
    sources = [c for caps in (frozen.get("captured") or {}).values() for c in caps or []]
    sources += list((frozen.get("scope_sources") or {}).values())
    for source in sources:
        episode = (source or {}).get("episode") if isinstance(source, dict) else None
        if not isinstance(episode, dict):
            continue
        title = (episode.get("public") or {}).get("ep_title")
        if episode.get("episode_id") and isinstance(title, str) and title:
            titles.setdefault(episode["episode_id"], title)
    return titles


def _report_placement_label(placement: dict, titles: dict) -> str:
    """Readable placement heading from frozen-captured episode titles.

    Titles come from the frozen payload (captured at freeze from the
    draft), never live lookups: historical payloads without them fall
    back honestly to the episode id. A show window is named by its dates
    only, like the app: the frozen payload holds no show title, and the
    raw id stays in the placement section's model (F-11, UAT-097).
    """
    if placement.get("scope") == "source_query":  # a research campaign's query (c7)
        return "Research query results"
    window = _report_window(placement)
    suffix = f" · {window}" if window else ""
    if placement.get("scope") == "episode" and placement.get("episode_id"):
        name = titles.get(placement["episode_id"]) or placement["episode_id"]
        return f"Episode {name}{suffix}"
    return f"Show window{suffix}"


def _frozen_review(review):
    """A review as a frozen report keeps it: the decision and its time,
    never who made it."""
    return None if review is None else {"decision": review["decision"], "at": review["at"]}


def _frozen_episode_verdicts(con: sqlite3.Connection, verdicts: dict, ctx=None) -> tuple:
    """The draft's per-episode verdicts as a frozen report keeps them, and
    their disclosure locators. A row whose evidence cannot be exported
    (no ref, or refused as a CSV row would be) is left out with every
    other row of its placement: the placement then reads as a report
    without verdicts, so a withheld heard row never turns into a clean
    no-match and no count reveals it. Reviews keep decision and time."""
    from . import citations
    seen: dict = {}

    def locators(ref):
        # The whole check runs as a probe (export_permitted): a withheld
        # row leaves no disclosure receipt on ctx (review r2 F2B-SEC-R2-01);
        # the freeze's final authorize_retained records the kept ones.
        if ref not in seen:
            box: dict = {}

            def check(probe):
                box["locs"] = _captured_attachment(con, ref, probe, export=True)["disclosure"]
                citations.authorize_retained(con, {"disclosure": box["locs"]}, probe, export=True)
            try:
                ok = citations.export_permitted(ctx, check)
            except (citations.CitationError, _ops.BadInput):
                ok = False
            seen[ref] = box["locs"] if ok else None
        return seen[ref]

    out, found = {}, []
    for pid, v in verdicts.items():
        if v.get("summary") is None or not v.get("episodes"):
            out[pid] = {"episodes": [], "summary": None}
            continue
        kept, kept_locs = [], []
        for entry in v["episodes"]:
            ids = [(entry["evidence_id"], entry["evidence_ref"])] + [
                (r["evidence_id"], r["evidence_ref"]) for r in entry["reads"]]
            ids = [(eid, ref) for eid, ref in ids if eid is not None]
            locs = [None if ref is None else locators(ref) for _eid, ref in ids]
            if any(loc is None for loc in locs):
                kept = []
                break
            row = copy.deepcopy(entry)
            row["review"] = _frozen_review(row["review"])
            for read in row["reads"]:
                read["review"] = _frozen_review(read["review"])
            kept.append(row)
            kept_locs += [loc for group in locs for loc in group]
        if kept:
            found += kept_locs
        out[pid] = ({"episodes": kept, "summary": _verdict_summary(kept)} if kept
                    else {"episodes": [], "summary": None})
    return out, found


@_db.atomic
@_model_names_off
def freeze_customer_report(con: sqlite3.Connection, campaign_id: int,
                           expected_revision, expected_pins: dict,
                           actor: str = "owner", ctx=None) -> dict:
    """Freeze one immutable report transaction: revision/pin check, evaluate
    persisted attachments, capture, render, persist snapshot+export, commit
    once. Failure leaves neither partial report nor partial history. The
    returned freeze digest binds campaign, report number, bytes digest,
    pins and aggregate revision for later tamper-evident verification."""
    camp = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if camp is None:
        raise _ops.BadInput("unknown campaign")
    camp = dict(camp)
    if camp.get("mode") not in CAMPAIGN_MODES:
        raise _ops.BadInput("legacy campaign is read-only pending qualified disposition")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    if int(expected_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    draft = build_customer_draft(con, campaign_id, ctx=ctx)
    if draft["availability"] != "active":
        raise _ops.BadInput("legacy campaign definition is read-only pending qualified disposition")
    # The caller posts back the pins it was shown (shown_draft_pins).
    if not _pins_match(shown_draft_pins(con, draft["pins"], ctx), expected_pins or {}):
        raise _ops.Conflict(camp["revision"])
    # Stale revisions and pins get the conflict first (UAT-100).
    _refuse_archived(con, camp)
    now = _now_iso()
    existing_nos = [r["report_no"] for r in con.execute(
        "SELECT report_no FROM campaign_reports WHERE workspace_id='local'"
        " AND campaign_id=?", (campaign_id,)).fetchall()]
    report_no = (max(existing_nos) + 1) if existing_nos else 1
    captured = {}
    for prefs in draft["pins"]["attachments"].values():
        for ref in prefs:
            if ref not in captured:
                captured[ref] = _captured_attachment(con, ref, ctx, export=True)
    captured_by_placement = {
        pid: [captured[ref] for ref in refs]
        for pid, refs in draft["pins"]["attachments"].items()}
    frozen = {"campaign_id": campaign_id, "report_no": report_no,
              "campaign": camp, "placements": draft["placements"],
              "evaluations": draft["allocation"]["evaluations"],
              "comparisons": draft["comparisons"],
              "coverage_by_placement": draft["coverage_by_placement"],
              "episode_titles": draft.get("episode_titles") or {},
              "attachments": draft["pins"]["attachments"],
              "captured": captured_by_placement,
              "synthetic": draft["synthetic"],
              "synthetic_placements": draft.get("synthetic_placements") or [],
              "pins": draft["pins"], "captured_at": now,
              "renderer": RENDERER_VERSION,
              "limitation": "Evidence captured at the recorded time; does not reflect"
                            " later corrections."}
    from . import citations
    scope_sources = {unit_id: _captured_attachment(con, scope["source_ref"], ctx, export=True)
                     for unit_id, scope in draft["pins"]["source_scopes"].items() if scope}
    frozen["scope_sources"] = scope_sources
    frozen["mode"] = draft["mode"]
    frozen["targets"] = draft["targets"]
    frozen["research_scope"] = draft["research_scope"]
    # A frozen report names no member: the brief version without who saved it.
    frozen["brief_versions"] = {unit: {k: v for k, v in version.items() if k != "actor"}
                                for unit, version in draft["brief_versions"].items()}
    frozen["assessment_scope"] = draft["assessment_scope"]
    frozen["allocation"] = draft["allocation"]
    # Stable reason codes, parallel to each unit's reasons (from
    # campaign-report/3 on; older reports have none and keep their raw
    # reasons only).
    frozen["reason_codes"] = draft["reason_codes"]
    frozen["attachment_pool"] = draft["attachment_pool"]
    frozen["pool_refs"] = draft["pool_refs"]
    frozen["suggestions"] = draft["suggestions"]
    frozen["disclosure"] = [locator for source in list(captured.values()) + list(scope_sources.values())
                            for locator in source["disclosure"]]
    for scope_ref in draft["pins"]["coverage_sources"].values():
        frozen["disclosure"] += _captured_attachment(con, scope_ref, ctx, export=True)["disclosure"]
    for ref in draft["pool_refs"].values():
        frozen["disclosure"] += _captured_attachment(con, ref, ctx, export=True)["disclosure"]
    frozen["disclosure"].append(
        citations.resource_disclosure("campaign", campaign_id))
    frozen["disclosure"] += [
        citations.resource_disclosure("brief", version["brief_id"])
        for version in frozen["brief_versions"].values()]
    frozen["episode_verdicts"], verdict_locators = _frozen_episode_verdicts(
        con, draft.get("episode_verdicts") or {}, ctx)
    frozen["disclosure"] += verdict_locators
    citations.authorize_retained(con, frozen, ctx, export=True)
    html_body = _render_report_html(frozen)
    digest = hashlib.sha256(html_body.encode()).hexdigest()
    frozen_json = json.dumps(frozen, sort_keys=True, ensure_ascii=False)
    cur = con.execute("INSERT INTO exports (created_at, format, filter,"
                      " aggregate_rev, item_count, payload)"
                      " VALUES (?, 'html', ?, ?, ?, ?)",
                      (now, json.dumps({"customer_report": {"campaign": campaign_id,
                                                            "report_no": report_no}}),
                       _db.aggregate_revision(con), len(draft["placements"]), html_body))
    export_id = cur.lastrowid
    retained = {"disclosure": frozen["disclosure"],
                "report_json_digest": hashlib.sha256(frozen_json.encode("utf-8")).hexdigest()}
    retained["capture_digest"] = citations.capture_digest(retained)
    con.execute("UPDATE exports SET digest=?, title=?, captured_json=? WHERE id=?",
                (digest, f"campaign-{campaign_id}-report-{report_no}"[:160],
                 json.dumps(retained),
                 export_id))
    con.execute("INSERT INTO campaign_reports (workspace_id, campaign_id, report_no,"
                " campaign_revision, pins_json, report_json, export_id, export_digest,"
                " renderer_version, created_at)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, report_no, camp["revision"], json.dumps(draft["pins"]),
                 frozen_json, export_id, digest, RENDERER_VERSION, now))
    rev = _db.bump_revision(con)
    freeze_digest = hashlib.sha256(json.dumps(
        {"campaign_id": campaign_id, "report_no": report_no,
         "digest": digest, "pins": draft["pins"], "aggregate_rev": rev,
         "actor": actor, "captured_at": now},
        sort_keys=True).encode()).hexdigest()
    _db.audit(con, actor, "customer_report_freeze", "campaign", str(campaign_id),
              {"report_no": report_no, "digest": digest,
               "freeze_digest": freeze_digest}, rev)
    return {"campaign_id": campaign_id, "report_no": report_no,
            "export_id": export_id, "digest": digest,
            "freeze_digest": freeze_digest, "aggregate_rev": rev}


def shown_draft_pins(con: sqlite3.Connection, pins: dict, ctx=None) -> dict:
    """The draft pins a caller is shown. A caller the audio gate refuses
    gets each attachment's source pins without the recording pin (its
    identity and digest), as in the record (CLIPCORE-SEC-R4-02). Allowed
    and local callers get the pins unchanged; stored and report pins keep
    the recording."""
    from .customer import _audio_allowed
    from .media import audio_gated
    if not audio_gated(ctx):
        return pins
    memo: dict = {}
    shown = {}
    for ref, state in (pins.get("attachment_state") or {}).items():
        source = state.get("source_pins") if isinstance(state, dict) else None
        if isinstance(source, dict) and "recording" in source \
                and not _audio_allowed(con, ctx, source.get("episode_id"), memo):
            state = dict(state, source_pins={k: v for k, v in source.items()
                                             if k != "recording"})
        shown[ref] = state
    return dict(pins, attachment_state=shown)


def _pins_match(current: dict, expected: dict) -> bool:
    if not isinstance(expected, dict) or not expected:
        return False
    return _pin_value_equal(current, expected)


def _pin_value_equal(current, expected) -> bool:
    """JSON numbers are equivalent across browser roundtrips, never booleans."""
    if type(current) in (int, float) and type(expected) in (int, float):
        return (not isinstance(current, float) or math.isfinite(current)) and \
            (not isinstance(expected, float) or math.isfinite(expected)) and current == expected
    if isinstance(current, str) and isinstance(expected, str):
        # Reason is a str subclass: same text is the same pin.
        return current == expected
    if type(current) is not type(expected):
        return False
    if isinstance(current, dict):
        return current.keys() == expected.keys() and all(
            type(key) is str and _pin_value_equal(value, expected[key])
            for key, value in current.items())
    if isinstance(current, list):
        return len(current) == len(expected) and all(
            _pin_value_equal(left, right) for left, right in zip(current, expected))
    return current is None or isinstance(current, (str, bool)) and current == expected


def get_customer_report(con: sqlite3.Connection, campaign_id: int,
                        report_no: int, ctx=None, *, export: bool = True) -> dict:
    """Frozen snapshot (stored report JSON and parsed artifact) plus current
    stale status/reasons.

    The frozen JSON/HTML bytes never change; staleness is reported in the
    wrapper only, never injected into the stored artifact. Returning the
    stored artifact releases it, so export permission is checked (AA-01);
    export=False is only for callers that verify an entry without
    returning its content (the metadata history and archive listings).
    """
    from . import citations
    citations.authorize_resource(con, "campaign", campaign_id, ctx)
    row = con.execute("SELECT * FROM campaign_reports WHERE workspace_id='local'"
                      " AND campaign_id=? AND report_no=?", (campaign_id, report_no)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown report for this campaign")
    stored = dict(row)
    frozen = json.loads(stored["report_json"])
    from . import exports, citations
    from .access import AuthorizationError
    try:
        exports.load_export_bytes(con, stored["export_id"], ctx, export=export)
        citations.authorize_retained(con, frozen, ctx, export=export)
    except AuthorizationError as exc:
        if not export:
            raise
        # Judge export r2: the error names the transcript rule only when it
        # alone refused the report's stored export and frozen sources.
        exports.refuse_export(con, _report_disclosure(con, stored["export_id"], frozen), ctx, exc)
    current_pins = _current_draft_pins(con, campaign_id, ctx)
    if current_pins is None or current_pins == DRAFT_WITHHELD:
        return {"report": stored, "frozen": frozen, "stale": True,
                "stale_reasons": [current_pins or "campaign unavailable"],
                "payload_immutable": True}
    old_pins = json.loads(stored["pins_json"])
    reasons = _report_stale_reasons(con, campaign_id, current_pins, old_pins)
    return {"report": stored, "frozen": frozen, "stale": bool(reasons),
            "stale_reasons": reasons, "payload_immutable": True}


# ALERT-DENY-4: the stale reason of a report whose current draft a
# source's own deny withholds (_current_draft_pins); the campaign is
# there, the comparison is not.
DRAFT_WITHHELD = "rights withheld"


def _current_draft_pins(con: sqlite3.Connection, campaign_id: int, ctx=None) -> dict | str | None:
    """The current draft's pins, which a stored report's stale flags compare
    with; None ("campaign unavailable") when the draft cannot be built.
    ALERT-DENY FIX1 item 2: a draft a source's own policy refuses (FIX2
    ADC-R1-04: for any grant) is withheld the same way, so the report
    history and each report a caller may read still show, and nothing the
    draft registered stays on ctx; the draft itself still answers 403.
    ALERT-DENY-4: that draft is DRAFT_WITHHELD, not None. Any other
    refusal propagates, as before."""
    from . import access, citations
    before = set(ctx.disclosures) if ctx is not None else None
    try:
        return build_customer_draft(con, campaign_id, ctx=ctx)["pins"]
    except (_ops.BadInput, citations.RevisionUnavailable):
        return None
    except access.DisclosureRefused as exc:
        if ctx is None or not exc.own_policy:
            raise
        ctx.disclosures.intersection_update(before)
        return DRAFT_WITHHELD


def _report_disclosure(con: sqlite3.Connection, export_id, frozen: dict) -> list:
    from . import exports
    return exports.stored_disclosure(con, export_id) + list(frozen.get("disclosure") or [])


def report_export_refusal(con: sqlite3.Connection, campaign_id: int,
                          report_no: int, ctx=None) -> str | None:
    """"transcript_export" when a listed report is refused only because
    transcript export is not granted, else None."""
    from . import exports
    row = con.execute("SELECT export_id, report_json FROM campaign_reports"
                      " WHERE workspace_id='local' AND campaign_id=? AND report_no=?",
                      (campaign_id, report_no)).fetchone()
    disclosures = _report_disclosure(con, row["export_id"], json.loads(row["report_json"]))
    return exports.TRANSCRIPT_EXPORT_REFUSED \
        if exports.transcript_refusal(con, disclosures, ctx) else None


def report_exportable(con: sqlite3.Connection, campaign_id: int,
                      report_no: int, ctx=None) -> bool:
    """Whether a listed report's artifacts may be opened (export) or only
    its history read. Refuses exactly as a read of the report would; the
    export probe repeats only the checks export=True adds to that read
    and records nothing on ctx."""
    from . import citations, exports
    get_customer_report(con, campaign_id, report_no, ctx, export=False)
    row = con.execute("SELECT export_id, report_json FROM campaign_reports"
                      " WHERE workspace_id='local' AND campaign_id=? AND report_no=?",
                      (campaign_id, report_no)).fetchone()

    def check(probe_ctx):
        exports.load_export_bytes(con, row["export_id"], probe_ctx)
        citations.authorize_retained(con, json.loads(row["report_json"]), probe_ctx, export=True)
    return citations.export_permitted(ctx, check)


def list_customer_reports(con: sqlite3.Connection, campaign_id: int, ctx=None) -> list[dict]:
    """Retained report history for one campaign with current stale flags."""
    from . import access, citations, exports
    citations.authorize_resource(con, "campaign", campaign_id, ctx)
    if con.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone() is None:
        raise _ops.BadInput("unknown campaign")
    rows = con.execute("SELECT report_no, campaign_revision, pins_json, export_digest,"
                       " created_at, export_id, report_json FROM campaign_reports WHERE workspace_id='local'"
                       " AND campaign_id=? ORDER BY report_no", (campaign_id,)).fetchall()
    if not rows:
        return []  # nothing to mark stale: no draft build (DEMO2 pass 9)
    current_pins = _current_draft_pins(con, campaign_id, ctx)
    out = []
    for row in rows:
        before = set(ctx.disclosures) if ctx is not None else None
        # One unverifiable stored copy never hides the others: its row is
        # listed without bytes, and opening it still refuses (409).
        try:
            available = {"available": True, "exportable": report_exportable(
                con, campaign_id, row["report_no"], ctx)}
            if not available["exportable"]:
                refusal = report_export_refusal(con, campaign_id, row["report_no"], ctx)
                if refusal:
                    available["export_refusal"] = refusal
        except exports.DigestMismatch:
            available = {"available": False, "unavailable_reason": "unverifiable",
                         "exportable": False}
        except access.AuthorizationError as exc:
            # An artifact made before the change to Plan A (design 6): listed
            # with the plan refusal; opening it still refuses (403).
            if not citations.plan_refusal(con, _report_disclosure(
                    con, row["export_id"], json.loads(row["report_json"])), ctx):
                # Row 211 (F2B-APP-SEC-R2-02): a report a source's own policy
                # denies is withheld and registers nothing; opening it still
                # refuses (403). ADS-R1-02: its row stays, its number only.
                # Any other refusal fails the list, as before.
                if not isinstance(exc, access.DisclosureRefused) or ctx is None \
                        or not exc.own_policy:
                    raise
                ctx.disclosures.intersection_update(before)
                out.append({"report_no": row["report_no"], "rights_withheld": True})
                continue
            available = {"available": True, "exportable": False, "export_refusal": "plan"}
        old_pins = json.loads(row["pins_json"])
        if current_pins is None or current_pins == DRAFT_WITHHELD:
            reasons = [current_pins or "campaign unavailable"]
        else:
            reasons = _report_stale_reasons(con, campaign_id, current_pins, old_pins)
        out.append({"report_no": row["report_no"],
                    "campaign_revision": row["campaign_revision"],
                    "captured_at": row["created_at"], "digest": row["export_digest"],
                    "stale": bool(reasons), "stale_reasons": sorted(set(reasons)),
                    **available})
    return out


def get_customer_report_html(con: sqlite3.Connection, campaign_id: int,
                             report_no: int, ctx=None) -> tuple[str, str]:
    """Stored HTML bytes + digest; preview and download serve these bytes.

    The digest is verified against the stored bytes on every read:
    corruption fails loudly, never serves silently altered bytes. The
    preview releases the same bytes as the download, so both need export
    permission (AA-01).
    """
    get_customer_report(con, campaign_id, report_no, ctx)
    row = con.execute("SELECT e.payload, r.export_digest FROM campaign_reports r"
                      " JOIN exports e ON e.id=r.export_id"
                      " WHERE r.workspace_id='local' AND r.campaign_id=? AND r.report_no=?",
                      (campaign_id, report_no)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown report for this campaign")
    actual = hashlib.sha256((row["payload"] or "").encode()).hexdigest()
    if actual != row["export_digest"]:
        raise _ops.BadInput("stored report bytes fail digest verification")
    return row["payload"], row["export_digest"]


# campaign-report/3.1 (UAT 2026-10-15): placements named by show and in
# window order, the citation-only verdict (B-8). campaign-report/4
# (walkthrough decision 8, 2026-10-16): the capture date read in UTC,
# every model dump under one Technical record heading, plain wording.
# campaign-report/4.1 (FREEZE + MKT-02 polish): the verdict and coverage
# words read as the app's chips ("not fully checked").
# campaign-report/4.2 (FUNC-2b): with per-episode verdicts each placement
# lists its episodes and the Summary counts by them; the earlier placement
# lines move into the Technical record.
# campaign-report/4.3 (WIN3, UX STRINGS #4): each placement's episode
# summary reads "Confirmed heard in {n} of {m} checked episode(s)" (was
# "Heard in"); counts and tails unchanged.
# Stored reports keep the renderer and bytes they were frozen with.
RENDERER_VERSION = "campaign-report/4.3"
# Renderers whose stored document carries exactly one self-written
# <style> block that its sandboxed content policy may allow by hash.
STYLE_HASH_RENDERERS = ("campaign-report/3", "campaign-report/3.1", "campaign-report/4",
                        "campaign-report/4.1", "campaign-report/4.2", "campaign-report/4.3")


def report_style_src(con: sqlite3.Connection, campaign_id: int,
                     report_no: int, payload: str) -> str:
    """CSP style-src for one stored report, or "" for none.

    The report HTML is served under a sandbox CSP with default-src
    'none', which also blocks its own inline <style>. For new freezes
    (STYLE_HASH_RENDERERS) the policy allows exactly that block by its
    sha256, never 'unsafe-inline'. Older reports keep the policy they had,
    and no stored byte changes."""
    import base64
    row = con.execute("SELECT renderer_version FROM campaign_reports"
                      " WHERE workspace_id='local' AND campaign_id=? AND report_no=?",
                      (campaign_id, report_no)).fetchone()
    if row is None or row[0] not in STYLE_HASH_RENDERERS:
        return ""
    m = re.search(r"<style>(.*?)</style>", payload or "", re.S)
    if not m:
        return ""
    digest = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
    return f"style-src 'sha256-{digest}'"


# -- Campaign modes, research targets, supplied placements (K06) ------------
#
# Research campaigns evaluate discovered evidence against research
# criteria. Supplied-requirements campaigns evaluate against owner
# requirements captured in a server-registered provided brief
# (briefs.py: no extraction, no client authority flags). Campaign mode
# is chosen at creation and stays fixed after save. Research targets
# select research scope only; supplied placements carry explicit
# counts and entered structured requirements with registered brief
# provenance. No mode conversion exists.

CAMPAIGN_MODES = ("research", "supplied_requirements")
TARGET_SCOPES = ("episode", "show_window")
PLACEMENT_BINDINGS = ("eligible", "citation_only")
SUPPORTED_REQUIREMENT_KEYS = ("message", "offer", "code", "duration_s",
                              "position_s")
PARKED_REQUIREMENT_KEYS = ("read_style", "production_timing",
                           "insertion_method", "placement_position")


def _require_v8(con: sqlite3.Connection, what: str) -> None:
    if not _db.has_v8_tables(con):
        raise _ops.BadInput(f"{what} requires the v8 schema")


def _campaign_exists(con: sqlite3.Connection, campaign_id: int) -> dict:
    from . import conversion
    conversion.require_executable(con, "campaign", campaign_id)
    row = con.execute("SELECT * FROM campaigns WHERE id=?",
                      (campaign_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown campaign")
    if row["mode"] not in CAMPAIGN_MODES:
        raise _ops.BadInput("legacy campaign mode is read-only pending qualified disposition")
    problems = _definition_problems(con, dict(row))
    if problems:
        raise _ops.BadInput("legacy_review_required: " + "; ".join(problems))
    return dict(row)


ARCHIVED_MESSAGE = "This campaign is archived. Restore it to make changes."
# What a customer may do with a campaign: an archived one is read-only
# (reads, its stored reports, Restore); the write guard below agrees.
ACTIVE_ACTIONS = ("read", "edit", "targets", "placements", "freeze", "reports")
ARCHIVED_ACTIONS = ("read", "stored_reports", "restore")


def customer_allowed_actions(camp: dict) -> list[str]:
    return list(ARCHIVED_ACTIONS if camp.get("status") == "archived" else ACTIVE_ACTIONS)


def _refuse_archived(con: sqlite3.Connection, camp_or_id) -> None:
    """Refuse every write on an archived campaign except Restore."""
    status = camp_or_id.get("status") if isinstance(camp_or_id, dict) else (con.execute(
        "SELECT status FROM campaigns WHERE id=?", (camp_or_id,)).fetchone() or [None])[0]
    if status == "archived":
        raise _ops.Archived(ARCHIVED_MESSAGE)


def _capture_episode_scope(con: sqlite3.Connection,
                           episode_id: str) -> dict:
    """Atomically capture the authorized SourceRef1 + effective bounds."""
    from . import citations as _citations
    import math
    try:
        source_ref = _citations.current_source_ref(con, "episode", episode_id)
    except _citations.RevisionUnavailable:
        raise _ops.BadInput("episode has no authorized publication;"
                            " publish it before targeting")
    resolved = _citations.resolve_source_state(con, source_ref, include_text=False,
                                               media=False)
    if resolved["relationship"] != "current":
        raise _ops.BadInput("episode publication is not current; explicit producer publication required")
    bounds = dict(resolved["effective_bounds"])
    duration = (resolved["episode"].get("public") or {}).get("duration_min")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
            and math.isfinite(duration) and duration > 0:
        # Preserve whole-episode coverage: a short transcript must not define
        # away the uncovered part of the same pinned episode's declared span.
        bounds["end_s"] = max(bounds["end_s"], duration * 60.0)
    return {"source_ref": source_ref, "bounds": bounds,
            "bounds_basis": "full_effective_source"}


def create_target(con: sqlite3.Connection, campaign_id: int, scope: str,
                  show_id: str | None = None, episode_id: str | None = None,
                  date_from: str | None = None, date_to: str | None = None,
                  timezone: str = "UTC", note: str = "",
                  actor: str = "owner",
                  expected_campaign_revision=None) -> dict:
    """Create a research target: one episode or one proven show window.

    Research campaigns only; supplied campaigns use placements. Episode
    targets capture the currently authorized SourceRef1 and effective
    bounds atomically from the episode id. Name/note edits retain the
    captured scope; an explicit episode-scope replacement recaptures it.
    A supplied displayed parent revision is checked before any write.
    """
    _require_v8(con, "campaign targets")
    camp = _campaign_exists(con, campaign_id)
    if expected_campaign_revision is not None \
            and int(expected_campaign_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    _refuse_archived(con, camp)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(campaigns)")}
    if camp.get("mode") != "research":
        raise _ops.BadInput("targets belong to research campaigns;"
                            " supplied campaigns use placements")
    if scope not in TARGET_SCOPES:
        raise _ops.BadInput(f"invalid target scope: {scope!r}")
    source_scope: dict = {}
    if scope == "episode":
        if not episode_id or not con.execute(
                "SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
            raise _ops.BadInput("episode targets require a known episode")
        source_scope = _capture_episode_scope(con, episode_id)
    else:
        if not show_id:
            raise _ops.BadInput("show windows require a proven show id")
        from . import customer as _customer
        try:
            _customer.resolve_customer_show(con, show_id)
        except Exception as exc:
            raise _ops.BadInput(f"unknown show window: {exc}")
        # Open-ended show windows are allowed; a bounded window must order.
        if date_from and date_to and date_to < date_from:
            raise _ops.BadInput("target date_to must not precede date_from")
    try:
        ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise _ops.BadInput(f"unknown target timezone: {timezone!r}")
    target_id = "ct-" + uuid.uuid4().hex[:16]
    now = _now_iso()
    con.execute("INSERT INTO campaign_targets (id, workspace_id, campaign_id,"
                " scope, show_id, episode_id, date_from, date_to, timezone,"
                " note, source_scope_json, revision, created_at, updated_at)"
                " VALUES (?, 'local', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (target_id, campaign_id, scope, show_id, episode_id,
                 date_from, date_to, timezone or "UTC", note or "",
                 json.dumps(source_scope, sort_keys=True), now, now))
    changed = _touch_campaign(con, campaign_id, actor, "target_create",
                              {"target": target_id, "scope": scope})
    return {"id": target_id, "scope": scope, "source_scope": source_scope,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


def list_targets(con: sqlite3.Connection, campaign_id: int) -> list[dict]:
    _require_v8(con, "campaign targets")
    _campaign_exists(con, campaign_id)
    targets = [dict(r) for r in con.execute(
        "SELECT * FROM campaign_targets WHERE workspace_id='local'"
        " AND campaign_id=? ORDER BY created_at, id",
        (campaign_id,)).fetchall()]
    for target in targets:
        target["evidence"] = [r["ref"] for r in con.execute(
            "SELECT ref FROM target_evidence WHERE workspace_id='local'"
            " AND target_id=? ORDER BY ref", (target["id"],)).fetchall()]
    return targets


def delete_target(con: sqlite3.Connection, campaign_id: int, target_id: str,
                  actor: str = "owner") -> dict:
    _require_v8(con, "campaign targets")
    _refuse_archived(con, _campaign_exists(con, campaign_id))
    row = con.execute("SELECT id FROM campaign_targets WHERE workspace_id='local'"
                      " AND id=? AND campaign_id=?", (target_id, campaign_id)
                      ).fetchone()
    if row is None:
        raise _ops.BadInput("unknown target for this campaign")
    con.execute("DELETE FROM target_evidence WHERE workspace_id='local'"
                " AND target_id=?", (target_id,))
    con.execute("DELETE FROM campaign_targets WHERE workspace_id='local'"
                " AND id=?", (target_id,))
    changed = _touch_campaign(con, campaign_id, actor, "target_delete",
                              {"target": target_id})
    return {"id": target_id, "deleted": True,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


def _target_ref_misfit(con, target, ref):
    """Why an exact ref falls outside one target/placement scope, as a
    stable code, or None when it fits. The one scope rule behind attach,
    move and target evidence (_target_allows_ref); placement_evidence_fit
    runs it read-only so a picker can say why before submit."""
    from . import citations, customer, customer_contracts
    try:
        resolved = citations.resolve_source_state(con, ref, media=False)
    except citations.CitationError:
        return "unresolvable"
    episode_id = resolved["episode"]["episode_id"]
    if target["scope"] == "episode":
        if episode_id != target.get("episode_id"):
            return "other_episode"
        captured_scope = target.get("source_scope")
        if captured_scope is None:
            captured_scope = json.loads(target.get("source_scope_json") or "{}")
        if not captured_scope:
            provenance = target.get("provenance") or {}
            if isinstance(provenance, str):
                provenance = json.loads(provenance)
            captured_scope = provenance.get("source_scope") or {}
        if captured_scope:
            scope_source = citations.resolve_source_state(con, captured_scope["source_ref"],
                                                          media=False)
            if resolved["pins"]["transcript_version_id"] != scope_source["pins"]["transcript_version_id"]:
                return "transcript_version"
            span, bounds = resolved["selected_bounds"], captured_scope["bounds"]
            if span["start_s"] < bounds["start_s"] or span["end_s"] > bounds["end_s"]:
                return "outside_bounds"
        return None
    if target["scope"] == "show_window":
        from . import search
        try:
            included = customer.resolve_customer_show(con, target["show_id"])
        except search.FilterRejected:
            return "outside_show"
        if episode_id not in included:
            return "outside_show"
        day = customer_contracts.publication_day(resolved["episode"]["public"].get("pub_date"))
        if not day:
            return None if not target.get("date_from") and not target.get("date_to") \
                else "outside_dates"
        inside = (not target.get("date_from") or day >= target["date_from"]) and \
            (not target.get("date_to") or day <= target["date_to"])
        return None if inside else "outside_dates"
    return "unsupported_scope"


def _target_allows_ref(con, target, ref):
    return _target_ref_misfit(con, target, ref) is None


def placement_evidence_fit(con: sqlite3.Connection, campaign_id: int,
                           ref: str) -> dict:
    """Which placements of a supplied campaign would accept this exact ref
    (UAT-092): {placement id: None | misfit code}. Read-only; the attach
    route applies the same rule and stays the judge. A placement whose
    captured source cannot be read answers "unavailable"."""
    from . import citations
    camp = _campaign_exists(con, campaign_id)
    if camp.get("mode") != "supplied_requirements":
        raise _ops.BadInput("placement attachments require supplied-requirement mode")
    if not isinstance(ref, str) or not ref:
        raise _ops.BadInput("ref must be a nonempty versioned reference")
    ref = _qualify_attachment_ref(con, ref)
    fits = {}
    for row in con.execute("SELECT id FROM expected_placements WHERE campaign_id=?"
                           " ORDER BY id", (campaign_id,)).fetchall():
        try:
            fits[row["id"]] = _target_ref_misfit(
                con, _placement_row(con, campaign_id, row["id"]), ref)
        except (_ops.BadInput, citations.CitationError):
            fits[row["id"]] = "unavailable"
    return {"ref": ref, "placements": fits}


def attach_target_evidence(con: sqlite3.Connection, campaign_id: int,
                           target_id: str, ref: str, expected_revision,
                           actor: str = "owner") -> dict:
    """Attach target evidence bound to eligibility (K06).

    The ref must resolve through pinned authority and fall inside the
    target scope; in-scope evidence outside an eligible proof may only
    attach as citation_only context, never as eligibility.
    """
    _require_v8(con, "campaign targets")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    camp = _campaign_exists(con, campaign_id)
    if int(expected_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    _refuse_archived(con, camp)
    target = con.execute("SELECT * FROM campaign_targets WHERE workspace_id='local'"
                         " AND id=? AND campaign_id=?",
                         (target_id, campaign_id)).fetchone()
    if target is None:
        raise _ops.BadInput("unknown target for this campaign")
    target = dict(target)
    from . import citations
    if not ref.startswith("sr1:"):
        ref = citations.qualify_legacy_source_ref(con, ref)
    if not _target_allows_ref(con, target, ref):
        raise _ops.BadInput("evidence is outside the target scope")
    citations.require_window_in_bounds(con, ref)
    cur = con.execute("INSERT OR IGNORE INTO target_evidence (workspace_id,"
                      " target_id, ref, attached_at, attached_by)"
                      " VALUES ('local', ?, ?, ?, ?)",
                      (target_id, ref, _now_iso(), actor))
    if not cur.rowcount:
        return {"target_id": target_id, "ref": ref, "duplicate": True,
                "campaign_revision": camp["revision"],
                "aggregate_rev": _db.aggregate_revision(con)}
    changed = _touch_campaign(con, campaign_id, actor, "target_evidence_attach",
                              {"target": target_id, "ref": ref})
    return {"target_id": target_id, "ref": ref,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


def detach_target_evidence(con: sqlite3.Connection, campaign_id: int,
                           target_id: str, ref: str, expected_revision,
                           actor: str = "owner") -> dict:
    """Remove one observation link from a target of this campaign (G2-01).

    Only the target_evidence row goes; the evidence itself is untouched.
    A link that is already gone answers cleanly with removed False and
    changes nothing (no revision, no audit), whatever revision was sent,
    so a retried remove never reads as a conflict.
    """
    _require_v8(con, "campaign targets")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    camp = _campaign_exists(con, campaign_id)
    _refuse_archived(con, camp)
    if con.execute("SELECT 1 FROM campaign_targets WHERE workspace_id='local'"
                   " AND id=? AND campaign_id=?", (target_id, campaign_id)
                   ).fetchone() is None:
        raise _ops.BadInput("unknown target for this campaign")
    if not ref.startswith("sr1:"):
        from . import citations
        ref = citations.qualify_legacy_source_ref(con, ref)
    if con.execute("SELECT 1 FROM target_evidence WHERE workspace_id='local'"
                   " AND target_id=? AND ref=?", (target_id, ref)).fetchone() is None:
        return {"target_id": target_id, "ref": ref, "removed": False,
                "campaign_revision": camp["revision"],
                "aggregate_rev": _db.aggregate_revision(con)}
    if int(expected_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    con.execute("DELETE FROM target_evidence WHERE workspace_id='local'"
                " AND target_id=? AND ref=?", (target_id, ref))
    changed = _touch_campaign(con, campaign_id, actor, "target_evidence_detach",
                              {"target": target_id, "ref": ref})
    return {"target_id": target_id, "ref": ref, "removed": True,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


# -- Customer campaign/placement DTOs (B3 envelopes) --------------------------

CAMPAIGN_FIELDS = ("name", "mode", "status", "brand", "source_query",
                   "message", "offer", "notes", "requirement_provenance")
PLACEMENT_FIELDS = ("scope", "show_id", "episode_id", "date_from", "date_to",
                    "timezone", "expected_count", "requirements",
                    "provenance", "binding")


def customer_list_summaries(con: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Customer directory summary on each campaign row, read in the
    caller's transaction: placement and target counts by aggregate
    query, the customer state, and the same availability the detail DTO
    reports. Never builds a draft; stored columns stay as they were."""
    if not rows:
        return rows
    placements = {r[0]: r[1] for r in con.execute(
        "SELECT campaign_id, COUNT(*) FROM expected_placements GROUP BY campaign_id")}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    targets = {r[0]: r[1] for r in con.execute(
        "SELECT campaign_id, COUNT(*) FROM campaign_targets WHERE workspace_id='local'"
        " GROUP BY campaign_id")} if "campaign_targets" in tables else {}
    observations = {r[0]: r[1] for r in con.execute(
        "SELECT t.campaign_id, COUNT(*) FROM target_evidence e JOIN campaign_targets t"
        " ON t.id=e.target_id WHERE e.workspace_id='local' GROUP BY t.campaign_id")} \
        if "target_evidence" in tables else {}
    from . import customer
    for row in rows:
        row["placement_count"] = placements.get(row["id"], 0)
        row["target_count"] = targets.get(row["id"], 0)
        row["observation_count"] = observations.get(row["id"], 0)
        row["state"] = customer.campaign_customer_state(con, row["id"])
        row["brand_display"] = campaign_brand_display(con, row.get("brand"))
        row["availability"] = ("legacy_review_required" if _definition_problems(con, row)
                               else "active")
    return rows


def campaign_projection(con: sqlite3.Connection, camp: dict) -> dict:
    """Customer campaign DTO: fixed mode, provenance, scope, actions.

    Legacy mode/null rows project availability legacy_review_required
    with read-only stored-report actions; no mode is inferred.
    """
    cols = {r["name"] for r in con.execute("PRAGMA table_info(campaigns)")}
    mode = camp.get("mode") if "mode" in cols else None
    ref = {}
    if "brief_ref_json" in cols:
        try:
            ref = json.loads(camp.get("brief_ref_json") or "{}")
        except (ValueError, TypeError):
            ref = {}
    targets = []
    if "mode" in cols:
        try:
            targets = [dict(r) for r in con.execute(
                "SELECT id, scope, show_id, episode_id, date_from, date_to,"
                " timezone, note, source_scope_json, revision FROM"
                " campaign_targets WHERE workspace_id='local' AND campaign_id=?"
                " ORDER BY created_at, id", (camp["id"],)).fetchall()]
        except sqlite3.Error:
            targets = []
    for target in targets:
        try:
            target["source_scope"] = json.loads(
                target.pop("source_scope_json") or "{}")
        except (ValueError, TypeError):
            target["source_scope"] = {}
    problems = _definition_problems(con, camp)
    if problems:
        return {"id": camp["id"], "mode": mode, "revision": camp["revision"],
                "name": camp.get("name"), "brand": camp.get("brand"),
                "brand_display": campaign_brand_display(con, camp.get("brand")),
                "notes": camp.get("notes") or "",
                "availability": "legacy_review_required", "reasons": problems,
                "allowed_actions": ["read", "stored_reports"],
                "requirement_provenance": None, "targets": targets}
    return {"id": camp["id"], "mode": mode, "revision": camp["revision"],
            "name": camp.get("name"), "status": camp.get("status"),
            "brand": camp.get("brand"),
            "brand_display": campaign_brand_display(con, camp.get("brand")),
            "source_query": ref.get("source_query"),
            "dates": {"from": camp.get("date_from"),
                      "to": camp.get("date_to")},
            "notes": camp.get("notes") or "",
            "requirement_provenance": ref or None,
            "availability": "active",
            "allowed_actions": customer_allowed_actions(camp),
            "targets": targets}


def _check_source_query(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _ops.BadInput("source_query must be null or a Query2 object")
    from . import customer_contracts as _contracts
    try:
        return _contracts.normalize_query(value)
    except Exception as exc:
        raise _ops.BadInput(f"invalid source_query: {exc}")


def _check_provenance_input(con: sqlite3.Connection, value) -> dict:
    from . import briefs as _briefs
    if not isinstance(value, dict) or set(value) != {"brief_ref"}:
        raise _ops.BadInput("requirement_provenance holds brief_ref only")
    for flag in ("synthetic", "contractual"):
        if flag in value:
            raise _ops.BadInput(
                f"provenance {flag} is server-known, not a client input")
    return _briefs.resolve_ref(con, value.get("brief_ref"))


@_db.atomic
def save_customer_campaign(con: sqlite3.Connection, body: dict | None,
                           campaign_id=None, expected_revision=None,
                           actor: str = "owner") -> dict:
    """Create/update a campaign through the B3 envelope (mode fixed).

    Create requires research/supplied_requirements mode; research has
    null requirement provenance and no expected_count, supplied carries
    an exact registered brief ref. A PUT that changes a saved
    campaign's mode returns 400 with a mode field error and no writes.
    """
    if not isinstance(body, dict) or set(body) != {"campaign"}:
        raise _ops.BadInput("body holds the campaign object only")
    fields = body["campaign"]
    if not isinstance(fields, dict):
        raise _ops.BadInput("campaign must be an object")
    unknown = set(fields) - set(CAMPAIGN_FIELDS)
    if unknown:
        raise _ops.BadInput(f"unknown campaign fields: {sorted(unknown)}")
    if campaign_id is None:
        mode = fields.get("mode")
        if mode not in CAMPAIGN_MODES:
            raise _ops.BadInput("campaign mode is required: 'research' or"
                                " 'supplied_requirements'")
        name = str(fields.get("name") or "").strip()
        if not name:
            raise _ops.BadInput("campaign name is required")
        status = fields.get("status", "active")
        if status not in ("active", "archived"):
            raise _ops.BadInput("invalid campaign status")
        brand = fields.get("brand")
        if brand is not None and (not isinstance(brand, str) or not brand):
            raise _ops.BadInput("brand must be a nonempty string or null")
        source_query = _check_source_query(fields.get("source_query"))
        provenance: dict = {}
        if mode == "research":
            if fields.get("requirement_provenance") is not None:
                raise _ops.BadInput("research campaigns carry no"
                                    " requirement provenance")
            provenance = {"source_query": source_query}
        else:
            if fields.get("requirement_provenance") is None:
                raise _ops.BadInput("supplied campaigns require"
                                    " requirement_provenance with brief_ref")
            provenance = _check_provenance_input(
                con, fields.get("requirement_provenance"))
        now = _now_iso()
        cur = con.execute("INSERT INTO campaigns (name, status, brand, message,"
                          " offer, notes, mode, brief_ref_json, revision,"
                          " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                          (name[:160], status,
                           brand.strip().lower() if brand else None,
                           str(fields.get("message") or "")[:2000],
                           str(fields.get("offer") or "")[:1000],
                           str(fields.get("notes") or "")[:4000], mode,
                           json.dumps(provenance, sort_keys=True), now))
        cid = cur.lastrowid
        if _db.has_history_tables(con):
            _db.record_campaign_snapshot(con, cid, actor)
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "campaign_create", "campaign", str(cid),
                  {"name": name, "mode": mode}, rev)
        row = con.execute("SELECT * FROM campaigns WHERE id=?",
                          (cid,)).fetchone()
        return {"campaign": campaign_projection(con, dict(row)),
                "aggregate_rev": rev}
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT * FROM campaigns WHERE id=?",
                      (campaign_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown campaign")
    camp = dict(row)
    from . import conversion
    conversion.require_executable(con, "campaign", campaign_id)
    if camp.get("mode") not in CAMPAIGN_MODES:
        raise _ops.BadInput("legacy campaign is read-only pending qualified disposition")
    if int(expected_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    if fields != {"status": "active"}:
        _refuse_archived(con, camp)
    if "mode" in fields and fields["mode"] != camp.get("mode"):
        raise _ops.BadInput("campaign mode is fixed after creation;"
                            " create a separate campaign for the other type")
    stored_ref: dict = {}
    try:
        stored_ref = json.loads(camp.get("brief_ref_json") or "{}")
    except (ValueError, TypeError):
        stored_ref = {}
    sets, args = [], []
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise _ops.BadInput("campaign name is required")
        sets.append("name=?")
        args.append(name[:160])
    if "status" in fields:
        if fields["status"] not in ("active", "archived"):
            raise _ops.BadInput("invalid campaign status")
        sets.append("status=?")
        args.append(fields["status"])
    if "brand" in fields:
        brand = fields["brand"]
        if brand is not None and (not isinstance(brand, str) or not brand):
            raise _ops.BadInput("brand must be a nonempty string or null")
        sets.append("brand=?")
        args.append(brand.strip().lower() if brand else None)
    for key in ("message", "offer", "notes"):
        if key in fields:
            cap = 2000 if key != "offer" else 1000
            if key == "notes":
                cap = 4000
            sets.append(f"{key}=?")
            args.append(str(fields[key] or "")[:cap])
    if "source_query" in fields:
        if camp.get("mode") != "research":
            raise _ops.BadInput("source_query belongs to research campaigns")
        stored_ref["source_query"] = _check_source_query(
            fields["source_query"])
        sets.append("brief_ref_json=?")
        args.append(json.dumps(stored_ref, sort_keys=True))
    if "requirement_provenance" in fields:
        if camp.get("mode") != "supplied_requirements":
            raise _ops.BadInput("requirement provenance belongs to"
                                " supplied-requirement campaigns")
        if fields["requirement_provenance"] is None:
            raise _ops.BadInput("supplied campaigns keep their registered"
                                " brief ref; create a new campaign to restart")
        stored_ref = _check_provenance_input(
            con, fields["requirement_provenance"])
        sets.append("brief_ref_json=?")
        args.append(json.dumps(stored_ref, sort_keys=True))
    if not sets:
        raise _ops.BadInput("no known campaign fields to update")
    con.execute(f"UPDATE campaigns SET {', '.join(sets)},"
                " updated_at=? WHERE id=?", (*args, _now_iso(), campaign_id))
    changed = _touch_campaign(con, campaign_id, actor, "campaign_update", {})
    fresh = con.execute("SELECT * FROM campaigns WHERE id=?",
                        (campaign_id,)).fetchone()
    return {"campaign": campaign_projection(con, dict(fresh)),
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}


def _check_requirements(requirements) -> dict:
    if not isinstance(requirements, dict):
        raise _ops.BadInput("requirements must be an object")
    unknown = set(requirements) - set(SUPPORTED_REQUIREMENT_KEYS)
    if unknown:
        parked = sorted(set(unknown) & set(PARKED_REQUIREMENT_KEYS))
        if parked:
            raise _ops.BadInput(
                f"unsupported parked requirement fields: {parked}")
        raise _ops.BadInput(f"unknown requirement fields: {sorted(unknown)}")
    parked_present = [k for k in PARKED_REQUIREMENT_KEYS if k in requirements]
    if parked_present:
        raise _ops.BadInput(
            f"unsupported parked requirement fields: {sorted(parked_present)}")
    checked: dict = {}
    for key in ("message", "offer", "code"):
        value = requirements.get(key)
        if value is not None and not isinstance(value, str):
            raise _ops.BadInput(f"requirement {key} must be a string or null")
        checked[key] = value
    for key in ("duration_s", "position_s"):
        value = requirements.get(key)
        if value is None:
            checked[key] = None
            continue
        if not isinstance(value, dict) or set(value) - {"min", "max"}:
            raise _ops.BadInput(f"requirement {key} must be null or"
                                " {min,max}")
        lo, hi = value.get("min"), value.get("max")
        for bound in (lo, hi):
            if bound is not None and (
                    isinstance(bound, bool) or not isinstance(bound, (int, float))
                    or bound != bound or bound in (float("inf"), float("-inf"))
                    or bound < 0):
                raise _ops.BadInput(f"requirement {key} bounds must be finite"
                                    " nonnegative numbers or null")
        if lo is not None and hi is not None and hi < lo:
            raise _ops.BadInput(f"requirement {key} max must not precede min")
        checked[key] = {"min": lo, "max": hi}
    return checked


def _operator_requirements(checked: dict) -> dict:
    """Store ranges in the operator [min,max] list shape; the B3 wire
    keeps the {min,max} mapping."""
    out = dict(checked)
    for key in ("duration_s", "position_s"):
        value = out.get(key)
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict):
            out[key] = [value.get("min"), value.get("max")]
    return out


def placement_allocation_state(con: sqlite3.Connection,
                               placement_id: str) -> dict:
    """Fresh/stale allocation marker for placement reads and edits.

    Stale is a distinct reported state when frozen evaluations predate
    the current placement revision; it never silently re-evaluates.
    """
    rows = con.execute("SELECT campaign_revision FROM placement_evaluations"
                       " WHERE placement_id=? ORDER BY id DESC LIMIT 1",
                       (placement_id,)).fetchone()
    if rows is None:
        return {"allocation_state": "fresh", "allocation_reasons": []}
    return {"allocation_state": "stale",
            "allocation_reasons": ["frozen evaluations predate the current"
                                   " placement revision; re-freeze to refresh"]}


def _check_placement_body(con: sqlite3.Connection, campaign_id: int,
                          body: dict, creating: bool) -> dict:
    camp = _campaign_exists(con, campaign_id)
    if camp.get("mode") != "supplied_requirements":
        raise _ops.BadInput("placements belong to supplied-requirement"
                            " campaigns; research campaigns use targets")
    if not isinstance(body, dict) \
            or set(body) - {"placement", "expected_campaign_revision"} \
            or "placement" not in body:
        raise _ops.BadInput("body holds the placement object and"
                            " expected_campaign_revision only")
    fields = body["placement"]
    if not isinstance(fields, dict):
        raise _ops.BadInput("placement must be an object")
    unknown = set(fields) - set(PLACEMENT_FIELDS)
    if unknown:
        raise _ops.BadInput(f"unknown placement fields: {sorted(unknown)}")
    return camp


@_db.atomic
def save_customer_placement(con: sqlite3.Connection, campaign_id: int,
                            body: dict | None, placement_id: str | None = None,
                            expected_revision=None,
                            expected_campaign_revision=None,
                            actor: str = "owner") -> dict:
    """Create/update a supplied placement through the B3 envelope.

    Creates check the displayed parent campaign revision; edits carry
    the displayed child revision too. Parked advanced keys are rejected
    without mutation, including null values. Episode placements capture
    the authorized source scope atomically from the episode id.
    """
    if expected_campaign_revision is None and isinstance(body, dict):
        expected_campaign_revision = body.get("expected_campaign_revision")
    if expected_campaign_revision is None:
        raise _ops.MissingPrecondition(
            "expected_campaign_revision is required")
    camp = _check_placement_body(con, campaign_id, body, placement_id is None)
    if int(expected_campaign_revision) != camp["revision"]:
        raise _ops.Conflict(camp["revision"])
    fields = body["placement"]
    previous = None
    if placement_id is not None:
        previous = _placement_row(con, campaign_id, placement_id)
        # A stale displayed placement revision is a conflict before the
        # archived refusal, as everywhere else (UAT-100).
        if expected_revision is not None and int(expected_revision) != previous["revision"]:
            raise _ops.Conflict(previous["revision"])
    _refuse_archived(con, camp)
    scope = fields.get("scope", previous["scope"] if previous else "show_window")
    if scope not in TARGET_SCOPES:
        raise _ops.BadInput(f"unknown placement scope: {scope!r}")
    episode_id = fields.get("episode_id", previous.get("episode_id") if previous else None)
    source_scope: dict = {}
    if scope == "episode":
        if not episode_id or not con.execute(
                "SELECT 1 FROM episodes WHERE id=?", (episode_id,)).fetchone():
            from . import citations
            if not episode_id:
                raise _ops.BadInput("episode_id is required for episode placements")
            raise citations.RevisionUnavailable("episode placement source is unavailable")
        source_scope = _capture_episode_scope(con, episode_id) \
            if previous is None or "episode_id" in fields or "scope" in fields \
            else (previous.get("provenance") or {}).get("source_scope", {})
    elif previous is None or fields.get("show_id", previous.get("show_id")) != previous.get("show_id") \
            or fields.get("scope", previous["scope"]) != previous["scope"]:
        # A new or changed show window needs its proven show, as research
        # targets do. An edit that keeps the stored show is not rechecked.
        show_id = fields.get("show_id", previous.get("show_id") if previous else "")
        if not show_id:
            raise _ops.BadInput("show windows require a proven show id")
        from . import customer as _customer
        try:
            _customer.resolve_customer_show(con, show_id)
        except Exception as exc:
            raise _ops.BadInput(f"unknown show window: {exc}")
    if placement_id is None:
        expected_count = fields.get("expected_count")
        if not isinstance(expected_count, int) \
                or isinstance(expected_count, bool) or expected_count <= 0:
            raise _ops.BadInput("supplied placements require an explicit"
                                " positive expected_count")
        requirements = _check_requirements(fields.get("requirements") or {})
        provenance = fields.get("provenance")
        if not isinstance(provenance, dict):
            raise _ops.BadInput("supplied placements require provenance"
                                " with brief_ref")
        for flag in ("synthetic", "contractual"):
            if flag in provenance:
                raise _ops.BadInput(
                    f"provenance {flag} is server-known, not a client input")
        resolved = _check_provenance_input(con, provenance)
        binding = fields.get("binding")
        if binding not in PLACEMENT_BINDINGS:
            raise _ops.BadInput("supplied placements require an explicit"
                                " binding: 'eligible' or 'citation_only'")
        stored_prov = {"brief_ref": resolved}
        stored_prov.update(resolved)
        stored_prov["binding"] = binding
        if source_scope:
            stored_prov["source_scope"] = source_scope
        out = save_expected_placement(
            con, campaign_id, scope, fields.get("show_id", ""), episode_id,
            fields.get("date_from"), fields.get("date_to"),
            fields.get("timezone", "UTC"), expected_count,
            _operator_requirements(requirements),
            {"brief_ref": provenance["brief_ref"],
             "source_scope": source_scope} if source_scope
            else {"brief_ref": provenance["brief_ref"]},
            0, actor)
        con.execute("UPDATE expected_placements SET provenance=? WHERE id=?",
                    (json.dumps(stored_prov, sort_keys=True), out["placement_id"]))
        changed = _touch_campaign(con, campaign_id, actor, "placement_create",
                                  {"placement": out["placement_id"]})
        row = con.execute("SELECT * FROM expected_placements WHERE id=?",
                          (out["placement_id"],)).fetchone()
        placed = dict(row)
        placed["requirements"] = requirements
        placed["provenance"] = stored_prov
        placed["source_scope"] = source_scope
        placed["binding"] = binding
        state = placement_allocation_state(con, placed["id"])
        camp_rev = con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (campaign_id,)).fetchone()["revision"]
        return {"placement": placed,
                "campaign_revision": camp_rev,
                "aggregate_rev": changed["aggregate_rev"], **state}
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    current = con.execute("SELECT * FROM expected_placements WHERE id=?"
                          " AND campaign_id=?", (placement_id, campaign_id)
                          ).fetchone()
    if current is None:
        raise _ops.BadInput("unknown placement for this campaign")
    strict: dict = {}
    for key in ("scope", "show_id", "episode_id", "date_from", "date_to",
                "timezone", "expected_count", "requirements", "provenance",
                "binding"):
        if key in fields:
            strict[key] = fields[key]
    if "requirements" in strict:
        strict["requirements"] = _operator_requirements(
            _check_requirements(strict["requirements"]))
    if "provenance" in strict:
        prov = strict["provenance"]
        if not isinstance(prov, dict):
            raise _ops.BadInput("provenance holds brief_ref only")
        for flag in ("synthetic", "contractual"):
            if flag in prov:
                raise _ops.BadInput(
                    f"provenance {flag} is server-known, not a client input")
        resolved = _check_provenance_input(con, prov)
        strict["provenance"] = {"brief_ref": resolved}
        if source_scope:
            strict["provenance"]["source_scope"] = source_scope
    if "expected_count" in strict:
        count = strict["expected_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise _ops.BadInput("expected_count must stay a positive integer")
    binding_changed = False
    if "binding" in strict:
        if strict["binding"] not in PLACEMENT_BINDINGS:
            raise _ops.BadInput("invalid placement binding;"
                                " 'eligible' or 'citation_only'")
        try:
            prior_prov = json.loads(current["provenance"] or "{}")
        except (ValueError, TypeError):
            prior_prov = {}
        binding_changed = prior_prov.get("binding", "eligible") != \
            strict["binding"]
        if "provenance" in strict:
            merged = dict(strict["provenance"])
        else:
            merged = dict(prior_prov)
        merged["binding"] = strict.pop("binding")
        strict["provenance"] = merged
    out = update_expected_placement(con, campaign_id, placement_id, strict,
                                    expected_revision, actor)
    if source_scope:
        row = con.execute("SELECT provenance FROM expected_placements"
                          " WHERE id=?", (placement_id,)).fetchone()
        try:
            stored = json.loads(row["provenance"] or "{}")
        except (ValueError, TypeError):
            stored = {}
        stored["source_scope"] = source_scope
        con.execute("UPDATE expected_placements SET provenance=? WHERE id=?",
                    (json.dumps(stored, sort_keys=True), placement_id))
    changed = _touch_campaign(con, campaign_id, actor, "placement_update", {"placement": placement_id})
    fresh = con.execute("SELECT * FROM expected_placements WHERE id=?",
                        (placement_id,)).fetchone()
    placed = dict(fresh)
    try:
        placed["requirements"] = json.loads(placed["requirements"] or "{}")
        placed["provenance"] = json.loads(placed["provenance"] or "{}")
    except (ValueError, TypeError):
        pass
    placed["source_scope"] = source_scope
    placed["binding"] = placement_binding(placed.get("provenance"))
    state = placement_allocation_state(con, placement_id)
    return {"placement": placed, "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"], **state}


def _qualify_attachment_ref(con, ref):
    from . import citations
    if not isinstance(ref, str) or not ref.startswith("sr1:") and not (
            re.fullmatch(r"ev:(.+)@v(\d+)", ref) or
            re.fullmatch(r"tx:(.+)@v(\d+):(\d+)", ref)):
        raise _ops.BadInput("attachment reference is malformed")
    try:
        ref = ref if ref.startswith("sr1:") else citations.qualify_legacy_source_ref(con, ref)
        # Database only: checking a reference does no recording proof for
        # any caller (CLIPCORE-SEC-R4-01).
        resolved = citations.resolve_source_state(con, ref, media=False)
    except citations.RevisionUnavailable:
        raise
    except citations.CitationError as exc:
        raise _ops.BadInput("attachment reference is malformed or unqualified") from exc
    if resolved["kind"] != "evidence":
        raise _ops.BadInput("attachment requires an evidence SourceRef1")
    return ref


@_db.atomic
def allocate_placement_evidence(con: sqlite3.Connection, campaign_id: int,
                                body: dict | None,
                                expected_campaign_revision=None,
                                actor: str = "owner") -> dict:
    """Retain/move/reference one exact ref across campaign units (K07).

    The wire is action/ref/from_target_id/to_target_id with the
    displayed campaign revision AND both unit revisions. A unit is a
    research target or a supplied placement of this campaign; both
    units must belong to it and share its mode family. Retain and
    reference address the holding unit (from equals to); move relocates
    the binding atomically. Reference marks citation-only and never
    earns quota credit; research target bindings are citations already.
    Attach/detach endpoints remain the source-binding writers; this
    action never attaches a new ref.

    Stale displayed revisions do not raise: they return the conflict
    DTO naming both units, the ref, displayed versus current
    revisions, per-unit binding/credit status and the allowed
    operations, with nothing written. Retain writes nothing by design.
    """
    if expected_campaign_revision is None and isinstance(body, dict):
        expected_campaign_revision = body.get("expected_campaign_revision")
    if expected_campaign_revision is None:
        raise _ops.MissingPrecondition(
            "expected_campaign_revision is required")
    camp = _campaign_exists(con, campaign_id)
    if not isinstance(body, dict):
        raise _ops.BadInput("body must be an object")
    action = body.get("action", "")
    if action not in ("retain", "move", "reference"):
        raise _ops.BadInput("allocation action must be retain, move or"
                            " reference")
    ref = body.get("ref", "")
    if not isinstance(ref, str) or not ref:
        raise _ops.BadInput("ref must be a nonempty string")
    from_id = body.get("from_target_id", "")
    to_id = body.get("to_target_id", "")
    if not isinstance(from_id, str) or not from_id:
        raise _ops.BadInput("from_target_id must be a nonempty string")
    if not isinstance(to_id, str) or not to_id:
        raise _ops.BadInput("to_target_id must be a nonempty string")
    if action in ("retain", "reference") and from_id != to_id:
        raise _ops.BadInput(f"{action} addresses the holding unit;"
                            " from_target_id must equal to_target_id")
    if action == "move" and from_id == to_id:
        raise _ops.BadInput("move needs two distinct units")
    ref = _qualify_attachment_ref(con, ref)
    from_unit = _allocation_unit(con, campaign_id, from_id)
    to_unit = _allocation_unit(con, campaign_id, to_id)
    if from_unit["kind"] != to_unit["kind"]:
        raise _ops.BadInput("allocation units must share one family:"
                            " two targets or two placements")
    for label, unit, key in (("from", from_unit, "expected_from_revision"),
                             ("to", to_unit, "expected_to_revision")):
        if from_id == to_id and label == "to":
            continue
        expected = body.get(key)
        if expected is None:
            raise _ops.MissingPrecondition(f"{key} is required")
        if int(expected) != unit["revision"]:
            return _allocation_conflict(
                con, campaign_id, action, ref, from_id, to_id, body,
                camp, from_unit, to_unit)
    if int(expected_campaign_revision) != camp["revision"]:
        return _allocation_conflict(con, campaign_id, action, ref,
                                    from_id, to_id, body, camp,
                                    from_unit, to_unit)
    # Stale displayed revisions get the conflict reply first (UAT-100).
    _refuse_archived(con, camp)
    from_state = _unit_binding(con, campaign_id, from_unit, ref)
    if not from_state["bound"]:
        raise _ops.BadInput(f"ref is not bound to {from_id}: {ref!r}")
    if action == "retain" or action == "reference" and (
            from_unit["kind"] == "target" or from_state["binding"] == "citation_only"):
        return {"action": action, "ref": ref,
                "from_target_id": from_id, "to_target_id": to_id,
                "binding": from_state["binding"],
                "credit_eligible": from_state["credit_eligible"],
                "campaign_revision": camp["revision"],
                "from_revision": from_unit["revision"],
                "to_revision": to_unit["revision"],
                "aggregate_rev": _db.aggregate_revision(con)}
    if action == "reference":
        if from_unit["kind"] == "placement":
            _set_placement_binding(con, campaign_id, from_id, ref,
                                   "citation_only")
            con.execute("UPDATE expected_placements SET revision=revision+1,updated_at=? WHERE id=?",
                        (_now_iso(), from_id))
            from_unit = _allocation_unit(con, campaign_id, from_id)
            to_unit = from_unit
        changed = _touch_campaign(con, campaign_id, actor,
                                  "allocation_reference",
                                  {"unit": from_id, "ref": ref})
        state = _unit_binding(con, campaign_id, from_unit, ref)
        return {"action": action, "ref": ref,
                "from_target_id": from_id, "to_target_id": to_id,
                "binding": state["binding"],
                "credit_eligible": state["credit_eligible"],
                "campaign_revision": changed["campaign_revision"],
                "from_revision": from_unit["revision"],
                "to_revision": to_unit["revision"],
                "aggregate_rev": changed["aggregate_rev"]}
    to_state = _unit_binding(con, campaign_id, to_unit, ref)
    if to_state["bound"]:
        raise _ops.BadInput(f"ref is already bound to {to_id}: {ref!r}")
    table = "campaign_targets" if to_unit["kind"] == "target" else "expected_placements"
    destination = dict(con.execute(f"SELECT * FROM {table} WHERE id=?", (to_id,)).fetchone())
    if not _target_allows_ref(con, destination, ref):
        raise _ops.BadInput("evidence is outside the captured destination scope")
    _move_unit_binding(con, campaign_id, from_unit, to_unit, ref,
                       from_state["binding"], actor)
    con.execute(f"UPDATE {table} SET revision=revision+1,updated_at=? WHERE id IN (?,?)",
                (_now_iso(), from_id, to_id))
    from_unit = _allocation_unit(con, campaign_id, from_id)
    to_unit = _allocation_unit(con, campaign_id, to_id)
    changed = _touch_campaign(con, campaign_id, actor, "allocation_move",
                              {"from": from_id, "to": to_id, "ref": ref})
    state = _unit_binding(con, campaign_id, to_unit, ref)
    return {"action": action, "ref": ref,
            "from_target_id": from_id, "to_target_id": to_id,
            "binding": state["binding"],
            "credit_eligible": state["credit_eligible"],
            "campaign_revision": changed["campaign_revision"],
            "from_revision": from_unit["revision"],
            "to_revision": to_unit["revision"],
            "aggregate_rev": changed["aggregate_rev"]}


def _allocation_unit(con: sqlite3.Connection, campaign_id: int,
                     unit_id: str) -> dict:
    """Resolve a unit id to its family and displayed revision (K07)."""
    row = con.execute("SELECT revision FROM campaign_targets"
                      " WHERE workspace_id='local' AND id=? AND campaign_id=?",
                      (unit_id, campaign_id)).fetchone()
    if row is not None:
        return {"kind": "target", "id": unit_id,
                "revision": row["revision"]}
    row = con.execute("SELECT revision FROM expected_placements"
                      " WHERE workspace_id='local' AND id=? AND campaign_id=?",
                      (unit_id, campaign_id)).fetchone()
    if row is not None:
        return {"kind": "placement", "id": unit_id,
                "revision": row["revision"]}
    raise _ops.BadInput(f"unknown unit for this campaign: {unit_id!r}")


def _unit_binding(con: sqlite3.Connection, campaign_id: int, unit: dict,
                  ref: str) -> dict:
    """Bound state plus credit eligibility for one unit/ref (K07).

    citation_only and research citations never earn quota credit; an
    eligible placement binding leaves actual credit to the allocator.
    """
    if unit["kind"] == "target":
        bound = con.execute("SELECT 1 FROM target_evidence"
                            " WHERE workspace_id='local' AND target_id=?"
                            " AND ref=?",
                            (unit["id"], ref)).fetchone() is not None
        return {"bound": bound, "binding": "citation",
                "credit_eligible": False}
    cols = {r["name"] for r in
            con.execute("PRAGMA table_info(placement_evidence)")}
    if "binding" in cols:
        row = con.execute("SELECT binding FROM placement_evidence"
                          " WHERE workspace_id='local' AND campaign_id=?"
                          " AND placement_id=? AND ref=?",
                          (campaign_id, unit["id"], ref)).fetchone()
        if row is None:
            return {"bound": False, "binding": "eligible",
                    "credit_eligible": True}
        eligible = row["binding"] != "citation_only"
        return {"bound": True, "binding": row["binding"],
                "credit_eligible": eligible}
    bound = con.execute("SELECT 1 FROM placement_evidence"
                        " WHERE workspace_id='local' AND campaign_id=?"
                        " AND placement_id=? AND ref=?",
                        (campaign_id, unit["id"], ref)).fetchone() is not None
    return {"bound": bound, "binding": "eligible",
            "credit_eligible": True}


def _set_placement_binding(con: sqlite3.Connection, campaign_id: int,
                           placement_id: str, ref: str,
                           binding: str) -> None:
    cols = {r["name"] for r in
            con.execute("PRAGMA table_info(placement_evidence)")}
    if "binding" not in cols:
        if binding != "eligible":
            raise _ops.BadInput("citation_only needs the v8 workspace")
        return
    cur = con.execute("UPDATE placement_evidence SET binding=?"
                      " WHERE workspace_id='local' AND campaign_id=?"
                      " AND placement_id=? AND ref=?",
                      (binding, campaign_id, placement_id, ref))
    if not cur.rowcount:
        raise _ops.BadInput(f"ref is not bound to {placement_id}: {ref!r}")


def _move_unit_binding(con: sqlite3.Connection, campaign_id: int,
                       from_unit: dict, to_unit: dict, ref: str,
                       binding: str, actor: str) -> None:
    """Relocate one binding atomically, preserving its credit class."""
    if from_unit["kind"] == "target":
        con.execute("DELETE FROM target_evidence WHERE workspace_id='local'"
                    " AND target_id=? AND ref=?", (from_unit["id"], ref))
        con.execute("INSERT OR IGNORE INTO target_evidence (workspace_id,"
                    " target_id, ref, attached_at, attached_by)"
                    " VALUES ('local', ?, ?, ?, ?)",
                    (to_unit["id"], ref, _now_iso(), actor))
        return
    con.execute("DELETE FROM placement_evidence WHERE workspace_id='local'"
                " AND campaign_id=? AND placement_id=? AND ref=?",
                (campaign_id, from_unit["id"], ref))
    cols = {r["name"] for r in
            con.execute("PRAGMA table_info(placement_evidence)")}
    if "binding" in cols:
        con.execute("INSERT OR IGNORE INTO placement_evidence (workspace_id,"
                    " campaign_id, placement_id, ref, attached_at,"
                    " attached_by, binding) VALUES ('local', ?, ?, ?, ?, ?, ?)",
                    (campaign_id, to_unit["id"], ref, _now_iso(), actor,
                     binding))
    else:
        con.execute("INSERT OR IGNORE INTO placement_evidence (workspace_id,"
                    " campaign_id, placement_id, ref, attached_at,"
                    " attached_by) VALUES ('local', ?, ?, ?, ?, ?)",
                    (campaign_id, to_unit["id"], ref, _now_iso(), actor))


def _allocation_conflict(con: sqlite3.Connection, campaign_id: int,
                         action: str, ref: str, from_id: str, to_id: str,
                         body: dict, camp: dict, from_unit: dict,
                         to_unit: dict) -> dict:
    """Stale-basis conflict DTO: names, revisions, per-unit credit
    state, allowed operations. Nothing is written; the caller replays
    its choice with displayed revisions."""
    return {"conflict": {
        "action": action, "ref": ref,
        "from_target_id": from_id, "to_target_id": to_id,
        "expected": {
            "campaign_revision": body.get("expected_campaign_revision"),
            "from_revision": body.get("expected_from_revision"),
            "to_revision": body.get("expected_to_revision")},
        "current": {
            "campaign_revision": camp["revision"],
            "from_revision": from_unit["revision"],
            "to_revision": to_unit["revision"]},
        "units": {
            "from": {**_unit_binding(con, campaign_id, from_unit, ref),
                     "unit_id": from_id, "kind": from_unit["kind"]},
            "to": {**_unit_binding(con, campaign_id, to_unit, ref),
                   "unit_id": to_id, "kind": to_unit["kind"]}},
        "allowed_operations": ["retain", "move", "reference"]}}

def target_campaign(con: sqlite3.Connection, target_id: str) -> int:
    """Owning campaign for a target id (route ownership check)."""
    row = con.execute("SELECT campaign_id FROM campaign_targets"
                      " WHERE workspace_id='local' AND id=?",
                      (target_id,)).fetchone()
    if row is None:
        raise _ops.BadInput("unknown target")
    return row["campaign_id"]


@_db.atomic
def update_target_note(con: sqlite3.Connection, campaign_id: int,
                       target_id: str, note: str, expected_revision,
                       actor: str = "owner") -> dict:
    """Edit a target note only; the captured source scope is retained."""
    _require_v8(con, "campaign targets")
    if expected_revision is None:
        raise _ops.MissingPrecondition("expected revision is required")
    row = con.execute("SELECT * FROM campaign_targets WHERE workspace_id='local'"
                      " AND id=? AND campaign_id=?", (target_id, campaign_id)
                      ).fetchone()
    if row is None:
        raise _ops.BadInput("unknown target for this campaign")
    if not isinstance(note, str):
        raise _ops.BadInput("note must be a string")
    if int(expected_revision) != row["revision"]:
        raise _ops.Conflict(row["revision"])
    _refuse_archived(con, campaign_id)
    con.execute("UPDATE campaign_targets SET note=?, revision=revision+1,"
                " updated_at=? WHERE id=?", (note, _now_iso(), target_id))
    changed = _touch_campaign(con, campaign_id, actor, "target_note",
                              {"target": target_id})
    fresh = con.execute("SELECT * FROM campaign_targets WHERE id=?",
                        (target_id,)).fetchone()
    target = dict(fresh)
    try:
        target["source_scope"] = json.loads(
            target.pop("source_scope_json") or "{}")
    except (ValueError, TypeError):
        target["source_scope"] = {}
    return {"target": target,
            "campaign_revision": changed["campaign_revision"],
            "aggregate_rev": changed["aggregate_rev"]}
