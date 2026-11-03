/* Design v3 G6: the operator Ad review page in the dark "OPERATOR · INTERNAL"
   look. Loads the real operator scripts in a stubbed sandbox (same loader as
   frontend_operator.test.cjs) and checks the rendered markup: facts only from
   the API, the member report header, one-key controls, the empty state, the
   rules list, and no inline styles (CSP style-src 'self').
   Run: node --test tests/frontend_dv3_g6.test.cjs */
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
const ITEM = {
  evidence_id: 'e1', show_title: 'The Daily', ep_title: 'Ep <1>', pub_date: '2026-10-08', span: [1893.12, 1920],
  raw_brand: 'youtube premium',
  read: { brand: { key: 'youtube premium', display: 'YouTube Premium', confirmed: false }, confidence: 'low', confidence_score: 0.704,
    candidates: [{ brand: 'youtube premium', score: 5.066, evidence: ['gliner:website'] }, { brand: 'offline <downloads>', score: 0.278, evidence: [] }] },
  transcript: [{ start: 1888.5, end: 1893.1, text: 'Before.', in_read: false }, { start: 1893.12, end: 1900, text: 'Try <it>.', in_read: true }],
  reports: [{ id: 1, reason: 'wrong_brand', note: 'Not <this> brand', created_at: '2026-10-14T16:41:02Z', status: 'open' }],
  media: { kind: 'local', src: '/media/x.mp3', label: 'Local file', source_offset_s: 0 },
};

