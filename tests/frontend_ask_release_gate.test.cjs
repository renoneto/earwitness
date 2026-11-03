/* ASK-RELEASE-GATE (T11, chat doc section 10): Ask never ships without the
   Reno-approved public wording (PUBLIC-WORDING.md rows 30-35). App owns and
   pins the bytes once in ask_public_rows.cjs; this test only requires that
   module, so any release candidate carrying Ask code but not App's
   lr-app-public-ask branch fails here (module missing or a row absent). */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PUBLIC = path.join(__dirname, '..', 'app', 'public');

test('Ask release gate: App\'s approved Ask wording is present on the public pages', () => {
  const { ASK_PUBLIC_ROWS } = require('./ask_public_rows.cjs');
  assert.ok(Array.isArray(ASK_PUBLIC_ROWS) && ASK_PUBLIC_ROWS.length >= 6, 'ASK_PUBLIC_ROWS has rows 30-35');
  for (const [page, bytes] of ASK_PUBLIC_ROWS) {
    const html = fs.readFileSync(path.join(PUBLIC, page), 'utf8');
    assert.ok(html.includes(bytes), `${page} lacks approved Ask wording: ${bytes.slice(0, 60)}`);
  }
});

test('Ask release gate: the in-app Ask wording rows are present in the app files', () => {
  const { ASK_INAPP_ROWS } = require('./ask_public_rows.cjs');
  assert.ok(Array.isArray(ASK_INAPP_ROWS) && ASK_INAPP_ROWS.length > 0, 'ASK_INAPP_ROWS is a non-empty array');
  for (const [file, bytes] of ASK_INAPP_ROWS) {
    const text = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
    assert.ok(text.includes(bytes), `${file} lacks approved in-app Ask wording: ${bytes.slice(0, 60)}`);
  }
});

// Approved after the shared pins file was written (LEDGER-LEAD3-ASK-5); App owns
// earwitness/app/public_rows/public_rows.json, so this row is pinned here until it lands there.
test('Ask release gate: the one-claim sentence (LEDGER-LEAD3-ASK-5) is in views.js', () => {
  const text = fs.readFileSync(path.join(__dirname, '..', 'app', 'views.js'), 'utf8');
  assert.ok(text.includes("claim removed because its passage didn't support it"));
});
