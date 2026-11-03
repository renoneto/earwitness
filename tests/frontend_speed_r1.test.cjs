/* SPEED-R1 (R1-16): Search List ran research-query once per view change,
   and every superseded change still ran on the server: Reno's List session
   sent six in a row (about 8 s), the last answer waiting behind the ones
   nobody waited for. A tab now has one research query in flight: a newer
   one waits unsent until the earlier is answered, and a view left while
   it waits takes its query out unsent. So:
   - one view change sends one query (Cards, List, a page, a sort);
   - quick changes (List pages 1 to 5) send the first and the last query,
     never one each, and the page shows the last one's results with the
     same markup as opening that page alone;
   - a left page's query is never sent later; an access boundary drops it;
   - other reads are not held by the lane.
   The real app scripts run in a stubbed browser over a fake fetch
   (harness from frontend_read_queue.test.cjs).
   Run: node --test tests/frontend_speed_r1.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const sleep = ms => new Promise(res => setTimeout(res, ms));

const SESSION = { ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws',
  workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' };
const reply = (status, body, headers = {}) => ({ ok: status < 400, status,
  headers: { get: k => (Object.hasOwn(headers, k) ? headers[k] : null) }, text: async () => JSON.stringify(body) });
const limited = (secs = '1') => reply(429, { ok: false, error: { code: 'RATE_LIMITED', message: 'slow down and retry' } }, { 'Retry-After': secs });
const abortError = () => { const e = new Error('The operation was aborted.'); e.name = 'AbortError'; return e; };

// FormData over a stub form's elements (frontend_demo2_pass14.test.cjs).
class FakeFormData {
  constructor(form) { this.form = form; }
  all() { return ((this.form && this.form.elements) || []).filter(el => el.name).map(el => [el.name, String(el.value ?? '')]); }
  get(k) { const e = this.all().find(([n]) => n === k); return e ? e[1] : null; }
  entries() { return this.all()[Symbol.iterator](); }
}

/* -- fixtures ------------------------------------------------------------- */
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const QUINCE = { key: 'quince', display: 'Quince', confirmed: true };
const AD = { ref: REF(1), record_kind: 'evidence', status: 'finding', show_title: 'Show A', ep_title: 'Episode One', episode_id: 'e1',
  pub_date: '2026-10-09', audio: 'unavailable', quotation_span: [60, 120], ad_break_id: 'brk:a', show: { show_identity_id: 'cs-a' },
  read: { brand: QUINCE, display_span: [60, 120], has_clip: false, needs_review: false },
  attribution: [{ brand: 'quince', basis: 'confirmed', channel: 'spoken' }] };
const OVERVIEW = { coverage: { episodes: 10, shows: 2, episodes_with_transcript: 10 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [], alert_lifecycle_counts: { active_local: 1 },
  newest_episode: { date: '2026-10-20', has_episodes: true } };
const TRACKED = { ok: true, items: [{ id: 'si-1', type: 'bookmark', title: 'quince', revision: 1, availability: 'current',
  target: { kind: 'brand', id: 'quince' } }], next_cursor: null };
const BRANDS = { brands: [{ key: 'quince', display: 'Quince', ad_breaks: 92, shows: 37, last_heard: '2026-10-19' }] };
const BUNDLE = { query: { version: 2, mode: 'ads', q: 'quince', sort: 'newest', page: 1, view: 'cards' }, revision: 'rev-1', selector: null,
  coverage: { episodes_in_scope: 62 },
  result: { items: [AD], total: 1, dated_total: 1, undated_total: 0, total_relation: 'exact', per_page: 20,
    ad_breaks: [{ id: 'brk:a', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [60, 120], read_count: 1,
      matching_read_count: 1, refs: [AD.ref], pub_date: '2026-10-09', group_id: 'brk:a', group_ad_count: 1, brands: [QUINCE] }],
    break_total: 1, break_dated_total: 1, break_undated_total: 0 },
  summary: { week: [] } };
const BRAND = { brand: { key: 'quince', brand: 'quince', display: 'Quince', ad_breaks: 2, episodes: 2, shows: 1, detections: 2,
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['quince'], shows: null, categories: null, statuses: null, audio: null,
    date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null } } } };

