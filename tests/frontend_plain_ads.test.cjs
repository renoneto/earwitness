/* PLAIN-ADS (DEP7, ws-app/HIGH-CONFIDENCE-DESIGN.md): customers see ads
   and mentions, never a review state. No "confirmed", "possible",
   "finding", "unresolved", "needs review", "under review", "verified"
   or "review team" on a customer page, in visible text or in aria-label
   and title attributes. A held ad (its brand waits for review inside
   EarWitness) reads "Brand not identified" and still counts toward no
   brand. Compare counts every ad the caller can see. "Report a mistake"
   (ads only) shows on Search cards and list rows, the episode and
   evidence pages and the Brand page's Recent ads, never to a share guest.
   Internal data is unchanged: the fixtures keep the server's status,
   review and basis fields.
   Run: node --test tests/frontend_plain_ads.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const STATUS = /\b(confirmed|unconfirmed|possible|unresolved|verified|unverified|needs review|under review|brand review|not counted yet|findings?|review team|not checked|name certainty|brand unclear)\b/i;
const decode = s => s.replace(/&nbsp;/g, ' ').replace(/&middot;/g, '·').replace(/&rsquo;/g, '’').replace(/&ldquo;|&rdquo;/g, '"')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
function surfaces(html) {
  const h = String(html).replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/g, ' ');
  const attrs = [...h.matchAll(/\s(?:aria-label|title|data-default)="([^"]*)"/g)].map(m => m[1]);
  return [h.replace(/<[^>]+>/g, ' '), ...attrs].map(decode);
}
function statusWords(html) {
  const out = [];
  for (let s of surfaces(html)) {
    s = s.replace(/\s+/g, ' ');
    for (const m of s.matchAll(new RegExp(STATUS.source, 'gi'))) out.push(s.slice(Math.max(0, m.index - 50), m.index + 40).trim());
  }
  return out;
}
const plain = (name, html) => assert.deepEqual(statusWords(html), [], `${name}: review state on a customer page`);
const text = F.text;

test('plain ads: the guard catches status words in text and attributes', () => {
  assert.ok(statusWords('<p>0 confirmed, 92 possible</p>').length >= 2);
  assert.ok(statusWords('<span title="Brand under review">x</span>').length > 0);
  assert.ok(statusWords('<p>Goes to the EarWitness review team.</p>').length > 0);
  assert.deepEqual(statusWords('<p>92 ads · Brand not identified · Report a mistake</p>'), []);
});

/* -- fixtures: server rows keep their internal review fields ------------ */
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const brand = (display, confirmed = false) => ({ key: display.toLowerCase(), display, confirmed });
const HELD = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const adItem = (n, span, b, over = {}) => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', review: null, show_title: 'Show A',
  ep_title: 'Episode ' + (over.ep || 'One'), episode_id: over.ep || 'e1', pub_date: '2026-10-09', audio: 'unavailable', quotation_span: span,
  show: { show_identity_id: 'cs-a' }, read: { brand: b, display_span: span, has_clip: false, needs_review: false },
  attribution: [{ brand: (b.key || 'x'), basis: 'suggested', channel: 'spoken' }], ...over });
function searchItems() {
  const a1 = adItem(1, [60, 120], brand('Quince', true), { status: 'confirmed', review: 'ad', ad_break_id: 'brk:a',
    attribution: [{ brand: 'quince', basis: 'confirmed', channel: 'spoken' }] });
  const b1 = adItem(2, [600, 660], brand('Quince'), { ad_break_id: 'brk:b', ep: 'e2', read: { brand: brand('Quince'), display_span: [600, 660], has_clip: false, needs_review: true } });
  const c1 = adItem(3, [900, 960], HELD, { ad_break_id: 'brk:c', ep: 'e3', attribution: [], read: { brand: HELD, display_span: [900, 960], has_clip: false, needs_review: true } });
  const d1 = adItem(4, [30, 50], brand('AG1'), { status: 'unresolved', review: 'unsure', ad_break_id: 'brk:d', ep: 'e4' });
  const items = [a1, b1, c1, d1];
  const breaks = items.map((it, i) => ({ id: it.ad_break_id, episode_id: it.episode_id, transcript_version_id: 7, on_current_transcript: true,
    span: it.quotation_span, read_count: 1, matching_read_count: 1, refs: [it.ref], pub_date: '2026-10-09', group_id: it.ad_break_id, group_ad_count: 1,
    brands: it.read.brand.under_review ? [] : [{ key: it.read.brand.key, display: it.read.brand.display, confirmed: !!it.read.brand.confirmed }] }));
  return { items, breaks };
}
function searchBundle(view = 'cards', q = 'quince') {
  const { items, breaks } = searchItems();
  return { query: { version: 2, mode: 'ads', q, sort: 'newest', page: 1, view }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 },
    result: { items, total: 4, dated_total: 4, undated_total: 0, total_relation: 'exact', per_page: 20,
      ad_breaks: breaks, break_total: 4, break_dated_total: 4, break_undated_total: 0 },
    summary: { week: [], status: [{ key: 'confirmed', count: 1, breaks: 1 }, { key: 'finding', count: 2, breaks: 2 }, { key: 'unresolved', count: 1, breaks: 1 }] } };
}
const BRANDS = [{ key: 'quince', display: 'Quince', confirmed: 0, confirmed_ads: 0, ad_breaks: 92, shows: 37, last_heard: '2026-10-19' },
  { key: 'ag1', display: 'AG1', confirmed: 0, confirmed_ads: 0, ad_breaks: 10, shows: 8, last_heard: '2026-10-18' }];
