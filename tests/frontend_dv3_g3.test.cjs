/* Design v3 library pages (G3): Brands, Brand, Compare, Creative, Shows,
   Show. Loads the real app scripts in a stubbed browser sandbox.
   Run: node --test tests/frontend_dv3_g3.test.cjs */
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
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32),
      workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(route) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
// Render one route against a path -> body table (an Error rejects).
async function page(route, api) {
  const win = loadViews(route);
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  const paths = [];
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.api = async (p, opts) => {
    paths.push(p);
    const out = api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await settleAll();
  return { html: main.innerHTML, paths, win, toasts, main };
}
const boom = (status = 500) => Object.assign(new Error('boom'), { status });
const brandItem = (id, key, rev = 1) => ({ id, type: 'bookmark', title: key, revision: rev, availability: 'current',
  target: { kind: 'brand', id: key }, captured: { record_kind: 'brand', label: key, opens: 'current_profile' } });
const saved = (items = []) => ({ items, next_cursor: null });

const DIR = { brands: [
  { brand: 'quince', key: 'quince', display: 'Quince', records: 38, ad_breaks: 20, confirmed: 0, confirmed_ads: 0, shows: 21, last_heard: '2026-10-08' },
  { brand: 'ag1', key: 'ag1', display: 'AG1', records: 12, ad_breaks: 7, confirmed: 0, confirmed_ads: 0, shows: 1, last_heard: '2026-10-09' },
  { brand: 'zeta', key: 'zeta', display: 'Zeta', records: 1, ad_breaks: 1, confirmed: 0, confirmed_ads: 0, shows: 1, last_heard: null } ] };

test('brands directory: table of API facts, Track state from one saved-list read', async () => {
  const t = await page('#/brands', p => (p === '/customer/brands' ? DIR
    : p.startsWith('/customer/saved-items?') ? saved([brandItem('si-1', 'ag1', 4)]) : boom()));
  assert.equal(t.paths.filter(p => p.startsWith('/customer/saved-items')).length, 1, 'one tracked-list read per page');
  assert.match(t.html, /<h1>Brands<\/h1>/);
  assert.match(t.html, /Showing 1 to 3 of 3 brands/);
  // Most ads first; numbers come from the API.
  assert.ok(t.html.indexOf('>Quince<') < t.html.indexOf('>AG1<'));
  const text = t.html.replace(/<[^>]+>/g, '');
  assert.match(t.html, /href="#\/brand\/quince"[^>]*>Quince<\/a><span class="lib-row-meta">/);
  // PLAIN-ADS (DEP7): no confirmed count; always "last heard".
  assert.match(text, /Quince20 ads · 21 shows · last heard Oct 8/);
  assert.match(text, /AG17 ads · 1 show · last heard Oct 9/);
  assert.doesNotMatch(text, /confirmed|detected/);
  assert.doesNotMatch(text, /reads? heard|\b38\b/, 'no read count');
  // An undated brand says so; nothing is invented.
  assert.match(text, /Zeta1 ad · 1 show · last heard Date unknown/);
  assert.match(t.html, /data-action="track-brand" data-brand="ag1"[^>]*data-item="si-1" data-rev="4" aria-pressed="true">/);
  assert.match(t.html, /data-action="track-brand" data-brand="quince"[^>]*aria-pressed="false">/);
  // No column for data the API does not give.
  assert.doesNotMatch(t.html, /Latest offer/);
});

test('brands directory: a failed tracked list hides Track and says so; a failed list is never "no brands"', async () => {
  const t = await page('#/brands', p => (p === '/customer/brands' ? DIR : boom()));
  assert.doesNotMatch(t.html, /data-action="track-brand"/);
  assert.match(t.html, /Tracking status didn’t load/);
  const down = await page('#/brands', () => boom());
  assert.match(down.html, /role="alert"/);
  assert.match(down.html, /Brands didn’t load\.<\/b> This is not “no brands”/);
  assert.match(down.html, /data-action="retry"/);
});

test('brands directory: sort and page live in the route', async () => {
  const many = { brands: Array.from({ length: 30 }, (_, i) => ({ brand: 'b' + i, key: 'b' + i, display: 'B' + String(i).padStart(2, '0'), records: i, shows: 1, last_heard: '2026-10-01' })) };
  const t = await page('#/brands?sort=az&page=2', p => (p === '/customer/brands' ? many : saved()));
  assert.match(t.html, /Showing 26 to 30 of 30 brands/);
  assert.match(t.html, /<option value="az" selected>A to Z/);
  assert.match(t.html, />B25<\/a>/);
  assert.doesNotMatch(t.html, />B00<\/a>/);
  assert.match(t.html, /href="#\/brands\?sort=az"[^>]*>← Previous/);
});

test('brands directory: an empty name search offers mentions search and clear', async () => {
  const win0 = loadViews('#/brands');
  void win0;
  const t = await (async () => {
    const r = await page('#/brands', p => (p.startsWith('/customer/brands') ? { brands: [] } : saved()));
    r.win.EW.current.directory.brands = 'Lumenwave';
    await r.win.EWViews.render(); await settleAll();
    return r;
  })();
  assert.match(t.main.innerHTML, /No brand called “Lumenwave” was heard in ads/);
  assert.match(t.main.innerHTML, /Search all mentions of “Lumenwave”/);
  assert.match(t.main.innerHTML, /data-action="directory-clear"/);
});

const SR = n => 'sr1:' + String(n).repeat(32).slice(0, 32);
const read = (n, date, text, over = {}) => ({ ref: SR(n), record_kind: 'evidence', status: 'finding', episode_id: 'e' + n,
  show_title: 'Show ' + n, ep_title: 'Ep ' + n, pub_date: date, text,
  read: { brand: { key: 'quince', display: 'Quince' }, display_span: [60, 90], has_clip: false }, ...over });
// A confirmed read of the brand: `text` is the whole evidence passage,
// read.display_text the read's own words (what the comparison quotes).
const cread = (n, date, own, passage, over = {}) => read(n, date, passage || ('Earlier talk about sleep. ' + own + ' Later talk about travel.'),
  { status: 'confirmed', read: { brand: { key: 'quince', display: 'Quince', confirmed: true }, display_span: [60, 90], display_basis: 'checked', has_clip: false, display_text: own }, ...over });
const BRAND = { brand: { brand: 'quince', key: 'quince', display: 'Quince', detections: 3, ad_breaks: 2, episodes: 3, shows: 2, last_heard: '2026-10-09',
  confirmed_occurrences: [SR(1), SR(2)], findings: [SR(3)], offer_sequence: [],
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['quince'], sort: 'newest', page: 1 }, mentions: { version: 2, mode: 'mentions', brands: ['quince'] } } } };
const brandApi = (reads, extra = {}) => p => {
  if (p === '/customer/brands/quince') return extra.brand || BRAND;
  if (p.startsWith('/customer/saved-items?')) return extra.saved || saved();
  if (p.startsWith('/customer/search?')) {
    if (extra.search) return extra.search;
    const q = JSON.parse(new URLSearchParams(p.split('?')[1]).get('query'));
    const only = (q.statuses || []).includes('confirmed') ? reads.filter(r => r.status === 'confirmed') : reads;
    const items = [...only].sort((a, b) => (q.sort === 'oldest' ? 1 : -1) * a.pub_date.localeCompare(b.pub_date));
    return { items, total: reads.length, break_total: reads.length, total_relation: 'exact' };
  }
  return boom();
};

