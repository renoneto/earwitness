/* ASK-RESTORE (lead 2026-10-29; WIN-REVIEW #2(b)): a paused Ask shows the
   asker's last cited answer read-only, under the paused line.
   On #/ask with Ask paused at the spend stop (`ask_paused`, switch on) and no
   turns in this tab, the page reads GET /customer/ask/threads and the thread,
   with the same guards as the open page (askLoadLatest: the page's render,
   #/ask, the authority epoch, the Ask store, no send, no turns). When the
   newest shown turn is an answered or partial turn with a source, the thread
   is drawn under the paused line: no question or follow-up box, no Ask, Ask
   again or Cancel, no POST /customer/ask. Sources, clips and the report
   control stay, as for an answer held at the spend stop (TRC-01). Anything
   else, a failed read, Ask switched off, or a read that lands after the page
   or the person changed draws nothing.
   Part 1 boots the real app (EW.api, render, tab return), its harness copied
   from tests/frontend_tabreturn_lows.test.cjs; part 2 drives askPage alone,
   rig copied from tests/frontend_ask_latest.test.cjs, one guard at a time.
   Fake transport only: no model, no spend.
   Run: node --test tests/frontend_ask_restore_paused.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'ask-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
const clone = o => JSON.parse(JSON.stringify(o));

const OWNER = 'oidc:https://i.example:owner';
const OTHER = 'oidc:https://i.example:other';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
const SIGNIN_REQUIRED = { error: { code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required', status: 401, details: {} },
  signin: { available: true, reason: '', login_url: null, methods: ['password'] } };
const ASK_PAUSED = { ask_available: false, ask_paused: true };
const ASK_OPEN = { ask_available: true, ask_paused: false };
const ASK_OFF = { ask_available: false };
const QUESTION = 'What do hosts say about quince jam?';
const turnAs = (name, id, extra) => Object.assign(clone(fixture(name)), { turn_id: id, thread_id: 'h1', question: QUESTION + ' ' + id }, extra);
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
const defer = () => { let resolve; const promise = new Promise(a => { resolve = a; }); return { promise, resolve }; };

// -- part 1: the real app ------------------------------------------------------
function loadPages(route, fetch) {
  const listeners = {};
  const docListeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, location: { hash: route, pathname: '/app/' },
    history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {}, scrollY: 0, fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  sandbox.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  sandbox.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  sandbox.listeners = listeners;
  sandbox.docListeners = docListeners;
  return sandbox;
}
// threads: principal -> the thread the server holds for that asker (asker only:
// anyone else gets null from /threads and 404 from /threads/<id>). hold: the
// next latest or thread read waits on a deferred (answered for the asker who
// sent it).
function server() {
  const state = { calls: [], signedOut: false, session: {}, ctx: 'ctx-owner', threads: {}, hold: {} };
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  const notFound = () => reply(404, { error: { code: 'NOT_FOUND', message: 'not found', status: 404, details: {} } });
  state.reply = reply;
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    state.calls.push(`${init.method || 'GET'} ${p}`);
    if (p === '/session' && state.onSession) { const f = state.onSession; state.onSession = null; f(); }
    if (state.signedOut) return reply(401, SIGNIN_REQUIRED);
    const who = state.session.principal || OWNER;
    if (p === '/session') return reply(200, clone({ ...HOSTED, workspace_context: state.ctx, ...state.session }));
    if (p === '/customer/ask' && init.method === 'POST') return state.askPost ? state.askPost() : reply(503, { error: { code: 'ASK_PAUSED', message: 'paused', status: 503, details: {} } });
    const polled = /^\/customer\/ask\/turns\/([^/]+)$/.exec(p);
    if (polled && (init.method || 'GET') === 'GET') return state.turn && state.turn.turn_id === decodeURIComponent(polled[1]) ? reply(200, clone(state.turn)) : notFound();
    if (p === '/customer/ask/threads' && (init.method || 'GET') === 'GET') {
      const held = state.hold.latest;
      state.hold.latest = null;
      if (held) await held.promise;
      const mine = state.threads[who];
      return reply(200, { thread_id: mine ? mine.thread_id : null });
    }
    const one = /^\/customer\/ask\/threads\/([^/]+)$/.exec(p);
    if (one && (init.method || 'GET') === 'GET') {
      const held = state.hold.thread;
      state.hold.thread = null;
      if (held) await held.promise;
      const mine = state.threads[who];
      return mine && mine.thread_id === decodeURIComponent(one[1]) ? reply(200, clone(mine)) : notFound();
    }
    if (p.startsWith('/customer/alerts')) return reply(200, { alerts: [], workspace_context: state.ctx });
    if (p === '/customer/brands') return reply(200, { brands: [], workspace_context: state.ctx });
    if (p.startsWith('/customer/campaigns')) return reply(200, { campaigns: [], workspace_context: state.ctx });
    if (p.startsWith('/customer/preferences')) {
      if (state.revokeOnPrefs) { state.revokeOnPrefs = false; state.signedOut = true; }
      return reply(200, { preferences: {}, workspace_context: state.ctx });
    }
    if (p.startsWith('/workspaces/')) return reply(200, { members: [], invites: [], workspace_context: state.ctx });
    if (p.startsWith('/customer/saved-items')) return reply(200, { items: [], workspace_context: state.ctx });
    return notFound();
  };
  return state;
}
function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false, ...props };
}
// #main paints like the browser; its .ask-page (while drawn) takes appended HTML.
function mainStub() {
  let html = '';
  const main = node({ inserted: [], appended: [], painted: [], forms: [] });
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.inserted = []; main.appended = []; main.forms = []; main.painted.push(v); } });
  main.insertAdjacentHTML = (pos, h) => { main.inserted.unshift({ html: h, remove() { main.inserted = main.inserted.filter(x => x !== this); } }); main.painted.push(h); };
  const askPage = node({ insertAdjacentHTML(pos, h) { assert.equal(pos, 'beforeend'); main.appended.push(h); main.painted.push(h); } });
  main.querySelector = sel => (sel === '.ask-page' && /class="ask-page"/.test(html) ? askPage : null);
  main.shown = () => main.inserted.map(b => b.html).join('') + html + main.appended.join('');
  return main;
}
// urlHistory: replaceState and pushState rewrite location.hash, as a browser does.
async function page(route, { session = {}, threads = {}, hold = {}, urlHistory = false } = {}) {
  const srv = server();
  srv.session = session;
  const win = loadPages(route, srv.fetch);
  if (urlHistory) {
    for (const mode of ['replaceState', 'pushState']) {
      win.history[mode] = (state, _title, url) => { win.history.state = state; if (url !== undefined) win.location.hash = url; };
    }
  }
  for (let i = 0; i < 5; i++) await tick();
  // The boot's own render is done: the reads below are this render's.
  srv.threads = threads;
  srv.hold = hold;
  srv.calls.length = 0;
  const main = mainStub();
  win.document.querySelector = sel => (sel === '#main' ? main : node());
  win.document.querySelectorAll = () => [];
  win.document.getElementById = () => null;
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  E.toast = () => {};
  win.location.hash = route;
  E.current.route = route;
  await win.EWViews.render();
  await drain(40);
  return { srv, win, E, main };
}
async function tabReturn(t) {
  for (const fn of t.win.listeners.blur || []) fn({ type: 'blur' });
  for (const fn of t.win.listeners.focus || []) fn({ type: 'focus' });
  await drain(80);
}
async function nav(t, route) {
  t.win.location.hash = route;
  t.E.current.route = route;
  await t.win.EWViews.render();
  await drain(120);
}
const PAUSED_LINE = 'Ask is paused right now.';
const thread1 = (...turns) => ({ [OWNER]: { thread_id: 'h1', turns } });
const asks = t => t.srv.calls.filter(c => c.includes('/customer/ask'));
const everPainted = (t, s) => t.main.painted.some(h => h.includes(s));
// The thread drawn under the paused line, and nothing that sends or spends.
function assertRestored(t, ids) {
  const shown = t.main.shown();
  const at = shown.indexOf(PAUSED_LINE);
  assert.ok(at >= 0, 'the paused line shows');
  for (const id of ids) {
    assert.ok(shown.indexOf(`id="ask-turn-${id}"`) > at, `${id} is drawn under the paused line`);
    assert.ok(shown.includes(QUESTION + ' ' + id), `${id}'s question`);
  }
  assert.equal((shown.match(/id="ask-turn-/g) || []).length, ids.length, 'only these turns');
  assertReadOnly(shown);
}
function assertReadOnly(shown) {
  const outsideReport = shown.replace(/<details class="report-problem ask-report">[\s\S]*?<\/details>/g, '');
  assert.doesNotMatch(outsideReport, /<textarea|<input/, 'no question or follow-up box');
  assert.doesNotMatch(shown, /id="ask-(q|fu|form|followup)"|name="question"|data-ask=/, 'no Ask form');
  assert.doesNotMatch(shown, /data-action="ask-(again|cancel|example)"/, 'no Ask again, Cancel or example');
  assert.doesNotMatch(outsideReport, /type="submit"/, 'no Ask button');
  assert.doesNotMatch(shown, />Ask<\/button>|>Ask again<|>Cancel</, 'no send words on a button');
}
const noAskPost = t => assert.ok(!t.srv.calls.includes('POST /customer/ask'), 'no POST /customer/ask');

test('a reload while paused (no turns in this tab) shows the last cited answer under the paused line, read-only', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  assert.deepEqual(asks(t), ['GET /customer/ask/threads', 'GET /customer/ask/threads/h1']);
  assertRestored(t, ['t1']);
  const shown = t.main.shown();
  assert.match(shown, /class="ask-sources"/, 'the sources stay');
  assert.match(shown, /data-action="ask-cite"/, 'the citation chips stay');
  assert.match(shown, /class="report-problem ask-report"/, 'the report control stays (TRC-01)');
  assert.match(shown, /data-action="clip-toggle"/, 'the clip control stays');
  assert.doesNotMatch(shown, /Try one of these/, 'no examples');
  noAskPost(t);
});

test('a partial newest turn is drawn with the earlier answers of its thread; failed, cancelled and working turns are left out', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('failed', 't0'), turnAs('cancelled', 't1'),
    turnAs('working', 't2'), turnAs('answered', 't3'), turnAs('nothing', 't4'), turnAs('hidden', 't5'), turnAs('partial', 't6')) });
  assertRestored(t, ['t3', 't4', 't6']);
  assert.doesNotMatch(t.main.shown(), /didn’t finish|no longer available|ask-steps/);
  noAskPost(t);
});

// A failed, working (FIX3) or cancelled (FIX4) newest turn no longer hides the answer before it (below).
test('the newest shown turn uncited, nothing or hidden: nothing is drawn, the paused line stays', async () => {
  for (const newest of [turnAs('answered', 't2', { sources: [] }), turnAs('partial', 't2', { sources: [] }), turnAs('nothing', 't2'),
    turnAs('hidden', 't2'),
    // the state rule alone, with sources present
    turnAs('answered', 't2', { state: 'nothing' })]) {
    const threads = thread1(turnAs('answered', 't1'), newest);
    if (newest.state === 'hidden') threads[OWNER].turns = [newest];
    const t = await page('#/ask', { session: ASK_PAUSED, threads });
    assert.ok(t.main.shown().includes(PAUSED_LINE));
    assert.equal(t.main.appended.length, 0, `${newest.state} with ${(newest.sources || []).length} sources drew a thread`);
    assert.ok(!everPainted(t, 'ask-turn-'), newest.state);
  }
});

test('a hidden newest turn after a cited answer: the answer shows, never "no longer available"', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1'), turnAs('hidden', 't2')) });
  assertRestored(t, ['t1']);
  assert.doesNotMatch(t.main.shown(), /no longer available/);
});

test('paused with no history: the paused page alone, as today', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED });
  assert.deepEqual(asks(t), ['GET /customer/ask/threads']);
  assert.ok(t.main.shown().includes(PAUSED_LINE));
  assert.equal(t.main.appended.length, 0);
  assert.equal(t.main.shown(), t.main.innerHTML);
});

test('Ask switched off (not paused): no Ask read, nothing drawn, as today', async () => {
  const t = await page('#/ask', { session: ASK_OFF, threads: thread1(turnAs('answered', 't1')) });
  assert.deepEqual(asks(t), []);
  assert.ok(!everPainted(t, 'ask-turn-'));
});

test('a second member never sees the first member\'s thread', async () => {
  const t = await page('#/ask', { session: { ...ASK_PAUSED, principal: OTHER, email: 'other@example.test', role: 'editor' },
    threads: thread1(turnAs('answered', 't1')) });
  assert.ok(t.main.shown().includes(PAUSED_LINE));
  assert.ok(!everPainted(t, 'ask-turn-'));
  assert.ok(!everPainted(t, QUESTION));
});

test('another member signs in on the tab while the thread read is out: the first member\'s answer is never drawn', async () => {
  const hold = { thread: defer() };
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')), hold: { thread: hold.thread } });
  assert.ok(!everPainted(t, 'ask-turn-'), 'fixture: the read is still out');
  t.srv.session = { ...ASK_PAUSED, principal: OTHER, email: 'other@example.test', role: 'editor' };
  t.srv.ctx = 'ctx-other';
  await tabReturn(t);
  hold.thread.resolve();
  await drain(40);
  assert.ok(!everPainted(t, 'ask-turn-t1'));
  assert.ok(!everPainted(t, QUESTION));
});

test('another member signs in on the tab after the answer is drawn: it is gone, and theirs is not the first member\'s', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  assertRestored(t, ['t1']);
  t.srv.session = { ...ASK_PAUSED, principal: OTHER, email: 'other@example.test', role: 'editor' };
  t.srv.ctx = 'ctx-other';
  await tabReturn(t);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other member adopted');
  assert.ok(!t.main.shown().includes(QUESTION));
});

test('a downgrade at a tab return while the thread read is out: the old read is dropped; the page reads again', async () => {
  const hold = { thread: defer() };
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')), hold: { thread: hold.thread } });
  t.srv.session = { ...ASK_PAUSED, role: 'viewer' };
  t.srv.ctx = 'ctx-viewer';
  t.srv.threads = thread1(turnAs('answered', 't2'));
  await tabReturn(t);
  assert.equal(t.E.current.workspaceRole, 'viewer', 'fixture: the viewer role adopted');
  hold.thread.resolve();
  await drain(40);
  assert.ok(!everPainted(t, 'ask-turn-t1'), 'the read from before the downgrade was drawn');
  assertRestored(t, ['t2']);
});

test('sign-out while the thread read is out: nothing is drawn', async () => {
  const hold = { thread: defer() };
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')), hold: { thread: hold.thread } });
  t.srv.signedOut = true;
  await tabReturn(t);
  hold.thread.resolve();
  await drain(40);
  assert.ok(!everPainted(t, 'ask-turn-'));
});

test('sign-out after the answer is drawn: it is gone', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  assertRestored(t, ['t1']);
  t.srv.signedOut = true;
  await tabReturn(t);
  assert.ok(!t.main.shown().includes(QUESTION));
});

test('leaving #/ask while paused and coming back reads and draws the answer again (it is not kept in the Ask store)', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  assertRestored(t, ['t1']);
  await nav(t, '#/alerts');
  assert.ok(!t.main.shown().includes(QUESTION));
  await nav(t, '#/ask');
  assertRestored(t, ['t1']);
  noAskPost(t);
});

test('paused, then the ledger reopens at a tab return: the open page with the answer and the follow-up box (Meta\'s (b))', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  assertRestored(t, ['t1']);
  t.srv.session = ASK_OPEN;
  await tabReturn(t);
  const shown = t.main.shown();
  assert.ok(!shown.includes(PAUSED_LINE));
  assert.match(shown, /id="ask-q"/, 'the open page');
});

// TR R2 (tests/frontend_tabreturn_lows.test.cjs), paused: Settings' own session
// read finds no one signed in; #/ask is opened just as the follow-up reads the
// session. The follow-up draws the sign-in page; the Ask read waits it out, so
// no second boundary ("Checking access…") and no second sign-in render.
test('the session ends as the paused #/ask opens: the sign-in page follows once, with no Ask read and no second boundary', async () => {
  const t = await page('#/alerts', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  t.srv.revokeOnPrefs = true;
  const at = {};
  t.srv.onSession = () => {
    t.srv.onSession = () => {
      t.win.location.hash = '#/ask';
      t.E.current.route = '#/ask';
      at.token = t.E.current.fetchToken;
      at.reads = t.srv.calls.length;
      t.win.EWViews.render();
    };
  };
  await nav(t, '#/settings');
  await new Promise(res => setTimeout(res, 60));
  await drain(200);
  assert.ok(at.token !== undefined, 'fixture: the follow-up read went out');
  assert.match(t.main.shown(), /id="signin-email"/, 'the sign-in page');
  assert.equal(t.E.current.fetchToken - at.token, 2, 'Ask, then the sign-in page, once');
  assert.deepEqual(t.srv.calls.slice(at.reads).filter(c => c.includes('/customer/ask')), [], 'no Ask read while the session is re-read');
  assert.ok(!everPainted(t, 'ask-turn-'));
});

/* ASK-RESTORE-FIX1 (open issue 3, lead): a tab that already holds Ask turns
   (an answer held at the spend stop, TRC-01) shows them read-only under the
   paused line at the next #/ask, by the restore's rules: the newest shown turn
   a cited answer; only answered, partial and nothing turns; no box, no send.
   No Ask read: the turns are this tab's. A role, person or sign-out change
   still drops them (the Ask store clears). */
