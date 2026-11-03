/* PODS-REDESIGN-PORT, Ad page (#/evidence/{ref}): "Named by Muse Spark"
   (PODS-STRINGS P1) after each model-named name in the header and the
   player's heard line; the P2 (spoken) or P3 (url) tip by that name's own
   channel, readable by screen readers, ending in one Report a mistake
   button; no brand alert and no campaign name for a model-named read.
   Run: node --test tests/frontend_b2_pods_ad.test.cjs */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const REF = 'named:pods1';
const P2 = 'Muse Spark, Meta’s AI model, named this brand from the words spoken in the ad’s transcript and found it on our brand list. If it’s wrong, use Report a mistake.';
const P3 = 'Muse Spark, Meta’s AI model, named this brand from a web address in the ad’s transcript and found it on our brand list. If it’s wrong, use Report a mistake.';
const BANNED = /\b(confirmed|verified|checked|possible|likely|suggested)\b/i;
const marks = s => (String(s).match(/class="ew-brand-mark">Named by Muse Spark</g) || []).length;
const seg = (id, start, end, text) => ({ segment_id: id, start_s: start, end_s: end, text, text_start: 0 });
const LINES = [
  seg(1, 1316, 1319, 'Before the break.'),
  seg(2, 1320, 1330, 'Protect yourself online with NordVPN. Go to nordvpn.com slash show.'),
  seg(3, 1330, 1400, 'Hiring? Post your job at linkedin.com slash partner.'),
  seg(4, 1404, 1410, 'And we are back.'),
];
const CLIP = { playback: 'local', kind: 'evidence_clip', source_offset_s: 1315, clip_start_s: 1315, clip_end_s: 1400, alignment: 'verified', timing: 'verified', trust: 'exact' };
const NORD = { key: 'nordvpn', display: 'NordVPN', display_basis: 'model', model_channel: 'spoken' };
const LINK_URL = { key: 'linkedin', display: 'LinkedIn', display_basis: 'model', model_channel: 'url' };
const TWO_NAMES = [{ key: 'nordvpn', display: 'NordVPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'LinkedIn', model_channel: 'url' }];
const QUINCE = { key: 'quince', display: 'Quince', display_basis: 'brand_list' };
function record(read = {}, said = {}) {
  return {
    ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
    review: { judgment: null, bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
    bounds_basis: 'full_effective_source', selected_bounds: { start_s: 1316, end_s: 1410 },
    episode: { episode_id: 'ep_p', show_title: 'Pods Show', ep_title: 'Pods episode', pub_date: '2026-10-21', fetch_date: '2026-10-22', duration_min: 50, transcript_duration_s: 3000 },
    show: { show_identity_id: 'cs-pods', display_title: 'Pods Show', unresolved: 0 },
    evidence: { id: 'named:ep_p.1320', episode_id: 'ep_p', orig_start_s: 1320, orig_end_s: 1400, transcript_version_id: 7 },
    media: CLIP, heard_at_s: 1322,
    read: { kind: 'named', source_label: 'Named read', brand: NORD, needs_review: false, self_promo: false, display_span: [1320, 1400], has_clip: true,
      candidates: [], said: { spoken_brand: null, web_addresses: [], promo_codes: [], ...said }, ...read },
    window: { segments: LINES, text_complete: true, next_cursor: null },
  };
}
const route = '#/evidence/' + encodeURIComponent(REF);
const api = rec => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  return new Error('unexpected ' + p);
};
const page = async (rec, width = 1440) => { const out = await H.render(route, api(rec), { width }); return { ...out, text: H.text(out.html) }; };
const headOf = html => (html.match(/<(?:section class="a-head"|div class="a-head--phone")[^]*?<\/h1>/) || [''])[0];
const tipsOf = html => [...html.matchAll(/<p class="a-named"[^>]*>([^]*?)<\/p>/g)].map(m => m[1]);
const plain = s => String(s).replace(/<[^>]+>/g, '');
const heardOf = html => (html.match(/into the episode, [^]*?is first heard/) || [''])[0];

test('header: P1 right after the model-named name, one unit; the h1 itself has no label', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page(record(), width);
    const head = headOf(html);
    assert.equal(marks(head), 1, `${width}: one label`);
    assert.match(head, /<span class="ew-brand-unit"><span class="s-strong">NordVPN<\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/, `${width}: name, then label, one unit`);
    const h1 = (head.match(/<h1[^]*?<\/h1>/) || [''])[0];
    assert.equal(marks(h1), 0, `${width}: h1 plain`);
  }
});

