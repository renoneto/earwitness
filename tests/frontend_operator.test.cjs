/* Operator Ad review page checks (admin review fixes, 2026-10-11).
   Loads the real operator scripts in a stubbed sandbox and checks the
   page's pure decisions: which key does what (never while typing), that
   a rule drops every prefetched read and the queue, and what the page
   says about a stream that ends before the read.
   Run: node --test tests/frontend_operator.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(names = '{ adReviewKey, adReviewAfterSave, adReviewAudioNote, adReviewBody, adReviewAskWords, AdReview }') {
  const listeners = [];
  const el = () => ({ textContent: '', innerHTML: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, removeAttribute() {}, addEventListener() {} });
  const document = { getElementById: el, querySelectorAll: () => [], querySelector: () => null,
    addEventListener: (type, fn) => listeners.push([type, fn]), removeEventListener() {} };
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
const typingIn = tag => ({ closest: sel => (sel.includes(tag) ? {} : null) });
const onPage = { closest: () => null };

test('review keys map to actions, and never fire while typing', () => {
  const { adReviewKey } = load();
  assert.equal(adReviewKey({ key: 'G', shiftKey: true, target: onPage }), 'generic');
  assert.equal(adReviewKey({ key: 'g', target: onPage }), null, 'rule keys need Shift');
  assert.equal(adReviewKey({ key: 'C', target: onPage }), 'confirm');
  assert.equal(adReviewKey({ key: 'A', shiftKey: true, target: onPage }), 'alias');
  assert.equal(adReviewKey({ key: 'a', target: onPage }), null, 'rule keys need Shift');
  assert.equal(adReviewKey({ key: '3', target: onPage }), 'pick:3');
  assert.equal(adReviewKey({ key: ' ', target: onPage }), 'play');
  assert.equal(adReviewKey({ key: 'Escape', target: onPage }), 'cancel');
  assert.equal(adReviewKey({ key: 'g', target: typingIn('input') }), null, 'typing a brand never stoplists');
  assert.equal(adReviewKey({ key: 'c', target: typingIn('textarea') }), null);
  assert.equal(adReviewKey({ key: 'n', target: typingIn('select') }), null);
  assert.equal(adReviewKey({ key: 'g', ctrlKey: true, target: onPage }), null);
  assert.equal(adReviewKey({ key: 'x', target: onPage }), null);
});

test('a rule drops every prefetched read and the queue; a one-read action keeps them', () => {
  const { adReviewAfterSave, AdReview } = load();
  AdReview.items = [{ evidence_id: 'a' }, { evidence_id: 'b' }]; AdReview.index = 1;
  AdReview.cache.set('b', Promise.resolve({ read: { brand: { key: 'ross' } } }));
  assert.equal(adReviewAfterSave({ corpus_changed: false, rules: [] }), false);
  assert.equal(AdReview.cache.size, 1);
  assert.equal(adReviewAfterSave({ corpus_changed: true, rules: [{ kind: 'stoplist', word: 'ross' }] }), true);
  assert.equal(AdReview.cache.size, 0, 'the prefetched read is gone, so C cannot confirm a stale brand');
  assert.equal(AdReview.items.length, 0, 'the queue reloads');
  assert.equal(AdReview.index, 0);
});

test('a stream that ends before the read says so instead of playing', () => {
  const { adReviewAudioNote } = load();
  assert.match(adReviewAudioNote(1530, 1740), /ends at 25:30, before the read starts/);
  assert.match(adReviewAudioNote(Infinity, 60), /length is unknown/);
  assert.match(adReviewAudioNote(NaN, 60), /length is unknown/);
  assert.equal(adReviewAudioNote(3600, 60), null);
});

test('actions on the heard-as spelling send the spelling on screen', () => {
  const { adReviewBody } = load();
  const item = { raw_brand: 'ross' };
  for (const action of ['type', 'alias', 'self_promo', 'generic']) {
    assert.equal(adReviewBody({ action }, item).heard, 'ross', action);
  }
  for (const action of ['confirm', 'pick', 'not_ad']) {
    assert.equal('heard' in adReviewBody({ action }, item), false, action);
  }
  assert.equal(adReviewBody({ action: 'type' }, { raw_brand: null }).heard, '');
});

test('Shift+Enter on a typed brand (every read) asks with the count', () => {
  const { adReviewAskWords } = load();
  // Decision 14: plain Enter saves this read only, so the rename prompt is
  // the alias one (Shift+A or Shift+Enter in the field).
  assert.match(adReviewAskWords('alias', { raw: 'state farm', reads: 110 }, 'Geico'), /Press Shift\+A again \(or Shift\+Enter in the brand field\) to show every read heard as “state farm” as Geico/);
  assert.match(adReviewAskWords('alias', { raw: 'shopify', reads: 3, known_brand: true }, 'Wix'), /“shopify” is a known brand/);
  assert.match(adReviewAskWords('alias', { raw: 'way fair', reads: 1 }, 'Wayfair'), /1 read\. Press Shift\+A again/);
  assert.match(adReviewAskWords('generic', { word: 'ross', reads: 2 }), /Press Shift\+G again to make “ross” a generic word/);
});

/* Option A (2026-10-13): the namer's product / network next to the brand. */
const loadOptionA = () => load('{ brandLabel, brandChoiceMarkup, adReviewItemMarkup }');

test('the operator brand label adds product and network, and nothing when absent', () => {
  const { brandLabel } = loadOptionA();
  assert.equal(brandLabel({ display: 'Apple', product: 'Apple Watch' }), 'Apple · Apple Watch');
  assert.equal(brandLabel({ display: 'Pivot', network: 'Vox Media | New York Magazine' }), 'Pivot · Vox Media | New York Magazine');
  assert.equal(brandLabel({ display: 'Apple', product: 'Apple Watch', network: 'Vox' }), 'Apple · Apple Watch · Vox');
  assert.equal(brandLabel({ display: 'Apple' }), 'Apple');
  assert.equal(brandLabel({}), '');
});

test('the evidence brand panel and the review page show the label, escaped; actions keep the brand', () => {
  const { brandChoiceMarkup, adReviewItemMarkup } = loadOptionA();
  const brand = { key: 'apple', display: 'Apple', product: 'Apple <Watch>', network: 'A & B' };
  const panel = brandChoiceMarkup({ brand, candidates: [] }, 1);
  assert.match(panel, /<p>Apple · Apple &lt;Watch&gt; · A &amp; B /);
  assert.ok(!panel.includes('<Watch>'));
  assert.match(brandChoiceMarkup({ brand: { key: 'apple', display: 'Apple' }, candidates: [] }, 1), /<p>Apple /);
  const item = { read: { brand, candidates: [] }, transcript: [], reports: [], span: [0, 60], raw_brand: 'apple' };
  const page = adReviewItemMarkup(item, 1);
  assert.match(page, /Guessed name \(operators only\)<\/span><strong>Apple · Apple &lt;Watch&gt; · A &amp; B<\/strong>/);
  assert.match(page, /data-brand="apple"/, 'the read keeps its brand key');
  assert.match(page, />Confirm Apple <kbd>C<\/kbd><\/button>/, 'confirm names the brand only');
  const plain = adReviewItemMarkup({ ...item, read: { brand: { key: 'apple', display: 'Apple' }, candidates: [] } }, 1);
  assert.match(plain, /Guessed name \(operators only\)<\/span><strong>Apple<\/strong>/);
});
