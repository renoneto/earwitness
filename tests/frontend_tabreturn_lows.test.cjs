/* TABRETURN-LOWS (lead LEDGER-LEAD3-NOGAP-1): two LOWs after RENO-R1-01.
   RR3-01: the session ends between Settings' preferences read (200) and
   its own session read (401). The page stayed on "Checking access…" with
   the signed-in nav until the next click or tab return. Now a render
   follows, so the sign-in page shows: no loop, never "<Page> is
   unavailable", typed entries kept as signedOutBoundary keeps them.
   APV-R1-01: a tab return whose session read changes what the page shows
   (ask_available / ask_paused) left the old state until the next in-app
   navigation. Now the current page is drawn again once, keeping scroll
   and typed entries; when nothing changed, nothing is drawn again.
   Fixtures copied from tests/frontend_signin_return.test.cjs: only fetch,
   DOM, timers and location nodes are simulated; the real EW.api,
   apiCall, boot, render and the tab-return gate run.
   Run: node --test tests/frontend_tabreturn_lows.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

const OWNER = 'oidc:https://i.example:owner';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
const SIGNIN_REQUIRED = { error: { code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required', status: 401, details: {} },
  signin: { available: true, reason: '', login_url: null, methods: ['password'] } };
const ASK_PAUSED = { ask_available: false, ask_paused: true };
const ASK_OPEN = { ask_available: true, ask_paused: false };
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };

class FakeFormData {
  constructor(form) { this.form = form; }
  all() { return ((this.form && this.form.elements) || []).filter(el => el.name).map(el => [el.name, String(el.value ?? '')]); }
  get(k) { const e = this.all().find(([n]) => n === k); return e ? e[1] : null; }
  entries() { return this.all()[Symbol.iterator](); }
}
function loadPages(route, fetch) {
  const listeners = {};
  const docListeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, FormData: FakeFormData, location: { hash: route, pathname: '/app/' },
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
// signedOut: every request answers 401 SIGNIN_REQUIRED, as the hosted
// server does. revokeOnPrefs: the session ends right after the
// preferences read answers (a logout from a second client between
// Settings' two reads).
function server() {
  // ctx: the workspace_context (the server changes it with every role
  // change). onSession: runs once as a session read goes out. ask: the
  // Ask POST (a fake transport: no model, no spend) and its turn reads.
  const state = { calls: [], signedOut: false, revokeOnPrefs: false, session: {}, ctx: 'ctx-owner', onSession: null,
    askPost: null, turn: null };
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  state.reply = reply;
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    state.calls.push(`${init.method || 'GET'} ${p}`);
    if (p === '/session' && state.onSession) { const f = state.onSession; state.onSession = null; f(); }
    if (state.signedOut) return reply(401, SIGNIN_REQUIRED);
    if (p === '/session') return reply(200, JSON.parse(JSON.stringify({ ...HOSTED, workspace_context: state.ctx, ...state.session })));
    if (p === '/customer/ask' && init.method === 'POST') return state.askPost ? state.askPost() : reply(503, { error: { code: 'ASK_PAUSED', message: 'paused', status: 503, details: {} } });
    if (p.startsWith('/customer/ask/turns/') && state.turn) return reply(200, state.turn);
    if (p.startsWith('/customer/campaigns')) return reply(200, { campaigns: [], workspace_context: state.ctx });
    if (p.startsWith('/customer/preferences')) {
      if (state.revokeOnPrefs) { state.revokeOnPrefs = false; state.signedOut = true; }
      return reply(200, { preferences: {}, workspace_context: state.ctx });
    }
    if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return reply(200, { alerts: [], workspace_context: state.ctx });
    if (p === '/customer/brands') return reply(200, { brands: [], workspace_context: state.ctx });
    if (p.startsWith('/customer/shows')) return reply(200, { shows: [], workspace_context: state.ctx });
    if (p.startsWith('/customer/saved-items')) return reply(200, { items: [], workspace_context: state.ctx });
    if (p.startsWith('/workspaces/')) return reply(200, { members: [], invites: [], workspace_context: state.ctx });
    return reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
  };
  return state;
}
function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false, ...props };
}
const matching = (forms, sel) => (/form/.test(sel) ? forms.filter(f => sel.split(',').some(part => {
  const want = part.match(/data-([a-z]+)="1"/);
  return !want || (f.dataset && f.dataset[want[1]] === '1');
})) : []);
function modalStub() {
  let html = '';
  const m = node({ open: false, forms: [], showModal() { this.open = true; }, close() { this.open = false; } });
  Object.defineProperty(m, 'innerHTML', { get: () => html, set: v => { html = v; m.forms = []; } });
  m.querySelectorAll = sel => matching(m.forms, sel);
  m.querySelector = sel => (sel === 'button' ? { focus() {} } : null);
  return m;
}
// A page form with one typed text field (changed from its rendered value).
function typedForm(t, id, text) {
  const field = { name: 'name', type: 'text', value: text, defaultValue: '', dataset: {} };
  const form = node({ id, dataset: { dirty: '1' }, elements: [field], getAttribute: k => (k === 'id' ? id : null) });
  t.main.forms.push(form);
  return form;
}
// #main that paints like the browser and remembers every paint.
function mainStub() {
  let html = '';
  const main = node({ inserted: [], painted: [] });
  main.forms = [];
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.inserted = []; main.forms = []; main.painted.push(v); } });
  main.insertAdjacentHTML = (pos, h) => { const b = { html: h, remove() { main.inserted = main.inserted.filter(x => x !== b); } }; main.inserted.unshift(b); main.painted.push(h); };
  main.shown = () => html + main.inserted.map(b => b.html).join('');
  return main;
}
async function page(route, { session = {}, ctx = null } = {}) {
  const srv = server();
  srv.session = session;
  if (ctx) srv.ctx = ctx;
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = mainStub();
  const modal = modalStub();
  const toastEl = node();
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#toast' ? toastEl : node());
  win.document.querySelectorAll = sel => matching([...(modal.open ? modal.forms : []), ...main.forms], sel);
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  E.toast = () => {};
  win.location.hash = route;
  E.current.route = route;
  await win.EWViews.render();
  await drain(40);
  return { srv, win, E, main, modal };
}
const fireWindow = (t, type, event = {}) => { for (const fn of t.win.listeners[type] || []) fn({ type, ...event }); };
const fireDocument = (t, type, event = {}) => { for (const fn of t.win.docListeners[type] || []) fn({ type, ...event }); };
async function leaveAndReturn(t, how) {
  if (how === 'blur') {
    fireWindow(t, 'blur');
    fireWindow(t, 'focus');
  } else {
    t.win.document.visibilityState = 'hidden';
    fireDocument(t, 'visibilitychange');
    t.win.document.visibilityState = 'visible';
    fireDocument(t, 'visibilitychange');
  }
  await drain(80);
}
async function nav(t, route) {
  t.win.location.hash = route;
  t.E.current.route = route;
  await t.win.EWViews.render();
  await drain(120);
}
const SIGNIN_FORM = /id="signin-email"/;
const UNAVAILABLE = /unavailable: browser sign-in is required/;
const CHECKING = /Checking access…/;
const PAUSED_LINE = /Ask is paused right now\./;
const everPainted = (t, re) => t.main.painted.some(h => re.test(h)) || re.test(t.main.shown());
const sessionReads = (t, from) => t.srv.calls.slice(from).filter(c => c === 'GET /session').length;

// RR3-01
test('RR3-01: the session ends between Settings\' preferences read and its own session read: the sign-in page follows, with no click or tab return', async () => {
  const t = await page('#/alerts');
  assert.equal(t.E.current.principal, OWNER, 'fixture: signed in');
  t.srv.revokeOnPrefs = true;
  const reads = t.srv.calls.length;
  await nav(t, '#/settings');
  const calls = t.srv.calls.slice(reads);
  assert.ok(calls.indexOf('GET /customer/preferences') >= 0 && calls.indexOf('GET /customer/preferences') < calls.indexOf('GET /session'),
    'fixture: preferences answered, then Settings\' own session read');
  assert.match(t.main.shown(), SIGNIN_FORM, 'the page stayed on "Checking access…" instead of the sign-in page');
  assert.doesNotMatch(t.main.shown(), CHECKING);
  assert.equal(t.E.current.principal, null, 'the signed-out session was not adopted');
  assert.equal(everPainted(t, UNAVAILABLE), false, 'Settings painted "Settings are unavailable: browser sign-in is required"');
});

test('RR3-01: no loop: the sign-in page settles after a bounded number of session reads, and nothing more while idle', async () => {
  const t = await page('#/alerts');
  t.srv.revokeOnPrefs = true;
  const reads = t.srv.calls.length;
  await nav(t, '#/settings');
  const settled = t.srv.calls.length;
  const paints = t.main.painted.length;
  assert.ok(sessionReads(t, reads) <= 3, `session reads after the race: ${sessionReads(t, reads)}`);
  // The signed-out session is adopted before anything else is read.
  const after = t.srv.calls.slice(reads);
  assert.deepEqual(after.slice(after.indexOf('GET /session')).filter(c => c !== 'GET /session'), [], 'a page read went out after Settings\' session read found no one signed in');
  await new Promise(res => setTimeout(res, 60));
  await drain(200);
  assert.equal(t.srv.calls.length, settled, 'reads went on while idle');
  assert.equal(t.main.painted.length, paints, 'the page kept painting while idle');
  assert.match(t.main.shown(), SIGNIN_FORM);
});

test('RR3-01: typed entries on Settings are kept as signedOutBoundary keeps them (stashed, announced when the same person returns)', async () => {
  const t = await page('#/settings');
  assert.equal(t.E.current.publishedRoute, '#/settings', 'fixture: Settings published');
  typedForm(t, 'settings-workspace', 'MY-TYPED-WORKSPACE-NAME');
  t.srv.revokeOnPrefs = true;
  // Settings drawn again in place (Retry): its reads run again.
  await t.win.EWViews.render();
  await drain(160);
  assert.match(t.main.shown(), SIGNIN_FORM, 'fixture: the sign-in page follows');
  t.srv.signedOut = false;
  await t.E.boot();
  await drain(20);
  await nav(t, '#/settings');
  assert.match(t.main.painted.join(' ').replace(/<[^>]+>/g, ' '), /Your unsaved entry/, 'the typed Settings entry was not stashed at the sign-out');
});

// APV-R1-01
for (const how of ['blur', 'hidden']) {
  test(`APV-R1-01 (${how}): Ask paused, the ledger reopens, tab return: #/ask shows Ask again with no navigation`, async () => {
    const t = await page('#/ask', { session: ASK_PAUSED });
    assert.match(t.main.shown(), PAUSED_LINE, 'fixture: paused');
    t.srv.session = ASK_OPEN;
    const reads = t.srv.calls.length;
    await leaveAndReturn(t, how);
    assert.equal(sessionReads(t, reads), 1, 'one session read per return');
    assert.equal(t.E.current.askAvailable, true, 'the reopened session was not adopted');
    assert.doesNotMatch(t.main.shown(), PAUSED_LINE, 'the paused line stayed after the return');
  });
}

test('APV-R1-01: Ask open, the spend stop, tab return: #/ask shows the paused line with no navigation', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  assert.equal(t.E.current.askAvailable, true, 'fixture: open');
  assert.doesNotMatch(t.main.shown(), PAUSED_LINE, 'fixture: open');
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.askPaused, true, 'the paused session was not adopted');
  assert.match(t.main.shown(), PAUSED_LINE, 'the open page stayed after the return');
});

test('APV-R1-01: a changed session draws the page again once, keeping its scroll and typed entries', async () => {
  const t = await page('#/alerts', { session: ASK_PAUSED });
  typedForm(t, 'page-note', 'MY-TYPED-PAGE-NOTE');
  const fresh = { name: 'name', type: 'text', value: '', defaultValue: '', dataset: {} };
  const freshForm = node({ id: 'page-note', elements: [fresh], getAttribute: k => (k === 'id' ? 'page-note' : null) });
  t.main.querySelector = sel => (sel === '#page-note' ? freshForm : null);
  const scrolls = [];
  t.win.scrollY = 640;
  t.win.scrollTo = (x, y) => { scrolls.push(y); };
  const renders = t.E.current.fetchToken;
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'blur');
  await drain(80);
  assert.equal(t.E.current.askAvailable, true, 'fixture: the reopened session');
  assert.equal(t.E.current.fetchToken - renders, 1, 'the page was not drawn again exactly once');
  assert.equal(scrolls[scrolls.length - 1], 640, 'the scroll position was lost');
  assert.equal(fresh.value, 'MY-TYPED-PAGE-NOTE', 'the typed entry was lost');
});

test('APV-R1-01: nothing changed, tab return: nothing is drawn again (one session read, no paint)', async () => {
  for (const session of [ASK_PAUSED, ASK_OPEN, {}]) {
    for (const route of ['#/ask', '#/alerts']) {
      const t = await page(route, { session });
      const paints = t.main.painted.length;
      const reads = t.srv.calls.length;
      await leaveAndReturn(t, 'blur');
      await leaveAndReturn(t, 'hidden');
      assert.deepEqual(t.srv.calls.slice(reads), ['GET /session', 'GET /session'], `${route} ${JSON.stringify(session)}: reads beyond the session`);
      assert.equal(t.main.painted.length, paints, `${route} ${JSON.stringify(session)}: the page was drawn again`);
    }
  }
});

test('APV-R1-01: one change draws the page again once; the next return with no change draws nothing', async () => {
  const t = await page('#/ask', { session: ASK_PAUSED });
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'blur');
  const paints = t.main.painted.length;
  const reads = t.srv.calls.length;
  await leaveAndReturn(t, 'blur');
  assert.equal(sessionReads(t, reads), 1);
  assert.equal(t.main.painted.length, paints, 'a return with no change drew the page again');
});

test('APV-R1-01: a changed session never draws over a held draft: the held entry and its notice stay', async () => {
  const t = await page('#/alerts', { session: ASK_PAUSED });
  const held = typedForm(t, 'page-note', 'MY-HELD-NOTE');
  held.dataset.held = '1';
  const renders = t.E.current.fetchToken;
  const paints = t.main.painted.length;
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken, renders, 'the page was drawn again over a held draft');
  assert.equal(t.main.painted.length, paints);
  assert.ok(t.main.forms.includes(held), 'the held draft left the page');
});

/* TABRETURN-LOWS-FIX1 (review tabreturn-lows-r1). TR-R1-01: the server
   changes workspace_context with every role change, so a tab return takes
   the role boundary, which drew the page before the new role was adopted.
   TR-R1-02: the session-change redraw of #/ask kept no typed question and
   could hide the person's own turn in flight. TR-R1-03: every compared
   field. TR-R1-06: the follow-up and the redraw's own limits. */
