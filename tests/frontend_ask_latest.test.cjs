/* Ask first answer (LEDGER-LEAD3-FA-1 point 2, plan 2026-10-28 section (b)):
   #/ask opened with no turns in this tab reads the member's latest thread
   (GET /customer/ask/threads, then .../threads/<id>), drops hidden turns,
   and draws the thread when its newest remaining turn is an answered or
   partial turn with a source. Anything else, a failed read, or a read
   that lands after the member left, asked, or changed workspace keeps
   today's empty page. Loaders and rig copied from
   frontend_ask_screens.test.cjs.
   Run: node --test tests/frontend_ask_latest.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'ask-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
const clone = o => JSON.parse(JSON.stringify(o));

function loadViews(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
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
  const win = sandbox.window;
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}

const flush = () => new Promise(res => setImmediate(res));
const defer = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

const TOKEN = 8;
// A stub document, a fake api by path, fake timers, and the #/ask page drawn once.
async function rig(route = '#/ask') {
  const win = loadViews('#/overview');
  await new Promise(res => setTimeout(res, 0));
  const E = win.EW;
  E.current.askAvailable = true;
  E.current.route = route;
  E.current.fetchToken = TOKEN;
  const timers = [];
  win.setTimeout = (fn, ms) => { const t = { fn, ms, cleared: false, fired: false }; timers.push(t); return t; };
  win.clearTimeout = t => { if (t) t.cleared = true; };
  const mk = extra => Object.assign({ value: '', hidden: false, disabled: false, textContent: '', innerHTML: '', outerHTML: '', dataset: {},
    focus() {}, scrollIntoView() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, insertAdjacentHTML() {} }, extra);
  const thread = mk({ sets: 0, html: '', inserted: [],
    insertAdjacentHTML(pos, h) { this.inserted.push([pos, h]); this.html += h; } });
  Object.defineProperty(thread, 'innerHTML', { get() { return this.html; }, set(v) { this.sets += 1; this.html = v; } });
  const mkForm = kind => {
    const box = mk({ id: kind === 'follow' ? 'ask-fu' : 'ask-q' }), slot = mk({ hidden: true }), submit = mk();
    return mk({ box, slot, submit, dataset: { ask: kind },
      querySelector: s => (s === '[name=question]' ? box : s === '.ask-refusal' ? slot : s === '[type=submit]' ? submit : null) });
  };
  const top = mkForm('new'), follow = mkForm('follow');
  follow.hidden = true;
  const status = mk(), count = mk({ hidden: true }), examples = mk();
  const els = { 'ask-thread': thread, 'ask-status': status, 'ask-form': top, 'ask-q': top.box, 'ask-followup': follow, 'ask-fu': follow.box };
  win.document.getElementById = id => (id.startsWith('ask-turn-') ? mk() : els[id] || null);
  win.document.querySelector = sel => (sel === '.ask-count' ? count : sel === '.ask-examples' ? examples : null);
  win.document.querySelectorAll = () => [];
  const calls = [], toasts = [];
  E.toast = (...a) => { toasts.push(a); };
  E.syncHistory = () => {};
  const r = { win, E, V: win.EWViews, timers, thread, top, follow, status, examples, calls, toasts, routes: {} };
  // routes: path -> value | Error | deferred; POST /customer/ask issues t9 in a new thread h9.
  E.api = (p, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push({ p, method, body: opts && opts.body ? clone(opts.body) : undefined, signal: opts && opts.signal });
    const got = r.routes[method + ' ' + p];
    if (got === undefined) return Promise.reject(new Error('unexpected ' + method + ' ' + p));
    if (got && got.promise) return got.promise;
    return got instanceof Error ? Promise.reject(got) : Promise.resolve(clone(got));
  };
  r.reads = () => calls.filter(c => c.method === 'GET').map(c => c.p);
  r.open = async (token = TOKEN) => {
    const html = await r.V.askPage(token);
    for (const t of timers.filter(t => t.ms === 0 && !t.fired)) { t.fired = true; t.fn(); }
    await flush();
    return html;
  };
  r.ask = async (form, text) => { form.box.value = text; await r.V.askSubmit(form); };
  return r;
}
const LATEST = 'GET /customer/ask/threads';
const THREAD = 'GET /customer/ask/threads/h1';
const answeredAs = (id, extra) => Object.assign(clone(fixture('answered')), { turn_id: id, thread_id: 'h1' }, extra);
const asState = (name, id) => Object.assign(clone(fixture(name)), { turn_id: id, thread_id: 'h1' });
const empty = r => { assert.equal(r.thread.sets, 0, 'thread not drawn'); assert.equal(r.thread.html, ''); assert.equal(r.examples.hidden, false, 'examples stay'); };
const drawn = (r, ids) => {
  assert.equal(r.thread.sets, 1, 'thread drawn once');
  for (const id of ids) assert.ok(r.thread.html.includes(`id="ask-turn-${id}"`), id);
  assert.equal((r.thread.html.match(/id="ask-turn-/g) || []).length, ids.length);
  assert.equal(r.examples.hidden, true, 'examples hide');
  assert.equal(r.follow.hidden, false, 'follow-up box shows');
};

test('an answered latest thread is read and drawn; a follow-up goes into it', async () => {
  const r = await rig();
  r.routes[LATEST] = { thread_id: 'h1' };
  r.routes[THREAD] = { thread_id: 'h1', turns: [answeredAs('t1')] };
  const html = await r.open();
  assert.ok(html.includes('Try one of these'), 'the page first draws the empty state');
  assert.deepEqual(r.reads(), ['/customer/ask/threads', '/customer/ask/threads/h1']);
  assert.ok(r.calls.every(c => c.signal), 'page reads carry the page signal');
  drawn(r, ['t1']);
  r.routes['POST /customer/ask'] = { turn_id: 't2', thread_id: 'h1' };
  await r.ask(r.follow, 'And which shows?');
  assert.deepEqual(r.calls.filter(c => c.method === 'POST')[0].body, { question: 'And which shows?', thread_id: 'h1' });
  assert.equal(r.toasts.length, 0);
});

test('a partial latest turn is drawn, with the earlier turns of its thread', async () => {
  const r = await rig();
  r.routes[LATEST] = { thread_id: 'h1' };
  r.routes[THREAD] = { thread_id: 'h1', turns: [answeredAs('t1'), asState('partial', 't2')] };
  await r.open();
  drawn(r, ['t1', 't2']);
});

// mutant: hidden turns kept
test('a hidden newest turn is skipped: alone it leaves the empty page, after an answer the answer shows', async () => {
  const r = await rig();
  r.routes[LATEST] = { thread_id: 'h1' };
  r.routes[THREAD] = { thread_id: 'h1', turns: [asState('hidden', 't1')] };
  await r.open();
  empty(r);
  assert.ok(!r.thread.html.includes('no longer available'));
  const r2 = await rig();
  r2.routes[LATEST] = { thread_id: 'h1' };
  r2.routes[THREAD] = { thread_id: 'h1', turns: [answeredAs('t1'), asState('hidden', 't2')] };
  await r2.open();
  drawn(r2, ['t1']);
  assert.ok(!r2.thread.html.includes('no longer available'));
});

// mutants: any state drawn; sources not required
// ASK-RESTORE-F1: failed and cancelled newest turns moved to the case below.
test('a newest turn that is not an answered or partial turn with a source keeps the empty page', async () => {
  for (const newest of [asState('nothing', 't2'),
    asState('working', 't2'), answeredAs('t2', { sources: [] }), answeredAs('t2', { state: 'partial', sources: [] }),
    answeredAs('t2', { state: 'nothing' }),
    answeredAs('t2', { state: 'working', step: 2 })]) {
    const r = await rig();
    r.routes[LATEST] = { thread_id: 'h1' };
    r.routes[THREAD] = { thread_id: 'h1', turns: [answeredAs('t1'), newest] };
    await r.open();
    empty(r);
  }
});

// ASK-RESTORE-F1 (ARV-C-02): a failed or cancelled newest turn no longer hides
// the cited answer before it; both are drawn, the newer one with Ask again.
// mutants: the skip removed; the skipped turn left out of the drawing
test('a cited answer, then a failed or cancelled turn: both are drawn, the unfinished one with Ask again', async () => {
  for (const newest of [asState('failed', 't2'), asState('cancelled', 't2'), answeredAs('t2', { state: 'failed' }), answeredAs('t2', { state: 'cancelled' })]) {
    const r = await rig();
    r.routes[LATEST] = { thread_id: 'h1' };
    r.routes[THREAD] = { thread_id: 'h1', turns: [answeredAs('t1'), newest] };
    await r.open();
    assert.equal(r.thread.sets, 1, 'thread drawn once');
    for (const id of ['t1', 't2']) assert.ok(r.thread.html.includes(`id="ask-turn-${id}"`), id);
    assert.equal((r.thread.html.match(/id="ask-turn-/g) || []).length, 2);
    assert.match(r.thread.html, /data-action="ask-again"[^>]*data-turn="t2"/, newest.state);
    assert.equal(r.examples.hidden, true, 'examples hide');
    assert.equal(r.follow.hidden, true, 'no follow-up box under an unfinished turn');
  }
});

test('no latest thread reads nothing more and keeps the empty page', async () => {
  const r = await rig();
  r.routes[LATEST] = { thread_id: null };
  await r.open();
  assert.deepEqual(r.reads(), ['/customer/ask/threads']);
  empty(r);
});

test('a failed read keeps the empty page with no message, banner or toast', async () => {
  const fail = (status, code) => Object.assign(new Error(code), { status, body: { error: { code } } });
  for (const [latest, thread] of [[fail(500, 'INTERNAL'), undefined], [fail(403, 'FEATURE_DISABLED'), undefined],
    [{ thread_id: 'h1' }, fail(404, 'NOT_FOUND')], [{ thread_id: 'h1' }, { thread_id: 'h1' }]]) {
    const r = await rig();
    r.routes[LATEST] = latest;
    if (thread !== undefined) r.routes[THREAD] = thread;
    await r.open();
    empty(r);
    assert.equal(r.top.slot.textContent, '');
    assert.equal(r.top.slot.hidden, true);
    assert.equal(r.status.textContent, '');
    assert.equal(r.toasts.length, 0);
  }
});

test('turns already in this tab, a brand question, or Ask paused read nothing', async () => {
  const r = await rig();
  r.routes['POST /customer/ask'] = { turn_id: 't9', thread_id: 'h9' };
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  r.timers.length = 0;
  await r.open();
  assert.ok(!r.reads().includes('/customer/ask/threads'));
  const b = await rig('#/ask?brand=Quince');
  b.routes['POST /customer/ask'] = { turn_id: 't9', thread_id: 'h9' };
  await b.open();
  assert.ok(!b.reads().includes('/customer/ask/threads'));
  const p = await rig();
  p.E.current.askAvailable = false;
  await p.open();
  assert.deepEqual(p.reads(), []);
});

// Navigation safety: each guard alone drops a late answer (mutants: one per guard).
const leaves = {
  'the member left #/ask': r => { r.E.current.route = '#/overview'; r.E.current.fetchToken += 1; r.E.current.authorityEpoch += 1; },
  'a later render took the page': r => { r.E.current.fetchToken += 1; },
  'the route alone changed': r => { r.E.current.route = '#/overview'; },
  'the authority epoch changed (workspace or member)': r => { r.E.current.authorityEpoch += 1; },
  'the Ask store was cleared (sign-out)': r => { r.E.clearRetainedStores(); },
};
for (const [name, leave] of Object.entries(leaves)) {
  for (const which of ['latest', 'thread']) {
    test(`a ${which} read that lands after ${name} draws nothing`, async () => {
      const r = await rig();
      const latest = defer(), thread = defer();
      r.routes[LATEST] = which === 'latest' ? latest : { thread_id: 'h1' };
      r.routes[THREAD] = which === 'thread' ? thread : { thread_id: 'h1', turns: [answeredAs('t1')] };
      await r.open();
      leave(r);
      latest.resolve({ thread_id: 'h1' });
      thread.resolve({ thread_id: 'h1', turns: [answeredAs('t1')] });
      await flush(); await flush();
      empty(r);
      r.routes[LATEST] = { thread_id: null };
      r.E.current.route = '#/ask';
      const html = await r.V.askPage(r.E.current.fetchToken);
      assert.ok(!html.includes('ask-turn-t1'), 'no turn kept for the next visit');
    });
  }
}

test('a question asked while the read is in flight keeps its turn', async () => {
  const r = await rig();
  const latest = defer();
  r.routes[LATEST] = latest;
  r.routes[THREAD] = { thread_id: 'h1', turns: [answeredAs('t1')] };
  r.routes['POST /customer/ask'] = { turn_id: 't9', thread_id: 'h9' };
  await r.open();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  latest.resolve({ thread_id: 'h1' });
  await flush(); await flush();
  assert.ok(r.thread.html.includes('id="ask-turn-t9"'));
  assert.ok(!r.thread.html.includes('ask-turn-t1'), 'the old answer does not replace the new turn');
  assert.equal(r.thread.sets, 1, 'drawn once, for the new turn');
  r.routes['POST /customer/ask'] = { turn_id: 't10', thread_id: 'h9' };
  r.E.current.askAvailable = true;
  const html = await r.V.askPage(r.E.current.fetchToken);
  assert.ok(html.includes('ask-turn-t9') && !html.includes('ask-turn-t1'));
});

// mutant: no sending guard
test('a question still sending when the read lands is not drawn over', async () => {
  const r = await rig();
  const post = defer();
  r.routes[LATEST] = { thread_id: 'h1' };
  const thread = defer();
  r.routes[THREAD] = thread;
  r.routes['POST /customer/ask'] = post;
  await r.open();
  r.top.box.value = 'How do hosts pitch VPNs?';
  const sending = r.V.askSubmit(r.top);
  thread.resolve({ thread_id: 'h1', turns: [answeredAs('t1')] });
  await flush(); await flush();
  assert.equal(r.thread.sets, 0, 'nothing drawn while the question is sending');
  post.resolve({ turn_id: 't9', thread_id: 'h9' });
  await sending;
  assert.ok(r.thread.html.includes('id="ask-turn-t9"') && !r.thread.html.includes('ask-turn-t1'));
  assert.equal(r.thread.sets, 1);
});
