/* DEMO2 pass 24 (owner decisions on the Astra round 13 and code round 20
   findings).
   1. Tab-return gate: from the visible, focus or pageshow event until
      that return's own session read answers, every non-session answer
      (data reads, write answers, player loads) is held and not painted.
      The same person: released in order. Another person or signed out:
      dropped, and the identity wipe runs. The return read is always
      issued after the return; a read issued before the tab was left
      never satisfies it (SEC-R13-01, SEC-R13-02).
   2. Throttle (R20-04): changed by pass 24b to one read per departure,
      sent at once, with no time window.
   3. History owner tag = per-tab id + identity epoch (sessionStorage,
      survives reloads) + principal + workspace. The same person's Back
      restores after a reload or a sign-in return; A -> B -> A, across
      reloads too, does not (R20-N1, R20-N2).
   4. A missing or mismatched tag also bypasses every Back cache: query,
      scroll and page caches (SEC-R13-03).
   5. Header "Search", "Compare" and "Saved" push a new entry; Back
      restores the previous query (R20-01).
   6. "Switched to …" only after the newer read is adopted, and only for
      the switch target (R20-03).
   7. Player load and record-read errors belong to their play; a newer
      play ends older ones (SEC-R13-04, R20-05).
   Pins (R20-N1): the identity epoch across loads, the workspace part of
   the tag, the R19-01 and R19-N3 second orders, audio stopping when a
   page's own session read changes the workspace.
   Fixtures are the pass 23 ones, plus a browser model that survives a
   reload and a per-tab sessionStorage. Only fetch, DOM, media, history,
   storage, timers and location nodes are simulated; the real EW.api,
   apiCall, boot, go, render, handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass24.test.cjs */
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
  return { pending: () => queue.length, run: () => { for (const id of queue.splice(0)) id.fn(...id.args); } };
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
// SEC-R13-01: answers that arrive while the return read is pending wait
// ============================================================================
test('SEC-R13-01: a record answer that arrives while the return read is pending is not painted; another person: never', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read was sent and is pending');
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'the previous person\'s unseen record was painted before the return read answered');
  session.release(0, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(80);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'the previous person\'s record was painted for the other person');
});
test('SEC-R13-01: signed out elsewhere: the held record answer is dropped', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  returnTab(t, 'visibilitychange');
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'painted before the return read answered');
  t.srv.signedOut = true;
  session.release(0, reply(401, SIGNED_OUT));
  await drain(80);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'the held record was painted on the signed-out tab');
});
test('SEC-R13-01 control: the same person: the held record is painted once the return read confirms', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'painted before the return read answered');
  session.release(0, reply(200, { ...HOSTED }));
  await drain(80);
  assert.equal(t.E.current.principal, OWNER);
  assert.match(t.main.shown(), new RegExp(UNSEEN), 'the same person\'s record was not painted after the confirmation');
});
test('SEC-R13-01: a player load that answers while the return read is pending starts nothing; another person: never', async () => {
  const t = await audioPage();
  const record = holdEach(t.win, isRecord('sr1.OLD'));
  const play = playClick(t, 'sr1.OLD');
  await drain(10);
  assert.equal(record.count(), 1, 'fixture: the record read for the play was sent');
  leaveTab(t);
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  record.release(0, reply(200, { ok: true, record: { media: { playback: 'local' }, selected_bounds: { start_s: 10, end_s: 50 } }, workspace_context: 'ctx-owner' }));
  await drain(30);
  const started = () => t.audios.some(a => a.attached && !a.paused && /sr1\.OLD/.test(a.src));
  assert.equal(started(), false, 'the previous person\'s clip started before the return read answered');
  session.release(0, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await play.catch(() => {});
  await drain(60);
  assert.equal(t.E.current.principal, OTHER);
  assert.equal(started(), false, 'the previous person\'s clip plays for the other person');
  assert.deepEqual(t.toasts.filter(m => /clip/.test(m)), []);
});
test('SEC-R13-01: a write answered while the return read is pending, then signed out elsewhere: nothing is announced', async () => {
  const t = await alertsPage();
  const write = holdEach(t.win, (url, opts) => url === '/api/v1/customer/alerts' && opts.method === 'POST');
  const saving = t.E.api('/customer/alerts', { method: 'POST', body: { name: 'OWNER-ALERT' } }).catch(e => e);
  await drain(10);
  assert.equal(write.count(), 1, 'fixture: the write was sent');
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  write.release(0, reply(200, { alert: { id: 7 }, workspace_context: 'ctx-owner' }));
  await drain(20);
  t.srv.signedOut = true;
  session.release(0, reply(401, SIGNED_OUT));
  await saving;
  await drain(60);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.deepEqual(t.toasts.filter(m => /Saved|saved|change/.test(m)), [], 'a write answer held for the return was announced on the signed-out tab');
});
test('SEC-R13-01 control: a write answered while the same person\'s return read is pending returns its result', async () => {
  const t = await alertsPage();
  const write = holdEach(t.win, (url, opts) => url === '/api/v1/customer/alerts' && opts.method === 'POST');
  const saving = t.E.api('/customer/alerts', { method: 'POST', body: { name: 'OWNER-ALERT' } }).then(r => ({ r }), e => ({ e }));
  await drain(10);
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  write.release(0, reply(200, { alert: { id: 7 }, workspace_context: 'ctx-owner' }));
  await drain(20);
  session.release(0, reply(200, { ...HOSTED }));
  const res = await saving;
  assert.ok(res.r && res.r.alert && res.r.alert.id === 7, `the same person's write lost its answer: ${res.e && res.e.message}`);
});
// Pass 27 (owner decision 1, SEC-R16-01): a failed read never releases,
// sign of a change or not; the follow-up read that confirms the same
// person does.
test('SEC-R13-01 transport policy: the return read fails, no sign of a change: nothing is painted until a read confirms the same person', async () => {
  const t = await alertsPage();
  const timers = longTimers(t);
  const record = await evidenceHeld(t);
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  record.release(0, unseenRecord);
  await drain(40);
  session.release(0, lostConnection);
  await drain(80);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'a failed return read released the held record');
  timers.run();
  await drain(20);
  assert.equal(session.count(), 2, 'fixture: the follow-up read');
  session.release(1, reply(200, { ...HOSTED }));
  await drain(80);
  assert.equal(t.E.current.principal, OWNER);
  assert.match(t.main.shown(), new RegExp(UNSEEN), 'the follow-up read confirmed the same person, but the page stayed held');
});
test('SEC-R13-01 transport policy: the return read fails after a held answer showed a change: hold until the next read', async () => {
  const t = await alertsPage();
  const advance = fakeClock(t);
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
  assert.equal(other.count(), 1, 'fixture: a second read was sent');
  // Another person signed in elsewhere: this read's answer names their
  // workspace context.
  record.release(0, unseenRecord);
  await drain(10);
  other.release(0, reply(200, { alerts: [], workspace_context: 'ctx-other' }));
  await drain(40);
  session.release(0, lostConnection);
  await drain(60);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'a failed return read released answers after a sign of a change');
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  advance(5000);
  timers.run();
  await drain(40);
  assert.equal(session.count(), 2, 'no next session read was issued');
  session.release(1, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await probe;
  await drain(80);
  assert.equal(t.E.current.principal, OTHER);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'the previous person\'s record was painted for the other person');
});