const searchApi = bundle => p => {
  if (p === '/customer/research-query') return bundle;
  if (p === '/customer/brands') return { brands: BRANDS };
  if (p === '/customer/shows') return { shows: [] };
  if (p.startsWith('/customer/filter-values?')) return { items: [{ value: 'quince', label: 'Quince', confirmed: true }] };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};

test('plain ads: Search cards and list say ads only, with Report a mistake on every ad', async () => {
  for (const view of ['cards', 'list']) {
    const { html } = await F.render('#/search?mode=ads&q=quince' + (view === 'list' ? '&view=list' : ''), searchApi(searchBundle(view)));
    assert.ok(html.length > 500, view + ' rendered');
    plain('Search ' + view, html);
    const t = text(html);
    assert.match(t, /Showing 1 to 4 of 4 ads/, view);
    assert.doesNotMatch(t, /Showing 1 to 4 of 4 ads ·/, view + ': no status mix after the count');
    assert.match(t, /Brand not identified/, view + ': a held ad');
    assert.equal((html.match(/<summary>Report a mistake<\/summary>/g) || []).length, 4, view + ': one report control per ad');
    assert.doesNotMatch(html, /Report a problem/);
  }
  // A brand-name query: one plain line, no name flag.
  const { html } = await F.render('#/search?mode=ads&q=quince', searchApi(searchBundle('cards', 'quince')));
  assert.match(text(html), /Quince is a brand heard in ads\. See where Quince advertises/);
});

test('plain ads: the Search route drops a stored status filter (old links)', async () => {
  let sent = null;
  const api = p => {
    if (p === '/customer/research-query') return searchBundle();
    return searchApi(searchBundle())(p);
  };
  const { win } = await F.render('#/search?mode=ads&statuses=confirmed', (p, o) => {
    if (p === '/customer/research-query') sent = o && o.body;
    return api(p, o);
  });
  assert.ok(sent, 'searched');
  assert.equal(sent.query.statuses, null);
  win.EW.current.route = '#/search?mode=ads&statuses=finding';
  assert.equal(win.EW.searchCriteria().statuses, null);
});

test('plain ads: report control wording, reasons and thank-you', async () => {
  const win = F.loadViews();
  const html = win.EWUI.reportControl('sr1:x');
  assert.match(html, /<summary>Report a mistake<\/summary>/);
  assert.deepEqual([...html.matchAll(/name="reason" value="([a-z_]+)"/g)].map(m => m[1]), ['not_an_ad', 'wrong_brand', 'other']);
  assert.match(text(html), /Not an ad.*Wrong brand.*Other.*Details \(optional\)/);
  assert.match(text(html), /Goes to EarWitness\./);
  plain('report control', html);
  const box = { outerHTML: '' };
  const form = { dataset: { reportRef: 'sr1:x' }, querySelector: sel => (sel.startsWith('input') ? { value: 'not_an_ad' } : sel.startsWith('textarea') ? { value: '' } : null),
    closest: () => box };
  let posted = null;
  win.EW.api = async (p, o) => { posted = [p, o]; return { report: { id: 1, status: 'received', replaced: false } }; };
  await win.EWViews.sendReport(form);
  assert.equal(posted[0], '/customer/records/' + encodeURIComponent('sr1:x') + '/report');
  assert.deepEqual(JSON.parse(JSON.stringify(posted[1].body)), { reason: 'not_an_ad', note: '' });
  assert.match(text(box.outerHTML), /✓ Your report was sent to the EarWitness team\. Nothing on this page changes for now\./);
  plain('report thanks', box.outerHTML);
});

/* -- Evidence and episode pages ----------------------------------------- */
const CUR = 36;
const EVREF = 'sr1:' + 'v'.repeat(32);
const evRecord = (over = {}) => ({ ref: EVREF, canonical_source_ref: EVREF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'quince', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 100, end_s: 150 },
  episode: { episode_id: 'ep_d1', show_title: 'Show A', ep_title: 'An episode', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 55.4, transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Show A', unresolved: 0 },
  evidence: { id: 'a2', episode_id: 'ep_d1', orig_start_s: 100, orig_end_s: 150, transcript_version_id: CUR }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', brand: { key: 'quince', display: 'Quince', confirmed: false }, needs_review: false, self_promo: false, confidence: 'medium',
    display_span: [100, 150], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Quince' } },
  window: { segments: [{ segment_id: 9, start_s: 100, end_s: 150, text: 'Go to Quince.', text_start: 0, text_end: 13 }], text_complete: true, next_cursor: null },
  ad_break: null, ...over });
const epAd = (id, span, b, over = {}) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: b, needs_review: false, self_promo: false, kind: 'rule', transcript_start_s: span[0], media: null,
  transcript_version_id: CUR, on_current_transcript: true, ...over });
const EP_ADS = [epAd('a1', [60, 120], brand('Quince')), epAd('a2', [100, 150], brand('Quince'), { status: 'confirmed' }),
  epAd('a3', [900, 960], HELD, { needs_review: true })];
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
const epReads = () => ({ episode_id: 'ep_d1', ads: EP_ADS.map((a, i) => ({ ...a, ad_break_id: 'brk:' + i })), self_promo: [], screening: SCREENED, screened_no_ads: false,
  current_transcript_version_id: CUR, earlier_transcripts: [], ad_break_counts: { current: 3, earlier: 0, no_copy: 0 },
  ad_breaks: EP_ADS.map((a, i) => ({ id: 'brk:' + i, episode_id: 'ep_d1', span: a.span, read_count: 1, refs: [a.evidence_id], brands: [],
    transcript_version_id: CUR, on_current_transcript: true, group_id: 'brk:' + i, group_ad_count: 1 })) });
const evidencePage = rec => F.render('#/evidence/' + encodeURIComponent(EVREF), p => {
  if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_d1') return { episode: epReads() };
  if (p === '/customer/brands') return { brands: BRANDS };
  return {};
});

