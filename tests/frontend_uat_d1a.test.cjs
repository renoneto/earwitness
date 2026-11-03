/* d16 row 1, frontend A (worker d1fa): Search, Episode and Evidence count
   and name ads, not reads. Fixtures follow the d16 contract
   (.tmp-uat-20261015/d16/D1-CONTRACT.md): the server's `ad_breaks` entries
   are now ads (reads overlapping on one transcript copy, split where two
   brands are named), each with `group_id` and `group_ad_count`. Customers
   see "N ads"; one record inside an ad is "evidence"; "Ad break" is only a
   label over 2+ ads of one overlap group. Plus review #1 F2/F7 (audio
   state on Search). Loader copied from frontend_uat_dec_d1ui.test.cjs.
   Run: node --test tests/frontend_uat_d1a.test.cjs */
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
    location: { hash: route, href: 'http://127.0.0.1:9152/app/' + route },
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
async function render(route, api, { wide = false } = {}) {
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
  if (wide) win.matchMedia = q => ({ matches: /min-width: 1200px/.test(q) });
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
// A number of reads or ad breaks anywhere ("3 reads", "2 ad breaks").
const READ_COUNT = /\b\d[\d,]*\s+(reads?|ad breaks?|detections?)\b/i;

/* ======================================================================
   1. Search Ads: one card per ad
   ====================================================================== */
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const brand = (display, confirmed = false) => ({ key: display.toLowerCase(), display, confirmed });
const NONE = { key: null, display: null, confirmed: false };
const adItem = (n, span, b, over = {}) => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', show_title: 'Show A',
  ep_title: 'Episode ' + (over.ep || 'One'), episode_id: over.ep || 'e1', pub_date: '2026-10-09', audio: 'unavailable', quotation_span: span,
  show: { show_identity_id: 'cs-a' },
  read: { brand: b, display_span: span, has_clip: false }, attribution: [], ...over });
function searchBundle({ items, breaks, total, breakTotal, view = 'cards', perPage = 10, relation = 'exact', status = null, extra = {} }) {
  return { query: { version: 2, mode: 'ads', q: 'chatgpt', sort: 'newest', page: 1, view }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 },
    result: { items, total, dated_total: total, undated_total: 0, total_relation: relation, per_page: perPage,
      ...(breaks ? { ad_breaks: breaks, break_total: breakTotal, break_dated_total: breakTotal, break_undated_total: 0 } : {}), ...extra },
    summary: { week: [], status: status || [{ key: 'confirmed', count: 0 }, { key: 'finding', count: total }] } };
}
const searchApi = bundle => p => {
  if (p === '/customer/research-query') return bundle;
  if (p === '/customer/brands') return { brands: [{ key: 'chatgpt', display: 'ChatGPT', confirmed: 2 }] };
  if (p === '/customer/shows') return { shows: [] };
  if (p.startsWith('/customer/filter-values?')) return { items: [{ value: 'chatgpt', label: 'ChatGPT', confirmed: true }] };
  return new Error('unexpected ' + p);
};
// Ad A: an unnamed read, then the read that names ChatGPT (3 pieces of
// evidence, 2 match). Ad B: one Progressive read, name not confirmed.
const pageOne = () => {
  const a0 = adItem(1, [60, 120], NONE, { ad_break_id: 'brk:a' });
  const a1 = adItem(2, [90, 150], brand('ChatGPT', true), { ad_break_id: 'brk:a' });
  const b1 = adItem(3, [600, 660], brand('Progressive'), { ad_break_id: 'brk:b', ep: 'e2' });
  const breaks = [
    { id: 'brk:a', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [60, 180], read_count: 3, matching_read_count: 2,
      refs: [a0.ref, a1.ref], brands: [{ key: 'chatgpt', display: 'ChatGPT', confirmed: true }], pub_date: '2026-10-09', group_id: 'brk:a', group_ad_count: 1 },
    { id: 'brk:b', episode_id: 'e2', transcript_version_id: 8, on_current_transcript: true, span: [600, 660], read_count: 1, matching_read_count: 1,
      refs: [b1.ref], brands: [{ key: 'progressive', display: 'Progressive', confirmed: false }], pub_date: '2026-10-09', group_id: 'brk:b', group_ad_count: 1 }];
  return { items: [a0, a1, b1], breaks };
};
const adBoxes = html => html.match(/<section class="ew-break ew-ad"[\s\S]*?<\/section>/g) || [];
const countLine = html => text((html.match(/<p class="ew-count-line"[^>]*>([\s\S]*?)<\/p>/) || [, ''])[1]);

