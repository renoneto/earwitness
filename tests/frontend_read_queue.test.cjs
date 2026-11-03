/* READ-QUEUE (QA-full G2-02, DEP7b; lead decision 2026-10-24): the hosted
   server admits 2 requests at once per browser session (asgi.py
   RateLimiter). The client sends at most 2 at once and queues the rest in
   the order the page asks for them; the server limiter is unchanged.
   - Overview, Search and Brand never have more than 2 requests in flight,
     and every panel still loads.
   - Order: what the person sees first loads first (Overview: the overview
     read and the tracked brands, then recent ads, then brand names).
   - A 429 still goes through the existing read retry (unchanged bounds);
     the rate-limited read keeps its place while it waits.
   - Leaving the page empties the queue: nothing queued is sent later.
   - A person's action (a write, a clip press) jumps the queue and still
     counts toward the 2.
   - A read aborted in flight still runs on the server until it finishes:
     its place is kept until the server answers it (the request itself is
     not aborted; the caller gets its AbortError at once), so the next
     page's reads do not meet a full server and give up (the G2-02 symptom).
   The real app scripts run in a stubbed browser over a fake fetch that
   counts requests in flight; each reply comes after a set latency.
   Run: node --test tests/frontend_read_queue.test.cjs */
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
    const call = { url: url.replace('/api/v1', ''), method, done: false, signal: init.signal || null };
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
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, DOMException,
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
  win.document.querySelector = () => dummy;
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

/* -- Overview ------------------------------------------------------------- */
test('RQ-1 Overview: never more than 2 requests in flight, every panel loads, overview and tracked brands first', async () => {
  const t = await app('#/overview');
  const html = await t.render();
  await settle();
  assert.ok(t.net.max <= 2, `Overview had ${t.net.max} requests in flight at once`);
  assert.deepEqual(urls(t.net), ['/customer/overview', '/customer/saved-items', '/customer/search', '/customer/brands'],
    'what the person sees first loads first');
  assert.doesNotMatch(html, /didn’t load/, 'no failed panel');
  assert.match(html, /<h2>(Brands you track|Tracked brands)<\/h2>/);   // the tracked-brands card loaded (its heading changes on lr-app-judge-small)
  assert.match(html, />Quince</, 'tracked brand listed');
  assert.match(html, /92 ads/, 'with its count from the brand names read');
  assert.match(html, /Tracked campaigns · 0/);
  assert.match(html, /Alert activity/);
  assert.match(html, /ew-ov-recent[\s\S]*Quince/, 'recent ads listed');
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 }, 'nothing left behind');
});

/* -- Search --------------------------------------------------------------- */
test('RQ-2 Search: 6 reads on load, never more than 2 in flight, all answered once, results first', async () => {
  const t = await app('#/search?mode=ads&q=quince');
  const html = await t.render();
  await settle();
  assert.ok(t.net.max <= 2, `Search had ${t.net.max} requests in flight at once`);
  assert.deepEqual(urls(t.net), ['/customer/research-query', '/customer/brands', '/customer/shows',
    '/customer/filter-values?field=brands', '/customer/filter-values?field=shows', '/customer/filter-values?field=categories'],
    'results, then names, then the filter rail; each once');
  assert.doesNotMatch(html, /Search didn’t finish|didn’t load/);
  assert.match(html, /Showing 1 to 1 of 1 ads?/);
  assert.match(html, /ew-search-body[^"]*has-rail/, 'the filter rail loaded');
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 });
});

/* -- Brand ---------------------------------------------------------------- */
test('RQ-3 Brand: 5 reads on load, never more than 2 in flight, Recent ads and the comparison load', async () => {
  const t = await app('#/brand/quince');
  const html = await t.render();
  await settle();
  assert.ok(t.net.max <= 2, `Brand had ${t.net.max} requests in flight at once`);
  const sent = urls(t.net);
  assert.deepEqual(sent.slice(0, 2), ['/customer/saved-items', '/customer/brands/quince']);
  assert.deepEqual(sent.slice(2).sort(), ['/customer/search', '/customer/search', '/customer/summary']);
  assert.match(html, /Recent ads/);
  assert.doesNotMatch(html, /didn’t load|unavailable/i);
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 });
});

