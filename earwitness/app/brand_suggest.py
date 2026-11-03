"""Brand suggestions (`brand-suggest`): one machine answer per off-list name.

import-names skips a heard name as "off-list" when it is not a known brand
(corrections.known_brand). This step asks one question per such name (the
names import-names' dry run lists with reason "off-list"; a name curation
would add in the same plan is known, so it is not asked): which company, if
any, do the ads that SAY the name advertise? The answer is stored as a
SUGGESTION for the operator to confirm or dismiss in Ad review.

A suggestion changes nothing until the operator confirms it: it is an
`ad_rules` row of kind `suggestion` (corrections.load, export, the Rules
panel and remove_rule never see it), so no read, brand, alias, rule, count,
alert or customer output moves. Confirm then writes exactly the rules the
existing manual actions write (the curated-list brand rule of
brand_curate.add_manual; the brand and alias rules of corrections.act
"alias"; the stoplist rule of act "generic"), each with a `source` and the
suggestion's evidence. Not this and Undo are recorded.

The question is the experiment's frozen arm A6s, prompt `PROMPT_VERSION`
(2026-10-17): the text below is copied, not imported, and owner acceptance
compares it with the experiment's byte for byte. The call goes through the
`agy` CLI (a parameter; tests use tests/fake_agy.py) in a fresh empty
folder, with a hard timeout, a call cache (a rerun after a failed write makes
no new call) and a call log with every attempt.

Only ads that say the name feed the question and count as support: the
namer's repair step can name a read whose transcript never says the name
("repair:<heard>-><brand>" on the names span). Each read is recorded with
`says_name` and `repaired_from`; a name no read says is skipped without a
call.

CONFIDENT (0.8) is a placeholder from the A7 design note, not validated.

Verdicts (decision D3): `brand` (is_brand, a company, confidence >=
CONFIDENT, no self-promotion hit), `not_brand` (not is_brand, confidence >=
CONFIDENT), `unsure` (everything else, including any self-promotion hit).
An unsure suggestion cannot be confirmed; the operator can say Not this.

Extra keys the panel needs sit in each provenance read (episode_id,
span_id, ad_start_s, ad_end_s) and provenance has `ads`.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from earwitness.pipeline import brand_namer as _bn
from earwitness.pipeline import detection as _det
from earwitness.pipeline import self_promo as _sp

from . import brand_curate
from . import db as _db

PROMPT_VERSION = "brand-suggest/a6s-1"
MODEL = "gemini-3.8-flash-low"
PRINT_TIMEOUT = "150s"
HARD_TIMEOUT_S = 200
CONFIDENT = 0.8
ACTOR = "brand-suggest"
SOURCE = "gemini suggestion, confirmed by {approved_by}"
KIND = "suggestion"
RETRIES = 2
MAX_CONSECUTIVE_ERRORS = 8
MAX_EXCERPTS = 3
MAX_URLS = 5
MAX_CODES = 5
SECRET_LINE = re.compile(r"(?i)\b[A-Z0-9_]*(KEY|TOKEN|PASSWORD|SECRET)\s*=")
NO_SAYING = "no read says the name"
SECRET_REASON = "prompt would carry a secret-like line"
SEARCH_TOOLS = {"search_web"}
WEB_TOOLS = {"search_web", "read_url_content", "open_browser_url", "read_browser_page",
             "browser_subagent"}
UNCHECKED = object()   # undo from code that shows no count (tests, tools)

# -- The frozen experiment arm (copied from the experiment's run_arms.py) ------------

S_STR_OR_NULL = {"type": ["string", "null"]}
_A5 = {"type": "object", "additionalProperties": False,
       "required": ["is_brand", "company", "heard_is_exact", "confidence"],
       "properties": {"is_brand": {"type": "boolean"}, "company": S_STR_OR_NULL,
                      "heard_is_exact": {"type": "boolean"},
                      "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}
SCHEMA_A6 = {**_A5, "required": _A5["required"] + ["official_domain", "reason"],
             "properties": {**_A5["properties"], "official_domain": S_STR_OR_NULL,
                            "reason": {"type": "string"}}}

QUESTIONS = """Answer these fields:
- is_brand: true only if these excerpts advertise a company, product or brand that the heard name refers to (the heard name may be a speech-to-text mishearing of it). False if the name is the show itself, a host, a guest, an email address, a generic word, or nothing is advertised under it.
- company: the advertised company's correct name, or null.
- heard_is_exact: true if the heard name is that company's real name (ignore case, spacing, punctuation and ".com"); false if it is a mishearing or a different name.
- confidence: 0 to 1."""


def text_of(ex):
    if isinstance(ex, str):
        return ex
    for k in ("text", "excerpt", "quote", "window_text", "span_text"):
        if isinstance(ex.get(k), str):
            return ex[k]
    return json.dumps(ex)


def listy(obj, *keys):
    out = []
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v:
            out.append(v)
        elif isinstance(v, list):
            out += [x if isinstance(x, str) else json.dumps(x) for x in v if x]
    return out


def name_of(obj):
    return obj.get("name") or obj.get("key") or obj.get("brand")


def render_reads(obj):
    lines = []
    for i, ex in enumerate(obj.get("excerpts") or [], 1):
        meta = {} if isinstance(ex, str) else ex
        where = ", ".join(f"{k}={meta[k]}" for k in ("show", "pub_date", "episode_id", "timestamp_s", "timestamp", "at", "start")
                          if meta.get(k) not in (None, ""))
        lines.append(f"Excerpt {i} ({where}):\n\"\"\"{text_of(ex).strip()}\"\"\"")
    urls = listy(obj, "urls_heard", "urls", "url")
    codes = listy(obj, "codes_heard", "codes", "code")
    either = listy(obj, "url_or_code_heard")
    if urls:
        lines.append("Web addresses heard in the reads: " + ", ".join(urls))
    if codes:
        lines.append("Promo codes heard: " + ", ".join(codes))
    if either:
        lines.append("Web addresses or promo codes heard in the reads: " + ", ".join(either))
    notes = obj.get("episode_notes") or obj.get("notes")
    if notes:
        notes = notes if isinstance(notes, str) else json.dumps(notes)
        lines.append(f"Episode notes (feed description):\n\"\"\"{notes.strip()[:2000]}\"\"\"")
    for m in (obj.get("notes_mentions") or [])[:3]:
        if isinstance(m, dict) and m.get("text"):
            lines.append(f"Episode notes that name it ({m.get('scope', 'notes')}):\n\"\"\"{m['text'].strip()[:700]}\"\"\"")
    return "\n\n".join(lines)


def prompt(obj):
    """The A6 prompt (the experiment's `prompt("A6", obj)`)."""
    name = name_of(obj)
    base = (f'A speech-to-text system heard the name "{name}" in podcast ad reads. '
            "Judge whether it is an advertised brand.\n\n" + render_reads(obj) + "\n\n" + QUESTIONS)
    return ("Use the search_web tool at least once: search for the heard name, the web address if one "
            "was heard, and the company you think it is. Use only search_web: do not read or write "
            "files, do not run commands, do not open pages.\n\n" + base +
            "\n- official_domain: the company's official website domain, or null."
            "\n- reason: at most 25 words.")


def parse_stream(raw):
    """Tool steps (any state, one per step index: `finish` never reaches DONE),
    the result event, and the per-step usage blocks."""
    tools, result, usage_steps = {}, None, []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "result":
            result = ev.get("result") or {}
        su = ev.get("step_update") or {}
        if su.get("step_type") == "tool":
            t = tools.setdefault(su.get("step_index"), {"tool": su.get("tool_name"), "states": []})
            t["params"] = (su.get("tool_info") or {}).get("parameters")
            t["states"].append(su.get("state"))
        if su.get("usage"):
            usage_steps.append(su["usage"])
    return list(tools.values()), result, usage_steps


def stream_model(raw):
    """The model named by the stream's `init` event (init.model), or None."""
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("event") == "init":
            model = (ev.get("init") or {}).get("model")
            return model if isinstance(model, str) else None
    return None


# -- Verdict ----------------------------------------------------------------------

def verdict_of(output: dict, self_promo) -> str:
    """brand / not_brand / unsure (module docstring)."""
    confidence = output.get("confidence")
    confident = isinstance(confidence, (int, float)) and confidence >= CONFIDENT
    if output.get("is_brand"):
        company = (output.get("company") or "").strip()
        return "brand" if confident and company and not self_promo else "unsure"
    return "not_brand" if confident else "unsure"


def _valid(output) -> bool:
    if not isinstance(output, dict) or not all(k in output for k in SCHEMA_A6["required"]):
        return False
    conf = output["confidence"]
    return (isinstance(output["is_brand"], bool) and isinstance(output["heard_is_exact"], bool)
            and isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 1
            and all(output[k] is None or isinstance(output[k], str)
                    for k in ("company", "official_domain", "reason")))


# -- Judgement input --------------------------------------------------------------

def _stamp() -> str:
    return "suggest-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def input_digest(inp: dict) -> str:
    return hashlib.sha256(_canonical_json({**inp, "prompt_version": PROMPT_VERSION}).encode()
                          ).hexdigest()


def _ad_text(segments, lo: float, hi: float) -> str:
    return " ".join(str(s.get("text") or "") for s in segments
                    if s.get("start") is not None and s["start"] < hi
                    and s.get("end") is not None and s["end"] > lo)


def _says(text: str, key: str) -> bool:
    return bool(brand_curate.spoken_forms(text, key) or brand_curate.address_matches(text, key))


def _cut(text: str, key: str) -> str:
    """The excerpt: from 600 characters before the first hit of a matching
    address or the key, 1500 characters (as the pilot's excerpts were cut)."""
    text = text.strip()
    low = text.lower()
    found = brand_curate.address_matches(text, key)
    addr = found[0][1] if found else ""
    hits = [low.find(s.lower()) for s in (addr, key) if s and s.lower() in low]
    at = max(0, min(hits) - 600) if hits else 0
    return text[at:at + 1500]


def _repair_of(doc, span_id, raw_brand):
    """<heard> of the `repair:<heard>-><brand>` evidence on the names span's
    candidate whose brand is the read's raw brand, else None."""
    from . import ad_reads
    for sp in (doc or {}).get("spans") or ():
        if not isinstance(sp, dict) or sp.get("span_id") != span_id:
            continue
        for cand in sp.get("candidates") or ():
            if isinstance(cand, dict) and ad_reads.brand_key(cand.get("brand")) == raw_brand:
                for ev in cand.get("evidence") or ():
                    m = re.match(r"repair:(.*)->(.*)$", str(ev))
                    if m:
                        return m.group(1)
    return None


def _urls_codes(texts: list, key: str) -> tuple:
    """Distinct web addresses (those whose label is the key first) and promo
    codes in the saying ads' text, at most 5 of each."""
    ck = _bn.compact(key)
    seen: set = set()
    mine, other = [], []
    for text in texts:
        for m in brand_curate._ADDRESS.finditer(text):
            if m.group(0).lower() not in seen:
                seen.add(m.group(0).lower())
                (mine if _bn.compact(m.group(1)) == ck else other).append(m.group(0))
    codes: list = []
    for text in texts:
        for rx in _det._CODE_RES:
            for m in rx.finditer(text):
                if m.group(1) not in codes:
                    codes.append(m.group(1))
    return (mine + other)[:MAX_URLS], codes[:MAX_CODES]


def _suggestion_rows(con) -> dict:
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_rules'"
                       ).fetchone():
        return {}
    return {key: dict(json.loads(value or "{}"), id=rid) for rid, key, value in con.execute(
        "SELECT id, key, value FROM ad_rules WHERE kind=? ORDER BY id", (KIND,))}


