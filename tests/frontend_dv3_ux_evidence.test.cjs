/* Design v3 UX review fixes, Evidence area (T1, T5 evidence copy).
   What the highlight is (detected window, reviewed times), where the
   brand name is first heard (a transcript fact, never an ad start), the
   heard spelling, the caveat before the actions, facts before the words,
   pre-scoped alerts, Add to campaign, Copy link, the Episode row Save and
   the evidence copy fixes. Loader helpers copied from
   frontend_dv3_g2.test.cjs. Run: node --test tests/frontend_dv3_ux_evidence.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route, origin: 'http://127.0.0.1:9171', pathname: '/app/' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const sessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(route = '#/overview') {
  const win = loadAt(route, { fetch: sessionFetch() });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
// A page render plus a working modal, toasts and API log.
async function page(route, api, { hosted = false, q = null } = {}) {
  const win = loadViews(route);
  await flushBoot();
  win.EW.current.hosted = hosted;
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, opts = {}) => { calls.push([opts.method || 'GET', p, opts.body]); return api(p, opts); };
  win.location.hash = route;
  win.EW.current.route = route;
  if (q !== null) win.EW.current.filters = { ...(win.EW.current.filters || {}), q };
  await win.EWViews.render();
  return { win, html: main.innerHTML, main, modal, toasts, calls };
}
function ledger(html) {
  const rows = {};
  for (const m of html.matchAll(/<th scope="row">([^<]*)<\/th><td[^>]*>([\s\S]*?)<\/td><\/tr>/g)) rows[m[1]] = m[2];
  return rows;
}
const lineFor = (html, seg) => (html.match(new RegExp(`<li[^>]*data-segment="${seg}"[^>]*>[\\s\\S]*?</li>`)) || [''])[0];

const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
const EV_ID = 'named:ep_ux.named.240000-309240';
const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const read = (over = {}) => ({ kind: 'named', source_label: 'Named read',
  brand: { key: 'betterhelp', display: 'BetterHelp', display_basis: 'brand_list', suggested: 'betterhelp', confirmed: false },
  needs_review: false, self_promo: false, self_promo_reason: null, confidence: 'high', display_span: [250.02, 309.24],
  display_basis: 'detected', has_clip: false,
  said: { web_addresses: ['betterhelp.com/npr'], promo_codes: [], address_brand: 'BetterHelp', code_brand: null, spoken_brand: 'Betterhelp' },
  ...over });
const segments = [
  { segment_id: 41, start_s: 240, end_s: 250, text: 'And the markets closed lower today.', text_start: 0, text_end: 35 },
  { segment_id: 42, start_s: 281.46, end_s: 286.46, text: 'For NPR News, I’m in Washington.', text_start: 0, text_end: 32 },
  { segment_id: 43, start_s: 286.46, end_s: 300, text: 'This message comes from Better Help. Find a therapist.', text_start: 0, text_end: 54 },
  { segment_id: 44, start_s: 300, end_s: 309.24, text: 'Visit betterhelp.com/npr today.', text_start: 0, text_end: 31 }];
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'betterhelp', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 240, end_s: 309.24 },
  episode: { episode_id: 'ep_ux', show_title: 'Up First', ep_title: 'Tuesday', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 14 },
  show: { show_identity_id: 'cs-upfirst', display_title: 'Up First', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_ux', orig_start_s: 240, orig_end_s: 309.24 }, media: { playback: 'remote', alignment: 'unknown' },
  read: read(), window: { segments, text_complete: true, next_cursor: null }, ...over });
const evApi = (rec, extra = () => undefined) => (p, opts) => {
  const own = extra(p, opts);
  if (own !== undefined) { if (own instanceof Error) throw own; return own; }
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_ux') return { episode: { ads: [] } };
  if (p === '/customer/shows') return { shows: [{ id: 'cs-upfirst', display_title: 'Up First' }] };
  throw new Error('unexpected ' + p);
};
const evidence = (rec, opts, extra) => page('#/evidence/' + encodeURIComponent(REF), evApi(rec, extra), opts);

/* -- UX-01 B/C: what the highlight is, where the brand is first heard -- */
// PLAIN-ADS (DEP7): every ad's player says "Ad" with one note; how its
// times were set stays internal.
test('a detected window reads "Ad", with no player note and no ad start', async () => {
  const { html } = await evidence(record());
  assert.match(html, /<span class="eyebrow ew-player-kind">Ad<\/span>/);
  assert.ok(!html.includes('A time shows where it was heard') && !html.includes('ew-player-note">A time'), 'the player note is gone');
  assert.doesNotMatch(html, /Ad starts here|The detector’s window/);
  assert.match(ledger(html)['How the time was found'], /Times from the transcript/);
  assert.match(html, /<section class="ew-card ew-player" aria-label="Where it was heard">/, 'no clip: the card is not called a clip (R2-01)');
});

test('the brand name tag sits on the first line that says it; the ledger gives its time', async () => {
  const { html } = await evidence(record());
  assert.equal((html.match(/Brand name first heard/g) || []).length, 1);
  assert.match(lineFor(html, 43), /<span class="ew-line-tag">Brand name first heard<\/span>/);
  assert.match(ledger(html)['Brand heard in the audio'], /<div class="ew-ledger-source">From the transcript · 4:46<\/div>/);
  assert.match(html, /data-action="jump-words"[^>]*data-segment="43"[^>]*>Jump to the brand name \(4:46\)</);
});