// The Ask page sends one question and polls it to `final` (Ask open, this tab).
async function answerInTab(t, final) {
  t.srv.askPost = () => t.srv.reply(200, { turn_id: final.turn_id, thread_id: final.thread_id });
  t.srv.turn = final;
  const box = node({ value: final.question }), slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  await t.win.EWViews.askSubmit(form);
  await new Promise(res => setTimeout(res, 1700));
  await drain(40);
  t.srv.askPost = null;
}
// Ask open, a cited answer in this tab, the spend stop at a tab return (the
// answer is held on screen, TRC-01), then away and back to #/ask.
async function heldThenPaused(route) {
  const t = await page('#/ask', { session: ASK_OPEN });
  await answerInTab(t, turnAs('answered', 't1'));
  await nav(t, '#/ask');
  assert.match(t.main.shown(), /id="ask-turn-t1"/, 'fixture: the answer on the open page');
  t.srv.session = ASK_PAUSED;
  await tabReturn(t);
  assert.match(t.main.shown(), /id="ask-q"/, 'fixture: the answer held on the open page (TRC-01)');
  await nav(t, route);
  if (route !== '#/ask') await nav(t, '#/ask');
  return t;
}
for (const route of ['#/ask', '#/alerts']) {
  test(`FIX1: the answer held in this tab at the spend stop, then ${route === '#/ask' ? '#/ask again' : '#/alerts and back'}: drawn read-only under the paused line, with no Ask read`, async () => {
    const t = await heldThenPaused(route);
    const from = t.srv.calls.length;
    await nav(t, '#/ask');
    assertRestored(t, ['t1']);
    const shown = t.main.shown();
    assert.match(shown, /class="ask-sources"/, 'the sources stay');
    assert.match(shown, /class="report-problem ask-report"/, 'the report control stays (TRC-01)');
    assert.match(shown, /data-action="clip-toggle"/, 'the clip control stays');
    assert.deepEqual(t.srv.calls.slice(from).filter(c => c.includes('/customer/ask')), [], 'no Ask read or send: the turns are this tab\'s');
    assert.equal(t.main.appended.length, 0, 'drawn with the page, not appended after a read');
  });
}