def _live_rules(con, value: dict, sid=None) -> list:
    """The rules a confirm made that are still this suggestion's ([kind, key,
    rule id]): the rule exists and its CURRENT value carries this
    suggestion's id. A rule an operator action rewrote since (same id, no
    `suggestion`) is theirs now: it counts as removed here."""
    sid = value.get("id") if sid is None else sid
    out = []
    for kind, key, rid in value.get("rules") or []:
        row = con.execute("SELECT value FROM ad_rules WHERE id=? AND kind=? AND key=?",
                          (rid, kind, key)).fetchone()
        mine = ((json.loads(row[0] or "{}").get("suggestion") or {}).get("id")) if row else None
        if row and mine == sid:
            out.append([kind, key, rid])
    return out


def _rules_gone(con, value: dict, sid=None) -> bool:
    """A confirmed suggestion none of whose rules is still its own (removed or
    rewritten since, in the Rules panel or by an operator action): its rules
    no longer apply."""
    return bool(value.get("rules")) and not _live_rules(con, value, sid)


def _is_confirmed(con, value) -> bool:
    return bool(value) and value.get("status") == "confirmed" and not _rules_gone(con, value)


def _writes_canonical(con, key: str, value: dict, rules: dict) -> tuple:
    """(what Confirm will write, the brand key it writes toward):
    `brand_list` (a curated-list brand: the company's own key is the heard
    key, compared compacted, "ghost bed" is "GhostBed"), `alias` (heard -> the
    canonical key, plus the brand rule when that key is not known), `generic`
    (a stoplist word), or None (unsure). It follows the company's key,
    whatever heard_correct says. When the keys match only compacted and a
    known key has that form ("ghost bed" known, heard "ghostbed"), or the
    company's key is known, it is an alias onto that known key: a second brand
    with the same display could not be added."""
    from . import ad_reads, corrections
    if value.get("verdict") == "not_brand":
        return "generic", None
    if value.get("verdict") != "brand":
        return None, None
    company = (value.get("company") or "").strip()
    canonical = corrections._brand_key_of(con, company, rules)
    if _bn.compact(canonical) != _bn.compact(key):
        return "alias", canonical
    heard = ad_reads.brand_key(key)
    if canonical != heard and corrections.known_brand(con, canonical, rules):
        return "alias", canonical
    other = next((k for k in sorted(corrections.known_keys(con, rules))
                  if k != heard and _bn.compact(k) == _bn.compact(key)), None)
    return ("alias", other) if other else ("brand_list", canonical)