// ============================================================================
// SEC-R13-02: a read issued before the tab was left never answers a return
// ============================================================================
for (const first of ['old', 'new']) {
  test(`SEC-R13-02: a reconcile held across a sibling-tab sign-in: the return issues its own read (${first} answer first)`, async () => {
    const t = await inviteEntry();
    const advance = fakeClock(t);
    const session = holdEach(t.win, isSessionRead);
    const sent = sentLog(t.win);
    const before = t.win.EWViews.reconcileCanonical();
    await drain(10);
    assert.equal(session.count(), 1, 'fixture: the earlier reconcile read is pending');
    leaveTab(t);
    advance(6000);
    t.srv.session = { ...HOSTED, ...OTHER_NO_WS };
    returnTab(t);
    await drain(20);
    assert.equal(session.count(), 2, 'the return joined a read issued before the tab was left');
    const oldAnswer = reply(200, { ...HOSTED, ...NO_WS });
    const newAnswer = reply(200, { ...HOSTED, ...OTHER_NO_WS });
    if (first === 'old') { session.release(0, oldAnswer); await drain(20); session.release(1, newAnswer); }
    else { session.release(1, newAnswer); await drain(20); session.release(0, oldAnswer); }
    await before.catch(() => {});
    await drain(80);
    assert.equal(sessionReads(sent), 2);
    assert.equal(t.E.current.principal, OTHER, 'the return ended on the session read before the tab was left');
    assert.doesNotMatch(t.main.shown(), /owner@example\.test/, 'the previous person is still painted');
  });
}

