/* Plan A excerpt view screens (design docs/plans/2026-10-22-plan-a-excerpt-view.md
   revision 4, section 8): the evidence page shows the Excerpt block and no
   source text or window paging, the episode page lists ads with excerpts
   and says full transcripts are not included, the All mentions tab is
   hidden, the export notice names ad details and excerpts and an old artifact
   refused by the plan says so. Loader helpers copied from
   frontend_dv3_ux_evidence.test.cjs. The clip fallback string is left to
   lr-clips-ui (owner). Run: node --test tests/frontend_plan_a.test.cjs */
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
const excerptRecord = (over = {}) => ({ ref: REF, disclosure: 'excerpt', record_kind: 'evidence', status: 'finding',
  text: SAFE, excerpt_bounds: { start_s: 25, end_s: 50 },
  review: { judgment: 'finding', bounds_verified: 0 }, attribution: [{ brand: 'Lumen', basis: 'spoken', channel: 'spoken' }],
  relationship: 'current', availability: 'available', bounds_basis: 'original',
  original_bounds: { start_s: 30, end_s: 45 }, effective_bounds: { start_s: 30, end_s: 45 }, selected_bounds: { start_s: 30, end_s: 45 },
  episode: { episode_id: 'ep_a', show_title: 'Morning Show', ep_title: 'Monday', pub_date: '2026-09-28', transcript_version_id: 2, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Morning Show', unresolved: 0 },
  evidence: { id: 'ad-a1', episode_id: 'ep_a', transcript_version_id: 2, orig_start_s: 30, orig_end_s: 45 },
  media: { playback: 'unavailable', reason: 'clip unavailable' }, read: null, ...over });
const api = rec => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_a')) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_a') return { episode: { episode_id: 'ep_a', excerpt_only: true, ads: [
    { ref: REF, evidence_id: 'ad-a1', status: 'finding', span: [30, 45], display_span: [30, 45], brand: { key: 'lumen', display: 'Lumen' },
      needs_review: false, self_promo: false, kind: 'rule', source_label: 'Rule', media: null, transcript_start_s: 30,
      transcript_version_id: 2, on_current_transcript: true, text: SAFE, excerpt_bounds: { start_s: 25, end_s: 50 } },
    { ref: 'sr1:d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4', evidence_id: 'ad-a2', status: 'finding', span: [200, 210], display_span: [200, 210],
      brand: { key: 'acme', display: 'Acme' }, needs_review: false, self_promo: false, kind: 'rule', source_label: 'Rule', media: null,
      transcript_start_s: 200, transcript_version_id: 2, on_current_transcript: true, text: null, excerpt_bounds: null }],
    self_promo: [], ad_breaks: [], screening: null } };
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
};

test('the evidence page shows the Excerpt block and no source text or window paging', async () => {
  const { html } = await page('#/evidence/' + encodeURIComponent(REF), api(excerptRecord()));
  assert.match(html, /<h2[^>]*>Excerpt<\/h2>/);
  assert.ok(html.includes(SAFE));
  assert.doesNotMatch(html, /What was said/);
  assert.doesNotMatch(html, /Open in the full transcript/);
  assert.doesNotMatch(html, /Load the next part/);
  assert.doesNotMatch(html, /TODO\(copy\)/, 'final copy (Sol r1)');
});

test('at excerpt level the no-clip note does not point to a transcript', async () => {
  const { html } = await page('#/evidence/' + encodeURIComponent(REF), api(excerptRecord()));
  assert.match(html, /No clip for this ad\./);
  assert.doesNotMatch(html, /review the transcript/i);
});

test('an ad without a safe excerpt says Excerpt unavailable', async () => {
  const { html } = await page('#/evidence/' + encodeURIComponent(REF), api(excerptRecord({ text: null, excerpt_bounds: null })));
  assert.match(html, /Excerpt unavailable/);
});

