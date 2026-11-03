/* ALERT-DENY-4 (UX c18508cac, AC-AS-A11-WD Addendum 3; Reno-approved
   10-29): a report listed or opened while the campaign's draft is
   withheld by the rights for its shows (server stale reason "rights
   withheld") says so, byte for byte, instead of "the campaign is no
   longer available"; a campaign whose draft really can't be built keeps
   the old line. Run: node --test tests/frontend_report_stale_rights.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page } = require('./frontend_alert_deny_page.cjs');

const RIGHTS = 'Stale: we can’t compare it with the campaign now, because the rights for its shows don’t allow it.';
const OLD = 'Stale: the campaign is no longer available';
const CAMP = { id: 1, name: 'Acme flight', mode: 'research', revision: 3, availability: 'active', targets: [],
  allowed_actions: ['read', 'reports'] };
const REPORT = no => ({ report_no: no, campaign_revision: 2, captured_at: '2026-10-14T09:00:00Z', digest: 'd'.repeat(64),
  available: true, exportable: true, stale: true, stale_reasons: ['rights withheld'] });
const campaignApi = reports => (p) => {
  if (p === '/customer/campaigns/1') return { campaign: CAMP };
  if (p === '/customer/campaigns/1/draft') return { draft: { campaign: CAMP, placements: [], targets: [], pins: {} } };
  if (p === '/customer/campaigns/1/reports') return { reports };
  throw new Error('unexpected request ' + p);
};
const text = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');

test('ALERT-DENY-4: the history row says the rights reason, byte for byte', async () => {
  const html = (await page('#/campaign/1', campaignApi([REPORT(1), { ...REPORT(2), stale_reasons: ['campaign unavailable'] }]))).main.innerHTML;
  assert.ok(text(html).includes(RIGHTS), text(html));
  assert.equal(text(html).split(RIGHTS).length - 1, 1);
  // The old line stays for a campaign whose draft really can't be built.
  assert.ok(text(html).includes(OLD));
});

test('ALERT-DENY-4: the report page leads its stale banner with the same line', async () => {
  const out = { report: {}, frozen: { campaign: { name: 'Acme flight' }, captured_at: '2026-10-14T09:00:00Z' },
    stale: true, stale_reasons: ['rights withheld'], payload_immutable: true };
  const html = (await page('#/campaign/1/report/1', p => {
    if (p === '/customer/campaigns/1/reports/1') return out;
    throw new Error('unexpected request ' + p);
  })).main.innerHTML;
  const banner = text(html.slice(html.indexOf('stale-banner'), html.indexOf('</p>', html.indexOf('stale-banner'))));
  assert.ok(banner.includes(RIGHTS + ' The frozen copy below is unchanged.'), banner);
  assert.doesNotMatch(banner, /no longer available|This report is stale/);
});
