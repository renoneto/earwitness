/* DEMO2 pass 22.
   Red cases: Astra round 11 (demo2-memo-astra-r11.md DEMO2-SEC-R11-01..05)
   and code review r18 (demo2-code-r18.md DEMO2-R18-01, R18-N1, R18-N2).
   Owner decisions (pass 22):
   - Session operations (GET /session, sign-in, sign-out) are numbered in
     the order they are issued. Only the newest issued one may be
     adopted; an older reply is discarded, empty workspace token or not.
   - A sign-in or sign-out callback navigates, redirects or reloads only
     when no newer session operation started since it was issued.
   - Clipboard copy (the fallback included) and every dialog opener run
     inside an operation stamp; a stale stamp drops their effects.
   - An identity change stops, unloads and detaches every audio element.
   Fixtures are the pass 21/21b ones. Only fetch, DOM, media, clipboard
   and location nodes are simulated; the real EW.api, apiCall, boot,
   render, handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass22.test.cjs */
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

// ============================================================================
// DEMO2-SEC-R11-01: a delayed session read never restores an earlier
// person, even when both people have an empty workspace token
// ============================================================================
async function delayedSessionRead(oldReply, people, { shape = NO_WS } = {}) {
  const t = await inviteEntry();
  assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is on the invitation');
  t.srv.session = { ...HOSTED, ...shape };
  const hold = holdRequest(t.win, isSessionRead);
  const first = t.E.boot();
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the owner\'s session read was dispatched');
  for (const session of people) await signIn(t, { ...shape, ...session });
  hold.release(oldReply);
  const err = await first.then(() => null, e => e);
  await drain(40);
  return { t, err };
}
const OWNER_OLD = (shape = NO_WS) => reply(200, { ...HOSTED, ...shape, email: 'owner@old.example.test', csrf_token: 'o'.repeat(32) });
test('SEC-R11-01: an older session read answering after the other person was adopted is discarded (empty tokens)', async () => {
  const { t } = await delayedSessionRead(OWNER_OLD(), [OTHER_NO_WS]);
  assert.equal(t.E.current.workspaceContext || '', '', 'fixture: the same empty token');
  assert.equal(t.E.current.principal, OTHER, 'the previous person was restored by an older session read');
  assert.equal(t.E.current.identity && t.E.current.identity.email, 'other@example.test', 'the previous identity was restored');
  assert.equal(t.E.current.csrf, OTHER_CSRF, 'the previous person\'s CSRF token was adopted');
});
test('SEC-R11-01: the same holds with production-shaped null token and contract', async () => {
  const NULLS = { active_workspace: '', workspace_context: null, workspaces: [], frontend_contract: null };
  const { t } = await delayedSessionRead(OWNER_OLD(NULLS), [{ principal: OTHER, email: 'other@example.test', csrf_token: OTHER_CSRF }], { shape: NULLS });
  assert.equal(t.E.current.principal, OTHER, 'the previous person was restored by an older session read');
  assert.equal(t.E.current.csrf, OTHER_CSRF);
});
test('SEC-R11-01: A -> B -> A: an older session read is not adopted over the returning person\'s newer one', async () => {
  const { t } = await delayedSessionRead(OWNER_OLD(), [OTHER_NO_WS, {}]);
  assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is back');
  assert.equal(t.E.current.identity && t.E.current.identity.email, 'owner@example.test', 'the older read\'s identity was adopted');
  assert.equal(t.E.current.csrf, 's'.repeat(32), 'the older read\'s CSRF token was adopted');
});
test('SEC-R11-01 control: the newest session read, answering late, is still adopted', async () => {
  const { t, err } = await delayedSessionRead(reply(200, { ...HOSTED, ...NO_WS, email: 'owner@new.example.test' }), []);
  assert.equal(err, null, 'the newest read was refused: ' + (err && err.message));
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(t.E.current.identity && t.E.current.identity.email, 'owner@new.example.test');
});

