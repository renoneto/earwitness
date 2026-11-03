/* WIN3 (lead WIN-2 #3): count contradictions and the own-brief form.
   1. Start campaign with typed brief text registers that brief first
      (the Save brief only call) and creates the campaign against it;
      a refused registration creates nothing.
   2. Add placement defaults to the campaign's own brief/version; an edit
      keeps the placement's stored brief.
   3. Overview's "N placements need attention" and its tags use the
      campaign page's rule and tag (one helper) when the server sends
      each placement's episode summary.
   4. The episode summary leads "Confirmed heard in N of M" (UX wording,
      pending approval): N stays the confirmed count, the tails stay.
   WIN3-FIX1: Edit campaign's Save changes registers typed brief text
      the same way (one shared path); with nothing typed its request is
      byte for byte today's.
   WIN3-FIX2: Save brief only, Start campaign and Save changes share one
      registration state (never two POSTs of the same text, never a
      resend after no answer); the "not saved yet" note follows a failed
      save only; a frozen report's page says its frozen HTML's lead
      ("Heard in" up to campaign-report/4.2).
   WIN3-FIX3: a refused Save brief only leaves the form editable; a
      campaign save with no answer never says "not saved yet"; Save brief
      only on text it already registered sends nothing (close and reopen:
      frontend_win3_reopen.test.cjs).
   Run: node --test tests/frontend_win3.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIXTURE = path.join(__dirname, 'fixtures', 'func2', 'episode-verdicts.json');
const freshDraft = () => JSON.parse(fs.readFileSync(FIXTURE, 'utf8')).draft;

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
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
      createElement: tag => ({ tagName: String(tag), dataset: {}, appendChild: () => {} }), createTextNode: t => t },
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
  const briefStatus = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? main : sel === '#brief-status' ? briefStatus
    : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.setTimeout = () => {};
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  const went = []; win.EW.go = r => { went.push(r); };
  return { win, modal, main, toasts, went, briefStatus };
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
const plain = v => JSON.parse(JSON.stringify(v));
const httpError = (status, message) => Object.assign(new Error(message), { status, serverMessage: true });

/* ---- 1. own brief on Start campaign --------------------------------------- */

// The create dialog's form as the person left it: supplied requirements,
// the brief select on its silent default (the first registered brief),
// and the "Add provided text as a new brief" fields.
function campaignForm(t, { title = '', text = '', selected = 'quince:1' } = {}) {
  const field = value => ({ value, setAttribute() {}, removeAttribute() {}, focus() {} });
  const submit = { disabled: false, textContent: 'Start campaign', setAttribute() {}, focus() {} };
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  const added = [];
  const select = Object.assign(field(selected), { appendChild(o) { added.push(o); return o; } });
  const parts = { '#brief-title': field(title), '#brief-text': field(text), '#campaign-brief': select,
    '#campaign-name': field('Judge test: Mint Mobile'), '#form-errors': box, '[type=submit]': submit, '#brief-status': t.briefStatus };
  const form = { dataset: {}, elements: [], getAttribute: () => 'campaign', closest: () => null,
    querySelector: s => parts[s] || null, querySelectorAll: s => (s === '[type=submit]' ? [submit] : []) };
  t.win.FormData = class { *entries() {
    yield ['mode', 'supplied_requirements']; yield ['name', 'Judge test: Mint Mobile']; yield ['brand', 'Mint Mobile'];
    yield ['message', 'Wireless plans from $15 a month']; yield ['offer', 'mintmobile.com/legends']; yield ['notes', ''];
    yield ['brief_ref', select.value];
    yield ['brief_title', parts['#brief-title'].value]; yield ['brief_text', parts['#brief-text'].value];
  } };
  return { form, box, select, added, parts };
}
const briefApi = ({ register = () => ({ brief_id: 'mint', version: 1 }), create = () => ({ campaign: { id: 9 } }) } = {}) => (p, opts) => {
  if (opts.method === 'POST' && p === '/customer/briefs') return register(opts.body);
  if (opts.method === 'POST' && p === '/customer/campaigns') return create(opts.body);
  throw new Error('unexpected request ' + (opts.method || 'GET') + ' ' + p);
};
const posts = t => t.calls.filter(c => c[0] === 'POST').map(c => c[1]);

test('WIN3-1 Start campaign with typed brief text registers it first and creates the campaign against it, not the select default', async () => {
  const t = withApi(await views(), briefApi());
  const f = campaignForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read in episode 446.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs', '/customer/campaigns'], 'the brief registers before the campaign');
  assert.deepEqual(plain(t.calls[0][2]), { title: 'Mint Mobile Oct', provided_text: 'One Mint Mobile host read in episode 446.' },
    'the same body Save brief only sends');
  const camp = t.calls[1][2].campaign;
  assert.deepEqual(plain(camp.requirement_provenance), { brief_ref: { kind: 'supplied_brief', brief_id: 'mint', version: 1 } });
  assert.equal(camp.mode, 'supplied_requirements');
  assert.deepEqual(t.went, ['#/campaign/9']);
});

test('WIN3-1 a refused registration creates nothing and the form says why in the brief words', async () => {
  const t = withApi(await views(), briefApi({ register: () => httpError(400, 'provided_text is too long') }));
  const f = campaignForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs'], 'no campaign is created');
  assert.match(f.box.innerHTML, /The brief for Judge test: Mint Mobile was not saved: provided_text is too long\./);
  assert.deepEqual(t.went, []);
  assert.equal(f.parts['[type=submit]'].disabled, false, 'Start campaign can be pressed again');
});

test('WIN3-1 a registration with no answer creates nothing: the brief line says it could not be confirmed and the form is held', async () => {
  const t = withApi(await views(), briefApi({ register: () => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true }) }));
  const f = campaignForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs']);
  assert.match(t.briefStatus.innerHTML, /The brief for Judge test: Mint Mobile could not be confirmed\./);
  // It may be saved: the save's no-answer rule holds the form (Reload),
  // so Start campaign never registers it again blind.
  assert.match(f.box.innerHTML, /Your change could not be confirmed\. Reload the workspace to continue\./);
  assert.equal(f.parts['[type=submit]'].disabled, true);
});

