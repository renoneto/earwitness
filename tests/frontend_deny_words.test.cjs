/* UX DENY-WORDS (ws-ux/DENY-WORDS.md 5e799bded; lead copied into
   DENY-PAIR-FIX): one honest line per silent gap, each only when its
   count > 0, singular and plural.
   - ADC-C-06/07, Saved > Reports: reports the plan withholds, and reports
     a grant whose shows list leaves a show out, as one muted line each
     under the list or its empty state (plan first). Rights-withheld
     reports keep their number-only row and are not counted.
   - FDV-R1-02, Saved: saved items a source's deny withholds; with nothing
     left the heading is "No saved excerpts available".
   - FDV-R1-03, Saved under Plan A with a deny: the plan line unchanged,
     then "N more can’t be shown here…", each item counted once.
   Run: node --test tests/frontend_deny_words.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page } = require('./frontend_alert_deny_page.cjs');

const text = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
const REF = 'sr1:' + 'c'.repeat(32);
const ITEM = { id: 'si-1', type: 'excerpt', title: 'Kept read', note: '', revision: 1, saved_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z', target: { ref: REF }, availability: 'current',
  captured: { source_ref: REF, text: 'kept words', source_kind: 'evidence' } };
const savedOut = (items, over = {}) => ({ items, total: items.length, total_relation: 'exact',
  counts: { excerpt: items.length, query: 0, bookmark: 0 }, collection_revision: 'r', next_cursor: null, ...over });
const REPORT = { id: 7, kind: 'report', title: 'Acme flight', campaign_id: 1, report_no: 1, captured_at: '2026-10-14T09:00:00Z',
  pins: {}, count: null, count_units: '', format: 'html', digest: 'd'.repeat(64),
  download_path: '/customer/campaigns/1/reports/1/html', exportable: true, stale: false, stale_reasons: [], limitations: [] };
const archiveOut = (items, over = {}) => ({ items, total: items.length, total_relation: 'exact', next_cursor: null,
  archive_revision: 'a', ...over });
const api = (saved, archive) => p => {
  if (p.startsWith('/customer/saved-items')) return saved;
  if (p.startsWith('/customer/archive')) return archive;
  if (p.startsWith('/customer/mistakes')) return { notes: [], mistakes: [], total: 0 };
  return { items: [], total: 0, total_relation: 'exact', counts: {} };
};
const saved = async out => (await page('#/saved', api(out, archiveOut([])))).main.innerHTML;
const reports = async out => (await page('#/saved?tab=reports', api(savedOut([]), out))).main.innerHTML;

const RIGHTS1 = '1 saved item can’t be shown here, because the rights for its shows don’t allow it.';
const RIGHTS = n => `${n} saved items can’t be shown here, because the rights for their shows don’t allow it.`;

test('FDV-R1-02: one saved item withheld by a deny, singular, with the list kept', async () => {
  const html = await saved(savedOut([ITEM], { rights_withheld: 1 }));
  assert.equal(text(html).split(RIGHTS1).length - 1, 1, html);
  assert.match(html, /Kept read/);
  assert.doesNotMatch(html, /not available on your plan/);
});

test('FDV-R1-02: plural; nothing left: "No saved excerpts available", never "No saved excerpts."', async () => {
  const html = await saved(savedOut([], { rights_withheld: 3 }));
  assert.ok(text(html).includes(RIGHTS(3)), html);
  assert.match(html, /No saved excerpts available/);
  assert.doesNotMatch(text(html), /No saved excerpts(?! available)/);
});

test('FDV-R1-03: plan and rights counts: the plan line unchanged, then the "more" sentence', async () => {
  const cases = [
    [1, 1, '1 saved item is not available on your plan. 1 more can’t be shown here, because the rights for its shows don’t allow it.'],
    [2, 3, '2 saved items are not available on your plan. 3 more can’t be shown here, because the rights for their shows don’t allow it.'],
    [1, 2, '1 saved item is not available on your plan. 2 more can’t be shown here, because the rights for their shows don’t allow it.'],
    [3, 1, '3 saved items are not available on your plan. 1 more can’t be shown here, because the rights for its shows don’t allow it.'],
  ];
  for (const [p, r, line] of cases) {
    const html = await saved(savedOut([], { plan_withheld: p, rights_withheld: r }));
    assert.ok(text(html).includes(line), `${p}/${r}: ${text(html)}`);
    assert.equal((html.match(/not available on your plan/g) || []).length, 1);
  }
});

test('FDV-R1-03: only one count > 0 shows only that sentence; none shows no line', async () => {
  let html = await saved(savedOut([], { plan_withheld: 2, rights_withheld: 0 }));
  assert.ok(text(html).includes('2 saved items are not available on your plan.'));
  assert.doesNotMatch(html, /can’t be shown here/);
  for (const over of [{ plan_withheld: 0, rights_withheld: 0 }, {}]) {
    html = await saved(savedOut([ITEM], over));
    assert.doesNotMatch(html, /not available on your plan|can’t be shown here/);
  }
});

const PLAN1 = '1 report is not available on your plan.';
const PLAN = n => `${n} reports are not available on your plan.`;
const ACCESS1 = '1 report isn’t shown, because your access doesn’t include all of its shows.';
const ACCESS = n => `${n} reports aren’t shown, because your access doesn’t include all of their shows.`;
const lines = html => [...html.matchAll(/<p class="saved-foot saved-withheld">([^<]*)<\/p>/g)].map(m => m[1]);

test('ADC-C-06/07: the plan line under the list, singular and plural', async () => {
  assert.deepEqual(lines(await reports(archiveOut([REPORT], { plan_withheld: 1 }))), [PLAN1]);
  assert.deepEqual(lines(await reports(archiveOut([REPORT], { plan_withheld: 4 }))), [PLAN(4)]);
});

test('ADC-C-06/07: the access line, singular and plural, under the empty state too', async () => {
  let html = await reports(archiveOut([], { access_withheld: 1 }));
  assert.deepEqual(lines(html), [ACCESS1]);
  assert.match(html, /No retained reports or exports/);
  assert.deepEqual(lines(await reports(archiveOut([], { access_withheld: 2 }))), [ACCESS(2)]);
});

test('ADC-C-06/07: both lines, plan first; none at 0; number-only rows stay and add nothing', async () => {
  assert.deepEqual(lines(await reports(archiveOut([REPORT], { plan_withheld: 2, access_withheld: 1 }))),
    [PLAN(2), ACCESS1]);
  const withheldRow = { kind: 'report', report_no: 2, rights_withheld: true };
  const html = await reports(archiveOut([REPORT, withheldRow], { plan_withheld: 0, access_withheld: 0 }));
  assert.deepEqual(lines(html), []);
  assert.match(text(html), /Report 2 can’t be shown here, because the rights for its shows don’t allow it\./);
});
