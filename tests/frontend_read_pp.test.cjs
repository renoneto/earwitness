/* READ-PP (SD5-SCR-R1-01/-03): each judge's alert read state is their own.
   The server sends each caller's own marks and counts; here two judges'
   answers for one workspace render on Overview and on Alerts. Judge B,
   who has read nothing, sees 3 new on both pages after judge A pressed
   Mark all read; judge A sees none on both. The two pages agree for each.
   Harness copied from frontend_demo2_pass6.test.cjs.
   Run: node --test tests/frontend_read_pp.test.cjs */
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

// One workspace: "New Quince ads" with 3 new matches (3 ads) and 96 past
// matches already read for everyone. read = the 3 new are read for this caller.
const events = read => [375, 376, 377].map((id, i) => ({ id, kind: 'new_finding', historical: false, read_state: read ? 1 : 0,
  observed_at: '2026-10-26T05:40:00Z', checked_at: '2026-10-26T05:40:00Z', definition_revision: 1,
  ad_id: `brk:${i}`, after_ref: `sr1:new${i}`, logical_identity: `finding:n${i}` }));
const unreadOf = read => (read ? 0 : 3);

const total = read => ({ alert_id: 4, alert_name: 'New Quince ads', lifecycle: 'active_local',
  query: { version: 2, mode: 'ads', brands: ['quince'] }, latest_observed_at: '2026-10-26T05:40:00Z',
  events: 99, unread: unreadOf(read), ads: 99, unread_ads: unreadOf(read), latest_kind: 'new_finding',
  new_ads: 3, past_ads: 96, new_unread_ads: unreadOf(read), latest_new_kind: 'new_finding', latest_past_kind: 'new_finding' });
const overviewApi = read => p => {
  if (p === '/customer/overview') return { coverage: {}, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [],
    alert_lifecycle_counts: { active_local: 1 }, alert_totals: [total(read)] };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  if (p === '/customer/brands') return { brands: [] };
  if (p.startsWith('/customer/search')) return { items: [] };
  throw new Error('unexpected ' + p);
};
const alertDto = read => ({ id: 4, name: 'New Quince ads', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, baseline_count: 96, last_successful_at: '2026-10-26T05:40:00Z',
  unread_event_count: unreadOf(read), unread_ad_count: unreadOf(read), events: events(read),
  query: { version: 2, mode: 'ads', q: '', brands: ['quince'] } });
const alertsApi = read => p => {
  if (p === '/customer/alerts') return { alerts: [alertDto(read)] };
  throw new Error('unexpected ' + p);
};

const judges = { 'judge A (pressed Mark all read)': true, 'judge B (read nothing)': false };

for (const [who, read] of Object.entries(judges)) {
  test(`${who}: Overview and Alerts agree`, async () => {
    const over = text(await pageHtml('#/overview', overviewApi(read)));
    const list = text(await pageHtml('#/alerts', alertsApi(read)));
    assert.match(over, /New Quince ads/);
    assert.match(list, /New Quince ads/);
    if (read) {
      // VIS-NOV1 #17 (RPP-SCR-R1-01): the judge who read every new match is told so.
      assert.match(over, /3 new matches, all read\. 96 earlier matches\. Latest new match: an ad\./);
      assert.doesNotMatch(over, /unread/);
      assert.doesNotMatch(list, /\d+ new/);
    } else {
      assert.match(over, /3 new matches · 3 unread\. 96 earlier matches\. Latest new match: an ad\./);
      assert.doesNotMatch(over, /all read/);
      assert.match(list, /3 new/);
    }
  });
}

// VIS-NOV1 #17: ", all read" replaces the final "." of "N new matches" only
// when this caller has read every new match (new_unread_ads 0); the rest of
// the line is unchanged, and no other words are new.
const overText = async over => text(await pageHtml('#/overview', p => (p === '/customer/overview'
  ? { ...overviewApi(false)(p), alert_totals: [{ ...total(false), ...over }] } : overviewApi(false)(p))));

test('#17 one of 3 new matches read: the plain line, with its unread count', async () => {
  const t = await overText({ unread: 2, unread_ads: 2, new_unread_ads: 2 });
  assert.match(t, /3 new matches · 2 unread\. 96 earlier matches\./);
  assert.doesNotMatch(t, /all read/);
});

test('#17 one new match, read: "1 new match, all read."', async () => {
  const t = await overText({ new_ads: 1, ads: 97, events: 97, unread: 0, unread_ads: 0, new_unread_ads: 0 });
  assert.match(t, /1 new match, all read\. 96 earlier matches\./);
});

test('#17 no new matches: "No new matches." unchanged', async () => {
  const t = await overText({ new_ads: 0, ads: 96, events: 96, unread: 0, unread_ads: 0, new_unread_ads: 0 });
  assert.match(t, /No new matches\. 96 earlier matches\./);
  assert.doesNotMatch(t, /all read/);
});

test('#17 an older server with no new_unread_ads: the plain line', async () => {
  const t = await overText({ unread: 0, unread_ads: 0, new_unread_ads: undefined });
  assert.match(t, /3 new matches\. 96 earlier matches\./);
  assert.doesNotMatch(t, /all read/);
});
