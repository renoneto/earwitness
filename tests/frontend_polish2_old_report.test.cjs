/* POLISH-2 (QW7-S2-02): a frozen report from before campaign-report/4.1
   keeps the earlier wording in its stored copy, and says so.
   Run: node --test tests/frontend_polish2_old_report.test.cjs */
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

const LINE = 'This report uses earlier wording: "Possible ad" is now "Ad found".';
const count = (s, sub) => s.split(sub).length - 1;
const render = async (renderer) => {
  const f = c1Frozen(true);
  if (renderer === undefined) delete f.renderer; else f.renderer = renderer;
  return renderRoute('#/campaign/1/report/1', reportApi(f));
};
const iframeTag = html => (html.match(/<iframe class="report-preview"[^>]*><\/iframe>/) || [])[0];
const IFRAME = '<iframe class="report-preview" title="Frozen report preview" sandbox="" src="/api/v1/customer/campaigns/1/reports/1/html"></iframe>';

test('POLISH-2 campaign-report/4 says it uses earlier wording, between the heading and the preview', async () => {
  const { html } = await render('campaign-report/4');
  assert.equal(count(visible(html), LINE), 1);
  const note = html.indexOf(`<p class="section-note">${LINE}</p>`);
  assert.ok(note > html.indexOf('Frozen preview'), 'after the Frozen preview heading');
  assert.ok(note > -1 && note < html.indexOf('<iframe class="report-preview"'), 'before the iframe');
});

for (const renderer of ['campaign-report/3', 'campaign-report/3.1', 'campaign-report/2', undefined, null, '']) {
  test(`POLISH-2 renderer ${JSON.stringify(renderer)} shows the earlier-wording line`, async () => {
    const { html } = await render(renderer);
    assert.equal(count(visible(html), LINE), 1);
    assert.equal(iframeTag(html), IFRAME);
  });
}

for (const renderer of ['campaign-report/4.1', 'campaign-report/4.2', 'campaign-report/5', 'something-else']) {
  test(`POLISH-2 renderer ${JSON.stringify(renderer)} does not show the earlier-wording line`, async () => {
    const { html } = await render(renderer);
    assert.equal(count(visible(html), LINE), 0);
    assert.equal(iframeTag(html), IFRAME);
  });
}
