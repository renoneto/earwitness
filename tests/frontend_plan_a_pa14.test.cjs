/* Plan A excerpt view, PA fix round 14: Muse round 2 (pa-r2) FIX-1..4 and POLISH-1..2.
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

/* FIX-1: a plan_limited All mentions answer reports no empty search. */
const searchBundle = (mode, q, extra = {}) => ({ query: { version: 2, mode, q, sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items: [], total: 0, per_page: 10, total_relation: 'exact', dated_total: 0, undated_total: 0, ...extra },
  coverage: { episodes_in_scope: 4 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/categories') return { categories: [] };
  if (p === '/customer/research-query') return b;
  throw new Error('unexpected ' + p);
};
const scopeLine = html => ((html.match(/<p class="ew-scope-line">[\s\S]*?<span title="[^"]*">([^<]*)<\/span>/) || [])[1]);
test('FIX-1 plan_limited: no count line, no counts disclosure, no tier sentence, no export note', async () => {
  const { html } = await page('#/search?mode=mentions&q=lumen',
    searchApi(searchBundle('mentions', 'lumen', { excerpt_only: true, plan_limited: true })), { q: 'lumen' });
  assert.match(html, /All mentions is not included in your plan\./);
  assert.doesNotMatch(html, /No mentions found/);
  assert.doesNotMatch(html, /ew-count-line/);
  assert.doesNotMatch(html, /About these counts/);
  assert.doesNotMatch(html, /Transcript-only episodes are searched too/);
  // SMALL-UI QW9b: the plan sentence on every tab, in its own element (SUI-SCR-R2-01).
  assert.equal(scopeLine(html), 'All dates, all shows in this collection. 4 episodes in scope.');
  assert.match(html, /<span class="ew-plan-scope">Your plan searches ad excerpts, not full transcripts\.<\/span>/);
  assert.doesNotMatch(html, /Nothing to export/);
});
test('FIX-1 Plan B control: an empty mentions search keeps its count line, disclosure, tier and export note', async () => {
  const { html } = await page('#/search?mode=mentions&q=lumen', searchApi(searchBundle('mentions', 'lumen')), { q: 'lumen' });
  assert.match(html, /<p class="ew-count-line" aria-live="polite">No mentions found/);
  assert.match(html, /About these counts/);
  assert.equal(scopeLine(html), 'All dates, all shows in this collection. 4 episodes in scope. Transcript-only episodes are searched too; they have no ad list.');
  assert.match(html, /Nothing to export: this search found no results\./);
});

/* FIX-2: the Overview search box opens Ads at excerpt level. */
async function overviewSubmit(planA, prefs = null) {
  const win = loadViews('#/overview');
  // A browser has timers; the preferences read races a 250 ms one.
  win.setTimeout = setTimeout;
  if (prefs) {
    const session = win.fetch;
    win.fetch = async (url, init) => {
      if (String(url) === '/api/v1/customer/preferences') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, preferences: { revision: 1, ...prefs } }) };
      }
      return session(url, init);
    };
  }
  await flushBoot();
  if (planA) asPlanA(win);
  const went = [];
  win.EW.go = r => went.push(r);
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const form = { _v: { q: 'lumen' }, dataset: {}, getAttribute: k => (k === 'id' ? 'overview-search' : null), querySelector: () => null, closest: () => null };
  await win.EWViews.handleSubmit(form);
  await settle();
  return went;
}
test('FIX-2 excerpt level: the Overview search lands on Ads', async () => {
  const went = await overviewSubmit(true);
  assert.equal(went.length, 1);
  assert.match(went[0], /mode=ads/);
  assert.match(went[0], /q=lumen/);
  assert.doesNotMatch(went[0], /mode=mentions/);
});
// QA-LIVE #11: with no stored choice, Search opens on Ads, as Settings
// shows (prefSearchTab's default); it was All mentions here.
test('FIX-2 Plan B control: with no stored choice the Overview search lands on Ads', async () => {
  const went = await overviewSubmit(false);
  assert.equal(went.length, 1);
  assert.match(went[0], /mode=ads/);
});
/* QA-LIVE #11: the Overview search box follows Settings "Open Search on". */
for (const [tab, want] of [['ads', 'ads'], ['mentions', 'mentions'], ['episodes', 'episodes']]) {
  test(`QA-LIVE #11 Plan B: stored "${tab}" opens the Overview search on ${want}`, async () => {
    const went = await overviewSubmit(false, { search_tab: tab });
    assert.equal(went.length, 1, String(went));
    assert.match(went[0], new RegExp('mode=' + want));
    assert.match(went[0], /q=lumen/);
  });
}
test('QA-LIVE #11 excerpt level: stored All mentions still opens Ads', async () => {
  const went = await overviewSubmit(true, { search_tab: 'mentions' });
  assert.equal(went.length, 1, String(went));
  assert.match(went[0], /mode=ads/);
});
test('QA-LIVE #11: leaving Overview while the preferences load opens nothing', async () => {
  const win = loadViews('#/overview');
  win.setTimeout = setTimeout;
  let release;
  const session = win.fetch;
  win.fetch = (url, init) => String(url) === '/api/v1/customer/preferences'
    ? new Promise(r => { release = () => r({ ok: true, status: 200, text: async () => JSON.stringify({ ok: true, preferences: { revision: 1, search_tab: 'episodes' } }) }); })
    : session(url, init);
  await flushBoot();
  const went = [];
  win.EW.go = r => went.push(r);
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const form = { _v: { q: 'lumen' }, dataset: {}, getAttribute: k => (k === 'id' ? 'overview-search' : null), querySelector: () => null, closest: () => null };
  const done = win.EWViews.handleSubmit(form);
  await flushBoot();
  win.EW.current.route = '#/brands';
  if (release) release();
  await done;
  await settle();
  assert.deepEqual(went, []);
});
test('QA-LIVE #11: preferences slower than a moment open Ads', async () => {
  const win = loadViews('#/overview');
  win.setTimeout = setTimeout;
  const session = win.fetch;
  win.fetch = (url, init) => String(url) === '/api/v1/customer/preferences' ? new Promise(() => {}) : session(url, init);
  await flushBoot();
  const went = [];
  win.EW.go = r => went.push(r);
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const form = { _v: { q: 'lumen' }, dataset: {}, getAttribute: k => (k === 'id' ? 'overview-search' : null), querySelector: () => null, closest: () => null };
  await win.EWViews.handleSubmit(form);
  await new Promise(r => setTimeout(r, 300));
  assert.equal(went.length, 1, String(went));
  assert.match(went[0], /mode=ads/);
});
// QA11-R3-01: the Overview box reads the preferences as its own operation
// (the scoped E): the read carries the operation's owner stamp.
test('QA11-R3-01: the Overview search reads the preferences under the operation\'s owner stamp', async () => {
  const win = loadViews('#/overview');
  win.setTimeout = setTimeout;
  await flushBoot();
  const stamps = [];
  const prefs = win.EW.preferences;
  win.EW.preferences = (...args) => { stamps.push(win.EWViews.operationStamp()); return prefs(...args); };
  win.EW.go = () => {};
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const owner = win.EWViews.identityStamp();
  assert.ok(owner, 'fixture: a person is signed in');
  const form = { _v: { q: 'lumen' }, dataset: {}, getAttribute: k => (k === 'id' ? 'overview-search' : null), querySelector: () => null, closest: () => null };
  await win.EWViews.handleSubmit(form);
  await settle();
  assert.deepEqual(stamps, [owner]);
});

