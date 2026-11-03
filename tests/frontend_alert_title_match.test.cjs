/* ALERT-TITLE-MATCH (lead decision): the Ads search tab keeps ads matched
   only on the show or episode title, and says so on those rows with
   "Matched title" (EW.basisLine). Rows matched on the ad's own text
   (passage or mixed) show nothing new, and other tabs are unchanged.
   Run: node --test tests/frontend_alert_title_match.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function load() {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: '#/search' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async () => { throw new Error('no network in unit test'); },
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
  sandbox.window.EW.current.route = '#/search';
  return sandbox.window;
}

const ad = basis => ({
  record_kind: 'evidence', evidence_id: 'ev-' + basis, ref: 'ref-' + basis, status: 'finding',
  show_title: 'Tech Talk', ep_title: 'Mark Zuckerberg on Muse', pub_date: '2026-10-01',
  text: 'Try daily greens today', match_basis: basis,
  read: { brand: { display: 'AG1', key: 'ag1', confirmed: true }, display_span: [10, 40] },
});

test('Ads cards: a title-only row says "Matched title"; own-text rows say nothing new', () => {
  const ui = load().EWUI;
  const title = ui.sourceCard(ad('title'), { query: 'zuckerberg', mode: 'ads', index: 0 });
  assert.match(title, /<span class="match-reason">Matched title<\/span>/);
  assert.equal((title.match(/Matched title/g) || []).length, 1);
  for (const basis of ['passage', 'mixed', undefined]) {
    const own = ui.sourceCard(ad(basis), { query: 'greens', mode: 'ads', index: 0 });
    assert.doesNotMatch(own, /match-reason|Matched/, String(basis));
  }
});

test('Ads list rows: a title-only row says "Matched title"; own-text rows say nothing new', () => {
  const ui = load().EWUI;
  const title = ui.resultRow(ad('title'), { index: 0 });
  assert.match(title, /<span class="match-reason">Matched title<\/span>/);
  for (const basis of ['passage', 'mixed', undefined]) {
    assert.doesNotMatch(ui.resultRow(ad(basis), { index: 0 }), /match-reason|Matched/, String(basis));
  }
});

test('other tabs are unchanged: a mentions card never shows the label', () => {
  const ui = load().EWUI;
  assert.doesNotMatch(ui.sourceCard(ad('title'), { query: 'zuckerberg', mode: 'mentions' }), /Matched/);
  assert.doesNotMatch(ui.sourceCard({ ...ad('title'), record_kind: 'transcript_passage' }, { mode: 'mentions' }), /Matched/);
});

test('the label is the shared EW.basisLine wording, escaped', () => {
  const win = load();
  assert.equal(win.EW.basisLine({ match_basis: 'title' }), '<span class="match-reason">Matched title</span>');
  assert.match(win.EWUI.sourceCard(ad('title'), { mode: 'ads' }), new RegExp(win.EW.basisLine({ match_basis: 'title' })));
});

test('phone List view: the label gets its own line, not cut off after the episode', () => {
  // Below 900 px the Show · Episode line is one ellipsized line; an inline
  // label after it was clipped (shots at 390). It must sit on its own line.
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const at = css.indexOf('@media (max-width: 899px)');
  assert.ok(at >= 0);
  let depth = 0, end = css.indexOf('{', at);
  for (let j = end; j < css.length; j++) { if (css[j] === '{') depth++; else if (css[j] === '}' && !--depth) { end = j; break; } }
  const phone = css.slice(at, end);
  assert.match(phone, /\.ew-row-where \.match-reason\s*\{[^}]*display:\s*block/);
});
