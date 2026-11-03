/* Row 1 (d16), frontend B: customers see ads, not reads, on Overview,
   Brands, Brand, Show, Compare, Alerts, Campaigns, Saved and Settings
   (plan Task 4.1; D/D1-CONTRACT.md). Counts come only from the server's
   ad fields through adCount; an older server with no ad field shows no
   number. Also row 11 (" ads" alert-name suffix) and review F3 (the
   decision-13 sentence in comparisonRows, said once per page).
   Run: node --test tests/frontend_uat_d1b.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const F = require('./frontend_uat_d1b_fixtures.cjs');
const { render, text } = F;

const NO_CHECKS = 'No checks entered for this placement. The brief text is not read automatically; add Message, Offer, Code or Length to check them.';
const count = (hay, needle) => hay.split(needle).length - 1;

/* -- Brands directory -- */
test('d1b Brands: sort "Most ads", an Ads column, no "Reads heard"; counts are ads', async () => {
  const { html } = await render('#/brands', F.dirApi(F.DIRBRANDS));
  assert.match(html, /<option value="reads" selected>Most ads<\/option>/);
  assert.match(html, /<th scope="col" class="ew-num lib-wide" aria-sort="descending">Ads ↓<\/th>/);
  assert.doesNotMatch(html, /Reads heard|Ad breaks|Most ad breaks/);
  const order = [...html.matchAll(/<a class="lib-name" href="#\/brand\/[^"]*">([^<]+)<\/a>/g)].map(m => m[1]);
  assert.deepEqual(order, ['Progressive', 'ChatGPT', 'QVC']);
  assert.match(html, /<span>7 ads<\/span> · <span>3 shows<\/span>/);
  assert.match(html, /of 3 brands · counts are ads heard in this collection<\/caption>/, 'one space before the dot');
  assert.match(html, /<td class="ew-num lib-wide">7<\/td><td class="ew-num lib-wide">3<\/td>/, 'ChatGPT: 7 ads, then 3 shows; PLAIN-ADS: no Confirmed column; no read column');
  assert.doesNotMatch(html, /confirmed/i);
});

test('d1b Brands from an older server (no ad field): no number, no Ads column', async () => {
  const { html } = await render('#/brands', F.dirApi(F.OLD_DIRBRANDS));
  assert.doesNotMatch(html, />Ads( ↓)?<\/th>|Reads heard/);
  assert.doesNotMatch(text(html), /\b\d+ (ads?|reads?)\b/);
  assert.doesNotMatch(text(html), /\b13\b|\b14\b/);
});

test('d1b directoryCard: a brand card counts ads, none without the ad field', () => {
  const win = F.loadViews();
  assert.match(win.EWUI.directoryCard({ key: 'ag1', display: 'AG1', records: 9, ad_breaks: 4 }, { kind: 'brand' }), /<dt>Ads<\/dt><dd>4<\/dd>/);
  const old = win.EWUI.directoryCard({ key: 'ag1', display: 'AG1', records: 9 }, { kind: 'brand' });
  assert.doesNotMatch(old, /Reads|>9</);
});

/* -- Brand page -- */
test('d1b Brand: "N ads", "Recent ads" one row per ad, top shows in ads', async () => {
  const { html } = await render('#/brand/chatgpt', F.brandApi());
  assert.match(text(html), /In this collection: 7 ads in 6 episodes across 3 shows/);
  assert.match(html, /<h2 id="reads-h">Recent ads<\/h2>/);
  assert.match(text(html), /See all 7 ads in Search/);
  assert.equal(count(html, '<li class="lib-read">'), 2, 'two reads of one ad are one row');
  assert.match(html, /<span class="lib-row-sub">2 ads · last heard Oct 9<\/span>/);
  assert.doesNotMatch(text(html), /\b13\b|Recent reads|\breads?\b/i);
});

test('d1b Brand from an older server: no number of ads anywhere', async () => {
  const detail = F.brandDetail({ ad_breaks: undefined, top_shows: [{ show_id: 'cs-a', display_title: 'Show A', reads: 3, confirmed: 0, last_heard: '2026-10-09' }] });
  delete detail.brand.ad_breaks;
  const { html } = await render('#/brand/chatgpt', F.brandApi(detail, F.BRAND_ITEMS, { total: 13 }));
  const page = html.replace(/<section class="ew-card lib-offer"[\s\S]*?<\/section>/, ''); // offer labels name dates ("Oct 1 ad")
  assert.doesNotMatch(text(page), /\b\d+ ads?\b/);
  assert.doesNotMatch(text(page), /\b13\b|\b3 reads?\b/);
  assert.match(text(page), /In this collection: in 6 episodes across 3 shows/);
  assert.match(text(page), /See all in Search/);
});

/* -- Show page -- */
test('d1b Show: sponsor rows and episode tags count ads', async () => {
  const { html } = await render('#/show/cs-1', F.showApi());
  const table = (html.match(/<section class="ew-card lib-sponsors"[\s\S]*?<\/section>/) || [''])[0];
  assert.match(table, /<td class="ew-num">4 ads<\/td>/);
  assert.match(table, /<td class="ew-num">1 ad<\/td>/);
  assert.doesNotMatch(text(table), /\breads?\b|\b6\b/i);
  assert.match(text(html), /Screened · 5 ads listed/);
  assert.match(text(html), /Screened · 1 ad listed/);
  assert.doesNotMatch(text(html), /confirmed|possible/);
  const old = await render('#/show/cs-1', F.showApi({ ...F.SHOW,
    sponsors: F.SHOW.sponsors.map(({ ad_breaks, ...r }) => r), episodes: F.SHOW.episodes.map(({ ad_breaks, ...e }) => e) }));
  assert.match(text(old.html), /Screened · ads listed/);
  assert.doesNotMatch(text(old.html), /\b\d+ (possible )?(ads?|reads?)\b/i);
});

/* -- Compare -- */
test('d1b Compare counts ads from the ads field (PLAIN-ADS: every ad)', async () => {
  const { html } = await render('#/compare?brands=chatgpt,progressive', F.compareApi());
  assert.match(html, /<strong class="ew-num">3 ads<\/strong>/);
  assert.match(html, /<strong class="ew-num">2 ads<\/strong>/);
  assert.match(html, /<h2 id="compare-h">Ads<\/h2><span class="lib-count">Shared scale 0–3<\/span>/);
  assert.doesNotMatch(text(html), /Heard in 7 ads|confirmed/);
  assert.match(html, /aria-label="ChatGPT, all dates in this collection: 3 ads\. Open exact records\."/);
  assert.doesNotMatch(text(html), /\b4 confirmed|occurrences?|\breads?\b/i);
  const old = await render('#/compare?brands=chatgpt,progressive', F.compareApi(F.compareData(false), F.OLD_DIRBRANDS));
  assert.doesNotMatch(text(old.html), /\b\d+ (ads? )?confirmed|Heard in/);
});

/* -- Overview -- */
test('d1b Overview: tracked brands in ads; "Recent ads" one row per ad; unread from unread_ads', async () => {
  const { html } = await render('#/overview', F.overviewApi());
  assert.match(html, /<span class="meta ew-ov-right">7 ads<\/span>/);
  assert.match(html, /<h2>Recent ads<\/h2>/);
  const recent = (html.match(/<section class="ew-card ew-ov-card ew-ov-recent">[\s\S]*?<\/section>/) || [''])[0];
  const refs = [...recent.matchAll(/class="ew-ov-name ew-ov-lead" href="#\/evidence\/([^"]+)"/g)].map(m => m[1]);
  assert.equal(refs.length, 3);
  assert.ok(!refs.includes(encodeURIComponent(F.REF(2))), 'the second read of ad brk:a is not its own row');
  assert.match(text(html), /3 changes · 2 unread\./);
  const old = await render('#/overview', F.overviewApi({ brands: F.OLD_DIRBRANDS }));
  assert.doesNotMatch(old.html, /ew-ov-right/);
});

test('d1b Overview from an older server (no unread_ads): the event counts stay', async () => {
  const totals = F.OV_TOTALS.map(({ ads, unread_ads, ...t }) => t);
  const { html } = await render('#/overview', F.overviewApi({ ov: F.overview({ alert_totals: totals }) }));
  assert.match(text(html), /5 changes · 4 unread\./);
});

/* -- Alert page -- */
test('d1b Alert: rows group by ad_id; New while any evidence is unread; "New evidence for this ad"', async () => {
  const events = [F.ev(1, 'epA', [10, 40], 'brk:a', { read_state: 1 }), F.ev(2, 'epA', [30, 60], 'brk:a'),
    F.ev(3, 'epA', [300, 340], 'brk:b', { read_state: 1 }), F.ev(4, 'epB', [10, 40], 'brk:c')];
  const { html } = await render('#/alert/1', F.alertApi(F.ALERT, events));
  const rows = html.split('<li class="inbox-event').slice(1).map(r => r.slice(0, r.indexOf('</li>')));
  assert.equal(rows.length, 3, 'one row per ad');
  const a = rows.find(r => r.includes('data-event-id="1"'));
  assert.match(a, /data-tag="info">New</);
  assert.match(text(a), /New evidence for this ad/);
  assert.match(a, /href="#\/evidence\/sr1%3A2/);
  const b = rows.find(r => r.includes('data-event-id="3"'));
  assert.match(b, /data-tag="neutral">Seen</);
  assert.doesNotMatch(text(b), /New evidence/);
  assert.match(html, /Mark all read/);
});

test('d1b Alert: a fully unread ad with two pieces of evidence is one New row, no "New evidence" line', async () => {
  const { html } = await render('#/alert/1', F.alertApi(F.ALERT, [F.ev(1, 'epA', [10, 40], 'brk:a'), F.ev(2, 'epA', [30, 60], 'brk:a')]));
  const rows = html.split('<li class="inbox-event').slice(1);
  assert.equal(rows.length, 1);
  assert.match(rows[0], /data-tag="info">New</);
  assert.doesNotMatch(text(rows[0]), /New evidence/);
  assert.match(text(rows[0]), /Evidence \(2\)/);
});

test('d1b Alert: events with no ad_id (older server) stay one row each', async () => {
  const { html } = await render('#/alert/1', F.alertApi(F.ALERT, [F.ev(1, 'epA', [10, 40], undefined), F.ev(2, 'epA', [30, 60], undefined)]));
  assert.equal(html.split('<li class="inbox-event').length - 1, 2);
});

test('d1b Alert matching line counts breaks as ads; no number without breaks', async () => {
  const aside = html => text(html.slice(html.indexOf('<h3>Current matches</h3>'), html.indexOf('<h3>Check local records</h3>')));
  const big = html => [...html.slice(html.indexOf('<h3>Current matches</h3>'), html.indexOf('<h3>Check local records</h3>')).matchAll(/<p class="ew-work-big">([^<]*)<\/p>/g)].map(m => m[1]);
  let r = await render('#/alert/1', F.alertApi(F.ALERT));
  assert.deepEqual(big(r.html), ['7']);
  assert.match(aside(r.html), /In stored records: 7 ads\./);
  r = await render('#/alert/1', F.alertApi({ ...F.ALERT, current_matches: { count: 13, breaks: 7 } }));
  assert.match(aside(r.html), /7 ads match this alert’s search today\./);
  r = await render('#/alert/1', F.alertApi({ ...F.ALERT, query: { version: 2, mode: 'mentions', q: 'chatgpt' },
    current_matches: { count: 16, tracked: 34, breaks: 7, tracked_by_kind: { new_text: 16, new_finding: 18 } } }));
  assert.deepEqual(big(r.html), ['16'], 'Search\'s passage count (DEMO-SOL-R1-10)');
  assert.match(aside(r.html), /In stored records: 16 matching passages, as Search counts them\. It also watches 7 ads in the same scope\./);
  r = await render('#/alert/1', F.alertApi({ ...F.ALERT, current_matches: { count: 13, tracked: 13, tracked_by_kind: { new_finding: 13 } } }));
  assert.deepEqual(big(r.html), []);
  assert.doesNotMatch(aside(r.html), /\b13\b|\breads?\b/);
});

test('d1b row 11: a brand alert is named "<brand> ads"', () => {
  const win = F.loadViews();
  assert.equal(win.EWViews.evidenceAlertPreset({ brand: 'chatgpt', brandName: 'ChatGPT' }).name, 'ChatGPT ads');
});

/* -- Campaigns -- */
test('d1b Campaign: "Expects N ads", "N ads waiting for review", "Ads found" filter', async () => {
  const { html } = await render('#/campaign/6', F.campaignApi(F.UNREVIEWED()));
  const t = text(html);
  assert.match(t, /Expects 2 ads/);
  assert.match(html, /<span class="tag amber">2 ads waiting for review<\/span>/);
  assert.match(t, /Ads found · 0/);
  assert.match(t, /0 of 1 counted placement has a result yet — 1 with ads waiting for review\./);
  assert.match(t, /Requirement outcome: Ads in scope are waiting for review, so a match can't be ruled out\./);
  // Customer copy only: the server's raw tokens stay in Technical details,
  // and the decision-13 sentence keeps its verb ("not read automatically").
  const copy = text(html.replace(/<details class="tech-details">[\s\S]*?<\/details>/g, '')).replace(NO_CHECKS, '');
  assert.doesNotMatch(copy, /detections?|\breads?\b/i);
});

test('d1b Campaign: "Ad found" and "No matching ad in the available transcript"', async () => {
  let r = await render('#/campaign/6', F.campaignApi(F.draft(F.COMPLETE(0), 'supported')));
  assert.match(r.html, /<span class="tag green">Ad found<\/span>/);
  assert.match(text(r.html), /1 of 1 counted placement has a result: 1 with an ad found\./);
  r = await render('#/campaign/6', F.campaignApi(F.draft(F.COMPLETE(0), 'no_observation_in_covered_sources')));
  assert.match(text(r.html), /No matching ad in the available transcript/);
  assert.match(text(r.html), /1 with no matching ad in the available transcript\./);
  const one = F.draft(F.COMPLETE(1), 'unreviewed_detections_in_scope');
  one.placements[0].expected_count = 1;
  r = await render('#/campaign/6', F.campaignApi(one));
  assert.match(r.html, /<span class="tag amber">1 ad waiting for review<\/span>/);
  assert.match(text(r.html), /Expects 1 ad\b/);
});

test('d1b F3 placement page: the decision-13 sentence replaces "No stated requirements", once', async () => {
  const { html } = await render('#/campaign/6/placement/' + encodeURIComponent(F.PID), F.campaignApi(F.draft(F.COMPLETE(0), 'ambiguous')));
  const t = text(html);
  assert.doesNotMatch(t, /No stated requirements/);
  assert.equal(count(t, NO_CHECKS), 1, 'said once on the page');
});

test('d1b F3 comparisonRows with no outcome on screen says the decision-13 sentence', () => {
  const win = F.loadViews();
  const d = F.draft(F.COMPLETE(0), 'ambiguous');
  assert.ok(text(win.EWViews.comparisonRows(d, F.PID)).includes(NO_CHECKS));
  d.assessment_scope.by_unit[F.PID].entered_fields = ['message'];
  assert.doesNotMatch(text(win.EWViews.comparisonRows(d, F.PID)), /No checks entered|No stated requirements/);
});

/* -- Settings / Saved -- */
test('d1b Settings: "Ads" (PLAIN-ADS)', async () => {
  const { html } = await render('#/settings', F.settingsApi);
  assert.match(html, /<dt>Ads<\/dt><dd class="small">Ads: what we found in the episodes we hold\. An ad found is not proof it ran as bought\.<\/dd>/);
  assert.doesNotMatch(html, /detection/i);
});

test('d1b Saved: "you saved an ad from an earlier transcript"', async () => {
  const { html } = await render('#/saved?tab=bookmarks', F.savedApi);
  assert.match(html, /Earlier copy: you saved an ad from an earlier transcript of the episode\. Nothing changed after you saved\./);
});