// Every read of the three pages, answered by path.
function answer(url, init) {
  const p = url.replace('/api/v1', '');
  if (p === '/customer/overview') return OVERVIEW;
  if (p.startsWith('/customer/saved-items')) return TRACKED;
  if (p.startsWith('/customer/search')) return { items: [AD], total: 1, total_relation: 'exact', break_total: 1 };
  if (p.startsWith('/customer/summary')) return { groups: [] };
  if (p === '/customer/brands') return BRANDS;
  if (p === '/customer/shows') return { shows: [] };
  if (p.startsWith('/customer/brands/')) return BRAND;
  if (p.startsWith('/customer/filter-values?')) return { items: [{ value: 'quince', label: 'Quince', confirmed: true }] };
  if (p === '/customer/research-query') return BUNDLE;
  if (p === '/customer/preferences') return { ok: true, preferences: { revision: 1 }, revision: 1 };
  if (p.startsWith('/customer/records/')) return { record: { ref: p.split('/').pop(), media: {} } };
  if (init && init.method && init.method !== 'GET') return { ok: true, item: { id: 1 } };
  return new Error('unexpected ' + p);
}

/* -- the counting network --------------------------------------------------
   Every customer request (not the session read) is counted from the moment
   fetch is called until its answer (or its abort). net.max is the most in
   flight at once; net.sent the order requests went out. */
function network(route, { latency = () => 15, routes = answer } = {}) {
  const net = { open: 0, max: 0, sent: [], live: true };
  const docListeners = {};
  const fetch = (url, init = {}) => {
    const method = init.method || 'GET';
    if (url === '/api/v1/session') return Promise.resolve(reply(200, SESSION));
    if (!net.live) return Promise.reject(new Error('not yet'));
    net.open += 1;
    net.max = Math.max(net.max, net.open);
    const call = { url: url.replace('/api/v1', ''), method, done: false, signal: init.signal || null, body: init.body || null };
    net.sent.push(call);
    return new Promise((resolve, reject) => {
      const end = () => { if (call.done) return false; call.done = true; net.open -= 1; return true; };
      const timer = setTimeout(async () => {
        if (!end()) return;
        try {
          const out = await routes(url, init);
          if (out instanceof Error) reject(out);
          else resolve(out && typeof out.status === 'number' && out.headers ? out : reply(200, out));
        } catch (err) { reject(err); }
      }, latency(call.url, method));
      if (init.signal) init.signal.addEventListener('abort', () => { clearTimeout(timer); if (end()) reject(abortError()); }, { once: true });
    });
  };
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, DOMException, FormData: FakeFormData,
    location: { hash: route, pathname: '/app/' }, history: { replaceState() {} }, scrollTo() {}, fetch,
    matchMedia: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  const win = sandbox;
  // Retry-After waits run at once; request timeouts (20 s and up) never fire.
  const waits = [];
  win.EW.requestTimeout.set = (fn, ms) => {
    const t = { fn, ms, done: false };
    waits.push(t);
    if (ms <= 10000) setTimeout(() => { if (!t.done) { t.done = true; fn(); } }, 0);
    return t;
  };
  win.EW.requestTimeout.clear = t => { if (t) t.done = true; };
  win.EW.current.route = route;
  return { win, net, waits, docListeners };
}