test('d1a Search: "Showing 1 to 2 of 2 ads"; the tab and About these counts say ads', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2 })));
  assert.match(countLine(html), /^Showing 1 to 2 of 2 ads\b/);
  assert.match(html, /<span class="ew-tab-count" title="2 ads">2<\/span>/);
  assert.match(html, /aria-label="Ads, 2 ads"/);
  const about = (html.match(/<summary>About these counts<\/summary>[\s\S]*?<\/details>/) || [''])[0];
  assert.match(text(about), /2 ads · All dates/);
  assert.match(text(about), /An ad is evidence in one episode whose times overlap, split where two different brands are named/);
  assert.match(text(about), /dated: 2 ads/);
  assert.doesNotMatch(text(html), READ_COUNT);
});

test('d1a Search: one card per ad; brand from the read that named it; "Evidence (k)" inside', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2 })));
  const boxes = adBoxes(html);
  assert.equal(boxes.length, 2, 'one card per ad');
  const [a, b] = boxes;
  const headA = (a.match(/<div class="ew-break-head">[\s\S]*?<\/div>/) || [''])[0];
  assert.match(text(headA), /ChatGPT/);
  // PLAIN-ADS-FIX3 (screen R3-01): the header shows the range of the reads
  // listed (1:00–2:00 and 1:30–2:30), never the ad's whole window (1:00–3:00).
  assert.match(text(headA), /1:00–2:30/);
  assert.doesNotMatch(text(headA), /3:00/);
  assert.match(text(headA), /Evidence \(3\) · 2 match this search/);
  assert.doesNotMatch(headA, /Name not confirmed/, 'a confirmed name carries no flag');
  const headB = (b.match(/<div class="ew-break-head">[\s\S]*?<\/div>/) || [''])[0];
  assert.match(headB, /Progressive<\/b><\/span>/);
  assert.doesNotMatch(headB, /Name not confirmed/, 'PLAIN-ADS: no name flag');
  assert.match(text(headB), /Evidence \(1\)/);
  assert.doesNotMatch(text(headB), /match this search/);
  // The evidence of the ad sits inside, each with its own select box, preview and link.
  assert.match(a, /<div class="ew-break-reads" role="group" aria-label="Evidence for this ad">/);
  assert.equal((a.match(/name="selected_ref" value="sr1:/g) || []).length, 2);
  assert.match(a, /data-action="inspect" data-index="0"/);
  assert.match(a, /data-action="inspect" data-index="1"/);
  assert.match(b, /data-action="inspect" data-index="2"/);
  assert.doesNotMatch(text(html), /Ad break|Brands named/);
});

test('d1a Search: attaching from an ad card attaches the read that named it, else the first read', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2 })));
  const [a, b] = adBoxes(html);
  const attach = box => ((box.match(/<button[^>]*data-action="add-to-campaign"[^>]*>/) || [''])[0].match(/data-ref="([^"]*)"/) || [, ''])[1];
  assert.equal(attach(a), REF(2), 'the ChatGPT read, not the unnamed first read');
  assert.equal(attach(b), REF(3));
  // An ad whose reads name no brand attaches its first read.
  const u0 = adItem(4, [10, 40], NONE, { ad_break_id: 'brk:u' }), u1 = adItem(5, [20, 50], { under_review: true }, { ad_break_id: 'brk:u' });
  const ub = [{ id: 'brk:u', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [10, 50], read_count: 2, matching_read_count: 2, refs: [u0.ref, u1.ref], brands: [], pub_date: '2026-10-09', group_id: 'brk:u', group_ad_count: 1 }];
  const u = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items: [u0, u1], breaks: ub, total: 2, breakTotal: 1 })));
  const [box] = adBoxes(u.html);
  assert.equal(attach(box), REF(4));
  assert.match(text(box), /Brand not identified/);
  assert.match(countLine(u.html), /^Showing 1 to 1 of 1 ad\b/);
});