const SEARCH = '#/search?mode=mentions';
const EXPORT = /Export…/;
const SELECT = /name="selected_ref"|id="select-page"/;
const FEATURES = ['invites', 'sharing', 'agent_access', 'ask', 'meaning_alerts', 'brief_check'];
const allOn = () => Object.fromEntries(FEATURES.map(n => [n, true]));

for (const route of ['#/campaigns', '#/alerts', '#/settings', SEARCH]) {
  test(`TR-R1-01: owner -> viewer with a new workspace_context (as the server sends it), one tab return on ${route}: the viewer role is adopted and no editor control shows`, async () => {
    const t = await page(route);
    assert.equal(t.E.current.workspaceRole, 'owner', 'fixture: owner');
    if (route === SEARCH) assert.match(t.main.shown(), EXPORT, 'fixture: Export… shows for the owner');
    const renders = t.E.current.fetchToken;
    const reads = t.srv.calls.length;
    t.srv.session = { role: 'viewer' };
    t.srv.ctx = 'ctx-viewer';
    await leaveAndReturn(t, 'blur');
    await drain(120);
    assert.equal(t.E.current.workspaceRole, 'viewer', 'the editor role stayed after one return');
    assert.ok(t.E.current.fetchToken - renders >= 1, 'the page was not drawn again');
    assert.ok(sessionReads(t, reads) <= 2, `session reads: ${sessionReads(t, reads)}`);
    assert.doesNotMatch(t.main.shown(), EXPORT, 'Export… stayed for a viewer');
    assert.doesNotMatch(t.main.shown(), SELECT, 'select boxes stayed for a viewer');
  });
}

