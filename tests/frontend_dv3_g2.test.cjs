/* Design v3 G2: Evidence and Episode pages. Product rules that must hold
   in the new layout: separate fact rows (heard brand, web address,
   address brand, code, brand name review, timing), disagreement shown
   not resolved, unknown stays unknown, brand under review hides every
   name fact, pods from timestamps only, origin-named back links, and
   the clip lead-in preference. Loader helpers copied from
   frontend_query.test.cjs. Run: node --test tests/frontend_dv3_g2.test.cjs */
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
const sessionFetch = (prefs = null) => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  if (prefs && url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: prefs, revision: 1 }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function render(route, api, { prefs = null, hosted = null, origin = null, onWin = null } = {}) {
  const win = loadViews(sessionFetch(prefs));
  if (onWin) onWin(win);
  await flushBoot();
  if (prefs) await win.EW.preferences();
  if (hosted !== null) win.EW.current.hosted = hosted;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const paths = [];
  win.EW.api = async (p, opts) => { paths.push(p); return api(p, opts); };
  win.location.hash = route;
  win.EW.current.route = route;
  if (origin) win.EW.current.back.set(route, origin);
  await win.EWViews.render();
  return { html: main.innerHTML, paths };
}
// Visible ledger rows: label -> cell html.
function ledger(html) {
  const rows = {};
  for (const m of html.matchAll(/<th scope="row">([^<]*)<\/th><td[^>]*>([\s\S]*?)<\/td><\/tr>/g)) rows[m[1]] = m[2];
  return rows;
}

const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const EV_ID = 'named:ep_g2.named.120000-180000';
const read = (over = {}) => ({ kind: 'named', source_label: 'Named read',
  brand: { key: 'northwind mattress', display: 'Northwind Mattress', display_basis: 'brand_list', suggested: 'northwind mattress', confirmed: true },
  needs_review: false, self_promo: false, self_promo_reason: null, confidence: 'high', display_span: [120, 170], has_clip: false,
  said: { web_addresses: ['restwell.com/quiet'], promo_codes: [], address_brand: 'Restwell', code_brand: null, spoken_brand: 'Northwind Mattress' },
  ...over });
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'confirmed',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [{ brand: 'northwind mattress', basis: 'confirmed', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_g2', show_title: 'Quiet Hours', ep_title: 'Why we wake at 3am', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_g2', orig_start_s: 110, orig_end_s: 180 }, media: { playback: 'remote', alignment: 'unknown' },
  read: read(),
  window: { segments: [
    { segment_id: 1, start_s: 110, end_s: 119, text: 'Let’s take a short break.', text_start: 0, text_end: 25 },
    { segment_id: 2, start_s: 120, end_s: 150, text: 'Head to restwell.com/quiet to find yours.', text_start: 0, text_end: 41 },
    { segment_id: 3, start_s: 171, end_s: 180, text: 'Okay, back to it.', text_start: 0, text_end: 17 }],
  text_complete: true, next_cursor: null }, ...over });
const evApi = (rec, episode = null) => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_g2' && episode) return { episode };
  throw new Error('unexpected ' + p);
};

test('the fact ledger keeps each fact in its own row and shows a disagreement without picking', async () => {
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(record()), { hosted: false });
  const rows = ledger(html);
  // PLAIN-ADS-FIX2 (screen R2-02): only found facts are listed; no code was read out, so no code row.
  assert.deepEqual(Object.keys(rows), ['Brand heard in the audio', 'Web address read out', 'Brand the address points to',
    'How the time was found']);
  assert.match(rows['Brand heard in the audio'], /^Northwind Mattress/);
  assert.match(rows['Web address read out'], /^restwell\.com\/quiet/);
  assert.match(rows['Brand the address points to'], /^Restwell/);
  assert.match(rows['Brand the address points to'], /Differs from what was heard\. We show both facts and don’t pick one\./);
  assert.match(html, /<tr class="is-uncertain"><th scope="row">Brand the address points to/);
  assert.doesNotMatch(html, /None found/);
  assert.match(rows['How the time was found'], /Found automatically/);
  assert.match(rows['How the time was found'], /Times from the transcript/);
  assert.doesNotMatch(html, /review team|Brand name review/, 'PLAIN-ADS: no review row');
  assert.match(html, /<h1 class="ew-reader-h1">Northwind Mattress<\/h1>/, 'the title keeps the read’s own brand');
  assert.match(html, /<span class="ew-said-url">restwell\.com\/quiet<\/span>/, 'the recorded web address is set apart in the passage');
});