test('d1a Search: an ad on an earlier copy says it was found in an earlier copy of the transcript', async () => {
  const two = adItem(2, [30, 90], brand('QVC', true), { ad_break_id: 'brk:y' });
  const breaks = [{ id: 'brk:y', episode_id: 'e1', transcript_version_id: 3, on_current_transcript: false, span: [30, 90], read_count: 1, matching_read_count: 1, refs: [two.ref], brands: [{ key: 'qvc', display: 'QVC', confirmed: true }], pub_date: null, group_id: 'brk:y', group_ad_count: 1 }];
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items: [two], breaks, total: 1, breakTotal: 1 })));
  assert.match(text(adBoxes(html)[0]), /Found in an earlier copy of the transcript/);
});

test('d1a Search: a lower bound says "at least N ads" in the line, the tab and the dated part', async () => {
  const { items, breaks } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 60, breakTotal: 25, perPage: 2, relation: 'lower_bound' })));
  assert.match(countLine(html), /^Showing 1 to 2 of at least 25 ads\b/);
  assert.match(html, /aria-label="Ads, at least 25 ads"/);
  assert.match(text(html), /dated: at least 25 ads/);
  assert.match(html, /<span>Page 1 of 13<\/span>/, '25 ads at 2 per page');
});

// PLAIN-ADS (DEP7): no status mix after the count, whatever the status cells.
test('d1a Search: no status mix after the count', async () => {
  const { items, breaks } = pageOne();
  const status = [{ key: 'confirmed', count: 1, breaks: 1 }, { key: 'finding', count: 2, breaks: 1 }];
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2, status })));
  assert.match(countLine(html), /of 2 ads$/);
  assert.doesNotMatch(html, /ew-count-mix|confirmed|possible/);
  const old = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, breaks, total: 3, breakTotal: 2 })));
  assert.doesNotMatch(old.html, /ew-count-mix/, 'status cells without an ad count: no mix (never a read count)');
});

test('d1a Search from an older server (no ad count): no number of ads or reads', async () => {
  const { items } = pageOne();
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items, total: 3 })));
  assert.equal(adBoxes(html).length, 0);
  assert.match(countLine(html), /^Ads found · count not available/);
  assert.doesNotMatch(countLine(html), /\d/);
  assert.doesNotMatch(html, /data-mode="ads"[^>]*>Ads<span class="ew-tab-count"/);
  assert.doesNotMatch(text(html), READ_COUNT);
  assert.doesNotMatch(text(html), /\b\d+ ads?\b/);
});

test('d1a Search: brand jump, rail note and brand filter help say ads and evidence', async () => {
  const { items, breaks } = pageOne();
  const bundle = searchBundle({ items, breaks, total: 3, breakTotal: 2 });
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(bundle), { wide: true });
  assert.match(text(html), /Show only ads named ChatGPT/);
  assert.match(text(html), /Ads with Brand not identified match no brand\./);
  assert.match(text(html), /Most heard first, one name per brand with spellings merged\./);
});

