/* Astra UX-15: operator Processing and Data & Settings order.
   Processing puts New run and Runs before the input inventory. Data & Settings
   has an in-page index (buttons, not anchors: the operator routes on the hash)
   and the 292-row source table comes last. No rows are removed.
   Run: node --test tests/frontend_dv3_astra_operator.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadOperator() {
  const els = new Map();
  const mk = (id) => ({ id, innerHTML: '', textContent: '', value: '', disabled: false, checked: false, dataset: {}, isConnected: true,
    files: [], elements: [], appended: [], classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, removeAttribute() {},
    getAttribute: () => null, addEventListener() {}, append(child) { this.appended.push(child); }, querySelector: () => null,
    querySelectorAll: () => [], focus() {} });
  const document = { getElementById: (id) => { if (!els.has(id)) els.set(id, mk(id)); return els.get(id); },
    querySelectorAll: () => [], querySelector: () => null, createElement: () => mk(''), addEventListener() {}, removeEventListener() {},
    body: { dataset: {} } };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document, sessionStorage: store, localStorage: store, window: { scrollY: 0 }, location: { hash: '' },
    history: { replaceState() {} }, console, setTimeout, clearTimeout, URLSearchParams, AbortController, CSS: { escape: (s) => s },
    fetch: async () => { throw new Error('no network in tests'); } };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  }
  return { ctx, els, run: (code) => vm.runInContext(code, ctx) };
}
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

test('UX-15 Processing: New run and Runs come before the input inventory', async () => {
  const o = loadOperator();
  o.ctx.api = async (p) => {
    if (p === '/api/v1/processing/sources') return { feed_sources: [], registered_inputs: [1, 2, 3].map((n) => ({ show_title: `Show ${n}`, episodes: n })), uploads: {} };
    if (p.startsWith('/api/v1/processing/runs')) return { runs: [] };
    throw new Error('unexpected ' + p);
  };
  o.run('App').controller = new AbortController();
  await o.run('Views').processing([]);
  const html = o.els.get('view').innerHTML;
  const at = (s) => { const i = html.indexOf(s); assert.ok(i >= 0, `missing ${s}`); return i; };
  assert.ok(at('<h2>New run</h2>') < at('id="proc-runs"'));
  assert.ok(at('id="proc-runs"') < at('<h2>Add sources</h2>'));
  assert.ok(at('<h2>Add sources</h2>') < at('<h2>Registered inputs · 3 shows</h2>'));
  for (const id of ['rss-form', 'audio-upload', 'audio-file']) assert.ok(at(`id="${id}"`) > at('<h2>Add sources</h2>'));
  assert.doesNotMatch(html, /selectable inputs/);
  assert.match(html, /Shows already imported are listed under Registered inputs\./);
  assert.match(html, /<tr><td>Show 3<\/td><td class="num">3<\/td><\/tr>/, 'every inventory row is still listed');
});

function dataPage() {
  const o = loadOperator();
  const jumps = [];
  o.ctx.api = async (p) => {
    if (p === '/api/v1/overview') return { import_state: [{ id: 1, source_key: 'a', version_no: 1, is_active: true, sha256: 'x' }, { id: 2, source_key: 'b', version_no: 1, is_active: false, sha256: 'y' }] };
    if (p === '/api/v1/settings') return { settings: { playback_speed: 1, page_density: 'comfortable', revision: 0 } };
    if (p === '/api/v1/audit?limit=20') return { audit: [] };
    if (p === '/api/v1/processing/sources') return { feed_sources: [], registered_inputs: [], uploads: {} };
    if (p === '/api/v1/processing/runs?limit=5') return { runs: [] };
    throw new Error('unexpected ' + p);
  };
  o.ctx.wireAgentConnections = () => {}; o.ctx.wireHostedConnections = () => {};
  o.ctx.wireForm = () => {};
  const section = (id) => ({ id, scrolled: null, h2: { tabIndex: 0, focused: null, focus(opts) { this.focused = opts; } },
    scrollIntoView(opts) { this.scrolled = opts; }, querySelector(sel) { return sel === 'h2' ? this.h2 : null; } });
  const sections = new Map();
  o.ctx.document.getElementById = ((orig) => (id) => (id.startsWith('ds-') ? (sections.has(id) ? sections.get(id) : sections.set(id, section(id)).get(id)) : orig(id)))(o.ctx.document.getElementById);
  o.ctx.document.querySelectorAll = (sel) => {
    if (sel !== '[data-jump]') return [];
    const html = o.els.get('view').innerHTML;
    return [...html.matchAll(/data-jump="([^"]+)"/g)].map((m) => { let b = jumps.find((j) => j.dataset.jump === m[1]); if (!b) { b = { dataset: { jump: m[1] }, onclick: null }; jumps.push(b); } return b; });
  };
  o.ctx.window.matchMedia = () => ({ matches: false });
  o.run('App').controller = new AbortController();
  return { o, jumps, sections };
}

test('UX-15 Data & Settings has an in-page index of buttons; every target exists; source versions come last', async () => {
  const { o } = dataPage();
  await o.run('Views').data([]); await settle();
  const html = o.els.get('view').innerHTML;
  assert.match(html, /<nav class="op-index" aria-label="Sections on this page">/);
  const targets = [...html.matchAll(/data-jump="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(targets.length >= 8);
  for (const t of targets) assert.ok(html.includes(`<section class="panel" id="${t}">`), `section ${t} exists`);
  const panels = [...html.matchAll(/<section class="panel"[^>]*>/g)].map((m) => m[0]);
  assert.match(panels[panels.length - 1], /id="ds-source-versions"/);
  assert.match(html, /<h2>Source versions and fingerprints · 2<\/h2>/);
  assert.doesNotMatch(html, /href="#ds-/, 'anchors would fire the hash router');
  assert.equal([...html.matchAll(/data-source="/g)].length, 2, 'no rows removed');
});

test('UX-15 a Data & Settings index button scrolls to its section and focuses the heading', async () => {
  const { o, jumps, sections } = dataPage();
  await o.run('Views').data([]); await settle();
  const settings = jumps.find((j) => j.dataset.jump === 'ds-settings');
  assert.ok(settings && settings.onclick, 'the button is wired');
  settings.onclick();
  const s = sections.get('ds-settings');
  assert.equal(s.scrolled.block, 'start');
  assert.equal(s.scrolled.behavior, 'smooth');
  assert.equal(s.h2.tabIndex, -1);
  assert.deepEqual({ ...s.h2.focused }, { preventScroll: true });
});

test('UX-15 the index jump does not animate for reduced motion', async () => {
  const { o, jumps, sections } = dataPage();
  o.ctx.window.matchMedia = () => ({ matches: true });
  await o.run('Views').data([]); await settle();
  jumps.find((j) => j.dataset.jump === 'ds-backup').onclick();
  assert.equal(sections.get('ds-backup').scrolled.behavior, 'auto');
});