test('plain ads: evidence page in every review state reads "Ad"', async () => {
  const base = evRecord();
  const cases = [
    ['finding', base],
    ['confirmed, times checked', evRecord({ status: 'confirmed', review: { judgment: 'ad', bounds_verified: 1 }, attribution: [{ brand: 'quince', basis: 'confirmed', channel: 'spoken' }],
      read: { ...base.read, display_basis: 'checked', brand: { ...base.read.brand, confirmed: true } } })],
    ['times corrected', evRecord({ read: { ...base.read, display_basis: 'corrected' } })],
    ['needs review', evRecord({ read: { ...base.read, needs_review: true, confidence: 'low' } })],
    ['unresolved', evRecord({ status: 'unresolved', review: { judgment: 'unsure', bounds_verified: 0 }, attribution: [] })],
  ];
  for (const [name, rec] of cases) {
    const { html } = await evidencePage(rec);
    assert.ok(/data-reader="evidence"/.test(html), name + ' rendered');
    plain('Evidence ' + name, html);
    assert.match(html, /<summary>Report a mistake<\/summary>/, name);
    assert.doesNotMatch(text(html), /A time shows where it was heard/, name + ': the player note is gone');
  }
});

test('plain ads: a held ad says "Brand not identified" and counts toward no brand', async () => {
  const base = evRecord();
  const { html } = await evidencePage(evRecord({ attribution: [], read: { ...base.read, brand: HELD, needs_review: true, said: undefined } }));
  plain('Evidence held', html);
  assert.match(text(html), /We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand\./);
  const h1 = html.slice(html.indexOf('<h1'), html.indexOf('</h1>'));
  assert.match(text(h1), /Brand not identified/);
  assert.doesNotMatch(h1, /Quince/, 'no guessed name');
});

test('plain ads: episode page ad list', async () => {
  const rec = { ref: 'ep:ep_d1', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
    record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
    episode: { episode_id: 'ep_d1', transcript_version_id: CUR, ep_title: 'An episode', show_title: 'Show A', pub_date: '2026-09-20',
      duration_min: 55.4, transcript_duration_s: 3324, transcript_is_current: true },
    show: { show_identity_id: 'cs-a', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3324 }, selected_bounds: { start_s: 0, end_s: 3324 }, media: {},
    window: { segments: [{ segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 }], text_complete: true, next_cursor: null } };
  const { html } = await F.render('#/episode/ep_d1', p => {
    if (p === '/customer/records/' + encodeURIComponent('ep:ep_d1')) return { record: rec };
    if (p === '/customer/episodes/ep_d1') return { episode: epReads() };
    return {};
  });
  assert.match(html, /Ads in this episode/);
  plain('Episode', html);
  assert.match(text(html), /Brand not identified/);
  assert.ok((html.match(/<summary>Report a mistake<\/summary>/g) || []).length >= 3);
});

/* -- Brands, Brand, Show, Compare ---------------------------------------- */
test('plain ads: Brands directory has no Confirmed column', async () => {
  const { html } = await F.render('#/brands', F.dirApi(F.DIRBRANDS));
  plain('Brands', html);
  const t = text(html);
  assert.match(t, /Ads with Brand not identified aren’t counted toward any brand\./);
  assert.match(t, /Last heard/);
});

test('plain ads: Brand page header, shows, recent ads with Report a mistake, offers', async () => {
  const detail = F.brandDetail({ confirmed_occurrences: [], findings: ['sr1:f1', 'sr1:f2'], detections: 13 });
  detail.brand.top_shows[0].confirmed_ads = 0;
  const items = F.BRAND_ITEMS.map(it => ({ ...it, status: 'finding', read: { ...it.read, display_basis: 'checked', brand: { ...it.read.brand, confirmed: false } } }));
  const { html } = await F.render('#/brand/chatgpt', F.brandApi(detail, items));
  plain('Brand', html);
  const t = text(html);
  assert.match(t, /In this collection: 7 ads in 6 episodes across 3 shows · last heard/);
  assert.match(t, /Shows where it was heard/);
  assert.doesNotMatch(html, /Confirmed ads only/);
  assert.match(t, /Ads \(2\)/, 'repeated offers counted as ads');
  assert.match(t, /How the offer changed/, 'offers built from every ad that is not held, with checked times');
  assert.match(t, /These are ads we heard in this collection, not proof they ran as bought, not an advertiser roster or a market-share estimate\./);
  const recent = html.slice(html.indexOf('id="reads-h"'));
  assert.ok((recent.match(/<summary>Report a mistake<\/summary>/g) || []).length >= 2, 'Recent ads rows carry the report control');
});

