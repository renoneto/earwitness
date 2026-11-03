/* UAT 2026-10-15 campaign UI fix round (frontend half).
   VL-06: a show window is named by its show (draft.show_titles), with one
   label everywhere (page, placement head, Move/Detach, Add to campaign);
   clashes are numbered as before. VL-28: "Episode: <title>".
   Placements read in window order (start date, then name).
   VL-04/05/07/13/29, B-8, B-9: copy. VL-09: brief title in Edit campaign;
   attach errors in plain words. VL-10: read-only campaigns are not
   "tracked" on Overview. Archive / Restore on the campaign page.
   Run: node --test tests/frontend_uat_cui.test.cjs */
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
const MAR = 'exp:mar', APR = 'exp:apr', MAY = 'exp:may', EP = 'exp:ep', SRC = 'sr1:aaaa';
const win = (id, from, to, show = 'cs-1', extra = {}) => ({ id, scope: 'show_window', show_id: show, date_from: from, date_to: to,
  expected_count: 1, revision: 1, binding: 'eligible', provenance: {}, ...extra });
const supplied = (over = {}) => ({
  campaign: { id: 9, revision: 7, name: 'UAT supplied', brand: 'kachava', brand_display: 'Kachava' }, mode: 'supplied_requirements',
  synthetic: false, synthetic_placements: [],
  // Hash-id order on purpose: May, Mar, Apr.
  placements: [win(MAY, '2026-05-01', '2026-05-31'), win(MAR, '2026-03-01', '2026-03-31'), win(APR, '2026-04-01', '2026-04-30', 'cs-2')],
  targets: [], episode_titles: { s001e0: 'When an Ex Police Chief Becomes a Hitman' },
  show_titles: { 'cs-1': 'Casefile', 'cs-2': 'Call Her Daddy' },
  coverage_by_placement: {}, comparisons: {},
  allocation: { evaluations: {}, citations: {} }, reason_codes: { evaluations: {}, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: {}, limitations: [] },
  pins: { attachments: { [MAR]: [SRC] }, attachment_state: { [SRC]: {} } },
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'], ...over });
const detail = (d, over = {}) => ({ campaign: { id: 9, mode: 'supplied_requirements', revision: 7, name: 'UAT supplied', status: 'active',
  brand: 'kachava', brand_display: 'Kachava', requirement_provenance: {}, allowed_actions: d.allowed_actions, ...over } });
const labelItem = { ref: SRC, show_title: 'Casefile', ep_title: 'Hitman', pub_date: '2026-03-21', quotation_span: [698, 764],
  read: { brand: { display: 'Kachava', confirmed: true }, display_span: [698, 764] } };
const campApi = (d = supplied(), over = {}, extra = {}) => p => {
  if (p === '/customer/campaigns/9') return detail(d, over);
  if (p === '/customer/campaigns/9/draft') return { draft: d };
  if (p === '/customer/campaigns/9/reports') return { reports: [] };
  if (p.startsWith('/customer/search?')) return { items: [labelItem] };
  if (Object.hasOwn(extra, p)) return typeof extra[p] === 'function' ? extra[p]() : extra[p];
  throw new Error('unexpected ' + p);
};
const heads = html => [...html.matchAll(/<h3 class="placement-head"><a [^>]*>([^<]*)<\/a>/g)].map(m => m[1]);

/* -- VL-06 / VL-28 / order ---------------------------------------------------- */
test('VL-06 the campaign page names each window by its show, in window order', async () => {
  const t = withApi(await views(), campApi());
  const html = await at(t, '#/campaign/9');
  assert.deepEqual(heads(html), ['Casefile · Mar 1–Mar 31, 2026', 'Call Her Daddy · Apr 1–Apr 30, 2026', 'Casefile · May 1–May 31, 2026']);
  assert.doesNotMatch(visible(html), /Show window ·/);
});

test('VL-06 without a show title the window keeps "Show window"; an episode reads "Episode: <title>"', async () => {
  const d = supplied({ show_titles: {}, placements: [win(MAR, '2026-03-01', '2026-03-31'),
    { id: EP, scope: 'episode', show_id: 'cs-1', episode_id: 's001e0', date_from: '2026-02-01', date_to: '2026-02-28', expected_count: 1, revision: 1, binding: 'eligible', provenance: {} }] });
  const t = withApi(await views(), campApi(d));
  const html = await at(t, '#/campaign/9');
  assert.deepEqual(heads(html), ['Episode: When an Ex Police Chief Becomes a Hitman · Feb 1–Feb 28, 2026', 'Show window · Mar 1–Mar 31, 2026']);
});

test('VL-06 undated windows come after dated ones; same dates order by name', async () => {
  const d = supplied({ placements: [win('exp:z', null, null), win('exp:b', '2026-03-01', '2026-03-31'), win('exp:a', '2026-03-01', '2026-03-31', 'cs-2')] });
  const t = withApi(await views(), campApi(d));
  const html = await at(t, '#/campaign/9');
  assert.deepEqual(heads(html), ['Call Her Daddy · Mar 1–Mar 31, 2026', 'Casefile · Mar 1–Mar 31, 2026', 'Casefile']);
});

test('VL-06 the placement page, Move and Detach use the same show label; clashes are numbered', async () => {
  const d = supplied({ placements: [win(MAR, '2026-03-01', '2026-03-31'), win(APR, '2026-03-01', '2026-03-31'), win(MAY, '2026-05-01', '2026-05-31', 'cs-2')] });
  const t = withApi(await views(), campApi(d));
  const html = await at(t, '#/campaign/9/placement/' + encodeURIComponent(MAR));
  assert.match(html, /<h1>Casefile · Mar 1–Mar 31, 2026 \(1\)<\/h1>/);
  await t.win.EWViews.handleAction({ dataset: dataOf(html, 'allocation-request" data-op="move'), disabled: false, closest: () => null }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /Move it from “Casefile · Mar 1–Mar 31, 2026 \(1\)” to the placement you choose\./);
  const options = [...m.matchAll(/<option value="([^"]+)"[^>]*>([^<]*)<\/option>/g)].map(x => [x[1], x[2]]);
  assert.deepEqual(options, [[APR, 'Casefile · Mar 1–Mar 31, 2026 (2)'], [MAY, 'Call Her Daddy · May 1–May 31, 2026']]);
  await t.win.EWViews.handleAction({ dataset: dataOf(html, 'detach-evidence') }); await settle();
  assert.match(visible(t.modal.innerHTML), /Detach it from “Casefile · Mar 1–Mar 31, 2026 \(1\)”/);
});

test('VL-06 Add to campaign names the show and lists placements in window order', async () => {
  const REF = 'sr1:dc39c846855a4ef6a58fc873d477389e';
  const d = supplied({ placements: [win(MAY, '2026-03-15', '2026-03-31'), win(MAR, '2026-03-01', '2026-03-31')] });
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns') return { campaigns: [{ id: 9, name: 'UAT supplied', mode: 'supplied_requirements', availability: 'active', revision: 7 }] };
    if (p === '/customer/campaigns/9/draft') return { draft: d };
    if (p.startsWith('/customer/campaigns/9/evidence-fit')) return { placements: {} };
    throw new Error('unexpected ' + p);
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: REF, episode: 's001e9', show: 'cs-1', day: '2026-03-21' } }); await settle();
  const labels = [...t.modal.innerHTML.matchAll(/<b>UAT supplied<\/b> · ([^<]*)<\/span>/g)].map(x => x[1]);
  assert.deepEqual(labels, ['Casefile · Mar 1–Mar 31, 2026', 'Casefile · Mar 15–Mar 31, 2026']);
});

