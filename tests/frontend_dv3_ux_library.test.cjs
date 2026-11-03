/* Design v3 UX review, library worker (T4 worker A): Compare all dates,
   honest zero, "N confirmed"; Brands directory and Brand page confirmed
   split, top shows, per-read code/address tags, trend; Show two-part
   episode tags and the under-review sponsors count.
   Loader helpers copied from frontend_dv3_final_a.test.cjs.
   Run: node --test tests/frontend_dv3_ux_library.test.cjs */
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


/* -- Compare ------------------------------------------------------------ */
const cellOf = (count, over = {}) => ({ count, ads: count, refs: count ? ['sr1:' + 'c'.repeat(32)] : [], coverage: { matched_in_scope: count },
  selector: count ? { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'confirmed_occurrences' } } : null, ...over });
const cmpData = (counts, over = {}) => ({ brands: counts.map(c => c[0]), period_a: null, period_b: null,
  shared_scale: Math.max(0, ...counts.map(c => c[1])), revision: 'r',
  cells: counts.map(([brand, n, extra]) => ({ brand, a: cellOf(n, extra) })), ...over });
const DIR = { brands: [{ key: 'betterhelp', display: 'BetterHelp', records: 21, ad_breaks: 12, confirmed: 0, confirmed_ads: 0 }, { key: 'mint mobile', display: 'Mint Mobile', records: 9, ad_breaks: 5, confirmed: 0, confirmed_ads: 0 }] };
function compareApi(data, seen = []) {
  return (p, o) => {
    if (p === '/customer/brands') return DIR;
    if (p.startsWith('/customer/compare?')) { seen.push(p); return data; }
    return new Error('unexpected ' + p);
  };
}

test('Compare with no dates asks for all dates and renders the matrix', async () => {
  const seen = [];
  const r = await render('#/compare?brands=betterhelp,mint%20mobile', compareApi(cmpData([['betterhelp', 0], ['mint mobile', 0]]), seen));
  assert.equal(seen.length, 1, 'the comparison is fetched without a period');
  const q = new URLSearchParams(seen[0].split('?')[1]);
  assert.ok(!q.get('period_a') || q.get('period_a') === 'null', 'no period_a sent: ' + seen[0]);
  assert.match(r.html, /class="ew-table matrix lib-matrix"/);
  assert.match(r.html, /<th scope="col">All dates in this collection<\/th>/);
  assert.match(r.html, /Selected: all dates in this collection/);
  assert.doesNotMatch(r.html, /Choose a period/);
  assert.doesNotMatch(r.html, /field-error/);
  // PLAIN-ADS: the cell is the brand's ads, so no "Heard in N ads" line repeats it.
  assert.doesNotMatch(r.html, /Heard in|only confirmed ads are compared/);
  assert.doesNotMatch(r.html, /21 reads/, 'the read count is never shown');
  assert.doesNotMatch(r.html, /required/, 'the dates are optional');
});

test('Compare date errors: one date, empty pair, comparison without a selected period', async () => {
  const api = compareApi(cmpData([['betterhelp', 0]]));
  const one = await render('#/compare?brands=betterhelp&from=2026-10-01&to=', api);
  assert.match(one.html, /<p class="field-error">Enter both dates, or clear both to use all dates\./);
  assert.doesNotMatch(one.html, /lib-matrix/);
  const empty = await render('#/compare?brands=betterhelp&from=&to=', api);
  assert.doesNotMatch(empty.html, /field-error/);
  assert.match(empty.html, /All dates in this collection/);
  assert.match(empty.html, /lib-matrix/);
  const cmpOnly = await render('#/compare?brands=betterhelp&compareFrom=2026-09-01&compareTo=2026-09-30', api);
  assert.match(cmpOnly.html, /<p class="field-error">Choose a selected period before adding a comparison period\./);
  const dated = await render('#/compare?brands=betterhelp&from=2026-10-01&to=2026-10-08', api);
  assert.doesNotMatch(dated.html, /field-error/);
  assert.match(dated.html, /<th scope="col">Selected period<\/th>/);
  assert.doesNotMatch(dated.html, /Heard in/, 'the undated directory number never sits beside a dated count');
});