// ============================================================================
// R20-04, as changed by pass 24b: a return 4 s after the previous one
// gets its own read at once (one read per departure, no time window)
// ============================================================================
test('R20-04: a return 4 s after the previous one, after a sign-out elsewhere: its own read, at once', async () => {
  const t = await audioPage();
  const player = await playing(t);
  const advance = fakeClock(t);
  const timers = longTimers(t);
  const sent = sentLog(t.win);
  leaveTab(t);
  returnTab(t);
  await drain(40);
  assert.equal(sessionReads(sent), 1, 'fixture: the first return read');
  advance(4000);
  leaveTab(t);
  t.srv.signedOut = true;
  returnTab(t);
  await drain(60);
  // The return read, then the signed-out page's own read.
  assert.ok(sessionReads(sent) >= 2, 'the second return did not read');
  assert.equal(timers.pending(), 0, 'the second return waited on a timer');
  assert.equal(t.E.current.principal, null, 'the sign-out was not adopted');
  stoppedAndGone(player, 'the search player');
});
test('R20-04: the second return\'s read is gated: an answer waits for it', async () => {
  const t = await alertsPage();
  const advance = fakeClock(t);
  leaveTab(t);
  returnTab(t);
  await drain(40);
  advance(3000);
  const record = await evidenceHeld(t);
  leaveTab(t);
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the second return read is pending');
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'an answer was painted before the return read answered');
  session.release(0, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(80);
  assert.equal(t.E.current.principal, OTHER, 'the return read did not adopt the other person');
  assert.equal(everPainted(t, new RegExp(UNSEEN)), false, 'the previous person\'s record was painted for the other person');
});

// ============================================================================
// SEC-R13-03: a missing or mismatched owner tag restores no Back cache
// ============================================================================
const TAMPER = {
  'missing tag': st => { delete st.earWitnessOwner; },
  'another tab or document': st => { st.earWitnessOwner.doc = 'another-document'; st.earWitnessOwner.tab = 'another-tab'; },
  'another principal': st => { st.earWitnessOwner.principal = OTHER; },
  'another identity epoch': st => { st.earWitnessOwner.gen = 99; st.earWitnessOwner.epoch = 99; },
  'another workspace': st => { st.earWitnessOwner.workspace = 'ws-z'; },
};
for (const [mode, tamper] of Object.entries(TAMPER)) {
  test(`SEC-R13-03: same-URL popstate onto an evidence entry with a ${mode}: no Back query, no scroll`, async () => {
    const t = await alertsPage();
    const B = browser(t);
    await go(t, SEARCH);
    await go(t, EVIDENCE);
    assert.match(t.main.innerHTML, LABEL, 'fixture: the owner sees their own origin');
    const st = B.cur().state;
    tamper(st);
    B.cur().state = st;
    t.E.current.scroll.set(EVIDENCE, 777);
    const scrolls = [];
    t.win.scrollTo = (x, y) => scrolls.push(y);
    for (const fn of t.win.listeners.popstate || []) fn({ state: clone(B.cur().state) });
    await drain(60);
    assert.equal(t.E.current.route, EVIDENCE, 'fixture: still the evidence page');
    noSecret(t.main.innerHTML, 'an unowned entry restored the Back query');
    assert.ok(!scrolls.includes(777), `an unowned entry restored the scroll position: ${JSON.stringify(scrolls)}`);
  });
}
test('SEC-R13-03: a direct hash entry for an evidence page seen before: no Back query', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  await go(t, '#/alerts');
  t.win.location.hash = EVIDENCE;
  assert.equal(B.cur().state, null, 'fixture: a new entry with no state');
  for (const fn of t.win.listeners.popstate || []) fn({ state: null });
  for (const fn of t.win.listeners.hashchange || []) fn({});
  await drain(60);
  assert.equal(t.E.current.route, EVIDENCE);
  noSecret(t.main.innerHTML, 'an untagged entry inherited the Back query');
});

