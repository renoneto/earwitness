/* Plan A excerpt view, PA fix round 16: Muse round 3 (pa-r3) FIX-1, POLISH-1, POLISH-2.
   Helpers are copied from the PA14 test by loading its source text.
   Run: node --test tests/frontend_plan_a_pa16.test.cjs */
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
async function page(route, api, { q = null } = {}) {
  const win = loadViews(route);
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, opts = {}) => api(p, opts);
  win.location.hash = route;
  win.EW.current.route = route;
  if (q !== null) win.EW.current.filters = { ...(win.EW.current.filters || {}), q };
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const asPlanA = win => win.EW.notePlan({ excerpt_only: true });

/* FIX-1: the search inspector at excerpt level, for an ad with no quote. */
const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
const ITEM = { ref: REF, record_kind: 'evidence', status: 'finding', span: [30, 45], display_span: [30, 45],
  ep_title: 'Monday', show_title: 'Morning Show', pub_date: '2026-09-28', media: null,
  read: { kind: 'named', brand: { key: 'glimmer', display: 'Glimmer', confirmed: true }, needs_review: false, self_promo: false, display_span: [30, 45] } };
async function inspector(item, planA) {
  const win = loadViews('#/search');
  await flushBoot();
  if (planA) asPlanA(win);
  return win.EWViews.inspectorHtml(item, { q: '' }, 1);
}
test('FIX-1 excerpt level, no quote: no Save excerpt and no passage claim; clip note stays', async () => {
  const html = await inspector(ITEM, true);
  assert.doesNotMatch(html, /Save excerpt/);
  assert.doesNotMatch(html, /passage is on the evidence page/);
  assert.match(html, /class="ew-inspector-noclip">[^<]*[A-Za-z]/);
  assert.match(html, /Open evidence/);
});
test('FIX-1 excerpt level, with a quote: Save excerpt stays', async () => {
  const html = await inspector({ ...ITEM, text: 'brought to you by Glimmer' }, true);
  assert.match(html, /Save excerpt/);
});
test('FIX-1 Plan B control: no quote still offers Save excerpt and the passage sentence', async () => {
  const html = await inspector(ITEM, false);
  assert.match(html, /Save excerpt/);
  assert.match(html, /The passage is on the evidence page\./);
});

/* POLISH-1 and POLISH-2: the plan-limited All mentions page. */
const searchBundle = (extra = {}) => ({ query: { version: 2, mode: 'mentions', q: 'lumen', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items: [], total: 0, per_page: 10, total_relation: 'exact', dated_total: 0, undated_total: 0, ...extra },
  coverage: { episodes_in_scope: 4 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/categories') return { categories: [] };
  if (p === '/customer/research-query') return b;
  throw new Error('unexpected ' + p);
};
const scopeP = html => (html.match(/<p class="ew-scope-line">[\s\S]*?<\/p>/) || [''])[0];
const exportBtn = html => (html.match(/<button[^>]*id="export-open"[^>]*>|<button[^>]*data-action="export-open"[^>]*>/) || [''])[0];
const limited = () => page('#/search?mode=mentions&q=lumen', searchApi(searchBundle({ excerpt_only: true, plan_limited: true })), { q: 'lumen' });
test('POLISH-1 plan limited: the disabled Export points at the plan card heading', async () => {
  const { html } = await limited();
  const btn = exportBtn(html);
  assert.ok(btn, 'export button present');
  assert.match(btn, /disabled/);
  const id = (btn.match(/aria-describedby="([^"]+)"/) || [])[1];
  assert.ok(id, 'aria-describedby present');
  assert.match(html, new RegExp(`<h2[^>]*id="${id}"[^>]*>All mentions is not included in your plan\\.</h2>`));
});
test('POLISH-1 Plan B control: an empty search keeps export-open-why', async () => {
  const { html } = await page('#/search?mode=mentions&q=lumen', searchApi(searchBundle()), { q: 'lumen' });
  assert.match(exportBtn(html), /aria-describedby="export-open-why"/);
  assert.match(html, /id="export-open-why"/);
});
test('POLISH-2 plan limited: no tier tag in the scope line', async () => {
  const { html } = await limited();
  assert.doesNotMatch(scopeP(html), /data-tag="tier"/);
  assert.doesNotMatch(scopeP(html), /Full \+ transcript only/);
  assert.match(scopeP(html), /4 episodes in scope\./);
});
test('POLISH-2 Plan B control: an empty mentions search keeps the tier tag', async () => {
  const { html } = await page('#/search?mode=mentions&q=lumen', searchApi(searchBundle()), { q: 'lumen' });
  assert.match(scopeP(html), /Full \+ transcript only/);
});
