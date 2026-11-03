/* Design v3 final fixes A: honest ad labels in Search, the Show sponsors
   empty sentence, Compare display names and first-load error, the Brand
   under review heading, "This read" for possible ads, and the Episode
   transcript find. Loader helpers copied from frontend_dv3_g2.test.cjs.
   Run: node --test tests/frontend_dv3_final_a.test.cjs */
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
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
// Render one route against a path -> body function (an Error rejects).
async function render(route, api, { hosted = null } = {}) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
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

/* -- Search ------------------------------------------------------------ */
const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', product: 'Greens', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const bundle = (mode, items) => ({ query: { version: 2, mode, q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  throw new Error('unexpected ' + p);
};

// PLAIN-ADS (DEP7): every ad is "Ad" in the inspector, whatever its review.
test('the Search inspector names the ad "Ad", never "Ad read" and never its review state', async () => {
  const f = await render('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  assert.match(f.html, /Selected · Ad</);
  assert.match(f.html, /<th scope="row">Kind<\/th><td[^>]*>Ad</);
  assert.doesNotMatch(f.html, /Ad read|Possible ad|Name review/);
  const c = await render('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem({ status: 'confirmed' })])));
  assert.match(c.html, /Selected · Ad</);
  assert.match(c.html, /<th scope="row">Kind<\/th><td[^>]*>Ad</);
  assert.doesNotMatch(c.html, /Confirmed/);
  const m = await render('#/search?mode=mentions&q=sleep', searchApi(bundle('mentions', [adItem({ record_kind: 'transcript_passage', status: 'mention', read: null })])));
  assert.match(m.html, /Selected · Mention/);
  assert.match(m.html, /<th scope="row">Kind<\/th><td[^>]*>Transcript passage</);
});

test('All mentions: an evidence row carries no status label and never "Ad read"', async () => {
  const items = [adItem(), adItem({ ref: 'sr1:c', status: 'confirmed' }), adItem({ ref: 'sr1:p', record_kind: 'transcript_passage', status: 'mention', read: null })];
  const { html } = await render('#/search?mode=mentions&q=sleep', searchApi(bundle('mentions', items)), { hosted: false });
  const card = ref => (html.match(new RegExp(`<article class="ew-result[^>]*id="row-${ref}"[\\s\\S]*?</article>`)) || [''])[0];
  assert.doesNotMatch(card('sr1:a'), /Possible ad|Confirmed/);
  assert.doesNotMatch(card('sr1:c'), /Possible ad|Confirmed/);
  assert.doesNotMatch(html, /Ad read/);
  // dv3-ux UX-04: a mention names its kind with an outline "Mention" tag; it never carries an ad label.
  assert.doesNotMatch(card('sr1:p'), /data-tag="outline">(Possible ad|Confirmed ad)/);
  assert.match(card('sr1:p'), /data-tag="outline">Mention</);
});

test('List view: no status label; a held brand reads "Brand not identified"; each ad row carries Report a mistake', async () => {
  const hidden = adItem({ ref: 'sr1:h', read: { brand: { ...UNDER_REVIEW }, display_span: [10, 40], has_clip: false } });
  const { html } = await render('#/search?mode=ads&q=sleep&view=list', searchApi(bundle('ads', [adItem(), hidden])));
  const row = ref => (html.match(new RegExp(`<li class="ew-row[^>]*id="row-${ref}"[\\s\\S]*?</li>`)) || [''])[0];
  assert.doesNotMatch(row('sr1:a'), /Possible ad/);
  assert.match(row('sr1:a'), /<span class="ew-row-status ew-row-report"><details class="report-problem"><summary>Report a mistake<\/summary>/);
  assert.match(row('sr1:h'), /Brand not identified/);
  assert.doesNotMatch(row('sr1:h'), /Possible ad|Not counted yet/);
});

test('a Brand under review result card has a heading', async () => {
  const hidden = adItem({ ref: 'sr1:h', read: { brand: { ...UNDER_REVIEW }, display_span: [10, 40], has_clip: false } });
  const { html } = await render('#/search?mode=ads&q=sleep&view=cards', searchApi(bundle('ads', [hidden])));
  assert.match(html, /<h3 class="ew-result-title"><span class="ew-brand-none">Brand not identified<\/span><\/h3>/);
});

/* -- Show sponsors ------------------------------------------------------ */
const FULL = { jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full' };
const NOADS = { ...FULL, screened_no_ads: true };
const show = (episodes, sponsors = []) => ({ show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: episodes.length, episodes_with_transcript: episodes.length }, sponsors, episodes });
const ep = (id, over = {}) => ({ id, ep_title: id, pub_date: '2026-10-09', transcript: true, screening: NOADS, ...over });
const showHtml = async s => (await render('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? s : new Error('unexpected ' + p)))).html;

test('Show sponsors: the empty sentence follows what the episodes show', async () => {
  const listed = await showHtml(show([ep('e1', { ads: 2, screening: FULL }), ep('e2')]));
  assert.match(listed, /class="lib-sponsor-empty">No named sponsors yet\./);
  assert.match(listed, /Not counted: ads with Brand not identified/);
  assert.doesNotMatch(listed, /No ads found in this show/);
  const partial = await showHtml(show([ep('e1'), ep('e2', { screening: { stopped_partway: true, stage: 'transcribe' } })]));
  assert.match(partial, /class="lib-sponsor-empty">No ads found in the screened episodes\./);
  const unscreened = await showHtml(show([ep('e1'), ep('e2', { screening: null })]));
  assert.match(unscreened, /No ads found in the screened episodes\./);
  const meta = await showHtml(show([ep('e1'), ep('e2', { transcript: false, screening: null })]));
  assert.match(meta, /No ads found in the screened episodes\./);
  const clean = await showHtml(show([ep('e1'), ep('e2')]));
  assert.match(clean, /class="lib-sponsor-empty">No ads found in this show's episodes\./);
  const none = await showHtml(show([ep('e1', { screening: null }), ep('e2', { screening: { stopped_partway: true, stage: 'fetch' } })]));
  assert.match(none, /class="lib-sponsor-empty">No episodes screened yet\./);
  assert.doesNotMatch(none, /screened episodes/);
  const unknown = await showHtml(show([ep('e1', { screening: undefined }), ep('e2', { screening: null })]));
  assert.match(unknown, /class="lib-sponsor-empty">No named sponsors found\./, 'screening not reported: claim nothing about screening');
});

test('Find in transcript: lines added by Load more are filtered by the current words', async () => {
  const src = require('fs').readFileSync(require('path').join(__dirname, '..', 'app', 'views.js'), 'utf8');
  const body = src.slice(src.indexOf('async function appendWindow('), src.indexOf('\n  }\n', src.indexOf('async function appendWindow(')));
  assert.match(body, /data-transcript-find/, 'appendWindow re-applies the find field after appending lines');
});

/* -- Compare ------------------------------------------------------------ */
const cmp = brands => ({ brands: brands.map(b => b.brand), period_a: null, period_b: null, shared_scale: 1, revision: 'r',
  cells: brands.map(b => ({ ...b, a: { count: 1, ads: 1, refs: [], coverage: { matched_in_scope: 1 }, selector: null } })) });

test('Compare names brands by display name (key when none) and holds the date error until Apply', async () => {
  const api = p => (p.startsWith('/customer/compare?') ? cmp([{ brand: 'betterhelp' }, { brand: 'mint mobile' }])
    : p === '/customer/brands' ? { brands: [{ key: 'betterhelp', display: 'BetterHelp' }] } : new Error('unexpected ' + p));
  const first = await render('#/compare?brands=betterhelp,mint%20mobile', api);
  assert.match(first.html, /<p class="lib-scope">BetterHelp and mint mobile · /);
  assert.doesNotMatch(first.html, /field-error/, 'no error before the user applies dates');
  assert.match(first.html, /<th scope="col">All dates in this collection<\/th>/, 'no period means all dates (UX-06)');
  const applied = await render('#/compare?brands=betterhelp,mint%20mobile&from=&to=', api);
  assert.doesNotMatch(applied.html, /field-error/, 'both dates cleared: all dates, no error');
  const half = await render('#/compare?brands=betterhelp,mint%20mobile&from=2026-10-01&to=', api);
  assert.match(half.html, /<p class="field-error">Enter both dates, or clear both to use all dates\./);
  const ok = await render('#/compare?brands=betterhelp,mint%20mobile&from=2026-10-01&to=2026-10-08', api);
  assert.doesNotMatch(ok.html, /field-error/);
  assert.match(ok.html, /<th scope="row">BetterHelp<\/th>/);
  assert.match(ok.html, /<th scope="row">mint mobile<\/th>/);
});

/* -- Evidence pod ------------------------------------------------------- */
const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const EV_ID = 'named:ep_f.named.120000-180000';
const epAd = (id, span, display) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }, needs_review: false, self_promo: false,
  kind: 'named', source_label: 'Named read', transcript_start_s: span[0], media: null });
const record = status => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status,
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_f', show_title: 'Quiet Hours', ep_title: 'T', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_f', orig_start_s: 110, orig_end_s: 180 }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [121.5, 170], has_clip: false },
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null } });
const podApi = status => p => {
  // D-1: the server sends the read's ad break (the two reads overlap).
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: { ...record(status), ad_break: { id: 'brk:0', span: [60, 170], read_count: 2, index: 1, refs: ['e1', EV_ID], brands: [], on_current_transcript: true } } };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_f') return { episode: { episode_id: 'ep_f', self_promo: [], screened_no_ads: false,
    screening: { label: 'Screened', jev_screened: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false },
    ads: [epAd('e1', [60, 130], 'Northwind'), epAd(EV_ID, [121.5, 170], 'Northwind')] } };
  return new Error('unexpected ' + p);
};