test('WIN3-1 a title without text (or text without a title) sends nothing and asks for both', async () => {
  for (const typed of [{ title: 'Mint Mobile Oct' }, { text: 'One Mint Mobile host read.' }]) {
    const t = withApi(await views(), briefApi());
    const f = campaignForm(t, typed);
    await t.win.EWViews.handleSubmit(f.form);
    await settle();
    assert.deepEqual(posts(t), [], JSON.stringify(typed));
    assert.match(f.box.innerHTML, /Give the brief a title and paste its provided text first\./, JSON.stringify(typed));
  }
});

test('WIN3-1 nothing typed: the chosen registered brief is used, with no registration', async () => {
  const t = withApi(await views(), briefApi());
  const f = campaignForm(t, { selected: 'quince:2' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/campaigns']);
  assert.deepEqual(plain(t.calls[0][2].campaign.requirement_provenance.brief_ref), { kind: 'supplied_brief', brief_id: 'quince', version: 2 });
});

test('WIN3-1 Save brief only still registers and selects; Start campaign after it never registers the same text twice', async () => {
  const t = withApi(await views(), briefApi());
  const f = campaignForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleAction({ dataset: { action: 'brief-register' }, disabled: false, closest: () => f.form });
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs']);
  assert.equal(f.select.value, 'mint:1', 'Save brief only selects the new brief');
  assert.match(t.briefStatus.innerHTML, /Brief “Mint Mobile Oct” saved \(version 1\)\. The campaign itself is not saved yet/);
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs', '/customer/campaigns'], 'one registration only');
  assert.deepEqual(plain(t.calls[1][2].campaign.requirement_provenance.brief_ref), { kind: 'supplied_brief', brief_id: 'mint', version: 1 });
});

test('WIN3-1 the brief saved but the campaign refused: a second Start campaign reuses that brief', async () => {
  let fail = true;
  const t = withApi(await views(), briefApi({ create: () => (fail ? httpError(400, 'name is taken') : { campaign: { id: 9 } }) }));
  const f = campaignForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs', '/customer/campaigns']);
  assert.equal(f.select.value, 'mint:1', 'the saved brief is now the choice');
  assert.match(t.briefStatus.innerHTML, /Brief “Mint Mobile Oct” saved \(version 1\)/);
  fail = false;
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(posts(t), ['/customer/briefs', '/customer/campaigns', '/customer/campaigns'], 'no second registration');
  assert.deepEqual(plain(t.calls[2][2].campaign.requirement_provenance.brief_ref), { kind: 'supplied_brief', brief_id: 'mint', version: 1 });
  assert.deepEqual(t.went, ['#/campaign/9']);
});

/* ---- 2. placement form default -------------------------------------------- */

const BRIEFS = { items: [{ brief_id: 'quince', current_version: 1, title: 'Quince podcast ads' }, { brief_id: 'mint', current_version: 2, title: 'Mint Mobile Oct' }] };
const placementApi = (draft) => (p) => {
  if (p === '/customer/campaigns/3/draft') return { draft };
  if (p === '/customer/shows') return { shows: [{ id: 's1', display_title: 'Myths and Legends' }] };
  if (p.startsWith('/customer/shows/')) return { episodes: [] };
  if (p.startsWith('/customer/briefs?')) return BRIEFS;
  throw new Error('unexpected request ' + p);
};
const briefOptions = html => /<select id="placement-brief"[^>]*>(.*?)<\/select>/.exec(html)[1];
const selectedBrief = html => (/<option value="([^"]*)" selected>/.exec(briefOptions(html)) || [])[1];

test('WIN3-2 Add placement defaults to the campaign\'s own brief and version, not the first brief', async () => {
  const draft = { campaign: { id: 3, revision: 4 }, pins: {}, placements: [],
    requirement_provenance: { kind: 'supplied_brief', brief_id: 'mint', version: 1, digest: 'd1', source_label: 'Mint Mobile Oct' } };
  const t = withApi(await views('#/campaign/3'), placementApi(draft));
  t.win.EW.current.route = '#/campaign/3'; t.win.location.hash = '#/campaign/3';
  await t.win.EWViews.handleAction({ dataset: { action: 'add-placement' } });
  await settle();
  assert.equal(t.modal.open, true);
  assert.equal(selectedBrief(t.modal.innerHTML), 'mint:1', 'the campaign\'s brief, at its own version (the list carries v2)');
  assert.equal((briefOptions(t.modal.innerHTML).match(/ selected/g) || []).length, 1);
  assert.match(briefOptions(t.modal.innerHTML), /<option value="mint:1" selected>Mint Mobile Oct v1<\/option>/, 'named by its title');
});

test('WIN3-2 editing a placement keeps its stored brief, even when the campaign has another', async () => {
  const draft = { campaign: { id: 3, revision: 4 }, pins: {},
    requirement_provenance: { kind: 'supplied_brief', brief_id: 'mint', version: 1 },
    placements: [{ id: 'p1', scope: 'show_window', show_id: 's1', revision: 2, binding: 'eligible', expected_count: 1,
      provenance: { brief_ref: { brief_id: 'quince', version: 1 } } }] };
  const t = withApi(await views('#/campaign/3/placement/p1'), placementApi(draft));
  t.win.EW.current.route = '#/campaign/3/placement/p1'; t.win.location.hash = '#/campaign/3/placement/p1';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-placement' } });
  await settle();
  assert.equal(selectedBrief(t.modal.innerHTML), 'quince:1');
});

test('WIN3-2 a campaign with no brief: Add placement keeps today\'s list', async () => {
  const draft = { campaign: { id: 3, revision: 4 }, pins: {}, placements: [], requirement_provenance: null };
  const t = withApi(await views('#/campaign/3'), placementApi(draft));
  t.win.EW.current.route = '#/campaign/3'; t.win.location.hash = '#/campaign/3';
  await t.win.EWViews.handleAction({ dataset: { action: 'add-placement' } });
  await settle();
  assert.equal(briefOptions(t.modal.innerHTML), '<option value="quince:1">Quince podcast ads v1</option><option value="mint:2">Mint Mobile Oct v2</option>');
});

/* ---- 3. Overview counts = campaign page counts ----------------------------- */

const OV = over => ({ coverage: { episodes: 1, episodes_with_transcript: 1, shows: 1 }, recent_events: [], offer_sequence: [],
  alert_totals: [], alert_lifecycle_counts: { active_local: 1, paused: 0, stopped: 0 }, tracked_campaigns: [], campaign_gaps: [], activity: [], ...over });
async function overview(ov) {
  const t = withApi(await views(), p => {
    if (p === '/customer/overview') return ov;
    if (p.startsWith('/customer/search')) return { items: [] };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    if (p === '/customer/brands') return { brands: [] };
    return {};
  });
  return at(t, '#/overview');
}
const sum = over => ({ heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 0, checked: 0, waiting_review: 0, not_checked: 0, unconfirmed: 0, ...over });
// The seeded "Quince ad check" (WIN-REVIEW shots 11 and 33): the server's
// old per-placement labels, plus each placement's episode summary
// (logs/win3/SERVER.md). Every placement comes, as the campaign page has them.
const QUINCE = [
  { id: 'p-space', label: 'No matching ad in the available transcript', linkage: 'none', outcome: 'no_observation_in_covered_sources', s: sum({ not_heard: 1, checked: 1 }) },
  { id: 'p-431', label: '4 waiting for review', linkage: 'unreviewed', outcome: 'unreviewed_detections_in_scope', s: sum({ heard: 1, heard_right: 1, checked: 1 }) },
  { id: 'p-756', label: '6 waiting for review', linkage: 'unreviewed', outcome: 'unreviewed_detections_in_scope', s: sum({ unconfirmed: 1 }) },
  { id: 'p-maga', label: '2 waiting for review', linkage: 'unreviewed', outcome: 'unreviewed_detections_in_scope', s: sum({ unconfirmed: 1 }) },
  { id: 'p-bench', label: 'No matching ad in the available transcript', linkage: 'none', outcome: 'no_observation_in_covered_sources', s: sum({ not_heard: 1, checked: 1 }) },
];
const quinceRows = (withSummary = true) => QUINCE.map(q => ({ campaign_id: 7, campaign_name: 'Quince ad check', placement_id: q.id,
  outcome: q.outcome, linkage: q.linkage, label: q.label, placement_label: 'Episode: ' + q.id, placement_href: `#/campaign/7/placement/${q.id}`,
  ...(withSummary ? { episode_summary: q.s } : {}) }));
const QUINCE_CARD = { id: 7, name: 'Quince ad check', state: 'Tracking', brand: 'Quince' };

test('WIN3-3 Overview "need attention" and its tags use the campaign page rule: Quince ad check reads 2, not 5', async () => {
  const html = await overview(OV({ tracked_campaigns: [QUINCE_CARD], campaign_gaps: quinceRows() }));
  const text = visible(html);
  assert.match(text, /Quince ad check Tracking Quince 2 placements need attention/);
  assert.doesNotMatch(text, /5 placements need attention/);
  assert.doesNotMatch(text, /waiting for review/, 'the old per-placement label is gone');
  const listed = [...html.matchAll(/href="#\/campaign\/7\/placement\/([^"]+)"/g)].map(m => m[1]);
  assert.deepEqual(listed, ['p-756', 'p-maga'], 'the placements the campaign page filters as Needs attention');
  assert.equal((text.match(/Confirmed heard in 0 of 1 checked episode · 1 episode to confirm/g) || []).length, 2, 'the campaign page\'s own tag');
});

test('WIN3-3 Overview and the campaign page agree on one draft: the same placements, the same tags', async () => {
  // The FUNC-2 fixture: pl-ep is "supported" yet has a read to confirm;
  // pl-amb has no episodes and an ad found. The campaign page says 3.
  const draft = freshDraft();
  const t = withApi(await views('#/campaign/1'), p => {
    if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Fern Q4 host reads', brand: 'fern', requirement_provenance: {}, allowed_actions: draft.allowed_actions } };
    if (p === '/customer/campaigns/1/draft') return { draft };
    if (p === '/customer/campaigns/1/reports') return { reports: [] };
    throw new Error('unexpected request ' + p);
  });
  const camp = await at(t, '#/campaign/1');
  const n = Number(/data-filter="needs-attention"[^>]*>Needs attention · (\d+)</.exec(camp)[1]);
  assert.equal(n, 3);
  const ev = draft.allocation.evaluations, cov = draft.coverage_by_placement;
  const linkage = { 'pl-win': 'insufficient', 'pl-ep': 'insufficient', 'pl-amb': 'possible', 'pl-url': 'insufficient' };
  const rows = draft.placements.map(p => {
    const v = draft.episode_verdicts[p.id] || {};
    return { campaign_id: 1, campaign_name: 'Fern Q4 host reads', placement_id: p.id, outcome: ev[p.id].outcome,
      linkage: linkage[p.id], label: cov[p.id].state === 'complete' ? 'Ad found' : 'Not enough data',
      placement_label: 'Placement ' + p.id, placement_href: `#/campaign/1/placement/${p.id}`,
      episode_summary: (v.episodes || []).length ? v.summary : null };
  });
  const html = await overview(OV({ tracked_campaigns: [{ id: 1, name: 'Fern Q4 host reads', state: 'Tracking' }], campaign_gaps: rows }));
  assert.match(visible(html), new RegExp(`${n} placements need attention`));
  const listed = [...html.matchAll(/href="#\/campaign\/1\/placement\/([^"]+)"/g)].map(m => m[1]);
  assert.deepEqual(listed, ['pl-win', 'pl-ep', 'pl-url']);
  // Each Overview tag is the campaign row's tag, word for word.
  for (const pid of listed) {
    const tagOf = (h, re) => (re.exec(h) || [])[1];
    const ovTag = tagOf(html, new RegExp(`placement/${pid}"[^>]*>[^<]*</a>(?:<span data-part="tag">)?(<span class="tag[^"]*"[^>]*>[^<]*</span>)`));
    const campTag = tagOf(camp, new RegExp(`data-episodes="${pid}" data-part="tag">(<span class="tag[^"]*"[^>]*>[^<]*</span>)`));
    assert.ok(ovTag && campTag, pid);
    assert.equal(visible(ovTag), visible(campTag), pid);
  }
});

test('WIN3-3 an older server (no episode summary): rows keep their labels; an "Ad found" row is not "needs attention", as on the campaign page', async () => {
  const rows = quinceRows(false);
  rows.push({ campaign_id: 7, campaign_name: 'Quince ad check', placement_id: 'p-found', outcome: 'pending', linkage: 'possible', label: 'Ad found',
    placement_label: 'Episode: p-found', placement_href: '#/campaign/7/placement/p-found' });
  const html = await overview(OV({ tracked_campaigns: [QUINCE_CARD], campaign_gaps: rows }));
  const text = visible(html);
  assert.match(text, /5 placements need attention/);
  assert.match(text, /4 waiting for review/);
  assert.doesNotMatch(html, /placement\/p-found/);
});

test('WIN3-3 a campaign with nothing needing attention has no attention line and is not listed first', async () => {
  const calm = quinceRows().filter(r => ['p-431', 'p-space'].includes(r.placement_id));
  const html = await overview(OV({ tracked_campaigns: [{ id: 6, name: 'Other', state: 'Tracking' }, QUINCE_CARD], campaign_gaps: calm }));
  assert.doesNotMatch(visible(html), /need attention|needs attention/i);
  assert.ok(html.indexOf('>Other<') < html.indexOf('>Quince ad check<'), 'order unchanged');
});

/* ---- 4. "Confirmed heard in N of M" ---------------------------------------- */

async function campaignPage(draft) {
  const t = withApi(await views('#/campaign/1'), p => {
    if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Fern', brand: 'fern', requirement_provenance: {}, allowed_actions: draft.allowed_actions } };
    if (p === '/customer/campaigns/1/draft') return { draft };
    if (p === '/customer/campaigns/1/reports') return { reports: [] };
    throw new Error('unexpected request ' + p);
  });
  return { camp: await at(t, '#/campaign/1'), place: await at(t, '#/campaign/1/placement/pl-ep') };
}
// Every summary line leads with the new words; none says a bare "Heard in N".
const BARE = /(?<!Confirmed )[Hh]eard in \d/;

test('WIN3-4 a heard-not-confirmed episode: "Confirmed heard in 0 of 1", the approved tail, nothing new', async () => {
  const draft = freshDraft();
  draft.episode_verdicts['pl-ep'].summary = sum({ unconfirmed: 1 });
  draft.episode_verdicts['pl-url'].summary = sum({ heard: 1, heard_right: 1, not_heard: 1, checked: 2, unconfirmed: 1 });
  const { camp, place } = await campaignPage(draft);
  assert.match(place, /<p class="ew-ep-summary">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm<\/p>/);
  assert.match(place, /data-episodes="pl-ep" data-part="tag"><span class="tag amber">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm</);
  assert.match(visible(place), /Heard · not confirmed/, 'the row says the rest');
  assert.match(visible(camp), /Confirmed heard in 0 of 1 checked episode · 1 episode to confirm/);
  assert.match(visible(camp), /Confirmed heard in 1 of 3 checked episodes · 1 episode to confirm/);
  assert.doesNotMatch(visible(camp) + visible(place), BARE);
});

test('WIN3-4 singular/plural and the other tails as today', async () => {
  const draft = freshDraft();
  draft.episode_verdicts['pl-ep'].summary = sum({ waiting_review: 1 });
  draft.episode_verdicts['pl-url'].summary = sum({ heard: 1, heard_right: 1, checked: 1 });
  draft.episode_verdicts['pl-win'].summary = sum({ heard: 2, heard_right: 2, not_heard: 1, checked: 3, not_checked: 2 });
  const { camp } = await campaignPage(draft);
  const text = visible(camp);
  assert.match(text, /Confirmed heard in 0 of 1 checked episode · 1 episode waiting for review/);
  assert.match(text, /Confirmed heard in 1 of 1 checked episode(?! ·)/);
  assert.match(text, /Confirmed heard in 2 of 3 checked episodes · 2 episodes not checked/);
  assert.doesNotMatch(text, BARE);
});

test('WIN3-4 the count stays confirmed-only: the tag stays amber and "Ads found" counts confirmed episodes only', async () => {
  const draft = freshDraft();
  for (const pid of ['pl-win', 'pl-ep', 'pl-url']) draft.episode_verdicts[pid].summary = sum({ unconfirmed: 1 });
  const { camp } = await campaignPage(draft);
  assert.match(camp, /data-filter="possible"[^>]*>Ads found · 1</, 'only pl-amb (no episodes, ad found)');
  assert.match(camp, /data-episodes="pl-ep" data-part="tag"><span class="tag amber">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm</);
});

test('WIN3-4 Overview shows the same words as the campaign row', async () => {
  const html = await overview(OV({ tracked_campaigns: [QUINCE_CARD], campaign_gaps: quinceRows() }));
  assert.doesNotMatch(visible(html), BARE);
  assert.match(html, /<span class="tag amber">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm<\/span>/);
});

// WIN3-FIX4 (screen W3-SCR-R1-01): at 390 the long episode tag must wrap
// on Overview as it does on the campaign row, the placement and the Report
// card, so it sits in the same data-part="tag" wrapper (work.css), both
// under a tracked campaign and in the stray "Needs attention" list.
test('WIN3-FIX4 Overview episode tags sit in the data-part="tag" wrapper that lets them wrap', async () => {
  const nested = await overview(OV({ tracked_campaigns: [QUINCE_CARD], campaign_gaps: quinceRows() }));
  const stray = await overview(OV({ tracked_campaigns: [], campaign_gaps: quinceRows() }));
  for (const html of [nested, stray]) {
    const tags = html.match(/<span class="tag amber">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm<\/span>/g) || [];
    assert.ok(tags.length >= 2, 'the two unconfirmed placements are listed');
    const wrapped = html.match(/<span data-part="tag"><span class="tag amber">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm<\/span><\/span>/g) || [];
    assert.equal(wrapped.length, tags.length, 'every Overview episode tag is wrapped');
  }
});

// W3-SCR-R2-I2: three strays, so the third sits in the "N more placements
// need attention" details, drawn by the same gapRow: its tag is wrapped too.
test('WIN3-FIX4 a stray row in the "more" details keeps the data-part="tag" wrapper', async () => {
  const rows = quinceRows();
  rows.push({ ...rows.find(r => r.placement_id === 'p-maga'), placement_id: 'p-third', placement_label: 'Episode: p-third', placement_href: '#/campaign/7/placement/p-third' });
  const html = await overview(OV({ tracked_campaigns: [], campaign_gaps: rows }));
  const at = html.indexOf('<details class="ew-ov-details"><summary>1 more placements need attention</summary>');
  assert.ok(at >= 0, 'the third stray is in the details');
  const more = html.slice(at, html.indexOf('</details>', at));
  assert.match(more, /href="#\/campaign\/7\/placement\/p-third"/);
  assert.match(more, /<span data-part="tag"><span class="tag amber">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm<\/span><\/span>/);
});

/* ---- WIN3-FIX1: Edit campaign registers typed brief text too -------------- */

// The Edit campaign dialog's form (supplied requirements): the stored
// brief on the select, and the same "Add provided text as a new brief"
// fields. No mode: an edit never sends one.
function editForm(t, { title = '', text = '', selected = 'quince:2', supplied = true } = {}) {
  const f = campaignForm(t, { title, text, selected });
  f.parts['[type=submit]'].textContent = 'Save changes';
  t.win.FormData = class { *entries() {
    yield ['campaign_id', '5']; yield ['expected_revision', '3'];
    yield ['name', 'Judge test: Mint Mobile']; yield ['brand', 'Mint Mobile'];
    yield ['message', 'Wireless plans from $15 a month']; yield ['offer', 'mintmobile.com/legends']; yield ['notes', ''];
    if (supplied) {
      yield ['brief_ref', f.select.value];
      yield ['brief_title', f.parts['#brief-title'].value]; yield ['brief_text', f.parts['#brief-text'].value];
    }
  } };
  if (!supplied) { delete f.parts['#brief-title']; delete f.parts['#brief-text']; delete f.parts['#campaign-brief']; }
  return f;
}
const editApi = ({ register = () => ({ brief_id: 'mint', version: 1 }), save = () => ({ campaign: { id: 5 } }) } = {}) => (p, opts) => {
  if (opts.method === 'POST' && p === '/customer/briefs') return register(opts.body);
  if (opts.method === 'PUT' && p === '/customer/campaigns/5') return save(opts.body);
  throw new Error('unexpected request ' + (opts.method || 'GET') + ' ' + p);
};
const writes = t => t.calls.filter(c => c[0] !== 'GET').map(c => `${c[0]} ${c[1]}`);

test('WIN3-FIX1 Save changes with typed brief text registers it first and saves the campaign against it', async () => {
  const t = withApi(await views(), editApi());
  const f = editForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read in episode 446.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'PUT /customer/campaigns/5'], 'the brief registers before the save');
  assert.deepEqual(plain(t.calls[0][2]), { title: 'Mint Mobile Oct', provided_text: 'One Mint Mobile host read in episode 446.' },
    'the same body Save brief only sends');
  const body = plain(t.calls[1][2]);
  assert.deepEqual(body.campaign.requirement_provenance, { brief_ref: { kind: 'supplied_brief', brief_id: 'mint', version: 1 } });
  assert.equal(body.expected_revision, 3);
  assert.equal('mode' in body.campaign, false, 'an edit never sends a mode');
  assert.equal(f.select.value, 'mint:1', 'the new brief is the choice');
  assert.deepEqual(t.went, ['#/campaign/5']);
});

