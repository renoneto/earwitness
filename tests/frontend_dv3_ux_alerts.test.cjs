/* Design v3 UX review: alert page and hub (UX-03, R2-05, UX-11d).
   Run: node --test tests/frontend_dv3_ux_alerts.test.cjs */
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
const rowOf = (html, name) => new RegExp(`<li class="ew-work-alert"[^>]*>(?:(?!</li>).)*${name}(?:(?!</li>).)*</li>`).exec(html)[0];
const count = (html, re) => (html.match(re) || []).length;

const ALERT = { id: 1, name: 'Free trial wording', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free trial' },
  current_matches: { count: 48, tracked: 40, breaks: 34, tracked_by_kind: { new_finding: 40 }, held_for_brand_review: 14 } };
const SUM = { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'NPR News Now', ep_title: '12am ET',
  pub_date: '2026-10-09', span: [250.02, 309.24], text: 'Before the break. You can try it free trial today at the site. More words after.',
  historical: false, read: { brand: { key: 'betterhelp', display: 'BetterHelp', confirmed: false }, needs_review: false, historical: false } };
const ev = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:' + id, read_state: 0, historical: false,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM, ...over });
async function alertHtml(alert, { brands } = {}) {
  const { html } = await renderRoute('#/alert/' + alert.id, p => {
    if (p === '/customer/alerts/' + alert.id) return { alert };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') { if (brands instanceof Error) throw brands; return { brands: brands || [] }; }
    throw new Error('unexpected request ' + p);
  });
  return html;
}

// PLAIN-ADS (DEP7): the row shows no review state.
test('UX-03 a change row shows brand, show, episode, time and the matched words, never a review state', async () => {
  const html = await alertHtml({ ...ALERT, events: [ev(1)], unread_event_count: 1 });
  for (const s of ['BetterHelp', 'NPR News Now', '12am ET', '4:10–5:09']) assert.ok(html.includes(s), s);
  for (const s of ['Possible ad', 'Name not confirmed', 'Needs review']) assert.ok(!html.includes(s), s);
  assert.match(html, /<mark>free<\/mark> <mark>trial<\/mark>/);
  assert.doesNotMatch(html, /New advertising passage/);
  assert.match(html, /href="#\/evidence\/sr1%3A1"[^>]*>Open evidence →/);
});

test('UX-03 a brand under review shows no name and is never highlighted', async () => {
  const read = { brand: { key: null, display: null, confirmed: false, under_review: true }, needs_review: true, historical: false };
  const html = await alertHtml({ ...ALERT, query: { version: 2, mode: 'ads', q: '', brands: null }, unread_event_count: 1,
    events: [ev(1, { summary: { ...SUM, text: 'Simply safe keeps your home secure.', read } })] });
  assert.match(html, /Brand not identified/);
  assert.doesNotMatch(html, /<mark>/);
  assert.doesNotMatch(html, /BetterHelp/);
});

test('UX-03 a brand alert centres the excerpt on a visible brand only', async () => {
  const html = await alertHtml({ ...ALERT, query: { version: 2, mode: 'ads', q: '', brands: ['betterhelp'] }, unread_event_count: 1,
    events: [ev(1, { summary: { ...SUM, text: 'News first. BetterHelp is online therapy.' } })] }, { brands: [{ key: 'betterhelp', display: 'BetterHelp' }] });
  assert.match(html, /<mark>BetterHelp<\/mark>/);
});

test('UX-03 an event with no summary keeps today\'s row', async () => {
  const html = await alertHtml({ ...ALERT, events: [ev(1, { summary: null })], unread_event_count: 1 });
  assert.match(html, /New advertising passage/);
  assert.match(html, /href="#\/evidence\/sr1%3A1"[^>]*>Open evidence →/);
  const gone = await alertHtml({ ...ALERT, events: [ev(1, { summary: null, after_ref: null })], unread_event_count: 1 });
  assert.match(gone, /historical target is unavailable/);
});