test('the pod heading names the ad and its evidence the same way for a possible and a confirmed ad', async () => {
  // Row 1: the unit is the ad either way; the status tag, not the heading, says confirmed.
  const possible = await render('#/evidence/' + encodeURIComponent(REF), podApi('finding'), { hosted: false });
  assert.match(possible.html, /<h2 id="ew-pod-h">Other evidence for this ad/);
  assert.match(possible.html, /One of 2 pieces of evidence for this ad/);
  assert.doesNotMatch(possible.html, /This ad is|reads? in this ad break/);
  const confirmed = await render('#/evidence/' + encodeURIComponent(REF), podApi('confirmed'), { hosted: false });
  assert.match(confirmed.html, /<h2 id="ew-pod-h">Other evidence for this ad/);
  assert.match(confirmed.html, /One of 2 pieces of evidence for this ad/);
  // PLAIN-ADS (DEP7): both read "Ad"; no status tag says possible or confirmed.
  for (const page of [possible, confirmed]) {
    assert.match(page.html, /<span class="eyebrow">Ad · one of 2 pieces of evidence for this ad<\/span>/);
    assert.doesNotMatch(page.html, /Confirmed ad|Possible ad/);
  }
});

/* -- Episode transcript find -------------------------------------------- */
test('the episode transcript has a find field that filters the loaded lines (no request)', async () => {
  const rec = { ref: 'ep:ep_f', source_ref: 'sr1:e2e2', canonical_source_ref: 'sr1:e2e2', record_kind: 'source_publication', relationship: 'current',
    episode: { episode_id: 'ep_f', transcript_version_id: 3, ep_title: 'T', show_title: 'S', pub_date: '2026-10-09', duration_min: 58 },
    show: { show_identity_id: 'cs-long', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3480 }, selected_bounds: { start_s: 0, end_s: 3480 }, media: {},
    window: { segments: [{ segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 },
      { segment_id: 2, start_s: 120, end_s: 130, text: 'Go to AG1.', text_start: 0, text_end: 10 }], text_complete: true, next_cursor: null } };
  const calls = [];
  const { html, win } = await render('#/episode/ep_f', p => {
    calls.push(p);
    if (p === '/customer/records/' + encodeURIComponent('ep:ep_f')) return { record: rec };
    if (p === '/customer/episodes/ep_f') return { episode: { episode_id: 'ep_f', ads: [], self_promo: [], screened_no_ads: true,
      screening: { label: 'Screened', jev_screened: true, screened_no_ads: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false } } };
    return new Error('unexpected ' + p);
  });
  assert.match(html, /<input type="search" class="ew-find"[^>]* data-transcript-find[^>]*aria-label="Find in loaded transcript"/);
  assert.match(html, /data-find-status/);
  assert.doesNotMatch(html, /style=/);
  const lis = [{ textContent: 'Quick break.', hidden: false }, { textContent: 'Go to AG1.', hidden: false }]
    .map(l => ({ ...l, querySelector: () => l }));
  const status = { textContent: '' };
  const box = { querySelectorAll: () => lis, querySelector: sel => (sel === '[data-find-status]' ? status : null) };
  const input = v => ({ target: { value: v, matches: s => s === '[data-transcript-find]', closest: s => (s === '.ew-transcript' ? box : null) } });
  const fire = ev => win.__listeners.input.forEach(fn => fn(ev));
  const before = calls.length;
  fire(input('ag1'));
  assert.deepEqual(lis.map(l => l.hidden), [true, false]);
  assert.equal(status.textContent, '1 of 2 loaded lines match');
  fire(input('zzz'));
  assert.deepEqual(lis.map(l => l.hidden), [true, true]);
  assert.match(status.textContent, /No match in the loaded part/);
  fire(input('  '));
  assert.deepEqual(lis.map(l => l.hidden), [false, false]);
  assert.equal(status.textContent, '');
  assert.equal(calls.length, before, 'find reads only the lines already loaded');
});
