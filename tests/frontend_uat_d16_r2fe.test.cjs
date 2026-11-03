/* d16 review #2 frontend fixes (worker r2fe), D/review/findings-2.md:
   R2-03 alert unread numbers are ads (unread_ad_count), no number without it;
   R2-04 Episode earlier-copy / no-copy blocks render by server ad with the
   same ad box and "Ad break" runs as the current copy; R2-05 confirmed ads
   (confirmed_ads) on the Brands directory and Brand top shows, never the
   per-evidence `confirmed` beside an ad count; Search "Brand not identified".
   Run: node --test tests/frontend_uat_d16_r2fe.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const F = require('./frontend_uat_d1b_fixtures.cjs');
const { render, text } = F;

/* ======================================================================
   R2-03 Alerts: unread numbers count ads (server unread_ad_count)
   ====================================================================== */
const listApi = alerts => p => {
  if (p === '/customer/alerts') return { alerts };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'chatgpt', display: 'ChatGPT' }] };
  return new Error('unexpected ' + p);
};
const listAlert = (id, over) => ({ ...F.ALERT, id, name: 'Alert ' + id, events: undefined, ...over });
const rowOf = (html, id) => (html.match(new RegExp(`<li class="ew-work-alert"[^>]*>(?:(?!<li class="ew-work-alert")[\\s\\S])*?href="#/alert/${id}"[\\s\\S]*?</li>`)) || [''])[0];

test('R2-03 Alerts list: two unread events of one ad say "1 new", never "2 new"', async () => {
  const { html } = await render('#/alerts', listApi([listAlert(1, { unread_event_count: 2, unread_ad_count: 1 })]));
  assert.match(html, /<span class="ew-tag ew-tag-count ew-work-new">1 new<\/span>/);
  assert.doesNotMatch(text(html), /\b2 new\b|\b2 unread\b/);
  assert.match(html, /data-alert-groups="all new"/, 'still under New matches');
});

test('R2-03 Alerts list: a paused alert says "N unread" in ads; the unread sort uses ads', async () => {
  const { html } = await render('#/alerts', listApi([
    listAlert(1, { unread_event_count: 5, unread_ad_count: 1 }),
    listAlert(2, { unread_event_count: 3, unread_ad_count: 3 }),
    listAlert(3, { lifecycle: 'paused', unread_event_count: 4, unread_ad_count: 2 })]));
  const order = [...html.matchAll(/<a class="ew-work-title" href="#\/alert\/(\d+)">/g)].map(m => m[1]);
  assert.deepEqual(order, ['2', '3', '1'], 'most unread ads first, not most unread events');
  assert.match(rowOf(html, 3), /2 unread/);
  assert.doesNotMatch(rowOf(html, 3), /4 unread/);
  assert.match(rowOf(html, 1), />1 new</);
  assert.doesNotMatch(rowOf(html, 1), /5 new/);
});

test('R2-03 Alerts list from an older server (no unread_ad_count): no unread number, still a New match', async () => {
  const { html } = await render('#/alerts', listApi([listAlert(1, { unread_event_count: 2 }), listAlert(2, { lifecycle: 'paused', unread_event_count: 3 })]));
  assert.doesNotMatch(text(html), /\b\d+ (new|unread)\b/);
  assert.match(rowOf(html, 1), /data-alert-groups="all new"/);
  assert.match(rowOf(html, 1), />New</, 'an unnumbered New tag');
  assert.match(rowOf(html, 2), />Unread</, 'a paused alert says Unread without a number');
});

const alertApi = (events, over = {}) => p => {
  if (p === '/customer/alerts/1') return { alert: { ...F.ALERT, events, unread_event_count: events.filter(e => !e.read_state).length, ...over } };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'chatgpt', display: 'ChatGPT' }] };
  return new Error('unexpected ' + p);
};
const twoOfOneAd = () => [F.ev(1, 'epA', [10, 40], 'brk:a'), F.ev(2, 'epA', [30, 60], 'brk:a'), F.ev(3, 'epB', [10, 40], 'brk:c', { read_state: 1 })];

test('R2-03 Alert page: two unread events of one ad are "1 unread"; changes count ads', async () => {
  const { html } = await render('#/alert/1', alertApi(twoOfOneAd(), { unread_ad_count: 1 }));
  assert.match(html, /<span><strong>1<\/strong> unread<\/span>/);
  const meta = (html.match(/<h2>Changes<\/h2>[\s\S]*?<span class="ew-work-meta">([^<]*)<\/span>/) || [, ''])[1];
  assert.match(meta, /^2 changes · 1 unread/);
  assert.doesNotMatch(text(html), /\b2 unread\b|\b3 changes\b/);
  assert.match(html, /data-action="mark-read"[^>]*data-id="1" >|data-action="mark-read"[^>]*data-id="1">/, 'Mark all read stays enabled');
  assert.match(html, /data-tag="info">New</);
  assert.match(html, /data-tag="neutral">Seen</);
});