const ROLE_CHANGES = {
  'a downgrade (new context)': t => { t.srv.session = { ...ASK_PAUSED, role: 'viewer' }; t.srv.ctx = 'ctx-viewer'; },
  'another member signs in': t => { t.srv.session = { ...ASK_PAUSED, principal: OTHER, email: 'other@example.test', role: 'editor' }; t.srv.ctx = 'ctx-other'; },
  'a sign-out': t => { t.srv.signedOut = true; },
};
for (const [name, change] of Object.entries(ROLE_CHANGES)) {
  test(`FIX1: the held thread drawn while paused, then ${name} at a tab return: it is dropped and never painted again`, async () => {
    const t = await heldThenPaused('#/ask');
    assertRestored(t, ['t1']);
    t.srv.threads = {};
    change(t);
    const from = t.main.painted.length;
    await tabReturn(t);
    await nav(t, '#/ask');
    assert.ok(!t.main.painted.slice(from).some(h => h.includes('ask-turn-t1') || h.includes(QUESTION)), 'the thread was painted after the change');
    assert.ok(!t.main.shown().includes(QUESTION));
  });
}

/* Ask switched off is not the spend stop: a typed #/ask reads nothing and
   draws no turns. ASK-OFF-1 (Reno's batch; UX 7f1aaef45, 11f8e4c2c): its own
   line in its own element, its own heading and no lede. */
const OFF_LINE = 'Ask is off for now. When it is on, Muse Spark, Meta’s AI model, answers your questions from the episodes, with every claim linked to the passage behind it.';
const OFF_P = `<p class="ask-off ask-unavailable" role="status">${OFF_LINE}</p>`;
const OFF_HEAD = '<header class="ask-head"><h1>Ask about the ads</h1></header>';
const PAUSED_HEAD = '<header class="ask-head"><h1>Ask what the shows in this collection are saying</h1><p class="ask-lede">Every claim links to the passage behind it, with a clip where we have one. If the episodes don\'t support something, we say so.</p></header>';
test('FIX1: Ask switched off, a typed #/ask: the off line; no Ask request', async () => {
  const t = await page('#/ask', { session: ASK_OFF, threads: thread1(turnAs('answered', 't1')) });
  const shown = t.main.shown();
  assert.ok(shown.includes(OFF_P), 'the off line');
  assert.ok(!shown.includes(PAUSED_LINE), 'the paused line on the off page');
  assert.deepEqual(asks(t), []);
});
test('ASK-OFF-1: Ask switched off: the heading is "Ask about the ads", with no lede', async () => {
  const t = await page('#/ask', { session: ASK_OFF });
  const shown = t.main.shown();
  assert.ok(shown.includes(`<div class="ask-page">${OFF_HEAD}${OFF_P}</div>`), 'the off heading, no lede, then the off line');
  assert.doesNotMatch(shown, /class="ask-lede"/, 'the lede on the off page');
  assert.doesNotMatch(shown, /Ask what the shows in this collection are saying/);
});
test('ASK-OFF-1: the spend stop keeps the paused line in its own element, and today\'s heading and lede', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED });
  const shown = t.main.shown();
  assert.ok(shown.includes(`<div class="ask-page">${PAUSED_HEAD}<p class="ask-off" role="status">${PAUSED_LINE}</p>`));
  assert.ok(!shown.includes('ask-unavailable'));
  assert.ok(!shown.includes(OFF_LINE));
  assert.ok(!shown.includes('Ask about the ads'));
});

/* ASK-RESTORE-FIX2 ARS-R1-02: a #/ask?brand=X opened while Ask is paused or
   switched off drops ?brand from the route, as the open page does, so the
   brand question is never sent later when Ask reopens (a tab return, a render). */