// The app with its views; the boot render reads nothing (as in the other
// view tests). page.render() renders `route` and returns its #main.
async function app(route, opts = {}) {
  const t = network(route, opts);
  t.net.live = false;
  const { win } = t;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  // opts.modalListeners: the #modal dialog's own listeners views.js adds at load.
  const loadModal = opts.modalListeners && { ...dummy,
    addEventListener: (type, fn) => { (opts.modalListeners[type] = opts.modalListeners[type] || []).push(fn); } };
  win.document.querySelector = sel => (sel === '#modal' && loadModal ? loadModal : dummy);
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settle();
  let shown = '';
  const main = { querySelectorAll: () => [], querySelector: () => null, insertAdjacentHTML() {}, setAttribute() {},
    get innerHTML() { return shown; }, set innerHTML(v) { shown = v; } };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {};
  t.net.sent.length = 0;
  t.net.max = 0;
  t.net.live = true;
  const go = async r => { win.location.hash = r; win.EW.current.route = r; await win.EWViews.render(); return main.innerHTML; };
  return { ...t, main, render: () => go(route), go };
}
const urls = net => net.sent.map(c => c.url.split('?')[0] + (c.url.includes('field=') ? '?' + c.url.match(/field=\w+/)[0] : ''));
const queue = win => ({ ...win.EW.requestQueue() });   // a plain object of this realm


const rqSent = net => net.sent.filter(c => c.url === '/customer/research-query');
const bodies = net => rqSent(net).map(c => JSON.parse(c.body).query.page);
const LIST = p => `#/search?mode=ads&q=quince&view=list&page=${p}`;
// The research query takes 120 ms; every other read 5 ms.
const slowQuery = { latency: url => (url === '/customer/research-query' ? 120 : 5) };
// Pages 1-6 exist (the bundle says 60 ads).
function paged(url, init) {
  const out = answer(url, init);
  if (url.endsWith('/customer/research-query')) {
    const page = JSON.parse(init.body).query.page;
    return { ...out, query: { ...out.query, page }, result: { ...out.result, total: 60, break_total: 60, per_page: 10,
      items: [{ ...AD, ep_title: 'Episode on page ' + page }] } };
  }
  return out;
}

test('S1 one view change sends one research query (List, Cards, a page, a sort)', async () => {
  const t = await app(LIST(1), { ...slowQuery, routes: paged });
  for (const r of [LIST(1), '#/search?mode=ads&q=quince&view=cards&page=1', LIST(2),
    '#/search?mode=ads&q=quince&view=list&page=2&sort=oldest']) {
    t.net.sent.length = 0;
    const html = await t.go(r);
    await settle();
    assert.equal(rqSent(t.net).length, 1, r);
    assert.doesNotMatch(html, /Search didn’t finish/);
  }
});

test('S2 quick List page changes send the first and the last query only, and show the last', async () => {
  const t = await app(LIST(1), { ...slowQuery, routes: paged });
  const renders = [];
  for (let p = 1; p <= 5; p++) {
    t.win.location.hash = LIST(p); t.win.EW.current.route = LIST(p);
    renders.push(t.win.EWViews.render());
    await sleep(20);
  }
  await Promise.all(renders);
  await sleep(200);
  await settle();
  assert.deepEqual(bodies(t.net), [1, 5], 'the first and the last view, never the pages in between');
  assert.match(t.main.innerHTML, /Episode on page 5/);
  // The same markup as opening page 5 alone.
  const alone = await app(LIST(5), { ...slowQuery, routes: paged });
  const html = await alone.render();
  await settle();
  assert.equal(t.main.innerHTML, html);
  assert.ok(t.net.max <= 2);
});

test('S3 the newest query waits for the one in flight, then goes; other reads do not wait', async () => {
  const t = await app(LIST(1), { ...slowQuery, routes: paged });
  const first = t.win.EWViews.render();
  await sleep(30);
  t.win.location.hash = LIST(2); t.win.EW.current.route = LIST(2);
  const second = t.win.EWViews.render();
  await sleep(30);
  assert.deepEqual(bodies(t.net), [1], 'page 2 waits while page 1 is in flight');
  assert.ok(t.net.sent.some(c => c.url === '/customer/brands' && t.net.sent.indexOf(c) > 0),
    'other reads still go');
  await Promise.all([first, second]);
  await sleep(150);
  await settle();
  assert.deepEqual(bodies(t.net), [1, 2]);
  assert.match(t.main.innerHTML, /Episode on page 2/);
});

