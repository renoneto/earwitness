/* UAT walkthrough fix round, library worker:
   VL-21 phone week-row rule scoped to Search; VL-24/VL-26 category labels and
   the brand page's show qualifier; VL-32 plurals; VL-33 Compare copy; VL-34
   inline link in notes; VL-37 filter label; V-21/VS F-16 stamps in the
   Settings time zone; V-25 Settings copy and one "Saving…"; V-31 "Collection";
   V-12/B-10 reader check.
   Loader copied from frontend_dv3_ux_library.test.cjs.
   Run: node --test tests/frontend_uat_library.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const read = f => fs.readFileSync(path.join(APP, f), 'utf8');

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
    vm.runInContext(read(file), sandbox, { filename: file });
  }
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, { hosted = null } = {}) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(read('views.js'), win, { filename: 'views.js' });
  await flushBoot();
  if (hosted !== null) win.EW.current.hosted = hosted;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  return { html: main.innerHTML, win };
}

/* -- VL-21: the phone week-row rule is Search's --------------------------- */
test('VL-21: no search.css rule hides .ew-week-row outside the Search results', () => {
  const css = read('search.css');
  const rules = [...css.matchAll(/([^{}]+)\{\s*display:\s*none;?\s*\}/g)].map(m => m[1].trim()).filter(s => /\.ew-week-row$/.test(s));
  assert.ok(rules.length >= 1, 'the Search rule is still there');
  for (const sel of rules) assert.match(sel, /^\.ew-search-results[ :]/, 'scoped to Search: ' + sel);
});

/* -- VL-24 / VL-26: category labels, brand page qualifier ----------------- */
test('VL-24: category slugs read as labels; unknown slugs are title-cased', () => {
  const U = loadAt('#/shows').EWUI;
  assert.equal(U.categoryLabel('truecrime'), 'True crime');
  assert.equal(U.categoryLabel('comedy'), 'Comedy');
  assert.equal(U.categoryLabel('business'), 'Business');
  assert.equal(U.categoryLabel('technology'), 'Technology');
  assert.equal(U.categoryLabel('self_help'), 'Self Help');
  const card = U.directoryCard({ id: 'cs-1', display_title: 'S', category_summary: ['truecrime', 'comedy'], episodes: 2 }, { kind: 'shows' });
  assert.match(card, /True crime · Comedy/);
  assert.doesNotMatch(card, /truecrime/);
});

const SHOW_DETAIL = (over = {}) => ({ show_id: 'cs-1', display_title: 'The Long Run', category_summary: ['truecrime'], artwork_url: null, unresolved: false,
  coverage: { episodes: 1, episodes_with_transcript: 1 }, sponsors: [], sponsors_not_counted: {},
  episodes: [{ id: 'e1', ep_title: 'One', pub_date: '2026-10-09', transcript: true, ads: 0, screening: null }], ...over });
const showApi = show => p => (p === '/customer/shows/cs-1' ? show : new Error('unexpected ' + p));

test('VL-24: the Show page eyebrow uses the category label', async () => {
  const r = await render('#/show/cs-1', showApi(SHOW_DETAIL()));
  assert.match(r.html, /Show · True crime/);
  assert.doesNotMatch(r.html, /truecrime/);
});

const brandDetail = top => ({ brand: { brand: 'progressive', key: 'progressive', display: 'Progressive', detections: 9,
  confirmed_occurrences: [], findings: [], offer_sequence: [], episodes: 4, shows: 3, last_heard: '2026-10-09',
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['progressive'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null }, mentions: null },
  top_shows: top, reads_without_show: 0 } });
const brandApi = detail => p => {
  if (p === '/customer/brands/progressive') return detail;
  if (p.startsWith('/customer/search?')) return { items: [], total: 0, total_relation: 'exact' };
  if (p.startsWith('/customer/summary?')) return { cells: [], total_relation: 'exact' };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};