test('VL-28 coverage names episodes in quotes joined with "and"; no check at all says so (B-9)', async () => {
  const labels = { e1: 'When an Ex Police Chief Becomes a Hitman', e2: 'The Unthinkable Discovery' };
  const d = supplied({ placements: [win(MAR, '2026-03-01', '2026-03-31'), win(APR, '2026-04-01', '2026-04-30')],
    coverage_by_placement: {
      [MAR]: { state: 'incomplete', missing: ['e1', 'e2'], failed: [], episodes: ['e1', 'e2'], exclusions: [], episode_labels: labels },
      [APR]: { state: 'incomplete', missing: ['e2'], failed: [], episodes: ['e1', 'e2'], exclusions: [], episode_labels: labels } } });
  const t = withApi(await views(), campApi(d));
  const text = visible(await at(t, '#/campaign/9'));
  assert.match(text, /No full-episode check is recorded yet for “When an Ex Police Chief Becomes a Hitman” and “The Unthinkable Discovery”\./);
  assert.match(text, /No full check of “The Unthinkable Discovery” yet, so a missed ad can't be ruled out\./);
  assert.doesNotMatch(text, /not available|no transcript yet|Hitman, The/);
});

test('VL-07 three or more titles read as a list ending in "and"; the missing-transcripts reason says screening', async () => {
  const labels = { e1: 'A', e2: 'B', e3: 'C' };
  const ev = { outcome: 'incomplete_coverage', reasons: ['missing transcripts', 'failed fetches'], eligible_ids: [], allocated_elsewhere: [] };
  const codes = [{ code: 'missing_transcripts', field: null }, { code: 'failed_fetches', field: null }];
  const d = supplied({ placements: [win(MAR, '2026-03-01', '2026-03-31'), win(APR, '2026-04-01', '2026-04-30')],
    coverage_by_placement: {
      [MAR]: { state: 'incomplete', missing: ['e1', 'e2', 'e3'], failed: [], episodes: ['e1', 'e2', 'e3'], exclusions: ['x'], episode_labels: labels },
      [APR]: { state: 'incomplete', missing: [], failed: ['e1'], episodes: ['e1'], exclusions: [], episode_labels: labels } },
    allocation: { evaluations: { [MAR]: ev, [APR]: ev }, citations: {} },
    reason_codes: { evaluations: { [MAR]: codes, [APR]: codes }, citations: {} } });
  const t = withApi(await views(), campApi(d));
  const text = visible(await at(t, '#/campaign/9'));
  assert.match(text, /No full check of “A”, “B” and “C” yet, so a missed ad can't be ruled out\./);
  assert.match(text, /Some episodes in this window have no full-episode check recorded yet\./);
  assert.doesNotMatch(text, /no transcript yet|not available/);
});

test('VL-07 research coverage counts episodes without a whole-episode screening record', async () => {
  const d = supplied({ mode: 'research', placements: [], targets: [{ id: 't1', scope: 'show_window', show_id: 'cs-1', date_from: '2026-03-01', date_to: '2026-03-31', evidence: [] }],
    coverage_by_placement: { t1: { state: 'incomplete', missing: ['e1'], failed: [], episodes: ['e1', 'e2'], episode_labels: {} } } });
  const t = withApi(await views(), campApi(d, { mode: 'research', targets: d.targets }));
  const html = await at(t, '#/campaign/9');
  assert.match(visible(html), /2 episodes in scope\. 1 not fully checked\./, 'N5 with no title known: the count alone');
  assert.match(html, /Casefile · Mar 1–Mar 31, 2026/, 'a research target is named by its show too');
});

/* -- VL-04 / VL-05 / VL-13 copy -------------------------------------------------- */
test('VL-04 a match here that is not matched yet never says "source"', async () => {
  const d = supplied({ placements: [win(MAR, '2026-03-01', '2026-03-31')],
    allocation: { evaluations: { [MAR]: { outcome: 'pending', reasons: ['x: unresolved correspondence', 'x: ambiguous'], eligible_ids: [], allocated_elsewhere: [], occurrence_order: ['x'] } }, citations: {} },
    reason_codes: { evaluations: { [MAR]: [{ code: 'unresolved_correspondence', field: null }, { code: 'correspondence_ambiguous', field: null }] }, citations: {} } });
  const t = withApi(await views(), campApi(d));
  const html = await at(t, '#/campaign/9');
  const line = /<li>(The match here isn't checked yet:[^<]*)<\/li>/.exec(html.replace(/&#39;/g, "'"));
  assert.ok(line, 'the match-check line is shown');
  assert.equal(line[1], "The match here isn't checked yet: not yet matched to one distinct ad; may cover more than one ad.");
  d.allocation.evaluations[MAR].occurrence_order = ['x', 'y'];
  const many = visible(await at(t, '#/campaign/9'));
  assert.match(many, /The matches here still need: matching to one distinct ad; telling the ads apart\./);
});

test('VL-05 / VL-13 the placement dialog promises nothing automatic and says what Binding means', async () => {
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns/9/draft') return { draft: supplied() };
    if (p === '/customer/shows') return { shows: [] };
    if (p.startsWith('/customer/briefs')) return { items: [] };
    throw new Error('unexpected ' + p);
  });
  t.win.EW.current.route = '#/campaign/9';
  await t.win.EWViews.handleAction({ dataset: { action: 'add-placement' } }); await settle();
  const m = t.modal.innerHTML;
  assert.doesNotMatch(m, /automatically/);
  assert.match(m, /After saving, open the placement and use Search this window, then Add to campaign on the evidence to attach it\./);
  assert.match(m, /<label for="placement-binding">Counts toward the expected total\?<\/label>/);
  assert.match(m, /<option value="eligible" >Counts<\/option><option value="citation_only" >Citation only<\/option>/);
});

test('VL-13 the placement page and allocation dialogs use plain words', async () => {
  const t = withApi(await views(), campApi(supplied({ attachment_pool: [] })));
  const html = await at(t, '#/campaign/9/placement/' + encodeURIComponent(MAR));
  assert.match(html, /This only describes stored records; it never proves delivery or fulfillment\./);
  assert.match(html, /<h3>Suggested evidence<\/h3>/);
  assert.doesNotMatch(html, /Attachment pool|A linkage describes/);
  await t.win.EWViews.handleAction({ dataset: dataOf(html, 'allocation-request" data-op="retain'), disabled: false, closest: () => null }); await settle();
  // The sentence names the placement (supervisor follow-up to VL-13).
  assert.match(t.modal.innerHTML, /Keep it on “Casefile · Mar 1–Mar 31, 2026”\. Nothing moves, and whether it counts stays the same\./);
  assert.match(t.modal.innerHTML, /This source is already on this placement\./);
  assert.doesNotMatch(t.modal.innerHTML, /quota|by the server on confirm|already bound here/);
  await t.win.EWViews.handleAction({ dataset: dataOf(html, 'allocation-request" data-op="reference'), disabled: false, closest: () => null }); await settle();
  assert.match(t.modal.innerHTML, /Reference: cited only, never counted\. We check the change when you confirm\./);
});

test('VL-13 a frozen report says it matches what was frozen, or that the frozen copy is unchanged', async () => {
  const frozen = { placements: [], evaluations: {}, coverage_by_placement: {}, campaign: { name: 'UAT supplied' }, captured_at: '2026-10-16T05:10:00Z' };
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns/9/reports/1') return { frozen, stale: false };
    if (p === '/customer/campaigns/9/reports/2') return { frozen, stale: true, stale_reasons: ['placements'] };
    throw new Error('unexpected ' + p);
  });
  assert.match(await at(t, '#/campaign/9/report/1'), /<p class="section-note">Matches what was frozen\.<\/p>/);
  const stale = await at(t, '#/campaign/9/report/2');
  assert.match(stale, /The frozen copy below is unchanged\./);
  assert.doesNotMatch(stale, /frozen bytes|frozen pins/);
});

