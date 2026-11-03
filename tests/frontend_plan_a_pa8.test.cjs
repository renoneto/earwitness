/* Plan A excerpt view, PA fix round 8: the Saved list's plan-withheld
   line and the last transcript wording on the episode page at excerpt
   level, with Plan B controls. Loader helpers copied from
   frontend_plan_a_sol_r1.test.cjs. Run: node --test tests/frontend_plan_a_pa8.test.cjs */
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

/* Item 1: one page-level line for saved items withheld by the plan. */
async function savedHtml(out) {
  const win = loadViews('#/saved');
  await flushBoot();
  const E = win.EW;
  const api = async p => {
    if (p.startsWith('/customer/saved-items')) return out;
    return { items: [], total: 0, total_relation: 'exact', counts: {} };
  };
  return win.EWSaved.render({ api, esc: E.esc, link: E.link, scope: { query: { q: '' }, selector: null },
    workspaceContext: '', signal: null, isCurrent: true });
}
const ITEM = { id: 'si-1', type: 'excerpt', title: 'Kept read', note: '', revision: 1, saved_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z', target: { ref: REF }, availability: 'current',
  captured: { source_ref: REF, text: 'kept words', source_kind: 'evidence' } };
const listOut = (items, over = {}) => ({ items, total: items.length, total_relation: 'exact',
  counts: { excerpt: items.length, query: 0, bookmark: 0 }, collection_revision: 'r', next_cursor: null, ...over });

test('item 1 plural: N saved items are not available on your plan, with the list kept', async () => {
  const html = await savedHtml(listOut([ITEM], { plan_withheld: 3 }));
  assert.match(html, /3 saved items are not available on your plan\./);
  assert.equal((html.match(/not available on your plan/g) || []).length, 1, 'one page-level line');
  assert.match(html, /<ul class="ew-card saved-list">/);
  assert.match(html, /Kept read/);
});
test('item 1 singular: 1 saved item is not available on your plan', async () => {
  const html = await savedHtml(listOut([], { plan_withheld: 1 }));
  assert.match(html, /1 saved item is not available on your plan\./);
  assert.doesNotMatch(html, /1 saved items/);
});
test('item 1 no line at 0 or when absent', async () => {
  for (const over of [{ plan_withheld: 0 }, {}]) {
    const html = await savedHtml(listOut([ITEM], over));
    assert.doesNotMatch(html, /not available on your plan/);
    assert.match(html, /Kept read/);
  }
});

/* Item 2: the screening line and the ad rows' hidden clip text. */
const LOCAL = { ...AD_A, ref: 'sr1:d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4', evidence_id: 'ad-a2', span: [60, 75], display_span: [60, 75],
  transcript_start_s: 60, media: { playback: 'local', source_offset_s: 0 } };
const epApi = (rec, screening, { excerpt = true } = {}) => p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_a')) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_a') {
    const ex = a => (excerpt ? { ...a, text: SAFE, excerpt_bounds: { start_s: a.span[0] - 5, end_s: a.span[1] + 5 } } : a);
    return { episode: { episode_id: 'ep_a', excerpt_only: excerpt, ads: [ex(AD_A), ex(LOCAL)],
      self_promo: [], ad_breaks: [], screening } };
  }
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
};
const status = html => ((html.match(/<p class="ew-episode-status">[\s\S]*?<span>([^<]+)<\/span><a href="\/coverage\/">/) || [])[1]);
const SCREENED = { jev_screened: true, coverage_tier: 'full', label: 'Screened' };
const NO_ADS = { transcript_only: true, coverage_tier: 'transcript', label: 'Transcript only' };

test('item 2 excerpt level: Ads checked. and Not checked for ads. (PA9 FIX-1)', async () => {
  const a = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED));
  assert.equal(status(a.html), 'Ads checked.');
  assert.doesNotMatch(a.html, /Transcript and ads checked/);
  const b = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), NO_ADS));
  assert.equal(status(b.html), 'Not checked for ads.');
  assert.doesNotMatch(b.html, /Searchable; alerts read it/);
});
test('item 2 excerpt level: an ad row without a clip says Clip unavailable to screen readers', async () => {
  const { html } = await page('#/episode/ep_a', epApi(episodeRec(excerptRecord), SCREENED));
  assert.match(html, /<span class="visually-hidden">Clip unavailable<\/span>/);
  assert.doesNotMatch(html, /No clip \(transcript only\)/);
});
test('item 2 Plan B control: the transcript wording is unchanged', async () => {
  const a = await page('#/episode/ep_a', epApi(episodeRec(fullRecord), SCREENED, { excerpt: false }));
  assert.equal(status(a.html), 'Transcript and ads checked.');
  assert.match(a.html, /<span class="visually-hidden">No clip \(transcript only\)<\/span>/);
  const b = await page('#/episode/ep_a', epApi(episodeRec(fullRecord), NO_ADS, { excerpt: false }));
  assert.equal(status(b.html), 'Searchable; alerts read it; no ad list.');
});
