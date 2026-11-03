/* PODS-APP (lead PODS-5; UX final strings R/ws-ux/PODS-STRINGS.md P1-P4).
   Ads Muse Spark named in Meta's pods batch carry Fresh's read shape
   (2026-10-28, binding): read.brand.display_basis "model",
   read.brand.named_by "muse-spark" (never rendered), read.brand.model_channel
   "spoken" | "url". Every surface that shows such a brand adds P1
   "Named by Muse Spark" right after it (muted text, never an icon alone).
   The ad detail page adds one tip under the brand: P2 for the spoken
   channel, P3 for the url channel (required there; never P2 on a url row).
   The ad lists (Search results, the Brand page's Recent ads, the episode
   page) add P4, counted from the model-named ads shown on that list and
   hidden at 0. No confirmed/verified/checked/possible/likely/suggested next
   to the label. Any other brand basis, a held brand, or a span basis
   read.display_basis "model" adds nothing: the page is byte-equal with the
   label, tip and summary taken out. Campaign screens treat a model-named
   read as "Brand not identified" (FUNC-1 decision 2; FIX1).
   Run: node --test tests/frontend_pods_named_by_muse.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadApp() {
  const store = {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} },
    querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  const doc = {
    activeElement: null,
    querySelector: () => dummy, querySelectorAll: () => [], addEventListener: () => {},
    createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t,
  };
  const sandbox = {
    console, URLSearchParams, AbortController, URL, setTimeout, clearTimeout,
    location: { hash: '#/overview', href: 'http://127.0.0.1:9172/app/#/overview' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: doc,
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => {};
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  return { win: sandbox, doc };
}
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await new Promise(res => setTimeout(res, 0)); };
async function render(route, api, { hosted = false, ask = false } = {}) {
  const { win, doc } = loadApp();
  await settle(1);
  win.EW.current.hosted = hosted;
  if (ask) win.EW.current.askAvailable = true;
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  doc.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, classList: { add() {}, remove() {} },
    querySelector: () => null, querySelectorAll: () => [] });
  win.EW.header = () => {};
  win.EW.toast = () => {};
  win.EW.api = async (p, opts) => {
    const r = await api(p, opts);
    if (r instanceof Error) throw r;
    if (r === undefined) throw new Error('unexpected request ' + p);
    return r;
  };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await settle();
  return main.innerHTML;
}

/* -- the contract (PODS-STRINGS P1-P4, exact bytes) ------------------------ */
const P1 = 'Named by Muse Spark';
const MARK = ' <span class="ew-brand-mark">Named by Muse Spark</span>';
// FIX1 (UX 2026-10-28): the app's apostrophe, ’, as PODS-STRINGS.md now has it.
const P2 = 'Muse Spark, Meta’s AI model, named this brand from the words spoken in the ad’s transcript and found it on our brand list. If it’s wrong, use Report a mistake.';
const P3 = 'Muse Spark, Meta’s AI model, named this brand from a web address in the ad’s transcript and found it on our brand list. If it’s wrong, use Report a mistake.';
const P4 = n => `Muse Spark named the brand in ${n} of these ads.`;
const P4_HTML = n => `<p class="section-note ew-model-count">${P4(n)}</p>`;
const TIP_RE = /<p class="ew-reader-named">[\s\S]*?<\/p>/;
const P4_RE = /<p class="section-note ew-model-count">[^<]*<\/p>/g;
const BANNED = /\b(confirmed|verified|checked|possible|likely|suggested)\b/i;
const count = (hay, needle) => hay.split(needle).length - 1;
const textOf = html => html.replace(/<[^>]*>/g, '');
// What the model fixture adds over the same fixture with another basis.
// FIX1: a name kept with its label (.ew-brand-unit) is unwrapped too.
const UNIT_RE = /<span class="ew-brand-unit">([\s\S]*?) <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/g;
const strip = html => html.replace(UNIT_RE, '$1').split(MARK).join('').replace(TIP_RE, '').replace(P4_RE, '');

// Fresh's shape: a model brand has named_by and model_channel; any other
// brand has neither key.
const brand = (basis, over = {}) => (basis === 'model'
  ? { key: 'quince', display: 'Quince', display_basis: 'model', suggested: null, confirmed: false, named_by: 'muse-spark', model_channel: 'spoken', ...over }
  : { key: 'quince', display: 'Quince', display_basis: basis, suggested: 'quince', confirmed: false, ...over });
const OTHER_BASES = ['curated', 'brand_list', 'transcript_cased', 'none'];
const HELD = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const SURFACES = [];

// One surface: a model read (either channel) shows the label `marks`
// times, never shows named_by, and every other basis (and a span basis
// "model") renders the same page with nothing added: no label, tip or
// summary (item 6: no model rows, no change).
const PODS_MARKUP = /ew-brand-mark|ew-reader-named|ew-model-count|Muse Spark/;
// Part 4 (UX): a card whose every name is model-named has no Create alert;
// alert buttons are taken out of both sides before comparing (the part-4
// Create alert test pins where it shows and where it does not). FIX1: the
// same for the Add to campaign brand name (the FIX1 test pins it).
const noAlert = html => html.replace(/<button[^>]*data-action="alert-(?:brand|evidence)"[^>]*>[^<]*<\/button>/g, '')
  .replace(/(data-action="add-to-campaign"[^>]*?) data-brand-name="[^"]*"/g, '$1');
async function surface(name, renderWith, { marks = 1 } = {}) {
  SURFACES.push({ name, renderWith, marks });
  const model = await renderWith(brand('model'), {});
  for (const channel of ['spoken', 'url']) {
    const html = channel === 'spoken' ? model : await renderWith(brand('model', { model_channel: 'url' }), {});
    assert.equal(count(html, MARK), marks, `${name} (${channel}): the label after the brand`);
    assert.doesNotMatch(html, /muse-spark|model_channel/, `${name} (${channel}): named_by and the channel are never rendered`);
    assert.doesNotMatch(html, /aria-hidden="true">Named by Muse Spark/, `${name} (${channel}): screen readers read the label`);
  }
  for (const basis of OTHER_BASES) {
    const other = await renderWith(brand(basis), {});
    assert.doesNotMatch(other, PODS_MARKUP, `${name}: no label, tip or summary for ${basis}`);
    if (basis === 'brand_list') {
      assert.match(other, /Quince/, `${name}: the fixture shows the brand`);
      assert.equal(noAlert(strip(model)), noAlert(other), `${name}: nothing else changes`);
      assert.equal(noAlert(strip(await renderWith(brand('model', { model_channel: 'url' }), {}))), noAlert(other), `${name} (url): nothing else changes`);
    }
  }
  const spanBasis = await renderWith(brand('brand_list'), { display_basis: 'model' });
  assert.doesNotMatch(spanBasis, PODS_MARKUP, `${name}: the span basis read.display_basis is not the key`);
  const held = await renderWith(HELD, {});
  assert.doesNotMatch(held, PODS_MARKUP, `${name}: a held brand has no label`);
  return model;
}

/* -- Search ----------------------------------------------------------------- */
const searchItem = (b, readOver = {}) => ({ ref: 'sr1:' + 'a'.repeat(32), record_kind: 'evidence', status: 'finding', ep_title: 'Ep one',
  show_title: 'Show one', show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: b, display_span: [62, 121], display_basis: 'detected', has_clip: false, needs_review: false, ...readOver } });

test('Search card: the mark follows the brand in the card heading', async () => {
  const { win } = loadApp();
  const html = await surface('search card', (b, r) => win.EWUI.sourceCard(searchItem(b, r), { query: 'sleep', index: 0 }));
  assert.match(html, /<h3 class="ew-result-title"><span class="ew-brand-unit"><span class="ew-brand">Quince<\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span><\/h3>/);
});

test('Search list row: the mark follows the brand in the row', async () => {
  const { win } = loadApp();
  const html = await surface('search row', (b, r) => win.EWUI.resultRow(searchItem(b, r), { index: 0 }));
  assert.match(html, /<span class="ew-row-brand"><span class="ew-brand-unit"><span class="ew-row-brand-name"><span class="ew-brand">Quince<\/span><\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span><\/span>/);
});

