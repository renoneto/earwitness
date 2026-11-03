/* UAT fix round, review #1 (r1b): Settings mentions wording (R1-05),
   archived campaigns off Overview (R1-07), other brands named from the
   directory (R1-10), copy leftovers (R1-13), not-found h1 (R1-15) and no
   order numbers in an overlapping ad break (R1-16).
   Loader and fixtures copied from frontend_uat_tv.test.cjs.
   Run: node --test tests/frontend_uat_r1b.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const SRC = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9173/app/' + route },
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

/* -- fixtures (frontend_uat_tv.test.cjs shapes) -- */
const CUR = 36;
const brandOf = display => (display ? { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }
  : { key: null, display: null, display_basis: 'none', confirmed: false });
const epAd = (id, span, display, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: brandOf(display), needs_review: false, self_promo: false, kind: 'rule', source_label: 'Rule candidate',
  transcript_start_s: span[0], media: null, transcript_version_id: CUR, on_current_transcript: true, ...over });
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
  current_transcript_version_id: CUR, earlier_transcripts: [], ...over });
const epRecord = (over = {}) => ({ ref: 'ep:ep_tv', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
  record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
  episode: { episode_id: 'ep_tv', transcript_version_id: CUR, ep_title: 'Michelle Parker', show_title: 'True Crime', pub_date: '2026-09-20',
    duration_min: 85.8, transcript_duration_s: 5148, transcript_is_current: true },
  show: { show_identity_id: 'cs-tc', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 5148 }, selected_bounds: { start_s: 0, end_s: 5148 }, media: {},
  window: { segments: [{ segment_id: 1, start_s: 100, end_s: 110, text: 'Lowes has you covered.', text_start: 0, text_end: 22 }], text_complete: true, next_cursor: null },
  ...over });
const episodePage = reads => page('#/episode/ep_tv', p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_tv')) return { record: epRecord() };
  if (p === '/customer/episodes/ep_tv') return { episode: reads };
  return {};
});
const REF = 'sr1:' + 'a'.repeat(32);
const evRecord = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'progressive', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 3400, end_s: 3480 },
  episode: { episode_id: 'ep_tv', show_title: 'True Crime', ep_title: 'Michelle Parker', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 85.8, transcript_version_id: CUR, transcript_duration_s: 5148, transcript_is_current: true },
  show: { show_identity_id: 'cs-tc', display_title: 'True Crime', unresolved: 0 },
  evidence: { id: 'o1', episode_id: 'ep_tv', orig_start_s: 3400, orig_end_s: 3480, transcript_version_id: CUR }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'rule', source_label: 'Rule candidate', brand: brandOf('Progressive'), needs_review: false, self_promo: false, confidence: 'high',
    display_span: [3400, 3480], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Progressive' } },
  window: { segments: [{ segment_id: 9, start_s: 3400, end_s: 3480, text: 'Progressive saves you money.', text_start: 0, text_end: 28 }], text_complete: true, next_cursor: null },
  ...over });
const evidencePage = (rec, brands) => page('#/evidence/' + encodeURIComponent(REF), p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_tv') return { episode: epReads([]) };
  if (p === '/customer/brands') return brands instanceof Error ? brands : { brands: brands || [] };
  return {};
});

/* -- R1-05: Settings says what All mentions counts (F-8) -- */
test('R1-05 Settings: All mentions counts transcript passages, never "ad records plus"', async () => {
  const { html } = await page('#/settings', p => (p === '/customer/preferences' ? { preferences: { revision: 1 } } : {}));
  assert.match(html, /<dt>All mentions<\/dt><dd class="small">Transcript passages that say the words, inside or outside ads\.<\/dd>/);
  assert.doesNotMatch(html, /Ad records plus|ordinary transcript mentions/);
  for (const file of fs.readdirSync(APP).filter(f => /\.(js|html)$/.test(f))) {
    assert.doesNotMatch(fs.readFileSync(path.join(APP, file), 'utf8'), /Ad records plus|ordinary transcript mentions/i, file);
  }
});