test('S4 a page left while its query waits never sends it later', async () => {
  const t = await app(LIST(1), { ...slowQuery, routes: paged });
  const first = t.win.EWViews.render();
  await sleep(30);
  t.win.location.hash = LIST(2); t.win.EW.current.route = LIST(2);
  const second = t.win.EWViews.render();
  await sleep(10);
  t.win.location.hash = '#/brands'; t.win.EW.current.route = '#/brands';
  const third = t.win.EWViews.render();
  await Promise.all([first, second, third]);
  await sleep(300);
  await settle();
  assert.deepEqual(bodies(t.net), [1], 'page 2 left before its turn: never sent');
});

test('S5 an access boundary drops a waiting query unsent', async () => {
  const t = network(LIST(1), slowQuery);
  await settle();
  const body = { query: { version: 2, mode: 'ads', q: 'x', page: 1 }, selector: null, summaries: [] };
  const a = t.win.EW.api('/customer/research-query', { method: 'POST', body });
  const b = t.win.EW.api('/customer/research-query', { method: 'POST', body: { ...body, query: { ...body.query, page: 2 } } });
  await sleep(20);
  assert.equal(rqSent(t.net).length, 1);
  t.win.EW.authorityBoundary();
  await assert.rejects(b);
  await a.catch(() => {});
  await sleep(200);
  assert.equal(rqSent(t.net).length, 1, 'the waiting query was never sent');
});

/* SPEED-FIX1 (SPC-R1-07): "Preview matches" in the alert dialog passes an
   abort signal, so a superseded press is dropped: unsent while it waits
   in the lane; in flight, its answer is never painted (READ-QUEUE lets a
   sent request finish, so its place stays counted). The box shows the
   last press's count and never a failure for a dropped one. */
function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, ...props };
}
const previewed = (url, init) => {
  const p = url.replace('/api/v1', '');
  if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return { alerts: [] };
  if (p === '/customer/research-query') {
    const q = JSON.parse(init.body).query.q;
    return { ...BUNDLE, query: { ...BUNDLE.query, q }, result: { ...BUNDLE.result, total: q.length, break_total: q.length } };
  }
  return answer(url, init);
};

test('S6 quick Preview presses send the first and the last query; the box shows the last count', async () => {
  const t = await app('#/search?mode=ads&q=quince', { ...slowQuery, routes: previewed });
  await t.render();
  await settle();
  const modal = node({ open: false, showModal() { this.open = true; }, close() { this.open = false; },
    querySelector: sel => (sel === 'button' ? { focus() {} } : null) });
  const paints = [];
  let painted = '';
  const box = node({ id: 'alert-preview' });
  Object.defineProperty(box, 'innerHTML', { get: () => painted, set: v => { painted = v; paints.push(v); } });
  const words = node({ name: 'q', value: 'a', type: 'text' });
  const form = node({ id: 'alert-form', elements: [['mode', 'ads'], ['brand', ''], ['show', ''], ['status', ''], ['audio', ''],
    ['from', ''], ['to', ''], ['initial_query', JSON.stringify({ version: 2, mode: 'ads', q: '' })], ['initial_selector', 'null'],
    ['touched_fields', '["q"]']].map(([name, value]) => ({ name, value, type: 'text' })).concat([words]) });
  const main = t.main;
  t.win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#alert-form' ? form
    : sel === '#alert-preview' ? box : node());
  await t.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  await settle();
  t.net.sent.length = 0;
  const presses = [];
  for (const q of ['aa', 'aaa', 'aaaaa']) {
    words.value = q;
    presses.push(t.win.EWViews.handleAction({ dataset: { action: 'alert-preview' } }));
    await sleep(20);
  }
  await Promise.all(presses);
  await sleep(300);
  await settle();
  const sent = rqSent(t.net).map(c => JSON.parse(c.body).query.q);
  assert.deepEqual(sent, ['aa', 'aaaaa'], 'the middle press waited in the lane and was dropped unsent');
  assert.equal(box.dataset.previewState, 'result');
  assert.match(box.innerHTML, /Preview: 5 matching ads/);
  const results = paints.filter(p => /Preview:|failed/i.test(p));
  assert.deepEqual(results.map(p => (p.match(/Preview: (\d+)/) || [p])[1] || p), ['5'],
    'only the last press paints: no count of a superseded press, no failure');
});

