/* ALERT-DENY-1/-2 (UX 265747d03, Reno batch; ALERT-DENY FIX1 item 3): the
   alert page's lines for matches the rights for their shows withhold. The
   server says "some" or "all" (matches_withheld) and never a count, title
   or source; the page draws UX's line, byte for byte, and every number on
   it is the server's (which already leaves withheld matches out).
   Loader helpers copied from frontend_creator_only.test.cjs.
   Run: node --test tests/frontend_alert_deny_words.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
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
const SOME = 'Some matches can\u2019t be shown here, because the rights for their shows don\u2019t allow it.';
const ALL = 'This alert\u2019s current matches can\u2019t be shown here, because the rights for their shows don\u2019t allow it.';
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

test('ALERT-DENY-1: some withheld: the line, byte for byte, directly under the matches list', async () => {
  const html = (await page('#/alert/5', alertApi({ ...ALERT, matches_withheld: 'some' }))).main.innerHTML;
  const card = changesCard(html);
  assert.ok(card.includes(`</ul><div class="ew-card-body"><p class="section-note">${SOME}</p></div>`), card);
  assert.equal(html.split(SOME).length - 1, 1);
  assert.ok(!html.includes(ALL));
});

test('ALERT-DENY-1: with a pager, the line stays directly under the list, above the pager', async () => {
  const second = { ...EVENT, id: 13, logical_identity: 'finding:ad-a9', ad_id: 'ad:ep_a:1', after_ref: 'sr1:' + 'c'.repeat(32) };
  const alert = { ...ALERT, events: [EVENT, second], unread_event_count: 2, unread_ad_count: 2, matches_withheld: 'some',
    event_page: { page: 1, per_page: 1, total: 2, event_ids: [12], episodes: 2, grouped: true } };
  const card = changesCard((await page('#/alert/5', alertApi(alert))).main.innerHTML);
  assert.match(card, /lib-pager/);
  assert.ok(card.indexOf(SOME) < card.indexOf('lib-pager'), card);
  assert.ok(card.includes(`</ul><div class="ew-card-body"><p class="section-note">${SOME}</p></div>`), card);
});

test('ALERT-DENY-2: all withheld: the line in place of the empty list, never the usual empty state', async () => {
  const alert = { ...ALERT, events: [], unread_event_count: 0, unread_ad_count: 0, matches_withheld: 'all',
    current_matches: { ...ALERT.current_matches, count: 0, tracked: 0, breaks: 0, tracked_by_kind: {} } };
  const html = (await page('#/alert/5', alertApi(alert))).main.innerHTML;
  const card = changesCard(html);
  assert.ok(card.includes(`<div class="ew-card-body"><p class="section-note">${ALL}</p></div>`), card);
  assert.equal(html.split(ALL).length - 1, 1);
  assert.ok(!html.includes(SOME));
  assert.doesNotMatch(card, /No new matches|No baseline yet|Current matches are your starting point/);
});

test('ALERT-DENY: some withheld with an empty list keeps the usual empty state and adds the line under it', async () => {
  const alert = { ...ALERT, events: [], unread_event_count: 0, unread_ad_count: 0, matches_withheld: 'some' };
  const card = changesCard((await page('#/alert/5', alertApi(alert))).main.innerHTML);
  assert.match(card, /No new matches/);
  assert.ok(card.includes(`<div class="ew-card-body"><p class="section-note">${SOME}</p></div>`));
  assert.ok(card.indexOf(SOME) > card.indexOf('No new matches'));
});

test('ALERT-DENY: nothing withheld: neither line', async () => {
  for (const alert of [ALERT, { ...ALERT, events: [], unread_event_count: 0, unread_ad_count: 0 }]) {
    const html = (await page('#/alert/5', alertApi(alert))).main.innerHTML;
    assert.ok(!html.includes(SOME) && !html.includes(ALL));
  }
});

test('ALERT-DENY: no number on the page counts a withheld match: each is the server\'s', async () => {
  // The server's page for one kept event of three (two denied): 1 change,
  // 1 unread, 2 current ads (the scan without the denied match).
  const html = (await page('#/alert/5', alertApi({ ...ALERT, matches_withheld: 'some' }))).main.innerHTML;
  assert.match(html, /<strong>1<\/strong> unread/);
  assert.match(changesCard(html), /1 change · 1 unread/);
  const current = html.slice(html.indexOf('<h3>Current matches</h3>'));
  assert.match(current, /<p class="ew-work-big">2<\/p>/);
  assert.doesNotMatch(html, /\b3 (changes|ads|matches)\b|<strong>3<\/strong>|ew-work-big">3</);
  // The line itself carries no number.
  assert.doesNotMatch(SOME + ALL, /\d/);
});
