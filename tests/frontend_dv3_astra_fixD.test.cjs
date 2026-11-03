/* Astra decision-round review fixes (customer pages): D-01 the Evidence
   status tag for every viewer (decision 2), D-05 one status tag on
   Overview recent rows and alert change rows, D-07 the closed-dialog toast
   for a dialog that keeps no draft, D-08 a stored empty list reads "None",
   D-09 an unresolved read's kind word. Loader helpers copied from
   frontend_dv3_g2.test.cjs and frontend_dv3_astra_proposals.test.cjs.
   Run: node --test tests/frontend_dv3_astra_fixD.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const flush = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });

function server(prefs) {
  return async (url, init = {}) => {
    const method = init.method || 'GET';
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (prefs && url === '/api/v1/customer/preferences' && method === 'GET') return json({ ok: true, workspace_context: 'ctx-ws', preferences: prefs });
    if (url === '/api/v1/customer/reader-status') return json({ ok: true, workspace_context: 'ctx-ws', configured: false, verified: false, note: 'n' });
    throw new Error('unexpected ' + method + ' ' + url);
  };
}
// The customer app on one route. api (when given) answers EW.api; hosted
// is set once the page's first read starts (the shell's own hosted
// admission is not under test, as in frontend_dv3_g2).
async function boot(route, { api = null, prefs = null, hosted = null } = {}) {
  const store = {};
  const listeners = {};
  const win = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route }, history: { replaceState() {} }, scrollTo() {},
    fetch: server(prefs),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  win.window = win; win.globalThis = win; win.listeners = listeners; win.addEventListener = () => {};
  vm.createContext(win);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.EW.current.route = route;
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, classList: { add() {}, remove() {} }, querySelector: () => null, querySelectorAll: () => [] });
  win.EW.header = () => {};
  if (api) win.EW.api = async p => { if (hosted !== null) win.EW.current.hosted = hosted; const r = await api(p); if (r === undefined) throw new Error('unexpected request ' + p); return r; };
  win.location.hash = route; win.EW.current.route = route;
  await win.EWViews.render(); await settleAll();
  return { win, html: main.innerHTML };
}

/* -- Evidence fixtures (frontend_dv3_g2 / frontend_dv3_ux_evidence) -- */
const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const read = (over = {}) => ({ kind: 'named', source_label: 'Named read',
  brand: { key: 'wayfair', display: 'Wayfair', display_basis: 'brand_list', confirmed: true },
  needs_review: false, self_promo: false, self_promo_reason: null, confidence: 'high', display_span: [0, 60], has_clip: false,
  said: { web_addresses: ['Wai Faire.com'], promo_codes: [], address_brand: null, code_brand: null, spoken_brand: 'Wayfair' },
  candidates: [{ brand: 'wayfair', evidence: ['gliner:company', 'repair:wai faire->wayfair', 'domain'] }], ...over });
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'confirmed',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [{ brand: 'wayfair', basis: 'confirmed', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 0, end_s: 60 },
  episode: { episode_id: 'ep_g2', show_title: 'Quiet Hours', ep_title: 'Why we wake at 3am', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: 'named:ep_g2.named.0-60000', episode_id: 'ep_g2', orig_start_s: 0, orig_end_s: 60 }, media: { playback: 'remote', alignment: 'unknown' },
  read: read(),
  window: { segments: [{ segment_id: 7, start_s: 0, end_s: 25, text: 'With Wai Faire, one night is all it takes.', text_start: 0, text_end: 42 }],
    text_complete: true, next_cursor: null }, ...over });
const evidence = (rec, hosted) => boot('#/evidence/' + encodeURIComponent(REF), { hosted, api: p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  return undefined;
} });
function ledger(html) {
  const rows = {};
  for (const m of html.matchAll(/<th scope="row">([^<]*)<\/th><td[^>]*>([\s\S]*?)<\/td><\/tr>/g)) rows[m[1]] = m[2];
  return rows;
}
const h1row = html => (html.match(/<div class="ew-reader-h1row">[\s\S]*?<\/div>/) || [''])[0];
const eyebrow = html => (html.match(/<span class="eyebrow">([^<]*)<\/span>/) || [])[1];
const aboutRecord = html => (html.match(/<dt>Record<\/dt><dd>([^<]*)<\/dd>/) || [])[1];

