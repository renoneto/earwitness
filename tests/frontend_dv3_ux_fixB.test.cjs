/* Design v3 UX review fixes, worker fixB (F-08..F-24, alerts/saved/library/campaigns).
   Run: node --test tests/frontend_dv3_ux_fixB.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

// A sandbox whose document keeps its listeners and tracks the focused element.
function loadApp() {
  const store = {};
  const listeners = {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} },
    querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  const doc = {
    activeElement: null,
    querySelector: () => dummy, querySelectorAll: () => [],
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t,
  };
  const sandbox = {
    console, URLSearchParams, AbortController, setTimeout, clearTimeout,
    location: { hash: '#/saved' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: doc,
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  sandbox.EW.current.route = '#/saved';
  return { win: sandbox, doc, listeners };
}
const settle = () => new Promise(res => setTimeout(res, 0));

// ---- F-09 Saved Actions menu: focus and dialog opener ----
function savedMenuDom(doc) {
  const el = (props) => {
    const e = { dataset: {}, hidden: false, isConnected: true, ...props,
      focus() { doc.activeElement = e; }, closest: () => null, contains: () => false, hasAttribute: () => false, getAttribute: () => null };
    return e;
  };
  const summary = el({ tagName: 'SUMMARY' });
  const panel = el({ className: 'ew-row-menu-panel' });
  const remove = el({ tagName: 'BUTTON', dataset: { action: 'delete-saved', id: '5', rev: '3' }, disabled: false });
  const edit = el({ tagName: 'BUTTON', dataset: { action: 'edit-saved', id: '5' }, disabled: false });
  const menu = el({ open: true, querySelector: sel => (sel === 'summary' ? summary : null) });
  menu.contains = t => [summary, panel, remove, edit].includes(t);
  for (const b of [remove, edit]) {
    b.closest = sel => (sel === '.ew-row-menu-panel' ? panel : sel === '[data-action]' ? b : null);
  }
  remove.inMenu = edit.inMenu = true;
  const outside = el({ tagName: 'P' });
  const dialogButton = el({ tagName: 'BUTTON' });
  // Like a browser: close() returns focus to the element focused before
  // showModal(), unless it is hidden (a closed menu's item), then <body>.
  const modal = { innerHTML: '', open: false, setAttribute() {},
    showModal() { this.open = true; this.prev = doc.activeElement; },
    close() { this.open = false; const p = this.prev; doc.activeElement = p && !(p.inMenu && !menu.open) ? p : null; },
    querySelector: sel => (sel === 'button' ? dialogButton : null), querySelectorAll: () => [] };
  doc.querySelectorAll = sel => (sel === 'details.ew-row-menu[open]' ? (menu.open ? [menu] : []) : []);
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} },
    querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  doc.querySelector = sel => (sel === '#modal' ? modal : sel === 'details.ew-row-menu[open]' ? (menu.open ? menu : null) : dummy);
  return { menu, summary, remove, edit, outside, modal };
}
const click = (listeners, target) => { for (const fn of listeners.click) fn({ target, button: 0, preventDefault() {} }); };
const key = (listeners, k) => { for (const fn of listeners.keydown) fn({ key: k, preventDefault() {} }); };

test('F-09 Remove from the Actions menu: focus moves to the menu summary, which becomes the dialog opener', async () => {
  const { win, doc, listeners } = loadApp();
  await settle();
  const dom = savedMenuDom(doc);
  doc.activeElement = dom.remove;
  click(listeners, dom.remove);
  await settle();
  await settle();
  assert.equal(dom.menu.open, false, 'the menu closes');
  assert.ok(dom.modal.open, 'the Remove dialog opened');
  assert.match(dom.modal.innerHTML, /Remove saved item/);
  assert.equal(win.EW.current.dialogOpener, dom.summary, 'the opener is the visible summary, not the hidden menu item');
  // Escape on the dialog closes it; focus goes back to the opener (the
  // browser restores the element focused before showModal).
  key(listeners, 'Escape');
  assert.equal(dom.modal.open, false);
  assert.equal(doc.activeElement, dom.summary);
});

test('F-09 Edit from the Actions menu also records the summary as the opener', async () => {
  const { win, doc, listeners } = loadApp();
  await settle();
  const dom = savedMenuDom(doc);
  win.EW.api = async (p) => { if (p === '/customer/saved/5') return { item: { id: 5, kind: 'query', revision: 3, title: 'T', query: { version: 2, mode: 'ads', q: 'x' } } }; throw new Error('unexpected ' + p); };
  doc.activeElement = dom.edit;
  click(listeners, dom.edit);
  assert.equal(doc.activeElement, dom.summary, 'focus is on the summary before the dialog opens');
  assert.equal(dom.menu.open, false);
});

test('F-09 an outside click closes the menu and leaves focus where the user clicked', async () => {
  const { doc, listeners } = loadApp();
  await settle();
  const dom = savedMenuDom(doc);
  doc.activeElement = dom.outside;
  click(listeners, dom.outside);
  assert.equal(dom.menu.open, false);
  assert.equal(doc.activeElement, dom.outside);
});

test('F-09 Escape on an open Actions menu closes it and focuses its summary', async () => {
  const { doc, listeners } = loadApp();
  await settle();
  const dom = savedMenuDom(doc);
  doc.activeElement = dom.remove;
  key(listeners, 'Escape');
  assert.equal(dom.menu.open, false);
  assert.equal(doc.activeElement, dom.summary);
});

// ---- F-24 Saved rows: the title is the only Open link when it goes to the same place ----
async function savedRows(route, items) {
  const { win } = loadApp();
  win.EW.current.route = route;
  const api = async (p) => (p.startsWith('/customer/saved-items') ? { items, total: items.length, total_relation: 'exact', counts: {}, next_cursor: null }
    : { items: [], notes: [], total: 0, total_relation: 'exact', next_cursor: null, counts: {} });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  return html.match(/<li class="saved-row">[^]*?<\/li>/g) || [];
}
const hrefs = row => [...row.matchAll(/<a [^>]*href="([^"]+)"/g)].map(m => m[1]);

test('F-24 a saved query row has one link to its results: the title', async () => {
  const [row] = await savedRows('#/saved?tab=queries', [{ id: 7, type: 'query', title: 'Free trial', revision: 1,
    query: { version: 2, mode: 'ads', q: 'free trial' } }]);
  assert.match(row, /<h3 class="saved-title"><a class="" href="#\/search\?[^"]*">Free trial<\/a>/);
  assert.doesNotMatch(row, /Open current results/);
  assert.equal(hrefs(row).filter(h => h.startsWith('#/search')).length, 1);
});

test('F-24 a broken saved query keeps a plain title, its reason, and no Open link', async () => {
  const [row] = await savedRows('#/saved?tab=queries', [{ id: 8, type: 'query', title: 'Broken', revision: 1, query: { mode: 'nope' } }]);
  assert.match(row, /<span>Broken<\/span>/);
  assert.match(row, /This saved query is unreadable/);
  assert.doesNotMatch(row, /Open current results/);
});

test('F-24 bookmark rows: the title opens the target; no second Open link to the same place', async () => {
  const rows = await savedRows('#/saved?tab=bookmarks', [
    { id: 1, type: 'bookmark', title: 'A show', revision: 1, target: { kind: 'show', id: 'sh1' } },
    { id: 2, type: 'bookmark', title: 'A brand', revision: 1, target: { kind: 'brand', id: 'acme' } },
    { id: 3, type: 'bookmark', title: 'An episode pin', revision: 1, target: { kind: 'episode', id: 'ep1', source_ref: 'sr1:x' } },
    { id: 4, type: 'bookmark', title: 'Gone', revision: 1, target: { kind: 'episode' } },
  ]);
  assert.equal(rows.length, 4);
  for (const row of rows.slice(0, 3)) {
    const h = hrefs(row);
    assert.equal(h.length, new Set(h).size, 'no destination is linked twice: ' + h.join(' '));
    assert.doesNotMatch(row, /Open current profile|Open brand →|Open captured pin/);
  }
  assert.match(rows[0], /<a class="" href="#\/show\/sh1">A show<\/a>/);
  assert.match(rows[3], /<span>Gone<\/span>/);
  assert.match(rows[3], /Saved record retained; its target is unavailable\./, 'an unavailable target still says so');
});

test('F-24 an armed danger button never outranks the disabled look', () => {
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  const rule = /^([^{\n]*\.button\.danger\.armed[^{]*)\{/m.exec(css);
  assert.ok(rule, 'armed rule present');
  for (const sel of rule[1].split(',').map(s => s.trim()).filter(Boolean)) {
    assert.match(sel, /:not\(:disabled\):not\(\[aria-disabled=true\]\)$/, sel);
  }
});

// ---- page render helper ----
async function renderRoute(route, api) {
  const { win, doc } = loadApp();
  await settle();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  doc.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, classList: { add() {}, remove() {} }, querySelector: () => null, querySelectorAll: () => [] });
  win.EW.header = () => {};
  win.EW.api = async (p) => { const r = await api(p); if (r instanceof Error) throw r; if (r === undefined) throw new Error('unexpected request ' + p); return r; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, doc, html: main.innerHTML };
}

// ---- F-10 Show sponsors empty sentence ----
const SCREENED = { jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full' };
const NO_ADS = { ...SCREENED, screened_no_ads: true };
const showDetail = episodes => ({ show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: episodes.length, episodes_with_transcript: episodes.length }, sponsors: [], episodes });
const showEp = (id, screening) => ({ id, ep_title: id, pub_date: '2026-10-09', transcript: true, screening });
const sponsorEmpty = async episodes => {
  const { html } = await renderRoute('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? showDetail(episodes) : undefined));
  return (/class="lib-sponsor-empty">([^<]*)</.exec(html) || [])[1];
};

test('F-10 "No ads found" only when the screened episodes say no ads; screened alone is "No ads listed"', async () => {
  assert.equal(await sponsorEmpty([showEp('e1', SCREENED), showEp('e2', SCREENED)]), 'No ads listed in the screened episodes.');
  assert.equal(await sponsorEmpty([showEp('e1', SCREENED), showEp('e2', NO_ADS)]), 'No ads listed in the screened episodes.');
  assert.equal(await sponsorEmpty([showEp('e1', SCREENED), showEp('e2', null)]), 'No ads listed in the screened episodes.');
  assert.equal(await sponsorEmpty([showEp('e1', NO_ADS), showEp('e2', NO_ADS)]), "No ads found in this show's episodes.");
  assert.equal(await sponsorEmpty([showEp('e1', NO_ADS), showEp('e2', null)]), 'No ads found in the screened episodes.');
});

// ---- F-17 a withheld brand is not "Brand not identified" ----
const OVERVIEW = { coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [] };
const overviewLead = async items => {
  const { html } = await renderRoute('#/overview', p => (p === '/customer/overview' ? OVERVIEW
    : p.startsWith('/customer/saved-items') ? { items: [] } : p.startsWith('/customer/search') ? { items } : undefined));
  return [...html.matchAll(/class="ew-ov-name ew-ov-lead"[^>]*>((?:(?!<\/a>).)*)<\/a>/g)].map(m => m[1]);
};
const det = (ref, read) => ({ ref, record_kind: 'evidence', status: 'finding', ep_title: 'Ep', show_title: 'S', pub_date: '2026-10-09', text: 'a', read });

// PLAIN-ADS-FIX3 (lead, screen R3-02): Overview Recent ads lists only ads
// whose brand shows; a withheld read, an earlier version or a read with no
// name is left out there (Search still lists them).
test('F-17 Overview: a withheld read, an earlier version and a read with no name are left out of Recent ads', async () => {
  const leads = await overviewLead([
    det('sr1:w', null),
    det('sr1:h', { historical: true, brand: { key: 'acme', display: 'Acme', confirmed: true } }),
    det('sr1:n', { brand: { key: null, display: null, confirmed: false }, needs_review: false, historical: false }),
    det('sr1:a', { brand: { key: 'acme', display: 'Acme', confirmed: false }, needs_review: false, historical: false }),
  ]);
  assert.deepEqual(leads, ['<strong><span class="ew-brand">Acme</span></strong>']);
});

const ALERT_SUM = { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'NPR', ep_title: '12am', pub_date: '2026-10-09',
  span: [250, 309], text: 'Try it free today.', historical: false };
const alertPage = async summaries => {
  const alert = { id: 1, name: 'Free trial', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
    last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free' },
    events: summaries.map((summary, i) => ({ id: i + 1, kind: 'new_finding', after_ref: 'sr1:' + i, read_state: 0, historical: false,
      observed_at: '2026-10-14T16:27:59+00:00', summary: { ...summary, episode_id: 'ep' + i } })) };
  const { html } = await renderRoute('#/alert/1', p => (p.startsWith('/customer/alerts/1') ? { alert } : p === '/customer/shows' ? { shows: [] } : p === '/customer/brands' ? { brands: [] } : undefined));
  return [...html.matchAll(/<div class="ew-work-event-head">(<strong>[^<]*<\/strong>)/g)].map(m => m[1]);
};

test('F-17 alert rows: a withheld read says "Ad record"; a read with no name says "Brand not identified"', async () => {
  const heads = await alertPage([{ ...ALERT_SUM, read: null }, { ...ALERT_SUM, read: { brand: { key: null, display: null, confirmed: false }, needs_review: false, historical: false } }]);
  assert.deepEqual(heads.sort(), ['<strong>Ad record</strong>', '<strong>Brand not identified</strong>']);
});

// ---- F-16 the alert read skips summaries where the page does not use them ----
test('F-16 Mark all read and the Edit dialog ask for the alert without summaries; the alert page keeps them', async () => {
  const seen = [];
  const alert = { id: 1, name: 'Free trial', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
    last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free' }, event_types: ['new_finding'],
    events: [{ id: 5, kind: 'new_finding', after_ref: 'sr1:x', read_state: 0, observed_at: '2026-10-14T16:27:59+00:00', summary: null }] };
  const api = p => { seen.push(p); return p.startsWith('/customer/alerts/1') && !p.includes('/read') ? { alert }
    : p.endsWith('/read') ? { ok: true } : p === '/customer/shows' ? { shows: [] } : p === '/customer/brands' ? { brands: [] } : undefined; };
  const { win, doc } = await renderRoute('#/alert/1', api);
  assert.ok(seen.includes('/customer/alerts/1'), 'the alert page reads the summaries');
  seen.length = 0;
  win.EW.render = () => {};
  await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '1' }, disabled: false, closest: () => null });
  assert.deepEqual(seen, ['/customer/alerts/1?summaries=0', '/customer/alerts/1/read']);
  seen.length = 0;
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; },
    querySelector: () => ({ focus() {}, addEventListener() {}, appendChild() {}, querySelector: () => null, querySelectorAll: () => [] }), querySelectorAll: () => [] };
  doc.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '', setAttribute() {}, classList: { add() {}, remove() {} }, querySelector: () => null, querySelectorAll: () => [] });
  await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: '1' }, disabled: false, closest: () => null });
  assert.equal(seen[0], '/customer/alerts/1?summaries=0');
  assert.ok(modal.open, 'the Edit dialog opened');
});