for (const [name, session] of [['paused', ASK_PAUSED], ['switched off', ASK_OFF]]) {
  test(`FIX2: #/ask?brand= opened while Ask is ${name}, then Ask reopens and the page is drawn again: no question is sent`, async () => {
    const t = await page('#/ask?brand=Quince', { session, threads: thread1(turnAs('answered', 't1')), urlHistory: true });
    noAskPost(t);
    assert.equal(t.E.current.route, '#/ask', 'the brand stayed in the route');
    assert.ok(!(t.E.current.publishedRoute || '').includes('brand'), 'the brand stayed in the published route');
    assert.equal(t.win.location.hash, '#/ask', 'the brand stayed in the address bar');
    t.srv.session = ASK_OPEN;
    await tabReturn(t);
    await t.win.EWViews.render();
    await drain(120);
    await nav(t, t.E.current.route);
    noAskPost(t);
  });
}

/* ASK-RESTORE-FIX3 (FIX2 open issue 1; Meta and App, paused path only): a
   follow-up refused at the reserve stays in its thread and reads as `failed`.
   On the paused page a failed or working turn no longer hides the cited
   answer before it: the "newest cited" rule runs on the turns without them,
   as askPausedThread never draws them. The open page keeps its rule, and the
   thread read (the API) is unchanged. In the tab the refused send shows its
   refusal and adds no turn. */
const FOLLOW_Q = 'And on day 121?';
const REFUSED = turnAs('failed', 't2', { question: FOLLOW_Q, error_code: 'ASK_PAUSED' });
async function answerThenRefusedFollowUp() {
  const t = await page('#/ask', { session: ASK_OPEN });
  await answerInTab(t, turnAs('answered', 't1'));
  await nav(t, '#/ask');
  assert.match(t.main.shown(), /id="ask-turn-t1"/, 'fixture: the answer on the open page');
  const box = node({ value: FOLLOW_Q }), slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'follow' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  const from = t.srv.calls.length;
  await t.win.EWViews.askSubmit(form);
  await drain(40);
  assert.deepEqual(t.srv.calls.slice(from).filter(c => c.includes('/customer/ask')), ['POST /customer/ask'], 'fixture: the follow-up was sent');
  return { t, slot };
}
test('FIX3: an answer, then a follow-up refused at the reserve: the tab shows the refusal and adds no turn (unchanged)', async () => {
  const { t, slot } = await answerThenRefusedFollowUp();
  assert.equal(slot.textContent, PAUSED_LINE, 'the refusal the asker sees at send time');
  assert.equal(slot.hidden, false);
  await nav(t, '#/ask');
  const shown = t.main.shown();
  assert.equal((shown.match(/id="ask-turn-/g) || []).length, 1, 'only the answer');
  assert.doesNotMatch(shown, /didn’t finish|data-action="ask-again"/);
});
test('FIX3: an answer, then a follow-up refused at the reserve, then a reload while paused: the answer is drawn read-only under the paused line', async () => {
  await answerThenRefusedFollowUp();
  // The reload: a new tab. Before FIX4 the thread read held the refused follow-up,
  // as failed; it still stands for a turn that failed after it was accepted.
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1'), clone(REFUSED)) });
  assert.deepEqual(asks(t), ['GET /customer/ask/threads', 'GET /customer/ask/threads/h1']);
  assertRestored(t, ['t1']);
  assert.ok(!t.main.shown().includes(FOLLOW_Q), 'the failed turn is not drawn');
  assert.doesNotMatch(t.main.shown(), /didn’t finish/);
  noAskPost(t);
});
test('FIX3: a paused reload of [a cited answer, then a failed or working turn]: the answer is drawn read-only, without the newer turn', async () => {
  for (const newest of [turnAs('failed', 't2'), clone(REFUSED), turnAs('working', 't2'),
    turnAs('answered', 't2', { state: 'failed' }), turnAs('answered', 't2', { state: 'working', step: 2 })]) {
    const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1'), newest) });
    assertRestored(t, ['t1']);
    assert.doesNotMatch(t.main.shown(), /didn’t finish|ask-steps/, newest.state);
    noAskPost(t);
  }
});
test('FIX3: paused, [only a failed turn], [an uncited answer, then a failed turn] or [a cited answer, failed, then nothing]: nothing is drawn', async () => {
  for (const turns of [[turnAs('failed', 't1')], [clone(REFUSED)], [turnAs('working', 't1')],
    [turnAs('answered', 't1', { sources: [] }), clone(REFUSED)],
    [turnAs('answered', 't1'), clone(REFUSED), turnAs('nothing', 't3')]]) {
    const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(...turns) });
    assert.ok(t.main.shown().includes(PAUSED_LINE));
    assert.equal(t.main.appended.length, 0, turns.map(x => x.state).join(','));
    assert.ok(!everPainted(t, 'ask-turn-'));
  }
});
// The open path's FIX3 pin ([a cited answer, a failed turn] adopts no thread)
// moved to the ASK-RESTORE-F1 cases below: the open reload now draws it.

/* ASK-RESTORE-FIX4 (Meta agrees): (a) the thread read skips a turn refused at
   the reserve (refused_budget), as latest_turn and the day count do; the tab
   never showed it as a turn. So [an answer, a refused follow-up] reads as
   [the answer]: the server side is tests/test_ask_routes.py LatestThreadTest.
   (b) On the paused page a cancelled turn after the answer no longer hides it
   either. The open page keeps its rule for cancelled turns. */
test('FIX4 (a): an answer, then a follow-up refused at the reserve, then a reload with Ask open: the thread is drawn with the follow-up box', async () => {
  await answerThenRefusedFollowUp();
  // The reload: a new tab. The thread read skips the refused follow-up (FIX4).
  const t = await page('#/ask', { session: ASK_OPEN, threads: thread1(turnAs('answered', 't1')) });
  assert.deepEqual(asks(t), ['GET /customer/ask/threads', 'GET /customer/ask/threads/h1']);
  // The harness has no #ask-thread to repaint in place: the next render draws the adopted turns.
  await nav(t, '#/ask');
  const shown = t.main.shown();
  assert.match(shown, /id="ask-turn-t1"/, 'the answer');
  assert.equal((shown.match(/id="ask-turn-/g) || []).length, 1, 'only the answer');
  assert.ok(!shown.includes(FOLLOW_Q), 'the refused follow-up is not drawn');
  assert.ok(!shown.includes(PAUSED_LINE));
  assert.doesNotMatch(shown, /didn’t finish|data-action="ask-again"/);
  assert.match(shown, /<section class="ask-examples" hidden>/, 'the examples are hidden: a thread is shown');
  assert.match(shown, /id="ask-thread"/, 'the live thread');
  assert.match(shown, /data-ask="follow"/, 'the follow-up box: the normal open page');
  noAskPost(t);
});
test('FIX4 (a): the same thread read on a paused reload: the answer is drawn read-only under the paused line', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1')) });
  assertRestored(t, ['t1']);
  assert.ok(!t.main.shown().includes(FOLLOW_Q));
  noAskPost(t);
});
test('FIX4 (b): a paused reload of [a cited answer, then a cancelled turn]: the answer is drawn read-only, without the cancelled turn', async () => {
  for (const turns of [[turnAs('answered', 't1'), turnAs('cancelled', 't2')],
    [turnAs('answered', 't1'), turnAs('answered', 't2', { state: 'cancelled' })],
    [turnAs('answered', 't1'), clone(REFUSED), turnAs('cancelled', 't3')],
    [turnAs('answered', 't1'), turnAs('cancelled', 't2'), turnAs('failed', 't3')]]) {
    const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(...turns) });
    assertRestored(t, ['t1']);
    assert.doesNotMatch(t.main.shown(), /didn’t finish|data-action="ask-again"/, turns.map(x => x.state).join(','));
    noAskPost(t);
  }
});
test('FIX4 (b): paused, [only a cancelled turn] or [an uncited answer, then a cancelled turn]: nothing is drawn', async () => {
  for (const turns of [[turnAs('cancelled', 't1')], [turnAs('answered', 't1', { sources: [] }), turnAs('cancelled', 't2')]]) {
    const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(...turns) });
    assert.ok(t.main.shown().includes(PAUSED_LINE));
    assert.equal(t.main.appended.length, 0, turns.map(x => x.state).join(','));
    assert.ok(!everPainted(t, 'ask-turn-'));
  }
});
/* ASK-RESTORE-F1 (Meta 10-29, ARV-C-02, no-gap rule): with Ask open, a reload
   decides whether to show the thread from the newest turn that is not failed or
   cancelled, so a cancelled follow-up, or one that failed after it was
   accepted, no longer hides the answer before it. Every non-hidden turn is
   still drawn, so the member sees that follow-up as the tab did ("The answer
   didn't finish" with Ask again). A working newest turn keeps the empty page,
   as before: the open read does not poll it. (This replaces the FIX3 and FIX4
   open-path pins.) */
