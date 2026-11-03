/* UAT fix round, review #1 (r1a), frontend part.
   R1-12: a read with no transcript version (on_current_transcript null)
   is unknown: it is never in the earlier-copy block (R2-03: nor counted
   as an ad, on the strip or in a pod: frontend_uat_r2.test.cjs), and never claims a line of the
   transcript shown (no line tag, no Jump to transcript). Loader copied
   from frontend_uat_tv.test.cjs. Run: node --test tests/frontend_uat_r1a.test.cjs */
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
/* D-1 (decisions round): the server groups an episode's ad reads into ad
   breaks (same transcript copy; spans chain while start < running max
   end; touching spans are two breaks; a read with no copy stands alone).
   Fixtures carry what the server sends. */
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
  return out.map((g, i) => ({ id: 'brk:' + i, transcript_version_id: g[0].transcript_version_id === undefined ? 'cur' : g[0].transcript_version_id, on_current_transcript: g[0].on_current_transcript !== false,
    span: g[0].span ? [g[0].span[0], Math.max(...g.map(a => a.span[1]))] : null, refs: g.map(a => a.evidence_id), read_count: g.length, brands: [] }));
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


const unknownTx = (id, span, display) => epAd(id, span, display, { transcript_version_id: null, on_current_transcript: null });

test('R1-12: a read with no transcript version stays with the current reads, not in the earlier copy', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes'), unknownTx('u1', [1000, 1060], 'Acme'), earlier('o1', [3400, 3480], 'Progressive')];
  const { html } = await episodePage(epReads(ads));
  // R2-03 / row 1: the unknown read is counted in its own part, never with the current ads, and has no strip mark.
  assert.match(html, /<span class="ew-ads-count">1 ad; no transcript copy: 1 ad; earlier copy: 1 ad<\/span>/);
  assert.deepEqual(stripRects(html), [1], 'only the current read is on the strip');
  const block = (html.match(/<div class="ew-earlier">[\s\S]*?<!--\/ew-earlier--><\/div>/) || [''])[0];
  assert.match(block, /Progressive/);
  assert.doesNotMatch(block, /Acme/, 'never put in the earlier-copy block');
  assert.ok(html.indexOf('Acme') < html.indexOf('ew-earlier'));
});

test('R1-12: an unknown-transcript read claims no line of the transcript shown', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes'), unknownTx('u1', [1000, 1060], 'Acme')];
  const { html } = await episodePage(epReads(ads));
  assert.deepEqual(lineTags(html), ['Ad · Lowes'], 'no "Ad · Acme" tag on the current transcript');
  assert.equal((html.match(/Jump to transcript/g) || []).length, 1, 'only the current read jumps into the transcript');
  const row = (html.match(/<li class="ew-ad-row[^>]*data-ref="sr1:u1[^"]*"[\s\S]*?<\/li>/) || [''])[0];
  assert.match(row, /Acme/);
  assert.match(row, /Open evidence/);
  assert.doesNotMatch(row, /Jump to transcript/);
});

test('R1-12: an older server that sends no flag keeps every read current', async () => {
  const ads = [epAd('c1', [100, 160], 'Lowes', { on_current_transcript: undefined, transcript_version_id: undefined })];
  const { html } = await episodePage(epReads(ads));
  assert.deepEqual(lineTags(html), ['Ad · Lowes']);
  assert.equal((html.match(/Jump to transcript/g) || []).length, 1);
});
