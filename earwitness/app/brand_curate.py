"""Automated curation of the known-brand list (`curate-brands`).

import-names skips a spoken brand as "off-list" unless the brand is known
(corrections.known_brand). This step adds an off-list brand to the known
list on the ads' own corroboration, free and deterministic, in one
transaction with one audit row. Rule `brand-curate/2`:

A read AGREES with a candidate key when, in the read's transcript text (the
segments of the episode's current copy that overlap the read), both hold:
  1. a web address whose one-token label is the key (compared compacted),
     or a promo code that is the key, or a url/code brand association of the
     read that is the key; and
  2. the brand is said as a word outside an address ("AT&T" for att).
A read in a show named after the key never agrees (the show's own name: the
key is a run of whole words of the show title).
A candidate is curated when its agreeing reads cover at least MIN_ADS
distinct episodes (an alias of an episode is that episode; one spot played
twice in one episode is one rendering), and it is not stoplisted, not
removed or stoplisted before, not mapped by an alias rule, not rejected by
a person, not near a known brand, and not near another passing candidate
with at least as many episodes. The display is derived from the transcript
spelling, never typed. The rule is machine evidence (origin "auto"), never
a human label; a person removes it in the operator Rules panel and it never
comes back.

`add_manual` is the Reno-approved way in for a brand the rule leaves out.
"""
from __future__ import annotations

import datetime
import json
import re
from collections import Counter

from earwitness.pipeline import brand_namer as _bn
from earwitness.pipeline import detection as _det

from . import db as _db

RULE_VERSION = "brand-curate/2"
AUTO_ACTOR = "brand-curate"
MIN_ADS = 2
NO_TABLES = "no ad review tables (run upgrade)"

_ADDRESS = re.compile(r"(?<![A-Za-z0-9&'@.-])(?:www\.)?([A-Za-z0-9&'-]+)(?:\.|\s+dot\s+)"
                      + _bn.TLD + r"\b", re.I)
_NOT_ADDRESS = r"(?!\s*(?:\.|\s+dot\s+)" + _bn.TLD + r"\b)"


def address_matches(text: str, key: str) -> list:
    """[(label, matched text)] for each web address (or promo code) in
    `text` whose one-token label is `key`, compared compacted."""
    want = _bn.compact(key)
    out = [(m.group(1), m.group(0)) for m in _ADDRESS.finditer(text or "")
           if _bn.compact(m.group(1)) == want]
    for rx in _det._CODE_RES:
        out += [(m.group(1), m.group(0)) for m in rx.finditer(text or "")
                if _bn.compact(m.group(1)) == want]
    return out


def spoken_forms(text: str, key: str) -> list:
    """The forms in `text` that say the brand as a word outside an address
    or a promo code: one optional [&.'] inside a word of the key, one
    optional space or hyphen at a space of the key ("AT&T" is att, "Ghost
    bed" and "GhostBed" are ghost bed; "QU-I-N-C-E", "GhostBed.com" and
    "use code GHOSTBED" are not quince or ghost bed)."""
    chars = (key or "").strip()
    if not chars:
        return []
    parts = []
    for i, ch in enumerate(chars):
        if ch == " ":
            parts.append(r"[\s-]?")
        else:
            parts.append(re.escape(ch))
            if i < len(chars) - 1 and chars[i + 1] != " ":
                parts.append(r"[&.'’]?")
    rx = re.compile(r"(?<![A-Za-z0-9])" + "".join(parts) + r"(?![A-Za-z0-9])" + _NOT_ADDRESS,
                    re.I)
    codes = [m.span(1) for code in _det._CODE_RES for m in code.finditer(text or "")]
    return [m.group(0) for m in rx.finditer(text or "")
            if not any(a < m.end() and m.start() < b for a, b in codes)]


def is_show_name(show_title: str, key: str) -> bool:
    """The key is the show's own name: its compact form equals that of a
    contiguous run of whole words of the show title."""
    ck = _bn.compact(key)
    words = re.findall(r"[A-Za-z0-9&']+", show_title or "")
    return bool(ck) and any(_bn.compact("".join(words[i:j])) == ck
                            for i in range(len(words)) for j in range(i + 1, len(words) + 1))


