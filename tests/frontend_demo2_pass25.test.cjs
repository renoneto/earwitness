/* DEMO2 pass 25 (owner decisions on review r21 and Astra r14).
   1. A page that loads hidden or unfocused counts one departure at load.
      A departure or return during startup is remembered; its return read
      runs once, as soon as startup finishes (R21-01).
   2. The hold starts at a hidden or pagehide departure: every non-session
      answer that arrives from then until the return read answers waits
      unpainted, in-flight clip loads included. A blur alone holds nothing
      (accepted); it still counts as a departure (R21-02, SEC-R14-01).
   3. After failed return reads, the `online` event, an in-app navigation
      or "Check again" sends one new session read. The notice "Couldn't
      confirm your session." with "Check again" is shown; no timeout
      releases anything (R21-03).
   4. An unowned history entry (no tag, null state, mismatched tag) never
      restores queries, Back labels, scroll or stashed form values; it
      renders fresh, a same-route restore included (SEC-R14-02, -03).
   5. An explicit sign-out advances the per-tab epoch before any reload or
      render (SEC-R14-04).
   6. "Switched to …" only after the session is adopted, on every
      ordering (SEC-R14-05).
   7. At most one return read in flight; a newer one supersedes the older
      (R21-N2 accepted).
   Plus the R21-N1 pins: the 401/403/409 change signal, Settings' own
   session read settling the hold (release and drop), and the hold on a
   back-forward cache restore.
   Fixtures are the pass 24b ones, with a load-time visibility hook.
   Run: node --test tests/frontend_demo2_pass25.test.cjs */
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
// Pass 26 (R22-N1, decision 3): a browser-like document. The body's children, the top layer
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
  E.toast = m => { toasts.push(m); toastEl.textContent = m; };
  if (!B) { win.location.hash = route; E.current.route = route; }
  // A page that loads hidden or with its session read held: its render waits.
  if (at) win.EWViews.render(); else await win.EWViews.render();
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
// Pass 25 helpers
// ============================================================================
const UNSEEN_RE = new RegExp(UNSEEN);
const NOTICE_RE = /Couldn(?:'|&#39;)t confirm your session\./;
const CHECK_AGAIN_RE = /data-action="return-check"[^>]*>Check again</;
// Pass 26 (decision 3): the notice is a surface of its own, above the
// concealed page; counted like a browser (R22-N1).
const surfaces = t => t.win.document.body.children.filter(el => el.attrs && 'data-return-check' in el.attrs && el.open);
const noticeShown = t => surfaces(t).map(el => el.innerHTML).join('');
const noticeCount = t => surfaces(t).length + t.main.querySelectorAll('[data-return-check]').length;
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
// R21-01: a page that loads hidden or unfocused counts one departure
// ============================================================================
for (const [variant, change, want] of [
  ['another person signed in', t => { t.srv.session = { ...HOSTED, ...OTHER_SESSION }; }, OTHER],
  ['signed out', t => { t.srv.signedOut = true; }, null],
]) {
  test(`R21-01: a tab opened in the background (loads hidden), ${variant} elsewhere: the first look reads the session`, async () => {
    const t = await page('#/alerts', { at: { hidden: true } });
    const sent = sentLog(t.win);
    change(t);
    returnTab(t, 'visibilitychange');
    returnTab(t);
    await drain(80);
    assert.ok(sessionReads(sent) >= 1, 'the first look at a tab opened in the background read nothing');
    assert.equal(t.E.current.principal, want, 'the tab kept the previous person');
  });
}
test('R21-01 control: a tab opened in the background, the same person: one read at the first look, then the page publishes', async () => {
  const t = await page('#/alerts', { at: { hidden: true } });
  const sent = sentLog(t.win);
  assert.notEqual(t.E.current.publishedRoute, '#/alerts', 'the page published while it loaded hidden');
  returnTab(t, 'visibilitychange');
  returnTab(t);
  fireWindow(t, 'pageshow', { persisted: false });
  await drain(80);
  assert.equal(sessionReads(sent), 1, 'not exactly one read at the first look');
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(t.E.current.publishedRoute, '#/alerts', 'the page did not publish after the return read');
});
test('R21-01: the load\'s own pageshow of a tab opened in the background is no return; the first look reads once', async () => {
  const t = await page('#/alerts', { at: { hidden: true } });
  const sent = sentLog(t.win);
  fireWindow(t, 'pageshow', { persisted: false });
  fireWindow(t, 'focus');
  await drain(40);
  assert.equal(sessionReads(sent), 0, 'a read was sent while the tab is still hidden');
  assert.notEqual(t.E.current.publishedRoute, '#/alerts', 'the page published while hidden');
  returnTab(t, 'visibilitychange');
  await drain(80);
  assert.equal(sessionReads(sent), 1, 'not exactly one read at the first look');
  assert.equal(t.E.current.publishedRoute, '#/alerts');
});
test('R21-01: a page that loads visible but unfocused counts a departure: the first focus reads the session', async () => {
  const t = await page('#/alerts', { at: { unfocused: true } });
  const sent = sentLog(t.win);
  // Pass 26 (R22-01): a browser fires the load's own pageshow.
  fireWindow(t, 'pageshow', { persisted: false });
  await drain(40);
  assert.equal(t.E.current.publishedRoute, '#/alerts', 'fixture: a visible page publishes (a blur holds nothing)');
  assert.equal(sessionReads(sent), 0, 'the load\'s own pageshow read the session');
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  returnTab(t);
  await drain(80);
  assert.equal(sessionReads(sent), 1, 'the first focus of a page loaded unfocused did not read once');
  assert.equal(t.E.current.principal, OTHER);
});
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
test('R21-01 (P5): a departure and a return while the startup read is in flight: one return read once startup ends', async () => {
  const boot = heldStartup();
  const t = await page('#/alerts', { at: boot.at });
  assert.ok(boot.release, 'fixture: the startup session read is in flight');
  const sent = sentLog(t.win);
  leaveTab(t);
  t.srv.signedOut = true;
  returnTab(t, 'visibilitychange');
  returnTab(t);
  await drain(10);
  boot.release();
  await drain(80);
  assert.ok(sessionReads(sent) >= 1, 'no session read after startup ended');
  assert.equal(t.E.current.principal, null, 'the startup answer, older than the departure, stays adopted');
});
test('R21-01 (P5) control: the same person: exactly one return read after startup, then the page publishes', async () => {
  const boot = heldStartup();
  const t = await page('#/alerts', { at: boot.at });
  const sent = sentLog(t.win);
  blurTab(t);
  returnTab(t);
  await drain(10);
  assert.equal(sessionReads(sent), 0, 'a return read was sent before startup ended');
  boot.release();
  await drain(80);
  assert.equal(sessionReads(sent), 1, 'not exactly one return read after startup');
  assert.equal(t.E.current.publishedRoute, '#/alerts');
});
test('R21-01: a departure during startup and still hidden when it ends: no read until the return, then one', async () => {
  const boot = heldStartup();
  const t = await page('#/alerts', { at: boot.at });
  const sent = sentLog(t.win);
  leaveTab(t);
  boot.release();
  await drain(60);
  assert.equal(sessionReads(sent), 0, 'a read was sent while the tab is still hidden');
  assert.notEqual(t.E.current.publishedRoute, '#/alerts', 'the page published while hidden');
  returnTab(t, 'visibilitychange');
  await drain(60);
  assert.equal(sessionReads(sent), 1, 'not exactly one read at the return');
  assert.equal(t.E.current.publishedRoute, '#/alerts');
});

// ============================================================================
// R21-02 / SEC-R14-01: the hold starts at a hidden or pagehide departure
// ============================================================================
test('R21-02 / SEC-R14-01: a record answer that lands while the tab is hidden is not painted; another person: never', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted while the tab was hidden');
  const session = holdEach(t.win, isSessionRead);
  returnTab(t, 'visibilitychange');
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'on screen at the return, before the return read answered');
  session.release(0, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(80);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'the previous person\'s record was painted for the other person');
});
test('R21-02: signed out elsewhere while hidden: the answer that landed while hidden is dropped', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  t.srv.signedOut = true;
  record.release(0, unseenRecord);
  await drain(40);
  returnTab(t, 'visibilitychange');
  await drain(80);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'the held record was painted on the signed-out tab');
});
test('R21-02 control: the same person: the answer that landed while hidden is painted once the return read confirms', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted while the tab was hidden');
  const session = holdEach(t.win, isSessionRead);
  returnTab(t, 'visibilitychange');
  await drain(20);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted before the return read answered');
  session.release(0, reply(200, { ...HOSTED }));
  await drain(80);
  assert.match(t.main.shown(), UNSEEN_RE, 'the same person\'s record was not painted after the confirmation');
});
test('R21-02: another person adopted while the tab is hidden: the previous person\'s held answer is dropped; the new person\'s page publishes after the return read', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  leaveTab(t);
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  record.release(0, unseenRecord);
  await drain(20);
  await t.win.EWViews.reconcileCanonical();
  await drain(60);
  assert.equal(t.E.current.principal, OTHER, 'fixture: a session read while hidden adopted the other person');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'the previous person\'s record was painted');
  assert.equal(record.count(), 2, 'fixture: the other person\'s page read the record');
  record.release(1, reply(200, { record: { record_id: 'OTHER-OWN-RECORD' }, workspace_context: 'ctx-other' }));
  await drain(40);
  assert.notEqual(t.E.current.publishedToken, t.E.current.fetchToken, 'the other person\'s page published while hidden');
  returnTab(t, 'visibilitychange');
  await drain(80);
  assert.equal(t.E.current.principal, OTHER);
  assert.match(t.main.shown(), /OTHER-OWN-RECORD/, 'the other person\'s own record was dropped');
  assert.equal(t.E.current.publishedToken, t.E.current.fetchToken, 'the other person\'s page did not publish after the return read');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'the previous person\'s record was painted for the other person');
});
test('R21-02: a pagehide departure holds too', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  fireWindow(t, 'pagehide', { persisted: false });
  record.release(0, unseenRecord);
  await drain(40);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted after a pagehide departure');
  const session = holdEach(t.win, isSessionRead);
  returnTab(t);
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted before the return read answered');
  session.release(0, reply(200, { ...HOSTED }));
  await drain(80);
  assert.match(t.main.shown(), UNSEEN_RE, 'the same person\'s record was not painted after the confirmation');
});
test('R21-02: a clip still loading when the tab hides starts only after the return read confirms', async () => {
  const t = await audioPage();
  const record = holdEach(t.win, isRecord('sr1.OLD'));
  const play = playClick(t, 'sr1.OLD');
  await drain(10);
  assert.equal(record.count(), 1, 'fixture: the clip\'s record read was sent');
  leaveTab(t);
  record.release(0, clipAnswer);
  await drain(30);
  assert.equal(clipStarted(t, 'sr1.OLD'), false, 'the clip started while the tab was hidden');
  const session = holdEach(t.win, isSessionRead);
  returnTab(t, 'visibilitychange');
  await drain(20);
  assert.equal(clipStarted(t, 'sr1.OLD'), false, 'the clip started before the return read answered');
  session.release(0, reply(200, { ...HOSTED }));
  await play.catch(() => {});
  await drain(60);
  assert.equal(clipStarted(t, 'sr1.OLD'), true, 'the same person\'s clip did not start after the return');
});
test('R21-02: a clip still loading when the tab hides, another person on the return: it never starts', async () => {
  const t = await audioPage();
  const record = holdEach(t.win, isRecord('sr1.OLD'));
  const play = playClick(t, 'sr1.OLD');
  await drain(10);
  leaveTab(t);
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  record.release(0, clipAnswer);
  await drain(30);
  assert.equal(clipStarted(t, 'sr1.OLD'), false, 'the clip started while the tab was hidden');
  returnTab(t, 'visibilitychange');
  await play.catch(() => {});
  await drain(80);
  assert.equal(t.E.current.principal, OTHER);
  assert.equal(clipStarted(t, 'sr1.OLD'), false, 'the previous person\'s clip plays for the other person');
});
test('R21-02 control (accepted): a blur alone holds nothing: an answer before the focus is painted', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  blurTab(t);
  record.release(0, unseenRecord);
  await drain(40);
  assert.match(t.main.shown(), UNSEEN_RE, 'a blur without hidden held the answer');
  assert.equal(t.win.EWViews.tabReturnGate(), null, 'a blur alone opened the hold');
});

