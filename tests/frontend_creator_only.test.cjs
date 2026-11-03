/* FR-11 / UX audit B2: only the person who made a saved item, alert or
   campaign can change it (or a workspace owner). The server says so per
   item (can_change; a campaign's allowed_actions), and the screens never
   show a button the server would refuse. Seeded items (demo-curator) are
   read-only for judges; a judge's own items keep every button.
   Loader helpers copied from frontend_campaign_remove.test.cjs.
   Run: node --test tests/frontend_creator_only.test.cjs */
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
function confirmForm(win, html) {
  const hidden = {};
  for (const m of html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)) hidden[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  win.FormData = class { constructor() { this.e = Object.entries(hidden); } *entries() { yield* this.e; } get(k) { return hidden[k]; } };
  return { getAttribute: k => (k === 'id' ? 'confirm' : null), dataset: {}, querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, elements: [] };
}
// Values made in the page's realm compare as plain data.
const plain = v => JSON.parse(JSON.stringify(v));
const dataOf = tag => Object.fromEntries([...tag.matchAll(/\sdata-([a-z-]+)="([^"]*)"/g)]
  .map(([, k, v]) => [k.replace(/-([a-z])/g, (_, c) => c.toUpperCase()), v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')]));
const unesc = h => h.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

const has = (html, action) => new RegExp(`data-action="${action}"`).test(html);

/* -- Campaign page ---------------------------------------------------------- */
const FULL = ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'];
const OTHERS = ['read', 'freeze', 'reports'];
const campaignApi = allowed => (p) => {
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 5, name: 'Shopify ads', status: 'active',
    allowed_actions: allowed } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 5 }, mode: 'research', placements: [],
    targets: [{ id: 't1', scope: 'show_window', show_id: 's1', evidence: ['sr1:a'] }], show_titles: { s1: 'TBPN' }, allocation: {},
    coverage_by_placement: {}, allowed_actions: allowed } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};

test('FR-11 campaign page: a campaign someone else made has no change buttons; Freeze stays', async () => {
  const t = await page('#/campaign/2', campaignApi(OTHERS));
  const html = t.main.innerHTML;
  for (const action of ['edit-campaign', 'archive-campaign', 'add-target', 'remove-observation']) {
    assert.ok(!has(html, action), action);
  }
  assert.doesNotMatch(html, />Attach evidence</);
  assert.doesNotMatch(html, /Planned/);
  assert.doesNotMatch(html, /use Add to campaign on a passage/);
  assert.ok(has(html, 'freeze-report'));
  assert.match(html, /Observation 1 · TBPN/);
});

test('FR-11 campaign page: the maker (or an owner) keeps every button', async () => {
  const t = await page('#/campaign/2', campaignApi(FULL));
  const html = t.main.innerHTML;
  for (const action of ['edit-campaign', 'archive-campaign', 'add-target', 'remove-observation', 'freeze-report']) {
    assert.ok(has(html, action), action);
  }
  assert.match(html, />Attach evidence</);
});

/* -- Add to campaign -------------------------------------------------------- */
const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
const dialogApi = (p) => {
  if (p === '/customer/campaigns') return { campaigns: [
    { id: 5, name: 'Seeded scan', mode: 'research', availability: 'active', revision: 2, can_change: false },
    { id: 6, name: 'My scan', mode: 'research', availability: 'active', revision: 1, can_change: true }] };
  if (p === '/customer/campaigns/5/targets') return { ok: true, targets: [{ id: 'ct-5', scope: 'show_window', show_id: 'cs-upfirst', evidence: [] }] };
  if (p === '/customer/campaigns/6/targets') return { ok: true, targets: [{ id: 'ct-6', scope: 'show_window', show_id: 'cs-upfirst', evidence: [] }] };
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 1, name: 'x' } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 1 }, mode: 'research', placements: [], targets: [], allocation: {}, coverage_by_placement: {} } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};

test('FR-11 Add to campaign offers only campaigns the caller may change', async () => {
  const t = await page('#/campaign/2', dialogApi);
  await t.win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst',
    day: '2026-10-07', showName: 'Up First' } }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /value="6\|ct-6"/);
  assert.doesNotMatch(m, /Seeded scan|value="5\|/);
  assert.ok(!t.calls.some(c => c[1] === '/customer/campaigns/5/targets'), 'no read of a campaign it cannot change');
});