test('the episode page lists ads with excerpts and says full transcripts are not included', async () => {
  const rec = excerptRecord({ ref: 'sr1:e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5', record_kind: 'source_publication', text: null,
    excerpt_bounds: null, evidence: {}, ads: [] });
  const { html } = await page('#/episode/ep_a', api(rec));
  assert.match(html, /Full transcripts are not included in your plan\./);
  assert.ok(html.includes(SAFE));
  assert.match(html, /Excerpt unavailable/);
  assert.doesNotMatch(html, /Jump to transcript/);
  assert.doesNotMatch(html, /Find in loaded transcript/);
});

test('search hides the All mentions tab and the export notice says excerpts only', async () => {
  const { win } = await page('#/evidence/' + encodeURIComponent(REF), api(excerptRecord()));
  assert.equal(win.EW.current.excerptOnly, true);
  const head = win.EWViews._planA.searchTabs('ads');
  assert.doesNotMatch(head, /All mentions/);
  assert.match(head, /Episodes/);
  assert.match(win.EWViews._planA.exportNotice(), /Includes ad details and excerpts\. Full transcripts are not included\./);
});

test('an old artifact refused by the plan shows Not available on your plan', async () => {
  const { win } = await page('#/evidence/' + encodeURIComponent(REF), api(excerptRecord()));
  assert.match(win.EWViews._planA.refusalText('plan'), /Not available on your plan/);
  const err = Object.assign(new Error('not available on your plan'), { status: 403 });
  assert.equal(win.EWUI.planRefusal(err), true);
  assert.equal(win.EWUI.planRefusal(Object.assign(new Error('source disclosure denied'), { status: 403 })), false);
});

/* Review round 1 (code MED-1, Astra LOW-1): the Search page's own route
   carries the plan signal, even with zero results; a full response clears
   it and a workspace switch drops it. */
const searchBundle = (mode, extra = {}) => ({ query: { version: 2, mode, q: '', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items: [], total: 0, per_page: 10, total_relation: 'exact', dated_total: 0, undated_total: 0, ...extra },
  coverage: { episodes_in_scope: 4 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  throw new Error('unexpected ' + p);
};
const tabsOf = html => (html.match(/data-mode="mentions"/g) || []).length;

test('a cold Plan A search with zero results hides All mentions and says excerpts only', async () => {
  const { win, html } = await page('#/search?mode=ads', searchApi(searchBundle('ads', { excerpt_only: true })));
  assert.equal(win.EW.current.excerptOnly, true);
  assert.equal(tabsOf(html), 0);
  assert.match(win.EWViews._planA.exportNotice(), /Includes ad details and excerpts\. Full transcripts are not included\./);
});

test('a full Search response clears the plan flag (after an upgrade)', async () => {
  const { win, main } = await page('#/search?mode=ads', searchApi(searchBundle('ads', { excerpt_only: true })));
  assert.equal(win.EW.current.excerptOnly, true);
  win.EW.api = async p => searchApi(searchBundle('ads'))(p);
  await win.EWViews.render();
  assert.equal(win.EW.current.excerptOnly, false);
  assert.ok(tabsOf(main.innerHTML) > 0);
  assert.doesNotMatch(win.EWViews._planA.exportNotice(), /Includes ad details and excerpts\. Full transcripts are not included\./);
});

test('a workspace switch drops the plan flag', async () => {
  const { win } = await page('#/search?mode=ads', searchApi(searchBundle('ads', { excerpt_only: true })));
  assert.doesNotMatch(win.EWViews._planA.searchTabs('ads'), /All mentions/);
  win.EW.current.workspaceContext = 'ctx-other';
  win.EW.clearWorkspaceState();
  assert.match(win.EWViews._planA.searchTabs('ads'), /All mentions/);
  assert.doesNotMatch(win.EWViews._planA.exportNotice(), /Includes ad details and excerpts\. Full transcripts are not included\./);
});
