/* Decisions round, review #1 fixes for D-1 ad breaks (worker r1d1).
   R1-09 show page episode tag says ad breaks first; R1-10 the Ads tab's
   accessible name says its unit; R1-12 lower bounds with break data;
   R1-24 the Compare heard line uses the shared adCount wording.
   Loader copied from frontend_uat_dec_d1ui.test.cjs.
   Run: node --test tests/frontend_uat_dec_r1d1.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout, URL,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route },
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
async function render(route, api) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return { html: main.innerHTML, win };
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/\s+/g, ' ');

/* -- R1-09 show page episode tag ---------------------------------------- */
const SC = { jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full' };
const SHOW = { show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: 5, episodes_with_transcript: 5 }, sponsors: [], sponsors_not_counted: { under_review: 0, unnamed: 0 },
  episodes: [
    { id: 'e1', ep_title: 'One', pub_date: '2026-10-09', transcript: true, ads: 3, ad_breaks: 2, confirmed: 0, screening: SC },
    { id: 'e2', ep_title: 'Two', pub_date: '2026-10-02', transcript: true, ads: 3, ad_breaks: 1, confirmed: 2, screening: SC },
    { id: 'e3', ep_title: 'Three', pub_date: '2026-09-18', transcript: true, ads: 1, ad_breaks: 1, confirmed: 0, screening: SC },
    { id: 'e4', ep_title: 'Four', pub_date: '2026-09-11', transcript: true, ads: 1, ad_breaks: 1, confirmed: 1, screening: SC },
    { id: 'e5', ep_title: 'Five', pub_date: '2026-09-04', transcript: true, ads: 4, ad_breaks: 3, screening: SC }] };
const showApi = show => p => (p === '/customer/shows/cs-1' ? show : new Error('unexpected ' + p));
const epRowOf = (html, id) => (html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];

test('R1-09: the show page episode tag counts ads, with no review words (PLAIN-ADS)', async () => {
  const { html } = await render('#/show/cs-1', showApi(SHOW));
  assert.match(epRowOf(html, 'e1'), />Screened · 2 ads listed</);
  assert.match(epRowOf(html, 'e2'), />Screened · 1 ad listed</);
  assert.match(epRowOf(html, 'e3'), />Screened · 1 ad listed</);
  assert.match(epRowOf(html, 'e4'), />Screened · 1 ad listed</);
  assert.match(epRowOf(html, 'e5'), />Screened · 3 ads listed</, 'no confirmed count: no claim either way');
  assert.doesNotMatch(epRowOf(html, 'e5'), /possible|confirmed\)/);
  assert.doesNotMatch(text(html), /\d[\d,]* (possible )?reads?\b|ad breaks?/, 'no read count, no ad breaks');
});

test('R1-09: an older server without ad_breaks shows no number in the tag', async () => {
  const old = { ...SHOW, episodes: SHOW.episodes.map(({ ad_breaks, ...e }) => e) };
  const { html } = await render('#/show/cs-1', showApi(old));
  assert.match(epRowOf(html, 'e1'), />Screened · ads listed</);
  assert.match(epRowOf(html, 'e2'), />Screened · ads listed</);
  assert.doesNotMatch(html, /ad breaks? ·/);
  assert.doesNotMatch(text(html), /\d[\d,]* (possible )?(ad )?(ads?|reads?)\b/, 'no read count as a fallback');
});

/* -- R1-10 / R1-12 Search Ads tab and count line ------------------------ */
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const adItem = (n, span, over = {}) => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', show_title: 'Show A',
  ep_title: 'Episode One', episode_id: 'e1', pub_date: '2026-10-09', audio: 'unavailable', quotation_span: span,
  read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: false }, display_span: span, has_clip: false }, attribution: [], ...over });
function adsBundle({ relation = 'exact', total = 3, breakTotal = 2, perPage = 10 } = {}) {
  const a1 = adItem(1, [60, 120], { ad_break_id: 'brk:a' });
  const a2 = adItem(2, [90, 150], { ad_break_id: 'brk:a' });
  const b1 = adItem(3, [600, 660], { ad_break_id: 'brk:b' });
  const breaks = [
    { id: 'brk:a', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [60, 150], read_count: 2, matching_read_count: 2,
      refs: [a1.ref, a2.ref], brands: [{ key: 'chatgpt', display: 'ChatGPT', confirmed: false }], pub_date: '2026-10-09' },
    { id: 'brk:b', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [600, 660], read_count: 1, matching_read_count: 1,
      refs: [b1.ref], brands: [{ key: 'chatgpt', display: 'ChatGPT', confirmed: false }], pub_date: '2026-10-09' }];
  return { query: { version: 2, mode: 'ads', q: 'chatgpt', sort: 'newest', page: 1, view: 'cards' }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 },
    result: { items: [a1, a2, b1], total, dated_total: total, undated_total: 0, total_relation: relation,
      truncation_reason: relation === 'lower_bound' ? 'SCAN_CAP' : null, per_page: perPage,
      ad_breaks: breaks, break_total: breakTotal, break_dated_total: breakTotal, break_undated_total: 0 },
    summary: { week: [], status: [{ key: 'confirmed', count: 0 }, { key: 'finding', count: total }] } };
}
const searchApi = bundle => p => {
  if (p === '/customer/research-query') return bundle;
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  return new Error('unexpected ' + p);
};
const adsTab = html => (html.match(/<button type="button" data-mode="ads"[^>]*>[\s\S]*?<\/button>/) || [''])[0];

