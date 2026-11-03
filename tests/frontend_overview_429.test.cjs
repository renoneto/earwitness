/* OVERVIEW-429 (DEP7): the hosted server allows 2 concurrent requests per
   browser session. The Overview starts 3 reads at once, so one of them
   gets 429 with Retry-After. A rate-limited READ is retried in the shared
   fetch wrapper after Retry-After: at most 2 retries, at most 3 s of total
   waiting, never after an abort, never for a write. A page shows its error
   only after the retries fail.
   The real app scripts run in a stubbed browser over a stubbed fetch.
   Run: node --test tests/frontend_overview_429.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };

const SESSION = { ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws',
  workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' };
const reply = (status, body, headers = {}) => ({ ok: status < 400, status,
  headers: { get: k => (Object.hasOwn(headers, k) ? headers[k] : null) }, text: async () => JSON.stringify(body) });
const limited = (secs = '1') => reply(429, { ok: false, error: { code: 'RATE_LIMITED', message: 'slow down and retry' } }, { 'Retry-After': secs });
const OVERVIEW = { coverage: { episodes: 1, shows: 1, episodes_with_transcript: 1 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [] };
const TRACKED = { ok: true, items: [{ id: 'si-1', type: 'bookmark', title: 'quince', revision: 1, availability: 'current',
  target: { kind: 'brand', id: 'quince' } }], next_cursor: null };
const BRANDS = { brands: [{ key: 'quince', display: 'Quince', ad_breaks: 4 }] };

// Loads the app in a sandbox. Timers the app asks for through
// E.requestTimeout are recorded; `fire` decides whether one runs now.
function load(route, routes, { fire = () => true } = {}) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method || 'GET' });
    if (url === '/api/v1/session') return reply(200, SESSION);
    const out = await routes(url, init);
    if (out instanceof Error) throw out;
    return out && typeof out.status === 'number' && out.headers ? out : reply(200, out);
  };
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, DOMException,
    location: { hash: route, pathname: '/app/' }, history: { replaceState() {} }, scrollTo() {}, fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  const win = sandbox;
  const waits = [];
  win.EW.requestTimeout.set = (fn, ms) => {
    const t = { fn, ms, done: false };
    waits.push(t);
    if (fire(ms)) setTimeout(() => { if (!t.done) { t.done = true; fn(); } }, 0);
    return t;
  };
  win.EW.requestTimeout.clear = t => { if (t) t.done = true; };
  win.EW.current.route = route;
  return { win, calls, waits };
}

async function overview(routes) {
  // Request timeouts (20 s and up) never fire; shorter waits run at once.
  // The boot render reads nothing (as in the other view tests); the
  // counted requests start with the test's own render.
  let live = false;
  const t = load('#/overview', (url, init) => (live ? routes(url, init) : new Error('not yet')), { fire: ms => ms <= 10000 });
  const { win } = t;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settle();
  // Every publication of #main is kept with the requests sent so far:
  // the FIRST page a person sees is the one that matters.
  const pages = [];
  let shown = '';
  const main = { querySelectorAll: () => [], querySelector: () => null, insertAdjacentHTML() {},
    get innerHTML() { return shown; },
    set innerHTML(v) { shown = v; if (/Tracked brands/.test(v)) pages.push({ html: v, saved: savedCalls(t.calls), waits: retryWaits(t.waits) }); } };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.location.hash = '#/overview';
  win.EW.current.route = '#/overview';
  t.calls.length = 0;
  t.waits.length = 0;
  live = true;
  return { ...t, main, pages, render: async () => { await win.EWViews.render(); return pages.length ? pages[0].html : main.innerHTML; } };
}
const overviewRoutes = (saved) => url => {
  if (url === '/api/v1/customer/overview') return OVERVIEW;
  if (url.startsWith('/api/v1/customer/search')) return { items: [] };
  if (url === '/api/v1/customer/saved-items?type=bookmark&limit=20') return saved();
  if (url === '/api/v1/customer/brands') return BRANDS;
  throw new Error('unexpected ' + url);
};
const savedCalls = calls => calls.filter(c => c.url.startsWith('/api/v1/customer/saved-items')).length;
const retryWaits = waits => waits.filter(w => w.ms <= 3000).map(w => w.ms);
// "Always 429", but an unbounded retry loop ends (and fails the counts)
// after 10 requests instead of hanging the suite.
const alwaysLimited = (secs = '1') => { let n = 0; return () => (++n > 10 ? reply(200, { ok: true, items: [], next_cursor: null, brands: [] }) : limited(secs)); };

test('429-1 one 429 on the tracked list: the Overview shows the tracked brands, not the error', async () => {
  let n = 0;
  const t = await overview(overviewRoutes(() => (n++ === 0 ? limited('1') : TRACKED)));
  const html = await t.render();
  assert.doesNotMatch(html, /Tracked brands didn’t load/, 'the 429 was retried before the page showed an error');
  assert.match(html, /Tracked brands/);
  assert.match(html, /#\/brand\/quince/, 'the tracked brand is listed');
  assert.match(html, />Quince</);
  assert.doesNotMatch(html, /data-auto-retry/, 'no "Trying again automatically" note after a read that recovered');
  assert.equal(t.pages[0].saved, 2, 'one retry before the first page');
  assert.deepEqual(t.pages[0].waits, [1000], 'waited the server Retry-After');
});

test('429-2 a persistent 429 still shows the error, after at most 2 retries and 3 s of waiting', async () => {
  const t = await overview(overviewRoutes(alwaysLimited('1')));
  const html = await t.render();
  assert.match(html, /Tracked brands didn’t load/, 'the error shows once the retries fail');
  assert.equal(t.pages[0].saved, 3, 'first try plus 2 retries before the error shows');
  assert.deepEqual(t.pages[0].waits, [1000, 1000], 'two Retry-After waits, 2 s in all');
});

test('429-3 the 3 s cap: Retry-After 2 retries once; Retry-After 4 never', async () => {
  for (const [secs, expected] of [['2', 2], ['4', 1]]) {
    const t = load('#/brands', alwaysLimited(secs));
    await settle();
    const err = await t.win.EW.api('/customer/brands').then(() => null, e => e);
    assert.equal(err && err.status, 429, `Retry-After ${secs}: still fails`);
    assert.equal(t.calls.filter(c => c.url === '/api/v1/customer/brands').length, expected, `Retry-After ${secs}: ${expected} request(s)`);
    const total = retryWaits(t.waits).reduce((a, b) => a + b, 0);
    assert.ok(total <= 3000, `Retry-After ${secs}: waited ${total} ms`);
  }
});

test('429-4 a write that gets 429 is never retried', async () => {
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const t = load('#/brands', () => limited('1'));
    await settle();
    const err = await t.win.EW.api('/customer/saved-items', { method, body: {} }).then(() => null, e => e);
    assert.equal(err && err.status, 429, `${method}: fails`);
    assert.equal(t.calls.filter(c => c.url === '/api/v1/customer/saved-items').length, 1, `${method}: sent once`);
  }
  // The read-only research query is a POST too: not retried either.
  const t = load('#/search', () => limited('1'));
  await settle();
  await t.win.EW.api('/customer/research-query', { method: 'POST', body: {} }).catch(() => {});
  assert.equal(t.calls.filter(c => c.url === '/api/v1/customer/research-query').length, 1);
});

test('429-5 an aborted read is not retried', async () => {
  // Aborted while waiting for Retry-After: rejects as aborted, no new request.
  const t = load('#/brands', () => limited('1'), { fire: () => false });
  await settle();
  const ctl = new AbortController();
  const read = t.win.EW.api('/customer/brands', { signal: ctl.signal }).then(() => null, e => e);
  await settle();
  assert.equal(retryWaits(t.waits).length, 1, 'fixture: waiting for Retry-After');
  ctl.abort();
  const err = await Promise.race([read, settle().then(() => ({ name: 'still waiting after the abort' }))]);
  assert.equal(err && err.name, 'AbortError');
  await settle();
  for (const w of t.waits) if (!w.done) { w.done = true; w.fn(); }
  await settle();
  assert.equal(t.calls.filter(c => c.url === '/api/v1/customer/brands').length, 1, 'never sent again');
  // Aborted before the 429 arrived: no wait, no retry.
  let release;
  const u = load('#/brands', () => new Promise(res => { release = () => res(limited('1')); }));
  await settle();
  const ctl2 = new AbortController();
  const read2 = u.win.EW.api('/customer/brands', { signal: ctl2.signal }).then(() => null, e => e);
  await settle();
  ctl2.abort();
  release();
  await read2;
  await settle();
  assert.equal(u.calls.filter(c => c.url === '/api/v1/customer/brands').length, 1);
  assert.equal(retryWaits(u.waits).length, 0, 'no Retry-After wait after an abort');
});

/* -- PLAIN-ADS-FIX2 (plain-ads code r2) ---------------------------------- */
const brandCalls = calls => calls.filter(c => c.url === '/api/v1/customer/brands').length;
const runRetryWaits = waits => { for (const w of waits) if (!w.done && w.ms <= 3000) { w.done = true; w.fn(); } };