// ============================================================================
// R20-01: the header's plain routes push; Back restores the query
// ============================================================================
const PLAIN = [
  ['Search', '#/search?q=acme', '#/search'],
  ['Compare', '#/compare?brands=acme,globex', '#/compare'],
  ['Saved', '#/saved?tab=bookmarks&q=acme', '#/saved?tab=bookmarks'],
];
for (const [name, withTerms, plain] of PLAIN) {
  test(`R20-01: ${name} with terms, then the plain ${name} link: Back restores the terms, Forward the plain route`, async () => {
    const t = await alertsPage();
    const B = browser(t);
    await go(t, withTerms);
    assert.equal(t.E.current.route, withTerms, 'fixture: the route with terms');
    const before = B.entries.length;
    clickLink(t, plain);
    await drain(60);
    assert.equal(t.E.current.route, plain, 'fixture: the plain route');
    assert.equal(B.entries.length, before + 1, 'the plain route did not push a new entry');
    await B.traverse(-1);
    assert.equal(t.E.current.route, withTerms, 'Back lost the terms');
    await B.traverse(1);
    assert.equal(t.E.current.route, plain, 'Forward lost the plain route');
    await B.traverse(-1);
    assert.equal(t.E.current.route, withTerms, 'the entry holding the terms was overwritten');
  });
}

// ============================================================================
// R20-03: "Switched to …" only after the newer read is adopted
// ============================================================================
async function switchWithNewerRead(t, newerAnswer) {
  switchServer(t);
  const session = holdEach(t.win, isSessionRead);
  const switching = chooseWs(t, 'ws-b');
  await drain(30);
  assert.equal(session.count(), 1, 'fixture: the switch\'s confirming read is pending');
  const newer = t.E.boot().catch(() => {});
  await drain(5);
  assert.equal(session.count(), 2, 'fixture: a newer page read is pending');
  session.release(0, reply(200, { ...HOSTED, ...WS_B }));
  await drain(30);
  session.release(1, reply(200, newerAnswer));
  await newer;
  await switching;
  await drain(80);
}
test('R20-03: the newer read lands in ws (moved by another tab): no "Switched to ws-b."', async () => {
  const t = await alertsPage();
  await switchWithNewerRead(t, { ...HOSTED, workspace_switch_revision: 9 });
  assert.equal(t.E.current.activeWorkspace, 'ws', 'fixture: the newer read adopted ws');
  assert.ok(!t.toasts.includes('Switched to ws-b.'), `announced a switch the adopted session contradicts: ${JSON.stringify(t.toasts)}`);
});
test('R19-N3 pin (second order): the switch read answers first, the newer read confirms ws-b: "Switched to ws-b."', async () => {
  const t = await alertsPage();
  await switchWithNewerRead(t, { ...HOSTED, ...WS_B });
  assert.equal(t.E.current.activeWorkspace, 'ws-b');
  assert.ok(t.toasts.includes('Switched to ws-b.'), `no "Switched to" message: ${JSON.stringify(t.toasts)}`);
});

