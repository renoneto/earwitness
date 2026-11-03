/* Plan A excerpt view, PA fix round 9: Muse pa-r1 FIX-1..3 and
   POLISH-1..3 at excerpt level, each with a Plan B control. Loader helpers
   copied from frontend_plan_a_pa8.test.cjs.
   Run: node --test tests/frontend_plan_a_pa9.test.cjs */
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
const api = (rec, { excerpt = true } = {}) => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_a')) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_a') return { episode: { episode_id: 'ep_a', excerpt_only: excerpt, ads: excerpt
    ? [{ ...AD_A, text: SAFE, excerpt_bounds: { start_s: 25, end_s: 50 } }] : [AD_A],
    self_promo: [], ad_breaks: [], screening: null } };
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
};
const evidence = rec => page('#/evidence/' + encodeURIComponent(REF), api(rec));
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
const cover = html => (html.match(/<section class="ew-cover">[\s\S]*?<\/section>/) || [''])[0];
const aboutAudio = html => ((html.match(/<dt>Audio<\/dt><dd>([^<]*)<\/dd>/) || [])[1]);

/* FIX-1: a transcript-tier episode is never described as checked-and-clear
   or searchable at excerpt level. */
test('FIX-1 excerpt level: transcript-tier status reads Not checked for ads.', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), TIER_T));
  assert.equal(status(html), 'Not checked for ads.');
  assert.doesNotMatch(html, /No ads detected/);
  const s = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED));
  assert.equal(status(s.html), 'Ads checked.');
});
test('FIX-1 excerpt level: the cover card second line', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), TIER_T));
  assert.match(cover(html), /<p><b>Transcript only:<\/b> no ad list; not checked for ads\.<\/p>/);
  assert.doesNotMatch(cover(html), /searchable/);
});
test('FIX-1 Plan B control: status and cover card unchanged', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(fullRecord), TIER_T, { excerpt: false }));
  assert.equal(status(html), 'Searchable; alerts read it; no ad list.');
  assert.match(cover(html), /<p><b>Transcript only:<\/b> searchable and alerts read it, but no ad list\.<\/p>/);
});

/* FIX-1 and FIX-3 on the Overview: the scope line and the recent-ads empty line. */
const overviewApi = excerpt => p => {
  if (p.startsWith('/customer/search')) return { items: [], total: 0, ...(excerpt ? { excerpt_only: true } : {}) };
  if (p.startsWith('/customer/saved-items')) return { items: [], next_cursor: null };
  return {};
};
const scope = html => ((html.match(/<p class="ew-ov-scope">([\s\S]*?) <a href="\/coverage\/">/) || [])[1]);
test('FIX-1 / FIX-3 excerpt level: Overview scope line and recent-ads line', async () => {
  const { html } = await page('#/overview', overviewApi(true));
  assert.equal(scope(html), 'Scope: the shows in this collection. Ads come from full-coverage episodes; other episodes are not checked for ads.');
  assert.match(html, /No ads found\. Search ads or create an alert to check for matching records\./);
  assert.doesNotMatch(html, /Search transcripts/);
});
test('FIX-1 / FIX-3 Plan B control: Overview unchanged', async () => {
  const { html } = await page('#/overview', overviewApi(false));
  assert.equal(scope(html), 'Scope: the shows in this collection. Ads come from full-coverage episodes; transcript-only episodes are searchable.');
  assert.match(html, /No ads found\. Search transcripts or create an alert to check for matching records\./);
});

/* FIX-2: mentions is not offered at excerpt level, and a plan-limited
   answer says so. */
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
const state = html => (html.match(/<div class="ew-state">[\s\S]*?<\/div><\/div>|<div class="ew-state">[\s\S]*?<\/div>/) || [''])[0];
test('FIX-2 excerpt level: the Ads empty state has no Search all mentions button', async () => {
  const { html } = await page('#/search?mode=ads&q=zz', searchApi(searchBundle('ads', 'zz', { excerpt_only: true })), { q: 'zz' });
  assert.match(html, /No ads say/);
  assert.doesNotMatch(html, /Search all mentions/);
});
test('FIX-2 Plan B control: the Ads empty state keeps Search all mentions', async () => {
  const { html } = await page('#/search?mode=ads&q=zz', searchApi(searchBundle('ads', 'zz')), { q: 'zz' });
  assert.match(html, /Search all mentions/);
});
test('FIX-2 plan_limited: All mentions is not included in your plan.', async () => {
  const { html } = await page('#/search?mode=mentions&q=zz',
    searchApi(searchBundle('mentions', 'zz', { excerpt_only: true, plan_limited: true })), { q: 'zz' });
  const s = state(html);
  assert.match(s, /All mentions is not included in your plan\./);
  assert.doesNotMatch(html, /No mentions say/);
  assert.doesNotMatch(html, /Broaden the query/);
});
test('FIX-2 Plan B control: an empty mentions search keeps its words', async () => {
  const { html } = await page('#/search?mode=mentions&q=zz', searchApi(searchBundle('mentions', 'zz')), { q: 'zz' });
  assert.match(html, /No mentions say “zz” in this scope/);
  assert.match(html, /Broaden the query or remove a filter\./);
  assert.doesNotMatch(html, /not included in your plan/);
});

