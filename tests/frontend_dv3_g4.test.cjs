/* Design v3 G4 (Alerts + Campaigns) render checks.
   Loads the real app scripts in a stubbed browser sandbox (helpers copied
   from frontend_query.test.cjs) and asserts the restyled pages keep the
   product rules: states from the server fields, no Meaning option,
   display-only filters, research has no verdict and no attach write.
   Run: node --test tests/frontend_dv3_g4.test.cjs */
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
function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function renderRoute(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const rowOf = (html, name) => new RegExp(`<li class="ew-work-alert"[^>]*>(?:(?!</li>).)*${name}(?:(?!</li>).)*</li>`).exec(html)[0];

const base = { revision: 1, definition_revision: 1, query: { mode: 'ads', q: 'x' }, last_successful_at: '2026-10-14T09:00:00Z', baseline_complete: true };
const ALERTS = [
  { ...base, id: 1, name: 'Fresh', lifecycle: 'active_local', unread_event_count: 2 },
  { ...base, id: 2, name: 'Calm', lifecycle: 'active_local', unread_event_count: 0 },
  { ...base, id: 3, name: 'Broken', lifecycle: 'active_local', last_attempt_result: 'error', unread_event_count: 0 },
  { ...base, id: 4, name: 'Waiting', lifecycle: 'active_local', baseline_complete: false, unread_event_count: 0 },
  { ...base, id: 5, name: 'Held', lifecycle: 'paused', unread_event_count: 0 },
  { ...base, id: 6, name: 'Ended', lifecycle: 'stopped', unread_event_count: 0 },
];

test('G4 alerts hub: six states from the server fields, display-only tabs, no Meaning option', async () => {
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: ALERTS };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  const looks = { Fresh: ['ew-tag-confirmed', 'Active'], Calm: ['ew-tag-outline', 'Quiet'], Broken: ['ew-tag-gap', 'Check failed'],
    Waiting: ['ew-tag-pending', 'Setup required'], Held: ['ew-tag-outline', 'Paused'], Ended: ['ew-tag-neutral', 'Stopped'] };
  for (const [name, [cls, label]] of Object.entries(looks)) {
    assert.match(rowOf(html, name), new RegExp(`class="ew-tag ${cls} ew-work-state"[^>]*>(?:<span[^>]*>[^<]*</span>)?${label}<`), name);
  }
  assert.match(rowOf(html, 'Broken'), /not “no matches”/, 'a failed check never reads as no matches');
  assert.match(rowOf(html, 'Broken'), /data-action="check-alert"[^>]*>Check again</);
  assert.doesNotMatch(rowOf(html, 'Calm'), /check-alert/, 'Check again only on a failed check');
  assert.match(rowOf(html, 'Held'), /data-action="pause-alert"[^>]*data-rev="1" data-lifecycle="paused">Resume</, 'Resume keeps the displayed revision');
  assert.doesNotMatch(rowOf(html, 'Ended'), /pause-alert/, 'a stopped alert has no lifecycle control');
  assert.match(html, /data-tab="all" aria-pressed="true"[^>]*>All · 6</);
  assert.match(html, /data-tab="new" aria-pressed="false"[^>]*>New matches · 1</);
  assert.match(html, /data-tab="attention" aria-pressed="false"[^>]*>Needs attention · 2</);
  assert.match(html, /data-tab="stopped" aria-pressed="false"[^>]*>Stopped · 1</);
  assert.match(html, /Alerts show here in the app\. EarWitness sends no email\./);
  assert.doesNotMatch(html, /Meaning/, 'Meaning alerts are out of scope');
});