/* -- 429 ------------------------------------------------------------------ */
test('RQ-4 a 429 still uses the existing read retry, and the waiting read keeps its place', async () => {
  // On the Overview: one 429 on the overview read, then the page loads.
  let n = 0;
  const t = await app('#/overview', { routes: (url, init) => (url === '/api/v1/customer/overview' && n++ === 0 ? limited('1') : answer(url, init)) });
  const html = await t.render();
  await settle();
  assert.ok(t.net.max <= 2, `${t.net.max} in flight`);
  assert.equal(urls(t.net).filter(u => u === '/customer/overview').length, 2, 'retried once');
  assert.deepEqual(t.waits.filter(w => w.ms <= 3000).map(w => w.ms), [1000], 'waited the server Retry-After');
  assert.doesNotMatch(html, /didn’t load|data-auto-retry/);
  assert.match(html, />Quince</);
  // Directly: while A waits out its Retry-After, its place is not given to C.
  let a = 0;
  const releases = [];
  const u = network('#/brands', { routes: url => {
    if (url.endsWith('/a')) return a++ === 0 ? limited('1') : { ok: true };
    if (url.endsWith('/b')) return new Promise(res => releases.push(() => res({ ok: true })));
    return { ok: true };
  } });
  u.win.EW.requestTimeout.set = (fn, ms) => { const w = { fn, ms, done: false }; u.waits.push(w); return w; };
  await settle();
  const ra = u.win.EW.api('/customer/x/a'), rb = u.win.EW.api('/customer/x/b'), rc = u.win.EW.api('/customer/x/c');
  await sleep(40);
  assert.deepEqual(u.net.sent.map(c => c.url), ['/customer/x/a', '/customer/x/b'], 'A got 429, B in flight, C queued');
  assert.equal(u.waits.filter(w => w.ms === 1000).length, 1, 'A waits for Retry-After');
  assert.deepEqual(u.net.sent.map(c => c.url).filter(x => x.endsWith('/c')), [], 'C is not sent while A waits');
  for (const w of u.waits) if (w.ms === 1000 && !w.done) { w.done = true; w.fn(); }
  await ra;
  await sleep(30);
  assert.deepEqual(u.net.sent.map(c => c.url), ['/customer/x/a', '/customer/x/b', '/customer/x/a', '/customer/x/c'], 'A retried in its place, then C');
  releases.forEach(r => r());
  await Promise.all([rb, rc]);
  assert.ok(u.net.max <= 2);
});

/* -- leaving the page --------------------------------------------------- */
// sampleMs: a moment while the page's first two reads are still in flight
// (each answer takes 60 ms; Search and Brand ask their next reads only
// after the first answer).
for (const [name, route, firstTwo, sampleMs] of [
  ['Overview', '#/overview', ['/customer/overview', '/customer/saved-items'], 30],
  ['Search', '#/search?mode=ads&q=quince', ['/customer/research-query', '/customer/brands'], 80],
  ['Brand', '#/brand/quince', ['/customer/saved-items', '/customer/brands/quince'], 80],
]) {
  test(`RQ-5 leaving ${name} empties the queue: nothing it queued is sent later`, async () => {
    const t = await app(route, { latency: () => 60 });
    const done = t.render();
    await sleep(sampleMs);
    const before = urls(t.net);
    assert.ok(queue(t.win).queued > 0 || name === 'Brand', `${name}: reads are queued (${JSON.stringify(queue(t.win))})`);
    assert.deepEqual(before.slice(0, 2), firstTwo);
    await t.go('#/no-such-page');
    await done;
    assert.equal(queue(t.win).queued, 0, `${name}: queue emptied on leaving`);
    const sentAfter = t.net.sent.length;
    await sleep(400);
    assert.equal(t.net.sent.length, sentAfter, `${name}: a queued read was sent after the page was left: ${urls(t.net).slice(sentAfter)}`);
    assert.ok(t.net.max <= 2);
  });
}