test('WIN3-FIX1 a refused registration saves nothing and the form says why in the brief words', async () => {
  const t = withApi(await views(), editApi({ register: () => httpError(400, 'provided_text is too long') }));
  const f = editForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs'], 'the campaign is not saved');
  assert.match(f.box.innerHTML, /The brief for Judge test: Mint Mobile was not saved: provided_text is too long\./);
  assert.deepEqual(t.went, []);
  assert.equal(f.select.value, 'quince:2', 'the stored brief stays the choice');
  assert.equal(f.parts['[type=submit]'].disabled, false, 'Save changes can be pressed again');
});

test('WIN3-FIX1 a registration with no answer saves nothing: the brief line says it could not be confirmed and the form is held', async () => {
  const t = withApi(await views(), editApi({ register: () => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true }) }));
  const f = editForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs']);
  assert.match(t.briefStatus.innerHTML, /The brief for Judge test: Mint Mobile could not be confirmed\./);
  assert.match(f.box.innerHTML, /Your change could not be confirmed\. Reload the workspace to continue\./);
  assert.equal(f.parts['[type=submit]'].disabled, true);
});

test('WIN3-FIX1 a title without text (or text without a title) sends nothing and asks for both', async () => {
  for (const typed of [{ title: 'Mint Mobile Oct' }, { text: 'One Mint Mobile host read.' }]) {
    const t = withApi(await views(), editApi());
    const f = editForm(t, typed);
    await t.win.EWViews.handleSubmit(f.form);
    await settle();
    assert.deepEqual(writes(t), [], JSON.stringify(typed));
    assert.match(f.box.innerHTML, /Give the brief a title and paste its provided text first\./, JSON.stringify(typed));
  }
});

