/* UAT fix round, scope worker (frontend):
   F-3  a week chart cell scope is named "Week of Sep 14–20", the unknown
        cell "Date unknown", and the scope line states the same dates.
   F-9  a show chip carries the same ID qualifier as the filter list when
        two shows share a title.
   F-4  Apply with From/Through and "All dates" submits "known": the
        server already leaves undated episodes out of a date range.
   VL-12 show page: "N ads listed" / "N ads listed (all|some
        confirmed)", and the sponsors footer notes listed ads with no
        brand name (no number).
   Loader copied from frontend_uat_fixes.test.cjs.
   Run: node --test tests/frontend_uat_scope.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
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
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, setup) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  if (setup) setup(win);
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return { win, html: main.innerHTML };
}

/* -- Search scope ---------------------------------------------------------- */
const BASE = { version: 2, mode: 'ads', q: '', brands: null, shows: null, categories: null, statuses: null, audio: null,
  date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null };
const cellSelector = (key, group_by = 'week') => ({ version: 1, base_query: BASE, clause: { kind: 'summary_cell', group_by, key },
  count_units: 'distinct_results', scope_mode: 'counted_cohort', view_revision: 'rev-1', workspace_context: 'ctx-ws' });
const bundle = (query, selector = null, episodes = 38) => ({ query, selector, revision: 'rev-1',
  result: { items: [], total: 0, per_page: 10, total_relation: 'exact', dated_total: 0, undated_total: 0 },
  coverage: { episodes_in_scope: episodes, sources_included: episodes, excluded_undated: false }, summary: { week: [] } });