test('G6 the read shows a member report header, the facts the API gives, and no inline styles', () => {
  const { adReviewItemMarkup, AdReview } = load('{ adReviewItemMarkup, AdReview }');
  AdReview.items = [{ evidence_id: 'e1', brand: 'youtube premium', reasons: ['report:wrong_brand'], reports: 1, brand_frequency: 37 }];
  AdReview.index = 0;
  const html = adReviewItemMarkup(ITEM, 1);
  assert.doesNotMatch(html, /style=/, 'CSP: no inline styles');
  assert.match(html, /<b>Member report · Wrong brand<\/b>/);
  assert.match(html, /<q>Not &lt;this&gt; brand<\/q>/, 'the member note is escaped');
  assert.match(html, /Guessed name \(operators only\)<\/span><strong>YouTube Premium<\/strong>/);
  assert.match(html, /Spoken name<\/span><strong class="ar-mono">“youtube premium”<\/strong>/);
  assert.match(html, /Reads carrying this brand<\/span><strong>37<\/strong>/);
  assert.match(html, /Namer's confidence<\/span><strong>low \(0\.70\)<\/strong>/);
  assert.doesNotMatch(html, /Web address/, 'the operator API has no web address: no row, nothing invented');
  assert.match(html, /<span class="ar-clock" id="ar-clock">31:33<\/span>/);
  assert.match(html, /31:33 → 32:00/);
  assert.match(html, /<li class="in-read"><time>31:33<\/time><span>Try &lt;it&gt;\.<\/span><\/li>/);
  assert.match(html, /class="ar-cand is-top" data-ar-pick="1" data-brand="youtube premium"><kbd>1<\/kbd>/);
  assert.match(html, /current guess · score 5\.07 · gliner:website/);
  assert.match(html, /<b>offline &lt;downloads&gt;<\/b>/);
  assert.match(html, /Type a brand <kbd>T<\/kbd> — keys are off while typing/);
  assert.match(html, /<button type="submit" class="primary">Save as self-promotion<\/button>/);
  assert.match(html, /id="ar-promo-cancel">Cancel <kbd>Esc<\/kbd><\/button>/);
  for (const [action, key] of [['confirm', 'C'], ['type', 'T'], ['alias', 'Shift\\+A'], ['not_ad', 'N'], ['self_promo', 'P'], ['generic', 'Shift\\+G'], ['skip', 'S / →'], ['play', 'Space'], ['replay', 'R'], ['cancel', 'Esc']]) {
    assert.match(html, new RegExp(`data-ar="${action}"[^>]*>[^<]*(<[^k][^>]*>[^<]*)*<kbd>${key}</kbd>`), action);
  }
  assert.match(html, /Rename every “youtube premium” read <kbd>Shift\+A<\/kbd>/);
  assert.match(html, /id="ar-rules"/, 'the rules sit in the decide column');
});

test('G6 without audio, facts or reports the page says so and shows no player controls', () => {
  const { adReviewItemMarkup, AdReview } = load('{ adReviewItemMarkup, AdReview }');
  AdReview.items = []; AdReview.index = 0;
  const html = adReviewItemMarkup({ ...ITEM, media: null, reports: [], raw_brand: null,
    read: { brand: {}, candidates: [] } }, 1);
  assert.doesNotMatch(html, /Member report/);
  assert.doesNotMatch(html, /data-ar="play"|data-ar="replay"|id="ar-audio"/);
  assert.match(html, /No audio for this episode\. Judge the read on the transcript\./);
  assert.match(html, /Guessed name \(operators only\)<\/span><strong>Not named<\/strong>/);
  assert.doesNotMatch(html, /Spoken name|Reads carrying|confidence<\/span>/);
  assert.match(html, /The namer listed no candidates\./);
  assert.match(html, /data-ar="confirm" disabled>Confirm shown brand <kbd>C<\/kbd>/);
  assert.match(html, /data-ar="alias" disabled>Rename every read <kbd>Shift\+A<\/kbd>/);
});

test('G6 queue line counts what the queue returned; the empty state names the filters', () => {
  const { adReviewQueueLine, adReviewEmptyMarkup, AdReview } = load('{ adReviewQueueLine, adReviewEmptyMarkup, AdReview }');
  AdReview.items = []; AdReview.total = 0;
  assert.equal(adReviewQueueLine(1), 'Queue empty');
  AdReview.items = [{ reports: 1 }, { reports: 0 }, { reports: 2 }]; AdReview.total = 3;
  assert.equal(adReviewQueueLine(1), 'Read 1 of 3 waiting · 2 member reports');
  AdReview.items = [{ reports: 1 }]; AdReview.total = 40;
  assert.equal(adReviewQueueLine(1), 'Read 1 of 1 · 40 waiting in all · 1 member report');
  AdReview.filter = { show: '', brand: '', reason: '' };
  const empty = adReviewEmptyMarkup();
  assert.match(empty, /<h2 class="ar-empty-title">Nothing waits for review\.<\/h2>/);
  assert.match(empty, /Filters: all shows, all brands, all reasons\./);
  AdReview.filter = { show: 'The <Daily>', brand: 'wix', reason: 'report' };
  const filtered = adReviewEmptyMarkup();
  assert.match(filtered, /Nothing waits for review with these filters\./);
  assert.match(filtered, /Filters: The &lt;Daily&gt;, brand “wix”, Member reports\./);
  assert.doesNotMatch(empty + filtered, /style=/);
});

test('G6 rules list: one row per rule with its Remove control; empty kinds hidden', () => {
  const { adReviewRulesMarkup } = load('{ adReviewRulesMarkup }');
  const none = adReviewRulesMarkup({ rules: { brand_aliases: [], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 0, rule_ids: {} } });
  assert.match(none, /<details class="ar-rules"><summary>Rules so far · 0/);
  assert.match(none, /No rules yet\./);
  assert.doesNotMatch(none, /<h3>/);
  const some = adReviewRulesMarkup({ rules: { brand_aliases: [{ raw: 'applecard', display: 'Apple <Card>' }], stoplist: [], self_promo: [], not_ads: [], new_brands: [],
    labels: 2, rule_ids: { 'alias\tapplecard': 7 } } });
  assert.match(some, /Rules so far · 1/);
  assert.match(some, /· 2 labeled examples</);
  const one = adReviewRulesMarkup({ rules: { brand_aliases: [], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: {} } });
  assert.match(one, /· 1 labeled example</);
  assert.match(some, /<h3>Brand aliases<\/h3>/);
  assert.match(some, /<li><span>“applecard” → Apple &lt;Card&gt;<\/span><button type="button" class="ar-remove" data-rule="7">Remove<\/button><\/li>/);
  assert.doesNotMatch(some, /Generic words/);
  assert.match(some, /corrections export/);
});
