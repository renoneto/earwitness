/* Wrong password on the sign-in form (design pass, Fresh): the server answers
   POST /api/v1/session/password with 401 SIGNIN_FAILED. Through the real
   EW.api request path (fetch stubbed, EW.api not replaced):
   1. the sign-in form stays and its own error box says the password is wrong;
      no signed-out boundary redraws the page as "unavailable" with Retry;
   2. a rate limit (429) reads in the form the same way;
   3. a wrong current password on Change password (401 PASSWORD_INCORRECT)
      stays in its form too;
   4. a 401 from any other call keeps its boundary, and so does a signed-out
      Change password (401 SIGNIN_REQUIRED).
   Run: node --test tests/frontend_signin_wrongpw.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const PASSWORD_ONLY = { available: true, reason: '', login_url: null, methods: ['password'] };
const PRINCIPAL = 'oidc:https://pilot.example:acct-' + '1'.repeat(32);

const reply = (status, body, headers = {}) => ({
  ok: status >= 200 && status < 300, status,
  headers: { get: k => (k in headers ? headers[k] : null) },
  text: async () => JSON.stringify(body),
});
const signinRequired = () => reply(401, { error: { code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required', status: 401, details: {} }, signin: PASSWORD_ONLY });
const signedOutSession = () => reply(200, { ok: true, authenticated: false, signin: PASSWORD_ONLY,
  csrf_token: 's'.repeat(32), workspace_context: '', workspace_switch_revision: 0, frontend_contract: 'earwitness/customer-ui/2' });
const signedInSession = () => reply(200, { ok: true, authenticated: true, principal: PRINCIPAL, email: 'judge@example.com',
  subject: 'acct-' + '1'.repeat(32), csrf_token: 'c'.repeat(43), active_workspace: '', workspaces: [], workspace_names: {},
  workspace_switch_revision: 0, signin_method: 'password', frontend_contract: 'earwitness/customer-ui/2' });

const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const text = html => String(html).replace(/<[^>]+>/g, ' ').replace(/&#39;|&rsquo;/g, '’').replace(/\s+/g, ' ').trim();

/* The real app.js, ui.js, saved.js and views.js in one context, with fetch
   answered by `route(url, init)`. EW.api is the app's own. */
function load(routeHash, route) {
  const store = {};
  const nav = { replaced: [], reloads: 0 };
  const calls = [];
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const other = () => ({ innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: routeHash, pathname: '/app/', replace(u) { nav.replaced.push(u); }, reload() { nav.reloads++; } },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url, init = {}) => { calls.push({ url, method: init.method || 'GET' }); return route(url, init); },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: sel => (sel === '#main' ? main : other()), querySelectorAll: () => [], listeners: {},
      addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  const win = sandbox.window;
  win.addEventListener = () => {};
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  win.EW.header = () => {};
  win.EW.toast = () => {};
  win.EW.saveNotice = () => {};
  return { win, nav, main, calls };
}

/* A fake form: named values, a submit button, an error box and a status line. */
function fakeForm(win, id, fields) {
  const els = {};
  // The design pass's busy label: the button says "Signing in…" while busy.
  const submit = { disabled: false, textContent: 'Sign in', setAttribute() {}, removeAttribute() {} };
  const errors = { innerHTML: '', setAttribute() {}, focus() {}, dataset: {} };
  const status = { textContent: '' };
  for (const [name, value] of Object.entries(fields)) els[name] = { name, value, setAttribute() {}, removeAttribute() {}, focus() {}, parentElement: null };
  win.FormData = class { constructor() { this.f = els; } *entries() { for (const e of Object.values(this.f)) yield [e.name, e.value]; } };
  const byId = { '#signin-email': els.email, '#signin-password-input': els.password,
    '#pw-current': els.current_password, '#pw-new': els.new_password, '#pw-confirm': els.confirm_password };
  const form = {
    getAttribute: k => (k === 'id' ? id : null), dataset: {}, setAttribute() {}, removeAttribute() {},
    querySelector: sel => (sel === '[type=submit]' ? submit : sel === '[data-form-errors]' ? errors
      : sel === '[data-password-status]' ? status : byId[sel] || null),
    querySelectorAll: () => [],
  };
  return { form, els, submit, errors, status };
}