def _near(c: str, k: str) -> bool:
    """Two compacted spellings that may be one brand (or a piece of one)."""
    if _bn._similar(c, k):
        return True
    if min(len(c), len(k)) >= 2 and abs(len(c) - len(k)) <= 1 and _bn.levenshtein(c, k) <= 1:
        return True
    return (len(c) >= 4 and c in k) or (len(k) >= 4 and k in c)


def _cased(form: str) -> bool:
    """A capital after the first character inside one word (AT&T, GhostBed)."""
    return any(c.isupper() for w in re.split(r"[\s\-]+", form) for c in w[1:])


def _display(said: Counter, labels: Counter):
    """Derived from what the ads say: the most common spoken form with inner
    case, else the most common address label with inner case and no hyphen,
    else the most common form that carries case (ad_reads._informative),
    else none (the display falls back to title case)."""
    from . import ad_reads

    def top(counter, ok):
        forms = [f for f, _n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])) if ok(f)]
        return forms[0] if forms else None
    return (top(said, _cased) or top(labels, lambda f: _cased(f) and "-" not in f)
            or top(said + labels, ad_reads._informative))


def _stamp() -> str:
    return "curate-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _result(run_id, dry_run) -> dict:
    return {"run_id": run_id, "rule": RULE_VERSION, "dry_run": dry_run, "would_write": 0,
            "written": 0, "candidates": 0, "added": [], "skipped": []}


def _removed_before(con) -> set:
    return {k for (k,) in con.execute("SELECT key FROM ad_rules_removed WHERE kind IN"
                                      " ('brand', 'stoplist')")}


def _rejected(con, keys: set, key: str) -> bool:
    """A person said it is generic, or replaced it with another brand."""
    from . import ad_reads
    marks = ",".join("?" * len(keys))
    for action, answer in con.execute(
            f"SELECT action, answer FROM ad_labels WHERE raw_brand IN ({marks})", tuple(keys)):
        if action == "generic":
            return True
        try:
            brand = json.loads(answer or "{}").get("brand")
        except (TypeError, ValueError, AttributeError):
            brand = None
        if brand and ad_reads.brand_key(brand) != key:
            return True
    return False


def curate_brands(con, corpus, *, dry_run: bool, run_id: str | None = None,
                  actor: str = "operator") -> dict:
    """Plan (and unless dry_run, apply) the curation in one transaction. See
    the module docstring. Without the ad review tables it writes nothing and
    says so (import-names keeps working)."""
    from . import ad_reads, corrections, jev_rescreen, name_import
    run_id = run_id or _stamp()
    out = _result(run_id, dry_run)
    if not corrections.present(con):
        out["skipped_all"] = NO_TABLES
        return out
    with _db.transaction(con, write=not dry_run):
        plan = name_import.import_names(con, corpus, dry_run=True, run_id=run_id, actor=actor,
                                        curate=False)
        by_key: dict = {}
        for d in plan["decisions"]:
            if d["reason"] == "off-list":
                by_key.setdefault(d["brand"], []).append(d)
        out["candidates"] = len(by_key)
        if not by_key:
            return out
        rules = corrections.load(con)
        current = jev_rescreen._current_copies(con)
        removed = _removed_before(con)
        known = sorted((k, _bn.compact(k)) for k in corrections.known_keys(con, rules))
        stats: dict = {}
        for key, reads in sorted(by_key.items()):
            stats[key] = _read_stats(con, key, reads, current)
        passing, skipped = {}, {}
        for key, st in stats.items():
            reason = _reason(con, key, st, by_key[key], rules, removed, known)
            if reason:
                skipped[key] = reason
            else:
                passing[key] = st
        for key, st in sorted(passing.items()):
            rivals = sorted((o for o, ot in passing.items() if o != key
                             and len(ot["ads"]) >= len(st["ads"])
                             and _near(_bn.compact(key), _bn.compact(o))),
                            key=lambda o: (-len(passing[o]["ads"]), o))
            if rivals:
                skipped[key] = f"sound-alike of another candidate: {rivals[0]}"
        for key, st in sorted(stats.items()):
            if key in skipped:
                out["skipped"].append({"brand": key, "reason": skipped[key],
                                       "ads": len(st["ads"])})
            else:
                said, labels = st["said"], st["labels"]
                out["added"].append({
                    "brand": key, "display": _display(said, labels), "ads": len(st["ads"]),
                    "evidence": [e for e, _k in st["reads"]],
                    "addresses": [a for a, _n in sorted(st["addresses"].items(),
                                                        key=lambda kv: (-kv[1], kv[0]))],
                    "said": [f for f, _n in sorted(said.items(), key=lambda kv: (-kv[1], kv[0]))],
                    "from_read": st["reads"][0][1]})
        out["would_write"] = len(out["added"])
        if dry_run or not out["added"]:
            return out
        for a in out["added"]:
            corrections._put_rule(con, "brand", a["brand"], {
                "display": a["display"], "origin": "auto", "rule": RULE_VERSION,
                "run_id": run_id, "ads": a["ads"], "evidence": a["evidence"],
                "addresses": a["addresses"], "said": a["said"], "from_read": a["from_read"]},
                AUTO_ACTOR)
        out["written"] = len(out["added"])
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "brand_curate", "workspace", run_id,
                  {"rule": RULE_VERSION, "corpus": str(corpus),
                   "added": [{k: v for k, v in a.items() if k != "from_read"}
                             for a in out["added"]],
                   "skipped": out["skipped"]}, rev)
    return out