test('no line says the brand: no tag, the plain source and the plain jump', async () => {
  const rec = record({ read: read({ said: { ...read().said, spoken_brand: 'Talkspace' } }) });
  const { html } = await evidence(rec);
  assert.doesNotMatch(html, /Brand name first heard/);
  assert.match(ledger(html)['Brand heard in the audio'], /<div class="ew-ledger-source">From the transcript<\/div>/);
  assert.match(html, /data-action="jump-words"[^>]*>Jump to the words</);
  assert.doesNotMatch(html, /data-action="jump-words"[^>]*data-segment/);
});

test('a brand match starts and ends on word edges ("during" never holds "ring")', async () => {
  const rec = record({ read: read({ brand: { key: 'ring', display: 'Ring', display_basis: 'brand_list', confirmed: false },
    said: { ...read().said, spoken_brand: 'Ring' } }),
  window: { segments: [{ segment_id: 1, start_s: 240, end_s: 250, text: 'During the break we talk.', text_start: 0, text_end: 25 },
    { segment_id: 2, start_s: 250, end_s: 260, text: 'Ring doorbells, ring.com.', text_start: 0, text_end: 25 }], text_complete: true, next_cursor: null } });
  const { html } = await evidence(rec);
  assert.doesNotMatch(lineFor(html, 1), /Brand name first heard/);
  assert.match(lineFor(html, 2), /Brand name first heard/);
});

test('a brand under review gets no tag and no heard spelling', async () => {
  const rec = record({ read: read({ brand: { ...UNDER_REVIEW }, needs_review: true, said: undefined,
    candidates: undefined }) });
  const { html } = await evidence(rec);
  assert.doesNotMatch(html, /Brand name first heard|Heard as|BetterHelp|Betterhelp/);
  assert.match(html, /<p class="ew-reader-caveat">We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand\. Know the brand\? Use <button type="button" class="text-link" data-action="goto-report">Report a mistake<\/button>\.<\/p>/);
});

test('checked bounds on a judged ad: the label, "Ad starts here" on the start line, one tag per line', async () => {
  const rec = record({ review: { judgment: 'ad', bounds_verified: 1 },
    read: read({ display_span: [286.46, 309.24], display_basis: 'checked' }) });
  const { html } = await evidence(rec);
  // A possible ad never says "Ad read" (F-02); a confirmed one: frontend_dv3_ux_fixA.
  assert.match(html, /<span class="eyebrow ew-player-kind">Ad<\/span>/);
  assert.doesNotMatch(html, /review team/);
  assert.match(lineFor(html, 43), /<span class="ew-line-tag">Ad starts here<\/span>/);
  assert.doesNotMatch(lineFor(html, 43), /Brand name first heard/, 'both on one line: "Ad starts here" only');
  assert.equal((html.match(/Ad starts here/g) || []).length, 1);
  assert.match(ledger(html)['How the time was found'], /Times from the transcript/);
  assert.doesNotMatch(html, /The window can begin before the ad/);
});

test('corrected (not checked) times and unsure judgments never show an ad start', async () => {
  const corrected = await evidence(record({ review: { judgment: 'ad', bounds_verified: 0 },
    read: read({ display_span: [286.46, 309.24], display_basis: 'corrected' }) }));
  assert.match(corrected.html, /<span class="eyebrow ew-player-kind">Ad<\/span>/);
  assert.doesNotMatch(corrected.html, /Ad read|review team/);
  assert.match(ledger(corrected.html)['How the time was found'], /Times from the transcript/);
  assert.doesNotMatch(corrected.html, /Ad starts here/);
  const unsure = await evidence(record({ review: { judgment: 'unsure', bounds_verified: 1 },
    read: read({ display_span: [286.46, 309.24], display_basis: 'checked' }) }));
  assert.match(unsure.html, /<span class="eyebrow ew-player-kind">Ad<\/span>/);
  assert.doesNotMatch(unsure.html, /Ad starts here|Ad read ·/);
});

/* -- R2-02: the heard spelling -- */
const WAYFAIR = { segments: [{ segment_id: 7, start_s: 0, end_s: 25, text: 'With Wai Faire, one night is all it takes.', text_start: 0, text_end: 42 },
  { segment_id: 8, start_s: 27, end_s: 57, text: 'Hiring? Try LinkedIn jobs.', text_start: 0, text_end: 26 }], text_complete: true, next_cursor: null };
const wayfair = (over = {}) => read({ brand: { key: 'wayfair', display: 'Wayfair', display_basis: 'none', confirmed: false },
  said: { web_addresses: ['Wai Faire.com'], promo_codes: [], address_brand: null, code_brand: null, spoken_brand: 'Wayfair' },
  candidates: [{ brand: 'wayfair', evidence: ['gliner:company', 'repair:wai faire->wayfair', 'domain'] }, { brand: 'linkedin', evidence: ['repair:linked in->linkedin'] }],
  display_span: [0, 60], ...over });