const searchApi = (b, shows = []) => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows };
  if (p === '/customer/research-query') return b;
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  return new Error('unexpected ' + p);
};
const selRoute = sel => '#/search?mode=ads&selector=' + encodeURIComponent(JSON.stringify(sel));
const cellChip = html => (html.match(/<button[^>]*class="ew-chip filter-chip cell-scope"[^>]*>([^<]*)</) || [])[1];
const scopeLine = html => (html.match(/<p class="ew-scope-line">[\s\S]*?<span title="[^"]*">([^<]*)<\/span>/) || [])[1];

test('F-3: a week cell scope is named by its dates, in the chip and the scope line', async () => {
  const sel = cellSelector('2026-09-14');
  const { html } = await render(selRoute(sel), searchApi(bundle(BASE, sel)));
  assert.equal(cellChip(html).trim(), 'Week of Sep 14–20');
  assert.match(html, /aria-label="Remove Week of Sep 14–20"/);
  assert.equal(scopeLine(html), 'Week of Sep 14–20, all shows in this collection. 38 episodes in scope. Ads come from full-coverage episodes only.');
  assert.doesNotMatch(html, /Selected chart cell/);
});

test('F-3: a week across two months names both months', async () => {
  const sel = cellSelector('2026-09-28');
  const { html } = await render(selRoute(sel), searchApi(bundle(BASE, sel)));
  assert.equal(cellChip(html).trim(), 'Week of Sep 28–Oct 4');
});

test('F-3: the unknown-date cell is "Date unknown"; the scope line says undated episodes', async () => {
  const sel = cellSelector('unknown');
  const { html } = await render(selRoute(sel), searchApi(bundle(BASE, sel, 9)));
  assert.equal(cellChip(html).trim(), 'Date unknown');
  assert.equal(scopeLine(html), 'Undated episodes only, all shows in this collection. 9 episodes in scope. Ads come from full-coverage episodes only.');
});

test('F-3: any other chart cell keeps the general label', async () => {
  const sel = cellSelector('confirmed', 'status');
  const { html } = await render(selRoute(sel), searchApi(bundle(BASE, sel)));
  assert.equal(cellChip(html).trim(), 'Selected chart cell');
  assert.match(scopeLine(html), /^All dates, /);
});

test('F-9: two shows with one title get two different chip labels', async () => {
  const shows = [{ id: 'cs-19b8688e19c1', display_title: 'I Said God Damn!' }, { id: 'cs-603d25c15c03', display_title: 'I Said God Damn!' }];
  const q = { ...BASE, shows: shows.map(s => s.id) };
  const { html } = await render('#/search?mode=ads&shows=cs-19b8688e19c1&shows=cs-603d25c15c03', searchApi(bundle(q), shows));
  const labels = [...html.matchAll(/data-key="shows"[^>]*aria-label="Remove ([^"]*)"/g)].map(m => m[1]);
  assert.equal(labels.length, 2, html.slice(0, 200));
  assert.notEqual(labels[0], labels[1]);
  assert.deepEqual(labels.sort(), ['I Said God Damn! (cs-19b86…)', 'I Said God Damn! (cs-603d2…)']);
});

test('F-9: a show title nobody else uses has no qualifier', async () => {
  const shows = [{ id: 'cs-19b8688e19c1', display_title: 'I Said God Damn!' }, { id: 'cs-1', display_title: 'Other' }];
  const { html } = await render('#/search?mode=ads&shows=cs-19b8688e19c1', searchApi(bundle({ ...BASE, shows: ['cs-19b8688e19c1'] }), shows));
  assert.match(html, /aria-label="Remove I Said God Damn!"/);
});

/* -- F-4 Apply with a date range ------------------------------------------- */
const filtersForm = fields => ({
  getAttribute: k => (k === 'id' ? 'filters' : null), dataset: {}, elements: [],
  querySelector: s => (/^\[name=init_/.test(s) ? { value: 'null' } : /^fieldset/.test(s) ? { dataset: {} } : null),
  querySelectorAll: () => [], closest: () => null, _fields: fields });
async function applyFilters(fields) {
  const calls = [];
  const { win } = await render('#/search?mode=ads', searchApi(bundle(BASE)), w => {
    w.EW.go = (...a) => { calls.push(String(a[0])); };
  });
  calls.length = 0;
  win.FormData = class { constructor(f) { this.f = f; } *entries() { yield* Object.entries(this.f._fields); } };
  win.EW.closeDialog = () => {};
  await win.EWViews.handleSubmit(filtersForm(fields));
  for (let i = 0; i < 5; i++) await flushBoot();
  stopTimers();
  assert.equal(calls.length, 1, JSON.stringify(calls));
  return new URLSearchParams(calls[0].split('?')[1]);
}

test('F-4: Apply with From/Through and "All dates" submits known dates', async () => {
  const p = await applyFilters({ date_status: 'all', from: '2026-09-14', to: '2026-09-20', sort: 'newest' });
  assert.equal(p.get('date_status'), 'known');
  assert.equal(p.get('from'), '2026-09-14');
  assert.equal(p.get('to'), '2026-09-20');
});

test('F-4: a From date alone also submits known dates', async () => {
  const p = await applyFilters({ date_status: 'all', from: '2026-09-14', to: '', sort: 'newest' });
  assert.equal(p.get('date_status'), 'known');
});

test('F-4: no bounds keeps "All dates"; "Unknown dates only" still clears bounds', async () => {
  const all = await applyFilters({ date_status: 'all', from: '', to: '', sort: 'newest' });
  assert.ok(!all.get('date_status') || all.get('date_status') === 'all', all.toString());
  const unknown = await applyFilters({ date_status: 'unknown', from: '2026-09-14', to: '', sort: 'newest' });
  assert.equal(unknown.get('date_status'), 'unknown');
  assert.equal(unknown.get('from'), null);
});

/* -- VL-12 show page -------------------------------------------------------- */
const SC = { jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full' };
const SHOW = { show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: 4, episodes_with_transcript: 4 },
  sponsors: [{ key: 'ag1', display: 'AG1', episodes: 1, ads: 2, first_seen: '2026-09-01', last_seen: '2026-10-09' }],
  sponsors_not_counted: { under_review: 0, unnamed: 3 },
  episodes: [
    { id: 'e1', ep_title: 'One', pub_date: '2026-10-09', transcript: true, ads: 3, ad_breaks: 2, confirmed: 0, screening: SC },
    { id: 'e2', ep_title: 'Two', pub_date: '2026-10-02', transcript: true, ads: 3, ad_breaks: 2, confirmed: 2, screening: SC },
    { id: 'e3', ep_title: 'Three', pub_date: '2026-09-18', transcript: true, ads: 1, ad_breaks: 1, confirmed: 0, screening: SC },
    { id: 'e4', ep_title: 'Four', pub_date: '2026-09-11', transcript: true, ads: 1, ad_breaks: 1, confirmed: 1, screening: SC }] };
const showApi = show => p => (p === '/customer/shows/cs-1' ? show : new Error('unexpected ' + p));
const epRowOf = (html, id) => (html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];

test('VL-12: the episode tag says possible ads, or whether all or some are confirmed', async () => {
  const { html } = await render('#/show/cs-1', showApi(SHOW));
  assert.match(epRowOf(html, 'e1'), />Screened · 2 ads listed</);
  assert.match(epRowOf(html, 'e2'), />Screened · 2 ads listed</);
  assert.match(epRowOf(html, 'e3'), />Screened · 1 ad listed</);
  assert.match(epRowOf(html, 'e4'), />Screened · 1 ad listed</);
  assert.doesNotMatch(html, /\(\d+ confirmed\)|\d+ (possible )?(ad )?reads?\b/, 'the evidence count is never shown beside ads');
});

test('VL-12: an older server without the confirmed count claims neither', async () => {
  const old = { ...SHOW, episodes: SHOW.episodes.map(({ confirmed, ...e }) => e) };
  const { html } = await render('#/show/cs-1', showApi(old));
  assert.match(epRowOf(html, 'e1'), />Screened · 2 ads listed</);
  assert.doesNotMatch(html, /possible ad(s| reads?)? listed|confirmed\)/);
});

test('VL-12: the sponsors footer says some listed ads have no brand name, never a number', async () => {
  const { html } = await render('#/show/cs-1', showApi(SHOW));
  // Row 1: `unnamed` counts evidence, so the footer gives no number.
  assert.match(html, /<p class="lib-sponsor-note">Some listed ads have no brand name and are not in this list\.<\/p>/);
  assert.doesNotMatch(html, /\d+ listed (reads?|ads?)/);
  const one = await render('#/show/cs-1', showApi({ ...SHOW, sponsors_not_counted: { under_review: 0, unnamed: 1 } }));
  assert.match(one.html, /Some listed ads have no brand name and are not in this list\./);
  assert.doesNotMatch(one.html, /\d+ listed (reads?|ads?)/);
  const empty = await render('#/show/cs-1', showApi({ ...SHOW, sponsors: [] }));
  assert.match(empty.html, /Some listed ads have no brand name and are not in this list\./, 'said beside an empty list too');
  for (const notCounted of [{ under_review: 0, unnamed: 0 }, { under_review: 0 }]) {
    const none = await render('#/show/cs-1', showApi({ ...SHOW, sponsors_not_counted: notCounted }));
    assert.doesNotMatch(none.html, /no brand name/);
  }
});