/* -- B-8 ----------------------------------------------------------------------- */
const citeDraft = n => supplied({ placements: [win(MAR, '2026-03-01', '2026-03-31', 'cs-1', { binding: 'citation_only' }), win(APR, '2026-04-01', '2026-04-30', 'cs-1', { binding: 'citation_only' })].slice(0, n),
  allocation: { evaluations: {}, citations: Object.fromEntries([MAR, APR].slice(0, n).map(id => [id, { binding: 'citation_only', reasons: ['citation_only placements never allocate'] }])) } });
const verdict = html => /<p class="ew-work-verdict">([^<]*)<\/p>/.exec(html)[1];

test('B-8 with nothing counted the verdict says what the placements are', async () => {
  const one = withApi(await views(), campApi(citeDraft(1)));
  assert.equal(verdict(await at(one, '#/campaign/9')), 'No placement is counted for credit. This placement is a citation only, never counted.');
  const two = withApi(await views(), campApi(citeDraft(2)));
  assert.equal(verdict(await at(two, '#/campaign/9')), 'No placement is counted for credit. All 2 placements are citations only, never counted.');
});

/* -- VL-29 ------------------------------------------------------------------------ */
test('VL-29 a move that stays a reference says why it does not count', async () => {
  const d = supplied();
  const t = withApi(await views(), campApi(d, {}, { '/customer/campaigns/9/allocation': { action: 'move', credit_eligible: false } }));
  const html = await at(t, '#/campaign/9/placement/' + encodeURIComponent(MAR));
  await t.win.EWViews.handleAction({ dataset: dataOf(html, 'allocation-request" data-op="move'), disabled: false, closest: () => null }); await settle();
  await t.win.EWViews.handleSubmit(formFrom(t.win, t.modal.innerHTML, 'allocation-confirm', { to_target_id: APR })); await settle();
  assert.ok(t.toasts.includes("Moved. It's still a reference, so it doesn't count toward the expected total."), t.toasts.join(' | '));
});