const bundle = items => ({ query: { version: 2, mode: 'ads', q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 4 }, summary: { week: [] } });
const searchApi = item => p => (p === '/customer/brands' ? { brands: [] } : p === '/customer/shows' ? { shows: [] }
  : p === '/customer/research-query' ? bundle([item]) : p.startsWith('/customer/filter-values?') ? { items: [] } : undefined);
const asideOf = html => (html.match(/<aside class="ew-inspector"[\s\S]*?<\/aside>/) || [''])[0];

test('Search page: the card and the panel heading carry the mark', async () => {
  const html = await surface('search page', (b, r) => render('#/search?mode=ads&q=sleep', searchApi(searchItem(b, r))), { marks: 2 });
  assert.match(asideOf(html), /<h2><span class="ew-brand-unit"><span class="ew-brand">Quince<\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span><\/h2>/);
});

/* -- Overview Recent ads ---------------------------------------------------- */
const OVERVIEW = { coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [] };
test('Overview Recent ads: the mark follows the brand link', async () => {
  const det = (b, r) => ({ ref: 'sr1:o', record_kind: 'evidence', status: 'finding', ep_title: 'Ep', show_title: 'S', pub_date: '2026-10-09', text: 'a',
    read: { brand: b, needs_review: false, historical: false, display_basis: 'detected', ...r } });
  const html = await surface('overview', (b, r) => render('#/overview', p => (p === '/customer/overview' ? OVERVIEW
    : p.startsWith('/customer/saved-items') ? { items: [] } : p.startsWith('/customer/search') ? { items: [det(b, r)] } : undefined)));
  assert.match(html, /<span class="ew-brand">Quince<\/span><\/strong><\/a> <span class="ew-brand-mark">Named by Muse Spark<\/span>/);
});

/* -- Brand page: Recent ads rows and the offer comparison ------------------- */
const SR = n => 'sr1:' + String(n).repeat(32).slice(0, 32);
const BRAND = { brand: { brand: 'quince', key: 'quince', display: 'Quince', detections: 2, ad_breaks: 2, episodes: 2, shows: 2, last_heard: '2026-10-09',
  offer_sequence: [], drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['quince'], sort: 'newest', page: 1 }, mentions: { version: 2, mode: 'mentions', brands: ['quince'] } } } };
const brandRead = (n, date, own, b, r) => ({ ref: SR(n), record_kind: 'evidence', status: 'finding', episode_id: 'e' + n, show_title: 'Show ' + n,
  ep_title: 'Ep ' + n, pub_date: date, text: 'Talk. ' + own + ' More talk.',
  read: { brand: b, display_span: [60, 90], display_basis: 'checked', has_clip: false, display_text: own, ...r } });
const brandApi = reads => p => {
  if (p === '/customer/brands/quince') return BRAND;
  if (p.startsWith('/customer/saved-items')) return { items: [], next_cursor: null };
  if (p.startsWith('/customer/search?')) {
    const q = JSON.parse(new URLSearchParams(p.split('?')[1]).get('query'));
    const items = [...reads].sort((a, b) => (q.sort === 'oldest' ? 1 : -1) * a.pub_date.localeCompare(b.pub_date));
    return { items, total: reads.length, break_total: reads.length, total_relation: 'exact' };
  }
  if (p.startsWith('/customer/brands/quince/')) return {};
  return undefined;
};
const recentOf = html => (html.match(/<ul class="ew-card lib-reads">[\s\S]*?<\/ul>/) || [''])[0];
const offerOf = html => (html.match(/<div class="lib-offer-pair">[\s\S]*?<p class="lib-offer-note">/) || [''])[0];

test('Brand page: each Recent ads row and each compared ad of a model read carries the mark', async () => {
  // Read 1 is model-named; read 2 is the other basis in every run.
  const html = await surface('brand page', (b, r) => render('#/brand/quince', brandApi([
    brandRead(1, '2026-08-12', 'Use code SLEEP10.', b, r), brandRead(2, '2026-10-09', 'Use code SLEEP20.', brand('brand_list'), {})])), { marks: 2 });
  assert.equal(count(recentOf(html), MARK), 1, 'one Recent ads row');
  assert.match(recentOf(html), /<span class="lib-read-where"><b>Show 1<\/b> <span class="lib-muted">· Ep 1 · Aug 12<\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/, 'on the model read\'s row, after where it was heard');
  assert.equal(count(offerOf(html), MARK), 1, 'one compared ad');
  assert.match(offerOf(html), /<figcaption><b>Earlier ad<\/b> <span class="ew-brand-mark">Named by Muse Spark<\/span><span>Aug 12, 2026<\/span><\/figcaption>/);
});

/* -- Episode page and the evidence page's other evidence --------------------- */
const epAd = (id, span, b, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: b, needs_review: false, self_promo: false, kind: 'named', source_label: 'Named read', transcript_start_s: span[0], media: null, ...over });
const oneBreak = (ads, i, read_count) => ({ id: 'brk:' + i, transcript_version_id: 'cur', on_current_transcript: true,
  span: [ads[0].span[0], Math.max(...ads.map(a => a.span[1]))], refs: ads.map(a => a.evidence_id), read_count, brands: [], group_id: 'brk:' + i, group_ad_count: 1 });
const epReads = (groups) => ({ episode_id: 'ep_f1', ads: groups.flat(), self_promo: [],
  ad_breaks: groups.map((g, i) => oneBreak(g, i, g.length)),
  ad_break_counts: { current: groups.length, earlier: 0, no_copy: 0 },
  screening: { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null },
  screened_no_ads: false });

const EP_REC = { ref: 'ep:ep_f1', source_ref: 'sr1:e1e1', canonical_source_ref: 'sr1:e1e1', record_kind: 'source_publication', relationship: 'current',
  episode: { episode_id: 'ep_f1', transcript_version_id: 3, ep_title: 'Recovery weeks', show_title: 'The Long Run', pub_date: '2026-10-09', duration_min: 58 },
  show: { show_identity_id: 'cs-long', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3480 }, selected_bounds: { start_s: 0, end_s: 3480 }, media: {},
  window: { segments: [{ segment_id: 1, start_s: 900, end_s: 910, text: 'Quick break.', text_start: 0, text_end: 12 }], text_complete: true, next_cursor: null } };
const epApi = episode => p => (p === '/customer/records/' + encodeURIComponent('ep:ep_f1') ? { record: EP_REC }
  : p === '/customer/episodes/ep_f1' ? { episode } : undefined);

test('Episode page: the ad row names the brand, then the mark', async () => {
  const html = await surface('episode page', (b, r) => render('#/episode/ep_f1', epApi(epReads([[epAd('e1', [60, 125], b, r)]]))));
  assert.match(html, /<b class="ew-ad-brand">Quince<\/b> <span class="ew-brand-mark">Named by Muse Spark<\/span>/);
});

const REF = 'sr1:' + 'c'.repeat(32);
const EV_ID = 'named:ep_f1.named.120000-170000';
const evRead = (b, over = {}) => ({ kind: 'named', source_label: 'Named read', brand: b, needs_review: false, self_promo: false,
  self_promo_reason: null, confidence: 'high', display_span: [120, 170], display_basis: 'detected', has_clip: false,
  said: { web_addresses: [], promo_codes: [], address_brand: null, code_brand: null, spoken_brand: 'Quince' }, ...over });
const evRecord = (read, over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'quince', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_f1', show_title: 'Quiet Hours', ep_title: 'Why we wake at 3am', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_f1', orig_start_s: 110, orig_end_s: 180 }, media: { playback: 'remote', alignment: 'unknown' },
  read, window: { segments: [
    { segment_id: 1, start_s: 110, end_s: 119, text: 'Let’s take a short break.', text_start: 0, text_end: 25 },
    { segment_id: 2, start_s: 120, end_s: 150, text: 'Quince has linen sheets.', text_start: 0, text_end: 23 },
    { segment_id: 3, start_s: 171, end_s: 180, text: 'Okay, back to it.', text_start: 0, text_end: 17 }],
  text_complete: true, next_cursor: null }, ...over });
const evApi = (rec, episode = { ads: [] }) => p => (p === '/customer/records/' + encodeURIComponent(REF) ? { record: rec }
  : p.endsWith('/mistake') ? { mistakes: [] } : p === '/customer/episodes/ep_f1' ? { episode }
  : p === '/customer/shows' ? { shows: [] } : undefined);
