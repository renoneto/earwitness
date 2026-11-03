/* Plan A excerpt view, PA fix round 12: remaining mentions and transcript entry points.
   Helpers copied from frontend_plan_a_pa11.test.cjs.
   Run: node --test tests/frontend_plan_a_pa12.test.cjs */
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

/* Item 1: brand search page with no match. */
const dirApi = p => {
  if (p.startsWith('/customer/brands')) return { brands: [] };
  throw new Error('unexpected ' + p);
};
const dirPre = planA => win => { if (planA) asPlanA(win); win.EW.current.directory = { brands: 'zzz', shows: '' }; };
test('item 1, excerpt level: no Search all mentions action on the empty brand search', async () => {
  const { html } = await page('#/brands', dirApi, { pre: dirPre(true) });
  assert.match(html, /No brand called/);
  assert.doesNotMatch(html, /Search all mentions/);
  assert.doesNotMatch(html, /mode=mentions/);
  assert.match(html, /Clear the filter/);
});
test('item 1, Plan B control: action stays', async () => {
  const { html } = await page('#/brands', dirApi, { pre: dirPre(false) });
  assert.match(html, /Search all mentions of “zzz”/);
});

/* Item 2: brand page All mentions link. */
const brandApi = p => {
  if (p === '/customer/brands/lumen') return { brand: { brand: 'lumen', key: 'lumen', display: 'Lumen', detections: 9,
    confirmed_occurrences: [], findings: [], offer_sequence: [], episodes: 4, shows: 3, last_heard: '2026-10-09',
    drill_down: { ads: { version: 2, mode: 'ads', brands: ['lumen'] }, mentions: { version: 2, mode: 'mentions', brands: ['lumen'] } },
    top_shows: [], reads_without_show: 0 } };
  if (p.startsWith('/customer/search?')) return { items: [], total: 0, total_relation: 'exact' };
  if (p.startsWith('/customer/summary?')) return { cells: [], total_relation: 'exact' };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  throw new Error('unexpected ' + p);
};
test('item 2, excerpt level: no All mentions link on the brand page', async () => {
  const { html } = await page('#/brand/lumen', brandApi, { pre: asPlanA });
  assert.match(html, /<h2 id="reads-h">Recent ads<\/h2>/, 'the page rendered');
  assert.doesNotMatch(html, /All mentions of|Confirmed ads only/);
});
test('item 2, Plan B control: link stays', async () => {
  const { html } = await page('#/brand/lumen', brandApi);
  assert.match(html, /All mentions of Lumen/);
});

/* Item 3: research campaign coverage line. */
const researchApi = p => {
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study' } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [], allocation: {},
    targets: [{ id: 't1', scope: 'episode', episode_id: 'e1', evidence: [] }],
    coverage_by_placement: { t1: { state: 'complete', episodes: ['e1'], missing: [], failed: [] } } } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  throw new Error('unexpected ' + p);
};
test('item 3, excerpt level: coverage line says screened, no transcript search', async () => {
  const { html } = await page('#/campaign/2', researchApi, { pre: asPlanA });
  assert.match(html, /Every one is screened for ads; transcripts are not included in your plan\./);
  assert.doesNotMatch(html, /complete transcript search/);
});
test('item 3, Plan B control: line unchanged', async () => {
  const { html } = await page('#/campaign/2', researchApi);
  assert.match(html, /Every one has a complete transcript search\./);
});

/* Item 4: help entry on Settings. */
const settingsApi = p => {
  if (p === '/customer/preferences') return { preferences: { revision: 1, workspace_name: 'Studio' } };
  throw new Error('unexpected ' + p);
};
test('item 4, excerpt level: no All mentions help entry', async () => {
  const { html } = await page('#/settings', settingsApi, { pre: asPlanA });
  assert.match(html, /<dt>Ads<\/dt>/);
  assert.match(html, /<dt>Episodes<\/dt>/);
  assert.doesNotMatch(html, /<dt>All mentions<\/dt>/);
  assert.doesNotMatch(html, /Transcript passages that say the words/);
});
test('item 4, Plan B control: entry stays', async () => {
  const { html } = await page('#/settings', settingsApi);
  assert.match(html, /<dt>All mentions<\/dt>/);
});

/* Item 5: empty-state prompt for All mentions. */
const searchApi = p => {
  if (p.startsWith('/customer/brands')) return { brands: [] };
  if (p.startsWith('/customer/shows')) return { shows: [] };
  throw new Error('unexpected ' + p);
};
test('item 5, excerpt level: the All mentions prompt does not render', async () => {
  const { html } = await page('#/search?mode=mentions', searchApi, { pre: asPlanA });
  assert.doesNotMatch(html, /Type words to search all transcripts/);
  assert.doesNotMatch(html, /All mentions lists the passages/);
});
test('item 5, Plan B control: prompt renders', async () => {
  const { html } = await page('#/search?mode=mentions', searchApi);
  assert.match(html, /Type words to search all transcripts/);
});

/* Item 6: evidence page jump. Already gated; pin it. */
const evApi = rec => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  throw new Error('unexpected ' + p);
};
test('item 6, excerpt level: jump says Jump to excerpt, never the words', async () => {
  const { html } = await page('#/evidence/' + encodeURIComponent(REF), evApi(excerptRecord()));
  assert.match(html, /Jump to excerpt/);
  assert.doesNotMatch(html, /Jump to the words/);
});
