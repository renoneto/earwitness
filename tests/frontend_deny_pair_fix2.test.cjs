/* DENY-PAIR-FIX2 page items (App's screen confirm, UX words 10-30).
   - DPV-01: Saved > Reports' number-only row ("Report N can’t be shown
     here…") starts with the empty mark column, so its text sits in the
     text columns, never in the 40 px icon column.
   - DPV-02: the footer "Frozen files never change. Each download is the
     same file as the first." shows only when a row below can be downloaded.
   - DPV-05: Saved's empty excerpt state (a deny, and Plan A): heading, then
     the withheld line, then "To save an excerpt, open an ad and choose Save
     excerpt.", which replaces "Save an excerpt from any evidence reader."
   - DPC-06 (UX DENY-WORDS (4), 34c544385): withheld exports join the plan,
     access and rights lines, each kind named only when its count > 0.
   Run: node --test tests/frontend_deny_pair_fix2.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page } = require('./frontend_alert_deny_page.cjs');

const text = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
const savedOut = (items, over = {}) => ({ items, total: items.length, total_relation: 'exact',
  counts: { excerpt: items.length, query: 0, bookmark: 0 }, collection_revision: 'r', next_cursor: null, ...over });
const REPORT = { id: 7, kind: 'report', title: 'Acme flight', campaign_id: 1, report_no: 1, captured_at: '2026-10-14T09:00:00Z',
  pins: {}, count: null, count_units: '', format: 'html', digest: 'd'.repeat(64),
  download_path: '/customer/campaigns/1/reports/1/html', exportable: true, stale: false, stale_reasons: [], limitations: [] };
const EXPORT = { ...REPORT, id: 9, kind: 'export', title: 'Lumen rows', campaign_id: null, report_no: null, count: 3,
  count_units: 'rows', format: 'csv', download_path: '/customer/exports/9' };
const NUMBER_ONLY = { kind: 'report', report_no: 2, rights_withheld: true };
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

const FOOTER = 'Frozen files never change. Each download is the same file as the first.';
const HINT = 'To save an excerpt, open an ad and choose Save excerpt.';
const OLD_HINT = 'Save an excerpt from any evidence reader.';
const RIGHTS1 = '1 saved item can’t be shown here, because the rights for its shows don’t allow it.';
const RIGHTS = n => `${n} saved items can’t be shown here, because the rights for their shows don’t allow it.`;
const ACCESS = /aren’t shown|isn’t shown|your access doesn’t include/;

test('DPV-01: the number-only row starts with the empty mark column, its text in the text column', async () => {
  const html = await reports(archiveOut([REPORT, NUMBER_ONLY]));
  const rows = html.match(/<li class="saved-row">[\s\S]*?<\/li>/g);
  const row = rows.find(r => r.includes('Report 2 can’t be shown here'));
  assert.ok(row, html);
  assert.match(row, /^<li class="saved-row"><span class="saved-mark" aria-hidden="true"><\/span><div class="saved-main">/);
  assert.equal(text(row), 'Report 2 can’t be shown here, because the rights for its shows don’t allow it.');
});

test('DPV-02: the download footer shows only when a row below can be downloaded', async () => {
  const cases = [
    ['a report with a download', [REPORT], true],
    ['an export with a download', [EXPORT], true],
    ['only a number-only row', [NUMBER_ONLY], false],
    ['only a row without export permission', [{ ...REPORT, exportable: false }], false],
    ['only a row without a stored download', [{ ...REPORT, download_path: null }], false],
    ['a number-only row and a downloadable one', [REPORT, NUMBER_ONLY], true],
  ];
  for (const [name, items, shown] of cases) {
    const t = text(await reports(archiveOut(items)));
    assert.equal(t.includes(FOOTER), shown, name);
    assert.ok(!t.includes('same bytes as the first'), name);
  }
});

test('DPV-05: the empty excerpt state reads heading, withheld line, then the new hint (deny and Plan A)', async () => {
  for (const [name, over, line] of [
    ['deny', { rights_withheld: 1 }, RIGHTS1],
    ['Plan A', { plan_withheld: 2, excerpt_only: true }, '2 saved items are not available on your plan.'],
  ]) {
    const t = text(await saved(savedOut([], over)));
    const at = [t.indexOf('No saved excerpts available'), t.indexOf(line), t.indexOf(HINT)];
    assert.ok(at.every(i => i >= 0) && at[0] < at[1] && at[1] < at[2], `${name}: ${t}`);
    assert.ok(!t.includes(OLD_HINT), name);
  }
  // Nothing saved and nothing withheld: the new hint too.
  const t = text(await saved(savedOut([])));
  assert.ok(t.includes(HINT) && !t.includes(OLD_HINT), t);
});

test('DPC-06 (UX DENY-WORDS (4)): reports and exports, one line per reason, each kind only when > 0', async () => {
  const lines = async over => [...(await reports(archiveOut([], over))).matchAll(/<p class="saved-foot saved-withheld">([^<]*)<\/p>/g)].map(m => m[1]);
  const X = (plan = 0, access = 0, rights = 0) => ({ exports_withheld: { plan, access, rights } });
  const cases = [
    // Plan: exports alone, with reports, verb by the total.
    [{ ...X(1) }, ['1 export is not available on your plan.']],
    [{ ...X(3) }, ['3 exports are not available on your plan.']],
    [{ plan_withheld: 1, ...X(2) }, ['1 report and 2 exports are not available on your plan.']],
    [{ plan_withheld: 2, ...X(1) }, ['2 reports and 1 export are not available on your plan.']],
    [{ plan_withheld: 1, ...X(1) }, ['1 report and 1 export are not available on your plan.']],
    [{ plan_withheld: 1 }, ['1 report is not available on your plan.']],
    [{ plan_withheld: 2 }, ['2 reports are not available on your plan.']],
    // Access.
    [{ ...X(0, 1) }, ['1 export isn’t shown, because your access doesn’t include all of its shows.']],
    [{ ...X(0, 2) }, ['2 exports aren’t shown, because your access doesn’t include all of their shows.']],
    [{ access_withheld: 1, ...X(0, 2) }, ['1 report and 2 exports aren’t shown, because your access doesn’t include all of their shows.']],
    [{ access_withheld: 1 }, ['1 report isn’t shown, because your access doesn’t include all of its shows.']],
    [{ access_withheld: 3 }, ['3 reports aren’t shown, because your access doesn’t include all of their shows.']],
    // Rights: exports only (reports keep their number-only rows).
    [{ ...X(0, 0, 1) }, ['1 export can’t be shown here, because the rights for its shows don’t allow it.']],
    [{ ...X(0, 0, 4) }, ['4 exports can’t be shown here, because the rights for their shows don’t allow it.']],
    // Order: plan, access, rights.
    [{ plan_withheld: 1, access_withheld: 1, ...X(1, 0, 2) }, [
      '1 report and 1 export are not available on your plan.',
      '1 report isn’t shown, because your access doesn’t include all of its shows.',
      '2 exports can’t be shown here, because the rights for their shows don’t allow it.']],
    [{ ...X(0, 0, 0) }, []],
    [{ plan_withheld: 0, access_withheld: 0 }, []],
  ];
  for (const [over, want] of cases) assert.deepEqual(await lines(over), want, JSON.stringify(over));
  for (const n of [0, -1, 1.5, 'x', null]) {
    assert.deepEqual(await lines(X(n, n, n)), [], String(n));
  }
  // Under a list too, after the rows.
  const html = text(await reports(archiveOut([REPORT], X(0, 0, 1))));
  assert.ok(html.indexOf('Acme flight') < html.indexOf('1 export can’t be shown here'), html);
});

test('Saved never uses the archive’s access words; its deny line is UX’s DENY-WORDS line', async () => {
  for (const n of [1, 2]) {
    const t = text(await saved(savedOut([], { rights_withheld: n, plan_withheld: 0 })));
    assert.ok(t.includes(n === 1 ? RIGHTS1 : RIGHTS(n)), t);
    assert.doesNotMatch(t, ACCESS);
  }
  assert.match(text(await reports(archiveOut([], { access_withheld: 1 }))), ACCESS);
});