// ============================================================================
// DEMO2-SEC-R11-02: sign-in and sign-out callbacks navigate only while no
// newer session operation has started
// ============================================================================
function captureLocation(win) {
  const nav = { hrefs: [], reloads: [] };
  let href = 'https://earwitness.example/app/' + (win.location.hash || '');
  Object.defineProperty(win.location, 'href', { configurable: true, get: () => href, set: v => { href = v; nav.hrefs.push({ principal: win.EW.current.principal, href: v }); } });
  win.location.pathname = '/app/';
  win.location.origin = 'https://earwitness.example';
  win.location.reload = () => { nav.reloads.push(win.EW.current.principal); };
  return nav;
}
const AUTHORIZE = 'https://id.example/authorize?state=previous-person';
async function heldSignin(between) {
  const t = await inviteEntry();
  const nav = captureLocation(t.win);
  const hold = holdRequest(t.win, (url, opts) => url === '/api/v1/session/login' && opts.method === 'POST');
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'signin-start' }, closest: () => null });
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the sign-in request was sent');
  await between(t);
  const before = t.toasts.length;
  hold.release(reply(200, { ok: true, authorize_url: AUTHORIZE }));
  await pending.catch(() => {});
  await drain(40);
  return { t, nav, toasts: t.toasts.slice(before) };
}
test('SEC-R11-02: a sign-in answer after another person was adopted does not redirect their page', async () => {
  const { t, nav, toasts } = await heldSignin(async t => {
    await signIn(t, OTHER_NO_WS);
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is adopted');
  });
  assert.deepEqual(nav.hrefs, [], 'the previous sign-in redirected the other person\'s page');
  assert.deepEqual(toasts.filter(m => /Sign-in/.test(m)), []);
});
// Pass 22b (owner decision) replaced the same-person case: a session read
// never cancels a sign-in redirect (frontend_demo2_pass22b.test.cjs 22b-a).
test('SEC-R11-02 control: the same person\'s sign-in, with nothing newer, still redirects', async () => {
  const { nav } = await heldSignin(async () => {});
  assert.deepEqual(nav.hrefs.map(h => h.href), [AUTHORIZE], 'the same person\'s sign-in did not redirect');
});
async function heldSignout(between, answer = reply(200, { ok: true })) {
  const t = await alertsPage();
  const nav = captureLocation(t.win);
  const hold = holdRequest(t.win, (url, opts) => url === '/api/v1/session/logout' && opts.method === 'POST');
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'signout' }, closest: () => null });
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the sign-out request was sent');
  await between(t);
  hold.release(answer);
  await pending.catch(() => {});
  await drain(40);
  return { t, nav };
}
test('SEC-R11-02: a sign-out answer after another person was adopted does not reload their page', async () => {
  const { t, nav } = await heldSignout(async t => {
    await signIn(t, OTHER_SESSION);
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is adopted');
  });
  assert.deepEqual(nav.reloads, [], 'the previous sign-out reloaded the other person\'s page');
  assert.equal(t.E.current.principal, OTHER);
});
test('SEC-R11-02: a failed sign-out answer after another person was adopted does not reload either', async () => {
  const { nav } = await heldSignout(async t => { await signIn(t, OTHER_SESSION); }, () => { throw new TypeError('Failed to fetch'); });
  assert.deepEqual(nav.reloads, [], 'the previous sign-out\'s failure reloaded the other person\'s page');
});
test('SEC-R11-02 control: the same person\'s sign-out, with nothing newer, still reloads', async () => {
  const { nav } = await heldSignout(async () => {});
  assert.deepEqual(nav.reloads, [OWNER], 'the same person\'s sign-out did not reload');
});