test('plain ads: Show sponsors caption and note', async () => {
  const { html } = await F.render('#/show/cs-1', F.showApi());
  plain('Show', html);
  assert.match(text(html), /Brands heard in this show's ads\. Self-promotion is not counted\./);
});

test('plain ads: Compare asks for every ad and shows the ads count', async () => {
  let sent = null;
  const data = { cells: [{ brand: 'chatgpt', a: { count: 9, ads: 7, refs: ['sr1:x'], coverage: { matched_in_scope: 9 }, shows: 3, last_heard: '2026-10-19',
    selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'all_results' } } } },
  { brand: 'progressive', a: { count: 13, ads: 13, refs: ['sr1:y'], coverage: { matched_in_scope: 13 }, shows: 5, last_heard: '2026-10-01',
    selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'all_results' } } } }], shared_scale: 13 };
  const api = F.compareApi(data);
  const { html } = await F.render('#/compare?brands=chatgpt,progressive', p => {
    if (p.startsWith('/customer/compare')) sent = new URLSearchParams(p.split('?')[1]);
    return api(p);
  });
  plain('Compare', html);
  assert.equal(JSON.parse(sent.get('query')).statuses, null, 'no confirmed-only scope');
  const t = text(html);
  assert.match(t, /Brands · ads/);
  assert.match(html, /<h2 id="compare-h">Ads<\/h2>/);
  assert.match(t, /Ads found in the episodes we hold, each counted once however much evidence it has\./);
  assert.match(t, /7 ads 3 shows · last heard Oct 19/);
  assert.match(t, /13 ads/);
  assert.match(t, /Ads by brand and period/);
  assert.doesNotMatch(t, /only confirmed ads are compared/);
  const zero = await F.render('#/compare?brands=chatgpt,progressive', F.compareApi({ cells: [{ brand: 'chatgpt', a: { count: 0, ads: 0 } }], shared_scale: 0 }));
  plain('Compare zero', zero.html);
  assert.match(text(zero.html), /No ads were found for these brands in the episodes we hold\. This does not show they did not advertise\./);
});

/* -- Overview, Alerts, Settings ------------------------------------------ */
test('plain ads: Overview', async () => {
  const totals = [{ ...F.OV_TOTALS[0], new_ads: 2, past_ads: 1, latest_new_kind: 'new_finding', latest_new_status: 'confirmed',
    baseline_held_ads_for_brand_review: 1 }];
  const items = F.OV_ITEMS.map((it, i) => ({ ...it, status: i ? 'finding' : 'confirmed', read: { ...it.read, brand: { ...it.read.brand, confirmed: !i }, needs_review: i === 2 } }));
  const { html } = await F.render('#/overview', F.overviewApi({ ov: F.overview({ alert_totals: totals }), items }));
  plain('Overview', html);
  assert.match(text(html), /Latest new match: an ad\./);
});

test('plain ads: Alerts and an alert with held ads', async () => {
  const events = [F.ev(1, 'epA', [10, 40], 'brk:a'), F.ev(2, 'epB', [30, 60], 'brk:b', { summary: F.SUM('epB', [30, 60], { status: 'confirmed' }) })];
  const list = await F.render('#/alerts', F.alertApi(F.ALERT, events));
  plain('Alerts', list.html);
  const alert = { ...F.ALERT, query: { ...F.ALERT.query, statuses: ['confirmed'] },
    current_matches: { ...F.ALERT.current_matches, held_for_brand_review: 2 } };
  const one = await F.render('#/alert/1', F.alertApi(alert, events));
  plain('Alert', one.html);
  const t = text(one.html);
  assert.match(t, /2 more ads have Brand not identified and aren’t counted here\./);
  assert.match(t, /status: Ads/, 'a stored status reads "Ads" in the criteria');
});

test('plain ads: Settings collection definitions', async () => {
  const { html } = await F.render('#/settings', F.settingsApi);
  plain('Settings', html);
  assert.match(text(html), /Ads: what we found in the episodes we hold\. An ad found is not proof it ran as bought\./);
});

test('plain ads: filter and alert dialogs offer no status group', () => {
  const src = fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /'Record status'/);
  assert.doesNotMatch(src, /\['confirmed', 'Confirmed ads'\]/);
  assert.doesNotMatch(src, /Ad findings/);
});

test('plain ads: a share guest gets no report control', async () => {
  const win = F.loadViews();
  await new Promise(r => setTimeout(r, 0));
  win.EW.current.hosted = true;
  win.EW.current.principal = 'oidc:https://i.example:guest';
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async p => (p.startsWith('/shared/evidence/') ? { evidence: { title: 'Quince ad', text: 'Go to Quince.', evidence_id: 'a2', status: 'finding' } } : {});
  const route = '#/shared?workspace=ws&share_id=s1&type=evidence&id=a2';
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 0));
  assert.match(main.innerHTML, /Shared record/);
  assert.doesNotMatch(main.innerHTML, /Report a mistake|report-form/);
  plain('Shared guest', main.innerHTML);
});

/* -- PLAIN-ADS-FIX1 (plain-ads code r1 + screen r1) ----------------------- */
const HELD_TAG = /ew-tag-review|data-tag="review"/;
const HELD_PLAIN = /<span class="ew-brand-none">Brand not identified<\/span>/;

// SCREEN R1-01: an ad whose times were never checked quotes its whole
// detected window, which can run into another brand's ad. The comparison
// quotes only reads whose words are the ad's own (display_basis 'checked').
test('plain ads FIX1: "How the offer changed" never quotes a window that spans two brands', async () => {
  const detail = F.brandDetail({ confirmed_occurrences: [], findings: ['sr1:f1', 'sr1:f2'], detections: 13 });
  const win = (n, words, basis) => F.brandItem(n, 'brk:' + n, { status: 'finding',
    read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: false }, display_span: [18, 120], display_basis: basis,
      display_text: words, has_clip: false, said: { web_addresses: [], promo_codes: [] } } });
  const spans = [win(1, 'Try ChatGPT today. This episode is sponsored by HexClad. HexClad cookware is nice.', 'detected'),
    win(2, 'Try ChatGPT. Menopause care from Alloy Health, talk to a doctor.', 'corrected'),
    win(3, 'Try ChatGPT now. Back to the show.', 'detected')];
  const { html } = await F.render('#/brand/chatgpt', F.brandApi(detail, spans));
  plain('Brand, unchecked windows', html);
  assert.doesNotMatch(html, /How the offer changed/, 'no comparison from unchecked windows');
  assert.doesNotMatch(html, /HexClad|Alloy Health/, 'another brand\'s ad is never quoted');
  // Times set but not checked ('corrected') are not the ad's own words either.
  const corrected = [win(1, 'Try ChatGPT. This episode is sponsored by HexClad.', 'corrected'), win(2, 'Try ChatGPT. Alloy Health menopause care.', 'corrected')];
  const c = await F.render('#/brand/chatgpt', F.brandApi(detail, corrected));
  assert.doesNotMatch(c.html, /How the offer changed|HexClad|Alloy Health/, 'corrected windows are not compared');
  // Two reads whose times were checked: their own words compare.
  const checked = [win(1, 'Try ChatGPT and get a month free.', 'checked'), win(2, 'Try ChatGPT and get two months free.', 'checked'),
    win(3, 'Try ChatGPT. This episode is sponsored by HexClad.', 'detected')];
  const ok = await F.render('#/brand/chatgpt', F.brandApi(detail, checked));
  assert.match(ok.html, /How the offer changed/);
  assert.doesNotMatch(ok.html, /HexClad/);
});

