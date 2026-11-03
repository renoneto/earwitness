/* DEMO2 pass 22b (owner decisions on the pass 22 open points).
   1. Kept: only the newest issued session read is adopted.
   2. Two sequences. The combined one (reads, sign-in, sign-out) decides
      which session state is adopted. A session-change sequence, advanced
      only by sign-in and sign-out, decides their follow-ups: a redirect
      or reload is cancelled only by a newer sign-in or sign-out (or, for
      a sign-in, by a later read showing another person). When a sign-in
      or sign-out answers, every session read issued before that answer
      is discarded.
   3. Audio stops, unloads and detaches on ANY workspace change, the same
      person's included; late search-player messages for the old
      workspace are dropped.
   Fixtures are the pass 22 ones. Only fetch, DOM, media and location
   nodes are simulated; the real EW.api, apiCall, boot, render,
   handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass22b.test.cjs */
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
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, FormData: FakeFormData, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
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
function alertsServer() {
  const data = (p, init = {}) => {
    const method = init.method || 'GET';
    if (p === '/customer/shows') return { shows: [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }] };
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return method !== 'GET' ? { alert: { id: 1 } } : { alerts: [] };
    if (p.startsWith('/customer/filter-values')) return { items: [{ value: 'cs-private', label: 'PRIVATE-SHOW' }] };
    if (p.startsWith('/customer/summary')) return { groups: [] };
    return undefined;
  };
  return server(data, HOSTED);
}
async function alertsPage() {
  const srv = alertsServer();
  const win = loadPages('#/alerts', srv.fetch);
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
  win.location.hash = '#/alerts';
  E.current.route = '#/alerts';
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

// ============================================================================
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
for (const late of [false, true]) {
  const when = late ? 'still in flight when the sign-in answers' : 'answered before the sign-in answers';
  test(`22b-a: the same person's sign-in still redirects after a page session read (${when})`, async () => {
    const t = await inviteEntry();
    loginServer(t);
    const nav = captureLocation(t.win);
    const hold = holdRequest(t.win, isLogin);
    const pending = signinClick(t);
    await drain(5);
    assert.ok(hold.sent(), 'fixture: the sign-in was sent');
    const finishRead = await pageReadDuring(t, late, OWNER_STALE);
    hold.release(reply(200, { ok: true, authorize_url: AUTH + 'mine' }));
    await pending.catch(() => {});
    await drain(20);
    assert.deepEqual(nav.hrefs.map(h => h.href), [AUTH + 'mine'], 'the page read cancelled the same person\'s sign-in redirect');
    if (finishRead) {
      await finishRead();
      assert.equal(t.E.current.identity && t.E.current.identity.email, 'owner@example.test', 'a read issued before the sign-in answered was adopted after it');
      assert.equal(t.E.current.csrf, 's'.repeat(32), 'a read issued before the sign-in answered overwrote its result');
    }
  });
  const whenOut = late ? 'still in flight when the sign-out answers' : 'answered before the sign-out answers';
  test(`22b-b: the same person's sign-out still reloads after a page session read (${whenOut})`, async () => {
    const t = await alertsPage();
    const nav = captureLocation(t.win);
    const hold = holdRequest(t.win, isLogout);
    const pending = signoutClick(t);
    await drain(5);
    assert.ok(hold.sent(), 'fixture: the sign-out was sent');
    const finishRead = await pageReadDuring(t, late, reply(200, { ...HOSTED, email: 'owner@stale.example.test', csrf_token: 'o'.repeat(32) }));
    hold.release(reply(200, { ok: true }));
    await pending.catch(() => {});
    await drain(20);
    assert.deepEqual(nav.reloads, [OWNER], 'the page read cancelled the same person\'s sign-out reload');
    if (finishRead) {
      await finishRead();
      assert.equal(t.E.current.identity && t.E.current.identity.email, 'owner@example.test', 'a read issued before the sign-out answered was adopted after it');
    }
  });
}
test('22b-c: a sign-in answering after a newer sign-in already redirected does nothing visible', async () => {
  const t = await inviteEntry();
  loginServer(t);
  const nav = captureLocation(t.win);
  const holdA = holdRequest(t.win, isLogin);
  const a = signinClick(t);
  await drain(5);
  assert.ok(holdA.sent(), 'fixture: sign-in A was sent');
  await signinClick(t);
  await drain(10);
  assert.deepEqual(nav.hrefs.map(h => h.href), [AUTH + '1'], 'fixture: the newer sign-in B redirected');
  const before = t.toasts.length;
  holdA.release(reply(200, { ok: true, authorize_url: AUTH + 'A' }));
  await a.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.hrefs.map(h => h.href), [AUTH + '1'], 'the stale sign-in A redirected after the newer one');
  assert.deepEqual(t.toasts.slice(before), []);
});
test('22b-d: sign-in A, then a newer sign-in B: A answering first redirects nothing; B still redirects', async () => {
  const t = await inviteEntry();
  const nav = captureLocation(t.win);
  const holdA = holdRequest(t.win, isLogin);
  const a = signinClick(t);
  await drain(5);
  const holdB = holdRequest(t.win, isLogin);
  const b = signinClick(t);
  await drain(5);
  assert.ok(holdA.sent() && holdB.sent(), 'fixture: both sign-ins were sent');
  const before = t.toasts.length;
  holdA.release(reply(200, { ok: true, authorize_url: AUTH + 'A' }));
  await a.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.hrefs, [], 'the stale sign-in A redirected');
  assert.deepEqual(t.toasts.slice(before), [], 'the stale sign-in A said something');
  holdB.release(reply(200, { ok: true, authorize_url: AUTH + 'B' }));
  await b.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.hrefs.map(h => h.href), [AUTH + 'B'], 'the newest sign-in B did not redirect');
});
test('22b-c: a sign-in is cancelled by a newer sign-out', async () => {
  const t = await alertsPage();
  const nav = captureLocation(t.win);
  const holdA = holdRequest(t.win, isLogin);
  const a = signinClick(t);
  await drain(5);
  const holdOut = holdRequest(t.win, isLogout);
  const out = signoutClick(t);
  await drain(5);
  assert.ok(holdA.sent() && holdOut.sent(), 'fixture: sign-in then sign-out were sent');
  holdA.release(reply(200, { ok: true, authorize_url: AUTH + 'A' }));
  await a.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.hrefs, [], 'a sign-in superseded by a newer sign-out redirected');
  holdOut.release(reply(200, { ok: true }));
  await out.catch(() => {});
  await drain(20);
  assert.deepEqual(nav.reloads, [OWNER], 'the newest sign-out did not reload');
});

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
test('22b-3: the same person switches workspace while a clip plays: every player stops, unloads and detaches', async () => {
  const t = await audioPage();
  switchServer(t);
  const player = await playing(t);
  const clip = audioModel(t, { id: 'clip-c1', dataset: { clip: 'c1' }, attached: true, paused: false, src: '/api/v1/customer/records/sr1.WS-A-CLIP/media' });
  await chooseWsB(t);
  stoppedAndGone(player, 'the search player');
  stoppedAndGone(clip, 'the clip player');
});
test('22b-3: the same person adopted in another workspace (switched in another tab): the clip stops and detaches', async () => {
  const t = await audioPage();
  const player = await playing(t);
  await signIn(t, WS_B);
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: the other workspace is adopted');
  stoppedAndGone(player, 'the search player');
});
test('22b-3: a search play from the old workspace that fails after the switch is not announced', async () => {
  const t = await audioPage();
  switchServer(t);
  let fail = null;
  t.holdPlay = () => new Promise((res, rej) => { fail = rej; });
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: PRIVATE_AUDIO }, closest: () => null });
  await drain(10);
  assert.ok(fail, 'fixture: the search player\'s play() is pending');
  t.holdPlay = null;
  await chooseWsB(t);
  const before = t.toasts.length;
  fail(Object.assign(new Error('The play() request was interrupted by a call to pause().'), { name: 'AbortError' }));
  await pending.catch(() => {});
  await drain(10);
  assert.deepEqual(t.toasts.slice(before).filter(m => /clip/.test(m)), [], 'the old workspace\'s play failure was announced');
});
