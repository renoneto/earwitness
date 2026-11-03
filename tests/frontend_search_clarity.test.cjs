/* SEARCH-CLARITY (UX audit M3/M4, quick wins 5 and 6; lead-approved
   strings in ws-app/UX-STRINGS-DRAFT.md).
   - Search: a brand query's helper line says how many ads mention the
     words and how many of them are the brand's ads (the server's
     brand_break_total); with no count, today's line.
   - Search: a query that is no brand in the Brands list says so above
     the ads, without claiming no ad exists.
   - Evidence: a "Brand not identified" ad says why in one line and
     points to Report a mistake.
   - Shows: Sort by most ads with clips (default) or A to Z, and an
     "Ads with clips" fact on each card.
   Run: node --test tests/frontend_search_clarity.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const text = html => F.text(html).replace(/&ldquo;|&rdquo;/g, '"');

/* -- Search ---------------------------------------------------------------- */
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const brand = (display) => ({ key: display.toLowerCase(), display, confirmed: false });
const adItem = (n, b) => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', review: null, show_title: 'Show A',
  ep_title: 'Episode ' + n, episode_id: 'e' + n, pub_date: '2026-10-09', audio: 'unavailable', quotation_span: [60, 120],
  show: { show_identity_id: 'cs-a' }, read: { brand: b, display_span: [60, 120], has_clip: false, needs_review: false },
  attribution: [{ brand: b.key, basis: 'suggested', channel: 'spoken' }], ad_break_id: 'brk:' + n, match_basis: 'passage' });
function searchBundle(q, { breakTotal = 17, brandTotal, relation = 'exact', count = 2, mode = 'ads' } = {}) {
  const items = Array.from({ length: count }, (_, i) => adItem(i + 1, brand(i ? 'Quince' : 'AG1')));
  const breaks = items.map(it => ({ id: it.ad_break_id, episode_id: it.episode_id, transcript_version_id: 7, on_current_transcript: true,
    span: it.quotation_span, read_count: 1, matching_read_count: 1, refs: [it.ref], pub_date: '2026-10-09', group_id: it.ad_break_id,
    group_ad_count: 1, brands: [{ key: it.read.brand.key, display: it.read.brand.display, confirmed: false }] }));
  const result = { items, total: breakTotal, dated_total: breakTotal, undated_total: 0, total_relation: relation, per_page: 10,
    ad_breaks: breaks, break_total: breakTotal, break_dated_total: breakTotal, break_undated_total: 0 };
  if (brandTotal !== undefined) result.brand_break_total = brandTotal;
  return { query: { version: 2, mode, q, sort: 'newest', page: 1 }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 }, result, summary: { week: [] } };
}
const BRANDS = [{ key: 'quince', display: 'Quince', ad_breaks: 92, shows: 37, last_heard: '2026-10-19' },
  { key: 'ag1', display: 'AG1', ad_breaks: 10, shows: 8, last_heard: '2026-10-18' }];