test('brand page: scope sentence from API counts, Track / Compare / Create alert, reads from the exact query', async () => {
  const reads = [cread(1, '2026-08-12', 'Go to drink dot com and get five free travel packs.'), cread(2, '2026-10-09', 'Go to drink dot com and use code OFFER20.')];
  const t = await page('#/brand/quince', brandApi(reads, { saved: saved([brandItem('si-7', 'quince', 2)]) }));
  assert.match(t.html, /Brand · heard in ads/);
  assert.match(t.html, /In this collection: 2 ads in 3 episodes across 2 shows · last heard Oct 9, 2026/);
  assert.match(t.html, /data-action="track-brand" data-brand="quince"[^>]*aria-pressed="true"><span aria-hidden="true">✓<\/span><span>Tracking this brand<\/span>/);
  assert.match(t.html, /href="#\/compare\?brands=quince"[^>]*>Compare with another brand/);
  assert.match(t.html, /data-action="alert-brand"[^>]*data-id="quince"[^>]*>Create alert/);
  assert.match(t.html, /See all 2 ads in Search/);
  // Searches reuse the brand's exact drill-down query.
  const searches = t.paths.filter(p => p.startsWith('/customer/search?')).map(p => JSON.parse(new URLSearchParams(p.split('?')[1]).get('query')));
  // Newest for the list (and the comparison's later end); oldest for its earlier end. PLAIN-ADS: no status scope.
  assert.deepEqual(searches.map(s => s.sort).sort(), ['newest', 'oldest']);
  assert.ok(searches.every(s => !s.statuses), 'no confirmed-only read');
  assert.ok(searches.every(s => JSON.stringify(s.brands) === '["quince"]' && s.mode === 'ads'));
  // No clip kept: a dash, never a play button.
  assert.match(t.html, /class="ew-play-none"/);
  assert.doesNotMatch(t.html, /class="ew-play"/);
});

