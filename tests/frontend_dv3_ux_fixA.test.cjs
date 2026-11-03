/* Design v3 UX review, fix pass A (review findings F-01..F-07, F-19, F-21,
   F-24 Search/Evidence items). Rules held: an unconfirmed name is never
   stated as an advertiser, "Ad read" only on a confirmed ad, the heard
   spelling only when the spoken brand is the shown brand (spoken brand
   and web address stay separate facts), Brand not identified unchanged.
   Loader copied from frontend_dv3_ux_evidence.test.cjs.
   Run: node --test tests/frontend_dv3_ux_fixA.test.cjs */
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
    location: { hash: route, href: 'http://127.0.0.1:9172/app/' + route, origin: 'http://127.0.0.1:9172', pathname: '/app/' },
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
  const rerender = async () => { await win.EWViews.render(); return main.innerHTML; };
  return { win, html: main.innerHTML, main, modal, toasts, calls, rerender };
}
function ledger(html) {
  const rows = {};
  for (const m of html.matchAll(/<th scope="row">([^<]*)<\/th><td[^>]*>([\s\S]*?)<\/td><\/tr>/g)) rows[m[1]] = m[2];
  return rows;
}
const datasetOf = (html, action) => {
  const btn = new RegExp(`<button[^>]*data-action="${action}"[^>]*>`).exec(html)[0];
  const d = { action };
  for (const m of btn.matchAll(/data-([a-z-]+)="([^"]*)"/g)) d[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[2];
  return d;
};