test('R2-03 Alert page from an older server (no unread_ad_count): no unread number; New/Seen and Mark all read stay', async () => {
  const { html } = await render('#/alert/1', alertApi(twoOfOneAd()));
  assert.doesNotMatch(text(html), /\b\d+ unread\b/);
  assert.match(html, /Mark all read/);
  assert.doesNotMatch((html.match(/<button[^>]*data-action="mark-read"[^>]*>/) || [''])[0], /disabled/);
  assert.match(html, /data-tag="info">New</);
  // Nothing unread: Mark all read is disabled, still no number.
  const seen = await render('#/alert/1', alertApi(twoOfOneAd().map(e => ({ ...e, read_state: 1 }))));
  assert.match((seen.html.match(/<button[^>]*data-action="mark-read"[^>]*>/) || [''])[0], /disabled/);
});

/* ======================================================================
   R2-04 Episode: earlier-copy and no-copy evidence render by server ad
   ====================================================================== */
const CUR = 34, OLD = 9;
const epAd = (id, span, display, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: display ? { key: display.toLowerCase(), display, confirmed: false } : { key: null, display: null, confirmed: false },
  needs_review: false, self_promo: false, kind: 'rule', transcript_start_s: span ? span[0] : null, media: null,
  transcript_version_id: CUR, on_current_transcript: true, ...over });
const old = (id, span, display, over = {}) => epAd(id, span, display, { transcript_version_id: OLD, on_current_transcript: false, ...over });
const ad = (id, span, refs, group, n, over = {}) => ({ id, episode_id: 'ep_r2', span, read_count: refs.length, refs, brands: [],
  transcript_version_id: CUR, on_current_transcript: true, group_id: group, group_ad_count: n, ...over });
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
// Modelled on s001e0's earlier copy (transcript 9): one overlap group split
// by brand into Kachava / She's Birdie / Kachava (group_ad_count 3), plus a
// lone two-evidence Spinquest ad. The current copy holds one ad.
function s001e0Like() {
  const E = { transcript_version_id: OLD, on_current_transcript: false };
  const ads = [epAd('c1', [15, 177], null),
    old('k1', [600, 660], 'Kachava'), old('k2', [640, 709.92], null),
    old('b1', [616.68, 700], "She's Birdie"), old('b2', [650, 756.92], "She's Birdie"), old('b3', [700, 740], null),
    old('k3', [698.04, 764.92], 'Kachava'),
    old('s1', [1560, 1620], 'Spinquest'), old('s2', [1600, 1692.92], null)];
  const ids = { c1: 'brk:c', k1: 'brk:k', k2: 'brk:k', b1: 'brk:b', b2: 'brk:b', b3: 'brk:b', k3: 'brk:k3', s1: 'brk:s', s2: 'brk:s' };
  const ad_breaks = [ad('brk:c', [15, 177], ['c1'], 'brk:c', 1),
    ad('brk:k', [600, 709.92], ['k1', 'k2'], 'brk:k', 3, E), ad('brk:b', [616.68, 756.92], ['b1', 'b2', 'b3'], 'brk:k', 3, E),
    ad('brk:k3', [698.04, 764.92], ['k3'], 'brk:k', 3, E), ad('brk:s', [1560, 1692.92], ['s1', 's2'], 'brk:s', 1, E)];
  return { episode_id: 'ep_r2', ads: ads.map(a => ({ ...a, ad_break_id: ids[a.evidence_id] })), self_promo: [], screening: SCREENED, screened_no_ads: false,
    current_transcript_version_id: CUR, earlier_transcripts: [{ transcript_version_id: OLD, duration_s: 2900, fetch_date: '2026-09-21', fetch_date_basis: 'fetch_date' }],
    ad_breaks, ad_break_counts: { current: 1, earlier: 4, no_copy: 0 } };
}
const epRecord = () => ({ ref: 'ep:ep_r2', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
  record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
  episode: { episode_id: 'ep_r2', transcript_version_id: CUR, ep_title: 'An episode', show_title: 'Show A', pub_date: '2026-09-20',
    duration_min: 50, transcript_duration_s: 3000, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3000 }, selected_bounds: { start_s: 0, end_s: 3000 }, media: {},
  window: { segments: [], text_complete: true, next_cursor: null } });
