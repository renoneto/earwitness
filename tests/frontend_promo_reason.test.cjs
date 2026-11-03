/* ADRULES (ADA-R2-01): self-promo reasons written by the ad rules carry a
   rule-set prefix ("R1:own-name:…", "R1:own-call:…"). Pages say them in
   words like the pipeline's own reasons, never as raw codes.
   Run: node --test tests/frontend_promo_reason.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function ui() {
  const sandbox = { console, document: { querySelector: () => null, querySelectorAll: () => [] } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'app', 'ui.js'), 'utf8'), sandbox, { filename: 'ui.js' });
  return sandbox.window.EWUI;
}

test('pipeline reasons stay as they were', () => {
  const u = ui();
  assert.equal(u.selfPromoReason('own-name:the telepathy tapes'), 'Names the show itself (the telepathy tapes)');
  assert.equal(u.selfPromoReason('phrase:at patreon'), 'Says “at patreon”');
  assert.equal(u.selfPromoReason('something odd'), 'something odd');
});

test('ADA-R2-01: ad-rule reasons with an R1 prefix read in words, never as codes', () => {
  const u = ui();
  assert.equal(u.selfPromoReason('R1:own-name:the telepathy tapes'), 'Names the show itself (the telepathy tapes)');
  assert.equal(u.selfPromoReason('R1:phrase:at patreon'), 'Says “at patreon”');
  assert.equal(u.selfPromoReason('R1:own-domain:telepathytapes.com'), "Points to the show's own website (telepathytapes.com)");
  assert.equal(u.selfPromoReason('R1:own-call:leave us a review'), 'Says “leave us a review”');
  assert.equal(u.selfPromoReason('own-call:our patreon'), 'Says “our patreon”');
  for (const raw of ['R1:own-name:x', 'R1:own-call:subscribe to the show', 'R1:phrase:my book']) {
    assert.doesNotMatch(u.selfPromoReason(raw), /^R1:|own-|:/, raw);
  }
});