test('TR-R1-01: viewer -> editor with a new workspace_context, one tab return on Search: the editor role is adopted and Export… shows', async () => {
  const t = await page(SEARCH, { session: { role: 'viewer' }, ctx: 'ctx-viewer' });
  assert.equal(t.E.current.workspaceRole, 'viewer', 'fixture: viewer');
  assert.doesNotMatch(t.main.shown(), EXPORT, 'fixture: no Export… for a viewer');
  t.srv.session = { role: 'editor' };
  t.srv.ctx = 'ctx-editor';
  await leaveAndReturn(t, 'blur');
  await drain(120);
  assert.equal(t.E.current.workspaceRole, 'editor', 'the viewer role stayed after one return');
  assert.match(t.main.shown(), EXPORT, 'Export… did not come back for an editor');
});

// TR-R1-03: each field the redraw compares.
test('TR-R1-03: the role alone changes (same workspace_context): one redraw, the role adopted, Export… gone', async () => {
  const t = await page(SEARCH);
  const renders = t.E.current.fetchToken;
  t.srv.session = { role: 'viewer' };
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.workspaceRole, 'viewer');
  assert.equal(t.E.current.fetchToken - renders, 1, 'not drawn again exactly once');
  assert.doesNotMatch(t.main.shown(), EXPORT);
});

for (const name of FEATURES) {
  test(`TR-R1-03: pilot switch ${name} on -> off, tab return: one redraw, adopted; the next return with no change draws nothing`, async () => {
    const t = await page('#/alerts', { session: { features: allOn() } });
    const renders = t.E.current.fetchToken;
    t.srv.session = { features: { ...allOn(), [name]: false } };
    await leaveAndReturn(t, 'blur');
    assert.equal(t.E.current.pilotFeatures[name], false, 'the switch was not adopted');
    assert.equal(t.E.current.fetchToken - renders, 1, 'not drawn again exactly once');
    await leaveAndReturn(t, 'hidden');
    assert.equal(t.E.current.fetchToken - renders, 1, 'a return with no change drew the page again');
  });
}

