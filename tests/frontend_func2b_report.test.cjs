/* FUNC-2b: per-episode verdicts in a report frozen after confirmation.
   The Report page lists the frozen episode_verdicts read-only (lead
   LEDGER-LEAD3-F2B-1): FUNC-2's verdict strings, a decided row reads
   "Confirmed · <date>" / "Rejected · <date>" (the confirm date in the
   app's day form, EW.formatDay via timeHtml day), no member name, no
   Confirm / Reject / Undo, no Play. With episode data the old
   per-placement lines go to Technical details (owner, UX ask
   2026-10-28). A report frozen before (no field) renders as before.
   Fixture: tests/fixtures/func2/episode-verdicts.json, frozen here.
   Run: node --test tests/frontend_func2b_report.test.cjs */
'use strict';
// The golden snapshots show local times (the app's default zone is the
// machine's), rendered in this zone; pin it before anything formats a date.
process.env.TZ = 'America/Los_Angeles';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const APP = path.join(ROOT, 'app');
const FIXTURE = path.join(__dirname, 'fixtures', 'func2', 'episode-verdicts.json');
// Today's Report page for a report frozen before FUNC-2b (no episode
// verdicts), as golden snapshots: tests/fixtures/func2b/report-no-verdicts-
// {owner,viewer}.html, rendered by aa86d3a9's own views.js from FIXTURE
// (WIN1-APP-FIX4b). A snapshot does not depend on git history, commit
// messages or how far views.js has grown; regenerate it only on purpose.
const golden = role => fs.readFileSync(path.join(__dirname, 'fixtures', 'func2b', `report-no-verdicts-${role}.html`), 'utf8');
const ROUTE = '#/campaign/1/report/1';

