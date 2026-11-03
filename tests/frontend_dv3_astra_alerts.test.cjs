/* Astra round 2 fixes on the alert page, the alert dialog and the Overview
   search box: UX-03 (latest check vs history, earlier-settings label from
   the stored revision), UX-04 (phone order), UX-07 (stale preview, errors
   that clear) and UX-12 (Escape in the Overview search).
   Run: node --test tests/frontend_dv3_astra_alerts.test.cjs */
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
function loadViews(fetch, route = '#/overview', { docListeners } = {}) {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  if (docListeners) win.document.addEventListener = (type, fn) => docListeners.push([type, fn]);
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
const T1 = '2026-10-10T08:00:00Z';
const T2 = '2026-10-14T16:27:57Z';
const ALERT = { id: 1, name: 'Free trial wording', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free trial' },
  current_matches: { count: 48, tracked: 34, tracked_by_kind: { new_finding: 34 }, held_for_brand_review: 14 } };
const SUM = { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'NPR News Now', ep_title: '12am ET',
  pub_date: '2026-10-09', span: [250.02, 309.24], text: 'Before the break. You can try it free trial today with this offer. More words after.',
  historical: false, read: { brand: { key: 'betterhelp', display: 'BetterHelp', confirmed: false }, needs_review: false, historical: false } };
const ev = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:' + id, read_state: 0, historical: false,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM, ...over });
async function alertHtml(alert) {
  const { html } = await renderRoute('#/alert/' + alert.id, p => {
    if (p === '/customer/alerts/' + alert.id) return { alert };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: [] };
    throw new Error('unexpected request ' + p);
  });
  return html;
}
const idx = (html, s) => html.indexOf(s);

// ---- UX-03: latest check vs history; earlier settings from the stored revision ----

test('UX-03b the latest check result sits apart from earlier changes', async () => {
  const html = await alertHtml({ ...ALERT, definition_revision: 2, last_successful_definition_revision: 2, last_successful_at: T2,
    unread_event_count: 2, unread_ad_count: 2,
    events: [ev(1, { definition_revision: 2, checked_at: T2 }),
      ev(2, { definition_revision: 2, checked_at: T1, summary: { ...SUM, episode_id: 'ep2', ep_title: '1am ET' } })] });
  const latest = idx(html, 'Latest check · ');
  const earlier = idx(html, 'Earlier changes');
  assert.ok(latest > 0, 'Latest check sub-head');
  assert.ok(earlier > latest, 'Earlier changes after Latest check');
  const one = idx(html, 'data-event-id="1"'), two = idx(html, 'data-event-id="2"');
  assert.ok(latest < one && one < earlier, 'event 1 is the latest check');
  assert.ok(two > earlier, 'event 2 is history');
  assert.match(html, /2 changes · 2 unread/, 'the header still counts every event');
  assert.doesNotMatch(html, /Found under earlier settings/);
});

test('UX-03b a check that found nothing says so in the page', async () => {
  const html = await alertHtml({ ...ALERT, definition_revision: 1, last_successful_definition_revision: 1, last_successful_at: T2,
    unread_event_count: 1, events: [ev(1, { definition_revision: 1, checked_at: T1 })] });
  assert.match(html, /Latest check · <time [^>]*>[^<]*<\/time><\/h3><p class="section-note ew-work-import">No new matches in stored records\.<\/p>/);
  assert.ok(idx(html, 'data-event-id="1"') > idx(html, 'Earlier changes'));
});

