/* QW7 copy pass: no customer surface says "possible ad", and an evidence
   page has one caveat block. The internal key `possible` stays.
   Run: node --test tests/frontend_qw7_copy.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const F = require('./frontend_uat_d1b_fixtures.cjs');
const { render, text } = F;

const POSSIBLE_AD = /possible ads?\b/i;
const count = (hay, needle) => hay.split(needle).length - 1;

test('QW7 no customer surface says possible ad', async () => {
  const camp = await render('#/campaign/6', F.campaignApi(F.draft(F.COMPLETE(0), 'supported')));
  const campText = text(camp.html);
  assert.doesNotMatch(camp.html, POSSIBLE_AD);
  assert.doesNotMatch(campText, POSSIBLE_AD);
  assert.match(camp.html, /<span class="tag green">Ad found<\/span>/);
  assert.match(camp.html, /data-filter="possible" aria-pressed="false">Ads found · 1</, 'the internal filter key stays');
  assert.match(campText, /1 of 1 counted placement has a result: 1 with an ad found\./);

  const pending = await render('#/campaign/6', F.campaignApi(F.UNREVIEWED()));
  assert.doesNotMatch(text(pending.html), POSSIBLE_AD);
  assert.match(text(pending.html), /Ads found · 0/);

  const ov = await render('#/overview', F.overviewApi({ ov: F.overview({ campaign_gaps: F.OV_GAPS, tracked_campaigns: F.OV_TRACKED }) }));
  assert.doesNotMatch(ov.html, POSSIBLE_AD);
  // WIN3: an ad found needs no attention (the campaign page's filter), so
  // Overview lists only the other placements.
  assert.doesNotMatch(text(ov.html), /Ad found/);
  assert.match(text(ov.html), /No matching ad in the available transcript/);
});

test('QW7 source: no possible ad wording left in views.js or ui.js code', () => {
  const strip = s => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '').replace(/([^:'"`\\])\/\/[^\n]*$/gm, '$1');
  // POLISH-2 (lead LEDGER-LEAD3-POL2-1): the one line on old frozen reports quotes the old
  // wording on purpose, to say it changed. Only that exact sentence, exactly once, is exempt.
  const EARLIER = 'This report uses earlier wording: "Possible ad" is now "Ad found".';
  for (const file of ['views.js', 'ui.js']) {
    const raw = strip(fs.readFileSync(path.join(F.APP, file), 'utf8'));
    assert.equal(raw.split(EARLIER).length - 1, file === 'views.js' ? 1 : 0, `${file}: earlier-wording line count`);
    const code = raw.split(EARLIER).join('');
    assert.doesNotMatch(code, POSSIBLE_AD, file);
  }
  const views = fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8');
  assert.ok(views.includes('3. See which placements have an ad found. An ad found does not prove it ran as bought.'), 'create dialog step 3');
  assert.ok(!views.includes('Check the linkage'), 'old step 3 is gone');
  for (const s of ["A match here isn't tied to one distinct ad yet.", 'A match here may cover more than one ad.',
    "We haven't checked the brand on a match here yet.", 'A match here has a date too unclear to place in this window.',
    "A match here has a suggested change we haven't checked yet."]) {
    assert.ok(views.includes(s), s);
  }
});

/* -- Evidence pages -------------------------------------------------------- */
const REF = 'sr1:' + 'q'.repeat(32);
const CUR = 36;
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [{ brand: 'quince', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 100, end_s: 150 },
  episode: { episode_id: 'ep_q', show_title: 'Show A', ep_title: 'An episode', pub_date: '2026-09-20', fetch_date: '2026-09-21',
    duration_min: 55.4, transcript_version_id: CUR, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Show A', unresolved: 0 },
  evidence: { id: 'a2', episode_id: 'ep_q', orig_start_s: 100, orig_end_s: 150, transcript_version_id: CUR }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', brand: { key: 'quince', display: 'Quince', confirmed: false }, needs_review: false, self_promo: false, confidence: 'medium',
    display_span: [100, 150], display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Quince' } },
  window: { segments: [{ segment_id: 9, start_s: 100, end_s: 150, text: 'Go to Quince.', text_start: 0, text_end: 13 }], text_complete: true, next_cursor: null },
  ad_break: null, ...over });
const evidencePage = rec => render('#/evidence/' + encodeURIComponent(REF), p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/brands') return { brands: [] };
  return {};
});
const showsSection = html => {
  const at = html.indexOf('<section class="ew-shows">');
  assert.ok(at >= 0, 'a shows section');
  return html.slice(at, html.indexOf('</section>', at));
};