function loadAt(route, fetch) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch,
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
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function renderReport(frozen, { role = 'owner', views = fs.readFileSync(path.join(APP, 'views.js'), 'utf8') } = {}) {
  const win = loadAt('#/overview', sessionFetch);
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(views, win, { filename: 'views.js' });
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {};
  win.EW.api = async (p) => {
    if (p === '/customer/campaigns/1/reports/1') return { frozen, stale: false };
    throw new Error('unexpected request ' + p);
  };
  win.EW.current.workspaceRole = role;
  win.location.hash = ROUTE;
  win.EW.current.route = ROUTE;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ')
  .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const techText = html => (String(html).match(/<details class="tech-details">[\s\S]*?<\/details>/g) || []).join(' ').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
const epRow = (html, eid) => {
  const at = html.indexOf(`data-episode="${eid}"`);
  assert.ok(at >= 0, 'row for ' + eid);
  const start = html.lastIndexOf('<li class="ew-ep"', at);
  const ends = [html.indexOf('<li class="ew-ep"', at), html.indexOf('</ol>', at)].filter(i => i >= 0);
  assert.ok(start >= 0 && ends.length, 'row bounds for ' + eid);
  return html.slice(start, Math.min(...ends));
};
const verdictOf = (html, eid) => {
  const m = /<span class="ew-ep-verdict">([^<]*)<\/span>/.exec(epRow(html, eid));
  return m ? m[1] : null;
};
// One placement card of the report, by its heading text.
const card = (html, head) => {
  const at = html.indexOf(head);
  assert.ok(at >= 0, 'card ' + head);
  const start = html.lastIndexOf('<section class="ew-card ew-work-card">', at);
  return html.slice(start, html.indexOf('</section>', at));
};

/* The agreed frozen shape (Fresh's FUNC-2b): the draft's episode_verdicts,
   each review as { decision, at } only (no by_name), on the reads too.
   Plus "Quince after Confirm": the r2 shot's placement (4 ads waiting for
   review, 3 matches still needing advertiser confirmation), frozen right
   after one Confirm. */
const QUINCE = 'pl-q';
function frozenReport({ verdicts = true } = {}) {
  const d = JSON.parse(fs.readFileSync(FIXTURE, 'utf8')).draft;
  const strip = r => (r && r.decision ? { decision: r.decision, at: r.at } : r);
  const ev = JSON.parse(JSON.stringify(d.episode_verdicts));
  for (const v of Object.values(ev)) {
    for (const ep of v.episodes) {
      ep.review = strip(ep.review);
      for (const r of ep.reads || []) r.review = strip(r.review);
    }
  }
  ev[QUINCE] = { summary: { heard: 1, heard_right: 1, heard_wrong: 0, not_heard: 0, checked: 1, waiting_review: 0, not_checked: 0, unconfirmed: 0 },
    episodes: [{ episode_id: 'e-q1', show: 'Ask Daddy', episode_title: "#431: Daddy's $20 Little Habit", published: '2026-10-21', verdict: 'heard_right',
      mismatch: [], said_code: null, brief_code: null, evidence_id: 'ev-q1', evidence_ref: 'sr1:' + 'a'.repeat(32), has_clip: true, display_span: [600, 640],
      waiting_for_review: 0, not_checked_reason: null, review_revision: 2, basis: 'judge_review', review: { decision: 'confirmed', at: '2026-10-28T08:30:00Z' },
      reads: [{ evidence_id: 'ev-q1', evidence_ref: 'sr1:' + 'a'.repeat(32), machine_code: null, machine_url: null, brief_code: null, has_clip: true, display_span: [600, 640],
        review: { decision: 'confirmed', at: '2026-10-28T08:30:00Z' } }] }] };
  // WIN3-FIX2: frozen at campaign-report/4.3, whose summary leads "Confirmed heard in".
  const frozen = {
    renderer: 'campaign-report/4.3',
    campaign: d.campaign, campaign_revision: 4, captured_at: '2026-10-28T09:00:00Z',
    placements: d.placements.concat([{ id: QUINCE, scope: 'episode', episode_id: 'e-q1', expected_count: 1, binding: 'eligible', requirements: {}, provenance: { binding: 'eligible' } }]),
    episode_titles: Object.assign({}, d.episode_titles, { 'e-q1': "#431: Daddy's $20 Little Habit" }),
    evaluations: Object.assign({}, d.allocation.evaluations, { [QUINCE]: { placement_id: QUINCE, outcome: 'unreviewed_detections_in_scope',
      eligible_ids: [], allocated_elsewhere: [], occurrence_order: ['o1', 'o2', 'o3'],
      reasons: ['unreviewed detections in scope', 'o1: attribution unconfirmed', 'o2: not reviewed ad'] } }),
    coverage_by_placement: Object.assign({}, d.coverage_by_placement, { [QUINCE]: { state: 'complete', missing: [], failed: [], episodes: ['e-q1'], waiting_for_review: 4 } }),
    comparisons: { 'pl-ep': [{ field: 'message', expected: 'Sleep better', observed: null, state: 'unknown' }] },
    allocation: { citations: {} },
    reason_codes: { evaluations: { [QUINCE]: [{ code: 'unreviewed_detections_in_scope' }, { code: 'attribution_unconfirmed' }, { code: 'not_reviewed_ad' }] }, citations: {} },
    assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: { [QUINCE]: { entered_fields: [], assessed_fields: [], unknown_fields: [] },
      'pl-ep': { entered_fields: ['message'], assessed_fields: [], unknown_fields: ['message'] } } },
  };
  if (verdicts) frozen.episode_verdicts = ev;
  return frozen;
}
const dayOf = (win, at) => win.EW.formatDay(at);

// ---- strings ----------------------------------------------------------------

test('FUNC2B report: every verdict string is FUNC-2\'s, exact and escaped', async () => {
  const { html } = await renderReport(frozenReport());
  assert.equal(verdictOf(html, 'e10'), 'Heard, right code');
  assert.equal(verdictOf(html, 'e11'), 'Heard, wrong code: said FURN40, brief says FERN40; doesn&#39;t match the brief: offer said 20% off, brief says &lt;b&gt;40% off&lt;/b&gt;');
  assert.equal(verdictOf(html, 'e12'), 'Heard, code sounds like FURN&lt;40&gt; (brief: FERN40) · not confirmed');
  assert.equal(verdictOf(html, 'e13'), 'Waiting for review');
  assert.equal(verdictOf(html, 'e14'), 'Not heard in this episode');
  assert.equal(verdictOf(html, 'e16'), 'Not checked yet');
  assert.equal(verdictOf(html, 'e21'), 'Heard');
  assert.equal(verdictOf(html, 'e22'), 'Heard · not confirmed');
  assert.equal(verdictOf(html, 'e41'), 'Heard, wrong URL: said fern.co/&lt;x&gt;, brief says fern.com/40; doesn&#39;t match the brief: length said 45, brief says 60–90');
  assert.equal(verdictOf(html, 'e46'), 'Heard, no code heard (brief: FERN40)');
  assert.equal(verdictOf(html, 'e-q1'), 'Heard');
  assert.doesNotMatch(html, /<b>40% off<\/b>|FURN<40>|<i>live<\/i>/, 'no raw markup from the payload');
});