test('WIN3-FIX1 nothing typed: Save changes sends exactly today\'s request, byte for byte', async () => {
  const cases = [
    [{ selected: 'quince:2' }, '{"campaign":{"name":"Judge test: Mint Mobile","brand":"Mint Mobile","message":"Wireless plans from $15 a month","offer":"mintmobile.com/legends","notes":"","requirement_provenance":{"brief_ref":{"kind":"supplied_brief","brief_id":"quince","version":2}}},"expected_revision":3}'],
    [{ selected: '' }, '{"campaign":{"name":"Judge test: Mint Mobile","brand":"Mint Mobile","message":"Wireless plans from $15 a month","offer":"mintmobile.com/legends","notes":""},"expected_revision":3}'],
    [{ supplied: false }, '{"campaign":{"name":"Judge test: Mint Mobile","brand":"Mint Mobile","message":"Wireless plans from $15 a month","offer":"mintmobile.com/legends","notes":""},"expected_revision":3}'],
  ];
  for (const [opts, wire] of cases) {
    const t = withApi(await views(), editApi());
    const f = editForm(t, opts);
    await t.win.EWViews.handleSubmit(f.form);
    await settle();
    assert.deepEqual(writes(t), ['PUT /customer/campaigns/5'], JSON.stringify(opts));
    assert.equal(JSON.stringify(t.calls[0][2]), wire, JSON.stringify(opts));
    assert.deepEqual(t.went, ['#/campaign/5'], JSON.stringify(opts));
  }
});

