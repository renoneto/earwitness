/* PREFS-SHARED (LEDGER-LEAD3-AUD-1 item 1, claims-audit Problem 2): each
   member's Settings display name, time zone, Search tab, results view and
   default date range are their own. The server answers each caller with
   their own values (tests/test_member_prefs.py); the client shows what it
   is sent, keeps no other person's preferences across a sign-in, and
   saves with the revision it showed.
   Harness from frontend_dv3_g5.test.cjs (real app.js, ui.js, saved.js and
   views.js in a stubbed browser sandbox).
   Run: node --test tests/frontend_member_prefs.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl,
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
const flush = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });

function loadViews(fetch, route) {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
// A hosted server that answers each person with their own preferences
// (PREFS-SHARED): srv.who is the person signed in now.
function server(byPerson, calls) {
  const srv = { who: 'A', prefs: byPerson };
  const fetch = async (url, init = {}) => {
    const method = init.method || 'GET';
    calls.push({ url, method, who: srv.who, body: init.body ? JSON.parse(init.body) : null });
    const mine = () => srv.prefs[srv.who];
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (url === '/api/v1/customer/preferences' && method === 'GET') return json({ ok: true, workspace_context: 'ctx-ws', preferences: mine() });
    if (url === '/api/v1/customer/preferences' && method === 'PUT') {
      const body = JSON.parse(init.body);
      if (Number(body.expected_revision) !== mine().revision) return json({ ok: false, error: { code: 'CONFLICT', message: 'stale revision', current_revision: mine().revision } }, 412);
      delete body.expected_revision;
      srv.prefs[srv.who] = { ...mine(), ...body, revision: mine().revision + 1 };
      return json({ ok: true, workspace_context: 'ctx-ws', preferences: mine(), revision: mine().revision });
    }
    if (url === '/api/v1/customer/reader-status') return json({ ok: true, workspace_context: 'ctx-ws', configured: false, verified: false, note: 'n' });
    throw new Error('unexpected ' + method + ' ' + url);
  };
  return { fetch, state: srv };
}
async function settingsWin(prefs, calls = []) {
  const { fetch, state } = server(prefs, calls);
  const win = loadViews(fetch, '#/settings');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  return { win, main, state, toasts };
}
// A minimal settings-display form: the fields the submit reads.
function displayForm(win, entries, main) {
  const field = { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }, focus() {}, parentElement: null };
  const slot = { textContent: '' };
  const state = { dataset: {}, textContent: '' };
  const errors = { innerHTML: '', setAttribute() {}, focus() {} };
  const submit = { disabled: false, textContent: 'Save changes' };
  const form = {
    dataset: {}, isConnected: true, elements: [],
    getAttribute: a => (a === 'id' ? 'settings-display' : null),
    closest: () => null, contains: () => false,
    querySelector: s => ({ '#pref-display-name': field, '[data-field-error]': slot, '.ew-save-state': state,
      '[type=submit]': submit, '[data-form-errors]': errors }[s] || null),
    querySelectorAll: () => [],
  };
  win.FormData = class { *entries() { for (const e of entries) yield e; } };
  main.querySelector = s => (s === '#settings-display' ? form : null);
  return { form, field, slot, state, errors };
}

const A = { revision: 5, display_name: 'Alice Judge', time_zone: 'Asia/Tokyo', search_tab: 'episodes', results_view: 'list', default_range: '90d', workspace_name: 'Judges' };
const B = { revision: 3, workspace_name: 'Judges' };

test('PREFS another person adopted in the same tab never gets the previous person\'s cached preferences', async () => {
  const calls = [];
  const { win, state } = await settingsWin({ A: { ...A }, B: { ...B } }, calls);
  assert.ok(win.EW.retainedStoreList().some(s => s.name === 'preferences'), 'preferences is a retained store');
  assert.equal((await win.EW.preferences()).display_name, 'Alice Judge');
  state.who = 'B';
  // What settleIdentity runs when another person is adopted.
  win.EW.clearRetainedStores();
  const seen = await win.EW.preferences();
  assert.equal(seen.display_name, undefined);
  assert.equal(seen.time_zone, undefined);
  assert.equal(win.EW.prefResultsView(), 'cards');
  assert.equal(win.EW.prefSearchTab(), 'ads');
  assert.equal(calls.filter(c => c.url === '/api/v1/customer/preferences' && c.who === 'B').length, 1);
});

test('PREFS Settings for a member with no name of their own shows an empty Display name and saves none', async () => {
  const calls = [];
  const { win, main, state } = await settingsWin({ B: { ...B }, A: { ...A } }, calls);
  state.who = 'B';
  win.EW.clearRetainedStores();
  await win.EWViews.render(); await settleAll();
  const html = main.innerHTML;
  assert.match(html, /id="pref-display-name"[^>]*value=""/);
  assert.match(html, /name="display_name_set" value=""/);
  assert.match(html, /name="expected_revision" value="3"/);
  assert.ok(!html.includes('Alice Judge'), 'no other person\'s name');
  assert.ok(!/value="Asia\/Tokyo" selected/.test(html), 'no other person\'s time zone');
  assert.ok(/<option value="">This device/.test(html));
  const { form } = displayForm(win, [['expected_revision', '3'], ['display_name_set', ''], ['search_tab', 'ads'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', ''], ['display_name', '']], main);
  await win.EWViews.handleSubmit(form); await settleAll();
  const put = calls.find(c => c.method === 'PUT');
  assert.equal(put.who, 'B');
  assert.equal('display_name' in put.body, false);
  assert.equal(put.body.expected_revision, 3);
  assert.equal(state.prefs.A.display_name, 'Alice Judge', 'A is untouched');
});