const evidence = rec => render('#/evidence/' + encodeURIComponent(REF), evApi(rec));

test('Ad detail: the label after the brand title and one tip under it, P2 for spoken, P3 for url', async () => {
  const html = await surface('ad detail', (b, r) => evidence(evRecord(evRead(b, r))));
  assert.match(html, /<div class="ew-reader-h1row"><h1 class="ew-reader-h1">Quince<\/h1> <span class="ew-brand-mark">Named by Muse Spark<\/span>/);
  for (const [channel, words, not] of [['spoken', P2, P3], ['url', P3, P2]]) {
    const page = await evidence(evRecord(evRead(brand('model', { model_channel: channel }))));
    const tip = page.match(TIP_RE);
    assert.ok(tip, `${channel}: the tip renders`);
    assert.equal(textOf(tip[0]), words, `${channel}: its own tip`);
    assert.equal(count(textOf(page), not), 0, `${channel}: never the other channel's tip`);
    assert.equal(count(page, 'class="ew-reader-named"'), 1, `${channel}: one tip`);
    // Under the brand: right after the title row, before the actions.
    assert.ok(page.indexOf('class="ew-reader-named"') > page.indexOf('class="ew-reader-h1row"'));
    assert.ok(page.indexOf('class="ew-reader-named"') < page.indexOf('ew-reader-actions'));
    // The report words go to this read's own Report a mistake control (the page's existing jump), not a new link.
    assert.match(tip[0], /use <button type="button" class="text-link" data-action="goto-report">Report a mistake<\/button>\.<\/p>$/);
    assert.match(page, /<details class="report-problem"><summary>Report a mistake<\/summary>/, 'the Report a mistake control is on the page');
  }
  for (const basis of OTHER_BASES) assert.doesNotMatch(await evidence(evRecord(evRead(brand(basis)))), /ew-reader-named|Muse Spark/, basis);
});

test('Ad detail: a url row is never shown the spoken tip, whatever its URL/code attribution says', async () => {
  // The model url channel is not an attribution row: the record's
  // attribution and said.web_addresses stay as they are and do not pick the tip.
  const url = brand('model', { model_channel: 'url' });
  const rec = evRecord(evRead(url, { said: { web_addresses: [], promo_codes: [], address_brand: null, code_brand: null, spoken_brand: 'Quince' } }),
    { attribution: [{ brand: 'quince', basis: 'suggested', channel: 'spoken' }] });
  const page = await evidence(rec);
  assert.equal(textOf(page.match(TIP_RE)[0]), P3);
  // A model brand with no known channel says who named it (P1) but guesses no tip.
  const unknown = await evidence(evRecord(evRead(brand('model', { model_channel: null }))));
  assert.equal(count(unknown, MARK), 1);
  assert.doesNotMatch(unknown, TIP_RE);
});

test('Ad detail: an earlier version has no own control, so the tip names Report a mistake as plain words', async () => {
  for (const [channel, words] of [['spoken', P2], ['url', P3]]) {
    const html = await evidence(evRecord(evRead(brand('model', { model_channel: channel })), { historical: true }));
    const tip = html.match(TIP_RE);
    assert.ok(tip, `${channel}: an earlier version of a model read still says who named it`);
    assert.equal(textOf(tip[0]), words);
    assert.doesNotMatch(tip[0], /<button/);
  }
});

test('Ad detail: the other evidence of the same ad carries the mark on its own row only', async () => {
  const run = async (b, r) => {
    const ads = [epAd(EV_ID, [120, 170], brand('brand_list')), epAd('e2', [150, 200], b, r)];
    const ep = epReads([ads]);
    const rec = evRecord(evRead(brand('brand_list')), { ad_break: { ...ep.ad_breaks[0], index: 0 } });
    return render('#/evidence/' + encodeURIComponent(REF), evApi(rec, ep));
  };
  const html = await surface('pod rows', run);
  assert.match(html, /<span class="ew-pod-name"><span class="ew-brand-unit"><b>Quince<\/b> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span><\/span>/);
});

/* -- Alert matches ---------------------------------------------------------- */
test('Alert matches: the matched ad names its brand, then the mark', async () => {
  const run = async (b, r) => {
    const alert = { id: 1, name: 'Sheets', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
      last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'sheets' },
      events: [{ id: 1, kind: 'new_finding', after_ref: 'sr1:0', read_state: 0, historical: false, observed_at: '2026-10-14T16:27:59+00:00',
        summary: { record_kind: 'evidence', status: 'finding', episode_id: 'ep0', show_title: 'NPR', ep_title: '12am', pub_date: '2026-10-09',
          span: [250, 309], text: 'Linen sheets.', historical: false, read: { brand: b, needs_review: false, historical: false, display_basis: 'detected', ...r } } }] };
    return render('#/alert/1', p => (p.startsWith('/customer/alerts/1') ? { alert } : p === '/customer/shows' ? { shows: [] } : p === '/customer/brands' ? { brands: [] } : undefined));
  };
  const html = await surface('alert matches', run);
  assert.match(html, /<div class="ew-work-event-head"><span class="ew-brand-unit"><strong><span class="ew-brand">Quince<\/span><\/strong> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/);
});

/* -- escaping, style, no confidence words ----------------------------------- */
test('the brand name is escaped next to the mark; the mark is fixed text', async () => {
  const { win } = loadApp();
  const evil = brand('model', { display: '<img src=x onerror=alert(1)>&' });
  const card = win.EWUI.sourceCard(searchItem(evil), { query: '', index: 0 });
  assert.match(card, /&lt;img src=x onerror=alert\(1\)&gt;&amp;<\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span>/);
  assert.doesNotMatch(card, /<img/);
  const page = await evidence(evRecord(evRead(evil)));
  assert.doesNotMatch(page, /<img/);
  assert.equal(count(page, MARK), 1);
});

test('the mark uses the "Spelling as transcribed." note style and is never hidden', () => {
  const css = ['styles.css', 'evidence.css', 'search.css'].map(f => fs.readFileSync(path.join(APP, f), 'utf8')).join('\n');
  const rule = /\.ew-brand-mark\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, '.ew-brand-mark rule');
  assert.match(rule[1], /font:\s*var\(--ew-t-meta\)/);
  assert.match(rule[1], /color:\s*var\(--ew-uncertain\)/);
  assert.doesNotMatch(rule[1], /display:\s*none|visibility:\s*hidden/);
});

/* -- P4: the summary line on ad lists, counted from the ads shown ------------ */
const M = (ch = 'spoken') => brand('model', { model_channel: ch });
const N = () => brand('brand_list');
const sItem = (n, b, over = {}) => ({ ...searchItem(b), ref: SR(n), ep_title: 'Ep ' + n, ...over });
const searchWith = (items, extra = {}, view = 'cards') => render('#/search?mode=ads&q=sleep&view=' + view, p => (p === '/customer/brands' ? { brands: [] } : p === '/customer/shows' ? { shows: [] }
  : p === '/customer/research-query' ? (() => { const out = bundle(items); Object.assign(out.result, extra); return out; })()
    : p.startsWith('/customer/filter-values?') ? { items: [] } : undefined));
const resultsOf = html => html.slice(html.indexOf('<div id="search-results">'));

test('P4 Search: counts the model-named ads among the results shown, under the results bar', async () => {
  const html = await searchWith([sItem(1, M()), sItem(2, N()), sItem(3, M('url')), sItem(4, N())]);
  assert.equal(count(html, P4_HTML(2)), 1, 'two of the four ads');
  // First in the results, before the first card.
  assert.ok(resultsOf(html).startsWith('<div id="search-results">' + P4_HTML(2)), 'at the top of the results');
  // Singular is the same sentence with 1.
  const one = await searchWith([sItem(1, M('url')), sItem(2, N())]);
  assert.equal(count(one, P4_HTML(1)), 1);
  assert.equal(P4(1), 'Muse Spark named the brand in 1 of these ads.');
  // None shown: no line.
  const none = await searchWith([sItem(1, N()), sItem(2, N())]);
  assert.doesNotMatch(none, /ew-model-count|Muse Spark/);
  // A held read is not a shown model name.
  const held = await searchWith([sItem(1, M({}), { read: { ...searchItem(M()).read, brand: { ...M(), under_review: true, display: null } } }), sItem(2, N())]);
  assert.doesNotMatch(held, /ew-model-count/);
});

