"""Design pass: the public /pricing/ page and its pricing.json slots.

Contract agreed with the Purchase thread (ws-app STATE 2026-10-23):
app/public/pricing.json is tracked and never served. The server reads it
when it serves /pricing/ and renders each plan's slot into the static
page. "on" shows a same-tab button to a https://buy.stripe.com/ link;
"off" says "Early access opens soon."; "closed" says "Early access sales
have closed.". Every card shows "Questions: <support_email>" as a mailto
link. A missing or invalid file renders every card "off" with no button,
logs a warning and still answers 200. Both adapters (hosted ASGI and the
local server) behave the same. While the template still has a
placeholder, every plan renders "off". The page wording is pending Reno,
design-pass-wording.md.

AUDIO-LINE (owner decision, 2026-10-23): pricing.json carries a required,
never-served "audio" key: "pending", "included" or "not_included". While it
is "pending" the audio line is hidden on both cards, every plan renders
"off" (logged only when the file asks for a plan that is not "off") and
--check-live fails with "pricing.json: audio decision pending". Decided, both
cards show one identical line. A missing or invalid value makes the file
invalid (every card off) and --check-live names it.

LEGAL-FIX1 (code review note 1): Terms and Privacy do not yet cover buyers.
Each carries a hidden marker, BUYER_TERMS, until Reno approves buyer terms.
While either page carries it (or cannot be read), every plan renders "off"
under the placeholder rule (logged only when a plan is not "off"), and
--check-live fails with "<page>: buyer terms pending".

LEGAL-FIX2 (code review notes 1-2): the marker matches with any whitespace
inside the comment, in both the render gate and --check-live, and
--check-live checks it on Terms and Privacy on its own, not through the
LOCAL_BUILD list.

LEGAL-FIX3 (Reno, lead ledger LEDGER-LEAD2-RENO-PRICING-1): the committed
page shows the planned prices, Plan A "$99 a month" and Plan B "$199 a
month"; both plans stay "off" and audio is "not_included". The only
placeholder left is [PILOT END], inside the on-only purchase terms, so it
never renders while every plan is off. Gate tests use a test template with
a [PRICE] placeholder so they keep testing the gate once the date is set.
The buyer-terms marker matches in any letter case (code review r3 note A).

PILOT-END (lead, 2026-10-23; Reno "who cares"): the pilot end is set to
PILOT_END_TEXT on Pricing, Terms and Privacy. No placeholder is left in the
committed pages; --check-live reports only the two buyer-terms lines. Gate
tests restore a [PILOT END] placeholder in a test template.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import asgi as _asgi
from earwitness.app import public_pricing

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import app_server as srv  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PUBLIC = REPO / "app" / "public"
LINK_A = "https://buy.stripe.com/test_aaaa1111"
LINK_B = "https://buy.stripe.com/test_bbbb2222"
EMAIL = "help@example.com"
# The public form of the deployed pilot_end, 2026-12-18T23:59:00Z (host
# layout and rights basis manifest). The app has no constant for it: the
# hosted server reads EARWITNESS_PILOT_END, and the pages are static files.
PILOT_END_TEXT = "December 18, 2026"
PILOT_END_SPAN = '<span class="placeholder">[PILOT END]</span>'
# Wording rows A2, G1 and G2 (design-pass-wording.md): local-build text that
# blocks publishing until Reno approves public text. Exact phrases only.
LOCAL_BUILD = {
    "access.html": ("local development build",
                    "Public accounts and invitations are not activated", "on this machine"),
    "privacy.html": ("local development build", "Local-build data"),
    "terms.html": ("local development build", "not activated by this local build",
                   "Local development and research use", "on this machine",
                   "not approved public legal text"),
}


def encoded(data) -> bytes:
    """A pricing.json body: raw bytes or text as given, anything else as JSON."""
    if isinstance(data, bytes):
        return data
    return (data if isinstance(data, str) else json.dumps(data)).encode("utf-8")


def fill(template: str) -> str:
    return re.sub(r'<span class="placeholder">[^<]*</span>', "29 USD", template)


PRICE_LINE = re.compile(r'<p class="plan-price">.*?</p>')
PRICE_PLACEHOLDER = '<p class="plan-price"><span class="placeholder">[PRICE]</span> a month</p>'


def with_price_placeholder(template: str) -> str:
    """A test template: every price line back to the [PRICE] placeholder
    (the gate under test, whatever the committed prices are)."""
    out, n = PRICE_LINE.subn(PRICE_PLACEHOLDER, template)
    assert n == 2, n
    return out


def with_pilot_end_placeholder(template: str) -> str:
    """A test template: the pilot end back to the [PILOT END] placeholder
    (the gate under test, now that the committed date is set)."""
    assert template.count(PILOT_END_TEXT) == 2, template.count(PILOT_END_TEXT)
    return template.replace(PILOT_END_TEXT, PILOT_END_SPAN)


BUYER_TERMS = "<!-- buyer-terms: pending -->"
BUYER_PAGES = ("terms.html", "privacy.html")
# Hand-edited forms of the marker that must still count (LEGAL-FIX2).
BUYER_TERMS_VARIANTS = ("<!--buyer-terms: pending-->",
                        "<!--  buyer-terms :  pending  -->",
                        "<!--\nbuyer-terms:\tpending\n-->",
                        # LEGAL-FIX3 (code review r3 note A): any letter case.
                        "<!-- Buyer-Terms: PENDING -->")


def without_buyer_marker(public) -> None:
    """Terms and Privacy as they will be once Reno approves buyer terms."""
    for name in BUYER_PAGES:
        path = Path(public, name)
        if path.exists():
            path.write_text(path.read_text(encoding="utf-8").replace(BUYER_TERMS + "\n", "")
                            .replace(BUYER_TERMS, ""), encoding="utf-8")


AUDIO_LINE = '<p class="plan-audio">Short audio clips of detected ads: {}</p>'
AUDIO_TEXT = {"included": "Included", "not_included": "Not included"}


def config(a="off", b="off", link_a=None, link_b=None, email=EMAIL, audio="included", **extra):
    data = {"format": "earwitness-pricing/1", "support_email": email, "audio": audio,
            "plan_a": {"state": a, "link": link_a},
            "plan_b": {"state": b, "link": link_b}}
    data.update(extra)
    return data


class PricingAdapters(unittest.TestCase):
    """Calls both adapters against a temporary copy of app/public."""

    PORT = 8781
    HOST = f"127.0.0.1:{PORT}"

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="ew-pricing-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.app_dir = os.path.join(tmp, "app")
        shutil.copytree(REPO / "app", self.app_dir,
                        ignore=shutil.ignore_patterns("operator"))
        self.public = Path(self.app_dir, "public")
        self.hosted = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=tmp, grants_db=os.path.join(tmp, "grants.sqlite3"),
            app_dir=self.app_dir))
        prev_port = srv._state["port"]
        srv._state["port"] = self.PORT
        self.addCleanup(srv._state.__setitem__, "port", prev_port)
        patcher = mock.patch.object(srv, "PUBLIC_DIR", self.public)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data):
        Path(self.public, "pricing.json").write_bytes(encoded(data))

    def fill_template(self):
        """The copied template with its placeholders replaced, and the
        buyer-terms marker gone from Terms and Privacy, as they will be at
        go-live. While any placeholder or marker remains every plan is off."""
        path = Path(self.public, "pricing.html")
        path.write_text(fill(path.read_text(encoding="utf-8")), encoding="utf-8")
        without_buyer_marker(self.public)

    def price_placeholder_template(self):
        """The copied template with its prices back to [PRICE] placeholders."""
        path = Path(self.public, "pricing.html")
        path.write_text(with_price_placeholder(path.read_text(encoding="utf-8")),
                        encoding="utf-8")

    def mark_buyer_terms(self, name, marker=BUYER_TERMS):
        path = Path(self.public, name)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "<body>", "<body>\n" + marker, 1), encoding="utf-8")

    def local_call(self, path):
        handler = srv.Handler.__new__(srv.Handler)
        handler.command = "GET"
        handler.path = path
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": self.HOST, "Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        data = handler.wfile.getvalue()
        head, _, rest = data.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        hdrs = {}
        for line in head.split(b"\r\n")[1:]:
            key, _, value = line.partition(b":")
            hdrs[key.strip().lower().decode()] = value.strip().decode()
        return status, hdrs, rest

    def hosted_call(self, path):
        raw_path, _, query = path.partition("?")
        scope = {"type": "http", "method": "GET", "path": raw_path,
                 "scheme": "http", "headers": [(b"host", b"127.0.0.1:8771")],
                 "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.hosted(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent
                        if m["type"] == "http.response.body")
        hdrs = {(k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
                for k, v in start.get("headers", [])}
        return start["status"], hdrs, body

    def calls(self):
        return (("local", self.local_call), ("hosted", self.hosted_call))

    def page(self, call):
        status, hdrs, raw = call("/pricing/")
        self.assertEqual(status, 200, raw[:300])
        self.assertIn("text/html", hdrs.get("content-type", ""))
        self.assertIn("default-src 'self'", hdrs.get("content-security-policy", ""))
        return raw.decode()

    @staticmethod
    def card(html, plan):
        start = html.index(f'id="plan-{plan}"')
        end = html.index("</article>", start)
        return html[start:end]


class PricingRouteTests(PricingAdapters):

    def test_default_file_renders_both_plans_off(self):
        # The committed file itself, both plans "off".
        shutil.copy(PUBLIC / "pricing.json", self.public / "pricing.json")
        for name, call in self.calls():
            html = self.page(call)
            for plan in ("a", "b"):
                card = self.card(html, plan)
                self.assertIn("Early access opens soon.", card, name)
                self.assertNotIn("buy.stripe.com", card, name)
                self.assertNotIn('class="cta', card, name)

    # PUB-48 rows 25-26: Plan A card first, marked "If ready in time".
    def test_plan_a_first_then_plan_b_plan_a_marked_if_ready_in_time(self):
        self.write(config())
        for name, call in self.calls():
            html = self.page(call)
            self.assertLess(html.index('id="plan-a"'), html.index('id="plan-b"'), name)
            self.assertIn("If ready in time", self.card(html, "a"), name)
            self.assertNotIn("If ready in time", self.card(html, "b"), name)

    def test_on_cards_carry_the_purchase_design_sentences(self):
        self.fill_template()
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B))
        for name, call in self.calls():
            html = self.page(call)
            for plan in ("a", "b"):
                card = self.card(html, plan)
                for needle in (
                        "Early access ends on",
                        "Your last month is charged only up to that day. No charge after it.",
                        "All data is deleted within 60 days after it.",
                        "Your workspace is ready the morning after payment. We set it up"
                        " in our nightly maintenance window (01:30 Pacific).",
                        "One purchase per person.",
                        'href="/terms/"', 'href="/privacy/"',
                        'class="plan-price"'):
                    self.assertIn(needle, card, f"{name} plan {plan}: {needle}")

    def test_on_shows_a_same_tab_button_to_the_link(self):
        self.fill_template()
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B))
        for name, call in self.calls():
            html = self.page(call)
            for plan, link in (("a", LINK_A), ("b", LINK_B)):
                card = self.card(html, plan)
                self.assertIn(f'<a class="cta plan-buy" href="{link}"', card, name)
                self.assertNotIn('class="plan-state"', card, name)
                self.assertNotIn("target=", card, name)
                self.assertNotIn("Early access opens soon.", card, name)
                self.assertNotIn("Early access sales have closed.", card, name)

    def test_off_and_closed_show_text_and_no_button(self):
        self.fill_template()  # placeholders force every plan off, closed included
        self.write(config(a="off", b="closed"))
        for name, call in self.calls():
            html = self.page(call)
            a, b = self.card(html, "a"), self.card(html, "b")
            self.assertIn("Early access opens soon.", a, name)
            self.assertIn("Early access sales have closed.", b, name)
            self.assertNotIn("Early access opens soon.", b, name)
            for card in (a, b):
                self.assertNotIn("plan-buy", card, name)
                self.assertNotIn("buy.stripe.com", card, name)

    def test_mixed_states_render_per_plan(self):
        self.fill_template()
        self.write(config(a="closed", b="on", link_b=LINK_B))
        for name, call in self.calls():
            html = self.page(call)
            self.assertIn(LINK_B, self.card(html, "b"), name)
            self.assertIn("Early access sales have closed.", self.card(html, "a"), name)
            self.assertNotIn(LINK_B, self.card(html, "a"), name)

    def test_every_card_in_every_state_shows_questions_mailto(self):
        self.fill_template()
        for state in ("on", "off", "closed"):
            link = LINK_A if state == "on" else None
            self.write(config(a=state, b=state, link_a=link,
                              link_b=LINK_B if state == "on" else None))
            for name, call in self.calls():
                html = self.page(call)
                for plan in ("a", "b"):
                    self.assertIn(
                        f'Questions: <a href="mailto:{EMAIL}">{EMAIL}</a>',
                        self.card(html, plan), f"{name} {state} {plan}")

    def test_page_stays_static(self):
        self.fill_template()
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B))
        for name, call in self.calls():
            html = self.page(call)
            self.assertEqual(html.count("<script"), 1, name)
            self.assertIn('<script src="/public/public.js" defer></script>', html)
            self.assertNotIn("js.stripe.com", html)
            self.assertNotIn("<!--pricing", html, "every slot and block is rendered")
            self.assertNotIn("<!--/pricing", html, "every block marker is removed")
            self.assertNotIn("style=", html)


    # Design fix pass 1 (DPM-R1-03): in off and closed the state sits right
    # under the price, and nothing on the card describes a purchase.
    def test_off_and_closed_state_under_the_price_and_no_purchase_copy(self):
        self.fill_template()
        self.write(config(a="off", b="closed"))
        for name, call in self.calls():
            html = self.page(call)
            for plan, text in (("a", "Early access opens soon."),
                               ("b", "Early access sales have closed.")):
                card = self.card(html, plan)
                price = card.index('class="plan-price"')
                state = card.index(f'<p class="plan-state">{text}</p>')
                self.assertLess(price, state, f"{name} {plan}")
                self.assertLess(state, card.index("<h3>"), f"{name} {plan}: before the plan details")
                for gone in ("Before you buy", "after payment", "Early access ends on",
                             "Your last month is charged", "One purchase per person",
                             'href="/terms/"', "plan-terms"):
                    self.assertNotIn(gone, card, f"{name} {plan}: {gone}")
                self.assertIn(f'Questions: <a href="mailto:{EMAIL}">', card, f"{name} {plan}")

    # DPM-R1-04: the Plan A condition shows only while Plan A is not on.
    def test_plan_a_condition_shows_only_while_plan_a_is_not_on(self):
        self.fill_template()
        lede = "Plan A goes on sale only if it is ready before the pilot ends."
        for a, b, shown in (("off", "on", True), ("closed", "off", True),
                            ("on", "off", False), ("on", "on", False)):
            self.write(config(a=a, b=b, link_a=LINK_A if a == "on" else None,
                              link_b=LINK_B if b == "on" else None))
            for name, call in self.calls():
                html = self.page(call)
                card_a = self.card(html, "a")
                check = self.assertIn if shown else self.assertNotIn
                check("If ready in time", card_a, f"{name} a={a}")
                check(lede, html, f"{name} a={a}")
                self.assertNotIn("If ready in time", self.card(html, "b"), name)

    # DPM-R1-09: each buy link's accessible name names its plan.
    def test_buy_links_have_distinct_names_naming_the_plan(self):
        self.fill_template()
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B))
        for name, call in self.calls():
            html = self.page(call)
            names = re.findall(r'<a class="cta plan-buy"[^>]*aria-label="([^"]+)"', html)
            self.assertEqual(names, ["Buy early access \u2014 Plan A, Ad intelligence",
                                     "Buy early access \u2014 Plan B, Full catalog"], name)
            self.assertEqual(html.count(">Buy early access</a>"), 2, "visible text unchanged")

    # DPM-R1-10: the transcript difference once, at the top of each card.
    def test_transcript_difference_stated_once_at_the_top_of_each_card(self):
        self.write(config())
        for name, call in self.calls():
            html = self.page(call)
            for plan, key in (("b", "Ads, plus full transcripts: search any word or phrase."),
                              ("a", "No full transcripts and no transcript search.")):
                card = self.card(html, plan)
                self.assertEqual(card.count(key), 1, f"{name} {plan}")
                at = card.index(f'<p class="plan-key">{key}</p>')
                self.assertLess(card.index("</h2>"), at, f"{name} {plan}")
                self.assertLess(at, card.index('class="plan-price"'), f"{name} {plan}")

    # DPC-R1-03: a plan never goes on while the template has placeholders.
    def test_placeholders_in_the_template_force_every_plan_off(self):
        self.price_placeholder_template()
        self.write(config(a="on", link_a=LINK_A, b="closed"))
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("template has placeholders" in line for line in logs.output),
                            f"{name}: {logs.output}")
            for plan in ("a", "b"):
                card = self.card(html, plan)
                self.assertIn("Early access opens soon.", card, f"{name} {plan}")
                self.assertNotIn("plan-buy", card, f"{name} {plan}")
                self.assertNotIn("buy.stripe.com", card, f"{name} {plan}")
                self.assertIn(f'Questions: <a href="mailto:{EMAIL}">', card, f"{name} {plan}")

    # AUD-R1-01 (was DPC-R2-01): a bare placeholder span, with no [PRICE] or
    # [PILOT END] text and audio decided, still keeps every plan off.
    def test_bare_placeholder_span_keeps_every_plan_off(self):
        path = Path(self.public, "pricing.html")
        text = fill(path.read_text(encoding="utf-8"))
        self.assertNotIn("[PRICE]", text)
        self.assertNotIn("[PILOT END]", text)
        text = text.replace("</main>", '<span class="placeholder">x</span></main>', 1)
        self.assertEqual(text.count('class="placeholder"'), 1)
        path.write_text(text, encoding="utf-8")
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B, audio="included"))
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("template has placeholders" in line for line in logs.output),
                            f"{name}: {logs.output}")
            self.assertNotIn("plan-buy", html, name)
            self.assertNotIn("buy.stripe.com", html, name)

    # DPC-R2-02: the normal pre-launch state (every plan off) logs nothing.
    def test_placeholders_with_every_plan_off_log_nothing(self):
        # The committed file: every plan off.
        self.price_placeholder_template()
        shutil.copy(PUBLIC / "pricing.json", self.public / "pricing.json")
        for name, call in self.calls():
            with self.assertNoLogs("earwitness.ops", level="WARNING"):
                html = self.page(call)
            for plan in ("a", "b"):
                self.assertIn("Early access opens soon.", self.card(html, plan), f"{name} {plan}")

    # DPC-R2-02: any plan not "off" (on or closed) is forced off and logged.
    def test_placeholders_with_a_plan_not_off_force_off_and_log(self):
        self.price_placeholder_template()
        for label, data in (("a on", config(a="on", link_a=LINK_A)),
                            ("b closed", config(b="closed"))):
            with self.subTest(label):
                self.write(data)
                for name, call in self.calls():
                    with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                        html = self.page(call)
                    self.assertTrue(any("template has placeholders" in line for line in logs.output),
                                    f"{label} {name}: {logs.output}")
                    for plan in ("a", "b"):
                        card = self.card(html, plan)
                        self.assertIn("Early access opens soon.", card, f"{label} {name} {plan}")
                        self.assertNotIn("plan-buy", card, f"{label} {name} {plan}")

    # LEGAL-FIX3: a [PILOT END] placeholder alone (inside the on-only terms)
    # still keeps every plan off while any plan asks on. PILOT-END: the
    # committed date is set, so a test template restores the placeholder.
    def test_a_pilot_end_placeholder_keeps_every_plan_off(self):
        path = Path(self.public, "pricing.html")
        path.write_text(with_pilot_end_placeholder(path.read_text(encoding="utf-8")),
                        encoding="utf-8")
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B, audio="included"))
        without_buyer_marker(self.public)
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("template has placeholders" in line for line in logs.output),
                            f"{name}: {logs.output}")
            self.assertNotIn("plan-buy", html, name)

    # LEGAL-FIX3 (Reno, LEDGER-LEAD2-RENO-PRICING-1): the planned prices show,
    # buying stays off, and no placeholder is visible on the served page.
    def test_committed_page_shows_the_planned_prices_with_buying_off(self):
        shutil.copy(PUBLIC / "pricing.json", self.public / "pricing.json")
        for name, call in self.calls():
            with self.assertNoLogs("earwitness.ops", level="WARNING"):
                html = self.page(call)
            for plan, price in (("a", "$99 a month"), ("b", "$199 a month")):
                card = self.card(html, plan)
                self.assertEqual(card.count(f'<p class="plan-price">{price}</p>'), 1,
                                 f"{name} {plan}")
                self.assertIn('<p class="plan-state">Early access opens soon.</p>', card,
                              f"{name} {plan}")
                self.assertNotIn("plan-buy", card, f"{name} {plan}")
            self.assertNotIn("[PRICE]", html, name)
            self.assertNotIn("[PILOT END]", html, name)
            self.assertNotIn('class="placeholder"', html, f"{name}: nothing visible")

    # DPC-R1-02: an "&" in the link is escaped in the rendered href.
    def test_on_link_with_ampersand_renders_escaped(self):
        self.fill_template()
        self.write(config(b="on", link_b="https://buy.stripe.com/x?a=1&b=2"))
        for name, call in self.calls():
            card = self.card(self.page(call), "b")
            self.assertIn('href="https://buy.stripe.com/x?a=1&amp;b=2"', card, name)
            self.assertNotIn("a=1&b=2", card, name)


class BuyerTermsGateTests(PricingAdapters):
    """LEGAL-FIX1 (code review note 1): no plan goes on while Terms or
    Privacy still carry the buyer-terms marker (buyer terms pending)."""

    def test_committed_terms_and_privacy_carry_the_marker(self):
        for name in BUYER_PAGES:
            self.assertEqual((PUBLIC / name).read_text(encoding="utf-8").count(BUYER_TERMS), 1, name)
        self.assertNotIn("buyer-terms", (PUBLIC / "access.html").read_text(encoding="utf-8"))

    def test_marker_on_terms_or_privacy_forces_every_plan_off_and_logs(self):
        for page in BUYER_PAGES:
            with self.subTest(page):
                self.fill_template()
                self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B, audio="included"))
                for name, call in self.calls():
                    with self.assertNoLogs("earwitness.ops", level="WARNING"):
                        self.assertIn("plan-buy", self.page(call), f"{page} {name} baseline")
                self.mark_buyer_terms(page)
                for name, call in self.calls():
                    with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                        html = self.page(call)
                    self.assertTrue(any("buyer terms pending" in line for line in logs.output),
                                    f"{page} {name}: {logs.output}")
                    self.assertNotIn("plan-buy", html, f"{page} {name}")
                    self.assertNotIn("buy.stripe.com", html, f"{page} {name}")
                    for plan in ("a", "b"):
                        card = self.card(html, plan)
                        self.assertIn("Early access opens soon.", card, f"{page} {name} {plan}")
                        self.assertIn(f'Questions: <a href="mailto:{EMAIL}">', card,
                                      f"{page} {name} {plan}")

    def test_a_hand_edited_marker_still_forces_every_plan_off(self):
        """LEGAL-FIX2 (code review note 1): whitespace inside the comment
        does not hide the marker from the render gate."""
        for page in BUYER_PAGES:
            for marker in BUYER_TERMS_VARIANTS:
                with self.subTest(page=page, marker=marker):
                    self.fill_template()
                    self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B,
                                      audio="included"))
                    self.mark_buyer_terms(page, marker)
                    for name, call in self.calls():
                        with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                            html = self.page(call)
                        self.assertTrue(any("buyer terms pending" in line for line in logs.output),
                                        f"{page} {name}: {logs.output}")
                        self.assertNotIn("plan-buy", html, f"{page} {name}")
                    path = Path(self.public, page)
                    path.write_text(path.read_text(encoding="utf-8").replace(marker + "\n", "", 1),
                                    encoding="utf-8")

    def test_marker_with_a_plan_closed_forces_off_and_logs(self):
        self.fill_template()
        self.mark_buyer_terms("terms.html")
        self.write(config(b="closed", audio="included"))
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("buyer terms pending" in line for line in logs.output),
                            f"{name}: {logs.output}")
            self.assertNotIn("Early access sales have closed.", html, name)

    def test_marker_with_every_plan_off_logs_nothing(self):
        self.fill_template()
        self.mark_buyer_terms("privacy.html")
        self.write(config(audio="included"))
        for name, call in self.calls():
            with self.assertNoLogs("earwitness.ops", level="WARNING"):
                self.page(call)

    def test_a_missing_terms_or_privacy_page_keeps_every_plan_off(self):
        for page in BUYER_PAGES:
            with self.subTest(page):
                self.fill_template()
                self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B, audio="included"))
                path = Path(self.public, page)
                kept = path.read_bytes()
                path.unlink()
                for name, call in self.calls():
                    with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                        html = self.page(call)
                    self.assertTrue(any("buyer terms pending" in line for line in logs.output),
                                    f"{page} {name}: {logs.output}")
                    self.assertNotIn("plan-buy", html, f"{page} {name}")
                path.write_bytes(kept)


class PricingFailClosedTests(PricingAdapters):

    BAD = {
        "missing format": {k: v for k, v in config().items() if k != "format"},
        "wrong format": config(format="earwitness-pricing/2"),
        "unknown top key": config(extra=1),
        "unknown plan key": dict(config(), plan_a={"state": "off", "link": None, "price": 9}),
        "missing plan": {k: v for k, v in config().items() if k != "plan_b"},
        "bad state": config(a="open"),
        "state not a string": config(a=True),
        "on without link": config(a="on"),
        "on with http link": config(a="on", link_a="http://buy.stripe.com/x"),
        "on with other host": config(a="on", link_a="https://buy.stripe.com.evil.test/x"),
        "on with bare prefix": config(a="on", link_a="https://buy.stripe.com/"),
        "on with quote in link": config(a="on", link_a='https://buy.stripe.com/x" onclick="y'),
        "on with javascript link": config(a="on", link_a="javascript:alert(1)"),
        "on with prefix after another host": config(
            a="on", link_a="https://evil.test/https://buy.stripe.com/x"),
        "on with prefix inside javascript": config(a="on", link_a="javascript:x//buy.stripe.com/x"),
        "off with link": config(a="off", link_a=LINK_A),
        "closed with link": config(b="closed", link_b=LINK_B),
        "bad email": config(email="not an email"),
        "email with markup": config(email='a"<b>@example.com'),
        "not an object": [],
        "audio missing": {k: v for k, v in config().items() if k != "audio"},
        "audio unknown": config(audio="maybe"),
        "audio not a string": config(audio=True),
        "audio null": config(audio=None),
        "audio a list": config(audio=["pending"]),
        # Valid content, only too large: the size cap alone refuses it.
        "over 16 KB": json.dumps(config(a="on", link_a=LINK_A)) + " " * (16 * 1024),
        # Valid content in UTF-16: only the UTF-8 decode refuses it.
        "not UTF-8": json.dumps(config(a="on", link_a=LINK_A)).encode("utf-16"),
        "deeply nested": "[" * 1500 + "]" * 1500,
    }

    def assert_fallback(self, label):
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("pricing" in line for line in logs.output),
                            f"{label} {name}: {logs.output}")
            for plan in ("a", "b"):
                card = self.card(html, plan)
                self.assertIn("Early access opens soon.", card, f"{label} {name}")
                self.assertNotIn("plan-buy", card, f"{label} {name}")
                self.assertNotIn("buy.stripe.com", card, f"{label} {name}")
                self.assertNotIn("Early access sales have closed.", card, f"{label} {name}")
                self.assertNotIn("Short audio clips", card, f"{label} {name}: fail closed")

    def test_invalid_files_fall_back_to_off(self):
        for label, data in self.BAD.items():
            with self.subTest(label):
                self.write(data)
                self.assert_fallback(label)

    def test_one_bad_plan_turns_off_the_good_one_too(self):
        self.write(config(a="on", link_a=LINK_A, b="sold"))
        self.assert_fallback("one bad plan")

    def test_not_json_duplicate_keys_and_missing_file(self):
        for label, text in (("not json", "{nope"),
                            ("duplicate key", '{"format": "earwitness-pricing/1",'
                             ' "support_email": "help@example.com", "audio": "included",'
                             ' "plan_a": {"state": "off", "link": null},'
                             ' "plan_b": {"state": "off", "link": null},'
                             ' "plan_b": {"state": "on", "link": "https://buy.stripe.com/x"}}')):
            with self.subTest(label):
                self.write(text)
                self.assert_fallback(label)
        Path(self.public, "pricing.json").unlink()
        self.assert_fallback("missing file")

    def test_recursion_error_while_parsing_falls_back_to_off(self):
        # DPC-R1-01: Python 3.10/3.11 raise RecursionError on deep nesting.
        self.write(config(a="on", link_a=LINK_A))
        with mock.patch.object(public_pricing.json, "loads", side_effect=RecursionError):
            self.assert_fallback("recursion")

    def test_fallback_never_shows_an_untrusted_support_address(self):
        self.write(config(a="bogus", email="trap@example.com"))
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING"):
                html = self.page(call)
            self.assertNotIn("trap@example.com", html, name)


class PricingNeverServedTests(PricingAdapters):

    def test_pricing_json_is_never_served(self):
        self.write(config(email="secret-marker@example.com"))
        doc = Path(self.public, "404.html").read_bytes()
        for route in ("/pricing.json", "/public/pricing.json",
                      "/app/public/pricing.json", "/pricing/pricing.json",
                      "/app/pricing.json", "/public/pricing.html",
                      "/public/../public/pricing.json"):
            for name, call in self.calls():
                status, _hdrs, raw = call(route)
                self.assertEqual(status, 404, f"{name} {route}")
                self.assertEqual(raw, doc, f"{name} {route}")
                self.assertNotIn(b"secret-marker", raw)
                self.assertNotIn(b"earwitness-pricing", raw)

    def test_not_in_any_served_table(self):
        for table in (srv.PUBLIC_DOCS, srv.PUBLIC_ASSETS, srv.STATIC,
                      _asgi.PUBLIC_DOCS, _asgi.PUBLIC_ASSETS, _asgi.APP_STATIC):
            self.assertNotIn("pricing.json", table)
            self.assertFalse(any(k.endswith("pricing.json") for k in table))

    def test_route_and_canonical_redirect_on_both_adapters(self):
        for mod in (srv, _asgi):
            self.assertEqual(mod.PUBLIC_ROUTES.get("/pricing/"), "pricing.html")
            self.assertEqual(mod.CANONICAL_REDIRECTS.get("/pricing"), "/pricing/")
        self.write(config())
        for name, call in self.calls():
            status, hdrs, _raw = call("/pricing")
            self.assertIn(status, (301, 302), name)
            self.assertEqual(hdrs.get("location"), "/pricing/", name)
            status, hdrs, _raw = call("/pricing?utm=1")
            self.assertIn(status, (301, 302), name)
            self.assertEqual(hdrs.get("location"), "/pricing/?utm=1", name)


class CommittedPricingFileTests(unittest.TestCase):

    def test_committed_template_carries_the_planned_prices(self):
        html = (PUBLIC / "pricing.html").read_text(encoding="utf-8")
        self.assertNotIn("[PRICE]", html)
        self.assertEqual(PRICE_LINE.findall(html), ['<p class="plan-price">$99 a month</p>',
                                                   '<p class="plan-price">$199 a month</p>'],
                         "Plan A card first, then Plan B (PUB-48 row 26)")
        b = html.index('id="plan-b"')
        self.assertIn('<p class="plan-price">$199 a month</p>', html[b:])
        # PILOT-END: no placeholder is left; the pilot end is set, once per card.
        self.assertNotIn('class="placeholder"', html)
        self.assertNotIn("[PILOT END]", html)
        self.assertEqual(html.count(f"<p>Early access ends on {PILOT_END_TEXT}. "), 2)

    def test_committed_support_address_is_the_sign_in_address(self):
        # One support address on the site: the sign-in page's SUPPORT_CONTACT.
        views = (REPO / "app" / "views.js").read_text(encoding="utf-8")
        contact = re.search(r"const SUPPORT_CONTACT = '([^']+)';", views).group(1)
        data = json.loads((PUBLIC / "pricing.json").read_text(encoding="utf-8"))
        self.assertEqual(data["support_email"], contact)
        self.assertNotIn("example.com", data["support_email"])

    def test_committed_default_is_exact_and_off(self):
        data = json.loads((PUBLIC / "pricing.json").read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["format", "support_email", "audio", "plan_a", "plan_b"])
        self.assertEqual(data["format"], "earwitness-pricing/1")
        # LEGAL-FIX3: lead call with Reno's pricing decision (LEDGER-LEAD2-RENO-
        # PRICING-1): no audio clips for paying customers until paid rights settle.
        self.assertEqual(data["audio"], "not_included")
        self.assertEqual(data["plan_a"], {"state": "off", "link": None})
        self.assertEqual(data["plan_b"], {"state": "off", "link": None})
        self.assertIsInstance(data["support_email"], str)
        cfg, problem = public_pricing.load(PUBLIC / "pricing.json")
        self.assertIsNone(problem)
        self.assertEqual(cfg["support_email"], data["support_email"])


class PricingValidatorTests(unittest.TestCase):

    def load(self, data):
        tmp = tempfile.mkdtemp(prefix="ew-pricing-v-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp, "pricing.json")
        path.write_bytes(encoded(data))
        return public_pricing.load(path)

    def test_slot_html_escapes_the_address(self):
        # The validator refuses this address, so only a direct call reaches
        # the escaping (DPC-R1-02).
        cfg = {"support_email": 'a"<b>@x.co', "audio": "pending",
               "plan_a": {"state": "off", "link": None},
               "plan_b": {"state": "off", "link": None}}
        out = public_pricing.slot_html("plan_a", cfg)
        self.assertIn("a&quot;&lt;b&gt;@x.co", out)
        self.assertNotIn('a"<b>', out)

    def test_read_is_bounded(self):
        # DPC-R1-05: never more than MAX_BYTES + 1 bytes are read.
        tmp = tempfile.mkdtemp(prefix="ew-pricing-v-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp, "pricing.json")
        path.write_bytes(b" " * (public_pricing.MAX_BYTES * 4))
        seen = []
        real_open = open

        def spy(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            real_read = handle.read

            def read(n=-1):
                seen.append(n)
                return real_read(n)
            handle.read = read
            return handle
        with mock.patch("builtins.open", spy):
            _cfg, problem = public_pricing.load(path)
        self.assertEqual(problem, "file is too large")
        self.assertEqual(seen, [public_pricing.MAX_BYTES + 1])

    def test_valid_states(self):
        cfg, problem = self.load(config(a="on", link_a=LINK_A, b="closed"))
        self.assertIsNone(problem)
        self.assertEqual(cfg["plan_a"], {"state": "on", "link": LINK_A})
        self.assertEqual(cfg["plan_b"], {"state": "closed", "link": None})

    def test_invalid_returns_off_config_and_a_reason(self):
        for label, data in PricingFailClosedTests.BAD.items():
            with self.subTest(label):
                cfg, problem = self.load(data)
                self.assertIsInstance(problem, str)
                self.assertTrue(problem)
                self.assertEqual(cfg["plan_a"], {"state": "off", "link": None})
                self.assertEqual(cfg["plan_b"], {"state": "off", "link": None})
                self.assertIsNone(cfg["support_email"])


class AudioLineTests(PricingAdapters):
    """AUDIO-LINE: no internal decision state on the public pricing page."""

    INTERNAL = ("Reno", "pending", "audio decision", "Short audio clips")

    def assert_public(self, html, label):
        for needle in self.INTERNAL:
            self.assertNotIn(needle, html, f"{label}: {needle}")

    def test_committed_files_show_not_included_on_both_cards_and_name_no_one(self):
        # LEGAL-FIX3: the committed decision is "not_included".
        shutil.copy(PUBLIC / "pricing.json", self.public / "pricing.json")
        line = AUDIO_LINE.format("Not included")
        for name, call in self.calls():
            with self.assertNoLogs("earwitness.ops", level="WARNING"):
                html = self.page(call)
            for needle in ("Reno", "pending", "audio decision"):
                self.assertNotIn(needle, html, f"{name}: {needle}")
            self.assertEqual(html.count("plan-audio"), 2, name)
            for plan in ("a", "b"):
                card = self.card(html, plan)
                self.assertEqual(card.count(line), 1, f"{name} {plan}")
                self.assertIn("Early access opens soon.", card, f"{name} {plan}")

    def test_pending_hides_the_audio_line_and_names_no_one(self):
        self.write(config(audio="pending"))
        for name, call in self.calls():
            with self.assertNoLogs("earwitness.ops", level="WARNING"):
                html = self.page(call)
            self.assert_public(html, name)
            self.assertNotIn("plan-audio", html, name)

    def test_pending_hides_the_line_with_a_filled_template_too(self):
        self.fill_template()
        for a, b in (("off", "off"), ("on", "on"), ("closed", "off")):
            self.write(config(a=a, b=b, audio="pending", link_a=LINK_A if a == "on" else None,
                              link_b=LINK_B if b == "on" else None))
            for name, call in self.calls():
                if (a, b) == ("off", "off"):
                    with self.assertNoLogs("earwitness.ops", level="WARNING"):
                        html = self.page(call)
                else:
                    with self.assertLogs("earwitness.ops", level="WARNING"):
                        html = self.page(call)
                self.assert_public(html, f"{name} {a}/{b}")
                self.assertNotIn("plan-audio", html, f"{name} {a}/{b}")

    def test_decided_audio_renders_one_identical_line_on_both_cards(self):
        self.fill_template()
        for value, text in AUDIO_TEXT.items():
            for a, b in (("off", "off"), ("on", "on"), ("closed", "on")):
                self.write(config(a=a, b=b, audio=value, link_a=LINK_A if a == "on" else None,
                                  link_b=LINK_B if b == "on" else None))
                for name, call in self.calls():
                    html = self.page(call)
                    line = AUDIO_LINE.format(text)
                    label = f"{name} {value} {a}/{b}"
                    self.assertEqual(html.count(line), 2, label)
                    self.assertEqual(html.count("plan-audio"), 2, label)
                    for plan in ("a", "b"):
                        card = self.card(html, plan)
                        self.assertEqual(card.count(line), 1, f"{label} {plan}")
                        # Where the template had it: after the list, before "Not included:".
                        self.assertLess(card.index("</ul>"), card.index(line), f"{label} {plan}")
                        self.assertLess(card.index(line), card.index('class="plan-not"'),
                                        f"{label} {plan}")
                    self.assertNotIn("Reno", html, label)
                    self.assertNotIn("<!--pricing", html, label)

    def test_decided_audio_line_shows_with_the_committed_template(self):
        # The line does not wait for prices: it follows the decision only.
        self.write(config(audio="not_included"))
        for name, call in self.calls():
            html = self.page(call)
            self.assertEqual(html.count(AUDIO_LINE.format("Not included")), 2, name)

    def test_pending_with_everything_filled_keeps_every_plan_off_and_logs(self):
        self.fill_template()
        self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B, audio="pending"))
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("audio decision pending" in line for line in logs.output),
                            f"{name}: {logs.output}")
            self.assertNotIn("plan-buy", html, name)
            self.assertNotIn("buy.stripe.com", html, name)
            for plan in ("a", "b"):
                card = self.card(html, plan)
                self.assertIn("Early access opens soon.", card, f"{name} {plan}")
                self.assertIn(f'Questions: <a href="mailto:{EMAIL}">', card, f"{name} {plan}")

    def test_pending_with_a_plan_closed_forces_off_and_logs(self):
        self.fill_template()
        self.write(config(b="closed", audio="pending"))
        for name, call in self.calls():
            with self.assertLogs("earwitness.ops", level="WARNING") as logs:
                html = self.page(call)
            self.assertTrue(any("audio decision pending" in line for line in logs.output),
                            f"{name}: {logs.output}")
            self.assertNotIn("Early access sales have closed.", html, name)

    def test_pending_with_every_plan_off_logs_nothing(self):
        self.fill_template()
        self.write(config(audio="pending"))
        for name, call in self.calls():
            with self.assertNoLogs("earwitness.ops", level="WARNING"):
                self.page(call)

    def test_decided_audio_with_everything_filled_shows_the_buy_links(self):
        self.fill_template()
        for value in AUDIO_TEXT:
            self.write(config(a="on", link_a=LINK_A, b="on", link_b=LINK_B, audio=value))
            for name, call in self.calls():
                with self.assertNoLogs("earwitness.ops", level="WARNING"):
                    html = self.page(call)
                self.assertIn(f'<a class="cta plan-buy" href="{LINK_A}"', self.card(html, "a"),
                              f"{name} {value}")
                self.assertIn(f'<a class="cta plan-buy" href="{LINK_B}"', self.card(html, "b"),
                              f"{name} {value}")

    def test_validator_names_missing_and_invalid_audio(self):
        tmp = tempfile.mkdtemp(prefix="ew-pricing-audio-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp, "pricing.json")
        data = config()
        del data["audio"]
        path.write_text(json.dumps(data), encoding="utf-8")
        cfg, problem = public_pricing.load(path)
        self.assertEqual(problem, "audio key missing")
        self.assertEqual(cfg["audio"], "pending", "fail closed: no audio line")
        for value in ("maybe", "", True, None, ["pending"], {"v": 1}):
            with self.subTest(value=value):
                path.write_text(json.dumps(config(audio=value)), encoding="utf-8")
                cfg, problem = public_pricing.load(path)
                self.assertTrue(problem.startswith("audio value invalid"), problem)
                self.assertEqual(cfg["audio"], "pending")
                self.assertEqual(cfg["plan_a"], {"state": "off", "link": None})


class PublicPagesNameNoOneTests(unittest.TestCase):

    def test_no_public_page_names_reno_or_a_pending_decision(self):
        served = [p for p in sorted(PUBLIC.iterdir())
                  if p.suffix in (".html", ".css", ".js")]
        self.assertIn(PUBLIC / "pricing.html", served)
        for path in served:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Reno", text, path.name)
            self.assertNotIn("pending Reno", text, path.name)


class GoLiveCheckTests(unittest.TestCase):
    """DPM-R1-01: `python -m earwitness.app.public_pricing --check-live
    <app_dir>` fails while the served page would show a placeholder or an
    example.com address, and passes once real values are in."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="ew-pricing-live-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.app_dir = Path(tmp, "app")
        (self.app_dir / "public").mkdir(parents=True)
        for name in ("pricing.html", "pricing.json", *LOCAL_BUILD):
            shutil.copy(PUBLIC / name, self.app_dir / "public" / name)
        self.approve_legal()

    def approve_legal(self):
        """The legal pages as they will be once Reno approves public text
        and buyer terms and the pilot end is set (their placeholders
        filled, the buyer-terms marker gone)."""
        for name, phrases in LOCAL_BUILD.items():
            path = self.app_dir / "public" / name
            text = path.read_text(encoding="utf-8")
            for phrase in phrases:
                text = text.replace(phrase, "approved public text")
            path.write_text(fill(text), encoding="utf-8")
        without_buyer_marker(self.app_dir / "public")

    def check(self, app_dir):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        return subprocess.run(
            [sys.executable, "-m", "earwitness.app.public_pricing", "--check-live", str(app_dir)],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)

    def set_template(self, text):
        (self.app_dir / "public" / "pricing.html").write_text(text, encoding="utf-8")

    def template(self):
        return (PUBLIC / "pricing.html").read_text(encoding="utf-8")

    def test_the_committed_page_is_not_ready(self):
        # LEGAL-FIX3: prices and audio are decided. PILOT-END: the pilot end
        # is set; only the buyer terms still block go-live.
        out = self.check(REPO / "app")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(out.stdout.splitlines(),
                         ["NOT READY: terms.html: buyer terms pending",
                          "NOT READY: privacy.html: buyer terms pending"],
                         out.stdout + out.stderr)

    def set_pending_audio(self):
        path = self.app_dir / "public" / "pricing.json"
        data = json.loads((PUBLIC / "pricing.json").read_text(encoding="utf-8"))
        data["audio"] = "pending"
        path.write_text(json.dumps(data), encoding="utf-8")

    def decide_audio(self, value="included"):
        """The committed file with the audio decision made."""
        path = self.app_dir / "public" / "pricing.json"
        data = json.loads((PUBLIC / "pricing.json").read_text(encoding="utf-8"))
        data["audio"] = value
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_filled_page_with_the_real_address_is_ready(self):
        self.set_template(fill(self.template()))
        for value in ("included", "not_included"):
            with self.subTest(value):
                self.decide_audio(value)
                out = self.check(self.app_dir)
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    # AUDIO-LINE: the committed file's pending audio decision blocks go-live.
    def test_pending_audio_fails_and_names_the_reason(self):
        self.set_template(fill(self.template()))
        self.set_pending_audio()
        out = self.check(self.app_dir)
        self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("NOT READY: pricing.json: audio decision pending", out.stdout)
        self.assertEqual(public_pricing.live_problems(self.app_dir),
                         ["pricing.json: audio decision pending"])
        self.decide_audio("not_included")
        self.assertEqual(public_pricing.live_problems(self.app_dir), [])

    def test_pending_reason_goes_once_decided_while_other_reasons_stay(self):
        self.set_template(with_price_placeholder(self.template()))
        self.set_pending_audio()
        out = self.check(self.app_dir)
        self.assertIn("pricing.json: audio decision pending", out.stdout)
        self.decide_audio("included")  # template left with its placeholders
        problems = public_pricing.live_problems(self.app_dir)
        self.assertNotIn("pricing.json: audio decision pending", problems)
        self.assertIn("the page still shows [PRICE]", problems)

    # Owner addition (Purchase): a hand edit that drops or garbles the key
    # never passes silently, and the reason says so.
    def test_missing_audio_key_fails_and_names_it(self):
        self.set_template(fill(self.template()))
        data = json.loads((PUBLIC / "pricing.json").read_text(encoding="utf-8"))
        del data["audio"]
        (self.app_dir / "public" / "pricing.json").write_text(json.dumps(data), encoding="utf-8")
        out = self.check(self.app_dir)
        self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("NOT READY: pricing.json: audio key missing", out.stdout)

    def test_invalid_audio_value_fails_and_names_it(self):
        self.set_template(fill(self.template()))
        for value in ("maybe", "Included", True, None, ["included"], 1):
            with self.subTest(value=value):
                self.decide_audio(value)
                out = self.check(self.app_dir)
                self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertIn("NOT READY: pricing.json: audio value invalid", out.stdout)

    def test_each_leftover_fails(self):
        self.decide_audio()  # only the leftover under test blocks
        filled = fill(self.template())
        cases = {
            "price": PRICE_LINE.sub(PRICE_PLACEHOLDER, filled, count=1),
            "pilot end": filled.replace(f"Early access ends on {PILOT_END_TEXT}",
                                        "Early access ends on [PILOT END]", 1),
            "placeholder class": filled.replace("<h3>", '<span class="placeholder">x</span><h3>', 1),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertNotEqual(text, filled, label)
                self.set_template(text)
                out = self.check(self.app_dir)
                self.assertNotEqual(out.returncode, 0, label)
        self.set_template(filled)
        (self.app_dir / "public" / "pricing.json").write_text(
            json.dumps(config(email="support@example.com")), encoding="utf-8")
        out = self.check(self.app_dir)
        self.assertNotEqual(out.returncode, 0, "example.com address")
        self.assertIn("example.com", out.stdout + out.stderr)

    # Owner addition (Muse DPM-R1-02): the legal pages' local-build text.
    # LEGAL-PAGES (G7-legal): the committed pages carry public text now, so
    # no local-build phrase is reported; the guard itself stays (next test).
    def test_the_committed_legal_pages_carry_no_local_build_wording(self):
        self.set_template(fill(self.template()))
        for name in LOCAL_BUILD:
            shutil.copy(PUBLIC / name, self.app_dir / "public" / name)
        out = self.check(self.app_dir)
        self.assertNotIn("still says", out.stdout, out.stdout)

    def test_each_local_build_phrase_fails_and_is_named(self):
        self.set_template(fill(self.template()))
        self.decide_audio()
        self.assertEqual(public_pricing.live_problems(self.app_dir), [])
        for name, phrases in LOCAL_BUILD.items():
            path = self.app_dir / "public" / name
            clean = path.read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(name=name, phrase=phrase):
                    self.assertNotIn(phrase, clean)
                    path.write_text(clean.replace("</h1>", f"</h1>\n<p>{phrase}</p>", 1),
                                    encoding="utf-8")
                    self.assertEqual(public_pricing.live_problems(self.app_dir),
                                     [f'{name} still says "{phrase}"'])
            path.write_text(clean, encoding="utf-8")

    def test_broad_words_alone_pass(self):
        # Only the exact local-build wording blocks, not the words "local" or "build".
        self.set_template(fill(self.template()))
        path = self.app_dir / "public" / "privacy.html"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "</h1>", "</h1>\n<p>Local time. We build reports from development notes.</p>", 1),
            encoding="utf-8")
        self.decide_audio()
        self.assertEqual(public_pricing.live_problems(self.app_dir), [])

    def test_a_missing_legal_page_fails(self):
        self.set_template(fill(self.template()))
        self.decide_audio()
        (self.app_dir / "public" / "terms.html").unlink()
        problems = public_pricing.live_problems(self.app_dir)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("terms.html", problems[0])

    # PILOT-END: the committed legal pages show the set pilot end, so the
    # go-live check reports no placeholder on them, only the buyer terms.
    def test_the_committed_legal_pages_show_the_pilot_end_date(self):
        self.set_template(fill(self.template()))
        self.decide_audio()
        for name in LOCAL_BUILD:
            shutil.copy(PUBLIC / name, self.app_dir / "public" / name)
        problems = public_pricing.live_problems(self.app_dir)
        self.assertEqual(problems, [f"{name}: buyer terms pending" for name in BUYER_PAGES])
        self.assertIn(f"ends on {PILOT_END_TEXT}.", (PUBLIC / "terms.html").read_text(encoding="utf-8"))
        self.assertIn(f"At {PILOT_END_TEXT}, all access ends",
                      (PUBLIC / "privacy.html").read_text(encoding="utf-8"))

    def test_a_placeholder_on_each_legal_page_fails_and_is_named(self):
        self.set_template(fill(self.template()))
        self.decide_audio()
        self.assertEqual(public_pricing.live_problems(self.app_dir), [])
        for name in LOCAL_BUILD:
            path = self.app_dir / "public" / name
            clean = path.read_text(encoding="utf-8")
            with self.subTest(name=name):
                path.write_text(clean.replace(
                    "</h1>", '</h1>\n<p>Ends <span class="placeholder">[PILOT END]</span>.</p>', 1),
                    encoding="utf-8")
                problems = public_pricing.live_problems(self.app_dir)
                self.assertIn(f"{name} still shows [PILOT END]", problems)
                self.assertIn(f'{name} still shows {public_pricing.PLACEHOLDER}', problems)
                out = self.check(self.app_dir)
                self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
                self.assertIn(f"NOT READY: {name} still shows [PILOT END]", out.stdout)
            path.write_text(clean, encoding="utf-8")

    # LEGAL-FIX1 (code review note 1): buyer terms are not written yet.
    def test_the_committed_terms_and_privacy_report_buyer_terms_pending(self):
        self.set_template(fill(self.template()))
        self.decide_audio()
        for name in LOCAL_BUILD:
            shutil.copy(PUBLIC / name, self.app_dir / "public" / name)
        problems = public_pricing.live_problems(self.app_dir)
        for name in BUYER_PAGES:
            self.assertIn(f"{name}: buyer terms pending", problems, problems)
        self.assertNotIn("access.html: buyer terms pending", problems)

    def test_the_buyer_terms_marker_on_each_page_fails_and_is_named(self):
        self.set_template(fill(self.template()))
        self.decide_audio()
        self.assertEqual(public_pricing.live_problems(self.app_dir), [])
        for name in BUYER_PAGES:
            path = self.app_dir / "public" / name
            clean = path.read_text(encoding="utf-8")
            with self.subTest(name=name):
                path.write_text(clean.replace("<body>", "<body>\n" + BUYER_TERMS, 1),
                                encoding="utf-8")
                self.assertEqual(public_pricing.live_problems(self.app_dir),
                                 [f"{name}: buyer terms pending"])
                out = self.check(self.app_dir)
                self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
                self.assertIn(f"NOT READY: {name}: buyer terms pending", out.stdout)
            path.write_text(clean, encoding="utf-8")

    def test_a_hand_edited_marker_fails_check_live(self):
        """LEGAL-FIX2 (code review note 1): whitespace inside the comment
        does not hide the marker from --check-live."""
        self.set_template(fill(self.template()))
        self.decide_audio()
        self.assertEqual(public_pricing.live_problems(self.app_dir), [])
        for name in BUYER_PAGES:
            path = self.app_dir / "public" / name
            clean = path.read_text(encoding="utf-8")
            for marker in BUYER_TERMS_VARIANTS:
                with self.subTest(name=name, marker=marker):
                    path.write_text(clean.replace("<body>", "<body>\n" + marker, 1),
                                    encoding="utf-8")
                    self.assertEqual(public_pricing.live_problems(self.app_dir),
                                     [f"{name}: buyer terms pending"])
            out = self.check(self.app_dir)
            self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
            self.assertIn(f"NOT READY: {name}: buyer terms pending", out.stdout)
            path.write_text(clean, encoding="utf-8")

    def test_the_buyer_terms_check_does_not_ride_on_the_local_build_list(self):
        """LEGAL-FIX2 (code review note 2): Terms and Privacy are checked for
        the marker, or fail when unreadable, even if LOCAL_BUILD drops them."""
        self.set_template(fill(self.template()))
        self.decide_audio()
        for name in BUYER_PAGES:
            path = self.app_dir / "public" / name
            clean = path.read_text(encoding="utf-8")
            with self.subTest(name=name), \
                    mock.patch.object(public_pricing, "LOCAL_BUILD", {}):
                self.assertEqual(public_pricing.live_problems(self.app_dir), [])
                path.write_text(clean.replace("<body>", "<body>\n" + BUYER_TERMS, 1),
                                encoding="utf-8")
                self.assertEqual(public_pricing.live_problems(self.app_dir),
                                 [f"{name}: buyer terms pending"])
                path.write_bytes(b"\xff\xfe not utf-8")
                self.assertEqual(len([p for p in public_pricing.live_problems(self.app_dir)
                                      if p.startswith(name)]), 1)
                path.unlink()
                self.assertTrue(any(p.startswith(name)
                                    for p in public_pricing.live_problems(self.app_dir)))
            path.write_text(clean, encoding="utf-8")

    def test_an_invalid_file_fails(self):
        self.set_template(fill(self.template()))
        (self.app_dir / "public" / "pricing.json").write_text("{nope", encoding="utf-8")
        out = self.check(self.app_dir)
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