for (const [from, to] of [[false, true], [true, false]]) {
  test(`TR-R1-03: meaning alerts ${from ? 'on' : 'off'} -> ${to ? 'on' : 'off'}, tab return: one redraw, adopted; the next return draws nothing`, async () => {
    const t = await page('#/alerts', { session: { meaning_alerts_available: from } });
    const renders = t.E.current.fetchToken;
    t.srv.session = { meaning_alerts_available: to };
    await leaveAndReturn(t, 'blur');
    assert.equal(t.E.current.meaningAlertsAvailable, to, 'not adopted');
    assert.equal(t.E.current.fetchToken - renders, 1, 'not drawn again exactly once');
    await leaveAndReturn(t, 'blur');
    assert.equal(t.E.current.fetchToken - renders, 1);
  });
}

// TR-R1-02: the Ask question box (not a draft: markDirty skips it).
function askDom(t, text) {
  const box = { id: 'ask-q', name: 'question', value: text, focus() {} };
  askBox(t, box);
  return box;
}
// The question box on the page (null: no box, as on the paused page).
function askBox(t, box) {
  const plain = t.plainQuery || (t.plainQuery = t.win.document.querySelector);
  t.win.document.querySelector = sel => (sel === '#ask-q' ? box : plain(sel));
  t.win.document.getElementById = id => (id === 'ask-q' ? box : null);
}
const askBoxText = t => {
  const m = t.main.shown().match(/<textarea id="ask-q"[^>]*>([^<]*)<\/textarea>/);
  return m ? m[1] : null;
};

test('TR-R1-02: a typed Ask question is kept across open -> paused -> open by tab returns', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  assert.equal(askBoxText(t), '', 'fixture: an empty box');
  askDom(t, 'MY-TYPED-QUESTION');
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  assert.match(t.main.shown(), PAUSED_LINE, 'fixture: paused');
  // The paused page has no box.
  askBox(t, null);
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'hidden');
  assert.equal(askBoxText(t), 'MY-TYPED-QUESTION', 'the typed question was lost');
  // Drawn once: a later redraw with an empty box shows an empty box.
  askBox(t, { value: '' });
  await nav(t, '#/ask');
  assert.equal(askBoxText(t), '', 'the kept question came back a second time');
});

test('TR-R1-02: a pilot switch changes while Ask stays open: the redraw keeps the typed question', async () => {
  const t = await page('#/ask', { session: { ...ASK_OPEN, features: allOn() } });
  askDom(t, 'MY-TYPED-QUESTION');
  const renders = t.E.current.fetchToken;
  t.srv.session = { ...ASK_OPEN, features: { ...allOn(), sharing: false } };
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'fixture: drawn again once');
  assert.equal(askBoxText(t), 'MY-TYPED-QUESTION', 'the typed question was lost');
});

test('TR-R1-02: a kept question goes with the Ask thread when the retained stores clear (workspace, person, role)', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  askDom(t, 'MY-TYPED-QUESTION');
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  askBox(t, null);
  t.E.forgetRetainedViews();
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'blur');
  assert.equal(askBoxText(t), '', 'the question outlived the cleared stores');
});

test('TR-R1-02: a turn in flight is never hidden by a redraw: no swap to the paused line until it is done', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  const box = askDom(t, 'MY-QUESTION');
  let release = null;
  t.srv.askPost = () => new Promise(res => { release = () => res(t.srv.reply(200, { turn_id: 'T1', thread_id: 'TH1' })); });
  const slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  const sending = t.win.EWViews.askSubmit(form);
  await drain(20);
  assert.ok(release, 'fixture: the send is in flight');
  const renders = t.E.current.fetchToken;
  // 1. The send is in flight; the ledger stops (the held turn counts).
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken, renders, 'drawn again while the send was in flight');
  assert.doesNotMatch(t.main.shown(), PAUSED_LINE);
  release();
  await sending;
  await drain(20);
  // 2. The turn works.
  t.srv.turn = { turn_id: 'T1', thread_id: 'TH1', question: 'MY-QUESTION', state: 'working', step: 1 };
  await leaveAndReturn(t, 'hidden');
  assert.equal(t.E.current.fetchToken, renders, 'drawn again while the turn worked');
  assert.doesNotMatch(t.main.shown(), PAUSED_LINE);
  // 3. The turn is done (the poll reads it): the next return draws the paused line.
  t.srv.turn = { turn_id: 'T1', thread_id: 'TH1', question: 'MY-QUESTION', state: 'failed' };
  await new Promise(res => setTimeout(res, 1700));
  await drain(40);
  assert.ok(t.srv.calls.includes('GET /customer/ask/turns/T1'), 'fixture: the poll read the turn');
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'not drawn again once the turn was done');
  assert.match(t.main.shown(), PAUSED_LINE);
});

// TR-R1-06: reviewer mutants R1, R2, R4.
test('R1: the sign-in or workspace page\'s own session read finding no one signed in draws itself: no follow-up read or render', async () => {
  const t = await page('#/overview', { session: { active_workspace: '', workspaces: ['ws', 'ws2'] }, ctx: '' });
  assert.ok(t.E.current.principal, 'fixture: signed in');
  assert.equal(t.E.current.activeWorkspace, '', 'fixture: the workspace page');
  t.srv.signedOut = true;
  const renders = t.E.current.fetchToken;
  const reads = t.srv.calls.length;
  await nav(t, '#/overview');
  await new Promise(res => setTimeout(res, 60));
  await drain(120);
  assert.match(t.main.shown(), SIGNIN_FORM, 'fixture: the sign-in page');
  assert.equal(sessionReads(t, reads), 1, 'a follow-up read the session again');
  assert.equal(t.E.current.fetchToken - renders, 1, 'a follow-up drew the page again');
});