// ============================================================================
// SEC-R13-04 / R20-05: an old play's errors stay with it
// ============================================================================
for (const [how, answer] of [['HTTP 500', reply(500, { ok: false, error: { code: 'SERVER', message: 'old clip failed' }, workspace_context: 'ctx-owner' })], ['lost connection', lostConnection]]) {
  test(`SEC-R13-04 / R20-05: an old play's record read fails (${how}) after a newer play started: nothing is said`, async () => {
    const t = await audioPage();
    const old = holdEach(t.win, isRecord('sr1.OLD'));
    const first = playClick(t, 'sr1.OLD');
    await drain(10);
    assert.equal(old.count(), 1, 'fixture: the old record read is pending');
    await playClick(t, 'sr1.NEW');
    await drain(10);
    const player = t.audios.find(a => a.id === 'search-audio');
    assert.ok(player && !player.paused && /sr1\.NEW/.test(player.src), 'fixture: the newer clip plays');
    const before = t.toasts.length;
    old.release(0, answer);
    await first.catch(() => {});
    await drain(20);
    assert.deepEqual(t.toasts.slice(before).filter(m => /clip/.test(m)), [], 'the old play\'s error was announced over the newer one');
    assert.ok(!player.paused && /sr1\.NEW/.test(player.src), 'the newer clip stopped');
  });
}
test('SEC-R13-04 control: the play\'s own record read fails: it says so', async () => {
  const t = await audioPage();
  const old = holdEach(t.win, isRecord('sr1.OLD'));
  const first = playClick(t, 'sr1.OLD');
  await drain(10);
  old.release(0, reply(500, { ok: false, error: { code: 'SERVER', message: 'boom' }, workspace_context: 'ctx-owner' }));
  await first.catch(() => {});
  await drain(20);
  assert.ok(t.toasts.some(m => /This clip could not be loaded/.test(m)), `no failure message: ${JSON.stringify(t.toasts)}`);
});

// ============================================================================
// Owner decision 3 / R20-N2: the same person's Back survives a reload
// ============================================================================
test('R20-N2: the same person reloads on the evidence page: the Back label and Back restore the search', async () => {
  const storage = memStorage();
  const t = await page('#/alerts', { storage });
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  assert.match(t.main.innerHTML, LABEL, 'fixture: the owner sees their own origin');
  const t2 = await reload(B, storage);
  assert.equal(t2.E.current.principal, OWNER);
  assert.match(t2.main.innerHTML, LABEL, 'after a reload the same person lost the Back label');
  await B.traverse(-1);
  assert.equal(t2.E.current.route, SEARCH, 'after a reload the same person\'s Back lost the search');
  for (const url of B.urls()) noSecret(url, 'a history URL holds the private search');
  // sessionStorage holds the tab id and the epoch, nothing else.
  assert.deepEqual(Object.keys(storage.data), ['earWitnessTabEpoch'], 'sessionStorage holds more than the tab epoch');
  const kept = JSON.parse(storage.data.earWitnessTabEpoch);
  assert.deepEqual(Object.keys(kept).sort(), ['n', 'tab']);
  assert.equal(typeof kept.tab, 'string');
  assert.ok(Number.isInteger(kept.n));
  for (const word of [OWNER, 'owner@example.test', 'ctx-owner', SECRET]) assert.ok(!storage.data.earWitnessTabEpoch.includes(word), 'sessionStorage holds ' + word);
});
test('R20-N2: the same person signs in again after the session expired: Back restores the earlier entries', async () => {
  const storage = memStorage();
  const t = await page('#/alerts', { storage });
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  await go(t, '#/alerts');
  t.srv.signedOut = true;
  await t.win.EWViews.followSignedOut();
  await drain(40);
  assert.equal(t.E.current.principal, null, 'fixture: the session expired');
  B.land('#/overview');
  const t2 = await reload(B, storage);
  assert.equal(t2.E.current.principal, OWNER, 'fixture: the same person is back');
  await B.traverse(-1);
  await B.traverse(-1);
  assert.equal(t2.E.current.route, EVIDENCE, 'fixture: Back reached the evidence entry');
  assert.match(t2.main.innerHTML, LABEL, 'after the sign-in return the Back label is gone');
  await B.traverse(-1);
  assert.equal(t2.E.current.route, SEARCH, 'after the sign-in return Back lost the search');
});

