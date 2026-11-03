/* D-1 / d16 row 1 customer UI. Fixtures follow the server contract
   (.tmp-uat-20261015/d16/D1-CONTRACT.md): the server groups evidence into
   ads (overlap on one transcript copy, split where two different brands
   are named) and the page never chains evidence itself. Counts read
   "N ads" from the server's ad fields, never a read count, and show no
   number without them; one record inside an ad is "evidence"; "Ad break"
   is only a label over 2+ ads of one overlap group.
   Loader copied from frontend_uat_search.test.cjs.
   Run: node --test tests/frontend_uat_dec_d1ui.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout, URL,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route, href: 'http://127.0.0.1:9172/app/' + route },
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
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return { html: main.innerHTML, win };
}
function loadViews() {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/\s+/g, ' ');
// A count of "ads" anywhere: "3 ads", "1 ad" (never "ad break", "ad read").
const ADS_COUNT = /\b\d[\d,]*\s+ads?\b(?! break| read)/;
// Row 1: customer counts are ads; a read count or "ad break" unit is never shown.
const READS_COUNT = /\b\d[\d,]*\s+reads?\b|ad breaks?/i;
// d16 row 1: no read/ad-break noun as a counted unit.
const READ_UNIT = /\b\d[\d,]*\s+(reads?|ad breaks?)\b/;

/* -- the contract's break rule, test side only: builds server-shaped
   fixtures (same episode and transcript copy; start < running max end). -- */
/* Row 1 (d16): like the server, an overlap chain splits wherever two
   different brands are named; a read naming no brand, or one under
   review, joins the current ad. Pieces keep the chain as group_id. */