test('a repaired spelling says how it was heard, in the transcript casing, in the head and the ledger', async () => {
  const { html } = await evidence(record({ read: wayfair(), window: WAYFAIR }));
  assert.match(html, /<p class="ew-reader-heard">Heard as “Wai Faire” · shown as Wayfair<\/p>/);
  const row = ledger(html)['Brand heard in the audio'];
  assert.match(row, /^Wai Faire<div class="ew-ledger-source">From the transcript · 0:00<\/div>/);
  assert.match(row, /Shown as Wayfair, the brand’s usual spelling\.</);
  assert.doesNotMatch(row, /Spelling as transcribed/);
  assert.match(lineFor(html, 7), /Brand name first heard/);
  assert.doesNotMatch(html, /Heard as “linked/i, 'other candidates are never read');
  const confirmed = await evidence(record({ read: wayfair({ brand: { key: 'wayfair', display: 'Wayfair', display_basis: 'brand_list', confirmed: true } }), window: WAYFAIR,
    attribution: [{ brand: 'wayfair', basis: 'confirmed', channel: 'spoken' }] }));
  assert.match(confirmed.html, /<p class="ew-reader-heard">Heard as “Wai Faire” · shown as Wayfair<\/p>/);
  // PLAIN-ADS: the same words whether or not the brand was confirmed.
  assert.match(ledger(confirmed.html)['Brand heard in the audio'], /Shown as Wayfair, the brand’s usual spelling\.</, 'local');
  assert.doesNotMatch(confirmed.html, /review team/);
});

test('no repair token, a repair to another brand, or a brand under review: no heard line', async () => {
  const none = await evidence(record({ read: wayfair({ candidates: [{ brand: 'wayfair', evidence: ['domain'] }] }), window: WAYFAIR }));
  assert.doesNotMatch(none.html, /ew-reader-heard/);
  assert.match(ledger(none.html)['Brand heard in the audio'], /Spelling as transcribed\./);
  const other = await evidence(record({ read: wayfair({ candidates: [{ brand: 'wayfair', evidence: ['repair:wai faire->waifair'] }] }), window: WAYFAIR }));
  assert.doesNotMatch(other.html, /ew-reader-heard/);
  const hidden = await evidence(record({ read: wayfair({ brand: { ...UNDER_REVIEW } }), window: WAYFAIR }));
  assert.doesNotMatch(hidden.html, /ew-reader-heard|Wai Faire”/);
});

/* -- R2-12 / UX-13: caveat before the actions, facts before the words -- */
// PLAIN-ADS (DEP7): only a held ad has a caveat; an unreviewed or a
// confirmed ad has none.
test('the held caveat comes before the primary action; any other ad has none', async () => {
  const { html } = await evidence(record({ read: read({ brand: { ...UNDER_REVIEW }, needs_review: true, said: undefined, candidates: undefined }) }));
  const caveat = html.indexOf('<p class="ew-reader-caveat">We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand. Know the brand? Use <button type="button" class="text-link" data-action="goto-report">Report a mistake</button>.</p>');
  assert.ok(caveat > 0, 'caveat shown');
  assert.ok(caveat < html.indexOf('data-action="save-excerpt"'), 'before Save excerpt');
  const plain = await evidence(record());
  assert.doesNotMatch(plain.html, /ew-reader-caveat/);
  const ok = await evidence(record({ status: 'confirmed', attribution: [{ brand: 'betterhelp', basis: 'confirmed', channel: 'spoken' }],
    read: read({ brand: { ...read().brand, confirmed: true } }) }));
  assert.doesNotMatch(ok.html, /ew-reader-caveat/);
});

test('the facts come before the words in the page order; the grid knows it', async () => {
  const { html } = await evidence(record());
  assert.match(html, /<div class="ew-reader-grid has-facts"><section class="ew-card ew-facts-card">/);
  assert.ok(html.indexOf('Each fact and its source') < html.indexOf('What was said'));
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  assert.match(css, /\.ew-reader-grid\.has-facts \{ grid-template-areas: "facts" "said" "side"; grid-template-rows: none; \}/, 'phone order');
  const passage = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined })));
  assert.doesNotMatch(passage.html, /has-facts/, 'the passage reader keeps the plain grid');
});

test('Jump to the brand name focuses its line', async () => {
  const t = await evidence(record());
  const line = { tabIndex: 0, scrolled: null, focused: null, scrollIntoView(o) { this.scrolled = o; }, focus(o) { this.focused = o; } };
  const seen = [];
  t.win.document.querySelector = sel => { seen.push(sel); return sel === '.ew-said [data-segment="43"]' ? line : null; };
  t.win.matchMedia = () => ({ matches: true });
  await t.win.EWViews.handleAction({ dataset: { action: 'jump-words', segment: '43' } });
  assert.equal(line.tabIndex, -1);
  assert.deepEqual({ ...line.scrolled }, { block: 'start', behavior: 'auto' }, 'instant under reduced motion');
  assert.deepEqual({ ...line.focused }, { preventScroll: true });
  const head = { focus() { this.f = true; }, scrollIntoView() {} };
  t.win.document.querySelector = sel => (sel === '#ew-said-h' ? head : null);
  await t.win.EWViews.handleAction({ dataset: { action: 'jump-words' } });
  assert.equal(head.f, true, 'no brand line: the words heading');
});

