/* ALERT-DENY FIX2: the hosted page loader the FIX2 frontend tests share
   (copied from frontend_alert_deny_words.test.cjs, itself from
   frontend_creator_only.test.cjs). Not a test file itself. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch, toastEl = null } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: sel => (sel === '#toast' ? toastEl : null), querySelectorAll: () => [], addEventListener: () => {},
      body: { style: {} }, createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
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
function loadViews(route) {
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
// A hosted page with a working modal, toasts (with their options) and an API log.
async function page(route, api, { role = 'editor' } = {}) {
  const win = loadViews(route);
  await flushBoot();
  win.EW.current.hosted = true;
  Object.assign(win.EW.current, { principal: 'oidc:https://i.example:judge', workspaceRole: role, activeWorkspace: 'judges-demo' });
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  // Every element handed out records a focus() on it by its selector.
  const focused = [];
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal
    : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} }, focus() { focused.push(sel); } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = (m, opts) => toasts.push([m, opts]); win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, opts = {}) => {
    calls.push([opts.method || 'GET', p, opts.body]);
    const out = await api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, main, modal, toasts, calls, focused };
}
const T1 = '2026-10-14T09:00:00Z';
const EVENT = { id: 12, kind: 'new_finding', logical_identity: 'finding:ad-a8', after_ref: 'sr1:' + 'b'.repeat(32), before_ref: null,
  read_state: 0, historical: 0, observed_at: T1, checked_at: T1, definition_revision: 1, ad_id: 'ad:ep_b:1', summary: null };
const ALERT = { id: 5, name: 'Lumen', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, baseline_count: null, unread_event_count: 1, unread_ad_count: 1, events: [EVENT],
  query: { version: 2, mode: 'ads', q: 'lumen' },
  current_matches: { count: 2, tracked: 2, breaks: 2, tracked_by_kind: { new_finding: 2 }, held_for_brand_review: 0, held_ads_for_brand_review: 0 } };
const alertApi = alert => (p) => {
  if (p === '/customer/alerts/' + alert.id || p.startsWith('/customer/alerts/' + alert.id + '?')) return { alert };
  if (p === '/customer/alerts') return { alerts: [alert] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected request ' + p);
};
const changesCard = html => html.slice(html.indexOf('aria-label="Alert inbox"'), html.indexOf('<aside'));
const SOME = 'Some matches can\u2019t be shown here, because the rights for their shows don\u2019t allow it.';
const ALL = 'This alert\u2019s current matches can\u2019t be shown here, because the rights for their shows don\u2019t allow it.';
// A server refusal as E.api throws it (status, message, body).
function refusal(status, message, error = {}) {
  const err = new Error(message);
  err.status = status;
  err.body = { error: { code: 'FORBIDDEN', message, status, ...error } };
  return err;
}

module.exports = { page, alertApi, changesCard, refusal, ALERT, EVENT, T1, SOME, ALL };
