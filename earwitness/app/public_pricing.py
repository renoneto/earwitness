"""The /pricing/ page's plan slots, read from app/public/pricing.json.

Contract agreed with the Purchase thread (2026-10-23). The file is tracked
in git and never served. Its keys are exactly:

    {"format": "earwitness-pricing/1", "support_email": "...",
     "audio": "pending",
     "plan_a": {"state": "off", "link": null},
     "plan_b": {"state": "off", "link": null}}

``state`` is "on" (a same-tab button to ``link``, which must start with
https://buy.stripe.com/), "off" ("Early access opens soon.") or "closed"
("Early access sales have closed."). ``link`` is null unless the state is
"on". ``audio`` is Reno's decision on short audio clips for customers:
"pending", "included" or "not_included" (AUDIO-LINE, 2026-10-23). It is
never shown as such: while it is "pending" neither card has an audio line;
once decided both cards show one identical line, "Short audio clips of
detected ads: Included" or "...: Not included". Both servers read the file
each time they serve /pricing/ and render the slots into the static page.
A missing or invalid file (a missing or unknown ``audio`` included) turns
every plan "off" with no button, no support address and no audio line, and
logs one warning; the page still serves.

The template (pricing.html) marks what depends on a plan's state:
``<!--pricing-state:P-->`` (the off/closed line, under the price),
``<!--pricing-slot:P-->`` (the buy button when on, then "Questions"),
``<!--pricing-audio:P-->`` (the audio line, or nothing while pending),
``<!--pricing-if-on:P-->...<!--/pricing-if-on:P-->`` (purchase terms,
kept only when P is on) and ``<!--pricing-unless-on:P-->...
<!--/pricing-unless-on:P-->`` (kept only when P is not on). While the
template still has a placeholder, or ``audio`` is still "pending", and
some plan in the file is not "off", every plan renders "off" and one
warning is logged (the normal all-off pre-launch state logs nothing).
Terms and Privacy do not yet cover buyers (LEGAL-FIX1): each carries the
hidden marker BUYER_TERMS, ``<!-- buyer-terms: pending -->``, until Reno
approves buyer terms. It is matched by a regex (_BUYER_TERMS_RE) that allows
any whitespace inside the comment and any letter case, so a hand edit of the
comment still counts (LEGAL-FIX2, LEGAL-FIX3). While either page carries it,
or cannot be read, the same rule turns every plan "off".

Go-live check: ``python -m earwitness.app.public_pricing --check-live
<app_dir>`` exits 1 while the page would show a placeholder or an
example.com address, pricing.json is invalid (a missing or invalid
``audio`` is named as such), the audio decision is still pending, or
Access, Privacy or Terms still carry the local-build wording (LOCAL_BUILD)
or any NOT_LIVE text (the [PILOT END] placeholder), or Terms or Privacy
still match the buyer-terms marker regex ("terms.html: buyer terms pending");
0 when it is ready.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

from earwitness.app import oplog as _oplog

FORMAT = "earwitness-pricing/1"
PLANS = ("plan_a", "plan_b")
STATES = ("on", "off", "closed")
AUDIO = ("pending", "included", "not_included")
AUDIO_TEXT = {"included": "Included", "not_included": "Not included"}
AUDIO_LABEL = "Short audio clips of detected ads"
AUDIO_PENDING = "audio decision pending"
LINK_PREFIX = "https://buy.stripe.com/"
MAX_BYTES = 16 * 1024
# A plain address only: it is shown and used in a mailto link.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# After the prefix: URL characters only (no quotes, spaces, angle brackets
# or backslashes), at least one.
_LINK_TAIL = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+")
STATE_TEXT = {"off": "Early access opens soon.",
              "closed": "Early access sales have closed."}
BUY_TEXT = "Buy early access"
# The accessible names of the buy links: each names its plan (DPM-R1-09).
PLAN_NAMES = {"plan_a": "Plan A, Ad intelligence", "plan_b": "Plan B, Full catalog"}
PLACEHOLDER = 'class="placeholder"'
# What must never reach a published page (the go-live check).
NOT_LIVE = ("[PRICE]", "[PILOT END]", PLACEHOLDER, "example.com")
# Wording rows A2, G1 and G2 (design-pass-wording.md) mark this local-build
# text "BLOCKS publishing" until Reno approves public text. Exact phrases
# from the current pages, not broad words.
LOCAL_BUILD = {
    "access.html": ("local development build",
                    "Public accounts and invitations are not activated", "on this machine"),
    "privacy.html": ("local development build", "Local-build data"),
    "terms.html": ("local development build", "not activated by this local build",
                   "Local development and research use", "on this machine",
                   "not approved public legal text"),
}
# Hidden marker on the pages that must cover buyers before any plan goes on.
# Remove it from each page only when Reno approves its buyer text.
BUYER_TERMS = "<!-- buyer-terms: pending -->"
# Any whitespace inside the comment (LEGAL-FIX2) and any letter case
# (LEGAL-FIX3) still counts, so a hand edit of the comment cannot silently
# open the gate.
_BUYER_TERMS_RE = re.compile(r"<!--\s*buyer-terms\s*:\s*pending\s*-->", re.IGNORECASE)
BUYER_TERMS_PAGES = ("terms.html", "privacy.html")
BUYER_TERMS_PENDING = "buyer terms pending"
_BLOCK = re.compile(r"<!--pricing-(if-on|unless-on):(plan_[ab])-->\n?(.*?)<!--/pricing-\1:\2-->\n?",
                    re.DOTALL)


def _off() -> dict:
    return {"support_email": None, "audio": "pending",
            "plan_a": {"state": "off", "link": None},
            "plan_b": {"state": "off", "link": None}}


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _check(data) -> str | None:
    """The first problem with a parsed file, or None when it is valid."""
    if not isinstance(data, dict):
        return "not a JSON object"
    # Named on its own so a hand edit that drops or garbles the decision
    # never passes the go-live check as a generic problem (Purchase).
    if "audio" not in data:
        return "audio key missing"
    if not isinstance(data["audio"], str) or data["audio"] not in AUDIO:
        return f"audio value invalid (must be one of {', '.join(AUDIO)})"
    keys = set(data)
    wanted = {"format", "support_email", "audio", *PLANS}
    if keys != wanted:
        extra, missing = sorted(keys - wanted), sorted(wanted - keys)
        return f"keys differ (unknown {extra}, missing {missing})"
    if data["format"] != FORMAT:
        return "unknown format"
    email = data["support_email"]
    if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email):
        return "support_email is not a plain email address"
    for plan in PLANS:
        slot = data[plan]
        if not isinstance(slot, dict) or set(slot) != {"state", "link"}:
            return f"{plan} keys must be exactly state and link"
        state, link = slot["state"], slot["link"]
        if not isinstance(state, str) or state not in STATES:
            return f"{plan} state must be one of {', '.join(STATES)}"
        if state == "on":
            if not isinstance(link, str) or not link.startswith(LINK_PREFIX) \
                    or not _LINK_TAIL.fullmatch(link[len(LINK_PREFIX):]):
                return f"{plan} link must be a {LINK_PREFIX} URL when on"
        elif link is not None:
            return f"{plan} link must be null unless the state is on"
    return None


def load(path) -> tuple[dict, str | None]:
    """(config, problem). A valid file gives its plans and address and no
    problem; anything else gives every plan off, no address, and the
    reason. Never raises for a bad or missing file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return _off(), "file is too large"
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except FileNotFoundError:
        return _off(), "file is missing"
    # RecursionError: deep nesting on Python 3.10 and 3.11 (DPC-R1-01).
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return _off(), f"file is not valid JSON ({type(exc).__name__})"
    problem = _check(data)
    if problem:
        return _off(), problem
    return ({"support_email": data["support_email"], "audio": data["audio"],
             "plan_a": {"state": data["plan_a"]["state"], "link": data["plan_a"]["link"]},
             "plan_b": {"state": data["plan_b"]["state"], "link": data["plan_b"]["link"]}},
            None)


