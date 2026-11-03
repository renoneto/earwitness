/* PW4: built-in email and password sign-in screens (PW design rev 4, 9).
   1. Signed out with the password method: Email and Password fields and a
      Sign in button; the forgot-password line; errors in plain words; an
      admission refusal goes to the existing Access unavailable page.
   2. A signed-in local (password) session gets Change password on the
      account page (current, new, confirm); an OIDC session does not.
   3. Both routes are session operations in app.js: they pass the write
      fence and the epoch rule, and a sign-in clears the old identity.
   Run: node --test tests/frontend_pw_signin.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadViews(route, fetchImpl) {
  const store = {};
  const nav = { replaced: [], reloads: 0 };
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/', replace(u) { nav.replaced.push(u); }, reload() { nav.reloads++; } },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetchImpl || (async (url) => {
      if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
      throw new Error('no network in unit test');
    }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], listeners: {}, addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
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
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return { win, nav };
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await flush(); };
const httpError = (status, message, extra = {}) => Object.assign(new Error(message), { status, ...extra });
const plain = v => JSON.parse(JSON.stringify(v));  // vm objects compare across realms
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#39;|&rsquo;/g, '’').replace(/\s+/g, ' ').trim();

async function hosted(route, api, { principal = null, email = '', ws = '', signin = null } = {}) {
  const { win, nav } = loadViews(route);
  await settle();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {};
  win.EW.saveNotice = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = principal;
  win.EW.current.identity = { email, subject: '' };
  win.EW.current.activeWorkspace = ws;
  win.EW.current.workspaces = [];
  win.EW.current.workspaceContext = null;
  win.EW.current.signin = signin;
  const calls = [];
  win.EW.api = async (p, o = {}) => { calls.push({ path: p, method: o.method || 'GET', body: o.body }); return api(p, o); };
  win.location.hash = route;
  win.EW.current.route = route;
  const render = async () => { await win.EWViews.render(); await settle(); return main.innerHTML; };
  return { win, nav, main, calls, render };
}

const PASSWORD_ONLY = { available: true, reason: '', login_url: null, methods: ['password'] };
const signedOut = signin => () => { throw httpError(401, 'browser sign-in is required', { body: { error: { code: 'SIGNIN_REQUIRED' }, signin } }); };

/* A fake form: named values, a submit button, an error box and a status line. */
function fakeForm(win, id, fields) {
  const els = {};
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

// ---- 1. Sign-in form -------------------------------------------------------

test('signed out with the password method: Email, Password, Sign in, and the forgot line', async () => {
  const page = await hosted('#/overview', signedOut(PASSWORD_ONLY));
  const html = await page.render();
  assert.match(html, /<h1>Sign in<\/h1>/);
  assert.match(html, /<form id="signin-password"[^>]*novalidate/);
  assert.match(html, /<label for="signin-email">Email<\/label><input id="signin-email" name="email" type="email" autocomplete="username"/);
  assert.match(html, /<label for="signin-password-input">Password<\/label><input id="signin-password-input" name="password" type="password" autocomplete="current-password"/);
  assert.match(html, /<button class="button primary" type="submit">Sign in<\/button>/);
  assert.match(text(html), /Forgot your password\? Ask support@getearwitness\.com for a new one\./);
  assert.doesNotMatch(html, /\[CONTACT\]/, 'no placeholder reaches the screen');
  assert.doesNotMatch(text(html), /invited/i, 'the sign-in screen names the support contact, not the inviter');
  assert.match(html, /id="signin-errors" data-form-errors role="alert"/);
  assert.doesNotMatch(html, /data-action="signin-start"/, 'no provider button when only the password method is on');
});

test('both methods: the form and the provider button; an older server keeps the button only', async () => {
  const both = await (await hosted('#/overview', signedOut({ ...PASSWORD_ONLY, login_url: '/api/v1/session/login', methods: ['password', 'oidc'] }))).render();
  assert.match(both, /id="signin-password"/);
  assert.match(both, /data-action="signin-start"/);
  const oidcOnly = await (await hosted('#/overview', signedOut({ ...PASSWORD_ONLY, login_url: '/api/v1/session/login', methods: ['oidc'] }))).render();
  assert.doesNotMatch(oidcOnly, /id="signin-password"/, 'no password form when the server does not offer the password method');
  assert.match(oidcOnly, /data-action="signin-start"/);
  const older = await (await hosted('#/overview', signedOut({ available: true, reason: '', login_url: '/api/v1/session/login' }))).render();
  assert.doesNotMatch(older, /id="signin-password"/);
  assert.match(older, /data-action="signin-start"[^>]*>Sign in</);
});

test('the invite gate signs in with the form when the password method is on', async () => {
  const route = '#/invite?workspace=ws-secret&id=invite:abc123';
  const page = await hosted(route, () => { throw new Error('no reads when signed out'); }, { signin: PASSWORD_ONLY });
  const html = await page.render();
  assert.match(html, /<h1>Open an EarWitness link<\/h1>/);
  assert.match(html, /id="signin-password"/);
  assert.doesNotMatch(html, /data-action="signin-start"|ws-secret|abc123/);
  assert.match(text(html), /Forgot your password\? Ask support@getearwitness\.com for a new one\./);
  assert.doesNotMatch(html, /\[CONTACT\]/, 'no placeholder reaches the screen');
  assert.doesNotMatch(text(html), /invited/i);
});

test('the support contact is one value in views.js', () => {
  const src = require('node:fs').readFileSync(require('node:path').join(__dirname, '..', 'app', 'views.js'), 'utf8');
  assert.equal((src.match(/const SUPPORT_CONTACT = '[^']+';/g) || []).length, 1, 'exactly one SUPPORT_CONTACT definition');
  assert.doesNotMatch(src, /\[CONTACT\]/);
});

test('submit posts email, password and this page as return_to, then reloads there', async () => {
  const route = '#/invite?workspace=ws-secret&id=invite:abc123';
  const page = await hosted(route, (p) => {
    if (p === '/session/password') return { return_to: '/app/' + route };
    throw new Error('unexpected ' + p);
  }, { signin: PASSWORD_ONLY });
  await page.render();
  const { form, submit } = fakeForm(page.win, 'signin-password', { email: '  judge@example.com ', password: 'ABCDE-FGHJK-LMNPQ-RSTUV' });
  let busy = null;
  const api = page.win.EW.api;
  page.win.EW.api = async (p, o) => { busy = submit.disabled; return api(p, o); };
  await page.win.EWViews.handleSubmit(form);
  await settle();
  const post = page.calls.find(c => c.path === '/session/password');
  assert.deepEqual(plain(post && { method: post.method, body: post.body }),
    { method: 'POST', body: { email: 'judge@example.com', password: 'ABCDE-FGHJK-LMNPQ-RSTUV', return_to: '/app/' + route } });
  assert.equal(busy, true, 'the button is busy while signing in');
  assert.deepEqual(page.nav.replaced, ['/app/' + route]);
  assert.equal(page.nav.reloads, 1);
});

test('DPM-R1-12: while signing in the button reads "Signing in…" and the form is aria-busy; a failure restores both', async () => {
  const fail = httpError(401, 'email or password is incorrect', { body: { error: { code: 'SIGNIN_FAILED' } } });
  const page = await hosted('#/overview', (p) => { if (p === '/session/password') throw fail; return signedOut(PASSWORD_ONLY)(); });
  await page.render();
  const { form, submit } = fakeForm(page.win, 'signin-password', { email: 'judge@example.com', password: 'wrong' });
  const attrs = el => { el.attrs = {}; el.setAttribute = (k, v) => { el.attrs[k] = String(v); }; el.removeAttribute = k => { delete el.attrs[k]; }; };
  attrs(form); attrs(submit);
  submit.textContent = 'Sign in';
  let during = null;
  const api = page.win.EW.api;
  page.win.EW.api = async (p, o) => {
    during = { text: submit.textContent, disabled: submit.disabled, formBusy: form.attrs['aria-busy'], buttonBusy: submit.attrs['aria-busy'] };
    return api(p, o);
  };
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.deepEqual(during, { text: 'Signing in…', disabled: true, formBusy: 'true', buttonBusy: 'true' });
  assert.equal(submit.textContent, 'Sign in');
  assert.equal(submit.disabled, false);
  assert.equal(form.attrs['aria-busy'], undefined);
  assert.equal(submit.attrs['aria-busy'], undefined);
  const css = fs.readFileSync(path.join(APP, 'account.css'), 'utf8');
  assert.match(css, /\.signin-card \.button\.primary\[aria-busy=true\]:disabled \{[^}]*background: var\(--ew-action\)/,
    'the busy button keeps a readable fill instead of the pale disabled tint');
});

test('empty fields are refused without a request', async () => {
  const page = await hosted('#/overview', signedOut(PASSWORD_ONLY));
  await page.render();
  for (const [fields, message] of [[{ email: '', password: 'x' }, 'Enter your email.'], [{ email: 'a@b.c', password: '' }, 'Enter your password.']]) {
    const { form, errors } = fakeForm(page.win, 'signin-password', fields);
    await page.win.EWViews.handleSubmit(form);
    assert.match(errors.innerHTML, new RegExp(message.replace('.', '\\.')));
  }
  assert.equal(page.calls.filter(c => c.path === '/session/password').length, 0);
});

test('failures read in plain words; the admission refusal opens Access unavailable', async () => {
  const cases = [
    [httpError(401, 'email or password is incorrect', { body: { error: { code: 'SIGNIN_FAILED' } } }), 'Email or password is incorrect.'],
    [httpError(429, 'Too many requests', { rateLimited: true, body: { error: { code: 'RATE_LIMITED' } } }), 'Too many attempts. Try again in a few minutes.'],
  ];
  for (const [error, message] of cases) {
    const page = await hosted('#/overview', (p) => { if (p === '/session/password') throw error; return signedOut(PASSWORD_ONLY)(); });
    await page.render();
    const { form, errors, els, submit } = fakeForm(page.win, 'signin-password', { email: 'judge@example.com', password: 'wrong' });
    await page.win.EWViews.handleSubmit(form);
    await settle();
    assert.ok(text(errors.innerHTML).includes(message), message);
    assert.equal(els.password.value, '', 'the password field is cleared');
    assert.equal(els.email.value, 'judge@example.com', 'the email stays');
    assert.equal(submit.disabled, false);
    assert.equal(page.nav.reloads, 0);
  }
  const refused = httpError(403, 'this account is not admitted to the pilot', { body: { error: { code: 'FORBIDDEN', message: 'this account is not admitted to the pilot' } } });
  const page = await hosted('#/overview', (p) => { if (p === '/session/password') throw refused; return signedOut(PASSWORD_ONLY)(); });
  await page.render();
  const { form } = fakeForm(page.win, 'signin-password', { email: 'judge@example.com', password: 'right' });
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.equal(page.win.location.hash, '#/access-unavailable');
  assert.equal(page.nav.reloads, 0);
});

// ---- 2. Change password ----------------------------------------------------

function session(method) {
  return { ok: true, authenticated: true, principal: 'oidc:https://pilot.example:acct-' + '1'.repeat(32), email: 'judge@example.com',
    subject: 'acct-' + '1'.repeat(32), csrf_token: 'c'.repeat(43), active_workspace: '', workspaces: [], workspace_names: {},
    workspace_switch_revision: 0, signin_method: method };
}

// QA7-ONEWS: sign-in now opens the one admitted workspace, so the judge
// never passes the chooser; Change password stays reachable from Settings.
test('a password session with an active workspace keeps Change password in Settings', async () => {
  const bound = { ...session('password'), active_workspace: 'solo', workspaces: ['solo'], workspace_names: { solo: 'Solo' },
    workspace_context: 'ctx-solo', workspace_switch_revision: 1, role: 'owner' };
  const page = await hosted('#/settings', (p) => {
    if (p === '/session') return bound;
    if (p === '/customer/preferences') return { preferences: { revision: 1 } };
    throw httpError(500, 'not part of this test');
  }, { principal: bound.principal, email: 'judge@example.com', ws: 'solo' });
  page.win.EW.current.workspaceContext = 'ctx-solo';
  page.win.EW.current.workspaces = ['solo'];
  const html = await page.render();
  assert.match(html, /Settings/);
  assert.match(html, /<h2>Change password<\/h2>/);
  assert.match(html, /<form id="password-change" novalidate>/);
  assert.doesNotMatch(text(html), /Choose a workspace to continue/);
});

test('a password session gets Change password with current, new and confirm', async () => {
  const page = await hosted('#/overview', (p) => { if (p === '/session') return session('password'); throw new Error('unexpected ' + p); },
    { principal: 'oidc:https://pilot.example:acct-' + '1'.repeat(32), email: 'judge@example.com' });
  const html = await page.render();
  assert.match(html, /<h2>Change password<\/h2>/);
  assert.match(html, /<form id="password-change" novalidate>/);
  assert.match(html, /<label for="pw-current">Current password<\/label><input id="pw-current" name="current_password" type="password" autocomplete="current-password"/);
  assert.match(html, /<label for="pw-new">New password<\/label><input id="pw-new" name="new_password" type="password" autocomplete="new-password"/);
  assert.match(html, /<label for="pw-confirm">Confirm new password<\/label><input id="pw-confirm" name="confirm_password" type="password" autocomplete="new-password"/);
  assert.match(html, /<button class="button" type="submit">Change password<\/button>/);
  assert.match(html, /data-password-status/);
  // Sol FIX-1: the account's email, never the acct- subject.
  assert.match(text(html), /Signed in as judge@example\.com\./);
  assert.doesNotMatch(text(html), /acct-/);
  const oidc = await (await hosted('#/overview', (p) => { if (p === '/session') return session('oidc'); throw new Error('unexpected ' + p); },
    { principal: 'oidc:https://accounts.example:u1', email: 'u1@example.com' })).render();
  assert.match(oidc, /Workspace access/);
  assert.doesNotMatch(oidc, /Change password/);
});

test('change: checks on the page first, then success says other devices are signed out', async () => {
  const page = await hosted('#/overview', (p, o) => {
    if (p === '/session') return session('password');
    if (p === '/session/password/change') return { ok: true };
    throw new Error('unexpected ' + p);
  }, { principal: 'oidc:https://pilot.example:acct-' + '1'.repeat(32), email: 'judge@example.com' });
  await page.render();
  for (const [fields, message] of [
    [{ current_password: '', new_password: 'a fresh chosen password', confirm_password: 'a fresh chosen password' }, 'Enter your current password.'],
    [{ current_password: 'old', new_password: 'short', confirm_password: 'short' }, 'Use at least 12 characters for the new password.'],
    [{ current_password: 'old', new_password: 'a fresh chosen password', confirm_password: 'a fresh chosen passwor' }, 'The new passwords don’t match.']]) {
    const { form, errors } = fakeForm(page.win, 'password-change', fields);
    await page.win.EWViews.handleSubmit(form);
    assert.ok(text(errors.innerHTML).includes(message), message);
  }
  assert.equal(page.calls.filter(c => c.path === '/session/password/change').length, 0);
  const { form, els, status } = fakeForm(page.win, 'password-change',
    { current_password: 'old one', new_password: 'a fresh chosen password', confirm_password: 'a fresh chosen password' });
  await page.win.EWViews.handleSubmit(form);
  await settle();
  const post = page.calls.find(c => c.path === '/session/password/change');
  assert.deepEqual(plain(post && { method: post.method, body: post.body }),
    { method: 'POST', body: { current_password: 'old one', new_password: 'a fresh chosen password' } });
  assert.equal(status.textContent, 'Password changed. Other devices are signed out.');
  for (const name of ['current_password', 'new_password', 'confirm_password']) assert.equal(els[name].value, '', name + ' cleared');
});

test('change failures: wrong current, too many, changed elsewhere', async () => {
  const cases = [
    [httpError(401, 'current password is incorrect', { body: { error: { code: 'PASSWORD_INCORRECT' } } }), 'Current password is incorrect.'],
    [httpError(429, 'Too many requests', { rateLimited: true }), 'Too many attempts. Try again in a few minutes.'],
    [httpError(409, 'Your password was changed elsewhere. Sign in again.', { body: { error: { code: 'PASSWORD_CHANGED' } } }), 'Your password was changed elsewhere. Sign in again.'],
  ];
  for (const [error, message] of cases) {
    const page = await hosted('#/overview', (p) => { if (p === '/session') return session('password'); throw error; },
      { principal: 'oidc:https://pilot.example:acct-' + '1'.repeat(32), email: 'judge@example.com' });
    await page.render();
    const { form, errors, status, submit } = fakeForm(page.win, 'password-change',
      { current_password: 'old one', new_password: 'a fresh chosen password', confirm_password: 'a fresh chosen password' });
    await page.win.EWViews.handleSubmit(form);
    await settle();
    assert.ok(text(errors.innerHTML).includes(message), message);
    assert.equal(status.textContent, '');
    assert.equal(submit.disabled, false);
  }
});

// ---- 3. app.js wiring -----------------------------------------------------

test('both password routes are session operations: they pass the write fence and the epoch rule', async () => {
  for (const route of ['/session/password', '/session/password/change']) {
    const seen = [];
    const { win } = loadViews('#/overview', async (url, init) => {
      if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false }) };
      if (url === '/api/v1' + route) seen.push({ url, init });
      return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(route.endsWith('change') ? { ok: true } : { return_to: '/app/' }) };
    });
    await settle();
    win.EW.current.hosted = true;
    win.EW.current.writeFence = { op: 'switch', rev: 3 };
    win.EW.current.displayedWorkspaceSwitchRevision = undefined;
    win.EW.current.csrf = 'c'.repeat(43);
    win.EW.current.sessionCsrf = 's'.repeat(43);
    await win.EW.api(route, { method: 'POST', body: { a: 1 } });
    assert.equal(seen.length, 1, route + ' was dispatched');
    assert.equal(seen[0].url, '/api/v1' + route);
    assert.equal(seen[0].init.headers['Content-Type'], 'application/json');
    assert.equal(seen[0].init.headers['X-EarWitness-Workspace-Switch-Revision'], undefined);
    if (route === '/session/password') assert.equal(win.EW.current.sessionCsrf, '', 'a sign-in clears the old identity');
    else assert.equal(win.EW.current.sessionCsrf, 's'.repeat(43), 'a password change keeps the session');
  }
});

