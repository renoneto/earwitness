/* d16 row 1: the shared customer count helper. Customers see "N ads";
   with no ad count from the server, no number (never a read count
   dressed as ads). Run: node --test tests/frontend_uat_d1_helper.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
function load() {
  const sandbox = {
    console, URLSearchParams, AbortController, URL, setTimeout: () => 0, clearTimeout() {},
    location: { hash: '#/settings', href: 'http://127.0.0.1:9160/app/#/settings' },
    history: { replaceState() {} }, scrollTo() {},
    fetch: async () => { throw new Error('no network in unit test'); },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => ({ innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } }), querySelectorAll: () => [], addEventListener() {},
      createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t },
    addEventListener() {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  return sandbox.window.EWViews;
}

test('row 1 adCount: "1 ad", "12 ads", thousands separators', () => {
  const { adCount } = load();
  assert.equal(adCount(1), '1 ad');
  assert.equal(adCount(12), '12 ads');
  assert.equal(adCount(1234), '1,234 ads');
  assert.equal(adCount(0), '0 ads');
});

test('row 1 adCount: a lower bound says "at least N ads"', () => {
  const { adCount } = load();
  assert.equal(adCount(50, 'at least '), 'at least 50 ads');
});

test('row 1 adCount: no ad count from the server gives no number', () => {
  const { adCount } = load();
  for (const v of [null, undefined, '', 'x', NaN]) assert.equal(adCount(v), '');
});