const episodePage = reads => render('#/episode/ep_r2', p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_r2')) return { record: epRecord() };
  if (p === '/customer/episodes/ep_r2') return { episode: reads };
  return {};
});
const adsCount = html => text((html.match(/<span class="ew-ads-count">([\s\S]*?)<\/span>/) || [, ''])[1]).trim();
const block = (html, cls) => (html.match(new RegExp(`<div class="${cls}">[\\s\\S]*?<!--/${cls}--></div>`)) || [''])[0];
const adBreakBoxes = html => html.match(/<div class="ew-adbreak">[\s\S]*?<!--\/adbreak--><\/div>/g) || [];
const podBoxes = html => html.match(/<div class="ew-pod ew-pod-box">[\s\S]*?<\/ol><\/div>/g) || [];
// One ad = one pod box (2+ pieces of evidence) or one single-row list.
const adUnits = html => (html.match(/<div class="ew-pod ew-pod-box">|<ol class="ew-ad-rows">/g) || []).length;

test('R2-04 Episode: the earlier copy lists its ads in ad boxes under "Ad break · range · 3 ads"', async () => {
  const { html } = await episodePage(s001e0Like());
  assert.equal(adsCount(html), '1 ad; earlier copy: 4 ads');
  const earlier = block(html, 'ew-earlier');
  assert.ok(earlier, 'the earlier-copy block ends with its marker');
  assert.match(earlier, /<h3>Ads found in an earlier copy of the transcript<\/h3>/);
  assert.equal(adUnits(earlier), 4, 'one box per ad, as many as the count line says');
  const groups = adBreakBoxes(earlier);
  assert.equal(groups.length, 1, 'only the split group gets the label');
  assert.match(groups[0], /<h3 class="ew-adbreak-h">Ad break <span aria-hidden="true">·<\/span> <span class="ew-time"[^>]*>10:00–12:4\d<\/span> <span aria-hidden="true">·<\/span> 3 ads<\/h3>/);
  assert.equal(adUnits(groups[0]), 3);
  const order = [...groups[0].matchAll(/Kachava|She’s Birdie|She's Birdie|She&#39;s Birdie/g)].map(m => m[0].replace(/’|&#39;/, "'"));
  assert.deepEqual([...new Set(order)], ['Kachava', "She's Birdie"]);
  assert.match(text(groups[0]), /Kachava[\s\S]*She.s Birdie[\s\S]*Kachava/, 'Kachava, She’s Birdie, Kachava by start');
  const pods = podBoxes(earlier);
  assert.equal(pods.length, 3, 'Kachava (2), She’s Birdie (3) and Spinquest (2) are ad boxes');
  assert.match(text(pods[0]), /Evidence \(2\), some overlapping/);
  assert.match(text(pods[1]), /Evidence \(3\), some overlapping/);
  assert.doesNotMatch(groups[0], /Spinquest/, 'the lone ad sits outside the Ad break');
  assert.match(earlier, /Spinquest/);
  assert.doesNotMatch(earlier, /Jump to transcript/, 'earlier-copy times are not on the transcript below');
  // The current copy keeps its own box; no Ad break label there.
  const current = html.slice(0, html.indexOf('<div class="ew-earlier">'));
  assert.doesNotMatch(current, /ew-adbreak|Kachava/);
  assert.equal((text(html).match(/Ad break/g) || []).length, 1);
});

test('R2-04 Episode: an older server with no ads for the earlier copy keeps one row per piece of evidence', async () => {
  const reads = s001e0Like();
  reads.ad_breaks = reads.ad_breaks.filter(b => b.on_current_transcript);
  delete reads.ad_break_counts;
  const { html } = await episodePage(reads);
  const earlier = block(html, 'ew-earlier');
  assert.equal(adUnits(earlier), 8);
  assert.doesNotMatch(earlier, /ew-adbreak|ew-pod-box/);
  assert.doesNotMatch(adsCount(html), /\d/);
});

test('R2-04 Episode: no-copy evidence renders by server ad too', async () => {
  const N = { transcript_version_id: null, on_current_transcript: null };
  const ads = [epAd('c1', [15, 177], 'Lowes', { ad_break_id: 'brk:c' }),
    epAd('n1', [1000, 1060], 'Acme', { ...N, ad_break_id: 'brk:n' }), epAd('n2', [1030, 1090], null, { ...N, ad_break_id: 'brk:n' })];
  const reads = { ...s001e0Like(), ads, earlier_transcripts: [],
    ad_breaks: [ad('brk:c', [15, 177], ['c1'], 'brk:c', 1), ad('brk:n', [1000, 1090], ['n1', 'n2'], 'brk:n', 1, N)],
    ad_break_counts: { current: 1, earlier: 0, no_copy: 1 } };
  const { html } = await episodePage(reads);
  assert.equal(adsCount(html), '1 ad; no transcript copy: 1 ad');
  const loose = block(html, 'ew-unplaced');
  assert.match(text(loose), /Ads found with no recorded transcript copy; their times are not placed on this transcript\./);
  assert.equal(adUnits(loose), 1);
  assert.match(text(podBoxes(loose)[0] || ''), /Evidence \(2\), some overlapping/);
});

/* ======================================================================
   R2-05 Confirmed ads on Brands and Brand top shows
   ====================================================================== */
// PLAIN-ADS (DEP7) replaces R2-05: no confirmed number on Brands or Brand top shows.
test('PLAIN-ADS Brands: no confirmed number or column, even with confirmed_ads', async () => {
  const brands = [{ key: 'chatgpt', display: 'ChatGPT', brand: 'chatgpt', records: 13, ad_breaks: 7, confirmed: 3, confirmed_ads: 2, shows: 4, last_heard: '2026-10-09' }];
  const { html } = await render('#/brands', F.dirApi(brands));
  assert.match(html, /<span>7 ads<\/span> · <span>4 shows<\/span>/);
  assert.match(html, /<td class="ew-num lib-wide">7<\/td><td class="ew-num lib-wide">4<\/td>/);
  assert.doesNotMatch(text(html), /confirmed/i);
});

test('R2-05 Brands without confirmed_ads: no confirmed number and no Confirmed column', async () => {
  const brands = [{ key: 'chatgpt', display: 'ChatGPT', brand: 'chatgpt', records: 13, ad_breaks: 7, confirmed: 3, shows: 4, last_heard: '2026-10-09' }];
  const { html } = await render('#/brands', F.dirApi(brands));
  assert.doesNotMatch(text(html), /\d+ confirmed/);
  assert.doesNotMatch(html, />Confirmed<\/th>/);
  assert.doesNotMatch(html, /<td class="ew-num lib-wide">3<\/td>/);
});

test('PLAIN-ADS Brand top shows: "N ads", never a confirmed number', async () => {
  const detail = F.brandDetail({ top_shows: [
    { show_id: 'cs-a', display_title: 'Show A', reads: 5, ad_breaks: 3, confirmed: 2, confirmed_ads: 1, last_heard: '2026-10-09' },
    { show_id: 'cs-b', display_title: 'Show B', reads: 4, ad_breaks: 2, confirmed: 3, last_heard: '2026-10-01' }] });
  const { html } = await render('#/brand/chatgpt', F.brandApi(detail));
  assert.match(html, /<span class="lib-row-sub">3 ads · last heard Oct 9<\/span>/);
  assert.match(html, /<span class="lib-row-sub">2 ads · last heard Oct 1<\/span>/);
  assert.doesNotMatch(text(html), /confirmed/);
});

/* ======================================================================
   Search ad card: a brandless ad says "Brand not identified"
   ====================================================================== */
test('Search ad card head: an ad naming no brand says "Brand not identified"', async () => {
  const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
  const item = n => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', show_title: 'Show A', ep_title: 'Episode', episode_id: 'e1',
    pub_date: '2026-10-09', audio: 'unavailable', quotation_span: [10 * n, 10 * n + 30], ad_break_id: 'brk:u', transcript_version_id: 7,
    read: { brand: { key: null, display: null, confirmed: false }, display_span: [10 * n, 10 * n + 30], display_text: 'Go now', has_clip: false, said: { web_addresses: [], promo_codes: [] } },
    attribution: [] });
  const items = [item(1), item(2)];
  const ad_breaks = [{ id: 'brk:u', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [10, 50], read_count: 2, matching_read_count: 2,
    refs: items.map(i => i.ref), brands: [], pub_date: '2026-10-09', group_id: 'brk:u', group_ad_count: 1 }];
  const bundle = { query: { version: 2, mode: 'ads', q: 'go', sort: 'newest', page: 1, view: 'cards' }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 3 },
    result: { items, total: 2, dated_total: 2, undated_total: 0, total_relation: 'exact', per_page: 10,
      ad_breaks, break_total: 1, break_dated_total: 1, break_undated_total: 0 },
    summary: { week: [], status: [{ key: 'confirmed', count: 0 }, { key: 'finding', count: 2 }] } };
  const { html } = await render('#/search?mode=ads&q=go', p => {
    if (p === '/customer/research-query') return bundle;
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/shows') return { shows: [] };
    if (p.startsWith('/customer/filter-values?')) return { items: [] };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    return new Error('unexpected ' + p);
  });
  assert.match(html, /<span class="ew-brand-none">Brand not identified<\/span>/);
  assert.doesNotMatch(html, /No brand named/);
});