test('Compare with every count 0: no bars, no scale, one plain line on the matrix', async () => {
  const r = await render('#/compare?brands=betterhelp,mint%20mobile', compareApi(cmpData([['betterhelp', 0], ['mint mobile', 0]])));
  assert.doesNotMatch(r.html, /lib-bar/);
  assert.doesNotMatch(r.html, /Shared scale/);
  assert.match(r.html, /<p class="section-note lib-compare-zero">No ads were found for these brands in the episodes we hold\. This does not show they did not advertise\.<\/p>/);
  assert.match(r.html, /<th scope="row">BetterHelp/);
  assert.match(r.html, /<th scope="row">Mint Mobile/);
  assert.match(r.html, /<strong class="ew-num">0 ads<\/strong>/);
  assert.doesNotMatch(r.html, /observed<\/strong>|0 observed/);
  assert.doesNotMatch(r.html, /absence not established/);
  assert.match(r.html, /Ads found in the episodes we hold, each counted once however much evidence it has\./);
  assert.doesNotMatch(r.html, /confirmed/);
  const dated = await render('#/compare?brands=betterhelp&from=2026-10-01&to=2026-10-08', compareApi(cmpData([['betterhelp', 0]], { period_a: { from: '2026-10-01', to: '2026-10-08' } })));
  assert.match(dated.html, /in the episodes we hold for this period\./);
});

test('Compare with a positive count: bars on one scale, "N ads", shows and last heard', async () => {
  const r = await render('#/compare?brands=betterhelp,mint%20mobile', compareApi(cmpData([
    ['betterhelp', 3, { shows: 2, last_heard: '2026-10-09' }], ['mint mobile', 0, { shows: 0, last_heard: null }]])));
  assert.equal((r.html.match(/<svg class="chart-svg lib-bar"/g) || []).length, 2);
  assert.match(r.html, /Shared scale 0–3/);
  assert.match(r.html, /<strong class="ew-num">3 ads<\/strong>/);
  assert.match(r.html, /<strong class="ew-num">0 ads<\/strong>/);
  assert.doesNotMatch(r.html, /observed<\/strong>/);
  assert.match(r.html, /<span class="lib-row-sub">2 shows · last heard Oct 9<\/span>/);
  assert.doesNotMatch(r.html, /lib-compare-zero/);
  assert.match(r.html, /aria-label="BetterHelp, all dates in this collection: 3 ads\. Open exact records\."/);
  assert.doesNotMatch(r.html.replace(/href="[^"]*"/g, ''), /confirmed/, 'none in the page words (the fixture selector is a link target)');
});

test('Compare Apply with both dates empty drops the date params', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  const at = src.indexOf("id === 'compare-range-form'");
  const body = src.slice(at, src.indexOf("} else if (id === 'compare-select')", at));
  assert.match(body, /if \(!v\.from && !v\.to\) \{ params\.delete\('from'\); params\.delete\('to'\); \}/);
});

/* -- Brands directory and Brand page ------------------------------------ */
const dirApi = brands => p => {
  if (p === '/customer/brands') return { brands };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};

test('Brands directory: "Ads", no Confirmed column, and the honest intro (PLAIN-ADS)', async () => {
  const r = await render('#/brands', dirApi([{ key: 'betterhelp', display: 'BetterHelp', brand: 'betterhelp', records: 21, ad_breaks: 12, confirmed: 0, confirmed_ads: 0, shows: 16, last_heard: '2026-10-09' }]));
  assert.match(r.html, />Ads( ↓)?<\/th>/);
  assert.doesNotMatch(r.html, /Reads heard|\b21\b/, 'no read column or read count');
  assert.doesNotMatch(r.html, />Confirmed<\/th>|confirmed/);
  assert.match(r.html, /<td class="ew-num lib-wide">12<\/td><td class="ew-num lib-wide">16<\/td>/);
  assert.match(r.html, /<span>12 ads<\/span> · <span>16 shows<\/span>/);
  assert.match(r.html, /not an advertiser roster or a market-share estimate\. Ads with Brand not identified aren’t counted toward any brand\./);
  assert.match(r.html, /counts are ads heard in this collection<\/caption>/);
  const old = await render('#/brands', dirApi([{ key: 'betterhelp', display: 'BetterHelp', records: 21 }]));
  assert.doesNotMatch(old.html, />Confirmed<\/th>/, 'an older server without confirmed draws no column');
});