// ============================================================================
// DEMO2-SEC-R11-03: the clipboard fallback never copies the previous
// person's evidence locator
// ============================================================================
const PRIVATE_REF = 'sr1.PRIVATE-OWNER-REF';
async function heldCopy(between, outcome) {
  const t = await alertsPage();
  t.win.URL = URL;
  captureLocation(t.win);
  const copies = [], appended = [];
  let settle;
  t.win.navigator = { clipboard: { writeText: () => new Promise((res, rej) => { settle = { res, rej }; }) } };
  t.win.document.body = { append: n => { n.attached = true; appended.push({ principal: t.E.current.principal, value: n.value }); } };
  const create = t.win.document.createElement;
  t.win.document.createElement = tag => (tag === 'textarea'
    ? { tagName: 'TEXTAREA', value: '', attached: false, select() {}, remove() { this.attached = false; }, setAttribute() {}, style: {} }
    : create(tag));
  t.win.document.execCommand = cmd => { copies.push({ principal: t.E.current.principal, cmd }); return true; };
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'copy-read-link', ref: PRIVATE_REF }, closest: () => null });
  await drain(5);
  assert.ok(settle, 'fixture: the clipboard write started');
  await between(t);
  const before = t.toasts.length;
  if (outcome === 'reject') settle.rej(new Error('NotAllowedError')); else settle.res();
  await pending.catch(() => {});
  await drain(20);
  return { t, copies, appended, toasts: t.toasts.slice(before) };
}
for (const [label, people] of [['A -> B', [OTHER_SESSION]], ['A -> B -> A', [OTHER_SESSION, {}]]]) {
  test(`SEC-R11-03 (${label}): a clipboard rejection after another person was adopted starts no fallback copy`, async () => {
    const { appended, copies, toasts } = await heldCopy(async t => { for (const s of people) await signIn(t, s); }, 'reject');
    assert.deepEqual(appended, [], 'the previous person\'s locator was put into the page');
    assert.deepEqual(copies, [], 'a fallback copy ran');
    assert.deepEqual(toasts.filter(m => /Link copied|Copy didn/.test(m)), [], 'the copy was announced');
  });
  test(`SEC-R11-03 (${label}): a clipboard success after another person was adopted is not announced`, async () => {
    const { toasts } = await heldCopy(async t => { for (const s of people) await signIn(t, s); }, 'resolve');
    assert.deepEqual(toasts.filter(m => /Link copied|Copy didn/.test(m)), [], 'the copy was announced');
  });
}
test('SEC-R11-03 control: the same person\'s clipboard rejection still uses the fallback and says so', async () => {
  const { appended, copies, toasts } = await heldCopy(async () => {}, 'reject');
  assert.equal(appended.length, 1, 'the fallback did not run');
  assert.match(appended[0].value, /#\/evidence\/sr1\.PRIVATE-OWNER-REF$/);
  assert.equal(copies.length, 1);
  assert.ok(toasts.some(m => /^Link copied/.test(m)), 'the same person lost the copy toast: ' + JSON.stringify(toasts));
});

// ============================================================================
// DEMO2-SEC-R11-04: a dropped dialog opener recreates no notice
// ============================================================================
async function heldEditAlert(between) {
  const t = await alertsPage();
  const hold = holdRequest(t.win, url => /\/customer\/alerts\/1\?summaries=0$/.test(url));
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: '1' }, closest: () => null });
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the alert read was sent');
  await between(t);
  hold.release(reply(200, { ok: true, alert: { id: 1, name: 'OWNER PRIVATE ALERT' }, workspace_context: 'ctx-owner' }));
  await pending.catch(() => {});
  await drain(40);
  return t;
}
const OTHER_RECORD = /This record belongs to another workspace/;
test('SEC-R11-04: an alert opener dropped after another person was adopted shows them no notice', async () => {
  const t = await heldEditAlert(async t => {
    await signIn(t, { ...OTHER_SESSION, active_workspace: 'ws-b', workspace_context: 'ctx-other-b' });
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is adopted');
  });
  assert.ok(!(t.E.current.outcomeNotice && t.E.current.outcomeNotice.otherRecord), 'the previous opener recorded a notice: ' + JSON.stringify(t.E.current.outcomeNotice));
  assert.doesNotMatch(t.main.shown(), OTHER_RECORD, 'the other person sees the previous opener\'s notice');
  assert.doesNotMatch(t.main.shown() + t.modal.innerHTML, /OWNER PRIVATE ALERT/);
  assert.equal(t.modal.open, false);
});
test('SEC-R11-04 control: the same person in another workspace still gets the "another workspace" guidance', async () => {
  const t = await heldEditAlert(async t => { await signIn(t, { active_workspace: 'ws-b', workspace_context: 'ctx-owner-b' }); });
  assert.equal(t.E.current.principal, OWNER);
  assert.ok(t.E.current.outcomeNotice && t.E.current.outcomeNotice.otherRecord, 'the same person lost the guidance');
});

