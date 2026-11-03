/* PERF pass 2: the Alerts hub reads show names only for alerts limited to named shows (Sol DEMO2 r1 finding 06); a timed-out read sent as POST says it took too long, a timed-out write keeps the save wording.
   Run: node --test tests/frontend_judge_perf_alerts.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
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
const canonicalSessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function renderRoute(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}

const ALL_SHOWS = { id: 1, name: 'Zuckerberg', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z', unread_event_count: 0, unread_ad_count: 0,
  query: { version: 2, mode: 'mentions', q: 'Zuckerberg', shows: null } };

test('an all-shows alert list never asks for show names', async () => {
  const asked = [];
  const { html } = await renderRoute('#/alerts', p => {
    asked.push(p);
    if (p === '/customer/alerts') return { alerts: [ALL_SHOWS, { ...ALL_SHOWS, id: 2, name: 'Quince', query: { version: 2, mode: 'ads', q: '', brands: null } }] };
    if (p === '/customer/shows') throw new Error('The server took too long to respond.');
    throw new Error('unexpected request ' + p);
  });
  assert.deepEqual(asked, ['/customer/alerts']);
  assert.doesNotMatch(html, /Show names are unavailable/);
  assert.match(html, /Zuckerberg/);
});

test('an alert limited to named shows still shows their names', async () => {
  const asked = [];
  const { html } = await renderRoute('#/alerts', p => {
    asked.push(p);
    if (p === '/customer/alerts') return { alerts: [ALL_SHOWS, { ...ALL_SHOWS, id: 2, name: 'On Vergecast', query: { version: 2, mode: 'mentions', q: 'Muse', shows: ['show-v'] } }] };
    if (p === '/customer/shows') return { shows: [{ id: 'show-v', display_title: 'The Vergecast' }] };
    throw new Error('unexpected request ' + p);
  });
  assert.ok(asked.includes('/customer/shows'));
  assert.match(html, /The Vergecast/);
});

function hangingApp() {
  const win = loadAt('#/search?mode=ads', { fetch: (url, init) => new Promise((_, reject) => {
    if (init && init.signal) init.signal.addEventListener('abort', () => { const e = new Error('aborted'); e.name = 'AbortError'; reject(e); });
  }) });
  win.setTimeout = setTimeout; win.clearTimeout = clearTimeout;
  win.EW.requestTimeout.set = (fn, ms) => setTimeout(fn, ms);
  win.EW.requestTimeout.readMs = 20;
  win.EW.requestTimeout.writeMs = 20;
  return win;
}

test('a search sent as POST that times out says it took too long, not an unconfirmed change', async () => {
  const win = hangingApp();
  const err = await win.EW.api('/customer/research-query', { method: 'POST', body: { query: { version: 2, mode: 'ads' } } })
    .then(() => null, e => e);
  assert.ok(err, 'the request must fail');
  assert.equal(err.message, 'The server took too long to respond.');
  assert.ok(!err.outcomeUnknown);
});

test('a real write that times out keeps the unconfirmed-change wording', async () => {
  const win = hangingApp();
  const err = await win.EW.api('/customer/alerts', { method: 'POST', body: { name: 'x' } }).then(() => null, e => e);
  assert.ok(err, 'the request must fail');
  assert.equal(err.message, 'Your change could not be confirmed.');
});