def _read_stats(con, key: str, reads: list, current: dict) -> dict:
    """What the candidate's reads say: the agreeing reads, their ads, the
    address and spoken forms, and why the others do not agree."""
    st = {"reads": [], "ads": set(), "addresses": Counter(), "said": Counter(),
          "labels": Counter(), "show": False, "any_address": False, "any_said": False}
    ck = _bn.compact(key)
    for d in reads:
        row = con.execute(
            "SELECT e.orig_start_s, e.orig_end_s, e.source_record_key, ep.show_title,"
            " COALESCE(ep.alias_of, ep.id) AS canonical_episode FROM evidence"
            " e JOIN episodes ep ON ep.id=e.episode_id WHERE e.id=?", (d["evidence_id"],)
        ).fetchone()
        if row is None:
            continue
        segments = current.get(d["episode_id"], {}).get("segments") or []
        text = " ".join(str(s.get("text") or "") for s in segments
                        if s.get("start") is not None and s["start"] < row["orig_end_s"]
                        and s.get("end") is not None and s["end"] > row["orig_start_s"])
        found = address_matches(text, key)
        assoc = [b for (b,) in con.execute(
            "SELECT brand FROM brand_assocs WHERE evidence_id=? AND origin='detector'"
            " AND channel IN ('url', 'code')", (d["evidence_id"],)) if _bn.compact(b) == ck]
        forms = spoken_forms(text, key)
        st["any_address"] |= bool(found or assoc)
        st["any_said"] |= bool(forms)
        if not (found or assoc) or not forms:
            continue
        if is_show_name(row["show_title"], key):
            st["show"] = True
            continue
        st["reads"].append((d["evidence_id"], row["source_record_key"]))
        st["ads"].add(row["canonical_episode"])
        st["addresses"].update([m[1] for m in found] + assoc)
        st["labels"].update(m[0] for m in found)
        st["said"].update(forms)
    return st


def _reason(con, key, st, reads, rules, removed, known) -> str | None:
    """Why the candidate is not curated, or None when every gate passes."""
    ck = _bn.compact(key)
    if ck in {_bn.compact(k) for k in rules["stoplist"]}:
        return "stoplisted"
    for k in sorted(removed):   # a sound-alike of a removed brand stays out too
        if _near(ck, _bn.compact(k)) or ck == _bn.compact(k):
            return f"removed before: {k}"
    if ck in {_bn.compact(k) for k in rules["alias"]}:
        return "an alias rule maps it"
    raws = {key} | {d["raw_brand"] for d in reads if d.get("raw_brand")}
    if _rejected(con, raws, key):
        return "a person rejected it"
    n = len(st["ads"])
    if n < MIN_ADS:
        if n == 1:
            return f"1 agreeing episode, needs {MIN_ADS}"
        if st["show"]:
            return "the show's own name"
        if st["any_address"] and not st["any_said"]:
            return "web address, but the name is not said as a word"
        if st["any_said"] and not st["any_address"]:
            return "name said, but no matching web address"
        if st["any_said"] and st["any_address"]:
            return "no read has both the web address and the name said"
        return "no matching web address"
    for k, kc in known:
        if kc and _near(ck, kc):
            return f"near a known brand: {k}"
    return None