/* -- R2-03: Create alert from this, pre-scoped -- */
test('Create alert from this read opens on its brand ads', async () => {
  const t = await evidence(record());
  assert.match(t.html, /data-action="alert-evidence"[^>]*data-brand="betterhelp" data-brand-name="BetterHelp" data-show="cs-upfirst"/);
  const btn = /<button[^>]*data-action="alert-evidence"[^>]*>/.exec(t.html)[0];
  const dataset = { action: 'alert-evidence' };
  for (const m of btn.matchAll(/data-([a-z-]+)="([^"]*)"/g)) dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[2];
  await t.win.EWViews.handleAction({ dataset }); await settle();
  assert.match(t.modal.innerHTML, /<option value="ads" selected>Ads<\/option>/);
  assert.match(t.modal.innerHTML, /id="alert-brand" name="brand" maxlength="120" value="betterhelp"/);
  assert.match(t.modal.innerHTML, /id="alert-name" name="name" required maxlength="120" value="BetterHelp ads"/);
  assert.match(t.modal.innerHTML, /id="alert-scope"[^>]*>[^<]*Ads · brand betterhelp/);
});

test('a brand under review alerts on the show, never a guessed name; a passage on its searched words', async () => {
  const hidden = await evidence(record({ read: read({ brand: { ...UNDER_REVIEW }, needs_review: true, said: undefined }) }));
  const btn = /<button[^>]*data-action="alert-evidence"[^>]*>/.exec(hidden.html)[0];
  assert.doesNotMatch(btn, /data-brand/);
  await hidden.win.EWViews.handleAction({ dataset: { action: 'alert-evidence', show: 'cs-upfirst', showName: 'Up First' } }); await settle();
  assert.match(hidden.modal.innerHTML, /id="alert-brand" name="brand" maxlength="120" value=""/);
  assert.match(hidden.modal.innerHTML, /<option value="cs-upfirst" selected>/);
  // D-5: a show with no brand and no words watches its new episodes.
  assert.match(hidden.modal.innerHTML, /<option value="episodes" selected>Episodes<\/option>/);
  assert.match(hidden.modal.innerHTML, /value="Up First new episodes"/);
  assert.doesNotMatch(hidden.modal.innerHTML, /betterhelp/i);
  const said = { segments: [{ segment_id: 50, start_s: 240, end_s: 250, text: 'Start a free trial today.', text_start: 0, text_end: 25 }], text_complete: true, next_cursor: null };
  const passage = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined, window: said })), { q: 'free trial' });
  assert.match(passage.html, /data-action="alert-evidence"[^>]*data-kind="passage"/);
  // The words come with the passage that says them (F-19); stale words: frontend_dv3_ux_fixA.
  assert.match(passage.html, /data-action="alert-evidence"[^>]*data-q="free trial"/);
  await passage.win.EWViews.handleAction({ dataset: { action: 'alert-evidence', kind: 'passage', show: 'cs-upfirst', q: 'free trial' } }); await settle();
  assert.match(passage.modal.innerHTML, /<option value="mentions" selected>All mentions<\/option>/);
  assert.match(passage.modal.innerHTML, /id="alert-q" name="q" maxlength="200" value="free trial"/);
});

test('PERF-R1-05 a passage carries the searched words only when it says them as whole words', async () => {
  const win = w => ({ segments: [{ segment_id: 50, start_s: 240, end_s: 250, text: w, text_start: 0, text_end: w.length }], text_complete: true, next_cursor: null });
  const metal = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined, window: win('Heavy metal music today.') })), { q: 'Meta' });
  assert.match(metal.html, /data-action="alert-evidence"[^>]*data-kind="passage"/);
  assert.doesNotMatch(/<button[^>]*data-action="alert-evidence"[^>]*>/.exec(metal.html)[0], /data-q=/);
  const meta = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined, window: win("Meta's Muse launched.") })), { q: 'Meta' });
  assert.match(meta.html, /data-action="alert-evidence"[^>]*data-q="Meta"/);
});

/* -- UX-05 / R2-04: Add to campaign and Copy link -- */
test('Add to campaign is on a current evidence page only; Copy link on every reader', async () => {
  const cur = await evidence(record());
  assert.match(cur.html, /data-action="add-to-campaign" data-ref="sr1:c3c3[^"]*" data-episode="ep_ux" data-show="cs-upfirst" data-day="2026-10-07"/);
  assert.match(cur.html, /data-action="copy-read-link" data-ref="sr1:c3c3/);
  // CR-SCR-R1-04: the shown brand names the ad in Remove from campaign; a brand under review never.
  assert.match(cur.html, /data-action="add-to-campaign"[^>]*data-brand-name="BetterHelp"/);
  // ... and by the time the ad starts, as the page shows it.
  assert.match(cur.html, /data-action="add-to-campaign"[^>]*data-at="\d+:\d{2}(:\d{2})?"/);
  const review = await evidence(record({ read: read({ brand: { ...UNDER_REVIEW, key: 'betterhelp', display: 'BetterHelp' }, needs_review: true, said: undefined }) }));
  assert.doesNotMatch(/<button[^>]*data-action="add-to-campaign"[^>]*>/.exec(review.html)[0], /data-brand/);
  const old = await evidence(record({ historical: true, relationship: 'historical', read: { historical: true, current_ref: 'sr1:new' } }));
  assert.doesNotMatch(old.html, /add-to-campaign/);
  const passage = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined })));
  assert.doesNotMatch(passage.html, /add-to-campaign/);
  assert.match(passage.html, /data-action="copy-read-link"/);
});

