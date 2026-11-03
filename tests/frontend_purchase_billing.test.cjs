/* BY2: the Billing section in Settings (purchase design 4.5, 4.7; BY1 code
   review M1). Comped workspaces show nothing to buy; a purchase seat shows
   "Manage billing"; the Upgrade form shows only when the server says in-app
   checkout is on; a past-due workspace inside grace shows a one-sentence
   banner with the end of grace (copy marked TODO(copy) in views.js).
   Run: node --test tests/frontend_purchase_billing.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadViews(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/' },
    history: { replaceState() {} },
    scrollTo() {},
    // Boot reads one compatible session; nothing else reaches the network.
    fetch: async (url) => {
      if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
      throw new Error('no network in unit test');
    },
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
  const win = sandbox.window;
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await flush(); };
const httpError = (status, message, extra = {}) => Object.assign(new Error(message), { status, ...extra });

/* A hosted page: main element captured, header captured, api stubbed. */
async function hosted(route, api, { principal = 'oidc:https://i.example:carol', email = 'carol@example.test', ws = '', context = null, workspaces = [] } = {}) {
  const win = loadViews(route);
  await settle();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const header = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? header : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  let memberHeader = 0;
  win.EW.header = () => { memberHeader++; };
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.saveNotice = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = principal;
  win.EW.current.identity = { email, subject: '' };
  win.EW.current.activeWorkspace = ws;
  win.EW.current.workspaces = workspaces;
  win.EW.current.workspaceContext = context;
  const calls = [];
  win.EW.api = async (p, o = {}) => { calls.push({ path: p, method: o.method || 'GET', body: o.body }); return api(p, o); };
  win.location.hash = route;
  win.EW.current.route = route;
  const render = async () => { await win.EWViews.render(); await settle(); return main.innerHTML; };
  return { win, main, header, calls, toasts, render, memberHeaders: () => memberHeader };
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#39;|&rsquo;/g, '’').replace(/\s+/g, ' ').trim();
const OWNER = 'oidc:https://i.example:owner';
const PRICES = [{ price_id: 'price_a', amount_cents: 9900, interval: 'month' }];

async function billingSection(billing) {
  const api = (p) => {
    if (p === '/session') return { authenticated: true, principal: OWNER, active_workspace: 'ws', workspaces: ['ws'], frontend_contract: 'earwitness/customer-ui/2' };
    if (p === '/customer/preferences') return { preferences: {} };
    if (p === '/workspaces/ws/members') return { members: [] };
    if (p === '/workspaces/ws/invites') return { invites: [] };
    if (p === '/shares') return { shares: [] };
    if (p === '/billing/status') return billing;
    throw new Error('unexpected ' + p);
  };
  const page = await hosted('#/settings', api, { principal: OWNER, email: 'owner@example.test', ws: 'ws', context: 'ctx-ws', workspaces: ['ws'] });
  const html = await page.render();
  const at = html.indexOf('<h2>Billing</h2>');
  assert.ok(at > 0, 'the Billing section renders');
  return html.slice(at, html.indexOf('</section>', at));
}

test('comped: nothing to buy and nothing to manage', async () => {
  const html = await billingSection({ configured: true, comped: true, seat: false, checkout: false, prices: PRICES,
    entitlement: { state: 'comped', allowed: true } });
  assert.match(html, /data-billing-comped/);
  assert.doesNotMatch(html, /billing-checkout|Upgrade|billing-portal|access blocked|Unknown/);
});

test('seat-backed: Manage billing opens the portal; no Upgrade form', async () => {
  const html = await billingSection({ configured: true, comped: false, seat: true, checkout: false, prices: PRICES,
    entitlement: { state: 'active', allowed: true } });
  assert.match(html, /data-action="billing-portal"[^>]*>Manage billing</);
  assert.doesNotMatch(html, /billing-checkout|Upgrade|data-billing-banner/);
});

test('in-app checkout off: no Upgrade form even when access is blocked', async () => {
  const html = await billingSection({ configured: true, comped: false, seat: false, checkout: false, prices: PRICES,
    entitlement: { state: 'unknown', allowed: false } });
  assert.doesNotMatch(html, /billing-checkout|Upgrade/);
});

test('in-app checkout on: the Upgrade form is back for a blocked workspace', async () => {
  const html = await billingSection({ configured: true, comped: false, seat: false, checkout: true, prices: PRICES,
    entitlement: { state: 'unknown', allowed: false } });
  assert.match(html, /<form id="billing-checkout">/);
  assert.match(html, />Upgrade</);
});

test('past due inside grace: one-sentence banner with the end of grace', async () => {
  const ends = Date.UTC(2026, 10, 14, 12) / 1000;
  const html = await billingSection({ configured: true, comped: false, seat: true, checkout: false, prices: PRICES,
    entitlement: { state: 'past_due', allowed: true, grace_remaining_s: 100, grace_ends_at: ends } });
  const banner = (/<div class="notice warn" role="status" data-billing-banner>[^]*?<\/div>/.exec(html) || [''])[0];
  assert.ok(banner, html);
  assert.equal(text(banner), 'Your payment failed. Access continues until Nov 14.');
  assert.match(html, />Manage billing</);
});

test('no banner without an end of grace or outside past due', async () => {
  for (const ent of [{ state: 'past_due', allowed: true }, { state: 'active', allowed: true, grace_ends_at: 5 }]) {
    const html = await billingSection({ configured: true, seat: true, checkout: false, prices: [], entitlement: ent });
    assert.doesNotMatch(html, /data-billing-banner/);
  }
});
