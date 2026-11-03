/* Design v3 G1 (Search + Overview) render checks.
   Loads the real app scripts in a stubbed browser sandbox (same loader as
   frontend_query.test.cjs) and asserts the v3 Search and Overview pages keep
   the product rules: counts only where the API gives them, unknown stays
   unknown, Brand not identified never shows a guess, errors are never
   "no results", and the URL stays canonical over Settings preferences.
   Run: node --test tests/frontend_dv3_g1-search.test.cjs */
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
    console, URLSearchParams, AbortController,
    location: { hash: route },
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
const sessionFetch = (extra = {}) => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  if (extra[url]) return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', ...extra[url] }) };
  throw new Error('no network in unit test');
};
async function views(route, apiFn, fetchExtra = {}) {
  const win = loadAt(route, { fetch: sessionFetch(fetchExtra) });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => apiFn(p, o);
  win.location.hash = route;
  win.EW.current.route = route;
  return { win, main, render: async () => { await win.EWViews.render(); return main.innerHTML; } };
}

const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', product: 'Greens', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const bundle = (mode, items, extra = {}) => ({ query: { version: 2, mode, q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: 64, per_page: 10, total_relation: 'exact', dated_total: 64, undated_total: 0, ...extra },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
// Row 1: the server's ads (one per item here); n ads among the 64 pieces of evidence.
const AD = (items, n, extra = {}) => ({ break_total: n, break_dated_total: n, break_undated_total: 0,
  ad_breaks: items.map(x => ({ id: 'brk:' + x.ref, refs: [x.ref], read_count: 1, span: [62, 121], brands: [{ key: 'ag1', display: 'AG1', confirmed: false }] })), ...extra });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  throw new Error('unexpected ' + p);
};

test('the Ads view rides in the search URL (Ads tab only) and never splits the export selection', () => {
  const E = loadAt('#/search?mode=ads&q=x&view=list').EW;
  assert.match(E.searchURL({ mode: 'ads', q: 'x', page: 2 }), /view=list/, 'a search route keeps the view it shows');
  assert.match(E.searchURL({ mode: 'ads', q: 'x', view: 'cards' }), /view=cards/, 'an explicit view wins');
  assert.doesNotMatch(E.searchURL({ mode: 'mentions', q: 'x' }), /view=/, 'List exists on the Ads tab only');
  assert.equal(E.searchCriteria().view, undefined, 'the view is never a Query2 field');
  E.current.route = '#/brand/x';
  assert.doesNotMatch(E.searchURL({ mode: 'ads', q: 'x' }), /view=/);
});