const brandDetail = (over = {}) => ({ brand: { brand: 'betterhelp', key: 'betterhelp', display: 'BetterHelp', detections: 21, ad_breaks: 12,
  confirmed_occurrences: [], findings: ['sr1:x'], offer_sequence: [], episodes: 19, shows: 16, last_heard: '2026-10-09',
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['betterhelp'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null },
    mentions: { version: 2, mode: 'mentions', q: '', brands: ['betterhelp'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null } },
  top_shows: [{ show_id: 'cs-a', display_title: 'Show A', reads: 3, ad_breaks: 2, confirmed: 0, confirmed_ads: 0, last_heard: '2026-10-09' },
    { show_id: 'cs-b', display_title: 'Show B', reads: 1, ad_breaks: 1, confirmed: 0, confirmed_ads: 0, last_heard: null }], reads_without_show: 2, ...over } });
const readItem = (over = {}) => ({ ref: 'sr1:' + 'r'.repeat(32), record_kind: 'evidence', status: 'finding', ep_title: 'Ep', show_title: 'Show A',
  pub_date: '2026-10-09', quotation_span: [60, 120], audio: 'available',
  read: { brand: { key: 'betterhelp', display: 'BetterHelp', confirmed: false }, display_span: [60, 120],
    said: { web_addresses: ['betterhelp.com/NPR', 'wrothies.com'], promo_codes: ['OFFER20'], address_brand: 'BetterHelp', code_brand: null, spoken_brand: 'BetterHelp' } }, ...over });
function brandApi({ detail = brandDetail(), items = [readItem()], summary = { cells: [{ key: '2026-10-05', count: 6, breaks: 4, interval: { from: '2026-10-05', to: '2026-10-11' } }], total_relation: 'exact' }, seen = [] } = {}) {
  return p => {
    seen.push(p);
    if (p === '/customer/brands/betterhelp') return detail;
    if (p.startsWith('/customer/search?')) return { items, total: items.length, total_relation: 'exact' };
    if (p.startsWith('/customer/summary?')) return summary;
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    return new Error('unexpected ' + p);
  };
}