/* -- Alerts ----------------------------------------------------------------- */
const T1 = '2026-10-14T09:00:00Z';
const ALERT = { id: 5, name: 'New Quince ads', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, unread_event_count: 0, events: [], query: { version: 2, mode: 'ads', q: '', brands: ['quince'] },
  current_matches: { count: 1, tracked: 1, tracked_by_kind: { new_finding: 1 }, held_for_brand_review: 0 } };
const alertsApi = alert => (p) => {
  if (p === '/customer/alerts') return { alerts: [alert] };
  if (p === '/customer/alerts/' + alert.id) return { alert };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'quince', display: 'Quince' }] };
  throw new Error('unexpected request ' + p);
};
const ALERT_CHANGES = ['pause-alert', 'edit-alert', 'stop-alert', 'backfill-alert'];

test('FR-11 alerts hub: an alert someone else made has no Pause or Edit; Open stays', async () => {
  const t = await page('#/alerts', alertsApi({ ...ALERT, can_change: false }));
  const html = t.main.innerHTML;
  for (const action of ALERT_CHANGES) assert.ok(!has(html, action), action);
  assert.match(html, /href="#\/alert\/5"[^>]*>Open</);
  const mine = await page('#/alerts', alertsApi({ ...ALERT, can_change: true }));
  assert.ok(has(mine.main.innerHTML, 'pause-alert') && has(mine.main.innerHTML, 'edit-alert'));
});

test('FR-11 alert page: no Edit, Pause, Stop or Include earlier matches on an alert someone else made', async () => {
  const t = await page('#/alert/5', alertsApi({ ...ALERT, can_change: false }));
  const html = t.main.innerHTML;
  for (const action of ALERT_CHANGES) assert.ok(!has(html, action), action);
  assert.ok(has(html, 'mark-read'));
  const mine = await page('#/alert/5', alertsApi({ ...ALERT, can_change: true }));
  for (const action of ALERT_CHANGES) assert.ok(has(mine.main.innerHTML, action), action);
});

/* -- Saved ------------------------------------------------------------------ */
async function savedHtml(route, items) {
  const win = loadAt(route);
  const api = async () => ({ items, total: items.length, total_relation: 'exact', counts: { excerpt: 0, query: items.length, bookmark: 0 }, next_cursor: null });
  return win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
}
const QUERY = { id: 'si-1', type: 'query', title: 'Quince ads', revision: 1, availability: 'current', note: 'Start here.',
  query: { version: 2, mode: 'ads', q: '', brands: ['quince'] } };

test('FR-11 Saved: an item someone else made has no Edit or Remove; your own has both', async () => {
  const theirs = await savedHtml('#/saved?tab=queries', [{ ...QUERY, can_change: false }]);
  assert.ok(!has(theirs, 'edit-saved') && !has(theirs, 'delete-saved'), theirs);
  assert.match(theirs, /Quince ads/);
  const mine = await savedHtml('#/saved?tab=queries', [{ ...QUERY, can_change: true }]);
  assert.ok(has(mine, 'edit-saved') && has(mine, 'delete-saved'));
});

/* -- Track a brand ---------------------------------------------------------- */
const DIR = { brands: [
  { brand: 'quince', key: 'quince', display: 'Quince', records: 38, ad_breaks: 20, confirmed: 0, confirmed_ads: 0, shows: 21, last_heard: '2026-10-08' },
  { brand: 'ag1', key: 'ag1', display: 'AG1', records: 12, ad_breaks: 7, confirmed: 0, confirmed_ads: 0, shows: 1, last_heard: '2026-10-09' },
  { brand: 'chase', key: 'chase', display: 'Chase', records: 9, ad_breaks: 5, confirmed: 0, confirmed_ads: 0, shows: 1, last_heard: '2026-10-09' }] };
const brandItem = (id, key, canChange) => ({ id, type: 'bookmark', title: key, revision: 1, availability: 'current', can_change: canChange,
  target: { kind: 'brand', id: key }, captured: { record_kind: 'brand', label: key, opens: 'current_profile' } });