test('a matching address brand says so; codes become tags and marked text', async () => {
  const rec = record({ read: read({ said: { web_addresses: ['northwind.com'], promo_codes: ['SLEEP20'], address_brand: 'Northwind Mattress', code_brand: null, spoken_brand: 'northwind mattress' } }),
    window: { segments: [{ segment_id: 2, start_s: 120, end_s: 150, text: 'Use code sleep20 at northwind.com', text_start: 0, text_end: 33 }], text_complete: true, next_cursor: null } });
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { hosted: false });
  const rows = ledger(html);
  assert.match(rows['Brand the address points to'], /^Northwind Mattress · matches what was heard/);
  assert.doesNotMatch(html, /is-uncertain/);
  assert.match(html, /data-tag="code">Code heard <b>SLEEP20<\/b>/);
  assert.match(html, /<span class="ew-said-code">sleep20<\/span>/);
});

test('unknown stays unknown: no said block lists no address or code row, never None found', async () => {
  const rec = record({ read: read({ said: undefined }) });
  delete rec.read.said;
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { hosted: false });
  const rows = ledger(html);
  // PLAIN-ADS-FIX2 (screen R2-02): what was not found is not listed.
  assert.deepEqual(Object.keys(rows).filter(k => /Web address|address points|Promo code/.test(k)), []);
  assert.doesNotMatch(html, /None found/);
});

test('a held brand shows no name fact anywhere on the page', async () => {
  const hidden = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
  const rec = record({ status: 'finding', attribution: [], read: read({ brand: hidden, needs_review: true, said: undefined }) });
  delete rec.read.said;
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { hosted: false });
  const rows = ledger(html);
  // PLAIN-ADS-FIX1 (screen R1-04): no name fact rows at all, no "Not shown".
  for (const k of ['Brand heard in the audio', 'Web address read out', 'Brand the address points to', 'Promo code read out']) {
    assert.equal(rows[k], undefined, k);
  }
  assert.doesNotMatch(html, /Not shown/);
  assert.ok(rows['How the time was found'], 'how the time was found stays');
  assert.equal(rows['Brand name review'], undefined);
  assert.match(html, /<p class="ew-reader-caveat">We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand\. Know the brand\? Use <button type="button" class="text-link" data-action="goto-report">Report a mistake<\/button>\.<\/p>/, 'the caveat says why it counts toward no brand (SEARCH-CLARITY)');
  assert.match(html, /<h1 class="ew-reader-h1">Brand not identified<\/h1>/);
  assert.doesNotMatch(html, /Northwind|Restwell|Brand confidence|Name certainty|namer’s guess/);
});

test('members see no status tag; About › Record says Ad (PLAIN-ADS)', async () => {
  // Hosted is set once the record read starts: the shell's own hosted
  // admission is not under test here, only the page's labels.
  let win = null;
  const api = evApi(record());
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), p => { if (win) win.EW.current.hosted = true; return api(p); }, { onWin: w => { win = w; } });
  const about = (html.match(/<dt>Record<\/dt><dd>([^<]*)<\/dd>/) || [])[1];
  assert.equal(about, 'Ad');
  assert.doesNotMatch(html, /Confirmed ad|review team/);
  assert.equal(ledger(html)['Brand name review'], undefined);
});

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
const epAd = (id, span, display, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }, needs_review: false, self_promo: false,
  kind: 'named', source_label: 'Named read', transcript_start_s: span[0], media: null, ...over });
const epReads = (ads, promo = []) => withBreaks({ episode_id: 'ep_g2', ads, self_promo: promo,
  screening: { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null },
  screened_no_ads: false });

