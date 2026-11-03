"""Ask's model credential and tool dispatcher (base design 4.2-4.3, chat doc 3).

`make_model_session` derives the `model_session` credential from a member's
live browser session: scopes exactly research:read, plus transcripts:read
when the addendum allows, and the origin session's id and switch revision.
services._current_authority re-reads that session on every call.

`dispatch` runs one model tool call:
  1. a name outside ALLOWLIST is refused (TOOL_NOT_ALLOWED) before any work;
  2. only a model_session context is dispatched;
  3. a stop-word-only search_transcripts query is refused (STOP_WORD_QUERY),
     and so is a search_evidence filter with no query and no brand
     (FILTER_TOO_BROAD: it would return the collection's first records);
     a search_evidence query also needs three letters or digits, since it
     is a substring match;
  4. the fence (services.fenced_authority_check) runs before the tool, so a
     signed-out session does no work, and again after every result, errors
     included; a refusal discards the result (FORBIDDEN; a fence failure that
     is not an authority refusal is INTERNAL, also discarded);
  5. AGENT_ENTRYPOINTS[name] runs under its own ExecutionBudget, with
     check_result_fit and _fail_from as earwitness.agents.call_tool applies
     them;
  6. a 0-hit multi-word search_transcripts with complete coverage gets the
     one-segment hint;
  7. search_evidence drops items whose query matched only across fields or
     in attrs (match_basis cross_field; Ask's model has no notes): the search
     also matches the attrs JSON, key names included, so "code" or "url"
     would otherwise match every detector record. The drop runs on the page
     the tool already cut.
Brand mode (`outside_ads=True`) pages search_transcripts 50 hits a page, up
to 10 pages, and keeps only hits the injected `is_mention` rule (ask_contract's
mention label) returns True for.

No HTTP, no model, no writes.
"""
from __future__ import annotations

import copy
import json
import re

from earwitness.app import access as _access
from earwitness.app import agent_contracts as _contracts
from earwitness.app import services as _services

ALLOWLIST = ("search_transcripts", "get_transcript_window", "search_evidence",
             "get_evidence", "list_brands", "list_shows")

RESEARCH_READ = _contracts.RESEARCH_READ
TRANSCRIPTS_READ = _contracts.TRANSCRIPTS_READ

TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
FORBIDDEN = "FORBIDDEN"
STOP_WORD_QUERY = "STOP_WORD_QUERY"
STOP_WORD_MESSAGE = "search for a more specific word; very common words are not searched"
ONE_SEGMENT_HINT = "0 hits: all words must be in one short segment; try one or two words"
FILTER_TOO_BROAD = "FILTER_TOO_BROAD"
FILTER_TOO_BROAD_MESSAGE = ("give filter.query (one or two literal words) or filter.brand"
                            " (a name from list_brands)")
#: Model-facing text for refusals the model can fix by asking again.
MESSAGES = {STOP_WORD_QUERY: STOP_WORD_MESSAGE, FILTER_TOO_BROAD: FILTER_TOO_BROAD_MESSAGE}

EVIDENCE_QUERY_MIN = 3   # letters or digits in a search_evidence query
OUTSIDE_ADS_PAGE = 50
OUTSIDE_ADS_PAGES = 10

# Words Ask refuses to search alone. Measured on the 1,500-episode demo copy
# (2026-10-26, agent search_transcripts, one word each): words in 50,000+
# segments; common two-letter words and contraction pieces (under three
# letters they skip the transcript index: "he" 5.0 s, "re" 2.2 s); slower
# ones (his, her, going, i'll: 0.8-1.8 s). Every one-letter word is refused
# too (stop_words_only; "s" 6.6 s). Rare function words (hers, ours, off,
# whom: 0.1-0.2 s) and two-letter topics such as "AI" (0.8 s) stay
# searchable; some are brand names. Not complete: the cost follows how often
# the letters occur inside words, so word parts such as "th" (4.2 s) or
# "ing" (2.5 s) still run, bounded by the per-call budget.
STOP_WORDS = frozenset("""
a about all am an and are as at be because but by can do don't for get go going
have he her his i i'll i'm if in is it it's just know like ll me my no not of oh
ok on one or out re right so that that's the there they think this to uh um up ve
was we what with yeah you your
""".split())