/* -- a person's action jumps the queue -------------------------------- */
test('RQ-6 a write and a clip press jump the queue, still within 2 in flight', async () => {
  const held = [];
  const t = network('#/brands', { routes: url => (url.includes('/slow/') ? new Promise(res => held.push(() => res({ ok: true }))) : { ok: true, item: { id: 1 } }) });
  await settle();
  const E = t.win.EW;
  E.current.displayedWorkspaceSwitchRevision = 7;
  const reads = ['/customer/slow/1', '/customer/slow/2', '/customer/q/3', '/customer/q/4', '/customer/q/5'].map(p => E.api(p));
  await sleep(10);
  assert.deepEqual(t.net.sent.map(c => c.url), ['/customer/slow/1', '/customer/slow/2']);
  assert.equal(queue(t.win).queued, 3);
  const write = E.api('/customer/saved-items', { method: 'POST', body: {} });
  const press = E.api('/customer/records/sr1%3Aabc', { press: true });
  await sleep(10);
  assert.equal(t.net.sent.length, 2, 'the server is full: nothing more goes out');
  held.shift()();
  await sleep(40);
  const order = t.net.sent.map(c => c.url);
  assert.deepEqual(order.slice(2, 4), ['/customer/saved-items', '/customer/records/sr1%3Aabc'], 'the write, then the press, ahead of the queued reads: ' + order);
  held.shift()();
  await Promise.all([write, press, ...reads]);
  assert.deepEqual(t.net.sent.map(c => c.url).slice(4), ['/customer/q/3', '/customer/q/4', '/customer/q/5'], 'queued reads in the order asked');
  assert.ok(t.net.max <= 2, `${t.net.max} in flight`);
  // The Search clip press reads its record as a press.
  const views = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(views, /const read = \(\) => E\.api\('\/customer\/records\/' \+ encodeURIComponent\(ref\), \{[^}]*press: true/);
});

/* -- an aborted read still runs on the server ------------------------- */
test('RQ-7 a read aborted in flight keeps its place until the server has answered it', async () => {
  const t = network('#/brands', { latency: url => (url.includes('/warm') ? 120 : url.includes('/slow/') ? 1000 : 5), routes: () => ({ ok: true }) });
  await settle();
  const E = t.win.EW;
  await E.api('/customer/warm');                      // this session's reads take about 120 ms
  const ctl = new AbortController();
  const gone = ['/customer/slow/1', '/customer/slow/2'].map(p => E.api(p, { signal: ctl.signal }).catch(e => e));
  await sleep(10);
  ctl.abort();
  await Promise.all(gone);
  const next = E.api('/customer/next');
  await sleep(30);
  assert.equal(t.net.sent.filter(c => c.url === '/customer/next').length, 0, 'sent while the aborted reads still ran on the server');
  assert.equal(queue(t.win).queued, 1);
  await next;
  assert.equal(t.net.sent.filter(c => c.url === '/customer/next').length, 1, 'sent once they are done');
  // A queued read that is aborted never goes out and holds nothing.
  const held = [];
  const u = network('#/brands', { routes: () => new Promise(res => held.push(() => res({ ok: true }))) });
  await settle();
  const busy = [u.win.EW.api('/customer/a'), u.win.EW.api('/customer/b')];
  const ctl2 = new AbortController();
  const queued = u.win.EW.api('/customer/c', { signal: ctl2.signal }).then(() => null, e => e);
  await sleep(5);
  ctl2.abort();
  assert.equal((await queued).name, 'AbortError');
  assert.equal(queue(u.win).queued, 0);
  await sleep(30);                                   // both reached the server
  held.splice(0).forEach(r => r());
  await Promise.all(busy);
  assert.deepEqual(queue(u.win), { limit: 2, busy: 0, queued: 0 });
  assert.equal(u.net.sent.filter(c => c.url === '/customer/c').length, 0);
});

/* -- not counted by the server: session operations -------------------- */
test('RQ-8 the session read is not queued behind customer reads', async () => {
  const held = [];
  const t = network('#/brands', { routes: () => new Promise(res => held.push(() => res({ ok: true }))) });
  await settle();
  const E = t.win.EW;
  // (The session read may adopt another context: these reads are then dropped.)
  const busy = [E.api('/customer/a'), E.api('/customer/b'), E.api('/customer/c')].map(r => r.catch(() => null));
  const s = await Promise.race([E.api('/session').then(() => 'answered', () => 'answered'), sleep(100).then(() => 'queued')]);
  assert.equal(s, 'answered');
  // Answer every customer read as it reaches the server (the third after a place frees).
  for (let i = 0; i < 10; i++) { await sleep(20); held.splice(0).forEach(r => r()); }
  await Promise.all(busy);
});

/* -- leaving a page for another (G2-02 as recorded) -------------------- */
test('RQ-9 Search to Overview: the left page\'s sent reads finish and keep their places; Overview never meets more than 2 at the server', async () => {
  const t = await app('#/search?mode=ads&q=quince', { latency: () => 60 });
  const done = t.render();
  await sleep(90);                                    // the results answered; the names reads are out
  const left = t.net.sent.filter(c => !c.done);
  assert.ok(left.length > 0, 'fixture: Search reads in flight when the page is left');
  const html = await t.go('#/overview');
  await done;
  await settle();
  assert.ok(t.net.max <= 2, `${t.net.max} requests at the server at once`);
  assert.deepEqual(left.filter(c => c.signal && c.signal.aborted).map(c => c.url), [], 'a sent read was aborted on leaving');
  assert.doesNotMatch(html, /didn’t load/);
  assert.match(html, /<h2>(Brands you track|Tracked brands)<\/h2>/);   // the tracked-brands card loaded (its heading changes on lr-app-judge-small)
  assert.match(html, />Quince</);
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 });
});