test('QW7 evidence page has exactly one caveat block', async () => {
  for (const [name, rec] of [['ad', record()], ['mention', record({ record_kind: 'transcript_passage' })]]) {
    const { html } = await evidencePage(rec);
    assert.equal(count(html, 'class="ew-shows"'), 1, name + ': one shows section');
    assert.equal(count(showsSection(html), '<h2'), 1, name + ': one heading in it');
    assert.match(showsSection(html), /<h2>What this shows<\/h2>/, name);
    assert.doesNotMatch(html, /What it does not show/, name);
    assert.doesNotMatch(html, /ew-player-note">A time shows|A time shows where it was heard/, name + ': player note removed');
    assert.doesNotMatch(html, /met the brief|every listener/i, name);
  }
});

test('QW7 evidence page copy: ad, mention, details card', async () => {
  const ad = await evidencePage(record());
  assert.match(text(showsSection(ad.html)), /These words were heard at this time in this episode\./);
  assert.match(text(showsSection(ad.html)), /It does not show how many people heard the ad, or that it ran as bought\./);
  const about = ad.html.slice(ad.html.indexOf('ew-about'));
  assert.match(text(about), /Details/);
  assert.doesNotMatch(text(about.slice(0, about.indexOf('<details'))), /About this record|Relationship|Current version/);
  assert.match(text(about.slice(0, about.indexOf('<details'))), /We fetched the audio/);
  const tech = [...about.matchAll(/<details class="tech-details">[\s\S]*?<\/details>/g)].map(m => m[0]).join('\n');
  assert.match(tech, /<p class="small muted">Version: Current version<\/p>/);
  assert.match(tech, /<p class="small muted">Other listeners may hear different ads in this episode\.<\/p>/);

  const noExcerpt = await evidencePage(record({ text: null, excerpt_bounds: null, window: { segments: [], text_complete: true, next_cursor: null } }));
  assert.doesNotMatch(noExcerpt.html, /This record marks an ad|These words were spoken/);

  const mention = await evidencePage(record({ record_kind: 'transcript_passage' }));
  const shows = text(showsSection(mention.html));
  assert.match(shows, /These words are said at this time in this episode\./);
  assert.match(shows, /This page doesn't say whether they are part of an ad\./);
});

/* -- Addendum (LEDGER-LEAD3-QW7-2): footer motto and "Save a final report" -- */
test('QW7 addendum: the app footer motto is plain', () => {
  const shell = fs.readFileSync(path.join(F.APP, 'index.html'), 'utf8');
  assert.ok(shell.includes("<span>We show what was heard. When we can't tell, we say so.</span>"), 'new motto');
  assert.ok(!shell.includes('Unknown stays unknown'), 'old motto is gone');
});

test('QW7 addendum: the save action says "Save a final report", the noun stays "frozen report"', () => {
  const views = fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8');
  for (const s of [
    "'Save a final report…'", '>Save a final report…</button>',
    'You can save a final report once a placement has a result. None has a result yet.',
    "' You can still save a final report of the whole campaign.'",
    'A report saved now holds your targets and coverage only. There are no observations yet.',
    "confirmDialog('Save a final report', `Save a final report of ${scope} now? It is a saved copy that won't change. If the campaign changes later, the report is marked Stale.`",
    "submit.textContent = 'Saving…'", "E.saveNotice('Saving…', false)",
    '<strong>This report was not saved.</strong>', 'The campaign changed after you started saving it',
    'Review the current draft and save again, or discard this confirmation.',
    'Close this and save the report again from the campaign.', 'Reload the campaign to save its current draft.',
  ]) assert.ok(views.includes(s), s);
  for (const s of ['Freeze a report', 'Freezing…', 'Freezing now', 'still freezes', "'Freeze report'", 'Freeze ${scope}',
    'This freeze is stale', 'after this freeze was prepared', 'freeze again', 'freeze the report again', 'to freeze its current draft',
    'You can freeze a report']) assert.ok(!views.includes(s), 'gone: ' + s);
  // The noun and the internal keys stay.
  assert.ok(views.includes("'freeze-report'") && views.includes('freeze-review'), 'internal action keys stay');
  assert.match(views, /A frozen report is a saved copy that won't change\./);
});

/* -- Addendum 2 (LEDGER-LEAD3-QW7-3): the last "freeze" action words -- */
test('QW7 addendum 2: the remaining action words say "save a final report"', () => {
  const views = fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8');
  const saved = fs.readFileSync(path.join(F.APP, 'saved.js'), 'utf8');
  for (const s of [
    "' A final report holds the full campaign.'",
    "Report ${out.report_no} saved. Open it from the campaign.",
    "This report can't be saved as a final report. The rights for these shows don't allow saving these ads in a report. Nothing was saved.",
    "This report can't be saved as a final report right now. Nothing was saved.",
    'The reasons recorded when the report was saved are in Technical details.',
  ]) assert.ok(views.includes(s), s);
  assert.ok(saved.includes("'Save a final campaign report or capture a research export. Re-downloads are byte-identical.'"), 'saved.js empty note');
  for (const s of ['Freezing captures', 'frozen. Open it', "can't be frozen", 'recorded at freeze']) assert.ok(!views.includes(s), 'gone: ' + s);
  assert.ok(!saved.includes('Freeze a campaign report'), 'gone: saved.js Freeze a campaign report');
  // Stored data and keys stay: the old stale reason key and internal action keys.
  assert.ok(views.includes("'freeze-report'") && views.includes('freeze-review'), 'internal action keys stay');
});