test('G4 alerts tab hides rows outside its group and changes nothing else', async () => {
  const win = loadViews();
  const mk = (groups) => ({ hidden: false, dataset: { alertGroups: groups } });
  const rows = [mk('all new'), mk('all attention'), mk('all stopped'), mk('all')];
  const tabs = ['all', 'new', 'attention', 'stopped'].map(t => ({ dataset: { action: 'alerts-tab', tab: t }, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } }));
  const main = { querySelectorAll: sel => (sel === '[data-alert-groups]' ? rows : sel === '[data-action="alerts-tab"]' ? tabs : []) };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  let calls = 0;
  win.EW.api = async () => { calls++; return {}; };
  await win.EWViews.handleAction(tabs[2]);
  assert.deepEqual(rows.map(r => r.hidden), [true, false, true, true]);
  assert.deepEqual(tabs.map(t => t.attrs['aria-pressed']), ['false', 'false', 'true', 'false']);
  await win.EWViews.handleAction(tabs[0]);
  assert.deepEqual(rows.map(r => r.hidden), [false, false, false, false]);
  assert.equal(calls, 0, 'a tab never fetches or writes');
});

test('G4 research campaign: targets, coverage and notes from the API, attach shown as planned, no verdict', async () => {
  const { html } = await renderRoute('#/campaign/2', p => {
    if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study', notes: 'Ask <the> agency',
      targets: [{ id: 't1', scope: 'episode', episode_id: 'e1' }, { id: 't2', scope: 'show_window', show_id: 's1', date_from: '2026-10-01', date_to: '2026-10-10' }] } };
    if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [],
      targets: [{ id: 't1', scope: 'episode', episode_id: 'e1', evidence: [] }, { id: 't2', scope: 'show_window', evidence: [] }], allocation: {},
      coverage_by_placement: { t1: { state: 'incomplete', episodes: ['e1'], missing: ['e1'], failed: [], episode_labels: { e1: 'Ep One' } },
        t2: { state: 'complete', episodes: ['e1', 'e2', 'e3'], missing: [], failed: [] } } } };
    if (p === '/customer/campaigns/2/reports') return { reports: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(html, /Research targets/);
  assert.match(html, />Episode: Ep One</, 'the episode title comes from the coverage labels');
  assert.match(html, /3 episodes in scope\. 1 not fully checked: <a class="" href="#\/episode\/e1">Ep One<\/a>\./, 'coverage counts unique episodes from the draft; N5 names the episode');
  assert.match(html, /Ask &lt;the&gt; agency/, 'notes are escaped');
  assert.match(html, /<button type="button" class="button ew-work-planned-btn" disabled[^>]*>Attach evidence<\/button>/);
  assert.doesNotMatch(html, /data-action="attach[^"]*"[^>]*>Attach evidence/, 'the planned control carries no write action');
  assert.match(html, /Planned/);
  assert.doesNotMatch(html, /Requirement outcome:|Citation only|Possible ad/);
  assert.match(html, /data-action="freeze-report"[^>]*>Save a final report…</);
});