test('FR-11 brands: a brand someone else tracks shows Tracking with no button; Track and your own toggle stay', async () => {
  const t = await page('#/brands', p => (p === '/customer/brands' ? DIR
    : p.startsWith('/customer/saved-items?') ? { items: [brandItem('si-1', 'ag1', false), brandItem('si-2', 'chase', true)], next_cursor: null }
      : new Error('unexpected ' + p)));
  const html = t.main.innerHTML;
  assert.doesNotMatch(html, /data-action="track-brand" data-brand="ag1"/);
  assert.match(html, /Tracking<\/span><span class="visually-hidden"> AG1<\/span>/);
  assert.match(html, /data-action="track-brand" data-brand="chase"[^>]*aria-pressed="true"/);
  assert.match(html, /data-action="track-brand" data-brand="quince"[^>]*aria-pressed="false"/);
});

/* CO-SCR-R1-01 / CO-R1-02: a Track button loaded before another member
   tracked the brand. Tracking is workspace-wide, so the save returns their
   item; the button becomes the read-only Tracking label, never a toggle
   the server would refuse. Your own track stays a toggle. */
function staleTrack(compact) {
  const attrs = { 'aria-pressed': 'false' };
  return { dataset: { action: 'track-brand', brand: 'quince', name: 'Quince', compact: compact ? '1' : '' },
    getAttribute: k => (k in attrs ? attrs[k] : null), setAttribute(k, v) { attrs[k] = String(v); },
    removeAttribute(k) { delete attrs[k]; }, classList: { toggle() {} }, innerHTML: '', outerHTML: '', disabled: false, attrs };
}
const trackApi = item => p => (p === '/customer/brands' ? DIR
  : p.startsWith('/customer/saved-items?') ? { items: [], next_cursor: null }
    : p === '/customer/saved-items' ? { ok: true, created: false, item }
      : new Error('unexpected ' + p));

test('FR-11 Track on a stale page: a brand someone else just tracked shows the Tracking label, not a toggle', async () => {
  for (const compact of [false, true]) {
    const t = await page('#/brands', trackApi(brandItem('si-9', 'quince', false)));
    const { el, parent } = liveTrack(compact);
    await t.win.EWViews.handleAction(el);
    const tag = parent.children[0] || {};
    assert.ok(parent.children.length === 1 && tag !== el, `compact=${compact}`);
    assert.match(tag.html, /lib-track-by-other/, `compact=${compact}`);
    assert.doesNotMatch(tag.html, /data-action="track-brand"/);
    assert.match(tag.html, compact ? /Tracking<\/span><span class="visually-hidden"> Quince<\/span>/ : /Tracking this brand/);
    assert.deepEqual(t.toasts.map(x => x[0]), ['Tracking Quince. It is in Saved, under Bookmarks.']);
  }
});

test('FR-11 Track on a stale page: your own track stays a toggle you can stop', async () => {
  const t = await page('#/brands', trackApi(brandItem('si-8', 'quince', true)));
  const el = staleTrack(false);
  await t.win.EWViews.handleAction(el);
  assert.equal(el.outerHTML, '');
  assert.equal(el.attrs['aria-pressed'], 'true');
  assert.equal(el.dataset.item, 'si-8');
});

/* CO-SCR-R1-02..05: what a member sees on an item someone else made. */
const MADE_BY_OTHER = '<p class="notice" role="note">Only the person who made this can change it.</p>';
const freezeClass = html => ((html.match(/<button type="button" class="button ([^"]*)" data-action="freeze-report"/) || [])[1] || '').trim();
const campApi = (camp, draftExtra = {}) => (p) => {
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 5, name: 'Shopify ads', ...camp } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 5, status: camp.status }, mode: 'research', placements: [],
    targets: [{ id: 't1', scope: 'show_window', show_id: 's1', evidence: ['sr1:a'] }], show_titles: { s1: 'TBPN' }, allocation: {},
    coverage_by_placement: {}, allowed_actions: camp.allowed_actions, ...draftExtra } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  if (p === '/customer/campaigns/2/targets') return { targets: [{ id: 't1', scope: 'show_window', show_id: 's1', revision: 1, evidence: [] }] };
  if (p === '/customer/shows/s1') return { display_title: 'TBPN', episodes: [] };
  throw new Error('unexpected request ' + p);
};

