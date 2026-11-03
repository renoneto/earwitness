/* Design v3 UX review (UX-07 + R2-07): supplied campaign, placement and
   frozen report pages. One verdict line that claims only "has a result",
   reasons grouped with counts, assessment scope folded into each
   placement, all-Unknown tables collapsed, honest Freeze states.
   Run: node --test tests/frontend_dv3_ux_campaign.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const canonicalSessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function renderRoute(route, api) {
  const win = loadAt('#/overview', { fetch: canonicalSessionFetch() });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ').replace(/&#39;/g, "'").replace(/\s+/g, ' ');

// Campaign 1 of the golden workspace (t4-c1-draft.json), trimmed to the
// fields these pages read.
const CITE = 'exp:799e89fcf319', WIN = 'exp:f3ba15d41424', OCC = 'named:13ae6be7';
const FIELDS = ['code', 'duration_s', 'message', 'offer'];
const unknownRows = () => ['message', 'offer', 'code', 'duration_s'].map(f => ({ field: f, expected: f === 'duration_s' ? [60, 90] : 'x', observed: null, state: 'unknown', reason: 'no eligible occurrence' }));
const c1Draft = () => ({
  campaign: { id: 1, revision: 4, name: 'BetterHelp Q4 host reads' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [
    { id: CITE, scope: 'episode', show_id: 's1', episode_id: 'ep5', expected_count: 1, binding: 'citation_only', provenance: { binding: 'citation_only' } },
    { id: WIN, scope: 'show_window', show_id: 's1', date_from: '2026-06-18', date_to: '2026-10-09', expected_count: 2, binding: 'eligible', provenance: { binding: 'eligible' } },
  ],
  targets: [], episode_titles: { ep5: 'The Last 12 Weeks - Ep. 5' },
  coverage_by_placement: {
    [CITE]: { state: 'incomplete', missing: ['ep5'], failed: [], episodes: ['ep5'], episode_labels: { ep5: 'The Last 12 Weeks - Ep. 5' } },
    [WIN]: { state: 'incomplete', missing: ['ep4', 'ep5'], failed: [], episodes: ['ep4', 'ep5'], episode_labels: { ep4: 'The Last 12 Weeks - Ep. 4', ep5: 'The Last 12 Weeks - Ep. 5' } },
  },
  comparisons: { [CITE]: unknownRows(), [WIN]: unknownRows() },
  allocation: {
    evaluations: { [WIN]: { placement_id: WIN, outcome: 'incomplete_coverage', eligible_ids: [], allocated_elsewhere: [], occurrence_order: [OCC],
      reasons: ["missing transcripts: ['Ep. 4', 'Ep. 5']", 'failed fetches: []', `${OCC}: unresolved correspondence`, `${OCC}: not currently reviewed_ad`,
        `${OCC}: advertiser attribution unconfirmed`, `${OCC}: duration_s: audio alignment unverified`, `${OCC}: message: unverified required field is unknown`,
        `${OCC}: offer: unverified required field is unknown`, `${OCC}: code: unverified required field is unknown`] } },
    citations: { [CITE]: { binding: 'citation_only', reasons: ['citation_only placements never allocate'], eligible_ids: [], allocated_ids: [] } },
  },
  reason_codes: {
    evaluations: { [WIN]: [{ code: 'missing_transcripts', field: null }, { code: 'no_failed_fetches', field: null }, { code: 'unresolved_correspondence', field: null },
      { code: 'not_reviewed_ad', field: null }, { code: 'attribution_unconfirmed', field: null }, { code: 'requirement_unverified', field: 'duration_s' },
      { code: 'requirement_unverified', field: 'message' }, { code: 'requirement_unverified', field: 'offer' }, { code: 'requirement_unverified', field: 'code' }] },
    citations: { [CITE]: [{ code: 'citation_only', field: null }] },
  },
  assessment_scope: { basis: 'entered_structured_requirements_only',
    by_unit: { [CITE]: { entered_fields: FIELDS, assessed_fields: [], unknown_fields: FIELDS }, [WIN]: { entered_fields: FIELDS, assessed_fields: [], unknown_fields: FIELDS } },
    limitations: ['Only entered supported checks are assessed; no whole-brief or legal compliance claim.'] },
  pins: { attachments: {} },
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'],
});
const c1Api = (draft = c1Draft(), extra = {}) => p => {
  if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'BetterHelp Q4 host reads', brand: 'betterhelp', requirement_provenance: {}, allowed_actions: draft.allowed_actions } };
  if (p === '/customer/campaigns/1/draft') return { draft };
  if (p === '/customer/campaigns/1/reports') return { reports: [] };
  if (Object.hasOwn(extra, p)) return extra[p];
  throw new Error('unexpected request ' + p);
};
const rowOf = (html, id) => html.split('<li class="ew-work-placement">').slice(1).find(c => c.includes('placement/' + encodeURIComponent(id) + '"')).split('</section>')[0];
const VERDICT_C1 = '0 of 1 counted placement has a result yet — not every episode in scope has been fully checked for ads yet. 1 more is a citation only, never counted.';

test('UX-07 supplied campaign: one verdict line that says only what the data supports', async () => {
  const { html } = await renderRoute('#/campaign/1', c1Api());
  const m = /<p class="ew-work-verdict">([^<]*)<\/p>/.exec(html);
  assert.ok(m, 'the verdict line is rendered');
  assert.equal(m[1], VERDICT_C1);
  assert.equal((html.match(/ew-work-verdict/g) || []).length, 1, 'said once');
  assert.ok(html.indexOf('ew-work-verdict') > html.indexOf('id="placements-h"'), 'the verdict sits at the top of Placements');
  assert.ok(html.indexOf('ew-work-verdict') < html.indexOf('ew-work-filters'), 'before the filters');
  assert.doesNotMatch(visible(html), /\bverif(?:ied|iable)\b|\bdelivered\b|\bran\b/i, 'never claims verification or delivery');
});

test('UX-07 show-window row: grouped reasons, folded scope, no outcome repeat, no 8-item list', async () => {
  const { html } = await renderRoute('#/campaign/1', c1Api());
  const row = rowOf(html, WIN);
  const text = visible(row);
  assert.match(text, /No full-episode check is recorded yet for “The Last 12 Weeks - Ep\. 4” and “The Last 12 Weeks - Ep\. 5”\./);
  assert.match(text, /The match here isn't checked yet: not yet matched to one distinct ad; not reviewed as an ad; advertiser not confirmed\./);
  assert.match(text, /Checked against the brief: none yet\. Couldn't be checked, so left out of the outcome: Code, Duration, Message, Offer\./);
  assert.doesNotMatch(text, /Requirement outcome: Incomplete/, 'the coverage sentence already says why');
  assert.doesNotMatch(text, /no transcript yet|can't be verified/, 'coverage and unknown-field reasons are not repeated');
  const items = (row.match(/<ul class="ew-work-reasons">([\s\S]*?)<\/ul>/) || [, ''])[1].match(/<li>/g) || [];
  assert.equal(items.length, 1, 'one grouped reason, not eight bullets');
  assert.match(row, /<details class="tech-details">[\s\S]*incomplete_coverage[\s\S]*not currently reviewed_ad[\s\S]*entered: code, duration_s, message, offer/, 'raw outcome, reasons and scope stay in Technical details');
});

test('UX-07 supplied campaign: no Assessment scope card and no false "Captured at freeze"', async () => {
  const { html } = await renderRoute('#/campaign/1', c1Api());
  assert.doesNotMatch(html, /Assessment scope/);
  assert.doesNotMatch(html, /Captured at freeze/);
  assert.match(html, /Only entered checks are assessed\. No delivery, whole-brief or legal claim is made beyond them\./);
  const cite = visible(rowOf(html, CITE));
  assert.match(cite, /Citation only — never evaluated for credit\./);
  assert.match(cite, /Checked against the brief: none yet\./, 'the citation keeps its own scope facts');
});

test('UX-07 a requirement mismatch still renders first, and several occurrences are counted', async () => {
  const draft = c1Draft();
  draft.allocation.evaluations[WIN].occurrence_order = ['o1', 'o2', 'o3'];
  draft.allocation.evaluations[WIN].reasons.push('o2: message: mismatch');
  draft.reason_codes.evaluations[WIN].push({ code: 'requirement_mismatch', field: 'message' });
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  const list = /<ul class="ew-work-reasons">([\s\S]*?)<\/ul>/.exec(rowOf(html, WIN))[1];
  const lines = list.match(/<li>([^<]*)<\/li>/g).map(l => l.replace(/<\/?li>/g, '').replace(/&#39;/g, "'"));
  assert.equal(lines[0], "Doesn't match the brief: message.");
  // F-20(2): the grouped line never implies every match has every gap.
  assert.equal(lines[1], 'The matches here still need: matching to one distinct ad; ad review; advertiser confirmation.');
});

test('UX-07 verdict counts results and never says verified', async () => {
  const draft = c1Draft();
  delete draft.allocation.citations[CITE];
  draft.coverage_by_placement[WIN] = { state: 'complete' };
  draft.coverage_by_placement[CITE] = { state: 'complete' };
  draft.allocation.evaluations[WIN].outcome = 'supported';
  draft.allocation.evaluations[CITE] = { outcome: 'no_observation_in_covered_sources', reasons: [] };
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  assert.match(html, /<p class="ew-work-verdict">2 of 2 counted placements have a result: 1 with an ad found and 1 with no matching ad in the available transcript\.<\/p>/);
});

test('R2-07 supplied campaign with nothing to freeze: disabled Freeze with its reason inline', async () => {
  const { html } = await renderRoute('#/campaign/1', c1Api());
  assert.match(html, /<button type="button" class="button" disabled aria-describedby="freeze-why">Save a final report…<\/button>/);
  assert.doesNotMatch(html, /data-action="freeze-report"/, 'the disabled control carries no write action');
  assert.match(html, /<p class="ew-work-meta" id="freeze-why">You can save a final report once a placement has a result\. None has a result yet\.<\/p>/);
  assert.match(html, /class="button primary" data-action="add-placement"/, 'Add placement stays the primary action');
});

test('R2-07 supplied campaign without the freeze permission shows no Freeze at all', async () => {
  const draft = c1Draft();
  draft.allowed_actions = ['read', 'placements'];
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  assert.doesNotMatch(html, /Freeze a report/);
  assert.doesNotMatch(html, /Save a final report/);
  assert.doesNotMatch(html, /freeze-why/);
});

const researchApi = (evidence) => p => {
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study' } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [], allocation: {},
    targets: [{ id: 't1', scope: 'episode', episode_id: 'e1', evidence }], coverage_by_placement: {} } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};

test('FREEZE r3 campaign page: a frozen report in plain words', async () => {
  for (const evidence of [[], ['sr1:abc']]) {
    const text = visible((await renderRoute('#/campaign/2', researchApi(evidence))).html);
    assert.doesNotMatch(text, /immutable/i, String(evidence));
    assert.match(text, /No frozen reports yet\. A frozen report is a saved copy that won't change\./, String(evidence));
  }
});

test('R2-07 research campaign with no observations: Freeze is a plain button with a note', async () => {
  const { html } = await renderRoute('#/campaign/2', researchApi([]));
  assert.match(html, /<button type="button" class="button ?" data-action="freeze-report"[^>]*>Save a final report…</);
  assert.doesNotMatch(html, /class="button primary" data-action="freeze-report"/);
  assert.match(html, /A report saved now holds your targets and coverage only\. There are no observations yet\./);
  assert.match(html, /ew-work-planned-btn" disabled[^>]*>Attach evidence</, 'Attach evidence stays planned');
});

test('R2-07 research campaign with observations keeps Freeze primary and no note', async () => {
  const { html } = await renderRoute('#/campaign/2', researchApi(['sr1:abc']));
  assert.match(html, /class="button primary" data-action="freeze-report"/);
  assert.doesNotMatch(html, /there are no observations yet/);
});

test('R2-07 placement page: verdict said once, grouped reasons, collapsed all-Unknown table', async () => {
  const { html } = await renderRoute('#/campaign/1/placement/' + encodeURIComponent(WIN), c1Api());
  const text = visible(html);
  assert.doesNotMatch(text, /Not enough data\. A linkage/, 'the tag already names the linkage');
  assert.match(text, /No full-episode check is recorded yet for “The Last 12 Weeks - Ep\. 4” and “The Last 12 Weeks - Ep\. 5”\. This only describes stored records; it never proves delivery or fulfillment\./);
  assert.doesNotMatch(text, /Requirement outcome: Incomplete/);
  assert.match(text, /The match here isn't checked yet: not yet matched to one distinct ad; not reviewed as an ad; advertiser not confirmed\./);
  assert.match(text, /Checked against the brief: none yet\./);
  assert.match(html, /<div class="ew-card-body ew-work-req"><p class="section-note">None of the 4 agreed fields could be checked from the stored record: Message, Offer, Code, Duration\.<\/p><details class="ew-details-quiet"><summary>Field by field<\/summary><div class="ew-table-wrap ew-work-table"><table class="data-table">/);
});

test('R2-07 placement page: a supported row keeps the open table', async () => {
  const draft = c1Draft();
  draft.comparisons[WIN][0] = { field: 'message', expected: 'x', observed: 'x', state: 'supported' };
  const { html } = await renderRoute('#/campaign/1/placement/' + encodeURIComponent(WIN), c1Api(draft));
  assert.doesNotMatch(html, /Field by field/);
  assert.match(html, /<div class="ew-table-wrap ew-work-table"><table class="data-table">/);
});

const reportApi = (frozen) => p => {
  if (p === '/customer/campaigns/1/reports/1') return { frozen, stale: false };
  throw new Error('unexpected request ' + p);
};
const c1Frozen = (codes = false) => {
  const d = c1Draft();
  const f = { captured_at: '2026-10-14T16:30:00Z', renderer: codes ? 'campaign-report/3' : 'campaign-report/2', campaign: { name: d.campaign.name },
    placements: d.placements, targets: [], evaluations: d.allocation.evaluations, allocation: { citations: d.allocation.citations },
    coverage_by_placement: d.coverage_by_placement, comparisons: d.comparisons, assessment_scope: d.assessment_scope, episode_titles: d.episode_titles,
    synthetic: false, synthetic_placements: [] };
  if (codes) f.reason_codes = d.reason_codes;
  return f;
};

test('UX-07 frozen report: verdict, per-placement coverage and scope, collapsed table, no scope card', async () => {
  const { html } = await renderRoute('#/campaign/1/report/1', reportApi(c1Frozen(true)));
  const text = visible(html);
  assert.match(html, new RegExp(`<p class="ew-work-verdict">${VERDICT_C1.replace(/[.]/g, '\\.')}</p>`));
  assert.ok(html.indexOf('ew-work-verdict') > html.indexOf('Matches what was frozen.'), 'after the current/stale line');
  assert.doesNotMatch(html, /Assessment scope/);
  assert.match(html, /<p class="ew-work-reason">No full-episode check is recorded yet for “The Last 12 Weeks - Ep\. 4” and “The Last 12 Weeks - Ep\. 5”\.<\/p>/);
  assert.match(text, /The match here isn't checked yet: not yet matched to one distinct ad; not reviewed as an ad; advertiser not confirmed\./);
  assert.doesNotMatch(text, /Another condition affects this result/);
  assert.doesNotMatch(text, /Requirement outcome: Incomplete/);
  assert.match(html, /None of the 4 agreed fields could be checked from the stored record: Message, Offer, Code, Duration\.<\/p><details class="ew-details-quiet"><summary>Field by field<\/summary>/);
  assert.match(html, /<iframe class="report-preview"/, 'the frozen preview is unchanged');
});

test('UX-07 old frozen report without reason codes points to Technical details once', async () => {
  const { html } = await renderRoute('#/campaign/1/report/1', reportApi(c1Frozen(false)));
  const text = visible(html);
  assert.match(text, /The reasons recorded when the report was saved are in Technical details\./);
  assert.doesNotMatch(text, /Another condition affects this result/);
  assert.match(html, /<details class="tech-details">[\s\S]*not currently reviewed_ad/, 'the stored reasons stay readable');
});

test('UX-07 frozen report with a supported row keeps the open table', async () => {
  const f = c1Frozen(true);
  f.comparisons[WIN][0] = { field: 'message', expected: 'x', observed: 'x', state: 'supported' };
  const { html } = await renderRoute('#/campaign/1/report/1', reportApi(f));
  assert.equal((html.match(/Field by field/g) || []).length, 1, 'only the all-Unknown placement collapses');
  assert.match(html, /<div class="ew-table-wrap ew-work-table"><table class="data-table"><caption>Placement requirements at capture time<\/caption>/);
});

// ---- fixB: F-20(1) coverage reasons, F-22c verdict branches ----
test('F-20(1) a failed-fetches reason still shows when the coverage sentence names only missing transcripts', async () => {
  const draft = c1Draft();
  draft.coverage_by_placement[WIN] = { ...draft.coverage_by_placement[WIN], failed: ['ep6'] };
  draft.reason_codes.evaluations[WIN][1] = { code: 'failed_fetches', field: null };
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  const text = visible(rowOf(html, WIN));
  assert.match(text, /No full-episode check is recorded yet for/);
  assert.match(text, /Some sources in this window couldn't be fetched\./, 'the sentence does not say it, so the reason does');
  assert.doesNotMatch(text, /no transcript yet/, 'the sentence already says the missing transcripts');
});

test('F-20(1) a failed-fetches coverage sentence still drops its own reason, but not a missing-transcripts one', async () => {
  const draft = c1Draft();
  draft.coverage_by_placement[WIN] = { ...draft.coverage_by_placement[WIN], missing: [], failed: ['ep6'] };
  draft.reason_codes.evaluations[WIN][1] = { code: 'failed_fetches', field: null };
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  const text = visible(rowOf(html, WIN));
  assert.match(text, /Some sources in this window could not be loaded\./);
  assert.doesNotMatch(text, /couldn't be fetched/);
  assert.match(text, /Some episodes in this window have no full-episode check recorded yet\./);
});

test('F-22c verdict: no coverage information says "coverage isn\'t complete yet"', async () => {
  const draft = c1Draft();
  delete draft.coverage_by_placement[WIN];
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  assert.match(html, /<p class="ew-work-verdict">0 of 1 counted placement has a result yet — coverage isn&#39;t complete yet\. 1 more is a citation only, never counted\.<\/p>/);
});

test('F-22c verdict: results plus "; N without enough data"', async () => {
  const draft = c1Draft();
  delete draft.allocation.citations[CITE];
  draft.coverage_by_placement[WIN] = { state: 'complete' };
  draft.allocation.evaluations[WIN].outcome = 'supported';
  draft.allocation.evaluations[CITE] = { outcome: 'incomplete_coverage', reasons: [] };
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  assert.match(html, /<p class="ew-work-verdict">1 of 2 counted placements have a result: 1 with an ad found; 1 without enough data\.<\/p>/);
});

// ---- MKT-02: the raw coverage reason never says "missing transcripts" ----
// The server's reason text "missing transcripts: [...]" means no
// whole-episode coverage record yet, not a missing transcript. Technical
// details show it in plain words; the stored reason text is unchanged.
const MKT02_RAW = /No full check yet: \[&#39;Ep\. 4&#39;, &#39;Ep\. 5&#39;\]/;
for (const [name, route, api] of [
  ['campaign row', '#/campaign/1', () => c1Api()],
  ['placement page', '#/campaign/1/placement/' + encodeURIComponent(WIN), () => c1Api()],
  ['frozen report', '#/campaign/1/report/1', () => reportApi(c1Frozen(true))],
  ['old frozen report without reason codes', '#/campaign/1/report/1', () => reportApi(c1Frozen(false))],
]) {
  test(`MKT-02 ${name}: Technical details say "No full check yet", never "missing transcripts"`, async () => {
    const { html } = await renderRoute(route, api());
    assert.match(html, MKT02_RAW);
    assert.doesNotMatch(html, /missing transcripts/i);
    assert.match(html, /failed fetches: \[\]/, 'other raw reasons stay as stored');
  });
}

// MKT-02 addition: the coverage sentence and the verdict agree with the
// Technical details wording; none implies a transcript is missing.
for (const [name, route, api] of [
  ['campaign page', '#/campaign/1', () => c1Api()],
  ['placement page', '#/campaign/1/placement/' + encodeURIComponent(WIN), () => c1Api()],
  ['frozen report', '#/campaign/1/report/1', () => reportApi(c1Frozen(true))],
]) {
  test(`MKT-02 ${name}: shown coverage lines never say "transcript coverage"`, async () => {
    const { html } = await renderRoute(route, api());
    const text = visible(html);
    assert.match(text, /No full-episode check is recorded yet for “The Last 12 Weeks - Ep\. 4” and “The Last 12 Weeks - Ep\. 5”\./);
    assert.doesNotMatch(text, /transcript coverage/i);
  });
}

// MKT-02 code review r1: an empty coverage part is not shown, and only a
// reason that starts with the phrase is reworded.
test('MKT-02 only failed fetches: the empty "missing transcripts: []" part is dropped, other reasons stay', async () => {
  const draft = c1Draft();
  draft.coverage_by_placement[WIN] = { ...draft.coverage_by_placement[WIN], missing: [], failed: ['ep6'] };
  draft.allocation.evaluations[WIN].reasons[0] = 'missing transcripts: []';
  draft.allocation.evaluations[WIN].reasons[1] = "failed fetches: ['Ep. 6']";
  draft.reason_codes.evaluations[WIN][0] = { code: 'no_missing_transcripts', field: null };
  draft.reason_codes.evaluations[WIN][1] = { code: 'failed_fetches', field: null };
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  const row = rowOf(html, WIN);
  assert.doesNotMatch(row, /No full check yet: \[\]/);
  assert.doesNotMatch(row, /missing transcripts/i);
  assert.match(row, /failed fetches: \[&#39;Ep\. 6&#39;\]/);
  assert.match(row, /named:13ae6be7: not currently reviewed_ad/);
});

test('MKT-02 the phrase is reworded only at the start of a reason', async () => {
  const draft = c1Draft();
  draft.allocation.evaluations[WIN].reasons.push('named:13ae6be7: note mentions missing transcripts: none');
  draft.reason_codes.evaluations[WIN].push({ code: 'other', field: null });
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  const row = rowOf(html, WIN);
  assert.match(row, /named:13ae6be7: note mentions missing transcripts: none/, 'mid-reason text stays as stored');
  assert.match(row, MKT02_RAW, 'the leading phrase is still reworded');
});

// MKT-02 screen r1: the chip and a fetch-failed episode read in plain words.
test('MKT-02 the coverage chip reads "Not fully checked", never "Coverage incomplete"', async () => {
  const { html } = await renderRoute('#/campaign/1', c1Api());
  const text = visible(rowOf(html, WIN));
  assert.match(text, /Not fully checked/);
  assert.doesNotMatch(text, /Coverage incomplete/);
});

test('MKT-02 a fetch-failed episode: the sentence never claims no check exists', async () => {
  const draft = c1Draft();
  draft.coverage_by_placement[WIN] = { state: 'incomplete', missing: ['ep4'], failed: [], episodes: ['ep4'],
    exclusions: ['ep4: latest fetch failed'], episode_labels: { ep4: 'The Last 12 Weeks - Ep. 4' } };
  const { html } = await renderRoute('#/campaign/1', c1Api(draft));
  const text = visible(rowOf(html, WIN));
  assert.match(text, /No full check of “The Last 12 Weeks - Ep\. 4” yet, so a missed ad can't be ruled out\./);
  assert.doesNotMatch(text, /No full-episode check is recorded yet/);
});

// POLISH 6c: the frozen report shows the same coverage chip as the
// campaign page, next to the verdict chip, from the frozen coverage.
for (const [name, codes] of [['new frozen report', true], ['old frozen report', false]]) {
  test(`POLISH ${name}: the placement card shows the coverage chip beside the verdict chip`, async () => {
    const { html } = await renderRoute('#/campaign/1/report/1', reportApi(c1Frozen(codes)));
    const cards = html.split('<section class="ew-card ew-work-card">').slice(1).filter(c => c.includes('placement-head'));
    assert.ok(cards.length > 0);
    for (const card of cards) {
      const head = card.slice(0, card.indexOf('<div class="ew-card-body">'));
      assert.match(visible(head), /Not fully checked/);
    }
    assert.match(visible(rowOf((await renderRoute('#/campaign/1', c1Api())).html, WIN)), /Not fully checked/, 'same chip as the campaign page');
  });
}

test('POLISH frozen report: a complete placement shows "Coverage complete", a missing coverage "Coverage not checked"', async () => {
  const f = c1Frozen(true);
  f.coverage_by_placement = { ...f.coverage_by_placement, [WIN]: { ...f.coverage_by_placement[WIN], state: 'complete', missing: [], failed: [] } };
  const { html } = await renderRoute('#/campaign/1/report/1', reportApi(f));
  const heads = html.split('<section class="ew-card ew-work-card">').slice(1).filter(c => c.includes('placement-head')).map(c => visible(c.slice(0, c.indexOf('<div class="ew-card-body">'))));
  assert.ok(heads.some(h => /Coverage complete/.test(h)));
  const g = c1Frozen(true);
  g.coverage_by_placement = {};
  const none = await renderRoute('#/campaign/1/report/1', reportApi(g));
  assert.match(visible(none.html.split('<section class="ew-card ew-work-card">')[1].split('<div class="ew-card-body">')[0]), /Coverage not checked/);
});

// POL-V1-01: the verdict chip and the coverage chip sit together in one
// `.actions` wrapper, so the card head's space-between does not split them.
test('POL-V1-01 frozen report: the verdict chip and the coverage chip share one actions wrapper', async () => {
  const { html } = await renderRoute('#/campaign/1/report/1', reportApi(c1Frozen(true)));
  const cards = html.split('<section class="ew-card ew-work-card">').slice(1).filter(c => c.includes('class="placement-head"'));
  assert.ok(cards.length > 0);
  for (const card of cards) {
    const head = card.slice(0, card.indexOf('<div class="ew-card-body">'));
    const wrappers = head.match(/<span class="actions">[\s\S]*?<\/span><\/span>/g) || [];
    assert.equal(wrappers.length, 1, 'one actions wrapper in the card head');
    assert.match(visible(wrappers[0]), /Not fully checked/, 'coverage chip inside the wrapper');
    assert.match(wrappers[0], /class="tag[^"]*"/, 'a verdict chip inside the wrapper');
    assert.ok(head.indexOf('<span class="actions">') < head.indexOf('Not fully checked'));
    assert.equal(head.replace(wrappers[0], '').includes('Not fully checked'), false, 'no coverage chip outside the wrapper');
  }
});