test('the ad comes from the server: overlapping evidence shares it, evidence that only touches starts a new one', async () => {
  // D-1 replaces the old 2 s gap rule: the server's ad break holds reads
  // whose spans overlap; a read that does not overlap starts a new break.
  // Overlapping evidence naming one brand (and one under review) is one ad.
  const ads = [epAd('e1', [60, 125], 'Northwind Mattress'), epAd(EV_ID, [121.5, 170], 'Northwind Mattress'),
    epAd('e3', [165, 200], 'X', { brand: { key: null, display: null, display_basis: 'none', confirmed: false, under_review: true }, needs_review: true }),
    epAd('e4', [200, 240], 'Later')];
  const rec = record({ read: read({ display_span: [121.5, 170] }), ad_break: adBreakOf(epReads(ads), EV_ID) });
  const { html, paths } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec, epReads(ads)), { hosted: false });
  assert.ok(paths.includes('/customer/episodes/ep_g2'));
  assert.match(html, /<span class="eyebrow">Ad · one of 3 pieces of evidence for this ad<\/span>/);
  assert.match(html, /<h2 id="ew-pod-h">Other evidence for this ad/);
  assert.match(html, /One of 3 pieces of evidence for this ad/);
  assert.doesNotMatch(html, /\b(spot|read|Read|Spot|evidence|Evidence) \d|\d+ of \d+ (reads |pieces )?in|overlaps (read|spot|evidence) \d/, 'no evidence is numbered inside an ad');
  assert.match(html, /data-spot="1" aria-current="true"/);
  assert.match(html, /aria-label="Evidence, Brand not identified/);
  assert.doesNotMatch(html, /Later/, 'evidence that only touches the ad starts a new ad');
  assert.match(html, /<li data-spot-row="1" data-self[^>]*>[\s\S]*?This page/);
});

test('a single ad has no pod; a failed episode read only hides the pod', async () => {
  const single = await render('#/evidence/' + encodeURIComponent(REF), evApi(record(), epReads([epAd(EV_ID, [120, 170], 'Northwind Mattress')])), { hosted: false });
  assert.doesNotMatch(single.html, /ad pod/);
  assert.match(single.html, /<span class="eyebrow">Ad<\/span>/);
  const failed = await render('#/evidence/' + encodeURIComponent(REF), evApi(record(), null), { hosted: false });
  assert.match(failed.html, /Each fact and its source/);
  assert.doesNotMatch(failed.html, /ad pod/);
});

test('the back link names the Search query it came from; unknown origin keeps Back to results', async () => {
  const route = '#/evidence/' + encodeURIComponent(REF);
  const fromSearch = await render(route, evApi(record()), { origin: '#/search?mode=ads&q=sleep' });
  assert.match(fromSearch.html, /<a class="ew-back" href="#\/search\?mode=ads&amp;q=sleep">← Back to Search: “sleep”<\/a>/);
  const fromShow = await render(route, evApi(record()), { origin: '#/show/cs-quiet' });
  assert.match(fromShow.html, /← Back to Quiet Hours<\/a>/);
  const direct = await render(route, evApi(record()));
  assert.match(direct.html, /← Back to results<\/a>/);
});

test('the clip always starts 3 s early, whatever the stored preference, and plays through the custom controls', async () => {
  const media = { playback: 'local', kind: 'evidence_clip', source_offset_s: 100, alignment: 'verified', timing: 'verified', trust: 'exact' };
  const rec = record({ media });
  const off = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec));
  assert.match(off.html, /<audio id="clip-ev" class="ew-clip-audio" preload="none" src="\/api\/v1\/customer\/records\/sr1%3Ab2b2[^"]*\/media#t=17\.0"/);
  assert.match(off.html, /data-action="clip-toggle" data-clip="ev"/);
  assert.match(off.html, /data-delta="-5"/);
  assert.match(off.html, /data-clip-speed="ev"/);
  assert.match(off.html, /<span class="ew-time-xl" data-clip-now>2:00<\/span>/);
  assert.doesNotMatch(off.html, /<audio[^>]*controls/);
  const stored = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { prefs: { clip_lead_in: false } });
  assert.match(stored.html, /\/media#t=17\.0"/);
});