/* -- VL-09 ------------------------------------------------------------------------ */
test('VL-09 Edit campaign names the brief by title; its id sits in Technical details', async () => {
  const BID = 'brf-0123456789';
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns/9') return { campaign: { id: 9, revision: 7, mode: 'supplied_requirements', name: 'UAT supplied', requirement_provenance: { brief_id: BID, version: 2, digest: 'd' } } };
    if (p.startsWith('/customer/briefs?')) return { items: [] };
    if (p === `/customer/briefs/${BID}/versions/2`) return { brief_version: { title: 'Kachava fall brief', digest: 'd', provided_text: 'x' } };
    throw new Error('unexpected ' + p);
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: '9' } }); await settle();
  const m = t.modal.innerHTML;
  assert.match(visible(m), /Registered brief: “Kachava fall brief” version 2\. Brief versions are immutable; selecting another version is a normal edit\./);
  assert.doesNotMatch(visible(m), new RegExp(`Registered brief: ${BID}`));
  assert.match(m, new RegExp(`<details class="tech-details">[\\s\\S]*?${BID}`));
});

test('VL-09 attach errors read plainly; an unknown one keeps its raw text in Technical details', async () => {
  const cases = [
    ['attachment reference is malformed or unqualified', "That reference isn't evidence we hold. Copy it from the evidence's Technical details.", false],
    ['evidence is outside the captured placement scope', "That evidence is outside this placement's show or dates.", false],
    ['something new went wrong', "The source couldn't be attached.", true],
  ];
  for (const [raw, plain, tech] of cases) {
    const t = withApi(await views(), () => Object.assign(new Error(raw), { status: 400 }));
    const box = { innerHTML: '', setAttribute() {}, focus() {}, dataset: {} };
    const form = formFrom(t.win, '<input type="hidden" name="campaign_id" value="9"><input type="hidden" name="placement_id" value="exp:mar"><input type="hidden" name="expected_revision" value="7">', 'attach-evidence', { ref: 'sr1:x' });
    form.querySelector = sel => (sel === '#form-errors' ? box : null);
    await t.win.EWViews.handleSubmit(form); await settle();
    assert.match(visible(box.innerHTML), new RegExp(plain.replace(/[.?']/g, m => (m === "'" ? "'" : '\\' + m))));
    assert.equal(/tech-details/.test(box.innerHTML), tech, raw);
    if (tech) assert.match(box.innerHTML, /something new went wrong/);
    else assert.doesNotMatch(box.innerHTML, new RegExp(raw));
  }
});

/* -- VL-10 ------------------------------------------------------------------------ */
test('VL-10 read-only campaigns are not counted as tracked on Overview', async () => {
  const t = await views();
  const ov = { coverage: { episodes: 1, episodes_with_transcript: 1, shows: 1 }, recent_events: [], offer_sequence: [], alert_totals: [],
    tracked_campaigns: [{ id: 1, name: 'Old', state: 'Read-only', brand: 'acme' }, { id: 2, name: 'Old 2', state: 'Read-only' },
      { id: 9, name: 'UAT supplied', state: 'Tracking', brand: 'kachava', brand_display: 'Kachava' }],
    campaign_gaps: [], activity: [] };
  withApi(t, p => {
    if (p === '/customer/overview') return ov;
    if (p.startsWith('/customer/search')) return { items: [] };
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/alerts') return { alerts: [] };
    return {};
  });
  const html = await at(t, '#/overview');
  assert.match(html, /Tracked campaigns · 1</);
  assert.match(visible(html), /UAT supplied/);
  assert.doesNotMatch(visible(html), /\bOld\b/);
  assert.match(visible(html), /2 read-only campaigns from before campaign types are on the Campaigns page\./);
});

/* -- Archive ------------------------------------------------------------------------ */
test('Archive asks first with the danger style, then saves status with the displayed revision', async () => {
  const d = supplied();
  const t = withApi(await views(), campApi(d, {}, {}));
  const html = await at(t, '#/campaign/9');
  const a = dataOf(html, 'archive-campaign');
  assert.equal(a.status, 'archived');
  assert.match(html, /data-action="archive-campaign"[^>]*>Archive</);
  withApi(t, (p, opts) => (opts.method === 'PUT' ? { campaign: { id: 9, status: 'archived' }, campaign_revision: 8 } : campApi(d, { status: 'archived', revision: 8 })(p)));
  await t.win.EWViews.handleAction({ dataset: a }); await settle();
  assert.equal(t.calls.length, 0, 'the click alone sends nothing');
  const m = t.modal.innerHTML;
  assert.match(m, /<h2 id="dialog-title">Archive “UAT supplied”\?<\/h2>/);
  assert.match(visible(m), /The campaign is marked Archived\. Its placements, sources and frozen reports stay as they are, and you can restore it later\./);
  // CR-SCR-R1-03: the confirm opens on Cancel, not on the red Archive.
  assert.match(m, /<button class="button" type="button" data-action="close-dialog" autofocus>Cancel<\/button><button class="button danger armed" type="submit">Archive<\/button>/);
  await t.win.EWViews.handleSubmit(formFrom(t.win, m, 'confirm')); await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls.filter(c => c[0] === 'PUT'))),
    [['PUT', '/customer/campaigns/9', { campaign: { status: 'archived' }, expected_revision: 7 }]]);
  assert.ok(t.toasts.includes('Campaign archived.'));
});

test('Restore is offered on an archived campaign and is not styled as danger', async () => {
  // UAT-100: the server's actions for an archived campaign.
  const d = supplied({ allowed_actions: ['read', 'stored_reports', 'restore'] });
  const t = withApi(await views(), campApi(d, { status: 'archived' }));
  const html = await at(t, '#/campaign/9');
  assert.match(html, /Archived/);
  const a = dataOf(html, 'archive-campaign');
  assert.equal(a.status, 'active');
  assert.match(html, /data-action="archive-campaign"[^>]*>Restore</);
  withApi(t, () => ({ campaign: { id: 9, status: 'active' } }));
  await t.win.EWViews.handleAction({ dataset: a }); await settle();
  assert.match(t.modal.innerHTML, /<button class="button primary" type="submit" autofocus>Restore<\/button>/);
  await t.win.EWViews.handleSubmit(formFrom(t.win, t.modal.innerHTML, 'confirm')); await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls.filter(c => c[0] === 'PUT'))),
    [['PUT', '/customer/campaigns/9', { campaign: { status: 'active' }, expected_revision: 7 }]]);
});