test('player heard line: P1 after the name', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page(record(), width);
    const heard = heardOf(html);
    assert.equal(marks(heard), 1, `${width}: heard line`);
    assert.equal(H.text(heard), 'into the episode, NordVPN Named by Muse Spark is first heard');
  }
});

test('P2 for a spoken model name, P3 for a url one, word for word, with one Report a mistake button', async () => {
  const spoken = await page(record());
  assert.deepEqual(tipsOf(spoken.html).map(plain), [P2]);
  assert.doesNotMatch(spoken.text, /from a web address in the ad’s transcript/);
  const url = await page(record({ brand: LINK_URL }));
  assert.deepEqual(tipsOf(url.html).map(plain), [P3]);
  assert.doesNotMatch(url.text, /from the words spoken in the ad’s transcript/);
  for (const { html } of [spoken, url]) {
    const tip = tipsOf(html)[0];
    assert.equal((tip.match(/<button type="button" class="a-named__report" data-b2-report>Report a mistake<\/button>/g) || []).length, 1, 'the report button ends the tip');
    // Readable by screen readers: plain text in the page, not hidden, not a tooltip.
    assert.doesNotMatch(html, /<p class="a-named"[^>]*(aria-hidden|hidden)/);
  }
});

test('an unknown channel gets the label but no tip', async () => {
  const { html } = await page(record({ brand: { ...NORD, model_channel: null } }));
  assert.equal(marks(headOf(html)), 1);
  assert.deepEqual(tipsOf(html), []);
});

test('two model names: each with its own P1 and its own tip by its own channel, one Report a mistake button after them', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page(record({ model_names: TWO_NAMES }), width);
    const head = headOf(html);
    assert.equal(marks(head), 2, `${width}: two labels in the header`);
    assert.match(head, /<img class="s-logo [^"]*"[^>]*alt="LinkedIn"[^>]*> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/, `${width}: LinkedIn's logo then its label`);
    assert.match((head.match(/<h1[^]*?<\/h1>/) || [''])[0], />NordVPN · LinkedIn ad on Pods Show</, `${width}: h1 names both, no main brand`);
    const tips = tipsOf(html);
    assert.equal(tips.length, 2, `${width}: one tip per name`);
    assert.equal(plain(tips[0]), 'NordVPN: ' + P2);
    assert.equal(plain(tips[1]), 'LinkedIn: ' + P3);
    assert.doesNotMatch(tips[0], /<button/, `${width}: the first tip names the control in plain words`);
    assert.match(tips[1], /data-b2-report>Report a mistake<\/button>\.$/, `${width}: one button, after both`);
    assert.equal(marks(heardOf(html)), 2, `${width}: heard line, both names labelled`);
  }
});

test('Report a mistake in the tip opens the page\'s own report control and focuses it', async () => {
  const { win, listeners } = await page(record());
  const summary = { focused: 0, focus() { this.focused++; }, scrollIntoView() {}, parentElement: { open: false } };
  win.document.querySelector = sel => (sel === '.a-facts .report-problem > summary' ? summary : null);
  const btn = { closest: s => (s === '[data-b2-report]' ? btn : null) };
  const ev = { target: btn, preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} };
  for (const l of listeners.filter(x => x.type === 'click')) l.fn(ev);
  assert.equal(summary.parentElement.open, true, 'the details open');
  assert.equal(summary.focused, 1, 'focus moves to the control');
  assert.match((await page(record())).html, /<details class="report-problem"><summary>Report a mistake<\/summary>/, 'the existing control, word for word');
});