test('FR-11 campaign someone else made: Freeze is not the primary button, and one line says why the rest is gone', async () => {
  const theirs = (await page('#/campaign/2', campApi({ status: 'active', allowed_actions: OTHERS, can_change: false }, { can_change: false }))).main.innerHTML;
  assert.equal(freezeClass(theirs), '');
  assert.equal(theirs.split(MADE_BY_OTHER).length - 1, 1);
  const mine = (await page('#/campaign/2', campApi({ status: 'active', allowed_actions: FULL }))).main.innerHTML;
  assert.equal(freezeClass(mine), 'primary');
  assert.doesNotMatch(mine, /Only the person who made this/);
});

test('FR-11 archived campaign someone else made: no "Restore it to make changes", no planned Attach evidence', async () => {
  const theirs = (await page('#/campaign/2', campApi({ status: 'archived', allowed_actions: ['read', 'stored_reports'], can_change: false },
    { can_change: false }))).main.innerHTML;
  assert.doesNotMatch(theirs, /Restore it to make changes/);
  assert.ok(theirs.includes(MADE_BY_OTHER));
  assert.doesNotMatch(theirs, /Planned|Attaching evidence to a research campaign is planned/);
  assert.ok(!has(theirs, 'archive-campaign'));
  const mine = (await page('#/campaign/2', campApi({ status: 'archived', allowed_actions: ['read', 'stored_reports', 'restore'] }))).main.innerHTML;
  assert.match(mine, /Archived\. Restore it to make changes\./);
  assert.ok(has(mine, 'archive-campaign'));
  assert.doesNotMatch(mine, /Only the person who made this/);
});

test('FR-11 research target of a campaign someone else made: the same line, never the Restore line', async () => {
  for (const status of ['active', 'archived']) {
    const html = (await page('#/campaign/2/target/t1', campApi({ status, allowed_actions: ['read', 'stored_reports'], can_change: false }))).main.innerHTML;
    assert.ok(html.includes(MADE_BY_OTHER), status);
    assert.doesNotMatch(html, /Restore it to make changes/, status);
    assert.ok(!has(html, 'edit-target-note'), status);
  }
});

test('FR-11 alert someone else made: one line says why Edit, Pause and Stop are gone', async () => {
  const theirs = (await page('#/alert/5', alertsApi({ ...ALERT, can_change: false }))).main.innerHTML;
  assert.equal(theirs.split(MADE_BY_OTHER).length - 1, 1);
  const mine = (await page('#/alert/5', alertsApi({ ...ALERT, can_change: true }))).main.innerHTML;
  assert.doesNotMatch(mine, /Only the person who made this/);
});

test('FR-11 Add to campaign: when only campaigns someone else made cover the read, they are listed, not chosen', async () => {
  const api = (p) => {
    if (p === '/customer/campaigns') return { campaigns: [
      { id: 5, name: 'Seeded scan', mode: 'research', availability: 'active', revision: 2, can_change: false },
      { id: 6, name: 'My scan', mode: 'research', availability: 'active', revision: 1, can_change: true }] };
    if (p === '/customer/campaigns/5/targets') return { ok: true, targets: [{ id: 'ct-5', scope: 'show_window', show_id: 'cs-upfirst', evidence: [] }] };
    if (p === '/customer/campaigns/6/targets') return { ok: true, targets: [{ id: 'ct-6', scope: 'show_window', show_id: 'cs-other', evidence: [] }] };
    return dialogApi(p);
  };
  const t = await page('#/campaign/2', api);
  await t.win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst',
    day: '2026-10-07', showName: 'Up First' } }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /<input type="radio" name="choice" disabled><span><b>Seeded scan<\/b> · [^<]*· Only the person who made this can change it\.<\/span>/);
  assert.doesNotMatch(m, /No campaign placement covers this episode|No research target covers this episode/);
  assert.doesNotMatch(m, /value="5\|/);
  assert.doesNotMatch(m, /type="submit"/);
});

/* CO-SCR-R2-02 / CO-R2-01: a viewer (a demoted maker too) gets can_change
   false on every item because of their role, not who made it. The note
   line is for members who can change items; a viewer sees none. */
