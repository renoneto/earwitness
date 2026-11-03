/* DEMO2 pass 21.
   Red cases: Astra round 10 (demo2-memo-astra-r10.md DEMO2-SEC-R10-01..04)
   and code review r17 (demo2-code-r17.md DEMO2-R17-01, R17-02, R17-N1,
   R17-N2).
   Owner rule: any moved identity generation makes an answer stale, even
   when the same principal comes back (A -> B -> A). The success path is
   guarded too. A stale answer keeps its server-side effect; the view
   renders fresh from server data; the user-facing effects (toast,
   outcome notice, page notice, refill or held draft, leave-prompt count,
   "workspace changed" and "Not saved" marks) are dropped. The same
   person keeps the existing behaviour.
   Fixtures are the pass 20 ones (Settings page form; Alerts with a
   dialog, a notice-painting #main and a toast), plus a Settings display
   form and an invitation entry with no active workspace. Only fetch and
   DOM nodes are simulated; the real EW.api, apiCall, boot, render,
   handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass21.test.cjs */
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
const KIM = { principal: 'kim', ctx: 'ctx-kim' };
const SAM_BACK = { principal: 'sam', ctx: 'ctx-a' };
const SAM_SESSION = reply(200, hostedSession({ principal: 'sam', email: 'sam@example.test', workspace_context: 'ctx-a', active_workspace: 'ws-a' }));
const adopt = async (t, state, who) => { Object.assign(state, who); await TRIGGERS['boot then render'](t); };

// ============================================================================
// DEMO2-SEC-R10-01 / DEMO2-R17-02: sam -> kim -> sam with the ORIGINAL
// context. The real server's token is a hash with no per-session part,
// so the returning person gets the same token back.
// ============================================================================
async function abaSettingsSave(answer, people) {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  const hold = holdRequest(t.win, (url, opts) => opts.method === 'PUT');
  t.type('PRIVATE OLD SAVE');
  const pending = t.win.EWViews.handleSubmit(t.root.form);
  await settleAll(5);
  assert.ok(hold.sent(), 'fixture: sam\'s save was dispatched');
  const stamp = t.win.EWViews.identityStamp();
  for (const who of people) await adopt(t, state, who);
  const before = toasts.length;
  hold.release(answer);
  await pending.catch(() => {});
  await settleAll(80);
  return { t, stamp, toasts: toasts.slice(before) };
}
const OLD_SAVED = reply(200, { ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'PRIVATE OLD SAVE', revision: 2 } });
test('SEC-R10-01: sam, kim, then sam again with the original context: the old save\'s success says nothing', async () => {
  const { t, stamp, toasts } = await abaSettingsSave(OLD_SAVED, [KIM, SAM_BACK]);
  assert.equal(t.E.current.principal, 'sam', 'fixture: sam is back');
  assert.equal(t.E.current.workspaceContext, 'ctx-a', 'fixture: the original context came back');
  assert.equal(t.win.EWViews.identityHolds(stamp), false, 'fixture: the original stamp no longer holds');
  assert.deepEqual(toasts, [], 'the old save\'s success was announced to the returning person');
  assert.ok(!(t.E.current.outcomeNotice && t.E.current.outcomeNotice.saved), 'an outcome notice records the old save');
  assert.equal(t.root.form.field.value, 'Alpha', 'the view renders from server data');
});
test('SEC-R10-01 control: the same person\'s save, with nobody else adopted, still says Saved.', async () => {
  const { t, toasts } = await abaSettingsSave(OLD_SAVED, []);
  assert.equal(t.E.current.principal, 'sam');
  assert.ok(toasts.includes('Saved.'), 'the same person lost the Saved toast: ' + JSON.stringify(toasts));
});