// PLAIN-ADS (DEP7) replaces D-01, D-09 and D-05: hosted members and the
// local operator read the same page, and no customer surface shows a
// review state. Every ad is "Ad"; no status tag anywhere.
test('D-01 Evidence: a confirmed read reads "Ad" with no status tag, hosted and local alike', async () => {
  for (const hosted of [true, false]) {
    const { html } = await evidence(record(), hosted);
    assert.doesNotMatch(h1row(html), /Confirmed|class="tag /, `hosted=${hosted}`);
    assert.equal(eyebrow(html), 'Ad', 'the eyebrow keeps the page’s own word');
    assert.equal(aboutRecord(html), 'Ad', 'About › Record keeps "Ad"');
  }
});

test('D-01 Evidence: the brand facts read the same for hosted and local, with no review words', async () => {
  const pages = await Promise.all([true, false].map(hosted => evidence(record(), hosted)));
  for (const { html } of pages) {
    const rows = ledger(html);
    assert.match(rows['Brand heard in the audio'], /the brand’s usual spelling\./);
    assert.doesNotMatch(rows['Brand heard in the audio'], /brand namer/);
    assert.equal(rows['Brand name review'], undefined, 'no Brand name review row');
    assert.doesNotMatch(html, /review team|[Cc]onfirmed/);
  }
  const unconfirmed = await evidence(record({ status: 'finding', review: { judgment: null, bounds_verified: 0 }, attribution: [],
    read: read({ brand: { key: 'wayfair', display: 'Wayfair', display_basis: 'none', confirmed: false } }) }), true);
  assert.doesNotMatch(unconfirmed.html, /Confirmed|confirmed the brand|not confirmed/, 'no review state either way');
});

test('D-09 Evidence: an unresolved read says "Ad" in the eyebrow and About › Record, with no tag', async () => {
  const { html } = await evidence(record({ status: 'unresolved', review: { judgment: null, bounds_verified: 0 }, attribution: [] }), true);
  assert.equal(eyebrow(html), 'Ad');
  assert.equal(aboutRecord(html), 'Ad');
  assert.doesNotMatch(h1row(html), /Unresolved/);
  assert.doesNotMatch(html, /Possible ad|Unresolved/);
});

/* -- D-05: Overview recent rows and alert change rows carry no status tag -- */
const statusItem = (ref, status) => ({ ref, record_kind: 'evidence', status, ep_title: 'Ep', show_title: 'S', pub_date: '2026-10-09', text: 'a',
  read: { brand: { key: 'acme', display: 'Acme', confirmed: true }, needs_review: false, historical: false } });
const OVERVIEW = { coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [] };

test('D-05 Overview recent rows: the same row for a possible, confirmed and unresolved ad', async () => {
  const items = [statusItem('sr1:f', 'finding'), statusItem('sr1:c', 'confirmed'), statusItem('sr1:u', 'unresolved')];
  const { html } = await boot('#/overview', { api: p => (p === '/customer/overview' ? OVERVIEW
    : p.startsWith('/customer/saved-items') ? { items: [] } : p.startsWith('/customer/search') ? { items } : undefined) });
  assert.match(html, /Acme/);
  assert.doesNotMatch(html, /Possible ad|Confirmed ad|Unresolved|ew-ov-tags/);
});

test('D-05 alert change rows: no status tag', async () => {
  const sum = status => ({ record_kind: 'evidence', status, episode_id: 'ep1', show_title: 'NPR', ep_title: '12am', pub_date: '2026-10-09',
    span: [250, 309], text: 'Try it free today.', historical: false, read: null });
  const alert = { id: 1, name: 'Free trial', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
    last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free' },
    events: ['finding', 'unresolved', 'confirmed'].map((status, i) => ({ id: i + 1, kind: 'new_finding', after_ref: 'sr1:' + i, read_state: 0, historical: false,
      observed_at: '2026-10-14T16:27:59+00:00', summary: { ...sum(status), episode_id: 'ep' + i } })) };
  const { html } = await boot('#/alert/1', { api: p => (p.startsWith('/customer/alerts/1') ? { alert } : p === '/customer/shows' ? { shows: [] } : p === '/customer/brands' ? { brands: [] } : undefined) });
  assert.match(html, /ew-work-event/);
  assert.doesNotMatch(html, /Possible ad|Unresolved|Confirmed ad/);
});

/* -- D-07: a dialog that keeps no draft says it was not saved -- */
test('D-07 Escape on a typed dialog that keeps no draft says "Not saved. You closed the dialog before saving." and keeps nothing', async () => {
  for (const [value, toasts] of [['someone@example.com', 1], ['', 0]]) {
    const { win } = await boot('#/settings', { prefs: { revision: 0 } });
    const input = { name: 'email', type: 'text', value, defaultValue: '' };
    const form = { dataset: { dirty: '1' }, elements: [input], getAttribute: k => (k === 'id' ? 'invite-create' : null), querySelectorAll: () => [input] };
    const modal = { open: true, querySelectorAll: s => (s === 'form[data-dirty="1"]' ? [form] : []) };
    win.document.querySelector = s => (s === '#modal' ? modal : null);
    const said = [];
    win.EW.toast = m => said.push(m);
    win.EW.closeDialog = () => { modal.open = false; };
    for (const f of win.listeners.keydown) f({ key: 'Escape' });
    assert.equal(modal.open, false);
    assert.deepEqual(said, toasts ? ['Not saved. You closed the dialog before saving.'] : [], JSON.stringify(value));
    assert.equal(win.EW.current.dialogDrafts.size, 0, 'no draft kept for this dialog');
  }
});

/* -- D-08: a stored empty list reads "None"; never stored stays "Not set" -- */
const proposals = html => {
  const start = html.indexOf('<details class="ew-settings-proposals">');
  assert.ok(start > 0);
  return html.slice(start, html.indexOf('</details>', start)).replace(/<\/?(dl|dt|dd|section|div|h2|span|p)[^>]*>/g, ' ').replace(/\s+/g, ' ');
};
test('D-08 Settings and Connections: an empty stored list says "None"', async () => {
  const settings = proposals((await boot('#/settings', { prefs: { revision: 2, share_fields: [] } })).html);
  assert.ok(settings.includes('Shared fields None'), settings);
  assert.ok(settings.includes('Viewer role Not set'), 'other rows stay Not set');
  const conn = proposals((await boot('#/connections', { prefs: { revision: 2, reader_scope: [], reader_fields: [] } })).html);
  assert.ok(conn.includes('Proposed scope None'), conn);
  assert.ok(conn.includes('Proposed fields None'), conn);
  const never = proposals((await boot('#/connections', { prefs: { revision: 0 } })).html);
  assert.ok(never.includes('Proposed scope Not set') && never.includes('Proposed fields Not set'), never);
});

/* -- D-10: the old third status mapping is gone -- */
test('D-10 no "Ad finding" / "Record" status mapping is left in the shell or views', () => {
  const appJs = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');
  const viewsJs = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.ok(!/function typeLabel|function typeTag|'Ad finding'/.test(appJs), 'app.js');
  assert.ok(!/function customerKind|function renderResult\b/.test(viewsJs), 'views.js');
});