test('bare #/search opens with the Settings tab, view and range; any query string wins', async () => {
  const prefs = { '/api/v1/customer/preferences': { preferences: { search_tab: 'ads', results_view: 'list', default_range: '30d', revision: 1 } } };
  const t = await views('#/search', searchApi(bundle('ads', [adItem()])), prefs);
  t.win.setTimeout = setTimeout;
  const went = [];
  t.win.EW.go = (r, replace) => { went.push([r, replace]); };
  const html = await t.render();
  assert.doesNotMatch(html, /Showing \d/, 'no results painted for the bare route (only the loading frame)');
  assert.equal(went.length, 1);
  assert.match(went[0][0], /^#\/search\?mode=ads&from=\d{4}-\d{2}-\d{2}.*view=list/);
  assert.equal(went[0][1], true, 'replaces the bare entry');
  const u = await views('#/search?mode=episodes&q=x', searchApi(bundle('episodes', [])), prefs);
  u.win.setTimeout = setTimeout;
  const went2 = [];
  u.win.EW.go = r => went2.push(r);
  await u.render();
  assert.deepEqual(went2, [], 'an explicit URL is canonical');
});

test('Search shows the count only on the open tab, the range line and the scope line', async () => {
  const two = [adItem(), adItem({ ref: 'sr1:b' })];
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', two, AD(two, 40))));
  const html = await t.render();
  assert.match(html, /data-mode="ads" aria-pressed="true" aria-label="Ads, 40 ads">Ads<span class="ew-tab-count" title="40 ads">40<\/span>/);
  assert.match(html, /data-mode="mentions" aria-pressed="false">All mentions<\/button>/, 'no invented count for other tabs');
  assert.match(html, /Showing 1 to 2 of 40 ads/);
  assert.match(html, /Full coverage/);
  assert.match(html, /288 episodes in scope\. Ads come from full-coverage episodes only\./);
  assert.match(html, /href="\/coverage\/">What we cover/);
  assert.match(html, /AG1 <span class="ew-brand-sep">·<\/span> <span class="ew-brand-product">Greens/);
  assert.match(html, />No clip \(transcript only\)</, 'no clip: said, no play button');
  assert.match(html, /Report a mistake/);
  assert.doesNotMatch(html, /data-action="confirm|>Confirm</, 'customers never confirm');
  assert.match(html, /<aside class="ew-inspector"[^>]*>.*Selected · Ad</s, 'the inspector shows the first row (PLAIN-ADS: "Ad")');
  assert.match(html, /data-action="inspect" data-index="1" aria-pressed="false"/, 'keyboard-selectable rows');
  // An older server with no ad count: no number on the tab or the count line.
  const old = await (await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', two)))).render();
  assert.match(old, /data-mode="ads" aria-pressed="true">Ads<\/button>/);
  assert.doesNotMatch(old, /\b64 (ads|reads)\b/);
});

test('a held brand shows "Brand not identified", never a guess, in cards, list rows and the inspector', async () => {
  const hidden = adItem({ ref: 'sr1:h', read: { brand: { ...UNDER_REVIEW }, display_span: [10, 40], has_clip: false } });
  const t = await views('#/search?mode=ads&q=sleep&view=list', searchApi(bundle('ads', [hidden])));
  const html = await t.render();
  assert.match(html, /class="ew-rows"/, 'List view');
  assert.match(html, /Brand not identified/);
  assert.doesNotMatch(html, /Not counted yet/, 'PLAIN-ADS: the list row has no status cell');
  assert.doesNotMatch(html, /alert-brand/, 'no brand alert for an unnamed read');
  const c = await views('#/search?mode=ads&q=sleep&view=cards', searchApi(bundle('ads', [hidden])));
  const cards = await c.render();
  assert.match(cards, /<span class="ew-brand-none">Brand not identified<\/span>/);
  assert.doesNotMatch(cards, /ew-tag-review/, 'PLAIN-ADS-FIX1: no amber held tag');
});

test('a failed search is never "no results"; an empty one says what was searched', async () => {
  const fail = await views('#/search?mode=ads&q=sleep', p => {
    if (p === '/customer/research-query') throw Object.assign(new Error('Failed to fetch'), { status: 0 });
    return searchApi(null)(p);
  });
  const html = await fail.render();
  assert.match(html, /Search didn’t finish/);
  assert.match(html, /Search is unavailable: Failed to fetch/);
  assert.match(html, /This is not “no results”/);
  assert.doesNotMatch(html, /No ads (found|say)/);
  assert.match(html, /ew-search-body/);
  assert.match(html, /Nothing to show until the search finishes/);
  const empty = await views('#/search?mode=ads&q=zzz', searchApi({ ...bundle('ads', []), query: { version: 2, mode: 'ads', q: 'zzz', sort: 'newest', page: 1 }, result: { items: [], total: 0, per_page: 10, total_relation: 'exact' } }));
  const e = await empty.render();
  assert.match(e, /No ads say “zzz” in this scope/);
  assert.match(e, /Search all mentions/);
  assert.match(e, /Unavailable or partial transcripts remain unknown/);
});

test('a partial scan is a notice with lower bounds, and the tab count says at least', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], AD([adItem()], 64, { total_relation: 'lower_bound', truncation_reason: 'max_rows' }))));
  const html = await t.render();
  assert.match(html, /ew-notice-partial/);
  assert.match(html, /<b>Partial<\/b>/);
  assert.match(html, /ew-tab-count" title="at least 64 ads">≥64</);
  assert.match(html, /of at least 64 ads/);
});

