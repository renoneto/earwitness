/* Brand curation: the operator Rules panel marks a brand the curation step
   added automatically (export actor "brand-curate"); a brand a person added
   shows as before. Same loader as frontend_dv3_g6.test.cjs.
   Run: node --test tests/frontend_brand_curate.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(names) {
  const el = () => ({ textContent: '', innerHTML: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, removeAttribute() {}, addEventListener() {} });
  const document = { getElementById: el, querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, removeEventListener() {} };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document, sessionStorage: store, localStorage: store, window: {}, location: { hash: '' },
    history: { replaceState() {} }, console, setTimeout, clearTimeout, URLSearchParams, AbortController,
    fetch: async () => { throw new Error('no network in tests'); } };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  }
  return vm.runInContext(`(${names})`, ctx);
}
const rules = (new_brands, rule_ids = {}) => ({ rules: { brand_aliases: [], stoplist: [], self_promo: [], not_ads: [], new_brands, labels: 0, rule_ids } });

test('a brand added by the curation step says "added automatically"; a person\'s brand does not', () => {
  const { adReviewRulesMarkup } = load('{ adReviewRulesMarkup }');
  const html = adReviewRulesMarkup(rules([
    { brand: 'att', display: 'AT&T', actor: 'brand-curate' },
    { brand: 'quince', display: 'Quince', actor: 'Reno' },
    { brand: 'wix', display: 'Wix', actor: 'owner' }], { 'brand\tatt': 4, 'brand\tquince': 5, 'brand\twix': 6 }));
  assert.match(html, /<li><span>AT&amp;T[^<]*<span class="meta"> · added automatically<\/span><\/span><button type="button" class="ar-remove" data-rule="4">Remove<\/button><\/li>/);
  assert.match(html, /<li><span>Quince<\/span><button type="button" class="ar-remove" data-rule="5">Remove<\/button><\/li>/);
  assert.match(html, /<li><span>Wix<\/span><button type="button" class="ar-remove" data-rule="6">Remove<\/button><\/li>/);
  assert.equal((html.match(/added automatically/g) || []).length, 1);
  assert.doesNotMatch(html, /style=/);
});