test('the episode page groups pods, keeps own promotions apart and labels the transcript lines', async () => {
  // e1 and e2 overlap and name one brand: one ad (two brands would be two ads).
  const ads = [epAd('e1', [60, 125], 'AG1'), epAd('e2', [120, 150], 'AG1', { said: { web_addresses: [], promo_codes: ['OFFER20'], address_brand: null, code_brand: null, spoken_brand: 'AG1' } }),
    epAd('e4', [900, 960], 'BetterHelp')];
  const promo = [epAd('p1', [150.5, 170], 'Quiet Hours', { self_promo: true, self_promo_reason: 'own-domain:quiethours.fm' })];
  const rec = { ref: 'ep:ep_g2', source_ref: 'sr1:e2e2', canonical_source_ref: 'sr1:e2e2', record_kind: 'source_publication', relationship: 'current',
    episode: { episode_id: 'ep_g2', transcript_version_id: 3, ep_title: 'Recovery weeks', show_title: 'The Long Run', pub_date: '2026-10-09', duration_min: 58 },
    show: { show_identity_id: 'cs-long', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3480 }, selected_bounds: { start_s: 0, end_s: 3480 }, media: {},
    window: { segments: [{ segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 },
      { segment_id: 2, start_s: 120, end_s: 130, text: 'Go to AG1.', text_start: 0, text_end: 10 }], text_complete: true, next_cursor: null } };
  const { html } = await render('#/episode/ep_g2', p => {
    if (p === '/customer/records/' + encodeURIComponent('ep:ep_g2')) return { record: rec };
    if (p === '/customer/episodes/ep_g2') return { episode: epReads(ads, promo) };
    throw new Error('unexpected ' + p);
  });
  assert.match(html, /<span class="ew-ads-count">2 ads<\/span>/);
  assert.equal((html.match(/class="ew-pod ew-pod-box"/g) || []).length, 1);
  assert.match(html, /Evidence \(2\), some overlapping/, 'the own promotion 0.5 s later never joins the ad');
  assert.match(html, /The show’s own promotions/);
  assert.match(html, /data-tag="code">Code heard <b>OFFER20<\/b>/);
  assert.match(html, /<span class="ew-line-tag">Ad · AG1<\/span><p>Quick break\.<\/p>/);
  assert.match(html, /<span class="ew-line-tag">Ad · AG1<\/span><p>Go to AG1\.<\/p>/);
  assert.match(html, /Where each ad sat in the 58:00 episode/);
  assert.match(html, /← Back to The Long Run<\/a>/, 'no origin: back to the show page');
  assert.match(html, /data-tag="tier"/);
});

test('the episode page never says no ads when the ad list is unknown', async () => {
  const rec = { ref: 'ep:ep_g2', source_ref: 'sr1:e2e2', relationship: 'current',
    episode: { episode_id: 'ep_g2', transcript_version_id: 3, ep_title: 'E', show_title: 'S', pub_date: '' }, media: {},
    window: { segments: [], text_complete: true, next_cursor: null } };
  for (const episode of [null, epReads([], [])]) {
    const unscreened = episode && { ...episode, screening: null };
    const { html } = await render('#/episode/ep_g2', p => {
      if (p.startsWith('/customer/records/')) return { record: rec };
      if (p === '/customer/episodes/ep_g2') { if (!unscreened) throw Object.assign(new Error('boom'), { status: 500 }); return { episode: unscreened }; }
      throw new Error('unexpected ' + p);
    });
    assert.doesNotMatch(html, /no ads/i);
  }
});

/* ---- review fixes: disagreement, unknown clip times, eyebrow, counts ---- */

test('the address owner is compared with what was heard, never with the attributed brand', async () => {
  // Spoken Y, attributed X, address X: the address agrees with the attribution but not with the audio.
  const rec = record({ read: read({ said: { web_addresses: ['northwind.com'], promo_codes: [], address_brand: 'Northwind Mattress', code_brand: null, spoken_brand: 'Zenith Pillow' } }) });
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { hosted: false });
  const rows = ledger(html);
  assert.match(rows['Brand heard in the audio'], /^Zenith Pillow/);
  assert.match(rows['Brand the address points to'], /^Northwind Mattress/);
  assert.doesNotMatch(rows['Brand the address points to'], /matches what was heard/);
  assert.match(rows['Brand the address points to'], /Differs from what was heard\. We show both facts and don’t pick one\./);
  assert.match(html, /<tr class="is-uncertain"><th scope="row">Brand the address points to/);
});

