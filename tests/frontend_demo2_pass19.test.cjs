/* DEMO2 pass 19.
   Red cases: Astra round 8 (demo2-memo-astra-r8.md DEMO2-SEC-R8-01,
   SEC-R8-02) and code review r15 (demo2-code-r15.md DEMO2-R15-01, R15-N1).
   Owner rules, when the signed-in person changes (by server principal;
   the only exception is the same principal):
   - Every live form resets to server data: page forms, Settings drafts
     and dialogs. Nothing the previous person typed survives, shown,
     held or refilled.
   - Every outcome notice and toast about an earlier entry or workspace
     is cleared.
   - A pending signed-out follow-up runs its notice and hold only if the
     identity it started under is still current.
   - The leave-page prompt counts nothing.
   The same person landing in another workspace is told once, in one
   combined notice.
   Only fetch and DOM nodes are simulated; the real EW.api, apiCall,
   boot, render, handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass19.test.cjs */
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
// DEMO2-SEC-R8-01 / DEMO2-R15-01: live page drafts across a person change
// ============================================================================
for (const [trigger, run] of Object.entries(TRIGGERS)) {
  test(`SEC-R8-01 / R15-01 (${trigger}): another person in the same workspace never inherits the live Settings draft`, async () => {
    const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
    const t = await settingsPage(state);
    t.type(SAM_TYPED);
    Object.assign(state, { principal: 'kim', ctx: 'ctx-next' });
    await run(t);
    freshForNext(t, 'kim', trigger);
  });

  test(`SEC-R8-01 / R15-01 (${trigger}): another person in another workspace never inherits the live Settings draft`, async () => {
    const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
    const t = await settingsPage(state);
    t.type(SAM_TYPED);
    Object.assign(state, { principal: 'kim', ws: 'ws-b', ctx: 'ctx-kim-b' });
    await run(t);
    freshForNext(t, 'kim', trigger + ', other workspace');
  });
}

test('SEC-R8-01 control (boot then render): the same person, same workspace, new context keeps the draft, refilled', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  t.type(SAM_TYPED);
  state.ctx = 'ctx-a2';
  await TRIGGERS['boot then render'](t);
  assert.equal(t.E.current.principal, 'sam');
  assert.equal(t.root.form.field.value, SAM_TYPED, 'the same person lost the draft');
  assert.equal(t.root.form.dataset.dirty, '1');
  assert.match(t.root.form.box.innerHTML, /Your unsaved entry was restored/);
  assert.equal(t.unload(), true);
});

test('SEC-R8-01 control (canonical reconcile): the same person, same workspace, new context keeps the draft', async () => {
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  t.type(SAM_TYPED);
  state.ctx = 'ctx-a2';
  await TRIGGERS['canonical reconcile'](t);
  assert.equal(t.E.current.principal, 'sam');
  assert.equal(t.root.form.field.value, SAM_TYPED, 'the same person lost the draft');
  assert.equal(t.root.form.dataset.dirty, '1');
});

test('SEC-R8-01: a stash entry captured from a page published for another person is never refilled', async () => {
  // A path that adopts another person without its own identity wipe:
  // the wipe before every publication still resets the live form, so
  // the in-place stash of that render captures nothing of the previous
  // person.
  const state = { principal: 'sam', ws: 'ws-a', ctx: 'ctx-a' };
  const t = await settingsPage(state);
  t.type(SAM_TYPED);
  // Adopt kim without the identity wipe (as a path that skipped it would).
  t.E.current.principal = 'kim';
  Object.assign(state, { principal: 'kim', ctx: 'ctx-a' });
  await t.win.EWViews.render(); await settleAll(40);
  assert.equal(t.root.form.field.value, 'Alpha', 'a stash entry typed for sam was refilled for kim');
  assert.notEqual(t.root.form.dataset.dirty, '1');
  assert.equal(t.unload(), false, 'the leave-page prompt counts sam\'s stashed entry for kim');
});

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
// DEMO2-SEC-R8-02: outcome notices across a person change
// ============================================================================
test('SEC-R8-02 A: a notice the same person was given does not survive another person signing in; neither does its toast', async () => {
  const t = await alertsPage();
  await openTypedAlert(t);
  await refuseWrite(t, null);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  t.modal.forms = [];
  await signIn(t, { active_workspace: 'ws-b', workspace_context: 'ctx-b' });
  assert.match(t.main.shown(), /Your unsaved entry in ws was not applied\./, 'fixture: the same person was told in ws-b');
  t.toastEl.textContent = 'Your unsaved entry in ws was not applied.';
  await signIn(t, { principal: OTHER, email: 'other@example.test', active_workspace: 'ws-b', workspace_context: 'ctx-other' });
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person is signed in');
  noticeCleared(t, 'after another person signed in');
});

test('SEC-R8-02 B: a signed-out follow-up that resumes after another person is adopted creates no notice', async () => {
  const t = await alertsPage();
  await openTypedAlert(t);
  await refuseWrite(t, { principal: OTHER, email: 'other@example.test', workspace_context: 'ctx-other' });
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
  t.modal.forms = [];
  noticeCleared(t, 'after the follow-up resumed');
  assert.equal(t.E.current.dialogDrafts.size, 0, 'a dialog draft survived');
  assert.equal(t.E.current.signedOutHeld, null, 'held text survived');
});

test('SEC-R8-02 B control: the same person landing in another workspace through the follow-up is told once', async () => {
  const t = await alertsPage();
  await openTypedAlert(t);
  await refuseWrite(t, { active_workspace: 'ws-b', workspace_context: 'ctx-b' });
  assert.equal(t.E.current.principal, OWNER);
  const dropped = (t.E.current.outcomeNotice && t.E.current.outcomeNotice.dropped) || [];
  assert.equal(dropped.length, 1, 'told ' + JSON.stringify(dropped));
  assert.match(dropped[0].text, /in ws were? not applied|in ws was not applied/);
});

// ============================================================================
// DEMO2-R15-N1: one combined notice for the same person, other workspace
// ============================================================================
test('R15-N1: the same person signs in to another workspace with a stashed page entry and a kept dialog draft: one combined notice', async () => {
  const t = await alertsPage();
  await openTypedAlert(t);
  const page = formStub('alert-page-note', null, [['note', 'Page typed']], { dirty: '1' });
  page.closest = () => null;
  t.pageForms.push(page);
  await refuseWrite(t, null);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.equal(t.E.current.signedOutHeld && t.E.current.signedOutHeld.texts.length, 2, 'fixture: both entries are held');
  t.modal.forms = []; t.pageForms.length = 0;
  await signIn(t, { active_workspace: 'ws-b', workspace_context: 'ctx-b' });
  const dropped = (t.E.current.outcomeNotice && t.E.current.outcomeNotice.dropped) || [];
  assert.equal(dropped.length, 1, 'more than one notice: ' + JSON.stringify(dropped));
  assert.match(t.main.shown(), /Your unsaved entries in ws were not applied\./);
  assert.doesNotMatch(t.main.shown(), /2 unsaved entries/);
});