// PA-R2-03: the Overview reads the tracked list after its overview read
// settles, so at most 2 reads are in flight and none gets 429.
// READ-QUEUE (G2-02) replaces the FIX2 chaining: the tracked list goes
// out with the overview read (what the person sees first), and the client
// queue holds the recent-ads read until one of the two answers.
test('FIX2 PA-R2-03 the Overview reads the tracked list with its overview read; recent ads wait for a free place', async () => {
  let release = null;
  const routes = overviewRoutes(() => TRACKED);
  const t = await overview(url => (url === '/api/v1/customer/overview'
    ? new Promise(res => { release = () => res(reply(200, OVERVIEW)); }) : routes(url)));
  const done = t.render();
  await settle();
  assert.ok(release, 'fixture: the overview read is in flight');
  assert.equal(savedCalls(t.calls), 1, 'the tracked list goes out with the overview read');
  assert.ok(t.calls.findIndex(c => c.url.startsWith('/api/v1/customer/search')) > t.calls.findIndex(c => c.url.startsWith('/api/v1/customer/saved-items')),
    'recent ads after the tracked list');
  release();
  const html = await done;
  assert.equal(savedCalls(t.calls), 1, 'read once');
  assert.match(html, />Quince</);
  // A failed overview read still lets the tracked list load.
  const u = await overview(url => (url === '/api/v1/customer/overview' ? reply(500, { ok: false, error: { code: 'X', message: 'down' } }) : routes(url)));
  const h2 = await u.render();
  assert.match(h2, />Quince</, 'tracked brands after a failed overview read');
});