/* -- R1-07: archived campaigns are not tracked on Overview -- */
test('R1-07 Overview leaves archived campaigns and their gaps out of Tracked campaigns', async () => {
  const overview = {
    coverage: { episodes: 3, shows: 1, episodes_with_transcript: 3 }, recent_events: [], offer_sequence: [], alert_totals: [], activity: [],
    tracked_campaigns: [
      { id: 1, name: 'Live one', brand: 'acme', brand_display: 'Acme', state: 'Tracking', href: '#/campaign/1' },
      { id: 2, name: 'Old one', brand: 'acme', brand_display: 'Acme', state: 'Archived', href: '#/campaign/2' },
      { id: 3, name: 'Legacy one', brand: 'acme', brand_display: 'Acme', state: 'Read-only', href: '#/campaign/3' }],
    campaign_gaps: [
      { campaign_id: 1, campaign_name: 'Live one', placement_id: 'exp:a', outcome: 'unknown', linkage: 'insufficient', label: 'Not enough data', placement_label: 'Casefile · expects 1 ad', placement_href: '#/campaign/1/placement/exp:a' },
      { campaign_id: 2, campaign_name: 'Old one', placement_id: 'exp:b', outcome: 'unknown', linkage: 'insufficient', label: 'Not enough data', placement_label: 'Serial · expects 2 ads', placement_href: '#/campaign/2/placement/exp:b' }],
  };
  const { html } = await page('#/overview', p => (p === '/customer/overview' ? overview : {}));
  assert.match(html, /Tracked campaigns · 1</);
  assert.match(html, /Live one/);
  assert.doesNotMatch(html, /Old one/, 'the archived campaign is not listed');
  assert.doesNotMatch(html, /Serial · expects 2 ads/, 'nor are its gaps');
  assert.doesNotMatch(html, /Needs attention/);
  assert.match(html, /1 read-only campaign from before campaign types is on the Campaigns page\./, 'the read-only note is unchanged');
});

/* -- R1-10: other brands on a read are named from the directory -- */
test('R1-10 attributionText names other brands from the directory, never the raw key', async () => {
  const win = loadViews();
  const at = win.EWViews.attributionText;
  const brands = [{ key: 'acme', brand: 'acme', display: 'Acme Co' }, { key: 'betterhelp', display: 'BetterHelp' }];
  const item = { read: { brand: { key: 'betterhelp', display: 'BetterHelp' } },
    attribution: [{ brand: 'betterhelp', basis: 'suggested' }, { brand: 'acme', basis: 'confirmed' }] };
  // PLAIN-ADS (DEP7): names only, never the review basis.
  assert.equal(at(item, brands), 'BetterHelp, Acme Co');
  // The API's own display name still wins.
  assert.equal(at({ attribution: [{ brand: 'acme', brand_display: 'ACME', basis: 'confirmed' }] }, brands), 'ACME');
  // A key the directory does not name is not shown (hidden or under review).
  assert.equal(at({ read: { brand: { key: 'betterhelp', display: 'BetterHelp' } },
    attribution: [{ brand: 'betterhelp', basis: 'suggested' }, { brand: 'hiddenco', basis: 'suggested' }] }, brands), 'BetterHelp');
  assert.equal(at({ attribution: [{ brand: 'hiddenco', basis: 'suggested' }] }, brands), 'Brand not identified');
  assert.equal(at({ attribution: [{ brand: 'hiddenco', basis: 'suggested' }] }), 'Brand not identified', 'no directory: no key');
  // A read whose brand is under review names no brand at all.
  assert.equal(at({ read: { brand: { key: null, display: null, under_review: true } },
    attribution: [{ brand: 'acme', basis: 'suggested' }] }, brands), 'Brand not identified');
  assert.equal(at({ attribution: [] }, brands), 'Brand not identified');
});