test('P4 Search: cards and list count the same; an ad with two model-named pieces of evidence counts once', async () => {
  // Ads grouped by the server's ad breaks: break A holds two model reads, B one plain read, C one model url read.
  const items = [sItem(1, M(), { ad_break_id: 'A' }), sItem(2, M(), { ad_break_id: 'A' }), sItem(3, N(), { ad_break_id: 'B' }), sItem(4, M('url'), { ad_break_id: 'C' })];
  const breaks = [{ id: 'A', refs: [SR(1), SR(2)], read_count: 2, span: [60, 121], brands: [] },
    { id: 'B', refs: [SR(3)], read_count: 1, span: [60, 121], brands: [] }, { id: 'C', refs: [SR(4)], read_count: 1, span: [60, 121], brands: [] }];
  const extra = { ad_breaks: breaks, break_total: 3, break_dated_total: 3, break_undated_total: 0 };
  for (const view of ['cards', 'list']) {
    const html = await searchWith(items, extra, view);
    assert.equal(count(html, P4_HTML(2)), 1, `${view}: ads A and C, not three pieces of evidence`);
    assert.ok(resultsOf(html).startsWith('<div id="search-results">' + P4_HTML(2)), `${view}: at the top of the results`);
  }
  // Without the server's ad breaks each result is its own ad.
  const flat = await searchWith(items.map(x => ({ ...x, ad_break_id: undefined })), {}, 'list');
  assert.equal(count(flat, P4_HTML(3)), 1);
});

test('P4 Brand page: counts only the Recent ads rows shown, never the reads past the list', async () => {
  // Seven reads; Recent ads shows the five newest. Two shown and two
  // past the list are model-named.
  const reads = [
    brandRead(1, '2026-10-09', 'a', M(), {}), brandRead(2, '2026-10-08', 'b', N(), {}), brandRead(3, '2026-10-07', 'c', M('url'), {}),
    brandRead(4, '2026-10-06', 'd', N(), {}), brandRead(5, '2026-10-05', 'e', N(), {}),
    brandRead(6, '2026-09-01', 'f', M(), {}), brandRead(7, '2026-08-01', 'g', M(), {})];
  const html = await render('#/brand/quince', brandApi(reads));
  assert.equal(count(recentOf(html), MARK), 2, 'two shown rows carry the label');
  assert.equal(count(html, P4_HTML(2)), 1, 'P4 counts the two shown, not four');
  // Under the Recent ads heading, right before its list.
  assert.ok(html.indexOf(P4_HTML(2)) > html.indexOf('id="reads-h"'));
  assert.ok(html.includes(P4_HTML(2) + '<ul class="ew-card lib-reads">'));
  const none = await render('#/brand/quince', brandApi(reads.map((r, i) => (i < 5 ? brandRead(i + 1, r.pub_date, 'x', N(), {}) : r))));
  assert.doesNotMatch(none, /ew-model-count/, 'model reads only past the list: no line');
});

test('P4 episode page: counts ads, once per ad, under the Ads in this episode heading', async () => {
  // Ad 1: two model-named pieces of evidence (one ad); ad 2: plain; ad 3: a model url read.
  const ep = epReads([[epAd('e1', [60, 125], M()), epAd('e2', [70, 130], M())], [epAd('e3', [400, 460], N())], [epAd('e4', [900, 960], M('url'))]]);
  const html = await render('#/episode/ep_f1', epApi(ep));
  assert.equal(count(html, MARK), 3, 'each model row carries the label');
  assert.equal(count(html, P4_HTML(2)), 1, 'two of the three ads');
  assert.ok(html.indexOf(P4_HTML(2)) > html.indexOf('id="ew-ads-h"'));
  assert.ok(html.indexOf(P4_HTML(2)) < html.indexOf('class="ew-ad-rows'));
  const one = await render('#/episode/ep_f1', epApi(epReads([[epAd('e3', [400, 460], N())], [epAd('e4', [900, 960], M('url'))]])));
  assert.equal(count(one, P4_HTML(1)), 1);
  const none = await render('#/episode/ep_f1', epApi(epReads([[epAd('e3', [400, 460], N())]])));
  assert.doesNotMatch(none, /ew-model-count/);
});

/* -- the words rule: nothing that sounds checked next to a model name ------ */
// "Near" = the block that holds the label, tip or summary, and the block
// on each side of it (blocks split at p, li, h1-h3, div, figcaption, tr).
function nearText(html, needle) {
  const blocks = html.split(/(?=<(?:p|li|h[1-3]|div|figcaption|tr|header)[\s>])|(?<=<\/(?:p|li|h[1-3]|div|figcaption|tr|header)>)/);
  const out = [];
  // Tags read as spaces, so a word right after a tag still has its word boundary.
  blocks.forEach((blk, i) => { if (blk.includes(needle)) out.push(blocks.slice(Math.max(0, i - 1), i + 2).join('').replace(/<[^>]*>/g, ' ')); });
  return out;
}
test('no confirmed, verified, checked, possible, likely or suggested next to a model-named brand, on any surface', async () => {
  assert.ok(SURFACES.length >= 9, 'every surface ran: ' + SURFACES.map(s => s.name).join(', '));
  for (const { name, renderWith } of SURFACES) {
    for (const channel of ['spoken', 'url']) {
      const html = await renderWith(M(channel), {});
      const near = [...nearText(html, MARK), ...nearText(html, 'ew-reader-named'), ...nearText(html, 'ew-model-count')];
      assert.ok(near.length, `${name}: the label is on the page`);
      for (const words of near) assert.doesNotMatch(words, BANNED, `${name} (${channel}): ${words.slice(0, 160)}`);
    }
  }
  for (const words of [P1, P2, P3, P4(1), P4(7)]) assert.doesNotMatch(words, BANNED);
});

/* -- PODS part 2: reads with two model names (read.model_names) ------------- */
// Fresh's shape (binding, 2026-10-28): read.model_names [{key, display,
// model_channel}] in the importer's order, present only on a read with a
// model name; read.brand stays the first name. Every name gets its own
// label (and on ad detail its own tip); no name is "the" brand.
const TWO = [{ key: 'quince', display: 'Quince', model_channel: 'spoken' }, { key: 'linkedin', display: 'LinkedIn', model_channel: 'url' }];
const two = (over = {}) => ({ model_names: TWO, ...over });
// The name, then (after its own closing tags) its own label.
const labelled = (html, name) => new RegExp(name + '(?:</[a-z0-9]+>)*' + MARK.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).test(html);
// Surfaces whose rows print the brand name (the Brand page's rows and
// compared ads print where/when, under the page's own brand: one label).
const NAMED = { 'search card': 2, 'search row': 2, 'search page': 4, overview: 2, 'brand page': 2, 'episode page': 2, 'ad detail': 2, 'pod rows': 2, 'alert matches': 2 };

test('two names: every surface labels each name on its own, in the server order', async () => {
  assert.ok(SURFACES.length >= 9);
  for (const { name, renderWith } of SURFACES) {
    const html = await renderWith(M('spoken'), two());
    assert.equal(count(html, MARK), NAMED[name], `${name}: one label per shown name`);
    if (name === 'brand page') continue;
    for (const n of ['Quince', 'LinkedIn']) assert.ok(labelled(html, n), `${name}: ${n} carries its own label`);
    assert.ok(textOf(html).indexOf('LinkedIn') > textOf(html).indexOf('Quince'), `${name}: server order, no name promoted`);
    assert.doesNotMatch(html, /model_names|model_channel|muse-spark/, `${name}: no field names in the markup`);
  }
});

test('two names: under the LinkedIn filter the card names both, each labelled, never Quince alone', async () => {
  const item = { ...searchItem(M()), read: { ...searchItem(M()).read, ...two() } };
  const html = await render('#/search?mode=ads&brands=linkedin', searchApi(item));
  const card = html.slice(html.indexOf('<div id="search-results">'), html.indexOf('<aside'));
  assert.ok(labelled(card, 'Quince') && labelled(card, 'LinkedIn'), 'both names labelled on the card');
  assert.equal(count(card, MARK), 2);
  const panel = asideOf(html);
  assert.ok(labelled(panel, 'Quince') && labelled(panel, 'LinkedIn'), 'both names labelled in the panel heading');
  assert.match(panel, /<th scope="row">Brand name<\/th><td>Quince · LinkedIn<\/td>/, 'the panel facts name both');
  assert.match(html, /Selected: Quince · LinkedIn, Show one/, 'the panel announcement names both');
});

