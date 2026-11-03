/* Astra independent-review fixes (fixR): H1 transcript ad tags only inside
   the loaded range, M1 chart title and unit by mode, M2 no exact zero under
   a lower bound, L1 hour times stay on one line, L2 no brand marks on a
   words alert's earlier-settings rows, N2 the Processing index target.
   Loader helpers copied from frontend_dv3_astra_reader.test.cjs.
   Run: node --test tests/frontend_dv3_astra_fixr.test.cjs */
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
async function render(route, api) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
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

/* -- H1: ad tags only for ads that start inside the loaded range ---------- */
const seg = (i, start, end, text) => ({ segment_id: i, start_s: start, end_s: end, text, text_start: 0, text_end: text.length });
const epAd = (id, span, display) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }, needs_review: false, self_promo: false,
  kind: 'named', source_label: 'Named read', transcript_start_s: span[0], media: null });
const ADS = [epAd('e1', [480, 540], 'Squarespace'), epAd('e2', [540, 600], 'AG1'),
  epAd('e3', [3620, 3680], 'Northwind'), epAd('e4', [3800, 3860], 'BetterHelp')];
const epRecord = window => ({ ref: 'ep:ep_h', source_ref: 'sr1:e2e2', canonical_source_ref: 'sr1:e2e2', record_kind: 'source_publication', relationship: 'current',
  episode: { episode_id: 'ep_h', transcript_version_id: 3, ep_title: 'T', show_title: 'S', pub_date: '2026-10-09', duration_min: 168 },
  show: { show_identity_id: 'cs-long', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 10080 }, selected_bounds: { start_s: 0, end_s: 10080 },
  bounds_basis: 'full_effective_source', media: {}, window });
const START = { segments: [seg(1, 0, 60, 'Hello there.'), seg(2, 60, 209, 'Welcome.')], text_complete: false, next_cursor: 'c2', truncation_reason: 'limit' };
// Opened at 1:00:00: the window starts at 3570 (30 s lead-in).
const MID = { segments: [seg(10, 3570, 3580, 'Ordinary speech one.'), seg(11, 3580, 3590, 'Ordinary speech two.'),
  seg(12, 3590, 3620, 'Ordinary speech three.'), seg(13, 3620, 3680, 'This hour is brought to you by Northwind.'),
  seg(14, 3680, 3700, 'Back to the show.')], text_complete: false, next_cursor: 'c9', truncation_reason: 'limit' };
const NEXT = { segments: [seg(15, 3700, 3800, 'More talk.'), seg(16, 3800, 3860, 'BetterHelp sponsors this.'), seg(17, 3860, 3900, 'Later.')],
  text_complete: false, next_cursor: 'c10', truncation_reason: 'limit' };