// The open page after the reload (the harness has no #ask-thread to repaint in
// place: the next render draws the adopted turns).
async function openReload(...turns) {
  const t = await page('#/ask', { session: ASK_OPEN, threads: thread1(...turns) });
  assert.deepEqual(asks(t), ['GET /customer/ask/threads', 'GET /customer/ask/threads/h1']);
  await nav(t, '#/ask');
  return t;
}
function assertOpenThread(t, ids, again) {
  const shown = t.main.shown();
  assert.ok(!shown.includes(PAUSED_LINE));
  assert.match(shown, /id="ask-thread"/, 'the live thread');
  for (const id of ids) assert.ok(shown.includes(`id="ask-turn-${id}"`), `${id} is drawn`);
  assert.equal((shown.match(/id="ask-turn-/g) || []).length, ids.length, 'every non-hidden turn, no other');
  for (const id of again) assert.match(shown, new RegExp(`data-action="ask-again"[^>]*data-turn="${id}"`), `${id} has Ask again`);
  assert.equal((shown.match(/data-action="ask-again"/g) || []).length, again.length, 'Ask again on the unfinished turns only');
  assert.match(shown, /<section class="ask-examples" hidden>/, 'the examples are hidden: a thread is shown');
  assert.match(shown, /id="ask-q"/, 'the top box');
  noAskPost(t);
}
function assertOpenEmpty(t, label) {
  const shown = t.main.shown();
  assert.ok(!everPainted(t, 'ask-turn-'), `${label}: a thread was adopted on the open page`);
  assert.match(shown, /<section class="ask-examples"><h2>Try one of these<\/h2>/, `${label}: the examples show`);
  assert.match(shown, /id="ask-q"/, 'the question box');
  assert.ok(!shown.includes(PAUSED_LINE));
  noAskPost(t);
}
test('F1: an open reload of [a cited answer, a cancelled follow-up] draws both turns: the answer, then the cancelled turn with Ask again', async () => {
  for (const newest of [turnAs('cancelled', 't2'), turnAs('answered', 't2', { state: 'cancelled' })]) {
    const t = await openReload(turnAs('answered', 't1'), newest);
    assertOpenThread(t, ['t1', 't2'], ['t2']);
    const shown = t.main.shown();
    assert.ok(shown.indexOf('id="ask-turn-t1"') < shown.indexOf('id="ask-turn-t2"'), 'the answer first');
    assert.match(shown, /<p class="ask-cancelled" role="status">The answer didn’t finish<\/p>/, 'the cancelled turn as the tab drew it');
    assert.match(shown, /class="ask-sources"/, "the answer's sources");
    assert.match(shown, /<form id="ask-followup"[^>]* hidden>/, 'no follow-up box under an unfinished turn, as in the tab');
  }
});
test('F1: an open reload of [a cited answer, a follow-up that failed after it was accepted] draws both turns, the failed one with Ask again', async () => {
  for (const newest of [turnAs('failed', 't2', { question: FOLLOW_Q }), turnAs('answered', 't2', { state: 'failed', question: FOLLOW_Q })]) {
    const t = await openReload(turnAs('answered', 't1'), newest);
    assertOpenThread(t, ['t1', 't2'], ['t2']);
    const shown = t.main.shown();
    assert.ok(shown.includes(FOLLOW_Q), "the follow-up's question");
    assert.match(shown, /<p class="ask-failed" role="alert">The answer didn’t finish<\/p>/, 'the failed turn as the tab drew it');
  }
});
// F2 (ARF1-01): Ask again on a restored stopped turn sends only on the click:
// one POST in the turn's own thread with its question (the tab's sent body is
// gone after a reload), then nothing on a second click.
function askPosts(t) {
  const posts = [];
  const inner = t.win.fetch;
  t.win.fetch = async (url, init = {}) => {
    if ((init.method || 'GET') === 'POST' && /\/customer\/ask$/.test(url)) posts.push(JSON.parse(init.body || '{}'));
    return inner(url, init);
  };
  t.srv.askPost = () => t.srv.reply(202, { turn_id: 't9', thread_id: 'h1' });
  return posts;
}
test('F2: Ask again on a restored [a cited answer, a cancelled or failed follow-up] sends one POST in that thread, and none on a second click', async () => {
  for (const state of ['cancelled', 'failed']) {
    const t = await openReload(turnAs('answered', 't1'), turnAs(state, 't2', { question: FOLLOW_Q }));
    const posts = askPosts(t);
    const shown = t.main.shown();
    assert.match(shown, /<button[^>]*data-action="ask-again"[^>]*data-turn="t2"/, `${state}: Ask again`);
    assert.doesNotMatch(shown, /<button[^>]*data-action="ask-again"[^>]*disabled/, `${state}: Ask again is not disabled`);
    noAskPost(t);
    await t.win.EWViews.askAgain('t2');
    await drain(40);
    assert.deepEqual(posts, [{ question: FOLLOW_Q, thread_id: 'h1' }], `${state}: one POST, in its thread`);
    await t.win.EWViews.askAgain('t2');
    await drain(40);
    assert.equal(posts.length, 1, `${state}: one resend per turn in this tab`);
    assert.equal(t.srv.calls.filter(c => c === 'POST /customer/ask').length, 1);
  }
});
// F2 (ARF1-03): the one-resend rule is per tab. After a reload, a stopped turn
// that was already resent shows Ask again enabled again; each click is still a
// new turn the server counts and caps (tests.test_ask_loop.ResendLoopTest).
test('F2: after a reload, a stopped turn already resent in the old tab has Ask again enabled (the rule is per tab)', async () => {
  const t = await openReload(turnAs('answered', 't1'), turnAs('cancelled', 't2', { question: FOLLOW_Q }), turnAs('failed', 't3', { question: FOLLOW_Q }));
  assertOpenThread(t, ['t1', 't2', 't3'], ['t2', 't3']);
  assert.doesNotMatch(t.main.shown(), /<button[^>]*data-action="ask-again"[^>]*disabled/);
});
test('F1: an open reload of [a cited answer, then failed and cancelled turns] draws all of them; a hidden turn stays out', async () => {
  const t = await openReload(turnAs('answered', 't1'), turnAs('failed', 't2'), turnAs('hidden', 't3'), turnAs('cancelled', 't4'));
  assertOpenThread(t, ['t1', 't2', 't4'], ['t2', 't4']);
  assert.doesNotMatch(t.main.shown(), /no longer available/);
});
test('F1: the open path still needs a cited answer before the unfinished turns: [failed], [cancelled], [uncited, failed] or [cited, failed, nothing] keep the empty page', async () => {
  for (const turns of [[turnAs('failed', 't1')], [turnAs('cancelled', 't1')], [turnAs('failed', 't1'), turnAs('cancelled', 't2')],
    [turnAs('answered', 't1', { sources: [] }), turnAs('failed', 't2')], [turnAs('nothing', 't1'), turnAs('cancelled', 't2')],
    [turnAs('answered', 't1'), turnAs('failed', 't2'), turnAs('nothing', 't3')]]) {
    assertOpenEmpty(await openReload(...turns), turns.map(x => x.state).join(','));
  }
});
// F3 (ARF1-02): a send that was never accepted (the start answered 500, or a
// restart while it was `reserving`) reads failed with NOT_STARTED. As the
// newest turn it keeps the empty page, as before F1 ([cited, NOT_STARTED,
// cancelled] is drawn since F5: below).
test('F3: an open reload of [a cited answer, a send that was never accepted (NOT_STARTED)] keeps the empty page, as before F1', async () => {
  for (const turns of [[turnAs('answered', 't1'), turnAs('failed', 't2', { question: FOLLOW_Q, error_code: 'NOT_STARTED' })],
    [turnAs('answered', 't1'), turnAs('answered', 't2', { state: 'failed', error_code: 'NOT_STARTED' })]]) {
    const t = await openReload(...turns);
    assertOpenEmpty(t, turns.map(x => x.state + (x.error_code ? ':' + x.error_code : '')).join(','));
    assert.doesNotMatch(t.main.shown(), /didn’t finish/);
  }
});
test('F3: a run that failed after it was accepted (INTERNAL, RESTART) is still drawn with Ask again', async () => {
  for (const code of ['INTERNAL', 'RESTART', 'TIMEOUT']) {
    const t = await openReload(turnAs('answered', 't1'), turnAs('failed', 't2', { question: FOLLOW_Q, error_code: code }));
    assertOpenThread(t, ['t1', 't2'], ['t2']);
  }
});
// F4 (ARF3-02): a never-accepted send is never drawn, also when a later answer
// exists: it is left out of the adopted turns, so it has no "The answer didn't
// finish" and no Ask again. A turn that failed after it was accepted is still
// drawn with Ask again (F1).
test('F4: an open reload of [cited, NOT_STARTED, cited] draws only the two answers; Ask again on the NOT_STARTED turn sends nothing', async () => {
  const t = await openReload(turnAs('answered', 't1'), turnAs('failed', 't2', { question: FOLLOW_Q, error_code: 'NOT_STARTED' }), turnAs('answered', 't3'));
  assertOpenThread(t, ['t1', 't3'], []);
  const shown = t.main.shown();
  assert.ok(!shown.includes(FOLLOW_Q), "the never-accepted send's question is not drawn");
  assert.doesNotMatch(shown, /didn’t finish/);
  const posts = askPosts(t);
  await t.win.EWViews.askAgain('t2');
  await drain(40);
  assert.deepEqual(posts, [], 'no POST for a never-accepted send');
  noAskPost(t);
});
test('F4: an open reload of [cited, NOT_STARTED, cited, failed after it was accepted] draws t1, t3 and t4, Ask again on t4 only', async () => {
  const t = await openReload(turnAs('answered', 't1'), turnAs('failed', 't2', { error_code: 'NOT_STARTED' }), turnAs('answered', 't3'),
    turnAs('failed', 't4', { question: FOLLOW_Q, error_code: 'INTERNAL' }));
  assertOpenThread(t, ['t1', 't3', 't4'], ['t4']);
  assert.equal((t.main.shown().match(/didn’t finish/g) || []).length, 1, 'only the accepted failed turn');
});
// F5 (ARF4-01): the open path decides on the turns without never-accepted
// sends, so [cited, NOT_STARTED, a follow-up that failed after it was accepted
// or was cancelled] draws the answer and the stopped follow-up with Ask again,
// as F1 does without the middle turn. A never-accepted newest turn still keeps
// the empty page (F3).
const NS_Q = 'A send that was never accepted?';
async function assertF5Drawn(stopped) {
  const t = await openReload(turnAs('answered', 't1'), turnAs('failed', 't2', { question: NS_Q, error_code: 'NOT_STARTED' }), stopped);
  assertOpenThread(t, ['t1', 't3'], ['t3']);
  const shown = t.main.shown();
  assert.ok(!shown.includes(NS_Q), `${stopped.state}: the never-accepted send's question is not drawn`);
  assert.ok(shown.includes(FOLLOW_Q), `${stopped.state}: the stopped follow-up's question`);
  assert.equal((shown.match(/didn’t finish/g) || []).length, 1, `${stopped.state}: only the stopped follow-up`);
  const posts = askPosts(t);
  await t.win.EWViews.askAgain('t2');
  await drain(40);
  assert.deepEqual(posts, [], `${stopped.state}: no POST for the never-accepted send`);
  await t.win.EWViews.askAgain('t3');
  await drain(40);
  assert.deepEqual(posts, [{ question: FOLLOW_Q, thread_id: 'h1' }], `${stopped.state}: one POST, in its thread`);
}
test('F5: an open reload of [cited, NOT_STARTED, failed after it was accepted] draws t1 and t3, Ask again on t3 sends one POST', async () => {
  await assertF5Drawn(turnAs('failed', 't3', { question: FOLLOW_Q, error_code: 'INTERNAL' }));
});
test('F5: an open reload of [cited, NOT_STARTED, cancelled] draws t1 and t3, Ask again on t3 sends one POST', async () => {
  await assertF5Drawn(turnAs('cancelled', 't3', { question: FOLLOW_Q }));
});
test('F5: a never-accepted newest turn still keeps the empty page: [cited, failed after it was accepted, NOT_STARTED], [cited, NOT_STARTED, cancelled, NOT_STARTED]', async () => {
  for (const turns of [[turnAs('answered', 't1'), turnAs('failed', 't2', { error_code: 'INTERNAL' }), turnAs('failed', 't3', { error_code: 'NOT_STARTED' })],
    [turnAs('answered', 't1'), turnAs('failed', 't2', { error_code: 'NOT_STARTED' }), turnAs('cancelled', 't3'), turnAs('failed', 't4', { error_code: 'NOT_STARTED' })]]) {
    const t = await openReload(...turns);
    assertOpenEmpty(t, turns.map(x => x.state + (x.error_code ? ':' + x.error_code : '')).join(','));
    assert.doesNotMatch(t.main.shown(), /didn’t finish/);
  }
});
test('F3: the paused page is unchanged for [a cited answer, NOT_STARTED]: only the answer, read-only', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(turnAs('answered', 't1'), turnAs('failed', 't2', { error_code: 'NOT_STARTED' })) });
  assertRestored(t, ['t1']);
  assert.doesNotMatch(t.main.shown(), /didn’t finish/);
  noAskPost(t);
});
// Pinned as before F1: a working newest turn is not adopted on the open page.
test('F1: an open reload of [a cited answer, a working turn] keeps the empty page, as before (pinned)', async () => {
  for (const turns of [[turnAs('answered', 't1'), turnAs('working', 't2')], [turnAs('answered', 't1'), turnAs('answered', 't2', { state: 'working', step: 2 })],
    [turnAs('answered', 't1'), turnAs('failed', 't2'), turnAs('working', 't3')], [turnAs('answered', 't1'), turnAs('working', 't2'), turnAs('cancelled', 't3')]]) {
    const t = await openReload(...turns);
    assertOpenEmpty(t, turns.map(x => x.state).join(','));
    assert.doesNotMatch(t.main.shown(), /ask-steps|data-action="ask-cancel"/);
    assert.ok(!t.srv.calls.some(c => c.startsWith('GET /customer/ask/turns/')), 'no poll');
  }
});
// The paused page keeps its own rule (FIX3, FIX4): the same threads draw only
// the answer, read-only, under the paused line.
test('F1: the paused page is unchanged for the same threads: only the answer, read-only', async () => {
  for (const turns of [[turnAs('answered', 't1'), turnAs('cancelled', 't2')], [turnAs('answered', 't1'), turnAs('failed', 't2')],
    [turnAs('answered', 't1'), turnAs('working', 't2')]]) {
    const t = await page('#/ask', { session: ASK_PAUSED, threads: thread1(...turns) });
    assertRestored(t, ['t1']);
    assert.doesNotMatch(t.main.shown(), /didn’t finish|ask-steps/);
    noAskPost(t);
  }
});

