/* d16 row 1 vocabulary guard (worker d1fa): render every customer page
   of Search, Episode and Evidence from fixtures, plus the public home and
   coverage pages, and fail on a read/detection/ad-break/pod/spot noun in
   visible text and in aria-label/title attributes. d1fb's guard
   (frontend_uat_d1_vocab_b.test.cjs) uses the same BANNED regex and ALLOW
   list over its pages. Allowed: the verb phrases in ALLOW, "Ad break · "
   labels over 2+ ads of one overlap group (group_ad_count >= 2), and text
   inside the Evidence page's Technical details.
   Run: node --test tests/frontend_uat_d1_vocab.test.cjs */
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

const BANNED = /\b(reads?|detections?|ad breaks?|pods?|spots?)\b/i;
// Verb phrases, each a full phrase (the verb "read", never the unit).
const ALLOW = [
  'read-only', // a view that cannot change anything
  'Mark all read', // alerts: mark events as seen
  'Web address read out', // fact row: the address was spoken
  'Promo code read out', // fact row: the code was spoken
  'the brand’s code or address was read out', // name certainty gloss
  'not read automatically', // row 13 copy
  'alerts read it', // coverage tier: alerts scan the transcript
];
const decode = s => s.replace(/&nbsp;/g, ' ').replace(/&middot;/g, '·').replace(/&rsquo;/g, '’').replace(/&ldquo;|&rdquo;/g, '"')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
// "Ad break · <range> · N ads" over N >= 2 ads is the one allowed label;
// on Search, "Ad break · N brands" over N >= 2 brands (NOGAP-AB, DET-1).
const AD_BREAK_LABEL = /<h3 class="ew-adbreak-h">Ad break [\s\S]*?\s(\d+) (?:ads|brands)<\/h3>/g;
function surfaces(html, { evidence = false } = {}) {
  let h = html.replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/g, ' ');
  h = h.replace(AD_BREAK_LABEL, (m, n) => (Number(n) >= 2 ? ' ' : m));
  if (evidence) h = h.replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ');
  const attrs = [...h.matchAll(/\s(?:aria-label|title)="([^"]*)"/g)].map(m => m[1]);
  const meta = [...h.matchAll(/<meta name="description" content="([^"]*)"/g)].map(m => m[1]);
  const visible = h.replace(/<[^>]+>/g, ' ');
  return [visible, ...attrs, ...meta].map(decode);
}
function offenders(html, opts) {
  const out = [];
  for (let s of surfaces(html, opts)) {
    s = s.replace(/\s+/g, ' ');
    for (const phrase of ALLOW) s = s.split(new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi')).join(' ');
    const re = new RegExp(BANNED.source, 'gi');
    for (const m of s.matchAll(re)) out.push(s.slice(Math.max(0, m.index - 50), m.index + 40).trim());
  }
  return out;
}
const clean = (name, html, opts) => assert.deepEqual(offenders(html, opts), [], `${name}: banned noun on a customer page`);

test('vocab guard: the guard itself catches the unit and spares the allowed phrases', () => {
  assert.deepEqual(offenders('<p>Web address read out · Mark all read · alerts read it</p>'), []);
  assert.ok(offenders('<p>3 reads</p>').length > 0);
  assert.ok(offenders('<button aria-label="Ad break, 2 reads">x</button>').length > 0, 'attributes are checked');
  assert.ok(offenders('<h3 class="ew-adbreak-h">Ad break · 1:00 · 1 ads</h3>').length > 0, 'never over one ad');
  assert.ok(offenders('<h3>Ad break · 1:00 · 2 ads</h3>').length > 0, 'only the group label element');
  assert.deepEqual(offenders('<h3 class="ew-adbreak-h">Ad break · 1:00 · 2 ads</h3>'), []);
  assert.ok(offenders('<details class="tech-details"><p>read 5</p></details>').length > 0, 'only the Evidence page spares Technical details');
  assert.deepEqual(offenders('<details class="tech-details"><p>read 5</p></details>', { evidence: true }), []);
});
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


const bundleOf = (mode, items, extra = {}) => ({ query: { version: 2, mode, q: 'x', sort: 'newest', page: 1 }, revision: 'r', selector: null, coverage: { episodes_in_scope: 3 },
  result: { items, total: items.length, dated_total: items.length, undated_total: 0, total_relation: 'exact', per_page: 10, ...extra }, summary: { week: [], status: [] } });

test('vocab guard: Search (Ads cards and list, lower bound, older server, empty, mentions, episodes, rail)', async () => {
  const { items, breaks } = pageOne();
  const sel = key => ({ version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'summary_cell', group_by: 'week', key } });
  const week = [{ key: '2026-09-07', label: 'w1', count: 3, breaks: 2, interval: { from: '2026-09-07', to: '2026-09-13' }, selector: sel('2026-09-07') },
    { key: 'unknown', label: 'Date unknown', count: 1, breaks: 1, selector: sel('unknown') }];
  const full = searchBundle({ items, breaks, total: 3, breakTotal: 2, status: [{ key: 'confirmed', count: 1, breaks: 1 }, { key: 'finding', count: 2, breaks: 1 }] });
  full.summary.week = week;
  const cases = [
    ['ads cards', '#/search?mode=ads&q=chatgpt', full, true],
    ['ads list', '#/search?mode=ads&q=chatgpt&view=list', searchBundle({ items, breaks, total: 3, breakTotal: 2, view: 'list' }), false],
    ['ads lower bound', '#/search?mode=ads&q=chatgpt', searchBundle({ items, breaks, total: 60, breakTotal: 25, perPage: 2, relation: 'lower_bound', extra: { truncation_reason: 'max_rows' } }), false],
    ['ads older server', '#/search?mode=ads&q=chatgpt', searchBundle({ items, total: 3 }), false],
    ['ads empty', '#/search?mode=ads&q=nothing', searchBundle({ items: [], breaks: [], total: 0, breakTotal: 0 }), false],
    ['ads no query', '#/search?mode=ads', { ...full, query: { ...full.query, q: '' } }, true],
    ['mentions', '#/search?mode=mentions&q=x', bundleOf('mentions', [{ ref: 'tx:1', record_kind: 'transcript_passage', text: 'x', episode_id: 'e1', show_title: 'S', pub_date: '2026-10-09' }]), false],
    ['episodes', '#/search?mode=episodes&q=x', bundleOf('episodes', [{ ref: 'ep:e1', record_kind: 'episode', episode_id: 'e1', ep_title: 'One', show_title: 'S', pub_date: '2026-10-09', matching_passages: 2, indexed_passages: 9 }]), false],
  ];
  for (const [name, route, bundle, wide] of cases) {
    const { html } = await render(route, searchApi(bundle), { wide });
    assert.ok(html.length > 200, name + ' rendered');
    clean('Search ' + name, html);
  }
});