def state_html(plan: str, cfg: dict) -> str:
    """The off/closed line under the price; nothing when the plan is on."""
    state = cfg[plan]["state"]
    return "" if state == "on" else f'<p class="plan-state">{STATE_TEXT[state]}</p>'


def slot_html(plan: str, cfg: dict) -> str:
    """The card's foot: the buy button when on, then "Questions"."""
    slot = cfg[plan]
    action = ""
    if slot["state"] == "on":
        href = html.escape(slot["link"], quote=True)
        label = html.escape(f"{BUY_TEXT} \u2014 {PLAN_NAMES[plan]}", quote=True)
        action = (f'<p class="plan-action"><a class="cta plan-buy" href="{href}"'
                  f' aria-label="{label}"'
                  f' aria-describedby="{plan.replace("_", "-")}-title">{BUY_TEXT}</a></p>')
    email = cfg.get("support_email")
    if email:
        safe = html.escape(email, quote=True)
        action += ("\n" if action else "") + (
            f'<p class="plan-questions">Questions: <a href="mailto:{safe}">{safe}</a></p>')
    return action


def audio_html(cfg: dict) -> str:
    """The audio line, identical on both cards; nothing while pending."""
    text = AUDIO_TEXT.get(cfg["audio"])
    if text is None:
        return ""
    return f'<p class="plan-audio">{html.escape(f"{AUDIO_LABEL}: {text}", quote=True)}</p>'