test('d1a week chart: bars and counts are ads; the data table has one Ads column', () => {
  const win = loadViews();
  const sel = key => ({ version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'summary_cell', group_by: 'week', key } });
  const cells = [
    { key: '2026-09-07', label: 'w1', count: 10, breaks: 2, interval: { from: '2026-09-07', to: '2026-09-13' }, selector: sel('2026-09-07') },
    { key: '2026-09-14', label: 'w2', count: 6, breaks: 4, interval: { from: '2026-09-14', to: '2026-09-20' }, selector: sel('2026-09-14') },
    { key: 'unknown', label: 'Date unknown', count: 3, breaks: 1, selector: sel('unknown') }];
  const out = win.EWViews.weekChart(cells, { mode: 'ads', q: '' }, { total: 17, dated_total: 14, undated_total: 3, total_relation: 'exact' }, { split: true });
  const heights = [...out.strip.matchAll(/<rect class="bar-rect" x="0" y="\d+" width="24" height="(\d+)"/g)].map(m => Number(m[1]));
  assert.deepEqual(heights, [15, 30]);
  assert.match(out.strip, /data-count="2 ads"/);
  assert.match(out.strip, /aria-label="Week of Sep 7–13: 2 ads\. Open exact records\."/);
  assert.match(out.strip, /most: 4 ads \(Sep 14\)/);
  assert.match(text(out.strip), /1 ad has no date in this scope/);
  assert.match(out.data, /<th>Dates \(inclusive\)<\/th><th>Ads<\/th><\/tr>/);
  assert.match(out.data, /<td>2<\/td><\/tr>/);
  assert.match(out.data, /Ads by date · Ads · actual date intervals/);
  assert.doesNotMatch(out.data, /confirmed|possible/);
  assert.doesNotMatch(out.strip + out.data, /[Rr]eads?\b|ad breaks?/);
  // Ads cells without an ad count (an older server): no bars, no number.
  const old = win.EWViews.weekChart(cells.map(({ breaks, ...c }) => c), { mode: 'ads', q: '' }, { total: 17, dated_total: 14, undated_total: 3, total_relation: 'exact' }, { split: true });
  assert.doesNotMatch(old.strip + old.data, /data-count|bar-rect|\b\d+ (ads?|reads?)\b/);
  // No summary cells: the undated part still says its ads from the server
  // total, and never the evidence total (3) as ads.
  const bare = win.EWViews.weekChart([], { mode: 'ads', q: '' }, { total: 3, undated_total: 3, break_undated_total: 2, total_relation: 'exact' }, { split: true });
  assert.match(text(bare.strip + bare.data), /2 ads have no date in this scope/);
  assert.doesNotMatch(text(bare.strip + bare.data), /\b3\b/);
  const bareOld = win.EWViews.weekChart([], { mode: 'ads', q: '' }, { total: 3, undated_total: 3, total_relation: 'exact' }, { split: true });
  assert.doesNotMatch(text(bareOld.strip + bareOld.data), /\b\d+\b/, 'an older server: no number');
});

test('d1a export toast: "Exported N ads" from the server ad count, else no number', () => {
  const win = loadViews();
  assert.equal(win.EWViews.exportedText({ total: 5, ad_count: 2 }, 'ads'), 'Exported 2 ads.');
  assert.equal(win.EWViews.exportedText({ item_count: 1, ad_count: 1 }, 'ads'), 'Exported 1 ad.');
  // No server ad count: never a row count dressed as ads.
  assert.equal(win.EWViews.exportedText({ total: 5 }, 'ads'), 'Exported.');
  assert.equal(win.EWViews.exportedText({ total: 1 }, 'ads'), 'Exported.');
  assert.equal(win.EWViews.exportedText({ total: 3 }, 'mentions'), 'Exported 3 passages.');
});

/* -- review #1 F2 / F7: Search audio state -- */
test('d1a F2: a rotated feed reads "Audio changed since transcription" on Search cards and the inspector', async () => {
  const win = loadAt('#/overview');
  const ui = win.EWUI;
  assert.equal(ui.audioState({ audio: 'available', audio_trust: 'rotated' }), 'Audio changed since transcription');
  assert.equal(ui.audioState({ audio_local: true, audio_trust: 'rotated' }), 'Audio changed since transcription');
  assert.match(ui.audioDetail({ audio: 'available', audio_trust: 'rotated' }), /[Rr]otated/);
  const it = adItem(9, [60, 120], brand('Chumba'), { audio: 'available', audio_local: true, audio_trust: 'rotated' });
  it.read.has_clip = true;
  const card = ui.sourceCard(it, { index: 0 });
  assert.match(card, /data-action="search-play"/, 'a local clip still plays');
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items: [it], total: 1 })));
  const aside = (html.match(/<aside class="ew-inspector"[\s\S]*<\/aside>/) || [''])[0];
  assert.match(aside, /Audio changed since transcription/);
  assert.doesNotMatch(aside, /Clip available/);
});