def _writes(con, key: str, value: dict, rules: dict):
    return _writes_canonical(con, key, value, rules)[0]


def _resolved(con, key: str, rules: dict) -> bool:
    """The name is known, stoplisted or mapped by an alias rule now: there is
    nothing left to confirm."""
    from . import corrections
    return (key in rules["stoplist"] or key in rules["alias"]
            or corrections.known_brand(con, key, rules))


def plan(con, corpus, *, force: bool = False, run_id: str | None = None) -> dict:
    """The names to ask about, read-only (a dry run's report is this dict):
    every off-list heard name with its reads, judgement input, input digest
    and a decision. `ask`: no suggestion yet, or its input or prompt changed,
    or `force`. `skip` with a reason: `confirmed` (never asked again),
    `unchanged` (the same input already has a suggestion; a dismissed one
    too), "no read says the name", or the secret guard."""
    from . import corrections, jev_rescreen, name_import
    corpus = Path(corpus)
    run_id = run_id or _stamp()
    out = {"run_id": run_id, "prompt_version": PROMPT_VERSION, "model": MODEL, "dry_run": True,
           "corpus": str(corpus), "force": force, "candidates": 0, "reads": 0, "names": [],
           "asked": 0, "calls": 0, "cached": 0, "stored": [], "failed": [], "skipped": []}
    if not corrections.present(con):
        out["skipped_all"] = brand_curate.NO_TABLES
        return out
    with _db.transaction(con, write=False):
        found = name_import.import_names(con, corpus, dry_run=True, run_id=run_id, curate=True)
        by_key: dict = {}
        for d in found["decisions"]:
            if d["reason"] == "off-list":
                by_key.setdefault(d["brand"], []).append(d)
        current = jev_rescreen._current_copies(con)
        existing = _suggestion_rows(con)
        docs: dict = {}
        for key, decisions in sorted(by_key.items()):
            out["names"].append(_plan_name(con, corpus, key, decisions, current, docs,
                                           existing.get(key), force))
    out["candidates"] = len(out["names"])
    out["reads"] = sum(len(n["reads"]) for n in out["names"])
    out["asked"] = sum(n["decision"] == "ask" for n in out["names"])
    return out


def _plan_name(con, corpus, key, decisions, current, docs, existing, force) -> dict:
    from . import name_import
    ads: dict = {}
    reads = []
    for d in sorted(decisions, key=lambda d: (d["episode_id"], d["evidence_id"])):
        row = con.execute(
            "SELECT e.orig_start_s, e.orig_end_s, e.source_record_key, ep.show_title,"
            " ep.pub_date FROM evidence e JOIN episodes ep ON ep.id=e.episode_id WHERE e.id=?",
            (d["evidence_id"],)).fetchone()
        if row is None:
            continue
        ep = d["episode_id"]
        if ep not in docs:
            docs[ep] = name_import._read_json(corpus / "names" / f"{ep}.json")
        ad = ads.setdefault((ep, d["span_id"]), {
            "episode_id": ep, "span_id": d["span_id"], "show": row["show_title"],
            "pub_date": row["pub_date"], "lo": row["orig_start_s"], "hi": row["orig_end_s"]})
        ad["lo"], ad["hi"] = min(ad["lo"], row["orig_start_s"]), max(ad["hi"], row["orig_end_s"])
        reads.append({"evidence_id": d["evidence_id"], "episode_id": ep, "span_id": d["span_id"],
                      "says_name": False,
                      "repaired_from": _repair_of(docs[ep], d["span_id"], d["raw_brand"]),
                      "source_record_key": row["source_record_key"]})
    for ad in ads.values():
        ad["text"] = _ad_text((current.get(ad["episode_id"]) or {}).get("segments") or [],
                              ad["lo"], ad["hi"])
        ad["says"] = _says(ad["text"], key)
    for r in reads:
        ad = ads[(r["episode_id"], r["span_id"])]
        r["says_name"], r["ad_start_s"], r["ad_end_s"] = ad["says"], ad["lo"], ad["hi"]
    saying = sorted((a for a in ads.values() if a["says"]),
                    key=lambda a: (a["episode_id"], a["lo"], str(a["span_id"])))
    n = {"name": key, "decision": "skip", "reason": "", "reads": reads,
         "reads_total": len(reads), "reads_saying": sum(r["says_name"] for r in reads),
         "reads_not_saying": sum(not r["says_name"] for r in reads), "ads": len(saying),
         "episodes": sorted({a["episode_id"] for a in saying}),
         "shows": sorted({a["show"] for a in saying if a["show"]}),
         "input": None, "input_sha256": None, "prompt": None, "verdict": None, "company": None,
         "official_domain": None, "confidence": None, "existing": (existing or {}).get("status")}
    urls, codes = _urls_codes([a["text"] for a in saying], key)
    n["urls_heard"], n["codes_heard"] = urls, codes
    if saying:
        n["input"] = {"name": key, "excerpts": [
            {"episode_id": a["episode_id"], "show": a["show"], "pub_date": a["pub_date"],
             "timestamp_s": a["lo"], "text": _cut(a["text"], key)} for a in saying[:MAX_EXCERPTS]],
            "urls_heard": urls, "codes_heard": codes, "episode_notes": None}
        n["input_sha256"] = input_digest(n["input"])
        n["prompt"] = prompt(n["input"])
    if _is_confirmed(con, existing):
        n["reason"] = "confirmed"
    elif not saying:
        n["reason"] = NO_SAYING
        heard = [r["repaired_from"] for r in reads]
        if reads and all(heard):
            n["reason"] += f" (namer repair: {', '.join(dict.fromkeys(heard))} -> {key})"
    elif SECRET_LINE.search(n["prompt"]):
        n["reason"] = SECRET_REASON
    elif not force and existing and existing.get("prompt_version") == PROMPT_VERSION \
            and (existing.get("provenance") or {}).get("input_sha256") == n["input_sha256"]:
        n["reason"] = "unchanged"
    else:
        n["decision"] = "ask"
    return n