test('two names on ad detail: each name its own tip by its own channel (Quince P2, LinkedIn P3)', async () => {
  for (const historical of [false, true]) {
    const page = await evidence(evRecord(evRead(M('spoken'), two()), { historical }));
    const tips = page.match(/<p class="ew-reader-named">[\s\S]*?<\/p>/g) || [];
    assert.deepEqual(tips.map(textOf), [`Quince: ${P2}`, `LinkedIn: ${P3}`], `historical ${historical}`);
    assert.match(page, /<h1 class="ew-reader-h1"><span class="ew-brand-unit">Quince <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span> · <span class="ew-brand-unit">LinkedIn <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span><\/h1>/);
  }
  // Two names on one channel still get one tip each.
  const both = await evidence(evRecord(evRead(M('url'), { model_names: TWO.map(n => ({ ...n, model_channel: 'url' })) })));
  assert.deepEqual((both.match(/<p class="ew-reader-named">[\s\S]*?<\/p>/g) || []).map(textOf), [`Quince: ${P3}`, `LinkedIn: ${P3}`]);
});

test('two names: the episode transcript tag names both', async () => {
  const ep = epReads([[epAd('e1', [900, 905], M(), two())]]);
  const html = await render('#/episode/ep_f1', epApi(ep));
  assert.match(html, /Ad · Quince · LinkedIn/);
});

test('model_names absent, or one name, renders as before; never on a read that is not model-named', async () => {
  for (const { name, renderWith } of SURFACES) {
    const plain = await renderWith(M('spoken'), {});
    assert.equal(await renderWith(M('spoken'), { model_names: [TWO[0]] }), plain, `${name}: one name = the brand alone`);
    const other = await renderWith(N(), {});
    assert.equal(await renderWith(N(), two()), other, `${name}: a non-model brand ignores the list`);
    assert.equal(await renderWith(HELD, two()), await renderWith(HELD, {}), `${name}: a held brand ignores the list`);
  }
});

test('P4 counts ads, not names: a two-name ad counts once', async () => {
  const twoItem = n => sItem(n, M(), { read: { ...searchItem(M()).read, ...two() } });
  const html = await searchWith([twoItem(1), sItem(2, N()), sItem(3, M('url'))]);
  assert.equal(count(html, P4_HTML(2)), 1);
  const ep = await render('#/episode/ep_f1', epApi(epReads([[epAd('e1', [60, 125], M(), two())], [epAd('e3', [400, 460], N())]])));
  assert.equal(count(ep, P4_HTML(1)), 1);
  const brandPage = await render('#/brand/quince', brandApi([brandRead(1, '2026-10-09', 'a', M(), two()), brandRead(2, '2026-10-08', 'b', N(), {})]));
  assert.equal(count(brandPage, P4_HTML(1)), 1);
});

/* -- model_only on Brands directory rows and Compare rows -------------------- */
// model_only: true when every read the viewer can see for the brand is
// model-named; absent otherwise. Mixed brands get no brand-level label.
const DIR = [
  { key: 'nordvpn', brand: 'nordvpn', display: 'NordVPN', records: 2, ad_breaks: 2, shows: 1, last_heard: '2026-10-09', model_only: true },
  { key: 'quince', brand: 'quince', display: 'Quince', records: 5, ad_breaks: 4, shows: 2, last_heard: '2026-10-08' },
  { key: 'casper', brand: 'casper', display: 'Casper', records: 1, ad_breaks: 1, shows: 1, last_heard: '2026-10-07', model_only: false }];
const dirApi = brands => p => (p === '/customer/brands' ? { brands } : p.startsWith('/customer/saved-items') ? { items: [], next_cursor: null } : undefined);
const rowOf = (html, name) => (html.match(new RegExp(`<tr><th scope="row"[^>]*>(?:(?!</th>).)*>${name}</a>(?:(?!</tr>).)*</tr>`)) || [''])[0];

test('Brands directory: the label after a model_only brand only; mixed and absent rows unchanged', async () => {
  const html = await render('#/brands', dirApi(DIR));
  assert.match(rowOf(html, 'NordVPN'), /<a class="lib-name" href="#\/brand\/nordvpn">NordVPN<\/a> <span class="ew-brand-mark">Named by Muse Spark<\/span><span class="lib-row-meta">/);
  assert.equal(count(rowOf(html, 'Quince'), 'Named by Muse Spark'), 0, 'mixed brand: no brand-level label');
  assert.equal(count(rowOf(html, 'Casper'), 'Named by Muse Spark'), 0, 'model_only false is not true');
  assert.equal(count(html, MARK), 1);
  const plain = await render('#/brands', dirApi(DIR.map(({ model_only, ...b }) => b)));
  // Part 4 (UX): the model_only row has no Track; the plain one's is taken out before comparing.
  assert.equal(html.split(MARK).join(''), plain.replace(/<button[^>]*data-action="track-brand" data-brand="nordvpn"[\s\S]*?<\/button>/, ''), 'nothing else changes');
  assert.doesNotMatch(plain, PODS_MARKUP);
});

const cmpCell = (n, extra = {}) => ({ count: n, ads: n, refs: [], coverage: { matched_in_scope: n }, shows: 1, last_heard: '2026-10-09', ...extra });
const cmpApi = rows => p => (p === '/customer/brands' ? { brands: DIR } : p.startsWith('/customer/compare?') ? { cells: rows } : undefined);
test('Compare: the label after a model_only brand row only', async () => {
  const rows = [{ brand: 'nordvpn', model_only: true, a: cmpCell(2) }, { brand: 'quince', a: cmpCell(4) }];
  const html = await render('#/compare?brands=nordvpn,quince', cmpApi(rows));
  assert.match(html, /<tr><th scope="row">NordVPN <span class="ew-brand-mark">Named by Muse Spark<\/span><\/th>/);
  assert.match(html, /<tr><th scope="row">Quince<\/th>/, 'mixed brand: no label');
  assert.equal(count(html, MARK), 1);
  const plain = await render('#/compare?brands=nordvpn,quince', cmpApi(rows.map(({ model_only, ...r }) => r)));
  assert.equal(html.split(MARK).join(''), plain);
  assert.doesNotMatch(plain, PODS_MARKUP);
});

test('no banned word next to a brand-level label', async () => {
  const pages = [await render('#/brands', dirApi(DIR)),
    await render('#/compare?brands=nordvpn,quince', cmpApi([{ brand: 'nordvpn', model_only: true, a: cmpCell(2) }, { brand: 'quince', a: cmpCell(4) }]))];
  for (const html of pages) {
    const near = nearText(html, MARK);
    assert.ok(near.length, 'the brand-level label is on the page');
    for (const words of near) assert.doesNotMatch(words, BANNED, words.slice(0, 160));
  }
});

/* -- PODS part 3: the brand page of a model_only brand; alert pages --------- */
// brand_detail.model_only (Fresh, after its next confirm round): true when
// every read the viewer can see for the brand is model-named; absent
// otherwise. UX 2026-10-28: P1 after the name in the header, as on the
// Brands rows, and no Track (tracking such a brand saves nothing).
const brandPageApi = (over, reads) => p => (p === '/customer/brands/quince' ? { brand: { ...BRAND.brand, ...over } } : brandApi(reads)(p));
const headOf = html => (html.match(/<header class="lib-head">[\s\S]*?<\/header>/) || [''])[0];