test('R17-02: the request layer treats an answer after A -> B -> A as late even with the original token', async () => {
  const { t, err, toasts } = await lateAlertWrite('saved', [OTHER_SESSION, { workspace_context: 'ctx-owner' }]);
  assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is back');
  assert.equal(t.E.current.workspaceContext, 'ctx-owner', 'fixture: with the original token');
  assert.ok(err && err.droppedContext, 'the answer reached its caller as an ordinary success');
  assert.equal(err && err.lateWrite, 'saved', 'its server effect stands and is classified as saved');
  assert.deepEqual(toasts.filter(m => PREVIOUS_WRITE.test(m)), [], 'the old write was announced');
  assert.ok(!(t.E.current.outcomeNotice && t.E.current.outcomeNotice.saved), 'its outcome notice was recreated');
});

// ============================================================================
// DEMO2-SEC-R10-02: Discard and switch after another person is adopted
// ============================================================================
async function discardSwitch(between) {
  const t = await alertsPage();
  t.E.current.pendingWorkspace = 'ws-b';
  t.E.current.promptDrafts = [];
  const hold = holdRequest(t.win, (url, opts) => url === '/api/v1/session/workspace' && opts.method === 'POST');
  const pending = t.win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await drain(10);
  assert.ok(hold.sent(), 'fixture: the discard switch was dispatched');
  await between(t);
  const before = t.toasts.length;
  hold.release(reply(200, { ...HOSTED, active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 }));
  await pending.catch(() => {});
  await drain(80);
  return { t, toasts: t.toasts.slice(before) };
}
const DISCARD = /discarded|Nothing was saved to/;
test('SEC-R10-02: Discard and switch answering after another person is adopted recreates no notice', async () => {
  const { t, toasts } = await discardSwitch(async t => {
    await signIn(t, OTHER_SESSION);
    assert.equal(t.E.current.outcomeNotice, null, 'fixture: adoption cleared the notices');
  });
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is signed in');
  assert.ok(!DISCARD.test(JSON.stringify(t.E.current.outcomeNotice || null)), 'the previous person\'s discard is recorded: ' + JSON.stringify(t.E.current.outcomeNotice));
  assert.doesNotMatch(t.main.shown(), DISCARD, 'the page paints the previous person\'s discard');
  assert.deepEqual(toasts.filter(m => DISCARD.test(m) || /Switched/.test(m)), [], 'the new person was toasted about the old switch');
});
test('SEC-R10-02 control: the same person\'s Discard and switch still records the discard', async () => {
  const { t } = await discardSwitch(async t => {
    t.srv.session = { ...HOSTED, active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 };
  });
  assert.equal(t.E.current.principal, OWNER);
  assert.match(JSON.stringify(t.E.current.outcomeNotice || null), /Unsaved changes were discarded/, 'the same person lost the discard record');
});

