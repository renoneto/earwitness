/* Brand suggestions: the operator Ad review panel "Suggested brands"
   (id ar-suggest, above the Rules panel). One row per stored suggestion:
   the heard name, "Name not confirmed", what is suggested, Confirm / Not
   this / Undo. Confirm and Undo that need the count show it inline and send
   it back (the Rules panel Remove flow). Imported strings are escaped.
   Page harness: the loader of frontend_brand_curate.test.cjs plus a host
   element for the panel and a recording fetch.
   Run: node --test tests/frontend_brand_suggest.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page({ respond = () => null, list = [] } = {}) {
  const posts = [], gets = [];
  let html = '', buttons = [];
  const attr = (tag, name) => { const m = tag.match(new RegExp(`\\sdata-${name}="([^"]*)"`)); return m ? m[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&') : undefined; };
  const parse = (h) => [...String(h).matchAll(/<button\b[^>]*>([^<]*)<\/button>/g)].map((m) => {
    const tag = m[0].match(/<button\b[^>]*>/)[0];
    const b = { tag, label: m[1], disabled: /\sdisabled(\s|>|=)/.test(tag), dataset: { sg: attr(tag, 'sg'), id: attr(tag, 'id'), verdict: attr(tag, 'verdict'), company: attr(tag, 'company'), heardCorrect: attr(tag, 'heard-correct'), fetchDate: attr(tag, 'fetch-date'), status: attr(tag, 'status') } };
    b.click = () => b.onclick && b.onclick();
    return b;
  });
  const host = { id: 'ar-suggest', get innerHTML() { return html; }, set innerHTML(v) { html = v; buttons = parse(v); },
    querySelectorAll: (sel) => (sel === 'button[data-sg]' ? buttons.filter((b) => b.dataset.sg) : []) };
  const stub = () => ({ textContent: '', innerHTML: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, removeAttribute() {}, addEventListener() {} });
  const document = { getElementById: (id) => (id === 'ar-suggest' ? host : stub()), querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, removeEventListener() {} };
  const json = (status, body) => ({ ok: status < 400, status, json: async () => body });
  let current = list;
  const fetch = async (url, init = {}) => {
    if (init.method === 'POST') {
      const body = init.body ? JSON.parse(init.body) : null;
      posts.push({ url, body });
      const out = respond(url, body, posts.length);
      if (out) return json(out.status, out.body);
      return json(200, { ok: true, state: 'confirmed', reads: 0 });
    }
    if (url === '/api/v1/admin/brand-suggestions') { gets.push(url); return json(200, { ok: true, suggestions: current }); }
    throw new Error(`unexpected ${url}`);
  };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document, sessionStorage: store, localStorage: store, window: {}, location: { hash: '' },
    history: { replaceState() {} }, console, setTimeout, clearTimeout, URLSearchParams, AbortController, fetch };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  }
  const api = vm.runInContext('({ App, AdReview, adReviewSuggestMarkup, loadAdReviewSuggest })', ctx);
  api.App.route = 'ad-review'; api.App.csrf = 't';
  const reloads = [];
  const setRender = (fn) => { ctx.render = fn; };
  api.AdReview.reloadRules = async (opts) => { reloads.push(opts); return true; };
  return { ...api, host, posts, gets, reloads, setRender, setList: (l) => { current = l; },
    button: (action, id) => buttons.find((b) => b.dataset.sg === action && (id === undefined || b.dataset.id === String(id))) };
}

const S = (over = {}) => ({ id: 1, key: 'century', state: 'pending', status: 'pending', verdict: 'brand', company: 'Sentry',
  official_domain: 'sentry.io', heard_correct: false, fetch_date: '2026-10-18T00:00:00+00:00', confidence: 0.92, evidence: 'Sentry is an error tracking company.',
  self_promo: null, ads: 2, shows: ['Show A', 'Show B'], reads_not_saying: 0, excerpt: 'sponsored by Century dot io', rules: [],
  decided_by: null, ...over });
const FD = '2026-10-18T00:00:00+00:00';
const markup = (items) => page().adReviewSuggestMarkup({ suggestions: items });

test('a pending brand: heard name, badge, suggestion with domain, misheard, evidence and both actions', () => {
  const html = markup([S()]);
  assert.match(html, /id="ar-suggest-h"|Suggested brands/);
  assert.match(html, /“century”/);
  assert.match(html, /<span class="badge report">Name not confirmed<\/span>/);
  assert.match(html, /heard in 2 ads when asked · Show A, Show B/);
  assert.match(html, /sponsored by Century dot io/);
  assert.match(html, /Suggested: <b>Sentry<\/b> \(sentry\.io\) · misheard/);
  assert.match(html, /Sentry is an error tracking company\./);
  assert.match(html, /<button type="button"[^>]*data-sg="confirm"[^>]*data-id="1"[^>]*data-verdict="brand"[^>]*data-company="Sentry"[^>]*>Confirm<\/button>/);
  assert.match(html, /<button type="button"[^>]*data-sg="dismiss"[^>]*>Not this<\/button>/);
  assert.doesNotMatch(html, /Gemini|agent/i);
  assert.doesNotMatch(html, /style=/);
});

test('a brand heard right without a domain: no parentheses and no "misheard"', () => {
  const html = markup([S({ key: 'vantrix', company: 'Vantrix', official_domain: null, heard_correct: true })]);
  assert.match(html, /Suggested: <b>Vantrix<\/b>(?! \()/);
  assert.doesNotMatch(html, /misheard/);
  assert.doesNotMatch(html, /\(null\)|\(\)/);
});

test('not a brand: Confirm and Not this', () => {
  const html = markup([S({ key: 'good', verdict: 'not_brand', company: null, heard_correct: null })]);
  assert.match(html, /Suggested: not a brand/);
  assert.match(html, /data-sg="confirm"[^>]*data-verdict="not_brand"/);
  assert.doesNotMatch(html, /data-company/);
  assert.match(html, /data-sg="dismiss"/);
});

test('unsure: "No clear suggestion", maybe the company, Not this only', () => {
  const html = markup([S({ verdict: 'unsure', confidence: 0.4 }), S({ id: 2, key: 'quantro', verdict: 'unsure', company: 'Quantro',
    self_promo: { reason: 'own-name:quantro', show: 'Quantro' } }), S({ id: 3, key: 'plain', verdict: 'unsure', company: null })]);
  assert.match(html, /No clear suggestion · maybe Sentry/);
  assert.match(html, /No clear suggestion · maybe Quantro · looks like the show's own promotion/);
  assert.match(html, /No clear suggestion<\/p>|No clear suggestion<\/span>|No clear suggestion(?! ·)/);
  assert.equal((html.match(/data-sg="confirm"/g) || []).length, 0);
  assert.equal((html.match(/data-sg="dismiss"/g) || []).length, 3);
});

test('resolved: no actions, "Already a known brand now"', () => {
  const html = markup([S({ state: 'resolved' })]);
  assert.match(html, /Already a known brand now/);
  assert.doesNotMatch(html, /<button/);
});

test('confirmed: who confirmed, what it made, Undo', () => {
  const alias = markup([S({ state: 'confirmed', status: 'confirmed', decided_by: 'Reno', rules: [['brand', 'sentry', 7], ['alias', 'century', 8]] })]);
  assert.match(alias, /Confirmed by Reno/);
  assert.match(alias, /Every read heard as “century” shows as Sentry/);
  assert.match(alias, /Sentry is on the brand list/);
  assert.match(alias, /data-sg="undo"[^>]*data-id="1"[^>]*>Undo<\/button>/);
  assert.doesNotMatch(alias, /data-sg="confirm"/);
  const word = markup([S({ key: 'good', state: 'confirmed', status: 'confirmed', verdict: 'not_brand', company: null, decided_by: 'Reno', rules: [['stoplist', 'good', 9]] })]);
  assert.match(word, /“good” is a generic word/);
});

test('dismissed: "Not this: left unconfirmed" and Undo', () => {
  const html = markup([S({ state: 'dismissed', status: 'dismissed' })]);
  assert.match(html, /Not this: left unconfirmed/);
  assert.match(html, /data-sg="undo"/);
  assert.doesNotMatch(html, /data-sg="confirm"|data-sg="dismiss"/);
});

test('reads that do not say the name: a muted count on the meta line', () => {
  assert.match(markup([S({ reads_not_saying: 1 })]), /heard in 2 ads when asked · Show A, Show B · 1 more read does not say it/);
  assert.match(markup([S({ reads_not_saying: 3 })]), /3 more reads do not say it/);
});

test('imported strings are escaped', () => {
  const html = markup([S({ key: '<img src=x onerror=1>', company: '"><script>alert(1)</script>', official_domain: '<b>x</b>', evidence: '<i>e</i>',
    shows: ['<u>show</u>'], excerpt: '<s>ex</s>' })]);
  assert.doesNotMatch(html, /<img|<script|<b>x|<i>e|<u>show|<s>ex/);
  assert.match(html, /&lt;img src=x onerror=1&gt;/);
  assert.match(html, /data-company="&quot;&gt;&lt;script&gt;/);
});

test('the panel counts the suggestions that wait', () => {
  const html = markup([S(), S({ id: 2, key: 'b', state: 'resolved' }), S({ id: 3, key: 'c', state: 'dismissed', status: 'dismissed' })]);
  assert.match(html, /Suggested brands · 1 waiting/);
});

test('Confirm posts the verdict and company the page showed, then reloads the list and the rules', async () => {
  const p = page({ list: [S()] });
  await p.loadAdReviewSuggest();
  assert.equal(p.gets.length, 1);
  p.setList([S({ state: 'confirmed', status: 'confirmed', decided_by: 'Reno', rules: [['alias', 'century', 8]] })]);
  await p.button('confirm', 1).click();
  assert.deepEqual(p.posts.map((x) => [x.url, x.body]), [['/api/v1/admin/brand-suggestions/1/confirm', { verdict: 'brand', company: 'Sentry', heard_correct: false, fetch_date: FD }]]);
  assert.equal(p.gets.length, 2);
  assert.equal(JSON.stringify(p.reloads), '[{"open":true}]');
  assert.match(p.host.innerHTML, /Confirmed by Reno/);
  assert.match(p.host.innerHTML, /Saved/);
});

test('the count question: shown inline, then the same press sends expected_reads', async () => {
  const p = page({ list: [S()], respond: (url, body) => (body.expected_reads === undefined
    ? { status: 409, body: { ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'count', details: { reads: 3, known_brand: false } } } } : null) });
  await p.loadAdReviewSuggest();
  await p.button('confirm', 1).click();
  assert.match(p.host.innerHTML, /This changes 3 reads\. Press Confirm again to save; Esc cancels\./);
  assert.equal(p.button('confirm', 1).label, 'Confirm: press again');
  assert.equal(p.posts.length, 1);
  await p.button('confirm', 1).click();
  assert.deepEqual(p.posts[1].body, { verdict: 'brand', company: 'Sentry', heard_correct: false, fetch_date: FD, expected_reads: 3 });
  assert.equal(JSON.stringify(p.reloads), '[{"open":true}]');
});

test('a known brand is said, and the second press carries known_brand', async () => {
  const p = page({ list: [S({ key: 'shopify', verdict: 'not_brand', company: null, heard_correct: null })], respond: (url, body) => (body.expected_reads === undefined
    ? { status: 409, body: { ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'count', details: { reads: 1, word: 'shopify', known_brand: true } } } } : null) });
  await p.loadAdReviewSuggest();
  await p.button('confirm').click();
  assert.match(p.host.innerHTML, /This changes 1 read\. “shopify” is a known brand\. Press Confirm again to save; Esc cancels\./);
  await p.button('confirm').click();
  assert.deepEqual(p.posts[1].body, { verdict: 'not_brand', company: null, heard_correct: null, fetch_date: FD, expected_reads: 1, known_brand: true });
});

test('Not this posts dismiss with the verdict and company shown', async () => {
  const p = page({ list: [S()], respond: () => ({ status: 200, body: { ok: true, state: 'dismissed' } }) });
  await p.loadAdReviewSuggest();
  await p.button('dismiss', 1).click();
  assert.deepEqual(p.posts.map((x) => [x.url, x.body]), [['/api/v1/admin/brand-suggestions/1/dismiss', { verdict: 'brand', company: 'Sentry', heard_correct: false, fetch_date: FD }]]);
  assert.equal(p.reloads.length, 0, 'Not this changes no rule');
});

test('Undo of a confirm asks the count first, then sends it, and redraws', async () => {
  const confirmed = S({ state: 'confirmed', status: 'confirmed', decided_by: 'Reno', rules: [['alias', 'century', 8]] });
  const p = page({ list: [confirmed], respond: (url, body) => (body.expected_reads === undefined
    ? { status: 409, body: { ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'count', details: { reads: 2, known_brand: false } } } } : { status: 200, body: { ok: true, state: 'pending', reads: 2 } }) });
  const drawn = [];
  p.setRender(() => { drawn.push(1); });
  await p.loadAdReviewSuggest();
  await p.button('undo', 1).click();
  assert.match(p.host.innerHTML, /This changes 2 reads\. Press Undo again to save; Esc cancels\./);
  assert.equal(p.button('undo', 1).label, 'Undo: press again');
  await p.button('undo', 1).click();
  assert.deepEqual(p.posts.map((x) => [x.url, x.body]), [
    ['/api/v1/admin/brand-suggestions/1/undo', { status: 'confirmed', fetch_date: FD }],
    ['/api/v1/admin/brand-suggestions/1/undo', { status: 'confirmed', fetch_date: FD, expected_reads: 2 }]]);
  assert.equal(drawn.length, 1, 'reads changed: the page is drawn again, which reloads the rules');
});

test('an error is shown near the row and nothing reloads; READ_CHANGED reloads the list', async () => {
  const p = page({ list: [S()], respond: () => ({ status: 400, body: { ok: false, error: { code: 'BAD_REQUEST', message: 'unsure cannot be confirmed' } } }) });
  await p.loadAdReviewSuggest();
  await p.button('confirm', 1).click();
  assert.match(p.host.innerHTML, /Not saved: unsure cannot be confirmed/);
  assert.match(p.host.innerHTML, /role="status" aria-live="polite"/);
  assert.equal(p.reloads.length, 0);
  const q = page({ list: [S()], respond: () => ({ status: 409, body: { ok: false, error: { code: 'READ_CHANGED', message: 'This suggestion changed', details: {} } } }) });
  await q.loadAdReviewSuggest();
  await q.button('confirm', 1).click();
  assert.equal(q.gets.length, 2);
  assert.match(q.host.innerHTML, /This suggestion changed/);
});

test('the page sends all four fields it showed: verdict, company, heard_correct and fetch_date', () => {
  const html = markup([S(), S({ id: 2, key: 'vantrix', heard_correct: true, company: 'Vantrix' }), S({ id: 3, key: 'good', verdict: 'not_brand', company: null, heard_correct: null })]);
  assert.match(html, /data-id="1"[^>]*data-heard-correct="false"[^>]*data-fetch-date="2026-10-18T00:00:00\+00:00"/);
  assert.match(html, /data-id="2"[^>]*data-heard-correct="true"/);
  assert.doesNotMatch(html.split('data-id="3"')[1].split('</li>')[0], /data-heard-correct/);
});

test('a confirmed suggestion whose rules are all gone: "Its rule was removed in Rules" and Undo', () => {
  const html = markup([S({ state: 'rule_removed', status: 'confirmed', decided_by: 'Reno', rules: [['alias', 'century', 8]] })]);
  assert.match(html, /Its rule was removed in Rules/);
  assert.match(html, /data-sg="undo"/);
  assert.doesNotMatch(html, /Confirmed by Reno|data-sg="confirm"|data-sg="dismiss"/);
  assert.match(html, /Suggested brands · 0 waiting/);
});

test('a Confirm or Undo that changed reads redraws the page like the Rules panel Remove', async () => {
  const p = page({ list: [S()], respond: () => ({ status: 200, body: { ok: true, state: 'confirmed', reads: 3, message: 'Confirmed.' } }) });
  const calls = [];
  p.setRender(() => { calls.push(1); });
  p.AdReview.items = [{ evidence_id: 'e1' }];
  p.AdReview.cache.set('e1', {});
  await p.loadAdReviewSuggest();
  await p.button('confirm', 1).click();
  assert.equal(calls.length, 1);
  assert.equal(p.AdReview.items.length, 0);
  assert.equal(p.AdReview.cache.size, 0);
  assert.equal(p.AdReview.lastMessage, 'Confirmed.');
  assert.equal(p.reloads.length, 0, 'the redraw reloads the rules itself');
});

test('Undo sends the status and fetch_date the page showed', () => {
  const html = markup([S({ state: 'dismissed', status: 'dismissed' })]);
  assert.match(html, /data-sg="undo"[^>]*data-status="dismissed"[^>]*data-fetch-date="2026-10-18T00:00:00\+00:00"|data-sg="undo"[^>]*data-fetch-date="2026-10-18T00:00:00\+00:00"[^>]*data-status="dismissed"/);
});

test('"misheard" follows what Confirm will write, not heard_correct', () => {
  assert.match(markup([S({ heard_correct: true, confirm_writes: 'alias' })]), / · misheard/);
  assert.doesNotMatch(markup([S({ heard_correct: false, confirm_writes: 'brand_list' })]), /misheard/);
});

test('a confirmed row lists the rules that are left', () => {
  const html = markup([S({ state: 'confirmed', status: 'confirmed', decided_by: 'Reno', rules: [['alias', 'century', 8]] })]);
  assert.match(html, /Every read heard as “century” shows as Sentry/);
  assert.doesNotMatch(html, /is on the brand list/);
});

test('the Ad review page carries the panel above the Rules panel', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'app/operator/views.js'), 'utf8');
  const first = src.indexOf('id="ar-suggest"');
  assert.ok(first > 0);
  for (const m of src.matchAll(/id="ar-suggest"/g)) {
    assert.ok(src.indexOf('id="ar-rules"', m.index) - m.index < 200, 'ar-suggest sits just above ar-rules');
  }
});