test('Brand page, model_only: the label after the name in the header and no Track', async () => {
  const reads = [brandRead(1, '2026-10-09', 'a', M(), {}), brandRead(2, '2026-10-08', 'b', M('url'), {})];
  const html = await render('#/brand/quince', brandPageApi({ model_only: true }, reads));
  const head = headOf(html);
  assert.match(head, /<h1 class="lib-title">Quince <span class="ew-brand-mark">Named by Muse Spark<\/span><\/h1>/);
  assert.doesNotMatch(head, /track-brand|lib-track/, 'no Track control');
  assert.doesNotMatch(html, /lib-track-failed/, 'no note about a hidden Track');
  // Tracking status failed to load: a model_only page still says nothing about Track.
  const failing = p => (p.startsWith('/customer/saved-items') ? new Error('down') : brandPageApi({ model_only: true }, reads)(p));
  const failed = await render('#/brand/quince', failing);
  assert.match(headOf(failed), /Named by Muse Spark/);
  assert.doesNotMatch(failed, /lib-track-failed|track-brand/, 'no Track and no Track note when tracking status fails');
  const plainFailed = await render('#/brand/quince', p => (p.startsWith('/customer/saved-items') ? new Error('down') : brandPageApi({}, reads)(p)));
  assert.match(plainFailed, /lib-track-failed/, 'a plain brand keeps its note');
  assert.match(head, /compare\?brands=quince/, 'Compare stays');
  assert.doesNotMatch(head, /alert-brand/, 'part 4: no Create alert for a brand only Muse Spark names');
  for (const words of nearText(head, MARK)) assert.doesNotMatch(words, BANNED);
  // Absent or false: the header is the page as before (Track shown, no label).
  const plain = await render('#/brand/quince', brandPageApi({}, reads));
  assert.match(headOf(plain), /data-action="track-brand"/);
  assert.doesNotMatch(headOf(plain), /ew-brand-mark/);
  assert.equal(await render('#/brand/quince', brandPageApi({ model_only: false }, reads)), plain, 'false is not true');
});

// Alert pages: model names never make events, but a match row whose read
// is model-named carries the read fields (labelled as on every read card),
// and the live "Current matches" box shows counts only, never a brand name.
test('Alert page: model-named match rows are labelled; Current matches names no brand', async () => {
  const alert = (read) => ({ id: 1, name: 'Sheets', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
    last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', brands: ['quince'] },
    current_matches: { count: 2, count_units: 'distinct_results', refs: ['sr1:0', 'sr1:1'], truncated: false, tracked: 2, tracked_by_kind: { new_finding: 2 }, breaks: 2, held_for_brand_review: 0 },
    events: [{ id: 1, kind: 'new_finding', after_ref: 'sr1:0', read_state: 0, historical: false, observed_at: '2026-10-14T16:27:59+00:00',
      summary: { record_kind: 'evidence', status: 'finding', episode_id: 'ep0', show_title: 'NPR', ep_title: '12am', pub_date: '2026-10-09',
        span: [250, 309], text: 'Linen sheets.', historical: false, read } }] });
  const api = a => p => (p.startsWith('/customer/alerts/1') ? { alert: a } : p === '/customer/shows' ? { shows: [] } : p === '/customer/brands' ? { brands: [] } : undefined);
  const html = await render('#/alert/1', api(alert({ brand: M('spoken'), ...two(), needs_review: false, self_promo: false, historical: false })));
  const rows = (html.match(/<ul class="ew-card-list ew-work-events">[\s\S]*?<\/ul>/) || [''])[0];
  assert.ok(labelled(rows, 'Quince') && labelled(rows, 'LinkedIn'), 'each name on the match row carries its label');
  const box = (html.match(/<h3>Current matches<\/h3>[\s\S]*?<\/section>/) || [''])[0];
  assert.ok(box, 'the Current matches box renders');
  assert.doesNotMatch(box, /Quince|LinkedIn|ew-brand/, 'counts only: no brand name to label');
  const plain = await render('#/alert/1', api(alert({ brand: N(), needs_review: false, self_promo: false, historical: false })));
  assert.doesNotMatch(plain, PODS_MARKUP);
});


/* -- PODS part 4 (UX 2026-10-28): actions and labels on model-named cards --- */
// 1. Create alert only for names that are not model-named; hidden when every
//    name on the card is model-named. 2. No Track on a model_only Brands row.
// 3. The preview button names every name. 4. A Search ad break whose reads
//    are all on the page and all model-named takes its header names from
//    them (display only, labelled, in order; never counted, never in brands).
test('Create alert: hidden on the Search panel and ad detail when every name is model-named; as before otherwise', async () => {
  for (const [what, b, r] of [['one model name', M(), {}], ['two model names', M(), two()]]) {
    const html = await render('#/search?mode=ads&q=sleep', searchApi({ ...searchItem(b), read: { ...searchItem(b).read, ...r } }));
    assert.doesNotMatch(asideOf(html), /alert-brand/, `panel, ${what}`);
    const page = await evidence(evRecord(evRead(b, r)));
    assert.doesNotMatch(page, /data-action="alert-evidence"/, `ad detail, ${what}`);
  }
  const plain = await render('#/search?mode=ads&q=sleep', searchApi(searchItem(N())));
  assert.match(asideOf(plain), /data-action="alert-brand" data-id="quince"/, 'a non-model card keeps it');
  assert.match(await evidence(evRecord(evRead(N()))), /data-action="alert-evidence" data-id="[^"]*" data-brand="quince"/, 'ad detail keeps it');
});

test('Brands directory: no Track on a model_only row; mixed and false rows keep it; the cell stays', async () => {
  const html = await render('#/brands', dirApi(DIR));
  assert.doesNotMatch(rowOf(html, 'NordVPN'), /track-brand/, 'model_only row: no Track');
  assert.match(rowOf(html, 'NordVPN'), /<td class="lib-track-cell"><\/td><\/tr>$/, 'its cell stays, empty');
  for (const n of ['Quince', 'Casper']) assert.match(rowOf(html, n), /data-action="track-brand"/, `${n} keeps Track`);
});

test('the List row preview button (and its Open evidence label) names every name; one name and other brands as before', async () => {
  const { win } = loadApp();
  const row = item => win.EWUI.resultRow(item, { index: 0 });
  const label = (item, what) => (row(item).match(new RegExp(`aria-label="(${what}[^"]*)"`)) || [])[1];
  const twoItem = { ...searchItem(M()), read: { ...searchItem(M()).read, ...two() } };
  assert.match(label(twoItem, 'Preview '), /^Preview Quince · LinkedIn(,|$)/);
  assert.match(label(twoItem, 'Open evidence: '), /^Open evidence: Quince · LinkedIn(,|$)/);
  assert.match(label(searchItem(M()), 'Preview '), /^Preview Quince(,|$)/);
  assert.equal(label(searchItem(N()), 'Preview '), label(searchItem(M()), 'Preview '), 'one name: same label as a plain brand');
});

/* Item 4: Search ad-break headers. */
const brkItems = (reads, id = 'A') => reads.map(([n, b, r = {}], i) => sItem(n, b, { ad_break_id: id, read: { ...searchItem(b).read, display_span: [60 + i, 121], ...r } }));
const brk = (id, refs, over = {}) => ({ id, refs, read_count: refs.length, span: [60, 121], brands: [], ...over });
const breakSearch = (items, breaks, view = 'cards') => searchWith(items, { ad_breaks: breaks, break_total: breaks.length, break_dated_total: breaks.length, break_undated_total: 0 }, view);
const headsOf = html => html.match(/<h3 class="ew-break-title"[^>]*>[\s\S]*?<\/h3>/g) || [];

test('ad-break header: a break whose reads are all here and all model-named shows their names, each labelled, in order', async () => {
  // Break A: a two-name read then a one-name url read (Casper); every read is on the page.
  const items = brkItems([[1, M(), two()], [2, M('url'), { model_names: [{ key: 'casper', display: 'Casper', model_channel: 'url' }] }]])
    .map((x, i) => (i === 1 ? { ...x, read: { ...x.read, brand: { ...M('url'), key: 'casper', display: 'Casper' } } } : x));
  for (const view of ['cards', 'list']) {
    const breaks = [brk('A', [SR(1), SR(2)])];
    const html = await breakSearch(items, breaks, view);
    const head = headsOf(html)[0];
    // FIX1 (PAV-R1-03): each name and its label in one unit, so the label never wraps alone.
    const unit = n => `<span class="ew-break-brand"><span class="ew-brand-unit"><b>${n}</b>${MARK}</span></span>`;
    assert.equal(head, '<h3 class="ew-break-title" id="brk-h-0">' + unit('Quince') + unit('LinkedIn') + unit('Casper') + '</h3>', view);
    assert.match(html, /<span class="ew-break-count">Evidence \(2\)<\/span>/, `${view}: the count is the server's`);
    assert.deepEqual(breaks, [brk('A', [SR(1), SR(2)])], `${view}: the break data is not changed (no names added to brands)`);
    assert.equal(count(html, P4_HTML(1)), 1, `${view}: P4 still counts one ad`);
  }
});