test('VL-26: same-title shows on the Brand page carry the Shows page qualifier; distinct titles do not', async () => {
  const twins = [{ show_id: 'cs-19b86aaaaaaa', display_title: 'True Crime All The Time Unsolved', reads: 2, confirmed: 0, last_heard: null },
    { show_id: 'cs-dc483bbbbbbb', display_title: 'True Crime All The Time Unsolved', reads: 2, confirmed: 0, last_heard: null },
    { show_id: 'cs-other', display_title: 'Other Show', reads: 1, confirmed: 0, last_heard: '2026-10-09' }];
  const r = await render('#/brand/progressive', brandApi(brandDetail(twins)));
  const rows = r.html.match(/<tr><td><a class="lib-name"[\s\S]*?<\/tr>/g) || [];
  assert.equal(rows.length, 3);
  assert.match(rows[0], /cs-19b86…/);
  assert.match(rows[1], /cs-dc483…/);
  assert.doesNotMatch(rows[2], /<span class="lib-row-sub">[^<]*cs-/, 'a distinct title gets no qualifier');
  assert.match(rows[0], /air dates unknown/);
});

/* -- VL-32: plurals ------------------------------------------------------- */
test('VL-32: one brand and one show read "1 brand" and "1 show"', async () => {
  const b = await render('#/brands', p => (p === '/customer/brands' ? { brands: [{ key: 'a', display: 'A', brand: 'a', records: 3 }] }
    : p.startsWith('/customer/saved-items') ? { items: [] } : new Error('unexpected ' + p)));
  assert.match(b.html, /of 1 brand( |<|·)/);
  assert.doesNotMatch(b.html, /of 1 brands/);
  const s = await render('#/shows', p => (p === '/customer/shows' ? { shows: [{ id: 'cs-1', display_title: 'S', episodes: 1 }] } : new Error('unexpected ' + p)));
  assert.match(s.html, /of 1 show( |<|·)/);
  assert.doesNotMatch(s.html, /of 1 shows/);
});

/* -- VL-33: Compare copy -------------------------------------------------- */
const cellOf = (count, over = {}) => ({ count, ads: count, refs: count ? ['sr1:' + 'c'.repeat(32)] : [], coverage: { matched_in_scope: count },
  selector: count ? { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'confirmed_occurrences' } } : null, ...over });
const cmpData = (counts, over = {}) => ({ brands: counts.map(c => c[0]), period_a: null, period_b: null,
  shared_scale: Math.max(0, ...counts.map(c => c[1])), revision: 'r',
  cells: counts.map(([brand, n]) => ({ brand, a: cellOf(n), b: cellOf(0) })), ...over });
const DIR = { brands: [{ key: 'betterhelp', display: 'BetterHelp', records: 21 }, { key: 'mint mobile', display: 'Mint Mobile', records: 9 }] };
const compareApi = data => p => (p === '/customer/brands' ? DIR : p.startsWith('/customer/compare?') ? data : new Error('unexpected ' + p));

test('VL-33: "one shared scale" is said only when bars are drawn', async () => {
  const zero = await render('#/compare?brands=betterhelp,mint%20mobile', compareApi(cmpData([['betterhelp', 0], ['mint mobile', 0]])));
  assert.doesNotMatch(zero.html, /one shared scale/);
  const bars = await render('#/compare?brands=betterhelp,mint%20mobile', compareApi(cmpData([['betterhelp', 3], ['mint mobile', 0]])));
  assert.match(bars.html, /BetterHelp and Mint Mobile · one shared scale; zero stays zero\./);
});

test('VL-33: the zero line says "this period" for one period and "these periods" for two', async () => {
  const one = await render('#/compare?brands=betterhelp&from=2026-10-01&to=2026-10-08',
    compareApi(cmpData([['betterhelp', 0]], { period_a: { from: '2026-10-01', to: '2026-10-08' } })));
  assert.match(one.html, /for this period\./);
  const two = await render('#/compare?brands=betterhelp&from=2026-10-01&to=2026-10-08&compareFrom=2026-09-01&compareTo=2026-09-08',
    compareApi(cmpData([['betterhelp', 0]], { period_a: { from: '2026-10-01', to: '2026-10-08' }, period_b: { from: '2026-09-01', to: '2026-09-08' } })));
  assert.match(two.html, /for these periods\./);
  assert.doesNotMatch(two.html, /for this period/);
});