// -- part 2: askPage alone, one guard at a time ---------------------------------
function loadViews() {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: '#/overview' },
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
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  const win = sandbox.window;
  win.EW.current.route = '#/overview';
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const TOKEN = 8;
async function rig() {
  const win = loadViews();
  await new Promise(res => setTimeout(res, 0));
  const E = win.EW;
  E.current.askAvailable = false;
  E.current.askPaused = true;
  E.current.route = '#/ask';
  E.current.fetchToken = TOKEN;
  const timers = [];
  win.setTimeout = (fn, ms) => { const t = { fn, ms, fired: false }; timers.push(t); return t; };
  win.clearTimeout = () => {};
  const appended = [];
  const askPage = { insertAdjacentHTML: (pos, h) => appended.push(h) };
  const main = { querySelector: sel => (sel === '.ask-page' ? askPage : null) };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.getElementById = () => null;
  const calls = [];
  const r = { win, E, V: win.EWViews, appended, calls, routes: {} };
  E.api = (p, opts) => {
    const method = (opts && opts.method) || 'GET';
    calls.push({ p, method, signal: opts && opts.signal });
    const got = r.routes[method + ' ' + p];
    if (got === undefined) return Promise.reject(new Error('unexpected ' + method + ' ' + p));
    if (got && got.promise) return got.promise;
    return Promise.resolve(clone(got));
  };
  r.open = async () => {
    const html = await r.V.askPage(TOKEN);
    for (const t of timers.filter(t => t.ms === 0 && !t.fired)) { t.fired = true; t.fn(); }
    await new Promise(res => setImmediate(res));
    return html;
  };
  return r;
}
const flush = () => new Promise(res => setImmediate(res));

