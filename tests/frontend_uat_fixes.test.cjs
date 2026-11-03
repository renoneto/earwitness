/* UAT fixes (owner walkthrough):
   2. The Evidence audio-unavailable note reads as proper sentences: the
      server's lowercase reason ("no authorized recording proof") is
      capitalised and ends with a full stop before the next sentence.
   3. UAT-013: the Settings "Results view" radios share the name results_view
      with the Search page's Cards/List switch. The Search handler must not
      act on them (it opened "Leave this page?" and snapped the radio back).
      Search's own switch keeps working, and a saved List view still opens
      Search on view=list (Ads tab).
   (1, the unknown-date sort, is a backend rule: tests/test_uat_fixes_sort.py.)
   Loader copied from frontend_dv3_astra_reader.test.cjs.
   Run: node --test tests/frontend_uat_fixes.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
// App timers (the 250 ms preference wait, retries) must not outlive a test.
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
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
let PREFS = {};
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1, ...PREFS }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, setup) {
  const win = loadAt('#/settings', { fetch: sessionFetch }); // boot on a route with no preference redirect
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  if (setup) setup(win);
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return setup ? win : main.innerHTML;
}

const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const EV_ID = 'named:ep_f.named.120000-180000';
const record = media => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_f', show_title: 'Quiet Hours', ep_title: 'T', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_f', orig_start_s: 110, orig_end_s: 180 }, media,
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [121.5, 170], has_clip: false },
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null } });
const api = media => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: record(media) };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_f') return { episode: { episode_id: 'ep_f', self_promo: [], screened_no_ads: false,
    screening: { label: 'Screened', jev_screened: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false }, ads: [] } };
  return new Error('unexpected ' + p);
};
const route = '#/evidence/' + encodeURIComponent(REF);
const noteOf = html => { const m = html.match(/<p class="ew-audio-note">([\s\S]*?)<\/p>/); assert.ok(m, 'an audio note is shown'); return m[1].replace(/<[^>]+>/g, ''); };
// d16 decision 7: the reason sentences are Technical details, one <p> each.
const techOf = html => { const m = html.match(/<details class="tech-details"><summary>Technical details<\/summary>([\s\S]*?)<\/details>/); assert.ok(m, 'technical details are shown'); return m[1].replace(/<\/p>/g, '</p> ').replace(/<[^>]+>/g, '').trim(); };
// The Episode page recording line still maps the server's reasons (views.js episodeAudio).
const EP_ROUTE = '#/episode/ep_f';
const epRecord = media => ({ ref: 'ep:ep_f', source_ref: REF, canonical_source_ref: REF, record_kind: 'source_publication', relationship: 'current',
  episode: { episode_id: 'ep_f', transcript_version_id: 3, ep_title: 'T', show_title: 'Quiet Hours', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 2484 }, selected_bounds: { start_s: 0, end_s: 2484 },
  bounds_basis: 'full_effective_source', media,
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null } });
const epApi = media => p => (p === '/customer/records/' + encodeURIComponent('ep:ep_f') ? { record: epRecord(media) } : api(media)(p));

test('UAT-2 Episode: "no authorized recording proof" reads as separate sentences, under Technical details', async () => {
  const html = await render(EP_ROUTE, epApi({ playback: 'unavailable', reason: 'no authorized recording proof', alignment: 'unknown', timing: 'unknown' }));
  assert.equal(noteOf(html), 'No clip (transcript only) The transcript remains readable. No recording or playback is implied.');
  assert.equal(techOf(html), 'No recording of this episode is kept here. Timing not checked against this audio.');
  assert.doesNotMatch(html, /authorized recording proof/i, 'no raw server reason');
});

test('UAT-2 Episode: the other unavailable reasons and the no-reason fallback end in a full stop before the next sentence', async () => {
  const cases = [
    [{ playback: 'unavailable', reason: 'registered media lacks pinned recording proof', alignment: 'unknown' }, 'The recording is not checked against this transcript. Timing not checked against this audio.'],
    [{ playback: 'unavailable', reason: 'no authorized recording proof.' }, 'No recording of this episode is kept here.'],
    [{ playback: 'unavailable' }, 'Source audio unavailable.'],
  ];
  for (const [media, want] of cases) {
    const html = await render(EP_ROUTE, epApi(media));
    const text = techOf(html);
    assert.equal(text, want, JSON.stringify(media));
    assert.doesNotMatch(text, /[a-z] (Alignment|Timing|The transcript)/, 'no sentence runs into the next');
    assert.doesNotMatch(text, /\.\./, 'no doubled full stop');
    assert.doesNotMatch(html, /lacks pinned recording proof|authorized recording proof/i, 'no raw server reason');
  }
});

// CLIP design 6.2 replaces the UAT-2 reason sentences on the Evidence player:
// no clip, for any reason, is one neutral sentence with no reason or technical details.
test('UAT-2 / CLIP 6.2: an unavailable recording shows only the neutral no-clip sentence on Evidence', async () => {
  const cases = [
    { playback: 'unavailable', reason: 'no authorized recording proof', alignment: 'unknown', timing: 'unknown' },
    { playback: 'unavailable', reason: 'registered media lacks pinned recording proof', alignment: 'unknown' },
    { playback: 'unavailable', reason: 'no authorized recording proof.' },
    { playback: 'unavailable' },
  ];
  for (const media of cases) {
    const html = await render(route, api(media));
    assert.ok(html.includes('No clip for this ad. The words are below.'), JSON.stringify(media));
    assert.doesNotMatch(html, /No recording of this episode|not checked against this transcript|Source audio unavailable|Timing not checked|authorized recording proof/, JSON.stringify(media));
  }
});

/* -- UAT-013 Settings results-view radios ---------------------------------- */
const fire = (win, target) => { for (const fn of win.__listeners.change || []) fn({ target }); };
const radio = (closest) => ({ name: 'results_view', value: 'list', type: 'radio', checked: true, closest: closest || (() => null) });
const prefApi = prefs => { PREFS = prefs; return p => new Error('unexpected ' + p); };
const spyGo = calls => win => { win.EW.go = (...a) => { calls.push(a); win.EW.current.route = String(a[0]); }; };