const epApi = p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_h')) return { record: epRecord(START) };
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_h') + '?start_s=3570&end_s=10080') return { record: epRecord(MID) };
  if (p === '/customer/episodes/ep_h') return { episode: { episode_id: 'ep_h', ads: ADS, self_promo: [], screened_no_ads: false,
    screening: { label: 'Screened', jev_screened: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false } } };
  if (p.startsWith('/customer/records/ep%3Aep_h?cursor=c9')) return { record: { ref: 'ep:ep_h', window: NEXT } };
  return new Error('unexpected ' + p);
};
const lineTags = html => [...html.matchAll(/<li[^>]*data-segment="(\d+)"[^>]*>.*?<\/li>/g)]
  .map(m => [m[1], (m[0].match(/<span class="ew-line-tag[^"]*">([^<]*)<\/span>/) || [])[1] || '']);

test('H1 a window opened mid-episode puts no earlier ad tags on its first lines; an ad inside it is tagged once', async () => {
  const { html } = await render('#/episode/ep_h?at=3600', epApi);
  const tags = lineTags(html);
  assert.deepEqual(tags.map(t => t[0]), ['10', '11', '12', '13', '14']);
  assert.deepEqual(tags.slice(0, 3).map(t => t[1]), ['', '', ''], 'ads at 8:00 and 9:00 never label ordinary speech at 59:30');
  assert.equal(tags[3][1], 'Ad · Northwind', 'the ad that starts inside the window keeps its tag');
  assert.equal(tags[4][1], '');
  assert.doesNotMatch(html, /ew-line-tag[^>]*>Ad pod · spot/);
  assert.equal((html.match(/>Ad · Northwind</g) || []).length, 1, 'tagged exactly once');
  // The ad still to come waits for Load the next part; the spent ones do not.
  const box = (html.match(/<div class="window-text"[^>]*>/) || [''])[0];
  assert.match(box, /data-tags="[^"]*BetterHelp/);
  assert.doesNotMatch(box, /Squarespace|AG1|Northwind/);
});

test('H1 the default window from 0:00 tags each ad break: reads that only touch are two breaks, each "Ad · name"', async () => {
  // D-1: e1 and e2 only touch, so they are two ad breaks, each tagged "Ad · name".
  const rec = epRecord({ segments: [seg(1, 470, 490, 'Quick break.'), seg(2, 540, 560, 'Go to AG1.')], text_complete: true, next_cursor: null });
  const { html } = await render('#/episode/ep_h', p => (p === '/customer/records/' + encodeURIComponent('ep:ep_h') ? { record: rec } : epApi(p)));
  assert.deepEqual(lineTags(html).map(t => t[1]), ['Ad · Squarespace', 'Ad · AG1']);
});

function appendedBox(tagsAttr, spansAttr) {
  const added = [];
  const list = { lastElementChild: null, appendChild(li) { added.push(li); this.lastElementChild = li; } };
  const box = { dataset: { count: '5', cursor: 'c9', loadedFrom: '3570', loadedTo: '3700', ...(tagsAttr ? { tags: tagsAttr } : {}), ...(spansAttr ? { spans: spansAttr } : {}) }, children: [],
    querySelector: sel => (sel === 'ol.transcript' ? list : null), closest: () => null };
  return { box, added };
}
const tagOf = li => (li.innerHTML.match(/<span class="ew-line-tag[^"]*">([^<]*)<\/span>/) || [])[1] || '';

test('H1 Load the next part tags an ad that starts in the appended part exactly once, and never an earlier ad', async () => {
  const { win, html } = await render('#/episode/ep_h?at=3600', epApi);
  const unq = name => (html.match(new RegExp(`data-${name}="([^"]*)"`)) || [])[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  const { box, added } = appendedBox(unq('tags'), unq('spans'));
  await win.EWViews.handleAction({ dataset: { action: 'window-more', ref: 'ep:ep_h', cursor: 'c9' }, closest: () => box });
  assert.deepEqual(added.map(tagOf), ['', 'Ad · BetterHelp', '']);
  // The appended ad line is shaded like the first window's ad lines (re-review R1).
  assert.deepEqual(added.map(li => li.className || ''), ['', 'active', '']);
  assert.match(html, /<li class="active" data-segment="13"/, 'the first window shades its ad line');
  assert.equal(box.dataset.tags, undefined, 'no tag left waiting');
  // A stale tag from before the appended part (here 9:00) is spent, not shown.
  const stale = appendedBox(JSON.stringify([{ at: 540, review: false, label: 'Ad pod · spot 2 · AG1' }, { at: 3800, review: true, label: 'Brand under review' }]));
  await win.EWViews.handleAction({ dataset: { action: 'window-more', ref: 'ep:ep_h', cursor: 'c9' }, closest: () => stale.box });
  assert.deepEqual(stale.added.map(tagOf), ['', 'Brand under review', '']);
  assert.match(stale.added[1].innerHTML, /class="ew-line-tag is-review"/);
  // A second append never repeats a tag.
  const again = appendedBox(stale.box.dataset.tags);
  await win.EWViews.handleAction({ dataset: { action: 'window-more', ref: 'ep:ep_h', cursor: 'c9' }, closest: () => again.box });
  assert.deepEqual(again.added.map(tagOf), ['', '', '']);
});

/* -- M1: chart title and unit by mode ------------------------------------ */
const CELLS = [{ key: 'w1', label: 'Jun 15–Jun 21', count: 1, interval: { from: '2026-06-15', to: '2026-06-21' },
  selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'all_results' } } },
{ key: 'w2', label: 'Jun 22–Jun 28', count: 4, interval: { from: '2026-06-22', to: '2026-06-28' },
  selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'all_results' } } }];

