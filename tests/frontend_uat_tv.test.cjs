/* UAT fix round, transcript versions and pods (tv): each read on its own
   transcript (B-1), honest pods (B-2 / VS F-2), windows past the stored
   length (VS F-7), name flags (B-3), copy fixes (B-6, B-7, V-30, F-14,
   V-32, V-37, V-38, VL-11, F-1 sites). Loader copied from
   frontend_dv3_ux_evidence.test.cjs. Run: node --test tests/frontend_uat_tv.test.cjs */
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
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route },
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
const sessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(route = '#/overview') {
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
async function page(route, api) {
  const win = loadViews(route);
  await flushBoot();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, opts = {}) => { calls.push(p); const out = api(p, opts); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML, calls };
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const lineTags = html => [...html.matchAll(/<span class="ew-line-tag">([^<]*)<\/span>/g)].map(m => m[1]);
const stripRects = html => (html.match(/<svg class="ew-strip-bar"[\s\S]*?<\/svg>/g) || []).map(s => (s.match(/<rect /g) || []).length);

/* D-1 (decisions round): the server groups an episode's ad reads into ad
   breaks (same transcript copy; spans chain while start < running max
   end; touching spans are two breaks; a read with no copy stands alone).
   Fixtures carry what the server sends. */
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
function serverBreaks(ads) {
  const out = [], byTv = new Map();
  for (const a of ads || []) {
    if (a.transcript_version_id === null || !a.span) { out.push([a]); continue; }
    const tv = String(a.transcript_version_id ?? 'cur');
    if (!byTv.has(tv)) byTv.set(tv, []);
    byTv.get(tv).push(a);
  }
  for (const list of byTv.values()) {
    let cur = null, max = null;
    for (const a of list.slice().sort((x, y) => x.span[0] - y.span[0] || x.span[1] - y.span[1])) {
      if (cur && a.span[0] < max) { cur.push(a); max = Math.max(max, a.span[1]); } else { cur = [a]; max = a.span[1]; out.push(cur); }
    }
  }
  const pieces = out.flatMap((chain, i) => splitByBrand(chain, brandKeys).map((g, j, all) => ({ g, id: 'brk:' + i + (j ? '.' + j : ''), group: 'brk:' + i, n: all.length })));
  return pieces.map(({ g, id, group, n }) => ({ id, transcript_version_id: g[0].transcript_version_id === undefined ? 'cur' : g[0].transcript_version_id, on_current_transcript: g[0].on_current_transcript !== false,
    span: g[0].span ? [g[0].span[0], Math.max(...g.map(a => a.span[1]))] : null, refs: g.map(a => a.evidence_id), read_count: g.length, brands: [],
    group_id: group, group_ad_count: n }));
}
function withBreaks(ep) {
  if (!ep || typeof ep !== 'object' || ep instanceof Error || ep.ad_breaks) return ep;
  const breaks = serverBreaks(ep.ads);
  const n = f => breaks.filter(f).length;
  return { ...ep, ad_breaks: breaks, ad_break_counts: { current: n(b => b.on_current_transcript && b.transcript_version_id !== null),
    earlier: n(b => !b.on_current_transcript), no_copy: n(b => b.transcript_version_id === null) } };
}
function adBreakOf(ep, id) {
  const b = ((withBreaks(ep) || {}).ad_breaks || []).find(x => x.refs.includes(id));
  return b ? { ...b, index: b.refs.indexOf(id) } : null;
}

/* -- fixtures: s002e0's shape. Current transcript 36 (55:24); reads found
   on the earlier transcript 11 (59:05) run past the current end. -- */
const CUR = 36, OLD = 11;
const brandOf = display => (display ? { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }
  : { key: null, display: null, display_basis: 'none', confirmed: false });
const epAd = (id, span, display, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: brandOf(display), needs_review: false, self_promo: false, kind: 'rule', source_label: 'Rule candidate',
  transcript_start_s: span[0], media: null, transcript_version_id: CUR, on_current_transcript: true, ...over });
const earlier = (id, span, display, over = {}) => epAd(id, span, display, { transcript_version_id: OLD, on_current_transcript: false, ...over });
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
const epReads = (ads, over = {}) => withBreaks({ episode_id: 'ep_tv', ads, self_promo: [], screening: SCREENED, screened_no_ads: false,
  current_transcript_version_id: CUR,
  earlier_transcripts: ads.some(a => a.on_current_transcript === false)
    ? [{ transcript_version_id: OLD, duration_s: 3544.88, fetch_date: '2026-09-21', fetch_date_basis: 'fetch_date' }] : [],
  ...over });