// SCREEN R1-02: a held ad reads as plain text, never the amber dashed
// "pending" tag, wherever it shows.
test('plain ads FIX1: a held ad is plain "Brand not identified" text on every surface', async () => {
  for (const view of ['cards', 'list']) {
    const { html } = await F.render('#/search?mode=ads&q=quince' + (view === 'list' ? '&view=list' : ''), searchApi(searchBundle(view)));
    assert.doesNotMatch(html, HELD_TAG, 'Search ' + view);
    assert.match(html, HELD_PLAIN, 'Search ' + view);
  }
  const base = evRecord();
  const ev = await evidencePage(evRecord({ attribution: [], read: { ...base.read, brand: HELD, needs_review: true, said: undefined } }));
  assert.doesNotMatch(ev.html, HELD_TAG, 'evidence page');
  assert.match(ev.html, /<h1 class="ew-reader-h1">Brand not identified<\/h1>/);
  const items = F.OV_ITEMS.map((it, i) => (i ? it : { ...it, read: { ...it.read, brand: HELD, needs_review: true } }));
  const ov = await F.render('#/overview', F.overviewApi({ items }));
  assert.doesNotMatch(ov.html, HELD_TAG, 'Overview recent ads');
  // FIX3 (screen R3-02): Recent ads lists only ads with a brand, so the
  // held ad is left out there (test 'Overview Recent ads lists only ...').
  const win = F.loadViews();
  assert.doesNotMatch(win.EWUI.brandHtml(HELD), HELD_TAG);
  assert.match(win.EWUI.brandHtml(HELD), HELD_PLAIN);
  assert.doesNotMatch(win.EWUI.readFacts({ read: { brand: HELD } }), HELD_TAG);
  assert.doesNotMatch(win.EWUI.resultRow(searchItems().items[2]), HELD_TAG);
});

test('plain ads FIX1: the episode page lists a held ad as plain text', async () => {
  const rec = { ref: 'ep:ep_d1', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
    record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
    episode: { episode_id: 'ep_d1', transcript_version_id: CUR, ep_title: 'An episode', show_title: 'Show A', pub_date: '2026-09-20',
      duration_min: 55.4, transcript_duration_s: 3324, transcript_is_current: true },
    show: { show_identity_id: 'cs-a', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3324 }, selected_bounds: { start_s: 0, end_s: 3324 }, media: {},
    window: { segments: [{ segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 }], text_complete: true, next_cursor: null } };
  const { html } = await F.render('#/episode/ep_d1', p => {
    if (p === '/customer/records/' + encodeURIComponent('ep:ep_d1')) return { record: rec };
    if (p === '/customer/episodes/ep_d1') return { episode: epReads() };
    return {};
  });
  assert.doesNotMatch(html, HELD_TAG);
  assert.match(html, /Brand not identified/);
});

// SCREEN R1-04 (+ R1-13): the held evidence page says "Brand not
// identified" once, with no "Not shown" rows.
test('plain ads FIX1: the held evidence page says "Brand not identified" once', async () => {
  const base = evRecord();
  const { html } = await evidencePage(evRecord({ attribution: [], read: { ...base.read, brand: HELD, needs_review: true, said: undefined } }));
  const t = text(html);
  assert.equal((t.match(/Brand not identified/gi) || []).length, 1, t);
  assert.doesNotMatch(t, /Not shown/);
  assert.match(t, /We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand\./);
  assert.match(t, /How the time was found/);
  plain('Evidence held', html);
});

// CODE PA-R1-02: the personal-note page voices no review state.
test('plain ads FIX1: the personal-note page has no reviewer state', async () => {
  const note = { id: 7, reason: 'Check the brand read', note_status: 'open', revision: 1, created_at: '2026-10-09T10:00:00Z',
    updated_at: '2026-10-09T10:00:00Z', delivery_context: 'local_only', review_status: 'ad', ref: EVREF, text: 'Go to Quince.' };
  const { html } = await F.render('#/note/7', p => (p === '/customer/mistakes/7' ? { note }
    : p.startsWith('/customer/') ? { items: [], notes: [], total: 0, total_relation: 'exact' } : {}));
  assert.match(html, /Personal note/);
  const t = text(html);
  assert.doesNotMatch(t, /Reviewer state|Review and verification|changing review/);
  assert.match(t, /Your status is yours to change\./);
  plain('Note', html);
});

// SCREEN R1-07: no preselected reason; the browser asks for one.
test('plain ads FIX1: the report form preselects no reason and requires one', () => {
  const html = F.loadViews().EWUI.reportControl('sr1:x');
  assert.doesNotMatch(html, /checked/);
  assert.equal((html.match(/name="reason" value="[a-z_]+" required/g) || []).length, 3);
});