// ============================================================================
// DEMO2-SEC-R11-05: an identity change stops, unloads and detaches every
// audio element
// ============================================================================
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
test('SEC-R11-05: the search player and a clip player stop, unload and detach when another person is adopted', async () => {
  const t = await audioPage();
  const player = await playing(t);
  const clip = audioModel(t, { id: 'clip-c1', dataset: { clip: 'c1' }, attached: true, paused: false, src: '/api/v1/customer/records/sr1.OWNER-CLIP/media' });
  await signIn(t, OTHER_SESSION);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is adopted');
  stoppedAndGone(player, 'the search player');
  stoppedAndGone(clip, 'the clip player');
});
test('SEC-R11-05: a play started by the previous person that fails after the change is not announced', async () => {
  const t = await audioPage();
  let fail = null;
  t.holdPlay = () => new Promise((res, rej) => { fail = rej; });
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: PRIVATE_AUDIO }, closest: () => null });
  await drain(10);
  assert.ok(fail, 'fixture: the search player\'s play() is pending');
  const player = t.audios.find(a => a.id === 'search-audio');
  await signIn(t, OTHER_SESSION);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is adopted');
  const before = t.toasts.length;
  fail(Object.assign(new Error('The play() request was interrupted by a call to pause().'), { name: 'AbortError' }));
  await pending.catch(() => {});
  await drain(10);
  assert.deepEqual(t.toasts.slice(before).filter(m => /clip did not start/.test(m)), [], 'the previous person\'s play failure was announced');
  stoppedAndGone(player, 'the search player');
});
test('SEC-R11-05 control: the same person signing in again keeps their clip playing', async () => {
  const t = await audioPage();
  const player = await playing(t);
  await signIn(t, {});
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(player.paused, false, 'the same person\'s clip stopped');
  assert.equal(player.attached, true);
  assert.match(player.src, /OWNER-PRIVATE-AUDIO/);
});

// ============================================================================
// DEMO2-R18-01: a failed invitation-accept transport writes nothing for
// the next person
// ============================================================================
async function failedAccept(other, failure) {
  const t = await inviteEntry();
  assert.match(t.main.shown(), /id="invite-accept"/, 'fixture: the Accept form is offered');
  const form = formStub('invite-accept', null, [['workspace', 'ws-b', 'hidden'], ['invite_id', 'inv-1', 'hidden']]);
  const hold = holdRequest(t.win, (url, opts) => /\/accept$/.test(url) && opts.method === 'POST');
  const pending = t.win.EWViews.handleSubmit(form);
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the acceptance was sent');
  if (other) await signIn(t, OTHER_NO_WS);
  const before = t.toasts.length;
  hold.release(failure);
  await pending.catch(() => {});
  await drain(60);
  return { t, toasts: t.toasts.slice(before) };
}
const LOST = () => { throw new TypeError('Failed to fetch'); };
const TIMED_OUT = () => { throw Object.assign(new Error('The operation timed out.'), { name: 'TimeoutError' }); };
for (const [label, failure] of [['lost connection', LOST], ['timeout', TIMED_OUT]]) {
  test(`R18-01 (${label}): the previous person's failed acceptance writes no error for the next person`, async () => {
    const { t, toasts } = await failedAccept(true, failure);
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is adopted');
    assert.equal(t.E.current.inviteError || null, null, 'inviteError was written for the next person: ' + JSON.stringify(t.E.current.inviteError));
    assert.doesNotMatch(t.main.shown(), /Acceptance could not be confirmed/);
    assert.match(t.main.shown(), /id="invite-accept"/, 'the next person is not offered the Accept form');
    assert.deepEqual(toasts, []);
  });
}
test('R18-01 control: the same person\'s failed acceptance still says it could not be confirmed', async () => {
  const { t } = await failedAccept(false, LOST);
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(JSON.stringify(t.E.current.inviteError), JSON.stringify({ key: 'ws-b|inv-1', kind: 'unconfirmed' }));
  assert.match(t.main.shown(), /Acceptance could not be confirmed/);
});