// QA11-R3-02: a failed preferences read is "no stored choice": the box
// opens Ads, with no narrowed-opening note.
test('QA11-R3-02: a failed preferences read (500) opens Ads with no note', async () => {
  const win = loadViews('#/overview');
  win.setTimeout = setTimeout;
  const session = win.fetch;
  win.fetch = (url, init) => String(url) === '/api/v1/customer/preferences'
    ? Promise.resolve({ ok: false, status: 500, text: async () => JSON.stringify({ ok: false, error: { code: 'INTERNAL', message: 'internal error', status: 500 } }) })
    : session(url, init);
  await flushBoot();
  const went = [];
  win.EW.go = r => went.push(r);
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const form = { _v: { q: 'lumen' }, dataset: {}, getAttribute: k => (k === 'id' ? 'overview-search' : null), querySelector: () => null, closest: () => null };
  await win.EWViews.handleSubmit(form);
  await settle();
  assert.equal(went.length, 1, String(went));
  assert.match(went[0], /mode=ads/);
  assert.match(went[0], /q=lumen/);
  assert.equal(win.EW.current.searchPrefNote, undefined);
});

// QA11-R1-01..03: a pending Overview search held across a sign-in
// change or a page change opens nothing; a narrowed opening keeps the
// plain Search open's note.
async function pendingOverviewSearch(prefs = { search_tab: 'episodes' }) {
  const win = loadViews('#/overview');
  win.setTimeout = setTimeout;
  let release;
  const session = win.fetch;
  win.fetch = (url, init) => String(url) === '/api/v1/customer/preferences'
    ? new Promise(r => { release = () => r({ ok: true, status: 200, text: async () => JSON.stringify({ ok: true, preferences: { revision: 1, ...prefs } }) }); })
    : session(url, init);
  await flushBoot();
  const went = [];
  const realGo = win.EW.go;
  win.EW.go = r => { went.push(r); win.EW.current.route = r; };
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const form = { _v: { q: 'secret words' }, dataset: {}, getAttribute: k => (k === 'id' ? 'overview-search' : null), querySelector: () => null, closest: () => null };
  return { win, went, realGo, submit: () => win.EWViews.handleSubmit(form), release: () => release && release() };
}
test('QA11-R1-01: another person signed in while the read is pending opens nothing', async () => {
  const s = await pendingOverviewSearch();
  s.win.EW.current.principal = 'alice';
  const done = s.submit();
  await flushBoot();
  s.win.EW.current.principal = 'bob';
  s.release(); await done; await settle();
  assert.deepEqual(s.went, []);
});
test('QA11-R1-02: leaving Overview and coming back before the read answers opens nothing', async () => {
  // QA11-R2-01: real navigation, not a hand-bumped epoch.
  const s = await pendingOverviewSearch();
  s.win.EW.current.framePath = '#/overview';
  s.win.history.pushState = (st, t, u) => { if (u) s.win.location.hash = String(u).replace(/^[^#]*/, ''); };
  const epoch = s.win.EW.current.authorityEpoch;
  const done = s.submit();
  await flushBoot();
  s.realGo('#/brands'); await settle();
  s.realGo('#/overview'); await settle();
  assert.notEqual(s.win.EW.current.authorityEpoch, epoch, 'the page really changed');
  s.release(); await done; await settle();
  assert.deepEqual(s.went, []);
});
test('QA11-R2-01: a background re-render of Overview while the read is pending still opens Search', async () => {
  const s = await pendingOverviewSearch();
  s.win.EW.current.framePath = '#/overview';
  const token = s.win.EW.current.fetchToken;
  const done = s.submit();
  await flushBoot();
  const painted = s.win.EWViews.render(); await settle();
  assert.notEqual(s.win.EW.current.fetchToken, token, 'the re-render really ran');
  s.release(); await done; await settle();
  try { await painted; } catch {}
  assert.equal(s.went.length, 1, String(s.went));
  assert.match(s.went[0], /mode=episodes/);
});
test('QA11-R1-02 control: the same person, still on Overview, gets Search', async () => {
  const s = await pendingOverviewSearch();
  s.win.EW.current.principal = 'alice';
  const done = s.submit();
  await flushBoot();
  s.release(); await done; await settle();
  assert.equal(s.went.length, 1, String(s.went));
  assert.match(s.went[0], /mode=episodes/);
});
test('QA11-R1-03: a narrowed opening keeps the "opened with your saved Search settings" note', async () => {
  const s = await pendingOverviewSearch({ search_tab: 'mentions' });
  const done = s.submit();
  await flushBoot();
  s.release(); await done; await settle();
  assert.equal(s.went.length, 1, String(s.went));
  assert.equal(s.win.EW.current.searchPrefNote, s.went[0]);
});
test('QA11-R1-03 control: a plain Ads opening sets no note', async () => {
  const s = await pendingOverviewSearch({ search_tab: 'ads' });
  const done = s.submit();
  await flushBoot();
  s.release(); await done; await settle();
  assert.equal(s.went.length, 1, String(s.went));
  assert.ok(!s.win.EW.current.searchPrefNote);
});
test('QA-LIVE #11: the stored results view comes along', async () => {
  const went = await overviewSubmit(false, { search_tab: 'ads', results_view: 'list' });
  assert.equal(went.length, 1, String(went));
  assert.match(went[0], /mode=ads/);
  assert.match(went[0], /view=list/);
});

/* FIX-3a: Settings "Open Search on". */
const settingsApi = (prefs = {}, extra = {}) => p => {
  if (p === '/customer/preferences') return { preferences: { revision: 1, workspace_name: 'Studio', ...prefs }, ...extra };
  throw new Error('unexpected ' + p);
};
const tabSelect = html => (html.match(/<select id="pref-search-tab" name="search_tab">([\s\S]*?)<\/select>/) || [])[1] || '';
const optionValues = sel => [...sel.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]);
test('FIX-3 excerpt level: Open Search on offers Ads and Episodes only', async () => {
  const { html } = await page('#/settings', settingsApi(), { pre: asPlanA });
  const sel = tabSelect(html);
  assert.deepEqual(optionValues(sel), ['ads', 'episodes']);
  assert.doesNotMatch(sel, /All mentions/);
});
test('FIX-3 excerpt level: a stored All mentions preference shows disabled and selected', async () => {
  const { html } = await page('#/settings', settingsApi({ search_tab: 'mentions' }), { pre: asPlanA });
  const sel = tabSelect(html);
  assert.match(sel, /<option value="mentions"[^>]*\bdisabled\b[^>]*>All mentions \(not included in your plan\)<\/option>/);
  assert.match(sel, /<option value="mentions"[^>]*\bselected\b/);
  assert.doesNotMatch(sel, /<option value="ads"[^>]*selected/);
});
test('FIX-3 excerpt level: saving other preferences keeps a stored All mentions preference', async () => {
  const win = loadViews('#/settings');
  await flushBoot();
  asPlanA(win);
  const bodies = [];
  win.EW.savePreferences = async body => { bodies.push(body); return { preferences: {} }; };
  win.EW.render = async () => {};
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  // A disabled selected option is not part of the form data.
  const form = { _v: { expected_revision: '1', display_name_set: '', results_view: 'list', default_range: 'all', time_zone: '', display_name: '' },
    dataset: {}, getAttribute: k => (k === 'id' ? 'settings-display' : null), querySelector: () => null, closest: () => null };
  await win.EWViews.handleSubmit(form);
  assert.equal(bodies.length, 1);
  assert.ok(!('search_tab' in bodies[0]), 'search_tab is not rewritten');
  assert.equal(bodies[0].results_view, 'list');
});
test('code r10 F-1: a plan flag cleared before save still keeps the stored preference', async () => {
  const win = loadViews('#/settings');
  await flushBoot();
  asPlanA(win);
  win.EW.current.excerptOnly = false; // the plan mark cleared mid-session
  const bodies = [];
  win.EW.savePreferences = async body => { bodies.push(body); return { preferences: {} }; };
  win.EW.render = async () => {};
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._v)[Symbol.iterator](); } };
  const form = { _v: { expected_revision: '1', display_name_set: '', results_view: 'list', default_range: 'all', time_zone: '', display_name: '' },
    dataset: {}, getAttribute: k => (k === 'id' ? 'settings-display' : null), querySelector: () => null, closest: () => null };
  await win.EWViews.handleSubmit(form);
  assert.equal(bodies.length, 1);
  assert.ok(!('search_tab' in bodies[0]), 'search_tab is not rewritten');
});
test('FIX-3 cold Settings: the preferences response carries the plan', async () => {
  const { html } = await page('#/settings', settingsApi({}, { excerpt_only: true }));
  assert.deepEqual(optionValues(tabSelect(html)), ['ads', 'episodes']);
});
test('FIX-3 Plan B control: All mentions stays offered', async () => {
  const { html } = await page('#/settings', settingsApi({ search_tab: 'mentions' }, { excerpt_only: false }));
  const sel = tabSelect(html);
  assert.deepEqual(optionValues(sel), ['ads', 'mentions', 'episodes']);
  assert.match(sel, /<option value="mentions" selected>All mentions<\/option>/);
});