test('Archive is not offered on a read-only campaign', async () => {
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns/1') return { campaign: { id: 1, name: 'Old', mode: null, availability: 'legacy_review_required', reasons: [] } };
    if (p === '/customer/campaigns/1/reports') return { reports: [] };
    throw new Error('unexpected ' + p);
  });
  const html = await at(t, '#/campaign/1');
  assert.doesNotMatch(html, /archive-campaign/);
});

test('VL-06 the placement windows chart names each row by its show, in window order, above its bar', async () => {
  const t = withApi(await views(), campApi());
  const html = await at(t, '#/campaign/9');
  const chart = /<section class="chart campaign-timeline">([\s\S]*?)<\/section>/.exec(html)[1];
  const rows = [...chart.matchAll(/<g><text x="0" y="([\d.]+)"[^>]*>([^<]*)<\/text><rect x="([\d.]+)" y="([\d.]+)"/g)];
  assert.deepEqual(rows.map(r => r[2]), ['Casefile · Mar 1–Mar 31, 2026', 'Call Her Daddy · Apr 1–Apr 30, 2026', 'Casefile · May 1–May 31, 2026']);
  for (const r of rows) assert.ok(Number(r[4]) > Number(r[1]), 'the bar sits below its name');
  assert.equal(rows[0][3], '0', 'the earliest window starts at the left edge');
});