test('UX-03c events from before a settings change are labelled from the stored revision only', async () => {
  const html = await alertHtml({ ...ALERT, name: 'Free trial wording v2', definition_revision: 2, last_successful_definition_revision: 1,
    last_successful_at: T2, query: { version: 2, mode: 'ads', q: 'free trial offer' }, unread_event_count: 2,
    events: [ev(1, { definition_revision: 1, checked_at: T2, historical: true }),
      ev(2, { definition_revision: 1, checked_at: T2, historical: true, summary: { ...SUM, episode_id: 'ep2' } })] });
  assert.match(html, /Not checked since the settings changed/);
  assert.match(html, /Found under earlier settings/);
  assert.match(html, /may not match the current settings/);
  assert.doesNotMatch(html, /<mark>offer<\/mark>/, 'never highlights the new words in old matches');
  assert.doesNotMatch(html, /<mark>free<\/mark>/);
  assert.doesNotMatch(html, /Earlier changes/, 'no empty history section');
  assert.ok(idx(html, 'data-event-id="1"') > idx(html, 'Found under earlier settings'));
  // Never describes the old settings: no old words, no "used to watch".
  const section = html.slice(idx(html, 'Found under earlier settings'));
  assert.doesNotMatch(section.slice(0, 400), /free trial|watched|used to/i);
});

test('UX-03c no provenance on the events: today\'s single list, no invented labels', async () => {
  const html = await alertHtml({ ...ALERT, unread_event_count: 2, events: [ev(1), ev(2, { summary: { ...SUM, episode_id: 'ep2' } })] });
  assert.doesNotMatch(html, /Found under earlier settings/);
  assert.doesNotMatch(html, /Latest check/);
  assert.doesNotMatch(html, /Earlier changes/);
  assert.ok(idx(html, 'data-event-id="1"') > 0 && idx(html, 'data-event-id="2"') > 0);
  assert.match(html, /<mark>free<\/mark> <mark>trial<\/mark>/);
});

test('UX-03 the latest bucket is not used for a revision that was never checked', async () => {
  // Same checked_at as the last success, but under an older revision:
  // that is history under earlier settings, never "latest".
  const html = await alertHtml({ ...ALERT, definition_revision: 2, last_successful_definition_revision: 2, last_successful_at: T2,
    unread_event_count: 1, events: [ev(1, { definition_revision: 1, checked_at: T2 })] });
  assert.ok(idx(html, 'data-event-id="1"') > idx(html, 'Found under earlier settings'));
  assert.match(html, /Latest check · <time [^>]*>[^<]*<\/time><\/h3><p class="section-note ew-work-import">No new matches in stored records\./);
});

// ---- UX-04: phone order ----

test('UX-04 / DEMO-SOL-R1-12 the changes come before the current state in the DOM', async () => {
  const html = await alertHtml({ ...ALERT, events: [ev(1)], unread_event_count: 1 });
  assert.match(html, /class="profile-grid ew-work-split ew-alert-split"/);
  assert.ok(idx(html, '<h2>Changes</h2>') > 0 && idx(html, '<h2>Changes</h2>') < idx(html, '<aside'), 'Changes first (Sol DEMO2 r1)');
  for (const s of ['Current matches', 'Check local records', 'Saved scope']) assert.ok(idx(html, s) > idx(html, '<h2>Changes</h2>'), s);
  assert.match(html, /<div class="ew-card-head"><h2>Changes<\/h2>(?:(?!<\/div>).)*data-action="mark-read"/, 'Mark all read stays at the history head');
});