/* -- Search fixtures -------------------------------------------------------- */
const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'whole', display: 'Whole', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const bundle = (mode, items, q = 'whole') => ({ query: { version: 2, mode, q, sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
const searchApi = (b, brands = []) => p => {
  if (p === '/customer/brands') return { brands };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  throw new Error('unexpected ' + p);
};
const asideOf = html => (html.match(/<aside class="ew-inspector"[\s\S]*?<\/aside>/) || [''])[0];
const jumpOf = html => (html.match(/<p class="ew-brand-jump">[\s\S]*?<\/p>/) || [''])[0];
const hrefOf = (html, text) => html.match(new RegExp(`href="([^"]+)">${text}`))[1].replace(/&amp;/g, '&');

/* -- F-01, replaced by PLAIN-ADS (DEP7): every directory brand is a brand
   heard in ads; no "Name not confirmed" tag, whatever its confirmed count. -- */
test('F-01 a directory name with no confirmed ad reads like any other brand', async () => {
  for (const entry of [{ key: 'whole', brand: 'whole', display: 'Whole', records: 40, confirmed: 0 },
    { key: 'whole', brand: 'whole', display: 'Whole', records: 40 }]) {
    const t = await page('#/search?mode=ads&q=whole', searchApi(bundle('ads', [adItem()]), [entry]));
    const jump = jumpOf(t.html);
    assert.ok(jump, 'the jump renders');
    assert.match(jump, /Whole is a brand heard in ads\. <a class="text-link" href="#\/brand\/whole">See where Whole advertises<\/a>/);
    assert.doesNotMatch(jump, /Name not confirmed|not confirmed|ew-tag-uncertain/);
  }
});

test('F-01 a confirmed directory name keeps "See where X advertises" with no uncertain tag', async () => {
  const t = await page('#/search?mode=ads&q=betterhelp', searchApi(bundle('ads', [adItem()], 'betterhelp'),
    [{ key: 'betterhelp', brand: 'betterhelp', display: 'BetterHelp', records: 21, confirmed: 3 }]));
  const jump = jumpOf(t.html);
  assert.match(jump, /BetterHelp is a brand heard in ads\. <a class="text-link" href="#\/brand\/betterhelp">See where BetterHelp advertises<\/a>/);
  assert.doesNotMatch(jump, /Name not confirmed|not confirmed/);
});

/* -- F-05: "Show only ads named X" lands on Ads, even from All mentions -- */
test('F-05 the brand filter link from All mentions opens Ads with the brand and no words', async () => {
  const entry = { key: 'whole', brand: 'whole', display: 'Whole', records: 40, confirmed: 0 };
  for (const mode of ['mentions', 'ads']) {
    const t = await page(`#/search?mode=${mode}&q=whole`, searchApi(bundle(mode, [adItem()]), [entry]));
    const p = new URLSearchParams(hrefOf(jumpOf(t.html), 'Show only ads named Whole').split('?')[1]);
    assert.equal(p.get('mode'), 'ads', mode);
    assert.equal(p.get('q'), null, mode);
    assert.match(p.get('brands'), /whole/, mode);
  }
});

/* -- F-06 and F-24, replaced by PLAIN-ADS (DEP7): a name the namer flagged
   carries no flag on the row, the inspector, the sheet or in the
   accessible names; a held brand still shows "Brand not identified". -- */
const needsRead = (over = {}) => ({ brand: { key: 'whole', display: 'Whole', confirmed: false }, needs_review: true, display_span: [62, 121], has_clip: false, ...over });
test('F-06 a flagged name carries no tag in the list row; a held brand shows the held tag', () => {
  const ui = loadAt('#/search').EWUI;
  const row = read => ui.resultRow(adItem({ read }), { index: 0 });
  assert.match(row(needsRead()), /<span class="ew-row-brand"><span class="ew-row-brand-name"><span class="ew-brand">Whole<\/span><\/span><\/span>/);
  assert.doesNotMatch(row(needsRead()), /Needs review|Name not confirmed/);
  const review = row(needsRead({ brand: { ...UNDER_REVIEW } }));
  // PLAIN-ADS-FIX1 (screen R1-02): the held brand is plain text.
  assert.match(review, /<span class="ew-brand-none">Brand not identified<\/span>/);
  assert.doesNotMatch(review, /data-tag="review"/);
  assert.doesNotMatch(review, /Needs review|Whole/, 'Brand not identified unchanged');
});

test('F-06 the inspector heading, its live title and the phone sheet name the brand with no flag', async () => {
  const t = await page('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem({ read: needsRead() })], 'sleep')));
  const aside = asideOf(t.html);
  assert.match(aside, /<h2><span class="ew-brand">Whole<\/span><\/h2>/);
  assert.match(aside, /data-inspector-live>Selected: Whole, Show one</);
  assert.doesNotMatch(aside, /Name not confirmed|Needs review/);
  const modal = { open: false, innerHTML: '', classList: { add() {}, remove() {} }, addEventListener() {} };
  t.win.matchMedia = () => ({ matches: false });
  t.win.document.querySelector = s => (s === '#modal' ? modal : null);
  t.win.EW.openDialog = (title, html) => { modal.innerHTML = html; };
  await t.win.EWViews.handleAction({ dataset: { action: 'inspect', index: '0' }, closest: () => null });
  assert.match(modal.innerHTML, /Whole/);
  assert.doesNotMatch(modal.innerHTML, /Needs review/);
  const hidden = await page('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem({ read: needsRead({ brand: { ...UNDER_REVIEW } }) })], 'sleep')));
  assert.doesNotMatch(asideOf(hidden.html), /Needs review|Whole/, 'Brand not identified unchanged');
});

test('F-24 row controls name the brand with no flag; preview labels never promise a panel', () => {
  const ui = loadAt('#/search').EWUI;
  const row = ui.resultRow(adItem(), { index: 0 });
  assert.match(row, /data-action="inspect"[^>]*aria-label="Preview Whole, at 1:02–2:01"/);
  assert.match(row, /aria-label="Open evidence: Whole, 1:02–2:01"/);
  assert.match(ui.resultRow(adItem({ read: needsRead() }), { index: 0 }), /aria-label="Open evidence: Whole, 1:02–2:01"/);
  const card = ui.sourceCard(adItem(), { query: 'sleep', index: 0 });
  for (const html of [row, card]) assert.doesNotMatch(html.replace(/<span class="ew-inspect-shown">[^<]*<\/span>/g, ''), /in the panel/);
  assert.match(card, /data-action="inspect"[^>]*aria-label="Preview this result"/);
});

/* -- F-21: chart peak, match wording, the settings note, the sheet class -- */
// Row 1: an ads chart counts the ads its cells carry (here one ad per piece of evidence).
const cell = (from, to, count) => ({ key: from, label: `${from}–${to}`, count, breaks: count, interval: { from, to },
  selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'summary_cell' } } });
test('F-21(1) a partial scan names the highest bar shown, not the peak week', async () => {
  const t = await page('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()], 'x')));
  const V = t.win.EWViews;
  const cells = [cell('2026-09-28', '2026-10-04', 2), cell('2026-10-05', '2026-10-11', 5)];
  const low = V.weekChart(cells, { mode: 'ads' }, { total_relation: 'lower_bound' });
  assert.match(low, /highest shown: at least 5 ads/);
  assert.doesNotMatch(low, /most:/);
  assert.match(V.weekChart(cells, { mode: 'ads' }, { total_relation: 'exact' }), /most: 5 ads \(Oct 5\)/, 'a complete scan keeps its peak');
});

test('F-21(2) the match line says whole words match (PERF-R1-01)', async () => {
  const t = await page('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()], 'x')));
  const V = t.win.EWViews;
  assert.match(V.matchModeLine({ mode: 'ads', q: 'free trial' }), /Both words, in any order: free, trial \(whole words\)/);
  assert.match(V.matchModeLine({ mode: 'ads', q: 'a b c' }), /\(whole words\)/);
  assert.match(V.matchModeLine({ mode: 'ads', q: '"free trial" code' }), /\(whole words\)/);
  for (const q of ['free trial', 'a b c', '"free trial" code', '"free trial"']) {
    assert.doesNotMatch(V.matchModeLine({ mode: 'ads', q }), /parts of words/, q);
  }
});

test('F-21(3) the settings note stays out of the cached frame and is cleared on route change', async () => {
  const route = '#/search?mode=ads&view=list&page=1';
  const t = await page(route, searchApi(bundle('ads', [adItem()], '')));
  t.win.EW.current.searchPrefNote = route;
  const html = await t.rerender();
  assert.match(html, /class="ew-pref-note"/);
  assert.doesNotMatch(t.win.EW.current.lastSearch.html, /ew-pref-note/, 'never in the retained frame');
  t.win.EW.current.route = '#/overview';
  t.win.location.hash = '#/overview';
  t.win.EW.api = async () => { throw new Error('offline'); };
  await t.win.EWViews.render().catch(() => {});
  assert.equal(t.win.EW.current.searchPrefNote, null, 'another route clears it');
  t.win.EW.current.route = route; t.win.location.hash = route;
  t.win.EW.api = async p => searchApi(bundle('ads', [adItem()], ''))(p);
  assert.doesNotMatch(await t.rerender(), /ew-pref-note/, 'Back does not bring it back');
});

test('F-21(5) a dialog opened from the phone sheet is not a sheet', () => {
  const win = loadAt('#/search');
  const classes = new Set(['ew-sheet']);
  const modal = { innerHTML: '', classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    setAttribute() {}, showModal() {}, querySelector: () => null };
  win.document.querySelector = s => (s === '#modal' ? modal : null);
  win.setTimeout = () => {};
  win.EW.openDialog('Add to campaign', '<p>x</p>');
  assert.equal(classes.has('ew-sheet'), false);
});

/* -- Evidence fixtures ------------------------------------------------------ */
const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
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
  { segment_id: 44, start_s: 300, end_s: 309.24, text: 'Visit betterhelp.com/npr today. Start a free trial.', text_start: 0, text_end: 51 }];
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'betterhelp', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 240, end_s: 309.24 },
  episode: { episode_id: 'ep_ux', show_title: 'Up First', ep_title: 'Tuesday', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 14 },
  show: { show_identity_id: 'cs-upfirst', display_title: 'Up First', unresolved: 0 },
  evidence: { id: 'named:ep_ux.named.240000-309240', episode_id: 'ep_ux', orig_start_s: 240, orig_end_s: 309.24 }, media: { playback: 'remote', alignment: 'unknown' },
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
const kindOf = html => (html.match(/<span class="eyebrow ew-player-kind">([^<]*)<\/span>/) || [])[1];

/* -- F-02, replaced by PLAIN-ADS (DEP7): the player says "Ad" for every
   ad, never "Ad read"; checked bounds plus judgment ad keep "Ad starts here". -- */
test('F-02 every ad\'s player says "Ad"; checked bounds keep "Ad starts here"; never "Ad read"', async () => {
  const checked = { review: { judgment: 'ad', bounds_verified: 1 }, read: read({ display_span: [286.46, 309.24], display_basis: 'checked' }) };
  const possible = await evidence(record(checked));
  assert.equal(kindOf(possible.html), 'Ad');
  assert.doesNotMatch(possible.html, /Ad read|review team/);
  assert.match(possible.html, /<span class="ew-line-tag">Ad starts here<\/span>/, 'checked bounds plus judgment ad');
  const corrected = await evidence(record({ review: { judgment: 'ad', bounds_verified: 0 }, read: read({ display_span: [286.46, 309.24], display_basis: 'corrected' }) }));
  assert.equal(kindOf(corrected.html), 'Ad');
  assert.doesNotMatch(corrected.html, /Ad read|review team/);
  const confirmed = await evidence(record({ ...checked, status: 'confirmed', attribution: [{ brand: 'betterhelp', basis: 'confirmed', channel: 'spoken' }],
    read: read({ display_span: [286.46, 309.24], display_basis: 'checked', brand: { ...read().brand, confirmed: true } }) }));
  assert.equal(kindOf(confirmed.html), 'Ad');
});

/* -- F-03: the heard spelling only when the spoken brand is the shown brand -- */
const WAYFAIR = { segments: [{ segment_id: 7, start_s: 0, end_s: 25, text: 'With Wai Faire, one night is all it takes.', text_start: 0, text_end: 42 },
  { segment_id: 8, start_s: 27, end_s: 57, text: 'Hiring? Try LinkedIn jobs.', text_start: 0, text_end: 26 }], text_complete: true, next_cursor: null };
const wayfair = spoken => read({ brand: { key: 'wayfair', display: 'Wayfair', display_basis: 'none', confirmed: false },
  said: { web_addresses: ['Wai Faire.com'], promo_codes: [], address_brand: 'Wai Faire', code_brand: null, spoken_brand: spoken },
  candidates: [{ brand: 'wayfair', evidence: ['gliner:company', 'repair:wai faire->wayfair', 'domain'] }], display_span: [0, 60] });
test('F-03 no spoken brand recorded: no "Heard as"; the row says Not recorded and the address is not compared', async () => {
  const { html } = await evidence(record({ read: wayfair(null), window: WAYFAIR }));
  assert.doesNotMatch(html, /ew-reader-heard|Heard as/);
  const rows = ledger(html);
  assert.match(rows['Brand heard in the audio'], /Not recorded/);
  assert.doesNotMatch(rows['Brand heard in the audio'], /Wai Faire|changed by the brand namer/);
  assert.match(rows['Brand the address points to'], /Not compared: no spoken brand was recorded/);
});

test('F-03 a spoken brand that differs from the shown brand stays the heard fact; no "Heard as"', async () => {
  const { html } = await evidence(record({ read: wayfair('Linkedin'), window: WAYFAIR }));
  assert.doesNotMatch(html, /ew-reader-heard|Heard as/);
  const rows = ledger(html);
  assert.match(rows['Brand heard in the audio'], /^Linkedin<div class="ew-ledger-source">/);
  assert.doesNotMatch(rows['Brand heard in the audio'], /Wai Faire|changed by the brand namer/);
  assert.match(rows['Brand the address points to'], /Differs from what was heard/);
  const same = await evidence(record({ read: wayfair('Wayfair'), window: WAYFAIR }));
  assert.match(same.html, /<p class="ew-reader-heard">Heard as “Wai Faire” · shown as Wayfair<\/p>/, 'the spoken brand is the shown brand');
});

/* -- F-04: the Evidence actions wrap below 1024 px ---------------------------- */
test('F-04 the reader actions take the full row and wrap below 1024 px', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  const block = (css.match(/@media \(max-width: 1023px\) \{([\s\S]*?)\n\}/) || [])[1] || '';
  assert.match(block, /\.ew-reader-actions \{[^}]*width: 100%;[^}]*\}/);
  assert.match(block, /\.ew-reader-actions \{[^}]*flex-wrap: wrap;[^}]*\}/);
});