// SCREEN R1-12: the replaced report is said plainly.
test('plain ads FIX1: a replaced report says it updated the earlier one', async () => {
  const win = F.loadViews();
  const box = { outerHTML: '' };
  const form = { dataset: { reportRef: 'sr1:x' }, querySelector: sel => (sel.startsWith('input') ? { value: 'wrong_brand' } : sel.startsWith('textarea') ? { value: '' } : null),
    closest: () => box };
  win.EW.api = async () => ({ report: { id: 1, status: 'received', replaced: true } });
  await win.EWViews.sendReport(form);
  assert.match(text(box.outerHTML), /✓ We updated your earlier report on this ad\. Nothing on this page changes for now\./);
  assert.doesNotMatch(box.outerHTML, /on this evidence/);
});

// SCREEN R1-08: "Latest past match: an ad."
test('plain ads FIX1: Overview says "Latest past match: an ad."', async () => {
  const totals = [{ ...F.OV_TOTALS[0], new_ads: 0, past_ads: 92, new_matches: 0, past_matches: 92, latest_new_kind: null, latest_past_kind: 'new_finding' }];
  const { html } = await F.render('#/overview', F.overviewApi({ ov: F.overview({ alert_totals: totals }) }));
  assert.match(text(html), /Latest past match: an ad\./);
});

// SCREEN R1-11, R1-14: no "brand namer", no "who owns it was not checked".
test('plain ads FIX1: evidence facts say no "brand namer" and no ownership caveat', async () => {
  const base = evRecord();
  const said = { web_addresses: ['quince.com'], promo_codes: [], spoken_brand: 'Kwince', address_brand: 'Quince' };
  const { html } = await evidencePage(evRecord({ read: { ...base.read, said },
    window: { segments: [{ segment_id: 9, start_s: 100, end_s: 150, text: 'Go to Kwince dot com.', text_start: 0, text_end: 21 }], text_complete: true, next_cursor: null } }));
  const t = text(html);
  assert.doesNotMatch(t, /brand namer|who owns it/i);
  const brand = await F.render('#/brand/chatgpt', F.brandApi(F.brandDetail(), F.BRAND_ITEMS.map(it => ({ ...it, read: { ...it.read, said: { web_addresses: ['chatgpt.com'], promo_codes: [] } } }))));
  assert.doesNotMatch(brand.html, /Who owns it/);
  assert.match(brand.html, /title="Heard in this ad\."/);
});

/* -- PLAIN-ADS-FIX2 (plain-ads screen r2, code r2 INFO) ------------------- */
// PA-SCR-R2-01: the Search filter note's held label is plain text, and no
// customer style paints a held ad in the amber review colour.
test('plain ads FIX2: the filter note says Brand not identified as plain text; no amber held style is left', async () => {
  // The rail shows at 1200 px and up: the same render as F.render, wide.
  const win = F.loadViews();
  await new Promise(r => setTimeout(r, 0));
  win.matchMedia = q => ({ matches: /min-width: 1200px/.test(q) });
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {};
  win.EW.current.hosted = false;
  const api = searchApi(searchBundle('cards'));
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = win.EW.current.route = '#/search?mode=ads&q=quince';
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 0));
  const html = main.innerHTML;
  const note = (html.match(/<p class="ew-rail-note">[\s\S]*?<\/p>/) || [''])[0];
  assert.ok(note, 'the filter rail note renders');
  assert.match(note, HELD_PLAIN);
  assert.doesNotMatch(note, /<b>/);
  assert.equal(text(note).replace(/\s+/g, ' ').trim(), 'Brand filters use the brand only. Ads with Brand not identified match no brand.');
  for (const file of ['styles.css', 'search.css', 'evidence.css']) {
    const css = fs.readFileSync(path.join(F.APP, file), 'utf8');
    const held = css.split('}').filter(rule => /\.ew-rail-note b\b|\.is-review\b/.test(rule.split('{')[0] || ''));
    assert.deepEqual(held.filter(rule => /--ew-review/.test(rule)).map(r => r.trim()), [], file + ': amber style on a held ad');
  }
});

// PA-SCR-R2-02: the facts card lists only what was found.
test('plain ads FIX2: evidence facts show only what was found', async () => {
  const base = evRecord();
  const NONE = /None found|No web address found|Not checked|not checked|Not recorded/;
  for (const said of [{ web_addresses: [], promo_codes: [], spoken_brand: 'Quince' }, null]) {
    const { html } = await evidencePage(evRecord({ read: { ...base.read, said } }));
    const card = html.slice(html.indexOf('ew-facts-card'), html.indexOf('ew-about'));
    const t = text(card);
    assert.doesNotMatch(t.replace(/Brand heard in the audio\s*Not recorded/, ''), NONE, JSON.stringify(said));
    assert.doesNotMatch(t, /Web address read out|Brand the address points to|Promo code read out/, JSON.stringify(said));
    assert.match(t, /How the time was found/);
  }
  const found = { web_addresses: ['quince.com'], promo_codes: ['QUINCE20'], spoken_brand: 'Quince' };
  const { html } = await evidencePage(evRecord({ read: { ...base.read, said: found } }));
  const t = text(html.slice(html.indexOf('ew-facts-card'), html.indexOf('ew-about')));
  assert.match(t, /Web address read out\s*quince\.com/);
  assert.match(t, /Promo code read out\s*QUINCE20/);
  assert.doesNotMatch(t, /Brand the address points to/, 'no owner recorded: no row');
  assert.doesNotMatch(t, /Not checked|not checked|where it leads/);
  const owned = await evidencePage(evRecord({ read: { ...base.read, said: { ...found, address_brand: 'Quince' } } }));
  assert.match(text(owned.html), /Brand the address points to\s*Quince · matches what was heard/);
});