// Settings' own session read (401), then the follow-up's read: `dest`
// is opened (a nav click) just as that read goes out.
function takeOverAtFollowUp(t, dest) {
  const at = {};
  t.srv.onSession = () => {
    t.srv.onSession = () => {
      t.win.location.hash = dest;
      t.E.current.route = dest;
      at.token = t.E.current.fetchToken;
      at.reads = t.srv.calls.length;
      t.win.EWViews.render();
    };
  };
  return at;
}
test('R2: a page drawn while the follow-up read the session (Ask, no page read) was drawn for the signed-in person: the sign-in page follows, never "Checking access…"', async () => {
  const t = await page('#/alerts', { session: ASK_OPEN });
  t.srv.revokeOnPrefs = true;
  const at = takeOverAtFollowUp(t, '#/ask');
  await nav(t, '#/settings');
  await new Promise(res => setTimeout(res, 60));
  await drain(200);
  assert.ok(at.token !== undefined, 'fixture: the follow-up read went out');
  assert.doesNotMatch(t.main.shown(), CHECKING, 'the page stayed on "Checking access…"');
  assert.match(t.main.shown(), SIGNIN_FORM, 'the sign-in page did not follow');
  assert.equal(t.E.current.fetchToken - at.token, 2, 'Ask, then the sign-in page, once');
  assert.equal(everPainted(t, UNAVAILABLE), false);
});

test('R2: a render still drawing when the follow-up adopts the sign-out draws it itself: one sign-in render, no second', async () => {
  const t = await page('#/alerts');
  t.srv.revokeOnPrefs = true;
  // The Campaigns read is answered only after the follow-up adopted the sign-out.
  let release = null;
  const plain = t.srv.fetch;
  t.win.fetch = async (url, init = {}) => {
    if (url.includes('/customer/campaigns')) {
      t.srv.calls.push('GET /customer/campaigns');
      await new Promise(res => { release = res; });
      return t.srv.reply(401, SIGNIN_REQUIRED);
    }
    return plain(url, init);
  };
  const at = takeOverAtFollowUp(t, '#/campaigns');
  await nav(t, '#/settings');
  assert.ok(release, 'fixture: the Campaigns read is held');
  assert.equal(t.E.current.principal, null, 'fixture: the follow-up adopted the sign-out');
  release();
  await new Promise(res => setTimeout(res, 60));
  await drain(200);
  assert.match(t.main.shown(), SIGNIN_FORM);
  assert.equal(t.E.current.fetchToken - at.token, 2, 'Campaigns, then the sign-in page, once');
  assert.equal(sessionReads(t, at.reads), 1, 'the sign-in page read the session more than once');
});

test('R4: a changed session never redraws while a conflict waits for its notice', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  const renders = t.E.current.fetchToken;
  t.srv.session = ASK_PAUSED;
  const outcome = await t.win.EWViews.reconcileCanonical({ conflict: {} });
  await drain(40);
  assert.equal(outcome, 'unchanged', 'the reconcile drew the page');
  assert.equal(t.E.current.fetchToken, renders, 'drawn again under a waiting conflict');
  assert.match(t.main.shown(), /data-reconcile-notice/, 'the conflict notice is not shown');
});

/* TABRETURN-LOWS-FIX2 (confirm review tabreturn-lows-confirm). TRC-01: a
   tab return during a spend stop swapped a finished answer (its sources
   and Report a mistake) for the paused line. TRC-02: the redraw dropped a
   typed follow-up question. TRC-03: the reviewer's probes C10 and C11. */
const OTHER = 'oidc:https://i.example:other';
const THREAD = /id="ask-turn-T1"/;
const ANSWER_TEXT = /THE-ANSWER-TEXT/;
async function finishedTurn(t, state = 'answered') {
  const box = askDom(t, 'MY-QUESTION');
  t.srv.askPost = () => t.srv.reply(200, { turn_id: 'T1', thread_id: 'TH1' });
  const slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  t.srv.turn = { turn_id: 'T1', thread_id: 'TH1', question: 'MY-QUESTION', state: 'working', step: 1 };
  await t.win.EWViews.askSubmit(form);
  await drain(20);
  t.srv.turn = { turn_id: 'T1', thread_id: 'TH1', question: 'MY-QUESTION', state,
    sections: [{ heading: null, paragraphs: [{ text: 'THE-ANSWER-TEXT', cites: [] }] }], lines: ['THE-ANSWER-TEXT'], sources: [] };
  await new Promise(res => setTimeout(res, 1700));
  await drain(40);
  await nav(t, '#/ask');
  t.srv.askPost = null;
}
// The follow-up box on the page (on top of the question box's stub).
function askFollow(t, value) {
  const fu = { id: 'ask-fu', name: 'question', value, focus() {} };
  const q = t.win.document.querySelector, g = t.win.document.getElementById;
  t.win.document.querySelector = sel => (sel === '#ask-fu' ? fu : q(sel));
  t.win.document.getElementById = id => (id === 'ask-fu' ? fu : g(id));
  return fu;
}
const askFollowText = t => {
  const m = t.main.shown().match(/<input id="ask-fu"[^>]*>/);
  if (!m) return null;
  const v = m[0].match(/ value="([^"]*)"/);
  return v ? v[1] : '';
};

for (const state of ['answered', 'partial', 'nothing']) {
  for (const next of ['#/ask', '#/alerts']) {
    test(`TRC-01: a finished turn (${state}) on #/ask, the spend stop, tab returns: the answer stays; the server refuses a new turn; the paused line after the navigation to ${next}`, async () => {
      const t = await page('#/ask', { session: ASK_OPEN });
      await finishedTurn(t, state);
      assert.match(t.main.shown(), THREAD, 'fixture: the answer on screen');
      const renders = t.E.current.fetchToken;
      const paints = t.main.painted.length;
      t.srv.session = ASK_PAUSED;
      await leaveAndReturn(t, 'blur');
      await leaveAndReturn(t, 'hidden');
      assert.equal(t.E.current.fetchToken, renders, 'the answer was drawn over');
      assert.equal(t.main.painted.length, paints);
      assert.doesNotMatch(t.main.shown(), PAUSED_LINE);
      assert.match(t.main.shown(), THREAD);
      assert.match(t.main.shown(), ANSWER_TEXT);
      if (state !== 'nothing') {
        assert.match(t.main.shown(), /class="ask-sources"/, 'the sources left the page');
        assert.match(t.main.shown(), /class="report-problem ask-report"/, 'Report a mistake left the page');
      }
      // The Ask form stays usable as the server allows: it refuses the turn.
      const box = askDom(t, 'MY-NEXT-QUESTION');
      const slot = node({ hidden: true });
      const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
      const posts = t.srv.calls.filter(c => c === 'POST /customer/ask').length;
      await t.win.EWViews.askSubmit(form);
      await drain(20);
      assert.equal(t.srv.calls.filter(c => c === 'POST /customer/ask').length, posts + 1, 'fixture: the turn was sent');
      assert.match(slot.textContent, PAUSED_LINE, 'the refusal was not shown');
      assert.match(t.main.shown(), THREAD, 'the refusal took the answer away');
      // The next in-app navigation shows the paused state.
      await nav(t, next);
      if (next !== '#/ask') await nav(t, '#/ask');
      assert.match(t.main.shown(), PAUSED_LINE);
      assert.doesNotMatch(t.main.shown(), THREAD);
    });
  }
}