test('Export offers the backend scopes and formats; Create file runs the existing export with the chosen format', async () => {
  const posts = [];
  const t = await views('#/search?mode=ads&q=sleep', p => {
    if (p === '/customer/research-export') return { export_id: 9, total: 64 };
    return searchApi(bundle('ads', [adItem()]))(p);
  });
  await t.render();
  let dialog = '';
  t.win.EW.openDialog = (title, body, footer) => { dialog = title + body + footer; };
  await t.win.EWViews.handleAction({ dataset: { action: 'export-open' } });
  assert.match(dialog, /Export ads/);
  assert.match(dialog, /value="export-selected" disabled/, 'no selection yet');
  assert.match(dialog, /All matches <span class="meta" id="export-all-count">\(64 pieces of evidence\)<\/span>/);
  assert.match(dialog, /<option value="csv">CSV \(spreadsheet\)<\/option><option value="json">JSON<\/option><option value="html">Printable page<\/option>/);
  assert.match(dialog, /data-action="export-all" id="export-run"/);
  t.win.EW.api = async (p, o) => { posts.push([p, o && o.body]); return { export_id: 9, total: 64 }; };
  t.win.EW.toast = () => {};
  t.win.EW.closeDialog = () => {};
  t.win.location.href = '';
  const sel = { value: 'json' };
  t.win.document.querySelector = s => (s === '#export-format' ? sel : null);
  await t.win.EWViews.handleAction({ dataset: { action: 'export-all' }, closest: () => null });
  assert.equal(posts[0][0], '/customer/research-export');
  assert.equal(posts[0][1].format, 'json');
  assert.equal(posts[0][1].scope, 'all_matches');
  assert.equal(posts[0][1].expected_view_revision, 'rev-1');
});

const overviewData = (over = {}) => ({ coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [], ...over });
const overviewApi = ({ overview = overviewData(), saved = { items: [] }, brands = [], search = { items: [] } } = {}) => p => {
  if (p === '/customer/overview') { if (overview instanceof Error) throw overview; return overview; }
  if (p.startsWith('/customer/saved-items')) { if (saved instanceof Error) throw saved; return saved; }
  if (p === '/customer/brands') return { brands };
  if (p.startsWith('/customer/search')) return search;
  throw new Error('unexpected ' + p);
};

test('Overview: nothing tracked is the first-use card; no KPI band or invented numbers', async () => {
  const t = await views('#/overview', overviewApi());
  const html = await t.render();
  assert.match(html, /You’re not tracking anything yet/);
  assert.match(html, /Go to Search/);
  assert.match(html, /data-action="overview-alert"/);
  assert.match(html, /Browse brands/);
  assert.doesNotMatch(html, /Alert activity/);
  assert.doesNotMatch(html, /%|trend/i);
});

test('Overview lists tracked brands by name and groups alert changes by alert', async () => {
  const saved = { items: [{ id: 'si-1', type: 'bookmark', revision: 1, target: { kind: 'brand', id: 'betterhelp' } },
    { id: 'si-2', type: 'bookmark', revision: 1, target: { kind: 'show', id: 'cs-1' } }] };
  const events = [
    { id: 2, alert_id: 3, alert_name: 'Promo codes', kind: 'new_finding', read_state: 0, observed_at: '2026-10-14T16:27:59+00:00', after_ref: 'sr1:x' },
    { id: 1, alert_id: 3, alert_name: 'Promo codes', kind: 'new_finding', read_state: 1, observed_at: '2026-10-13T16:27:59+00:00', after_ref: 'sr1:y' },
  ];
  const t = await views('#/overview', overviewApi({ saved, overview: overviewData({ recent_events: events }),
    brands: [{ key: 'betterhelp', brand: 'betterhelp', display: 'BetterHelp', records: 21, ad_breaks: 9 }] }));
  const html = await t.render();
  assert.match(html, /Tracked brands/);
  assert.match(html, /href="#\/brand\/betterhelp">BetterHelp</);
  assert.match(html, /<span class="meta ew-ov-right">9 ads<\/span>/);
  assert.doesNotMatch(html, /21 reads|\b21\b/, 'the read count is not shown');
  assert.doesNotMatch(html, /cs-1/, 'only brand bookmarks');
  assert.equal((html.match(/href="#\/alert\/3"/g) || []).length, 1, 'one row per alert');
  assert.match(html, /2 recent changes · 1 unread\. Latest: New advertising passage\./);
  assert.match(html, /href="#\/evidence\/sr1%3Ax">Open evidence/);
});