// PA-R2-02: a read rate-limited before an access change never delivers
// the answer read under the new access.
test('FIX2 PA-R2-02 a read waiting to retry when access changes surfaces as dropped and is not sent again', async () => {
  let n = 0;
  const t = load('#/brands', () => (n++ === 0 ? limited('1') : BRANDS), { fire: () => false });
  await settle();
  const read = t.win.EW.api('/customer/brands').then(value => ({ value }), e => e);
  await settle();
  assert.equal(retryWaits(t.waits).length, 1, 'fixture: waiting for Retry-After');
  t.win.EW.authorityBoundary();
  runRetryWaits(t.waits);
  const out = await read;
  assert.equal(out && out.droppedContext, true, 'dropped, not the after-change answer');
  assert.equal(brandCalls(t.calls), 1, 'no request after the access change');
  // The displayed workspace moves during the wait: the late answer is dropped.
  let m = 0;
  const u = load('#/brands', () => (m++ === 0 ? limited('1') : BRANDS), { fire: () => false });
  await settle();
  u.win.EW.current.workspaceContext = 'ctx-ws';
  const read2 = u.win.EW.api('/customer/brands').then(value => ({ value }), e => e);
  await settle();
  u.win.EW.current.workspaceContext = 'ctx-other';
  runRetryWaits(u.waits);
  const out2 = await read2;
  assert.equal(out2 && out2.droppedContext, true, 'the answer for the old workspace is dropped');
});