test('UX-04 / DEMO-SOL-R1-12 work.css keeps desktop columns and stacks Changes first under 1024px', () => {
  const css = fs.readFileSync(path.join(APP, 'work.css'), 'utf8');
  assert.match(css, /\.ew-alert-split\s*\{\s*grid-template-areas:\s*"main aside";/);
  assert.match(css, /\.ew-alert-split > \.ew-work-aside\s*\{\s*grid-area:\s*aside;/);
  const narrow = css.slice(css.indexOf('@media (max-width: 1023px)'));
  const block = narrow.slice(0, narrow.indexOf('\n}'));
  assert.match(block, /\.ew-alert-split\s*\{\s*grid-template-areas:\s*"main" "aside";/);
  assert.doesNotMatch(css, /\border:\s*-?\d/, 'no CSS order (reading order follows the DOM)');
});

// ---- UX-07: alert dialog ----

function fakeEl(props = {}) {
  return { dataset: {}, attrs: {}, innerHTML: '', textContent: '', isConnected: true,
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; }, hasAttribute(k) { return k in this.attrs; },
    focus() {}, addEventListener() {}, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    parentElement: null, ...props };
}
const invalid = el => el.attrs['aria-invalid'] === 'true';

test('UX-07 alertFormProblems lists every problem in submit order', () => {
  const win = loadViews(canonicalSessionFetch());
  const keys = v => [...win.EWViews.alertFormProblems(v).map(p => p.key)];
  assert.deepEqual(keys({ name: '', event_types: [] }), ['name', 'track']);
  assert.deepEqual(keys({ name: 'x', event_types: ['new_text'], from: '2026-10-10', to: '2026-10-01' }), ['dates']);
  assert.deepEqual(keys({ name: 'x', event_types: ['new_text'] }), []);
  assert.deepEqual(keys({ name: '  ' }), ['name', 'track']);
});

test('UX-07 formError clears the earlier field it marked when it marks a new one', () => {
  const win = loadAt('#/overview');
  const box = fakeEl();
  const name = fakeEl({ name: 'name' }), to = fakeEl({ name: 'to' });
  const form = { querySelector: s => (s === '#form-errors' ? box : null),
    querySelectorAll: s => (s === '[aria-invalid="true"]' ? [name, to].filter(invalid) : []) };
  win.EW.formError('Name it.', name, form);
  assert.ok(invalid(name));
  win.EW.formError('Dates.', to, form);
  assert.ok(!invalid(name), 'resolved field cleared');
  assert.ok(invalid(to), 'new field marked');
  win.EW.formError('Server said no.', null, form);
  assert.ok(!invalid(to), 'a message without a field clears earlier marks too');
  assert.match(box.innerHTML, /Server said no\./);
});

// A minimal alert dialog: the real alertDialog wires its listeners on a
// fake form; FormData reads the fake form's current entries.
async function openAlertDialog(initialMode = 'mentions') {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const fields = {
    name: fakeEl({ id: 'alert-name', name: 'name', value: '' }),
    q: fakeEl({ id: 'alert-q', name: 'q', value: 'free trial' }),
    mode: fakeEl({ id: 'alert-mode', name: 'mode', value: 'mentions' }),
    to: fakeEl({ id: 'alert-to', name: 'to', value: '' }),
    from: fakeEl({ id: 'alert-from', name: 'from', value: '' }),
  };
  const boxes = [fakeEl({ name: 'event_types', value: 'new_text', checked: false }), fakeEl({ name: 'event_types', value: 'new_finding', checked: false })];
  const errors = fakeEl({ id: 'form-errors' });
  const preview = fakeEl({ id: 'alert-preview', innerHTML: '<p class="section-note">Preview shows…</p>' });
  const listeners = [];
  const all = () => [...Object.values(fields), ...boxes];
  const byId = { '#form-errors': errors, '#alert-preview': preview, '#alert-name': fields.name, '#alert-to': fields.to,
    '#alert-mode': fields.mode, '#alert-touched': fakeEl(), '#alert-scope': fakeEl(), '#alert-track': fakeEl() };
  const inner = fakeEl({
    getAttribute: k => (k === 'id' ? 'alert-form' : null),
    addEventListener: (type, fn) => listeners.push([type, fn]),
    querySelector: s => byId[s] || null,
    querySelectorAll: s => {
      if (s === '[aria-invalid="true"]') return all().filter(invalid);
      if (/event_types/.test(s)) return boxes;
      return byId[s] ? [byId[s]] : [];
    },
  });
  const entries = () => {
    const out = [['initial_query', JSON.stringify({ version: 2, mode: initialMode })], ['initial_selector', 'null'], ['touched_fields', '[]']];
    for (const f of Object.values(fields)) out.push([f.name, f.value]);
    for (const b of boxes) if (b.checked) out.push(['event_types', b.value]);
    return out;
  };
  win.FormData = class { constructor() { this.e = entries(); } *entries() { yield* this.e; }
    get(k) { const hit = this.e.find(([n]) => n === k); return hit ? hit[1] : null; } };
  const dlgMap = { '#alert-form': inner, '.dialog-body': fakeEl(), '#alert-mode': fields.mode, '#alert-scope': fakeEl(), '#alert-touched': fakeEl(), '#alert-track': fakeEl() };
  const modal = fakeEl({ open: false, showModal() { this.open = true; }, close() { this.open = false; }, querySelector: s => dlgMap[s] || fakeEl() });
  win.document.querySelector = s => (s === '#modal' ? modal : s === '#alert-form' ? inner : s === '#alert-preview' ? preview : fakeEl());
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (p) => {
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  };
  await win.EWViews.handleAction({ dataset: { action: 'alerts-create' }, disabled: false, closest: () => null });
  const fire = (type, target) => { for (const [t, fn] of listeners) if (t === type) fn({ type, target }); };
  return { win, inner, fields, boxes, errors, preview, fire };
}

test('UX-07 changing the criteria marks a shown preview stale; the name does not', async () => {
  const d = await openAlertDialog();
  d.preview.innerHTML = '<p class="section-note" role="status">Preview: 322132 matching records. Preview only — nothing is saved or fetched.</p>';
  d.preview.dataset.previewState = 'result';
  d.fire('input', d.fields.name);
  assert.match(d.preview.innerHTML, /Preview: 322132/, 'a name edit keeps the preview');
  d.fields.mode.value = 'episodes';
  d.fire('change', d.fields.mode);
  assert.match(d.preview.innerHTML, /Settings changed — preview again/);
  assert.doesNotMatch(d.preview.innerHTML, /322132/);
  assert.equal(d.preview.dataset.previewState, 'stale');
});

test('UX-07 a late preview answer for old criteria never paints', async () => {
  const d = await openAlertDialog();
  let resolve;
  d.win.EW.api = (p) => {
    if (p === '/customer/research-query') return new Promise(r => { resolve = r; });
    throw new Error('unexpected request ' + p);
  };
  const pending = d.win.EWViews.handleAction({ dataset: { action: 'alert-preview' }, disabled: false, closest: () => null });
  await flushBoot();
  assert.match(d.preview.innerHTML, /Checking stored records/);
  d.fields.q.value = 'something else';
  d.fire('input', d.fields.q);
  resolve({ result: { total: 5, total_relation: 'exact', items: [{ record_kind: 'evidence' }] } });
  await pending;
  assert.doesNotMatch(d.preview.innerHTML, /Preview: /, 'old answer dropped');
});

test('UX-07 / VL-19 the preview counts in the unit Look in searches, never a "shown" sample', async () => {
  const d = await openAlertDialog();
  d.win.EW.api = async (p) => {
    if (p === '/customer/research-query') return { result: { total: 322132, total_relation: 'exact',
      items: Array.from({ length: 10 }, () => ({ record_kind: 'transcript_passage' })) } };
    throw new Error('unexpected request ' + p);
  };
  await d.win.EWViews.handleAction({ dataset: { action: 'alert-preview' }, disabled: false, closest: () => null });
  assert.match(d.preview.innerHTML, /Preview: 322132 matching passages\. Ads that match the same words are tracked too\./);
  assert.doesNotMatch(d.preview.innerHTML, /shown/);
  assert.equal(d.preview.dataset.previewState, 'result');
});

test('UX-07 resolved errors clear while typing; unresolved ones stay; no new error appears', async () => {
  const d = await openAlertDialog();
  const submit = () => d.win.EWViews.handleSubmit(d.inner);
  await submit();
  assert.match(d.errors.innerHTML, /Name your alert/);
  assert.ok(invalid(d.fields.name));
  // Typing a name resolves the name problem: mark and message go.
  d.fields.name.value = 'Test alert';
  d.fire('input', d.fields.name);
  assert.ok(!invalid(d.fields.name));
  assert.equal(d.errors.innerHTML, '', 'the resolved message is cleared');
  assert.doesNotMatch(d.errors.innerHTML, /Track at least one/, 'never shows a new error while typing');
  // Submit again: the track problem shows on the checkbox group.
  await submit();
  assert.match(d.errors.innerHTML, /Track at least one change type\./);
  assert.ok(d.boxes.every(invalid), 'every checkbox marked');
  assert.ok(!invalid(d.fields.name));
  // An unrelated edit keeps the still-invalid group marked.
  d.fire('input', d.fields.q);
  assert.ok(d.boxes.every(invalid));
  assert.match(d.errors.innerHTML, /Track at least one/);
  // Checking a type resolves it.
  d.boxes[0].checked = true;
  d.fire('change', d.boxes[0]);
  assert.ok(!d.boxes.some(invalid));
  assert.equal(d.errors.innerHTML, '');
});

test('UX-07 the track group points at the error message', async () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /<fieldset class="check-group" id="alert-track" aria-describedby="form-errors">/);
});