def manual_refusal(con, key: str, rules: dict, *, ignore_suggestion_undone: bool = False) -> str | None:
    """Why a brand key cannot be added by hand (add_manual, and a confirmed
    brand suggestion that heard the name right): stoplisted, removed before,
    or already known. None when it can. `ignore_suggestion_undone`: a rule that
    a brand suggestion's Confirm made and its Undo took back (reason "undone",
    its value carries `suggestion`) does not count as removed before, whatever
    suggestion it came from."""
    from . import corrections
    ck = _bn.compact(key)
    if ck in {_bn.compact(k) for k in rules["stoplist"]}:
        return f"“{key}” is on the stoplist (a generic word), not a brand"
    removed = _removed_before(con)
    if ignore_suggestion_undone:
        removed = {k for (k,) in con.execute(
            "SELECT key FROM ad_rules_removed WHERE kind IN ('brand', 'stoplist') AND NOT"
            " (reason='undone' AND json_extract(value, '$.suggestion') IS NOT NULL)")}
    if ck in {_bn.compact(k) for k in removed}:
        return f"“{key}” was removed before; a removed brand is not added again"
    if corrections.known_brand(con, key, rules) or ck in {
            _bn.compact(k) for k in corrections.known_keys(con, rules)}:
        return f"“{key}” is already known"
    return None


def add_manual(con, key: str, display: str, *, approved_by: str, note: str = "",
               dry_run: bool, run_id: str | None = None, actor: str = "operator") -> dict:
    """A Reno-approved brand the rule leaves out (origin manual). Refuses a
    key that is known, stoplisted or removed before (ValueError). An
    identical entry already there writes nothing."""
    from . import ad_reads, corrections
    run_id = run_id or _stamp()
    out = _result(run_id, dry_run)
    out["manual"] = True
    key = ad_reads.brand_key(key)
    display, approved_by, note = (display or "").strip(), (approved_by or "").strip(), note or ""
    if not key or not display or not approved_by:
        raise ValueError("a manual brand needs a brand, a display and an approver")
    if not corrections.present(con):
        raise ValueError(corrections.UPGRADE_MESSAGE)
    with _db.transaction(con, write=not dry_run):
        rules = corrections.load(con)
        same = [r for r in rules["rows"] if r["kind"] == "brand" and r["key"] == key
                and r["value"].get("origin") == "manual"
                and (r["value"].get("display"), r["value"].get("approved_by"),
                     r["value"].get("note")) == (display, approved_by, note)]
        if same:
            out["skipped"].append({"brand": key, "reason": "already added", "ads": 0,
                                   "decision": "unchanged"})
            return out
        refusal = manual_refusal(con, key, rules)
        if refusal:
            raise ValueError(refusal)
        out["added"].append({"brand": key, "display": display, "ads": 0, "evidence": [],
                             "addresses": [], "said": []})
        out["would_write"] = 1
        if dry_run:
            return out
        corrections._put_rule(con, "brand", key, {
            "display": display, "origin": "manual", "approved_by": approved_by, "note": note,
            "run_id": run_id}, approved_by)
        out["written"] = 1
        rev = _db.bump_revision(con)
        _db.audit(con, actor, "brand_curate_manual", "workspace", run_id,
                  {"rule": RULE_VERSION, "brand": key, "display": display,
                   "approved_by": approved_by, "note": note}, rev)
    return out


def format_report(result: dict) -> str:
    dry = result["dry_run"]
    lines = [("DRY RUN (writes nothing): " if dry else "")
             + f"brand curation {result['rule']}, run {result['run_id']}"]
    if result.get("skipped_all"):
        lines.append(f"nothing curated: {result['skipped_all']}")
    lines.append("brand\tdecision\tads\taddress\tdisplay\treason")
    for a in result["added"]:
        lines.append(f"{a['brand']}\tadd\t{a['ads']}\t{(a['addresses'] or ['-'])[0]}"
                     f"\t{a['display'] or '-'}\t")
    for s in result["skipped"]:
        lines.append(f"{s['brand']}\t{s.get('decision', 'skip')}\t{s['ads']}\t-\t-\t{s['reason']}")
    lines.append(f"would add {len(result['added'])} brands" if dry
                 else f"added {result['written']} brands")
    return "\n".join(lines)