const epRecord = (over = {}) => ({ ref: 'ep:ep_tv', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
  record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
  episode: { episode_id: 'ep_tv', transcript_version_id: CUR, ep_title: 'Michelle Parker', show_title: 'True Crime', pub_date: '2026-09-20',
    duration_min: 55.4, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-tc', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3324 }, selected_bounds: { start_s: 0, end_s: 3324 }, media: {},
  window: { segments: [
    { segment_id: 1, start_s: 100, end_s: 110, text: 'Lowes has you covered.', text_start: 0, text_end: 22 },
    { segment_id: 2, start_s: 3300, end_s: 3324, text: 'Thanks for listening.', text_start: 0, text_end: 21 }], text_complete: true, next_cursor: null },
  ...over });
const episodePage = (reads, rec = epRecord()) => page('#/episode/ep_tv', p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_tv')) return { record: rec };
  if (p === '/customer/episodes/ep_tv') return { episode: reads };
  return {};
});

/* -- B-1 Episode page -- */
test('B-1: reads on an earlier transcript stay out of pods, the strip, the tags and the count', async () => {
  // o1 and o2 overlap on the earlier copy but name two brands: two earlier-copy ads, never boxed with the current copy.
  const ads = [epAd('c1', [100, 160], 'Lowes'), earlier('o1', [3400, 3480], 'Progressive'), earlier('o2', [3470, 3546], 'Chumba')];
  const { html } = await episodePage(epReads(ads));
  assert.match(html, /<span class="ew-ads-count">1 ad; earlier copy: 2 ads<\/span>/);
  assert.doesNotMatch(html, /ew-pod-box/, 'no pod holds evidence of two copies or two brands');
  assert.deepEqual(stripRects(html), [1], 'only the current read is on the strip');
  assert.deepEqual(lineTags(html), ['Ad · Lowes']);
  assert.equal((html.match(/Jump to transcript/g) || []).length, 1, 'earlier reads have no Jump link');
  const block = (html.match(/<div class="ew-earlier">[\s\S]*?<!--\/ew-earlier--><\/div>/) || [''])[0];
  assert.match(block, /<h3>Ads found in an earlier copy of the transcript<\/h3>/);
  assert.match(text(block), /These ads were found in an earlier copy of the transcript \(fetched Sep 21, 2026\)\. Their times refer to that copy, not to the transcript below\./);
  assert.match(block, /Progressive/);
  assert.match(block, /Chumba/);
  assert.doesNotMatch(block, /ew-pod-n|data-spot-row|Jump to transcript/);
  assert.ok(html.indexOf('ew-earlier') > html.indexOf('Lowes'), 'the block follows the pods and rows');
});

test('B-1: when every read is in an earlier copy the page never says "no ads"', async () => {
  const ads = [earlier('o1', [3400, 3480], 'Progressive'), earlier('o2', [3480, 3546], 'Chumba')];
  const { html } = await episodePage(epReads(ads));
  assert.match(html, /<span class="ew-ads-count">earlier copy: 2 ads<\/span>/);
  assert.doesNotMatch(html, /No ads are listed|no ads found|not been screened/i);
  assert.match(html, /Ads found in an earlier copy of the transcript/);
  assert.deepEqual(lineTags(html), []);
  assert.deepEqual(stripRects(html), []);
});

test('B-1: an older copy fetched on an unknown day says no date; a transcription day says transcribed', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes'), earlier('o1', [3400, 3480], 'Progressive')];
  const one = await episodePage(epReads(ads));
  assert.match(one.html, /<span class="ew-ads-count">1 ad; earlier copy: 1 ad<\/span>/, 'singular');
  const none = await episodePage(epReads(ads, { earlier_transcripts: [{ transcript_version_id: OLD, duration_s: 3544.88, fetch_date: null, fetch_date_basis: null }] }));
  assert.match(text(none.html), /These ads were found in an earlier copy of the transcript\. Their times refer to that copy/);
  const tr = await episodePage(epReads(ads, { earlier_transcripts: [{ transcript_version_id: OLD, duration_s: 3544.88, fetch_date: '2026-09-21', fetch_date_basis: 'transcribed_at' }] }));
  assert.match(text(tr.html), /earlier copy of the transcript \(transcribed Sep 21, 2026\)\./);
});

/* -- B-1 Evidence page -- */
const REF = 'sr1:'.padEnd(36, 'a');
const evRecord = (over = {}, epOver = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'progressive', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 3400, end_s: 3480 },
  episode: { episode_id: 'ep_tv', show_title: 'True Crime', ep_title: 'Michelle Parker', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 55.4, transcript_version_id: OLD, transcript_duration_s: 3544.88, transcript_is_current: false, current_transcript_duration_s: 3324, ...epOver },
  show: { show_identity_id: 'cs-tc', display_title: 'True Crime', unresolved: 0 },
  evidence: { id: 'o1', episode_id: 'ep_tv', orig_start_s: 3400, orig_end_s: 3480, transcript_version_id: OLD }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'rule', source_label: 'Rule candidate', brand: brandOf('Progressive'), needs_review: false, self_promo: false, confidence: 'high',
    display_span: [3400, 3480], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Progressive' } },
  window: { segments: [{ segment_id: 9, start_s: 3400, end_s: 3480, text: 'Progressive saves you money.', text_start: 0, text_end: 28 }], text_complete: true, next_cursor: null },
  ...over });
