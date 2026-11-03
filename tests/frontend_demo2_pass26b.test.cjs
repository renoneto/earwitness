/* DEMO2 pass 26b (owner decisions on pass 26).
   1. Audio: a clip whose play was asked for but has not started when the
      tab is hidden, or a play asked for while the hold is engaged, does
      not start until the return read confirms the same person: then it
      starts. Another person or signed out: it is cancelled and torn down.
      A clip already playing keeps playing across hide and return
      (SEC-R15-02, P8).
   2. Accepted: a held Back/Forward is dropped when the check finds
      another person or sign-out (no test change).
   3. While a Check again read is in flight the recovery dialog says
      "Checking…" and its button is disabled with aria-busy; when the
      read fails it says "Couldn't confirm your session." again with an
      enabled Check again.
   4. The recovery dialog has a small rule of its own in the customer
      stylesheet, on existing tokens.
   Fixtures are the pass 26 ones; the audio model starts playback only
   when the media answers (the play() promise), as a browser does.
   Run: node --test tests/frontend_demo2_pass26b.test.cjs */
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
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.inserted = []; main.painted.push(v); if (main.onPaint) main.onPaint(v); } });
  main.insertAdjacentHTML = (pos, h) => { const b = { html: h, remove() { main.inserted = main.inserted.filter(x => x !== b); } }; main.inserted.unshift(b); main.painted.push(h); };
  main.querySelector = sel => (sel === '[data-outcome-notice]' ? main.inserted.find(b => /data-outcome-notice/.test(b.html)) || null
    : main.formFor ? main.formFor(sel) : null);
  // Pass 26 (R22-N1): an attribute selector finds the inserted blocks
  // that carry it, as a browser does, so removing one really removes it.
  main.querySelectorAll = sel => {
    const attr = /^\[(data-[a-z-]+)\]$/.exec(sel);
    return attr ? main.inserted.filter(b => new RegExp(attr[1] + '(?=[\\s>=])').test(b.html)) : [];
  };
  main.shown = () => html + main.inserted.map(b => b.html).join('');
  return main;
}
// Pass 26: a browser-like document. The body's children, the top layer
// of modal dialogs (the last one shown is on top), the focused element,
// and dialogs created by the page that leave the document when removed.
function domModel(win, main, modal, toastEl) {
  const doc = win.document;
  const top = [];
  const body = { children: [] };
  body.appendChild = n => { body.children.push(n); n.isConnected = true; return n; };
  doc.body = body;
  doc.topLayer = top;
  doc.activeElement = null;
  const focusTarget = owner => ({ owner, id: '', textContent: '', getAttribute: () => null, focus() { doc.activeElement = this; } });
  for (const el of [node({ id: 'header' }), main, modal, toastEl]) body.appendChild(el);
  const asDialog = d => {
    d.showModal = () => {
      if (d.open) throw new Error('InvalidStateError: the dialog is already open');
      d.open = true;
      top.push(d);
      const b = d.querySelector('button');
      if (b) b.focus();
    };
    d.close = () => { if (!d.open) return; d.open = false; top.splice(top.indexOf(d), 1); };
  };
  asDialog(modal);
  modal.button = focusTarget(modal);
  modal.querySelector = sel => (sel === 'button' ? modal.button : null);
  const create = doc.createElement;
  doc.createElement = tag => {
    if (tag !== 'dialog') return create(tag);
    const attrs = {};
    const d = node({ tagName: 'DIALOG', open: false, isConnected: false, attrs, listeners: {}, style: {} });
    d.setAttribute = (k, v) => { attrs[k] = String(v); };
    d.removeAttribute = k => { delete attrs[k]; };
    d.hasAttribute = k => k in attrs;
    d.getAttribute = k => (k in attrs ? attrs[k] : null);
    d.addEventListener = (type, fn) => { (d.listeners[type] = d.listeners[type] || []).push(fn); };
    d.button = focusTarget(d);
    d.querySelector = sel => (/button|return-check/.test(sel) && /data-action="return-check"/.test(d.innerHTML) ? d.button : null);
    d.remove = () => { d.close(); d.isConnected = false; body.children = body.children.filter(x => x !== d); };
    asDialog(d);
    return d;
  };
  return doc;
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
async function page(route = '#/alerts', { session = HOSTED, storage = null, B = null, at = null } = {}) {
  const srv = alertsServer(session || HOSTED);
  if (!session) srv.signedOut = true;
  const win = loadPages(route, srv.fetch, sb => {
    if (storage === 'unavailable') Object.defineProperty(sb, 'sessionStorage', { configurable: true, get() { throw new Error('SecurityError: storage is disabled'); } });
    else if (storage) sb.sessionStorage = storage;
    if (B) B.bind(sb);
    // Pass 25: the page's visibility and focus at load.
    if (at && at.hidden) sb.document.visibilityState = 'hidden';
    if (at && at.unfocused) sb.document.hasFocus = () => false;
    if (at && at.fetch) at.fetch(sb, srv);
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
  domModel(win, main, modal, toastEl);
  const E = win.EW;
  const toasts = [];
  // Pass 26: the application's own toast, for the toast-hold tests.
  const nativeToast = E.toast;
  E.toast = m => { toasts.push(m); toastEl.textContent = m; };
  if (!B) { win.location.hash = route; E.current.route = route; }
  // A page that loads hidden or with its session read held: its render waits.
  if (at) win.EWViews.render(); else await win.EWViews.render();
  await drain(30);
  return { srv, win, E, main, modal, toastEl, toasts, pageForms, nativeToast };
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
// Pass 25 helpers
// ============================================================================
const UNSEEN_RE = new RegExp(UNSEEN);
const NOTICE_RE = /Couldn(?:'|&#39;)t confirm your session\./;
const CHECK_AGAIN_RE = /data-action="return-check"[^>]*>Check again</;
const isLogout = (url, opts) => url === '/api/v1/session/logout' && opts.method === 'POST';
const signoutClick = t => t.win.EWViews.handleAction({ dataset: { action: 'signout' }, closest: () => null });
const checkAgain = t => t.win.EWViews.handleAction({ dataset: { action: 'return-check' }, closest: () => null });
function captureLocation(win) {
  const nav = { reloads: [] };
  win.location.reload = () => { nav.reloads.push(win.EW.current.principal); };
  return nav;
}
// Like holdEach, but a request whose signal aborts settles at once as an
// AbortError, as a browser fetch does. Tracks how many were open at once.
function holdAbortable(win, match) {
  const inner = win.fetch;
  const held = [];
  let maxOpen = 0;
  const open = () => held.filter(h => !h.settled).length;
  win.fetch = (url, opts = {}) => {
    if (!match(url, opts)) return inner(url, opts);
    const h = { url, settled: false, aborted: false };
    const gate = new Promise((res, rej) => {
      h.release = answer => { if (h.settled) return; h.settled = true; res(answer); };
      const abort = () => {
        if (h.settled) return;
        h.settled = true;
        h.aborted = true;
        const err = new Error('The operation was aborted.');
        err.name = 'AbortError';
        rej(err);
      };
      const sig = opts.signal;
      if (sig && sig.aborted) abort();
      else if (sig && sig.addEventListener) sig.addEventListener('abort', abort, { once: true });
    });
    held.push(h);
    maxOpen = Math.max(maxOpen, open());
    return gate.then(answer => answer());
  };
  return { held, count: () => held.length, open, maxOpen: () => maxOpen, release: (i, answer) => held[i].release(answer) };
}
const clipAnswer = reply(200, { ok: true, record: { media: { playback: 'local' }, selected_bounds: { start_s: 10, end_s: 50 } }, workspace_context: 'ctx-owner' });
const clipStarted = (t, ref) => t.audios.some(a => a.attached && !a.paused && a.src.includes(ref));

// ============================================================================
// Pass 25 helpers, unchanged
// ============================================================================
// The startup session read is held until release().
function heldStartup() {
  const box = {};
  box.at = { fetch: (sb, srv) => {
    let first = true;
    sb.fetch = (url, opts = {}) => {
      if (first && isSessionRead(url, opts)) {
        first = false;
        const answer = reply(200, JSON.parse(JSON.stringify(srv.session)));
        return new Promise(res => { box.release = () => res(answer()); });
      }
      return srv.fetch(url, opts);
    };
  } };
  return box;
}
// A held answer showed a change (a 401, 403 or 409); the return read and
// its one follow-up both fail.
async function stuckTab(status = 409) {
  const t = await alertsPage();
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
  record.release(0, unseenRecord);
  other.release(0, reply(status, { ok: false, error: { code: 'CHANGED', message: 'changed' } }));
  await drain(20);
  session.release(0, lostConnection);
  await drain(40);
  timers.run();
  await drain(20);
  assert.equal(session.count(), 2, 'fixture: the one follow-up read');
  session.release(1, lostConnection);
  await drain(60);
  return { t, timers, session, probe };
}
function nameForm(value, revision) {
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  const field = { name: 'workspace_name', type: 'text', value, defaultValue: value, dataset: {} };
  const rev = { name: 'expected_revision', type: 'hidden', value: revision, defaultValue: revision, dataset: {} };
  const submit = { type: 'submit', textContent: 'Save workspace name', disabled: false };
  return { box, field, rev, submit, dataset: {}, elements: [rev, field], getAttribute: a => (a === 'id' ? 'settings-workspace' : null), closest: () => null,
    contains: () => false, setAttribute() {}, removeAttribute() {},
    querySelector: s => (/errors|held-notice/.test(s) ? box : s === '[type=submit]' ? submit : null), querySelectorAll: s => (s === '[type=submit]' ? [submit] : []),
    reset() { field.value = field.defaultValue; rev.value = rev.defaultValue; } };
}
const SERVER_NAME = 'FRESH-SERVER';
const DRAFT = 'PRIVATE-DRAFT';
// Settings on screen, its name form modelled.
async function settingsPage() {
  const t = await page('#/settings');
  const B = browser(t);
  const inner = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => (url === '/api/v1/customer/preferences' && (!opts.method || opts.method === 'GET')
    ? reply(200, { preferences: { workspace_name: SERVER_NAME, revision: 3 }, workspace_context: t.E.current.workspaceContext })()
    : inner(url, opts));
  t.main.onPaint = html => {
    const m = html.match(/id="settings-workspace"><input type="hidden" name="expected_revision" value="([^"]*)">.*?name="workspace_name" value="([^"]*)"/);
    t.form = m ? nameForm(m[2], m[1]) : null;
  };
  t.main.formFor = sel => (sel === '#settings-workspace' && t.form ? t.form : null);
  t.win.document.querySelectorAll = sel => (/form\[data-(dirty|saving|held)="1"\]/.test(sel) && t.form
    && ['dirty', 'saving', 'held'].some(k => t.form.dataset[k] === '1' && sel.includes(`data-${k}="1"`)) ? [t.form] : []);
  t.win.document.getElementById = id => (id === 'settings-workspace' ? t.form : null);
  await t.win.EWViews.render();
  await drain(30);
  assert.ok(t.form, 'fixture: Settings rendered its name form');
  assert.equal(t.form.field.value, SERVER_NAME, 'fixture: the server value');
  const browse = async url => {
    t.win.location.hash = url;
    for (const fn of t.win.listeners.popstate || []) fn({ state: null });
    for (const fn of t.win.listeners.hashchange || []) fn({});
    await drain(60);
  };
  return { t, B, browse };
}
// Settings with a typed name, then Alerts through a browser hash entry
// (the draft is stashed).
async function settingsDraft() {
  const s = await settingsPage();
  s.t.form.field.value = DRAFT;
  s.t.form.dataset.dirty = '1';
  await s.browse('#/alerts');
  assert.equal(s.t.E.current.publishedRoute, '#/alerts', 'fixture: Alerts');
  return s;
}
const freshSettings = (t, why) => {
  assert.equal(t.E.current.publishedRoute, '#/settings', `fixture: Settings published (${why})`);
  assert.ok(t.form, `fixture: the name form (${why})`);
  assert.equal(t.form.field.value, SERVER_NAME, `the stashed value was refilled (${why})`);
  assert.notEqual(t.form.dataset.dirty, '1', `the form is marked as a draft (${why})`);
  assert.doesNotMatch(t.form.box.innerHTML, /restored/, `a restored-entry notice (${why})`);
};

// ============================================================================
// Pass 26 helpers
// ============================================================================
const NOT_KEPT = /Your unsaved entry on [^.<]*was not kept\./;
const told = t => NOT_KEPT.test(t.main.shown()) || NOT_KEPT.test(JSON.stringify(t.E.current.outcomeNotice || null));
// Hidden from sight and from assistive technology, and not usable.
const concealed = el => !!el && el.inert === true && (el.style.visibility === 'hidden' || el.hidden === true || el.style.display === 'none');
const asBefore = el => !!el && !el.inert && el.style.visibility !== 'hidden' && el.hidden !== true && el.style.display !== 'none';
const surfaces = t => t.win.document.body.children.filter(el => el.attrs && 'data-return-check' in el.attrs && el.open);
// Every recovery notice anywhere: its own surface, or one left in #main.
const noticeCount = t => surfaces(t).length + t.main.querySelectorAll('[data-return-check]').length;
const label = (t, el) => (el === t.main ? '#main' : el === t.modal ? 'the open dialog' : el === t.toastEl ? 'the toast region' : el.id || 'a page element');
// "Couldn't confirm your session." with Check again is the only visible
// and usable surface: above every dialog, with focus.
// `checking` (pass 26b, decision 3): a Check again read is in flight.
function recoveryOnly(t, why, { checking = false } = {}) {
  const doc = t.win.document;
  assert.equal(noticeCount(t), 1, `${why}: not exactly one recovery notice`);
  const s = surfaces(t);
  assert.equal(s.length, 1, `${why}: the recovery notice is not a surface of its own (it sits in the page)`);
  const surface = s[0];
  assert.match(surface.innerHTML, checking ? /Checking…/ : NOTICE_RE, `${why}: no ${checking ? '"Checking…"' : '"Couldn\'t confirm your session."'}`);
  assert.match(surface.innerHTML, CHECK_AGAIN_RE, `${why}: no Check again`);
  assert.equal(doc.topLayer[doc.topLayer.length - 1], surface, `${why}: the recovery surface is not above every dialog`);
  assert.ok(doc.activeElement && doc.activeElement.owner === surface, `${why}: the recovery surface does not have focus`);
  for (const el of doc.body.children) {
    if (el === surface || (el === t.modal && !t.modal.open)) continue;
    assert.ok(concealed(el), `${why}: ${label(t, el)} is still visible or usable`);
  }
}
function nothingConcealed(t, why) {
  for (const el of t.win.document.body.children) assert.ok(asBefore(el), `${why}: ${label(t, el)} was concealed`);
  assert.equal(surfaces(t).length, 0, `${why}: a recovery surface is up`);
}
// Every time #main or the dialog is shown again, what it shows.
function watchReveal(t) {
  const log = [];
  for (const [name, el] of [['main', t.main], ['dialog', t.modal]]) {
    let v = el.style.visibility || '';
    Object.defineProperty(el.style, 'visibility', { configurable: true, enumerable: true, get: () => v,
      set: x => { v = x; if (x !== 'hidden') log.push({ name, shown: name === 'main' ? t.main.shown() : (t.modal.open ? t.modal.innerHTML : '') }); } });
  }
  return log;
}
const PUBLISHED = 'R15-PRIVATE-PUBLISHED';
const PUBLISHED_RE = new RegExp(PUBLISHED);
const PRIVATE_DIALOG_RE = /R15-PRIVATE-DIALOG/;
// The owner's passage reader is on screen (published); another person
// gets their own record.
async function publishedReader() {
  const t = await alertsPage();
  const timers = longTimers(t);
  t.B = browser(t);
  const inner = t.win.fetch;
  t.win.fetch = (url, opts = {}) => (isRecord('sr1.R12-PIN')(url)
    ? Promise.resolve(reply(200, t.srv.session.principal === OWNER
      ? { record: { record_id: PUBLISHED }, workspace_context: 'ctx-owner' }
      : { record: { record_id: 'OTHER-OWN-RECORD' }, workspace_context: t.srv.session.workspace_context })())
    : inner(url, opts));
  await go(t, EVIDENCE);
  assert.match(t.main.innerHTML, PUBLISHED_RE, 'fixture: the reader is on screen');
  return { t, timers };
}
// A dialog with the owner's private text and a typed draft, open.
async function openPrivateDialog(t) {
  t.E.openDialog('Personal note', '<p>R15-PRIVATE-DIALOG</p>', '', '', { fixedCopy: true });
  await drain(5);
  const draft = formStub('note-form', t.modal, [['note', 'MY-DIALOG-DRAFT']], { dirty: '1' });
  t.modal.forms.push(draft);
  assert.ok(t.modal.open, 'fixture: the dialog is open');
  return draft;
}
// Departure, return, a held 409 (a sign of a change), then the return
// read and its one follow-up fail.
async function failConfirmation(t, timers) {
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  const other = holdEach(t.win, url => url === '/api/v1/customer/alerts');
  returnTab(t);
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  t.E.api('/customer/alerts').catch(e => e);
  await drain(5);
  other.release(0, reply(409, { ok: false, error: { code: 'CHANGED', message: 'changed' } }));
  await drain(20);
  nothingConcealed(t, 'while the return read is pending (decision 3: the snapshot stays)');
  session.release(0, lostConnection);
  await drain(40);
  timers.run();
  await drain(20);
  assert.equal(session.count(), 2, 'fixture: the one follow-up read');
  session.release(1, lostConnection);
  await drain(60);
  return { session };
}
// Departure and return with the return read held.
async function heldReturn(t, change = null) {
  leaveTab(t);
  if (change) change(t);
  const session = holdEach(t.win, isSessionRead);
  returnTab(t, 'visibilitychange');
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  assert.ok(t.win.EWViews.tabReturnGate(), 'fixture: the hold is engaged');
  return session;
}
const toOther = t => { t.srv.session = { ...HOSTED, ...OTHER_SESSION }; };
// Every "not kept" notice noted on the tab, kept or wiped later.
function watchNotice(t) {
  const noted = [];
  let v = t.E.current.outcomeNotice;
  Object.defineProperty(t.E.current, 'outcomeNotice', { configurable: true, enumerable: true, get: () => v,
    set: x => { v = x; if (NOT_KEPT.test(JSON.stringify(x || null))) noted.push(x); } });
  return noted;
}
// The toast region's text, every time it changes.
function watchToast(t) {
  const log = [];
  let v = t.toastEl.textContent || '';
  Object.defineProperty(t.toastEl, 'textContent', { configurable: true, get: () => v, set: x => { v = x; if (x) log.push(x); } });
  return log;
}


// ============================================================================
// Pass 26b helpers
// ============================================================================
// A media element as a browser runs it: play() asks for playback, which
// starts only when the media answers (t.startMedia() while t.holdMedia);
// pause() before that interrupts the request (AbortError).
function mediaModel(t) {
  const a = { tagName: 'AUDIO', id: '', dataset: {}, className: '', preload: '', paused: true, started: false, src: '', attached: false,
    loads: 0, pauses: 0, plays: 0, listeners: {}, pending: null,
    pause() {
      this.paused = true;
      this.started = false;
      this.pauses++;
      const p = this.pending;
      this.pending = null;
      if (p) { const err = new Error('The play() request was interrupted by a call to pause().'); err.name = 'AbortError'; p.reject(err); }
    },
    play() {
      this.plays++;
      this.paused = false;
      let entry;
      const promise = new Promise((resolve, reject) => { entry = { resolve, reject }; });
      this.pending = entry;
      const go = () => {
        if (this.pending !== entry) return;
        this.pending = null;
        this.started = true;
        if (this.listeners.playing) this.listeners.playing({ type: 'playing' });
        entry.resolve();
      };
      if (t.holdMedia) t.mediaQueue.push(go); else Promise.resolve().then(go);
      return promise;
    },
    load() { this.loads++; },
    removeAttribute(n) { if (n === 'src') this.src = ''; },
    setAttribute(n, v) { if (n === 'src') this.src = v; },
    remove() { this.attached = false; },
    addEventListener(type, fn) { this.listeners[type] = fn; } };
  t.audios.push(a);
  return a;
}
async function mediaPage() {
  const t = await alertsPage();
  t.audios = [];
  t.mediaQueue = [];
  t.holdMedia = false;
  t.startMedia = () => { for (const go of t.mediaQueue.splice(0)) go(); };
  const body = t.win.document.body;
  body.append = n => body.appendChild(n);
  const appendChild = body.appendChild;
  body.appendChild = n => { if (n.tagName === 'AUDIO') { n.attached = true; return n; } return appendChild(n); };
  const create = t.win.document.createElement;
  t.win.document.createElement = tag => (tag === 'audio' ? mediaModel(t) : create(tag));
  const qs = t.win.document.querySelector, qsa = t.win.document.querySelectorAll;
  const live = () => t.audios.filter(a => a.attached);
  t.win.document.querySelector = sel => (sel === '#search-audio' ? live().find(a => a.id === 'search-audio') || null : qs(sel));
  t.win.document.querySelectorAll = sel => (/^audio/.test(sel) ? live().filter(a => sel === 'audio' || (/data-clip/.test(sel) && a.dataset.clip)) : qsa(sel));
  t.win.document.getElementById = id => live().find(a => a.id === id) || null;
  const orig = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => (/\/customer\/records\/sr1\.[^/]*AUDIO[^/]*$/.test(url)
    ? reply(200, { ok: true, record: { media: { playback: 'local' }, selected_bounds: { start_s: 10, end_s: 50 } }, workspace_context: t.E.current.workspaceContext })()
    : orig(url, opts));
  return t;
}
// Audible: on the page, not paused, its media answered.
const audible = (t, ref) => t.audios.some(a => a.attached && !a.paused && a.started && a.src.includes(ref));
// Search play: the record read answers, the private src is set and
// play() is asked for; the media has not answered yet.
async function playRequested(t) {
  t.holdMedia = true;
  t.playOut = playClick(t, PRIVATE_AUDIO).catch(e => e);
  await drain(10);
  const player = t.audios.find(a => a.id === 'search-audio');
  assert.ok(player && player.attached && player.plays === 1, 'fixture: play() was asked for');
  assert.match(player.src, /OWNER-PRIVATE-AUDIO/, 'fixture: with the owner\'s media');
  assert.equal(audible(t, PRIVATE_AUDIO), false, 'fixture: not started yet');
  return player;
}
// An inline clip player on the page.
function clipPlayer(t, id = 'x') {
  const a = mediaModel(t);
  a.id = 'clip-' + id;
  a.dataset.clip = id;
  a.src = '/api/v1/customer/records/sr1.CLIP-AUDIO/media#t=10.0';
  a.attached = true;
  return a;
}
const clipToggle = (t, id = 'x') => t.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: id }, closest: () => null });
const surface = t => surfaces(t)[0] || null;
const checkButton = t => { const m = /<button[^>]*data-action="return-check"[^>]*>/.exec(surface(t) ? surface(t).innerHTML : ''); return m ? m[0] : ''; };

// ============================================================================
// Decision 1: a clip that has not started waits for the return read
// ============================================================================
test('P8: a clip asked for but not started when the tab is hidden does not start during the pending check; another person: it never starts and is torn down', async () => {
  const t = await mediaPage();
  const player = await playRequested(t);
  const session = await heldReturn(t, toOther);
  t.startMedia();
  await drain(20);
  assert.equal(audible(t, PRIVATE_AUDIO), false, 'the clip started while the return read is pending');
  session.release(0, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(60);
  t.startMedia();
  await drain(20);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
  assert.equal(audible(t, PRIVATE_AUDIO), false, 'the previous person\'s clip played for the other person');
  stoppedAndGone(player, 'the previous person\'s clip');
  assert.deepEqual(t.toasts.filter(m => /did not start/.test(m)), [], 'the cancelled clip said it failed');
});
test('P8: signed out on the return: the clip asked for never starts and is torn down', async () => {
  const t = await mediaPage();
  const player = await playRequested(t);
  const session = await heldReturn(t, x => { x.srv.signedOut = true; });
  t.startMedia();
  await drain(20);
  assert.equal(audible(t, PRIVATE_AUDIO), false, 'the clip started while the return read is pending');
  session.release(0, reply(401, SIGNED_OUT));
  await drain(60);
  t.startMedia();
  await drain(20);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.equal(audible(t, PRIVATE_AUDIO), false, 'the clip played on the signed-out tab');
  stoppedAndGone(player, 'the clip');
});
test('P8 control: the same person confirmed: the clip asked for starts then', async () => {
  const t = await mediaPage();
  await playRequested(t);
  const session = await heldReturn(t);
  t.startMedia();
  await drain(20);
  assert.equal(audible(t, PRIVATE_AUDIO), false, 'the clip started while the return read is pending');
  t.holdMedia = false;
  session.release(0, reply(200, { ...HOSTED }));
  await drain(60);
  assert.equal(audible(t, PRIVATE_AUDIO), true, 'the same person\'s clip did not start after the confirmation');
  assert.deepEqual(t.toasts.filter(m => /did not start/.test(m)), [], 'the held clip said it failed');
});
test('decision 1: a clip already playing keeps playing across hide and return', async () => {
  const t = await mediaPage();
  await playClick(t, PRIVATE_AUDIO);
  await drain(10);
  assert.equal(audible(t, PRIVATE_AUDIO), true, 'fixture: the clip plays');
  const player = t.audios.find(a => a.id === 'search-audio');
  const pauses = player.pauses;
  const session = await heldReturn(t);
  assert.equal(audible(t, PRIVATE_AUDIO), true, 'a playing clip was stopped when the tab was hidden');
  session.release(0, reply(200, { ...HOSTED }));
  await drain(60);
  assert.equal(audible(t, PRIVATE_AUDIO), true, 'a playing clip was stopped across the return');
  assert.equal(player.pauses, pauses, 'the playing clip was paused');
});
test('decision 1: a clip play asked for while the hold is engaged waits; another person: never', async () => {
  const t = await mediaPage();
  const clip = clipPlayer(t);
  const session = await heldReturn(t, toOther);
  await clipToggle(t);
  await drain(20);
  assert.equal(clip.started || (!clip.paused && clip.plays > 0), false, 'the clip started while held');
  session.release(0, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(60);
  assert.equal(clip.started, false, 'the previous person\'s clip played for the other person');
  assert.equal(clip.attached, false, 'the clip was not torn down');
});
test('decision 1 control: a clip play asked for while held, the same person: it starts after the confirmation', async () => {
  const t = await mediaPage();
  const clip = clipPlayer(t);
  const session = await heldReturn(t);
  await clipToggle(t);
  await drain(20);
  assert.equal(clip.plays, 0, 'play() was asked for while held');
  session.release(0, reply(200, { ...HOSTED }));
  await drain(60);
  assert.equal(clip.started && !clip.paused, true, 'the same person\'s clip did not start after the confirmation');
});

// ============================================================================
// Decision 3: "Checking…" while Check again reads
// ============================================================================
test('decision 3: after a failed confirmation the dialog offers an enabled Check again', async () => {
  const { t } = await stuckTab();
  recoveryOnly(t, 'failed confirmation');
  assert.doesNotMatch(surface(t).innerHTML, /Checking…/, 'says Checking before any check');
  assert.doesNotMatch(checkButton(t), /\sdisabled|aria-busy="true"/, 'Check again is not usable');
});
test('decision 3: while Check again reads, the dialog says "Checking…" and its button is disabled and busy', async () => {
  const { t, session } = await stuckTab();
  await checkAgain(t);
  await drain(10);
  assert.equal(session.count(), 3, 'fixture: one recovery read in flight');
  assert.equal(noticeCount(t), 1, 'not exactly one recovery dialog while checking');
  assert.match(surface(t).innerHTML, /Checking…/, 'no "Checking…" while the read is in flight');
  assert.doesNotMatch(surface(t).innerHTML, NOTICE_RE, 'still says it could not confirm while checking');
  assert.match(checkButton(t), /\sdisabled(?=[\s>=])/, 'the button stays enabled while the read is in flight');
  assert.match(checkButton(t), /aria-busy="true"/, 'the button is not marked busy');
});
test('decision 3: the read fails: back to "Couldn\'t confirm your session." with an enabled Check again', async () => {
  const { t, session } = await stuckTab();
  await checkAgain(t);
  await drain(10);
  session.release(2, lostConnection);
  await drain(60);
  recoveryOnly(t, 'after a failed Check again');
  assert.doesNotMatch(surface(t).innerHTML, /Checking…/, 'still says Checking after the failure');
  assert.doesNotMatch(checkButton(t), /\sdisabled|aria-busy="true"/, 'Check again stays disabled after the failure');
});

// ============================================================================
// Decision 4: the recovery dialog's own style
// ============================================================================
test('decision 4: the customer stylesheet has a small rule for the recovery dialog, on existing tokens', () => {
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  const rules = [...css.matchAll(/([^{}]*\.ew-return-check[^{}]*)\{([^}]*)\}/g)];
  assert.ok(rules.length > 0, 'no rule for .ew-return-check in app/styles.css');
  const body = rules.map(r => r[2]).join(';');
  assert.match(body, /width\s*:/, 'the recovery dialog has no width of its own');
  assert.doesNotMatch(body, /#[0-9a-f]{3,8}\b|rgba?\(/i, 'new colours instead of the existing tokens');
  assert.doesNotMatch(body, /url\(/, 'a new asset');
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  assert.match(html, /\/app\/styles\.css/, 'fixture: the customer app loads styles.css');
});
test('decision 4: the recovery dialog carries that class', async () => {
  const { t } = await stuckTab();
  assert.equal(surface(t).className, 'ew-return-check');
});