test('Brand page: N ads, no status tags, per-read code and address tags without a brand', async () => {
  const r = await render('#/brand/betterhelp', brandApi());
  assert.match(r.html, /In this collection: 12 ads in 19 episodes across 16 shows · last heard Oct 9, 2026/);
  assert.doesNotMatch(r.html, /Possible ad|confirmed/);
  const tags = [...r.html.matchAll(/<span class="lib-said" title="Heard in this ad\.">(<span class="ew-tag[^>]*>[^<]*<\/span>)<\/span>/g)].map(m => m[1]);
  assert.equal(tags.length, 2, 'at most two fact tags');
  assert.match(tags[0], /Code heard: OFFER20/);
  assert.match(tags[1], /Address heard: betterhelp\.com\/NPR/);
  for (const t of tags) assert.doesNotMatch(t, /BetterHelp/, 'no brand name inside a fact tag');
  assert.match(r.html, /<span class="lib-muted">\+1 more<\/span>/);
});

test('Brand page: top shows link to the show; absent top_shows draws no section', async () => {
  const r = await render('#/brand/betterhelp', brandApi());
  assert.match(r.html, /<h2 id="where-h">Shows where it was heard<\/h2><span class="lib-count">Top 2 of 16 shows<\/span>/);
  assert.match(r.html, /<a class="lib-name" href="#\/show\/cs-a">Show A<\/a><span class="lib-row-sub">2 ads · last heard Oct 9<\/span>/);
  assert.match(r.html, /1 ad · air dates unknown/);
  assert.match(r.html, /Ads whose show isn’t settled aren’t listed by show\./);
  const old = brandDetail(); delete old.brand.top_shows; delete old.brand.reads_without_show;
  const o = await render('#/brand/betterhelp', brandApi({ detail: old }));
  assert.doesNotMatch(o.html, /where-h/);
});

test('Brand page: the weekly trend uses the summary; a failed summary draws nothing and no error', async () => {
  const seen = [];
  const r = await render('#/brand/betterhelp', brandApi({ seen }));
  assert.ok(seen.some(p => p.startsWith('/customer/summary?') && p.includes('group_by=week')));
  // The chart draws from the server's ad counts.
  assert.match(r.html, /<h2 id="trend-h">Ads by week<\/h2>/);
  assert.match(r.html, /data-count="4 ads"/);
  assert.match(r.html, /class="chart ew-week"/);
  const f = await render('#/brand/betterhelp', brandApi({ summary: new Error('boom') }));
  assert.doesNotMatch(f.html, /trend-h/);
  assert.doesNotMatch(f.html, /boom|didn’t load|unavailable/);
  assert.match(f.html, /Recent ads/);
});

/* -- Show page ----------------------------------------------------------- */
const SC = (over = {}) => ({ jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full', ...over });
const SHOW = { show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: 6, episodes_with_transcript: 5 },
  sponsors: [{ key: 'ag1', display: 'AG1', episodes: 2, ads: 6, ad_breaks: 4, first_seen: '2026-09-01', last_seen: '2026-10-09' },
    { key: 'zeta', display: 'Zeta', episodes: 1, ads: 1, ad_breaks: 1, first_seen: '2026-09-01', last_seen: '2026-09-01' }],
  sponsors_not_counted: { under_review: 1 },
  episodes: [
    { id: 'e1', ep_title: 'One', pub_date: '2026-10-09', transcript: true, ads: 3, ad_breaks: 2, screening: SC() },
    { id: 'e2', ep_title: 'Two', pub_date: '2026-10-02', transcript: true, ads: 0, screening: SC({ screened_no_ads: true }) },
    { id: 'e3', ep_title: 'Three', pub_date: '2026-09-18', transcript: true, ads: 0, screening: { stopped_partway: true, stage: 'transcribe', coverage_tier: 'full' } },
    { id: 'e4', ep_title: 'Four', pub_date: '2026-09-11', transcript: true, ads: 0, screening: null },
    { id: 'e5', ep_title: 'Five', pub_date: '2026-08-28', transcript: false, ads: 0, screening: null },
    { id: 'e6', ep_title: 'Six', pub_date: '2026-08-21', transcript: true, ads: 0, screening: SC() }] };
const showApi = show => p => (p === '/customer/shows/cs-1' ? show : new Error('unexpected ' + p));
const epRowOf = (html, id) => (html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];

test('Show page: one two-part episode tag; never "0 ads"', async () => {
  const r = await render('#/show/cs-1', showApi(SHOW));
  assert.match(epRowOf(r.html, 'e1'), />Screened · 2 ads listed</);
  assert.match(epRowOf(r.html, 'e2'), />Screened · no ads found</);
  assert.match(epRowOf(r.html, 'e3'), />Stopped partway</);
  assert.match(epRowOf(r.html, 'e4'), />Not screened yet · ads unknown</);
  assert.match(epRowOf(r.html, 'e5'), />Metadata only</);
  assert.match(epRowOf(r.html, 'e6'), />Screened · no ads listed</);
  assert.doesNotMatch(r.html, /\b0 ads\b/);
  assert.doesNotMatch(epRowOf(r.html, 'e4'), /no ads/);
});

test('Show sponsors: "excludes ads with Brand not identified" beside the heading; rows count ads, not reads', async () => {
  const r = await render('#/show/cs-1', showApi(SHOW));
  assert.match(r.html, /<h2 id="sponsors-h">Sponsors heard<\/h2><span class="ew-work-meta">excludes ads with Brand not identified<\/span>/);
  assert.match(r.html, /<td class="ew-num">4 ads<\/td>/);
  assert.match(r.html, /<td class="ew-num">1 ad<\/td>/);
  assert.doesNotMatch(r.html, /<td class="ew-num">\d+ reads?<\/td>|excludes 1\b/);
  assert.match(r.html, /Not counted: ads with Brand not identified and the show’s own promotions\./, 'the own-promotion note stays');
  const none = await render('#/show/cs-1', showApi({ ...SHOW, sponsors_not_counted: { under_review: 0 } }));
  assert.doesNotMatch(none.html, /excludes/);
  const old = { ...SHOW }; delete old.sponsors_not_counted;
  const o = await render('#/show/cs-1', showApi(old));
  assert.doesNotMatch(o.html, /excludes/);
});

/* -- T5: one longDate, 44 px Track below 1024 --------------------------- */
const REF = 'sr1:' + 'b2'.repeat(16);
const EV_ID = 'named:ep_f.named.120000-180000';
const evRecord = pub => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_f', show_title: 'Quiet Hours', ep_title: 'T', pub_date: pub, fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_f', orig_start_s: 110, orig_end_s: 180 }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [121.5, 170], has_clip: false },
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null } });
const evApi = pub => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: evRecord(pub) };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_f') return { episode: { episode_id: 'ep_f', self_promo: [], screened_no_ads: false, screening: null, ads: [] } };
  return new Error('unexpected ' + p);
};