test('ad-break header: stays "Brand not identified" when the page lacks some of the break, or a read is not model-named; a named break is unchanged', async () => {
  const one = brkItems([[1, M()]]);
  // Only one of the break's two reads is on the page (the other did not match).
  const partial = await breakSearch(one, [brk('A', [SR(1)], { read_count: 2, matching_read_count: 1 })]);
  assert.match(headsOf(partial)[0], /<span class="ew-brand-none">Brand not identified<\/span>/, 'partial break: no guess');
  const mixed = await breakSearch(brkItems([[1, M()], [2, N()]]), [brk('A', [SR(1), SR(2)])]);
  assert.match(headsOf(mixed)[0], /Brand not identified/, 'a read that is not model-named: no names from reads');
  const held = await breakSearch(brkItems([[1, M()], [2, { ...HELD }]]), [brk('A', [SR(1), SR(2)])]);
  assert.match(headsOf(held)[0], /Brand not identified/, 'a held read: no names');
  const named = brk('A', [SR(1)], { brands: [{ key: 'casper', display: 'Casper' }] });
  const html = await breakSearch(one, [named]);
  assert.equal(headsOf(html)[0], '<h3 class="ew-break-title" id="brk-h-0"><span class="ew-break-brand"><b>Casper</b></span></h3>', 'a break with a brand keeps it alone');
});