# -- The call ---------------------------------------------------------------------

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower())[:40] + "-" + hashlib.sha1(
        name.encode()).hexdigest()[:8]


def ask(plans, *, agy: str = "agy", log_dir, run_id: str, max_calls: int = 100, workers: int = 3,
        model: str = MODEL, backoff: float = 20.0, cache_only: bool = False,
        use_cache: bool = True, sleep=time.sleep) -> list:
    """One agy call per planned name (no database access). A name whose input
    has an ok line in `<log dir>/calls.jsonl` is answered from it, with no
    call (the call cache). Every attempt is one line of calls.jsonl, its raw
    stream is kept under raw/<run id>/. Two retries, backoff 20 s doubled
    (tripled on a quota error); the run stops after 8 errors in a row;
    `max_calls` caps agy invocations, retries included. The cache key is the
    input digest, the prompt version and the model asked for. `use_cache`
    false (--force) makes a new call for every name; `cache_only` answers
    from the cache and never calls. Results come back in the order of
    `plans`."""
    log_dir = Path(log_dir).resolve()    # agy runs elsewhere: every path it gets is absolute
    if "/" in agy:
        agy = str(Path(agy).resolve())
    calls_path = log_dir / "calls.jsonl"
    cache: dict = {}
    if calls_path.is_file():
        for line in calls_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("ok") and (use_cache or cache_only):
                cache[(rec["input_sha256"], rec["prompt_version"],
                       rec.get("requested_model") or rec.get("model"))] = rec
    state = {"calls": 0, "consecutive": 0, "stop": False, "pause_until": 0.0}
    lock = threading.Lock()

    def answer(plan_, rec, cached, calls):
        return {"name": plan_["name"], "input_sha256": plan_["input_sha256"], "ok": True,
                "cached": cached, "calls": calls, "error": None, "output": rec["output"],
                "model": rec.get("model"), "usage": rec.get("usage"), "seconds": rec["seconds"],
                "agy_seconds": rec.get("agy_seconds"), "searches": rec.get("searches") or [],
                "search_count": rec.get("search_count") or 0, "fetch_date": rec["at"],
                "call": {"log": str(calls_path), "raw": str(log_dir / rec["raw"]),
                         "attempt": rec["attempt"], "seconds": rec["seconds"],
                         "usage": rec.get("usage")}}

    def failure(plan_, error, calls):
        return {"name": plan_["name"], "input_sha256": plan_["input_sha256"], "ok": False,
                "cached": False, "calls": calls, "error": error, "output": None}

    def take_call():
        with lock:
            if state["stop"] or state["calls"] >= max_calls:
                return False
            state["calls"] += 1
            return True

    schema_path = log_dir / "schema-a6.json"

    def one(plan_):
        hit = cache.get((plan_["input_sha256"], PROMPT_VERSION, model))
        if hit:
            return answer(plan_, hit, True, 0)
        if cache_only:
            return failure(plan_, "not answered (no saved answer for this input)", 0)
        made, error = 0, "max-calls"
        for attempt in range(1, RETRIES + 2):
            wait = state["pause_until"] - time.time()
            if wait > 0:
                sleep(wait)
            if not take_call():
                return failure(plan_, error if made else "max-calls", made)
            made += 1
            rec = _attempt(plan_, attempt, agy, model, log_dir, schema_path, run_id)
            with lock:
                with open(calls_path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                if rec["ok"]:
                    state["consecutive"] = 0
                else:
                    state["consecutive"] += 1
                    back = backoff * 2 ** (attempt - 1) * (3 if rec["error"] == "quota" else 1)
                    state["pause_until"] = max(state["pause_until"], time.time() + back)
                    if state["consecutive"] >= MAX_CONSECUTIVE_ERRORS:
                        state["stop"] = True
            if rec["ok"]:
                return answer(plan_, rec, False, made)
            error = rec["error"]
        return failure(plan_, error, made)

    plans = list(plans)
    if not plans:
        return []
    if not cache_only and any((p["input_sha256"], PROMPT_VERSION, model) not in cache
                              for p in plans):
        # Once, before any worker starts: a call never reads a half-written file.
        (log_dir / "raw" / run_id).mkdir(parents=True, exist_ok=True)
        schema_path.write_text(json.dumps(SCHEMA_A6))
    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as pool:
        return list(pool.map(one, plans))


def _attempt(plan_, attempt, agy, model, log_dir: Path, schema_path: Path, run_id: str) -> dict:
    (log_dir / "raw" / run_id).mkdir(parents=True, exist_ok=True)
    cmd = [agy, "--model", model, "--print-timeout", PRINT_TIMEOUT, "--output-format",
           "stream-json", "--json-schema", str(schema_path), "-p=" + plan_["prompt"]]
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="brand-suggest-") as empty:   # agy is an agent
        try:
            p = subprocess.run(cmd, cwd=empty, capture_output=True, text=True,
                               timeout=HARD_TIMEOUT_S)
            raw, err, rc = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            raw, err, rc = out.decode() if isinstance(out, bytes) else out, "hard timeout", -9
        except FileNotFoundError:
            raise ValueError(f"agy not found: {agy}") from None
    secs = round(time.time() - t0, 2)
    raw_rel = Path("raw") / run_id / f"{_slug(plan_['name'])}.{attempt}.jsonl"
    (log_dir / raw_rel).write_text(raw + ("\n#STDERR\n" + err if err else ""))
    tools, result, _steps = parse_stream(raw)
    output = (result or {}).get("structured_output")
    names = [t["tool"] for t in tools]
    queries = [t["params"]["query"] for t in tools if t["tool"] in SEARCH_TOOLS
               and isinstance(t.get("params"), dict) and isinstance(t["params"].get("query"), str)]
    rec = {"name": plan_["name"], "input_sha256": plan_["input_sha256"],
           "prompt_version": PROMPT_VERSION, "attempt": attempt, "rc": rc, "seconds": secs,
           "agy_seconds": (result or {}).get("duration_seconds"),
           "status": (result or {}).get("status"), "usage": (result or {}).get("usage"),
           "model": stream_model(raw), "requested_model": model, "searches": queries,
           "search_count": sum(n in SEARCH_TOOLS for n in names),
           "other_tools": sorted({n for n in names if n not in WEB_TOOLS and n != "finish"}),
           "output": output, "raw": str(raw_rel), "at": _db.utcnow(), "run_id": run_id}
    rec["ok"] = rc == 0 and rec["status"] == "SUCCESS" and _valid(output)
    rec["error"] = None
    if not rec["ok"]:
        blob = (raw[-2000:] + err).lower()
        rec["error"] = ("quota" if re.search(r"429|quota|resource_exhausted|rate limit", blob)
                        else "bad_output" if rc == 0 else f"rc={rc}")
        rec["error_tail"] = (err or raw)[-400:]
    return rec