test('no recorded spoken brand: heard row says Not recorded and the owner row says Not compared', async () => {
  const rec = record({ read: read({ said: { web_addresses: ['northwind.com'], promo_codes: [], address_brand: 'Northwind Mattress', code_brand: null, spoken_brand: null } }) });
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { hosted: false });
  const rows = ledger(html);
  assert.match(rows['Brand heard in the audio'], /Not recorded/);
  assert.doesNotMatch(rows['Brand heard in the audio'], /Northwind/);
  assert.match(rows['Brand the address points to'], /Northwind Mattress/);
  assert.match(rows['Brand the address points to'], /Not compared/);
  assert.doesNotMatch(rows['Brand the address points to'], /matches what was heard|Differs/);
  assert.doesNotMatch(html, /is-uncertain/);
});

// The markup's data-* of the element carrying data-clip-id="<id>".
function clipData(html, id) {
  const tagText = html.match(new RegExp('<[^>]*data-clip-id="' + id + '"[^>]*>'))[0];
  const data = {};
  for (const m of tagText.matchAll(/data-([a-z-]+)="([^"]*)"/g)) data[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[2];
  return data;
}
function fire(win, html, id, type, { currentTime = 0, paused = false, buttons = [] } = {}) {
  const box = { dataset: clipData(html, id), classList: { add() {}, remove() {}, toggle() {} }, querySelector: () => null, setAttribute() {}, removeAttribute() {} };
  const audio = { tagName: 'AUDIO', dataset: { clip: id }, paused, ended: false, currentTime, closest: () => box, pause() { this.paused = true; } };
  win.document.querySelectorAll = () => buttons;
  for (const fn of win.__listeners[type] || []) fn({ type, target: audio });
  return audio;
}
const LOCAL = { playback: 'local', kind: 'evidence_clip', source_offset_s: 0, alignment: 'verified', timing: 'verified', trust: 'exact' };

test('a clip with known times stops at its end bound', async () => {
  let w; const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(record({ media: LOCAL })), { onWin: x => { w = x; } });
  assert.equal(clipData(html, 'ev').end, '170');
  assert.equal(fire(w, html, 'ev', 'timeupdate', { currentTime: 169 }).paused, false);
  assert.equal(fire(w, html, 'ev', 'timeupdate', { currentTime: 170.2 }).paused, true);
});

test('a local clip with no recorded span keeps playing: unknown end means no auto-stop', async () => {
  const rec = record({ media: LOCAL, selected_bounds: null, evidence: { id: EV_ID, episode_id: 'ep_g2' }, read: read({ display_span: null }) });
  let w; const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { onWin: x => { w = x; } });
  const d = clipData(html, 'ev');
  assert.equal(d.start, ''); assert.equal(d.end, '');
  assert.equal(fire(w, html, 'ev', 'timeupdate', { currentTime: 0.1 }).paused, false, 'does not stop at once');
  assert.equal(fire(w, html, 'ev', 'timeupdate', { currentTime: 500 }).paused, false);
});

test('an episode recording with no length keeps playing', async () => {
  const rec = { ref: 'ep:ep_g2', source_ref: 'sr1:e2e2', relationship: 'current', episode: { episode_id: 'ep_g2', transcript_version_id: 3, ep_title: 'E', show_title: 'S', pub_date: '' },
    media: LOCAL, window: { segments: [], text_complete: true, next_cursor: null } };
  let w; const { html } = await render('#/episode/ep_g2', p => {
    if (p.startsWith('/customer/records/')) return { record: rec };
    throw new Error('x');
  }, { onWin: x => { w = x; } });
  assert.equal(clipData(html, 'ep').end, '');
  assert.equal(fire(w, html, 'ep', 'timeupdate', { currentTime: 0.2 }).paused, false);
  assert.equal(fire(w, html, 'ep', 'timeupdate', { currentTime: 900 }).paused, false);
});

test('an ad row with a clip but no span keeps playing', async () => {
  const ad = epAd('e1', [60, 120], 'Squarespace', { span: null, display_span: null, media: LOCAL });
  const rec = { ref: 'ep:ep_g2', source_ref: 'sr1:e2e2', relationship: 'current', episode: { episode_id: 'ep_g2', transcript_version_id: 3, ep_title: 'E', show_title: 'S', pub_date: '' },
    media: {}, window: { segments: [], text_complete: true, next_cursor: null } };
  let w; const { html } = await render('#/episode/ep_g2', p => {
    if (p.startsWith('/customer/records/')) return { record: rec };
    if (p === '/customer/episodes/ep_g2') return { episode: epReads([ad]) };
    throw new Error('x');
  }, { onWin: x => { w = x; } });
  const d = clipData(html, 'ad0');
  assert.equal(d.start, ''); assert.equal(d.end, '');
  assert.equal(fire(w, html, 'ad0', 'timeupdate', { currentTime: 0.1 }).paused, false);
});