// PA-R2-04: a shared name read (brands, shows) stops retrying once no
// page waits for it; a caller that still waits keeps the retry.
test('FIX2 PA-R2-04 a shared name read stops retrying when its page is left', async () => {
  const t = load('#/search', () => limited('1'), { fire: () => false });
  await settle();
  const ctl = new AbortController();
  const read = t.win.EW.sharedRead('/customer/brands', { signal: ctl.signal }).then(() => null, e => e);
  await settle();
  assert.equal(retryWaits(t.waits).length, 1, 'fixture: waiting for Retry-After');
  ctl.abort();
  assert.equal((await read).name, 'AbortError');
  runRetryWaits(t.waits);
  await settle();
  assert.equal(brandCalls(t.calls), 1, 'never sent again after the page was left');
  // Still waited for: the retry goes on.
  let n = 0;
  const u = load('#/search', () => (n++ === 0 ? limited('1') : BRANDS), { fire: () => false });
  await settle();
  const live = new AbortController();
  const kept = u.win.EW.sharedRead('/customer/brands', { signal: live.signal });
  await settle();
  runRetryWaits(u.waits);
  const out = await kept;
  assert.equal(out.brands[0].key, 'quince');
  assert.equal(brandCalls(u.calls), 2);
});

// PLAIN-ADS-FIX3 (code PA-R3-02): a kept shared read whose callers all
// left during its first try is treated as absent: a later caller that
// brings its route's proof (authorizedBy) reads fresh and gets the brands,
// never the left read's 429 without a retry.
test('FIX3 PA-R3-02 a kept shared read whose callers all left is not reused', async () => {
  let n = 0, release;
  const first = new Promise(res => { release = res; });
  const t = load('#/search', async () => { n += 1; if (n === 1) { await first; return limited('1'); } return BRANDS; }, { fire: ms => ms <= 10000 });
  await settle();
  const ctl = new AbortController();
  const a = t.win.EW.sharedRead('/customer/brands', { signal: ctl.signal }).then(() => null, e => e);
  await settle();
  ctl.abort();
  const left = await a;
  assert.equal(left && left.name, 'AbortError', String(left && left.message));
  const b = t.win.EW.sharedRead('/customer/brands', { authorizedBy: { ok: true } }).then(v => v, e => e);
  await settle();
  release();
  const out = await b;
  assert.ok(out && out.brands, 'B got: ' + JSON.stringify(out && (out.status || out.message || out)));
  assert.equal(out.brands[0].key, 'quince');
});

// PA-R2-06: read retries, then the page's own retry, then nothing more.
test('FIX2 PA-R2-06 a persistent 429: read retries, one page-level retry, at most 6 requests, no third round', async () => {
  const t = await overview(overviewRoutes(alwaysLimited('1')));
  await t.render();
  await settle(200);
  assert.match(t.pages[0].html, /data-auto-retry/, 'the first page promises one retry');
  assert.match(t.pages[0].html, /Trying again automatically in 1 second/);
  assert.ok(t.pages.length >= 2, 'the page-level retry rendered again');
  const last = t.pages[t.pages.length - 1].html;
  assert.match(last, /Tracked brands didn’t load/);
  assert.doesNotMatch(last, /data-auto-retry/, 'no second automatic retry');
  assert.equal(savedCalls(t.calls), 6, '3 tries on the first render, 3 on the page-level retry');
});

// PRC-R1-01: the RQ-R3-01 skip is the download's alone. A person's press
// read (a clip's record read, options.press) that waits out a 429 is still
// dropped by an access change or a page leave, and is not sent again.
for (const leavePage of [false, true]) {
  test(`PRC-R1-01 a press read waiting to retry is dropped by ${leavePage ? 'a page leave' : 'an access change'} (only a download skips)`, async () => {
    let n = 0;
    const t = load('#/brands', () => (n++ === 0 ? limited('1') : BRANDS), { fire: () => false });
    await settle();
    const read = t.win.EW.api('/customer/brands', { press: true }).then(value => ({ value }), e => e);
    await settle();
    assert.equal(retryWaits(t.waits).length, 1, 'fixture: waiting for Retry-After');
    t.win.EW.authorityBoundary({ leavePage });
    runRetryWaits(t.waits);
    const out = await read;
    assert.equal(out && out.droppedContext, true, 'dropped, not the after-change answer');
    assert.equal(brandCalls(t.calls), 1, 'no request after the boundary');
  });
}
