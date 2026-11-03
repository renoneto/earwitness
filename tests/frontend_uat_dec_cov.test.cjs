/* D-2 follow-up (hard rule 1) + review #1 R1-03: complete coverage with
   ads in the placement's scope still waiting for review is its own amber
   linkage `unreviewed` ("N ads waiting for review"), under Needs attention:
   never "no match" and never a possible ad. Freeze stays allowed.
   Run: node --test tests/frontend_uat_dec_cov.test.cjs */
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

const PID = 'exp:5c876f060783';
const draft = (cov, outcome, codes) => ({
  campaign: { id: 6, revision: 2, name: 'Shopify check' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [{ id: PID, scope: 'episode', show_id: 's1', episode_id: 's002e1', expected_count: 1, binding: 'eligible', provenance: { binding: 'eligible' } }],
  targets: [], episode_titles: { s002e1: 'Mitrice Richardson Part 2' },
  coverage_by_placement: { [PID]: cov },
  comparisons: { [PID]: [] },
  allocation: { evaluations: { [PID]: { placement_id: PID, outcome, eligible_ids: [], allocated_elsewhere: [], occurrence_order: [],
    reasons: codes.map(c => c.text) } }, citations: {} },
  reason_codes: { evaluations: { [PID]: codes.map(c => ({ code: c.code, field: null })) }, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: { [PID]: { entered_fields: [], assessed_fields: [], unknown_fields: [] } }, limitations: [] },
  pins: { attachments: {} },
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'],
});
const COMPLETE = n => ({ state: 'complete', missing: [], failed: [], episodes: ['s002e1'], episode_labels: { s002e1: 'Mitrice Richardson Part 2' }, waiting_for_review: n });
const api = d => p => {
  if (p === '/customer/campaigns/6') return { campaign: { id: 6, mode: 'supplied_requirements', revision: 2, name: 'Shopify check', brand: 'shopify', requirement_provenance: {}, allowed_actions: d.allowed_actions } };
  if (p === '/customer/campaigns/6/draft') return { draft: d };
  if (p === '/customer/campaigns/6/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};
const UNREVIEWED = draft(COMPLETE(2), 'unreviewed_detections_in_scope',
  [{ code: 'unreviewed_detections_in_scope', text: 'no reviewed match yet; reads in scope wait for review' }]);

test('campaign page: ads waiting for review are their own amber linkage, never "no match" or a possible ad', async () => {
  const { html } = await renderRoute('#/campaign/6', api(UNREVIEWED));
  const text = visible(html);
  assert.match(html, /<span class="tag amber">2 ads waiting for review<\/span>/);
  assert.doesNotMatch(text, /No match found|No matching (?:detection|ad)|no matching (?:detection|ad)/);
  assert.doesNotMatch(text, /\d+ possible (?:detection|ad)|Possible (?:detection|ad)|[Uu]nreviewed detection/);
  assert.match(text, /0 of 1 counted placement has a result yet — 1 with ads waiting for review\./);
  assert.match(text, /Requirement outcome: Ads in scope are waiting for review, so a match can't be ruled out\./);
  assert.match(html, /data-action="freeze-report"/, 'Freeze stays allowed');
  assert.doesNotMatch(text, /Another condition affects this result/, 'the reason has its own copy');
  assert.match(text, /Ads found · 0/);
  assert.match(text, /Needs attention · 1/);
});

test('placement page says the same', async () => {
  const { html } = await renderRoute('#/campaign/6/placement/' + encodeURIComponent(PID), api(UNREVIEWED));
  const text = visible(html);
  assert.match(text, /2 ads waiting for review/);
  assert.doesNotMatch(text, /No match found|No matching (?:detection|ad)|Possible (?:detection|ad)/);
});

test('one ad waiting is singular', async () => {
  const { html } = await renderRoute('#/campaign/6', api(draft(COMPLETE(1), 'unreviewed_detections_in_scope',
    [{ code: 'unreviewed_detections_in_scope', text: 'no reviewed match yet; reads in scope wait for review' }])));
  assert.match(html, /<span class="tag amber">1 ad waiting for review<\/span>/);
});

test('verdict names waiting placements apart from results', async () => {
  const two = draft(COMPLETE(1), 'unreviewed_detections_in_scope', []);
  const P2 = 'exp:000000000002';
  two.placements.push({ ...two.placements[0], id: P2 });
  two.coverage_by_placement[P2] = COMPLETE(0);
  two.allocation.evaluations[P2] = { placement_id: P2, outcome: 'no_observation_in_covered_sources', eligible_ids: [], allocated_elsewhere: [], occurrence_order: [], reasons: [] };
  two.reason_codes.evaluations[P2] = [];
  two.comparisons[P2] = [];
  two.assessment_scope.by_unit[P2] = { entered_fields: [], assessed_fields: [], unknown_fields: [] };
  const { html } = await renderRoute('#/campaign/6', api(two));
  assert.match(visible(html), /1 of 2 counted placements have a result: 1 with no matching ad in the available transcript; 1 with ads waiting for review\./);
});

test('no ad at all keeps the no-match copy', async () => {
  const { html } = await renderRoute('#/campaign/6', api(draft(COMPLETE(0), 'no_observation_in_covered_sources', [])));
  assert.match(visible(html), /No matching ad in the available transcript/);
});

test('Overview lists a waiting placement under Needs attention, amber, never green', async () => {
  const ov = { coverage: { episodes: 1, episodes_with_transcript: 1, shows: 1 }, recent_events: [], offer_sequence: [],
    alert_totals: [], alert_lifecycle_counts: { active_local: 0, paused: 0, stopped: 0 },
    tracked_campaigns: [{ id: 6, name: 'Shopify check', state: 'Tracking', brand: 'shopify', brand_display: 'Shopify' }],
    campaign_gaps: [{ campaign_id: 6, campaign_name: 'Shopify check', placement_id: PID, outcome: 'unreviewed_detections_in_scope',
      linkage: 'unreviewed', label: '1 waiting for review', placement_label: 'Mitrice Richardson Part 2',
      placement_href: '#/campaign/6/placement/' + PID }], activity: [] };
  const { html } = await renderRoute('#/overview', p => {
    if (p === '/customer/overview') return ov;
    if (p.startsWith('/customer/search')) return { items: [] };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    if (p === '/customer/brands') return { brands: [] };
    return {};
  });
  assert.match(visible(html), /1 placement needs attention/);
  assert.match(html, /<span class="tag amber">1 waiting for review<\/span>/);
  assert.doesNotMatch(html, /<span class="tag green">1 waiting for review/);
});