_EDGE = re.compile(r"^[\W_]+|[\W_]+$")
# A possessive at the end of a brand name ("BetterHelp's", "Mint Mobile’s").
_POSSESSIVE = re.compile(r"['\u2019]s\s*$", re.IGNORECASE)


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


#: Ask's search_evidence filter: the ResearchFilterV1 fields Ask's model may
#: use, all plain strings validate_filter accepts. Ask's override only; the
#: shared agent spec (earwitness.agents.TOOL_DEFS) is unchanged.
EVIDENCE_FILTER_SCHEMA = {
    "type": "object",
    "description": "Which ads to return. Give query or brand.",
    "properties": {
        "query": _text("A word, or two words in their spoken order, found in the ad"
                       " text, brand, show or episode title; at least 3 letters."),
        "brand": _text("A brand name exactly as list_brands gives it."),
        "show_query": _text("Optional: part of a show title."),
        # validate_filter matches a category whole (ops: ep.category=?); an
        # ad passage gives its show's category (ASK-RT1 RT-2).
        "category": _text("Optional: a show category exactly as an ad passage gives it,"
                          " such as Comedy."),
        "published_from": _text("Optional: earliest publication date, YYYY-MM-DD."),
        "published_to": _text("Optional: latest publication date, YYYY-MM-DD."),
    },
    "additionalProperties": False,
}

#: Ask's list_brands filter: the same fields without brand, all optional
#: (ASK-RT1 RT-2).
LIST_BRANDS_FILTER_SCHEMA = {
    "type": "object",
    "description": "Which ads to count brands in; every field is optional.",
    "properties": {k: v for k, v in EVIDENCE_FILTER_SCHEMA["properties"].items()
                   if k != "brand"},
    "additionalProperties": False,
}
#: Rows Ask's list_brands asks for: the tool's most, so its count is whole
#: whenever the filter has no more brand rows than this.
LIST_BRANDS_ROWS = _contracts.LIST_HARD_MAX