def _fill(text: str, cfg: dict) -> str:
    def block(match):
        on = cfg[match.group(2)]["state"] == "on"
        keep = on if match.group(1) == "if-on" else not on
        return match.group(3) if keep else ""
    text = _BLOCK.sub(block, text)
    for plan in PLANS:
        text = text.replace(f"<!--pricing-state:{plan}-->", state_html(plan, cfg))
        text = text.replace(f"<!--pricing-slot:{plan}-->", slot_html(plan, cfg))
        line = audio_html(cfg)
        text = text.replace(f"<!--pricing-audio:{plan}-->\n", line + "\n" if line else "")
        text = text.replace(f"<!--pricing-audio:{plan}-->", line)
    return text


def _placeholders_off(cfg: dict) -> dict:
    """Every plan off, the support address kept (the file itself is valid)."""
    return dict(cfg, **{plan: {"state": "off", "link": None} for plan in PLANS})


def _page_buyer_terms_pending(path) -> bool:
    """True while the page carries the marker or cannot be read."""
    try:
        return bool(_BUYER_TERMS_RE.search(Path(path).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return True


def _buyer_terms_pending(public_dir) -> bool:
    """True while Terms or Privacy carry the marker or cannot be read."""
    return any(_page_buyer_terms_pending(Path(public_dir) / name) for name in BUYER_TERMS_PAGES)


def render(template: bytes, public_dir) -> bytes:
    """The pricing page with both plans filled from public_dir/pricing.json."""
    cfg, problem = load(Path(public_dir) / "pricing.json")
    text = template.decode("utf-8")
    if problem:
        _oplog.log_event("public.pricing_invalid", level="warning",
                         problem=problem,
                         effect="every plan shown as off, no button")
    elif any(cfg[plan]["state"] != "off" for plan in PLANS):
        # DPC-R1-03: no plan goes on while a price or date is a placeholder,
        # nor while the audio decision is pending (AUDIO-LINE).
        # DPC-R2-02: all off already (the pre-launch default) logs nothing.
        reasons = []
        if any(mark in text for mark in (PLACEHOLDER, "[PRICE]", "[PILOT END]")):
            reasons.append("template has placeholders")
        if cfg["audio"] == "pending":
            reasons.append(AUDIO_PENDING)
        if _buyer_terms_pending(public_dir):
            reasons.append(BUYER_TERMS_PENDING)
        if reasons:
            cfg = _placeholders_off(cfg)
            _oplog.log_event("public.pricing_invalid", level="warning",
                             problem="; ".join(reasons),
                             effect="every plan shown as off, no button")
    return _fill(text, cfg).encode("utf-8")


def live_problems(app_dir) -> list[str]:
    """Why the public pages under app_dir are not ready to publish; empty
    when they are. Checks the pricing template (every state's text), the
    pricing page as it would be served, and the legal pages' local-build
    wording, placeholders and buyer-terms marker."""
    public = Path(app_dir) / "public"
    try:
        template = (public / "pricing.html").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"pricing.html cannot be read ({type(exc).__name__})"]
    cfg, problem = load(public / "pricing.json")
    problems = [f"pricing.json: {problem}"] if problem else []
    if not problem and cfg["audio"] == "pending":
        problems.append(f"pricing.json: {AUDIO_PENDING}")
    served = _fill(template, cfg)
    for needle in NOT_LIVE:
        if needle in template or needle in served:
            problems.append(f"the page still shows {needle}")
    for name, phrases in LOCAL_BUILD.items():
        try:
            page = (public / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{name} cannot be read ({type(exc).__name__})")
            continue
        problems.extend(f'{name} still says "{phrase}"' for phrase in phrases if phrase in page)
        # LEGAL-PAGES: the legal pages carry the pilot end as a placeholder
        # too; none may reach a published page.
        problems.extend(f"{name} still shows {needle}" for needle in NOT_LIVE if needle in page)
    # LEGAL-FIX2: checked on its own, so dropping a page from LOCAL_BUILD
    # cannot drop its buyer-terms check. An unreadable page fails here too,
    # unless the loop above already named it.
    for name in BUYER_TERMS_PAGES:
        if any(line.startswith(f"{name} cannot be read") for line in problems):
            continue
        if _page_buyer_terms_pending(public / name):
            problems.append(f"{name}: {BUYER_TERMS_PENDING}")
    return problems


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--check-live":
        print("usage: python -m earwitness.app.public_pricing --check-live <app_dir>",
              file=sys.stderr)
        return 2
    problems = live_problems(args[1])
    for line in problems:
        print(f"NOT READY: {line}")
    if not problems:
        print("ready: no placeholder, example address, pending audio decision,"
              " pending buyer terms or local-build wording")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