const campaignsApi = ({ attached = [], fail412 = false } = {}) => (p, opts) => {
  if (p === '/customer/campaigns') {
    return { campaigns: [
      { id: 3, name: 'Fall sleep push', mode: 'supplied_requirements', availability: 'active', revision: 4 },
      { id: 4, name: 'Old one', mode: 'supplied_requirements', availability: 'legacy_review_required', revision: 1 },
      { id: 5, name: 'Market scan', mode: 'research', availability: 'active', revision: 2 }] };
  }
  if (p === '/customer/campaigns/3/draft') {
    return { draft: { campaign: { id: 3, revision: 5 }, episode_titles: { ep_ux: 'Tuesday' },
      placements: [
        { id: 'exp:a', scope: 'show_window', show_id: 'cs-upfirst', date_from: '2026-10-01', date_to: '2026-10-31' },
        { id: 'exp:b', scope: 'episode', episode_id: 'ep_ux' },
        { id: 'exp:c', scope: 'show_window', show_id: 'cs-other', date_from: '2026-10-01', date_to: '2026-10-31' },
        { id: 'exp:d', scope: 'show_window', show_id: 'cs-upfirst', date_from: '2026-11-01', date_to: '2026-11-30' }],
      pins: { attachments: { 'exp:b': attached } } } };
  }
  if (p.startsWith('/customer/campaigns/4') || p.startsWith('/customer/campaigns/5')) throw new Error('only active supplied campaigns are read: ' + p);
  if (p === '/customer/campaigns/3/placements/exp%3Aa/evidence' && opts.method === 'POST') {
    if (fail412) { const e = new Error('stale revision; current is 6'); e.status = 412; e.body = { error: { code: 'CONFLICT' } }; return e; }
    return { campaign_revision: 6, ref: REF };
  }
  return undefined;
};
const readData = { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst', day: '2026-10-07' };

test('the campaign picker lists only covering placements, counts the rest and marks one already attached', async () => {
  const t = await evidence(record(), {}, campaignsApi({ attached: [REF] }));
  await t.win.EWViews.handleAction({ dataset: readData }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /<input type="radio" name="choice" value="3\|exp:a" checked autofocus><span><b>Fall sleep push<\/b> · Show window 1 · Oct 1–Oct 31, 2026<\/span>/);
  assert.match(m, /value="3\|exp:b" disabled><span><b>Fall sleep push<\/b> · Episode: Tuesday · already attached<\/span>/);
  assert.doesNotMatch(m, /exp:c|exp:d|Old one/);
  assert.match(m, /2 other placements don’t cover this episode or date\./);
  assert.match(m, /Research campaigns can’t hold evidence yet; that is planned\./);
  assert.match(m, /it is never verification/);
  assert.match(m, /<form id="attach-to-campaign"/);
  assert.match(m, /type="submit">Add to placement</);
});

test('no covering placement: the empty state and no submit', async () => {
  const t = await evidence(record(), {}, campaignsApi());
  await t.win.EWViews.handleAction({ dataset: { ...readData, show: 'cs-none', episode: 'ep_none' } }); await settle();
  assert.match(t.modal.innerHTML, /No campaign placement covers this episode\./);
  assert.match(t.modal.innerHTML, /href="#\/campaigns"[^>]*>Go to Campaigns</);
  assert.doesNotMatch(t.modal.innerHTML, /type="submit"/);
});

function formFrom(win, html, choice) {
  const hidden = {};
  for (const m of html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)) hidden[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  const entries = { ...hidden, choice };
  win.FormData = class { constructor() { this.e = Object.entries(entries); } *entries() { yield* this.e; } get(k) { return entries[k]; } };
  return { getAttribute: k => (k === 'id' ? 'attach-to-campaign' : null), dataset: {}, querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, elements: [] };
}

test('Add to placement posts exactly {ref, expected_revision} and stays on the read with an Open campaign link', async () => {
  const t = await evidence(record(), {}, campaignsApi());
  await t.win.EWViews.handleAction({ dataset: readData }); await settle();
  await t.win.EWViews.handleSubmit(formFrom(t.win, t.modal.innerHTML, '3|exp:a')); await settle();
  const post = t.calls.filter(c => c[0] === 'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(post)), [['POST', '/customer/campaigns/3/placements/exp%3Aa/evidence', { ref: REF, expected_revision: 5 }]]);
  assert.match(t.modal.innerHTML, /Added to Fall sleep push · Show window 1 · Oct 1–Oct 31, 2026\./);
  assert.match(t.modal.innerHTML, /does not by itself prove the ad ran as agreed/);
  assert.match(t.modal.innerHTML, /href="#\/campaign\/3"[^>]*>Open campaign</);
  // d16 decision 14: the dialog header's Close is the only Close (the footer one is gone).
  assert.equal((t.modal.innerHTML.match(/data-action="close-dialog"[^>]*>Close</g) || []).length, 1);
});

test('a changed campaign asks to choose again from fresh placements', async () => {
  const t = await evidence(record(), {}, campaignsApi({ fail412: true }));
  await t.win.EWViews.handleAction({ dataset: readData }); await settle();
  await t.win.EWViews.handleSubmit(formFrom(t.win, t.modal.innerHTML, '3|exp:a')); await settle();
  assert.match(t.modal.innerHTML, /The campaign changed\. Choose the placement again\./);
  assert.match(t.modal.innerHTML, /name="choice" value="3\|exp:a"/);
  assert.equal(t.calls.filter(c => c[1] === '/customer/campaigns/3/draft').length, 2, 're-read');
});

test('Copy link copies the pinned evidence URL and says where it opens', async () => {
  for (const hosted of [false, true]) {
    const t = await evidence(record(), { hosted });
    const wrote = [];
    t.win.navigator = { clipboard: { writeText: async u => { wrote.push(u); } } };
    await t.win.EWViews.handleAction({ dataset: { action: 'copy-read-link', ref: REF } });
    assert.equal(wrote.length, 1);
    assert.ok(wrote[0].endsWith('#/evidence/' + encodeURIComponent(REF)), wrote[0]);
    assert.equal(t.toasts.at(-1), hosted ? 'Link copied. It opens this exact version of the evidence for members of this workspace.'
      : 'Link copied. It opens this exact version of the evidence in this workspace on this computer.');
  }
  const t = await evidence(record());
  t.win.navigator = { clipboard: { writeText: async () => { throw new Error('denied'); } } };
  t.win.document.execCommand = () => { throw new Error('no'); };
  t.win.document.body = { append() {} };
  t.win.document.createElement = () => ({ select() {}, remove() {} });
  await t.win.EWViews.handleAction({ dataset: { action: 'copy-read-link', ref: REF } });
  assert.equal(t.toasts.at(-1), 'Copy didn’t work. Open the evidence and copy the address from the browser.');
});

test('the Search inspector has Add to campaign for an ad, not a mention, and Copy link for both', async () => {
  const ad = { ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one', episode_id: 'ep_1',
    show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep', quotation_span: [60, 120],
    read: { brand: { key: 'ag1', display: 'AG1', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false } };
  const mention = { ...ad, ref: 'sr1:m', record_kind: 'transcript_passage', status: 'mention', read: null };
  for (const [item, ads] of [[ad, true], [mention, false]]) {
    const t = await page('#/search?mode=ads&q=sleep&view=list', p => {
      if (p === '/customer/brands') return { brands: [] };
      if (p === '/customer/shows') return { shows: [] };
      if (p === '/customer/research-query') return { query: { version: 2, mode: 'ads', q: 'sleep', sort: 'newest', page: 1 }, revision: 'r',
        result: { items: [item], total: 1, per_page: 10, total_relation: 'exact', dated_total: 1, undated_total: 0 }, coverage: {}, summary: { week: [] } };
      throw new Error('unexpected ' + p);
    });
    const inspector = (t.html.match(/<aside class="ew-inspector"[\s\S]*?<\/aside>/) || [''])[0];
    assert.ok(inspector, 'inspector rendered');
    if (ads) assert.match(inspector, /data-action="add-to-campaign" data-ref="sr1:a" data-episode="ep_1" data-show="cs-1" data-day="2026-10-09"/);
    else assert.doesNotMatch(inspector, /add-to-campaign/);
    assert.match(inspector, new RegExp(`data-action="copy-read-link" data-ref="${item.ref}"`));
  }
});

/* -- R2-15c: Save excerpt on each Episode ad row -- */
test('each Episode ad row with a ref has Save excerpt; one without has none', async () => {
  const ad = (ref, at) => ({ ref, evidence_id: 'e-' + at, status: 'finding', span: [at, at + 30], display_span: [at, at + 30],
    brand: { key: 'b' + at, display: 'Brand ' + at, confirmed: false }, needs_review: false, self_promo: false, kind: 'named',
    source_label: 'Named read', transcript_start_s: at, media: null });
  const episode = { episode_id: 'ep_ux', show_title: 'Up First', ep_title: 'Tuesday', pub_date: '2026-10-07',
    ads: [ad('sr1:one', 60), ad('sr1:two', 400), ad(null, 800)], self_promo: [], screening: null };
  const t = await page('#/episode/ep_ux', p => {
    if (p === '/customer/episodes/ep_ux') return { episode };
    if (p.startsWith('/customer/records/')) return { record: { ref: 'sr1:ep', record_kind: 'episode', episode: { episode_id: 'ep_ux', ep_title: 'Tuesday' }, window: { segments: [] } } };
    return { ok: true };
  });
  const saves = [...t.html.matchAll(/data-action="save-excerpt"[^>]*data-ref="([^"]*)"/g)].map(m => m[1]);
  assert.deepEqual(saves.filter(r => r !== 'sr1:ep'), ['sr1:one', 'sr1:two']);
});

/* -- T5 evidence copy (R2-18, UX-15) -- */
test('a failed record read says "this passage" and never echoes a 500 body; a 503 keeps its words', async () => {
  const fail = status => p => { if (p.startsWith('/customer/records/')) { const e = new Error('internal x-trace'); e.status = status; throw e; } throw new Error('unexpected ' + p); };
  const five = await page('#/evidence/' + encodeURIComponent(REF), fail(500));
  assert.match(five.html, /We couldn’t open this passage/);
  assert.match(five.html, /The service had an error\. Nothing has changed\. Try again in a moment\./);
  assert.doesNotMatch(five.html, /x-trace|this ad/);
  const busy = await page('#/evidence/' + encodeURIComponent(REF), fail(503));
  assert.match(busy.html, /The service didn’t answer: internal x-trace\. Nothing has changed\./);
});

// PLAIN-ADS (DEP7): no Name certainty or review row; Record says Ad;
// attribution names the brand and how it was named, never its basis.
test('evidence words: plain method words, Ad, whole-second footnote, one attribution entry', async () => {
  const rec = record({ attribution: [{ brand: 'betterhelp', basis: 'suggested', channel: 'spoken' }, { brand: 'betterhelp', basis: 'suggested', channel: 'url' }],
    episode: { ...record().episode, pub_date: null } });
  const { html } = await evidence(rec);
  const rows = ledger(html);
  assert.equal(rows['Brand name review'], undefined);
  assert.doesNotMatch(html, /Brand confidence|Name certainty/);
  assert.match(rows['How the time was found'], /^Found automatically</);
  assert.doesNotMatch(html, /Named read|Evidence record/);
  assert.match(html, /<dt>Record<\/dt><dd>Ad<\/dd>/);
  assert.match(html, /<dt>Attribution<\/dt><dd>BetterHelp \(heard, web address\)<\/dd>/);
  assert.doesNotMatch(html, /<dt>Review<\/dt>/);
  assert.doesNotMatch(html, /Selected 4:00|detected window/, 'PLAIN-ADS-FIX2: one time range on evidence');
  assert.match(html, /Publication date unknown/);
  assert.doesNotMatch(html, /Published Date unknown/);
  const jev = await evidence(record({ read: read({ kind: 'jev', confidence: 'high' }) }));
  assert.match(ledger(jev.html)['How the time was found'], /^Automatic ad screening</);
  const rule = await evidence(record({ read: read({ kind: 'rule' }) }));
  assert.match(ledger(rule.html)['How the time was found'], /^Automatic keyword rule</);
  const confirmed = await evidence(record({ status: 'confirmed', attribution: [{ brand: 'betterhelp', basis: 'confirmed', channel: 'spoken' }],
    read: read({ brand: { ...read().brand, confirmed: true } }) }));
  assert.match(confirmed.html, /<dt>Record<\/dt><dd>Ad<\/dd>/, 'the eyebrow word: About › Record says Ad');
  const bare = await evidence(record({ read: undefined }));
  assert.match(bare.html, /<h1 class="ew-reader-h1">Ad<\/h1>/, 'no read: "Ad", never "Evidence record"');
});

/* DEMO-SOL-R1-04: hosted, a research campaign's targets take evidence
   (an ad read or a transcript passage) through the target route. */
const researchCampaignsApi = (seen = []) => (p, opts) => {
  seen.push(p);
  if (p === '/customer/campaigns') {
    return { campaigns: [
      { id: 3, name: 'Fall sleep push', mode: 'supplied_requirements', availability: 'active', revision: 4 },
      { id: 5, name: 'Market scan', mode: 'research', availability: 'active', revision: 2 }] };
  }
  if (p === '/customer/campaigns/3/draft') {
    return { draft: { campaign: { id: 3, revision: 5 }, placements: [{ id: 'exp:a', scope: 'show_window', show_id: 'cs-upfirst' }], pins: {} } };
  }
  // DEMO2-R1-01: a research campaign is read by its targets, never a draft.
  if (p === '/customer/campaigns/5/draft') throw new Error('no draft build for a research campaign');
  if (p === '/customer/campaigns/5/targets') {
    return { ok: true, targets: [{ id: 'ct-1', scope: 'show_window', show_id: 'cs-upfirst', evidence: [] },
      { id: 'ct-2', scope: 'show_window', show_id: 'cs-other', evidence: [] }] };
  }
  if (p === '/customer/campaigns/3/evidence-fit?ref=' + encodeURIComponent(REF)) return { placements: { 'exp:a': null } };
  if (p === '/customer/campaigns/5/targets/ct-1/evidence' && opts.method === 'POST') return { target_id: 'ct-1', ref: REF, campaign_revision: 8 };
  return undefined;
};

test('DEMO-SOL-R1-04 hosted: an ad read can go to a research target; it posts to the target route', async () => {
  const seen = [];
  const t = await evidence(record(), { hosted: true }, researchCampaignsApi(seen));
  await t.win.EWViews.handleAction({ dataset: { ...readData, showName: 'Up First' } }); await settle();
  assert.ok(!seen.includes('/customer/campaigns/5/draft'), 'no draft for a research campaign');
  const m = t.modal.innerHTML;
  assert.match(m, /value="3\|exp:a"/);
  assert.match(m, /<input type="radio" name="choice" value="5\|ct-1"><span><b>Market scan<\/b> · Up First · research observation<\/span>/);
  assert.doesNotMatch(m, /ct-2|can’t hold evidence|planned/);
  await t.win.EWViews.handleSubmit(formFrom(t.win, m, '5|ct-1')); await settle();
  const post = t.calls.filter(c => c[0] === 'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(post)), [['POST', '/customer/campaigns/5/targets/ct-1/evidence', { ref: REF, expected_revision: 2 }]]);
  assert.match(t.modal.innerHTML, /Added to Market scan · Up First\./);
  assert.match(t.modal.innerHTML, /href="#\/campaign\/5"[^>]*>Open campaign</);
});

test('FREEZE r4 hosted: adding to a research target says it is kept, with no possible-ad label', async () => {
  const t = await evidence(record(), { hosted: true }, researchCampaignsApi([]));
  await t.win.EWViews.handleAction({ dataset: { ...readData, showName: 'Up First' } }); await settle();
  await t.win.EWViews.handleSubmit(formFrom(t.win, t.modal.innerHTML, '5|ct-1')); await settle();
  const text = t.modal.innerHTML.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
  assert.match(text, /Added to Market scan · Up First\. Kept as a research observation\. Open campaign/);
  assert.doesNotMatch(text, /possible/i);
});

test('DEMO-SOL-R1-04 hosted: a transcript passage offers Add to campaign for research targets only', async () => {
  const seen = [];
  const passage = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined }), researchCampaignsApi(seen)));
  // Hosted, as a signed-in editor of the active workspace (a judge).
  Object.assign(passage.win.EW.current, { hosted: true, principal: 'oidc:https://i.example:judge', workspaceRole: 'editor', activeWorkspace: 'judges-demo' });
  await passage.win.EWViews.render();
  assert.match(passage.main.innerHTML, /data-action="add-to-campaign" data-ref="sr1:c3c3[^"]*"[^>]*data-show-name="Up First"[^>]*data-kind="passage"/);
  await passage.win.EWViews.handleAction({ dataset: { ...readData, kind: 'passage', showName: 'Up First' } }); await settle();
  const m = passage.modal.innerHTML;
  assert.match(m, /value="5\|ct-1"/);
  assert.doesNotMatch(m, /exp:a/, 'a placement takes ad reads only');
  assert.ok(!seen.includes('/customer/campaigns/3/draft'), 'no supplied draft is read for a passage');
  const local = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined })));
  assert.doesNotMatch(local.html, /add-to-campaign/, 'the local app keeps passages out');
});

