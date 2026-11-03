/* Plan A excerpt view, PA-FU6: a check of an All mentions alert at excerpt
   level answers plan_limited; the check result says so in place of "No new
   matches". Plan B control. Loader helpers copied from frontend_plan_a_pa9.test.cjs.
   Run: node --test tests/frontend_plan_a_fu6.test.cjs */
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
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route },
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
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const sessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(route = '#/overview') {
  const win = loadAt(route, { fetch: sessionFetch() });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}

const LINE = 'This alert looks in All mentions, which is not included in your plan.';

async function check(answer, { excerpt }) {
  const win = loadViews('#/alert/7');
  await flushBoot();
  if (excerpt) win.EW.notePlan({ excerpt_only: true });
  const box = { textContent: '', innerHTML: '', dataset: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); } };
  win.document.querySelector = sel => (sel === '#alert-progress' ? box : { innerHTML: '', textContent: '' });
  const toasts = [];
  win.EW.toast = m => { toasts.push(m); };
  win.EW.render = () => {};
  const calls = [];
  win.EW.api = async (p, opts) => { calls.push([p, opts && opts.method]); return answer; };
  await win.EWViews.handleAction({ dataset: { action: 'check-alert', id: '7' }, disabled: false, closest: () => null });
  assert.deepEqual(calls, [['/customer/alerts/7/check', 'POST']]);
  return toasts;
}

test('FU6 excerpt level: a plan_limited check says the alert looks in All mentions', async () => {
  const toasts = await check({ ok: true, emitted: 0, plan_limited: true }, { excerpt: true });
  assert.deepEqual(toasts, [LINE]);
});

test('FU6 the plan_limited answer is the excerpt-level signal: an alert page opened first (plan flag unknown) shows the line too', async () => {
  const toasts = await check({ ok: true, emitted: 0, plan_limited: true }, { excerpt: false });
  assert.deepEqual(toasts, [LINE]);
});

test('FU6 excerpt level without plan_limited: the usual no new matches', async () => {
  const toasts = await check({ ok: true, emitted: 0 }, { excerpt: true });
  assert.deepEqual(toasts, ['No new matches in stored records.']);
});

test('FU6 Plan B control: no plan line, the usual messages', async () => {
  assert.deepEqual(await check({ ok: true, emitted: 0 }, { excerpt: false }), ['No new matches in stored records.']);
  assert.deepEqual(await check({ ok: true, emitted: 2 }, { excerpt: false }), ['2 new matches.']);
});