test('WIN3-FIX1 Save brief only, then Save changes: one registration, the campaign saved against it', async () => {
  const t = withApi(await views(), editApi());
  const f = editForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleAction({ dataset: { action: 'brief-register' }, disabled: false, closest: () => f.form });
  await settle();
  assert.equal(f.select.value, 'mint:1');
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'PUT /customer/campaigns/5'], 'one registration only');
  assert.deepEqual(plain(t.calls[1][2].campaign.requirement_provenance.brief_ref), { kind: 'supplied_brief', brief_id: 'mint', version: 1 });
});

test('WIN3-FIX1 the brief saved but the campaign save refused: a second Save changes reuses that brief', async () => {
  let fail = true;
  const t = withApi(await views(), editApi({ save: () => (fail ? httpError(400, 'name is taken') : { campaign: { id: 5 } }) }));
  const f = editForm(t, { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' });
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'PUT /customer/campaigns/5']);
  assert.equal(f.select.value, 'mint:1', 'the saved brief is now the choice');
  assert.match(t.briefStatus.innerHTML, /Brief “Mint Mobile Oct” saved \(version 1\)/);
  fail = false;
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'PUT /customer/campaigns/5', 'PUT /customer/campaigns/5'], 'no second registration');
  assert.deepEqual(plain(t.calls[2][2].campaign.requirement_provenance.brief_ref), { kind: 'supplied_brief', brief_id: 'mint', version: 1 });
  assert.deepEqual(t.went, ['#/campaign/5']);
});