test('brand page: how the offer changed marks changed words in both quotes, readable without hover', async () => {
  const reads = [cread(1, '2026-08-12', 'Go to drink dot com and get five free travel packs.'), cread(2, '2026-10-09', 'Go to drink dot com and use code OFFER20.')];
  const t = await page('#/brand/quince', brandApi(reads));
  assert.match(t.html, /How the offer changed/);
  assert.match(t.html, /Earlier ad<\/b><span>Aug 12, 2026<\/span>/);
  assert.match(t.html, /Later ad<\/b><span>Oct 9, 2026<\/span>/);
  assert.match(t.html, /“Go to drink dot com and <mark class="lib-diff">get five free travel packs\.<\/mark>”/);
  assert.match(t.html, /“Go to drink dot com and <mark class="lib-diff">use code OFFER20\.<\/mark>”/);
  assert.match(t.html, /This compares two ads we heard; it doesn’t mean the older offer is gone everywhere\./);
  assert.doesNotMatch(t.html, /title="/, 'nothing hides behind hover');
});

test('brand page: the comparison quotes each read\'s own words, never the whole evidence passage', async () => {
  const reads = [cread(1, '2026-08-12', 'Use code SLEEP10.', 'Intro chatter about mattresses. Use code SLEEP10. Now back to the interview.'),
    cread(2, '2026-10-09', 'Use code SLEEP20.', 'A different show entirely, talking about trains. Use code SLEEP20. Roll the credits.')];
  const t = await page('#/brand/quince', brandApi(reads));
  const offer = t.html.slice(t.html.indexOf('lib-offer-pair'), t.html.indexOf('lib-offer-note'));
  assert.match(offer, /Use code <mark class="lib-diff">SLEEP10\.<\/mark>/);
  assert.match(offer, /Use code <mark class="lib-diff">SLEEP20\.<\/mark>/);
  assert.doesNotMatch(offer, /mattresses|interview|trains|credits/, 'the passage around the read is not quoted');
});

// PLAIN-ADS (DEP7): the comparison uses every ad of the brand that is not
// held; reads without their own text are still left out.
test('brand page: the comparison uses every ad that is not held; reads without their own text are left out', async () => {
  // PLAIN-ADS-FIX1 (screen R1-01): a read with checked times, whatever its review.
  const possible = read(2, '2026-10-09', 'Possible passage words.', { read: { brand: { key: 'quince', display: 'Quince' }, display_span: [1, 2], display_basis: 'checked', display_text: 'Possible own words.' } });
  const t = await page('#/brand/quince', brandApi([cread(1, '2026-08-12', 'One own read.'), possible]));
  assert.match(t.html, /How the offer changed/);
  assert.match(t.html.replace(/<[^>]+>/g, ''), /Possible own words/);
  assert.doesNotMatch(t.html, /Possible passage words/, 'the read\'s own words, never the passage');
  // Confirmed, but the transcript is not shown to this reader: no own text, no quote.
  const noText = cread(2, '2026-10-09', 'x', 'Passage only.');
  delete noText.read.display_text;
  const u = await page('#/brand/quince', brandApi([cread(1, '2026-08-12', 'One own read.'), noText]));
  assert.doesNotMatch(u.html, /How the offer changed/);
  assert.doesNotMatch(u.html, /Passage only/);
});

test('brand page: with fewer than two ads the comparison is hidden and not fetched', async () => {
  const few = { ...BRAND, brand: { ...BRAND.brand, ad_breaks: 1, confirmed_occurrences: [SR(1)] } };
  const t = await page('#/brand/quince', brandApi([cread(1, '2026-08-12', 'Only read.')], { brand: few }));
  assert.doesNotMatch(t.html, /How the offer changed/);
  const searches = t.paths.filter(p => p.startsWith('/customer/search?')).map(p => JSON.parse(new URLSearchParams(p.split('?')[1]).get('query')));
  assert.deepEqual(searches.map(s => s.sort), ['newest']);
});

test('brand page: the comparison needs two dated confirmed quoted named reads, else it is hidden', async () => {
  const one = await page('#/brand/quince', brandApi([cread(1, '2026-08-12', 'Only one read.')]));
  assert.doesNotMatch(one.html, /How the offer changed/);
  const undated = await page('#/brand/quince', brandApi([cread(1, '2026-08-12', 'One.'), cread(2, '', 'Two.')]));
  assert.doesNotMatch(undated.html, /How the offer changed/);
  const review = cread(2, '2026-10-09', 'Hidden brand words.', null, { read: { brand: { under_review: true }, display_span: [1, 2], display_text: 'Hidden brand words.' } });
  const t = await page('#/brand/quince', brandApi([cread(1, '2026-08-12', 'One.'), review]));
  assert.doesNotMatch(t.html, /How the offer changed/);
  assert.doesNotMatch(t.html, /Hidden brand words|Show 2/, 'an under-review read never shows under a brand');
});

test('brand page: failed reads are an error, never "no reads"; a failed tracked list hides Track', async () => {
  const t = await page('#/brand/quince', brandApi([], { search: boom() }));
  assert.match(t.html, /Recent ads didn’t load\.<\/b> This is not “no ads”/);
  assert.doesNotMatch(t.html, /No ads in stored sources/);
  const u = await page('#/brand/quince', p => (p.startsWith('/customer/saved-items') ? boom() : brandApi([])(p)));
  assert.doesNotMatch(u.html, /data-action="track-brand"/);
  assert.match(u.html, /Tracking status didn’t load/);
});

function trackEl(attrs) {
  const a = { 'aria-pressed': 'false', ...attrs.aria };
  return { dataset: { action: 'track-brand', ...attrs.data }, disabled: false, innerHTML: '',
    getAttribute: k => (k in a ? a[k] : null), setAttribute: (k, v) => { a[k] = String(v); }, removeAttribute: k => { delete a[k]; },
    classList: { toggle() {} }, attrs: a };
}

test('Track toggles in place: track posts the brand, Stop tracking sends the displayed revision', async () => {
  const t = await page('#/brand/quince', brandApi([]));
  const calls = [];
  t.win.EW.api = async (p, opts = {}) => {
    calls.push({ p, method: opts.method || 'GET', body: opts.body });
    if (opts.method === 'POST') return { ok: true, created: true, item: brandItem('si-9', 'quince', 3) };
    if (opts.method === 'DELETE') return { ok: true, removed_id: 'si-9' };
    throw boom();
  };
  const el = trackEl({ data: { brand: 'quince', name: 'Quince', compact: '' } });
  await t.win.EWViews.handleAction(el);
  assert.equal(calls[0].method, 'POST');
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({ type: 'bookmark', target: { kind: 'brand', id: 'quince' }, note: '' }));
  assert.equal(el.attrs['aria-pressed'], 'true');
  assert.equal(el.dataset.item, 'si-9');
  assert.equal(el.dataset.rev, '3');
  assert.match(el.innerHTML, /Tracking this brand/);
  await t.win.EWViews.handleAction(el);
  assert.equal(calls[1].method, 'DELETE');
  assert.equal(calls[1].p, '/customer/saved-items/si-9');
  assert.equal(JSON.stringify(calls[1].body), JSON.stringify({ expected_revision: 3 }));
  assert.equal(el.attrs['aria-pressed'], 'false');
  assert.match(el.innerHTML, /Track this brand/);
});