// ARC-R1-03: the key file is refused while an answer is on screen (switch on,
// no ask_paused): the send is refused with ASK_UNAVAILABLE, and its line is the
// one the next #/ask shows, with no answer under it (switched off, not paused).
test('TRC-01: a finished turn on #/ask, the key file refused, tab return: a send says the line the next #/ask shows', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  await finishedTurn(t);
  t.srv.session = { ask_available: false };
  await leaveAndReturn(t, 'blur');
  assert.match(t.main.shown(), THREAD, 'fixture: the answer held on screen');
  t.srv.askPost = () => t.srv.reply(503, { error: { code: 'ASK_UNAVAILABLE', message: 'unavailable', status: 503, details: {} } });
  const box = askDom(t, 'MY-NEXT-QUESTION');
  const slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  await t.win.EWViews.askSubmit(form);
  await drain(20);
  assert.ok(t.srv.calls.includes('POST /customer/ask'), 'fixture: the turn was sent');
  assert.ok(slot.textContent, 'the refusal was not shown');
  await nav(t, '#/ask');
  const line = (t.main.shown().match(/<p class="ask-off[^"]*" role="status">([^<]*)<\/p>/) || [])[1];
  assert.equal(line, slot.textContent, 'the refused send and the page say different lines');
  // ASK-OFF-1: that line is the off line.
  assert.equal(line, 'Ask is off for now. When it is on, Muse Spark, Meta\u2019s AI model, answers your questions from the episodes, with every claim linked to the passage behind it.');
  assert.doesNotMatch(t.main.shown(), THREAD);
});

test('TRC-01: the answer is held only while it is on screen: paused with the thread kept off screen, the ledger reopens, one return draws it again', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  await finishedTurn(t);
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  await nav(t, '#/ask');
  assert.match(t.main.shown(), PAUSED_LINE, 'fixture: paused at the navigation');
  const renders = t.E.current.fetchToken;
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'not drawn again once');
  assert.doesNotMatch(t.main.shown(), PAUSED_LINE);
  assert.match(t.main.shown(), THREAD);
});

test('TRC-01: the paused page with the thread kept off screen: a pilot switch changes while Ask stays paused: drawn again once', async () => {
  const t = await page('#/ask', { session: { ...ASK_OPEN, features: allOn() } });
  await finishedTurn(t);
  t.srv.session = { ...ASK_PAUSED, features: allOn() };
  await leaveAndReturn(t, 'blur');
  await nav(t, '#/ask');
  assert.match(t.main.shown(), PAUSED_LINE, 'fixture: paused at the navigation');
  const renders = t.E.current.fetchToken;
  t.srv.session = { ...ASK_PAUSED, features: { ...allOn(), sharing: false } };
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'the paused page was held for a thread not on screen');
  assert.equal(t.E.current.pilotFeatures.sharing, false);
  assert.match(t.main.shown(), PAUSED_LINE);
});

test('TRC-01: the person left the answer (on #/alerts), the spend stop, tab return: #/alerts is drawn again once with Ask paused', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  await finishedTurn(t);
  await nav(t, '#/alerts');
  const renders = t.E.current.fetchToken;
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'another page was held for an answer in the Ask store');
  assert.equal(t.E.current.askAvailable, false);
  assert.equal(t.E.current.askPaused, true);
});

test('TRC-01: with no answer on screen (a failed turn), the spend stop still draws the paused line at the return', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  await finishedTurn(t, 'failed');
  const renders = t.E.current.fetchToken;
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1);
  assert.match(t.main.shown(), PAUSED_LINE);
});

test('TRC-01: an answer on screen holds only the paused swap: a pilot switch change while Ask stays open draws #/ask again once, with the answer', async () => {
  const t = await page('#/ask', { session: { ...ASK_OPEN, features: allOn() } });
  await finishedTurn(t);
  const renders = t.E.current.fetchToken;
  t.srv.session = { ...ASK_OPEN, features: { ...allOn(), sharing: false } };
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'not drawn again once');
  assert.equal(t.E.current.pilotFeatures.sharing, false);
  assert.match(t.main.shown(), THREAD);
});

test('TRC-02: a typed follow-up question is kept across a redraw while Ask stays open (with the top question), and fills once', async () => {
  const t = await page('#/ask', { session: { ...ASK_OPEN, features: allOn() } });
  await finishedTurn(t);
  assert.ok(/id="ask-followup"/.test(t.main.shown()) && !/id="ask-followup"[^>]* hidden/.test(t.main.shown()), 'fixture: the follow-up form shows');
  assert.equal(askFollowText(t), '', 'fixture: an empty follow-up box');
  askDom(t, 'MY-TYPED-QUESTION');
  askFollow(t, 'MY-FOLLOW-UP "quoted" <b>');
  const renders = t.E.current.fetchToken;
  t.srv.session = { ...ASK_OPEN, features: { ...allOn(), sharing: false } };
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.fetchToken - renders, 1, 'fixture: drawn again once');
  assert.equal(askFollowText(t), 'MY-FOLLOW-UP &quot;quoted&quot; &lt;b&gt;', 'the typed follow-up was lost');
  assert.equal(askBoxText(t), 'MY-TYPED-QUESTION', 'the typed question was lost');
  assert.match(t.main.shown(), THREAD);
  // Drawn once: a later draw with empty boxes shows empty boxes.
  askDom(t, '');
  askFollow(t, '');
  await nav(t, '#/ask');
  assert.equal(askFollowText(t), '', 'the kept follow-up came back a second time');
  assert.equal(askBoxText(t), '');
});