/* DEMO2 code r2: R2-01 already attached, R2-02 episode target name, R2-04 failed-load words. */
const withTargets = (targets, base = researchCampaignsApi()) => (p, opts) =>
  (p === '/customer/campaigns/5/targets' ? { ok: true, targets } : base(p, opts));

test('DEMO2-R2-01 a research target that already holds the read is shown attached and disabled', async () => {
  const t = await evidence(record(), { hosted: true }, withTargets([{ id: 'ct-1', scope: 'show_window', show_id: 'cs-upfirst', evidence: [REF] }]));
  await t.win.EWViews.handleAction({ dataset: { ...readData, showName: 'Up First' } }); await settle();
  assert.match(t.modal.innerHTML, /value="5\|ct-1" disabled><span><b>Market scan<\/b> · Up First · research observation · already attached<\/span>/);
});

test('DEMO2-R2-02 an episode target is named from the read\'s episode title', async () => {
  const t = await evidence(record(), { hosted: true }, withTargets([{ id: 'ct-e', scope: 'episode', episode_id: 'ep_ux', evidence: [] }]));
  await t.win.EWViews.handleAction({ dataset: { ...readData, episodeName: 'Tuesday' } }); await settle();
  assert.match(t.modal.innerHTML, /value="5\|ct-e"><span><b>Market scan<\/b> · Episode: Tuesday · research observation<\/span>/);
});