test('WIN3-FIX1 one registration path: the brief call is written once in views.js', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.equal((src.match(/'\/customer\/briefs', \{ method: 'POST'/g) || []).length, 1);
});

/* ---- WIN3-FIX2 (code r1 W3-R1-01, -03, -04): one registration state ------- */
// Save brief only, Start campaign and Save changes share one "brief
// registration in flight / no answer" state on the form: while one brief
// POST runs, the others send none; after a no answer, none is sent again.

// A brief POST (and, when asked, the campaign write) the test answers later.
function laterApi({ write = false } = {}) {
  const briefs = [], writes = [];
  const later = list => new Promise((res, rej) => list.push({ res, rej }));
  const api = (p, opts) => {
    if (opts.method === 'POST' && p === '/customer/briefs') return later(briefs);
    if (opts.method === 'POST' && p === '/customer/campaigns') return write ? later(writes) : { campaign: { id: 9 } };
    if (opts.method === 'PUT' && p === '/customer/campaigns/5') return write ? later(writes) : { campaign: { id: 5 } };
    throw new Error('unexpected request ' + (opts.method || 'GET') + ' ' + p);
  };
  return { api, briefs, writes };
}
const KINDS = [
  { kind: 'Start campaign', form: campaignForm, write: 'POST /customer/campaigns', went: '#/campaign/9' },
  { kind: 'Save changes', form: editForm, write: 'PUT /customer/campaigns/5', went: '#/campaign/5' },
];
const typedMint = { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' };
const briefCount = t => writes(t).filter(w => w === 'POST /customer/briefs').length;
const saveOnly = f => ({ dataset: { action: 'brief-register' }, disabled: false, closest: () => f.form });
const BUSY = 'This change is already being saved.';
// A test whose request is never answered fails instead of hanging.
const LATER = { timeout: 5000 };
const NO_ANSWER = /Your change could not be confirmed\. Reload the workspace to continue\./;
const MINT_REF = { kind: 'supplied_brief', brief_id: 'mint', version: 1 };

for (const k of KINDS) {
  test(`WIN3-FIX2 ${k.kind} pressed twice while its brief POST runs: one registration, one save (W3-R1-03)`, LATER, async () => {
    const d = laterApi();
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    const b = t.win.EWViews.handleSubmit(f.form);
    await settle();
    assert.equal(briefCount(t), 1);
    assert.ok(t.toasts.includes(BUSY), 'the second press says the change is already being saved');
    d.briefs.forEach(r => r.res({ brief_id: 'mint', version: 1 }));
    await a; await b; await settle();
    assert.deepEqual(writes(t), ['POST /customer/briefs', k.write]);
    assert.deepEqual(t.went, [k.went]);
  });

  test(`WIN3-FIX2 Save brief only pressed while ${k.kind}'s brief POST runs: one registration (W3-R1-01)`, LATER, async () => {
    const d = laterApi();
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    await settle();
    const b = t.win.EWViews.handleAction(saveOnly(f));
    await settle();
    d.briefs.forEach((r, i) => r.res({ brief_id: 'mint' + (i || ''), version: 1 }));
    await a; await b; await settle();
    assert.equal(briefCount(t), 1);
    assert.deepEqual(writes(t), ['POST /customer/briefs', k.write]);
    assert.deepEqual(plain(t.calls.find(c => `${c[0]} ${c[1]}` === k.write)[2]).campaign.requirement_provenance.brief_ref, MINT_REF);
  });

  test(`WIN3-FIX2 ${k.kind} pressed while Save brief only's POST runs: no second registration; once it answers, ${k.kind} uses it (W3-R1-01)`, LATER, async () => {
    const d = laterApi();
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleAction(saveOnly(f));
    await settle();
    const b = t.win.EWViews.handleSubmit(f.form);
    await settle();
    const sent = writes(t);
    d.briefs.forEach((r, i) => r.res({ brief_id: 'mint' + (i || ''), version: 1 }));
    await a; await b; await settle();
    assert.deepEqual(sent, ['POST /customer/briefs'], 'nothing else is sent');
    assert.ok(t.toasts.includes(BUSY));
    assert.equal(f.select.value, 'mint:1');
    await t.win.EWViews.handleSubmit(f.form);
    await settle();
    assert.deepEqual(writes(t), ['POST /customer/briefs', k.write], 'one registration only');
    assert.deepEqual(plain(t.calls[1][2]).campaign.requirement_provenance.brief_ref, MINT_REF);
  });

  test(`WIN3-FIX2 Save brief only had no answer, then ${k.kind}: nothing is sent again; the form is held with Reload (W3-R1-01)`, async () => {
    const t = withApi(await views(), briefApi({ register: () => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true }) }));
    const f = k.form(t, typedMint);
    await t.win.EWViews.handleAction(saveOnly(f));
    await settle();
    await t.win.EWViews.handleSubmit(f.form);
    await settle();
    assert.deepEqual(writes(t), ['POST /customer/briefs'], 'no blind retry, no save');
    assert.match(f.box.innerHTML, NO_ANSWER);
    assert.match(f.box.innerHTML, /data-action="reload-workspace"/);
    assert.equal(f.parts['[type=submit]'].disabled, true);
    assert.equal(f.form.dataset.held, '1');
  });

  test(`WIN3-FIX2 ${k.kind}'s brief POST had no answer, then Save brief only: nothing is sent again (W3-R1-01)`, async () => {
    const t = withApi(await views(), briefApi({ register: () => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true }) }));
    const f = k.form(t, typedMint);
    await t.win.EWViews.handleSubmit(f.form);
    await settle();
    const el = saveOnly(f);
    await t.win.EWViews.handleAction(el);
    await settle();
    assert.deepEqual(writes(t), ['POST /customer/briefs']);
    assert.match(f.box.innerHTML, NO_ANSWER);
  });

  test(`WIN3-FIX2 ${k.kind}: "The campaign itself is not saved yet" shows only after the save fails, not while it runs (W3-R1-04)`, LATER, async () => {
    const d = laterApi({ write: true });
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    await settle();
    d.briefs[0].res({ brief_id: 'mint', version: 1 });
    await settle();
    assert.deepEqual(writes(t), ['POST /customer/briefs', k.write], 'the save is running');
    assert.doesNotMatch(t.briefStatus.innerHTML, /not saved yet/, 'not while the save runs');
    d.writes[0].rej(httpError(400, 'name is taken'));
    await a; await settle();
    assert.match(t.briefStatus.innerHTML, /Brief “Mint Mobile Oct” saved \(version 1\)\. The campaign itself is not saved yet — finish and submit the campaign form\./);
  });

  test(`WIN3-FIX2 ${k.kind} saved: the "not saved yet" note never shows (W3-R1-04)`, LATER, async () => {
    const d = laterApi({ write: true });
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    await settle();
    d.briefs[0].res({ brief_id: 'mint', version: 1 });
    await settle();
    d.writes[0].res({ campaign: { id: k.went.split('/').pop() } });
    await a; await settle();
    assert.doesNotMatch(t.briefStatus.innerHTML, /not saved yet/);
    assert.deepEqual(t.went, [k.went]);
  });
}