/* -- F-19: passage alert words, failed drafts, undated reads ------------------ */
test('F-19(1) a passage alert uses the searched words only when the passage says them', async () => {
  const passageRec = record({ record_kind: 'transcript_passage', read: undefined });
  const stale = await page('#/evidence/' + encodeURIComponent(REF), evApi(passageRec), { q: 'mattress' });
  const d = datasetOf(stale.html, 'alert-evidence');
  assert.equal(d.q, undefined, 'stale words are not carried');
  await stale.win.EWViews.handleAction({ dataset: d }); await settle();
  assert.match(stale.modal.innerHTML, /id="alert-q" name="q" maxlength="200" value=""/);
  assert.match(stale.modal.innerHTML, /<option value="cs-upfirst" selected>/, 'the show as mentions');
  const said = await page('#/evidence/' + encodeURIComponent(REF), evApi(passageRec), { q: 'free trial' });
  const e = datasetOf(said.html, 'alert-evidence');
  assert.equal(e.q, 'free trial');
  await said.win.EWViews.handleAction({ dataset: e }); await settle();
  assert.match(said.modal.innerHTML, /id="alert-q" name="q" maxlength="200" value="free trial"/);
});

const campaignsApi = ({ failDraft = false, undatedPlacement = false } = {}) => p => {
  if (p === '/customer/campaigns') {
    return { campaigns: [
      { id: 3, name: 'Fall sleep push', mode: 'supplied_requirements', availability: 'active', revision: 4 },
      { id: 6, name: 'Broken', mode: 'supplied_requirements', availability: 'active', revision: 1 }] };
  }
  if (p === '/customer/campaigns/3/draft') {
    return { draft: { campaign: { id: 3, revision: 5 }, episode_titles: { ep_ux: 'Tuesday' },
      placements: [undatedPlacement ? { id: 'exp:u', scope: 'show_window', show_id: 'cs-upfirst', date_from: null, date_to: null }
        : { id: 'exp:a', scope: 'show_window', show_id: 'cs-upfirst', date_from: '2026-10-01', date_to: '2026-10-31' }],
      pins: { attachments: {} } } };
  }
  if (p === '/customer/campaigns/6/draft') {
    if (failDraft) return new Error('the service had an error');
    return { draft: { campaign: { id: 6, revision: 1 }, placements: [], pins: { attachments: {} } } };
  }
  return undefined;
};
test('F-19(2) one campaign that fails to load does not hide the others', async () => {
  const t = await evidence(record(), {}, campaignsApi({ failDraft: true }));
  await t.win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst', day: '2026-10-07' } }); await settle();
  assert.match(t.modal.innerHTML, /name="choice" value="3\|exp:a"/);
  assert.match(t.modal.innerHTML, /1 campaign didn’t load, so its placements are not listed\./);
  assert.match(t.modal.innerHTML, /type="submit">Add to placement</);
});