// ============================================================================
// DEMO2-R18-N1 (pin): the workspace chooser queued behind a write sets no
// fence once another person was adopted (mutant X7 must fail this)
// ============================================================================
function writeInFlight(t) {
  const hold = holdRequest(t.win, (url, opts) => url === '/api/v1/customer/alerts/1/read' && opts.method === 'POST');
  const pending = t.E.api('/customer/alerts/1/read', { method: 'POST', body: { event_ids: [1] } }).catch(() => {});
  return { hold, pending };
}
const SWITCH_POST = 'POST /api/v1/session/workspace';
async function queuedChooser(people) {
  const t = await alertsPage();
  const save = writeInFlight(t);
  await drain(5);
  assert.ok(save.hold.sent(), 'fixture: a write is in flight');
  const choice = formStub('workspace-choice', null, [['workspace_id', 'ws-b', 'select-one', undefined]]);
  const pending = t.win.EWViews.handleSubmit(choice);
  await drain(10);
  const sent = sentLog(t.win);
  for (const s of people) await signIn(t, s);
  save.hold.release(reply(200, { ok: true, workspace_context: t.E.current.workspaceContext }));
  await save.pending;
  await pending.catch(() => {});
  await drain(80);
  return { t, sent };
}
for (const [label, people] of [['A -> B', [OTHER_SESSION]], ['A -> B -> A', [OTHER_SESSION, {}]]]) {
  test(`R18-N1 (${label}): a queued workspace choice sets no write fence and sends no switch`, async () => {
    const { t, sent } = await queuedChooser(people);
    assert.ok(!sent.includes(SWITCH_POST), 'the queued switch was sent: ' + JSON.stringify(sent));
    assert.equal(t.E.current.writeFence || null, null, 'the previous person\'s queued switch fenced the next person\'s writes');
    assert.ok(!t.E.current.switchWaiting, 'a switch wait is left behind');
  });
}
test('R18-N1 control: the same person\'s queued workspace choice is still sent', async () => {
  const { t, sent } = await queuedChooser([]);
  assert.equal(t.E.current.principal, OWNER);
  assert.ok(sent.includes(SWITCH_POST), 'the same person\'s switch was not sent: ' + JSON.stringify(sent));
});

// ============================================================================
// DEMO2-R18-N2: an operation's shared read carries its stamp
// ============================================================================
test('R18-N2: a stale operation\'s shared read sends nothing', async () => {
  const t = await alertsPage();
  assert.equal(typeof t.win.EWViews.opScope, 'function', 'the operation scope is not reachable');
  const op = t.win.EWViews.opScope();
  await signIn(t, OTHER_SESSION);
  const sent = sentLog(t.win);
  const err = await op.E.sharedRead('/customer/shows').then(() => null, e => e);
  assert.ok(!sent.some(s => /\/customer\/shows/.test(s)), 'the stale operation\'s shared read was sent: ' + JSON.stringify(sent));
  assert.ok(err && err.droppedContext, 'the stale shared read resolved');
});
test('R18-N2 control: a live operation\'s shared read is sent', async () => {
  const t = await alertsPage();
  assert.equal(typeof t.win.EWViews.opScope, 'function', 'the operation scope is not reachable');
  const op = t.win.EWViews.opScope();
  const sent = sentLog(t.win);
  const out = await op.E.sharedRead('/customer/shows');
  assert.ok(sent.some(s => /\/customer\/shows/.test(s)) || (out && out.shows), 'the live shared read was not served');
  assert.ok(out && Array.isArray(out.shows));
});