/* -- VL-34 / VL-37 -------------------------------------------------------- */
test('VL-34: a text link inside a section note is inline, without the 44 px box', () => {
  const css = read('styles.css');
  assert.match(css, /\.section-note \.text-link \{[^}]*display: inline;[^}]*min-height: 0;[^}]*padding: 0;[^}]*\}/);
});

test('VL-37: the episode filter on a show page is labelled "Episodes"', async () => {
  const r = await render('#/show/cs-1', showApi(SHOW_DETAIL()));
  assert.match(r.html, /<label class="lib-inline-field" for="show-episodes">Episodes<select id="show-episodes">/);
});

/* -- V-21 / VS F-16: stamps follow the Settings time zone ------------------ */
async function withZone(zone) {
  const win = loadAt('#/overview', { fetch: async (url) => {
    if (url === '/api/v1/customer/preferences') {
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1, time_zone: zone }, revision: 1 }) };
    }
    return sessionFetch(url);
  } });
  for (let i = 0; i < 20; i++) await flushBoot();
  // The first read learns the workspace context from its answer; the second one is cached under it.
  await win.EW.preferences();
  await win.EW.preferences();
  return win;
}
test('V-21: E.stamp formats in the Settings time zone and names it', async () => {
  const la = await withZone('America/Los_Angeles');
  // d16 decision 14: stamp() is a <time> (local words, UTC on hover); stampText() is the words alone.
  const words = html => html.replace(/<[^>]+>/g, '');
  assert.equal(words(la.EW.stamp('2026-10-16T05:10:00Z')), 'Oct 15, 10:10 PM PDT');
  assert.equal(la.EW.stampText('2026-10-16T05:10:00Z'), 'Oct 15, 10:10 PM PDT');
  const utc = await withZone('UTC');
  assert.equal(words(utc.EW.stamp('2026-10-16T05:10:00Z')), 'Oct 16, 5:10 AM UTC');
  const tokyo = await withZone('Asia/Tokyo');
  assert.match(words(tokyo.EW.stamp('2026-10-16T05:10:00Z')), /^Oct 16, 2:10 PM GMT\+9$/);
  assert.equal(utc.EW.stamp(null), 'No successful update');
  assert.equal(words(utc.EW.stamp('2026-10-16T05:10:00')), 'Oct 16, 5:10 AM UTC', 'a zoneless server time is UTC');
});
test('V-21: a stamp and a day label of the same moment agree on the date', async () => {
  const la = await withZone('America/Los_Angeles');
  assert.ok(la.EW.stampText('2026-10-16T05:10:00Z').startsWith(la.EW.formatDay('2026-10-16T05:10:00Z')));
});