test('F-19(3) an undated read fits a show placement with no dates, never a dated one', async () => {
  const undated = { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst', day: '' };
  const open = await evidence(record(), {}, campaignsApi({ undatedPlacement: true }));
  await open.win.EWViews.handleAction({ dataset: undated }); await settle();
  assert.match(open.modal.innerHTML, /name="choice" value="3\|exp:u"/);
  const dated = await evidence(record(), {}, campaignsApi());
  await dated.win.EWViews.handleAction({ dataset: undated }); await settle();
  assert.doesNotMatch(dated.modal.innerHTML, /name="choice"/);
});

/* -- F-24: copy link on a passage, unknown publication date ------------------ */
test('F-24 Copy link on a passage says "passage"; on evidence it says "evidence"', async () => {
  const passage = await page('#/evidence/' + encodeURIComponent(REF), evApi(record({ record_kind: 'transcript_passage', read: undefined })));
  const d = datasetOf(passage.html, 'copy-read-link');
  assert.equal(d.kind, 'passage');
  passage.win.navigator = { clipboard: { writeText: async () => {} } };
  await passage.win.EWViews.handleAction({ dataset: d });
  assert.equal(passage.toasts.at(-1), 'Link copied. It opens this exact version of the passage in this workspace on this computer.');
  const ad = await evidence(record());
  ad.win.navigator = { clipboard: { writeText: async () => {} } };
  await ad.win.EWViews.handleAction({ dataset: datasetOf(ad.html, 'copy-read-link') });
  assert.equal(ad.toasts.at(-1), 'Link copied. It opens this exact version of the evidence in this workspace on this computer.');
});

test('F-24 an unknown publication date says so once', async () => {
  const { html } = await evidence(record({ episode: { ...record().episode, pub_date: null } }));
  assert.match(html, /<p class="ew-reader-src">[^]*Publication date unknown<\/p>/);
  assert.doesNotMatch(html, /Published Date unknown|Published <\/p>/);
});

test('F-24 CSS: Search values added in the UX pass use tokens where one exists', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  assert.match(css, /h3\.ew-mention-title \{ font: var\(--ew-t-body\); font-weight: 600;/);
  assert.match(css, /\.ew-clip-len \{ justify-self: start; margin-left: calc\(var\(--ew-target\) \+ var\(--ew-s3\)\); \}/);
});
