/* DEMO2 pass 20.
   Red cases: Astra round 9 (demo2-memo-astra-r9.md DEMO2-SEC-R9-01,
   SEC-R9-02) and code review r16 (demo2-code-r16.md DEMO2-R16-01,
   R16-02, R16-N1, R16-N2).
   Owner rule: every async write, save answer, stale-draft recovery and
   reconcile records the identity it started under (identity generation
   and server principal). When it answers after a different person was
   adopted, its server-side effect stands, but every client-side effect
   aimed at the user is dropped: toast, outcome notice, page notice,
   refill, held draft, the leave-prompt count and "workspace changed"
   messages. The new person's view is rendered fresh from server data.
   The same person keeps the existing behaviour, across workspaces too.
   Fixtures are the pass 19 ones (Settings page form; Alerts with a
   dialog, a notice-painting #main and a toast). Only fetch and DOM
   nodes are simulated; the real EW.api, apiCall, boot, render,
   handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass20.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

// ============================================================================
// Fixture 1: a live Settings page form (frontend_query modelWin, trimmed)
// ============================================================================
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const hostedSession = (over = {}) => ({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
  active_workspace: 'ws-a', workspaces: ['ws-a', 'ws-b'], workspace_context: 'ctx-a',
  workspace_switch_revision: 5, frontend_contract: 'earwitness/customer-ui/2', ...over });
const json = (obj, ok = true, status = 200) => ({ ok, status, text: async () => JSON.stringify(obj) });
const uiRoot = () => {
  const root = { innerHTML: '', inserted: [] };
  root.querySelectorAll = () => [];
  root.querySelector = sel => (sel === '[data-outcome-notice]' ? root.inserted.find(b => /data-outcome-notice/.test(b.html)) || null : null);
  root.insertAdjacentHTML = (pos, html) => {
    const b = { html, remove() { root.inserted = root.inserted.filter(x => x !== b); } };
    root.inserted.unshift(b);
  };
  root.top = () => root.inserted.map(b => b.html).join('\n');
  return root;
};
const clone = v => (v == null ? null : JSON.parse(JSON.stringify(v)));
function makeBrowser(entries, i) {
  const B = { entries: entries.map(e => ({ doc: 'app', state: null, ...e })), i, q: [], L: {}, writes: [] };
  B.cur = () => B.entries[B.i];
  B.location = { get hash() { return B.cur().url; }, set hash(v) { B.entries.push({ url: v, state: null, doc: 'app' }); B.i = B.entries.length - 1; } };
  B.history = {
    get state() { return clone(B.cur().state); },
    get length() { return B.entries.length; },
    pushState(st, t, url) { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: url === undefined ? B.cur().url : url, state: clone(st), doc: 'app' }); B.i++; },
    replaceState(st, t, url) { const e = B.cur(); B.entries[B.i] = { url: url === undefined ? e.url : url, state: clone(st), doc: e.doc }; },
    go() {}, back() {}, forward() {},
  };
  return B;
}
// A rendered Settings page carries a fresh name form with the server
// value; typing marks it dirty, as markDirty does.
const pageForm = (value, revision = '1') => {
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  const field = { name: 'workspace_name', type: 'text', value, defaultValue: value };
  const rev = { name: 'expected_revision', type: 'hidden', value: revision, defaultValue: revision };
  const submit = { type: 'submit', textContent: 'Save workspace name', disabled: false };
  return { box, field, rev, submit, dataset: {}, elements: [rev, field], getAttribute: a => (a === 'id' ? 'settings-workspace' : null), closest: () => null,
    contains: () => false,
    querySelector: s => (s === '#form-errors' ? box : s === '[type=submit]' ? submit : null), querySelectorAll: s => (s === '[type=submit]' ? [submit] : []),
    reset() { field.value = field.defaultValue; rev.value = rev.defaultValue; } };
};
// The server: one session and Settings preferences, decided per request
// from `state` (principal, workspace, context).
const idServer = state => async (url, opts = {}) => {
  if (url === '/api/v1/session') return json(hostedSession({ principal: state.principal, email: state.principal + '@example.test',
    workspace_context: state.ctx, active_workspace: state.ws }));
  if (url.startsWith('/api/v1/session/')) return json({ ok: false, error: { code: 'NOT_FOUND', message: 'unexpected' } }, false, 404);
  return json({ ok: true, workspace_context: state.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
};
async function settingsPage(state) {
  const B = makeBrowser([{ url: '#/settings' }], 0);
  const server = idServer(state);
  const fetch = async (url, opts = {}) => {
    if (opts.method && opts.method !== 'GET') B.writes.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
    return server(url, opts);
  };
  const win = { console, URLSearchParams, AbortController, location: B.location, history: B.history, scrollTo() {}, fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {} };
  win.window = win; win.globalThis = win;
  vm.createContext(win);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.addEventListener = (type, fn) => { B.L[type] = fn; };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.EW.current.route = B.location.hash;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settleAll();
  const root = uiRoot();
  let html = '';
  Object.defineProperty(root, 'innerHTML', { get: () => html, set: v => {
    html = v;
    root.inserted = [];
    const m = v.match(/id="settings-workspace"><input type="hidden" name="expected_revision" value="([^"]*)"><div class="od-field"><label for="settings-name">Workspace display name<\/label><input id="settings-name" name="workspace_name" value="([^"]*)"/);
    root.form = m ? pageForm(m[2], m[1]) : null;
  } });
  root.querySelector = s => (root.form && s === '#settings-workspace' ? root.form
    : s === '[data-outcome-notice]' ? root.inserted.find(b => /data-outcome-notice/.test(b.html)) || null : null);
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? root : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && root.form
    && (root.form.dataset.dirty === '1' || (/data-held/.test(s) && root.form.dataset.held === '1')) ? [root.form] : []);
  win.EW.header = () => {}; win.EW.toast = () => {}; win.setTimeout = () => {};
  win.FormData = class { constructor(f) { this.f = f; } *entries() { for (const e of this.f.elements) if (e.name) yield [e.name, e.value]; } };
  // The stale-draft recovery looks its form up by id (Astra r9 probe).
  win.document.getElementById = id => (root.form && id === 'settings-workspace' ? root.form : null);
  await win.EWViews.render(); await settleAll();
  assert.ok(root.form, 'fixture: Settings rendered');
  const type = v => { root.form.field.value = v; root.form.dataset.dirty = '1'; };
  const unload = () => { let prompted = false; B.L.beforeunload({ preventDefault() { prompted = true; }, set returnValue(v) { prompted = true; } }); return prompted; };
  return { B, win, E: win.EW, root, modal, type, unload };
}
const SAM_TYPED = 'SAM PRIVATE LIVE DRAFT';
// What the next person sees: the server's value in a clean form, no
// restored or held notice, nothing of the previous text anywhere, and no
// leave-page prompt.
function freshForNext(t, who, why) {
  const { E, root } = t;
  assert.equal(E.current.principal, who, `fixture: ${who} adopted (${why})`);
  assert.ok(root.form, `Settings rendered (${why})`);
  assert.equal(root.form.field.value, 'Alpha', `the previous person's typed text is in ${who}'s form (${why})`);
  assert.notEqual(root.form.dataset.dirty, '1', `${who}'s form is marked as an unsaved draft (${why})`);
  assert.notEqual(root.form.dataset.held, '1', `${who}'s form is held (${why})`);
  assert.doesNotMatch(root.form.box.innerHTML, /restored|kept here/, `${who} is told an entry was restored or kept (${why})`);
  assert.ok(!root.innerHTML.includes(SAM_TYPED) && !root.top().includes(SAM_TYPED), `the page shows the previous typed text (${why})`);
  assert.equal(t.unload(), false, `the leave-page prompt counts the previous person's entry (${why})`);
}
// The two ways a tab learns of another person without seeing a sign-out.
const TRIGGERS = {
  // Boot, then render (Astra R8-01 step 4; r15 "boot then render").
  'boot then render': async t => { await t.E.boot(); await t.win.EWViews.render(); await settleAll(40); },
  // An ordinary read answers under the new context: the app's own
  // canonical reconcile adopts and renders.
  'canonical reconcile': async t => { await t.E.api('/customer/preferences').catch(() => {}); await settleAll(80); },
};

// ============================================================================
// Fixture 2: Alerts with a dialog, a notice-painting #main and a toast
// (pass 18 fixture, plus painted-notice capture)
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
const TYPED = 'OWNER PRIVATE DRAFT';
async function openTypedAlert(t) {
  await t.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  assert.equal(t.modal.open, true, 'fixture: Create alert opened');
  t.modal.forms = [formStub('alert-form', t.modal, [['name', TYPED], ['show', 'cs-private', 'select-one', undefined]], { dirty: '1' })];
}
// A Create alert write answered 401; the session read then answers
// `session` (signed out when null).
async function refuseWrite(t, session) {
  const { srv, E } = t;
  if (session) srv.session = { ...HOSTED, ...session }; else srv.signedOut = true;
  srv.answers['/customer/alerts'] = { method: 'POST', status: 401, code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required' };
  await assert.rejects(E.api('/customer/alerts', { method: 'POST', body: { name: 'x' } }), err => err.status === 401);
  await drain(100);
  delete srv.answers['/customer/alerts'];
}
async function signIn(t, session) {
  t.srv.signedOut = false;
  t.srv.session = { ...HOSTED, ...session };
  await t.E.boot();
  await t.win.EWViews.render();
  await drain(60);
}
const NOT_APPLIED = /was not applied|were not applied|unsaved entr/;
function noticeCleared(t, why) {
  const { E, main, toastEl } = t;
  assert.ok(!NOT_APPLIED.test(JSON.stringify(E.current.outcomeNotice || null)), `the outcome notice still refers to the previous entry (${why})`);
  assert.ok(!NOT_APPLIED.test(main.shown()), `the page shows a notice about the previous entry (${why}): ${main.shown().slice(0, 200)}`);
  assert.ok(!NOT_APPLIED.test(toastEl.textContent), `a toast about the previous entry is still shown (${why})`);
  assert.ok(!main.shown().includes(TYPED) && !t.modal.innerHTML.includes(TYPED), `the previous typed text is shown (${why})`);
  assert.equal(t.unload(), false, `the leave-page prompt still counts (${why})`);
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
const PREVIOUS_WRITE = /Saved in|Your change in|Your change was saved|was not saved|could not be confirmed/;

// ============================================================================
// DEMO2-SEC-R9-01: an earlier person's pending write recreates notices
// ============================================================================
const LATE = {
  saved: reply(200, { ok: true, alert: { id: 1 }, workspace_context: 'ctx-owner' }),
  rejected: reply(422, { ok: false, error: { code: 'INVALID', message: 'bad entry' }, workspace_context: 'ctx-owner' }),
  unknown: reply(500, { ok: false, error: { code: 'SERVER', message: 'boom' }, workspace_context: 'ctx-owner' }),
};
async function lateAlertWrite(kind, people) {
  const t = await alertsPage();
  const hold = holdRequest(t.win, (url, opts) => url === '/api/v1/customer/alerts' && opts.method === 'POST');
  const pending = t.E.api('/customer/alerts', { method: 'POST', body: { name: 'OWNER PRIVATE ENTRY' } });
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the owner\'s write was dispatched');
  for (const session of people) await signIn(t, session);
  const before = t.toasts.length;
  hold.release(LATE[kind]);
  const err = await pending.then(() => null, e => e);
  await drain(40);
  return { t, err, toasts: t.toasts.slice(before) };
}
const OTHER_SESSION = { principal: OTHER, email: 'other@example.test', workspace_context: 'ctx-other' };
for (const kind of Object.keys(LATE)) {
  test(`SEC-R9-01 (${kind}): the previous person's write answering after another person is adopted tells the new person nothing`, async () => {
    const { t, err, toasts } = await lateAlertWrite(kind, [OTHER_SESSION]);
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is signed in');
    assert.ok(err && err.droppedContext, 'the late answer still never reaches its caller');
    assert.deepEqual(toasts.filter(m => PREVIOUS_WRITE.test(m)), [], 'the new person was toasted about the previous person\'s write');
    assert.ok(!(t.E.current.outcomeNotice && t.E.current.outcomeNotice.saved), 'an outcome notice records the previous person\'s write: ' + JSON.stringify(t.E.current.outcomeNotice));
    assert.doesNotMatch(t.main.shown(), /Your change was saved|was not saved|could not be confirmed/, 'the page paints the previous person\'s outcome');
    assert.equal(t.unload(), false, 'the leave-page prompt counts something');
  });
}

test('SEC-R9-01: owner, then another person, then the owner again: the old write is still not announced', async () => {
  const { t, toasts } = await lateAlertWrite('saved', [OTHER_SESSION, { workspace_context: 'ctx-owner' }]);
  assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is back');
  assert.deepEqual(toasts.filter(m => PREVIOUS_WRITE.test(m)), [], 'an answer from before another person was adopted was announced');
  assert.ok(!(t.E.current.outcomeNotice && t.E.current.outcomeNotice.saved), 'its outcome notice was recreated');
});

test('SEC-R9-01 control: the same person in another workspace is still told where the late write was saved', async () => {
  const { t, err, toasts } = await lateAlertWrite('saved', [{ active_workspace: 'ws-b', workspace_context: 'ctx-b' }]);
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(err && err.lateWrite, 'saved');
  assert.ok(toasts.includes('Saved in ws.'), 'the same person lost the late-save toast: ' + JSON.stringify(toasts));
  assert.equal(t.E.current.outcomeNotice && t.E.current.outcomeNotice.saved, 'ws');
  assert.match(t.main.shown(), /Your change was saved to ws\./);
});

// ============================================================================
// DEMO2-R16-01: the previous person's Settings save answers late
// ============================================================================
async function lateSettingsSave(answer, next) {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  const hold = holdRequest(t.win, (url, opts) => opts.method === 'PUT');
  t.type(SAM_TYPED);
  const pending = t.win.EWViews.handleSubmit(t.root.form);
  await settleAll(5);
  assert.ok(hold.sent(), 'fixture: sam\'s save was dispatched');
  Object.assign(state, next);
  await TRIGGERS['canonical reconcile'](t);
  const before = toasts.length;
  const noticeBefore = JSON.stringify(t.E.current.outcomeNotice);
  hold.release(answer);
  await pending.catch(() => {});
  await settleAll(80);
  return { t, toasts: toasts.slice(before), noticeBefore };
}
const SAM_SAVED = reply(200, { ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: SAM_TYPED, revision: 2 } });
const SAM_401 = reply(401, { ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } });
for (const [name, answer, next] of [
  ['P1a: 200, same workspace', SAM_SAVED, { principal: 'kim', ctx: 'ctx-next' }],
  ['P1b: 401, other workspace', SAM_401, { principal: 'kim', ws: 'ws-b', ctx: 'ctx-kim-b' }],
  ['P1c: 200, other workspace', SAM_SAVED, { principal: 'kim', ws: 'ws-b', ctx: 'ctx-kim-b' }],
]) {
  test(`R16-01 (${name}): sam's Settings save answering after kim is adopted tells kim nothing`, async () => {
    const { t, toasts, noticeBefore } = await lateSettingsSave(answer, next);
    assert.equal(t.E.current.principal, 'kim', 'fixture: kim adopted');
    assert.deepEqual(toasts.filter(m => PREVIOUS_WRITE.test(m) || /ws-a/.test(m)), [], 'kim was toasted about sam\'s save');
    assert.equal(JSON.stringify(t.E.current.outcomeNotice), noticeBefore, 'sam\'s save changed kim\'s outcome notice');
    assert.doesNotMatch(t.root.top(), /Your change was saved|ws-a/, 'kim\'s page names sam\'s save or workspace');
    freshForNext(t, 'kim', name);
  });
}

test('R16-01: sam\'s save answering before kim\'s page is rendered holds nothing on the form on screen', async () => {
  // The window between adoption and the next render: the reset form is
  // still the live one, so a late hold would show and count for kim.
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  const holdPut = holdRequest(t.win, (url, opts) => opts.method === 'PUT');
  t.type(SAM_TYPED);
  const live = t.root.form;
  const pending = t.win.EWViews.handleSubmit(live);
  await settleAll(5);
  assert.ok(holdPut.sent(), 'fixture: sam\'s save was dispatched');
  Object.assign(state, { principal: 'kim', ctx: 'ctx-next' });
  await t.E.boot();
  assert.equal(t.E.current.principal, 'kim', 'fixture: kim adopted');
  assert.equal(t.root.form, live, 'fixture: the page is not rendered again yet');
  holdPut.release(SAM_SAVED);
  await pending.catch(() => {});
  await settleAll(20);
  assert.notEqual(live.dataset.held, '1', 'sam\'s late answer holds the form kim sees');
  assert.doesNotMatch(live.box.innerHTML, /Saved in|ws-a|Reload the workspace/, 'sam\'s late answer is written on the form kim sees');
  assert.equal(t.unload(), false, 'the leave-page prompt counts sam\'s late answer');
  assert.deepEqual(toasts.filter(m => PREVIOUS_WRITE.test(m) || /ws-a/.test(m)), []);
  await t.win.EWViews.render(); await settleAll(40);
  freshForNext(t, 'kim', 'after kim\'s page rendered');
});

// ============================================================================
// DEMO2-R16-02: the reconcile that adopts another person speaks about it
// ============================================================================
test('R16-02: the canonical reconcile that adopts another person in another workspace adds no "workspace changed" notice', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  t.type(SAM_TYPED);
  Object.assign(state, { principal: 'kim', ws: 'ws-b', ctx: 'ctx-kim-b' });
  await TRIGGERS['canonical reconcile'](t);
  freshForNext(t, 'kim', 'reconcile, other workspace');
  assert.equal(t.E.current.outcomeNotice, null, 'kim got an outcome notice: ' + JSON.stringify(t.E.current.outcomeNotice));
  assert.doesNotMatch(t.root.top(), /changed in another tab or window/);
  assert.deepEqual(toasts.filter(m => /another tab or window/.test(m)), []);
});

test('R16-02: a 409 conflict reconcile that adopts another person gives no conflict toast', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  const hold = holdRequest(t.win, (url, opts) => opts.method === 'POST');
  const pending = t.E.api('/customer/alerts/1/read', { method: 'POST', body: { event_ids: [1] } });
  await settleAll(5);
  assert.ok(hold.sent(), 'fixture: sam\'s action was dispatched');
  Object.assign(state, { principal: 'kim', ctx: 'ctx-next' });
  hold.release(reply(409, { ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'moved' }, workspace_context: 'ctx-a' }));
  const err = await pending.then(() => null, e => e);
  assert.ok(err && err.workspaceConflict, 'fixture: a workspace conflict');
  await settleAll(80);
  assert.equal(t.E.current.principal, 'kim', 'fixture: the reconcile adopted kim');
  assert.deepEqual(toasts.filter(m => /another tab or window/.test(m)), [], 'kim was told about sam\'s conflict');
  assert.doesNotMatch(t.root.top(), /another tab or window/);
});

test('R16-02 control: the same person moved to another workspace by another tab still gets the notice', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  Object.assign(state, { ws: 'ws-b', ctx: 'ctx-b' });
  await TRIGGERS['canonical reconcile'](t);
  assert.equal(t.E.current.principal, 'sam');
  assert.equal(t.E.current.outcomeNotice && t.E.current.outcomeNotice.elsewhere, true);
  assert.match(t.root.top(), /The workspace was changed in another tab or window\./);
});

// ============================================================================
// DEMO2-SEC-R9-02: pending stale-draft recovery after another person
// ============================================================================
async function staleDraftPending(t, state) {
  const hold412 = holdRequest(t.win, (url, opts) => opts.method === 'PUT');
  t.type(SAM_TYPED);
  const form = t.root.form;
  const saving = t.win.EWViews.handleSubmit(form);
  await settleAll(5);
  hold412.release(reply(412, { ok: false, error: { code: 'STALE', message: 'stale', details: { current_revision: 2 } }, workspace_context: state.ctx }));
  await saving.catch(() => {});
  await settleAll(20);
  assert.equal(t.E.current.staleDraft && t.E.current.staleDraft.values.workspace_name, SAM_TYPED, 'fixture: the 412 kept sam\'s entry for recovery');
  return form;
}
test('SEC-R9-02: a stale-draft recovery pending across another person never refills that person\'s form', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  const form = await staleDraftPending(t, state);
  // The reload's own render: its session read is held.
  const holdSession = holdRequest(t.win, url => url === '/api/v1/session');
  const reload = t.win.EWViews.handleAction({ dataset: { action: 'reload-stale-draft' }, closest: () => form });
  await settleAll(40);
  assert.ok(holdSession.sent(), 'fixture: the reload render waits on its session read');
  Object.assign(state, { principal: 'kim', ctx: 'ctx-kim' });
  await t.E.boot(); await t.win.EWViews.render(); await settleAll(40);
  assert.equal(t.E.current.principal, 'kim', 'fixture: kim adopted');
  assert.equal(t.root.form.field.value, 'Alpha', 'fixture: kim\'s page painted');
  assert.equal(t.E.current.staleDraft, null);
  const before = toasts.length;
  holdSession.release(reply(200, hostedSession({ principal: 'sam', email: 'sam@example.test', workspace_context: 'ctx-a', active_workspace: 'ws-a' })));
  await reload.catch(() => {});
  await settleAll(40);
  freshForNext(t, 'kim', 'after the old recovery finished');
  assert.deepEqual(toasts.slice(before).filter(m => /restored|Nothing was restored|no longer on screen/.test(m)), [], 'kim was toasted about sam\'s recovery');
  await t.win.EWViews.render(); await settleAll(40);
  freshForNext(t, 'kim', 'after the next render');
});

test('SEC-R9-02 control: the same person\'s stale-draft recovery still restores the entry', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const form = await staleDraftPending(t, state);
  await t.win.EWViews.handleAction({ dataset: { action: 'reload-stale-draft' }, closest: () => form });
  await settleAll(40);
  assert.equal(t.E.current.principal, 'sam');
  assert.equal(t.root.form.field.value, SAM_TYPED, 'the same person lost the recovered entry');
  assert.equal(t.root.form.dataset.dirty, '1');
  assert.equal(t.unload(), true);
});

// ============================================================================
// DEMO2-R16-N1: the counted wording counts entries, not notice items
// ============================================================================
test('R16-N1: a combined notice for two entries plus one more dropped entry reads "3 unsaved entries were not applied."', async () => {
  const t = await alertsPage();
  await openTypedAlert(t);
  const page = formStub('alert-page-note', null, [['note', 'Page typed']], { dirty: '1' });
  t.pageForms.push(page);
  await refuseWrite(t, null);
  t.modal.forms = []; t.pageForms.length = 0;
  await signIn(t, { active_workspace: 'ws-b', workspace_context: 'ctx-b' });
  assert.match(t.main.shown(), /Your unsaved entries in ws were not applied\./, 'fixture: one combined notice for two entries');
  // In ws-b the same person types a page entry; a render keeps it.
  t.pageForms.push(formStub('alert-page-note', null, [['note', 'Typed in ws-b']], { dirty: '1' }));
  await t.win.EWViews.render(); await drain(40);
  t.pageForms.length = 0;
  // Another tab moves this person back to ws: that entry is dropped too.
  t.srv.session = { ...HOSTED, active_workspace: 'ws', workspace_context: 'ctx-ws-again' };
  await t.E.boot(); await t.win.EWViews.render(); await drain(60);
  assert.equal(t.E.current.principal, OWNER);
  assert.match(t.main.shown(), /3 unsaved entries were not applied\./, 'the counted notice: ' + t.main.shown().slice(0, 300));
});

// ============================================================================
// DEMO2-R16-N2: the form reset itself, before any render
// ============================================================================
test('R16-N2: adopting another person resets the live form at once, before any render', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  t.type(SAM_TYPED);
  const form = t.root.form;
  Object.assign(state, { principal: 'kim', ctx: 'ctx-next' });
  await t.E.boot();
  assert.equal(t.E.current.principal, 'kim', 'fixture: kim adopted');
  assert.equal(form.field.value, 'Alpha', 'the live form still holds the previous person\'s text after adoption');
  assert.notEqual(form.dataset.dirty, '1');
});