test('UX-03 events in one episode group under one head; counts stay exact', async () => {
  const at = s => ({ ...SUM, span: [s, s + 30] });
  const html = await alertHtml({ ...ALERT, unread_event_count: 3, unread_ad_count: 3,
    events: [ev(1, { summary: at(500) }), ev(2, { summary: at(100) }), ev(3, { summary: at(900) })] });
  assert.equal(count(html, /class="ew-work-event-group"/g), 1);
  assert.match(html, /ew-work-group-head[^>]*>(?:(?!<\/div>).)*3 changes/);
  assert.match(html, /3 changes · 3 unread · 1 episode/);
  assert.equal(count(html, /data-event-id="/g), 3, 'every event keeps its own row');
  const order = ['sr1%3A2', 'sr1%3A1', 'sr1%3A3'].map(r => html.indexOf(`#/evidence/${r}"`));
  assert.deepEqual(order.slice().sort((a, b) => a - b), order, 'rows ordered by time in the episode');
});

test('UX-03 a passage inside an ad already listed folds into that ad\'s row', async () => {
  const text = { ...SUM, record_kind: 'source_publication', status: 'mention', span: [260, 270], text: 'try it free trial', read: null };
  const html = await alertHtml({ ...ALERT, unread_event_count: 2, unread_ad_count: 2,
    events: [ev(1), ev(2, { kind: 'new_text', summary: text })] });
  assert.equal(count(html, /<li class="inbox-event ew-work-event"/g), 1, 'one row');
  assert.match(html, /<li class="inbox-event ew-work-event" data-event-id="1">(?:(?!<\/li>).)*class="ew-work-also" data-event-id="2"/);
  assert.match(html, /Also matched the words at /);
  assert.match(html, /Open passage →/);
  assert.match(html, /2 changes · 2 unread/);
});

test('UX-03 all-historical imports say so once, not on every row', async () => {
  const html = await alertHtml({ ...ALERT, unread_event_count: 2,
    events: [ev(1, { historical: true }), ev(2, { historical: true, summary: { ...SUM, episode_id: 'ep2' } })] });
  assert.equal(count(html, /Earlier matches — already in the collection when this alert was created\. Added on/g), 1);
  assert.doesNotMatch(html, /Historical import/);
  const mixed = await alertHtml({ ...ALERT, unread_event_count: 2,
    events: [ev(1, { historical: true }), ev(2, { summary: { ...SUM, episode_id: 'ep2' } })] });
  assert.equal(count(mixed, /Historical import/g), 1);
  assert.doesNotMatch(mixed, /Earlier matches — already in the collection/);
});

test('R2-05 the alert page labels its counts and Mark all read sits in the Changes head', async () => {
  const html = await alertHtml({ ...ALERT, events: [ev(1)], unread_event_count: 1 });
  assert.match(html, /Current matches/);
  assert.match(html, /In stored records: 34 ads\./);
  assert.match(html, /14 more ads have Brand not identified and aren’t counted here/);
  assert.doesNotMatch(html, /new matches in stored data/);
  assert.match(html, /<div class="ew-card-head"><h2>Changes<\/h2>(?:(?!<\/div>).)*data-action="mark-read"/);
  const aside = html.slice(html.indexOf('<aside'), html.indexOf('</aside>'));
  assert.doesNotMatch(aside, /data-action="mark-read"/);
  const plain = await alertHtml({ ...ALERT, current_matches: { count: 3 }, events: [], unread_event_count: 0 });
  assert.doesNotMatch(plain, /In stored records|wait for brand review/, 'an older server says only the count');
});

test('UX-11d alert page eyebrow names the kind from the stored query', async () => {
  assert.match(await alertHtml({ ...ALERT, events: [] }), /<p class="eyebrow">Words alert</);
  assert.match(await alertHtml({ ...ALERT, query: { version: 2, mode: 'ads', q: '', brands: ['betterhelp'] }, events: [] },
    { brands: [{ key: 'betterhelp', display: 'BetterHelp' }] }), /<p class="eyebrow">Brand alert</);
  assert.match(await alertHtml({ ...ALERT, query: { version: 2, mode: 'mentions', q: '' }, events: [] }), /<p class="eyebrow">Alert</);
});

const HUB = [
  { id: 1, name: 'Free trial wording', revision: 1, lifecycle: 'active_local', baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z',
    unread_event_count: 40, unread_ad_count: 34, query: { mode: 'ads', q: 'free trial', brands: null } },
  { id: 2, name: 'BetterHelp reads', revision: 1, lifecycle: 'active_local', baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z',
    unread_event_count: 25, unread_ad_count: 21, query: { mode: 'ads', q: '', brands: ['betterhelp'] } },
  { id: 3, name: 'Promo code mentions', revision: 1, lifecycle: 'paused', baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z',
    unread_event_count: 150, unread_ad_count: 120, query: { mode: 'mentions', q: 'promo code' } },
  { id: 4, name: 'Narrowed', revision: 1, lifecycle: 'active_local', baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z',
    unread_event_count: 0, unread_ad_count: 0, query: { mode: 'ads', q: 'x', brands: ['acme'] } },
];
async function hubHtml(brands) {
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: HUB };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') { if (brands instanceof Error) throw brands; return { brands }; }
    throw new Error('unexpected request ' + p);
  });
  return html;
}