test('G4 supplied campaign: filter counts are display-only and every placement keeps its server reasons', async () => {
  const { html } = await renderRoute('#/campaign/1', p => {
    if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Q4', brand: 'acme', requirement_provenance: {} } };
    if (p === '/customer/campaigns/1/draft') return { draft: { campaign: { revision: 4 }, mode: 'supplied_requirements',
      placements: [{ id: 'p1', scope: 'show_window', date_from: '2026-10-01', date_to: '2026-10-15', expected_count: 1 },
        { id: 'p2', scope: 'show_window', date_from: '2026-11-01', date_to: '2026-11-15', expected_count: 1 }],
      pins: { attachments: { p1: ['sr1:a'], p2: [] } },
      allocation: { evaluations: { p1: { outcome: 'supported', reasons: ['r'], eligible_ids: ['o1'] }, p2: { outcome: 'incomplete_coverage', reasons: ['x'] } } },
      reason_codes: { evaluations: { p2: [{ code: 'missing_transcripts' }] } },
      coverage_by_placement: { p1: { state: 'complete' }, p2: { state: 'incomplete', missing: ['e9'], episode_labels: { e9: 'Ep 9' } } } } };
    if (p === '/customer/campaigns/1/reports') return { reports: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(html, /data-filter="all" aria-pressed="true">All placements · 2</);
  assert.match(html, /data-filter="possible" aria-pressed="false">Ads found · 1</);
  assert.match(html, /data-filter="needs-attention" aria-pressed="false">Needs attention · 1</);
  assert.doesNotMatch(html, /Some episodes in this window have no full-episode check recorded yet\./, 'the coverage sentence already says why; not repeated');
  assert.match(html, /<details class="tech-details">[\s\S]*Outcome <span class="mono">incomplete_coverage<\/span>[\s\S]*<li><span class="small mono">x<\/span>/, 'the raw server reason stays in Technical details');
  assert.match(html, /No full check of “Ep 9” yet, so a missed ad can(?:'|&#39;)t be ruled out\./);
  assert.equal((html.match(/Ad found</g) || []).length, 1, 'the linkage label is said once, by its tag');
  assert.match(html, /1 attached source<\/span><a class="text-link" href="#\/evidence\/sr1%3Aa">Open evidence</);
  assert.match(html, /data-action="freeze-report"[^>]*>Save a final report…</, 'a possible ad makes the campaign freezable');
  assert.doesNotMatch(html, /class="button primary" data-action="add-placement"/, 'only one primary action');
});

test('G4 allocation dialog names both placements and keeps the exact form contract', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  const meta = { placements: [{ id: 'p1', scope: 'show_window' }, { id: 'p2', scope: 'show_window' }], titles: {}, evals: {}, cites: {}, bound: { p1: ['sr1:a'] } };
  await win.EWViews.handleAction({ dataset: { action: 'allocation-request', op: 'reference', cid: '1', pid: 'p1', ref: 'sr1:a', crev: '4', frev: '2',
    units: JSON.stringify({ p1: 2, p2: 1 }), ev: '{}', meta: JSON.stringify(meta) }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /<h2 id="dialog-title">Reference this source<\/h2>/);
  // UAT-093: placements are named by scope and dates; two undated windows clash, so they are numbered.
  assert.match(modal.innerHTML, /Reference it on “Show window 1”/);
  assert.match(modal.innerHTML, /name="allocation_action" value="reference"/);
  assert.match(modal.innerHTML, /name="expected_from_revision" value="2"/);
  assert.match(modal.innerHTML, /type="submit">Reference it</);
  assert.match(modal.innerHTML, /Reference: cited only, never counted\. We check the change when you confirm\./);
});

test('G4 research coverage: unknown or missing coverage is never "every one has a complete search"', async () => {
  const research = (cov) => renderRoute('#/campaign/2', p => {
    if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study' } };
    if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [], allocation: {},
      targets: [{ id: 't1', scope: 'episode', episode_id: 'e1', evidence: [] }, { id: 't2', scope: 'show_window', evidence: [] }],
      coverage_by_placement: cov } };
    if (p === '/customer/campaigns/2/reports') return { reports: [] };
    throw new Error('unexpected request ' + p);
  });
  const EVERY = /Every one has a complete transcript search/;
  // One target complete, one unknown (episodes listed, nothing missing or failed).
  let { html } = await research({ t1: { state: 'complete', episodes: ['e1'], missing: [], failed: [] },
    t2: { state: 'unknown', episodes: ['e2', 'e3'], missing: [], failed: [] } });
  assert.doesNotMatch(html, EVERY, 'an unknown target blocks the "every one" sentence');
  assert.match(html, /3 episodes in scope\./);
  assert.match(html, /Coverage not checked/, 'the unknown target is shown as not checked');
  // A target with no coverage entry counts as not checked, not dropped.
  ({ html } = await research({ t1: { state: 'complete', episodes: ['e1'], missing: [], failed: [] } }));
  assert.doesNotMatch(html, EVERY);
  assert.match(html, /Coverage not checked/);
  // All complete: the sentence is still allowed.
  ({ html } = await research({ t1: { state: 'complete', episodes: ['e1'], missing: [], failed: [] },
    t2: { state: 'complete', episodes: ['e2'], missing: [], failed: [] } }));
  assert.match(html, EVERY);
  assert.doesNotMatch(html, /Coverage not checked/);
  // Row 1 (d1fb): a failed source is "couldn't be loaded", never "read".
  ({ html } = await research({ t1: { state: 'incomplete', episodes: ['e1', 'e2'], missing: [], failed: ['e2'] } }));
  assert.match(html, /1 couldn't be loaded\./);
  assert.doesNotMatch(html, /be read\b/);
});

test('G4 alerts hub: "N new" shows on any row with unread events, once', async () => {
  const alerts = [
    { ...base, id: 1, name: 'Fresh', lifecycle: 'active_local', unread_event_count: 4, unread_ad_count: 2 },
    { ...base, id: 5, name: 'Held', lifecycle: 'paused', unread_event_count: 5, unread_ad_count: 3 },
    { ...base, id: 3, name: 'Broken', lifecycle: 'active_local', last_attempt_result: 'error', unread_event_count: 2, unread_ad_count: 1 },
    { ...base, id: 2, name: 'Calm', lifecycle: 'active_local', unread_event_count: 0, unread_ad_count: 0 },
  ];
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(rowOf(html, 'Held'), /data-tag="outline">3 unread</, 'paused: grey "N unread" (UX review R2-05c)');
  assert.match(rowOf(html, 'Broken'), /ew-work-new[^>]*>1 new</);
  assert.match(rowOf(html, 'Fresh'), /ew-work-new[^>]*>2 new</, 'Active shows the same "N new" tag; its state tag says only "Active"');
  assert.doesNotMatch(rowOf(html, 'Calm'), /ew-work-new/);
});

test('G4 alerts hub filters are pressed buttons, not tabs', async () => {
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: ALERTS };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.doesNotMatch(html, /role="tab"|role="tablist"/);
  assert.match(html, /data-tab="all" aria-pressed="true"[^>]*>All · 6</);
  assert.match(html, /data-tab="new" aria-pressed="false"[^>]*>New matches · 1</);
});

test('G4 placement rows say when the expected count is not recorded', async () => {
  const { html } = await renderRoute('#/campaign/1', p => {
    if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Q4', brand: 'acme', requirement_provenance: {} } };
    if (p === '/customer/campaigns/1/draft') return { draft: { campaign: { revision: 4 }, mode: 'supplied_requirements',
      placements: [{ id: 'p1', scope: 'show_window' }], pins: { attachments: {} }, allocation: { evaluations: {} }, coverage_by_placement: {} } };
    if (p === '/customer/campaigns/1/reports') return { reports: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.doesNotMatch(html, /Expects \?/);
  assert.match(html, /Expected count not recorded/);
});

test('G4 allocation dialog: Retain says nothing moves and the binding and credit stay', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  const meta = { placements: [{ id: 'p1', scope: 'show_window' }], titles: {}, evals: {}, cites: {}, bound: { p1: ['sr1:a'] } };
  await win.EWViews.handleAction({ dataset: { action: 'allocation-request', op: 'retain', cid: '1', pid: 'p1', ref: 'sr1:a', crev: '4', frev: '2',
    units: JSON.stringify({ p1: 2 }), ev: '{}', meta: JSON.stringify(meta) }, disabled: false, closest: () => null });
  // The sentence names the placement (supervisor follow-up to VL-13).
  assert.match(modal.innerHTML, /Keep it on “Show window”\. Nothing moves, and whether it counts stays the same\./);
  assert.doesNotMatch(modal.innerHTML, /counts only there/);
  assert.doesNotMatch(modal.innerHTML, /Reference: cited only/, 'the Reference footnote belongs to Reference only');
  assert.match(modal.innerHTML, /We check the change when you confirm\./);
});

/* DEMO-SOL-R1-04: a hosted research campaign shows its observations and
   Attach evidence works (it opens Search on the campaign's own query,
   where Add to campaign attaches a passage to a target). */
async function renderHosted(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  win.EW.current.hosted = true;
  // A signed-in editor of the active workspace (what a judge is).
  Object.assign(win.EW.current, { principal: 'oidc:https://i.example:judge', workspaceRole: 'editor', activeWorkspace: 'judges-demo' });
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const MUSE_Q = { version: 2, mode: 'mentions', q: '"Meta\'s Muse"' };
const researchApi = p => {
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 5, name: 'Meta’s Muse: podcast coverage', source_query: MUSE_Q } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 5 }, mode: 'research', placements: [],
    targets: [{ id: 't1', scope: 'show_window', show_id: 's1', evidence: ['sr1:a', 'sr1:b'] }], show_titles: { s1: 'The Vergecast' }, allocation: {},
    coverage_by_placement: {} } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};

test('DEMO-SOL-R1-04 hosted research campaign: observations open evidence; Attach evidence is live, not planned', async () => {
  const { html } = await renderHosted('#/campaign/2', researchApi);
  assert.match(html, /<h2 id="obs-h">Observations · 2<\/h2>/);
  assert.match(html, /href="#\/evidence\/sr1%3Aa"[^>]*>Observation 1 · The Vergecast</);
  assert.doesNotMatch(html, /ew-work-planned-btn|Planned|is planned/);
  assert.match(html, /<a class="button" href="#\/search\?[^"]*mode=mentions[^"]*"[^>]*>Attach evidence<\/a>/);
  assert.match(html, /use Add to campaign on a passage/);
});

test('DEMO-SOL-R1-04 the campaign list counts research observations', async () => {
  const { html } = await renderHosted('#/campaigns', p => {
    if (p === '/customer/campaigns') return { campaigns: [
      { id: 2, name: 'Muse', mode: 'research', state: 'Draft', availability: 'active', target_count: 3, observation_count: 6 },
      { id: 3, name: 'Empty', mode: 'research', state: 'Draft', availability: 'active', target_count: 1, observation_count: 0 }] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(html, /3 research targets · 6 observations/);
  assert.match(html, /1 research target · no observations yet/);
});

/* DEMO2-R3-01: research with targets or observations is "Researching",
   not "Draft", in the Campaigns list and on the campaign page; active
   work sorts before empty drafts. */
test('DEMO2-R3-01 Campaigns list: "Researching" is an outline tag and sorts before empty drafts', async () => {
  const { html } = await renderHosted('#/campaigns', p => {
    if (p === '/customer/campaigns') return { campaigns: [
      { id: 1, name: 'Empty research', mode: 'research', state: 'Draft', availability: 'active', target_count: 0, observation_count: 0 },
      { id: 2, name: 'Muse', mode: 'research', state: 'Researching', availability: 'active', target_count: 3, observation_count: 6 }] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(html, /ew-tag-outline"[^>]*>Researching</);
  assert.match(html, /ew-tag-outline"[^>]*>Draft</);
  assert.ok(html.indexOf('>Muse<') < html.indexOf('>Empty research<'), 'Researching sorts before Draft');
});

test('DEMO2-R3-01 campaign page: research with a target reads "Researching"; without one, "Draft"', async () => {
  const { html } = await renderHosted('#/campaign/2', researchApi);
  assert.match(html, /Researching · 1 research target/);
  assert.doesNotMatch(html, /Draft · 1 research target/);
  const { html: bare } = await renderHosted('#/campaign/2', p => {
    if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 5 }, mode: 'research', placements: [],
      targets: [], show_titles: {}, allocation: {}, coverage_by_placement: {} } };
    return researchApi(p);
  });
  assert.match(bare, /Draft · 0 research targets/);
});

test('DEMO2-R4-01 campaign page: an archived research campaign with a target reads "Archived", never "Researching"', async () => {
  const { html } = await renderHosted('#/campaign/2', p => {
    if (p === '/customer/campaigns/2') {
      const c = researchApi(p);
      return { campaign: Object.assign({}, c.campaign, { status: 'archived' }) };
    }
    return researchApi(p);
  });
  assert.match(html, /Archived\. Restore it to make changes\./);
  assert.doesNotMatch(html, /Researching · /);
  assert.match(html, /Archived · 1 research target/);
});