test('Overview partial: a failed panel says so with Retry; the other panels still show', async () => {
  const t = await views('#/overview', overviewApi({ overview: new Error('boom'), saved: new Error('nope'),
    search: { items: [{ ref: 'sr1:r', record_kind: 'evidence', status: 'finding', ep_title: 'Recent ep', show_title: 'S', pub_date: '2026-10-09', text: 'a passage',
      read: { brand: { key: 'acme', display: 'Acme', confirmed: false } } }] } }));
  const html = await t.render();
  assert.match(html, /! Changes didn’t load\.<\/b> This is not “nothing changed”/);
  assert.match(html, /! Tracked brands didn’t load\.<\/b> This is not “no brands”/);
  assert.match(html, /data-action="retry"/);
  assert.match(html, /Recent ep/, 'recent detections still shown');
  assert.doesNotMatch(html, /You’re not tracking anything yet/, 'a failure is not first use');
});

// ---- G1 review fixes (round 1) ----
const cardOf = (item, opts) => loadAt('#/search').EWUI.sourceCard(item, opts);

test('the card says one of three audio states (d16 decision 7: remote only and unknown are "No clip kept")', () => {
  const base = { ref: 'sr1:x', record_kind: 'evidence', status: 'finding', ep_title: 'Ep', show_title: 'S', pub_date: '2026-01-01', text: 'cola' };
  const none = { brand: { key: 'a', display: 'A' }, has_clip: false, display_span: [1, 9] };
  const NONE = /No clip \(transcript only\)/;
  assert.match(cardOf({ ...base, audio: 'available', read: none }), NONE, 'the read says no clip');
  assert.match(cardOf({ ...base, audio: 'available' }), NONE, 'no read: remote only is no clip kept');
  assert.doesNotMatch(cardOf({ ...base, audio: 'available' }), /Remote audio only/, 'the remote detail is not a member state');
  assert.match(cardOf({ ...base, read: { ...none, historical: true }, audio: 'available' }), NONE, 'a historical read');
  assert.match(cardOf({ ...base, audio: 'mismatch', read: { ...none, has_clip: undefined } }), /Audio changed since transcription/);
  assert.match(cardOf({ ...base, audio: 'mismatch' }), /Audio changed since transcription/);
  assert.match(cardOf({ ...base }), NONE, 'unknown audio: no clip kept, never "available"');
  assert.match(cardOf({ ...base, record_kind: 'transcript_passage', status: 'mention', audio: 'available' }), NONE, 'a transcript passage has no clip claim');
  assert.match(cardOf({ ...base, audio: 'available', audio_local: true, quotation_span: [1, 9], read: none }), /data-action="search-play"/, 'a local recording plays');
  assert.doesNotMatch(cardOf({ ...base, audio: 'available', audio_local: true, quotation_span: [1, 9], read: none }), /No clip \(transcript only\)/);
  assert.match(cardOf({ ...base, audio: 'available', quotation_span: [1, 9], read: { ...none, has_clip: true } }), /data-action="search-play"/, 'a kept clip plays (the record media route serves it)');
});