/* -- AA-01 (release review R1-01) ----------------------------------------------- */
// Opening a report needs export permission; its history stays readable.
// Judge export Sol r1 EXP-SOL-R1-03 and code r2 EXP-R2-01: the plain
// transcript reason only when the server names it; otherwise the old words.
const HISTORY_ONLY = "You can see this report's history but not its contents.";
const TRANSCRIPT_ONLY = "This frozen report cannot be opened because it includes transcript text. This workspace allows ad excerpts to be exported, but not transcript text. You can still see when the report was frozen in the campaign history.";
const refusal = details => Object.assign(new Error('source disclosure denied'),
  { status: 403, body: { error: { code: 'FORBIDDEN', message: 'source disclosure denied', status: 403, details } } });

test('AA-01 a refused report says history only, with no Retry', async () => {
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns/9/reports/1') return refusal({});
    if (p === '/customer/campaigns/9/reports/2') return Object.assign(new Error('server error'), { status: 500 });
    if (p === '/customer/campaigns/9/reports/3') return refusal({ reason: 'transcript_export' });
    if (p === '/customer/campaigns/9/reports/4') return Object.assign(new Error('source disclosure denied'), { status: 403 });
    throw new Error('unexpected ' + p);
  });
  for (const [no, words] of [[1, HISTORY_ONLY], [4, HISTORY_ONLY], [3, TRANSCRIPT_ONLY]]) {
    const refused = await at(t, '#/campaign/9/report/' + no);
    assert.ok(visible(refused).includes(words), refused);
    assert.ok(!visible(refused).includes(words === HISTORY_ONLY ? TRANSCRIPT_ONLY : HISTORY_ONLY), refused);
    assert.doesNotMatch(refused, /data-action="retry"/);
    assert.match(refused, /<a [^>]*href="#\/campaign\/9"[^>]*>Back to campaign<\/a>/);
    assert.doesNotMatch(refused, /source disclosure denied|Report is unavailable/);
  }
  // Other failures keep Retry.
  assert.match(await at(t, '#/campaign/9/report/2'), /data-action="retry"/);
});