// The picker re-renders its "Save brief only" button (enabled) when the
// mode changes, so the button's own disabled state is not the guard.
test('WIN3-FIX2 Save brief only pressed again (a re-rendered button) while its POST runs: one registration', LATER, async () => {
  const d = laterApi();
  const t = withApi(await views(), d.api);
  const f = campaignForm(t, typedMint);
  const a = t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  const b = t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  const sent = briefCount(t);
  d.briefs.forEach((r, i) => r.res({ brief_id: 'mint' + (i || ''), version: 1 }));
  await a; await b; await settle();
  assert.equal(sent, 1);
  assert.ok(t.toasts.includes(BUSY));
});

test('WIN3-FIX2 Save brief only had no answer, then Save brief only again (a re-rendered button): nothing is sent again', async () => {
  const t = withApi(await views(), briefApi({ register: () => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true }) }));
  const f = campaignForm(t, typedMint);
  await t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  await t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs']);
  assert.match(f.box.innerHTML, NO_ANSWER);
});

for (const k of KINDS) {
  test(`WIN3-FIX2 Save brief only pressed while ${k.kind}'s campaign save runs (its brief already registered): no second registration`, LATER, async () => {
    const d = laterApi({ write: true });
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    await settle();
    d.briefs[0].res({ brief_id: 'mint', version: 1 });
    await settle();
    const b = t.win.EWViews.handleAction(saveOnly(f));
    await settle();
    const sent = writes(t);
    d.briefs.slice(1).forEach(r => r.res({ brief_id: 'mint2', version: 1 }));
    d.writes.forEach(r => r.res({ campaign: { id: k.went.split('/').pop() } }));
    await a; await b; await settle();
    assert.deepEqual(sent, ['POST /customer/briefs', k.write]);
    assert.ok(t.toasts.includes(BUSY));
  });

  test(`WIN3-FIX2 ${k.kind} pressed twice while its campaign save runs (brief registered): one save`, LATER, async () => {
    const d = laterApi({ write: true });
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    await settle();
    d.briefs[0].res({ brief_id: 'mint', version: 1 });
    await settle();
    const b = t.win.EWViews.handleSubmit(f.form);
    await settle();
    const sent = writes(t);
    d.writes.forEach(r => r.res({ campaign: { id: k.went.split('/').pop() } }));
    await a; await b; await settle();
    assert.deepEqual(sent, ['POST /customer/briefs', k.write]);
    assert.ok(t.toasts.includes(BUSY));
  });
}

/* ---- WIN3-FIX2 (code r1 W3-R1-02): a frozen report keeps its own words ---- */
// The App Report page says what that report's frozen HTML says: "Heard in"
// for campaign-report/4.2 and earlier (Fresh's fixture, frozen at
// d8db4a28), "Confirmed heard in" from 4.3 (frozen.renderer, the stored
// report JSON's renderer).
const WIN3_FIX = path.join(__dirname, 'fixtures', 'win3');
const stored42 = () => ({ frozen: JSON.parse(fs.readFileSync(path.join(WIN3_FIX, 'report-4.2-pre-win3.json'), 'utf8')),
  html: fs.readFileSync(path.join(WIN3_FIX, 'report-4.2-pre-win3.html'), 'utf8') });
