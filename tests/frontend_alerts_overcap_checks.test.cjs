/* Recent checks over the cap (A2 follow-up): a done check with over_cap > 0
   repeats the existing over-cap line in its Recent checks row
   (ALERT-VIEW.md), and the alert is not shown as quiet.
   Run: node --test tests/frontend_alerts_overcap_checks.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'alert-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));

function views() {
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: '#/overview' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async url => {
      if (url !== '/api/v1/session') throw new Error('no network in unit test');
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
        frontend_contract: 'earwitness/customer-ui/2' }) };
    },
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
  sandbox.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  sandbox.EW.current.meaningAlertsAvailable = true;
  return sandbox.EWViews;
}
const plain = html => html.replace(/<[^>]*>/g, ' ').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/\s+/g, ' ');
const OVER = n => `Checked the newest 100 of ${n} passages. The rest were not checked.`;

test('fixture: the newest check ran over the cap', () => {
  const a = fixture('alert-overcap');
  assert.equal(a.checks[0].state, 'done');
  assert.equal(a.checks[0].kind, 'check');
  assert.ok(a.checks[0].over_cap > 0);
  assert.equal(a.checks[1].over_cap, 0);
});

test('Recent checks: the over-cap check row repeats the over-cap line, the other row does not', () => {
  const html = views().meaningAlertHtml(fixture('alert-overcap'));
  const section = /<section class="ew-card ew-card-body meaning-checks"><h3>Recent checks<\/h3><ul>([\s\S]*?)<\/ul><\/section>/.exec(html);
  assert.ok(section, 'the Recent checks section renders');
  const rows = section[1].split('<li class="meaning-check').slice(1);
  assert.equal(rows.length, 2);
  assert.ok(plain(rows[0]).includes(OVER(140)), rows[0]);
  assert.ok(!plain(rows[1]).includes('The rest were not checked.'), rows[1]);
  assert.ok(plain(rows[0]).includes('100 passages checked in 5 batches'), 'the disclosure counts what was sent');
});

test('an alert whose last check ran over the cap is not shown as quiet', () => {
  const text = plain(views().meaningAlertHtml(fixture('alert-overcap')));
  assert.ok(!/No new matches/.test(text), text);
});