test('a plain brand: no label, no tip; Create an alert stays', async () => {
  const { html, text } = await page(record({ brand: QUINCE }));
  assert.equal(marks(html), 0);
  assert.deepEqual(tipsOf(html), []);
  assert.ok(text.includes('Create an alert for Quince'));
  assert.match(html, /data-adding="Quince ad · Pods Show/);
});

test('a model-named-only ad: no Create alert, no brand name on campaign screens', async () => {
  for (const rec of [record(), record({ model_names: TWO_NAMES })]) {
    for (const width of [1440, 390]) {
      const { html } = await page(rec, width);
      assert.doesNotMatch(html, /data-action="alert-evidence"/, `${width}: no Create alert`);
      assert.doesNotMatch(html, /create alert/i, `${width}: the More button does not offer it either`);
      for (const add of html.match(/<button[^>]*data-action="add-to-campaign"[^>]*>/g) || []) {
        assert.doesNotMatch(add, /data-brand-name=/, `${width}: no brand name for the campaign`);
        assert.match(add, /data-adding="Ad, brand not identified · Pods Show/, `${width}: campaign words`);
      }
    }
  }
});

test('no verification or confidence word next to the label or in the tips', async () => {
  for (const rec of [record(), record({ brand: LINK_URL }), record({ model_names: TWO_NAMES })]) {
    for (const width of [1440, 390]) {
      const { html } = await page(rec, width);
      for (const m of html.matchAll(/Named by Muse Spark/g)) {
        assert.doesNotMatch(H.text(html.slice(Math.max(0, m.index - 300), m.index + 300)), BANNED);
      }
      for (const t of tipsOf(html)) assert.doesNotMatch(H.text(t), BANNED);
    }
  }
});

test('screen-reader and short labels keep no P1 (Back link, aria labels)', async () => {
  const { html } = await page(record({ model_names: TWO_NAMES }));
  for (const a of html.match(/aria-label="[^"]*"/g) || []) assert.doesNotMatch(a, /Muse Spark/);
  assert.doesNotMatch((html.match(/<a class="a-back"[^]*?<\/a>/) || [''])[0], /Muse Spark/);
});

/* RPR-01: a hostile model name is escaped at every print site (header, h1, heard line, each two-name tip). */
test('RPR-01: a hostile model name is escaped in the header, h1, heard line and each two-name tip', async () => {
  const BAD = { key: 'nordvpn', display: 'Nord<x-pwn>VPN', display_basis: 'model', model_channel: 'spoken' };
  const BAD_TWO = [{ key: 'nordvpn', display: 'Nord<x-pwn>VPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'Link<x-pwn>In', model_channel: 'url' }];
  for (const rec of [record({ brand: BAD }), record({ brand: BAD, model_names: BAD_TWO })]) {
    for (const width of [1440, 390]) {
      const { html } = await page(rec, width);
      assert.doesNotMatch(html, /<x-pwn/, `${width}: raw name`);
      assert.match(headOf(html), /Nord&lt;x-pwn&gt;VPN/, `${width}: header draws it escaped`);
    }
  }
  const tips = tipsOf((await page(record({ brand: BAD, model_names: BAD_TWO }))).html);
  assert.equal(tips.length, 2);
  assert.match(tips[0], /^<b>Nord&lt;x-pwn&gt;VPN<\/b>: /, 'first tip, escaped name');
  assert.match(tips[1], /^<b>Link&lt;x-pwn&gt;In<\/b>: /, 'second tip, escaped name');
});

/* Review LOW (UX D41 item 6 note): at a wrap the tag line never ends on a stray "·": each separator rides with the next name. */
test('two-name tag line: the "·" sits inside the next name\'s unit, never after a label', async () => {
  for (const width of [1440, 390]) {
    const head = headOf((await page(record({ model_names: TWO_NAMES }), width)).html);
    assert.doesNotMatch(head, /Named by Muse Spark<\/span><\/span> · /, `${width}: no separator after the first unit`);
    assert.match(head, /<span class="ew-brand-unit">· <img class="s-logo [^"]*"[^>]*alt="LinkedIn"/, `${width}: the separator leads the second unit`);
  }
});