test('UAT-013: choosing Compact list on Settings does not navigate or open the leave guard', async () => {
  const calls = [];
  const win = await render('#/settings', prefApi({ results_view: 'cards' }), spyGo(calls));
  const radios = [{ name: 'results_view', value: 'cards', checked: false }, { name: 'results_view', value: 'list', checked: true }];
  win.document.querySelectorAll = sel => (sel === 'input[name=results_view]' ? radios : []);
  fire(win, radio(sel => (sel === '.ew-view-switch' ? null : null)));
  assert.deepEqual(calls, [], 'no navigation from the Settings radio');
  assert.equal(radios[1].checked, true, 'the radio is not snapped back to Cards');
});

test('UAT-013: the Search page switch still moves the URL to view=list', async () => {
  const calls = [];
  const win = await render('#/search?mode=ads', prefApi({}), spyGo(calls));
  calls.length = 0;
  win.EW.current.route = '#/search?mode=ads'; win.location.hash = '#/search?mode=ads';
  fire(win, radio(sel => (sel === '.ew-view-switch' ? {} : null)));
  assert.equal(calls.length, 1, 'one navigation');
  assert.match(String(calls[0][0]), /^#\/search\?.*view=list/);
});

test('UAT-013: a saved List view opens Search on the Ads tab with view=list', async () => {
  const calls = [];
  await render('#/search', prefApi({ search_tab: 'ads', results_view: 'list' }), spyGo(calls));
  assert.ok(calls.some(c => /^#\/search\?.*mode=ads/.test(String(c[0])) && /view=list/.test(String(c[0]))), JSON.stringify(calls));
});

test('UAT-013: List exists on the Ads tab only, so the All mentions default carries no view', async () => {
  const calls = [];
  await render('#/search', prefApi({ search_tab: 'mentions', results_view: 'list' }), spyGo(calls));
  assert.ok(calls.some(c => /mode=mentions/.test(String(c[0]))) && !calls.some(c => /view=/.test(String(c[0]))), JSON.stringify(calls));
});

/* -- UAT-037 Export… on an empty search ------------------------------------ */
const adItem = () => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'whole', display: 'Whole', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false } });
const bundle = items => ({ query: { version: 2, mode: 'ads', q: 'zz', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  return new Error('unexpected ' + p);
};
const exportOpen = html => (html.match(/<button[^>]*data-action="export-open"[^>]*>/) || [''])[0];

test('UAT-037: Export… is disabled with its reason when the search finds nothing', async () => {
  const html = await render('#/search?mode=ads&q=zz', searchApi(bundle([])));
  const btn = exportOpen(html);
  assert.ok(btn, 'the Export… button renders');
  assert.match(btn, /\bdisabled\b/);
  assert.match(btn, /aria-describedby="export-open-why"/);
  assert.match(html, /<span class="ew-help" id="export-open-why">Nothing to export: this search found no results\.<\/span>/);
});

test('UAT-037: Export… stays enabled, with no reason, when there are results', async () => {
  const html = await render('#/search?mode=ads&q=zz', searchApi(bundle([adItem()])));
  assert.doesNotMatch(exportOpen(html), /disabled|aria-describedby/);
  assert.doesNotMatch(html, /export-open-why/);
});

/* -- UAT-054 a second report replaces the open one -------------------------- */
const reportForm = () => {
  const box = { outerHTML: '<details>' }, status = { textContent: '' }, submit = { disabled: false };
  return { box, dataset: { reportRef: 'sr1:abc:1' }, classList: { contains: c => c === 'report-form' },
    closest: s => (s === '.report-problem' ? box : null),
    querySelector: s => s === '[role=status]' ? status : s === '[type=submit]' ? submit
      : s === 'input[name=reason]:checked' ? { value: 'wrong_brand' } : s === 'textarea[name=note]' ? { value: '' } : null };
};
const sendWith = async report => {
  const win = await render('#/settings', prefApi({}), () => {});
  win.EW.api = async () => ({ report });
  const form = reportForm();
  await win.EWViews.sendReport(form);
  return form.box.outerHTML;
};

test('UAT-054: a report that replaced an earlier open one says so', async () => {
  const html = await sendWith({ id: 7, status: 'received', replaced: true });
  assert.match(html, /We updated your earlier report on this ad\./);
  assert.match(html, /<b>✓ We updated your earlier report on this ad\.<\/b> <span>Nothing on this page changes for now\.<\/span>/);
  assert.doesNotMatch(html, /Your report was sent/);
});

test('UAT-054: a first report keeps the plain thank-you', async () => {
  for (const report of [{ id: 7, status: 'received', replaced: false }, { id: 7, status: 'received' }]) {
    const html = await sendWith(report);
    assert.match(html, /Your report was sent to the EarWitness team\./);
    assert.doesNotMatch(html, /replaced/);
  }
});