/* -- READ-QUEUE-FIX1 (code review r1) ---------------------------------- */
// RQ-R1-04: a shared name list whose every caller left (no boundary: a
// same-page re-render) leaves the queue and is never sent.
test('RQ-10 a shared name read whose callers all left leaves the queue unsent', async () => {
  const held = [];
  const t = network('#/brands', { routes: url => (url.endsWith('/customer/brands') ? BRANDS
    : new Promise(res => held.push(() => res({ ok: true })))) });
  await settle();
  const E = t.win.EW;
  const busy = [E.api('/customer/a'), E.api('/customer/b')];
  await sleep(5);
  const ctl = new AbortController();
  const names = E.sharedRead('/customer/brands', { signal: ctl.signal }).then(() => null, e => e);
  await settle();
  assert.equal(queue(t.win).queued, 1, 'fixture: the names read waits for a place');
  ctl.abort();                                       // the caller left; the epoch did not move
  assert.equal((await names).name, 'AbortError');
  await settle();
  assert.equal(queue(t.win).queued, 0, 'the names read left the queue');
  held.splice(0).forEach(r => r());
  await Promise.all(busy);
  await sleep(30);
  assert.deepEqual(t.net.sent.filter(c => c.url === '/customer/brands'), [], 'a names read no page waits for was sent');
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 });
});

// RQ-R1-03: a write that waited for a place checks again, when its place
// comes, what it was checked for when asked: the person, the write fence
// and a waiting workspace switch. Nothing is sent when one moved.
for (const [name, move, flag] of [
  ['another person signed in', (E, who) => { who.now = 'person-b'; }, 'staleOperation'],
  ['the write fence went up', E => { E.current.writeFence = { from: 'ctx-ws' }; }, 'writeFenced'],
  ['a workspace switch is waiting', E => { E.current.switchWaiting = true; }, 'switchWaiting'],
]) {
  test(`RQ-11 a queued write sends nothing when, at its turn, ${name}`, async () => {
    const held = [];
    const t = network('#/brands', { routes: url => (url.includes('/slow/') ? new Promise(res => held.push(() => res({ ok: true }))) : { ok: true, item: { id: 1 } }) });
    await settle();
    const E = t.win.EW;
    const who = { now: 'person-a' };
    t.win.EWViews = { operationStamp: () => who.now, identityStamp: () => who.now, identityHolds: s => s === who.now };
    E.current.displayedWorkspaceSwitchRevision = 7;
    // (Reads answered after another person signed in are dropped.)
    const reads = [E.api('/customer/slow/1'), E.api('/customer/slow/2')].map(r => r.catch(() => null));
    await sleep(5);
    const write = E.api('/customer/saved-items', { method: 'POST', body: {} }).then(() => null, e => e);
    await settle();
    assert.equal(queue(t.win).queued, 1, 'fixture: the write waits for a place');
    move(E, who);
    held.shift()();
    const err = await write;
    assert.ok(err && err[flag], `${name}: the write was not refused as ${flag}: ${err && err.message}`);
    assert.equal(err.outcomeUnknown, undefined, `${name}: told as unconfirmed, though nothing was sent`);
    assert.deepEqual(t.net.sent.filter(c => c.method === 'POST'), [], `${name}: the write was sent`);
    held.splice(0).forEach(r => r());
    await Promise.all(reads);
    assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 }, `${name}: its place was given back`);
    assert.equal(E.current.writesInFlight || 0, 0, `${name}: still counted as a write in flight`);
  });
}

