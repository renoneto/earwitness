/* ADS-R1-02 (lead decision 10-29, the approved rights pattern): a report
   the rights for its shows withhold keeps its row in the campaign's
   report history and in Saved > Reports, as its number only: "Report N
   can’t be shown here, because the rights for its shows don’t allow it."
   No title, date, author, count, id, link or source in the HTML or its
   attributes. Run: node --test tests/frontend_report_withheld_row.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page } = require('./frontend_alert_deny_page.cjs');

const LINE = n => `Report ${n} can’t be shown here, because the rights for its shows don’t allow it.`;
const CAMP = { id: 1, name: 'Acme flight', mode: 'research', revision: 3, availability: 'active', targets: [],
  allowed_actions: ['read', 'reports'] };
const READABLE = { report_no: 1, campaign_revision: 2, captured_at: '2026-10-14T09:00:00Z', digest: 'd'.repeat(64),
  available: true, exportable: true, stale: false, stale_reasons: [] };
const WITHHELD = { report_no: 2, rights_withheld: true };
const campaignApi = reports => (p) => {
  if (p === '/customer/campaigns/1') return { campaign: CAMP };
  if (p === '/customer/campaigns/1/draft') return { draft: { campaign: CAMP, placements: [], targets: [], pins: {} } };
  if (p === '/customer/campaigns/1/reports') return { reports };
  throw new Error('unexpected request ' + p);
};
const rowOf = (html, n) => {
  const at = html.indexOf(LINE(n));
  return html.slice(html.lastIndexOf('<li', at), html.indexOf('</li>', at) + 5);
};

test('ADS-R1-02: the campaign history shows the withheld report as its number and the line only', async () => {
  const html = (await page('#/campaign/1', campaignApi([READABLE, WITHHELD]))).main.innerHTML;
  assert.equal(html.split(LINE(2)).length - 1, 1);
  const row = rowOf(html, 2);
  assert.ok(row.startsWith('<li') && row.endsWith('</li>'), row);
  assert.doesNotMatch(row, /href|<time|Frozen|Stale|Current|data-|title=/);
  assert.equal(row.replace(/<[^>]*>/g, ''), LINE(2));
  // The readable report keeps its link.
  assert.match(html, /href="#\/campaign\/1\/report\/1"/);
  assert.doesNotMatch(html, /#\/campaign\/1\/report\/2/);
});

test('ADS-R1-02: Saved > Reports shows the withheld report as its number and the line only', async () => {
  const { win } = await page('#/campaign/1', campaignApi([READABLE]));
  const html = win.EWSaved.archiveRow(win.EW, { kind: 'report', ...WITHHELD });
  assert.equal(html.replace(/<[^>]*>/g, ''), LINE(2));
  assert.doesNotMatch(html, /href|<time|Frozen|Download|frozen|sha256|data-|title=/);
});
