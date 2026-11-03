/* ALERT-DENY-3 (UX c18508cac, AC-AS-A11-WD Addendum 3; Reno-approved
   10-29) and ADV-R1-01: when the rights for its shows withhold the
   campaign's draft (the draft read answers 403 with refusal
   "disclosure"), the campaign page still shows the campaign and its
   report history; in place of the draft, one line with no count, title
   or source, plus " Saved reports are below." only when there are any.
   Never the raw server text, never a Retry for that refusal.
   Run: node --test tests/frontend_campaign_draft_withheld.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page, refusal } = require('./frontend_alert_deny_page.cjs');

const LINE = 'The draft report can’t be shown here, because the rights for its shows don’t allow it.';
const SAVED = ' Saved reports are below.';
const CAMP = { id: 1, name: 'Acme flight', mode: 'supplied_requirements', brand: 'acme', brand_display: 'Acme',
  revision: 3, availability: 'active', allowed_actions: ['read', 'edit', 'reports'] };
const REPORT = { report_no: 1, campaign_revision: 2, captured_at: '2026-10-14T09:00:00Z', digest: 'd'.repeat(64),
  available: true, exportable: true, stale: true, stale_reasons: ['rights withheld'] };
const api = (reports, draftError) => (p) => {
  if (p === '/customer/campaigns/1') return { campaign: CAMP };
  if (p === '/customer/campaigns/1/draft') return draftError;
  if (p === '/customer/campaigns/1/reports') return reports instanceof Error ? reports : { reports };
  throw new Error('unexpected request ' + p);
};
const DENIED = () => refusal(403, 'source disclosure denied', { refusal: 'disclosure' });
const text = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');

test('ALERT-DENY-3: a withheld draft: the campaign, the line and the saved reports', async () => {
  const html = (await page('#/campaign/1', api([REPORT, { report_no: 2, rights_withheld: true }], DENIED()))).main.innerHTML;
  assert.ok(html.includes(LINE + SAVED), html);
  assert.equal(html.split(LINE).length - 1, 1);
  assert.match(html, /<h1>Acme flight<\/h1>/);
  assert.match(html, /href="#\/campaign\/1\/report\/1"/);
  assert.match(html, /Frozen reports/);
  assert.doesNotMatch(text(html), /Campaign is unavailable|source disclosure denied|Retry/);
  assert.doesNotMatch(html, /data-action="retry"/);
});

test('ALERT-DENY-3: no saved reports: the line alone', async () => {
  for (const reports of [[], new Error('reports read failed')]) {
    const html = (await page('#/campaign/1', api(reports, DENIED()))).main.innerHTML;
    assert.ok(html.includes(LINE), html);
    assert.ok(!html.includes(SAVED));
    assert.doesNotMatch(text(html), /source disclosure denied|Campaign is unavailable/);
  }
});

test('ALERT-DENY-3: any other draft failure keeps the old error', async () => {
  const html = (await page('#/campaign/1', api([REPORT], refusal(403, 'field disclosure denied')))).main.innerHTML;
  assert.ok(!html.includes(LINE));
  assert.match(text(html), /Campaign is unavailable: field disclosure denied/);
});

test('ADC-C-03: only number-only withheld rows below: the line alone', async () => {
  const rows = [{ report_no: 1, rights_withheld: true }, { report_no: 2, rights_withheld: true }];
  const html = (await page('#/campaign/1', api(rows, DENIED()))).main.innerHTML;
  assert.ok(html.includes(LINE), html);
  assert.ok(!html.includes(SAVED), html);
  // The withheld rows still show with their number.
  assert.match(text(html), /Report 1 can’t be shown here/);
  assert.match(text(html), /Report 2 can’t be shown here/);
});
