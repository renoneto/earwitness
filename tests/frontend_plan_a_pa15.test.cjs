/* Plan A excerpt view, PA fix round 15: a stored All mentions Search default opens Ads at excerpt level.
   Helpers copied from frontend_plan_a_pa12.test.cjs.
   Run: node --test tests/frontend_plan_a_pa14.test.cjs */
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
async function page(route, api, { hosted = false, q = null, pre = null } = {}) {
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
  if (pre) pre(win);
  win.location.hash = route;
  win.EW.current.route = route;
  if (q !== null) win.EW.current.filters = { ...(win.EW.current.filters || {}), q };
  await win.EWViews.render();
  return { win, html: main.innerHTML, main, modal, toasts, calls };
}

const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
const SAFE = 'this episode is brought to you by Lumen lamps go to lumen dot com slash morning zebra';
const READ = { kind: 'named', source_label: 'Named read',
  brand: { key: 'lumen', display: 'Lumen', display_basis: 'brand_list', suggested: 'lumen', confirmed: false },
  needs_review: false, self_promo: false, self_promo_reason: null, confidence: 'high', display_span: [30, 45],
  display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Lumen' } };
const base = { ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'finding', bounds_verified: 0 }, attribution: [{ brand: 'Lumen', basis: 'spoken', channel: 'spoken' }],
  relationship: 'current', availability: 'available', bounds_basis: 'original',
  original_bounds: { start_s: 30, end_s: 45 }, effective_bounds: { start_s: 30, end_s: 45 }, selected_bounds: { start_s: 30, end_s: 45 },
  episode: { episode_id: 'ep_a', show_title: 'Morning Show', ep_title: 'Monday', pub_date: '2026-09-28', transcript_version_id: 2, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Morning Show', unresolved: 0 },
  evidence: { id: 'ad-a1', episode_id: 'ep_a', transcript_version_id: 2, orig_start_s: 30, orig_end_s: 45 },
  media: { playback: 'unavailable', reason: 'clip unavailable' }, read: READ };
// Plan A: the record answered at excerpt level.
const excerptRecord = (over = {}) => ({ ...base, disclosure: 'excerpt', text: SAFE, excerpt_bounds: { start_s: 25, end_s: 50 }, ...over });
// Plan B: the full record with its transcript window.
const fullRecord = (over = {}) => ({ ...base, canonical_source_ref: REF, window: { segments: [], text_complete: true, next_cursor: null }, ...over });

const AD_A = { ref: REF, evidence_id: 'ad-a1', status: 'finding', span: [30, 45], display_span: [30, 45], brand: { key: 'lumen', display: 'Lumen' },
  needs_review: false, self_promo: false, kind: 'rule', source_label: 'Rule', media: null, transcript_start_s: 30,
  transcript_version_id: 2, on_current_transcript: true };
const episodeRec = (make, over = {}) => make({ ref: 'sr1:e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5', record_kind: 'source_publication',
  evidence: {}, read: null, ...over });

const LOCAL = { ...AD_A, ref: 'sr1:d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4', evidence_id: 'ad-a2', span: [60, 75], display_span: [60, 75],
  transcript_start_s: 60, media: { playback: 'local', source_offset_s: 0 } };
const epApi = (rec, screening, { excerpt = true, ads = [AD_A, LOCAL] } = {}) => p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_a')) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_a') {
    const ex = a => (excerpt ? { ...a, text: SAFE, excerpt_bounds: { start_s: a.span[0] - 5, end_s: a.span[1] + 5 } } : a);
    return { episode: { episode_id: 'ep_a', excerpt_only: excerpt, ads: ads.map(ex), self_promo: [], ad_breaks: [], screening } };
  }
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
};
const status = html => ((html.match(/<p class="ew-episode-status">[\s\S]*?<span>([^<]+)<\/span><a href="\/coverage\/">/) || [])[1]);
const SCREENED = { jev_screened: true, coverage_tier: 'full', label: 'Screened' };
const TIER_T = { transcript_only: true, coverage_tier: 'transcript', label: 'Transcript only' };



const asPlanA = win => win.EW.notePlan({ excerpt_only: true });


// Bare #/search with a stored "Open Search on" of mentions. The redirect
// target is the route the page navigates to.
const prefApi = (prefs, extra = {}) => p => {
  if (p === '/customer/preferences') return { preferences: { revision: 1, ...prefs }, ...extra };
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/categories') return { categories: [] };
  if (p === '/customer/research-query') return searchBundle('ads', '');
  throw new Error('unexpected ' + p);
};
const searchBundle = (mode, q) => ({ query: { version: 2, mode, q, sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items: [], total: 0, per_page: 10, total_relation: 'exact', dated_total: 0, undated_total: 0 },
  coverage: { episodes_in_scope: 4 }, summary: { week: [] } });
async function openBare(route, prefs, extra, pre) {
  const gone = [];
  const api = prefApi(prefs, extra);
  // The real preferences() reads through fetch, so the plan signal in the
  // response is what the app sees on a cold open.
  const r = await page(route, api, { pre: win => {
    win.EW.go = (to) => gone.push(to);
    win.fetch = async (url) => {
      const u = String(url).replace('/api/v1', '');
      if (u === '/customer/preferences') return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, ...api(u) }) };
      throw new Error('no network in unit test');
    };
    if (pre) pre(win);
  } });
  return { ...r, gone };
}
const writes = calls => calls.filter(c => c[0] !== 'GET' && /preferences/.test(c[1]));

// Ads with cards and no date range is the plain Search page: no redirect,
// and the page opens on Ads. Mentions would redirect to mode=mentions.
const noMentions = g => g.every(x => !/mode=mentions/.test(x));
test('PA15 excerpt level (known plan): stored mentions opens Ads, no preference write', async () => {
  const { gone, calls, win } = await openBare('#/search', { search_tab: 'mentions' }, {}, asPlanA);
  assert.ok(noMentions(gone), 'redirected to ' + gone);
  assert.equal(win.EW.current.filters.mode || 'ads', 'ads');
  assert.equal(writes(calls).length, 0);
});
test('PA15 excerpt level (cold, plan from the preferences response): stored mentions opens Ads', async () => {
  const { gone, calls, win } = await openBare('#/search', { search_tab: 'mentions' }, { excerpt_only: true });
  assert.ok(noMentions(gone), 'redirected to ' + gone);
  assert.equal(win.EW.current.filters.mode || 'ads', 'ads');
  assert.equal(writes(calls).length, 0);
});
test('PA15 excerpt level: the stored preference is not changed', async () => {
  const { win } = await openBare('#/search', { search_tab: 'mentions' }, {}, asPlanA);
  const prefs = await win.EW.preferences();
  assert.equal(prefs.search_tab, 'mentions');
});
test('PA15 excerpt level: an explicit mode=mentions URL still shows the plan state', async () => {
  const { html, gone } = await openBare('#/search?mode=mentions', { search_tab: 'mentions' }, {}, asPlanA);
  assert.equal(gone.length, 0);
  assert.match(html, /not included/i);
});
test('PA15 Plan B control: stored mentions still opens All mentions', async () => {
  const { gone } = await openBare('#/search', { search_tab: 'mentions' }, { excerpt_only: false });
  assert.equal(gone.length, 1);
  assert.match(gone[0], /mode=mentions/);
});
