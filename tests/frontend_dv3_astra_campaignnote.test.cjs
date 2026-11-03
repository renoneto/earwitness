/* Astra round 2, area campaignnote: UX-02b/02c (an attached source is
   named by brand, show, episode and time; the placement page points to
   Add to campaign from Evidence) and UX-08 (the personal-note dialog).
   Run: node --test tests/frontend_dv3_astra_campaignnote.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const VIEWS = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
const SAVED = fs.readFileSync(path.join(APP, 'saved.js'), 'utf8');

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
async function bootViews(route = '#/overview') {
  const win = loadAt(route);
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(VIEWS, win, { filename: 'views.js' });
  await flushBoot();
  return win;
}
async function renderRoute(route, api) {
  const win = await bootViews();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ').replace(/&#39;/g, "'").replace(/\s+/g, ' ');

// ---- UX-02b / UX-02c ------------------------------------------------------
const WIN = 'exp:f3ba15d41424';
const REF = 'sr1:1474b30c1d0e4a5f8b2c3d4e5f607182';
const REF2 = 'sr1:644cc4006f8e4d3aa1b2c3d4e5f60718';
const c1Draft = (over = {}) => ({
  campaign: { id: 1, revision: 4, name: 'BetterHelp Q4 host reads' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [
    { id: WIN, scope: 'show_window', show_id: 's1', date_from: '2026-06-18', date_to: '2026-10-09', expected_count: 2, revision: 1, binding: 'eligible', provenance: { binding: 'eligible' } },
    { id: 'exp:other', scope: 'episode', show_id: 's1', episode_id: 'ep5', expected_count: 1, revision: 1, binding: 'eligible', provenance: { binding: 'eligible' } },
  ],
  targets: [], episode_titles: { ep5: 'The Last 12 Weeks - Ep. 5' },
  coverage_by_placement: {}, comparisons: {},
  allocation: { evaluations: {}, citations: {} }, reason_codes: { evaluations: {}, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: {}, limitations: [] },
  pins: { attachments: { [WIN]: [REF] }, attachment_state: { [REF]: { evidence_revision: 3, source_pins: { episode_id: 'ep5' } } } },
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'],
  ...over,
});
const namedItem = (ref, over = {}) => ({ ref, show_title: 'Serial', ep_title: 'The Last 12 Weeks - Ep. 5', pub_date: '2026-09-02',
  quotation_span: [61, 80],
  read: { brand: { display: 'BetterHelp', confirmed: false, under_review: false }, needs_review: false, display_span: [62, 95] }, ...over });
// Records every search request; `reply` decides what the search returns.
function placementApi(draft, reply) {
  const searches = [];
  const api = p => {
    if (p === '/customer/campaigns/1/draft') return { draft };
    if (p.startsWith('/customer/search?')) {
      const query = JSON.parse(new URLSearchParams(p.split('?')[1]).get('query'));
      searches.push(query);
      return reply(query);
    }
    throw new Error('unexpected request ' + p);
  };
  api.searches = searches;
  return api;
}
const PLACEMENT = '#/campaign/1/placement/' + encodeURIComponent(WIN);
const attachedCard = html => /<section class="ew-card ew-work-card ew-work-attached">[\s\S]*?<\/section>/.exec(html)[0];

test('UX-02b an attached source is named by brand, show, episode and time, not "Source 1"', async () => {
  const api = placementApi(c1Draft(), q => ({ items: q.ids.map(r => namedItem(r)) }));
  const { html } = await renderRoute(PLACEMENT, api);
  const card = attachedCard(html);
  const row = /<ul class="ew-card-list ew-work-sources">([\s\S]*?)<\/ul>/.exec(card)[1];
  assert.match(row, /BetterHelp/);
  assert.doesNotMatch(row, /Name not confirmed/, 'PLAIN-ADS: no name flag');
  assert.match(visible(row), /Serial · The Last 12 Weeks - Ep\. 5 · /);
  assert.match(row, /data-start="00:01:02" data-end="00:01:35"/, 'the read\'s own time span');
  assert.doesNotMatch(row, />Source 1</);
  assert.match(row, /Open source →/, 'Open source stays');
  assert.match(row, /<details class="tech-details">[\s\S]*sr1:1474b30c/, 'the raw reference stays in Technical details');
  assert.doesNotMatch(visible(row), /sr1:1474/, 'the raw reference is not in the reading path');
  for (const op of ['retain', 'reference', 'move']) assert.match(row, new RegExp(`data-op="${op}"`));
  assert.match(row, /data-action="detach-evidence"/);
});

test('UX-02b a held brand shows "Brand not identified", never the brand key', async () => {
  const api = placementApi(c1Draft(), q => ({ items: q.ids.map(r => namedItem(r, { read: { brand: { display: 'Secretbrand', key: 'secretbrand', confirmed: false, under_review: true }, display_span: [10, 20] } })) }));
  const { html } = await renderRoute(PLACEMENT, api);
  const row = /<ul class="ew-card-list ew-work-sources">([\s\S]*?)<\/ul>/.exec(attachedCard(html))[1];
  assert.match(row, /Brand not identified/);
  assert.doesNotMatch(visible(row), /Secretbrand|secretbrand/);
});

test('UX-02b a read with no brand says "Brand not identified"; a confirmed name carries no flag', async () => {
  const none = placementApi(c1Draft(), q => ({ items: q.ids.map(r => namedItem(r, { read: { brand: {}, display_span: [10, 20] } })) }));
  const a = await renderRoute(PLACEMENT, none);
  assert.match(attachedCard(a.html), /Brand not identified/);
  const ok = placementApi(c1Draft(), q => ({ items: q.ids.map(r => namedItem(r, { read: { brand: { display: 'BetterHelp', confirmed: true }, display_span: [10, 20] } })) }));
  const b = await renderRoute(PLACEMENT, ok);
  assert.doesNotMatch(attachedCard(b.html), /Name not confirmed/);
});

test('UX-02b the identity read fails: "Attached source 1" with the episode title, the page and the four operations stay', async () => {
  const api = placementApi(c1Draft(), () => { throw Object.assign(new Error('boom'), { status: 500 }); });
  const { html } = await renderRoute(PLACEMENT, api);
  const row = /<ul class="ew-card-list ew-work-sources">([\s\S]*?)<\/ul>/.exec(attachedCard(html))[1];
  assert.match(visible(row), /Attached source 1/);
  assert.match(visible(row), /The Last 12 Weeks - Ep\. 5/);
  assert.doesNotMatch(row, />Source 1</);
  for (const op of ['retain', 'reference', 'move']) assert.match(row, new RegExp(`data-op="${op}"`));
  assert.match(row, /data-action="detach-evidence"/);
});

test('UX-02b a ref the search did not return falls back to its episode title', async () => {
  const api = placementApi(c1Draft(), () => ({ items: [] }));
  const { html } = await renderRoute(PLACEMENT, api);
  assert.match(visible(attachedCard(html)), /Attached source 1 The Last 12 Weeks - Ep\. 5/);
});

test('UX-02b the identity search asks for exactly these refs, in pages of 10', async () => {
  const api = placementApi(c1Draft(), () => ({ items: [] }));
  await renderRoute(PLACEMENT, api);
  assert.equal(api.searches.length, 1);
  assert.equal(api.searches[0].version, 2);
  assert.equal(api.searches[0].mode, 'ads');
  assert.deepEqual(api.searches[0].ids, [REF]);
  const many = Array.from({ length: 12 }, (_, i) => 'sr1:' + String(i).padStart(32, '0'));
  const draft = c1Draft({ pins: { attachments: { [WIN]: many }, attachment_state: {} } });
  const api2 = placementApi(draft, () => ({ items: [] }));
  await renderRoute(PLACEMENT, api2);
  assert.deepEqual(api2.searches.map(s => s.page).sort(), [1, 2], 'more than 10 refs read two pages');
  for (const s of api2.searches) assert.deepEqual([...s.ids].sort(), [...many].sort(), 'every page asks for the same refs');
});

test('UX-02b pool rows are named the same way', async () => {
  const draft = c1Draft({ attachment_pool: ['ev9'], pool_refs: { ev9: REF2 }, suggestions: ['ev9'] });
  const api = placementApi(draft, q => ({ items: q.ids.map(r => namedItem(r)) }));
  const { html } = await renderRoute(PLACEMENT, api);
  assert.deepEqual([...new Set(api.searches.flatMap(s => s.ids))].sort(), [REF, REF2].sort());
  const pool = /Suggested evidence[\s\S]*?<ul class="ew-card-list ew-work-sources">([\s\S]*?)<\/ul>/.exec(attachedCard(html))[1];
  assert.match(pool, /BetterHelp/);
  assert.doesNotMatch(pool, /Suggested evidence 1/);
  assert.match(pool, /data-action="attach-pool-evidence"/);
});

test('UX-02b the Move dialog and the buttons carry the readable source name', async () => {
  const api = placementApi(c1Draft(), q => ({ items: q.ids.map(r => namedItem(r)) }));
  const { html } = await renderRoute(PLACEMENT, api);
  assert.match(html, /data-action="allocation-request" data-op="retain"[^>]*data-label="BetterHelp · Serial · The Last 12 Weeks - Ep\. 5"/);
  // The handler passes d.label into the dialog title row.
  assert.match(VIEWS, /allocationDialog\(\{[^}]*label: d\.label/);
  assert.match(VIEWS, /<b>\$\{esc\(label \|\| 'This source'\)\} on \$\{esc\(labelOf\(pid\)\)\}<\/b>/);
});

test('UX-02c an empty pool points to Search this window and Add to campaign, and the exact reference form is folded', async () => {
  const api = placementApi(c1Draft(), q => ({ items: q.ids.map(r => namedItem(r)) }));
  const { html } = await renderRoute(PLACEMENT, api);
  const card = attachedCard(html);
  assert.match(visible(card), /No suggested sources\. To attach evidence, find it with Search this window ?, then use Add to campaign on it\./);
  assert.doesNotMatch(card, /Attachment pool is empty/);
  assert.match(card, /<a [^>]*href="#\/search[^"]*"[^>]*>Search this window<\/a>/, 'a link, in the card');
  const form = /<details class="tech-details"><summary>Attach by exact reference<\/summary>([\s\S]*?)<\/details>/.exec(card);
  assert.ok(form, 'the form sits in a details');
  assert.match(form[1], /<form id="attach-evidence"[\s\S]*id="attach-ref"/);
});

test('UX-02c an episode placement offers its transcript instead of a window search', async () => {
  const draft = c1Draft({ pins: { attachments: {}, attachment_state: {} } });
  const api = placementApi(draft, () => ({ items: [] }));
  const { html } = await renderRoute('#/campaign/1/placement/' + encodeURIComponent('exp:other'), api);
  const card = attachedCard(html);
  assert.match(visible(card), /find it with Open episode transcript ?, then use Add to campaign/);
  assert.match(card, /href="#\/episode\/ep5"/);
  assert.equal(api.searches.length, 0, 'nothing attached, nothing to look up');
});

// ---- UX-08 --------------------------------------------------------------
async function openNoteDialog(note) {
  const win = await bootViews();
  const modal = { innerHTML: '', open: false };
  win.document.querySelector = sel => (sel === '#modal' ? modal : null);
  const titles = [];
  win.EW.openDialog = t => { titles.push(t); };
  win.EW.api = async p => {
    if (p.endsWith('/mistake')) return { mistakes: note ? [note] : [] };
    if (p.startsWith('/customer/mistakes/')) return { note };
    throw new Error('unexpected request ' + p);
  };
  return { win, modal, titles };
}
async function submitNote(win, id, entries) {
  const errors = [];
  win.EW.formError = (message) => { errors.push(message); return false; };
  win.FormData = class { *entries() { yield* entries; } };
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  const submit = { disabled: false, textContent: 'Save', setAttribute() {}, focus() {} };
  const form = { dataset: {}, elements: [{ type: 'hidden' }], getAttribute: () => id, closest: () => null,
    querySelector: sel => (sel === '#form-errors' ? box : sel === '[type=submit]' ? submit : null),
    querySelectorAll: sel => (sel === '[type=submit]' ? [submit] : []) };
  await win.EWViews.handleSubmit(form);
  return errors;
}

test('UX-08 the add dialog says Add personal note, Note and Your note status, and that it is not a problem report', async () => {
  const { win, modal, titles } = await openNoteDialog(null);
  await win.EWViews.handleAction({ dataset: { action: 'report-mistake', id: 'sr1:x' } });
  assert.deepEqual(titles, ['Add personal note']);
  assert.match(modal.innerHTML, /<h2 id="dialog-title">Add personal note<\/h2>/);
  assert.match(modal.innerHTML, /for="mistake-reason">Note</);
  assert.match(modal.innerHTML, /for="mistake-status">Your note status</);
  assert.match(modal.innerHTML, /does not send a problem report/);
  assert.match(modal.innerHTML, /<option value="open">Open<\/option>/);
  assert.doesNotMatch(modal.innerHTML, /Report a mistake|What is wrong|tables are untouched/);
});

test('UX-08 an existing note opens as Edit personal note and keeps its id and revision', async () => {
  const { win, modal, titles } = await openNoteDialog({ id: 5, revision: 3, status: 'open', reason: 'Kept' });
  await win.EWViews.handleAction({ dataset: { action: 'report-mistake', id: 'sr1:x' } });
  assert.deepEqual(titles, ['Edit personal note']);
  assert.match(modal.innerHTML, /<h2 id="dialog-title">Edit personal note<\/h2>/);
  assert.match(modal.innerHTML, /Current note: open · Kept/);
  assert.match(modal.innerHTML, /name="mistake_id" value="5"/);
});

test('UX-08 an empty note says "Enter a note before saving." in both forms', async () => {
  const { win } = await openNoteDialog(null);
  const a = await submitNote(win, 'mistake', [['ref', 'sr1:x'], ['reason', '  '], ['status', 'open']]);
  assert.deepEqual(a, ['Enter a note before saving.']);
  const b = await submitNote(win, 'edit-note', [['mistake_id', '5'], ['reason', ''], ['status', 'open']]);
  assert.deepEqual(b, ['Enter a note before saving.']);
});

test('UX-08 the note page edit dialog is Edit personal note with Your note status', async () => {
  const { win, modal, titles } = await openNoteDialog({ id: 5, revision: 3, status: 'open', reason: 'Kept', ref: 'sr1:x' });
  await win.EWViews.handleAction({ dataset: { action: 'edit-note', id: '5', rev: '3' } });
  assert.deepEqual(titles, ['Edit personal note']);
  assert.match(modal.innerHTML, /<h2 id="dialog-title">Edit personal note<\/h2>/);
  assert.match(modal.innerHTML, /for="note-status">Your note status</);
  assert.match(modal.innerHTML, /Only your note changes\./);
  assert.doesNotMatch(modal.innerHTML, /metadata/);
});

// PLAIN-ADS (DEP7): "Report a mistake" is the ad report (ui.reportControl
// only); the personal note never takes that name.
test('UX-08 source guard: "Report a mistake" and "What is wrong?" only in reportControl, never in views.js or saved.js', () => {
  assert.doesNotMatch(VIEWS, /Report a mistake/);
  assert.doesNotMatch(SAVED, /Report a mistake/);
  assert.doesNotMatch(VIEWS, /What is wrong\?/);
  assert.match(fs.readFileSync(path.join(APP, 'ui.js'), 'utf8'), /What is wrong\?/);
  assert.match(fs.readFileSync(path.join(APP, 'ui.js'), 'utf8'), /<summary>Report a mistake<\/summary>/);
  assert.doesNotMatch(VIEWS, /Edit note metadata/);
});

test('UX-08 the Evidence note notice is a plain notice, not a warning, and says it is not a report', () => {
  assert.doesNotMatch(VIEWS, /notice warn">Note saved/);
  // PLAIN-ADS-FIX3 (screen R3-06): the note in quotes, then the plain line.
  // PLAIN-ADS-FIX4 (screen R4-05): no note state in the line.
  assert.match(VIEWS, /<div class="notice">Your note: “[^`]*” Notes are yours; they do not report a mistake to EarWitness\./);
});

test('UX-08 the empty Saved notes state points to Add personal note', () => {
  assert.match(SAVED, /No standalone notes yet\.', 'Notes attached to saved excerpts appear under Excerpts\. Add a standalone note from any evidence page\./);
});