// ============================================================================
// R21-03: after failed return reads: online, navigation, Check again
// ============================================================================
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
test('R21-03: after both return reads fail, the page says so and offers Check again; nothing is released', async () => {
  const { t, timers, session } = await stuckTab();
  assert.equal(noticeCount(t), 1, 'not exactly one notice');
  assert.match(noticeShown(t), NOTICE_RE, 'no notice that the session could not be confirmed');
  assert.match(noticeShown(t), CHECK_AGAIN_RE, 'no Check again');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'released after a sign of a change');
  timers.run();
  await drain(40);
  assert.equal(session.count(), 2, 'a read was sent on its own');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'a timeout released the held answers');
});
test('R21-03: the browser comes back online: one new session read; the same person\'s page publishes', async () => {
  const { t, session } = await stuckTab();
  fireWindow(t, 'online');
  await drain(20);
  assert.equal(session.count(), 3, 'the online event did not send one new session read');
  fireWindow(t, 'online');
  await drain(20);
  assert.equal(session.count(), 3, 'more than one read in flight');
  session.release(2, reply(200, { ...HOSTED }));
  await drain(80);
  assert.match(t.main.shown(), UNSEEN_RE, 'the same person\'s page did not publish');
  assert.equal(noticeCount(t), 0, 'the notice stays after the confirmation');
  assert.equal(t.win.EWViews.tabReturnGate(), null);
});
test('R21-03: an in-app navigation sends one new session read; the page publishes once it confirms', async () => {
  const { t, session } = await stuckTab();
  await go(t, '#/brands');
  assert.equal(session.count(), 3, 'the navigation did not send one new session read');
  assert.notEqual(t.E.current.publishedToken, t.E.current.fetchToken, 'published before the read confirmed');
  session.release(2, reply(200, { ...HOSTED }));
  await drain(80);
  assert.equal(t.E.current.publishedToken, t.E.current.fetchToken, 'the navigation stays on its loading frame');
  assert.equal(t.E.current.publishedRoute, '#/brands');
});
test('R21-03: Check again sends one new session read; it fails again: the notice comes back, nothing is released', async () => {
  const { t, session } = await stuckTab();
  await checkAgain(t);
  await drain(20);
  assert.equal(session.count(), 3, 'Check again did not send one new session read');
  await checkAgain(t);
  await drain(20);
  assert.equal(session.count(), 3, 'a second Check again sent another read while one is in flight');
  session.release(2, lostConnection);
  await drain(60);
  assert.equal(noticeCount(t), 1, 'the notice did not come back exactly once');
  assert.match(noticeShown(t), NOTICE_RE, 'the notice did not come back');
  assert.equal(everPainted(t, UNSEEN_RE), false, 'released without a confirmation');
  await checkAgain(t);
  await drain(20);
  assert.equal(session.count(), 4);
  session.release(3, reply(200, { ...HOSTED }));
  await drain(80);
  assert.match(t.main.shown(), UNSEEN_RE, 'the same person\'s page did not publish');
});
test('R21-03: Check again finds another person: the held answers are dropped', async () => {
  const { t, session } = await stuckTab();
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  await checkAgain(t);
  await drain(20);
  assert.equal(session.count(), 3, 'Check again did not send one new session read');
  session.release(2, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(80);
  assert.equal(t.E.current.principal, OTHER);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'the previous person\'s record was painted for the other person');
});