test('DEMO2-R2-04 a research campaign that fails to load is not called placements only', async () => {
  const failing = (p, opts) => {
    if (p === '/customer/campaigns/5/targets') { const e = new Error('boom'); e.status = 500; return e; }
    return researchCampaignsApi()(p, opts);
  };
  const t = await evidence(record(), { hosted: true }, failing);
  await t.win.EWViews.handleAction({ dataset: readData }); await settle();
  assert.match(t.modal.innerHTML, /1 campaign didn’t load, so its placements or research targets are not listed\./);
});

/* M11-PLAYER-FIX2 (LEDGER-LEAD3-CS-3, player part): the server caps a
   historical ref at the current read's display span, and its read block
   carries that span (read.display_span, CLIP-STOP-FIX3). The page's clip
   plays it, not the old ref's stored bounds, so it never runs past the
   cap. With no display span (the evidence row is gone), the stored bounds
   as before. The page still shows no current facts. */
test('M11-PLAYER-FIX2 CS-3: a historical record plays the current read\'s display span; without one, its stored bounds', async () => {
  const historical = over => record({ relationship: 'historical', historical: true,
    media: { playback: 'local', kind: 'evidence_clip', source_offset_s: 200, alignment: 'verified', timing: 'verified', trust: 'exact' },
    read: { historical: true, current_ref: 'sr1:' + 'd'.repeat(32), ...over } });
  const player = html => (/<section class="ew-card ew-player ew-clip"[^>]*>/.exec(html) || [''])[0];
  const { html } = await evidence(historical({ display_span: [250.5, 270] }));
  assert.match(player(html), / data-start="250.5" data-end="270" data-offset="200" data-lead="47.5"/, 'the clip plays the old ref\'s bounds, past the cap');
  assert.match(html, /Earlier version of this evidence/);
  assert.match(html, /Open the current version →/);
  assert.doesNotMatch(html, /BetterHelp/, 'a historical page showed the current read\'s brand');
  const gone = await evidence(historical({}));
  assert.match(player(gone.html), / data-start="240" data-end="309.24"/, 'no display span: the stored bounds');
});
