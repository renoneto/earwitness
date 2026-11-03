/* JUDGE-SMALL G1-03 + G1-16 (QA-full F11, F12; all 4 judges).
   G1-03: no Sign out in the header or the 390 Menu; it was only in
   Settings › Workspace access, under a one-option "Workspace" picker and
   a "Use workspace" button.
   G1-16: after sign-out the form stayed at #/settings with no "signed
   out" note, and the next sign-in landed on Settings.
   Now: Sign out in the header's secondary group (the phone menu shows the
   same nav), on the existing sign-out (POST /session/logout with the
   CSRF token); the reload lands on #/overview's sign-in with "You're
   signed out."; the picker and "Use workspace" are hidden when the person
   has exactly one workspace (and it is the active one).
   Harness after frontend_demo2_pass22b: the real EW.api, boot, render,
   header and handleAction run; fetch, DOM and location are simulated.
   Run: node --test tests/frontend_judge_signout.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const OWNER = 'oidc:https://i.example:owner';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws', 'ws-b'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
const text = html => String(html).replace(/<[^>]+>/g, ' ').replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();

function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false, ...props };
}
function memoryStore(init = {}) {
  const data = { ...init };
  return { data, getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: k => { delete data[k]; } };
}
// One page load: a server whose session is `session` (null: signed out),
// the app's own files, and the nodes the shell writes to.
async function load(route, { session = HOSTED, store = memoryStore(), logoutStatus = 200, forms = [] } = {}) {
  const calls = [];
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  const srv = { session };
  const fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    calls.push({ method: init.method || 'GET', path: p, headers: { ...(init.headers || {}) } });
    if (p === '/session') return srv.session ? reply(200, srv.session) : reply(401, { ok: false, error: 'signin', signin: { available: true, methods: ['password'] } });
    if (p === '/session/logout' && init.method === 'POST') {
      if (logoutStatus >= 400) return reply(logoutStatus, { ok: false, error: { code: 'UNAVAILABLE', message: 'down' } });
      srv.session = null; return reply(200, { ok: true });
    }
    if (p === '/customer/preferences') return reply(200, { preferences: {}, workspace_context: 'ctx-owner' });
    return reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
  };
  const replaced = [];
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, location: { hash: route, pathname: '/app/', search: '' },
    history: { replaceState(state, title, url) { replaced.push(String(url)); }, pushState() {}, state: null }, scrollTo() {},
    fetch, localStorage: memoryStore(), sessionStorage: store,
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  // Window listeners are kept: the reload runs the app's own beforeunload guard.
  const listeners = {};
  sandbox.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
  const main = node(), header = node();
  // One native dialog element; it renders and opens like the browser's.
  const modal = node({ open: false, showModal() { modal.open = true; }, close() { modal.open = false; }, querySelector: () => node() });
  // Page forms (JS-R1-01): a form matches the draft selectors by its marks.
  const marked = sel => forms.filter(f => (sel.includes('form[data-dirty') && f.dataset.dirty) || (sel.includes('form[data-held') && f.dataset.held)
    || (sel.includes('form[data-saving') && f.dataset.saving));
  sandbox.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? header : sel === '#modal' ? modal : node());
  sandbox.document.querySelectorAll = sel => marked(String(sel));
  sandbox.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  sandbox.matchMedia = () => ({ matches: false });
  sandbox.EW.toast = () => {};
  const reloads = [], unloadPrompts = [];
  sandbox.location.reload = () => {
    reloads.push(sandbox.EW.current.principal || '');
    // The browser asks "Reload site?" when a beforeunload listener cancels.
    const event = { prevented: false, returnValue: undefined, preventDefault() { event.prevented = true; } };
    for (const fn of listeners.beforeunload || []) fn(event);
    unloadPrompts.push(event.prevented || event.returnValue !== undefined);
  };
  await drain(10); // boot reads the session
  await sandbox.EWViews.render();
  await drain(30);
  return { win: sandbox, E: sandbox.EW, main, header, modal, calls, replaced, reloads, unloadPrompts, store, srv };
}
const signoutButton = html => (html.match(/<button[^>]*data-action="signout"[^>]*>Sign out<\/button>/) || [null])[0];

test('G1-03: the header has Sign out in its secondary group, inside the nav the phone menu opens', async () => {
  const t = await load('#/alerts');
  t.E.header('Alerts');
  const html = t.header.innerHTML;
  const nav = (html.match(/<nav class="primary-nav" id="primary-nav"[\s\S]*?<\/nav>/) || [''])[0];
  const secondary = (nav.match(/<span class="nav-secondary"[\s\S]*?<\/span><\/nav>/) || [''])[0];
  assert.ok(signoutButton(secondary), 'no Sign out button in the header\'s secondary group: ' + secondary);
  assert.match(secondary, /aria-label="Settings, connections and sign out"/);
  // The phone menu (below 1024 px) is this nav opened by Menu (aria-controls).
  assert.match(html, /aria-controls="primary-nav"/);
  // It reads as a nav item in both layouts, the phone menu's 48 px row included.
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  assert.match(css, /\.nav-secondary \.nav-signout \{/, 'no desktop style for the header Sign out');
  const phone = (css.match(/@media \(max-width: 1023px\) \{[\s\S]*?\n\}/) || [''])[0];
  assert.match(phone, /\.nav-signout[^{]*\{[^}]*min-height: 48px/, 'the phone menu Sign out is not a 48 px row');
});

test('G1-03: no Sign out in the header without a signed-in person (local owner mode)', async () => {
  const t = await load('#/alerts');
  t.E.current.principal = '';
  t.E.header('Alerts');
  assert.equal(signoutButton(t.header.innerHTML), null);
});

// The header button is data-action="signout" (signoutButton above): the
// existing sign-out action, the one Settings' Sign out runs.
test('G1-03 + G1-16: Sign out calls the existing sign-out with the CSRF token, then reloads on #/overview with the note', async () => {
  const t = await load('#/settings');
  await t.win.EWViews.handleAction({ dataset: { action: 'signout' }, closest: () => null });
  await drain(20);
  const out = t.calls.filter(c => c.method === 'POST');
  assert.deepEqual(out.map(c => c.path), ['/session/logout'], 'not the existing sign-out (one POST /session/logout)');
  assert.equal(out[0].headers['X-CSRF-Token'], 's'.repeat(32), 'the sign-out carries no CSRF token');
  assert.deepEqual(t.reloads, [OWNER], 'the sign-out did not reload');
  assert.ok(t.replaced.length && /#\/overview$/.test(t.replaced[t.replaced.length - 1]), 'the reload stays on #/settings: ' + JSON.stringify(t.replaced));
  // The reload: signed out, on #/overview, with the note once.
  const next = await load('#/overview', { session: null, store: t.store });
  const page = text(next.main.innerHTML);
  assert.match(page, /You're signed out\./, 'no signed-out note after the reload');
  assert.match(next.main.innerHTML, /id="signin-password"/, 'not the sign-in form');
  const later = await load('#/overview', { session: null, store: t.store });
  assert.doesNotMatch(text(later.main.innerHTML), /You're signed out\./, 'the note shows again on a later load');
});

test('G1-16: a signed-out page load with no sign-out before it has no note', async () => {
  const t = await load('#/overview', { session: null });
  assert.match(t.main.innerHTML, /id="signin-password"/, 'fixture: the sign-in form');
  assert.doesNotMatch(text(t.main.innerHTML), /signed out/i);
});

test('G1-03: Settings hides the picker and "Use workspace" for exactly one workspace; shows them for two', async () => {
  const one = await load('#/settings', { session: { ...HOSTED, workspaces: ['ws'] } });
  const html1 = one.main.innerHTML;
  assert.match(html1, /Workspace access/, 'fixture: the access section');
  assert.doesNotMatch(html1, /id="ws-pick"/, 'one workspace: the picker shows');
  assert.doesNotMatch(html1, />Use workspace</, 'one workspace: Use workspace shows');
  assert.ok(signoutButton(html1), 'one workspace: Sign out is gone from Settings');
  const two = await load('#/settings');
  const html2 = two.main.innerHTML;
  assert.match(html2, /<select id="ws-pick"/, 'two workspaces: no picker');
  assert.match(html2, />Use workspace</, 'two workspaces: no Use workspace');
  assert.ok(signoutButton(html2), 'two workspaces: no Sign out');
});

/* JUDGE-SMALL-FIX1 JS-R1-01 (code r1, High): the header Sign out is on
   every form page. With an unsaved entry it used to POST at once, then the
   app's own beforeunload guard raised the browser's "Reload site?"; Cancel
   left the person's page and draft on screen, signed out, with the
   "You're signed out." flag set. Now the app asks first; "Keep editing"
   changes and sends nothing; a confirmed sign-out clears the draft marks,
   the stash and the held copy before the reload, so nothing can stop it. */