// ============================================================================
// Owner decision 7 (R21-N2 accepted): at most one return read in flight
// ============================================================================
test('R21-N2: three departures and returns: one read each, at most one in flight; answers wait for the newest', async () => {
  const t = await alertsPage();
  const record = await evidenceHeld(t);
  const session = holdAbortable(t.win, isSessionRead);
  for (let i = 0; i < 3; i++) { leaveTab(t); returnTab(t, 'visibilitychange'); await drain(5); }
  assert.equal(session.count(), 3, 'each departure and return costs one session read');
  assert.ok(session.maxOpen() <= 1, `more than one return read in flight: ${session.maxOpen()}`);
  record.release(0, unseenRecord);
  await drain(20);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted before the newest return read answered');
  session.release(2, reply(200, { ...HOSTED }));
  await drain(80);
  assert.match(t.main.shown(), UNSEEN_RE, 'the newest read confirmed the same person, but the answer still waits');
});

// ============================================================================
// R21-N1 pins
// ============================================================================
for (const status of [401, 403, 409]) {
  test(`R21-N1 pin: a held ${status} is a sign of a change: two failed return reads release nothing`, async () => {
    const { t, timers } = await stuckTab(status);
    timers.run();
    await drain(40);
    assert.equal(everPainted(t, UNSEEN_RE), false, `released after a held ${status}`);
  });
}
// A person with no active workspace: the page is the chooser, whose own
// session read (accessSection) is issued with no other read before it.
// One read and one write answer are held for the return read.
const CHOOSER = { ...HOSTED, active_workspace: '', workspace_context: '' };
async function chooserWithHeldAnswers() {
  const t = await page('#/settings', { session: CHOOSER });
  const timers = longTimers(t);
  leaveTab(t);
  const session = holdEach(t.win, isSessionRead);
  const other = holdEach(t.win, url => url === '/api/v1/customer/brands' || url === '/api/v1/customer/alerts');
  returnTab(t);
  await drain(20);
  assert.equal(session.count(), 1, 'fixture: the return read is pending');
  // The render first: leaving the page on screen drops the reads in
  // flight before it.
  const rendering = t.win.EWViews.render();
  await drain(20);
  assert.equal(session.count(), 2, 'fixture: the page\'s own session read is pending, newer than the return read');
  const out = { read: null, write: null };
  t.E.api('/customer/brands').then(r => { out.read = { r }; }, e => { out.read = { e }; });
  t.E.api('/customer/alerts', { method: 'POST', body: { name: 'HELD' }, displayedRevision: 7 }).then(r => { out.write = { r }; }, e => { out.write = { e }; });
  await drain(5);
  assert.equal(other.count(), 2, 'fixture: the held read and write were sent');
  other.release(0, reply(200, { brands: [{ id: 'HELD' }] }));
  other.release(1, reply(200, { alert: { id: 9 } }));
  await drain(10);
  assert.deepEqual(out, { read: null, write: null }, 'fixture: the answers are held');
  return { t, timers, session, rendering, out };
}
test('R21-N1 pin: the page\'s own session read (accessSection), newer than the return read, releases at its adoption', async () => {
  const c = await chooserWithHeldAnswers();
  c.session.release(1, reply(200, { ...CHOOSER }));
  await drain(40);
  assert.ok(c.out.read && c.out.read.r, `the held read waits after the newer read adopted the same person: ${JSON.stringify(c.out.read)}`);
  assert.ok(c.out.write && c.out.write.r, `the held write waits after the newer read adopted the same person: ${JSON.stringify(c.out.write)}`);
  assert.equal(c.timers.pending(), 0, 'a follow-up read was scheduled');
  c.session.release(0, reply(200, { ...CHOOSER }));
  await drain(40);
});
test('R21-N1 pin: the page\'s own session read finds the session signed out: the held answers are dropped', async () => {
  const c = await chooserWithHeldAnswers();
  c.session.release(1, reply(401, SIGNED_OUT));
  await drain(10);
  // Pass 26 (owner decision 1): the 401 starts the signed-out follow-up,
  // whose session read is newer: that newest read decides.
  for (let i = 2; i < c.session.count(); i++) c.session.release(i, reply(401, SIGNED_OUT));
  await drain(40);
  assert.ok(c.out.read && c.out.write, `the held answers still wait after the newer read found the session signed out: ${JSON.stringify(c.out)}`);
  assert.ok(c.out.read.e && c.out.read.e.droppedContext, `the held read was released on a signed-out session: ${JSON.stringify(c.out.read)}`);
  assert.ok(c.out.write.e && c.out.write.e.droppedContext, `the held write's answer was released on a signed-out session: ${JSON.stringify(c.out.write)}`);
  assert.equal(c.timers.pending(), 0, 'a follow-up read was scheduled');
  c.session.release(0, reply(401, SIGNED_OUT));
  await drain(40);
});
test('R21-N1 pin: a back-forward cache restore (no pagehide seen) holds its own answers until the return read', async () => {
  const t = await alertsPage();
  browser(t);
  await go(t, EVIDENCE);
  const session = holdEach(t.win, isSessionRead);
  const record = holdEach(t.win, isRecord('sr1.R12-PIN'));
  fireWindow(t, 'pageshow', { persisted: true });
  await drain(20);
  assert.equal(record.count(), 1, 'fixture: the restore read the record again');
  // The restore's other reads still answer for the previous context: only
  // the hold keeps the record back.
  record.release(0, unseenRecord);
  await drain(30);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'painted before the return read answered');
  assert.ok(session.count() >= 1, 'the restore did not read the session');
  t.srv.session = { ...HOSTED, ...OTHER_SESSION };
  for (let i = 0; i < session.count(); i++) session.release(i, reply(200, { ...HOSTED, ...OTHER_SESSION }));
  await drain(80);
  assert.equal(t.E.current.principal, OTHER);
  assert.equal(everPainted(t, UNSEEN_RE), false, 'the previous person\'s record was painted for the other person');
});