test('UX-11d hub kind tags come from the stored query', async () => {
  const html = await hubHtml([{ key: 'betterhelp', display: 'BetterHelp' }]);
  const bh = rowOf(html, 'BetterHelp reads');
  assert.match(bh, /data-tag="brand"/);
  assert.doesNotMatch(bh, /data-tag="words"/);
  assert.match(bh, /ew-work-kind-value">BetterHelp</);
  assert.match(bh, /brand BetterHelp/);
  assert.match(rowOf(html, 'Free trial wording'), /data-tag="words"/);
  assert.match(rowOf(html, 'Narrowed'), /data-tag="words"/, 'words narrowed by brand is a Words alert');
  assert.doesNotMatch(rowOf(html, 'Narrowed'), /data-tag="brand"/);
  assert.doesNotMatch(html, /Any phrase/);
  const failed = await hubHtml(new Error('down'));
  assert.match(rowOf(failed, 'BetterHelp reads'), /betterhelp/);
  assert.doesNotMatch(failed, /notice error/);
});

test('R2-05c / F-08 a paused alert\'s unread events are a grey "N unread", left out of New matches', async () => {
  const html = await hubHtml([]);
  const row = rowOf(html, 'Promo code mentions');
  assert.match(row, /<span class="ew-tag ew-tag-outline" data-tag="outline">120 unread</);
  assert.doesNotMatch(row, /ew-tag-count/);
  assert.match(row, /data-alert-groups="all"/, 'paused: listed under All only');
  assert.match(html, /data-tab="new" aria-pressed="false"[^>]*>New matches · 2</, 'the tab count leaves the paused alert out');
  assert.match(rowOf(html, 'Free trial wording'), /data-alert-groups="all new"/);
  assert.match(rowOf(html, 'Free trial wording'), /ew-tag-count ew-work-new">34 new</);
});

test('F-08 a stopped alert with unread events is not under New matches either', async () => {
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: [{ ...HUB[0], id: 9, name: 'Ended one', lifecycle: 'stopped', unread_event_count: 7, unread_ad_count: 5 }, HUB[1]] };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(rowOf(html, 'Ended one'), /data-alert-groups="all stopped"/);
  assert.match(rowOf(html, 'Ended one'), /data-tag="outline">5 unread</);
  assert.match(html, /data-tab="new" aria-pressed="false"[^>]*>New matches · 1</);
});

// ---- UX-11 alert dialog ----
async function dialogHtml(alert) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const stubEl = () => ({ focus() {}, addEventListener() {}, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; } });
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector(sel) { return sel === '#alert-form' ? null : stubEl(); } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (p) => {
    if (alert && p === '/customer/alerts/' + alert.id + '?summaries=0') return { alert };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  };
  await win.EWViews.handleAction(alert ? { dataset: { action: 'edit-alert', id: String(alert.id) }, disabled: false, closest: () => null }
    : { dataset: { action: 'alerts-create' }, disabled: false, closest: () => null });
  return modal.innerHTML;
}

test('UX-11 dialog: Create alert / Save changes, summary pinned in the footer above the buttons, Track outside More filters', async () => {
  const create = await dialogHtml(null);
  assert.match(create, /<button class="button primary" type="submit">Create alert<\/button>/);
  assert.doesNotMatch(create, /Save local alert|Watching:/);
  assert.match(create, /id="alert-scope" role="status">This alert watches:/);
  assert.ok(create.indexOf('id="alert-scope"') > create.indexOf('id="alert-preview"'), 'summary after the preview');
  assert.match(create, /<div class="dialog-footer"><p class="section-note ew-alert-scope" id="alert-scope"[^>]*>[^<]*<\/p><button/, 'summary pinned in the footer, above the buttons');
  assert.match(create, /<label for="alert-mode">Look in<\/label>/);
  assert.match(create, /<details class="ew-details-quiet ew-alert-more"><summary>More filters/);
  const more = create.slice(create.indexOf('<details class="ew-details-quiet ew-alert-more"'), create.indexOf('</details>'));
  assert.doesNotMatch(more, /alert-track/, 'Track is not inside More filters');
  assert.match(more, /name="from"/);
  for (const n of ['q', 'mode', 'brand', 'show', 'from', 'to', 'audio', 'event_types']) assert.match(create, new RegExp(`name="${n}"`), n);
  assert.doesNotMatch(create, /name="status"|Record status/, 'PLAIN-ADS: no status filter');
  const edit = await dialogHtml({ id: 7, name: 'Dated', revision: 2, event_types: ['new_text'],
    query: { version: 2, mode: 'mentions', q: 'x', from: '2026-01-01' } });
  assert.match(edit, /type="submit">Save changes<\/button>/);
  assert.match(edit, /<details class="ew-details-quiet ew-alert-more" open>/, 'a set hidden filter opens More filters');
});