// ============================================================================
// R20-N1 pins: the identity epoch across loads, its fallback, the
// workspace part of the tag
// ============================================================================
test('R20-N1 pin: A, then B and A again on reloads (another tab signed them in): A\'s first-visit search is not restored', async () => {
  const storage = memStorage();
  const t = await page('#/alerts', { storage });
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  const t2 = await reload(B, storage, { ...HOSTED, ...OTHER_SESSION });
  assert.equal(t2.E.current.principal, OTHER, 'fixture: B after the reload');
  noSecret(t2.main.innerHTML, 'B sees A\'s search');
  const t3 = await reload(B, storage);
  assert.equal(t3.E.current.principal, OWNER, 'fixture: A after the second reload');
  await B.traverse(-1);
  assert.match(t3.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(t3.E.current.route, 'an entry from A\'s first visit was restored after B');
});
test('R20-N1 pin: A signs out, B signs in and out, A signs in: A\'s first-visit search is not restored', async () => {
  const storage = memStorage();
  const t = await page('#/alerts', { storage });
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  // A signs out: the page reloads on the same entry, signed out.
  const out1 = await reload(B, storage, null);
  assert.equal(out1.E.current.principal, null, 'fixture: signed out');
  // B signs in: the sign-in return lands on a new entry.
  B.land('#/overview');
  const b = await reload(B, storage, { ...HOSTED, ...OTHER_SESSION });
  assert.equal(b.E.current.principal, OTHER, 'fixture: B');
  await go(b, '#/alerts');
  const out2 = await reload(B, storage, null);
  assert.equal(out2.E.current.principal, null, 'fixture: B signed out');
  B.land('#/overview');
  const a = await reload(B, storage);
  assert.equal(a.E.current.principal, OWNER, 'fixture: A is back');
  while (B.i > 1) await B.traverse(-1);
  assert.match(a.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(a.E.current.route, 'an entry from A\'s first visit was restored after B');
});
test('R20-N1 pin: storage unavailable: a reload restores nothing (per-load fallback)', async () => {
  const t = await page('#/alerts', { storage: 'unavailable' });
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  assert.match(t.main.innerHTML, LABEL, 'fixture: the owner sees their own origin');
  const t2 = await reload(B, 'unavailable');
  assert.equal(t2.E.current.principal, OWNER);
  noSecret(t2.main.innerHTML, 'a reload without storage kept the Back label');
  await B.traverse(-1);
  noSecret(t2.E.current.route, 'a reload without storage restored the search');
});
test('R20-N1 pin: the same person moves to ws-b: Back to a ws search entry opens Search fresh', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  await signIn(t, WS_B);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: in ws-b');
  await B.traverse(-1);
  assert.match(t.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(t.E.current.route, 'a ws entry restored its search in ws-b');
});

// ============================================================================
// R20-N1 pins: R19-01 second order, audio on a page read's workspace
// ============================================================================
test('R19-01 pin (second order): the render\'s read answers first, then the reconcile finds nothing: Settings publishes', async () => {
  const t = await alertsPage('#/settings');
  const session = holdEach(t.win, isSessionRead);
  const rendering = t.win.EWViews.render();
  await drain(10);
  assert.equal(session.count(), 1, 'fixture: Settings\' own read is pending');
  const reconciling = t.win.EWViews.reconcileCanonical();
  await drain(10);
  assert.equal(session.count(), 2, 'fixture: the reconcile read is pending');
  session.release(0, reply(200, { ...HOSTED }));
  await rendering;
  await drain(20);
  session.release(1, reply(200, { ...HOSTED }));
  assert.equal(await reconciling, 'unchanged', 'fixture: the reconcile painted nothing');
  await drain(60);
  assert.equal(t.E.current.publishedToken, t.E.current.fetchToken, 'Settings stays on its loading frame');
  assert.equal(t.E.current.publishedRoute, '#/settings');
});
test('R20-N1 pin: a Settings render whose own session read names ws-b stops the player', async () => {
  const t = await audioPage('#/settings');
  const player = await playing(t);
  t.srv.session = { ...HOSTED, ...WS_B };
  // The page's other reads answer for the context they were sent from,
  // so no read-path reconcile adopts ws-b: the Settings page's own
  // session read is the only adopter.
  const inner = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => {
    const sentFor = t.E.current.workspaceContext || '';
    const r = await inner(url, opts);
    if (isSessionRead(url, opts)) return r;
    const body = await r.text();
    return { ...r, text: async () => body.replace(/"workspace_context":"[^"]*"/, `"workspace_context":"${sentFor}"`) };
  };
  await t.win.EWViews.render();
  await drain(60);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: the page read adopted ws-b');
  stoppedAndGone(player, 'the search player');
});