test('d1a F7: the no-play slot never says "Clip available"', () => {
  const win = loadAt('#/overview');
  const ui = win.EWUI;
  const it = adItem(8, null, brand('Lowes'), { audio_local: true, quotation_span: null });
  it.read.display_span = null;
  assert.match(ui.sourceCard(it, {}), /<span class="ew-noclip">No clip \(transcript only\)<\/span>/);
  assert.match(ui.resultRow(it, {}), /title="No clip \(transcript only\)"/);
  assert.equal(ui.clipNote(it), 'No clip (transcript only)');
});

test('d1a F7: the inspector Audio row never says "Clip available" when nothing plays there', async () => {
  const it = adItem(8, null, brand('Lowes'), { audio_local: true, quotation_span: null });
  it.read.display_span = null;
  const { html } = await render('#/search?mode=ads&q=chatgpt', searchApi(searchBundle({ items: [it], total: 1 })));
  const aside = (html.match(/<aside class="ew-inspector"[\s\S]*<\/aside>/) || [''])[0];
  assert.ok(aside.length > 0, 'the inspector rendered');
  assert.doesNotMatch(aside, /Clip available/);
  assert.match(aside, /No clip \(transcript only\)/);
});

/* ======================================================================
   2. Episode page: ads listed with their span; "Ad break" groups 2+ ads
   ====================================================================== */
const CUR = 36, OLD = 11;
const epAd = (id, span, display, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: display ? { key: display.toLowerCase(), display, confirmed: false } : { key: null, display: null, confirmed: false },
  needs_review: false, self_promo: false, kind: 'rule', transcript_start_s: span ? span[0] : null, media: null,
  transcript_version_id: CUR, on_current_transcript: true, ...over });
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
const ad = (id, span, refs, group, n, over = {}) => ({ id, episode_id: 'ep_d1', span, read_count: refs.length, refs, brands: [],
  transcript_version_id: CUR, on_current_transcript: true, group_id: group, group_ad_count: n, ...over });
