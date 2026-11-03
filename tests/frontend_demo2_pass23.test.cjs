/* DEMO2 pass 23 (owner decisions on the Astra round 12 and code round 19
   findings).
   1. Audio teardown runs on any change of principal, signed out included,
      and on any workspace change; player errors belong to their play
      operation, so an old error never speaks again (SEC-R12-01/04, R19-02).
   2. History entries the app creates carry an owner tag (identityGen,
      principal, workspace). A missing or mismatched tag on popstate,
      pageshow or restore renders the route fresh; private search terms
      and filters live in history state, never in the URL (SEC-R12-02).
   3. Every sign-in or sign-out completion discards every session read
      issued before it, adopted or superseded (SEC-R12-03).
   4. Returning to the tab (visible, focus, pageshow) reads the session,
      at most once per 5 s, under the newest-issued rule (SEC-R12-05).
   5. A render a newer read superseded re-renders once if that newer flow
      ends without painting (R19-01).
   6. Pins (R19-N1), a signed-out tab keeps the person's own sign-out
      reload (R19-N2), and "Switched to …" survives a page read during
      the switch (R19-N3).
   Fixtures are the pass 22b ones, with document listeners and a browser
   history model. Only fetch, DOM, media, history and location nodes are
   simulated; the real EW.api, apiCall, boot, go, render, handleAction,
   handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass23.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

// ============================================================================
// Fixture: Alerts with a dialog, a notice-painting #main and a toast
// (pass 21b fixture), plus an invitation entry with no active workspace
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

function loadPages(route, fetch) {
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
// #main that paints outcome notices like the browser: inserted blocks go
// with the next full rewrite of its HTML.
function mainStub() {
  let html = '';
  const main = node({ inserted: [] });
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.inserted = []; } });
  main.insertAdjacentHTML = (pos, h) => { const b = { html: h, remove() { main.inserted = main.inserted.filter(x => x !== b); } }; main.inserted.unshift(b); };
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
async function alertsPage(route = '#/alerts', session = HOSTED) {
  const srv = alertsServer(session);
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = mainStub();
  const modal = modalStub();
  const toastEl = node();
  const pageForms = [];
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#toast' ? toastEl : node());
  win.document.querySelectorAll = sel => {
    if (!/form/.test(sel)) return [];
    const page = pageForms.filter(f => sel.split(',').some(part => {
      const want = part.match(/data-([a-z]+)="1"/);
      return !want || (f.dataset && f.dataset[want[1]] === '1');
    }));
    return [...modal.querySelectorAll(sel), ...page];
  };
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  const toasts = [];
  E.toast = m => { toasts.push(m); toastEl.textContent = m; };
  win.location.hash = route;
  E.current.route = route;
  await win.EWViews.render();
  await drain(30);
  const unload = () => { let prompted = false; for (const fn of win.listeners.beforeunload || []) fn({ preventDefault() { prompted = true; }, set returnValue(v) { prompted = true; } }); return prompted; };
  return { srv, win, E, main, modal, toastEl, toasts, pageForms, unload };
}
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
  return { srv, win, E, main, modal, toastEl, toasts, unload: () => false };
}

// ============================================================================
// Shared helpers
// ============================================================================
// Holds the next request matching `match` until release(answer): the
// request has been dispatched under the identity current at the time.
function holdRequest(win, match) {
  const orig = win.fetch;
  let release, seen = false;
  const gate = new Promise(res => { release = res; });
  win.fetch = async (url, opts = {}) => {
    if (!seen && match(url, opts)) { seen = true; const answer = await gate; return answer(); }
    return orig(url, opts);
  };
  return { release: answer => release(answer), sent: () => seen };
}
const reply = (status, body) => () => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
// Every request that leaves the tab, after any hold already installed.
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
const isSessionRead = (url, opts) => url === '/api/v1/session' && (!opts.method || opts.method === 'GET');

function captureLocation(win) {
  const nav = { hrefs: [], reloads: [] };
  let href = 'https://earwitness.example/app/' + (win.location.hash || '');
  Object.defineProperty(win.location, 'href', { configurable: true, get: () => href, set: v => { href = v; nav.hrefs.push({ principal: win.EW.current.principal, href: v }); } });
  win.location.pathname = '/app/';
  win.location.reload = () => { nav.reloads.push(win.EW.current.principal); };
  return nav;
}
const AUTHORIZE = 'https://id.example/authorize?state=previous-person';

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
async function audioPage() {
  const t = await alertsPage();
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
  t.win.fetch = async (url, opts = {}) => (/\/customer\/records\/[^/]+$/.test(url)
    ? reply(200, { ok: true, record: { media: { playback: 'local' }, selected_bounds: { start_s: 10, end_s: 50 } }, workspace_context: t.E.current.workspaceContext })()
    : orig(url, opts));
  return t;
}
const PRIVATE_AUDIO = 'sr1.OWNER-PRIVATE-AUDIO';
async function playing(t) {
  await t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: PRIVATE_AUDIO }, closest: () => null });
  await drain(10);
  const player = t.audios.find(a => a.id === 'search-audio');
  assert.ok(player && player.attached && !player.paused, 'fixture: the search player plays');
  assert.match(player.src, /OWNER-PRIVATE-AUDIO/, 'fixture: with the owner\'s media');
  return player;
}
const stoppedAndGone = (a, what) => {
  assert.equal(a.paused, true, `${what} keeps playing for the next person`);
  assert.equal(a.src, '', `${what} keeps the previous person's media source`);
  assert.ok(a.loads >= 1, `${what} was not unloaded (load() after removing src)`);
  assert.equal(a.attached, false, `${what} stays attached to the page`);
};

// 2. Sign-in and sign-out follow-ups: the session-change sequence
// ============================================================================
// The server answers every sign-in with its own authorization URL.
function loginServer(t) {
  let n = 0;
  const inner = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => (url === '/api/v1/session/login' && opts.method === 'POST'
    ? reply(200, { ok: true, authorize_url: AUTH + (++n) })()
    : inner(url, opts));
}
const AUTH = 'https://id.example/authorize?n=';
const signinClick = t => t.win.EWViews.handleAction({ dataset: { action: 'signin-start' }, closest: () => null });
const signoutClick = t => t.win.EWViews.handleAction({ dataset: { action: 'signout' }, closest: () => null });
const isLogin = (url, opts) => url === '/api/v1/session/login' && opts.method === 'POST';
const isLogout = (url, opts) => url === '/api/v1/session/logout' && opts.method === 'POST';
// The page's own session read (boot, then render) issued while the
// sign-in or sign-out is in flight. `late`: that read is still in flight
// when the sign-in/out answers and answers afterwards with `lateReply`.
async function pageReadDuring(t, late, lateReply) {
  if (!late) { await t.E.boot(); await t.win.EWViews.render(); await drain(20); return null; }
  const held = holdRequest(t.win, isSessionRead);
  const booting = t.E.boot().catch(e => e);
  await drain(5);
  assert.ok(held.sent(), 'fixture: the page\'s session read was sent');
  return async () => { held.release(lateReply); await booting; await drain(20); };
}
const OWNER_STALE = reply(200, { ...HOSTED, ...NO_WS, email: 'owner@stale.example.test', csrf_token: 'o'.repeat(32) });
// ============================================================================
// 3. Audio stops on any workspace change, the same person's included
// ============================================================================
const WS_B = { active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 };
// The server commits a workspace switch for the same person.
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
async function chooseWsB(t) {
  const choice = formStub('workspace-choice', null, [['workspace_id', 'ws-b', 'select-one', undefined]]);
  await t.win.EWViews.handleSubmit(choice).catch(() => {});
  await drain(80);
  assert.equal(t.E.current.principal, OWNER, 'fixture: the same person');
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: the switch committed');
}

// ============================================================================
// Pass 23 helpers
// ============================================================================
const clone = v => (v == null ? null : JSON.parse(JSON.stringify(v)));
// A browser history: entries {url, state}; pushState/replaceState update
// the URL like a browser; Back/Forward fire popstate, then hashchange.
function browser(t) {
  const B = { entries: [{ url: t.win.location.hash, state: null }], i: 0 };
  B.cur = () => B.entries[B.i];
  Object.defineProperty(t.win.location, 'hash', { configurable: true, get: () => B.cur().url,
    set: v => { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: v, state: null }); B.i++; } });
  t.win.history = {
    get state() { return clone(B.cur().state); },
    pushState(st, ti, u) { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: u === undefined ? B.cur().url : u, state: clone(st) }); B.i++; },
    replaceState(st, ti, u) { B.entries[B.i] = { url: u === undefined ? B.cur().url : u, state: clone(st) }; },
  };
  B.traverse = async (d) => {
    const old = B.cur().url;
    B.i += d;
    for (const fn of t.win.listeners.popstate || []) fn({ state: clone(B.cur().state) });
    if (B.cur().url !== old) for (const fn of t.win.listeners.hashchange || []) fn({});
    await drain(60);
  };
  B.urls = () => B.entries.map(e => e.url);
  return B;
}
const go = async (t, route) => { t.E.go(route); await drain(60); };
const SECRET = 'PRIVATE-ACQUISITION-R12';
const SEARCH = `#/search?q=${SECRET}&brands=acme-secret`;
const EVIDENCE = '#/evidence/sr1.R12-PIN';
const noSecret = (s, what) => assert.doesNotMatch(String(s), /PRIVATE-ACQUISITION|acme-secret/, what);
const fireWindow = (t, type, event = {}) => { for (const fn of t.win.listeners[type] || []) fn({ type, ...event }); };
const fireDocument = (t, type, event = {}) => { for (const fn of t.win.docListeners[type] || []) fn({ type, ...event }); };
const sessionReads = sent => sent.filter(s => s === 'GET /api/v1/session').length;
// The tab's clock, for the 5 s tab-return throttle.
function fakeClock(t, start = 1e9) {
  t.win.__now = start;
  vm.runInContext('Date.now = () => globalThis.__now;', t.win);
  return ms => { t.win.__now += ms; };
}

// ============================================================================
// SEC-R12-01 / R19-02: signed-out adoption stops every player
// ============================================================================
for (const how of ['boot and render', 'followSignedOut']) {
  test(`SEC-R12-01 / R19-02: the session ends (${how}): the search and clip players stop, unload and detach`, async () => {
    const t = await audioPage();
    const player = await playing(t);
    const clip = audioModel(t, { id: 'clip-c1', dataset: { clip: 'c1' }, attached: true, paused: false, src: '/api/v1/customer/records/sr1.WS-A-CLIP/media' });
    t.srv.signedOut = true;
    if (how === 'followSignedOut') await t.win.EWViews.followSignedOut();
    else { await t.E.boot(); await t.win.EWViews.render(); }
    await drain(40);
    assert.equal(t.E.current.principal, null, 'fixture: signed out');
    assert.equal(t.E.current.activeWorkspace, '', 'fixture: no workspace');
    stoppedAndGone(player, 'the search player');
    stoppedAndGone(clip, 'the clip player');
  });
}
test('SEC-R12-01: a play pending when the session ends says nothing', async () => {
  const t = await audioPage();
  let fail = null;
  t.holdPlay = () => new Promise((res, rej) => { fail = rej; });
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: PRIVATE_AUDIO }, closest: () => null });
  await drain(10);
  assert.ok(fail, 'fixture: play() is pending');
  t.holdPlay = null;
  t.srv.signedOut = true;
  await t.win.EWViews.followSignedOut();
  await drain(20);
  const before = t.toasts.length;
  fail(Object.assign(new Error('interrupted'), { name: 'AbortError' }));
  await pending.catch(() => {});
  await drain(10);
  assert.deepEqual(t.toasts.slice(before).filter(m => /clip/.test(m)), []);
});
test('SEC-R12-01 control: the same person, same workspace, refreshed: the clip keeps playing', async () => {
  const t = await audioPage();
  const player = await playing(t);
  await signIn(t, {});
  assert.equal(player.paused, false, 'an unchanged session stopped the player');
  assert.equal(player.attached, true, 'an unchanged session detached the player');
});

// ============================================================================
// SEC-R12-04: an old search-player error stays old after ws -> ws-b -> ws
// ============================================================================
test('SEC-R12-04: a play from the first visit to ws that fails after ws -> ws-b -> ws is not announced', async () => {
  const t = await audioPage();
  let fail = null;
  t.holdPlay = () => new Promise((res, rej) => { fail = rej; });
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: PRIVATE_AUDIO }, closest: () => null });
  await drain(10);
  assert.ok(fail, 'fixture: play() is pending');
  t.holdPlay = null;
  await signIn(t, WS_B);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: in ws-b');
  await signIn(t, { workspace_switch_revision: 9 });
  assert.equal(t.E.current.activeWorkspace, 'ws', 'fixture: back in ws');
  const before = t.toasts.length;
  fail(Object.assign(new Error('The play() request was interrupted.'), { name: 'AbortError' }));
  await pending.catch(() => {});
  await drain(10);
  assert.deepEqual(t.toasts.slice(before).filter(m => /clip/.test(m)), [], 'the old visit\'s play failure was announced');
});
test('SEC-R12-04 control: a play that fails in the same visit still says so', async () => {
  const t = await audioPage();
  t.holdPlay = () => Promise.reject(Object.assign(new Error('NotAllowedError'), { name: 'NotAllowedError' }));
  await t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: PRIVATE_AUDIO }, closest: () => null });
  await drain(10);
  assert.ok(t.toasts.some(m => /The clip did not start/.test(m)), 'a live play failure was not announced');
});

// ============================================================================
// SEC-R12-02: history entries carry an owner tag; the URL holds no terms
// ============================================================================
test('SEC-R12-02: a search the app opens keeps its terms and filters out of the URL', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  assert.equal(t.E.current.route, SEARCH, 'the search route itself was lost');
  for (const url of B.urls()) noSecret(url, 'a history URL holds the private search');
  assert.equal(t.E.searchCriteria().q, SECRET, 'the page lost its own query');
});
test('SEC-R12-02: another person adopted on the evidence page: no Back label or link to the previous person\'s search', async () => {
  const t = await alertsPage();
  browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  assert.match(t.main.innerHTML, /Back to Search: “PRIVATE-ACQUISITION-R12”/, 'fixture: the owner sees their own origin');
  await signIn(t, OTHER_SESSION);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person');
  assert.equal(t.E.current.route, EVIDENCE, 'fixture: still on the evidence page');
  noSecret(t.main.innerHTML, 'the other person sees the previous person\'s search');
});
test('SEC-R12-02: Back to the previous person\'s search entry renders Search fresh', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  await signIn(t, OTHER_SESSION);
  await B.traverse(-1);
  assert.match(t.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(t.E.current.route, 'Back restored the previous person\'s query');
  noSecret(t.main.innerHTML, 'Back painted the previous person\'s query');
  noSecret(B.cur().url, 'the URL holds the previous person\'s query');
});
test('SEC-R12-02: A -> B -> A: Back to an entry from A\'s first visit renders Search fresh', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  await signIn(t, OTHER_SESSION);
  await signIn(t, {});
  assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is back');
  await B.traverse(-1);
  assert.match(t.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(t.E.current.route, 'an entry from before B was restored');
});
test('SEC-R12-02: another person adopted on a search route: the route loses the previous person\'s terms', async () => {
  const t = await alertsPage();
  browser(t);
  await go(t, SEARCH);
  await signIn(t, OTHER_SESSION);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person');
  noSecret(t.E.current.route, 'the other person\'s route keeps the previous query');
  noSecret(t.main.innerHTML, 'the other person\'s page shows the previous query');
});
test('SEC-R12-02: Back to an untagged entry whose URL carries search terms renders Search fresh and clears the URL', async () => {
  const t = await alertsPage();
  const B = browser(t);
  B.entries = [{ url: SEARCH, state: null }, { url: '#/alerts', state: null }];
  B.i = 1;
  await B.traverse(-1);
  assert.match(t.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  noSecret(t.E.current.route, 'an untagged entry restored its query');
  noSecret(B.cur().url, 'the URL still holds the terms');
});
test('SEC-R12-02 control: the same person\'s Back restores the search and its Back label', async () => {
  const t = await alertsPage();
  const B = browser(t);
  await go(t, SEARCH);
  await go(t, EVIDENCE);
  assert.match(t.main.innerHTML, /Back to Search: “PRIVATE-ACQUISITION-R12”/, 'the owner lost their Back label');
  await B.traverse(-1);
  assert.equal(t.E.current.route, SEARCH, 'the owner\'s Back lost the query');
  await B.traverse(1);
  assert.equal(t.E.current.route, EVIDENCE);
  assert.match(t.main.innerHTML, /Back to Search: “PRIVATE-ACQUISITION-R12”/, 'Forward lost the owner\'s Back label');
});

// ============================================================================
// SEC-R12-03: every sign-in/out completion discards earlier session reads
// ============================================================================
const PRE_LOGOUT = reply(200, { ...HOSTED, ...NO_WS, email: 'pre-logout-owner@example.test', csrf_token: 'p'.repeat(32) });
for (const [first, second] of [['sign-out', 'sign-in'], ['sign-in', 'sign-out']]) {
  test(`SEC-R12-03: ${first} A, newer ${second} B, read C: A's completion discards C`, async () => {
    const t = await inviteEntry();
    const nav = captureLocation(t.win);
    const isA = first === 'sign-out' ? isLogout : isLogin, isB = second === 'sign-out' ? isLogout : isLogin;
    const clickA = first === 'sign-out' ? signoutClick : signinClick, clickB = second === 'sign-out' ? signoutClick : signinClick;
    const holdA = holdRequest(t.win, isA);
    const a = clickA(t);
    await drain(5);
    const holdB = holdRequest(t.win, isB);
    const b = clickB(t);
    await drain(5);
    const holdC = holdRequest(t.win, isSessionRead);
    const c = t.E.boot().then(r => ({ r }), e => ({ e }));
    await drain(5);
    assert.ok(holdA.sent() && holdB.sent() && holdC.sent(), 'fixture: A, B and C were sent');
    holdA.release(reply(200, first === 'sign-out' ? { ok: true } : { ok: true, authorize_url: AUTH + 'A' }));
    await a.catch(() => {});
    await drain(20);
    assert.deepEqual(nav.hrefs, [], 'the superseded A navigated');
    assert.deepEqual(nav.reloads, [], 'the superseded A reloaded');
    holdC.release(PRE_LOGOUT);
    const res = await c;
    assert.ok(res.e && res.e.droppedContext, 'read C, issued before A answered, was admitted after it');
    assert.notEqual(t.E.current.identity && t.E.current.identity.email, 'pre-logout-owner@example.test', 'C\'s identity was adopted');
    holdB.release(() => { throw new TypeError('Failed to fetch'); });
    await b.catch(() => {});
    await drain(20);
  });
}
test('SEC-R12-03 control: a read issued after the superseded completion is adopted', async () => {
  const t = await inviteEntry();
  const holdA = holdRequest(t.win, isLogout);
  const a = signoutClick(t);
  await drain(5);
  const holdB = holdRequest(t.win, isLogin);
  const b = signinClick(t);
  await drain(5);
  holdA.release(reply(200, { ok: true }));
  await a.catch(() => {});
  await drain(10);
  const res = await t.E.boot().then(r => ({ r }), e => ({ e }));
  assert.ok(res.r && res.r.admitted, 'a fresh read after the completion was dropped');
  holdB.release(() => { throw new TypeError('Failed to fetch'); });
  await b.catch(() => {});
});

// ============================================================================
// SEC-R12-05: returning to the tab re-reads the session
// ============================================================================
test('SEC-R12-05: focus after another tab signed in someone else: the session is read and the other person shown', async () => {
  const t = await inviteEntry();
  assert.match(t.main.innerHTML, /owner@example\.test/, 'fixture: the owner is painted');
  const sent = sentLog(t.win);
  t.srv.session = { ...HOSTED, ...OTHER_NO_WS };
  // DEMO2 pass 24b: a return counts after a departure.
  fireWindow(t, 'blur');
  fireWindow(t, 'focus');
  await drain(60);
  assert.equal(sessionReads(sent), 1, 'focus did not read the session');
  assert.equal(t.E.current.principal, OTHER, 'the other person was not adopted');
  assert.doesNotMatch(t.main.shown(), /owner@example\.test/, 'the previous person is still painted');
});
test('SEC-R12-05: visible again after a sign-out in another tab: Sign in shows and every player stops', async () => {
  const t = await audioPage();
  const player = await playing(t);
  t.srv.signedOut = true;
  // DEMO2 pass 24b: a return counts after a departure.
  t.win.document.visibilityState = 'hidden';
  fireDocument(t, 'visibilitychange');
  t.win.document.visibilityState = 'visible';
  fireDocument(t, 'visibilitychange');
  await drain(60);
  assert.equal(t.E.current.principal, null, 'the sign-out was not adopted');
  assert.match(t.main.shown(), /Sign in/, 'Sign in is not shown');
  stoppedAndGone(player, 'the search player');
});
test('SEC-R12-05: a bfcache pageshow after another tab switched workspace adopts it and stops the player', async () => {
  const t = await audioPage();
  const player = await playing(t);
  t.srv.session = { ...HOSTED, ...WS_B };
  fireWindow(t, 'pageshow', { persisted: true });
  await drain(80);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'the other tab\'s workspace was not adopted');
  stoppedAndGone(player, 'the search player');
});
// DEMO2 pass 24b (owner decision): one return read per departure, at
// once; no time window; no read without a departure.
test('SEC-R12-05: overlapping focus, visibility and pageshow events of one departure read the session once', async () => {
  const t = await alertsPage();
  const advance = fakeClock(t);
  const sent = sentLog(t.win);
  t.win.document.visibilityState = 'hidden';
  fireDocument(t, 'visibilitychange');
  t.win.document.visibilityState = 'visible';
  fireWindow(t, 'focus');
  fireDocument(t, 'visibilitychange');
  fireWindow(t, 'pageshow', { persisted: false });
  await drain(40);
  assert.equal(sessionReads(sent), 1, 'overlapping events read more than once');
  advance(4000);
  fireWindow(t, 'focus');
  await drain(40);
  assert.equal(sessionReads(sent), 1, 'a focus with no departure read');
  advance(500);
  fireWindow(t, 'blur');
  fireWindow(t, 'focus');
  await drain(40);
  assert.equal(sessionReads(sent), 2, 'a new departure did not read at once');
});
test('SEC-R12-05: a hidden tab does not read the session', async () => {
  const t = await alertsPage();
  const sent = sentLog(t.win);
  t.win.document.visibilityState = 'hidden';
  fireDocument(t, 'visibilitychange');
  await drain(20);
  assert.equal(sessionReads(sent), 0);
});
test('SEC-R12-05 control: the same person on focus: the page and its typed entries stay', async () => {
  const t = await alertsPage();
  const before = t.main.innerHTML;
  const renders = t.E.current.fetchToken;
  fireWindow(t, 'blur');
  fireWindow(t, 'focus');
  await drain(40);
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(t.E.current.fetchToken, renders, 'an unchanged session re-rendered the page');
  assert.equal(t.main.innerHTML, before);
  assert.deepEqual(t.toasts, []);
});

// ============================================================================
// R19-01: a render a non-painting reconcile superseded still publishes
// ============================================================================
for (const how of ['unchanged', 'lost connection']) {
  test(`R19-01: Settings re-rendering while a reconcile reads the session publishes (reconcile ${how})`, async () => {
    const t = await alertsPage('#/settings');
    assert.equal(t.E.current.publishedRoute, '#/settings', 'fixture: Settings published');
    const held = holdRequest(t.win, isSessionRead);
    const rendering = t.win.EWViews.render();
    await drain(10);
    assert.ok(held.sent(), 'fixture: Settings\' own session read is in flight');
    if (how === 'lost connection') {
      const inner = t.win.fetch;
      let once = true;
      t.win.fetch = async (url, opts = {}) => {
        if (once && isSessionRead(url, opts)) { once = false; throw new TypeError('Failed to fetch'); }
        return inner(url, opts);
      };
    }
    const outcome = await t.win.EWViews.reconcileCanonical();
    assert.equal(outcome, how === 'unchanged' ? 'unchanged' : 'failed', 'fixture: the reconcile painted nothing');
    held.release(reply(200, { ...HOSTED }));
    await rendering;
    await drain(60);
    assert.equal(t.E.current.publishedToken, t.E.current.fetchToken, 'Settings stays on its loading frame');
    assert.equal(t.E.current.publishedRoute, '#/settings');
  });
}
test('R19-01 control: a reconcile that finds another workspace renders it', async () => {
  const t = await alertsPage('#/settings');
  const held = holdRequest(t.win, isSessionRead);
  const rendering = t.win.EWViews.render();
  await drain(10);
  t.srv.session = { ...HOSTED, ...WS_B };
  const outcome = await t.win.EWViews.reconcileCanonical();
  assert.equal(outcome, 'rendered');
  held.release(reply(200, { ...HOSTED }));
  await rendering;
  await drain(60);
  assert.equal(t.E.current.activeWorkspace, 'ws-b');
  assert.equal(t.E.current.publishedToken, t.E.current.fetchToken);
});

// ============================================================================
// R19-N1: pins
// ============================================================================
test('R19-N1 pin: a Settings render whose session read was superseded paints no failure', async () => {
  const t = await alertsPage('#/settings');
  const held = holdRequest(t.win, isSessionRead);
  const rendering = t.win.EWViews.render();
  await drain(10);
  assert.ok(held.sent(), 'fixture: Settings\' own session read is in flight');
  await t.E.boot();
  held.release(reply(200, { ...HOSTED }));
  await rendering;
  await drain(30);
  assert.doesNotMatch(t.main.shown(), /This view failed/, 'a superseded render painted a failure');
});
test('R19-N1 pin: a workspace-chooser render whose session read was superseded stops quietly', async () => {
  const t = await alertsPage('#/alerts', { ...HOSTED, ...NO_WS });
  assert.match(t.main.shown(), /workspace/i, 'fixture: the chooser shows');
  const held = holdRequest(t.win, isSessionRead);
  const rendering = t.win.EWViews.render().then(() => null, e => e);
  await drain(10);
  assert.ok(held.sent(), 'fixture: the chooser\'s session read is in flight');
  await t.E.boot();
  held.release(reply(200, { ...HOSTED, ...NO_WS }));
  assert.equal(await rendering, null, 'the superseded render failed');
  await drain(30);
  assert.doesNotMatch(t.main.shown(), /This view failed/, 'a superseded render painted a failure');
});
test('R19-N1 pin: an older session read that fails in transport after a newer one is dropped', async () => {
  const t = await alertsPage();
  const held = holdRequest(t.win, isSessionRead);
  const old = t.E.boot().then(r => ({ r }), e => ({ e }));
  await drain(5);
  assert.ok(held.sent());
  await t.E.boot();
  held.release(() => { throw new TypeError('Failed to fetch'); });
  const res = await old;
  assert.ok(res.e && res.e.droppedContext && res.e.supersededSession, 'the old read\'s transport failure reached its caller as live');
});
test('R19-N1 pin: Retry whose session read a newer one superseded says nothing when it fails in transport', async () => {
  const t = await alertsPage();
  const held = holdRequest(t.win, isSessionRead);
  const retry = t.win.EWViews.handleAction({ dataset: { action: 'retry-session' }, closest: () => null });
  await drain(5);
  assert.ok(held.sent());
  await t.E.boot();
  const before = t.toasts.length;
  held.release(() => { throw new TypeError('Failed to fetch'); });
  await retry.catch(() => {});
  await drain(30);
  assert.deepEqual(t.toasts.slice(before), [], 'the superseded Retry announced a failure');
});
test('R19-N1 pin: sign-out A, then sign-out B: one reload', async () => {
  const t = await alertsPage();
  const nav = captureLocation(t.win);
  const holdA = holdRequest(t.win, isLogout);
  const a = signoutClick(t);
  await drain(5);
  const holdB = holdRequest(t.win, isLogout);
  const b = signoutClick(t);
  await drain(5);
  assert.ok(holdA.sent() && holdB.sent());
  holdA.release(reply(200, { ok: true }));
  await a.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.reloads, [], 'the superseded sign-out A reloaded');
  holdB.release(reply(200, { ok: true }));
  await b.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.reloads, [OWNER], 'not exactly one reload');
});

// ============================================================================
// R19-N2: a tab that learned it is signed out keeps the person's reload
// ============================================================================
test('R19-N2: a 401 adopted before the sign-out answers: the sign-out still reloads', async () => {
  const t = await alertsPage();
  const nav = captureLocation(t.win);
  const hold = holdRequest(t.win, isLogout);
  const out = signoutClick(t);
  await drain(5);
  assert.ok(hold.sent());
  t.srv.signedOut = true;
  await t.win.EWViews.followSignedOut();
  await drain(20);
  assert.equal(t.E.current.principal, null, 'fixture: the tab adopted signed out');
  hold.release(reply(200, { ok: true }));
  await out.catch(() => {});
  await drain(20);
  assert.equal(nav.reloads.length, 1, 'the person\'s own sign-out did not reload');
});
test('R19-N2 control: another person on screen still cancels the sign-out reload', async () => {
  const t = await alertsPage();
  const nav = captureLocation(t.win);
  const hold = holdRequest(t.win, isLogout);
  const out = signoutClick(t);
  await drain(5);
  await signIn(t, OTHER_SESSION);
  hold.release(reply(200, { ok: true }));
  await out.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.reloads, []);
});

// ============================================================================
// R19-N3: "Switched to …" survives a page session read during the switch
// ============================================================================
test('R19-N3: a page read supersedes the switch\'s confirming read: "Switched to ws-b." still shows', async () => {
  const t = await alertsPage();
  switchServer(t);
  const held = holdRequest(t.win, isSessionRead);
  const choice = formStub('workspace-choice', null, [['workspace_id', 'ws-b', 'select-one', undefined]]);
  const switching = t.win.EWViews.handleSubmit(choice).catch(() => {});
  await drain(30);
  assert.ok(held.sent(), 'fixture: the switch\'s confirming read is in flight');
  await t.E.boot().catch(() => {});
  held.release(reply(200, { ...HOSTED, ...WS_B }));
  await switching;
  await drain(80);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: the switch landed');
  assert.ok(t.toasts.includes('Switched to ws-b.'), `no "Switched to" message: ${JSON.stringify(t.toasts)}`);
});