/* Signed out on #/overview with the password method; `password` answers the
   sign-in POST. Returns the page after the sign-in form is drawn. */
async function signedOutPage(password, hash = '#/overview') {
  const page = load(hash, (url, init) => {
    // The hosted server's signed-out session answer (asgi._signin_state).
    if (url === '/api/v1/session') return signinRequired();
    if (url === '/api/v1/session/password' && init.method === 'POST') return password();
    return signinRequired();
  });
  await settle();
  page.win.EW.current.hosted = true;
  page.win.EW.current.signin = PASSWORD_ONLY;
  page.win.EW.current.route = hash;
  await page.win.EWViews.render();
  await settle();
  // As in the browser: the page's first read finds no session (401), the
  // boundary checks access again, and that check draws the sign-in form.
  await page.win.EW.api('/customer/overview').catch(() => {});
  await settle();
  return page;
}

const FORM_DRAWN = /id="signin-password"/;

/* What a boundary looks like on this page: EWViews.refusalBoundary runs. */
function watchBoundary(win) {
  const seen = [];
  const real = win.EWViews.refusalBoundary;
  win.EWViews.refusalBoundary = (opts) => { seen.push(opts); return real(opts); };
  return seen;
}


test('red: a wrong password keeps the sign-in form and says so in the form, through the real request path', async () => {
  const page = await signedOutPage(() => reply(401, { error: { code: 'SIGNIN_FAILED', message: 'email or password is incorrect', status: 401, details: {} } }));
  assert.match(page.main.innerHTML, FORM_DRAWN, 'precondition: the signed-out page shows the sign-in form');
  const before = page.main.innerHTML;
  const boundaries = watchBoundary(page.win);
  const { form, errors, els, submit } = fakeForm(page.win, 'signin-password', { email: 'judge@example.com', password: 'wrong' });
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.ok(page.calls.some(c => c.url === '/api/v1/session/password' && c.method === 'POST'), 'the real request went out');
  assert.equal(text(errors.innerHTML), 'Email or password is incorrect.');
  assert.equal(boundaries.length, 0, 'no signed-out boundary starts');
  assert.equal(page.main.innerHTML, before, 'the page is not redrawn');
  assert.doesNotMatch(text(page.main.innerHTML), /unavailable|Retry/i);
  assert.equal(els.password.value, '');
  assert.equal(els.email.value, 'judge@example.com');
  assert.equal(submit.disabled, false);
  assert.equal(submit.textContent, 'Sign in', 'the busy label is cleared');
  assert.equal(page.nav.reloads, 0);
});

test('the invite link page signs in with the same form: a wrong password stays there too', async () => {
  const hash = '#/invite?workspace=acme&id=inv-1';
  const page = await signedOutPage(() => reply(401, { error: { code: 'SIGNIN_FAILED', message: 'email or password is incorrect', status: 401, details: {} } }), hash);
  assert.match(page.main.innerHTML, FORM_DRAWN, 'precondition: the link page shows the sign-in form');
  assert.match(text(page.main.innerHTML), /Open an EarWitness link/);
  const before = page.main.innerHTML;
  const boundaries = watchBoundary(page.win);
  const { form, errors } = fakeForm(page.win, 'signin-password', { email: 'judge@example.com', password: 'wrong' });
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.equal(text(errors.innerHTML), 'Email or password is incorrect.');
  assert.equal(boundaries.length, 0);
  assert.equal(page.main.innerHTML, before);
  assert.equal(page.nav.reloads, 0);
});

test('a rate-limited sign-in (429) reads in the form and keeps the page', async () => {
  const page = await signedOutPage(() => reply(429, { error: { code: 'RATE_LIMITED', message: 'slow down and retry', status: 429, details: {} } }, { 'Retry-After': '30' }));
  const before = page.main.innerHTML;
  const boundaries = watchBoundary(page.win);
  const { form, errors } = fakeForm(page.win, 'signin-password', { email: 'judge@example.com', password: 'again' });
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.equal(text(errors.innerHTML), 'Too many attempts. Try again in a few minutes.');
  assert.equal(boundaries.length, 0);
  assert.equal(page.main.innerHTML, before);
  assert.equal(page.nav.reloads, 0);
});