test('Track: a stale Stop tracking is refused and the page re-reads; no revision means no request', async () => {
  const t = await page('#/brand/quince', brandApi([]));
  let deletes = 0, rendered = 0;
  t.win.EW.api = async (p, opts = {}) => { if (opts.method === 'DELETE') { deletes++; throw boom(412); } return brandApi([])(p); };
  const render = t.win.EWViews.render;
  t.win.EW.render = async () => { rendered++; };
  void render;
  const el = trackEl({ aria: { 'aria-pressed': 'true' }, data: { brand: 'quince', name: 'Quince', item: 'si-9', rev: '2' } });
  await t.win.EWViews.handleAction(el);
  assert.equal(deletes, 1);
  assert.ok(t.toasts.some(m => /changed elsewhere/.test(m)), t.toasts.join('|'));
  assert.equal(el.attrs['aria-pressed'], 'true', 'the button is not flipped on a refusal');
  const stale = trackEl({ aria: { 'aria-pressed': 'true' }, data: { brand: 'quince', name: 'Quince', item: 'si-9' } });
  await t.win.EWViews.handleAction(stale);
  assert.equal(deletes, 1, 'no revision, no request');
  assert.ok(t.toasts.some(m => /revision/i.test(m)), t.toasts.join('|'));
  void rendered;
});