# -- Self-promotion (the experiment's v2_self_promo `match`) ------------------------

def _own_networks(ident: dict | None) -> list:
    """The publishers the show's feed names (authors, owners) as compact keys
    without network words (pipeline/promos.own_networks; promos.py is not in
    this tree, so the few lines are copied)."""
    def norm(text):
        t = str(text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
        return " ".join(re.findall(r"[a-z0-9]+", t))
    out = []
    for n in list((ident or {}).get("authors") or ()) + list((ident or {}).get("owners") or ()):
        words = [w for w in norm(n).split() if w not in _sp.NETWORK_WORDS
                 and w not in ("and", "the", "inc", "llc", "corp", "co", "studios", "studio")]
        k = "".join(words)
        if len(k) >= 3 and k not in out:
            out.append(k)
    return out


def _label_of(domain):
    if not domain:
        return None
    d = str(domain).strip().lower()
    return _sp.site_label(urlsplit(d if "//" in d else f"//{d}").hostname or "")


def self_promo_match(company, domain, ident: dict):
    """Why the company is the show's own promotion (own name, own domain, the
    publisher the feed names, a network's channel link), else None."""
    bkey = _sp.name_key(company)
    own_dom = _sp._own_labels(ident)
    dlab = _label_of(domain)
    hit = _sp._name_match(bkey, ident)
    if hit:
        return f"own-name:{hit}"
    hit = _sp._domain_match(bkey, own_dom)
    if hit:
        return f"own-domain:{hit}"
    if dlab and _sp._is_own_label(dlab, own_dom):
        return f"own-domain:{dlab}"
    pubs = _own_networks(ident)
    ckey = _own_networks({"authors": [company]})
    if ckey and ckey[0] in pubs:
        return f"author-owner:{ckey[0]}"
    if dlab and dlab in pubs:
        return f"author-owner:{dlab}"
    chan = {lab for lab, kinds in (ident.get("domain_sources") or {}).items()
            if "channel-link" in kinds and lab in _sp.NETWORKS}
    if dlab and dlab in chan:
        return f"network-domain:{dlab}"
    return None


def _show_identity(con, corpus: Path, episode_id: str, show_title: str) -> dict:
    """The read's own show identity: the cached one (`<corpus>/show-identity`)
    when the folder exists and the app knows the episode's feed URL, else the
    show-title fallback the experiment used."""
    folder = corpus / "show-identity"
    feed = None
    if folder.is_dir() and all(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
            for t in ("customer_episode_shows", "customer_show_identities")):
        row = con.execute(
            "SELECT i.authority_key FROM customer_episode_shows s JOIN customer_show_identities"
            " i ON i.id=s.show_identity_id WHERE s.episode_id=? AND i.authority_basis='feed_url'",
            (episode_id,)).fetchone()
        feed = row[0] if row else None
    ident = _sp.load_identity(folder, feed) if feed else None
    if ident is None:
        title = show_title or ""
        ident = {"title": title, "names": _sp._uniq([title] + _sp.TITLE_SPLIT.split(title))}
    return ident


def _self_promo_hit(con, corpus, n: dict, output: dict):
    """The first self-promotion hit among the reads that say the name:
    {"reason", "show"} or None."""
    if not output.get("is_brand") or not output.get("company"):
        return None
    shows = dict(con.execute("SELECT id, show_title FROM episodes WHERE id IN (%s)" % ",".join(
        "?" * len(n["episodes"])), n["episodes"])) if n["episodes"] else {}
    for ep in n["episodes"]:
        ident = _show_identity(con, Path(corpus), ep, shows.get(ep) or "")
        why = self_promo_match(output["company"], output.get("official_domain"), ident)
        if why:
            return {"reason": why, "show": shows.get(ep) or ident.get("title")}
    return None


# -- Store ------------------------------------------------------------------------

def _evidence_words(text: str) -> str:
    return " ".join(str(text or "").split()[:25])[:200]


def store(con, plan_: dict, results: list, *, run_id: str, corpus, actor: str = "operator") -> dict:
    """Store the answers in one write transaction: the suggestion rows
    (corrections._put_rule; status pending, a re-asked dismissed one becomes
    pending again), one audit row and one revision. Nothing to store is no
    write and no audit row. Returns the plan's report dict, updated."""
    from . import corrections
    out = {**plan_, "run_id": run_id, "dry_run": False, "stored": [], "failed": [], "skipped": [],
           "calls": sum(r["calls"] for r in results),
           "cached": sum(bool(r.get("cached")) for r in results),
           "names": [dict(n) for n in plan_["names"]]}
    by = {r["name"]: r for r in results}
    for n in out["names"]:
        if n["decision"] != "ask":
            out["skipped"].append({"name": n["name"], "reason": n["reason"]})
            continue
        r = by.get(n["name"])
        if r is None or not r["ok"]:
            n["decision"], n["reason"] = "failed", (r or {}).get("error") or "not asked"
            out["failed"].append({"name": n["name"], "error": n["reason"]})
    ready = [n for n in out["names"] if n["decision"] == "ask"]
    if not ready:
        return out
    verdicts = {}
    with _db.transaction(con, write=True):
        existing = _suggestion_rows(con)
        for n in ready:
            r = by[n["name"]]
            output = r["output"]
            hit = _self_promo_hit(con, corpus, n, output)
            verdict = verdict_of(output, hit)
            company = (output.get("company") or "").strip() or None
            value = {
                "status": "pending", "verdict": verdict, "company": company,
                "official_domain": (output.get("official_domain") or "").strip() or None,
                "heard_correct": output["heard_is_exact"] if output["is_brand"] else None,
                "confidence": output["confidence"], "evidence": _evidence_words(output["reason"]),
                "searches": r["searches"], "search_count": r["search_count"], "self_promo": hit,
                "model": r["model"], "prompt_version": PROMPT_VERSION,
                "fetch_date": r["fetch_date"],
                "provenance": {
                    "producer": ACTOR, "run_id": run_id, "corpus": str(corpus),
                    "input_sha256": n["input_sha256"],
                    "reads": [{k: v for k, v in rd.items() if k != "source_record_key"}
                              for rd in n["reads"]],
                    "reads_not_saying": n["reads_not_saying"], "episodes": len(n["episodes"]),
                    "ads": n["ads"], "shows": n["shows"], "urls_heard": n["urls_heard"],
                    "codes_heard": n["codes_heard"], "call": r["call"]},
                "decided_by": None, "decided_at": None, "rules": []}
            if _is_confirmed(con, existing.get(n["name"])):
                continue
            corrections._put_rule(con, KIND, n["name"], value, ACTOR)
            out["stored"].append(n["name"])
            verdicts[n["name"]] = verdict
            n.update(decision="stored", verdict=verdict, company=company,
                     official_domain=value["official_domain"], confidence=output["confidence"])
        if out["stored"]:
            rev = _db.bump_revision(con)
            models = sorted({by[k]["model"] for k in out["stored"] if by[k].get("model")})
            out["model"] = models[0] if len(models) == 1 else models or MODEL
            _db.audit(con, actor, "brand_suggest", "workspace", run_id,
                      {"prompt_version": PROMPT_VERSION, "model": out["model"],
                       "corpus": str(corpus),
                       "stored": out["stored"], "verdicts": verdicts,
                       "failed": out["failed"], "skipped": out["skipped"]}, rev)
    return out


def format_report(result: dict) -> str:
    dry = result["dry_run"]
    lines = [("DRY RUN (writes nothing): " if dry else "")
             + f"brand suggestions {result['prompt_version']}, run {result['run_id']}"]
    if result.get("skipped_all"):
        lines.append(f"nothing suggested: {result['skipped_all']}")
    lines.append(f"{result['candidates']} off-list names, {result['reads']} reads")
    lines.append("name\tdecision\treads\tverdict\tsuggestion\tconfidence\treason")
    for n in result["names"]:
        if n.get("verdict") == "not_brand":
            suggestion = "not a brand"
        elif n.get("company"):
            suggestion = n["company"] + (f" ({n['official_domain']})" if n.get("official_domain")
                                         else "")
        else:
            suggestion = "-"
        confidence = "-" if n.get("confidence") is None else f"{n['confidence']:.2f}"
        lines.append(f"{n['name']}\t{n['decision']}\t{len(n['reads'])}\t{n.get('verdict') or '-'}"
                     f"\t{suggestion}\t{confidence}\t{n['reason']}")
    if dry:
        lines.append(f"would ask {result['asked']} names")
    else:
        lines.append(f"asked {result['asked']} names ({result['calls']} calls, cached"
                     f" {result['cached']}), stored {len(result['stored'])} suggestions")
    return "\n".join(lines)


# -- The operator panel -----------------------------------------------------------

def _episode_segments(con, episode_id, memo: dict) -> list:
    if episode_id not in memo:
        row = con.execute("SELECT sv.payload FROM episodes e JOIN source_versions sv ON"
                          " sv.id=e.transcript_version_id WHERE e.id=?", (episode_id,)).fetchone()
        try:
            memo[episode_id] = json.loads(row[0]).get("segments") or [] if row else []
        except (TypeError, ValueError, AttributeError):
            memo[episode_id] = []
    return memo[episode_id]


def _short_excerpt(text: str, key: str) -> str:
    """At most 200 characters around the heard name (or its address)."""
    text = " ".join(text.split())
    found = brand_curate.spoken_forms(text, key) or [m[1] for m in
                                                     brand_curate.address_matches(text, key)]
    at = text.lower().find(found[0].lower()) if found else -1
    start = max(0, at - 80) if at >= 0 else 0
    return text[start:start + 200].strip()


def suggestions(con) -> list:
    """Every stored suggestion for the operator panel. `state` is the stored
    status, except a pending suggestion whose name is known now, stoplisted or
    mapped by an alias rule (`resolved`: nothing to confirm), and a confirmed
    one whose rules were all removed in the Rules panel (`rule_removed`). The read count, shows and excerpt come
    only from the reads that say the name."""
    from . import corrections
    rows = _suggestion_rows(con)
    rules = corrections.load(con)
    memo: dict = {}
    out = []
    for key, v in rows.items():
        prov = v.get("provenance") or {}
        status = v.get("status")
        state = status
        if status == "pending" and _resolved(con, key, rules):
            state = "resolved"
        elif status == "confirmed" and _rules_gone(con, v):
            state = "rule_removed"
        live = _live_rules(con, v) if status == "confirmed" else []
        excerpt = ""
        first = next((r for r in prov.get("reads") or () if r.get("says_name")), None)
        if first and first.get("episode_id") is not None:
            excerpt = _short_excerpt(_ad_text(
                _episode_segments(con, first["episode_id"], memo),
                first.get("ad_start_s", 0), first.get("ad_end_s", 0)), key)
        out.append({
            "id": v["id"], "key": key, "state": state, "status": status,
            "verdict": v.get("verdict"), "company": v.get("company"),
            "official_domain": v.get("official_domain"), "heard_correct": v.get("heard_correct"),
            "confidence": v.get("confidence"), "evidence": v.get("evidence"),
            "searches": v.get("searches") or [], "self_promo": v.get("self_promo"),
            "model": v.get("model"), "prompt_version": v.get("prompt_version"),
            "fetch_date": v.get("fetch_date"), "run_id": prov.get("run_id"),
            "decided_by": v.get("decided_by"), "decided_at": v.get("decided_at"),
            "rules": live, "ads": prov.get("ads", 0),
            "confirm_writes": _writes(con, key, v, rules) if state == "pending" else None,
            "shows": prov.get("shows") or [], "reads_not_saying": prov.get("reads_not_saying", 0),
            "excerpt": excerpt})
    order = {"pending": 0, "confirmed": 1, "rule_removed": 1, "dismissed": 2, "resolved": 3}
    out.sort(key=lambda s: (order.get(s["state"], 9), s["verdict"] == "unsure", s["key"]))
    return out


# -- Confirm / Not this / Undo ----------------------------------------------------

def _load(con, suggestion_id):
    from .ops import BadInput
    try:
        sid = int(suggestion_id)
    except (TypeError, ValueError):
        raise BadInput("unknown suggestion")
    row = con.execute("SELECT id, key, value FROM ad_rules WHERE id=? AND kind=?",
                      (sid, KIND)).fetchone()
    if row is None:
        raise BadInput("unknown suggestion")
    return row["id"], row["key"], json.loads(row["value"] or "{}")


SHOWN = ("verdict", "company", "heard_correct", "fetch_date")


def _shown_of(source) -> dict:
    out = {k: source.get(k) for k in SHOWN}
    out["company"] = out["company"] or None
    return out


def _pending_as_shown(value: dict, body) -> None:
    """The suggestion is still pending and is what the page showed (verdict,
    company, heard_correct and fetch_date: a later answer for the same name
    is another suggestion), else ReadChanged and nothing is saved."""
    from . import corrections
    body = body if isinstance(body, dict) else {}
    now = _shown_of(value)
    if value.get("status") != "pending" or _shown_of(body) != now:
        raise corrections.ReadChanged(
            "This suggestion changed since the page showed it. Nothing was saved; look again.",
            {"status": value.get("status"), **now})


def _save(con, key: str, value: dict) -> None:
    from . import corrections
    corrections._put_rule(con, KIND, key, value, ACTOR)


def _suggestion_fields(sid: int, value: dict) -> dict:
    keys = ("verdict", "company", "official_domain", "heard_correct", "confidence", "evidence",
            "searches", "model", "prompt_version", "fetch_date")
    return {"id": sid, **{k: value.get(k) for k in keys},
            "run_id": (value.get("provenance") or {}).get("run_id")}


def _first_read_key(con, value: dict):
    """The source_record_key of the first read that says the name."""
    for r in (value.get("provenance") or {}).get("reads") or ():
        if not r.get("says_name"):
            continue
        row = con.execute("SELECT source_record_key FROM evidence WHERE id=?",
                          (r.get("evidence_id"),)).fetchone()
        if row:
            return row[0]
    return None


@_db.atomic
def confirm(con, suggestion_id, body, *, actor: str = "owner", approved_by: str = "Reno") -> dict:
    """Make the suggestion's rules (module docstring). body: verdict and
    company as the page showed them (a mismatch or a suggestion that is not
    pending is ReadChanged, nothing saved), and expected_reads (plus
    known_brand) when the count was asked: a rule that changes a shown read,
    or touches a known brand, is saved only with the count; otherwise one
    click. No label, review or brand confirmation is written."""
    from . import ad_reads, corrections
    from .ops import BadInput
    corrections.require_tables(con)
    sid, key, value = _load(con, suggestion_id)
    _pending_as_shown(value, body)
    rules = corrections.load(con)
    if _resolved(con, key, rules):
        raise corrections.ReadChanged(
            "This name is a known brand, a generic word or has an alias rule now. Nothing was"
            " saved; look again.", {"status": "resolved", **_shown_of(value)})
    verdict, company = value.get("verdict"), value.get("company")
    if verdict not in ("brand", "not_brand"):
        raise BadInput("a suggestion with no clear answer cannot be confirmed; say Not this")
    extra = {"source": SOURCE.format(approved_by=approved_by),
             "suggestion": _suggestion_fields(sid, value)}
    run_id = (value.get("provenance") or {}).get("run_id")
    from_read = _first_read_key(con, value)
    made: list = []   # (kind, key)
    reads = 0

    def put(kind, rkey, rvalue, who=actor):
        corrections._put_rule(con, kind, rkey, {**rvalue, **extra}, who)
        made.append((kind, rkey))
    writes, canonical = _writes_canonical(con, key, value, rules)
    if writes == "brand_list":
        bkey = ad_reads.brand_key(key)
        display = (company or "").strip()
        refusal = brand_curate.manual_refusal(con, bkey, rules, ignore_suggestion_undone=True)
        if not bkey or not display:
            raise BadInput("a brand needs a heard name and a company")
        if refusal:
            raise BadInput(refusal)
        put("brand", bkey, {"display": display, "origin": "manual", "approved_by": approved_by,
                            "note": "", "run_id": run_id}, approved_by)
    elif writes == "alias":
        heard = key
        company = (company or "").strip()
        if not canonical or canonical in rules["stoplist"]:
            raise BadInput(f"“{company}” is on the generic-word list, not a brand")
        if heard in rules["stoplist"]:
            raise BadInput(f"“{heard}” is a generic word, not a spelling of a brand")
        if heard == canonical or ad_reads.canonical(ad_reads.alias_map(con), heard) == canonical:
            raise BadInput("every read heard this way already shows this brand")
        shown_before = corrections._shown_reads(con)
        heard_known = corrections.known_brand(con, heard, rules)
        if not corrections.known_brand(con, canonical, rules):
            put("brand", canonical, {"display": company, "from_read": from_read})
        put("alias", heard, {"canonical": canonical,
                             "display": company if company != company.lower() else None,
                             "from_read": from_read})
        ad_reads.refresh_rule_facts(con, actor, words={heard})
        reads = len(corrections._count_changes(shown_before, corrections._shown_reads(con)))
        if reads or heard_known:
            corrections._expect(body, {"raw": heard, "brand": canonical, "reads": reads,
                                       "known_brand": heard_known},
                                f"Every read heard as “{heard}” becomes {company}: confirm with"
                                " the count.")
    else:
        word = key
        if word in rules["alias"] or word in {c for c, _d in rules["alias"].values()}:
            raise BadInput(f"“{word}” is part of a brand alias; remove that rule first")
        if word in rules["stoplist"]:
            raise BadInput(f"“{word}” is already a generic word")
        shown_before = corrections._shown_reads(con)
        word_known = corrections.known_brand(con, word, rules)
        put("stoplist", word, {"from_read": from_read})
        ad_reads.refresh_rule_facts(con, actor, words={word})
        reads = len(corrections._count_changes(shown_before, corrections._shown_reads(con)))
        if reads or word_known:
            corrections._expect(body, {"word": word, "reads": reads, "known_brand": word_known},
                                f"“{word}” becomes a generic word on every read: confirm with"
                                " the count.")
    ids = [[kind, rkey, con.execute("SELECT id FROM ad_rules WHERE kind=? AND key=?",
                                    (kind, rkey)).fetchone()[0]] for kind, rkey in made]
    value.update(status="confirmed", decided_by=approved_by, decided_at=_db.utcnow(), rules=ids)
    _save(con, key, value)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "brand_suggestion_confirm", "ad_rule", key,
              {"suggestion": extra["suggestion"], "rules": ids, "reads": reads,
               "approved_by": approved_by}, rev)
    return {"id": sid, "state": "confirmed", "rules": ids, "reads": reads,
            "message": "Confirmed. The rules apply to every read now and on every load.",
            "aggregate_rev": rev}