test('M1 mentions and episodes charts name what they count, never ads', async () => {
  const { win } = await render('#/search', () => ({}));
  const m = win.EWViews.weekChart(CELLS, { mode: 'mentions', q: 'free trial' }, {});
  assert.match(m, /aria-label="Mentions matching “free trial”"/);
  assert.match(m, /Mentions matching “free trial” · Transcript passages · actual date intervals/);
  assert.match(m, /<caption>Transcript passages within each date interval<\/caption>/);
  assert.match(m, /: 1 transcript passage\. Open exact records\./);
  assert.match(m, /: 4 transcript passages\. Open exact records\./);
  assert.doesNotMatch(m, /Ads matching|Ads by date|ads \(confirmed|Ads \(confirmed/);
  const all = win.EWViews.weekChart(CELLS, { mode: 'mentions' }, {});
  assert.match(all, /aria-label="Mentions by date"/);
  const e = win.EWViews.weekChart(CELLS, { mode: 'episodes', q: 'wayfair' }, {});
  assert.match(e, /Episodes matching “wayfair” · Episodes · actual date intervals/);
  assert.match(e, /: 1 episode\. Open exact records\./);
  assert.match(e, /: 4 episodes\. Open exact records\./);
  assert.doesNotMatch(e, /Ads matching|Ads by date|ads \(confirmed|Ads \(confirmed/);
  // Row 1: an ads chart counts the ads its cells carry; cells with no ad count draw nothing.
  const ADCELLS = CELLS.map((c, i) => ({ ...c, breaks: i ? 3 : 1 }));
  const a = win.EWViews.weekChart(ADCELLS, { mode: 'ads', q: 'sleep' }, {});
  assert.match(a, /Ads matching “sleep” · Ads · actual date intervals/);
  assert.doesNotMatch(a, /confirmed|possible/, 'PLAIN-ADS: no review state in the chart');
  assert.match(a, /: 1 ad\. Open exact records\./);
  assert.match(win.EWViews.weekChart(ADCELLS, { mode: 'ads' }, {}), /aria-label="Ads by date"/);
  assert.match(win.EWViews.weekChart(ADCELLS, { mode: 'ads' }, { total_relation: 'lower_bound' }), /: at least 1 ad \(lower bound\)/);
  assert.equal(win.EWViews.weekChart(CELLS, { mode: 'ads', q: 'sleep' }, {}), '', 'no ad count: no chart, no number');
});

/* -- M2: no exact zero under a lower bound ------------------------------- */
const adItem = { ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', product: 'Greens', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false } };
const searchApi = (relation, status) => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return { query: { version: 2, mode: 'ads', q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
    // Row 1: ads come from the server (break_total, ad_breaks); the status mix counts each cell's ads.
    result: { items: [adItem], total: 19, per_page: 10, total_relation: relation, dated_total: 19, undated_total: 0,
      break_total: 19, break_dated_total: 19, break_undated_total: 0, ad_breaks: [{ id: 'brk:a', refs: ['sr1:a'], read_count: 1, span: [62, 121], brands: [] }] },
    coverage: { episodes_in_scope: 288 }, summary: { week: [], status } };
  return new Error('unexpected ' + p);
};
const countLine = html => ((html.match(/<p class="ew-count-line"[^>]*>(.*?)<\/p>/) || [])[1] || '').replace(/<[^>]+>/g, '');

// PLAIN-ADS (DEP7): the count line has no status mix; a lower bound still says "at least".
test('M2 under a lower bound the count is never written as exact, and no status mix follows it', async () => {
  let { html } = await render('#/search?mode=ads&q=sleep', searchApi('lower_bound', [{ key: 'finding', count: 25, breaks: 19 }]));
  assert.equal(countLine(html), 'Showing 1 to 1 of at least 19 ads');
  ({ html } = await render('#/search?mode=ads&q=sleep', searchApi('lower_bound', [{ key: 'confirmed', count: 25, breaks: 19 }])));
  assert.equal(countLine(html), 'Showing 1 to 1 of at least 19 ads');
  ({ html } = await render('#/search?mode=ads&q=sleep', searchApi('exact', [{ key: 'finding', count: 25, breaks: 19 }])));
  assert.equal(countLine(html), 'Showing 1 to 1 of 19 ads');
});

/* -- L1: hour times stay on one line ------------------------------------- */
test('L1 transcript times never wrap; the time column fits an hour mark', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  assert.match(css, /\.ew-reader \.transcript li \{ display: grid; grid-template-columns: minmax\(64px, max-content\) minmax\(0, 1fr\);/);
  assert.match(css, /\.ew-reader \.transcript li > \.mono \{[^}]*white-space: nowrap;/);
  assert.match(css, /\.ew-transcript \.transcript li \{ grid-template-columns: minmax\(56px, max-content\) minmax\(0, 1fr\); \}/, 'one phone width fits an hour mark (re-review N1)');
  assert.doesNotMatch(css, /grid-template-columns: (64|52)px minmax\(0, 1fr\)/);
});

/* -- L2: words alert earlier-settings rows carry no brand marks ---------- */
const T2 = '2026-10-14T16:27:57Z';
const SUM = { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'NPR News Now', ep_title: '12am ET',
  pub_date: '2026-10-09', span: [250.02, 309.24], text: 'Go to betterhelp.com for a free trial today.',
  historical: false, read: { brand: { key: 'betterhelp', display: 'BetterHelp', confirmed: false }, needs_review: false, historical: false } };
const ev = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:' + id, read_state: 0, historical: true,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM, definition_revision: 1, checked_at: T2, ...over });
const alertApi = alert => p => {
  if (p === '/customer/alerts/' + alert.id) return { alert };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [] };
  return new Error('unexpected ' + p);
};
const earlierPart = html => html.slice(html.indexOf('Found under earlier settings'));

test('L2 a words alert renders earlier-settings excerpts without marks; a brand alert still marks the brand', async () => {
  const base = { id: 1, name: 'A', revision: 1, definition_revision: 2, last_successful_definition_revision: 1, lifecycle: 'active_local',
    baseline_complete: true, last_successful_at: T2, current_matches: { count: 1 }, unread_event_count: 1, events: [ev(1)] };
  const words = await render('#/alert/1', alertApi({ ...base, query: { version: 2, mode: 'ads', q: 'free trial offer' } }));
  assert.ok(words.html.includes('Found under earlier settings'));
  const wordsPart = earlierPart(words.html);
  assert.match(wordsPart, /betterhelp\.com/);
  assert.doesNotMatch(wordsPart, /<mark>/, 'no brand or word marks the alert never used');
  const brand = await render('#/alert/1', alertApi({ ...base, query: { version: 2, mode: 'ads', brands: ['betterhelp'] } }));
  assert.match(earlierPart(brand.html), /<mark>betterhelp<\/mark>\.com/i, 'brand alerts keep the brand mark');
});

/* -- N2: the Processing index button opens Processing sources ------------- */
test('N2 Data & Settings "Processing" jumps to the Processing sources panel', () => {
  const src = fs.readFileSync(path.join(APP, 'operator', 'views.js'), 'utf8');
  assert.match(src, /<button type="button" data-jump="ds-proc-sources">Processing<\/button>/);
  assert.doesNotMatch(src, /data-jump="ds-runs">Processing</);
  const at = s => src.indexOf(s);
  assert.ok(at('"ds-proc-sources")') < at('"ds-runs")'), 'Recent runs follows Processing sources');
});