// Ad A (a1 ChatGPT + a2 unnamed) and ad B (a3 AG1) are one overlap group
// split by brand; ad C (a4 Lowes) stands alone.
function epReads(over = {}) {
  const ads = [epAd('a1', [60, 120], 'ChatGPT'), epAd('a2', [100, 150], null), epAd('a3', [140, 200], 'AG1'), epAd('a4', [900, 960], 'Lowes')];
  const ad_breaks = [ad('brk:A', [60, 150], ['a1', 'a2'], 'brk:A', 2), ad('brk:B', [140, 200], ['a3'], 'brk:A', 2), ad('brk:C', [900, 960], ['a4'], 'brk:C', 1)];
  const ids = { a1: 'brk:A', a2: 'brk:A', a3: 'brk:B', a4: 'brk:C' };
  return { episode_id: 'ep_d1', ads: ads.map(a => ({ ...a, ad_break_id: ids[a.evidence_id] })), self_promo: [], screening: SCREENED, screened_no_ads: false,
    current_transcript_version_id: CUR, earlier_transcripts: [], ad_breaks, ad_break_counts: { current: 3, earlier: 0, no_copy: 0 }, ...over };
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
const adBreakBoxes = html => html.match(/<div class="ew-adbreak">[\s\S]*?<!--\/adbreak--><\/div>/g) || [];
const podBoxes = html => html.match(/<div class="ew-pod ew-pod-box">[\s\S]*?<\/ol><\/div>/g) || [];

test('d1a Episode: counts ads; an ad with 2+ pieces of evidence is "Ad · range" with "Evidence (k)"', async () => {
  const { html } = await episodePage(epReads());
  assert.equal(adsCount(html), '3 ads');
  const boxes = podBoxes(html);
  assert.equal(boxes.length, 1, 'only the two-evidence ad is a box');
  assert.match(text(boxes[0]), /^Ad · 1:00–2:30/);
  assert.match(text(boxes[0]), /Evidence \(2\), some overlapping · press one to open it/);
  assert.match(boxes[0], /aria-label="Ad, 2 pieces of evidence"/);
  assert.match(boxes[0], /aria-label="Evidence, ChatGPT, 1:00 to 2:00"/);
  assert.match(boxes[0], /aria-label="Evidence, Brand not identified, 1:40 to 2:30, overlaps other evidence"/);
  assert.doesNotMatch(boxes[0], /AG1/, 'the AG1 ad is its own ad');
  assert.doesNotMatch(text(html), READ_COUNT);
});

test('d1a Episode: two ads of one overlap group sit under "Ad break · range · 2 ads"; a single ad never does', async () => {
  const { html } = await episodePage(epReads());
  const groups = adBreakBoxes(html);
  assert.equal(groups.length, 1);
  assert.match(groups[0], /<h3 class="ew-adbreak-h">Ad break <span aria-hidden="true">·<\/span> <span class="ew-time"[^>]*>1:00–3:20<\/span> <span aria-hidden="true">·<\/span> 2 ads<\/h3>/);
  assert.match(groups[0], /ChatGPT/);
  assert.match(groups[0], /AG1/);
  assert.doesNotMatch(groups[0], /Lowes/, 'the lone ad is outside the group');
  assert.equal((text(html).match(/Ad break/g) || []).length, 1);
  // group_ad_count 1 everywhere: no Ad break label at all.
  const solo = epReads();
  solo.ad_breaks = solo.ad_breaks.map(b => ({ ...b, group_id: b.id, group_ad_count: 1 }));
  const s = await episodePage(solo);
  assert.doesNotMatch(s.html, /Ad break|ew-adbreak/);
  // An older server with no group fields: no label either.
  const bare = epReads();
  bare.ad_breaks = bare.ad_breaks.map(({ group_id, group_ad_count, ...b }) => b);
  assert.doesNotMatch((await episodePage(bare)).html, /Ad break|ew-adbreak/);
});

test('d1a Episode: transcript line tags say "Ad · name" for every piece of evidence', async () => {
  const rec = epRecord([
    { segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 },
    { segment_id: 2, start_s: 140, end_s: 150, text: 'Go to AG1.', text_start: 0, text_end: 10 },
    { segment_id: 3, start_s: 900, end_s: 910, text: 'Lowes.', text_start: 0, text_end: 6 }]);
  const { html } = await episodePage(epReads(), rec);
  const tags = [...html.matchAll(/<span class="ew-line-tag">([^<]*)<\/span>/g)].map(m => m[1]);
  assert.ok(tags.length >= 3);
  for (const t of tags) assert.match(t, /^Ad · /);
});

test('d1a Episode: earlier-copy and no-copy blocks say ads; counts are ads per part', async () => {
  const ads = [epAd('c1', [60, 120], 'Lowes'),
    epAd('o1', [3400, 3480], 'Progressive', { transcript_version_id: OLD, on_current_transcript: false }),
    epAd('n1', [1000, 1060], 'Acme', { transcript_version_id: null, on_current_transcript: null })];
  const reads = epReads({ ads, earlier_transcripts: [{ transcript_version_id: OLD, duration_s: 3544, fetch_date: '2026-09-21', fetch_date_basis: 'fetch_date' }],
    ad_breaks: [ad('brk:c', [60, 120], ['c1'], 'brk:c', 1), ad('brk:o', [3400, 3480], ['o1'], 'brk:o', 1, { transcript_version_id: OLD, on_current_transcript: false }),
      ad('brk:n', [1000, 1060], ['n1'], 'brk:n', 1, { transcript_version_id: null })],
    ad_break_counts: { current: 1, earlier: 1, no_copy: 1 } });
  const { html } = await episodePage(reads);
  assert.equal(adsCount(html), '1 ad; no transcript copy: 1 ad; earlier copy: 1 ad');
  assert.match(html, /<div class="ew-earlier"><h3>Ads found in an earlier copy of the transcript<\/h3>/);
  assert.match(text(html), /These ads were found in an earlier copy of the transcript \(fetched Sep 21, 2026\)\. Their times refer to that copy, not to the transcript below\./);
  assert.match(text(html), /Ads found with no recorded transcript copy; their times are not placed on this transcript\./);
  assert.match(text(html), /This evidence has no transcript copy and no record page to open\.|Open evidence/);
  assert.match(text(html), /No audio clips for this episode’s ads\. Times and passages are still listed\./);
});

test('d1a Episode from an older server (no ad count): no number', async () => {
  const reads = epReads(); delete reads.ad_breaks; delete reads.ad_break_counts;
  const { html } = await episodePage(reads);
  assert.doesNotMatch(adsCount(html), /\d/);
  assert.doesNotMatch(text(html), READ_COUNT);
});

test('d1a Episode: stage words say ad screening and speaker labels', async () => {
  for (const [stage, word] of [['detect', 'ad screening'], ['diarize', 'speaker labels']]) {
    const reads = epReads({ ads: [], ad_breaks: [], ad_break_counts: { current: 0, earlier: 0, no_copy: 0 },
      screening: { ...SCREENED, jev_screened: false, stopped_partway: true, stage } });
    const { html } = await episodePage(reads);
    assert.match(text(html), new RegExp(`Processing stopped partway, at ${word}\\.`));
  }
});

/* ======================================================================
   3. Evidence page: one record, named as evidence for its ad
   ====================================================================== */
const EVREF = 'sr1:' + 'v'.repeat(32);
const evRecord = (adBreak, over = {}) => ({ ref: EVREF, canonical_source_ref: EVREF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'ag1', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 100, end_s: 150 },
  episode: { episode_id: 'ep_d1', show_title: 'Show A', ep_title: 'An episode', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 55.4, transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Show A', unresolved: 0 },
  evidence: { id: 'a2', episode_id: 'ep_d1', orig_start_s: 100, orig_end_s: 150, transcript_version_id: CUR }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', brand: { key: 'ag1', display: 'AG1', confirmed: false }, needs_review: false, self_promo: false,
    display_span: [100, 150], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [] } },
  window: { segments: [{ segment_id: 9, start_s: 100, end_s: 150, text: 'Go to AG1.', text_start: 0, text_end: 10 }], text_complete: true, next_cursor: null },
  ad_break: adBreak, ...over });