const searchApi = (bundle, brands = BRANDS) => p => {
  if (p === '/customer/research-query') return bundle;
  if (p === '/customer/brands') return brands instanceof Error ? brands : { brands };
  if (p === '/customer/shows') return { shows: [] };
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};
const search = async (q, opts = {}, brands = BRANDS) => {
  const enc = encodeURIComponent(q);
  const { html } = await F.render(`#/search?mode=${opts.mode || 'ads'}&q=${enc}`, searchApi(searchBundle(q, opts), brands));
  assert.ok(html.length > 500, 'rendered');
  return html;
};
const jump = html => (html.match(/<p class="ew-brand-jump[^"]*">[\s\S]*?<\/p>/) || [''])[0];
const LINKS = 'See where AG1 advertises · Show only ads named AG1';

test('search clarity: a brand query says how many of the ads are the brand’s', async () => {
  const line = text(jump(await search('ag1', { brandTotal: 10 }))).trim();
  assert.equal(line, `17 ads mention “ag1” · 10 of them are AG1 ads. ${LINKS}`);
});

test('search clarity: every ad the brand’s, one of many, one of one', async () => {
  assert.equal(text(jump(await search('ag1', { brandTotal: 17 }))).trim(), `17 ads mention “ag1”. All are AG1 ads. ${LINKS}`);
  assert.equal(text(jump(await search('ag1', { brandTotal: 1 }))).trim(), `17 ads mention “ag1” · 1 of them is an AG1 ad. ${LINKS}`);
  assert.equal(text(jump(await search('ag1', { breakTotal: 1, brandTotal: 1, count: 1 }))).trim(),
    `1 ad mentions “ag1” · it is an AG1 ad. ${LINKS}`);
});

test('search clarity: one ad short of all is never "All are" (SC-R1-02)', async () => {
  assert.equal(text(jump(await search('ag1', { brandTotal: 16 }))).trim(), `17 ads mention “ag1” · 16 of them are AG1 ads. ${LINKS}`);
  assert.equal(text(jump(await search('ag1', { breakTotal: 2, brandTotal: 1 }))).trim(), `2 ads mention “ag1” · 1 of them is an AG1 ad. ${LINKS}`);
});

/* SC-R1-03 (LEDGER-LEAD3-SC-2): under a lower bound the brand's share is
   unknown too, so the line is only "At least N ads mention …": never
   "All are …" and never "· N of them …". */
test('search clarity: a lower bound says only "At least N ads mention"', async () => {
  const lb = async opts => text(jump(await search('ag1', { relation: 'lower_bound', ...opts }))).trim();
  assert.equal(await lb({ brandTotal: 10 }), `At least 17 ads mention “ag1”. ${LINKS}`);
  assert.equal(await lb({ brandTotal: 17 }), `At least 17 ads mention “ag1”. ${LINKS}`, 'never "All are"');
  assert.equal(await lb({ brandTotal: 1 }), `At least 17 ads mention “ag1”. ${LINKS}`, 'never "1 of them is"');
  assert.equal(await lb({ breakTotal: 1, brandTotal: 1, count: 1 }), `At least 1 ad mentions “ag1”. ${LINKS}`, 'never "it is"');
  // None of the brand's ads among them keeps today's line, as for an exact total.
  assert.equal(await lb({ brandTotal: 0 }), `AG1 is a brand heard in ads. ${LINKS}`);
});

test('search clarity: the brand filter link carries no number', async () => {
  const html = jump(await search('ag1', { brandTotal: 10 }));
  const links = [...html.matchAll(/<a [^>]*>([^<]*)<\/a>/g)].map(m => m[1]);
  assert.deepEqual(links, ['See where AG1 advertises', 'Show only ads named AG1']);
});

test('search clarity: no count (an older server) keeps today’s line', async () => {
  assert.equal(text(jump(await search('ag1'))).trim(), `AG1 is a brand heard in ads. ${LINKS}`);
});

test('search clarity: no ad of the brand among them keeps today’s line, never "0 of them"', async () => {
  assert.equal(text(jump(await search('ag1', { brandTotal: 0 }))).trim(), `AG1 is a brand heard in ads. ${LINKS}`);
  assert.equal(text(jump(await search('ag1', { breakTotal: 1, brandTotal: 0, count: 1 }))).trim(), `AG1 is a brand heard in ads. ${LINKS}`);
});

test('search clarity: words that are no listed brand say so above the ads', async () => {
  const html = await search('nike');
  const line = text(jump(html)).trim();
  assert.equal(line, 'No brand called “nike” was heard in ads in this collection. These 17 ads say “nike” in their words or their episode title. Browse brands · What we cover');
  const head = jump(html);
  assert.match(head, /href="#\/brands"[^>]*>Browse brands</);
  assert.match(head, /href="\/coverage\/"[^>]*>What we cover</);
  assert.ok(html.indexOf(head) < html.indexOf('ew-result-list') || html.indexOf('ew-result-list') < 0, 'above the results');
});

test('search clarity: one ad that is no brand', async () => {
  const line = text(jump(await search('nike', { breakTotal: 1, count: 1 }))).trim();
  assert.equal(line, 'No brand called “nike” was heard in ads in this collection. This ad says “nike” in its words or its episode title. Browse brands · What we cover');
});

test('search clarity: no "no brand" line for quotes, other tabs, no ads, or a failed Brands read', async () => {
  assert.equal(jump(await search('"nike"')), '', 'quoted words');
  assert.equal(jump(await search('nike', { mode: 'mentions' })), '', 'All mentions');
  assert.equal(jump(await search('nike', { breakTotal: 0, count: 0 })), '', 'no ads');
  assert.equal(jump(await search('nike', {}, new Error('brands failed'))), '', 'brands read failed');
});

/* -- Evidence: why "Brand not identified" ----------------------------------- */
const EVREF = 'sr1:' + 'v'.repeat(32);
const HELD = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const evRecord = (over = {}) => ({ ref: EVREF, canonical_source_ref: EVREF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'quince', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 100, end_s: 150 },
  episode: { episode_id: 'ep_d1', show_title: 'Show A', ep_title: 'An episode', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 55.4, transcript_version_id: 36, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Show A', unresolved: 0 },
  evidence: { id: 'a2', episode_id: 'ep_d1', orig_start_s: 100, orig_end_s: 150, transcript_version_id: 36 }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', brand: { key: 'quince', display: 'Quince', confirmed: false }, needs_review: false, self_promo: false,
    display_span: [100, 150], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Quince' } },
  window: { segments: [{ segment_id: 9, start_s: 100, end_s: 150, text: 'Go to the store.', text_start: 0, text_end: 16 }], text_complete: true, next_cursor: null },
  ad_break: null, ...over });
const evidence = async rec => (await F.render('#/evidence/' + encodeURIComponent(EVREF), p => {
  if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/brands') return { brands: BRANDS };
  return {};
})).html;
// The words as read: an inline control adds no space of its own.
const caveat = html => [...html.matchAll(/<p class="ew-reader-caveat">([\s\S]*?)<\/p>/g)].map(m => text(m[1].replace(/<[^>]*>/g, '')).trim());
// The app's typographic apostrophe (’), as in the rest of its copy.
const H1_LINE = 'We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand. Know the brand? Use Report a mistake.';
const N1_LINE = 'We found this ad from its ad words, but no brand name was recorded, so it doesn’t count toward any brand. Know the brand? Use Report a mistake.';

test('search clarity: a held ad says why in one line, before the actions', async () => {
  const base = evRecord();
  const html = await evidence(evRecord({ attribution: [], read: { ...base.read, brand: HELD, needs_review: true, said: undefined } }));
  assert.deepEqual(caveat(html), [H1_LINE]);
  assert.doesNotMatch(html, /Not counted toward any brand\./);
  assert.ok(html.indexOf('ew-reader-caveat') < html.indexOf('data-action="save-excerpt"'), 'before Save excerpt');
  assert.match(html, /<summary>Report a mistake<\/summary>/, 'the control the line names is on the page');
});

test('search clarity: an ad with no brand recorded gets its own line', async () => {
  const base = evRecord();
  const html = await evidence(evRecord({ attribution: [], read: { ...base.read, brand: { key: null, display: null, display_basis: 'none', confirmed: false }, said: undefined } }));
  assert.match(text(html.slice(html.indexOf('<h1'), html.indexOf('</h1>'))), /Brand not identified/);
  assert.deepEqual(caveat(html), [N1_LINE]);
});

test('search clarity: a named ad with no shown name gets the first line', async () => {
  const base = evRecord();
  const html = await evidence(evRecord({ attribution: [{ brand: 'zorblax', basis: 'suggested', channel: 'spoken' }],
    read: { ...base.read, brand: { key: 'zorblax', display: null, display_basis: 'none', confirmed: false }, said: undefined } }));
  assert.deepEqual(caveat(html), [H1_LINE]);
});

test('search clarity: a named ad and the show’s own promotion have no line', async () => {
  assert.deepEqual(caveat(await evidence(evRecord())), []);
  const base = evRecord();
  const promo = await evidence(evRecord({ attribution: [], read: { ...base.read, brand: HELD, self_promo: true, said: undefined } }));
  assert.deepEqual(caveat(promo), []);
});

/* SC-SCR-R1-01: the held line's "Report a mistake" takes the member to
   this read's own control, not the first one below it (another read of
   the same ad, in "Other evidence for this ad"). */
const OTHER = 'sr1:' + 'o'.repeat(32);
const podAd = (id, ref, span, brand, held) => ({ ref, evidence_id: id, status: 'finding', span, display_span: span,
  brand: held ? HELD : { key: brand.toLowerCase(), display: brand, display_basis: 'brand_list', confirmed: false },
  needs_review: !!held, self_promo: false, kind: 'named', source_label: 'Named read', transcript_start_s: span[0], media: null });
const sharedHeld = (over = {}) => {
  const base = evRecord();
  return evRecord({ attribution: [], read: { ...base.read, brand: HELD, needs_review: true, said: undefined },
    ad_break: { id: 'brk:0', span: [60, 150], read_count: 2, index: 1, refs: [OTHER, EVREF], brands: [], on_current_transcript: true }, ...over });
};
const sharedPage = async rec => F.render('#/evidence/' + encodeURIComponent(EVREF), p => {
  if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/brands') return { brands: BRANDS };
  if (p === '/customer/episodes/ep_d1') return { episode: { episode_id: 'ep_d1', self_promo: [], screened_no_ads: false,
    screening: { label: 'Screened', jev_screened: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false },
    ads: [podAd('a1', OTHER, [60, 120], 'Lean'), podAd('a2', EVREF, [100, 150], null, true)] } };
  return {};
});

test('search clarity: on a shared ad, the held line’s Report a mistake goes to this read’s own control', async () => {
  const t = await sharedPage(sharedHeld());
  const html = t.html;
  assert.deepEqual(caveat(html), [H1_LINE], 'the words are unchanged');
  const line = (html.match(/<p class="ew-reader-caveat">[\s\S]*?<\/p>/) || [''])[0];
  const btn = (line.match(/<button [^>]*>Report a mistake<\/button>/) || [''])[0];
  assert.match(btn, /type="button"/);
  assert.match(btn, /data-action="goto-report"/);
  // The page has the other read's control first, below the line.
  const other = html.indexOf(`data-report-ref="${OTHER}"`);
  const own = html.indexOf(`data-report-ref="${EVREF}"`);
  assert.ok(other > 0 && own > other, 'the other read’s control comes first on the page');
  // A page-shaped document: the other read's control in the ad's rows,
  // this read's own in the side column.
  const control = name => ({ name, parentElement: { tagName: 'DETAILS', open: false }, scrolled: null, focused: null, scrollIntoView(o) { this.scrolled = o; }, focus(o) { this.focused = o; } });
  const theirs = control('other'), mine = control('own');
  const pick = sel => (/ew-reader-side|data-report-ref="sr1:v/.test(sel) ? mine : /report|summary/.test(sel) ? theirs : null);
  t.win.document.querySelector = sel => pick(String(sel));
  t.win.document.querySelectorAll = sel => { const hit = pick(String(sel)); return hit === mine ? [mine] : hit ? [theirs, mine] : []; };
  t.win.matchMedia = () => ({ matches: true });
  const data = Object.fromEntries([...btn.matchAll(/data-([a-z-]+)="([^"]*)"/g)].map(m => [m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()), m[2]]));
  await t.win.EWViews.handleAction({ dataset: data });
  assert.equal(theirs.focused, null, 'not the other read’s control');
  assert.equal(theirs.scrolled, null);
  assert.ok(mine.focused, 'this read’s own control has focus');
  assert.ok(mine.scrolled, 'and is scrolled to');
});

/* A page-shaped document for the line's button: the other read's control
   in the ad's rows, this read's own in the side column (or, once sent,
   its thank-you there). */
const pressLine = async ({ sent = false } = {}) => {
  const t = await sharedPage(sharedHeld());
  const line = (t.html.match(/<p class="ew-reader-caveat">[\s\S]*?<\/p>/) || [''])[0];
  const btn = (line.match(/<button [^>]*>Report a mistake<\/button>/) || [''])[0];
  const el = name => ({ name, tabIndex: 0, scrolled: null, focused: null, scrollIntoView(o) { this.scrolled = o; }, focus(o) { this.focused = o; } });
  const summary = name => { const s = el(name); s.parentElement = { tagName: 'DETAILS', open: false }; s.closest = () => s.parentElement; return s; };
  const theirs = summary('other'), mine = sent ? null : summary('own'), thanks = sent ? el('thanks') : null;
  const pick = sel => /ew-reader-side/.test(sel) ? (/report-thanks/.test(sel) ? thanks : /summary|report-problem/.test(sel) ? mine : null)
    : /report|summary/.test(sel) ? theirs : null;
  t.win.document.querySelector = sel => pick(String(sel));
  t.win.document.querySelectorAll = sel => { const hit = pick(String(sel)); return hit ? (hit === theirs ? [theirs] : [theirs, hit]) : []; };
  t.win.matchMedia = () => ({ matches: true });
  const data = Object.fromEntries([...btn.matchAll(/data-([a-z-]+)="([^"]*)"/g)].map(m => [m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()), m[2]]));
  await t.win.EWViews.handleAction({ dataset: data });
  return { theirs, mine, thanks };
};

test('search clarity: the line’s Report a mistake opens this read’s own control, then focuses it (SC-R2-02)', async () => {
  const { theirs, mine } = await pressLine();
  assert.equal(mine.parentElement.open, true, 'this read’s own control is open, as a press on it leaves it');
  assert.ok(mine.focused, 'and has focus');
  assert.ok(mine.scrolled, 'and is scrolled to');
  assert.equal(theirs.parentElement.open, false, 'the other read’s control stays closed');
  assert.equal(theirs.focused, null, 'and gets no focus');
});

test('search clarity: once this read’s report is sent, the line’s Report a mistake goes to the thank-you (SC-R2-01)', async () => {
  const { theirs, thanks } = await pressLine({ sent: true });
  assert.ok(thanks.scrolled, 'the thank-you is scrolled to');
  assert.ok(thanks.focused, 'and has focus');
  assert.equal(thanks.tabIndex, -1, 'focusable by script only');
  assert.equal(theirs.parentElement.open, false, 'the other read’s control stays closed');
  assert.equal(theirs.focused, null, 'and gets no focus');
  assert.equal(theirs.scrolled, null);
});

test('search clarity: an older copy of a held read has no own control, so the line stays plain words', async () => {
  const t = await sharedPage(sharedHeld({ historical: true }));
  const line = (t.html.match(/<p class="ew-reader-caveat">[\s\S]*?<\/p>/) || [''])[0];
  assert.deepEqual(caveat(t.html), [H1_LINE]);
  assert.doesNotMatch(line, /<button/);
});

/* -- Shows: Sort and Ads with clips --------------------------------------- */
const SHOWS = [
  { id: 'cs-a', display_title: 'Alpha Hour', episodes: 9, ad_breaks: 4, ads_with_clips: 1, category_summary: [] },
  { id: 'cs-b', display_title: 'Beta Talk', episodes: 3, ad_breaks: 8, ads_with_clips: 5, category_summary: [] },
  { id: 'cs-c', display_title: 'Charlie Show', episodes: 5, ad_breaks: 6, ads_with_clips: 1, category_summary: [] },
  { id: 'cs-d', display_title: 'Delta Pod', episodes: 2, ad_breaks: 4, ads_with_clips: 1, category_summary: [] },
  { id: 'cs-e', display_title: 'Echo', episodes: 1, ad_breaks: 0, ads_with_clips: 0, category_summary: [] },
];
const showsApi = (shows = SHOWS) => p => {
  if (p === '/customer/shows' || p.startsWith('/customer/shows?')) return { shows };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};
const order = html => [...html.matchAll(/<h2 class="lib-card-name"><a [^>]*>([^<]*)<\/a>/g)].map(m => m[1]);

test('search clarity: Shows sort by most ads with clips by default', async () => {
  const { html } = await F.render('#/shows', showsApi());
  const select = (html.match(/<select id="directory-sort"[\s\S]*?<\/select>/) || [''])[0];
  assert.match(html, /<label for="directory-sort">Sort<\/label>/);
  assert.deepEqual([...select.matchAll(/<option value="([a-z]+)"( selected)?>([^<]*)</g)].map(m => [m[1], !!m[2], m[3]]),
    [['clips', true, 'Most ads with clips'], ['az', false, 'A to Z']]);
  // Ties: more ads, then A to Z.
  assert.deepEqual(order(html), ['Beta Talk', 'Charlie Show', 'Alpha Hour', 'Delta Pod', 'Echo']);
});

test('search clarity: Shows A to Z', async () => {
  const { html } = await F.render('#/shows?sort=az', showsApi());
  assert.deepEqual(order(html), ['Alpha Hour', 'Beta Talk', 'Charlie Show', 'Delta Pod', 'Echo']);
  assert.match(html, /<option value="az" selected>A to Z</);
});

test('search clarity: each show card has Ads with clips beside Episodes', async () => {
  const { html } = await F.render('#/shows', showsApi());
  const card = (html.match(/<article class="ew-card lib-card">[\s\S]*?<\/article>/) || [''])[0];
  assert.match(card, /<dl class="ew-facts"><dt>Episodes<\/dt><dd>3<\/dd><dt>Ads with clips<\/dt><dd>5<\/dd><\/dl>/);
});