// ============================================================================
// SEC-R14-02: a same-route restore with null state renders fresh
// ============================================================================
test('SEC-R14-02: a same-URL popstate with null state: the previous Back query leaves the page', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  assert.match(t.main.innerHTML, LABEL, 'fixture: the owner sees their own origin');
  B.cur().state = null;
  const scrolls = [];
  t.E.current.scroll.set(EVIDENCE, 777);
  t.win.scrollTo = (x, y) => scrolls.push(y);
  for (const fn of t.win.listeners.popstate || []) fn({ state: null });
  await drain(60);
  assert.equal(t.E.current.route, EVIDENCE, 'fixture: still the evidence page');
  noSecret(t.main.shown(), 'a null-state restore kept the previous Back query on screen');
  assert.ok(!scrolls.includes(777), 'a null-state restore restored the scroll position');
});
test('SEC-R14-02 control: a same-URL popstate with the entry\'s own state keeps the Back label', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  for (const fn of t.win.listeners.popstate || []) fn({ state: clone(B.cur().state) });
  await drain(60);
  assert.match(t.main.innerHTML, LABEL, 'an owned restore lost the Back label');
});
test('SEC-R14-02 control: the skip link (#main) is no route change', async () => {
  const t = await alertsPage();
  browser(t);
  await go(t, EVIDENCE);
  const paints = t.main.painted.length;
  t.win.location.hash = '#main';
  for (const fn of t.win.listeners.popstate || []) fn({ state: null });
  for (const fn of t.win.listeners.hashchange || []) fn({});
  await drain(40);
  assert.equal(t.main.painted.length, paints, 'the skip link re-rendered the page');
});