const THREE = [epAd('a1', [60, 120], 'AG1'), epAd('a2', [100, 150], 'AG1'), epAd('a3', [140, 200], null)];
const evidencePage = (rec, reads = { ads: THREE }) => render('#/evidence/' + encodeURIComponent(EVREF), p => {
  if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_d1') return { episode: reads };
  if (p === '/customer/brands') return { brands: [] };
  return {};
});
const ad3 = (index = 1) => ({ id: 'brk:3', span: [60, 200], read_count: 3, index, refs: ['a1', 'a2', 'a3'], brands: [], on_current_transcript: true, group_id: 'brk:3', group_ad_count: 1 });

test('d1a Evidence: "One of 3 pieces of evidence for this ad"; "Other evidence for this ad" lists them', async () => {
  const { html } = await evidencePage(evRecord(ad3(1)));
  assert.match(html, /<span class="eyebrow">Ad · one of 3 pieces of evidence for this ad<\/span>/);
  const pod = (html.match(/<section class="ew-card ew-pod ew-pod-card"[\s\S]*?<\/section>/) || [''])[0];
  assert.match(text(pod), /^Other evidence for this ad · 1:00–3:20/);
  assert.match(text(pod), /One of 3 pieces of evidence for this ad\. Press one to open it\./);
  assert.equal((pod.match(/data-spot-row=/g) || []).length, 3);
  assert.match(pod, /<li data-spot-row="1" data-self/);
  assert.match(pod, /aria-label="Ad, 3 pieces of evidence"/);
  // Per-record facts stay: one record is one piece of evidence.
  assert.match(html, /Each fact and its source/);
  assert.match(text(html), /How the time was found\s*Found automatically/);
  // Outside Technical details no read noun (the "read out" verb rows stay).
  assert.doesNotMatch(text(html.replace(/<details class="tech-details">[\s\S]*?<\/details>/g, '')).replace(/(Web address|Promo code) read out/g, ''), /\b(reads?|ad breaks?)\b/i);
});

