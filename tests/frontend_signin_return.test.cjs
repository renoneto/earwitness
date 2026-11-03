/* RENO-R1-01: a signed-out visitor never lands on a signed-in page's error.
   Reno, on the live site: signed out on the sign-in page, typed an email,
   left the tab for the password manager and came back. The page became
   "Overview is unavailable: browser sign-in is required" with Retry, and
   no sign-in form.
   1. The tab return (window blur then focus, or hidden then visible)
      reads the session; its 401 SIGNIN_REQUIRED is the state the page
      already shows. The sign-in form stays, as typed: no new paint.
   2. A signed-in page whose session ended: a route read's 401
      SIGNIN_REQUIRED ends on the sign-in page, never on
      "<Page> is unavailable: browser sign-in is required".
   Controls: the same person's return keeps the page; a 403 that loses
   access still refuses on the page.
   Fixtures follow tests/frontend_demo2_pass24.test.cjs: only fetch, DOM,
   timers and location nodes are simulated; the real EW.api, apiCall,
   boot, render and the tab-return gate run.
   Run: node --test tests/frontend_signin_return.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

const OWNER = 'oidc:https://i.example:owner';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
// The server's own signed-out answer (asgi._signin_state).
const SIGNIN_REQUIRED = { error: { code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required', status: 401, details: {} },
  signin: { available: true, reason: '', login_url: null, methods: ['password'] } };
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };

class FakeFormData {
  constructor(form) { this.form = form; }
  all() { return ((this.form && this.form.elements) || []).filter(el => el.name).map(el => [el.name, String(el.value ?? '')]); }
  get(k) { const e = this.all().find(([n]) => n === k); return e ? e[1] : null; }
  entries() { return this.all()[Symbol.iterator](); }
}
function loadPages(route, fetch) {
  const listeners = {};
  const docListeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, FormData: FakeFormData, location: { hash: route, pathname: '/app/' },
    history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {}, fetch,
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
// Signed out: every request answers 401 SIGNIN_REQUIRED, as the hosted server does.
function server() {
  const state = { calls: [], signedOut: false, refuse: null, session: {}, sessionDelay: 0 };
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    state.calls.push(`${init.method || 'GET'} ${p}`);
    // sessionDelay: the session read answers after the page's reads.
    if (state.signedOut && p === '/session' && state.sessionDelay) await new Promise(res => setTimeout(res, state.sessionDelay));
    if (state.signedOut) return reply(401, SIGNIN_REQUIRED);
    if (p === '/session') return reply(200, JSON.parse(JSON.stringify({ ...HOSTED, ...state.session })));
    if (state.refuse && p.startsWith(state.refuse.path)) return reply(state.refuse.status, { ok: false, error: state.refuse.error });
    if (p === '/customer/alerts' && init.method === 'POST') return reply(200, { alert: { id: 1 }, workspace_context: 'ctx-owner' });
    if (p === '/customer/brands') return reply(200, { brands: [], workspace_context: 'ctx-owner' });
    if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return reply(200, { alerts: [], workspace_context: 'ctx-owner' });
    if (p.startsWith('/customer/shows')) return reply(200, { shows: [], workspace_context: 'ctx-owner' });
    if (p.startsWith('/customer/saved-items')) return reply(200, { items: [], workspace_context: 'ctx-owner' });
    if (p.startsWith('/customer/preferences')) return reply(200, { preferences: {}, workspace_context: 'ctx-owner' });
    return reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
  };
  return state;
}
function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false, ...props };
}
// The forms a selector list such as 'form[data-dirty="1"], form[data-held="1"]' picks.
const matching = (forms, sel) => (/form/.test(sel) ? forms.filter(f => sel.split(',').some(part => {
  const want = part.match(/data-([a-z]+)="1"/);
  return !want || (f.dataset && f.dataset[want[1]] === '1');
})) : []);
// The native dialog: its forms go when its content is replaced.
function modalStub() {
  let html = '';
  const m = node({ open: false, forms: [], showModal() { this.open = true; }, close() { this.open = false; } });
  Object.defineProperty(m, 'innerHTML', { get: () => html, set: v => { html = v; m.forms = []; } });
  m.querySelectorAll = sel => matching(m.forms, sel);
  m.querySelector = sel => (sel === 'button' ? { focus() {} } : null);
  return m;
}
// A form with one typed text field (changed from its rendered value).
function typedForm(t, id, text, inModal) {
  const field = { name: 'name', type: 'text', value: text, defaultValue: '', dataset: {} };
  const form = node({ id, dataset: { dirty: '1' }, elements: [field], getAttribute: k => (k === 'id' ? id : null),
    closest: sel => (sel === '#modal' && inModal ? t.modal : null) });
  (inModal ? t.modal : t.main).forms.push(form);
  return form;
}
// #main that paints like the browser and remembers every paint.
function mainStub() {
  let html = '';
  const main = node({ inserted: [], painted: [] });
  main.forms = [];
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.inserted = []; main.forms = []; main.painted.push(v); } });
  main.insertAdjacentHTML = (pos, h) => { const b = { html: h, remove() { main.inserted = main.inserted.filter(x => x !== b); } }; main.inserted.unshift(b); main.painted.push(h); };
  main.shown = () => html + main.inserted.map(b => b.html).join('');
  return main;
}
// A page load. signedOut: the server says no one is signed in.
async function page(route, { signedOut = false } = {}) {
  const srv = server();
  srv.signedOut = signedOut;
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = mainStub();
  const modal = modalStub();
  const toastEl = node();
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#toast' ? toastEl : node());
  win.document.querySelectorAll = sel => matching([...(modal.open ? modal.forms : []), ...main.forms], sel);
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  E.toast = () => {};
  win.location.hash = route;
  E.current.route = route;
  await win.EWViews.render();
  await drain(40);
  return { srv, win, E, main, modal };
}
const fireWindow = (t, type, event = {}) => { for (const fn of t.win.listeners[type] || []) fn({ type, ...event }); };
const fireDocument = (t, type, event = {}) => { for (const fn of t.win.docListeners[type] || []) fn({ type, ...event }); };
// Leave and come back: a password manager or another app (blur, focus),
// or another tab (hidden, visible).
async function leaveAndReturn(t, how) {
  if (how === 'blur') {
    fireWindow(t, 'blur');
    fireWindow(t, 'focus');
  } else {
    t.win.document.visibilityState = 'hidden';
    fireDocument(t, 'visibilitychange');
    t.win.document.visibilityState = 'visible';
    fireDocument(t, 'visibilitychange');
  }
  await drain(80);
}
// An in-app navigation (the fixture's history does not move location.hash).
async function nav(t, route) {
  t.win.location.hash = route;
  t.E.current.route = route;
  await t.win.EWViews.render();
  await drain(120);
}
const SIGNIN_FORM = /id="signin-email"/;
const UNAVAILABLE = /unavailable: browser sign-in is required/;
// Session reads after a write's 401 with this fixture: followSignedOut's
// read, then the sign-in page's. The bases read 4: there the follow-up's
// own session 401 started the page boundary again and drew the sign-in
// page twice; that session-read boundary is what RENO-R1-01 drops. One
// more (a write's boundary reading the session too) is RR1-03's mutant.
const WRITE_SESSION_READS = 3;
const SERVER_VALUE_RE = /SERVER-VALUE-WS-A-BRANDS/;
const everPainted = (t, re) => t.main.painted.some(h => re.test(h)) || re.test(t.main.shown());

for (const how of ['blur', 'hidden']) {
  test(`RENO-R1-01: signed out on the sign-in page, leave and return (${how}): the sign-in form stays as typed`, async () => {
    const t = await page('', { signedOut: true });
    assert.match(t.main.shown(), SIGNIN_FORM, 'fixture: the sign-in page shows');
    assert.equal(t.E.current.hosted, true, 'fixture: a hosted, signed-out page');
    const paints = t.main.painted.length;
    const reads = t.srv.calls.length;
    await leaveAndReturn(t, how);
    assert.ok(t.srv.calls.slice(reads).includes('GET /session'), 'fixture: the return read the session');
    assert.equal(everPainted(t, UNAVAILABLE), false, 'the signed-out return painted "<Page> is unavailable: browser sign-in is required"');
    assert.match(t.main.shown(), SIGNIN_FORM, 'the sign-in form is gone after the return');
    // Not painted again: the typed email stays in its field.
    assert.equal(t.main.painted.length, paints, 'the sign-in page was painted again (the typed email would be lost)');
    assert.equal(t.E.current.principal, null);
  });
}

test('RENO-R1-01: a signed-in page whose session ended: a route read\'s 401 ends on the sign-in page, never "<Page> is unavailable"', async () => {
  const t = await page('#/alerts');
  assert.equal(t.E.current.principal, OWNER, 'fixture: signed in');
  assert.doesNotMatch(t.main.shown(), SIGNIN_FORM, 'fixture: the alerts page');
  t.srv.signedOut = true;
  await nav(t, '#/brands');
  assert.equal(everPainted(t, UNAVAILABLE), false, 'a route\'s 401 SIGNIN_REQUIRED painted "<Page> is unavailable: browser sign-in is required"');
  assert.match(t.main.shown(), SIGNIN_FORM, 'the sign-in page did not show after the session ended');
  assert.equal(t.E.current.principal, null, 'the signed-out session was not adopted');
});

test('RENO-R1-01: the session ended, and its session read answers after the page\'s reads: the check waits for it, then the sign-in page', async () => {
  const t = await page('#/alerts');
  t.srv.signedOut = true;
  t.srv.sessionDelay = 30;
  await nav(t, '#/brands');
  await new Promise(res => setTimeout(res, 120));
  await drain(120);
  assert.equal(everPainted(t, UNAVAILABLE), false, 'the check ran before its session read answered and refused on the page');
  assert.match(t.main.shown(), SIGNIN_FORM);
});

test('RENO-R1-01: the session ended, then Retry on the page: still the sign-in page', async () => {
  const t = await page('#/alerts');
  t.srv.signedOut = true;
  await t.win.EWViews.render();
  await drain(120);
  assert.equal(everPainted(t, UNAVAILABLE), false, 'Alerts painted "Alerts are unavailable: browser sign-in is required"');
  assert.match(t.main.shown(), SIGNIN_FORM);
});

test('RENO-R1-01 control: the same person leaves and returns: the page stays as it was, one session read', async () => {
  const t = await page('#/alerts');
  const paints = t.main.painted.length;
  const reads = t.srv.calls.length;
  await leaveAndReturn(t, 'blur');
  assert.deepEqual(t.srv.calls.slice(reads), ['GET /session']);
  assert.equal(t.main.painted.length, paints, 'the same person\'s page was painted again');
  assert.equal(t.E.current.principal, OWNER);
});

test('RENO-R1-01 control: a 403 that loses access still refuses on the page, with no session read', async () => {
  const t = await page('#/alerts');
  t.srv.refuse = { path: '/customer/brands', status: 403, error: { code: 'FORBIDDEN', message: 'You no longer have access to this workspace.', access: 'lost' } };
  const reads = t.srv.calls.length;
  await nav(t, '#/brands');
  assert.match(t.main.shown(), /Brands are unavailable: You no longer have access to this workspace\./);
  assert.doesNotMatch(t.main.shown(), SIGNIN_FORM);
  assert.equal(t.srv.calls.slice(reads).filter(c => c === 'GET /session').length, 0, 'a lost-access 403 read the session');
});

// RR1-01: the session read that finds no one signed in is still an
// authority boundary: the previous person's kept views go and the epoch
// advances, as before. The same person signing in again into another
// workspace never gets the first workspace's kept views.
test('RR1-01: signed out on a tab return, then the same person in another workspace: the first workspace\'s kept views are gone', async () => {
  const t = await page('#/alerts');
  t.E.current.savedNote = { sentinel: true };
  const epoch = t.E.authorityEpoch();
  t.srv.signedOut = true;
  await leaveAndReturn(t, 'blur');
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.match(t.main.shown(), SIGNIN_FORM, 'fixture: the sign-in page shows');
  assert.ok(t.E.authorityEpoch() > epoch, 'the signed-out session read did not advance the authority epoch');
  assert.ok(!(t.E.current.savedNote && t.E.current.savedNote.sentinel), 'a kept view survived the sign-out');
  t.srv.signedOut = false;
  t.srv.session = { active_workspace: 'ws-b', workspaces: ['ws', 'ws-b'], workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 };
  await t.E.boot();
  await drain(40);
  assert.equal(t.E.current.principal, OWNER, 'fixture: the same person again');
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: in the other workspace');
  assert.ok(!(t.E.current.savedNote && t.E.current.savedNote.sentinel), 'the first workspace\'s kept view is still there in ws-b');
});

// RR1-03: a write's 401 behaves exactly as before: the boundary does not
// read the session (the write's own follow-up does, once).
test('RR1-03: a write\'s 401 SIGNIN_REQUIRED: its boundary reads no session (the follow-up does), then the sign-in page', async () => {
  const t = await page('#/alerts');
  t.srv.signedOut = true;
  const reads = t.srv.calls.length;
  const err = await t.E.api('/customer/alerts', { method: 'POST', body: { name: 'x' } }).catch(e => e);
  await drain(160);
  assert.equal(err && err.status, 401, 'fixture: the write was refused');
  assert.equal(err.signedOut, true, 'fixture: a signed-out write');
  assert.match(t.main.shown(), SIGNIN_FORM);
  assert.equal(t.srv.calls.slice(reads).filter(c => c === 'GET /session').length, WRITE_SESSION_READS, 'a write\'s 401 read the session a different number of times than before');
});

// RR2-01: a session read that finds no one signed in runs the 401
// boundary's teardown, as 6e3fff5a did: an untyped dialog closes, a typed
// one becomes the "Access changed" notice (its typed text kept, no server
// values), typed page entries are stashed, and the signed-in page leaves
// the screen. Then the sign-in page shows: the end state of 6e3fff5a.
// (6e3fff5a also painted "<Page> is unavailable" once on the way; that
// paint is RENO-R1-01's and is pinned by its own test below.)
const SERVER_VALUE = 'SERVER-VALUE-WS-A-BRANDS';
const WORKSPACE_CHANGED = /changed in another tab or window/;
async function revokedReturn(t, how = 'blur') {
  t.srv.signedOut = true;
  await leaveAndReturn(t, how);
  await drain(80);
}
for (const how of ['blur', 'hidden']) {
  test(`RR2-01 (${how}): a dialog open with nothing typed, the session ends, tab return: the dialog closes, the sign-in page shows`, async () => {
    const t = await page('#/alerts');
    assert.equal(t.E.openDialog('New alert', `<p>${SERVER_VALUE}</p>`, '', '', { epoch: t.E.authorityEpoch(), route: '#/alerts' }), true, 'fixture: the dialog opened');
    assert.equal(t.modal.open, true, 'fixture: open');
    await revokedReturn(t, how);
    assert.equal(t.E.current.principal, null, 'fixture: signed out');
    assert.equal(t.modal.open && SERVER_VALUE_RE.test(t.modal.innerHTML), false, 'the dialog still shows the workspace\'s values over the signed-out page');
    assert.equal(t.modal.open, false, 'the untyped dialog is still open');
    assert.doesNotMatch(t.modal.innerHTML, SERVER_VALUE_RE, 'the closed dialog still holds the workspace\'s values');
    assert.match(t.main.shown(), SIGNIN_FORM, 'the sign-in page did not show');
  });
}
test('RR2-01: a dialog with typed text, the session ends, tab return: the "Access changed" notice keeps the typed text, no workspace values, the sign-in page shows', async () => {
  const t = await page('#/alerts');
  t.E.openDialog('New alert', `<p>${SERVER_VALUE}</p>`, '', 'alert-form', { epoch: t.E.authorityEpoch(), route: '#/alerts' });
  typedForm(t, 'alert-form', 'MY-TYPED-ALERT', true);
  await revokedReturn(t);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.doesNotMatch(t.modal.open ? t.modal.innerHTML : '', SERVER_VALUE_RE, 'the dialog still shows the workspace\'s values');
  assert.match(t.modal.innerHTML, /data-access-held/, 'the typed dialog was not sealed with the held notice');
  assert.match(t.modal.innerHTML, /MY-TYPED-ALERT/, 'the typed text is not shown back to copy');
  assert.equal([...t.E.current.dialogDrafts.values()].some(d => (d.typed || []).includes('MY-TYPED-ALERT')), true, 'the typed entry was not kept for reopening');
  assert.match(t.main.shown(), SIGNIN_FORM, 'the signed-in page stayed instead of the sign-in page');
  assert.doesNotMatch(t.main.shown(), WORKSPACE_CHANGED, 'the page blames another tab');
});
test('RR2-01: typed text in a page form, the session ends, tab return: the signed-in page goes and the sign-in page shows', async () => {
  const t = await page('#/alerts');
  typedForm(t, 'page-note', 'MY-TYPED-PAGE-NOTE', false);
  await revokedReturn(t);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  assert.match(t.main.shown(), SIGNIN_FORM, 'the signed-in page stayed instead of the sign-in page');
  assert.doesNotMatch(t.main.shown(), WORKSPACE_CHANGED, 'the page blames another tab');
});
test('RR2-01: typed text in a page form, the session ends, tab return: the entry is stashed as on 6e3fff5a (announced when the same person returns to the page)', async () => {
  const t = await page('#/alerts');
  typedForm(t, 'page-note', 'MY-TYPED-PAGE-NOTE', false);
  await revokedReturn(t);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  t.srv.signedOut = false;
  await t.E.boot();
  await drain(20);
  await nav(t, '#/alerts');
  assert.match(t.main.painted.join(' ').replace(/<[^>]+>/g, ' '), /Your unsaved entry in ws was not applied\./, 'the typed page entry was not stashed at the sign-out');
});
test('RENO-R1-01: the session ends with a typed dialog open, tab return: "<Page> is unavailable" never shows on the way', async () => {
  const t = await page('#/alerts');
  t.E.openDialog('New alert', `<p>${SERVER_VALUE}</p>`, '', 'alert-form', { epoch: t.E.authorityEpoch(), route: '#/alerts' });
  typedForm(t, 'alert-form', 'MY-TYPED-ALERT', true);
  await revokedReturn(t);
  assert.equal(everPainted(t, UNAVAILABLE), false, 'the return painted "<Page> is unavailable: browser sign-in is required"');
  assert.match(t.main.shown(), SIGNIN_FORM);
});
