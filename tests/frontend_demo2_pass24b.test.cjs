/* DEMO2 pass 24b (owner decision on the pass 24 return-read throttle).
   A return counts only after a real departure (visibilitychange to
   hidden, window blur, pagehide). A focus or visible event with no
   departure since the last return does nothing: no gate, no read. Each
   departure allows exactly one return read, sent at once; every return
   event of that departure (focus, visible, pageshow) collapses into it.
   There is no time window. The gate applies to that read only. A return
   read that fails in transport keeps the pass 24 policy; its follow-up
   read is sent at most once, after 2 s.
   Fixtures are the pass 24 ones.
   Run: node --test tests/frontend_demo2_pass24b.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

// ============================================================================
// Fixture (pass 23), with a hook that runs before the app scripts load
// ============================================================================
class FakeFormData {
  constructor(form) { this.form = form; }
  all() { return ((this.form && this.form.elements) || []).filter(el => el.name && ((el.type !== 'checkbox' && el.type !== 'radio') || el.checked)).map(el => [el.name, String(el.value ?? '')]); }
  get(k) { const e = this.all().find(([n]) => n === k); return e ? e[1] : null; }
  entries() { return this.all()[Symbol.iterator](); }
}
const OWNER = 'oidc:https://i.example:owner';
const OTHER = 'oidc:https://i.example:other';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws', 'ws-b'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };

function loadPages(route, fetch, before) {
  const listeners = {};
  const docListeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, FormData: FakeFormData, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  if (before) before(sandbox);
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
function server(data, session) {
  const state = { calls: [], answers: {}, session: { ...session }, signedOut: false };
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    const method = init.method || 'GET';
    state.calls.push({ method, path: p });
    const fixed = state.answers[p] && (!state.answers[p].method || state.answers[p].method === method) ? state.answers[p] : null;
    if (p === '/session') {
      return state.signedOut ? reply(401, { ok: false, error: 'signin', signin: { available: true } }) : reply(200, JSON.parse(JSON.stringify(state.session)));
    }
    if (fixed) return reply(fixed.status, { ok: false, error: { code: fixed.code || 'FORBIDDEN', message: fixed.message } });
    const body = data(p, init);
    if (body === undefined) return reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
    const copy = JSON.parse(JSON.stringify(body));
    if (copy && typeof copy === 'object' && !('workspace_context' in copy)) copy.workspace_context = state.session.workspace_context;
    return reply(200, copy);
  };
  return state;
}
function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false, ...props };
}
function modalStub() {
  const m = node({ open: false, forms: [], showModal() { this.open = true; }, close() { this.open = false; },
    querySelector: sel => (sel === 'button' ? { focus() {} } : null) });
  m.querySelectorAll = sel => {
    if (!/form/.test(sel)) return [];
    return m.forms.filter(f => sel.split(',').some(part => {
      const want = part.match(/data-([a-z]+)="1"/);
      return !want || (f.dataset && f.dataset[want[1]] === '1');
    }));
  };
  return m;
}
function formStub(id, modal, fields, data = {}) {
  return node({ id, dataset: { ...data }, elements: fields.map(([name, value, type = 'text', defaultValue = '']) => ({ name, value, type, defaultValue, dataset: {} })),
    getAttribute: n => (n === 'id' ? id : null), closest: sel => (sel === '#modal' ? modal : null) });
}
// #main that paints like the browser, and remembers every full paint.
function mainStub() {
  let html = '';
  const main = node({ inserted: [], painted: [] });
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.inserted = []; main.painted.push(v); } });
  main.insertAdjacentHTML = (pos, h) => { const b = { html: h, remove() { main.inserted = main.inserted.filter(x => x !== b); } }; main.inserted.unshift(b); main.painted.push(h); };
  main.querySelector = sel => (sel === '[data-outcome-notice]' ? main.inserted.find(b => /data-outcome-notice/.test(b.html)) || null : null);
  main.shown = () => html + main.inserted.map(b => b.html).join('');
  return main;
}
function alertsServer(session = HOSTED) {
  const data = (p, init = {}) => {
    const method = init.method || 'GET';
    if (p === '/customer/shows') return { shows: [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }] };
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return method !== 'GET' ? { alert: { id: 1 } } : { alerts: [] };
    if (p.startsWith('/customer/filter-values')) return { items: [{ value: 'cs-private', label: 'PRIVATE-SHOW' }] };
    if (p.startsWith('/customer/summary')) return { groups: [] };
    if (p === '/customer/preferences') return { preferences: {} };
    if (/^\/customer\/records\/sr1\.R12-PIN$/.test(p)) return { record: { record_id: 'R12' } };
    return undefined;
  };
  return server(data, session);
}
// A per-tab sessionStorage, shared by the loads of one tab.
function memStorage() {
  const data = {};
  return { data, getItem: k => (Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: k => { delete data[k]; } };
}
// A page load. `B`: a browser history model the page loads into (a
// reload or a sign-in return); `storage`: the tab's sessionStorage, or
// 'unavailable' (access throws).
// `session` null: the server says signed out.
async function page(route = '#/alerts', { session = HOSTED, storage = null, B = null } = {}) {
  const srv = alertsServer(session || HOSTED);
  if (!session) srv.signedOut = true;
  const win = loadPages(route, srv.fetch, sb => {
    if (storage === 'unavailable') Object.defineProperty(sb, 'sessionStorage', { configurable: true, get() { throw new Error('SecurityError: storage is disabled'); } });
    else if (storage) sb.sessionStorage = storage;
    if (B) B.bind(sb);
  });
  for (let i = 0; i < 5; i++) await tick();
  const main = mainStub();
  const modal = modalStub();
  const toastEl = node();
  const pageForms = [];
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#toast' ? toastEl : node());
  win.document.querySelectorAll = sel => {
    if (!/form/.test(sel)) return [];
    const forms = pageForms.filter(f => sel.split(',').some(part => {
      const want = part.match(/data-([a-z]+)="1"/);
      return !want || (f.dataset && f.dataset[want[1]] === '1');
    }));
    return [...modal.querySelectorAll(sel), ...forms];
  };
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  const toasts = [];
  E.toast = m => { toasts.push(m); toastEl.textContent = m; };
  if (!B) { win.location.hash = route; E.current.route = route; }
  await win.EWViews.render();
  await drain(30);
  return { srv, win, E, main, modal, toastEl, toasts, pageForms };
}
const alertsPage = (route = '#/alerts', session = HOSTED) => page(route, { session });
const INVITE = '#/invite?workspace=ws-b&id=inv-1';
const NO_WS = { active_workspace: '', workspace_context: '', workspaces: [] };
async function inviteEntry() {
  const srv = server((p, init = {}) => {
    if (/\/invites\/inv-1\/preflight$/.test(p)) return { invitation: { workspace: 'ws-b', role: 'viewer', workspace_name: 'ws-b', state: 'pending' } };
    if (/\/invites\/inv-1\/accept$/.test(p) && init.method === 'POST') return { ok: true };
    return undefined;
  }, { ...HOSTED, ...NO_WS });
  const win = loadPages(INVITE, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = mainStub();
  const modal = modalStub();
  const toastEl = node();
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#toast' ? toastEl : node());
  win.document.querySelectorAll = () => [];
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  const toasts = [];
  E.toast = m => { toasts.push(m); toastEl.textContent = m; };
  win.location.hash = INVITE;
  E.current.route = INVITE;
  await win.EWViews.render();
  await drain(30);
  return { srv, win, E, main, modal, toastEl, toasts };
}

// ============================================================================
// Shared helpers
// ============================================================================
const reply = (status, body) => () => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
const lostConnection = () => { throw new TypeError('Failed to fetch'); };
// Holds every request matching `match`, in dispatch order; each is
// answered on its own with release(i, answer).
function holdEach(win, match) {
  const inner = win.fetch;
  const held = [];
  win.fetch = (url, opts = {}) => {
    if (!match(url, opts)) return inner(url, opts);
    let release;
    const gate = new Promise(res => { release = res; });
    held.push({ url, release });
    return gate.then(answer => answer());
  };
  return { held, count: () => held.length, release: (i, answer) => held[i].release(answer) };
}
// Every request that leaves the tab, logged at dispatch.
function sentLog(win) {
  const sent = [];
  const inner = win.fetch;
  win.fetch = (url, opts = {}) => { sent.push(`${opts.method || 'GET'} ${url}`); return inner(url, opts); };
  return sent;
}
async function signIn(t, session) {
  t.srv.signedOut = false;
  t.srv.session = { ...HOSTED, ...session };
  await t.E.boot();
  await t.win.EWViews.render();
  await drain(60);
}
const OTHER_CSRF = 'k'.repeat(32);
const OTHER_SESSION = { principal: OTHER, email: 'other@example.test', workspace_context: 'ctx-other', csrf_token: OTHER_CSRF };
const OTHER_NO_WS = { principal: OTHER, email: 'other@example.test', csrf_token: OTHER_CSRF, ...NO_WS };
const SIGNED_OUT = { ok: false, error: 'signin', signin: { available: true } };
const isSessionRead = (url, opts) => url === '/api/v1/session' && (!opts.method || opts.method === 'GET');
const sessionReads = sent => sent.filter(s => s === 'GET /api/v1/session').length;
const WS_B = { active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 };

function audioModel(t, extra = {}) {
  const a = { tagName: 'AUDIO', id: '', dataset: {}, className: '', preload: '', paused: true, src: '', attached: false, loads: 0, pauses: 0,
    listeners: {},
    pause() { this.paused = true; this.pauses++; },
    play() { this.paused = false; return t.holdPlay ? t.holdPlay() : Promise.resolve(); },
    load() { this.loads++; },
    removeAttribute(n) { if (n === 'src') this.src = ''; },
    setAttribute(n, v) { if (n === 'src') this.src = v; },
    remove() { this.attached = false; },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    ...extra };
  t.audios.push(a);
  return a;
}
async function audioPage(route = '#/alerts') {
  const t = await alertsPage(route);
  t.audios = [];
  const body = { appendChild: n => { n.attached = true; return n; }, append: n => { n.attached = true; } };
  t.win.document.body = body;
  const create = t.win.document.createElement;
  t.win.document.createElement = tag => (tag === 'audio' ? audioModel(t) : create(tag));
  const qs = t.win.document.querySelector, qsa = t.win.document.querySelectorAll;
  const live = () => t.audios.filter(a => a.attached);
  t.win.document.querySelector = sel => (sel === '#search-audio' ? live().find(a => a.id === 'search-audio') || null : qs(sel));
  t.win.document.querySelectorAll = sel => (/^audio/.test(sel) ? live().filter(a => sel === 'audio' || (/data-clip/.test(sel) && a.dataset.clip)) : qsa(sel));
  t.win.document.getElementById = id => live().find(a => a.id === id) || null;
  const orig = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => (/\/customer\/records\/sr1\.[^/]*AUDIO[^/]*$|\/customer\/records\/sr1\.(OLD|NEW)$/.test(url)
    ? reply(200, { ok: true, record: { media: { playback: 'local' }, selected_bounds: { start_s: 10, end_s: 50 } }, workspace_context: t.E.current.workspaceContext })()
    : orig(url, opts));
  return t;
}
const PRIVATE_AUDIO = 'sr1.OWNER-PRIVATE-AUDIO';
const playClick = (t, ref) => t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref }, closest: () => null });
async function playing(t) {
  await playClick(t, PRIVATE_AUDIO);
  await drain(10);
  const player = t.audios.find(a => a.id === 'search-audio');
  assert.ok(player && player.attached && !player.paused, 'fixture: the search player plays');
  assert.match(player.src, /OWNER-PRIVATE-AUDIO/, 'fixture: with the owner\'s media');
  return player;
}
const stoppedAndGone = (a, what) => {
  assert.equal(a.paused, true, `${what} keeps playing`);
  assert.equal(a.src, '', `${what} keeps its media source`);
  assert.ok(a.loads >= 1, `${what} was not unloaded (load() after removing src)`);
  assert.equal(a.attached, false, `${what} stays attached to the page`);
};
function switchServer(t) {
  const inner = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace' && opts.method === 'POST') {
      t.srv.session = { ...HOSTED, ...WS_B };
      return reply(200, { ...HOSTED, ...WS_B })();
    }
    return inner(url, opts);
  };
}
const chooseWs = (t, ws) => t.win.EWViews.handleSubmit(formStub('workspace-choice', null, [['workspace_id', ws, 'select-one', undefined]])).catch(() => {});

const clone = v => (v == null ? null : JSON.parse(JSON.stringify(v)));
// A browser history that survives a reload: entries {url, state};
// pushState/replaceState update the URL like a browser; Back/Forward
// fire popstate, then hashchange, on the page loaded now.
function browserModel(url) {
  const B = { entries: [{ url, state: null }], i: 0, win: null };
  B.cur = () => B.entries[B.i];
  B.bind = win => {
    B.win = win;
    Object.defineProperty(win.location, 'hash', { configurable: true, get: () => B.cur().url,
      set: v => { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: v, state: null }); B.i++; } });
    win.history = {
      get state() { return clone(B.cur().state); },
      pushState(st, ti, u) { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: u === undefined ? B.cur().url : u, state: clone(st) }); B.i++; },
      replaceState(st, ti, u) { B.entries[B.i] = { url: u === undefined ? B.cur().url : u, state: clone(st) }; },
    };
  };
  B.traverse = async (d) => {
    const old = B.cur().url;
    B.i += d;
    for (const fn of B.win.listeners.popstate || []) fn({ state: clone(B.cur().state) });
    if (B.cur().url !== old) for (const fn of B.win.listeners.hashchange || []) fn({});
    await drain(60);
  };
  // A new document entry with no state (a sign-in return lands here).
  B.land = u => { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: u, state: null }); B.i++; };
  B.urls = () => B.entries.map(e => e.url);
  return B;
}
function browser(t) { const B = browserModel(t.win.location.hash); B.bind(t.win); return B; }
const reload = (B, storage, session = HOSTED) => page(B.cur().url, { session, storage, B });
const go = async (t, route) => { t.E.go(route); await drain(60); };
const SECRET = 'PRIVATE-ACQUISITION-R12';
const SEARCH = `#/search?q=${SECRET}&brands=acme-secret`;
const EVIDENCE = '#/evidence/sr1.R12-PIN';
const LABEL = /Back to Search: “PRIVATE-ACQUISITION-R12”/;
const noSecret = (s, what) => assert.doesNotMatch(String(s), /PRIVATE-ACQUISITION|acme-secret/, what);
const fireWindow = (t, type, event = {}) => { for (const fn of t.win.listeners[type] || []) fn({ type, ...event }); };
const fireDocument = (t, type, event = {}) => { for (const fn of t.win.docListeners[type] || []) fn({ type, ...event }); };
const leaveTab = t => { t.win.document.visibilityState = 'hidden'; fireDocument(t, 'visibilitychange'); };
const blurTab = t => fireWindow(t, 'blur');
const returnTab = (t, type = 'focus') => {
  t.win.document.visibilityState = 'visible';
  if (type === 'visibilitychange') fireDocument(t, 'visibilitychange');
  else fireWindow(t, type, type === 'pageshow' ? { persisted: false } : {});
};
function fakeClock(t, start = 1e9) {
  t.win.__now = start;
  vm.runInContext('Date.now = () => globalThis.__now;', t.win);
  return ms => { t.win.__now += ms; };
}
// Timers of 500 ms or more wait for run(); shorter ones are real.
function longTimers(t) {
  const realSet = t.win.setTimeout, realClear = t.win.clearTimeout;
  const queue = [];
  t.win.setTimeout = (fn, ms, ...args) => {
    if (!(ms >= 500)) return realSet(fn, ms, ...args);
    const id = { fn, ms, args };
    queue.push(id);
    return id;
  };
  t.win.clearTimeout = id => { const i = queue.indexOf(id); if (i >= 0) queue.splice(i, 1); else realClear(id); };
  return { pending: () => queue.length, delays: () => queue.map(id => id.ms), run: () => { for (const id of queue.splice(0)) id.fn(...id.args); } };
}
// A header link click, through the page's own click handler.
function clickLink(t, href) {
  const anchor = { getAttribute: n => (n === 'href' ? href : null), hasAttribute: () => false, target: '' };
  const target = { closest: sel => (/a\[href/.test(sel) ? anchor : null) };
  for (const fn of t.win.docListeners.click || []) fn({ target, button: 0, preventDefault() {} });
}
const UNSEEN = 'OWNER-UNSEEN-R13';
const isRecord = ref => url => url === '/api/v1/customer/records/' + ref;
const unseenRecord = reply(200, { record: { record_id: UNSEEN }, workspace_context: 'ctx-owner' });
const everPainted = (t, re) => t.main.painted.some(h => re.test(h)) || re.test(t.main.shown());
// Opens the evidence page with its record read held (sent under OWNER).
async function evidenceHeld(t) {
  if (!t.B) t.B = browser(t);
  const record = holdEach(t.win, isRecord('sr1.R12-PIN'));
  t.E.go(EVIDENCE);
  await drain(30);
  assert.equal(record.count(), 1, 'fixture: the record read was sent');
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'fixture: not on screen when the tab was left');
  return record;
}

// ============================================================================
// Pass 24b: one return read per departure, at once
// ============================================================================
test('24b-a: a focus without a departure opens no gate and reads nothing', async () => {
  const t = await alertsPage();
  const sent = sentLog(t.win);
  const timers = longTimers(t);
  returnTab(t);
  await drain(30);
  assert.equal(t.win.EWViews.tabReturnGate(), null, 'a focus with no departure opened the gate');
  timers.run();
  await drain(30);
  assert.equal(sessionReads(sent), 0, 'a focus with no departure read the session');
});
test('24b-b: hidden, then visible, then focus: exactly one read, sent at once', async () => {
  const t = await alertsPage();
  fakeClock(t);
  const timers = longTimers(t);
  const sent = sentLog(t.win);
  leaveTab(t);
  returnTab(t, 'visibilitychange');
  await drain(5);
  assert.equal(sessionReads(sent), 1, 'the return read was not sent at once');
  await drain(30);
  returnTab(t);
  fireWindow(t, 'pageshow', { persisted: false });
  await drain(30);
  timers.run();
  await drain(30);
  assert.equal(sessionReads(sent), 1, 'one departure gave more than one return read');
  assert.equal(t.win.EWViews.tabReturnGate(), null, 'the gate stayed open after its read');
});
test('24b-c: a file picker closes (blur, then focus) 1 s after a return: answers wait only for that one read', async () => {
  const t = await alertsPage();
  const advance = fakeClock(t);
  const timers = longTimers(t);
  const sent = sentLog(t.win);
  leaveTab(t);
  returnTab(t, 'visibilitychange');
  await drain(40);
  assert.equal(sessionReads(sent), 1, 'fixture: the first return read');
  advance(1000);
  const record = await evidenceHeld(t);
  const session = holdEach(t.win, isSessionRead);
  blurTab(t);
  returnTab(t);
  await drain(10);
  assert.equal(session.count(), 1, 'the return after the picker closed did not read at once');
  assert.equal(timers.pending(), 0, `the return waits on a timer: ${JSON.stringify(timers.delays())}`);
  record.release(0, unseenRecord);
  await drain(30);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'painted before the return read answered');
  session.release(0, reply(200, { ...HOSTED }));
  await drain(60);
  assert.match(t.main.shown(), new RegExp(UNSEEN), 'the answer still waits after the return read answered');
});
test('24b-d: two departures 1 s apart each get their own read, at once', async () => {
  const t = await alertsPage();
  const advance = fakeClock(t);
  const timers = longTimers(t);
  const sent = sentLog(t.win);
  leaveTab(t);
  returnTab(t, 'visibilitychange');
  await drain(40);
  assert.equal(sessionReads(sent), 1, 'fixture: the first return read');
  advance(1000);
  t.srv.signedOut = true;
  leaveTab(t);
  returnTab(t, 'visibilitychange');
  await drain(10);
  // The return read, then the signed-out page's own reads.
  assert.ok(sessionReads(sent) >= 2, 'the second departure\'s return did not read at once');
  assert.equal(timers.pending(), 0, `the return waits on a timer: ${JSON.stringify(timers.delays())}`);
  await drain(40);
  assert.equal(t.E.current.principal, null, 'the second return did not adopt the sign-out');
});
test('24b-e: a return read that fails in transport after a sign of a change retries once, after 2 s', async () => {
  const t = await alertsPage();
  fakeClock(t);
  const timers = longTimers(t);
  const record = await evidenceHeld(t);
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  const other = holdEach(t.win, url => url === '/api/v1/customer/alerts');
  returnTab(t);
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  const probe = t.E.api('/customer/alerts').catch(e => e);
  await drain(5);
  record.release(0, unseenRecord);
  await drain(10);
  other.release(0, reply(200, { alerts: [], workspace_context: 'ctx-other' }));
  await drain(20);
  session.release(0, lostConnection);
  await drain(40);
  assert.deepEqual(timers.delays().filter(ms => ms >= 1000 && ms <= 6000), [2000], `no single follow-up read after 2 s: ${JSON.stringify(timers.delays())}`);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'released after a sign of a change');
  timers.run();
  await drain(20);
  assert.equal(session.count(), 2, 'the follow-up read was not sent');
  session.release(1, lostConnection);
  await drain(40);
  timers.run();
  await drain(40);
  assert.equal(session.count(), 2, 'more than one follow-up read');
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'released after a sign of a change');
  void probe;
});