test('d1a Evidence: a single-evidence ad shows no "other evidence" section', async () => {
  const one = await evidencePage(evRecord({ id: 'brk:1', span: [100, 150], read_count: 1, index: 0, refs: ['a2'], brands: [], on_current_transcript: true, group_id: 'brk:1', group_ad_count: 1 }));
  assert.doesNotMatch(one.html, /ew-pod-card|evidence for this ad/);
  assert.match(one.html, /<span class="eyebrow">Ad<\/span>/);
});

test('d1a Evidence: player kind, earlier copy, historical record and times basis say ad / evidence', async () => {
  const checked = await evidencePage(evRecord(null, { status: 'confirmed', review: { judgment: 'ad', bounds_verified: 1 },
    read: { ...evRecord(null).read, display_basis: 'checked' } }));
  assert.match(checked.html, /<span class="eyebrow ew-player-kind">Ad<\/span>/);
  assert.doesNotMatch(checked.html, /<dt>Times shown<\/dt>|detected window/, 'PLAIN-ADS-FIX2: one time range on evidence');
  const earlier = await evidencePage(evRecord(null, { episode: { ...evRecord(null).episode, transcript_is_current: false, transcript_duration_s: 3545, current_transcript_duration_s: 3324 } }));
  assert.match(text(earlier.html), /This evidence was found in an earlier copy of the transcript \(59:05\)\. The current copy is 55:24\. Times on this page refer to the earlier copy\./);
  const hist = await evidencePage(evRecord(null, { read: { historical: true, current_ref: 'sr1:' + 'c'.repeat(32) } }));
  assert.match(text(hist.html), /About this evidence/);
  assert.match(text(hist.html), /This page shows an earlier version of this evidence\./);
  assert.match(text(hist.html), /Earlier version of this evidence/);
});

test('d1a Evidence: copy-link and report toasts name the evidence, never a read', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  const copy = src.slice(src.indexOf('async function copyReadLink'), src.indexOf('// Jump to where the brand is first heard'));
  assert.doesNotMatch(copy, /'read'/);
  assert.match(copy, /'evidence'/);
  const report = src.slice(src.indexOf('async function sendReport'), src.indexOf('function markDirty'));
  assert.match(report, /We updated your earlier report on this ad\./);
  assert.doesNotMatch(report, /on this read/);
});

/* ======================================================================
   4. Public pages
   ====================================================================== */
test('d1a public pages: the home and coverage pages say ads, never reads or pods', () => {
  const home = fs.readFileSync(path.join(APP, 'public', 'index.html'), 'utf8');
  // LAND-1 (direction B2): real shows from the collection, named by their art.
  assert.match(home, /<meta name="description" content="EarWitness finds podcast ads and mentions in the shows it covers/);
  assert.doesNotMatch(home, /Illustrative passage &middot; discount-code ad/);
  assert.match(home, /<h2 id="shows-heading">Some of the shows we cover<\/h2>/);
  const cov = fs.readFileSync(path.join(APP, 'public', 'coverage.html'), 'utf8');
  assert.match(cov, /Search, alerts, ad lists and campaign checks all use it\./);
  assert.doesNotMatch(home + cov, /ad reads|ad pods|discount-code read|reads we found/);
});