function splitByBrand(chain, keysOf) {
  const ads = [];
  let cur = null;
  for (const r of chain) {
    const k = keysOf(r);
    if (!cur || (k.length && cur.keys.size && !k.some(x => cur.keys.has(x)))) { cur = { reads: [r], keys: new Set(k) }; ads.push(cur); }
    else { cur.reads.push(r); k.forEach(x => cur.keys.add(x)); }
  }
  return ads.map(a => a.reads);
}
const brandKeys = r => { const b = (r.read && r.read.brand) || r.brand || {}; return b.under_review || !b.key ? [] : [b.key]; };
function breaksOf(reads, { refOf = r => r.ref } = {}) {
  const groups = new Map();
  for (const r of reads) {
    const tv = r.transcript_version_id;
    const span = r.span || r.quotation_span;
    const key = tv == null || !span ? 'solo:' + refOf(r) : `${r.episode_id || 'ep'}|${tv}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const out = [];
  for (const list of groups.values()) {
    const sorted = list.slice().sort((a, b) => (a.span || a.quotation_span || [0])[0] - (b.span || b.quotation_span || [0])[0]);
    let cur = null;
    for (const r of sorted) {
      const sp = r.span || r.quotation_span;
      if (cur && sp && cur.max != null && sp[0] < cur.max) { cur.reads.push(r); cur.max = Math.max(cur.max, sp[1]); continue; }
      cur = { reads: [r], start: sp ? sp[0] : null, max: sp ? sp[1] : null };
      out.push(cur);
    }
  }
  const spanOf = r => r.span || r.quotation_span;
  const pieces = out.flatMap((b, i) => splitByBrand(b.reads, brandKeys).map((reads, j, all) => ({ reads, i, j, n: all.length })));
  return pieces.map(({ reads, i, j, n }) => ({ id: 'brk:' + String(i).padStart(16, '0') + (j ? '.' + j : ''),
    span: spanOf(reads[0]) ? [spanOf(reads[0])[0], Math.max(...reads.map(r => (spanOf(r) || [0, 0])[1]))] : null,
    reads, read_count: reads.length, refs: reads.map(refOf),
    transcript_version_id: reads[0].transcript_version_id ?? null, on_current_transcript: reads[0].on_current_transcript !== false,
    group_id: 'brk:' + String(i).padStart(16, '0'), group_ad_count: n }));
}

/* ======================================================================
   1. Search Ads: one card per ad break, its reads inside
   ====================================================================== */
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const brand = (display, confirmed = false) => ({ key: display.toLowerCase(), display, confirmed });
const adItem = (n, span, b, over = {}) => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', show_title: 'Show A',
  ep_title: 'Episode ' + (over.ep || 'One'), episode_id: over.ep || 'e1', pub_date: '2026-10-09', audio: 'unavailable', quotation_span: span,
  read: { brand: b, display_span: span, has_clip: false }, attribution: [], ...over });
// Two breaks on one page: break A holds 3 reads (2 match), break B one read.
function searchBundle({ items, breaks, total, breakTotal, view = 'cards', perPage = 10, extra = {} }) {
  return { query: { version: 2, mode: 'ads', q: 'chatgpt', sort: 'newest', page: 1, view }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 },
    result: { items, total, dated_total: total, undated_total: 0, total_relation: 'exact', per_page: perPage,
      ...(breaks ? { ad_breaks: breaks, break_total: breakTotal, break_dated_total: breakTotal, break_undated_total: 0 } : {}), ...extra },
    summary: { week: [], status: [{ key: 'confirmed', count: 0 }, { key: 'finding', count: total }] } };
}
const searchApi = bundle => p => {
  if (p === '/customer/research-query') return bundle;
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  return new Error('unexpected ' + p);
};
const pageOne = () => {
  const a1 = adItem(1, [60, 120], brand('ChatGPT', true), { ad_break_id: 'brk:a' });
  const a2 = adItem(2, [90, 150], brand('ChatGPT', true), { ad_break_id: 'brk:a' });
  const b1 = adItem(3, [600, 660], brand('Progressive'), { ad_break_id: 'brk:b', ep: 'e2' });
  const breaks = [
    { id: 'brk:a', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [60, 180], read_count: 3, matching_read_count: 2,
      refs: [a1.ref, a2.ref], brands: [{ key: 'chatgpt', display: 'ChatGPT', confirmed: true }], pub_date: '2026-10-09' },
    { id: 'brk:b', episode_id: 'e2', transcript_version_id: 8, on_current_transcript: true, span: [600, 660], read_count: 1, matching_read_count: 1,
      refs: [b1.ref], brands: [{ key: 'progressive', display: 'Progressive', confirmed: false }], pub_date: '2026-10-09' }];
  return { items: [a1, a2, b1], breaks };
};
const breakBoxes = html => html.match(/<section class="ew-break(?: [^"]*)?"[\s\S]*?<\/section>/g) || [];
const countLine = html => text((html.match(/<p class="ew-count-line"[^>]*>([\s\S]*?)<\/p>/) || [, ''])[1]);

test('D-1 Search Ads cards: one card per ad with its brands, range and "Evidence (k)"; evidence inside', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2 })));
  const boxes = breakBoxes(html);
  assert.equal(boxes.length, 2, 'one card per ad');
  const [a, b] = boxes;
  // PLAIN-ADS-FIX3 (screen R3-01): the header shows the range of the reads
  // listed (1:00–2:00 and 1:30–2:30), never the ad's whole window (1:00–3:00).
  assert.match(text(a), /1:00–2:30/);
  assert.match(text(b), /10:00–11:00/);
  assert.doesNotMatch(text(a) + text(b), /Ad break/);
  assert.match(b, /<span class="ew-break-count">Evidence \(1\)<\/span>/);
  assert.match(a, /<span class="ew-break-count">Evidence \(3\) · 2 match this search<\/span>/);
  assert.doesNotMatch(text(b), /match this search/);
  // The brand named in each ad (one per ad: two brands split); a name not confirmed keeps its flag.
  const titleOf = box => (box.match(/<h3 class="ew-break-title"[^>]*>[\s\S]*?<\/h3>/) || [''])[0];
  const brandsA = titleOf(a), brandsB = titleOf(b);
  assert.match(brandsA, /ChatGPT/);
  assert.doesNotMatch(brandsA, /Name not confirmed/, 'a confirmed name carries no flag');
  assert.match(brandsB, /Progressive<\/b><\/span>/);
  // Each piece of evidence keeps its own checkbox, inspector button and evidence link.
  assert.equal((a.match(/name="selected_ref" value="sr1:/g) || []).length, 2);
  assert.match(a, /data-action="inspect" data-index="0"/);
  assert.match(a, /data-action="inspect" data-index="1"/);
  assert.match(b, /data-action="inspect" data-index="2"/);
  assert.match(a, /href="#\/evidence\/sr1%3A1/);
  assert.match(b, /href="#\/evidence\/sr1%3A3/);
});

test('D-1 Search Ads count line: "N ads" from the server ad count, never reads', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2 })));
  const line = (html.match(/<p class="ew-count-line"[^>]*>([\s\S]*?)<\/p>/) || [, ''])[1];
  assert.match(text(line), /^Showing 1 to 2 of 2 ads$/);
  assert.match(html, /<span class="ew-tab-count"[^>]*>2<\/span>/, 'the Ads tab counts ads');
  const about = (html.match(/<summary>About these counts<\/summary>[\s\S]*?<\/details>/) || [''])[0];
  assert.match(text(about), /2 ads · All dates/);
  assert.match(text(about), /An ad is evidence in one episode whose times overlap, split where two different brands are named/);
  assert.doesNotMatch(about, /Ads counts ad reads from every detector/);
  assert.doesNotMatch(text(html), READ_UNIT);
});

test('D-1 Search Ads pages by ad: break_total (ads) sets the pages', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 60, breakTotal: 25, perPage: 2 })));
  assert.match(html, /<span>Page 1 of 13<\/span>/, '25 ads at 2 per page');
  assert.equal(countLine(html), 'Showing 1 to 2 of 25 ads');
  assert.doesNotMatch(text(html), /\b60 (ads|reads)\b/, 'the evidence total is never the count');
});

test('D-1 Search Ads list view: one row per ad, its evidence nested', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt&view=list', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2, view: 'list' })));
  const rows = html.match(/<li class="ew-break-row[^"]*"[\s\S]*?<\/ul><\/li>/g) || [];
  assert.equal(rows.length, 2);
  assert.match(text(rows[0]), /1:00–2:30/); // FIX3 screen R3-01: the listed reads' range
  assert.match(text(rows[0]), /Evidence \(3\) · 2 match this search/);
  assert.equal((rows[0].match(/<li class="ew-row/g) || []).length, 2, 'its matching evidence nested');
  assert.equal((rows[1].match(/<li class="ew-row/g) || []).length, 1);
  assert.match(rows[1], /data-index="2"/);
  assert.match(html, /id="select-page"/, 'select all on this page stays');
});

test('D-1 Search Ads: an ad on an earlier transcript copy says so; an ad with no time says so', async () => {
  const one = adItem(1, null, brand('Lowes'), { ad_break_id: 'brk:x', quotation_span: null, read: { brand: brand('Lowes') } });
  const two = adItem(2, [30, 90], brand('QVC'), { ad_break_id: 'brk:y' });
  const breaks = [
    { id: 'brk:x', episode_id: 'e1', transcript_version_id: null, on_current_transcript: true, span: null, read_count: 1, matching_read_count: 1, refs: [one.ref], brands: [{ key: 'lowes', display: 'Lowes', confirmed: true }], pub_date: null },
    { id: 'brk:y', episode_id: 'e1', transcript_version_id: 3, on_current_transcript: false, span: [30, 90], read_count: 1, matching_read_count: 1, refs: [two.ref], brands: [{ key: 'qvc', display: 'QVC', confirmed: true }], pub_date: null }];
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items: [one, two], breaks, total: 2, breakTotal: 2 })));
  const [x, y] = breakBoxes(html);
  assert.match(x, /<span class="ew-break-time"><span class="ew-time">time not recorded<\/span><\/span>/);
  assert.match(text(y), /Found in an earlier copy of the transcript/);
  assert.doesNotMatch(text(x), /earlier copy/, 'only the earlier-copy ad says so');
});

test('D-1 Search Ads: an ad that names no brand says so; under review names none', async () => {
  const one = adItem(1, [10, 40], { under_review: true }, { ad_break_id: 'brk:z' });
  const breaks = [{ id: 'brk:z', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [10, 40], read_count: 1, matching_read_count: 1, refs: [one.ref], brands: [], pub_date: '2026-10-09' }];
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items: [one], breaks, total: 1, breakTotal: 1 })));
  const [z] = breakBoxes(html);
  assert.match(text(z), /Brand not identified/);
  assert.equal(countLine(html), 'Showing 1 to 1 of 1 ad');
  assert.doesNotMatch(text(html), READ_UNIT);
});

test('D-1 Search Ads from an older server (no ad_breaks): flat cards, no number, never reads or ads', async () => {
  const { items } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, total: 3 })));
  assert.equal(breakBoxes(html).length, 0);
  assert.match(text(html), /Ads found · count not available/);
  assert.doesNotMatch(text(html), /Showing \d/, 'no server ad count: no number');
  assert.doesNotMatch(text(html), ADS_COUNT);
  assert.doesNotMatch(text(html), READ_UNIT);
});

test('D-1 Search Mentions keeps passages; no break boxes', async () => {
  const bundle = { query: { version: 2, mode: 'mentions', q: 'x', sort: 'newest', page: 1 }, revision: 'r', selector: null, coverage: {},
    result: { items: [{ ref: 'tx:1', record_kind: 'transcript_passage', text: 'x', episode_id: 'e1', show_title: 'S', pub_date: '2026-10-09' }], total: 1, dated_total: 1, undated_total: 0, total_relation: 'exact', per_page: 10 },
    summary: { week: [] } };
  const { html } = await render('#/search?mode=mentions&q=x', searchApi(bundle));
  assert.equal(breakBoxes(html).length, 0);
  assert.match(text(html), /Showing 1 to 1 of 1 mention/);
});

/* -- week chart: bars by breaks -- */
test('D-1 week chart: bars by ads; caption, readout and data table say ads', async () => {
  const win = loadViews();
  const sel = key => ({ version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'summary_cell', group_by: 'week', key } });
  const cells = [
    { key: '2026-09-07', label: 'w1', count: 10, breaks: 2, interval: { from: '2026-09-07', to: '2026-09-13' }, selector: sel('2026-09-07') },
    { key: '2026-09-14', label: 'w2', count: 6, breaks: 4, interval: { from: '2026-09-14', to: '2026-09-20' }, selector: sel('2026-09-14') },
    { key: 'unknown', label: 'Date unknown', count: 3, breaks: 1, selector: sel('unknown') }];
  const out = win.EWViews.weekChart(cells, { mode: 'ads', q: '' }, { total: 17, dated_total: 14, undated_total: 3, total_relation: 'exact' }, { split: true });
  const heights = [...out.strip.matchAll(/<rect class="bar-rect" x="0" y="\d+" width="24" height="(\d+)"/g)].map(m => Number(m[1]));
  assert.deepEqual(heights, [15, 30], 'the 4-ad week is the tall bar, not the 10-evidence week');
  assert.match(out.strip, /data-count="2 ads"/);
  assert.match(out.strip, /data-noun=""/, 'the readout shows the count text as is');
  assert.match(out.strip, /aria-label="Week of Sep 7–13: 2 ads\. Open exact records\."/);
  assert.match(out.strip, /most: 4 ads \(Sep 14\)/);
  assert.match(text(out.strip), /1 ad has no date in this scope/);
  assert.match(out.data, /<th>Dates \(inclusive\)<\/th><th>Ads<\/th><\/tr>/);
  assert.match(out.data, /<td>2<\/td><\/tr>/);
  assert.doesNotMatch(out.data + out.strip, /<td>10<\/td>|\b10 (ads|reads)\b/, 'the evidence count is never shown as ads');
  assert.match(out.data, /Ads · actual date intervals/);
  assert.doesNotMatch(text(out.data + out.strip), /\b(reads?|ad breaks?)\b/i);
});

test('D-1 week chart without breaks (mentions or older server) keeps counts as before', () => {
  const win = loadViews();
  const cells = [{ key: '2026-09-07', label: 'w1', count: 10, interval: { from: '2026-09-07', to: '2026-09-13' } }];
  const out = win.EWViews.weekChart(cells, { mode: 'mentions', q: 'x' }, { total: 10, dated_total: 10, undated_total: 0, total_relation: 'exact' }, { split: true });
  assert.match(out.strip, /data-count="10"/);
  assert.match(out.strip, /data-noun="mentions"/);
});

/* ======================================================================
   2. Episode page: breaks from episode.ad_breaks
   ====================================================================== */
const CUR = 36, OLD = 11;
const epAd = (id, span, display, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: display ? { key: display.toLowerCase(), display, confirmed: false } : { key: null, display: null, confirmed: false },
  needs_review: false, self_promo: false, kind: 'rule', transcript_start_s: span ? span[0] : null, media: null,
  transcript_version_id: CUR, on_current_transcript: true, ...over });
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
function epReads(ads, over = {}) {
  const breaks = breaksOf(ads.map(a => ({ ...a, episode_id: 'ep_d1' })), { refOf: a => a.evidence_id });
  const ids = new Map(breaks.flatMap(b => b.refs.map(r => [r, b.id])));
  const count = pred => breaks.filter(pred).length;
  return { episode_id: 'ep_d1', ads: ads.map(a => ({ ...a, ad_break_id: ids.get(a.evidence_id) })), self_promo: [], screening: SCREENED, screened_no_ads: false,
    current_transcript_version_id: CUR, earlier_transcripts: ads.some(a => a.on_current_transcript === false)
      ? [{ transcript_version_id: OLD, duration_s: 3544, fetch_date: '2026-09-21', fetch_date_basis: 'fetch_date' }] : [],
    ad_breaks: breaks.map(({ reads, ...b }) => ({ ...b, brands: [] })),
    ad_break_counts: { current: count(b => b.on_current_transcript && b.transcript_version_id != null), earlier: count(b => !b.on_current_transcript), no_copy: count(b => b.transcript_version_id == null) },
    ...over };
}
const epRecord = (segments = []) => ({ ref: 'ep:ep_d1', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
  record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
  episode: { episode_id: 'ep_d1', transcript_version_id: CUR, ep_title: 'An episode', show_title: 'Show A', pub_date: '2026-09-20',
    duration_min: 55.4, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3324 }, selected_bounds: { start_s: 0, end_s: 3324 }, media: {},
  window: { segments, text_complete: true, next_cursor: null } });
const episodePage = (reads, rec = epRecord()) => render('#/episode/ep_d1', p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_d1')) return { record: rec };
  if (p === '/customer/episodes/ep_d1') return { episode: reads };
  return {};
});
const adsCount = html => text((html.match(/<span class="ew-ads-count">([\s\S]*?)<\/span>/) || [, ''])[1]);
const podBoxes = html => html.match(/<div class="ew-pod ew-pod-box">[\s\S]*?<\/ol><\/div>/g) || [];
const lineTags = html => [...html.matchAll(/<span class="ew-line-tag">([^<]*)<\/span>/g)].map(m => m[1]);

test('D-1 Episode: the server’s ads make the boxes; the count says ads', async () => {
  // a1+a2 overlap and name one brand (one ad); a3 touches a2's end (its own ad); a4 alone.
  const ads = [epAd('a1', [60, 120], 'Squarespace'), epAd('a2', [100, 150], 'Squarespace'), epAd('a3', [150, 180], 'Quince'), epAd('a4', [900, 960], 'Lowes')];
  const { html } = await episodePage(epReads(ads));
  assert.equal(adsCount(html), '3 ads');
  const boxes = podBoxes(html);
  assert.equal(boxes.length, 1, 'only the multi-evidence ad is a box');
  assert.match(text(boxes[0]), /^Ad · 1:00–2:30/);
  assert.doesNotMatch(text(boxes[0]), /Ad break/, 'one ad is never an "Ad break"');
  assert.match(text(boxes[0]), /Evidence \(2\), some overlapping/);
  assert.doesNotMatch(boxes[0], /Quince/, 'evidence that only touches is its own ad');
  // Each row answers its bar spot (play/open), with no number shown.
  assert.deepEqual([...boxes[0].matchAll(/<li class="ew-ad-row[^"]*" data-spot-row="(\d)"/g)].map(m => m[1]), ['0', '1']);
  assert.doesNotMatch(boxes[0], /class="ew-pod-n"|ew-pod-num|[Rr]ead \d/);
  assert.doesNotMatch(html, /Ad pod|ad pod|back to back/);
  assert.doesNotMatch(text(html), READ_UNIT);
});

test('D-1 Episode: touching evidence is two ads (no client chaining, no 2 s gap)', async () => {
  const ads = [epAd('a1', [60, 120], 'Squarespace'), epAd('a2', [121, 150], 'AG1')];
  const { html } = await episodePage(epReads(ads));
  assert.equal(podBoxes(html).length, 0);
  assert.equal(adsCount(html), '2 ads');
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /POD_GAP_S|function adGroups/, 'the client chaining is gone');
});

test('D-1 Episode: transcript line tags say "Ad · name", never "Ad break"', async () => {
  const ads = [epAd('a1', [60, 120], 'Squarespace'), epAd('a2', [100, 150], 'AG1'), epAd('a4', [900, 960], 'Lowes')];
  const rec = epRecord([
    { segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 },
    { segment_id: 2, start_s: 100, end_s: 110, text: 'Go to AG1.', text_start: 0, text_end: 10 },
    { segment_id: 3, start_s: 900, end_s: 910, text: 'Lowes.', text_start: 0, text_end: 6 }]);
  const { html } = await episodePage(epReads(ads), rec);
  assert.deepEqual(lineTags(html), ['Ad · Squarespace', 'Ad · AG1', 'Ad · Lowes']);
});

test('D-1 Episode: earlier-copy and no-copy evidence keep their blocks; counts say ads per part', async () => {
  const ads = [epAd('c1', [60, 120], 'Lowes'),
    epAd('o1', [3400, 3480], 'Progressive', { transcript_version_id: OLD, on_current_transcript: false }),
    epAd('o2', [3450, 3500], 'Chumba', { transcript_version_id: OLD, on_current_transcript: false }),
    epAd('n1', [1000, 1060], 'Acme', { transcript_version_id: null, on_current_transcript: null })];
  const { html } = await episodePage(epReads(ads));
  // o1 Progressive and o2 Chumba overlap but name two brands: two earlier-copy ads.
  assert.equal(adsCount(html), '1 ad; no transcript copy: 1 ad; earlier copy: 2 ads');
  assert.match(html, /<div class="ew-earlier"><h3>Ads found in an earlier copy of the transcript<\/h3>/);
  assert.match(html, /<div class="ew-unplaced">/);
  assert.equal(podBoxes(html).length, 0, 'earlier-copy evidence never boxes with the current copy');
});

test('D-1 Episode from an older server (no ad_breaks): each piece stands alone, no number', async () => {
  const ads = [epAd('a1', [60, 120], 'Squarespace'), epAd('a2', [100, 150], 'AG1')];
  const reads = epReads(ads); delete reads.ad_breaks; delete reads.ad_break_counts;
  const { html } = await episodePage(reads);
  assert.equal(podBoxes(html).length, 0);
  assert.doesNotMatch(adsCount(html), /\d/, 'no server ad count: no number');
  assert.doesNotMatch(text(html), ADS_COUNT);
  assert.doesNotMatch(text(html), READ_UNIT);
});

test('D-1 pod bar: "Ad, N pieces of evidence"; each piece is "Evidence", never numbered', () => {
  const win = loadAt('#/overview');
  const bar = win.EWUI.podBar({ spots: [{ span: [0, 100], label: 'A' }, { span: [10, 50], label: 'B' }] });
  assert.match(bar, /aria-label="Ad, 2 pieces of evidence"/);
  assert.match(bar, /aria-label="Evidence, B, 0:10 to 0:50, overlaps other evidence"/);
  assert.doesNotMatch(bar, /[Ss]pot \d|Ad pod|[Rr]ead \d|ew-pod-num/);
});

/* ======================================================================
   3. Evidence page: the record's ad_break
   ====================================================================== */
const EVREF = 'sr1:' + 'v'.repeat(32);
const evRecord = (adBreak, over = {}) => ({ ref: EVREF, canonical_source_ref: EVREF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'ag1', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 100, end_s: 150 },
  episode: { episode_id: 'ep_d1', show_title: 'Show A', ep_title: 'An episode', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 55.4, transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Show A', unresolved: 0 },
  evidence: { id: 'a2', episode_id: 'ep_d1', orig_start_s: 100, orig_end_s: 150, transcript_version_id: CUR }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'rule', brand: { key: 'ag1', display: 'AG1', confirmed: false }, needs_review: false, self_promo: false,
    display_span: [100, 150], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [] } },
  window: { segments: [{ segment_id: 9, start_s: 100, end_s: 150, text: 'Go to AG1.', text_start: 0, text_end: 10 }], text_complete: true, next_cursor: null },
  ad_break: adBreak, ...over });
const evidencePage = (rec, reads) => render('#/evidence/' + encodeURIComponent(EVREF), p => {
  if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_d1') return { episode: reads };
  if (p === '/customer/brands') return { brands: [] };
  return {};
});
// One ad names one brand (two brands would split it): AG1, an unnamed piece, AG1.
const THREE = [epAd('a1', [60, 120], 'AG1'), epAd('a2', [100, 150], 'AG1'), epAd('a3', [140, 200], null)];
const brk3 = (index = 1) => ({ id: 'brk:3', span: [60, 200], read_count: 3, index, refs: ['a1', 'a2', 'a3'], brands: [], on_current_transcript: true });

test('D-1 Evidence: "one of 3 pieces of evidence for this ad" from the record’s ad_break, no ordinal', async () => {
  const { html } = await evidencePage(evRecord(brk3(1)), epReads(THREE));
  assert.match(html, /<span class="eyebrow">Ad · one of 3 pieces of evidence for this ad<\/span>/);
  const pod = (html.match(/<section class="ew-card ew-pod ew-pod-card"[\s\S]*?<\/section>/) || [''])[0];
  assert.match(text(pod), /Other evidence for this ad · 1:00–3:20/);
  assert.match(text(pod), /One of 3 pieces of evidence for this ad\./);
  assert.doesNotMatch(text(html), /\b(spot|read|Read|Spot) \d|\d+ of \d+ (reads )?in|overlaps (read|spot) \d/, 'index marks this read only, never an ordinal');
  assert.doesNotMatch(pod, /class="ew-pod-n"/);
  assert.equal((pod.match(/data-spot-row=/g) || []).length, 3);
  assert.match(pod, /<li data-spot-row="1" data-self/);
  assert.match(pod, /data-spot="1" aria-current="true"/);
  assert.doesNotMatch(html, /ad pod|Ad pod|spot \d of \d/);
});

test('D-1 Evidence: the refs may be evidence ids or source refs', async () => {
  const b = { ...brk3(0), refs: THREE.map(a => a.ref) };
  const { html } = await evidencePage(evRecord(b, { evidence: { id: 'a1', episode_id: 'ep_d1', orig_start_s: 60, orig_end_s: 120, transcript_version_id: CUR } }), epReads(THREE));
  assert.match(html, /one of 3 pieces of evidence for this ad/);
  assert.match(html, /<li data-spot-row="0" data-self/);
});

test('D-1 Evidence: the record’s refs may be null beside evidence_ids (contract change)', async () => {
  const b = { ...brk3(1), refs: [THREE[0].ref, null, THREE[2].ref], evidence_ids: ['a1', 'a2', 'a3'] };
  const reads = epReads(THREE);
  reads.ads[1].ref = null;
  const { html } = await evidencePage(evRecord(b), reads);
  assert.equal((html.match(/data-spot-row=/g) || []).length, 3);
  assert.match(html, /<li data-spot-row="1" data-self/);
});

test('D-1 Evidence: a one-read break, no break, or a read with no transcript copy shows no break section', async () => {
  const one = await evidencePage(evRecord({ id: 'brk:1', span: [100, 150], read_count: 1, index: 0, refs: ['a2'], brands: [], on_current_transcript: true }), epReads(THREE));
  assert.doesNotMatch(one.html, /ew-pod-card|in this ad break|evidence for this ad/);
  const none = await evidencePage(evRecord(null), epReads(THREE));
  assert.doesNotMatch(none.html, /ew-pod-card|in this ad break|evidence for this ad/);
  // R3-01: a versionless read never joins a break, whatever is sent.
  const loose = await evidencePage(evRecord(brk3(1), { evidence: { id: 'a2', episode_id: 'ep_d1', orig_start_s: 100, orig_end_s: 150, transcript_version_id: null } }), epReads(THREE));
  assert.doesNotMatch(loose.html, /ew-pod-card|in this ad break|evidence for this ad/);
  // An older server sends no ad_break: no client chaining either.
  const old = evRecord(null); delete old.ad_break;
  const o = await evidencePage(old, epReads(THREE));
  assert.doesNotMatch(o.html, /ew-pod-card|in this ad break|evidence for this ad/);
});

test('D-1 Evidence: a failed episode read keeps the ad line, never invents rows', async () => {
  const { html } = await evidencePage(evRecord(brk3(2)), new Error('boom'));
  assert.match(html, /<span class="eyebrow">Ad · one of 3 pieces of evidence for this ad<\/span>/);
  assert.doesNotMatch(html, /data-spot-row/);
});

/* ======================================================================
   4. Brand counts: directory, brand page, show sponsors, Overview
   ====================================================================== */
const dirApi = brands => p => {
  if (p === '/customer/brands') return { brands };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};
const DIRBRANDS = [
  { key: 'chatgpt', display: 'ChatGPT', brand: 'chatgpt', records: 13, ad_breaks: 7, confirmed: 2, confirmed_ads: 1, shows: 3, last_heard: '2026-10-09' },
  { key: 'progressive', display: 'Progressive', brand: 'progressive', records: 14, ad_breaks: 13, confirmed: 0, confirmed_ads: 0, shows: 5, last_heard: '2026-10-01' },
  { key: 'qvc', display: 'QVC', brand: 'qvc', records: 9, ad_breaks: 7, confirmed: 0, confirmed_ads: 0, shows: 1, last_heard: '2026-10-02' }];

test('D-1 Brands directory: an Ads column, no Reads heard; sorted by ads', async () => {
  const { html } = await render('#/brands', dirApi(DIRBRANDS));
  assert.match(html, /<thead><tr><th scope="col" aria-sort="none">Brand<\/th><th scope="col" class="ew-num lib-wide" aria-sort="descending">Ads ↓<\/th><th scope="col" class="ew-num lib-wide">Shows<\/th>/);
  assert.doesNotMatch(html, />Confirmed<\/th>/, 'PLAIN-ADS: no Confirmed column');
  assert.doesNotMatch(html, /Reads heard|Ad breaks/);
  assert.match(html, /<option value="reads" selected>Most ads<\/option>/);
  const order = [...html.matchAll(/<a class="lib-name" href="#\/brand\/[^"]*">([^<]+)<\/a>/g)].map(m => m[1]);
  assert.deepEqual(order, ['Progressive', 'ChatGPT', 'QVC'], 'ChatGPT and QVC tie on 7 ads; more reads first');
  assert.match(html, /<td class="ew-num lib-wide">7<\/td><td class="ew-num lib-wide">3<\/td>/, 'ChatGPT: 7 ads, then 3 shows; no confirmed or read column');
  const chatgpt = (html.match(/<tr><th scope="row" class="lib-brand-cell"><a class="lib-name" href="#\/brand\/chatgpt">[\s\S]*?<\/tr>/) || [''])[0];
  assert.ok(chatgpt, 'the ChatGPT row');
  assert.doesNotMatch(chatgpt, /\b13\b/, 'ChatGPT: its 13 reads are not shown');
  assert.match(html, /<span>7 ads<\/span> · <span>3 shows<\/span>/);
  assert.match(html, /counts are ads heard in this collection<\/caption>/);
  assert.match(html, /An ad that names two brands counts once for each\./);
  assert.doesNotMatch(text(html), READS_COUNT);
});

test('D-1 Brands directory from an older server (no ad_breaks): no count column, no number', async () => {
  const { html } = await render('#/brands', dirApi([{ key: 'betterhelp', display: 'BetterHelp', records: 21, confirmed: 0 }]));
  assert.doesNotMatch(html, />Ad breaks/);
  assert.doesNotMatch(html, />(Reads heard|Ads)( ↓)?<\/th>/);
  assert.match(html, /<option value="reads" selected>Most heard<\/option>/);
  assert.doesNotMatch(text(html), /\b21\b|\b\d+ (ads?|reads?)\b/);
});

const brandDetail = (over = {}) => ({ brand: { brand: 'chatgpt', key: 'chatgpt', display: 'ChatGPT', detections: 13, ad_breaks: 7,
  confirmed_occurrences: ['sr1:c1', 'sr1:c2'], findings: [], offer_sequence: [], episodes: 6, shows: 3, last_heard: '2026-10-09',
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['chatgpt'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null } },
  top_shows: [{ show_id: 'cs-a', display_title: 'Show A', reads: 3, ad_breaks: 2, confirmed: 0, confirmed_ads: 0, last_heard: '2026-10-09' }], reads_without_show: 0, ...over } });
const brandApi = (detail = brandDetail()) => p => {
  if (p === '/customer/brands/chatgpt') return detail;
  if (p.startsWith('/customer/search?')) return { items: [], total: 13, break_total: 7, total_relation: 'exact' };
  if (p.startsWith('/customer/summary?')) return { cells: [], total_relation: 'exact' };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};

test('D-1 Brand page: totals and top shows count ads', async () => {
  const { html } = await render('#/brand/chatgpt', brandApi());
  assert.match(html, /In this collection: 7 ads in 6 episodes across 3 shows/);
  assert.match(html, /<span class="lib-row-sub">2 ads · last heard Oct 9<\/span>/);
  assert.doesNotMatch(text(html), READS_COUNT);
});

const SHOW = { show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: 1, episodes_with_transcript: 1 },
  sponsors: [{ key: 'ag1', display: 'AG1', episodes: 2, ads: 6, ad_breaks: 4, first_seen: '2026-09-01', last_seen: '2026-10-09' },
    { key: 'zeta', display: 'Zeta', episodes: 1, ads: 1, ad_breaks: 1, first_seen: '2026-09-01', last_seen: '2026-09-01' }],
  sponsors_not_counted: { under_review: 1, unnamed: 2 },
  episodes: [{ id: 'e1', ep_title: 'One', pub_date: '2026-10-09', transcript: true, ads: 7, screening: SCREENED }] };

test('D-1 Show sponsors: each row counts ads, no reads', async () => {
  const { html } = await render('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? SHOW : new Error('unexpected ' + p)));
  const table = (html.match(/<section class="ew-card lib-sponsors"[\s\S]*?<\/section>/) || [''])[0];
  assert.match(table, /<td class="ew-num">4 ads<\/td>/);
  assert.match(table, /<td class="ew-num">1 ad<\/td>/);
  assert.match(text(table), /Some listed ads have no brand name and are not in this list\./);
  assert.doesNotMatch(text(table), READS_COUNT);
});

const overviewApi = brands => p => {
  if (p === '/customer/overview') return { coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [] };
  if (p.startsWith('/customer/saved-items')) return { items: [{ id: 'si-1', type: 'bookmark', revision: 1, target: { kind: 'brand', id: 'chatgpt' } }] };
  if (p === '/customer/brands') return { brands };
  if (p.startsWith('/customer/search')) return { items: [] };
  return new Error('unexpected ' + p);
};

test('D-1 Overview: a tracked brand row counts ads; never "possible detections"', async () => {
  const { html } = await render('#/overview', overviewApi(DIRBRANDS));
  const row = (html.match(/<li><div class="ew-ov-row"><a[^>]*href="#\/brand\/chatgpt"[\s\S]*?<\/li>/) || [''])[0];
  assert.match(row, /<span class="meta ew-ov-right">7 ads<\/span>/);
  assert.doesNotMatch(html, /possible detection/);
  const old = await render('#/overview', overviewApi([{ key: 'chatgpt', display: 'ChatGPT', records: 13 }]));
  assert.doesNotMatch(old.html, /ew-ov-right|13 reads/, 'an older server: no number');
});

/* ======================================================================
   5. Alerts: current matches count breaks first
   ====================================================================== */
const ALERT = { id: 5, name: 'ChatGPT', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: '2026-10-14T09:00:00Z', unread_event_count: 0, events: [], query: { version: 2, mode: 'ads', q: '', brands: ['chatgpt'] } };
const alertPage = alert => render('#/alert/5', p => {
  if (p === '/customer/alerts/5') return { alert };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'chatgpt', display: 'ChatGPT' }] };
  return new Error('unexpected ' + p);
});
const asideOf = html => html.slice(html.indexOf('<h3>Current matches</h3>'), html.indexOf('<h3>Check local records</h3>'));
const bigNumbers = html => [...asideOf(html).matchAll(/<p class="ew-work-big">([^<]*)<\/p>/g)].map(m => m[1]);

test('D-1 Alert: an ads alert counts ads from breaks ("7 ads")', async () => {
  const { html } = await alertPage({ ...ALERT, current_matches: { count: 13, breaks: 7, tracked: 13, tracked_by_kind: { new_finding: 13 }, held_for_brand_review: 0 } });
  assert.deepEqual(bigNumbers(html), ['7']);
  assert.match(text(asideOf(html)), /In stored records: 7 ads\./);
  assert.doesNotMatch(text(asideOf(html)), /\b13\b|\breads?\b/);
  const search = await alertPage({ ...ALERT, current_matches: { count: 13, breaks: 7 } });
  assert.deepEqual(bigNumbers(search.html), ['7']);
  assert.match(text(asideOf(search.html)), /7 ads match this alert’s search today\./);
  assert.doesNotMatch(text(asideOf(search.html)), /\b13\b|\breads?\b/);
});

test('D-1 / DEMO-SOL-R1-10 Alert: a words alert totals Search\'s passages; its ads are counted apart', async () => {
  const { html } = await alertPage({ ...ALERT, query: { version: 2, mode: 'mentions', q: 'chatgpt' },
    current_matches: { count: 16, tracked: 34, breaks: 7, tracked_by_kind: { new_text: 16, new_finding: 18 } } });
  assert.deepEqual(bigNumbers(html), ['16'], 'Search\'s 16 passages; the 7 ads stay apart (DEMO-SOL-R1-10)');
  assert.match(text(asideOf(html)), /In stored records: 16 matching passages, as Search counts them\. It also watches 7 ads in the same scope\./);
  assert.doesNotMatch(text(asideOf(html)), /\b18\b|\b34\b|\breads?\b/);
});

test('D-1 Alert from an older server (no breaks) shows no ad number', async () => {
  const { html } = await alertPage({ ...ALERT, current_matches: { count: 13, tracked: 13, tracked_by_kind: { new_finding: 13 } } });
  assert.deepEqual(bigNumbers(html), []);
  assert.match(text(asideOf(html)), /In stored records: ads \(no count from this server\)\./);
  assert.doesNotMatch(text(asideOf(html)), /\b13\b|\breads?\b/);
});

/* -- export toast: ads from the server ad count, else no number -- */
test('D-1 export toast: no server ad count, no number (never reads)', () => {
  const win = loadViews();
  assert.equal(win.EWViews.exportedText({ total: 2 }, 'ads'), 'Exported.');
  assert.equal(win.EWViews.exportedText({ total: 1 }, 'ads'), 'Exported.');
  assert.equal(win.EWViews.exportedText({ total: 2, ad_count: 1 }, 'ads'), 'Exported 1 ad.');
});