/* SPEED-FIX2 (SPC-C-05): closing the alert dialog, or editing its
   criteria, drops a preview still waiting in the research lane: it is
   never sent, and the next query does not wait behind it. */
async function previewWaiting(t) {
  await t.render();
  await settle();
  const listeners = {};
  const box = node({ id: 'alert-preview' });
  const words = node({ name: 'q', value: 'aa', type: 'text' });
  const form = node({ id: 'alert-form', elements: [['mode', 'ads'], ['brand', ''], ['show', ''], ['status', ''], ['audio', ''],
    ['from', ''], ['to', ''], ['initial_query', JSON.stringify({ version: 2, mode: 'ads', q: '' })], ['initial_selector', 'null'],
    ['touched_fields', '["q"]']].map(([name, value]) => ({ name, value, type: 'text' })).concat([words]),
    querySelector: sel => (sel === '#alert-preview' ? box : null),
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); } });
  const modal = node({ open: false, showModal() { this.open = true; }, close() { this.open = false; },
    querySelector: sel => (sel === 'button' ? { focus() {} } : sel === '#alert-form' ? form : sel === '#alert-preview' ? box
      : sel === '.dialog-body' ? node({ appendChild() {} }) : null),
    querySelectorAll: () => [] });
  const main = t.main;
  t.win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#alert-form' ? form
    : sel === '#alert-preview' ? box : node());
  await t.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  await settle();
  t.net.sent.length = 0;
  // A Search query in flight: the preview waits behind it in the lane.
  const held = t.win.EW.api('/customer/research-query', { method: 'POST',
    body: { query: { version: 2, mode: 'ads', q: 'held' }, selector: null, summaries: [], refresh_selector: false } });
  await sleep(10);
  const press = t.win.EWViews.handleAction({ dataset: { action: 'alert-preview' } });
  await sleep(10);
  return { box, words, listeners, modal, held, press };
}

test('S7 closing the alert dialog drops its waiting preview unsent', async () => {
  const t = await app('#/search?mode=ads&q=quince', { ...slowQuery, routes: previewed });
  const w = await previewWaiting(t);
  w.modal.open = true;
  await t.win.EWViews.handleAction({ dataset: { action: 'close-dialog' } });
  await w.held;
  await w.press;
  await sleep(300);
  await settle();
  assert.deepEqual(rqSent(t.net).map(c => JSON.parse(c.body).query.q), ['held'], 'the closed dialog\'s preview was never sent');
});

test('S9 the dialog\'s own close event (Escape) drops its waiting preview unsent', async () => {
  const modalListeners = {};
  const t = await app('#/search?mode=ads&q=quince', { ...slowQuery, routes: previewed, modalListeners });
  assert.ok((modalListeners.close || []).length, 'views.js listens to the dialog\'s close event');
  const w = await previewWaiting(t);
  // Escape closes the <dialog> itself: no close-dialog action, only its close event.
  w.modal.open = false;
  for (const fn of modalListeners.close) fn({ type: 'close' });
  await w.held;
  await w.press;
  await sleep(300);
  await settle();
  assert.deepEqual(rqSent(t.net).map(c => JSON.parse(c.body).query.q), ['held'], 'the Escape-closed dialog\'s preview was never sent');
});

test('S8 a criteria edit drops the waiting preview unsent', async () => {
  const t = await app('#/search?mode=ads&q=quince', { ...slowQuery, routes: previewed });
  const w = await previewWaiting(t);
  w.words.value = 'aaa';
  for (const fn of w.listeners.input || []) fn({ target: w.words });
  await w.held;
  await w.press;
  await sleep(300);
  await settle();
  assert.deepEqual(rqSent(t.net).map(c => JSON.parse(c.body).query.q), ['held'], 'the edited criteria\'s old preview was never sent');
  assert.notEqual(w.box.dataset.previewState, 'result');
});