/* -- V-25 / V-31: Settings ------------------------------------------------- */
const settingsApi = p => (p === '/customer/preferences' ? { preferences: { revision: 1, workspace_name: 'Studio' } } : new Error('unexpected ' + p));
test('V-25: Settings has one Workspace heading and the corrected copy', async () => {
  const r = await render('#/settings', settingsApi);
  assert.equal((r.html.match(/<h2>Workspace<\/h2>/g) || []).length, 1);
  assert.match(r.html, /The reader for this workspace is set by the operator\. See Connections\./);
  assert.doesNotMatch(r.html, /Reader scope is a local proposal/);
  assert.match(r.html, /Ads: what we found in the episodes we hold\. An ad found is not proof it ran as bought\./);
  assert.doesNotMatch(r.html, /Confirmed ads and unconfirmed findings|possible ad/, 'PLAIN-ADS');
  assert.doesNotMatch(r.html, /never reviewed-only/);
});
test('V-31: buyers see "Collection definitions", not "Corpus"', async () => {
  const r = await render('#/settings', settingsApi);
  assert.match(r.html, /<h2>Collection definitions<\/h2>/);
  assert.doesNotMatch(r.html, /Corpus/);
  assert.match(read('index.html'), /Collection definitions &amp; settings/);
  assert.doesNotMatch(read('index.html'), /Corpus/);
  assert.doesNotMatch(read('index.html'), /Operator tools/, 'd16 decision 12: the member footer drops the Operator tools link (it was decision D-5)');
  assert.match(r.html, /class="surface pad corpus-definitions"/, 'the CSS class name stays');
});
test('V-25: saving the display settings shows one "Saving…", in the status line', () => {
  const src = read('views.js');
  const at = src.indexOf("id === 'settings-display'");
  const body = src.slice(at, src.indexOf("id === 'workspace-choice'", at));
  assert.match(body, /settingsSaveState\(form, 'saving', 'Saving…'\)/);
  assert.match(body, /statusInline: true/, 'doSave leaves the button label and the page notice alone');
});
test('V-25: while display settings save, only the status line says "Saving…"', async () => {
  const gate = {}; gate.promise = new Promise(res => { gate.open = res; });
  const json = (o, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(o) });
  const fetch = async (url, init = {}) => {
    if (url === '/api/v1/customer/preferences' && (init.method || 'GET') === 'PUT') {
      await gate.promise;
      return json({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 2 }, revision: 2 });
    }
    if (url === '/api/v1/customer/preferences') return json({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 } });
    return sessionFetch(url);
  };
  const win = loadAt('#/settings', { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(read('views.js'), win, { filename: 'views.js' });
  await flushBoot();
  const notices = [];
  win.EW.saveNotice = m => notices.push(m);
  win.EW.toast = () => {}; win.EW.render = async () => {};
  const state = { dataset: {}, textContent: '' };
  const submit = { disabled: false, textContent: 'Save changes' };
  const form = { dataset: {}, isConnected: true, elements: [],
    getAttribute: a => (a === 'id' ? 'settings-display' : null), closest: () => null, contains: () => false,
    querySelector: s => ({ '.ew-save-state': state, '[type=submit]': submit }[s] || null), querySelectorAll: () => [] };
  win.FormData = class { *entries() { for (const e of [['expected_revision', '1'], ['display_name_set', ''], ['search_tab', 'ads'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', '']]) yield e; } };
  const p = win.EWViews.handleSubmit(form);
  for (let i = 0; i < 20; i++) await flushBoot();
  assert.equal(state.textContent, 'Saving…');
  assert.equal(submit.textContent, 'Save changes', 'the button keeps its label');
  assert.ok(!notices.includes('Saving…'), 'no second "Saving…" in the page notice');
  gate.open(); await p;
  assert.equal(submit.textContent, 'Save changes');
});

/* -- V-12 / B-10: reader check -------------------------------------------- */
const readerApi = (status = {}) => p => {
  if (p === '/customer/reader-status') return { configured: true, verified: false, profile: { profile: 'search_only', scopes: ['search', 'notes'] }, note: '', ...status };
  if (p === '/customer/preferences') return { preferences: { revision: 1 } };
  return new Error('unexpected ' + p);
};
test('B-10: Connections keeps profile and scopes in Technical details, not in the body', async () => {
  const r = await render('#/connections', readerApi());
  const tech = (r.html.match(/<details class="tech-details">[\s\S]*?<\/details>/) || [''])[0];
  assert.match(tech, /Profile: search_only · scopes: search, notes/);
  assert.doesNotMatch(r.html.replace(tech, ''), /scopes:/);
});
async function checkReader(status) {
  const r = await render('#/connections', readerApi(status));
  const box = { textContent: '' };
  const win = r.win;
  const prev = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#reader-check' ? box : prev(sel));
  win.EW.api = async p => { if (p === '/customer/reader-status') return { configured: true, verified: false, profile: { profile: 'x' }, note: '', ...status }; throw new Error('x'); };
  await win.EWViews.handleAction({ dataset: { action: 'check-reader' } });
  return box.textContent;
}
test('B-10: the reader check says when it ran and what it found', async () => {
  assert.match(await checkReader({ configured: false }), /^Checked \d{1,2}:\d{2}\s?[AP]M[^:]*: not configured\.$/);
  assert.match(await checkReader({ configured: true, verified: true }), /^Checked [^:]+:\d{2}\s?[AP]M[^:]*: configured, verified\.$/);
  assert.match(await checkReader({ configured: true, verified: false }), /: configured, not verified\.$/);
});