// ---- UX-12: Escape in the Overview search ----

async function escapeOn(route, field) {
  const docListeners = [];
  const win = loadViews(canonicalSessionFetch(), route, { docListeners });
  await flushBoot();
  const went = [];
  win.EW.go = h => went.push(h);
  win.document.querySelector = () => null;
  win.document.querySelectorAll = () => [];
  win.document.activeElement = field;
  win.EW.current.route = route;
  win.location.hash = route;
  const handlers = docListeners.filter(([t]) => t === 'keydown').map(([, fn]) => fn);
  assert.ok(handlers.length > 0);
  for (const fn of handlers) fn({ key: 'Escape', preventDefault() {}, target: field });
  return went;
}

test('UX-12 Escape in the Overview search clears the box and never navigates or searches', async () => {
  const field = { id: 'overview-search-query', value: 'free trial' };
  const went = await escapeOn('#/overview', field);
  assert.deepEqual(went, []);
  assert.equal(field.value, '');
  const empty = { id: 'overview-search-query', value: '' };
  assert.deepEqual(await escapeOn('#/overview', empty), []);
});

test('UAT-038 Escape on the Search page never runs a search: it restores the applied query (replaces UX-12’s clear-and-rerun)', async () => {
  const field = { id: 'search-query', value: 'typed', focus() {} };
  const went = await escapeOn('#/search?mode=ads&q=wayfair', field);
  assert.deepEqual(went, []);
  assert.equal(field.value, 'wayfair');
});

// Row 1 (d16, worker d1fb): an Ads preview counts the server's ads
// (break_total); with no ad count (older server) it shows no number.
test('d1b the Ads preview counts ads from break_total; no number without it', async () => {
  const d = await openAlertDialog('ads');
  d.fields.mode.value = 'ads';
  const run = async result => {
    d.win.EW.api = async (p) => {
      if (p === '/customer/research-query') return { result: { items: [], ...result } };
      throw new Error('unexpected request ' + p);
    };
    await d.win.EWViews.handleAction({ dataset: { action: 'alert-preview' }, disabled: false, closest: () => null });
    return d.preview.innerHTML;
  };
  assert.match(await run({ total: 13, break_total: 7, total_relation: 'exact' }), /Preview: 7 matching ads\./);
  assert.match(await run({ total: 60, break_total: 50, total_relation: 'lower_bound' }), /Preview: at least 50 matching ads\./);
  const old = await run({ total: 13, total_relation: 'exact' });
  assert.match(old, /Preview: matching ads\./);
  assert.doesNotMatch(old, /13|\breads?\b/);
});