test('TRC-02: a typed follow-up is kept across open -> paused -> open by tab returns', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  askDom(t, '');
  askFollow(t, 'MY-FOLLOW-UP');
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  assert.match(t.main.shown(), PAUSED_LINE, 'fixture: paused');
  askBox(t, null);
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'hidden');
  assert.equal(askFollowText(t), 'MY-FOLLOW-UP', 'the typed follow-up was lost');
});

test('TRC-02: a kept follow-up goes with the Ask thread when the retained stores clear', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  askDom(t, '');
  askFollow(t, 'MY-FOLLOW-UP');
  t.srv.session = ASK_PAUSED;
  await leaveAndReturn(t, 'blur');
  askBox(t, null);
  t.E.forgetRetainedViews();
  t.srv.session = ASK_OPEN;
  await leaveAndReturn(t, 'blur');
  assert.equal(askFollowText(t), '', 'the follow-up outlived the cleared stores');
});

// Kept while paused, then a real change: never painted after it.
const CHANGES = {
  'a role change (new context)': async t => { t.srv.session = { ...ASK_PAUSED, role: 'viewer' }; t.srv.ctx = 'ctx-viewer'; await leaveAndReturn(t, 'blur'); t.srv.session = { ...ASK_OPEN, role: 'viewer' }; await leaveAndReturn(t, 'blur'); },
  'a person change': async t => { t.srv.session = { ...ASK_OPEN, principal: OTHER, email: 'other@example.test' }; t.srv.ctx = 'ctx-other'; await leaveAndReturn(t, 'blur'); await nav(t, '#/ask'); },
  'a workspace change': async t => { t.srv.session = { ...ASK_OPEN, active_workspace: 'ws2', workspaces: ['ws', 'ws2'] }; t.srv.ctx = 'ctx-ws2'; await leaveAndReturn(t, 'blur'); await nav(t, '#/ask'); },
  'a sign-out and sign-in': async t => {
    t.srv.signedOut = true; await leaveAndReturn(t, 'blur');
    assert.match(t.main.shown(), SIGNIN_FORM, 'fixture: signed out');
    t.srv.signedOut = false; t.srv.session = ASK_OPEN; await t.E.boot(); await drain(20); await nav(t, '#/ask');
  },
};
for (const [name, change] of Object.entries(CHANGES)) {
  test(`TRC-02: the kept question and follow-up go at ${name}: never painted after it`, async () => {
    const t = await page('#/ask', { session: ASK_OPEN });
    askDom(t, 'MY-TYPED-QUESTION');
    askFollow(t, 'MY-FOLLOW-UP');
    t.srv.session = ASK_PAUSED;
    await leaveAndReturn(t, 'blur');
    assert.match(t.main.shown(), PAUSED_LINE, 'fixture: paused');
    askBox(t, null);
    const from = t.main.painted.length;
    await change(t);
    assert.doesNotMatch(t.main.shown(), PAUSED_LINE, 'fixture: Ask open after the change');
    assert.equal(t.main.painted.slice(from).some(h => /MY-TYPED-QUESTION|MY-FOLLOW-UP/.test(h)), false, 'a kept question was painted after the change');
    assert.equal(askBoxText(t), '');
    assert.equal(askFollowText(t), '');
  });
}

test('TRC-02: a refilled follow-up is no draft: no leave prompt and no Ask POST, on input or navigation', async () => {
  const t = await page('#/ask', { session: { ...ASK_OPEN, features: allOn() } });
  await finishedTurn(t);
  askFollow(t, 'MY-FOLLOW-UP');
  t.srv.session = { ...ASK_OPEN, features: { ...allOn(), sharing: false } };
  await leaveAndReturn(t, 'blur');
  assert.equal(askFollowText(t), 'MY-FOLLOW-UP', 'fixture: refilled');
  const posts = t.srv.calls.filter(c => c === 'POST /customer/ask').length;
  const form = node({ id: 'ask-followup', dataset: { ask: 'follow' }, classList: { add() {}, remove() {}, toggle() {}, contains: c => c === 'ask-form' }, getAttribute: k => (k === 'id' ? 'ask-followup' : null) });
  const fu = { id: 'ask-fu', name: 'question', value: 'MY-FOLLOW-UP more', dataset: {}, closest: () => form, matches: () => false };
  t.main.forms.push(form);
  for (const fn of t.win.docListeners.input || []) { try { fn({ type: 'input', target: fu }); } catch { /* other listeners need a fuller DOM */ } }
  await nav(t, '#/alerts');
  assert.equal(form.dataset.dirty, undefined, 'the follow-up became a draft');
  assert.equal(/Leave this page/.test(t.modal.innerHTML), false, 'a leave prompt opened');
  assert.equal(t.srv.calls.filter(c => c === 'POST /customer/ask').length, posts, 'a turn was sent');
});

// TRC-03: the reviewer's probes C10 and C11 (mutants O1 and O2).
test('TRC-03 (C10): owner -> viewer and Ask open -> paused at one tab return on #/ask (new context): the viewer and the paused line after that one return', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  t.srv.session = { ...ASK_PAUSED, role: 'viewer' };
  t.srv.ctx = 'ctx-viewer';
  await leaveAndReturn(t, 'blur');
  await drain(120);
  assert.equal(t.E.current.workspaceRole, 'viewer');
  assert.equal(t.E.current.askAvailable, false, 'the role boundary adopted the role only');
  assert.match(t.main.shown(), PAUSED_LINE, 'the open Ask form stayed for the new role after one return');
});