test('one longDate: an undated episode says "Publication date unknown", never "Published Date unknown"', async () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.equal((src.match(/function longDate\(/g) || []).length, 1);
  const undated = await render('#/evidence/' + encodeURIComponent(REF), evApi(''), { hosted: false });
  assert.match(undated.html, /Publication date unknown/);
  assert.doesNotMatch(undated.html, /Published Date unknown/);
  const dated = await render('#/evidence/' + encodeURIComponent(REF), evApi('2026-10-07'), { hosted: false });
  assert.match(dated.html, /Published Oct 7, 2026/);
});

test('library.css: the compact Track button is 44 px below 1024 px or on a coarse pointer', () => {
  const css = fs.readFileSync(path.join(APP, 'library.css'), 'utf8');
  assert.match(css, /@media \(max-width: 1023px\), \(pointer: coarse\) \{ \.lib-brand-table \.lib-track-sm \{ min-height: 44px; \} \}/);
  // A later narrow-screen rule never sets it back under 44 px.
  const after = css.slice(css.indexOf('(pointer: coarse) { .lib-brand-table .lib-track-sm'));
  for (const m of after.matchAll(/\.lib-brand-table \.lib-track-sm \{ min-height: ([^;]+);/g)) assert.equal(m[1], '44px');
});

// ---- fixB F-22c ----
test('F-22c Brand page: a read with Brand not identified is not listed, so none of its code or address tags show', async () => {
  const hidden = readItem({ ref: 'sr1:' + 'h'.repeat(32), read: { brand: { key: null, display: null, confirmed: false, under_review: true }, display_span: [60, 120],
    said: { web_addresses: ['hidden-brand.com/NPR'], promo_codes: ['HIDDEN99'], address_brand: null, code_brand: null, spoken_brand: null } } });
  const r = await render('#/brand/betterhelp', brandApi({ items: [hidden, readItem()] }));
  const rows = r.html.match(/<li class="lib-read">[\s\S]*?<\/li>/g) || [];
  assert.equal(rows.length, 1, 'only the named read is listed');
  assert.doesNotMatch(r.html, /HIDDEN99|hidden-brand\.com|hhhhhhhh/);
  assert.match(rows[0], /Code heard: OFFER20/);
});

test('F-22c Brand page trend: a lower-bound summary says "at least" on every bar and in the caption', async () => {
  const summary = { cells: [{ key: '2026-10-05', count: 6, breaks: 4, interval: { from: '2026-10-05', to: '2026-10-11' } },
    { key: '2026-10-12', count: 3, breaks: 2, interval: { from: '2026-10-12', to: '2026-10-18' } }], total_relation: 'lower_bound' };
  const r = await render('#/brand/betterhelp', brandApi({ summary }));
  const chart = (r.html.match(/<section class="chart ew-week"[\s\S]*?<\/section>/) || [''])[0];
  assert.match(chart, /aria-label="[^"]*\(lower bound\)"/);
  assert.match(chart, /data-count="at least 4 ads"/);
  assert.match(chart, /data-count="at least 2 ads"/);
  assert.doesNotMatch(chart, /data-count="\d/, 'no bare count');
  assert.match(chart, /lower bounds/);
  assert.match(chart, /<td>at least 4<\/td>/);
  const exact = await render('#/brand/betterhelp', brandApi());
  assert.doesNotMatch(exact.html, /at least|lower bound/);
});

/* DEMO-SOL-R1-08, replaced by PLAIN-ADS (DEP7): with or without a
   confirmed ad, the brand page says heard. */
test('PLAIN-ADS brand page with 0 confirmed: shows where it was heard, last heard', async () => {
  const r = await render('#/brand/betterhelp', brandApi());
  assert.match(r.html, /<h2 id="where-h">Shows where it was heard<\/h2>/);
  assert.match(r.html, /<span class="lib-row-sub">2 ads · last heard Oct 9<\/span>/);
  assert.match(r.html, /across 16 shows · last heard Oct 9, 2026/);
  assert.doesNotMatch(r.html, /last detected|possible matches/);
  const conf = await render('#/brand/betterhelp', brandApi({ detail: brandDetail({ confirmed_occurrences: [{ ref: 'sr1:c' }] }) }));
  assert.match(conf.html, /<h2 id="where-h">Shows where it was heard<\/h2>/, 'a confirmed ad keeps the heard wording');
  assert.match(conf.html, /last heard Oct 9, 2026/);
});