test('askPage paused: the page HTML is today\'s paused page; the thread is appended to it after the reads', async () => {
  const r = await rig();
  r.routes['GET /customer/ask/threads'] = { thread_id: 'h1' };
  r.routes['GET /customer/ask/threads/h1'] = { thread_id: 'h1', turns: [turnAs('answered', 't1')] };
  const html = await r.open();
  assert.equal(html, '<div class="ask-page"><header class="ask-head"><h1>Ask what the shows in this collection are saying</h1><p class="ask-lede">Every claim links to the passage behind it, with a clip where we have one. If the episodes don\'t support something, we say so.</p></header><p class="ask-off" role="status">Ask is paused right now.</p></div>');
  await flush();
  assert.equal(r.appended.length, 1);
  assert.ok(r.appended[0].includes('id="ask-turn-t1"'));
  assertReadOnly(r.appended[0]);
  assert.ok(r.calls.every(c => c.signal), 'page reads carry the page signal');
});

test('askPage paused with turns already in this tab or a question sending: no read (today\'s paused page)', async () => {
  const r = await rig();
  r.win.EW.current.askAvailable = true;
  r.routes['POST /customer/ask'] = { turn_id: 't9', thread_id: 'h9' };
  const box = { value: 'How do hosts pitch VPNs?' }, slot = { textContent: '', hidden: true };
  await r.V.askSubmit({ dataset: { ask: 'new' }, querySelector: s => (s === '[name=question]' ? box : s === '.ask-refusal' ? slot : null) });
  assert.ok(r.calls.some(c => c.method === 'POST'), 'fixture: a turn in this tab');
  r.E.current.askAvailable = false;
  await r.open();
  assert.ok(!r.calls.some(c => c.p === '/customer/ask/threads'));
  assert.equal(r.appended.length, 0);
});

const leaves = {
  'the member left #/ask': r => { r.E.current.route = '#/overview'; r.E.current.fetchToken += 1; r.E.current.authorityEpoch += 1; },
  'a later render took the page': r => { r.E.current.fetchToken += 1; },
  'the route alone changed': r => { r.E.current.route = '#/overview'; },
  'the authority epoch changed (another member, a downgrade, a workspace)': r => { r.E.current.authorityEpoch += 1; },
  'the Ask store was cleared (sign-out)': r => { r.E.clearRetainedStores(); },
};
for (const [name, leave] of Object.entries(leaves)) {
  for (const which of ['latest', 'thread']) {
    test(`paused: a ${which} read that lands after ${name} draws nothing`, async () => {
      const r = await rig();
      const latest = defer(), thread = defer();
      r.routes['GET /customer/ask/threads'] = which === 'latest' ? latest : { thread_id: 'h1' };
      r.routes['GET /customer/ask/threads/h1'] = which === 'thread' ? thread : { thread_id: 'h1', turns: [turnAs('answered', 't1')] };
      await r.open();
      leave(r);
      latest.resolve({ thread_id: 'h1' });
      thread.resolve({ thread_id: 'h1', turns: [turnAs('answered', 't1')] });
      await flush(); await flush();
      assert.equal(r.appended.length, 0);
    });
  }
}

// -- FIX1, part 2: turns already in this tab, and the off line ------------------
const PAUSED_HTML = '<div class="ask-page"><header class="ask-head"><h1>Ask what the shows in this collection are saying</h1><p class="ask-lede">Every claim links to the passage behind it, with a clip where we have one. If the episodes don\'t support something, we say so.</p></header><p class="ask-off" role="status">Ask is paused right now.</p>';
// Ask open: each turn is sent from this tab and polled to its final state.
async function sentInTab(r, finals) {
  r.E.current.askAvailable = true;
  for (const [i, fin] of finals.entries()) {
    r.routes['POST /customer/ask'] = { turn_id: fin.turn_id, thread_id: fin.thread_id };
    r.routes['GET /customer/ask/turns/' + fin.turn_id] = fin;
    const box = { value: fin.question }, slot = { textContent: '', hidden: true };
    await r.V.askSubmit({ dataset: { ask: i ? 'follow' : 'new' }, querySelector: s => (s === '[name=question]' ? box : s === '.ask-refusal' ? slot : null) });
    await r.poll();
  }
  r.E.current.askAvailable = false;
  r.calls.length = 0;
}
async function rigFix1() {
  const r = await rig();
  const fire = r.win.setTimeout;
  const timers = [];
  r.win.setTimeout = (fn, ms) => { const t = fire(fn, ms); timers.push(t); return t; };
  r.poll = async () => {
    for (const t of timers.filter(t => t.ms > 0 && !t.fired)) { t.fired = true; t.fn(); }
    await flush(); await flush();
  };
  return r;
}