test('FUNC2B report: a decided row reads "Confirmed · <date>" / "Rejected · <date>" (EW.formatDay)', async () => {
  const frozen = frozenReport();
  const { win, html } = await renderReport(frozen);
  const at10 = frozen.episode_verdicts['pl-win'].episodes[0].review.at;
  const at15 = frozen.episode_verdicts['pl-win'].episodes[5].review.at;
  assert.equal(frozen.episode_verdicts['pl-win'].episodes[5].review.decision, 'rejected');
  assert.match(visible(epRow(html, 'e10')), new RegExp(`Heard, right code Confirmed · ${dayOf(win, at10)} $`.replace(/ /g, '\\s*')));
  assert.match(visible(epRow(html, 'e15')), new RegExp(`Not heard in this episode Rejected · ${dayOf(win, at15)} $`.replace(/ /g, '\\s*')));
  assert.match(visible(epRow(html, 'e-q1')), /Heard\s*Confirmed · Oct 28\s*$/);
  // The date is a <time> with the exact instant (timeHtml, day form).
  assert.match(epRow(html, 'e10'), new RegExp(`Confirmed · <time datetime="${at10}"`));
  // An undecided row says neither.
  assert.doesNotMatch(visible(epRow(html, 'e11')), /Confirmed|Rejected/);
});

test('FUNC2B report: an unknown decision shows no word (F2B-APP-R1-01)', async () => {
  const frozen = frozenReport();
  frozen.episode_verdicts['pl-win'].episodes[0].review = { decision: 'other', at: '2026-10-20T10:00:00Z' };
  const { html } = await renderReport(frozen);
  assert.doesNotMatch(visible(epRow(html, 'e10')), /Confirmed|Rejected/);
});

test('FUNC2B report: a decided review with no date says only "Confirmed"', async () => {
  const frozen = frozenReport();
  delete frozen.episode_verdicts['pl-win'].episodes[0].review.at;
  const { html } = await renderReport(frozen);
  assert.match(visible(epRow(html, 'e10')), /Heard, right code\s*Confirmed\s*$/);
  assert.doesNotMatch(epRow(html, 'e10'), /Date unknown|·\s*$/);
});

test('FUNC2B report: each placement shows the frozen episode summary; a scope problem shows none', async () => {
  const { html } = await renderReport(frozenReport());
  const text = visible(html);
  assert.ok(text.includes('Confirmed heard in 2 of 6 checked episodes · 1 episode to confirm · 1 episode waiting for review · 1 episode not checked'), text);
  assert.ok(text.includes('Confirmed heard in 1 of 1 checked episode'), 'Quince after Confirm');
  assert.doesNotMatch(card(html, 'Show window 3'), /ew-ep-summary/);
});

// F2B-APP-SCR-R1-01: the card head's episode tag sits in the
// [data-part="tag"] wrapper, so FIX5's wrap rule (work.css) applies at 390.
// No data-episodes: a live review repaint never touches a frozen report.
test('FUNC2B report: the card head episode tag sits inside [data-part="tag"] (F2B-APP-SCR-R1-01)', async () => {
  const { html } = await renderReport(frozenReport());
  const heads = html.match(/<div class="ew-card-head">[\s\S]*?<\/div>/g) || [];
  const tagged = heads.filter(h => /Confirmed heard in \d+ of \d+ checked episode/.test(h));
  assert.ok(tagged.length >= 2, 'episode tags in card heads');
  for (const h of tagged) {
    assert.match(h, /<span class="actions"><span data-part="tag"><span class="tag[^"]*">Confirmed heard in \d+ of \d+ checked episodes?[^<]*<\/span><\/span>/, h);
  }
  assert.ok(html.includes('<span data-part="tag"><span class="tag amber">Confirmed heard in 2 of 6 checked episodes · 1 episode to confirm · 1 episode waiting for review · 1 episode not checked</span></span>'));
  assert.doesNotMatch(html, /data-episodes=/, 'no live repaint hook on a frozen report');
});

test('FUNC2B report: the date and show · episode on each row, escaped', async () => {
  const { html } = await renderReport(frozenReport());
  const row = epRow(html, 'e10');
  assert.match(row, /<span class="ew-ep-date">Oct 20<\/span>/);
  assert.match(row, /<span class="ew-ep-where">Morning Show · Ep\. 10 &lt;i&gt;live&lt;\/i&gt;<\/span>/);
});

