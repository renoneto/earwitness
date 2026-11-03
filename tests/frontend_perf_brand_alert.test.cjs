/* PERF-BRAND-ALERT (live QA #4): the alert page shows its matches in pages.
   The server pages the events (event_page); the page renders that page's
   rows, keeps every total from the full event list, and links pages with
   the app's pager ("Showing a to b of N changes", "← Previous", "Next →").
   Run: node --test tests/frontend_perf_brand_alert.test.cjs */
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

const ALERT = { id: 1, name: 'Free trial wording', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free trial' },
  current_matches: { count: 48, tracked: 40, breaks: 34, tracked_by_kind: { new_finding: 40 }, held_for_brand_review: 14 } };
const SUM = { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'NPR News Now', ep_title: '12am ET',
  pub_date: '2026-10-09', span: [250.02, 309.24], text: 'Before the break. You can try it free trial today at the site. More words after.',
  historical: false, read: { brand: { key: 'betterhelp', display: 'BetterHelp', confirmed: false }, needs_review: false, historical: false } };
const ev = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:' + id, read_state: 0, historical: false,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM, ...over });
const asked = [];
async function pageHtml(route, alert) {
  asked.length = 0;
  const { html } = await renderRoute(route, p => {
    asked.push(p);
    if (p.startsWith('/customer/alerts/' + alert.id)) return { alert };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: [] };
    throw new Error('unexpected request ' + p);
  });
  return html;
}
// 45 events, each in its own episode; the server's page order is newest first.
const N = 45, SIZE = 20;
const events = Array.from({ length: N }, (_, i) => ev(i + 1, {
  ad_id: 'ad' + (i + 1), observed_at: `2026-10-14T16:${String(i).padStart(2, '0')}:00+00:00`,
  summary: { ...SUM, episode_id: 'ep' + (i + 1), ep_title: 'Episode ' + (i + 1) } }));
const order = events.map(e => e.id).sort((a, b) => b - a);
const paged = (page) => {
  const ids = order.slice((page - 1) * SIZE, page * SIZE);
  return { ...ALERT, unread_event_count: N, unread_ad_count: N,
    events: events.map(e => (ids.includes(e.id) ? e : { ...e, summary: null })),
    event_page: { page, per_page: SIZE, total: N, event_ids: ids } };
};
const rows = html => [...html.matchAll(/<li class="inbox-event ew-work-event" data-event-id="(\d+)"/g)].map(m => Number(m[1]));
// Every change row drawn, with a summary or not (an event off the page has none).
const drawn = html => (html.match(/<li class="inbox-event ew-work-event"/g) || []).length;

test('PERF-BRAND-ALERT page 1 renders only its own matches; totals stay exact', async () => {
  const html = await pageHtml('#/alert/1', paged(1));
  assert.deepEqual(asked.filter(p => p.startsWith('/customer/alerts/')), ['/customer/alerts/1']);
  assert.deepEqual(rows(html), order.slice(0, SIZE));
  assert.equal(drawn(html), SIZE);
  assert.match(html, /45 changes/);
  assert.match(html, /<strong>45<\/strong> unread/);
  assert.match(html, /Showing 1 to 20 of 45 changes/);
  assert.match(html, /href="#\/alert\/1\?page=2"[^>]*>Next →/);
  assert.doesNotMatch(html, /← Previous/);
});

test('PERF-BRAND-ALERT a later page asks the server for that page and links both ways', async () => {
  const html = await pageHtml('#/alert/1?page=2', paged(2));
  assert.deepEqual(asked.filter(p => p.startsWith('/customer/alerts/')), ['/customer/alerts/1?page=2']);
  assert.deepEqual(rows(html), order.slice(SIZE, 2 * SIZE));
  assert.equal(drawn(html), SIZE);
  assert.match(html, /Showing 21 to 40 of 45 changes/);
  assert.match(html, /href="#\/alert\/1"[^>]*>← Previous/);
  assert.match(html, /href="#\/alert\/1\?page=3"[^>]*>Next →/);
  const last = await pageHtml('#/alert/1?page=3', paged(3));
  assert.deepEqual(rows(last), order.slice(2 * SIZE));
  assert.equal(drawn(last), N - 2 * SIZE);
  assert.match(last, /Showing 41 to 45 of 45 changes/);
  assert.doesNotMatch(last, /Next →/);
});

test('PERF-BRAND-ALERT the pager follows the page the server returned', async () => {
  // A page past the last: the server answers with its last page.
  const html = await pageHtml('#/alert/1?page=9', paged(3));
  assert.deepEqual(rows(html), order.slice(2 * SIZE));
  assert.match(html, /Showing 41 to 45 of 45 changes/);
});

test('PBA-R1-05 a page number past any real page asks the server, which answers with its last page', async () => {
  const html = await pageHtml('#/alert/1?page=99999999999999999999', paged(3));
  assert.deepEqual(asked.filter(p => p.startsWith('/customer/alerts/')), ['/customer/alerts/1?page=999999']);
  assert.match(html, /Showing 41 to 45 of 45 changes/);
});

test('PERF-BRAND-ALERT a reply without event_page renders every event and no pager', async () => {
  const html = await pageHtml('#/alert/1', { ...ALERT, unread_event_count: N, events });
  assert.equal(rows(html).length, N);
  assert.doesNotMatch(html, /Showing \d+ to \d+ of/);
});