test('vocab guard: Episode (ad break group, earlier copy, no copy, promo, stopped, transcript only, not screened, older server)', async () => {
  const rec = epRecord([{ segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 },
    { segment_id: 2, start_s: 900, end_s: 910, text: 'Lowes.', text_start: 0, text_end: 6 }]);
  const promo = epAd('p1', [2000, 2030], 'Show A', { self_promo: true, self_promo_reason: 'own-name:Show A' });
  const mixed = epReads({ self_promo: [promo],
    ads: [...epReads().ads, epAd('o1', [3400, 3480], 'Progressive', { transcript_version_id: OLD, on_current_transcript: false }),
      epAd('n1', [1000, 1060], 'Acme', { transcript_version_id: null, on_current_transcript: null, ref: null })],
    earlier_transcripts: [{ transcript_version_id: OLD, duration_s: 3544, fetch_date: '2026-09-21', fetch_date_basis: 'fetch_date' }],
    ad_breaks: [...epReads().ad_breaks, ad('brk:o', [3400, 3480], ['o1'], 'brk:o', 1, { transcript_version_id: OLD, on_current_transcript: false }),
      ad('brk:n', [1000, 1060], ['n1'], 'brk:n', 1, { transcript_version_id: null })],
    ad_break_counts: { current: 3, earlier: 1, no_copy: 1 } });
  const older = epReads(); delete older.ad_breaks; delete older.ad_break_counts;
  const empty = over => epReads({ ads: [], ad_breaks: [], ad_break_counts: { current: 0, earlier: 0, no_copy: 0 }, ...over });
  const cases = [
    ['group', epReads()], ['mixed', mixed], ['older server', older],
    ['stopped', empty({ screening: { ...SCREENED, jev_screened: false, stopped_partway: true, stage: 'detect' } })],
    ['transcript only', empty({ screening: { ...SCREENED, jev_screened: false, transcript_only: true, coverage_tier: 'transcript', label: 'Transcript only' } })],
    ['not screened', empty({ screening: { ...SCREENED, jev_screened: false } })],
    ['no ads', empty({ screened_no_ads: true })],
  ];
  for (const [name, reads] of cases) {
    const { html } = await episodePage(reads, rec);
    assert.ok(/Ads in this episode/.test(html), name + ' rendered');
    clean('Episode ' + name, html);
  }
  const failed = await render('#/episode/ep_d1', p => (p === '/customer/episodes/ep_d1' ? new Error('boom') : { record: rec }));
  clean('Episode failed list', failed.html);
});