test('list rows and the inspector use the same clip wording as the card', async () => {
  const noRead = adItem({ ref: 'sr1:n', read: undefined, audio: 'available' });
  const mism = adItem({ ref: 'sr1:m', audio: 'mismatch', read: { brand: { key: 'ag1', display: 'AG1' }, has_clip: undefined, display_span: [1, 2] } });
  const t = await views('#/search?mode=ads&q=sleep&view=list', searchApi(bundle('ads', [noRead, mism, adItem()])));
  const html = await t.render();
  assert.match(html, /title="No clip \(transcript only\)"/);
  assert.match(html, /title="Audio changed since transcription"/);
  assert.doesNotMatch(html, /title="(Remote audio only|Source audio mismatch)/);
  assert.match(html, /ew-inspector-noclip">No clip \(transcript only\)\. The passage is on the evidence page/, 'inspector agrees with the card');
  const t2 = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  assert.match(await t2.render(), /ew-inspector-noclip">No clip \(transcript only\)\./);
});

test('Overview never presents a capped count as exact (the API returns at most 50 recent changes)', async () => {
  const mk = n => Array.from({ length: n }, (_, i) => ({ id: i + 1, alert_id: 3, alert_name: 'Promo codes', kind: 'new_finding', read_state: 0,
    observed_at: '2026-10-14T16:27:59+00:00', after_ref: 'sr1:' + i }));
  const capped = await views('#/overview', overviewApi({ overview: overviewData({ recent_events: mk(50) }) }));
  const html = await capped.render();
  assert.match(html, /50\+ recent changes · 50\+ unread/);
  const under = await views('#/overview', overviewApi({ overview: overviewData({ recent_events: mk(49) }) }));
  assert.match(await under.render(), /49 recent changes · 49 unread/, 'below the cap the count is exact');
});

// ---- G1 review fixes: structure, ids, controls, CSS ----
test('List view: select-all is exposed to assistive tech and still visible below 900px', async () => {
  const t = await views('#/search?mode=ads&q=sleep&view=list', searchApi(bundle('ads', [adItem()])));
  const html = await t.render();
  assert.doesNotMatch(html, /<div class="ew-rows-head" aria-hidden/, 'the head row is not hidden from assistive tech');
  assert.match(html, /<span class="ew-rows-all"><label class="ew-select"><input type="checkbox" id="select-page"><span class="ew-select-all-text">Select all on this page<\/span><\/label><\/span>/);
  assert.match(html, /<span aria-hidden="true">Time<\/span>/, 'decorative column titles stay hidden');
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  // Only a list with nothing to select (a viewer, P3-3) drops its head row.
  assert.doesNotMatch(css, /(?<!\.ew-no-select )\.ew-rows-head \{ display: none; \}/, 'the head row (with select-all) is not removed below 900px');
});

const wideApi = b => p => (p.startsWith('/customer/filter-values?')
  ? { items: [{ value: 'a', label: 'A' }] } : searchApi(b)(p));
const ids = html => [...html.matchAll(/ id="([^"]+)"/g)].map(m => m[1]);

test('the rail and the Filters dialog never share element ids', async () => {
  const t = await views('#/search?mode=ads&q=sleep', wideApi(bundle('ads', [adItem()])));
  t.win.matchMedia = q => ({ matches: /1200/.test(q) });
  const rail = await t.render();
  assert.match(rail, /<form id="filters-rail"/);
  assert.match(rail, /<label for="filters-rail-from">From<\/label><input id="filters-rail-from"/);
  assert.match(rail, /<label for="filters-rail-to">Through<\/label><input id="filters-rail-to"/);
  assert.match(rail, /<div id="filters-rail-errors" data-form-errors role="alert"><\/div>/);
  assert.doesNotMatch(rail, /id="form-errors"/);
  const modal = { open: false, innerHTML: '', close() {}, showModal() { this.open = true; }, setAttribute() {}, classList: { add() {}, remove() {} }, addEventListener() {}, querySelector: () => null, querySelectorAll: () => [] };
  const q0 = t.win.document.querySelector;
  t.win.document.querySelector = s => (s === '#modal' ? modal : q0(s));
  t.win.EW.openDialog = () => { modal.open = true; };
  await t.win.EWViews.handleAction({ dataset: { action: 'filters' }, closest: () => null });
  assert.match(modal.innerHTML, /<label for="filters-from">From<\/label><input id="filters-from"/);
  assert.match(modal.innerHTML, /<label for="filters-to">Through<\/label><input id="filters-to"/);
  const both = ids(rail).concat(ids(modal.innerHTML));
  assert.deepEqual(both.filter((x, i) => both.indexOf(x) !== i), [], 'no duplicate ids with the dialog open');
});

test('the quick scope controls (Dates, Show, Brand) stay on the Search page and open their scope dialogs', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  const html = await t.render();
  for (const a of ['date-scope', 'scope-show', 'scope-brand']) assert.match(html, new RegExp(`class="ew-quick-scope"[^]*data-action="${a}"`));
  const modal = { innerHTML: '', open: false };
  const q0 = t.win.document.querySelector;
  t.win.document.querySelector = s => (s === '#modal' ? modal : q0(s));
  t.win.EW.openDialog = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'date-scope' }, closest: () => null });
  assert.match(modal.innerHTML, /<form id="scope"[^]*name="from"[^]*name="to"/);
  await t.win.EWViews.handleAction({ dataset: { action: 'scope-brand' }, closest: () => null });
  assert.match(modal.innerHTML, /name="brand"/);
});