/* Signed in with a password session on #/account; `change` answers the
   change POST; reads answer 200 with nothing in them. */
async function accountPage(change) {
  const page = load('#/account', (url, init) => {
    if (url === '/api/v1/session') return signedInSession();
    if (url === '/api/v1/session/password/change' && init.method === 'POST') return change();
    return reply(200, { ok: true });
  });
  await settle();
  page.win.EW.current.hosted = true;
  page.win.EW.current.route = '#/account';
  await page.win.EWViews.render();
  await settle();
  return page;
}

test('a wrong current password stays in the Change password form', async () => {
  const page = await accountPage(() => reply(401, { error: { code: 'PASSWORD_INCORRECT', message: 'current password is incorrect', status: 401, details: {} } }));
  assert.match(page.main.innerHTML, /id="password-change"/, 'precondition: the account page shows Change password');
  const before = page.main.innerHTML;
  const readsBefore = page.calls.length;
  const boundaries = watchBoundary(page.win);
  const { form, errors, status } = fakeForm(page.win, 'password-change',
    { current_password: 'old one', new_password: 'a fresh chosen password', confirm_password: 'a fresh chosen password' });
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.equal(text(errors.innerHTML), 'Current password is incorrect.');
  assert.equal(status.textContent, '');
  assert.equal(boundaries.length, 0, 'no boundary');
  assert.equal(page.main.innerHTML, before, 'no boundary redraw');
  assert.deepEqual(page.calls.slice(readsBefore).map(c => c.url), ['/api/v1/session/password/change'], 'no access re-check after a credential refusal');
});

test('other 401s keep their boundary: a signed-out read, and a signed-out Change password', async () => {
  const page = load('#/account', (url) => (url === '/api/v1/session' ? signedInSession() : signinRequired()));
  await settle();
  page.win.EW.current.hosted = true;
  const seen = watchBoundary(page.win);
  const err = await page.win.EW.api('/customer/overview').catch(e => e);
  assert.equal(err.status, 401);
  assert.equal(err.accessRefused, true, 'a 401 read starts the boundary');
  assert.equal(seen.length, 1);
  // Change password while signed out: SIGNIN_REQUIRED is a real sign-out.
  const err2 = await page.win.EW.api('/session/password/change', { method: 'POST', body: { current_password: 'x', new_password: 'y' } }).catch(e => e);
  assert.equal(err2.status, 401);
  assert.equal(err2.accessRefused, true, 'a signed-out change starts the boundary');
  assert.equal(seen.length, 2);
});

test('only the credential refusals are exempt: a 401 SIGNIN_FAILED on another path still starts the boundary', async () => {
  const page = load('#/overview', (url) => (url === '/api/v1/session' ? signedOutSession()
    : reply(401, { error: { code: 'SIGNIN_FAILED', message: 'email or password is incorrect', status: 401, details: {} } })));
  await settle();
  page.win.EW.current.hosted = true;
  const err = await page.win.EW.api('/customer/overview').catch(e => e);
  assert.equal(err.status, 401);
  assert.equal(err.accessRefused, true);
  const err2 = await page.win.EW.api('/session/password', { method: 'POST', body: { email: 'a@b.c', password: 'p' } }).catch(e => e);
  assert.equal(err2.status, 401);
  assert.notEqual(err2.accessRefused, true, 'the sign-in call itself is exempt');
  // Keyed on the code too: a 401 that says sign-in is required is a sign-out wherever it comes from.
  const page3 = load('#/overview', (url) => (url === '/api/v1/session' ? signedOutSession() : signinRequired()));
  await settle();
  page3.win.EW.current.hosted = true;
  const err3 = await page3.win.EW.api('/session/password', { method: 'POST', body: { email: 'a@b.c', password: 'p' } }).catch(e => e);
  assert.equal(err3.status, 401);
  assert.equal(err3.accessRefused, true, 'SIGNIN_REQUIRED from the sign-in call still starts the boundary');
});