def fold_category(text: str) -> str:
    """A category as count_ads matches it: case folded, letters and digits
    only, so "True Crime", "true crime" and "truecrime" are one category
    (red-team q4, 11-01)."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


#: Ask's own counting tool (ASK-RT2 item 1): run by ask._Run on the member's
#: customer view, never through dispatch or the agent contracts.
COUNT_ADS = "count_ads"
COUNT_ROWS = 10          # ranked rows a count result gives (W2 shows at most 10)
COUNT_FILTER_SCHEMA = {
    "type": "object",
    "description": "Which ads to count; every field is optional.",
    "properties": {
        "brand": _text("Optional: one brand name as list_brands gives it."),
        "show_query": _text("Optional: part of a show title."),
        "category": _text("Optional: part of a show category; Comedy also counts Comedy"
                          " Interviews and Comedy Fiction."),
        "published_from": _text("Optional: earliest publication date, YYYY-MM-DD."),
        "published_to": _text("Optional: latest publication date, YYYY-MM-DD."),
    },
    "additionalProperties": False,
}
COUNT_SPEC = {"type": "function", "function": {
    "name": COUNT_ADS,
    "description": ("Exact counts of the ads in this collection, as the app's Brands page"
                    " counts them: ads, episodes, shows and brands, with the brands (or shows)"
                    " that have the most ads. Use it for how many, most, top and ranking"
                    " questions. Each result has an id like c1."),
    "parameters": {"type": "object", "properties": {
        "filter": COUNT_FILTER_SCHEMA,
        "rank_by": {"type": "string", "enum": ["brands", "shows"],
                    "description": "Rank brands (the default) or shows by ads."}},
        "additionalProperties": False}}}


def _tool_specs() -> list:
    from earwitness.agents import TOOL_DEFS
    defs = {d["name"]: d for d in TOOL_DEFS}
    specs = []
    for name in ALLOWLIST:
        params = copy.deepcopy(defs[name]["schema"])
        if name == "search_evidence":
            params["properties"]["filter"] = copy.deepcopy(EVIDENCE_FILTER_SCHEMA)
        elif name == "list_brands":
            params["properties"]["filter"] = copy.deepcopy(LIST_BRANDS_FILTER_SCHEMA)
        specs.append({"type": "function",
                      "function": {"name": name, "description": defs[name]["description"],
                                   "parameters": params}})
    specs.append(copy.deepcopy(COUNT_SPEC))
    return specs


#: OpenAI-style `tools` list: the agent contracts' own schemas, with Ask's
#: search_evidence and list_brands filters.
TOOL_SPECS = _tool_specs()


def tool_specs(ctx) -> list:
    """TOOL_SPECS limited to the tools this credential's scopes reach: no
    transcript tools without transcripts:read (base design 4.3)."""
    have = set(ctx.scopes)
    scopes = dict(_contracts.TOOL_SCOPES, **{COUNT_ADS: (RESEARCH_READ,)})
    return [copy.deepcopy(spec) for spec in TOOL_SPECS
            if set(scopes[spec["function"]["name"]]) <= have]


# -- Counting (ASK-RT2 item 1) ----------------------------------------------

class CountRefused(ValueError):
    """A count_ads request the model can fix (INVALID_ARGUMENT)."""


def _count_args(args) -> tuple:
    """(filter, rank_by) of a count_ads request; CountRefused otherwise."""
    from earwitness.app import customer_contracts as _cc
    if not isinstance(args, dict) or set(args) - {"filter", "rank_by"}:
        raise CountRefused("count_ads takes filter and rank_by")
    filt = args.get("filter") or {}
    if not isinstance(filt, dict) or set(filt) - set(COUNT_FILTER_SCHEMA["properties"]):
        raise CountRefused("filter takes " + ", ".join(COUNT_FILTER_SCHEMA["properties"]))
    out = {}
    for key, value in filt.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise CountRefused(f"filter.{key} must be a string")
        value = " ".join(value.split())
        if not value:
            continue
        if key.startswith("published_") and _cc.publication_day(value) != value:
            raise CountRefused(f"filter.{key} must be a date, YYYY-MM-DD")
        out[key] = value
    if out.get("published_from") and out.get("published_to") \
            and out["published_to"] < out["published_from"]:
        raise CountRefused("filter.published_to is before filter.published_from")
    rank_by = args.get("rank_by") or "brands"
    if rank_by not in ("brands", "shows"):
        raise CountRefused("rank_by is brands or shows")
    return out, rank_by


def count_ads(con, ctx, args) -> dict:
    """_count_ads with model names on (COUNT-OFF-BY-1): a read Muse Spark
    named counts as Search's brand filter and the Brands page count it,
    unless an outer caller turned model names off."""
    from earwitness.app import customer as _cu
    with _cu.model_names(True):
        return _count_ads(con, ctx, args)


def _count_ads(con, ctx, args) -> dict:
    """Exact ad counts over the member's customer ad scope, from the same
    rows and helpers as the customer Brands page (customer._brands_directory):
    the Search Ads scope (customer._research_scope, Query2 mode "ads", DET
    and the display list as this workspace's settings make them), distinct
    ads by ad break (ad_breaks.scope_index and count), episodes and shows by
    customer._brand_reach, brand keys by ad_reads.assoc_key and names by
    customer.directory_display. A filter narrows the Query2: dates to
    from/to; a category to every category of the member's episodes that
    holds it (fold_category); show_query to the reads whose show title holds
    it; brand to that brand's row. Counts are over every matching ad,
    never a page. Raises CountRefused for a request the model can fix."""
    from earwitness.app import ad_breaks as _breaks
    from earwitness.app import ad_reads as _ad
    from earwitness.app import customer as _cu
    filt, rank_by = _count_args(args)
    query = {"version": 2, "mode": "ads"}
    if filt.get("published_from"):
        query["from"] = filt["published_from"]
    if filt.get("published_to"):
        query["to"] = filt["published_to"]
    categories = category_episodes = None
    if filt.get("category"):
        needle = fold_category(filt["category"])
        category_episodes = {}
        for r in _cu.permitted_episodes(con, ctx):
            cat = r.get("category")
            if needle and isinstance(cat, str) and needle in fold_category(cat):
                category_episodes[cat] = category_episodes.get(cat, 0) + 1
        categories = sorted(category_episodes)
        query["categories"] = categories
    items = _cu._research_scope(con, query, ctx)["items"]
    show_titles = show_ids = None
    if filt.get("show_query"):
        # The show title as the app shows it (the read's show), else the
        # episode's own; Search can carry the show only when every read
        # counted has one undisputed show identity (show_ids).
        needle = filt["show_query"].casefold()
        titles = {r["id"]: r.get("show_title") or "" for r in _cu.permitted_episodes(con, ctx)}

        def title_of(item):
            show = item.get("show") if isinstance(item.get("show"), dict) else {}
            for t in (show.get("display_title"), titles.get(item.get("episode_id"))):
                if isinstance(t, str) and t.strip():
                    return t
            return ""
        items = [i for i in items if needle in title_of(i).casefold()]
        show_titles = sorted({title_of(i) for i in items})
        ids = set()
        for i in items:
            show = i.get("show") if isinstance(i.get("show"), dict) else {}
            ids.add(show.get("show_identity_id") if show.get("show_identity_id")
                    and not _cu.show_mapping_disputed(show) else None)
        show_ids = sorted(ids) if None not in ids else [None]
    breaks = _breaks.scope_index(con, ctx)
    aliases = _cu.customer_aliases(con)
    merged = getattr(aliases, "display_keys", None) or {}
    by_key: dict = {}
    for item in items:
        # The Brands page's rule (customer.brand_count_keys): attribution
        # rows, then the keys Muse Spark named the read with.
        for key, brand in _cu.brand_count_keys(aliases, item):
            slot = by_key.setdefault(key, {"items": {}, "spellings": {}})
            slot["items"][id(item)] = item
            if brand is not None:
                slot["spellings"][brand] = slot["spellings"].get(brand, 0) + 1
    brands = []
    for key, slot in by_key.items():
        group = list(slot["items"].values())
        reach = _cu._brand_reach(group)
        shown = _ad.brand_display(con, key, aliases)
        brands.append({
            "key": key, "keys": [key, *sorted(merged.get(key, set()) - {key})],
            "brand": _cu._brand_representative(slot["spellings"]) if slot["spellings"]
            else shown[0],  # PODS: a brand only Muse Spark names (as the Brands page)
            "spellings": sorted(slot["spellings"]),
            "display": _cu.directory_display(key, aliases, slot["spellings"], shown),
            "ads": _breaks.count(breaks, (i.get("evidence_id") for i in group)),
            "episodes": reach["episodes"], "shows": reach["shows"]})
    brands.sort(key=lambda b: (-b["ads"], b["display"].casefold(), b["key"]))
    reach = _cu._brand_reach(items)
    result = {"filter": filt, "rank_by": rank_by, "categories": categories,
              "category_episodes": category_episodes,
              "show_titles": show_titles, "show_ids": show_ids,
              "total": {"ads": _breaks.count(breaks, (i.get("evidence_id") for i in items)),
                        "episodes": reach["episodes"], "shows": reach["shows"],
                        "brands": len(brands)}}
    if filt.get("brand"):
        def find(asked, directory=True):
            name = asked.casefold()
            key = _ad.brand_key(asked)
            key = (getattr(aliases, "display_filter", None) or {}).get(key) \
                or _ad.canonical(aliases, asked)
            found = [b for b in brands if key in b["keys"] or name in (
                b["display"].casefold(), b["brand"].casefold())
                or name in {s.casefold() for s in b["spellings"]}]
            hits = []
            if not found and directory:
                # The Brands page's own find (R1 MED-2: "Better Help" finds
                # BetterHelp): one brand found is that brand; several are no zero.
                hits = _cu._brands_directory(con, asked, ctx)
                if len(hits) == 1:
                    found = [b for b in brands
                             if hits[0]["key"] in b["keys"] or b["key"] in hits[0]["keys"]]
            return found, hits
        asked = filt["brand"]
        found, hits = find(asked)
        several = len(hits) > 1
        bare = _POSSESSIVE.sub("", asked)
        if not found and not hits and bare.strip() and bare != asked:
            # "BetterHelp's" is BetterHelp (count paths r1 LOW-1): an exact
            # key, display or spelling, else a Brands-page hit whose letters
            # match the stem's ("Better Help's", "MintMobile's"; confirm
            # MED-1), never a substring hit ("Help's" is no BetterHelp; plan
            # review M6). Other hits are matches, never a zero.
            found, _ = find(bare, directory=False)
            if not found:
                stem = _cu._compact_key(bare)
                hits = _cu._brands_directory(con, bare, ctx)
                same = [h for h in hits if stem in {_cu._compact_key(n) for n in (
                    h["key"], h["brand"], h["display"], *h["keys"])}]
                if len(same) == 1:
                    hits = same
                    found = [b for b in brands
                             if hits[0]["key"] in b["keys"] or b["key"] in hits[0]["keys"]]
                else:
                    several = bool(hits)
        result["brand"] = {k: found[0][k] for k in ("key", "brand", "display", "ads",
                                                     "episodes", "shows")} if found else None
        if not found and several:
            # Several brands match: named to the model (count paths r1 LOW-2).
            result["matches"] = [h["display"] for h in hits]
        if not found and not several:
            # No ad for the brand here (UX W1-zero): named as the Brands page
            # names it when it has ads elsewhere, else as asked; and whether
            # this scope has an ad with brand not identified.
            result["zero"] = {"display": hits[0]["display"] if hits else filt["brand"].strip(),
                              "named": filt["brand"],
                              # A read Muse Spark named is identified (RCM-F-01).
                              "unidentified": any(not _cu.brand_count_keys(aliases, i)
                                                  for i in items)}
        return result
    if rank_by == "shows":
        top = _cu._brand_top_shows(con, items, limit=COUNT_ROWS, breaks=breaks)["top_shows"]
        result["rows"] = [{"name": r["display_title"], "ads": r.get("ad_breaks", 0)}
                          for r in top if r["display_title"]]
    else:
        result["rows"] = [{"name": b["display"], "ads": b["ads"]} for b in brands[:COUNT_ROWS]]
    return result


# -- The model_session credential ------------------------------------------

def make_model_session(control, browser_ctx, *, transcripts: bool) -> _access.AccessContext:
    """A model_session from a member's live browser session.

    control: a control-store connection (sqlite3.Row rows). Refuses
    (AuthorizationError 403) anything but a member's browser session:
    agent tokens, share guests, bootstrap contexts, local credentials and
    other model sessions. The origin session must be live and on this
    workspace now."""
    if not isinstance(transcripts, bool):
        raise TypeError("transcripts must be a bool")
    b = browser_ctx
    if not isinstance(b, _access.AccessContext) or b.credential_kind != "browser_session" \
            or b.authority_kind != "membership" or b.is_share_guest \
            or b.principal_kind not in ("owner", "editor", "viewer") \
            or not b.session_id or b.session_binding is None or b.admitted_role is None \
            or not b.db_path or not b.db_identity or b.registry_policy is None:
        raise _access.AuthorizationError(
            "Ask needs a member's signed-in browser session", 403)
    scopes = {RESEARCH_READ, TRANSCRIPTS_READ} if transcripts else {RESEARCH_READ}
    ctx = _access.AccessContext.create(
        principal_id=b.principal_id, principal_kind=b.principal_kind,
        workspace_id=b.workspace_id, scopes=scopes, policy_revision=b.policy_revision,
        authority_kind="membership", credential_kind="model_session",
        policy_identity=b.policy_identity, content_policy=b.content_policy,
        session_id=b.session_id, db_path=b.db_path, db_identity=b.db_identity)
    ctx.session_binding = tuple(b.session_binding)
    ctx.registry_policy = b.registry_policy
    ctx.admitted_role = b.admitted_role
    ctx.control_store = b.control_store
    ctx.verification_context = b.verification_context
    row = control.execute("SELECT workspace_switch_revision FROM browser_sessions"
                          " WHERE session_hash=?", (b.session_id,)).fetchone()
    if row is None:
        raise _access.AccessLost("session unavailable")
    ctx.session_switch_revision = row[0]
    # The same checks every later call makes: live, same account, same
    # workspace, still admitted.
    _services._model_session_origin(control, ctx)
    return ctx


# -- Query rules -------------------------------------------------------------

def _query_words(query: str) -> list:
    from earwitness.app import customer
    terms, phrases = customer.tokenize_query(query)
    words = []
    for piece in terms + [w for phrase in phrases for w in phrase.split()]:
        folded = customer.normalize_token(piece).replace("’", "'")
        words.append(_EDGE.sub("", folded))
    return words


def stop_words_only(query) -> bool:
    """True when every word of the query is a stop word, one letter, or
    punctuation."""
    if not isinstance(query, str):
        return False
    words = _query_words(query)
    return bool(words) and all(len(w) <= 1 or w in STOP_WORDS for w in words)


def filter_too_broad(args) -> bool:
    """True when a search_evidence request has no usable filter.query (blank,
    or stop words only, as search_transcripts) and no non-blank filter.brand.
    A filter that is not an object is left to the strict parser."""
    if not isinstance(args, dict):
        return False
    filt = args.get("filter")
    if filt is None:
        filt = {}
    if not isinstance(filt, dict):
        return False
    query, brand = filt.get("query"), filt.get("brand")
    if any(v is not None and not isinstance(v, str) for v in (query, brand)):
        return False   # a wrong type is the parser's INVALID_ARGUMENT
    has_query = isinstance(query, str) and not stop_words_only(query) \
        and sum(ch.isalnum() for ch in query) >= EVIDENCE_QUERY_MIN
    has_brand = isinstance(brand, str) and bool(brand.strip())
    return not (has_query or has_brand)


def _shown_matches(page: dict) -> dict:
    """search_evidence without the items matched only across fields or in
    attrs, key names included (match_basis cross_field; Ask's model has no
    notes)."""
    items = page.get("items")
    if not isinstance(items, list):
        return page
    kept = [i for i in items if not (isinstance(i, dict) and i.get("match_basis") == "cross_field")]
    if len(kept) == len(items):
        return page
    return dict(page, items=kept, returned_count=len(kept),
                citations=[i["citation"] for i in kept if isinstance(i, dict) and i.get("citation")],
                attrs_only_dropped=len(items) - len(kept))


def _coverage_complete(page: dict) -> bool:
    cov = page.get("coverage") or {}
    searched = cov.get("transcripts_searched")
    return page.get("total_relation") == "exact" and isinstance(searched, int) \
        and searched == cov.get("transcripts_indexed")


# -- Dispatch ----------------------------------------------------------------

def _error(code: str, message: str | None = None, details: dict | None = None) -> dict:
    error = {"code": code}
    if message is not None:
        error["message"] = message
    if details:
        error["details"] = details
    return {"error": error}


def _fence_refusal(store, data_dir, ctx, name: str) -> dict | None:
    """None when the fence passes. An authority refusal is FORBIDDEN;
    any other failure (a busy database, a bug) is INTERNAL. Either way
    the caller discards what it holds."""
    from earwitness.app import connections as _conn
    try:
        _services.fenced_authority_check(store, data_dir, ctx, operation=name)
    except (_access.AuthorizationError, _conn.GrantError):
        return _error(FORBIDDEN)
    except Exception:
        return _error(_contracts.INTERNAL, f"internal error (request {ctx.request_id})")
    return None


def _mentions_only(entry, ctx, args: dict, budget, is_mention):
    """search_transcripts pages, keeping hits the mention rule accepts.

    Reads at most OUTSIDE_ADS_PAGE * OUTSIDE_ADS_PAGES matching segments:
    the native tool cuts a page at its 16 KiB aim, so "10 pages of 50" is
    counted in segments read, not in calls. A later page that runs out of
    time or sees the collection change keeps what was read, marked
    incomplete. Returns (result, first page)."""
    want = _contracts._limit(args.get("limit"), "search_transcripts")
    base = {k: v for k, v in args.items() if k not in ("limit", "cursor")}
    cap = OUTSIDE_ADS_PAGE * OUTSIDE_ADS_PAGES
    kept, wire, scanned, pages = [], 0, 0, 0
    first = last = cursor = None
    reason = None
    scan_complete = True
    while scanned < cap:
        request = dict(base, limit=min(OUTSIDE_ADS_PAGE, cap - scanned))
        if cursor:
            request["cursor"] = cursor
        try:
            page = entry(ctx, request, budget=budget)
        except TimeoutError:
            if first is None:
                raise
            reason = "TIME_BUDGET"
            break
        except _contracts.AgentError as exc:
            if first is None or exc.code != _contracts.RESULT_SET_CHANGED:
                raise
            reason = "RESULT_SET_CHANGED"
            break
        pages += 1
        first = first or page
        last = page
        scan_complete = scan_complete and _coverage_complete(page)
        items = page.get("items") or []
        for hit in items:
            scanned += 1
            if is_mention(hit) is not True:
                continue
            size = len(json.dumps(hit)) * 2
            if len(kept) >= want:
                reason = "PAGE_LIMIT"
            elif kept and (wire >= _contracts.RESULT_AIM_BYTES
                           or wire + size > _contracts.RESULT_HARD_BYTES):
                reason = "BYTE_BUDGET"
            if reason:
                break
            kept.append(hit)
            wire += size
        cursor = page.get("next_cursor")
        if reason or not cursor or not items:
            break
    if not reason and cursor:
        reason = "PAGE_CAP"
    if not reason and not scan_complete:
        reason = "SCAN_CAP"  # a page searched only part of the collection
    complete = reason is None
    result = dict(last)
    result.update({
        "items": kept, "returned_count": len(kept), "total_matches": len(kept),
        "total_relation": "exact" if complete else "lower_bound",
        "next_cursor": None, "truncated": not complete, "truncation_reason": reason,
        "citations": [h["citation"] for h in kept if h.get("citation")],
        "outside_ads": {"pages": pages, "segments_scanned": scanned,
                        "segments_matched": first.get("total_matches"),
                        "mentions": len(kept), "complete": complete}})
    return result, first


def dispatch(store, data_dir, ctx, name, args, *, budget_s: float = 10.0,
             outside_ads: bool = False, is_mention=None) -> dict:
    """One model tool call. Returns the tool result, or {"error": {...}}."""
    if not isinstance(name, str) or name not in ALLOWLIST:
        return _error(TOOL_NOT_ALLOWED)
    brand_mode = bool(outside_ads) and name == "search_transcripts"
    if brand_mode and not callable(is_mention):
        raise TypeError("outside_ads needs the mention rule (is_mention)")
    if not isinstance(ctx, _access.AccessContext) or ctx.credential_kind != "model_session":
        return _error(FORBIDDEN)
    if args is None:
        args = {}  # as call_tool
    args = dict(args) if isinstance(args, dict) else args
    brand_mode = brand_mode and isinstance(args, dict)  # else the parser refuses
    if name == "search_transcripts" and isinstance(args, dict) \
            and stop_words_only(args.get("query")):
        return _error(STOP_WORD_QUERY, STOP_WORD_MESSAGE)
    if name == "search_evidence" and filter_too_broad(args):
        return _error(FILTER_TOO_BROAD, FILTER_TOO_BROAD_MESSAGE)
    refused = _fence_refusal(store, data_dir, ctx, name)
    if refused:
        return refused
    outcome = _run(ctx, name, args, budget_s, brand_mode, is_mention)
    # The fence after every result, errors included; a refusal discards it.
    return _fence_refusal(store, data_dir, ctx, name) or outcome


def _run(ctx, name: str, args, budget_s: float, brand_mode: bool, is_mention) -> dict:
    from earwitness.agents import ToolFailure, _fail_from
    budget = _access.ExecutionBudget(budget_s)
    entry = _contracts.AGENT_ENTRYPOINTS[name]
    try:
        _contracts.bind_workspace(ctx.workspace_id, ctx.db_path)  # cursor secret
        if brand_mode:
            payload, first = _mentions_only(entry, ctx, args, budget, is_mention)
        else:
            payload = first = entry(ctx, args, budget=budget)
            if name == "search_evidence":
                payload = _shown_matches(payload)
    except ToolFailure as failure:
        return _error(failure.code, str(failure), failure.details)
    except Exception as exc:
        failure = _fail_from(exc, ctx.request_id)
        return _error(failure.code, str(failure), failure.details)
    fits, _ = _contracts.check_result_fit(payload)
    if not fits:
        return _error(_contracts.RESULT_TOO_LARGE,
                      f"tool {name} result exceeds the 64KiB wire cap; narrow the query")
    if name == "search_transcripts" and first.get("total_matches") == 0 \
            and _coverage_complete(first) \
            and len([w for w in _query_words(args.get("query") or "") if w]) > 1:
        payload["hint"] = ONE_SEGMENT_HINT
    return payload
