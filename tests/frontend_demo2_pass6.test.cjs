/* DEMO2 pass 6 (code review r5, DEMO2-R5-N1): Overview's "N new matches ·
   K unread" counts unread among the new matches only (the server's
   new_unread_ads), so K is never more than N, and the New tag follows it;
   a latest new match that is a confirmed ad is called "confirmed ad",
   never "possible ad". Harness copied from
   frontend_demo2_pass5_wording.test.cjs.
   Run: node --test tests/frontend_demo2_pass6.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};

function loadPages(route) {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, location: { hash: route }, history: { replaceState() {} }, scrollTo() {},
    fetch: sessionFetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  sandbox.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  sandbox.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  return sandbox;
}
async function pageHtml(route, api) {
  const win = loadPages(route);
  await new Promise(res => setTimeout(res, 0));
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async p => api(p);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return main.innerHTML;
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&#39;/g, "'").replace(/\s+/g, ' ');

// -- R1-05 Overview ---------------------------------------------------------
const overviewApi = totals => p => {
  if (p === '/customer/overview') return { coverage: {}, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [],
    alert_lifecycle_counts: { active_local: totals.length }, alert_totals: totals };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  if (p === '/customer/brands') return { brands: [] };
  if (p.startsWith('/customer/search')) return { items: [] };
  throw new Error('unexpected ' + p);
};
const total = (over = {}) => ({ alert_id: 2, alert_name: 'New Mark Zuckerberg mentions', lifecycle: 'active_local',
  query: { version: 2, mode: 'mentions', q: 'Mark Zuckerberg' }, latest_observed_at: '2026-10-22T07:58:00Z',
  events: 313, unread: 0, ads: 313, unread_ads: 0, latest_kind: 'new_finding',
  new_ads: 0, past_ads: 313, latest_new_kind: null, latest_past_kind: 'new_finding', ...over });

// -- R5-N1 Overview -----------------------------------------------------------
const newTagCount = html => (html.match(/data-tag="new"/g) || []).length;

test('R5-N1 unread counts new matches only, never more than the new count', async () => {
  const html = await pageHtml('#/overview', overviewApi([total({ new_ads: 1, past_ads: 3, unread_ads: 3, unread: 3, ads: 4,
    new_unread_ads: 1, latest_new_kind: 'new_text' })]));
  assert.match(text(html), /1 new match · 1 unread\. 3 earlier matches\./);
  assert.doesNotMatch(text(html), /3 unread/);
});

test('R5-N1 past unread matches only: no new matches, no New tag', async () => {
  const html = await pageHtml('#/overview', overviewApi([total({ new_ads: 0, past_ads: 3, unread_ads: 2, unread: 2, ads: 3,
    new_unread_ads: 0 })]));
  assert.match(text(html), /No new matches\. 3 earlier matches\./);
  assert.equal(newTagCount(html), 0, 'a New tag beside "No new matches."');
});

// PLAIN-ADS (DEP7): a confirmed and an unreviewed latest match are both "ad".
test('R5-N1 a confirmed latest new match is an ad, the same as any other ad', async () => {
  const t = text(await pageHtml('#/overview', overviewApi([total({ new_ads: 2, unread_ads: 1, unread: 1, ads: 315,
    new_unread_ads: 1, latest_new_kind: 'new_finding', latest_new_status: 'confirmed' })])));
  assert.match(t, /Latest new match: an ad\./);
  assert.doesNotMatch(t, /confirmed|possible/);
  const f = text(await pageHtml('#/overview', overviewApi([total({ new_ads: 2, unread_ads: 1, unread: 1, ads: 315,
    new_unread_ads: 1, latest_new_kind: 'new_finding', latest_new_status: 'finding' })])));
  assert.match(f, /Latest new match: an ad\./);
});

// -- DEMO2-R6-01 Overview ---------------------------------------------------
test('R6-01 a paused alert with only past unread matches shows the total unread, not the new-only count', async () => {
  const t = text(await pageHtml('#/overview', overviewApi([total({ lifecycle: 'paused', new_ads: 0, past_ads: 3, unread_ads: 2, unread: 2,
    ads: 3, new_unread_ads: 0 })])));
  // SMALL-UI QW9c: the Paused tag, then the unread count as its own tag.
  assert.match(t, /Paused2 unread/);
});

test('R6-01 a confirmed latest past match is a confirmed ad', async () => {
  const t = text(await pageHtml('#/overview', overviewApi([total({ new_ads: 0, past_ads: 3, ads: 3, latest_new_kind: 'new_finding',
    latest_new_status: 'finding', latest_past_kind: 'new_finding', latest_past_status: 'confirmed' })])));
  assert.match(t, /Latest past match: an ad\./);
});

// -- DEMO2-R7-01 Overview ---------------------------------------------------
// After a history import, the alert's highest event can be a past match.
// The row links (and dates) the match its text names, from that side.
const sideTotal = over => total({ new_ads: 1, past_ads: 4, ads: 5, unread_ads: 1, unread: 1, new_unread_ads: 1,
  latest_kind: 'new_finding', latest_after_ref: 'sr1:past', latest_observed_at: '2026-10-22T07:58:00Z',
  latest_new_kind: 'new_finding', latest_new_after_ref: 'sr1:new', latest_new_observed_at: '2026-09-03T10:00:00Z',
  latest_past_kind: 'new_finding', latest_past_after_ref: 'sr1:past', latest_past_observed_at: '2026-10-22T07:58:00Z', ...over });
const when = html => (html.match(/<span class="ew-ov-when">(.*?)<\/span><\/div>/) || [])[1] || '';

test('R7-01 "Latest new match" opens the latest new match, dated by it', async () => {
  const html = await pageHtml('#/overview', overviewApi([sideTotal()]));
  assert.match(text(html), /Latest new match: an ad\./);
  assert.match(html, /#\/evidence\/sr1%3Anew/);
  assert.doesNotMatch(html, /#\/evidence\/sr1%3Apast/);
  assert.match(text(when(html)), /Sep/);
});

test('R7-01 "Latest past match" opens the latest past match', async () => {
  const html = await pageHtml('#/overview', overviewApi([sideTotal({ new_ads: 0, new_unread_ads: 0,
    latest_new_kind: null, latest_new_after_ref: null, latest_new_observed_at: null })]));
  assert.match(text(html), /Latest past match: an ad\./);
  assert.match(html, /#\/evidence\/sr1%3Apast/);
  assert.match(text(when(html)), /Oct/);
});