const draftForm = () => node({ dataset: { dirty: '1' }, getAttribute: k => (k === 'id' ? 'alert-form' : null),
  elements: [{ type: 'text', name: 'name', value: 'zz private draft words', dataset: {} }] });
const act = (t, action) => t.win.EWViews.handleAction({ dataset: { action }, closest: () => null });
const posts = t => t.calls.filter(c => c.method === 'POST').map(c => c.path);
const ASK = 'Sign out? Your unsaved entry will not be kept.';

test('JS-R1-01: Sign out with an unsaved entry asks in the app first; nothing is sent until "Sign out" is chosen', async () => {
  // The person types after the page rendered.
  const form = draftForm(), forms = [];
  const t = await load('#/alerts', { forms });
  forms.push(form);
  const seq = t.E.sessionChangeSeq();
  await act(t, 'signout');
  await drain(20);
  assert.deepEqual(posts(t), [], 'the sign-out was sent before the person confirmed');
  assert.deepEqual(t.reloads, [], 'reloaded before the person confirmed');
  assert.equal(t.modal.open, true, 'no in-app question');
  assert.equal(text(t.modal.innerHTML), `${ASK} Sign out Keep editing`, 'the question or its buttons: ' + text(t.modal.innerHTML));
  assert.match(t.modal.innerHTML, /<button[^>]*data-action="signout-confirm"[^>]*>Sign out<\/button>/);
  assert.match(t.modal.innerHTML, /<button[^>]*data-action="signout-keep"[^>]*>Keep editing<\/button>/);
  assert.equal(t.E.sessionChangeSeq(), seq, 'asking started a session change');
  await act(t, 'signout-confirm');
  await drain(20);
  assert.equal(t.modal.open, false, 'the question stays open');
  assert.deepEqual(posts(t), ['/session/logout'], 'the confirmed sign-out was not sent once');
  assert.deepEqual(t.reloads, [OWNER], 'the confirmed sign-out did not reload');
  assert.deepEqual(t.unloadPrompts, [false], 'the browser asks "Reload site?": the leave guard still holds at the reload');
  assert.equal(form.dataset.dirty, undefined, 'the draft mark is still set');
  assert.equal(t.store.getItem('earWitnessSignedOut'), '1', 'no signed-out note for the reload');
  assert.ok(/#\/overview$/.test(t.replaced[t.replaced.length - 1] || ''), 'the reload does not land on Overview');
});

// The reviewer's harness case: an unsaved draft, then Sign out (and the
// in-app "Sign out" when asked) must never raise the browser's prompt.
test('JS-R1-01: a sign-out with an unsaved draft never meets the browser\'s leave prompt at its reload', async () => {
  const form = draftForm(), forms = [];
  const t = await load('#/alerts', { forms });
  forms.push(form);
  await act(t, 'signout');
  await drain(20);
  if (t.modal.open) { await act(t, 'signout-confirm'); await drain(20); }
  assert.deepEqual(t.reloads, [OWNER], 'no reload');
  assert.deepEqual(t.unloadPrompts, [false], 'the leave prompt was raised at the sign-out reload');
});

test('JS-R1-01: "Keep editing" changes nothing and sends nothing', async () => {
  // The person types after the page rendered.
  const form = draftForm(), forms = [];
  const t = await load('#/alerts', { forms });
  forms.push(form);
  const seq = t.E.sessionChangeSeq(), addresses = [...t.replaced];
  await act(t, 'signout');
  await drain(5);
  await act(t, 'signout-keep');
  await drain(20);
  assert.equal(t.modal.open, false, 'the question stays open');
  assert.deepEqual(posts(t), [], '"Keep editing" sent a request');
  assert.deepEqual(t.reloads, [], '"Keep editing" reloaded');
  assert.deepEqual(t.replaced, addresses, '"Keep editing" changed the address');
  assert.equal(form.dataset.dirty, '1', '"Keep editing" dropped the draft mark');
  assert.equal(t.store.getItem('earWitnessSignedOut'), null, '"Keep editing" set the signed-out note');
  assert.equal(t.E.sessionChangeSeq(), seq, '"Keep editing" started a session change');
  assert.equal(t.E.current.principal, OWNER, '"Keep editing" signed the person out');
});

test('JS-R1-01: with nothing unsaved, Sign out goes straight through and the reload is never stopped', async () => {
  const t = await load('#/alerts');
  await act(t, 'signout');
  await drain(20);
  assert.equal(t.modal.open, false, 'asked with nothing unsaved');
  assert.deepEqual(posts(t), ['/session/logout']);
  assert.deepEqual(t.unloadPrompts, [false]);
});

test('JS-R1-01: the "You\'re signed out." note is set only when the sign-out happened', async () => {
  const t = await load('#/alerts', { logoutStatus: 503 });
  await act(t, 'signout');
  await drain(20);
  assert.deepEqual(posts(t), ['/session/logout']);
  assert.equal(t.store.getItem('earWitnessSignedOut'), null, 'a failed sign-out still sets the signed-out note');
});

/* JUDGE-SMALL-FIX1 (JS-R1-07, code r1): the header group's screen-reader
   label names what is in it, Sign out included. */
const groupLabel = html => (html.match(/<span class="nav-secondary" role="group" aria-label="([^"]*)"/) || [])[1];
test('JS-R1-07: the header group label covers Sign out, and only when it is there', async () => {
  const t = await load('#/alerts');
  t.E.header('Alerts');
  assert.equal(groupLabel(t.header.innerHTML), 'Settings, connections and sign out');
  t.E.current.pilotFeatures = { agent_access: false };
  t.E.header('Alerts');
  assert.doesNotMatch(t.header.innerHTML, /href="#\/connections"/, 'fixture: no Connections link');
  assert.equal(groupLabel(t.header.innerHTML), 'Settings and sign out');
  t.E.current.pilotFeatures = null;
  t.E.current.principal = '';
  t.E.header('Alerts');
  assert.equal(groupLabel(t.header.innerHTML), 'Settings and connections');
});