test('R1-10 the Evidence page reads the brand directory for other brands and shows names', async () => {
  const rec = evRecord({ attribution: [{ brand: 'progressive', basis: 'suggested', channel: 'spoken' }, { brand: 'spinquest', basis: 'suggested', channel: 'url' }] });
  const { html, calls } = await evidencePage(rec, [{ key: 'spinquest', display: 'SpinQuest' }, { key: 'progressive', display: 'Progressive' }]);
  assert.ok(calls.includes('/customer/brands'));
  assert.match(text(html), /Progressive \(heard\), SpinQuest \(web address\)/);
  assert.doesNotMatch(text(html), /suggested|not confirmed/);
  assert.doesNotMatch(text(html), /spinquest \(/);
  // Only the read's own brand: no directory read.
  const own = await evidencePage(evRecord(), []);
  assert.ok(!own.calls.includes('/customer/brands'));
  assert.match(text(own.html), /Attribution\s*Progressive/);
  // A failed directory read never shows the key and never fails the page.
  const failed = await evidencePage(rec, new Error('down'));
  assert.match(text(failed.html), /Progressive \(heard\)/);
  assert.doesNotMatch(text(failed.html), /spinquest/);
});

/* -- R1-13: copy leftovers -- */
// PLAIN-ADS (DEP7): a named ad has no caveat. SEARCH-CLARITY (M3): an ad
// with no brand recorded says why it counts toward no brand.
test('R1-13 the Evidence caveat: an unnamed ad says why; a named one has none', async () => {
  const noName = { ...evRecord().read, brand: brandOf(null) };
  const why = /<p class="ew-reader-caveat">We found this ad from its ad words, but no brand name was recorded, so it doesn’t count toward any brand\. Know the brand\? Use <button type="button" class="text-link" data-action="goto-report">Report a mistake<\/button>\.<\/p>/;
  const possible = await evidencePage(evRecord({ read: noName, attribution: [] }));
  assert.match(possible.html, why);
  const unresolved = await evidencePage(evRecord({ status: 'unresolved', read: noName, attribution: [] }));
  assert.match(unresolved.html, why);
  const named = await evidencePage(evRecord({ status: 'unresolved' }), []);
  assert.doesNotMatch(named.html, /ew-reader-caveat|not confirmed/);
});

test('R1-13 the allocation dialog title uses plain words, not "source binding" (VL-13)', () => {
  assert.doesNotMatch(SRC, /source binding`/);
  assert.match(SRC, /E\.openDialog\(ALLOC_TITLE\[op\] \|\| 'Change this source', '', '', ''\)/);
  assert.doesNotMatch(SRC, /already bound to this source/);
});

const T1 = '2026-10-14T16:27:59+00:00';
const ALERT = { id: 5, name: 'Shopify', revision: 1, definition_revision: 2, last_successful_definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, unread_event_count: 1, query: { version: 2, mode: 'ads', q: '', brands: ['shopify'] },
  current_matches: { count: 1, tracked: 1, tracked_by_kind: { new_finding: 1 }, held_for_brand_review: 0 },
  events: [{ id: 1, kind: 'new_finding', after_ref: 'sr1:1', read_state: 0, historical: false, observed_at: T1, definition_revision: 1, checked_at: T1,
    summary: { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'S', ep_title: 'E', pub_date: '2026-10-09', span: [10, 40], text: 'words',
      read: { brand: { key: 'shopify', display: 'Shopify', confirmed: true }, needs_review: false, historical: false } } }] };
const alertPage = alert => page('#/alert/5', p => {
  if (p === '/customer/alerts/5') return { alert };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'shopify', display: 'Shopify' }] };
  return {};
});
test('R1-13 an unchecked stopped or paused alert never says "Use Check local records" (VL-17)', async () => {
  const active = await alertPage(ALERT);
  assert.match(active.html, /Not checked since the settings changed\. Use Check local records to set the new starting point\./);
  const stopped = await alertPage({ ...ALERT, lifecycle: 'stopped' });
  assert.match(stopped.html, /Stopped — no further checks\. History is kept\./);
  assert.doesNotMatch(stopped.html, /Use Check local records/);
  const paused = await alertPage({ ...ALERT, lifecycle: 'paused' });
  assert.match(paused.html, /Paused — resume the alert to check for new matches\. History is kept\./);
  assert.doesNotMatch(paused.html, /Use Check local records/);
});

test('R1-13 the campaign page says "Expects N ads" (row 1: the server counts ads)', () => {
  assert.match(SRC, /`Expects \$\{esc\(adCount\(p\.expected_count\) \|\| String\(p\.expected_count\)\)\}`/);
  assert.doesNotMatch(SRC, /Expects \$\{[^`]*'reads'/, 'never a read count');
});

/* -- R1-15: not-found pages have an h1 -- */
test('R1-15 every not-found page has one h1, styled like the old empty-state heading', async () => {
  const E = loadAt('#/overview').EW;
  for (const html of [E.notFound(), E.notFound({ kind: 'campaign' }), E.notFound({ route: true })]) {
    assert.equal((html.match(/<h1>/g) || []).length, 1, html);
    assert.doesNotMatch(html, /<h2>/);
  }
  assert.match(E.notFound({ kind: 'alert' }), /<div class="empty"><h1>We can(?:'|&#39;)t find this alert<\/h1>/);
  assert.match(E.empty('No matches', 'x'), /<h2>No matches<\/h2>/, 'other empty states keep their h2');
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  assert.match(css, /\.empty h1, \.empty h2, \.ew-state h2, \.ew-state h3 \{ font: 600 20px\/26px var\(--ew-serif\); \}/);
  const { html } = await page('#/campaign/99', () => Object.assign(new Error('gone'), { status: 404 }));
  assert.match(html, /<h1>We can(?:'|&#39;)t find this campaign<\/h1>/);
});

/* -- R1-16: overlapping break rows carry no order numbers -- */
test('R1-16 / D-1 rows in an overlapping ad are not numbered; reads 1 s apart are two ads with no box and no numbers', async () => {
  const nested = [epAd('d1', [2758.36, 2914.2], 'Progressive'), epAd('j1', [2760, 2820], null, { kind: 'jev', source_label: 'Jev window' })];
  const { html } = await episodePage(epReads(nested));
  const box = (html.match(/<div class="ew-pod ew-pod-box">[\s\S]*?<\/ol><\/div>/) || [''])[0];
  assert.match(text(box), /some overlapping/);
  assert.doesNotMatch(box, /class="ew-pod-n"/);
  // D-1 supersedes the back-to-back pod: reads 1 s apart are two ads, no box, no numbers.
  const pod = [epAd('a1', [100, 130], 'Lowes'), epAd('a2', [131, 160], 'Chumba')];
  const seq = await episodePage(epReads(pod));
  assert.doesNotMatch(seq.html, /ew-pod-box|back to back|class="ew-pod-n"/);
  assert.match(seq.html, /<span class="ew-ads-count">2 ads<\/span>/);
});