const evidencePage = (rec, reads) => page('#/evidence/' + encodeURIComponent(REF), p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: 'ad_break' in rec ? rec : { ...rec, ad_break: adBreakOf(reads, rec.evidence.id) } };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_tv') return { episode: reads };
  return {};
});

test('B-1: an earlier-copy read gets its pod from its own transcript and its own length', async () => {
  // D-1: o2 overlaps o1 (touching spans would be two breaks).
  const reads = epReads([earlier('o1', [3400, 3480], 'Progressive'), earlier('o2', [3470, 3546], 'Progressive'), epAd('c1', [3479, 3500], 'Lowes')]);
  const { html } = await evidencePage(evRecord(), reads);
  const pod = (html.match(/<section class="ew-card ew-pod ew-pod-card"[\s\S]*?<\/section>/) || [''])[0];
  assert.equal((pod.match(/data-spot-row=/g) || []).length, 2, 'the current-copy read is not in the pod');
  assert.doesNotMatch(pod, /Lowes/);
  assert.match(html, /Where it sat in the 59:05 episode/, 'the strip uses the pinned transcript length');
  assert.match(html, /<span>59:05<\/span><\/div>/);
  assert.doesNotMatch(html, /55:24 episode/);
  assert.match(text(html), /This evidence was found in an earlier copy of the transcript \(59:05\)\. The current copy is 55:24\. Times on this page refer to the earlier copy\./);
  assert.doesNotMatch(html, /Open in the full transcript/, 'the current transcript holds other words at these times');
});