test('PBA-R1-01 a later page answered without event_page draws every event and no pager', async () => {
  // A server that does not page (no event_page) ignores ?page=: no pager, no lost rows.
  const html = await pageHtml('#/alert/1?page=2', { ...ALERT, unread_event_count: N, events });
  assert.equal(rows(html).length, N);
  assert.doesNotMatch(html, /Showing \d+ to \d+ of|Next →|← Previous/);
});

// PBA-R1-02: 45 events in 9 episodes, every 3 events one ad (15 ads).
// The server pages whole ads (the header's "changes"); here 6 per page.
const ADS = 15, PER = 6;
const adEvents = Array.from({ length: N }, (_, i) => {
  const ad = Math.floor(i / 3), epi = ad % 9;
  return ev(i + 1, { ad_id: 'ad' + ad, observed_at: `2026-10-14T16:${String(i).padStart(2, '0')}:00+00:00`,
    summary: { ...SUM, episode_id: 'ep' + epi, ep_title: 'Episode ' + epi, span: [100 + 60 * i, 130 + 60 * i] } });
});
// Server page order: ads newest first, each ad's events together.
const adOrder = Array.from({ length: ADS }, (_, k) => ADS - 1 - k);
const adPaged = (page) => {
  const ads = new Set(adOrder.slice((page - 1) * PER, page * PER).map(k => 'ad' + k));
  const ids = adOrder.slice((page - 1) * PER, page * PER).flatMap(k => [3 * k + 3, 3 * k + 2, 3 * k + 1]);
  return { ...ALERT, unread_event_count: N, unread_ad_count: ADS,
    events: adEvents.map(e => (ads.has(e.ad_id) ? e : { ...e, summary: null })),
    event_page: { page, per_page: PER, total: ADS, event_ids: ids, episodes: 9, grouped: true } };
};

test('PBA-R1-02 pages count changes (whole ads), and the header counts every event', async () => {
  const shownAds = [];
  for (const [page, from, to] of [[1, 1, 6], [2, 7, 12], [3, 13, 15]]) {
    const html = await pageHtml('#/alert/1' + (page > 1 ? '?page=' + page : ''), adPaged(page));
    assert.match(html, /15 changes · 15 unread · 9 episodes/, `page ${page} header`);
    assert.match(html, new RegExp(`Showing ${from} to ${to} of 15 changes`), `page ${page} pager`);
    // One row per ad: the ad's other events fold into its row.
    const ads = rows(html).map(id => adEvents[id - 1].ad_id);
    assert.equal(ads.length, to - from + 1, `page ${page} rows`);
    assert.equal(new Set(ads).size, ads.length);
    shownAds.push(...ads);
  }
  assert.equal(new Set(shownAds).size, ADS, 'every ad on exactly one page');
});

// PBA-R1-06: the latest check's matches sit on page 3; page 1 says so.
test('PBA-R1-06 a page without the latest check says which page holds it', async () => {
  const checked = '2026-10-14T09:00:00Z';
  const withRev = (e, latest) => ({ ...e, definition_revision: 1, checked_at: latest ? checked : '2026-10-01T00:00:00Z' });
  const reply = (page, latestPages) => {
    const base = paged(page);
    return { ...base, definition_revision: 1, last_successful_definition_revision: 1,
      events: base.events.map(e => withRev(e, e.id <= 3)),
      event_page: { ...base.event_page, latest_check_pages: latestPages } };
  };
  const one = await pageHtml('#/alert/1', reply(1, [3]));
  assert.match(one, /<h3 class="ew-alert-subhead">Latest check · <time[^>]*>[^<]*<\/time><\/h3><p class="section-note ew-work-import">This check’s matches are on <a class="" href="#\/alert\/1\?page=3">page 3<\/a>\.<\/p>/);
  assert.match(one, /Earlier changes/);
  assert.doesNotMatch(one, /No new matches in stored records/);
  const spread = await pageHtml('#/alert/1', reply(1, [2, 3]));
  assert.match(spread, /This check’s matches start on <a class="" href="#\/alert\/1\?page=2">page 2<\/a>\./);
  // The page that holds them lists them under the heading, with no pointer.
  const three = await pageHtml('#/alert/1?page=3', reply(3, [3]));
  assert.match(three, /Latest check · /);
  assert.doesNotMatch(three, /This check’s matches/);
});

// PBA-R1-07: rows with one timestamp keep the order the page always drew
// (lower id first), whatever order the page's ids arrive in.
test('PBA-R1-07 tied rows keep their old order within a page', async () => {
  const tied = Array.from({ length: 6 }, (_, i) => ev(i + 1, { ad_id: 'ad' + (i + 1),
    summary: { ...SUM, episode_id: 'ep' + (i + 1), ep_title: 'Episode ' + (i + 1) } }));
  const unpaged = rows(await pageHtml('#/alert/1', { ...ALERT, unread_event_count: 6, events: tied }));
  assert.deepEqual(unpaged, [1, 2, 3, 4, 5, 6]);
  const ids = [3, 1, 2];
  const html = await pageHtml('#/alert/1', { ...ALERT, unread_event_count: 6, unread_ad_count: 6,
    events: tied.map(e => (ids.includes(e.id) ? e : { ...e, summary: null })),
    event_page: { page: 1, per_page: 3, total: 6, event_ids: ids, episodes: 6, grouped: false } });
  assert.deepEqual(rows(html), [1, 2, 3]);
});
