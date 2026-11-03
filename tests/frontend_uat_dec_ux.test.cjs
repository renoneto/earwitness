/* UAT decisions round, worker ux:
   UAT-038: Escape in the Search box never runs a search. It restores the
   applied query (the q of the current route) in the box, empty if none, and
   leaves focus in the box. Escape in dialogs, row menus and the narrow menu
   behaves as before (D13).
   UAT-013: the Settings "Results view" section says List view applies to
   the Ads tab.
   Loader copied from frontend_uat_fixes.test.cjs.
   Run: node --test tests/frontend_uat_dec_ux.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
// App timers (the 250 ms preference wait, retries) must not outlive a test.
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
let PREFS = {};
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1, ...PREFS }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, setup) {
  const win = loadAt('#/settings', { fetch: sessionFetch }); // boot on a route with no preference redirect
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  if (setup) setup(win);
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return setup ? win : main.innerHTML;
}

const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const EV_ID = 'named:ep_f.named.120000-180000';

/* -- UAT-038 Escape in the Search box --------------------------------------- */
async function escapeIn(route, field, { dialog, rowMenu, nav, fetches } = {}) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const listeners = [];
  win.document.addEventListener = (type, fn) => listeners.push([type, fn]);
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const went = [];
  win.EW.go = (...a) => went.push(String(a[0]));
  win.EW.menuClosed = 0;
  win.EW.closeMenu = () => { win.EW.menuClosed += 1; };
  win.EW.api = async p => { (fetches || []).push(p); return new Error('no fetch expected'); };
  win.document.querySelector = sel => {
    if (sel === '#modal') return dialog || null;
    if (sel === 'details.ew-row-menu[open]') return rowMenu || null;
    if (sel === '.primary-nav') return nav || null;
    return null;
  };
  win.document.activeElement = field;
  win.EW.current.route = route;
  win.location.hash = route;
  let prevented = 0;
  for (const [t, fn] of listeners) if (t === 'keydown') fn({ key: 'Escape', preventDefault() { prevented += 1; }, target: field });
  stopTimers();
  return { win, went, prevented: () => prevented };
}
const box = value => { const f = { id: 'search-query', value, focused: 0, focus() { f.focused += 1; } }; return f; };

test('UAT-038: Escape with typed text restores the applied query, no navigation, no fetch, focus stays', async () => {
  const fetches = [];
  const field = box('wayfair sofa');
  const { went, prevented } = await escapeIn('#/search?mode=ads&q=wayfair', field, { fetches });
  assert.deepEqual(went, [], 'Escape never navigates or searches');
  assert.deepEqual(fetches, [], 'Escape never fetches');
  assert.equal(field.value, 'wayfair', 'the applied query is back in the box');
  assert.ok(field.focused >= 1, 'focus stays in the box');
  assert.ok(prevented() >= 1, 'the browser own clear-on-Escape is suppressed');
});

test('UAT-038: with no applied query Escape empties the box without a search', async () => {
  const fetches = [];
  const field = box('typed but not run');
  const { went } = await escapeIn('#/search?mode=ads', field, { fetches });
  assert.deepEqual(went, []);
  assert.deepEqual(fetches, []);
  assert.equal(field.value, '');
  assert.ok(field.focused >= 1);
});

test('UAT-038: the applied query is read from the route on every mode, and an empty box with an applied query is restored', async () => {
  const field = box('');
  const { went } = await escapeIn('#/search?mode=mentions&q=free%20trial&page=2', field);
  assert.deepEqual(went, []);
  assert.equal(field.value, 'free trial');
});

test('UAT-038: Escape in a dialog still closes the dialog, not the search box', async () => {
  const field = box('changed');
  let closed = 0;
  const dialog = { open: true, close() { closed += 1; this.open = false; }, querySelector: () => null, querySelectorAll: () => [], dataset: {}, classList: { add() {}, remove() {}, contains: () => false } };
  const { went } = await escapeIn('#/search?mode=ads&q=wayfair', field, { dialog });
  assert.deepEqual(went, []);
  assert.equal(closed, 1, 'Escape closes the dialog');
  assert.equal(dialog.open, false);
  assert.equal(field.value, 'changed', 'the box is left alone while a dialog is open');
});

test('UAT-038: Escape with the narrow menu open closes the menu and leaves the box alone', async () => {
  const field = box('changed');
  const nav = { classList: { contains: c => c === 'menu-open' } };
  const { win, went } = await escapeIn('#/search?mode=ads&q=wayfair', field, { nav });
  assert.deepEqual(went, []);
  assert.equal(win.EW.menuClosed, 1, 'the menu closed');
  assert.equal(field.value, 'changed');
});

test('UAT-038: the Overview box keeps its own Escape (clear only)', async () => {
  const field = { id: 'overview-search-query', value: 'free trial', focus() {} };
  const { went } = await escapeIn('#/overview', field);
  assert.deepEqual(went, []);
  assert.equal(field.value, '');
});

test('UAT-038: the old Escape-runs-a-search path is gone from the source', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  const i = src.indexOf("if (event.key !== 'Escape') return;");
  assert.ok(i > 0);
  const handler = src.slice(i, src.indexOf('\n  });', i));
  assert.doesNotMatch(handler, /goGuarded|E\.go\(|searchURL/);
});

/* -- UAT-013 Settings line ---------------------------------------------------- */
async function settingsHtml() {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : dummy);
  win.EW.header = () => {};
  win.EW.api = async (p) => {
    if (p === '/customer/preferences') return { preferences: { revision: 1, ...PREFS }, revision: 1 };
    throw new Error('unexpected ' + p);
  };
  win.location.hash = '#/settings';
  win.EW.current.route = '#/settings';
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return main.innerHTML;
}

test('UAT-013: the Results view section says List view applies to the Ads tab', async () => {
  PREFS = { results_view: 'cards' };
  const html = await settingsHtml();
  const m = html.match(/<fieldset class="radio-group"[^>]*>[\s\S]*?<\/fieldset>(<p [^>]*>List view applies to the Ads tab\.<\/p>)/);
  assert.ok(m, 'the line sits right under the Results view fieldset');
  assert.match(m[1], /class="field-help"/);
  assert.doesNotMatch(m[1], /style=/);
});