// The same report frozen at 4.3: Fresh's test_frozen_episode_verdicts pins
// that a 4.3 freeze is the 4.2 bytes with only the lead words changed.
const stored43 = () => {
  const s = stored42();
  s.frozen.renderer = 'campaign-report/4.3';
  s.html = s.html.replace(/Heard in /g, 'Confirmed heard in ');
  return s;
};
async function reportOf(frozen) {
  const t = withApi(await views(), p => {
    if (p === '/customer/campaigns/1/reports/1') return { frozen, stale: false };
    throw new Error('unexpected request ' + p);
  });
  return at(t, '#/campaign/1/report/1');
}
const storedSummary = html => {
  const m = /<p>((?:Confirmed h|H)eard in \d+ of \d+ checked episodes?[^<]*)<\/p>/.exec(html);
  assert.ok(m, 'the stored HTML has a summary line');
  return m[1];
};
const appSummaries = html => [...html.matchAll(/<p class="ew-ep-summary">([^<]*)<\/p>|<span data-part="tag"><span class="tag[^"]*">([^<]*)<\/span>/g)].map(m => m[1] || m[2]);

for (const [name, make, lead] of [['4.2', stored42, 'Heard in'], ['4.3', stored43, 'Confirmed heard in']]) {
  test(`WIN3-FIX2 a report frozen at campaign-report/${name}: the App Report page says its frozen HTML's words ("${lead}")`, async () => {
    const s = make();
    const want = storedSummary(s.html);
    assert.equal(want, `${lead} 2 of 2 checked episodes`);
    const html = await reportOf(s.frozen);
    const got = appSummaries(html);
    assert.equal(got.length, 2, 'the summary line and the card tag');
    for (const g of got) assert.equal(g, want);
    if (lead === 'Heard in') assert.doesNotMatch(visible(html), /Confirmed heard in/);
  });
}

test('WIN3-FIX2 the renderer gate: 4.2 and earlier (or none) say "Heard in"; 4.3 and later say "Confirmed heard in"', async () => {
  const cases = [[undefined, 'Heard in'], ['campaign-report/4.1', 'Heard in'], ['campaign-report/4.2', 'Heard in'],
    ['campaign-report/4.3', 'Confirmed heard in'], ['campaign-report/4.10', 'Confirmed heard in'], ['campaign-report/5', 'Confirmed heard in']];
  for (const [renderer, lead] of cases) {
    const s = stored42();
    if (renderer === undefined) delete s.frozen.renderer; else s.frozen.renderer = renderer;
    const got = appSummaries(await reportOf(s.frozen));
    assert.deepEqual(got, [`${lead} 2 of 2 checked episodes`, `${lead} 2 of 2 checked episodes`], String(renderer));
  }
});

test('WIN3-FIX2 the live pages keep "Confirmed heard in" whatever a report says', async () => {
  const draft = freshDraft();
  draft.episode_verdicts['pl-ep'].summary = sum({ heard: 1, heard_right: 1, checked: 1 });
  const { camp, place } = await campaignPage(draft);
  assert.match(visible(camp), /Confirmed heard in 1 of 1 checked episode/);
  assert.match(visible(place), /Confirmed heard in 1 of 1 checked episode/);
  assert.doesNotMatch(visible(camp) + visible(place), BARE);
});

/* ---- WIN3-FIX3 (code r2 W3-R2-02, W3-R2-03) ------------------------------ */
// A refused Save brief only (the server said no) is not a no-answer: the
// form stays editable, and the fixed text registers on the next press.
test('WIN3-FIX3 a refused Save brief only leaves the form editable; the fixed text then registers once (W3-R2-02)', async () => {
  let n = 0;
  const t = withApi(await views(), briefApi({ register: () => (n++ ? { brief_id: 'mint', version: 1 } : httpError(400, 'provided_text is too short')) }));
  const f = campaignForm(t, typedMint);
  const first = saveOnly(f);
  await t.win.EWViews.handleAction(first);
  await settle();
  assert.match(t.briefStatus.innerHTML, /was not saved: provided_text is too short\./);
  assert.equal(first.disabled, false, 'Save brief only can be pressed again');
  assert.equal(f.form.dataset.held, undefined, 'not held');
  assert.equal(f.parts['[type=submit]'].disabled, false);
  f.parts['#brief-text'].value = typedMint.text + ' Fixed.';
  await t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'POST /customer/briefs'], 'the fixed text is sent');
  assert.equal(t.calls[1][2].provided_text, typedMint.text + ' Fixed.');
  assert.equal(f.select.value, 'mint:1');
  await t.win.EWViews.handleSubmit(f.form);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'POST /customer/briefs', 'POST /customer/campaigns'], 'registered once');
  assert.deepEqual(plain(t.calls[2][2]).campaign.requirement_provenance.brief_ref, MINT_REF);
});

for (const k of KINDS) {
  // The campaign may be saved, so "not saved yet" would be false.
  test(`WIN3-FIX3 ${k.kind}'s campaign save had no answer: "not saved yet" does not show; the form is held (W3-R2-02)`, LATER, async () => {
    const d = laterApi({ write: true });
    const t = withApi(await views(), d.api);
    const f = k.form(t, typedMint);
    const a = t.win.EWViews.handleSubmit(f.form);
    await settle();
    d.briefs[0].res({ brief_id: 'mint', version: 1 });
    await settle();
    d.writes[0].rej(Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true }));
    await a; await settle();
    assert.deepEqual(writes(t), ['POST /customer/briefs', k.write]);
    assert.doesNotMatch(t.briefStatus.innerHTML, /not saved yet/);
    assert.match(f.box.innerHTML, NO_ANSWER);
    assert.equal(f.form.dataset.held, '1');
  });
}

// Save brief only pressed again on text it already registered: that
// brief is reused (its saved line again), nothing is sent. Other text
// still registers.
test('WIN3-FIX3 Save brief only pressed again on the same registered text sends nothing; changed text registers (W3-R2-03)', async () => {
  let n = 0;
  const t = withApi(await views(), briefApi({ register: () => ({ brief_id: n++ ? 'mint2' : 'mint', version: 1 }) }));
  const f = campaignForm(t, typedMint);
  await t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  t.briefStatus.innerHTML = '';
  const again = saveOnly(f);
  await t.win.EWViews.handleAction(again);
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs'], 'no second registration');
  assert.match(t.briefStatus.innerHTML, /Brief “Mint Mobile Oct” saved \(version 1\)\./, 'the saved line again');
  assert.equal(again.disabled, false);
  assert.equal(f.select.value, 'mint:1');
  f.parts['#brief-text'].value = 'Another Mint Mobile read.';
  await t.win.EWViews.handleAction(saveOnly(f));
  await settle();
  assert.deepEqual(writes(t), ['POST /customer/briefs', 'POST /customer/briefs'], 'changed text registers');
  assert.equal(f.select.value, 'mint2:1');
});