@_db.atomic
def dismiss(con, suggestion_id, body, *, actor: str = "owner") -> dict:
    """Not this: the pending suggestion the page showed is left unconfirmed.
    Nothing else changes."""
    from . import corrections
    corrections.require_tables(con)
    sid, key, value = _load(con, suggestion_id)
    _pending_as_shown(value, body)
    value.update(status="dismissed", decided_by=actor, decided_at=_db.utcnow())
    _save(con, key, value)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "brand_suggestion_dismiss", "ad_rule", key,
              {"suggestion": _suggestion_fields(sid, value)}, rev)
    return {"id": sid, "state": "dismissed", "aggregate_rev": rev}


@_db.atomic
def undo(con, suggestion_id, *, actor: str = "owner", expected_reads=UNCHECKED,
         shown=None) -> dict:
    """Undo Not this (dismissed -> pending) or a confirm: the rules the
    confirm made that are still there are removed, newest first
    (corrections.remove_rule; a rule removed earlier from the Rules panel is
    skipped), then the suggestion is pending again. Count first as the Rules
    panel Remove asks: when the removals change shown reads, expected_reads
    must be that count (else ConfirmNeeded and nothing is undone). `shown`
    (the page's status and fetch_date, when given) must match the stored
    suggestion, else ReadChanged and nothing is undone."""
    from . import corrections
    from .ops import BadInput
    corrections.require_tables(con)
    sid, key, value = _load(con, suggestion_id)
    status = value.get("status")
    if shown is not None and (shown.get("status"), shown.get("fetch_date")) != (
            status, value.get("fetch_date")):
        raise corrections.ReadChanged(
            "This suggestion changed since the page showed it. Nothing was undone; look again.",
            {"status": status, "fetch_date": value.get("fetch_date")})
    removed: list = []
    reads = 0
    if status == "confirmed":
        before = corrections._shown_reads(con)
        for kind, rkey, rid in reversed(_live_rules(con, value, sid)):   # only its own rules
            corrections.remove_rule(con, rid, actor, reason="undone")
            removed.append([kind, rkey, rid])
        reads = len(corrections._count_changes(before, corrections._shown_reads(con)))
        if reads and expected_reads is not UNCHECKED:
            corrections._expect({"expected_reads": expected_reads},
                                {"reads": reads, "known_brand": False},
                                f"Undoing this changes {reads} read{'' if reads == 1 else 's'}:"
                                " confirm with the count.")
    elif status != "dismissed":
        raise BadInput("nothing to undo: this suggestion is waiting")
    value.update(status="pending", decided_by=None, decided_at=None, rules=[])
    _save(con, key, value)
    rev = _db.bump_revision(con)
    _db.audit(con, actor, "brand_suggestion_undo", "ad_rule", key,
              {"from": status, "removed": removed, "reads": reads}, rev)
    return {"id": sid, "state": "pending", "removed": removed, "reads": reads,
            "message": "Undone. The suggestion is waiting again.", "aggregate_rev": rev}