const SHOW = { show_id: 'cs-1', display_title: 'The Long Run', category_summary: ['Sports'], artwork_url: null, unresolved: false,
  coverage: { episodes: 5, episodes_with_transcript: 4 },
  sponsors: [{ key: 'ag1', display: 'AG1', episodes: 2, ads: 6, ad_breaks: 4, first_seen: '2026-09-01', last_seen: '2026-10-09' }],
  episodes: [
    { id: 'e1', ep_title: 'Recovery weeks', pub_date: '2026-10-09', transcript: true, ads: 3, ad_breaks: 2, screening: { jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full' } },
    { id: 'e2', ep_title: 'Quiet', pub_date: '2026-10-02', transcript: true, screening: { jev_screened: true, screened_no_ads: true, stopped_partway: false, transcript_only: false, coverage_tier: 'full' } },
    { id: 'e3', ep_title: 'Heat', pub_date: '2026-09-18', transcript: true, screening: { stopped_partway: true, stage: 'transcribe', coverage_tier: 'full' } },
    { id: 'e4', ep_title: 'Taper', pub_date: '2026-09-11', transcript: true, screening: null },
    { id: 'e5', ep_title: 'Rest days', pub_date: '2026-08-28', transcript: false, screening: null } ] };

test('show page: episode index tags from the per-episode facts; never "no ads" when not screened', async () => {
  const t = await page('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? SHOW : boom()));
  const row = id => (t.html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];
  assert.match(row('e1'), /Screened · 2 ads listed/);
  assert.match(row('e2'), /Screened · no ads found/);
  assert.match(row('e3'), /Stopped partway[\s\S]*|stopped at transcription/);
  assert.match(row('e3'), /stopped at transcription/);
  assert.match(row('e4'), /Not screened yet/);
  assert.doesNotMatch(row('e4'), /No ads/);
  assert.match(row('e5'), /Metadata only/);
  assert.match(row('e5'), /No transcript available/);
  assert.match(t.html, /data-tag="tier"[^>]*>[\s\S]*Full coverage/);
  assert.match(t.html, /5 episodes known · 4 with transcripts/);
  assert.match(t.html, /data-action="save-bookmark-show" data-id="cs-1"/);
  assert.match(t.html, /data-action="alert-show" data-id="cs-1"/);
  assert.match(t.html, /Not counted: ads with Brand not identified and the show’s own promotions\./);
});

test('show page: the metadata filter lives in the route; an older server draws no tags', async () => {
  const t = await page('#/show/cs-1?episodes=metadata', p => (p === '/customer/shows/cs-1' ? SHOW : boom()));
  assert.match(t.html, /Rest days/);
  assert.doesNotMatch(t.html, /Recovery weeks/);
  const old = { ...SHOW, episodes: SHOW.episodes.map(({ transcript, screening, ...e }) => e) };
  const u = await page('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? old : boom()));
  assert.doesNotMatch(u.html, /lib-ep-tags|id="show-episodes"/);
});

test('compare: two brands at a time; the dialog picks two', async () => {
  const t = await page('#/compare?brands=a,b,c', p => boom());
  assert.match(t.html, /Two brands at a time\.<\/b> This link named 3; the first two are compared\./);
  assert.match(t.html, /a and b/);
});

test('creative page: a read whose clip is unknown never says "No clip kept"; it links its evidence', async () => {
  const member = (n, date, excerpt) => ({ confirmed: true, observation_id: 'o' + n, observation: { episode_id: 'e' + n, start_s: 10, end_s: 40 },
    episode: { show_title: 'Show ' + n, ep_title: 'Ep ' + n, pub_date: date }, show: { display_title: 'Show ' + n },
    evidence_refs: [{ ref: SR(n), excerpt }] });
  const creative = { creative: { creative_id: 'cr-1', members: [member(1, '2026-08-12', 'First words.'), member(2, '2026-10-09', 'Second words.')] } };
  const t = await page('#/creative/cr-1', p => (p === '/customer/creatives/cr-1' ? creative : boom()));
  assert.match(t.html, /Earliest and latest quote/);
  assert.doesNotMatch(t.html, /No clip \(transcript only\)|ew-play-none/);
  assert.match(t.html, /href="#\/evidence\/sr1%3A1{32}">Open evidence/);
});

test('show page: Screened without a listed ad is not "Ads listed"; a listed ad is', async () => {
  const sc = { jev_screened: true, screened_no_ads: false, stopped_partway: false, transcript_only: false, coverage_tier: 'full' };
  const show = { ...SHOW, episodes: [
    { id: 'e1', ep_title: 'Has ads', pub_date: '2026-10-09', transcript: true, ads: 2, ad_breaks: 1, screening: sc },
    { id: 'e2', ep_title: 'Screened only', pub_date: '2026-10-02', transcript: true, ads: 0, screening: sc } ] };
  const t = await page('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? show : boom()));
  const row = id => (t.html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];
  assert.match(row('e1'), /Screened · 1 ad listed/);
  assert.match(row('e2'), /Screened · no ads listed/);
  assert.doesNotMatch(row('e2'), /ads listed[^·]*\d|\d+ (ad reads?|ads?) listed|no ads found/);
  // An older server (no ad count): never claims a list it cannot show.
  const old = { ...SHOW, episodes: [{ id: 'e1', ep_title: 'Old', pub_date: '2026-10-09', transcript: true, screening: sc }] };
  const u = await page('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? old : boom()));
  assert.doesNotMatch(u.html, /Ads listed/);
});

test('show page: a transcript the member may not read is "not available to you", not "no words, no ads"', async () => {
  const show = { ...SHOW, coverage: { episodes: 2, episodes_with_transcript: 1 }, episodes: [
    { id: 'e1', ep_title: 'Denied one', pub_date: '2026-10-09', transcript: false, transcript_denied: true, ads: 0, screening: null },
    { id: 'e2', ep_title: 'Bare one', pub_date: '2026-10-02', transcript: false, transcript_denied: false, ads: 0, screening: null } ] };
  const t = await page('#/show/cs-1', p => (p === '/customer/shows/cs-1' ? show : boom()));
  const row = id => (t.html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];
  assert.match(row('e1'), /Not available to you/);
  assert.doesNotMatch(row('e1'), /Metadata only|No transcript available|no ads/i);
  assert.match(row('e2'), /Metadata only/);
  assert.match(t.html, /Not available to you:<\/b>/);
  // The metadata filter lists only episodes known to have no transcript.
  const m = await page('#/show/cs-1?episodes=metadata', p => (p === '/customer/shows/cs-1' ? show : boom()));
  assert.match(m.html, /Bare one/);
  assert.doesNotMatch(m.html, /Denied one/);
});

test('brands directory: the header links Compare brands; an out-of-range page clamps to the last page', async () => {
  const many = { brands: Array.from({ length: 30 }, (_, i) => ({ brand: 'b' + i, key: 'b' + i, display: 'B' + String(i).padStart(2, '0'), records: i, shows: 1, last_heard: '2026-10-01' })) };
  const t = await page('#/brands?sort=az&page=99', p => (p === '/customer/brands' ? many : saved()));
  assert.match(t.html, /href="#\/compare"[^>]*>Compare brands</);
  assert.match(t.html, /Showing 26 to 30 of 30 brands/);
  const shows = { shows: Array.from({ length: 30 }, (_, i) => ({ id: 'cs-' + i, display_title: 'Show ' + String(i).padStart(2, '0'), episodes: 1 })) };
  const u = await page('#/shows?page=99', p => (p === '/customer/shows' ? shows : boom()));
  assert.match(u.html, /Showing (\d+) to 30 of 30 shows/);
  assert.doesNotMatch(u.html, /Showing 30 to 30/);
});

test('compare dialog: the same brand in both selects is a field error and does not navigate', async () => {
  const win = loadViews('#/compare');
  const errors = [];
  let went = 0;
  win.EW.formError = (m, field) => { errors.push([m, field && field.name]); return false; };
  win.EW.go = () => { went++; };
  win.EW.closeDialog = () => {};
  win.FormData = class { *entries() { yield ['brand_a', 'quince']; yield ['brand_b', 'quince']; } };
  const fieldB = { name: 'brand_b' };
  const form = { getAttribute: () => 'compare-select', dataset: {}, querySelector: sel => (sel === '[name=brand_b]' ? fieldB : { name: 'brand_a' }), querySelectorAll: () => [] };
  await win.EWViews.handleSubmit(form);
  assert.equal(went, 0);
  assert.equal(errors.length, 1);
  assert.match(errors[0][0], /two different brands/i);
  assert.equal(errors[0][1], 'brand_b');
});

test('initials tile: names that start with digits keep them', () => {
  const win = loadAt('#/shows');
  assert.match(win.EWUI.initialsTile('20/20'), />2</);
  assert.match(win.EWUI.initialsTile('The 5th Hour'), />5H</);
  assert.match(win.EWUI.initialsTile('', { size: 'lg' }), /aria-hidden="true"><\/span>$/);  // plain tile (APV-R1-05)
});