test('−5 s stops at the lead-in start, and the play button reads Pause clip while playing', async () => {
  const rec = record({ media: { ...LOCAL, source_offset_s: 100 } });
  let w; const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(rec), { prefs: { clip_lead_in: true }, onWin: x => { w = x; } });
  const d = clipData(html, 'ev');
  assert.equal(d.start, '120');
  assert.equal(d.lead, '17', 'the lead-in start in recording time: 120 - 100 - 3');
  const audio = fire(w, html, 'ev', 'timeupdate', { currentTime: 18 });
  // the seek button, through the page's action handler
  const box = { dataset: d };
  const seekAudio = { tagName: 'AUDIO', currentTime: 18, closest: () => box, paused: true };
  w.document.getElementById = () => seekAudio;
  await w.EWViews.handleAction({ dataset: { action: 'clip-seek', clip: 'ev', delta: '-5' } });
  assert.equal(seekAudio.currentTime, 17, 'not 20 (the read start): the lead-in start');
  const attrs = {};
  const btn = { dataset: { clip: 'ev' }, classList: { toggle() {} }, setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: k => { delete attrs[k]; }, getAttribute: k => attrs[k], innerHTML: '' };
  attrs['aria-label'] = 'Play clip, 1:50 to 2:50';
  w.document.getElementById = () => null;
  // Sol R1-01: play only starts loading; playing presents the pause control.
  fire(w, html, 'ev', 'play', { paused: false, buttons: [btn] });
  assert.notEqual(attrs['aria-label'], 'Pause clip');
  fire(w, html, 'ev', 'playing', { paused: false, buttons: [btn] });
  assert.equal(attrs['aria-label'], 'Pause clip');
  assert.equal(attrs['aria-pressed'], 'true');
  fire(w, html, 'ev', 'pause', { paused: true, buttons: [btn] });
  assert.equal(attrs['aria-label'], 'Play clip, 1:50 to 2:50');
  assert.equal(attrs['aria-pressed'], 'false');
  void audio;
});

test('the eyebrow says Ad for every ad, whatever its review (PLAIN-ADS, as statusLabel)', async () => {
  const eyebrow = async status => (await render('#/evidence/' + encodeURIComponent(REF), evApi(record({ status })), { hosted: false })).html.match(/<span class="eyebrow">([^<]*)<\/span>/)[1];
  assert.equal(await eyebrow('confirmed'), 'Ad');
  assert.equal(await eyebrow('finding'), 'Ad');
  assert.equal(await eyebrow('unresolved'), 'Ad');
});

test('stopped partway with no ads counts it in stage words, not the raw label', async () => {
  const rec = { ref: 'ep:ep_g2', source_ref: 'sr1:e2e2', relationship: 'current', episode: { episode_id: 'ep_g2', transcript_version_id: 3, ep_title: 'E', show_title: 'S', pub_date: '' },
    media: {}, window: { segments: [], text_complete: true, next_cursor: null } };
  const episode = { ...epReads([]), screening: { label: 'Stopped partway (fetch)', jev_screened: false, stopped_partway: true, stage: 'fetch', coverage_tier: 'transcript_only', transcript_only: false } };
  const { html } = await render('#/episode/ep_g2', p => {
    if (p.startsWith('/customer/records/')) return { record: rec };
    if (p === '/customer/episodes/ep_g2') return { episode };
    throw new Error('x');
  });
  assert.doesNotMatch(html, /Stopped partway \(fetch\)/);
  assert.match(html, /<span class="ew-ads-count">Stopped partway · audio download<\/span>/);
});

test('css: the back link keeps the 40 px target and short pod spots keep one', () => {
  const ev = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  assert.doesNotMatch(ev, /\.ew-reader-head > \.ew-back\s*\{[^}]*min-height: 32px/);
  assert.match(fs.readFileSync(path.join(APP, 'styles.css'), 'utf8'), /\.ew-pod-spot \{[^}]*min-width: var\(--ew-target\)/);
});