test('vocab guard: Evidence (multi, single, confirmed checked, earlier copy, historical, under review, passage)', async () => {
  const base = evRecord(null);
  const cases = [
    ['multi', evRecord(ad3(1))],
    ['single', evRecord({ id: 'brk:1', span: [100, 150], read_count: 1, index: 0, refs: ['a2'], brands: [], on_current_transcript: true, group_id: 'brk:1', group_ad_count: 1 })],
    ['confirmed checked', evRecord(null, { status: 'confirmed', review: { judgment: 'ad', bounds_verified: 1 }, attribution: [{ brand: 'ag1', basis: 'confirmed', channel: 'spoken' }],
      read: { ...base.read, kind: 'rule', confidence: 'high', display_basis: 'checked', brand: { ...base.read.brand, confirmed: true }, said: { spoken_brand: 'AG1', web_addresses: ['ag1.com'], address_brand: 'AG1', promo_codes: ['AG10'] } } })],
    ['needs review', evRecord(null, { read: { ...base.read, needs_review: true, confidence: 'low' } })],
    ['earlier copy', evRecord(null, { episode: { ...base.episode, transcript_is_current: false, transcript_duration_s: 3545, current_transcript_duration_s: 3324 } })],
    ['historical', evRecord(null, { read: { historical: true, current_ref: 'sr1:' + 'c'.repeat(32) } })],
    ['under review', evRecord(null, { read: { ...base.read, brand: { under_review: true } } })],
    ['self promo', evRecord(null, { read: { ...base.read, self_promo: true, self_promo_reason: 'own-name:Show A' } })],
    ['local clip', evRecord(null, { media: { playback: 'local', alignment: 'verified', src: '/x' } })],
  ];
  for (const [name, rec] of cases) {
    const { html } = await evidencePage(rec);
    assert.ok(/data-reader="evidence"/.test(html), name + ' rendered');
    clean('Evidence ' + name, html, { evidence: true });
  }
  const passage = { ref: EVREF, record_kind: 'transcript_passage', status: 'mention', episode: base.episode, show: base.show, media: {},
    selected_bounds: { start_s: 100, end_s: 150 }, window: base.window, bounds_basis: 'full_effective_source' };
  const p = await evidencePage(passage);
  clean('Evidence passage', p.html, { evidence: true });
});

test('vocab guard: public home and coverage pages', () => {
  for (const file of ['index.html', 'coverage.html']) {
    clean('public ' + file, fs.readFileSync(path.join(APP, 'public', file), 'utf8'));
  }
});