test('AA-01 the campaign history does not link a report that cannot be opened', async () => {
  const rows = [{ report_no: 1, captured_at: '2026-10-16T05:10:00Z', digest: 'aa', stale: false, available: true, exportable: false },
    { report_no: 2, captured_at: '2026-10-17T05:10:00Z', digest: 'bb', stale: false, available: true, exportable: true },
    { report_no: 3, captured_at: '2026-10-18T05:10:00Z', digest: 'cc', stale: false, available: true, exportable: false, export_refusal: 'transcript_export' }];
  const base = campApi();
  const t = withApi(await views(), p => (p === '/customer/campaigns/9/reports' ? { reports: rows } : base(p)));
  const html = await at(t, '#/campaign/9');
  assert.doesNotMatch(html, /#\/campaign\/9\/report\/1"/);
  assert.doesNotMatch(html, /#\/campaign\/9\/report\/3"/);
  assert.match(html, /#\/campaign\/9\/report\/2"/);
  const items = [...html.matchAll(/<li class="ew-work-report">([\s\S]*?)<\/li>/g)].map(m => visible(m[1]));
  const one = items.find(i => i.includes('Report 1'));
  const three = items.find(i => i.includes('Report 3'));
  assert.ok(one.includes(HISTORY_ONLY) && !one.includes(TRANSCRIPT_ONLY), one);
  assert.ok(three.includes(TRANSCRIPT_ONLY) && !three.includes(HISTORY_ONLY), three);
});

test('MKT-02 verdict: one checked and one missing episode reads "not every episode in scope has been fully checked for ads yet"', async () => {
  const labels = { e1: 'When an Ex Police Chief Becomes a Hitman', e2: 'The Unthinkable Discovery' };
  const d = supplied({ placements: [win(APR, '2026-04-01', '2026-04-30')],
    coverage_by_placement: {
      [APR]: { state: 'incomplete', missing: ['e2'], failed: [], episodes: ['e1', 'e2'], exclusions: [], episode_labels: labels } } });
  const t = withApi(await views(), campApi(d));
  const html = await at(t, '#/campaign/9');
  const m = /<p class="ew-work-verdict">([^<]*)<\/p>/.exec(html);
  assert.ok(m, 'the verdict line is rendered');
  assert.equal(m[1], '0 of 1 counted placement has a result yet — not every episode in scope has been fully checked for ads yet.');
  assert.doesNotMatch(visible(html), /for the episodes in scope|transcript coverage|whole-episode screening/);
});
