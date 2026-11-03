/* UAT 2026-10-15 walkthrough re-run, UAT-100: an archived campaign is
   read-only. Its campaign, placement and target pages show no mutating
   control, only Restore and read/download, driven by allowed_actions;
   the campaign page says "Archived. Restore it to make changes." The
   Add to campaign dialog does not offer an archived campaign. An active
   campaign is unchanged.
   Run: node --test tests/frontend_uat_arch.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9178/app/' + route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
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
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };

async function views(route = '#/overview') {
  const win = loadAt(route);
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.setTimeout = () => {};
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  return { win, modal, main, toasts };
}
const withApi = (t, api) => {
  t.calls = [];
  t.win.EW.api = async (p, opts = {}) => {
    t.calls.push([opts.method || 'GET', p, opts.body]);
    const out = await api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  return t;
};
async function at(t, route) {
  t.win.location.hash = route; t.win.EW.current.route = route;
  await t.win.EWViews.render();
  return t.main.innerHTML;
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ')
  .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
function formFrom(win, html, id, extra = {}) {
  const hidden = {};
  for (const m of html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)) hidden[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  const entries = { ...hidden, ...extra };
  win.FormData = class { constructor() { this.e = Object.entries(entries); } *entries() { yield* this.e; } get(k) { return entries[k]; } };
  return { getAttribute: k => (k === 'id' ? id : null), dataset: {}, querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, elements: [] };
}
const dataOf = (html, action) => {
  const tag = new RegExp(`<button[^>]*data-action="${action}"[^>]*>`).exec(html)[0];
  const d = {};
  for (const m of tag.matchAll(/data-([a-z-]+)=(?:"([^"]*)"|'([^']*)')/g)) d[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = (m[2] ?? m[3]).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  return d;
};

/* -- fixtures ---------------------------------------------------------------- */
const ACTIVE = ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'];
const ARCHIVED = ['read', 'stored_reports', 'restore'];
const MAR = 'exp:mar', APR = 'exp:apr', SRC = 'sr1:aaaa', POOL = 'sr1:bbbb', TID = 'ct-1';
const LINE = 'Archived. Restore it to make changes.';
const win = (id, from, to) => ({ id, scope: 'show_window', show_id: 'cs-1', date_from: from, date_to: to,
  expected_count: 1, revision: 1, binding: 'eligible', provenance: {} });
const supplied = (allowed, status) => ({
  campaign: { id: 9, revision: 7, name: 'UAT supplied', status, brand: 'kachava', brand_display: 'Kachava' }, mode: 'supplied_requirements',
  availability: 'active', allowed_actions: allowed, synthetic: false, synthetic_placements: [],
  placements: [win(MAR, '2026-03-01', '2026-03-31'), win(APR, '2026-04-01', '2026-04-30')],
  targets: [], episode_titles: {}, show_titles: { 'cs-1': 'Casefile' },
  coverage_by_placement: { [MAR]: { state: 'complete', episodes: ['e1'], missing: [], failed: [] } }, comparisons: {},
  allocation: { evaluations: { [MAR]: { outcome: 'met', eligible_ids: [] } }, citations: {} }, reason_codes: { evaluations: {}, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: {}, limitations: [] },
  attachment_pool: [POOL], pool_refs: { [POOL]: POOL }, suggestions: [],
  pins: { attachments: { [MAR]: [SRC] }, attachment_state: { [SRC]: {} } } });
const research = (allowed, status) => ({ ...supplied(allowed, status), mode: 'research', placements: [], coverage_by_placement: {},
  allocation: { evaluations: {}, citations: {} }, pins: { attachments: {}, attachment_state: {} },
  targets: [{ id: TID, scope: 'show_window', show_id: 'cs-1', date_from: '2026-03-01', date_to: '2026-03-31', revision: 2, note: 'n', evidence: [] }] });
const REPORT = { report_no: 1, captured_at: '2026-10-15T10:00:00Z', stale: false, available: true };
const campApi = d => p => {
  const status = d.campaign.status;
  if (p === '/customer/campaigns/9') return { campaign: { id: 9, mode: d.mode, revision: 7, name: 'UAT supplied', status,
    brand: 'kachava', brand_display: 'Kachava', requirement_provenance: {}, availability: 'active', allowed_actions: d.allowed_actions } };
  if (p === '/customer/campaigns/9/draft') return { draft: d };
  if (p === '/customer/campaigns/9/reports') return { reports: [REPORT] };
  if (p === '/customer/campaigns/9/targets') return { targets: d.targets };
  if (p === '/customer/shows/cs-1') return { display_title: 'Casefile', episodes: [] };
  if (p.startsWith('/customer/search?')) return { items: [] };
  throw new Error('unexpected ' + p);
};
const actions = html => [...String(html).matchAll(/data-action="([^"]+)"/g)].map(m => m[1]);
const MUTATING = ['add-placement', 'edit-campaign', 'freeze-report', 'add-target', 'edit-placement', 'allocation-request',
  'detach-evidence', 'attach-pool-evidence', 'edit-target-note'];