// ============================================================================
// DEMO2-SEC-R10-03 / DEMO2-R17-01: Reload, Retry and Sign in recovery
// actions resuming after another person is adopted
// ============================================================================
async function heldRecovery(action, other) {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  t.type(SAM_TYPED);
  t.root.form.dataset.held = '1';
  const holdSession = holdRequest(t.win, url => url === '/api/v1/session');
  const pending = t.win.EWViews.handleAction({ dataset: { action }, closest: () => null });
  await settleAll(40);
  assert.ok(holdSession.sent(), `fixture: ${action} waits on its session read`);
  if (other) {
    await adopt(t, state, KIM);
    assert.equal(t.E.current.principal, 'kim', 'fixture: kim adopted');
    assert.equal(t.root.form.field.value, 'Alpha', 'fixture: kim\'s page painted');
  }
  const before = toasts.length;
  holdSession.release(SAM_SESSION);
  await pending.catch(() => {});
  await settleAll(60);
  return { t, toasts: toasts.slice(before), all: toasts };
}
const HELD_COPY = /was not applied|were not applied|not kept after signing out|could not be confirmed|not reloaded|ws-a/;
for (const action of ['reload-workspace', 'retry', 'show-signin']) {
  test(`SEC-R10-03 / R17-01 (${action}): the previous person's held entry is not announced to the next person`, async () => {
    const { t, toasts } = await heldRecovery(action, true);
    assert.equal(t.E.current.principal, 'kim', 'fixture: kim is signed in');
    assert.deepEqual(toasts.filter(m => HELD_COPY.test(m)), [], `${action} told kim about sam's entry`);
    assert.ok(!HELD_COPY.test(JSON.stringify(t.E.current.outcomeNotice || null)), `${action} recorded sam's entry for kim: ` + JSON.stringify(t.E.current.outcomeNotice));
    assert.doesNotMatch(t.root.top(), HELD_COPY, 'kim\'s page names sam\'s entry or workspace');
    freshForNext(t, 'kim', action);
  });
}
for (const [action, copy] of [['reload-workspace', 'Your unsaved entry in ws-a was not applied.'], ['retry', 'Your unsaved entry in ws-a was not applied.'],
  ['show-signin', 'Your unsaved entry was not kept after signing out.']]) {
  test(`SEC-R10-03 control (${action}): the same person is still told about the held entry`, async () => {
    const { t, toasts } = await heldRecovery(action, false);
    assert.equal(t.E.current.principal, 'sam');
    assert.ok(toasts.includes(copy), `the same person lost the ${action} message: ` + JSON.stringify(toasts));
  });
}

// ============================================================================
// DEMO2-SEC-R10-04: display-preferences refusal after another person
// ============================================================================
function displayForm(name) {
  const status = { dataset: { state: '' }, textContent: '' };
  const field = { name: 'display_name', type: 'text', value: name, defaultValue: name, removeAttribute() {}, setAttribute() {}, focus() {} };
  const slot = { textContent: '' };
  const box = { innerHTML: '', setAttribute() {}, focus() {}, dataset: {} };
  const submit = { type: 'submit', textContent: 'Save changes', disabled: false };
  const hidden = (n, v) => ({ name: n, type: 'hidden', value: v, defaultValue: v });
  const elements = [hidden('expected_revision', '1'), hidden('display_name_set', name ? '1' : ''), hidden('search_tab', 'ads'),
    hidden('results_view', 'cards'), hidden('default_range', 'all'), hidden('time_zone', ''), field];
  return { status, field, slot, box, submit, dataset: {}, elements, getAttribute: a => (a === 'id' ? 'settings-display' : null),
    closest: () => null, contains: () => false, reset() {},
    querySelector: s => (s === '#pref-display-name' ? field : s === '[data-field-error]' ? slot : s === '.ew-save-state' ? status
      : s === '[type=submit]' ? submit : s === '#form-errors' || s === '[data-form-errors]' ? box : null),
    querySelectorAll: s => (s === '[type=submit]' ? [submit] : []) };
}
async function displayRefusal(other) {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  const toasts = [];
  t.E.toast = m => { toasts.push(m); };
  const pageQuery = t.root.querySelector;
  let live = displayForm('SAM PRIVATE NAME');
  t.root.querySelector = s => (s === '#settings-display' ? live : pageQuery(s));
  const hold = holdRequest(t.win, (url, opts) => opts.method === 'PUT');
  const pending = t.win.EWViews.handleSubmit(live);
  await settleAll(5);
  assert.ok(hold.sent(), 'fixture: sam\'s display preferences were sent');
  if (other) {
    await adopt(t, state, KIM);
    assert.equal(t.E.current.principal, 'kim', 'fixture: kim adopted');
    live = displayForm('');
  }
  hold.release(reply(422, { ok: false, error: { code: 'INVALID', message: 'bad entry' }, workspace_context: 'ctx-a' }));
  await pending.catch(() => {});
  await settleAll(40);
  return { t, live, toasts };
}
test('SEC-R10-04: a display-preferences refusal answering after kim is adopted leaves kim\'s form clean', async () => {
  const { t, live, toasts } = await displayRefusal(true);
  assert.equal(t.E.current.principal, 'kim');
  assert.notEqual(live.status.dataset.state, 'failed', 'kim\'s display form is marked failed by sam\'s save');
  assert.doesNotMatch(live.status.textContent, /Not saved/, 'kim\'s display form says "Not saved"');
  assert.equal(live.box.innerHTML, '', 'kim\'s display form shows a form error');
  assert.deepEqual(toasts.filter(m => PREVIOUS_WRITE.test(m) || /ws-a/.test(m)), []);
});
test('SEC-R10-04 control: the same person\'s refusal still marks the live display form "Not saved"', async () => {
  const { t, live } = await displayRefusal(false);
  assert.equal(t.E.current.principal, 'sam');
  assert.equal(live.status.dataset.state, 'failed');
  assert.equal(live.status.textContent, 'Not saved. Your changes are kept. Try again.');
});