// PA-SCR-R2-03: one time range per ad on the evidence page, in plain words.
test('plain ads FIX2: the evidence page shows one time range and no "detected" wording', async () => {
  const base = evRecord();
  const held = evRecord({ attribution: [], read: { ...base.read, brand: HELD, needs_review: true, said: undefined } });
  for (const rec of [base, held]) {
    const { html } = await evidencePage(rec);
    const t = text(html);
    assert.doesNotMatch(t, /detected/i);
    assert.doesNotMatch(t, /Times shown/);
    assert.doesNotMatch(t, /Selected \d/);
    assert.match(t, /Complete text\./);
    assert.match(t, /No clip for this ad\. The words are below\./);
  }
  // A mention passage shares the player: it is never called an ad.
  const passage = await evidencePage(evRecord({ record_kind: 'transcript_passage' }));
  assert.match(passage.html, /data-reader="passage"/);
  assert.match(text(passage.html), /No clip is kept for this passage\. The words are below\./);
  assert.doesNotMatch(text(passage.html), /detected|kept for this ad/);
  assert.match(text(passage.html), /Selected \d/, 'a passage keeps its selected range');
  const src = fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /detected passage|full detected window/);
  assert.match(src, /noClip: 'No clip for this ad\. The words are below\.'/);
  assert.match(src, /passage: 'We found an ad at this time in this episode\.'/);
});

// CODE INFO PA-R2-07: the Saved notes tab names no review or verification.
test('plain ads FIX2: the Saved notes tab says only that notes are yours', () => {
  const src = fs.readFileSync(path.join(F.APP, 'saved.js'), 'utf8');
  assert.doesNotMatch(src, /Review and verification/);
  assert.match(src, /<p class="saved-foot">Notes are yours\.<\/p>/);
});

/* -- PLAIN-ADS-FIX3 ------------------------------------------------------ */
// Screen R3-02 (branch part) + the lead's checked-brand rule: Overview ›
// Recent ads lists only ads whose brand shows (the server sends a brand
// that is not checked as held, like an ad held for brand review), and
// quotes no text.
test('plain ads FIX3: Overview Recent ads lists only ads with a brand and quotes nothing', async () => {
  const held = F.ovItem(1, 'brk:h', { text: 'held words here', read: { brand: HELD, display_span: [60, 120], needs_review: true } });
  const noRead = F.ovItem(2, 'brk:n', { text: 'withheld words', read: null });
  const earlier = F.ovItem(3, 'brk:e', { read: { historical: true } });
  const named = F.ovItem(4, 'brk:q', { text: "But I know what I'm getting in jail and co-aker.",
    read: { brand: { key: 'quince', display: 'Quince', confirmed: false }, display_span: [60, 120] } });
  const { html } = await F.render('#/overview', F.overviewApi({ items: [held, noRead, earlier, named] }));
  const start = html.indexOf('ew-ov-recent');
  assert.ok(start > 0, 'recent ads card');
  const card = html.slice(start, html.indexOf('</section>', start));
  const t = text(card);
  assert.match(t, /Quince/);
  assert.doesNotMatch(t, /Brand not identified|Ad record|Earlier version/);
  assert.doesNotMatch(t, /getting in jail|held words|withheld words/, 'no quote');
  assert.doesNotMatch(card, /ew-ov-excerpt/);
  assert.match(t, /Open evidence/);
  // Only held ads: the card says none were found, never a held row.
  const none = await F.render('#/overview', F.overviewApi({ items: [held, noRead] }));
  const s2 = none.html.indexOf('ew-ov-recent');
  assert.doesNotMatch(text(none.html.slice(s2, none.html.indexOf('</section>', s2))), /Brand not identified/);
});

// Screen R3-01: a list group header shows the range of the reads it lists,
// never the minute window beside the ad's own time.
test('plain ads FIX3: a Search group header shows its reads\' time range, not the window', async () => {
  const span = [546, 585];
  const it = adItem(7, span, HELD, { ad_break_id: 'brk:w', attribution: [], read: { brand: HELD, display_span: span, has_clip: false, needs_review: true } });
  const bundle = searchBundle('list');
  bundle.result.items = [it];
  bundle.result.ad_breaks = [{ id: 'brk:w', episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true, span: [540, 600],
    read_count: 1, matching_read_count: 1, refs: [it.ref], pub_date: '2026-10-09', group_id: 'brk:w', group_ad_count: 1, brands: [] }];
  bundle.result.total = bundle.result.break_total = 1;
  for (const view of ['list', 'cards']) {
    bundle.query.view = view;
    const { html } = await F.render('#/search?mode=ads&q=quince' + (view === 'list' ? '&view=list' : ''), searchApi(bundle));
    const heads = [...html.matchAll(/<span class="ew-break-time">([\s\S]*?)<\/span><span class="ew-break-count">/g)].map(m => text(m[1]));
    assert.ok(heads.length >= 1, view + ': a group header');
    for (const h of heads) {
      assert.match(h, /9:06–9:45/, view);
      assert.doesNotMatch(h, /9:00|10:00/, view + ': the window');
    }
  }
  // No read time: the break's own span stays.
  const win = F.loadViews();
  assert.match(text(win.EWViews.breakHead({ span: [540, 600], read_count: 1, brands: [] }, 'h1', [{ read: { display_span: [null, null] } }])), /9:00–10:00/);
});