test('password forms are never unsaved drafts: no leave-page prompt, nothing kept to refill', async () => {
  const { win } = loadViews('#/overview');
  const listeners = win.document.listeners;
  for (const id of ['signin-password', 'password-change']) {
    const form = { dataset: {}, classList: { contains: () => false }, getAttribute: k => (k === 'id' ? id : null) };
    const field = { closest: sel => (sel === 'form' ? form : null), matches: () => false };
    for (const type of ['input', 'change']) for (const fn of listeners[type] || []) fn({ type, target: field });
    assert.equal(form.dataset.dirty, undefined, `${id} must not be marked as an unsaved draft`);
  }
});

test('boot keeps the local session email as the identity shown on the account page', async () => {
  const payload = session('password');
  const { win } = loadViews('#/overview', async (url) => {
    if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(payload) };
    throw new Error('no network in unit test');
  });
  await settle();
  await win.EW.boot();
  assert.equal(win.EW.current.identity.email, 'judge@example.com');
  assert.equal(win.EW.signedInName(), 'judge@example.com');
});

test('Sol POLISH-1: both credential forms are capped at 480px and fill narrower screens', () => {
  const css = fs.readFileSync(path.join(APP, 'account.css'), 'utf8');
  const rule = css.match(/#signin-password,\s*#password-change\s*\{([^}]*)\}/);
  assert.ok(rule, 'one rule for both credential forms');
  assert.match(rule[1], /max-width:\s*480px/);
  assert.match(rule[1], /(^|[;\s])width:\s*100%/);
});