/* FIX-3b: campaign placement window actions. */
const CITE = 'exp:799e89fcf319', WIN = 'exp:f3ba15d41424', LOOSE = 'exp:aaaaaaaaaaaa';
const campDraft = () => ({
  campaign: { id: 1, revision: 4, name: 'Lumen Q4 host reads' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [
    { id: CITE, scope: 'episode', show_id: 's1', episode_id: 'ep5', expected_count: 1, binding: 'eligible', provenance: { binding: 'eligible' } },
    { id: WIN, scope: 'show_window', show_id: 's1', date_from: '2026-06-18', date_to: '2026-10-09', expected_count: 2, binding: 'eligible', provenance: { binding: 'eligible' } },
    { id: LOOSE, scope: 'show_window', date_from: '2026-06-18', expected_count: 1, binding: 'eligible', provenance: { binding: 'eligible' } },
  ],
  targets: [], episode_titles: { ep5: 'Ep. 5' }, coverage_by_placement: {}, comparisons: {},
  allocation: { evaluations: {}, citations: {} }, reason_codes: { evaluations: {}, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: {}, limitations: [] },
  pins: { attachments: {} }, allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'],
});
const campApi = p => {
  if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Lumen Q4 host reads', brand: 'lumen', requirement_provenance: {} } };
  if (p === '/customer/campaigns/1/draft') return { draft: campDraft() };
  if (p === '/customer/campaigns/1/reports') return { reports: [] };
  throw new Error('unexpected ' + p);
};
const placement = (pid, planA) => page('#/campaign/1/placement/' + encodeURIComponent(pid), campApi, { pre: planA ? asPlanA : null });
const hrefOf = (html, text) => [...html.matchAll(/<a [^>]*href="([^"]*)"[^>]*>([^<]*)<\/a>/g)].filter(m => m[2] === text).map(m => m[1]);
test('FIX-3 excerpt level: Search this window opens Ads with the same scope', async () => {
  const { html } = await placement(WIN, true);
  const hrefs = hrefOf(html, 'Search this window');
  assert.ok(hrefs.length >= 1, 'link present');
  for (const h of hrefs) {
    assert.match(h, /mode=ads/);
    assert.doesNotMatch(h, /mode=mentions/);
    assert.match(h, /shows=s1/);
    assert.match(h, /from=2026-06-18/);
    assert.match(h, /to=2026-10-09/);
  }
});
test('FIX-3 excerpt level: the no-target Search link opens Ads', async () => {
  const { html } = await placement(LOOSE, true);
  const hrefs = hrefOf(html, 'Search');
  assert.ok(hrefs.length >= 1, 'link present');
  for (const h of hrefs) { assert.match(h, /mode=ads/); assert.doesNotMatch(h, /mode=mentions/); }
});
test('FIX-3 excerpt level: the episode action reads Open episode', async () => {
  const { html } = await placement(CITE, true);
  assert.ok(hrefOf(html, 'Open episode').length >= 1);
  assert.doesNotMatch(html, /Open episode transcript/);
});
test('FIX-3 Plan B control: placement actions unchanged', async () => {
  const w = await placement(WIN, false);
  for (const h of hrefOf(w.html, 'Search this window')) assert.match(h, /mode=mentions/);
  const l = await placement(LOOSE, false);
  for (const h of hrefOf(l.html, 'Search')) assert.match(h, /mode=mentions/);
  const c = await placement(CITE, false);
  assert.ok(hrefOf(c.html, 'Open episode transcript').length >= 1);
});

/* FIX-4: the show header at excerpt level. */
const showApi = (denied, extra = {}) => p => {
  if (p === '/customer/shows/cs-a') return { display_title: 'Morning Show', coverage: { episodes: 1, episodes_with_transcript: 0 },
    sponsors: [], episodes: [{ id: 'ep_a', ep_title: 'Monday', pub_date: '2026-09-28', transcript: false, transcript_denied: denied }], ...extra };
  throw new Error('unexpected ' + p);
};
const showMeta = html => ((html.match(/<p class="lib-scope">[\s\S]*?<span>([^<]*)<\/span><a href="\/coverage\/">/) || [])[1]);
test('FIX-4 excerpt level: a denied transcript reads transcripts not included in your plan', async () => {
  const { html } = await page('#/show/cs-a', showApi(true), { pre: asPlanA });
  assert.equal(showMeta(html), '1 episode known · transcripts not included in your plan · Sep 28');
});
test('FIX-4 cold show page: the show response carries the plan', async () => {
  const { html } = await page('#/show/cs-a', showApi(true, { excerpt_only: true }));
  assert.equal(showMeta(html), '1 episode known · transcripts not included in your plan · Sep 28');
});
test('FIX-4 excerpt level: with no denied transcript the count stays', async () => {
  const { html } = await page('#/show/cs-a', showApi(false), { pre: asPlanA });
  assert.equal(showMeta(html), '1 episode known · 0 with transcripts · Sep 28');
});
test('FIX-4 Plan B control: a denied transcript keeps the count', async () => {
  const { html } = await page('#/show/cs-a', showApi(true, { excerpt_only: false }));
  assert.equal(showMeta(html), '1 episode known · 0 with transcripts · Sep 28');
});

/* POLISH-1: the What we cover card's first line. */
const cover = html => (html.match(/<section class="ew-cover">[\s\S]*?<\/section>/) || [''])[0];
test('POLISH-1 excerpt level: Full coverage line names excerpts and the plan', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED));
  assert.match(cover(html), /<p><b>Full coverage:<\/b> a list of ads, with excerpts; transcripts are not included in your plan\.<\/p>/);
  assert.doesNotMatch(cover(html), /transcript and a list of ads/);
});
test('POLISH-1 Plan B control: Full coverage line unchanged', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(fullRecord), SCREENED, { excerpt: false }));
  assert.match(cover(html), /<p><b>Full coverage:<\/b> transcript and a list of ads\.<\/p>/);
});

/* POLISH-2: a cut excerpt on an episode ad row ends with " …". */
const excerpts = html => [...html.matchAll(/<p class="ew-ad-excerpt">([^<]*)<\/p>/g)].map(m => m[1]);
test('POLISH-2 excerpt level: a truncated ad row ends with the cut mark', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED, { ads: [{ ...AD_A, truncated: true }, LOCAL] }));
  const rows = excerpts(html);
  assert.equal(rows.length, 2);
  assert.ok(rows.some(t => t.endsWith(' …')), 'the cut row is marked');
  assert.equal(rows.filter(t => t.endsWith(' …')).length, 1, 'only the cut row');
});
test('POLISH-2 excerpt level: an uncut row has no mark', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED));
  for (const t of excerpts(html)) assert.ok(!t.endsWith('…'));
});
test('POLISH-2 Plan B control: no excerpt line on Plan B rows', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(fullRecord), SCREENED, { excerpt: false, ads: [{ ...AD_A, truncated: true }, LOCAL] }));
  assert.equal(excerpts(html).length, 0);
  assert.doesNotMatch(html, /ew-ad-excerpt/);
});