test('the inspector announces only the selected title, not the whole panel', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  const html = await t.render();
  const aside = html.match(/<aside class="ew-inspector"[^>]*>/)[0];
  assert.doesNotMatch(aside, /aria-live/);
  assert.match(html, /<p class="visually-hidden" role="status" aria-live="polite" data-inspector-live>Selected: AG1[^<]*<\/p><div class="ew-inspector-body">/);
});

const cssDecls = (css, selector) => {
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  for (let m; (m = re.exec(css));) if (m[1].split(',').map(s => s.trim()).includes(selector)) out.push(m[2]);
  return out;
};
test('search.css: hit targets are at least the --ew-target size', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const sel = ['.ew-select', '.ew-rail-head .button.quiet', '.filter-block .actions .button.quiet', '.filter-block .check-list label',
    '.ew-row .ew-play', '.ew-view-switch .ew-segmented-options label', '.ew-week-bar', '.ew-inspect', '.ew-row-open'];
  for (const s of sel) {
    const decls = cssDecls(css, s).join(';');
    assert.ok(decls, s + ' has a rule');
    for (const m of decls.matchAll(/(?:^|;|\s)(min-height|min-width|height|width)\s*:\s*(\d+)px/g)) {
      assert.ok(Number(m[2]) >= 40, `${s} ${m[1]} ${m[2]}px is under 40px`);
    }
  }
  assert.match(cssDecls(css, '.ew-select').join(';'), /min-height: var\(--ew-target\)/);
});

test('search.css: type comes from the --ew-t-* tokens wherever one matches', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const tokens = fs.readFileSync(path.join(APP, 'tokens.css'), 'utf8');
  const norm = v => v.replace(/\s+/g, ' ').trim();
  const known = new Map([...tokens.matchAll(/--ew-t-([a-z0-9-]+)\s*:\s*([^;]+);/g)].map(m => [norm(m[2]), m[1]]));
  const hits = [];
  for (const m of css.matchAll(/font\s*:\s*([^;}]+)[;}]/g)) {
    const v = norm(m[1]);
    if (!v.startsWith('var(') && known.has(v)) hits.push(`${v} -> --ew-t-${known.get(v)}`);
  }
  assert.deepEqual(hits, []);
});

test('search.css: list rows keep time, date and status below 900px', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  assert.doesNotMatch(css, /\.ew-row-time, \.ew-row-date, \.ew-row-status \{ display: none; \}/);
  assert.match(css, /grid-template-areas: "check play brand open" "check play where open" "check play meta open"/);
  assert.match(css, /\.ew-row-meta \{ grid-area: meta; display: flex;/, 'time, date and status share the third line');
});