test('R1-10: the Ads tab count says its unit in the accessible name', async () => {
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(adsBundle()));
  const tab = adsTab(html);
  assert.match(tab, /aria-label="Ads, 2 ads"/);
  assert.match(tab, /<span class="ew-tab-count"[^>]*>2<\/span>/, 'the visible count is unchanged');
  const other = (html.match(/<button type="button" data-mode="mentions"[^>]*>/) || [''])[0];
  assert.doesNotMatch(other, /aria-label/, 'tabs without a count keep their text as their name');
});

test('R1-12: a lower bound with ad data says "at least" for ads, in the count line and the tab', async () => {
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(adsBundle({ relation: 'lower_bound', total: 60, breakTotal: 25, perPage: 2 })));
  const line = (html.match(/<p class="ew-count-line"[^>]*>([\s\S]*?)<\/p>/) || [, ''])[1];
  assert.match(text(line), /^Showing 1 to 2 of at least 25 ads/);
  const tab = adsTab(html);
  assert.match(tab, /aria-label="Ads, at least 25 ads"/);
  assert.match(tab, /<span class="ew-tab-count"[^>]*>≥25<\/span>/);
  assert.match(text(html), /dated: at least 25 ads/, 'the dated part is a lower bound too');
  assert.doesNotMatch(text(html), /of 25 ads|(?<!least )25 ads|\b60\b/, 'a lower bound is never shown as exact; evidence is never counted as ads');
});

test('R1-12: an older server lower bound (no ad data) shows no Ads number at all, never an exact one', async () => {
  const bundle = adsBundle({ relation: 'lower_bound', total: 60 });
  for (const k of ['ad_breaks', 'break_total', 'break_dated_total', 'break_undated_total']) delete bundle.result[k];
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(bundle));
  // Row 1: no server ad count, no number (the dated part included).
  assert.match(text(html), /Ads found · count not available/);
  assert.doesNotMatch(text(html), /\b60\b|\bdated:/);
});

/* -- R1-24 Compare heard line ------------------------------------------- */
const cellOf = count => ({ count, refs: [], coverage: { matched_in_scope: count }, selector: null,
  shows: 0, last_heard: null, possible_shows: 0, possible_last_heard: null });
const cmpData = rows => ({ brands: rows.map(r => r[0]), period_a: null, period_b: null, shared_scale: 0, revision: 'r',
  cells: rows.map(([brand, a]) => ({ brand, a })) });
const DIR = { brands: [{ key: 'chatgpt', display: 'ChatGPT', records: 12345, ad_breaks: 2345 },
  { key: 'progressive', display: 'Progressive', records: 1 }] };
const compareApi = data => p => (p === '/customer/brands' ? DIR : p.startsWith('/customer/compare?') ? data : new Error('unexpected ' + p));
const rowOf = (html, name) => {
  const start = html.indexOf(`<th scope="row">${name}`);
  assert.ok(start >= 0, 'row ' + name);
  return html.slice(start, html.indexOf('</tr>', start));
};

// PLAIN-ADS (DEP7): no heard line; the cell is the brand's ads.
test('R1-24: Compare has no heard line and never the directory read count', async () => {
  const { html } = await render('#/compare?brands=chatgpt,progressive', compareApi(cmpData([['chatgpt', cellOf(0)], ['progressive', cellOf(0)]])));
  assert.doesNotMatch(html, /Heard in|only confirmed ads are compared/);
  assert.doesNotMatch(html, /12,345|\d+ reads?\b/);
});

test('R1-09: the longer episode tag wraps inside its row instead of widening the page at 390 px', () => {
  const css = fs.readFileSync(path.join(APP, 'library.css'), 'utf8');
  assert.match(css, /\.lib-ep-tags \.ew-tag \{[^}]*white-space: normal;[^}]*max-width: 100%;/);
  assert.match(css, /\.lib-ep-tags \{[^}]*min-width: 0;/);
});