// ============================================================================
// SEC-R14-03: an unowned entry refills no stashed form value
// ============================================================================
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
// Settings with a typed name, then Alerts through a browser hash entry
// (the draft is stashed).
async function settingsDraft(opts) {
  const t = await page('#/settings', opts);
  const B = browser(t);
  t.srv.prefs = SERVER_NAME;
  const inner = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => (url === '/api/v1/customer/preferences'
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
  t.form.field.value = DRAFT;
  t.form.dataset.dirty = '1';
  const browse = async url => {
    t.win.location.hash = url;
    for (const fn of t.win.listeners.popstate || []) fn({ state: null });
    for (const fn of t.win.listeners.hashchange || []) fn({});
    await drain(60);
  };
  await browse('#/alerts');
  assert.equal(t.E.current.publishedRoute, '#/alerts', 'fixture: Alerts');
  return { t, B, browse };
}
const freshSettings = (t, why) => {
  assert.equal(t.E.current.publishedRoute, '#/settings', `fixture: Settings published (${why})`);
  assert.ok(t.form, `fixture: the name form (${why})`);
  assert.equal(t.form.field.value, SERVER_NAME, `the stashed value was refilled (${why})`);
  assert.notEqual(t.form.dataset.dirty, '1', `the form is marked as a draft (${why})`);
  assert.doesNotMatch(t.form.box.innerHTML, /restored/, `a restored-entry notice (${why})`);
};
test('SEC-R14-03: a new entry with null state for Settings does not refill the stashed draft', async () => {
  const { t, browse } = await settingsDraft();
  await browse('#/settings');
  freshSettings(t, 'null state');
});
for (const [mode, tamper] of [
  ['a missing owner tag', st => { delete st.earWitnessOwner; }],
  ['another principal on the tag', st => { st.earWitnessOwner.principal = OTHER; }],
  ['another tab on the tag', st => { st.earWitnessOwner.tab = 'another-tab'; }],
]) {
  test(`SEC-R14-03: Back to the Settings entry with ${mode} does not refill the stashed draft`, async () => {
    const { t, B } = await settingsDraft();
    const st = B.entries[B.i - 1].state;
    assert.ok(st && st.earWitnessOwner, 'fixture: the Settings entry was tagged');
    tamper(st);
    await B.traverse(-1);
    freshSettings(t, mode);
  });
}
// FS-R2-01 (BL-033): an address arrival the browser announces (Navigation
// API push) is a new entry too: it opens fresh and refills no stashed draft.
test('SEC-R14-03 / BL-033: an announced address arrival at Settings does not refill the stashed draft', async () => {
  const nav = [];
  const navigation = { addEventListener: (type, fn) => { if (type === 'navigate') nav.push(fn); } };
  // URL: the listener reads the destination's hash with it.
  const { t, browse } = await settingsDraft({ at: { fetch: sb => { sb.navigation = navigation; sb.URL = URL; } } });
  assert.equal(nav.length, 1, 'fixture: the app listens for navigate');
  for (const fn of nav) fn({ navigationType: 'push', hashChange: true, destination: { url: 'http://127.0.0.1:9153/app/#/settings' } });
  await browse('#/settings');
  freshSettings(t, 'announced arrival');
});
test('SEC-R14-03 control: Back to the owned Settings entry refills the same person\'s draft', async () => {
  const { t, B } = await settingsDraft();
  await B.traverse(-1);
  assert.equal(t.E.current.publishedRoute, '#/settings', 'fixture: Settings published');
  assert.equal(t.form.field.value, DRAFT, 'an owned entry lost the same person\'s draft');
  assert.equal(t.form.dataset.dirty, '1');
});

// ============================================================================
// SEC-R14-04: an explicit sign-out advances the epoch before any render
// ============================================================================
test('SEC-R14-04: a signed-out render overlapping an explicit sign-out: the epoch still advances; Back restores nothing', async () => {
  const storage = memStorage();
  const t = await page('#/alerts', { storage });
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  const before = JSON.parse(storage.data.earWitnessTabEpoch).n;
  const nav = captureLocation(t.win);
  const logout = holdEach(t.win, isLogout);
  const out = signoutClick(t);
  await drain(5);
  assert.equal(logout.count(), 1, 'fixture: the sign-out was sent');
  t.srv.signedOut = true;
  await t.E.boot().catch(() => {});
  await t.win.EWViews.render();
  await drain(40);
  assert.equal(t.E.current.principal, null, 'fixture: the overlapping read adopted signed out');
  logout.release(0, reply(200, { ok: true }));
  await out.catch(() => {});
  await drain(20);
  assert.equal(nav.reloads.length, 1, 'fixture: one reload');
  assert.ok(JSON.parse(storage.data.earWitnessTabEpoch).n > before, 'the explicit sign-out did not advance the tab epoch');
  const out1 = await reload(B, storage, null);
  assert.equal(out1.E.current.principal, null, 'fixture: signed out after the reload');
  B.land('#/overview');
  const a = await reload(B, storage);
  assert.equal(a.E.current.principal, OWNER, 'fixture: the same person signed in again');
  while (B.i > 1) await B.traverse(-1);
  assert.match(a.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(a.E.current.route, 'the search from before the explicit sign-out was restored');
});
test('SEC-R14-04: the epoch advances before the sign-out answers', async () => {
  const storage = memStorage();
  const t = await page('#/alerts', { storage });
  const before = JSON.parse(storage.data.earWitnessTabEpoch).n;
  captureLocation(t.win);
  const logout = holdEach(t.win, isLogout);
  const out = signoutClick(t);
  await drain(5);
  assert.ok(JSON.parse(storage.data.earWitnessTabEpoch).n > before, 'the epoch waits for the sign-out answer');
  logout.release(0, reply(200, { ok: true }));
  await out.catch(() => {});
});

// ============================================================================
// SEC-R14-05: "Switched to …" only after the newer read is adopted
// ============================================================================
async function switchGap(gap, newerAnswer) {
  const t = await alertsPage();
  switchServer(t);
  const session = holdEach(t.win, isSessionRead);
  const seen = [];
  t.E.toast = m => { seen.push({ m, ws: t.E.current.activeWorkspace }); };
  const switching = chooseWs(t, 'ws-b');
  await drain(30);
  assert.equal(session.count(), 1, 'fixture: the switch\'s confirming read is pending');
  const newer = t.E.boot().catch(() => {});
  await drain(5);
  assert.equal(session.count(), 2, 'fixture: a newer page read is pending');
  session.release(0, reply(200, { ...HOSTED, ...WS_B }));
  for (let i = 0; i < gap; i++) await Promise.resolve();
  session.release(1, newerAnswer ? reply(200, newerAnswer) : lostConnection);
  await newer;
  await switching;
  await drain(80);
  return { t, seen };
}
test('SEC-R14-05: whatever the gap between the two answers, no "Switched to ws-b." when the newer read lands in ws', async () => {
  const early = [];
  for (let gap = 0; gap <= 12; gap++) {
    const { t, seen } = await switchGap(gap, { ...HOSTED, workspace_switch_revision: 9 });
    assert.equal(t.E.current.activeWorkspace, 'ws', `fixture: the newer read adopted ws (gap ${gap})`);
    if (seen.some(s => /Switched to ws-b/.test(s.m))) early.push(gap);
  }
  assert.deepEqual(early, [], `"Switched to ws-b." before the newer read was adopted, at gaps ${JSON.stringify(early)}`);
});
test('SEC-R14-05 (decision 6): the newer read fails, so no session read since the switch was adopted: no "Switched to ws-b."', async () => {
  const { t, seen } = await switchGap(40, null);
  assert.deepEqual(seen.filter(s => /Switched to ws-b/.test(s.m)), [], 'announced a switch no adopted session read confirmed');
  void t;
});
test('SEC-R14-05 control: the newer read confirms ws-b, whatever the gap: "Switched to ws-b." once it is adopted', async () => {
  const missing = [];
  for (const gap of [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 40]) {
    const { t, seen } = await switchGap(gap, { ...HOSTED, ...WS_B });
    assert.equal(t.E.current.activeWorkspace, 'ws-b', `fixture: ws-b adopted (gap ${gap})`);
    if (!seen.some(s => s.m === 'Switched to ws-b.' && s.ws === 'ws-b')) missing.push(gap);
  }
  assert.deepEqual(missing, [], `no "Switched to ws-b." at gaps ${JSON.stringify(missing)}`);
});