// ============================================================================
// DEMO2-R17-N2: invitation acceptance with no active workspace (every
// person's token is empty)
// ============================================================================
const INVITE = '#/invite?workspace=ws-b&id=inv-1';
const NO_WS = { active_workspace: '', workspace_context: '', workspaces: [] };
async function inviteEntry() {
  const srv = server((p, init = {}) => {
    if (/\/invites\/inv-1\/preflight$/.test(p)) return { invitation: { workspace: 'ws-b', role: 'viewer', workspace_name: 'ws-b' } };
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
async function acceptInvite(other) {
  const t = await inviteEntry();
  assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is on the invitation');
  assert.equal(t.E.current.workspaceContext || '', '', 'fixture: no active workspace, so no token');
  const form = formStub('invite-accept', null, [['workspace', 'ws-b', 'hidden'], ['invite_id', 'inv-1', 'hidden']]);
  const hold = holdRequest(t.win, (url, opts) => /\/accept$/.test(url) && opts.method === 'POST');
  const pending = t.win.EWViews.handleSubmit(form);
  await drain(5);
  assert.ok(hold.sent(), 'fixture: the acceptance was sent');
  if (other) {
    await signIn(t, { principal: OTHER, email: 'other@example.test', ...NO_WS });
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is signed in');
    assert.equal(t.E.current.workspaceContext || '', '', 'fixture: the same empty token');
  }
  const before = t.toasts.length;
  hold.release(reply(200, { ok: true }));
  await pending.catch(() => {});
  await drain(60);
  return { t, toasts: t.toasts.slice(before) };
}
test('R17-N2: an invitation accepted by the previous person never shows "Invitation accepted." to the next one', async () => {
  const { t, toasts } = await acceptInvite(true);
  assert.equal(t.E.current.principal, OTHER);
  assert.ok(!t.E.current.inviteAccepted, 'the previous person\'s acceptance is recorded for the next one: ' + JSON.stringify(t.E.current.inviteAccepted));
  assert.doesNotMatch(t.main.shown(), /Invitation accepted/, 'the next person sees "Invitation accepted."');
  assert.deepEqual(toasts.filter(m => /accepted|Switched/.test(m)), []);
});
test('R17-N2 control: the same person\'s acceptance still shows "Invitation accepted."', async () => {
  const { t } = await acceptInvite(false);
  assert.equal(t.E.current.principal, OWNER);
  assert.equal(t.E.current.inviteAccepted && t.E.current.inviteAccepted.workspace, 'ws-b');
  assert.match(t.main.shown(), /Invitation accepted/);
});

// ============================================================================
// DEMO2-R17-N1: the switch path's own guards, pinned
// ============================================================================
const SWITCHED = /Switched to|another tab or window|could not be confirmed|switch/i;
async function chooseWorkspace(duringConfirm) {
  const t = await alertsPage();
  const goes = [];
  const go = t.E.go;
  t.E.go = r => { goes.push(r); return go(r); };
  const holdPost = holdRequest(t.win, (url, opts) => url === '/api/v1/session/workspace' && opts.method === 'POST');
  const choice = formStub('workspace-choice', null, [['workspace_id', 'ws-b', 'select-one', undefined], ['from_entry', 'invite', 'hidden']]);
  const pending = t.win.EWViews.handleSubmit(choice);
  await drain(10);
  assert.ok(holdPost.sent(), 'fixture: the switch was dispatched');
  const before = t.toasts.length;
  await duringConfirm(t, holdPost);
  await pending.catch(() => {});
  await drain(80);
  return { t, goes, toasts: t.toasts.slice(before) };
}
const SWITCH_REPLY = reply(200, { ...HOSTED, active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 });
test('R17-N1 (success guard): the switch\'s confirming read adopts another person: no switch toast, notice or exit route', async () => {
  const { t, goes, toasts } = await chooseWorkspace(async (t, holdPost) => {
    // The confirming read finds another person in ws-b.
    t.srv.session = { ...HOSTED, ...OTHER_SESSION, active_workspace: 'ws-b', workspace_context: 'ctx-other-b', workspace_switch_revision: 9 };
    holdPost.release(SWITCH_REPLY);
  });
  assert.equal(t.E.current.principal, OTHER, 'fixture: the confirming read adopted the other person');
  assert.deepEqual(toasts.filter(m => SWITCHED.test(m)), [], 'the other person was told about the switch');
  assert.equal(t.E.current.outcomeNotice, null, 'the other person got an outcome notice');
  assert.deepEqual(goes, [], 'the other person was sent to the switch\'s exit route');
});
test('R17-N1 (confirm-failure guard): the confirming read fails after another person was adopted: no failure toast', async () => {
  const { t, goes, toasts } = await chooseWorkspace(async (t, holdPost) => {
    const holdRead = holdRequest(t.win, url => url === '/api/v1/session');
    holdPost.release(SWITCH_REPLY);
    await drain(10);
    assert.ok(holdRead.sent(), 'fixture: the confirming read was dispatched');
    await signIn(t, { ...OTHER_SESSION, active_workspace: 'ws-b', workspace_context: 'ctx-other-b', workspace_switch_revision: 9 });
    assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
    holdRead.release(() => { throw new TypeError('network down'); });
  });
  assert.equal(t.E.current.principal, OTHER);
  assert.deepEqual(toasts.filter(m => SWITCHED.test(m)), [], 'the other person was told the switch failed');
  assert.ok(!JSON.stringify(t.E.current.outcomeNotice || null).includes('elsewhere'), 'the other person got a switch notice');
  assert.deepEqual(goes, []);
});
test('R17-N1 (refused guard): a 409 reconcile refused after A -> B -> A adds no "changed in another tab" notice', async () => {
  const { t, toasts } = await chooseWorkspace(async (t, holdPost) => {
    // DEMO2 pass 22: session reads are adopted newest-issued only, so
    // A -> B -> A happens before the 409 and its reconcile read is the
    // newest one.
    await signIn(t, OTHER_SESSION);
    await signIn(t, {});
    assert.equal(t.E.current.principal, OWNER, 'fixture: the owner is back');
    const holdRead = holdRequest(t.win, url => url === '/api/v1/session');
    holdPost.release(reply(409, { ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'moved' }, workspace_context: 'ctx-owner' }));
    await drain(10);
    assert.ok(holdRead.sent(), 'fixture: the 409 reconcile read was dispatched');
    holdRead.release(reply(200, { ...HOSTED, active_workspace: 'ws-c', workspace_switch_revision: 9, frontend_contract: 'earwitness/customer-ui/99' }));
  });
  assert.ok(t.E.current.bootRefused, 'fixture: the reconcile found a refused workspace');
  assert.ok(!(t.E.current.outcomeNotice && t.E.current.outcomeNotice.elsewhere), 'an old switch recorded "changed in another tab" for the returning person');
  assert.deepEqual(toasts.filter(m => /another tab or window/.test(m)), []);
});