// ---- read-only ----------------------------------------------------------------

test('FUNC2B report: no Confirm, Reject, Undo, Code heard, Play or Open evidence, even for an owner', async () => {
  for (const role of ['owner', 'editor', 'viewer']) {
    const { html } = await renderReport(frozenReport(), { role });
    assert.doesNotMatch(html, /data-action="episode-review"|name="code"|Undo review|>Confirm<|>Reject<|Code heard/, role);
    assert.doesNotMatch(html, /search-play|data-action="play|ew-ep-act|Open evidence|#\/evidence\//, role);
    assert.doesNotMatch(html, /data-episodes=/, role + ': nothing a review repaint could target');
  }
});

test('FUNC2B report: no member name anywhere, even if a name leaks into the frozen payload', async () => {
  const frozen = frozenReport();
  const NAMES = ['Dana & <b>Co</b>', 'Robin Judge', 'Sam Q.'];
  let i = 0;
  for (const v of Object.values(frozen.episode_verdicts)) {
    for (const ep of v.episodes) {
      if (ep.review) ep.review.by_name = NAMES[i++ % NAMES.length];
      for (const r of ep.reads || []) if (r.review) r.review.by_name = NAMES[i++ % NAMES.length];
    }
  }
  assert.ok(i >= 3, 'names injected');
  const { html } = await renderReport(frozen);
  for (const n of ['Dana', 'Robin', 'Sam Q', '&lt;b&gt;Co']) assert.ok(!html.includes(n), 'no name: ' + n);
  assert.doesNotMatch(html, /Confirmed by|Rejected by/);
});

// ---- the old lines after a Confirm (owner, UX ask 2026-10-28) ---------------

test('FUNC2B report frozen right after a Confirm: the old waiting/needs lines are in Technical details only', async () => {
  const { html } = await renderReport(frozenReport());
  const q = card(html, 'Daddy');
  const shown = visible(q);
  assert.ok(shown.includes('Confirmed heard in 1 of 1 checked episode'), shown);
  assert.doesNotMatch(shown, /waiting for review/, 'no "4 ads waiting for review"');
  assert.doesNotMatch(shown, /still need|advertiser confirmation|Requirement outcome|still waiting for review/, shown);
  assert.match(techText(q), /unreviewed_detections_in_scope/);
  // FIX5: "No checks entered…" stays visible, once.
  assert.equal((shown.match(/No checks entered for this placement/g) || []).length, 1, shown);
  // The verdict line counts by the episode rule: Quince has a result.
  const line = /<p class="ew-work-verdict">([^<]*)<\/p>/.exec(html)[1];
  assert.ok(line.includes('5 of 5 counted placements have a result'), line);
});

test('FUNC2B report: with episodes, the all-unknown requirements line is in Technical details', async () => {
  const { html } = await renderReport(frozenReport());
  const ep = card(html, 'Show window 2');
  assert.doesNotMatch(visible(ep), /couldn't be checked from the stored record/);
  assert.match(techText(ep), /couldn't be checked from the stored record/);
});

test('FUNC2B report frozen before this change: the same old lines stay visible', async () => {
  const { html } = await renderReport(frozenReport({ verdicts: false }));
  const shown = visible(card(html, 'Daddy'));
  assert.ok(shown.includes('4 ads waiting for review'), shown);
  assert.ok(shown.includes('Requirement outcome:'), shown);
  assert.ok(shown.includes('The matches here still need:'), shown);
  assert.doesNotMatch(html, /ew-ep|ew-episodes|[Hh]eard in /);
});

test('FUNC2B report frozen before this change renders byte-equal to today\'s Report page', async () => {
  for (const role of ['owner', 'viewer']) {
    const today = golden(role);
    const now = await renderReport(frozenReport({ verdicts: false }), { role });
    assert.ok(today.length > 1000, 'snapshot');
    assert.equal(now.html, today, `${role}: a report frozen before FUNC-2b no longer renders as its golden snapshot`
      + ' (tests/fixtures/func2b); regenerate the snapshot only for an intended change to the old Report page');
  }
  // An empty field is the same as none.
  const empty = frozenReport({ verdicts: false });
  empty.episode_verdicts = {};
  assert.equal((await renderReport(empty)).html, golden('owner'));
});
