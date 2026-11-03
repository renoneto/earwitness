/* DEMO2 pass 5 (Sol R1-09, rehearsal r2): a saved excerpt shows the
   passage it saved (captured.selected_bounds), never the whole record its
   ref resolves to (effective/original bounds: the whole episode for a
   transcript passage). Loads the real app scripts in a stubbed sandbox.
   Run: node --test tests/frontend_demo2_pass5_saved.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function load(route) {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl,
    location: { hash: route }, history: { replaceState() {} }, scrollTo() {},
    fetch: async () => { throw new Error('no network in unit test'); },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
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

async function excerptsHtml(captured) {
  const win = load('#/saved?tab=excerpts');
  const items = [{ id: 'si-muse', type: 'excerpt', title: 'Newest Meta\'s Muse mention', revision: 1,
    availability: 'current', target: { ref: 'sr1:p', start_s: 854.84, end_s: 857.84 }, captured }];
  const api = async () => ({ items, total: 1, total_relation: 'exact', counts: { excerpt: 1, query: 0, bookmark: 0 }, next_cursor: null });
  return win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
}

// The rehearsal r2 item: the passage 14:14.84-14:17.84 of a 1:38:42 episode.
const CAPTURED = {
  text: "Meta's Muse tops 5 billion downloads,", source_ref: 'sr1:p',
  labels: { show_title: 'Last Week in AI', ep_title: '#260 - Muse, Misalignment, Math', pub_date: '2026-10-19' },
  original_bounds: { start_s: 0, end_s: 5922 }, effective_bounds: { start_s: 0, end_s: 5922 },
  selected_bounds: { start_s: 854.84, end_s: 857.84 },
};

test('R1-09 a saved excerpt row shows its saved passage, not the whole episode', async () => {
  const html = await excerptsHtml(CAPTURED);
  assert.doesNotMatch(html, /1:38:42/, 'the row shows the episode bounds');
  assert.doesNotMatch(html, />0:00</, 'the row starts at the episode start');
  assert.match(html, /14:15–14:18/, 'the row shows the saved passage (rounded seconds)');
});

test('R1-09 an excerpt saved before selected bounds were captured keeps its record bounds', async () => {
  const { selected_bounds: _drop, ...older } = CAPTURED;
  const html = await excerptsHtml({ ...older, effective_bounds: { start_s: 60, end_s: 120 } });
  assert.match(html, /1:00/);
  assert.match(html, /2:00/);
});