// RQ-R1-01 (export download): the file after an export is read through
// the queue as a person's action: ahead of queued page reads, holding its
// place until the answer, so the server never meets a third request.
test('RQ-12 an export download goes ahead of queued reads and holds a place until its answer', async () => {
  const held = [];
  const CSV = 'ref,brand\nsr1:a,Quince\n';
  const file = { ok: true, status: 200, text: async () => CSV,
    headers: { get: k => ({ 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="earwitness-export-9.csv"' })[k] || null } };
  let fileAnswer = null, postAnswer = null;
  const t = await app('#/search?mode=ads&q=quince', { routes: (url, init) => {
    if (url.includes('/slow/')) return new Promise(res => held.push(() => res({ ok: true })));
    if (url === '/api/v1/customer/research-export') return new Promise(res => { postAnswer = () => res({ ok: true, export_id: 9, total: 1, ad_count: 1 }); });
    if (url === '/api/v1/customer/exports/9') return new Promise(res => { fileAnswer = () => res(file); });
    return answer(url, init);
  } });
  const E = t.win.EW, win = t.win;
  const saved = [];
  win.Blob = class { constructor(parts, opts) { this.parts = parts; this.type = opts && opts.type; } };
  win.URL = { createObjectURL: b => { saved.push({ blob: b }); return 'blob:x/' + saved.length; }, revokeObjectURL() {} };
  const body = { appendChild: k => k };
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#export-format' ? { value: 'csv' } : base(sel));
  win.document.body = body;
  win.document.createElement = tag => ({ tagName: tag, dataset: {}, style: {}, setAttribute() {}, remove() {},
    click() { saved[saved.length - 1].a = { href: this.href, download: this.download }; } });
  E.closeDialog = () => {};
  E.current.displayedRevision = 'rev-1';
  E.current.displayedWorkspaceSwitchRevision = 7;
  win.location.href = '';
  t.net.sent.length = 0;
  const sentUrls = () => t.net.sent.map(c => c.url);
  const first = E.api('/customer/slow/1');
  await sleep(5);
  const exporting = win.EWViews.handleAction({ dataset: { action: 'export-all' }, closest: () => null });
  await settle();                                    // the export POST takes the second place
  const pageReads = [E.api('/customer/slow/a'), E.api('/customer/slow/b')];
  await sleep(5);
  assert.deepEqual(queue(t.win), { limit: 2, busy: 2, queued: 2 }, 'fixture: two page reads wait');
  postAnswer();
  await exporting; await sleep(30);                  // its place went to /slow/a; the download waits
  assert.deepEqual(sentUrls(), ['/customer/slow/1', '/customer/research-export', '/customer/slow/a']);
  assert.equal(win.location.href, '', 'the file was opened around the queue (location.href)');
  held.shift()();                                    // /slow/1 answers: one place frees
  await sleep(30);
  assert.deepEqual(sentUrls().slice(3), ['/customer/exports/9'], 'the download goes ahead of the queued read: ' + sentUrls());
  assert.deepEqual(queue(t.win), { limit: 2, busy: 2, queued: 1 }, 'the download holds its place until it is answered');
  fileAnswer(); await sleep(30);
  assert.deepEqual(sentUrls().slice(4), ['/customer/slow/b'], 'the queued read went out once the file answered');
  assert.equal(saved.length, 1, 'the file was saved once');
  assert.deepEqual([...saved[0].blob.parts], [CSV]);
  assert.equal(saved[0].blob.type, 'text/csv; charset=utf-8');
  assert.deepEqual(saved[0].a, { href: 'blob:x/1', download: 'earwitness-export-9.csv' });
  for (let i = 0; i < 5; i++) { held.splice(0).forEach(r => r()); await sleep(10); }
  await Promise.all([first, ...pageReads]);
  assert.ok(t.net.max <= 2, `${t.net.max} in flight`);
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 });
});

/* -- READ-QUEUE-FIX2 ------------------------------------------------------
   Every stored file the page saves (an export's file, a Saved › Reports
   download, the frozen report) is read through the queue as a person's
   action and saved under the server's name. `files` answers by path, each
   held until the test answers it. */
async function fileApp(route, files, extra = () => undefined) {
  const held = [], answers = {};
  const t = await app(route, { routes: (url, init) => {
    if (url.includes('/slow/')) return new Promise(res => held.push(() => res({ ok: true })));
    if (url === '/api/v1/customer/research-export') return { ok: true, export_id: 9, total: 1, ad_count: 1 };
    const p = url.replace('/api/v1', '');
    if (Object.hasOwn(files, p)) return new Promise(res => { answers[p] = () => res(files[p]); });
    const more = extra(p);
    return more === undefined ? answer(url, init) : more;
  } });
  const E = t.win.EW, win = t.win;
  const saved = [], toasts = [];
  win.Blob = class { constructor(parts, opts) { this.parts = parts; this.type = opts && opts.type; } };
  win.URL = { createObjectURL: b => { saved.push({ blob: b }); return 'blob:x/' + saved.length; }, revokeObjectURL() {} };
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#export-format' ? { value: 'csv' } : base(sel));
  win.document.body = { appendChild: k => k };
  win.document.createElement = tag => ({ tagName: tag, dataset: {}, style: {}, setAttribute() {}, remove() {},
    click() { saved[saved.length - 1].a = { href: this.href, download: this.download }; } });
  E.toast = msg => toasts.push(msg);
  E.closeDialog = () => {};
  E.current.displayedRevision = 'rev-1';
  E.current.displayedWorkspaceSwitchRevision = 7;
  win.location.href = '';
  t.net.sent.length = 0;
  return { ...t, E, held, answers, saved, toasts, sent: () => t.net.sent.map(c => c.url) };
}
const fileReply = (name, type, body) => ({ ok: true, status: 200, text: async () => body,
  headers: { get: k => ({ 'Content-Type': type, 'Content-Disposition': `attachment; filename="${name}"` })[k] || null } });
const CSV = 'ref,brand\nsr1:a,Quince\n';
const CSV_FILE = fileReply('earwitness-export-9.csv', 'text/csv; charset=utf-8', CSV);
const exportAll = f => f.win.EWViews.handleAction({ dataset: { action: 'export-all' }, closest: () => null });
const didNotDownload = f => f.toasts.filter(m => /download/i.test(m));

// RQ-R2-03 (A12): a download is not a page's read: leaving the page keeps it.
test('RQ-13 an export\'s file read when the page was left still saves under the server\'s name', async () => {
  const f = await fileApp('#/search?mode=ads&q=quince', { '/customer/exports/9': CSV_FILE });
  await exportAll(f); await sleep(30);
  assert.ok(f.answers['/customer/exports/9'], 'fixture: the file read is out: ' + f.sent());
  await f.go('#/overview'); await sleep(10);
  f.answers['/customer/exports/9'](); await sleep(30);
  assert.equal(f.saved.length, 1, 'leaving the page dropped the download');
  assert.deepEqual(f.saved[0].a, { href: 'blob:x/1', download: 'earwitness-export-9.csv' });
  assert.deepEqual(didNotDownload(f), []);
  await settle();
  assert.deepEqual(queue(f.win), { limit: 2, busy: 0, queued: 0 });
});

// RQ-R2-03 (A15): a file answered under another workspace or person is dropped unsaved, silently.
for (const [what, move] of [
  ['the workspace changed', E => { E.current.workspaceContext = 'ctx-other'; }],
  ['another person signed in', E => { E.current.principal = 'person-b'; }],
]) {
  test(`RQ-14 an export's file answered after ${what} is not saved, and nothing is said`, async () => {
    const f = await fileApp('#/search?mode=ads&q=quince', { '/customer/exports/9': CSV_FILE });
    f.E.current.principal = 'person-a';
    await exportAll(f); await sleep(30);
    assert.ok(f.answers['/customer/exports/9'], 'fixture: the file read is out');
    move(f.E);
    f.answers['/customer/exports/9'](); await sleep(30);
    assert.equal(f.saved.length, 0, `a file answered after ${what} was saved`);
    assert.deepEqual(didNotDownload(f), [], 'a dropped file was reported as failed');
    assert.deepEqual(queue(f.win), { limit: 2, busy: 0, queued: 0 });
  });
}

// RQ-R2-03 (A16) and RQ-R2-07: a failed download says so. A 409 (the
// stored view changed) says what the export's own 409 says, with no
// pointer to Saved › Reports (it refuses there too).
for (const [status, code, said] of [
  [500, 'INTERNAL', 'The file did not download. Download it from Saved › Reports.'],
  [409, 'VIEW_CHANGED', 'The collection changed; refresh the results, then export again.'],
]) {
  test(`RQ-15 an export's file that fails with ${status} says: ${said}`, async () => {
    const f = await fileApp('#/search?mode=ads&q=quince',
      { '/customer/exports/9': reply(status, { ok: false, error: { code, message: 'no', status, details: {} } }) });
    await exportAll(f); await sleep(30);
    f.answers['/customer/exports/9'](); await sleep(30);
    assert.equal(f.saved.length, 0);
    assert.deepEqual(didNotDownload(f).concat(f.toasts.filter(m => m === said)).slice(-1), [said], 'toasts: ' + JSON.stringify(f.toasts));
  });
}

// RQ-R2-04: Saved › Reports downloads and the frozen report download go
// through the queue as the export's file does: a person's action ahead of
// queued page reads, its place held until the answer, saved under the
// server's name; without Blob, the plain link (location.href).
const REPORT_HTML = '<!doctype html><title>Report 1</title>';
const REPORT_FILE = fileReply('earwitness-report-1.html', 'text/html; charset=utf-8', REPORT_HTML);
const ROW = { kind: 'export', id: 9, title: 'Research CSV', format: 'csv', count: 1, captured_at: '2026-10-07T08:41:00+00:00',
  download_path: '/customer/exports/9' };
const REPORT_ROW = { kind: 'report', id: 2, title: 'Q4', campaign_id: 3, report_no: 1, format: 'html', captured_at: '2026-10-07T08:40:49+00:00',
  download_path: '/customer/campaigns/3/reports/1/html' };
const linkIn = html => {
  const a = /<a [^>]*data-action="download-file"[^>]*>/.exec(html);
  assert.ok(a, 'no queued download link in ' + html);
  const attr = n => (new RegExp(`${n}="([^"]*)"`).exec(a[0]) || [])[1];
  return { href: attr('href'), path: (attr('data-path') || '').replace(/&amp;/g, '&') };
};
for (const [what, route, link, file, name, body] of [
  ['Saved › Reports export row', '#/saved?tab=reports', f => linkIn(f.win.EWSaved.archiveRow(f.E, ROW)), '/customer/exports/9', 'earwitness-export-9.csv', CSV],
  ['Saved › Reports report row', '#/saved?tab=reports', f => linkIn(f.win.EWSaved.archiveRow(f.E, REPORT_ROW)), '/customer/campaigns/3/reports/1/html?download=1', 'earwitness-report-1.html', REPORT_HTML],
  ['frozen report page', '#/campaign/3/report/1', async f => linkIn(await f.render()), '/customer/campaigns/3/reports/1/html?download=1', 'earwitness-report-1.html', REPORT_HTML],
]) {
  const files = { [file]: file.includes('/reports/') ? REPORT_FILE : CSV_FILE };
  const extra = p => (p === '/customer/campaigns/3/reports/1' ? { frozen: { captured_at: '2026-10-07T08:40:49+00:00', placements: [] } } : undefined);
  test(`RQ-16 ${what}: Download goes ahead of queued reads, holds its place, saves under the server's name`, async () => {
    const f = await fileApp(route, files, extra);
    const a = await link(f);
    assert.equal(a.href, '/api/v1' + file, 'the plain link stays (modifier clicks, no script)');
    assert.equal(a.path, file);
    f.net.sent.length = 0;
    const reads = [f.E.api('/customer/slow/1'), f.E.api('/customer/slow/2'), f.E.api('/customer/slow/3')];
    await sleep(30);                                 // the slow reads reached the server
    assert.deepEqual(queue(f.win), { limit: 2, busy: 2, queued: 1 }, 'fixture');
    const pressed = f.win.EWViews.handleAction({ dataset: { action: 'download-file', path: a.path }, closest: () => null });
    await sleep(5);
    assert.equal(f.win.location.href, '', 'the file was opened around the queue (location.href)');
    f.held.shift()(); await sleep(30);
    assert.deepEqual(f.sent().slice(2), [file], 'the download did not go ahead of the queued read: ' + f.sent());
    assert.deepEqual(queue(f.win), { limit: 2, busy: 2, queued: 1 }, 'the download holds its place until it is answered');
    f.answers[file](); await pressed; await sleep(30);
    assert.deepEqual(f.sent().slice(3), ['/customer/slow/3'], 'the queued read went out once the file answered');
    assert.equal(f.saved.length, 1, 'the file was saved once');
    assert.deepEqual([...f.saved[0].blob.parts], [body]);
    assert.deepEqual(f.saved[0].a, { href: 'blob:x/1', download: name });
    for (let i = 0; i < 4; i++) { f.held.splice(0).forEach(r => r()); await sleep(10); }
    await Promise.all(reads);
    assert.ok(f.net.max <= 2, `${f.net.max} in flight`);
    assert.deepEqual(queue(f.win), { limit: 2, busy: 0, queued: 0 });
  });
}

test('RQ-16 a Download click is the page\'s (no second native download); a modifier click stays the browser\'s; no Blob falls back to the link', async () => {
  const f = await fileApp('#/saved?tab=reports', { '/customer/exports/9': CSV_FILE });
  const a = linkIn(f.win.EWSaved.archiveRow(f.E, ROW));
  const el = { dataset: { action: 'download-file', path: a.path }, disabled: false, hasAttribute: () => false };
  const click = mods => {
    const ev = { button: 0, prevented: false, ...mods, preventDefault() { ev.prevented = true; },
      target: { closest: sel => (/data-action/.test(sel) ? el : null) } };
    for (const fn of f.docListeners.click || []) fn(ev);
    return ev;
  };
  assert.ok((f.docListeners.click || []).length, 'fixture: the page listens for clicks');
  const plain = click({});
  assert.equal(plain.prevented, true, 'a plain click also ran the browser\'s own download');
  await sleep(30);
  f.answers['/customer/exports/9'](); await sleep(30);
  assert.equal(f.saved.length, 1, 'a plain click saved nothing');
  f.net.sent.length = 0;
  const cmd = click({ metaKey: true });
  await sleep(10);
  assert.equal(cmd.prevented, false, 'a modifier click was taken from the browser');
  assert.deepEqual(f.sent(), [], 'a modifier click also read the file');
  delete f.win.Blob;
  click({}); await sleep(10);
  assert.equal(f.win.location.href, '/api/v1/customer/exports/9', 'no Blob: the plain link');
  assert.deepEqual(f.sent(), []);
});

/* -- READ-QUEUE-MRPREP ----------------------------------------------------
   RQ-R3-01: leaving the page does not stop a download, also while it
   waits out a 429. RQ-R3-02: every modifier click and a middle-click on a
   queued download link stay the browser's. RQ-SCR-R3-03: a download has
   the export's longer read limit, not 20 s. */
for (const [what, row, file, name] of [
  ['export row', ROW, '/customer/exports/9', 'earwitness-export-9.csv'],
  ['report row', REPORT_ROW, '/customer/campaigns/3/reports/1/html?download=1', 'earwitness-report-1.html'],
]) {
  test(`RQ-18 a Saved › Reports ${what} Download that got 429 still saves when the page is left during the wait`, async () => {
    let tries = 0;
    const f = await fileApp('#/saved?tab=reports', {},
      p => (p === file ? (tries++ === 0 ? limited('1') : (file.includes('/reports/') ? REPORT_FILE : CSV_FILE)) : undefined));
    const waits = [];
    const set = f.E.requestTimeout.set;
    f.E.requestTimeout.set = (fn, ms) => (ms === 1000 ? (waits.push(fn), {}) : set(fn, ms));
    const a = linkIn(f.win.EWSaved.archiveRow(f.E, row));
    const pressed = f.win.EWViews.handleAction({ dataset: { action: 'download-file', path: a.path }, closest: () => null });
    await sleep(30);
    assert.equal(waits.length, 1, 'fixture: the download waits out its 429: ' + f.sent());
    await f.go('#/overview'); await sleep(10);
    waits.shift()(); await pressed; await sleep(30);
    assert.equal(tries, 2, 'the retry was not sent after the page was left');
    assert.equal(f.saved.length, 1, 'nothing was saved: ' + JSON.stringify(f.toasts));
    assert.deepEqual(f.saved[0].a, { href: 'blob:x/1', download: name });
    await settle();
    assert.deepEqual(queue(f.win), { limit: 2, busy: 0, queued: 0 });
  });

  test(`RQ-19 a modifier click or a middle-click on a Saved › Reports ${what} Download stays the browser's`, async () => {
    const f = await fileApp('#/saved?tab=reports', {});
    const a = linkIn(f.win.EWSaved.archiveRow(f.E, row));
    const el = { dataset: { action: 'download-file', path: a.path }, disabled: false, hasAttribute: () => false };
    for (const mods of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
      const ev = { button: 0, prevented: false, ...mods, preventDefault() { ev.prevented = true; },
        target: { closest: sel => (/data-action/.test(sel) ? el : null) } };
      for (const fn of f.docListeners.click || []) fn(ev);
      await sleep(10);
      assert.equal(ev.prevented, false, `${JSON.stringify(mods)} was taken from the browser`);
      assert.deepEqual(f.sent(), [], `${JSON.stringify(mods)} also read the file`);
    }
    assert.equal(f.saved.length, 0);
  });

  test(`RQ-20 a Saved › Reports ${what} Download slower than 20 s still saves (the export's limit)`, async () => {
    const f = await fileApp('#/saved?tab=reports', { [file]: file.includes('/reports/') ? REPORT_FILE : CSV_FILE });
    const a = linkIn(f.win.EWSaved.archiveRow(f.E, row));
    f.waits.length = 0;
    const pressed = f.win.EWViews.handleAction({ dataset: { action: 'download-file', path: a.path }, closest: () => null });
    await sleep(30);
    assert.ok(f.answers[file], 'fixture: the file read is out');
    const limits = f.waits.map(w => w.ms);
    for (const w of f.waits) if (w.ms <= f.E.requestTimeout.readMs && !w.done) { w.done = true; w.fn(); }   // 20 s pass
    await sleep(10);
    f.answers[file](); await pressed; await sleep(30);
    assert.equal(f.saved.length, 1, 'a download slower than 20 s gave up: ' + JSON.stringify(f.toasts));
    assert.deepEqual(f.saved[0].a, { href: 'blob:x/1', download: name });
    assert.deepEqual(f.toasts, []);
    assert.deepEqual(limits, [f.E.requestTimeout.exportMs], 'the download still has a limit: the export\'s');
  });
}

// RQ-R1-07: a read whose place is granted in the tick its caller aborts is not sent.
test('RQ-17 a queued read aborted in the tick its place is granted is not sent; the place goes on', async () => {
  const held = [];
  const t = network('#/brands', { routes: url => (url.includes('/slow/') ? new Promise(res => held.push(() => res({ ok: true }))) : { ok: true }) });
  await settle();
  const E = t.win.EW;
  const reads = [E.api('/customer/slow/1'), E.api('/customer/slow/2')];
  await sleep(5);
  // The grant takes the entry's abort listener off; the caller aborts right then.
  const ctl = new AbortController();
  const off = ctl.signal.removeEventListener.bind(ctl.signal);
  let armed = true;
  ctl.signal.removeEventListener = (type, fn) => { off(type, fn); if (armed && type === 'abort') { armed = false; ctl.abort(); } };
  const late = E.api('/customer/q/3', { retrySignal: ctl.signal }).then(() => 'answered', e => e.name);
  const next = E.api('/customer/q/4');
  await sleep(5);
  assert.deepEqual(queue(t.win), { limit: 2, busy: 2, queued: 2 }, 'fixture');
  await sleep(30);                                   // the slow reads reached the server
  held.shift()();
  assert.equal(await late, 'AbortError');
  await next; await sleep(10);
  assert.deepEqual(t.net.sent.map(c => c.url).filter(u => u.startsWith('/customer/q/')), ['/customer/q/4'], 'the aborted read was sent');
  held.splice(0).forEach(r => r());
  await Promise.all(reads);
  assert.deepEqual(queue(t.win), { limit: 2, busy: 0, queued: 0 });
});