/* -- FIX1 (r1 reviews PAC/PAS/PAV-R1; UX 2026-10-28) ------------------------- */
const dataOf = tag => Object.fromEntries([...tag.matchAll(/\sdata-([a-z-]+)="([^"]*)"/g)]
  .map(([, k, v]) => [k.replace(/-([a-z])/g, (_, c) => c.toUpperCase()), v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')]));
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
// The read cases a campaign screen may get from the display search.
const CAMPAIGN_CASES = [['one name, spoken', M(), {}], ['one name, url', M('url'), {}], ['two names', M(), two()]];

/* Item 1 (PAC-R1-01 = PAS-R1-01 = PAV-R1-01): a placement's "Attached
   sources" and "Suggested evidence" rows, and the Retain, Reference, Move
   and Detach labels taken from them, say "Brand not identified" for a
   model-named read (FUNC-1 decision 2). Other brands are named as before. */
const WIN = 'exp:f3ba15d41424';
const AREF = 'sr1:1474b30c1d0e4a5f8b2c3d4e5f607182';
const PREF = 'sr1:644cc4006f8e4d3aa1b2c3d4e5f60718';
const placementDraft = () => ({
  campaign: { id: 1, revision: 4, name: 'Quince Q4 host reads' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [
    { id: WIN, scope: 'show_window', show_id: 's1', date_from: '2026-06-18', date_to: '2026-10-09', expected_count: 2, revision: 1, binding: 'eligible', provenance: { binding: 'eligible' } },
    { id: 'exp:other', scope: 'episode', show_id: 's1', episode_id: 'ep5', expected_count: 1, revision: 1, binding: 'eligible', provenance: { binding: 'eligible' } },
  ],
  targets: [], episode_titles: { ep5: 'The Last 12 Weeks - Ep. 5' },
  coverage_by_placement: {}, comparisons: {},
  allocation: { evaluations: {}, citations: {} }, reason_codes: { evaluations: {}, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: {}, limitations: [] },
  pins: { attachments: { [WIN]: [AREF] }, attachment_state: { [AREF]: { evidence_revision: 3, source_pins: { episode_id: 'ep5' } } } },
  attachment_pool: [PREF],
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'],
});
const placementItem = (ref, b, r = {}) => ({ ref, show_title: 'Serial', ep_title: 'The Last 12 Weeks - Ep. 5', pub_date: '2026-09-02',
  quotation_span: [61, 80], read: { brand: b, needs_review: false, display_span: [62, 95], ...r } });
const placementPage = (b, r = {}) => render('#/campaign/1/placement/' + encodeURIComponent(WIN), p => (p === '/customer/campaigns/1/draft' ? { draft: placementDraft() }
  : p.startsWith('/customer/search?') ? { items: JSON.parse(new URLSearchParams(p.split('?')[1]).get('query')).ids.map(ref => placementItem(ref, b, r)) } : undefined));
const sourceRows = html => [...html.matchAll(/<ul class="ew-card-list ew-work-sources">([\s\S]*?)<\/ul>/g)].map(m => m[1]);
const opLabels = row => [...row.matchAll(/<button\b[^>]*data-(?:op|action)="(retain|reference|move|detach-evidence)"[^>]*>/g)].map(m => [m[1], dataOf(m[0]).label]);

test('FIX1 placement: an attached or suggested model-named read says "Brand not identified", and so do its Retain, Reference, Move and Detach labels', async () => {
  for (const [what, b, r] of CAMPAIGN_CASES) {
    const html = await placementPage(b, r);
    const [attached, pool] = sourceRows(html);
    assert.ok(attached && pool, `${what}: both lists render`);
    for (const [list, row] of [['attached', attached], ['suggested', pool]]) {
      assert.match(row, /<div class="ew-work-source-head"><strong>Brand not identified<\/strong>/, `${what}, ${list}: the campaign wording`);
      assert.doesNotMatch(visible(row), /Quince|LinkedIn|Named by Muse Spark/, `${what}, ${list}: no model name and no label`);
    }
    const ops = opLabels(attached);
    assert.deepEqual(ops.map(o => o[0]), ['retain', 'reference', 'move', 'detach-evidence'], `${what}: the four operations stay`);
    for (const [op, label] of ops) assert.equal(label, 'Brand not identified · Serial · The Last 12 Weeks - Ep. 5', `${what}: ${op} label`);
  }
  // Any other brand is named as before.
  const plain = await placementPage(N());
  const [attached, pool] = sourceRows(plain);
  for (const row of [attached, pool]) assert.match(row, /<div class="ew-work-source-head"><strong><span class="ew-brand">Quince<\/span><\/strong>/);
  for (const [op, label] of opLabels(attached)) assert.equal(label, 'Quince · Serial · The Last 12 Weeks - Ep. 5', `plain: ${op} label`);
});

/* Item 1: the ad detail "Add to campaign" button carries no brand name for a
   model-named read, so the Add/Remove-from-campaign confirm (and its Retry)
   names it "Ad at {time}", never by the model's name. */
test('FIX1 ad detail: Add to campaign passes no brand name for a model-named read; other brands keep theirs', async () => {
  const campaignTag = html => (html.match(/<button\b[^>]*data-action="add-to-campaign"[^>]*>/) || [''])[0];
  for (const [what, b, r] of CAMPAIGN_CASES) {
    const tag = campaignTag(await evidence(evRecord(evRead(b, r))));
    assert.ok(tag, `${what}: the button renders`);
    const data = dataOf(tag);
    assert.equal(data.brandName, undefined, `${what}: no brand name for the campaign confirm`);
    assert.equal(data.at, '2:00', `${what}: the confirm names the ad by its time`);
  }
  assert.equal(dataOf(campaignTag(await evidence(evRecord(evRead(N()))))).brandName, 'Quince', 'another brand keeps its name');
});

/* Item 1: a brand-check episode's reads (FUNC-2) name the read's brand; a
   model-named read names none (its time only). */
const FUNC2 = path.join(__dirname, 'fixtures', 'func2', 'episode-verdicts.json');
const func2Page = (b, r = {}) => {
  const draft = JSON.parse(fs.readFileSync(FUNC2, 'utf8')).draft;
  const read = draft.episode_verdicts['pl-win'].episodes[0].reads[0];
  Object.assign(read, { brand: b }, r);
  return render('#/campaign/1/placement/pl-win', p => (p === '/customer/campaigns/1' ? { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Fern Q4 host reads', brand: 'fern', requirement_provenance: {}, allowed_actions: draft.allowed_actions } }
    : p === '/customer/campaigns/1/draft' ? { draft } : p === '/customer/campaigns/1/reports' ? { reports: [] } : undefined));
};
const func2Read = html => { const at = html.indexOf('data-read="ev-10"'); return html.slice(html.lastIndexOf('<li class="ew-ep-read', at), html.indexOf('</li>', at)); };
test('FIX1 brand-check episode reads: a model-named read names no brand', async () => {
  for (const [what, b, r] of CAMPAIGN_CASES) {
    const row = func2Read(await func2Page(b, r));
    assert.ok(row.includes('ew-ep-read-name'), `${what}: the read keeps its time`);
    assert.doesNotMatch(row, /ew-ep-read-brand|Quince|LinkedIn|Named by Muse Spark/, `${what}: no model name`);
  }
  assert.match(func2Read(await func2Page(N())), /<span class="ew-ep-read-brand">Quince<\/span>/, 'another brand keeps its name');
});

/* Item 2 (lead PODS-6): no "What podcasts say about {brand}" on a model_only
   brand page, as with Track. Other brand pages keep it, unchanged. */
test('FIX1 brand page: no Ask button when model_only; kept otherwise', async () => {
  const reads = [brandRead(1, '2026-10-09', 'a', M(), {})];
  const ASK = /href="#\/ask\?brand=Quince"[^>]*>What podcasts say about Quince</;
  const only = await render('#/brand/quince', brandPageApi({ model_only: true, ask_button: true }, reads), { ask: true });
  assert.doesNotMatch(headOf(only), /#\/ask\?|What podcasts say/, 'model_only: no Ask');
  assert.match(headOf(only), /compare\?brands=quince/, 'Compare stays');
  for (const over of [{}, { model_only: false }]) {
    assert.match(headOf(await render('#/brand/quince', brandPageApi({ ...over, ask_button: true }, reads), { ask: true })), ASK, `${JSON.stringify(over)}: Ask stays`);
  }
});

/* Item 3 (PAC-R1-02 = PAS-R1-02): hostile names (the security probe's) stay
   text on the new two-name print sites. */
const H1 = 'Q"><img src=x onerror=alert(1)>&<script>alert(2)</script>\'x';
const H2 = "L'><svg/onload=alert(5)>`${7*7}";
const E1 = 'Q&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&amp;&lt;script&gt;alert(2)&lt;/script&gt;&#39;x';
const E2 = 'L&#39;&gt;&lt;svg/onload=alert(5)&gt;`${7*7}';
const HOSTILE_TWO = [{ key: 'k" onmouseover="alert(3)', display: H1, model_channel: 'spoken' }, { key: 'javascript:alert(4)', display: H2, model_channel: 'url' }];
const HM = () => brand('model', { key: HOSTILE_TWO[0].key, display: H1 });
const RAW = /<img\b|<script\b|<svg\/onload|"\s*onmouseover=/i;
test('FIX1 escaping: the ad-break header names from reads', async () => {
  for (const view of ['cards', 'list']) {
    const html = await breakSearch(brkItems([[1, HM(), { model_names: HOSTILE_TWO }]]), [brk('A', [SR(1)])], view);
    const head = headsOf(html)[0] || '';
    assert.ok(head.includes(`<b>${E1}</b>`) && head.includes(`<b>${E2}</b>`), `${view}: both names, escaped`);
    assert.doesNotMatch(html, RAW, `${view}: no raw markup`);
  }
});
test('FIX1 escaping: the two-name ad detail title and each tip\'s name', async () => {
  const html = await evidence(evRecord(evRead(HM(), { model_names: HOSTILE_TWO })));
  const h1 = (html.match(/<h1 class="ew-reader-h1">[\s\S]*?<\/h1>/) || [''])[0];
  assert.ok(h1.includes(E1) && h1.includes(E2), 'title: both names, escaped');
  const tips = html.match(/<p class="ew-reader-named">[\s\S]*?<\/p>/g) || [];
  assert.equal(tips.length, 2);
  assert.ok(tips[0].startsWith(`<p class="ew-reader-named"><b>${E1}</b>: `), 'first tip lead, escaped');
  assert.ok(tips[1].startsWith(`<p class="ew-reader-named"><b>${E2}</b>: `), 'second tip lead, escaped');
  assert.doesNotMatch(html, RAW, 'no raw markup');
});
test('FIX1 escaping: the List row Preview and Open evidence labels', () => {
  const { win } = loadApp();
  const row = win.EWUI.resultRow({ ...searchItem(HM()), read: { ...searchItem(HM()).read, model_names: HOSTILE_TWO } }, { index: 0 });
  assert.doesNotMatch(row, RAW, 'no raw markup');
  for (const what of ['Preview ', 'Open evidence: ']) {
    const label = (row.match(new RegExp(`aria-label="(${what}[^"]*)"`)) || [])[1] || '';
    assert.ok(label.startsWith(`${what}${E1} · ${E2}`), `${what}: both names, escaped`);
  }
});

/* Item 4 (PAC-R1-03): a held brand never gets the label, even when it
   carries a model display basis and a name. */
test('FIX1 a held model-named brand has no label, tip or summary on any surface', async () => {
  const heldModel = { ...brand('model'), under_review: true };
  assert.ok(SURFACES.length >= 9);
  for (const { name, renderWith } of SURFACES) {
    assert.doesNotMatch(await renderWith(heldModel, {}), PODS_MARKUP, name);
    assert.doesNotMatch(await renderWith(heldModel, two()), PODS_MARKUP, `${name}, two names`);
  }
});

/* PAV-R1-04 (UX 2026-10-28): both tips' Report a mistake go to the same
   control (goto-report, no target of its own), so a two-name ad shows the
   button once, after the last tip; each tip still says the words. */
test('FIX1 two names on ad detail: one Report a mistake button, after both tips; each tip keeps the words', async () => {
  const html = await evidence(evRecord(evRead(M(), two())));
  const tips = html.match(/<p class="ew-reader-named">[\s\S]*?<\/p>/g) || [];
  assert.equal(tips.length, 2);
  assert.equal(textOf(tips[0]), 'Quince: ' + P2);
  assert.equal(textOf(tips[1]), 'LinkedIn: ' + P3);
  assert.doesNotMatch(tips[0], /<button/, 'the first tip names it in plain words');
  assert.match(tips[1], /use <button type="button" class="text-link" data-action="goto-report">Report a mistake<\/button>\.<\/p>$/, 'the button ends the last tip');
  assert.equal(count(tips.join(''), 'data-action="goto-report"'), 1);
  // One name: its tip keeps the button.
  assert.match((await evidence(evRecord(evRead(M())))).match(TIP_RE)[0], /data-action="goto-report"/);
});

/* PAV-R1-03: each name and its label stay together (one unit that does not
   break between them) in the ad-break header and the panel (and 390 sheet)
   heading; the CSS keeps the name itself free to wrap. */
test('FIX1 a name and its label form one unit in the ad-break header, the panel heading and every brandsHtml surface', async () => {
  const html = await render('#/search?mode=ads&q=sleep', searchApi({ ...searchItem(M()), read: { ...searchItem(M()).read, ...two() } }));
  assert.match(asideOf(html), new RegExp('<h2><span class="ew-brand-unit"><span class="ew-brand">Quince</span>' + MARK + '</span> · <span class="ew-brand-unit"><span class="ew-brand">LinkedIn</span>' + MARK + '</span></h2>'));
  const plain = await render('#/search?mode=ads&q=sleep', searchApi(searchItem(N())));
  assert.match(asideOf(plain), /<h2><span class="ew-brand">Quince<\/span><\/h2>/, 'a plain brand is unchanged');
  // Cards and rows too: every model name through brandsHtml is one unit.
  assert.match(html, new RegExp('<span class="ew-brand-unit"><span class="ew-brand">Quince</span>' + MARK + '</span> · <span class="ew-brand-unit"><span class="ew-brand">LinkedIn</span>'));
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  assert.match(css, /\.ew-brand-unit \{ display: inline-flex;[^}]*align-items: last baseline;[^}]*max-width: 100%; \}/);
  assert.match(css, /\.ew-brand-unit > :not\(\.ew-brand-mark\) \{ min-width: 0; overflow-wrap: anywhere; \}/);
  assert.match(css, /\.ew-brand-unit > \.ew-brand-mark \{ flex: none; \}/);
});