test('FR-11 viewer: no "Only the person who made this" line on campaign, target, alert or Add to campaign', async () => {
  const camp = { status: 'active', allowed_actions: OTHERS, can_change: false };
  const pages = [
    (await page('#/campaign/2', campApi(camp, { can_change: false }), { role: 'viewer' })).main.innerHTML,
    (await page('#/campaign/2/target/t1', campApi(camp), { role: 'viewer' })).main.innerHTML,
    (await page('#/alert/5', alertsApi({ ...ALERT, can_change: false }), { role: 'viewer' })).main.innerHTML,
  ];
  pages.forEach((html, i) => assert.doesNotMatch(html, /Only the person who made this/, `page ${i}`));
  // An archived campaign keeps the line a viewer saw before FR-11.
  const archived = (await page('#/campaign/2', campApi({ status: 'archived', allowed_actions: ['read', 'stored_reports'], can_change: false },
    { can_change: false }), { role: 'viewer' })).main.innerHTML;
  assert.doesNotMatch(archived, /Only the person who made this/);
  assert.match(archived, /Archived\. Restore it to make changes\./);
  const api = (p) => (p === '/customer/campaigns' ? { campaigns: [
    { id: 5, name: 'Seeded scan', mode: 'research', availability: 'active', revision: 2, can_change: false }] } : dialogApi(p));
  const t = await page('#/campaign/2', api, { role: 'viewer' });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst',
    day: '2026-10-07', showName: 'Up First' } }); await settle();
  assert.match(t.modal.innerHTML, /<input type="radio" name="choice" disabled><span><b>Seeded scan<\/b>/);
  assert.doesNotMatch(t.modal.innerHTML, /Only the person who made this/);
});

/* CO-SCR-R2-01: the stale-Track swap keeps keyboard focus on the new
   "✓ Tracking" label (Brand page and Brands list), never the page top.
   A small live DOM: one parent, focus falls to body when the focused
   node leaves it. */
function liveTrack(compact) {
  const body = { tagName: 'BODY' };
  const doc = { activeElement: null };
  const parent = { children: [] };
  const detach = n => {
    const i = parent.children.indexOf(n);
    if (i >= 0) parent.children.splice(i, 1);
    if (doc.activeElement === n) doc.activeElement = body;
  };
  const node = html => ({ html, attrs: {}, setAttribute(k, v) { this.attrs[k] = String(v); }, focus() { doc.activeElement = this; },
    remove() { detach(this); } });
  const el = staleTrack(compact);
  delete el.outerHTML;
  Object.defineProperty(el, 'outerHTML', { set(h) { const i = parent.children.indexOf(el); parent.children.splice(i, 1, node(h)); if (doc.activeElement === el) doc.activeElement = body; } });
  Object.assign(el, { parentNode: parent, focus() { doc.activeElement = el; }, remove() { detach(el); },
    insertAdjacentHTML(pos, h) { const i = parent.children.indexOf(el); parent.children.splice(pos === 'beforebegin' ? i : i + 1, 0, node(h)); } });
  Object.defineProperty(el, 'previousElementSibling', { get() { return parent.children[parent.children.indexOf(el) - 1] || null; } });
  Object.defineProperty(el, 'nextElementSibling', { get() { const i = parent.children.indexOf(el); return i < 0 ? null : parent.children[i + 1] || null; } });
  parent.children.push(el);
  el.focus();
  return { el, parent, doc, body };
}

test('FR-11 Track on a stale page: focus stays on the Tracking label after the swap', async () => {
  for (const compact of [false, true]) {
    const t = await page('#/brands', trackApi(brandItem('si-9', 'quince', false)));
    const { el, parent, doc, body } = liveTrack(compact);
    await t.win.EWViews.handleAction(el);
    assert.equal(parent.children.length, 1, `compact=${compact}`);
    const tag = parent.children[0];
    assert.notEqual(tag, el);
    assert.match(tag.html, /lib-track-by-other/);
    assert.doesNotMatch(tag.html, /data-action="track-brand"/);
    assert.notEqual(doc.activeElement, body, `compact=${compact}: focus fell to the page`);
    assert.equal(doc.activeElement, tag);
    assert.equal(tag.attrs.tabindex, '-1');
  }
});