test('B-1: a read on the current transcript has no earlier-copy line and keeps its jump', async () => {
  const rec = evRecord({}, { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  const { html } = await evidencePage(rec, epReads([]));
  assert.doesNotMatch(html, /earlier copy/);
  assert.match(html, /Open in the full transcript/);
});

test('B-1 step 6: a transcript that runs past the rounded stored length uses its own end (22:49, not 22:48)', async () => {
  const rec = evRecord({ selected_bounds: { start_s: 1320, end_s: 1368.8 } }, { duration_min: 22.8, transcript_version_id: CUR, transcript_duration_s: 1368.84, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [1320, 1368.8];
  const { html } = await evidencePage(rec, epReads([]));
  assert.match(html, /Where it sat in the 22:49 episode/);
  assert.doesNotMatch(html, /stored episode length/, 'rounding is not a changed recording');
});

/* -- VS F-7: windows past the stored length -- */
test('F-7: a window past the stored length lengthens the strip and says so, never clamped', async () => {
  const rec = evRecord({}, { transcript_version_id: CUR, transcript_duration_s: undefined, transcript_is_current: undefined, current_transcript_duration_s: undefined });
  const { html } = await evidencePage(rec, epReads([]));
  assert.match(html, /Where it sat in the 58:00 episode/, 'the strip runs to the read’s end (3480 s), not the stored 55:24');
  assert.match(html, /<span>58:00<\/span><\/div>/);
  assert.match(text(html), /The stored episode length \(55:24\) is shorter than the transcript; the audio may have changed since transcription\./);
  const rect = (html.match(/<rect class="is-this" x="([\d.]+)" y="0" width="([\d.]+)"/) || []).slice(1).map(Number);
  assert.ok(rect[0] + rect[1] <= 1000.1, 'the mark fits inside the strip');
});

test('F-7: the Episode strip covers a current read past the stored length and says so', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes'), epAd('c2', [3320, 3400], 'Late')];
  const { html } = await episodePage(epReads(ads), epRecord({ episode: { ...epRecord().episode, transcript_duration_s: undefined } }));
  assert.match(html, /Where each ad sat in the 56:40 episode/);
  assert.deepEqual(stripRects(html), [2]);
  assert.match(text(html), /The stored episode length \(55:24\) is shorter than the transcript/);
});

/* -- B-2 / VS F-2: honest pods -- */
const nested = () => [epAd('d1', [2758.36, 2914.2], 'Progressive'), epAd('j1', [2760, 2820], null, { kind: 'jev', source_label: 'Jev window' })];
test('B-2: nested windows: range to the largest end, overlap wording, no order claim (s009e0)', async () => {
  const { html } = await episodePage(epReads(nested()), epRecord({ episode: { ...epRecord().episode, duration_min: 85.8, transcript_duration_s: 5148 } }));
  const box = (html.match(/<div class="ew-pod ew-pod-box">[\s\S]*?<\/ol><\/div>/) || [''])[0];
  assert.match(text(box), /45:58–48:34/);
  assert.doesNotMatch(text(box), /45:58–47:00/);
  assert.match(text(box), /Evidence \(2\), some overlapping · press one to open it/);
  assert.doesNotMatch(html, /back to back/);
  assert.match(box, /class="ew-pod-spot[^"]* is-overlap[^"]*"[^>]*aria-label="Evidence, Brand not identified, 46:00 to 47:00, overlaps other evidence"/);
  assert.match(box, /overlaps other evidence/);
  assert.doesNotMatch(box, /\b(spot|read|Read|Spot) \d|\d+ of \d+ (reads )?in|overlaps (read|spot) \d/);
});

test('B-2: overlapping reads on Evidence: one of n pieces of evidence for this ad, no ordinal', async () => {
  // Row 1: the decided wording is "one of n pieces of evidence for this ad"; overlapping evidence has no known order.
  const rec = evRecord({ evidence: { id: 'j1', episode_id: 'ep_tv', orig_start_s: 2760, orig_end_s: 2820 } }, { transcript_version_id: CUR, transcript_duration_s: 5148, transcript_is_current: true, current_transcript_duration_s: undefined, duration_min: 85.8 });
  rec.read.display_span = [2760, 2820];
  const { html } = await evidencePage(rec, epReads(nested()));
  assert.match(html, /<span class="eyebrow">Ad · one of 2 pieces of evidence for this ad<\/span>/);
  assert.match(text(html), /Other evidence for this ad · 45:58–48:34/);
  assert.match(text(html), /One of 2 pieces of evidence for this ad\./);
  assert.doesNotMatch(text(html), /\b(spot|read|Read|Spot) \d|\d+ of \d+ (reads )?in|overlaps (read|spot) \d/);
  assert.doesNotMatch(html, /spot \d of \d/);
});

test('B-2: overlapping reads tag transcript lines "Ad · name" with no spot number', async () => {
  const rec = epRecord({ episode: { ...epRecord().episode, duration_min: 85.8, transcript_duration_s: 5148 },
    window: { segments: [{ segment_id: 1, start_s: 2758, end_s: 2770, text: 'Progressive.', text_start: 0, text_end: 12 }], text_complete: true, next_cursor: null } });
  const { html } = await episodePage(epReads(nested()), rec);
  assert.deepEqual(lineTags(html), ['Ad · Progressive']);
});

test('B-2 / D-1: reads that only touch are two ads: no box, no "back to back", no ordinal', async () => {
  // D-1 replaces the old back-to-back pod: touching spans are two ads.
  const ads = [epAd('a1', [60, 120], 'Squarespace'), epAd('a2', [121, 150], 'AG1')];
  const ep = await episodePage(epReads(ads));
  assert.doesNotMatch(ep.html, /ew-pod-box|back to back|Ad pod/);
  assert.match(ep.html, /<span class="ew-ads-count">2 ads<\/span>/);
  assert.doesNotMatch(ep.html, /is-overlap|overlaps (spot|read)/);
  const rec = evRecord({ evidence: { id: 'a2', episode_id: 'ep_tv', orig_start_s: 121, orig_end_s: 150 } }, { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [121, 150];
  const ev = await evidencePage(rec, epReads(ads));
  assert.match(ev.html, /<span class="eyebrow">Ad<\/span>/);
  assert.doesNotMatch(ev.html, /ew-pod-card|spot \d of \d|in this ad break|pieces? of evidence for this ad/);
});

test('B-2: podBar marks a spot that starts inside an earlier one', () => {
  const win = loadViews();
  const bar = win.EWUI.podBar({ spots: [{ span: [0, 100], label: 'A' }, { span: [10, 50], label: 'B' }, { span: [100, 130], label: 'C' }] });
  assert.match(bar, /class="ew-pod-spot[^"]*is-overlap[^"]*"[^>]*data-spot="1"[^>]*aria-label="Evidence, B, 0:10 to 0:50, overlaps other evidence"/);
  assert.doesNotMatch(bar.split('data-spot="2"')[1].split('</button>')[0] + bar.match(/<button[^>]*data-spot="2"[^>]*>/)[0], /overlap/);
  assert.doesNotMatch(bar.match(/<button[^>]*data-spot="0"[^>]*>/)[0], /overlap/);
});

/* -- B-3: name flags on episode rows and Evidence pod rows -- */
// PLAIN-ADS (DEP7): no name flag on episode rows or pod rows.
test('B-3: no name flag on episode rows and pod rows, whatever the review', async () => {
  const ads = [epAd('u1', [60, 120], 'Progressive'), epAd('k1', [110, 150], 'Progressive', { brand: { ...brandOf('Progressive'), confirmed: true } }),
    epAd('r1', [400, 450], null, { brand: { key: null, display: null, display_basis: 'none', confirmed: false, under_review: true }, needs_review: true })];
  const { html } = await episodePage(epReads(ads));
  const row = id => (html.match(new RegExp(`<li class="ew-ad-row[^"]*"[^>]*data-ref="sr1:${id}[^"]*">[\\s\\S]*?</li>`)) || [''])[0];
  assert.match(row('u1'), /<b class="ew-ad-brand">Progressive<\/b>/);
  assert.doesNotMatch(row('u1'), /Name not confirmed|Needs review/);
  assert.doesNotMatch(row('k1'), /Name not confirmed|Needs review/);
  assert.doesNotMatch(row('r1'), /Name not confirmed|Needs review/);
  const rec = evRecord({ evidence: { id: 'k1', episode_id: 'ep_tv', orig_start_s: 110, orig_end_s: 150 } }, { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [110, 150];
  const ev = await evidencePage(rec, epReads(ads.slice(0, 2)));
  const podRow = n => (ev.html.match(new RegExp(`<li data-spot-row="${n}"[\\s\\S]*?</li>`)) || [''])[0];
  assert.match(podRow(0), /<b>Progressive<\/b>/);
  assert.doesNotMatch(podRow(0), /Name not confirmed/);
  assert.doesNotMatch(podRow(1), /Name not confirmed/);
});

/* -- small copy fixes -- */
// PLAIN-ADS-FIX2 (screen R2-03) replaces B-6: an evidence page shows one
// time range, so it names no window and has no "Times shown" row.
test('B-6: the evidence page names no window and has no "Times shown" row', async () => {
  const rec = evRecord({ selected_bounds: { start_s: 46, end_s: 120 } }, { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [46, 120];
  rec.window.segments = [{ segment_id: 9, start_s: 46, end_s: 120, text: 'Progressive saves you money.', text_start: 0, text_end: 28 }];
  const { html } = await evidencePage(rec, epReads([]));
  assert.doesNotMatch(html, /Selected 0:46|detected window/);
  assert.doesNotMatch(html, /<dt>Times shown<\/dt>/);
  assert.match(html, /Complete text\./);
  assert.doesNotMatch(html, /the whole source|Time range from/);
});

test('B-7: the code chip says "Code heard"', () => {
  const win = loadViews();
  assert.match(win.EWUI.tag('code', null, { value: 'news' }), /data-tag="code">Code heard <b>news<\/b>/);
});

// CLIP-PLAYER-FIX (2a, lead spec 2026-10-24) supersedes V-30's ends: the
// big clock keeps episode time; the seek bar shows elapsed and the clip's
// length, and its label keeps the episode times.
test('V-30 (superseded by 2a): the big clock and the seek bar label keep episode time; the bar ends are elapsed and length', async () => {
  const rec = evRecord({ selected_bounds: { start_s: 154, end_s: 159 }, media: { playback: 'local', source_offset_s: 0, alignment: 'verified', timing: 'verified' } },
    { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [154, 159];
  const { html } = await evidencePage(rec, epReads([]));
  assert.match(html, /<div class="ew-player-progress"><span class="ew-time" data-clip-elapsed>0:00<\/span><input type="range" min="0" max="5" step="1" value="0" data-clip-seek="ev" aria-valuemin="0" aria-valuemax="5" aria-valuenow="0" aria-valuetext="0:00 of 0:05" aria-label="Position in clip, 2:34 to 2:39"><span class="ew-time">0:05<\/span><\/div>/);
  assert.match(html, /data-clip-now>2:34</, 'the big clock left episode time');
});

test('F-14 / V-32: no raw alignment enum and no raw recording-proof reason', async () => {
  const remote = await evidencePage(evRecord({ media: { playback: 'remote', alignment: 'unknown' } }, { transcript_is_current: true }), epReads([]));
  assert.doesNotMatch(remote.html, /Alignment: /);
  // CLIP design 6.2: the no-clip player names no reason and no timing note.
  assert.match(text(remote.html), /No clip for this ad\./);
  assert.doesNotMatch(remote.html, /Timing not checked/);
  const none = await evidencePage(evRecord({ media: { playback: 'unavailable', reason: 'no authorized recording proof', alignment: 'unknown' } }, { transcript_is_current: true }), epReads([]));
  assert.doesNotMatch(none.html, /authorized recording proof|Alignment: /i);
  assert.match(text(none.html), /No clip for this ad\. The words are below\./);
  assert.doesNotMatch(none.html, /No recording of this episode|Timing not checked/);
  const verified = await evidencePage(evRecord({ media: { playback: 'local', source_offset_s: 0, alignment: 'verified', timing: 'verified' } }, { transcript_is_current: true }), epReads([]));
  assert.doesNotMatch(verified.html, /Alignment|Timing not checked/);
});

test('V-37: the jump placeholder fits the episode and the real end says End of transcript', async () => {
  const rec = epRecord({ episode: { ...epRecord().episode, duration_min: 18.5, transcript_duration_s: 1109 },
    window: { segments: [{ segment_id: 1, start_s: 1100, end_s: 1109, text: 'Bye.', text_start: 0, text_end: 4 }], text_complete: true, next_cursor: null, truncation_reason: 'x' },
    effective_bounds: { start_s: 0, end_s: 1109 }, selected_bounds: { start_s: 0, end_s: 1109 } });
  const { html } = await episodePage(epReads([]), rec);
  assert.match(html, /End of transcript \(18:29\)\. Selected 0:00–18:29 \(the whole transcript\)\./);
  assert.doesNotMatch(html, /Complete interval text/);
  const bound = epRecord({ episode: { ...epRecord().episode, duration_min: 18.5, transcript_duration_s: 1109 },
    window: { segments: [{ segment_id: 1, start_s: 0, end_s: 9, text: 'Hi.', text_start: 0, text_end: 3 }], text_complete: false, next_cursor: 'c1', truncation_reason: 'limit' } });
  const b = await episodePage(epReads([]), bound);
  assert.match(b.html, /<input id="tr-jump" name="at" inputmode="numeric" placeholder="9:00"/);
  assert.doesNotMatch(b.html, /1:05:00/);
  const ev = await evidencePage(evRecord({}, { transcript_is_current: true }), epReads([]));
  assert.match(ev.html, /Complete text\./, 'a read’s window is not the end of the transcript');
  assert.doesNotMatch(ev.html, /End of transcript/);
});

test('V-38: no leading dash on the no-clip note; remote audio uses a comma', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes')];
  const { html } = await episodePage(epReads(ads));
  assert.match(html, /<p class="ew-ads-note">No audio clips for this episode’s ads\./);
  assert.doesNotMatch(html, /— No audio clips/);
  // d16 decision 7: the remote detail is a technical detail, defined once in ui.js (audioDetail).
  for (const file of ['ui.js', 'views.js']) {
    const src = fs.readFileSync(path.join(APP, file), 'utf8');
    if (file === 'ui.js') assert.match(src, /'Remote audio only, no local playback'/, file);
    assert.doesNotMatch(src, /Remote audio only — no local playback/, file);
  }
});

test('VL-11: no coverage tier badge on an episode that was never screened', async () => {
  const unscreened = { label: 'Not screened', jev_screened: false, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
  const { html } = await episodePage(epReads([], { screening: unscreened }));
  const status = (html.match(/<p class="ew-episode-status">[\s\S]*?<\/p>/) || [''])[0];
  assert.doesNotMatch(status, /data-tag="tier"|Full coverage/);
  const screened = await episodePage(epReads([epAd('c1', [100, 160], 'Lowes')]));
  assert.match(screened.html, /<p class="ew-episode-status"><span class="ew-tag ew-tier ew-tier-full" data-tag="tier">/);
});

test('VL-11: the show page shows no tier badge when no episode was screened', async () => {
  const ep = (id, screening) => ({ id, ep_title: id, pub_date: '2026-09-20', transcript: true, screening });
  const unscreened = { label: 'Not screened', jev_screened: false, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false };
  const show = (eps) => page('#/show/cs-tc', p => {
    if (p.startsWith('/customer/shows/cs-tc')) return { id: 'cs-tc', display_title: 'Call Her Daddy', episodes: eps, coverage: { episodes: eps.length, episodes_with_transcript: eps.length }, sponsors: [] };
    return {};
  });
  const none = await show([ep('s023e0', unscreened), ep('s023e1', unscreened)]);
  assert.doesNotMatch(none.html, /data-tag="tier"/);
  const some = await show([ep('s1', SCREENED), ep('s2', unscreened)]);
  assert.match(some.html, /data-tag="tier"[^>]*>[\s\S]*Full coverage/);
});

test('F-1: a read with no shown brand says "Brand not identified" on Episode, Evidence and the pod bar', async () => {
  const ads = [epAd('n1', [60, 120], null), epAd('n2', [100, 150], 'Quince')];
  const ep = await episodePage(epReads(ads), epRecord({ window: { segments: [{ segment_id: 1, start_s: 60, end_s: 70, text: 'Hi.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null } }));
  assert.match(ep.html, /<b class="ew-ad-brand">Brand not identified<\/b>/);
  assert.match(ep.html, /aria-label="Evidence, Brand not identified,/);
  assert.deepEqual(lineTags(ep.html), ['Ad · brand not identified']);
  const rec = evRecord({ evidence: { id: 'n2', episode_id: 'ep_tv', orig_start_s: 100, orig_end_s: 150 }, attribution: [] }, { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [100, 150];
  rec.read.brand = brandOf(null);
  const ev = await evidencePage(rec, epReads(ads));
  assert.match(ev.html, /<h1 class="ew-reader-h1">Brand not identified<\/h1>/);
  assert.match(ev.html, /<p class="ew-reader-caveat">We found this ad from its ad words, but no brand name was recorded, so it doesn’t count toward any brand\. Know the brand\? Use <button type="button" class="text-link" data-action="goto-report">Report a mistake<\/button>\.<\/p>/, 'SEARCH-CLARITY: an unnamed ad says why');
  assert.match(ev.html, /<li data-spot-row="0"[\s\S]*?<b>Brand not identified<\/b>/);
  for (const html of [ep.html, ev.html]) assert.doesNotMatch(html, /Brand not named|brand not named|Unnamed spot/);
});

/* -- R3-01 / R3-02 (re-review #3): a read with no transcript version
   (on_current_transcript null) never joins an Evidence pod, and its
   Episode row says why it has nothing to open. -- */
const noTx = (id, span, display, over = {}) => epAd(id, span, display, { transcript_version_id: null, on_current_transcript: null, ...over });

test('R3-01: Evidence leaves a read with no transcript version out of its pod', async () => {
  // s001e0 shape: the 0:15 read lost its version; the 0:55 read is on the current copy and touches it.
  const ads = [noTx('u1', [15, 55], 'Acme'), epAd('c1', [55, 178], 'Progressive')];
  const rec = evRecord({ evidence: { id: 'c1', episode_id: 'ep_tv', orig_start_s: 55, orig_end_s: 178, transcript_version_id: CUR } },
    { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [55, 178];
  const { html } = await evidencePage(rec, epReads(ads));
  assert.doesNotMatch(text(html), /one of 2|in an ad break|spot \d of \d/, 'not pooled with a read that has no transcript copy');
  assert.doesNotMatch(html, /ew-pod-box|data-spot-row/);
  // Control: two reads on the current copy that overlap still form an ad break (D-1: touching ones would not).
  const both = await evidencePage(rec, epReads([epAd('u1', [15, 60], null), epAd('c1', [55, 178], 'Progressive')]));
  assert.match(text(both.html), /0:15–2:58/);
});

test('R3-01: Evidence for a read with no transcript version shows no pod', async () => {
  const ads = [noTx('u1', [15, 55], 'Acme'), epAd('c1', [55, 178], 'Progressive')];
  const rec = evRecord({ evidence: { id: 'u1', episode_id: 'ep_tv', orig_start_s: 15, orig_end_s: 55, transcript_version_id: null } },
    { transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true, current_transcript_duration_s: undefined });
  rec.read.display_span = [15, 55];
  const { html } = await evidencePage(rec, epReads(ads));
  assert.doesNotMatch(text(html), /one of 2|in an ad break|spot \d of \d/);
});

test('R3-02: a no-transcript-copy row with nothing to open says why', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes'), noTx('u1', [1000, 1060], null, { ref: null })];
  const { html } = await episodePage(epReads(ads));
  const block = (html.match(/<div class="ew-unplaced">[\s\S]*?<!--\/ew-unplaced--><\/div>/) || [''])[0];
  assert.match(text(block), /This evidence has no transcript copy and no record page to open\./);
  assert.doesNotMatch(block, /Details are not disclosed in this view/);
  // Elsewhere the withheld-row wording is unchanged.
  const other = await episodePage(epReads([epAd('c1', [100, 160], 'Lowes', { ref: null })]));
  assert.match(other.html, /Details are not disclosed in this view\./);
});

/* -- Gate 5 (UAT-046 re-run): an earlier copy can hold the same words
   (s000e0: both copies 22:49, only re-segmented), so no earlier-copy
   line claims the ads rotated. -- */
test('G5: earlier-copy lines never claim the ads rotated', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes'), earlier('o1', [3400, 3480], 'Progressive')];
  const ep = await episodePage(epReads(ads));
  assert.doesNotMatch(ep.html, /rotated/i);
  const ev = await evidencePage(evRecord(), epReads(ads));
  assert.doesNotMatch(ev.html, /ads have rotated/i);
  const same = await evidencePage(evRecord({}, { transcript_duration_s: 1368.84, current_transcript_duration_s: 1368.84 }), epReads(ads));
  assert.match(text(same.html), /This evidence was found in an earlier copy of the transcript \(22:49\)\. The current copy is 22:49\. Times on this page refer to the earlier copy\./);
  const noLen = await evidencePage(evRecord({}, { current_transcript_duration_s: undefined }), epReads(ads));
  assert.match(text(noLen.html), /This evidence was found in an earlier copy of the transcript \(59:05\)\. Times on this page refer to that copy\./);
});

/* JUDGE-SMALL G3-03 (QA-full F33): an episode page whose ad rows play
   clips said "No clip kept (transcript only) · … No recording or playback
   is implied." in the Transcript box and "Audio: No clip kept" under
   About, right next to a playing clip. With no clip of its own, when any
   ad row has a playable clip (the row's Play rule: a ref and local
   playback), both places say "Clips for N of M ads; the full episode audio
   is not kept." With no row clip both stay the member's no-clip state. */
const LOCAL = { playback: 'local', source_offset_s: 0 };
const episodeAudioLine = html => text((html.match(/<div class="ew-episode-audio">[\s\S]*?<\/p>/) || [''])[0]).trim();
const aboutAudioRow = html => ((html.match(/<dt>Audio<\/dt><dd>([^<]*)<\/dd>/) || [])[1]);
const g303Ads = clips => ['a1', 'a2', 'a3', 'a4'].map((id, i) => epAd(id, [100 + i * 200, 160 + i * 200], 'Brand' + i, i < clips ? { media: LOCAL } : {}));

test('G3-03: no ad row has a clip: the Transcript box and About keep the no-clip state', async () => {
  const none = await episodePage(epReads(g303Ads(0)));
  const NONE = none.win.EWUI.AUDIO_STATES.none;
  assert.ok(episodeAudioLine(none.html).startsWith(NONE), 'transcript box: ' + episodeAudioLine(none.html));
  assert.equal(aboutAudioRow(none.html), NONE);
  assert.doesNotMatch(none.html, /Clips for \d+ of \d+ ads/);
});

test('G3-03: some ad rows have clips: both places say "Clips for 2 of 4 ads; the full episode audio is not kept."', async () => {
  const some = await episodePage(epReads(g303Ads(2)));
  const LINE = 'Clips for 2 of 4 ads; the full episode audio is not kept.';
  assert.equal(episodeAudioLine(some.html), LINE);
  assert.equal(aboutAudioRow(some.html), LINE);
  assert.doesNotMatch(text(some.html), /No recording or playback is implied/);
  assert.equal((some.html.match(/data-action="clip-toggle"/g) || []).length, 2, 'fixture: two rows play');
});

test('G3-03: every ad row has a clip: "Clips for 4 of 4 ads"; a row without a ref never counts as a clip', async () => {
  const all = await episodePage(epReads(g303Ads(4)));
  assert.equal(episodeAudioLine(all.html), 'Clips for 4 of 4 ads; the full episode audio is not kept.');
  assert.equal(aboutAudioRow(all.html), 'Clips for 4 of 4 ads; the full episode audio is not kept.');
  // No ref: no Play button on the row, so no clip (the row's own rule).
  const noRef = g303Ads(2); noRef[1] = { ...noRef[1], ref: null };
  const one = await episodePage(epReads(noRef));
  assert.equal(aboutAudioRow(one.html), 'Clips for 1 of 4 ads; the full episode audio is not kept.');
  // A local episode recording keeps its own player and words.
  const ownAudio = await episodePage(epReads(g303Ads(2)), epRecord({ media: LOCAL }));
  assert.doesNotMatch(ownAudio.html, /Clips for \d+ of \d+ ads/);
});

/* JUDGE-SMALL-FIX1 (JS-R1-03, code r1): an episode whose only playable
   clips are the show's own promotions kept "No clip (transcript only) …"
   in the Transcript box and About beside a promotion's Play. It takes the
   same counted line, over its own promotions: "Clips for N of M own
   promotions; the full episode audio is not kept." When an ad row plays,
   the ad line stands unchanged. */
const g303Promos = clips => ['p1', 'p2'].map((id, i) => epAd(id, [1000 + i * 200, 1030 + i * 200], 'Own show',
  { self_promo: true, self_promo_reason: 'show_name', ...(i < clips ? { media: LOCAL } : {}) }));

test('JS-R1-03: only own-promotion rows have clips: both places say "Clips for 1 of 2 own promotions; …"', async () => {
  const promoOnly = await episodePage(epReads(g303Ads(0), { self_promo: g303Promos(1) }));
  const LINE = 'Clips for 1 of 2 own promotions; the full episode audio is not kept.';
  assert.equal((promoOnly.html.match(/data-action="clip-toggle"/g) || []).length, 1, 'fixture: one promotion row plays');
  assert.equal(episodeAudioLine(promoOnly.html), LINE);
  assert.equal(aboutAudioRow(promoOnly.html), LINE);
  assert.doesNotMatch(text(promoOnly.html), /No recording or playback is implied/);
  // An ad row that plays keeps the ad line; no promotion clip keeps the no-clip state.
  const both = await episodePage(epReads(g303Ads(2), { self_promo: g303Promos(1) }));
  assert.equal(aboutAudioRow(both.html), 'Clips for 2 of 4 ads; the full episode audio is not kept.');
  const none = await episodePage(epReads(g303Ads(0), { self_promo: g303Promos(0) }));
  assert.equal(aboutAudioRow(none.html), none.win.EWUI.AUDIO_STATES.none);
});