test('TRC-03 (C11): a turn working while the person is on #/alerts: a pilot switch change still draws #/alerts again once', async () => {
  const t = await page('#/ask', { session: { ...ASK_OPEN, features: allOn() } });
  const box = askDom(t, 'MY-QUESTION');
  t.srv.askPost = () => t.srv.reply(200, { turn_id: 'T1', thread_id: 'TH1' });
  t.srv.turn = { turn_id: 'T1', thread_id: 'TH1', question: 'MY-QUESTION', state: 'working', step: 1 };
  const slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  await t.win.EWViews.askSubmit(form);
  await drain(20);
  await nav(t, '#/alerts');
  const renders = t.E.current.fetchToken;
  t.srv.session = { ...ASK_OPEN, features: { ...allOn(), sharing: false } };
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.pilotFeatures.sharing, false);
  assert.equal(t.E.current.fetchToken - renders, 1, '#/alerts was not drawn again while a turn worked in the Ask store');
});
// TRC2-01 (confirm2 probe D2): a role downgrade and the spend stop at one
// return, answer on screen: the role boundary wins over the answer hold.
test('TRC2-01: answer on screen, owner -> viewer (new context) and the spend stop at one return: viewer and the paused line, not held', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  await finishedTurn(t);
  const renders = t.E.current.fetchToken;
  t.srv.session = { ...ASK_PAUSED, role: 'viewer' };
  t.srv.ctx = 'ctx-viewer';
  await leaveAndReturn(t, 'blur');
  await drain(120);
  assert.equal(t.E.current.workspaceRole, 'viewer', 'the role change was held with the answer');
  assert.equal(t.E.current.askAvailable, false);
  assert.ok(t.E.current.fetchToken > renders, 'held: not drawn again');
  assert.match(t.main.shown(), PAUSED_LINE);
  assert.doesNotMatch(t.main.shown(), THREAD);
});

// ARV-C-01 (ASK-RESTORE-F3): a send refused with ASK_UNAVAILABLE or ASK_PAUSED
// re-reads the session as a tab return does, so the next in-app #/ask draws
// the off or paused page. No tab return in these cases. Other refusals and a
// sent turn read no session.
const OFF_LINE_TEXT = 'Ask is off for now. When it is on, Muse Spark, Meta’s AI model, answers your questions from the episodes, with every claim linked to the passage behind it.';
const OFF_PAGE = /<p class="ask-off[^"]*" role="status">Ask is off for now\./;
const refuse = (status, code) => () => ({ ok: false, status, headers: { get: () => null },
  text: async () => JSON.stringify({ error: { code, message: code.toLowerCase(), status, details: {} } }) });
async function refusedSend(t, askPost) {
  t.srv.askPost = askPost;
  const box = askDom(t, 'MY-NEXT-QUESTION');
  const slot = node({ hidden: true });
  const form = node({ dataset: { ask: 'new' }, querySelector: sel => (sel === '.ask-refusal' ? slot : sel === '[name=question]' ? box : null) });
  const from = t.srv.calls.length;
  await t.win.EWViews.askSubmit(form);
  await drain(40);
  assert.equal(t.srv.calls.slice(from).filter(c => c === 'POST /customer/ask').length, 1, 'fixture: one send');
  return { slot, from };
}
const ARV_CASES = [
  ['ASK_UNAVAILABLE (the key file refused)', { ask_available: false }, refuse(503, 'ASK_UNAVAILABLE'), OFF_PAGE, OFF_LINE_TEXT],
  ['ASK_PAUSED (the spend stop)', ASK_PAUSED, refuse(503, 'ASK_PAUSED'), PAUSED_LINE, 'Ask is paused right now.'],
];
for (const [label, session, askPost, pageLine, slotLine] of ARV_CASES) {
  test(`ARV-C-01: a finished turn on #/ask, a send refused with ${label}: one session read; the answer stays; in-app #/overview then #/ask draws the matching page`, async () => {
    const t = await page('#/ask', { session: ASK_OPEN });
    await finishedTurn(t);
    assert.match(t.main.shown(), THREAD, 'fixture: the answer on screen');
    t.srv.session = session;
    const { slot, from } = await refusedSend(t, askPost);
    assert.equal(slot.textContent, slotLine, 'the refusal line');
    assert.equal(sessionReads(t, from), 1, 'one session read after the refusal');
    assert.match(t.main.shown(), THREAD, 'the answer on screen is kept, as at a tab return');
    await nav(t, '#/overview');
    await nav(t, '#/ask');
    assert.match(t.main.shown(), pageLine, 'the next #/ask does not draw the matching page');
    assert.doesNotMatch(t.main.shown(), THREAD);
    assert.doesNotMatch(t.main.shown(), /id="ask-q"/, 'no question box');
    assert.equal(t.srv.calls.slice(from).filter(c => c === 'POST /customer/ask').length, 1, 'no second send');
  });
  test(`ARV-C-01: the empty open #/ask, a send refused with ${label}: one session read; in-app #/overview then #/ask draws the matching page`, async () => {
    const t = await page('#/ask', { session: ASK_OPEN });
    t.srv.session = session;
    const { from } = await refusedSend(t, askPost);
    assert.equal(sessionReads(t, from), 1, 'one session read after the refusal');
    await nav(t, '#/overview');
    await nav(t, '#/ask');
    assert.match(t.main.shown(), pageLine, 'the next #/ask does not draw the matching page');
    assert.doesNotMatch(t.main.shown(), /id="ask-q"/, 'no question box');
  });
}
test('ARV-C-01: a sent turn, and a refusal with any other code or status, read no session', async () => {
  const t = await page('#/ask', { session: ASK_OPEN });
  const sent = t.srv.calls.length;
  await finishedTurn(t);
  assert.equal(sessionReads(t, sent), 0, 'a sent turn read the session');
  for (const askPost of [refuse(429, 'ASK_LIMIT_DAY'), refuse(503, 'ASK_BUSY'), refuse(400, 'INVALID_QUESTION'), refuse(500, 'INTERNAL'), refuse(409, 'CONFLICT_X')]) {
    const { from } = await refusedSend(t, askPost);
    assert.equal(sessionReads(t, from), 0, 'a refusal that does not change Ask read the session');
  }
  await nav(t, '#/overview');
  await nav(t, '#/ask');
  assert.match(t.main.shown(), /id="ask-q"/, 'the open page');
});