test('FIX1 askPage paused with turns in this tab: answered, partial and nothing drawn read-only under the paused line in the page HTML; failed and cancelled left out; no read', async () => {
  const r = await rigFix1();
  await sentInTab(r, [turnAs('answered', 't1'), turnAs('failed', 't2'), turnAs('nothing', 't3'), turnAs('cancelled', 't4'), turnAs('partial', 't5')]);
  const html = await r.open();
  await flush();
  assert.ok(html.startsWith(PAUSED_HTML + '<div class="ask-thread">'), 'the thread right under the paused line');
  for (const id of ['t1', 't3', 't5']) assert.ok(html.includes(`id="ask-turn-${id}"`), id);
  for (const id of ['t2', 't4']) assert.ok(!html.includes(`id="ask-turn-${id}"`), id + ' drawn');
  assert.equal((html.match(/id="ask-turn-/g) || []).length, 3);
  assert.doesNotMatch(html, /id="ask-thread"/, 'not the live thread the poll repaints');
  assertReadOnly(html);
  assert.match(html, /class="report-problem ask-report"/, 'the report control stays (TRC-01)');
  assert.match(html, /data-action="clip-toggle"/, 'the clip control stays');
  assert.deepEqual(r.calls, [], 'no Ask read or send');
  assert.equal(r.appended.length, 0);
});

// ARC-R1-02 (mutant R1: askPausedThread keeps hidden turns): a turn hidden
// after the answer (a rights change mid-turn) leaves the answer drawn.
test('FIX2 askPage paused with turns in this tab [a cited answer, a hidden turn]: the answer is drawn, never "no longer available"', async () => {
  const r = await rigFix1();
  await sentInTab(r, [turnAs('answered', 't1'), turnAs('hidden', 't2')]);
  const html = await r.open();
  await flush();
  assert.ok(html.startsWith(PAUSED_HTML + '<div class="ask-thread">'), 'the answer under the paused line');
  assert.ok(html.includes('id="ask-turn-t1"'));
  assert.equal((html.match(/id="ask-turn-/g) || []).length, 1, 'only the answer');
  assert.doesNotMatch(html, /no longer available/);
  assertReadOnly(html);
  assert.deepEqual(r.calls, []);
});

test('FIX1 askPage paused with turns in this tab: the newest uncited or nothing: today\'s paused page, nothing read', async () => {
  for (const newest of [turnAs('answered', 't2', { sources: [] }), turnAs('partial', 't2', { sources: [] }), turnAs('nothing', 't2'),
    turnAs('answered', 't2', { state: 'nothing' })]) {
    const r = await rigFix1();
    await sentInTab(r, [turnAs('answered', 't1'), newest]);
    const html = await r.open();
    await flush();
    assert.equal(html, PAUSED_HTML + '</div>', `${newest.state} with ${(newest.sources || []).length} sources drew a thread`);
    assert.deepEqual(r.calls, []);
    assert.equal(r.appended.length, 0);
  }
});

// FIX3: a failed turn after the answer in this tab no longer hides it.
test('FIX3 askPage paused with turns in this tab [a cited answer, a failed turn]: the answer is drawn read-only, the failed turn left out', async () => {
  for (const newest of [turnAs('failed', 't2'), turnAs('answered', 't2', { state: 'failed' })]) {
    const r = await rigFix1();
    await sentInTab(r, [turnAs('answered', 't1'), newest]);
    const html = await r.open();
    await flush();
    assert.ok(html.startsWith(PAUSED_HTML + '<div class="ask-thread">'), 'the answer under the paused line');
    assert.ok(html.includes('id="ask-turn-t1"'));
    assert.equal((html.match(/id="ask-turn-/g) || []).length, 1, 'only the answer');
    assert.doesNotMatch(html, /didn’t finish|data-action="ask-again"/);
    assertReadOnly(html);
    assert.deepEqual(r.calls, []);
  }
});
test('FIX3 askPage paused, the thread read [a cited answer, a failed turn]: the answer is appended read-only', async () => {
  const r = await rig();
  r.routes['GET /customer/ask/threads'] = { thread_id: 'h1' };
  r.routes['GET /customer/ask/threads/h1'] = { thread_id: 'h1', turns: [turnAs('answered', 't1'), turnAs('failed', 't2', { error_code: 'ASK_PAUSED' })] };
  await r.open();
  await flush();
  assert.equal(r.appended.length, 1);
  assert.ok(r.appended[0].includes('id="ask-turn-t1"'));
  assert.ok(!r.appended[0].includes('id="ask-turn-t2"'));
  assertReadOnly(r.appended[0]);
});
// FIX4 (b): a cancelled turn after the answer no longer hides it on the paused page.
test('FIX4 askPage paused with turns in this tab [a cited answer, a cancelled turn]: the answer is drawn read-only, the cancelled turn left out', async () => {
  for (const newest of [turnAs('cancelled', 't2'), turnAs('answered', 't2', { state: 'cancelled' })]) {
    const r = await rigFix1();
    await sentInTab(r, [turnAs('answered', 't1'), newest]);
    const html = await r.open();
    await flush();
    assert.ok(html.startsWith(PAUSED_HTML + '<div class="ask-thread">'), 'the answer under the paused line');
    assert.ok(html.includes('id="ask-turn-t1"'));
    assert.equal((html.match(/id="ask-turn-/g) || []).length, 1, 'only the answer');
    assert.doesNotMatch(html, /didn’t finish|data-action="ask-again"/);
    assertReadOnly(html);
    assert.deepEqual(r.calls, []);
  }
});
test('FIX4 askPage paused, the thread read [a cited answer, a cancelled turn]: the answer is appended read-only', async () => {
  const r = await rig();
  r.routes['GET /customer/ask/threads'] = { thread_id: 'h1' };
  r.routes['GET /customer/ask/threads/h1'] = { thread_id: 'h1', turns: [turnAs('answered', 't1'), turnAs('cancelled', 't2')] };
  await r.open();
  await flush();
  assert.equal(r.appended.length, 1);
  assert.ok(r.appended[0].includes('id="ask-turn-t1"'));
  assert.ok(!r.appended[0].includes('id="ask-turn-t2"'));
  assertReadOnly(r.appended[0]);
});

test('FIX1 askPage paused, turns in this tab, then the Ask store clears (a role, person or workspace change): nothing of them is drawn', async () => {
  const r = await rigFix1();
  await sentInTab(r, [turnAs('answered', 't1')]);
  r.E.clearRetainedStores();
  r.routes['GET /customer/ask/threads'] = { thread_id: null };
  const html = await r.open();
  await flush();
  assert.equal(html, PAUSED_HTML + '</div>');
  assert.ok(!r.appended.join('').includes('ask-turn-t1'));
});

const OFF_HTML = '<div class="ask-page">' + OFF_HEAD + OFF_P + '</div>';
test('FIX1 askPage switched off: its own off line, exactly; no Ask request; turns in this tab are not drawn', async () => {
  const r = await rigFix1();
  await sentInTab(r, [turnAs('answered', 't1')]);
  r.E.current.askPaused = false;
  const html = await r.open();
  await flush(); await flush();
  assert.equal(html, OFF_HTML);
  assert.deepEqual(r.calls, [], 'an Ask request with the switch off');
  assert.equal(r.appended.length, 0);
});

test('FIX1: the paused and off states draw different elements and words', async () => {
  const r = await rigFix1();
  const paused = await r.open();
  r.E.current.askPaused = false;
  const off = await r.open();
  assert.ok(paused.startsWith(PAUSED_HTML), 'the paused page keeps today\'s heading, lede and line');
  assert.match(off, /<p class="ask-off ask-unavailable" role="status">/);
  assert.ok(!paused.includes('ask-unavailable') && !paused.includes(OFF_LINE));
  assert.ok(!off.includes('Ask is paused right now.'));
});

test('FIX1: askRefusalText: ASK_UNAVAILABLE is the off line, ASK_PAUSED the paused line, exactly', async () => {
  const r = await rigFix1();
  assert.equal(r.V.askRefusalText('ASK_UNAVAILABLE'), OFF_LINE);
  assert.equal(r.V.askRefusalText('ASK_PAUSED'), 'Ask is paused right now.');
});

test('FIX1: a send refused with ASK_UNAVAILABLE shows the off line; with ASK_PAUSED the paused line', async () => {
  for (const [code, line] of [['ASK_UNAVAILABLE', OFF_LINE], ['ASK_PAUSED', 'Ask is paused right now.']]) {
    const r = await rigFix1();
    r.E.current.askAvailable = true;
    const err = Object.assign(new Error('refused'), { status: 503, body: { error: { code } } });
    r.routes['POST /customer/ask'] = { promise: Promise.reject(err) };
    r.routes['POST /customer/ask'].promise.catch(() => {});
    const box = { value: 'How do hosts pitch VPNs?' }, slot = { textContent: '', hidden: true };
    await r.V.askSubmit({ dataset: { ask: 'new' }, querySelector: s => (s === '[name=question]' ? box : s === '.ask-refusal' ? slot : null) });
    assert.equal(slot.textContent, line, code);
  }
});

test('FIX1: the home Ask card at the spend stop keeps the paused line; switched off it draws nothing, as today', async () => {
  const r = await rigFix1();
  assert.equal(r.V.askOverviewCard(), '<section class="ew-card ask-card"><h2>Ask what the shows are saying</h2><p class="ask-off">Ask is paused right now.</p></section>');
  r.E.current.askPaused = false;
  assert.equal(r.V.askOverviewCard(), '');
});