async function dialogHtml({ excerpt }) {
  const win = loadViews('#/alerts');
  await flushBoot();
  if (excerpt) { win.EW.current.excerptOnly = true; win.EW.current.excerptOnlyContext = win.EW.current.workspaceContext || ''; }
  const stubEl = () => ({ focus() {}, addEventListener() {}, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; } });
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector(sel) { return sel === '#alert-form' ? null : stubEl(); } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = fn => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async p => {
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: [] };
    throw new Error('unexpected request ' + p);
  };
  await win.EWViews.handleAction({ dataset: { action: 'alerts-create' }, disabled: false, closest: () => null });
  return modal.innerHTML;
}
const lookIn = html => (html.match(/<select id="alert-mode" name="mode">([\s\S]*?)<\/select>/) || [])[1];
test('FIX-2 excerpt level: the alert dialog offers no All mentions', async () => {
  const sel = lookIn(await dialogHtml({ excerpt: true }));
  assert.ok(sel, 'Look in select rendered');
  assert.doesNotMatch(sel, /All mentions|value="mentions"/);
  assert.match(sel, /<option value="ads" selected>Ads<\/option>/);
  assert.match(sel, /value="episodes"/);
});
test('FIX-2 Plan B control: the alert dialog keeps All mentions', async () => {
  const sel = lookIn(await dialogHtml({ excerpt: false }));
  assert.match(sel, /<option value="mentions" selected>All mentions<\/option>/);
});

/* FIX-3 and POLISH-1 on Saved. */
async function savedHtml(out, { excerpt = false, tab = '#/saved' } = {}) {
  const win = loadViews(tab);
  await flushBoot();
  const E = win.EW;
  if (excerpt) { E.current.excerptOnly = true; E.current.excerptOnlyContext = E.current.workspaceContext || ''; }
  E.current.route = tab;
  const api = async p => {
    if (p.startsWith('/customer/saved-items')) return out;
    return { items: [], total: 0, total_relation: 'exact', counts: {} };
  };
  return win.EWSaved.render({ api, esc: E.esc, link: E.link, scope: { query: { q: '' }, selector: null },
    workspaceContext: '', signal: null, isCurrent: true });
}
const listOut = (items, over = {}) => ({ items, total: items.length, total_relation: 'exact',
  counts: { excerpt: items.length, query: 0, bookmark: 0 }, collection_revision: 'r', next_cursor: null, ...over });
test('FIX-3 excerpt level: the Saved empty state button reads Search ads', async () => {
  const html = await savedHtml(listOut([]), { excerpt: true });
  assert.match(html, /<a class="button" href="#\/search">Search ads<\/a>/);
  assert.doesNotMatch(html, /Search transcripts/);
});
test('FIX-3 Plan B control: the Saved empty state keeps Search transcripts', async () => {
  const html = await savedHtml(listOut([]));
  assert.match(html, /<a class="button" href="#\/search">Search transcripts<\/a>/);
});
test('FIX-3 notes empty state: Search ads at excerpt level, Search transcripts on Plan B', async () => {
  const notes = { notes: [], total: 0, total_relation: 'exact' };
  const a = await savedHtml(notes, { excerpt: true, tab: '#/saved?tab=notes' });
  assert.match(a, /<h2>No standalone notes yet\.<\/h2>/);
  assert.match(a, /<a class="button" href="#\/search">Search ads<\/a>/);
  assert.doesNotMatch(a, /Search transcripts/);
  const b = await savedHtml(notes, { tab: '#/saved?tab=notes' });
  assert.match(b, /<a class="button" href="#\/search">Search transcripts<\/a>/);
});
test('POLISH-1: withheld with nothing listed reads No saved excerpts available, the line inside the card', async () => {
  const html = await savedHtml(listOut([], { plan_withheld: 1 }), { excerpt: true });
  const card = (html.match(/<div class="empty">[\s\S]*?<\/div><\/div>|<div class="empty">[\s\S]*?<\/div>/) || [''])[0];
  assert.match(card, /<h2>No saved excerpts available<\/h2>/);
  assert.ok(card.indexOf('1 saved item is not available on your plan.') > card.indexOf('No saved excerpts available'));
  assert.ok(card.indexOf('1 saved item is not available on your plan.') < card.indexOf('To save an excerpt, open an ad and choose Save excerpt.'));
  assert.equal((html.match(/not available on your plan/g) || []).length, 1);
});
test('POLISH-1 control: no withheld keeps No saved excerpts', async () => {
  const html = await savedHtml(listOut([], { plan_withheld: 0 }), { excerpt: true });
  assert.match(html, /<h2>No saved excerpts<\/h2>/);
  assert.doesNotMatch(html, /available/);
  const b = await savedHtml(listOut([]));
  assert.match(b, /<h2>No saved excerpts<\/h2>/);
});

/* POLISH-2: the episode About Audio row names where the clips are. */
test('POLISH-2 excerpt level: Clips in the ad list when an ad row has a clip', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED));
  assert.equal(aboutAudio(html), 'Clips in the ad list');
  const none = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED, { ads: [AD_A] }));
  assert.equal(aboutAudio(none.html), 'Clip unavailable');
});
test('POLISH-2 controls: evidence page and Plan B unchanged', async () => {
  const ev = await evidence(excerptRecord());
  assert.doesNotMatch(ev.html, /Clips in the ad list/);
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(fullRecord), SCREENED, { excerpt: false }));
  assert.doesNotMatch(html, /Clips in the ad list/);
});

/* POLISH-3: a cut excerpt ends with " …" on screen only. */
test('POLISH-3 excerpt level: a truncated excerpt shows the mark', async () => {
  const cut = await evidence(excerptRecord({ truncated: true }));
  assert.ok(cut.html.includes(`<blockquote class="ew-excerpt-text">${SAFE} …</blockquote>`));
  const whole = await evidence(excerptRecord());
  assert.ok(whole.html.includes(`<blockquote class="ew-excerpt-text">${SAFE}</blockquote>`));
});
test('POLISH-3 Plan B control: no excerpt block and no mark', async () => {
  const { html } = await evidence(fullRecord({ truncated: true }));
  assert.doesNotMatch(html, /ew-excerpt-text/);
  assert.doesNotMatch(html, / …<\/blockquote>/);
});