const noMutating = (html, where) => {
  for (const a of MUTATING) assert.ok(!actions(html).includes(a), `${where}: no ${a}`);
  assert.doesNotMatch(html, /id="attach-evidence"/, `${where}: no attach-by-reference form`);
  assert.doesNotMatch(html, /<button[^>]*disabled[^>]*>(?:Freeze a report|Save a final report)/, `${where}: no disabled Freeze either`);
};

/* -- archived ------------------------------------------------------------------- */
test('an archived supplied campaign shows only Restore and its reports, with the archived line', async () => {
  const t = withApi(await views(), campApi(supplied(ARCHIVED, 'archived')));
  const html = await at(t, '#/campaign/9');
  noMutating(html, 'campaign page');
  assert.match(html, /data-action="archive-campaign"[^>]*data-status="active"[^>]*>Restore</);
  assert.ok(visible(html).includes(LINE));
  assert.match(visible(html), /Archived/);
  assert.match(html, /href="#\/campaign\/9\/report\/1"/, 'the stored report stays readable');
  assert.match(html, /href="#\/campaign\/9\/placement\//, 'placements stay open to read');
});

test('an archived campaign placement page has no Edit, Attach, Move, Detach, Retain or Reference', async () => {
  const t = withApi(await views(), campApi(supplied(ARCHIVED, 'archived')));
  const html = await at(t, '#/campaign/9/placement/' + encodeURIComponent(MAR));
  noMutating(html, 'placement page');
  assert.doesNotMatch(visible(html), /\b(Retain|Reference|Move…|Detach|Attach source)\b/);
  assert.match(html, /href="#\/evidence\/sr1%3Aaaaa"/, 'attached sources stay open to read');
  assert.ok(visible(html).includes(LINE));
});

test('an archived research campaign offers no Add research target; its target page no Edit name/note', async () => {
  const d = research(ARCHIVED, 'archived');
  const t = withApi(await views(), campApi(d));
  let html = await at(t, '#/campaign/9');
  noMutating(html, 'research page');
  assert.match(html, />Restore</);
  assert.ok(visible(html).includes(LINE));
  html = await at(t, '#/campaign/9/target/' + TID);
  noMutating(html, 'target page');
  assert.ok(visible(html).includes(LINE));
});

test('Add to campaign does not offer an archived campaign', async () => {
  const REF = 'sr1:dc39c846855a4ef6a58fc873d477389e';
  const live = { ...supplied(ACTIVE, 'active'), campaign: { id: 8, revision: 3, name: 'Live one', status: 'active' } };
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns') return { campaigns: [
      { id: 9, name: 'UAT supplied', mode: 'supplied_requirements', availability: 'active', status: 'archived', revision: 7 },
      { id: 8, name: 'Live one', mode: 'supplied_requirements', availability: 'active', status: 'active', revision: 3 }] };
    if (p === '/customer/campaigns/8/draft') return { draft: live };
    if (p === '/customer/campaigns/9/draft') return { draft: supplied(ARCHIVED, 'archived') };
    if (p.startsWith('/customer/campaigns/8/evidence-fit')) return { placements: {} };
    throw new Error('unexpected ' + p);
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: REF, episode: 's001e9', show: 'cs-1', day: '2026-03-21' } }); await settle();
  assert.match(t.modal.innerHTML, /<b>Live one<\/b>/);
  assert.doesNotMatch(t.modal.innerHTML, /UAT supplied/);
  assert.ok(!t.calls.some(c => c[1] === '/customer/campaigns/9/draft'), 'the archived campaign is not even read');
});

test('the Archive confirmation says the campaign is read-only until restored', async () => {
  const t = withApi(await views(), campApi(supplied(ACTIVE, 'active')));
  const html = await at(t, '#/campaign/9');
  const tag = /<button[^>]*data-action="archive-campaign"[^>]*>/.exec(html)[0];
  const ds = {};
  for (const m of tag.matchAll(/data-([a-z-]+)="([^"]*)"/g)) ds[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&amp;/g, '&');
  await t.win.EWViews.handleAction({ dataset: ds }); await settle();
  assert.match(visible(t.modal.innerHTML), /stay as they are, and you can restore it later\. Until then it is read-only\./);
});

/* -- active: unchanged ------------------------------------------------------------- */
test('an active campaign keeps its controls and has no archived line', async () => {
  const t = withApi(await views(), campApi(supplied(ACTIVE, 'active')));
  let html = await at(t, '#/campaign/9');
  for (const a of ['add-placement', 'edit-campaign', 'archive-campaign']) assert.ok(actions(html).includes(a), a);
  assert.match(html, /data-action="archive-campaign"[^>]*>Archive</);
  assert.ok(!visible(html).includes(LINE));
  html = await at(t, '#/campaign/9/placement/' + encodeURIComponent(MAR));
  for (const a of ['edit-placement', 'allocation-request', 'detach-evidence', 'attach-pool-evidence']) assert.ok(actions(html).includes(a), a);
  assert.match(html, /id="attach-evidence"/);
  assert.ok(!visible(html).includes(LINE));
  const r = research(ACTIVE, 'active');
  const t2 = withApi(await views(), campApi(r));
  html = await at(t2, '#/campaign/9');
  assert.ok(actions(html).includes('add-target'));
  html = await at(t2, '#/campaign/9/target/' + TID);
  assert.ok(actions(html).includes('edit-target-note'));
});
