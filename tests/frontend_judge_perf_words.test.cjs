/* PERF brief, Sol DEMO2 r1 finding 03: the search words are marked and
   located as whole words, as the server matches them, so "Meta" never
   marks "meta" inside "metallurgy" or opens a snippet at "metal".
   Run: node --test tests/frontend_judge_perf_words.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function load() {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: '#/overview' }, history: { replaceState() {} }, scrollTo() {},
    fetch: async () => { throw new Error('no network'); },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
      createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t },
    addEventListener() {},
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(APP, 'app.js'), 'utf8'), sandbox, { filename: 'app.js' });
  return sandbox.EW;
}

test('highlight marks whole words only', () => {
  const E = load();
  const out = E.highlight("Heavy metal, metallurgy and Meta's Muse at the museum", 'Meta Muse');
  assert.equal(out, 'Heavy metal, metallurgy and <mark>Meta</mark>&#39;s <mark>Muse</mark> at the museum');
  assert.doesNotMatch(out, /<mark>meta<\/mark>l/i);
  assert.doesNotMatch(out, /<mark>muse<\/mark>um/i);
});

test('highlight keeps phrases and non-word edges', () => {
  const E = load();
  assert.match(E.highlight("Try Meta's Muse today", '"Meta\'s Muse"'), /<mark>Meta(&#39;|')s Muse<\/mark>/);
  assert.match(E.highlight('Use code $5off now', '$5off'), /<mark>\$5off<\/mark>/);
});

test('snippet opens at the first whole-word match', () => {
  const E = load();
  const text = 'Heavy metal ' + 'x '.repeat(200) + 'and then Meta announced a model ' + 'y '.repeat(200);
  const out = E.snippet(text, 'Meta', 120);
  assert.match(out, /Meta announced/);
});