// Screen R3-05 and R3-06: note wording names no review state.
test('plain ads FIX3: the saved-note notice and the note box say notes are yours', async () => {
  const win = F.loadViews();
  win.EW.current.savedNote = { id: 4, revision: 1 };
  const notice = text(win.EWViews.savedNoteNotice());
  assert.match(notice, /Note saved\. Your note is saved in this workspace\. Notes are yours; they do not report a mistake to EarWitness\./);
  assert.doesNotMatch(notice, /review state/i);
  const { html } = await F.render('#/evidence/' + encodeURIComponent(EVREF), p => {
    if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: evRecord() };
    if (p.endsWith('/mistake')) return { mistakes: [{ id: 4, status: 'open', reason: 'Check the Quince offer wording' }] };
    if (p === '/customer/episodes/ep_d1') return { episode: epReads() };
    if (p === '/customer/brands') return { brands: BRANDS };
    return {};
  });
  const t = text(html);
  assert.match(t, /Your note: “Check the Quince offer wording” Notes are yours; they do not report a mistake to EarWitness\./);
  assert.doesNotMatch(t, /· open\.|It is not a report to EarWitness/);
});

// Screen R3-07: the episode page note says "its ads".
test('plain ads FIX3: the episode note says "its ads"', () => {
  const src = fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /its detected ads/);
  assert.match(src, /episodeNote: 'This page shows this episode and its ads\. A bookmark saves the episode\.'/);
});

/* -- PLAIN-ADS-FIX4 -------------------------------------------------------- */
// Screen PA-SCR-R4-04: a query that is a key merged into a brand row (an
// alias such as "schwab") gets the brand line, named by the row.
test('plain ads FIX4: the brand line resolves a merged key to its brand', async () => {
  const brands = [{ key: 'charles schwab', display: 'Charles Schwab', keys: ['charles schwab', 'choiceology', 'schwab'], ad_breaks: 40, shows: 15, last_heard: '2026-10-17' },
    { key: 'helix sleep', display: 'Helix Sleep', keys: ['helix sleep', 'helixsleep'], ad_breaks: 11, shows: 6, last_heard: '2026-10-17' }];
  const api = bundle => p => (p === '/customer/brands' ? { brands } : searchApi(bundle)(p));
  for (const [q, name, key] of [['schwab', 'Charles Schwab', 'charles schwab'], ['HelixSleep', 'Helix Sleep', 'helix sleep'], ['Charles Schwab', 'Charles Schwab', 'charles schwab']]) {
    const { html } = await F.render('#/search?mode=ads&q=' + encodeURIComponent(q), api(searchBundle('cards', q)));
    const t = text(html);
    assert.match(t, new RegExp(`${name} is a brand heard in ads\\. See where ${name} advertises`), q);
    assert.ok(html.includes('#/brand/' + encodeURIComponent(key)), q + ': links the row');
  }
  // A word that is no key of any row gets no line.
  const { html } = await F.render('#/search?mode=ads&q=schwa', api(searchBundle('cards', 'schwa')));
  assert.doesNotMatch(text(html), /is a brand heard in ads/);
});

// Screen PA-SCR-R4-05: the note box names no state.
test('plain ads FIX4: the note box says "Your note:" with no "(open)"', async () => {
  for (const status of ['open', 'acknowledged', 'resolved']) {
    const { html } = await F.render('#/evidence/' + encodeURIComponent(EVREF), p => {
      if (p === '/customer/records/' + encodeURIComponent(EVREF)) return { record: evRecord() };
      if (p.endsWith('/mistake')) return { mistakes: [{ id: 4, status, reason: 'Check the Quince offer wording' }] };
      if (p === '/customer/episodes/ep_d1') return { episode: epReads() };
      if (p === '/customer/brands') return { brands: BRANDS };
      return {};
    });
    const t = text(html);
    assert.match(t, /Your note: “Check the Quince offer wording” Notes are yours; they do not report a mistake to EarWitness\./, status);
    assert.doesNotMatch(t, /Your note \(/, status);
  }
});

// Screen PA-SCR-R4-06: ads on the first page but none with a shown brand is
// not "No ads found".
test('plain ads FIX4: Overview Recent ads never says "No ads found" when ads exist', async () => {
  const held = n => F.ovItem(n, 'brk:h' + n, { read: { brand: HELD, display_span: [60, 120], needs_review: true } });
  const card = html => { const s = html.indexOf('ew-ov-recent'); return text(html.slice(s, html.indexOf('</section>', s))); };
  const some = await F.render('#/overview', F.overviewApi({ items: [held(1), held(2), held(3)] }));
  assert.doesNotMatch(card(some.html), /No ads found/);
  assert.match(card(some.html), /The newest ads are in Search ?\./);
  assert.ok(/ew-ov-recent[\s\S]*?href="#\/search"[^>]*>Search<\/a>\./.test(some.html), 'links Search');
  const none = await F.render('#/overview', F.overviewApi({ items: [] }));
  assert.match(card(none.html), /No ads found\./, 'an empty collection still says so');
});

// Screen PA-SCR-R4-07: below 600 px the Brand page's Recent ads rows put the
// time on its own line (never wrapped mid-range) and give show · episode the
// row's width.
test('plain ads FIX4: Brand page Recent ads rows at 390 px', () => {
  const css = fs.readFileSync(path.join(F.APP, 'library.css'), 'utf8');
  const start = css.indexOf('@media (max-width: 599px)');
  assert.ok(start > 0);
  let depth = 0, end = start;
  for (let i = css.indexOf('{', start); i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) { end = i; break; }
  }
  const narrow = css.slice(start, end);
  const rule = sel => { const m = narrow.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')); return m ? m[1] : ''; };
  assert.match(rule('.lib-read-time .ew-time'), /white-space:\s*nowrap/);
  assert.doesNotMatch(narrow, /\.lib-read-time \.ew-time \{ white-space: normal; \}/);
  assert.match(rule('.lib-read-time'), /grid-row:\s*1/);
  assert.match(rule('.lib-read-where'), /grid-column:\s*2 \/ -1/);
  assert.match(rule('.lib-read-where'), /grid-row:\s*2/);
});
