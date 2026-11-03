/* Ask screens (T10): red tests for the turn markup, disclosure, overview card,
   nav item, brand button, refusals and the #/ask page. Contract:
   ws-meta/briefs/T10-ask-screens.md and ASK-VIEW.md. Loaders copied from
   frontend_dv3_final_b.test.cjs.
   Run: node --test tests/frontend_ask_screens.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'ask-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
const clone = o => JSON.parse(JSON.stringify(o));

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
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
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const canonicalSessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function renderRoute(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}

// A fresh sandbox per test; askAvailable set as the test needs.
function views(available) {
  const win = loadViews(canonicalSessionFetch());
  if (available !== undefined) win.EW.current.askAvailable = available;
  return win;
}
const turnHtml = turn => views(true).EWViews.askTurnHtml(turn);
const articleInner = html => /<article[^>]*>([\s\S]*)<\/article>/.exec(html)[1];
// Rendered text: tags dropped, &nbsp; as a space (chips share a span with the last word).
const plain = html => html.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ');

const WORKING = fixture('working');
const ANSWERED = fixture('answered');
// ASK-UX-R1 (b): the method as steps (UX words draft 2).
const MUSE = "Muse Spark, Meta's AI model, read the passages and wrote the answer using only the ones it cites.";
const STEP1 = 'Searched the words of 40 episodes for words linked to your question (3 searches).';
const STEP2 = 'Found 17 passages that matched.';
const CHECKED = 'Checked every quote word for word against its passage. ';
const DROPPED_2 = CHECKED + "2 claims removed because their passage didn't support them";

test('working: three steps in order, only the 2nd is current, Cancel button', () => {
  const html = turnHtml(WORKING);
  const steps = ['Searching the episodes', 'Searching again', 'Writing the answer and checking each quote'];
  const at = steps.map(s => html.indexOf(s));
  assert.ok(at.every(i => i >= 0), 'all three step strings present');
  assert.ok(at[0] < at[1] && at[1] < at[2], 'steps in order');
  const ol = /<ol class="ask-steps">([\s\S]*?)<\/ol>/.exec(html);
  assert.ok(ol, 'ordered list class ask-steps');
  const lis = ol[1].match(/<li[^>]*>[\s\S]*?<\/li>/g);
  assert.equal(lis.length, 3);
  assert.equal((html.match(/aria-current="step"/g) || []).length, 1);
  assert.match(lis[1], /aria-current="step"/);
  assert.doesNotMatch(lis[0], /aria-current/);
  assert.doesNotMatch(lis[2], /aria-current/);
  assert.match(html, /<button[^>]*data-action="ask-cancel"[^>]*>\s*Cancel\s*<\/button>/);
});

test('answered: based_on, note, paragraphs, no null heading, chips, sources heading, report, disclosure', () => {
  const html = turnHtml(ANSWERED);
  assert.ok(html.includes('Based on 3 passages in 3 episodes'));
  assert.ok(html.includes('The numbers after each paragraph link to its passages. Check each one.'));
  for (const p of ANSWERED.sections[0].paragraphs) assert.ok(plain(html).includes(p.text), p.text);
  assert.doesNotMatch(html, /<h3>\s*(null|undefined)?\s*<\/h3>/, 'no h3 for the null heading');
  assert.equal((html.match(/<h3/g) || []).length, 1, 'only the Sources h3');
  assert.match(html, /class="ask-cite"/);
  assert.ok(html.includes('Sources · 3 passages'));
  assert.ok(html.includes('<summary>Report a mistake</summary>'));
  assert.ok(html.includes('How this answer was made'));
});

// LEDGER-LEAD3-ASK-3: one source reads "1 passage"; every other count keeps "N passages".
test('sources heading: exactly one source is singular', () => {
  const turn = clone(ANSWERED);
  turn.sources = turn.sources.slice(0, 1);
  const html = turnHtml(turn);
  assert.ok(html.includes('<h3>Sources · 1 passage</h3>'), 'singular heading');
  assert.ok(!html.includes('1 passages'));
});

test('sources heading: two sources stay plural', () => {
  const turn = clone(ANSWERED);
  turn.sources = turn.sources.slice(0, 2);
  assert.ok(turnHtml(turn).includes('<h3>Sources · 2 passages</h3>'));
});

test('answered-brand: both section headings as h3, in order', () => {
  const html = turnHtml(fixture('answered-brand'));
  const a = html.indexOf('<h3>Ads</h3>');
  const b = html.indexOf('<h3>Mentions outside its ads</h3>');
  assert.ok(a >= 0, 'Ads heading');
  assert.ok(b > a, 'Mentions heading after Ads');
});

test('partial: Not clear from the passages block, its sentence, uncertain text and the server line', () => {
  const t = fixture('partial');
  const html = turnHtml(t);
  assert.ok(html.includes('<h3>Not clear from the passages</h3>'));
  assert.ok(html.includes("These points come from passages that touch on your question but don't settle it, so they aren't in the answer above."));
  assert.ok(!html.includes('Less certain'));
  assert.ok(plain(html).includes(t.uncertain[0].text));
  assert.ok(html.includes(t.lines[0]));
});

test('nothing: server line and disclosure only, no note, sources or Less certain', () => {
  const t = fixture('nothing');
  const html = turnHtml(t);
  assert.ok(html.includes(t.lines[0]));
  assert.ok(html.includes('How this answer was made'));
  assert.ok(!html.includes('The numbers after each paragraph'));
  assert.ok(!html.includes('Sources ·'));
  assert.ok(!html.includes('Not clear from the passages'));
  // Code review r4 (CODE-R4-02): the whole turn, so no screen wording can creep in.
  assert.equal(articleInner(html), `<h2 class="ask-q" tabindex="-1">${t.question}</h2><p class="ask-nothing">${t.lines[0]}</p>`
    + views().EWViews.askDisclosureHtml(t.disclosure));
});

for (const name of ['failed', 'cancelled']) {
  test(`${name}: "The answer didn’t finish" and Ask again, no disclosure`, () => {
    const html = turnHtml(fixture(name));
    assert.ok(html.includes('The answer didn’t finish'));
    assert.ok(html.includes('Ask again'));
    assert.ok(!html.includes('How this answer was made'));
  });
}

test('hidden: only the unavailable line, no question, no button', () => {
  const html = turnHtml(fixture('hidden'));
  assert.equal(articleInner(html).trim(), '<p class="ask-hidden">This answer is no longer available</p>');
  assert.ok(!html.includes('class="ask-q"'));
  assert.ok(!html.includes('<button'));
});

test('the disclosure details is closed by default', () => {
  const win = views();
  const own = win.EWViews.askDisclosureHtml(ANSWERED.disclosure);
  assert.match(own, /<details class="ask-disclosure"[^>]*>/);
  assert.doesNotMatch(/<details class="ask-disclosure"[^>]*>/.exec(own)[0], /\bopen\b/);
  const full = win.EWViews.askTurnHtml(ANSWERED);
  assert.match(full, /<details class="ask-disclosure"[^>]*>/);
  assert.doesNotMatch(/<details class="ask-disclosure"[^>]*>/.exec(full)[0], /\bopen\b/);
});

test('ASK-UX-R1 (b): the method is five numbered steps in order, with the turn\'s numbers', () => {
  const html = views().EWViews.askDisclosureHtml(ANSWERED.disclosure);
  const ol = /<summary>How this answer was made<\/summary><ol class="ask-method">([\s\S]*?)<\/ol>/.exec(html);
  assert.ok(ol, 'a numbered list under the pinned summary');
  assert.deepEqual(ol[1].match(/<li>[\s\S]*?<\/li>/g), [STEP1, STEP2, MUSE, DROPPED_2, 'Cost of this answer: $0.04'].map(t => `<li>${t}</li>`));
});

test('model step: only muse-spark-* says Muse Spark; another model, or none, has no model step', () => {
  const win = views();
  assert.ok(win.EWViews.askDisclosureHtml(ANSWERED.disclosure).includes(MUSE));
  const other = win.EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { model: 'llama-4-maverick' }));
  assert.ok(!other.includes('Muse Spark') && !other.includes('llama'));
  assert.ok(other.includes(DROPPED_2), 'a model call still has the check step');
  for (const model of ['', null, undefined]) {
    const none = win.EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { model }));
    assert.ok(!none.includes('Muse Spark') && !none.includes(CHECKED), String(model));
  }
});

test('check step: dropped 2 names the removed claims; dropped 0 says No claims removed; the pinned lines byte for byte', () => {
  const win = views();
  assert.ok(win.EWViews.askDisclosureHtml(ANSWERED.disclosure).includes(DROPPED_2));
  const zero = win.EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { dropped: 0 }));
  assert.ok(zero.includes(`<li>${CHECKED}No claims removed</li>`));
  assert.ok(!zero.includes('claims removed because'));
});

// LEDGER-LEAD3-ASK-4: a count of exactly 1 is singular; ASK-UX-R1 (b) keeps it (Meta, 11-02).
// LEDGER-LEAD3-ASK-5: one removed claim reads "its passage didn't support it".
test('steps: 1 episode, 1 search, 1 passage, 1 claim are singular', () => {
  const html = views().EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { word_searches: 1, passages_read: 1, dropped: 1, episodes_searched: 1, episodes_in_scope: 1 }));
  assert.ok(html.includes('<li>Searched the words of 1 episode for words linked to your question (1 search).</li>'), html);
  assert.ok(html.includes('<li>Found 1 passage that matched.</li>'), html);
  assert.ok(html.includes(`<li>${CHECKED}1 claim removed because its passage didn't support it</li>`), html);
});

test('steps: 2 searches, 2 passages, 2 claims are plural', () => {
  const html = views().EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { word_searches: 2, passages_read: 2, dropped: 2 }));
  assert.ok(html.includes('<li>Searched the words of 40 episodes for words linked to your question (2 searches).</li>'), html);
  assert.ok(html.includes('<li>Found 2 passages that matched.</li>'), html);
  assert.ok(html.includes(`<li>${CHECKED}2 claims removed because their passage didn't support them</li>`), html);
});

test('step 1: a cut search says N of M; a missing count drops the number; no word search, no step 1', () => {
  const V = views().EWViews;
  const withD = patch => V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), patch));
  assert.ok(withD({ episodes_searched: 12, episodes_in_scope: 40 }).includes('<li>Searched the words of 12 of 40 episodes for words linked to your question (3 searches).</li>'));
  for (const patch of [{ episodes_searched: null, episodes_in_scope: null }, { episodes_in_scope: 0 }, { episodes_in_scope: 'x' }]) {
    assert.ok(withD(patch).includes('<li>Searched the words of the episodes for words linked to your question (3 searches).</li>'), JSON.stringify(patch));
  }
  assert.ok(withD({ word_searches: 0 }).includes('<li>Searched the words of 40 episodes for words linked to your question.</li>'));
  for (const word_search of [false, undefined, 'true']) assert.ok(!withD({ word_search }).includes('Searched the words'), String(word_search));
  assert.ok(!/\b0 (of|episodes|passages|search)/.test(withD({ word_searches: 0, passages_read: 0, episodes_searched: 0, episodes_in_scope: 0 })), 'never 0');
});

test('step 2: no passages found, no step 2 (never 0)', () => {
  const V = views().EWViews;
  for (const passages_read of [0, null, -1]) assert.ok(!V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { passages_read })).includes('Found '), String(passages_read));
});

test('cost step: judges (cost_shown false) see no cost line; every other account does', () => {
  const V = views().EWViews;
  const judge = V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { cost_shown: false }));
  assert.ok(!judge.includes('Cost'), judge);
  assert.ok(judge.includes('How this answer was made') && judge.includes(DROPPED_2));
  for (const cost_shown of [true, undefined]) assert.ok(V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { cost_shown })).includes('Cost of this answer: $0.04'));
  assert.ok(V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { cost_shown: false, usd_settled: null })).indexOf('Cost: pending') === -1);
});

// Code review r5 (CODE-R5-04): two decimals exactly.
test('cost line is exactly two decimals', () => {
  const html = c => views().EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { usd_settled: c }));
  assert.ok(html(0.04).includes('<li>Cost of this answer: $0.04</li>'));
  assert.ok(html(0.1).includes('<li>Cost of this answer: $0.10</li>'));
});

test('cost line: settled shows dollars; null shows Cost: pending', () => {
  const win = views();
  assert.ok(win.EWViews.askDisclosureHtml(ANSWERED.disclosure).includes('Cost of this answer: $0.04'));
  const pending = win.EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { usd_settled: null }));
  assert.ok(pending.includes('Cost: pending'));
  assert.ok(!pending.includes('Cost of this answer'));
});

test('overview card renders only when ask is available', () => {
  assert.equal(views(false).EWViews.askOverviewCard(), '');
  assert.equal(views(undefined).EWViews.askOverviewCard(), '');
  const card = views(true).EWViews.askOverviewCard();
  assert.ok(card.includes('Ask what the shows are saying'));
  assert.ok(card.includes('Type a question. Muse Spark, Meta\'s AI model, answers from the episodes and cites a passage for every claim.'));
  assert.ok(card.includes('Ask a question'));
  assert.ok(card.includes('href="#/ask"'));
});

test('nav: an Ask anchor appears only when ask is available', () => {
  const navHtml = available => {
    const win = views(available);
    let captured = '';
    const stub = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
    const header = { set innerHTML(v) { captured = v; }, get innerHTML() { return captured; } };
    win.document.querySelector = sel => (sel === '#header' ? header : stub);
    win.EW.header('Overview');
    return captured;
  };
  assert.ok(navHtml(true).includes('href="#/ask"'), 'available');
  assert.ok(!navHtml(false).includes('href="#/ask"'), 'false');
  assert.ok(!navHtml(undefined).includes('href="#/ask"'), 'undefined');
});

test('brand button: link when available and ask_button is true; empty otherwise', () => {
  const brand = { display: 'Quince', ask_button: true };
  const on = views(true).EWViews.askBrandButton(brand);
  assert.ok(on.includes('href="#/ask?brand=Quince"'));
  assert.ok(on.includes('What podcasts say about Quince'));
  assert.equal(views(true).EWViews.askBrandButton({ display: 'Quince', ask_button: false }), '');
  assert.equal(views(false).EWViews.askBrandButton(brand), '');
});

test('an unknown state shows the question only: no sources, report or disclosure (CODE-R2-05)', () => {
  const t = Object.assign(clone(ANSWERED), { state: 'queued' });
  const html = turnHtml(t);
  assert.ok(html.includes(t.question));
  assert.ok(!html.includes('Sources ·'));
  assert.ok(!html.includes('report-problem'));
  assert.ok(!html.includes('How this answer was made'));
});

// Code review r2 (CODE-R2-04): the session's ask_available is the one gate.
test('render() takes askAvailable from the session: only ask_available true shows the card', async () => {
  for (const [value, shown] of [[true, true], [false, false], [undefined, false], ['true', false]]) {
    const win = loadViews(canonicalSessionFetch());
    await flushBoot();
    const main = { innerHTML: '' };
    win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
    win.EW.header = () => {};
    win.EW.api = async () => { throw new Error('no network'); };
    win.EW.current.adoptedSession = value === undefined ? { role: 'editor' } : { role: 'editor', ask_available: value };
    await win.EWViews.render().catch(() => {});
    assert.equal(win.EW.current.askAvailable, shown, String(value));
    assert.equal(win.EWViews.askOverviewCard() !== '', shown, String(value));
  }
});

// Code review r3 (CODE-R3-01): the entry points render on the real pages.
async function routeWithAsk(route, api, available) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async p => { const out = api(p); if (out === undefined) throw Object.assign(new Error('not stubbed ' + p), { status: 500 }); return out; };
  win.location.hash = route;
  win.EW.current.adoptedSession = { role: 'editor', ask_available: available };
  await win.EWViews.render();
  for (let i = 0; i < 5; i++) await new Promise(res => setTimeout(res, 0));
  return main.innerHTML;
}
const OV_EMPTY = { coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [] };
const ovApi = overview => p => (p === '/customer/overview' ? overview
  : p.startsWith('/customer/saved-items') ? { items: [], next_cursor: null } : p.startsWith('/customer/search') ? { items: [] } : undefined);

test('Overview shows the Ask card on first use and on the full page only when ask_available is true', async () => {
  // OV_EMPTY is the first-use page; a failed overview read draws the full grid.
  for (const overview of [OV_EMPTY, null]) {
    const api = overview ? ovApi(overview) : p => (p === '/customer/overview' ? (() => { throw Object.assign(new Error('down'), { status: 500 }); })() : ovApi(OV_EMPTY)(p));
    const on = await routeWithAsk('#/overview', api, true);
    assert.match(on, /class="ew-card ask-card"/, overview ? 'first use' : 'full page');
    assert.match(on, /href="#\/ask"/);
    const off = await routeWithAsk('#/overview', api, false);
    assert.doesNotMatch(off, /ask-card/, overview ? 'first use off' : 'full page off');
  }
});

const BRAND_READ = ask_button => ({ brand: { brand: 'quince', key: 'quince', display: 'Quince', detections: 3, ad_breaks: 2, episodes: 3, shows: 2, last_heard: '2026-10-09',
  confirmed_occurrences: [], findings: [], offer_sequence: [], ask_button,
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['quince'], sort: 'newest', page: 1 }, mentions: { version: 2, mode: 'mentions', brands: ['quince'] } } } });
const brandApi = ask_button => p => (p === '/customer/brands/quince' ? BRAND_READ(ask_button)
  : p.startsWith('/customer/saved-items') ? { items: [], next_cursor: null }
  : p.startsWith('/customer/search') ? { items: [], total: 0, break_total: 0, total_relation: 'exact' }
  : p.startsWith('/customer/summary') ? { cells: [] } : undefined);

test('the brand page shows "What podcasts say about Quince" only when ask_available and ask_button are both true', async () => {
  const on = await routeWithAsk('#/brand/quince', brandApi(true), true);
  assert.match(on, /href="#\/ask\?brand=Quince"[^>]*>What podcasts say about Quince</);
  assert.doesNotMatch(await routeWithAsk('#/brand/quince', brandApi(false), true), /#\/ask\?brand=/);
  assert.doesNotMatch(await routeWithAsk('#/brand/quince', brandApi(true), false), /#\/ask\?brand=/);
});

test('the question is escaped in working and answered turns', () => {
  const q = '<img src=x onerror=alert(1)>';
  for (const base of [WORKING, ANSWERED]) {
    const html = turnHtml(Object.assign(clone(base), { question: q }));
    assert.ok(html.includes('&lt;img'), base.state + ': escaped');
    assert.ok(!html.includes('<img'), base.state + ': no raw tag');
  }
});

test('cite chips target the numbered source of the cited pid, and that element exists', () => {
  const html = turnHtml(ANSWERED);
  const n = pid => ANSWERED.sources.findIndex(s => s.pid === pid) + 1;
  const expected = [];
  for (const sec of ANSWERED.sections) for (const p of sec.paragraphs) for (const pid of p.cites) expected.push(`ask-src-${ANSWERED.turn_id}-${n(pid)}`);
  const chips = [...html.matchAll(/<button[^>]*class="ask-cite"[^>]*>/g)].map(m => m[0]);
  assert.equal(chips.length, expected.length, 'one chip per cite');
  const targets = chips.map(c => /data-target="([^"]+)"/.exec(c)[1]);
  assert.deepEqual(targets, expected);
  for (const t of new Set(targets)) assert.ok(html.includes(`id="${t}"`), 'element ' + t);
});

test('askRefusalText gives the exact sentence for each code', () => {
  const V = views().EWViews;
  const busy = 'Ask is busy right now. Try again in a minute.';
  assert.equal(V.askRefusalText('ASK_LIMIT_DAY'), 'You have reached today\'s limit for Ask. It comes back tomorrow.');
  assert.equal(V.askRefusalText('ASK_PAUSED'), 'Ask is paused right now.');
  // ASK-OFF-1 (UX 7f1aaef45): switched off is not the spend stop.
  assert.equal(V.askRefusalText('ASK_UNAVAILABLE'), 'Ask is off for now. When it is on, Muse Spark, Meta\u2019s AI model, answers your questions from the episodes, with every claim linked to the passage behind it.');
  assert.equal(V.askRefusalText('ASK_BUSY'), busy);
  assert.equal(V.askRefusalText('INVALID_QUESTION'), 'Questions can be 1 to 300 characters.');
  assert.equal(V.askRefusalText('SOMETHING_NEW'), busy);
});

// askPage is called directly with a token matching fetchToken: render() re-reads
// the session and would reset askAvailable.
async function pageHtml({ available, features, paused }) {
  const win = views(available);
  win.EW.current.fetchToken = 7;
  if (paused !== undefined) win.EW.current.askPaused = paused;
  if (features !== undefined) win.EW.current.pilotFeatures = features;
  return win.EWViews.askPage(7);
}

test('#/ask page when available: title, lede, examples, form', async () => {
  const html = await pageHtml({ available: true });
  assert.ok(html.includes('Ask what the shows in this collection are saying'));
  assert.ok(html.includes('Every claim links to the passage behind it, with a clip where we have one. If the episodes don\'t support something, we say so.'));
  for (const q of ['How do hosts pitch VPNs in this collection?', 'Which shows talk about mortgage rates?']) {
    assert.ok(html.includes(q), q);
  }
  // ASK-EX (LEDGER-LEAD3-ASKEX-1): the mattress example failed on the demo copy; Oct 30 ships two.
  assert.ok(!html.includes('What offers do mattress brands make in their ads?'), 'mattress example dropped');
  assert.equal((html.match(/data-action="ask-example"/g) || []).length, 2, 'exactly two examples');
  assert.ok(html.includes('Your question'));
  assert.ok(html.includes('id="ask-form"'));
});

test('#/ask page when paused: paused text, title, no form', async () => {
  const html = await pageHtml({ available: false, features: { ask: true }, paused: true });
  assert.ok(html.includes('Ask is paused right now.'));
  assert.ok(html.includes('Ask what the shows in this collection are saying'));
  // ASK-OFF-1 (UX 11f8e4c2c): the paused page keeps today's heading and lede.
  assert.ok(html.includes('<p class="ask-lede">Every claim links to the passage behind it, with a clip where we have one. If the episodes don\'t support something, we say so.</p>'));
  assert.ok(!html.includes('Ask about the ads'));
  assert.ok(!html.includes('id="ask-form"'));
});

// ARC-R1-03: a refused key file is Ask switched off (switch on, no ask_paused):
// the typed #/ask and a send refused for it (ASK_UNAVAILABLE) say the same line.
test('#/ask page with the key file refused: the line a refused send shows, no form', async () => {
  const html = await pageHtml({ available: false, features: { ask: true } });
  const line = (html.match(/<p class="ask-off[^"]*" role="status">([^<]*)<\/p>/) || [])[1];
  assert.ok(line, 'no off line');
  assert.equal(line, views(false).EWViews.askRefusalText('ASK_UNAVAILABLE'));
  assert.equal(line, 'Ask is off for now. When it is on, Muse Spark, Meta\u2019s AI model, answers your questions from the episodes, with every claim linked to the passage behind it.');
  assert.ok(!html.includes('id="ask-form"'));
});

test('#/ask page when the pilot has ask off: off text, no form', async () => {
  const html = await pageHtml({ available: false, features: { ask: false } });
  // ASK-OFF-1 (UX 7f1aaef45, 11f8e4c2c): switched off has its own line and
  // heading, and no lede.
  assert.ok(html.includes('Ask is off for now. When it is on, Muse Spark, Meta\u2019s AI model, answers your questions from the episodes, with every claim linked to the passage behind it.'));
  assert.ok(html.includes('<header class="ask-head"><h1>Ask about the ads</h1></header>'));
  assert.ok(!html.includes('class="ask-lede"'));
  assert.ok(!html.includes('Ask is paused right now.'));
  assert.ok(!html.includes('Ask is off in this pilot.'));
  assert.ok(!html.includes('id="ask-form"'));
});

test('#/ask is routed: render() shows the Ask page title', async () => {
  const { html } = await renderRoute('#/ask', p => { throw new Error('unexpected request ' + p); });
  // No ask_available in this session: Ask is switched off (ASK-OFF-1 heading).
  assert.ok(html.includes('<div class="ask-page"><header class="ask-head"><h1>Ask about the ads</h1></header>'));
});

test('no inline style attribute in any turn markup', () => {
  for (const name of ['working', 'answered', 'answered-brand', 'partial', 'nothing', 'failed', 'cancelled', 'hidden']) {
    const html = turnHtml(fixture(name));
    assert.ok(html.length > 0, name + ': markup produced');
    assert.doesNotMatch(html, /\sstyle=/, name);
  }
});

// Owner decisions on screen review round 1.
test('cancelled is neutral (status), failed is the alert', () => {
  const cancelled = turnHtml(fixture('cancelled'));
  assert.match(cancelled, /<p class="ask-cancelled" role="status">The answer didn’t finish<\/p>/);
  assert.doesNotMatch(cancelled, /role="alert">The answer/, 'the cancelled sentence is not an alert (the empty refusal slot is)');
  assert.match(turnHtml(fixture('failed')), /<p class="ask-failed" role="alert">The answer didn’t finish<\/p>/);
});
test('nothing with one server line renders it as one paragraph', () => {
  const t = fixture('nothing');
  assert.ok(turnHtml(t).includes(`<p class="ask-nothing">${t.lines[0]}</p>`));
});
test('the report control is the app one: Report a mistake, Details, Send report', () => {
  const html = turnHtml(ANSWERED);
  assert.match(html, /<details class="report-problem ask-report"><summary>Report a mistake<\/summary>/);
  assert.ok(html.includes('Send report'));
  assert.ok(!html.includes('Report a problem with this answer'));
});

// ---- Round 1 fixes (code review r1 + screen review r1) ----------------------
const flush = () => new Promise(res => setImmediate(res));
const defer = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const refusal = (status, code) => Object.assign(new Error(code), { status, body: { error: { code } } });

// A stub document of simple element objects, a fake api and fake timers.
async function askRig() {
  const win = views(true);
  await flushBoot();
  const E = win.EW;
  E.current.askAvailable = true;
  E.current.route = '#/ask';
  const timers = [];
  win.setTimeout = (fn, ms) => { const t = { fn, ms, cleared: false, fired: false }; timers.push(t); return t; };
  win.clearTimeout = t => { if (t) t.cleared = true; };
  const mk = extra => Object.assign({ value: '', hidden: false, disabled: false, textContent: '', innerHTML: '', outerHTML: '', dataset: {},
    focused: 0, scrolled: 0, focus() { this.focused += 1; }, scrollIntoView() { this.scrolled += 1; },
    querySelector: () => null, querySelectorAll: () => [], closest: () => null, insertAdjacentHTML() {} }, extra);
  const thread = mk({ sets: 0, html: '', inserted: [],
    insertAdjacentHTML(pos, h) { this.inserted.push([pos, h]); this.html += h; } });
  Object.defineProperty(thread, 'innerHTML', { get() { return this.html; }, set(v) { this.sets += 1; this.html = v; } });
  const mkForm = kind => {
    const box = mk({ id: kind === 'follow' ? 'ask-fu' : 'ask-q' }), slot = mk({ hidden: true }), submit = mk();
    return mk({ box, slot, submit, dataset: { ask: kind },
      querySelector: s => (s === '[name=question]' ? box : s === '.ask-refusal' ? slot : s === '[type=submit]' ? submit : null) });
  };
  const top = mkForm('new'), follow = mkForm('follow');
  const status = mk(), count = mk({ hidden: true }), examples = mk();
  const exampleButtons = [mk(), mk(), mk()];
  const turnEls = {};
  const turnEl = id => turnEls[id] || (turnEls[id] = (() => { const q = mk(); return mk({ q, querySelector: s => (s === '.ask-q' ? q : null) }); })());
  const els = { 'ask-thread': thread, 'ask-status': status, 'ask-form': top, 'ask-q': top.box, 'ask-followup': follow, 'ask-fu': follow.box };
  const doc = win.document;
  doc.getElementById = id => (id.startsWith('ask-turn-') ? turnEl(id.slice(9)) : els[id] || null);
  doc.querySelector = sel => (sel === '.ask-count' ? count : sel === '.ask-examples' ? examples : null);
  doc.querySelectorAll = sel => (String(sel).includes('ask-') ? [top.submit, follow.submit, ...exampleButtons] : []);
  const calls = [];
  const rig = { win, E, V: win.EWViews, timers, thread, top, follow, status, count, examples, exampleButtons, turnEl, calls, syncs: [], handler: async () => { throw new Error('unexpected request'); } };
  E.api = (p, opts) => { calls.push({ p, method: (opts && opts.method) || 'GET', body: opts && opts.body ? clone(opts.body) : undefined }); return rig.handler(p, opts || {}); };
  E.syncHistory = () => { rig.syncs.push(E.current.route); };
  rig.posts = () => calls.filter(c => c.p === '/customer/ask');
  rig.reads = () => calls.filter(c => c.method === 'GET');
  rig.poll = () => timers.filter(t => t.ms === 1500 && !t.cleared && !t.fired);
  rig.fire = async t => { t.fired = true; t.fn(); await flush(); };
  // A POST /customer/ask handler that issues t1, t2, ... in thread h1 (or h<n> for a new thread).
  let n = 0;
  rig.okPosts = () => { rig.handler = async (p, o) => { if (p === '/customer/ask') { n += 1; return { turn_id: 't' + n, thread_id: (o.body && o.body.thread_id) || 'h' + n }; } throw new Error('unexpected ' + p); }; };
  rig.ask = async (form, text) => { form.box.value = text; await rig.V.askSubmit(form); };
  return rig;
}
const answeredAs = (id, extra) => Object.assign(clone(ANSWERED), { turn_id: id, thread_id: 'h1' }, extra);
const workingAs = (id, step) => Object.assign(clone(WORKING), { turn_id: id, thread_id: 'h1', step });

test('polling: GET the turn every 1500 ms, never two reads in flight, announce steps, then focus the question', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  assert.equal(r.poll().length, 1, 'one 1500 ms timer after the send');
  const read = defer();
  r.handler = async p => { if (p === '/customer/ask/turns/t1') return read.promise; throw new Error('unexpected ' + p); };
  const first = r.poll()[0];
  await r.fire(first);
  first.fn(); await flush();
  assert.deepEqual(r.reads().map(c => c.p), ['/customer/ask/turns/t1'], 'one read in flight');
  assert.equal(r.poll().length, 0, 'no timer while a read is in flight');
  read.resolve(workingAs('t1', 2));
  await flush();
  assert.equal(r.status.textContent, 'Searching again');
  assert.equal(r.poll().length, 1);
  assert.equal(r.poll()[0].ms, 1500);
  r.handler = async () => answeredAs('t1');
  const before = r.turnEl('t1').q.focused;
  await r.fire(r.poll()[0]);
  assert.equal(r.status.textContent, '', 'status cleared when the turn leaves working');
  assert.equal(r.turnEl('t1').q.focused, before + 1, 'focus moved to the question');
  assert.equal(r.poll().length, 0, 'polling stopped');
});

test('a finished turn does not take focus from the question box the asker is typing in (screen R2-01)', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  r.win.document.activeElement = r.top.box;
  r.handler = async () => answeredAs('t1');
  const before = r.turnEl('t1').q.focused;
  await r.fire(r.poll()[0]);
  assert.equal(r.turnEl('t1').q.focused, before, 'focus stays in the box');
  r.top.box.value = '';
  await r.ask(r.top, 'Which shows talk about mortgage rates?');
  r.win.document.activeElement = r.win.document.body = { tag: 'body' };
  r.handler = async () => answeredAs('t2');
  const before2 = r.turnEl('t2').q.focused;
  await r.fire(r.poll()[0]);
  assert.equal(r.turnEl('t2').q.focused, before2 + 1, 'focus on the page body moves to the finished question');
});

test('polling stops when the route leaves #/ask', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  r.E.current.route = '#/overview';
  await r.fire(r.poll()[0]);
  assert.equal(r.reads().length, 0);
});

// Code review r2 (CODE-R2-03): a failed read never invents a failed turn; the poll slows down.
test('failed reads keep the turn working and back off 3, 6, 12, then 15 s; a success resets to 1.5 s', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  const next = () => r.timers.filter(t => !t.cleared && !t.fired && t.ms > 0);
  let fail = () => { throw new Error('offline'); };
  r.handler = async () => fail();
  const delays = [];
  for (let i = 0; i < 6; i++) { await r.fire(next()[0]); delays.push(next()[0].ms); }
  assert.deepEqual(delays, [3000, 6000, 12000, 15000, 15000, 15000]);
  fail = () => { throw refusal(503, 'X'); };
  await r.fire(next()[0]);
  fail = () => { throw refusal(429, 'X'); };
  await r.fire(next()[0]);
  fail = () => { throw refusal(408, 'X'); };
  await r.fire(next()[0]);
  assert.ok(!r.turnEl('t1').outerHTML.includes('data-state="failed"'), 'network, 5xx, 429 and 408 never mark it failed');
  r.handler = async () => workingAs('t1', 1);
  await r.fire(next()[0]);
  assert.equal(next()[0].ms, 1500, 'a success resets the delay');
});

test('a read refused with 404 or 403 marks the turn failed and stops polling', async () => {
  for (const status of [404, 403]) {
    const r = await askRig();
    r.okPosts();
    await r.ask(r.top, 'How do hosts pitch VPNs?');
    r.handler = async () => { throw refusal(status, 'NOT_FOUND'); };
    await r.fire(r.poll()[0]);
    assert.ok(r.turnEl('t1').outerHTML.includes('data-state="failed"'), String(status));
    assert.equal(r.timers.filter(t => !t.cleared && !t.fired && t.ms > 0).length, 0);
  }
});

// Code review r2 (CODE-R2-02): an unchanged working read does not redraw the turn.
test('a working read with the same step leaves the turn alone; a new step redraws and keeps focus on Cancel', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  const el = r.turnEl('t1');
  const oldCancel = { dataset: { action: 'ask-cancel' } }, newCancel = { dataset: { action: 'ask-cancel' }, focused: 0, focus() { this.focused += 1; } };
  el.querySelector = s => (s === '.ask-q' ? el.q : s === '[data-action=ask-cancel]' ? newCancel : null);
  el.contains = node => node === oldCancel;
  r.win.document.activeElement = oldCancel;
  el.outerHTML = 'unchanged';
  r.handler = async () => workingAs('t1', 1);
  await r.fire(r.poll()[0]);
  assert.equal(el.outerHTML, 'unchanged', 'same step: no redraw');
  r.handler = async () => workingAs('t1', 2);
  await r.fire(r.poll()[0]);
  assert.notEqual(el.outerHTML, 'unchanged', 'new step: redrawn');
  assert.equal(newCancel.focused, 1, 'Cancel keeps focus across the redraw');
});

test('a follow-up POSTs the thread_id and is appended, not rebuilt; examples and box clear', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  assert.deepEqual(r.posts()[0].body, { question: 'How do hosts pitch VPNs?' });
  assert.equal(r.thread.sets, 1, 'a new thread rebuilds #ask-thread once');
  assert.equal(r.top.box.value, '');
  await r.ask(r.follow, 'And mattresses?');
  assert.deepEqual(r.posts()[1].body, { question: 'And mattresses?', thread_id: 'h1' });
  assert.equal(r.thread.sets, 1, 'no rebuild for a follow-up');
  assert.equal(r.thread.inserted.length, 1);
  assert.equal(r.thread.inserted[0][0], 'beforeend');
  assert.ok(r.thread.inserted[0][1].includes('id="ask-turn-t2"'));
  assert.equal(r.turnEl('t2').scrolled, 1, 'the new turn scrolls into view');
  assert.equal(r.turnEl('t2').q.focused, 1, 'and its question takes focus');
});

test('a brand question is POSTed once per navigation and the route loses ?brand', async () => {
  const r = await askRig();
  r.okPosts();
  r.E.current.route = '#/ask?brand=Quince';
  r.E.current.publishedRoute = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.V.askPage(7);
  assert.equal(r.timers.filter(t => t.ms === 0 && !t.fired).length, 1, 'one pending timer for two renders');
  await r.fire(r.timers.find(t => t.ms === 0));
  assert.deepEqual(r.posts().map(c => c.body), [{ prepared: 'brand:Quince' }]);
  assert.equal(r.E.current.route, '#/ask');
  assert.equal(r.E.current.publishedRoute, '#/ask');
  assert.deepEqual(r.syncs, ['#/ask']);
  await r.V.askPage(7);
  for (const t of r.timers.filter(t => t.ms === 0 && !t.fired)) await r.fire(t);
  assert.equal(r.posts().length, 1, 're-render after the param is gone does not resend');
  r.handler = async () => answeredAs('t1');
  await r.fire(r.poll()[0]);
  r.okPosts();
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  for (const t of r.timers.filter(t => t.ms === 0 && !t.fired)) await r.fire(t);
  assert.equal(r.posts().length, 2, 'a new navigation sends again');
});

// Code review r5 (CODE-R5-01): a brand press never sends over a send in flight or a working
// turn, and ?brand always leaves the route, so no later render sends it unasked.
test('a brand question that finds a send in flight sends nothing and drops ?brand; a later render sends nothing', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  r.top.box.value = 'How do hosts pitch VPNs?';
  const sending = r.V.askSubmit(r.top);
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.find(t => t.ms === 0 && !t.fired));
  assert.equal(r.posts().length, 1, 'only the first send');
  assert.equal(r.E.current.route, '#/ask');
  assert.deepEqual(r.syncs, ['#/ask']);
  post.resolve({ turn_id: 't1', thread_id: 'h1' });
  await sending;
  r.handler = async () => answeredAs('t1');
  await r.fire(r.poll()[0]);
  await r.V.askPage(7);
  for (const t of r.timers.filter(t => t.ms === 0 && !t.fired)) await r.fire(t);
  assert.equal(r.posts().length, 1, 'no unasked brand send on a later render');
});

test('a brand question while a turn works sends nothing, keeps the turn and resumes polling it', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'Q1');
  // The asker goes to the brand page: the pending poll finds #/ask gone and stops.
  r.E.current.route = '#/brand/quince';
  await r.fire(r.poll()[0]);
  assert.equal(r.poll().length, 0);
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.find(t => t.ms === 0 && !t.fired));
  assert.equal(r.posts().length, 1, 'no brand send over t1');
  assert.equal(r.E.current.route, '#/ask');
  assert.equal(r.poll().length, 1, 'polling t1 resumes');
  assert.match(await r.V.askPage(8), /id="ask-turn-t1"/, 't1 kept');
});

test('a refused brand question drops ?brand', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'Q1');
  r.handler = async () => answeredAs('t1');
  await r.fire(r.poll()[0]);
  r.handler = async () => { throw refusal(429, 'ASK_LIMIT_DAY'); };
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.find(t => t.ms === 0 && !t.fired));
  assert.equal(r.posts().length, 2, 'the brand send went out and was refused');
  assert.equal(r.E.current.route, '#/ask');
});

test('a refused brand question shows its sentence and a new click retries', async () => {
  const r = await askRig();
  r.handler = async () => { throw refusal(429, 'ASK_BUSY'); };
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.find(t => t.ms === 0));
  assert.equal(r.top.slot.textContent, 'Ask is busy right now. Try again in a minute.');
  assert.equal(r.E.current.route, '#/ask');
  r.okPosts();
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.filter(t => t.ms === 0 && !t.fired)[0]);
  assert.equal(r.posts().length, 2);
});

test('Ask again resends a brand turn\'s {prepared}; a question turn resends its question in its own thread', async () => {
  const r = await askRig();
  r.okPosts();
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.find(t => t.ms === 0));
  // Ask again shows on a failed or cancelled turn.
  r.handler = async () => Object.assign(workingAs('t1', 1), { state: 'failed' });
  await r.fire(r.poll()[0]);
  r.okPosts();
  const setsBefore = r.thread.sets;
  await r.V.askAgain('t1');
  assert.deepEqual(r.posts()[1].body, { prepared: 'brand:Quince' });
  assert.equal(r.thread.sets, setsBefore + 1, 'a prepared resend replaces the display');
  assert.equal(r.thread.inserted.length, 0);
  r.handler = async () => answeredAs('t2');
  await r.fire(r.poll()[0]);
  r.okPosts();
  await r.ask(r.top, 'Which shows talk about rates?');
  const q = r.posts()[2];
  assert.deepEqual(q.body, { question: 'Which shows talk about rates?' });
  const idQ = 't3';
  const sets = r.thread.sets;
  await r.V.askAgain(idQ);
  assert.deepEqual(r.posts()[3].body, { question: 'Which shows talk about rates?', thread_id: 'h3' });
  assert.equal(r.thread.sets, sets, 'a question resend is appended');
  assert.equal(r.thread.inserted.length, 1);
});

// Code review r3 (CODE-R3-02): a send with no server code is not "busy".
// Screen review r4 (SCR-R4-01): the top box cannot start a new thread over a turn that is still working.
test('while a turn works the top Ask is disabled and a top send does nothing; it unlocks when the turn lands', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  assert.equal(r.top.submit.disabled, true, 'locked while t1 works');
  await r.ask(r.top, 'Which shows talk about mortgage rates?');
  assert.equal(r.posts().length, 1, 'no second send');
  assert.equal(r.top.box.value, 'Which shows talk about mortgage rates?', 'the draft stays');
  r.handler = async () => answeredAs('t1');
  await r.fire(r.poll()[0]);
  assert.equal(r.top.submit.disabled, false, 'unlocked once t1 answered');
  const html = await r.V.askPage(9);
  assert.match(html, /<form id="ask-form"[\s\S]*?<button class="button primary" type="submit">Ask<\/button>/, 'a fresh page with no working turn is unlocked');
});

test('a fresh #/ask render with a working turn draws the top Ask disabled', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  const html = await r.V.askPage(9);
  assert.match(html, /<form id="ask-form"[\s\S]*?<button class="button primary" type="submit" disabled>Ask<\/button>/);
});

// Code review r4 (CODE-R4-01): Ask again on a brand turn starts a new thread, so it waits too.
test('Ask again on a brand turn does nothing while another turn works', async () => {
  const r = await askRig();
  r.okPosts();
  r.E.current.route = '#/ask?brand=Quince';
  await r.V.askPage(7);
  await r.fire(r.timers.find(t => t.ms === 0));
  await r.ask(r.follow, 'And their prices?');
  r.handler = async p => (p.endsWith('/t1') ? Object.assign(answeredAs('t1'), { state: 'failed' }) : workingAs('t2', 1));
  await r.fire(r.poll()[0]);
  r.okPosts();
  await r.V.askAgain('t1');
  assert.equal(r.posts().length, 2, 'no resend while t2 works');
});

// Code review r4 (CODE-R4-05): one resend per turn.
test('Ask again sends once per turn and then its button is disabled', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'Q1');
  r.handler = async () => Object.assign(workingAs('t1', 1), { state: 'failed' });
  await r.fire(r.poll()[0]);
  const again = { disabled: false };
  const el = r.turnEl('t1');
  el.querySelector = s => (s === '.ask-q' ? el.q : s === '[data-action=ask-again]' ? again : null);
  r.okPosts();
  await r.V.askAgain('t1');
  await r.V.askAgain('t1');
  assert.equal(r.posts().length, 2, 'one resend');
  assert.equal(again.disabled, true);
  assert.match(r.V.askTurnHtml(Object.assign(workingAs('t1', 1), { state: 'failed' })), /data-action="ask-again"[^>]*disabled/, 'a redraw keeps it disabled');
});

// Code review r4 (CODE-R4-07): only busy-like refusals say busy.
test('an unknown refusal code with a message shows that message; ASK_BUSY, 5xx and 429 say busy', async () => {
  const busy = 'Ask is busy right now. Try again in a minute.';
  const cases = [
    [Object.assign(new Error('You cannot ask in this workspace.'), { status: 403, body: { error: { code: 'FORBIDDEN' } } }), 'You cannot ask in this workspace.'],
    [refusal(429, 'ASK_BUSY'), busy],
    [Object.assign(new Error('x'), { status: 502, body: { error: { code: 'BAD_GATEWAY' } } }), busy],
  ];
  for (const [err, text] of cases) {
    const r = await askRig();
    r.handler = async () => { throw err; };
    await r.ask(r.top, 'How do hosts pitch VPNs?');
    assert.equal(r.top.slot.textContent, text, err.message);
  }
});

// Code review r4 (CODE-R4-06): a redraw during a send keeps the send buttons disabled.
test('a #/ask render while a send is in flight draws the Ask buttons disabled', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  r.top.box.value = 'Q1';
  const sending = r.V.askSubmit(r.top);
  const html = await r.V.askPage(9);
  assert.equal((html.match(/type="submit" disabled>Ask<\/button>/g) || []).length, 2, 'top and follow-up');
  assert.equal((html.match(/class="button ask-example" data-action="ask-example" [^>]*disabled/g) || []).length, 2, 'examples (CODE-R5-03; two since ASKEX-1)');
  post.resolve({ turn_id: 't1', thread_id: 'h1' });
  await sending;
});

// Code review r4 (CODE-R4-03): attribute and brand escaping.
test('turn_id, state and the brand name cannot break out of attributes', () => {
  const evil = 'x" onfocus="alert(1)';
  const t = Object.assign(clone(ANSWERED), { turn_id: evil, thread_id: evil, state: 'answered' });
  const html = turnHtml(t);
  assert.ok(!html.includes('" onfocus="'), 'turn_id escaped in id, data-target, data-turn');
  assert.ok(html.includes('x&quot; onfocus=&quot;alert(1)'));
  assert.ok(!turnHtml(Object.assign(clone(ANSWERED), { state: evil })).includes('" onfocus="'), 'state escaped');
  const b = views(true).EWViews.askBrandButton({ display: '<img src=x onerror=1>&"', ask_button: true });
  assert.ok(b.includes('What podcasts say about &lt;img src=x onerror=1&gt;&amp;&quot;'), b);
  assert.ok(b.includes('%3Cimg'), 'encoded in the link');
  assert.ok(!b.includes('<img'));
});

// Code review r4 (CODE-R4-04): Cancel, follow-up visibility and examples.
test('Cancel POSTs the cancel once', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'Q1');
  r.handler = async () => ({});
  await r.V.askCancel('t1');
  assert.deepEqual(r.calls.filter(c => c.p.endsWith('/cancel')).map(c => [c.p, c.method]), [['/customer/ask/turns/t1/cancel', 'POST']]);
});

test('the follow-up box shows only when the last turn is answered or partial; examples hide once there are turns', async () => {
  for (const [state, shown] of [['working', false], ['answered', true], ['partial', true], ['nothing', false], ['failed', false], ['cancelled', false], ['hidden', false]]) {
    const r = await askRig();
    r.okPosts();
    const before = await r.V.askPage(9);
    assert.match(before, /<section class="ask-examples">/, 'examples shown with no turns');
    await r.ask(r.top, 'Q1');
    assert.equal(r.examples.hidden, true, 'examples hidden once a turn exists');
    const follow = r.win.document.getElementById('ask-followup');
    if (state !== 'working') { r.handler = async () => Object.assign(answeredAs('t1'), { state }); await r.fire(r.poll()[0]); }
    assert.equal(follow.hidden, !shown, state);
    const html = await r.V.askPage(9);
    assert.match(html, /<section class="ask-examples" hidden>/);
    assert.equal(/<form id="ask-followup"[^>]*hidden>/.test(html), !shown, state + ' on render');
  }
});

test('a send whose outcome is unknown or that was held shows the app\'s own message, not the busy sentence', async () => {
  const cases = [
    [Object.assign(new Error('Your change could not be confirmed.'), { outcomeUnknown: true }), 'Your change could not be confirmed.'],
    [Object.assign(new Error('A workspace switch is in progress. Nothing was saved.'), { status: 0, writeFenced: true }), 'A workspace switch is in progress. Nothing was saved.'],
    [refusal(503, undefined), 'Ask is busy right now. Try again in a minute.'],
    [Object.assign(new Error('Too many requests'), { status: 429 }), 'Ask is busy right now. Try again in a minute.'],
    [new Error(''), 'Ask is busy right now. Try again in a minute.'],
  ];
  for (const [err, text] of cases) {
    const r = await askRig();
    r.handler = async () => { throw err; };
    await r.ask(r.top, 'How do hosts pitch VPNs?');
    assert.equal(r.top.slot.textContent, text, err.message || String(err.status));
  }
});

// Code review r3 (CODE-R3-05): an Ask again refusal shows next to its button.
test('a failed or cancelled turn has its own refusal slot, and an Ask again refusal goes there', async () => {
  for (const name of ['failed', 'cancelled']) assert.match(turnHtml(fixture(name)), /data-action="ask-again"[^>]*>[^<]*<\/button><p class="ask-refusal" role="alert" hidden><\/p>/, name);
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  const slot = { textContent: '', hidden: true };
  const el = r.turnEl('t1');
  el.querySelector = s => (s === '.ask-q' ? el.q : s === '.ask-refusal' ? slot : null);
  r.handler = async () => { throw refusal(429, 'ASK_LIMIT_DAY'); };
  await r.V.askAgain('t1');
  assert.equal(slot.textContent, "You have reached today's limit for Ask. It comes back tomorrow.");
  assert.equal(slot.hidden, false);
  assert.equal(r.top.slot.textContent, '', 'not in the top form');
});

// Code review r3 (CODE-R3-04): rules the earlier tests left open.
test('model step: only the muse-spark- prefix says Muse Spark', () => {
  for (const id of ['muse-2', 'muse-spark', 'xmuse-spark-1']) {
    const html = views().EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { model: id }));
    assert.ok(!html.includes('Muse Spark'), id);
  }
});

test('clip spans that are not numbers render no player and break nothing out', () => {
  const t = clone(ANSWERED);
  t.sources[0].clip = Object.assign({}, t.sources[0].clip, { start_s: '1" onmouseover="x', end_s: '<b>', display_span: ['1" onmouseover="x', '<b>'] });
  const html = turnHtml(t);
  assert.ok(!html.includes('" onmouseover="x'), 'no raw quote break-out');
  assert.ok(!html.includes('<b>"'));
  assert.doesNotMatch(html, /<audio/);
});

/* ASK-PLAY (LEDGER-LEAD3-AUD-1): Play on a source plays the cited read's
   span through the evidence page's wiring: EWUI.playSpan(read, current),
   localMedia's offset and lead-in, never the clip file from its start. */
const askClip = patch => Object.assign({ ref: 'evidence:fx-ev1', playback: 'local' }, patch);
function clipBox(clip, win = views(true)) {
  const t = clone(ANSWERED);
  t.sources[0].clip = clip;
  const html = win.EWViews.askTurnHtml(t);
  const box = /<div class="ask-src-clip ew-clip"([^>]*)>/.exec(html);
  const audio = /<audio [^>]*src="([^"]*)"/.exec(html);
  const attr = name => { const m = box && new RegExp(` data-${name}="([^"]*)"`).exec(box[1]); return m ? m[1] : null; };
  return { html, box, src: audio && audio[1], start: attr('start'), end: attr('end'), offset: attr('offset'), lead: attr('lead') };
}

test('ASK-PLAY: a file at 0 with the read at 28-58 s plays the read from its lead-in', () => {
  const r = clipBox(askClip({ source_offset_s: 0, start_s: 28, end_s: 58, display_span: [28, 58] }));
  assert.deepEqual([r.start, r.end, r.offset, r.lead], ['28', '58', '0', '25']);
  assert.equal(r.src, '/api/v1/customer/records/evidence%3Afx-ev1/media#t=25.0');
});

test('ASK-PLAY: a file at 1200 s with the read at 1228-1258 s seeks 25 s into the file', () => {
  const win = views(true);
  const r = clipBox(askClip({ source_offset_s: 1200, start_s: 1228, end_s: 1258, display_span: [1228, 1258] }), win);
  assert.deepEqual([r.start, r.end, r.offset, r.lead], ['1228', '1258', '1200', '25']);
  assert.ok(r.src.endsWith('/media#t=25.0'), r.src);
  assert.ok(r.html.includes(win.EWUI.timeRangeHtml([1228, 1258])), 'the time range shows the span');
});

test('ASK-PLAY: the display span wins over the cited span (EWUI.playSpan)', () => {
  const win = views(true);
  const r = clipBox(askClip({ source_offset_s: 1200, start_s: 1228, end_s: 1258, display_span: [1230, 1250] }), win);
  assert.deepEqual([r.start, r.end, r.offset, r.lead], ['1230', '1250', '1200', '27']);
  assert.ok(r.src.endsWith('#t=27.0'), r.src);
  assert.ok(r.html.includes(win.EWUI.timeRangeHtml([1230, 1250])));
  // No display span: the cited span.
  const cited = clipBox(askClip({ source_offset_s: 1200, start_s: 1228, end_s: 1258, display_span: null }));
  assert.deepEqual([cited.start, cited.end, cited.lead], ['1228', '1258', '25']);
});

test('ASK-PLAY: the source asks the shared EWUI.playSpan for its span', () => {
  const win = views(true);
  const calls = [];
  win.EWUI.playSpan = (read, current) => { calls.push([read, current]); return [1240, 1245]; };
  const clip = askClip({ source_offset_s: 1200, start_s: 1228, end_s: 1258, display_span: [1230, 1250] });
  const r = clipBox(clip, win);
  assert.equal(calls.length, 1);
  assert.deepEqual(clone(calls[0][0].display_span), [1230, 1250]);
  assert.deepEqual(clone(calls[0][1]), [1228, 1258]);
  assert.deepEqual([r.start, r.end, r.lead], ['1240', '1245', '37']);
});

test('ASK-PLAY: no clip, no playable span or no local media means no player', () => {
  for (const clip of [null, undefined,
    askClip({ source_offset_s: 0, start_s: null, end_s: null, display_span: null }),
    askClip({ source_offset_s: 0, start_s: 58, end_s: 28, display_span: [58, 28] }),
    askClip({ playback: 'unavailable', source_offset_s: 0, start_s: 28, end_s: 58, display_span: [28, 58] }),
    askClip({ ref: '', source_offset_s: 0, start_s: 28, end_s: 58, display_span: [28, 58] })]) {
    const r = clipBox(clip);
    assert.equal(r.box, null, JSON.stringify(clip));
    assert.doesNotMatch(r.html, /<audio|clip-toggle/, JSON.stringify(clip));
  }
});

test('INVALID_QUESTION keeps the typed text and shows the sentence in the form slot; ASK_LIMIT_DAY shows its sentence', async () => {
  const r = await askRig();
  r.handler = async () => { throw refusal(400, 'INVALID_QUESTION'); };
  await r.ask(r.top, '  typed text  ');
  assert.equal(r.top.box.value, '  typed text  ');
  assert.equal(r.top.slot.textContent, 'Questions can be 1 to 300 characters.');
  assert.equal(r.top.slot.hidden, false);
  r.handler = async () => { throw refusal(429, 'ASK_LIMIT_DAY'); };
  await r.ask(r.top, 'again');
  assert.equal(r.top.slot.textContent, "You have reached today's limit for Ask. It comes back tomorrow.");
  assert.equal(r.top.box.value, 'again');
});

test('the count shows 260/300 past 250, is hidden at or below 250, and announces only at 250 and 300', async () => {
  const r = await askRig();
  const type = len => { r.top.box.value = 'x'.repeat(len); r.status.textContent = ''; r.V.onAskInput({ target: r.top.box }); };
  type(260);
  assert.equal(r.count.textContent, '260/300');
  assert.equal(r.count.hidden, false);
  assert.equal(r.status.textContent, '', 'no announcement at 260');
  type(250);
  assert.equal(r.count.hidden, true);
  assert.equal(r.status.textContent, '250/300');
  type(251);
  assert.equal(r.status.textContent, '');
  type(300);
  assert.equal(r.status.textContent, '300/300');
  type(10);
  assert.equal(r.count.hidden, true);
  r.top.box.value = 'x'.repeat(260);
  r.V.onAskInput({ target: r.top.box });
  r.okPosts();
  await r.V.askSubmit(r.top);
  assert.equal(r.count.hidden, true, 'count cleared with the box on success');
  assert.equal(r.count.textContent, '');
});

// Code review r2 (CODE-R2-01): a turn the server accepted is kept when the asker leaves #/ask.
test('a send accepted after the asker left #/ask keeps the turn, draws nothing, and resumes on return', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  r.top.box.value = 'How do hosts pitch VPNs?';
  const sending = r.V.askSubmit(r.top);
  r.E.current.authorityEpoch += 1;
  r.E.current.route = '#/overview';
  post.resolve({ turn_id: 't9', thread_id: 'h9' });
  await sending;
  assert.equal(r.thread.sets, 0);
  assert.equal(r.thread.inserted.length, 0);
  assert.equal(r.poll().length, 0, 'no poll off the page');
  r.E.current.route = '#/ask';
  const html = await r.V.askPage(8);
  assert.ok(html.includes('id="ask-turn-t9"'), 'the turn is back on the page');
  for (const t of r.timers.filter(t => t.ms === 0 && !t.fired)) await r.fire(t);
  assert.equal(r.poll().length, 1, 'polling resumes');
});

test('a send accepted after the asker left and came back to #/ask is drawn and polled', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  r.top.box.value = 'How do hosts pitch VPNs?';
  const sending = r.V.askSubmit(r.top);
  r.E.current.authorityEpoch += 2;
  post.resolve({ turn_id: 't9', thread_id: 'h9' });
  await sending;
  assert.equal(r.thread.sets, 1, 'drawn as a new thread');
  assert.ok(r.thread.html.includes('id="ask-turn-t9"'));
  assert.equal(r.poll().length, 1);
});

test('a send result that arrives after the Ask store was cleared adds no turn and says nothing', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  r.top.box.value = 'How do hosts pitch VPNs?';
  const sending = r.V.askSubmit(r.top);
  r.E.current.authorityEpoch += 1;
  r.E.clearRetainedStores();
  post.resolve({ turn_id: 't9', thread_id: 'h9' });
  await sending;
  assert.equal(r.thread.sets, 0);
  assert.equal(r.thread.inserted.length, 0);
  assert.equal(r.poll().length, 0);
  assert.ok(!(await r.V.askPage(8)).includes('ask-turn-t9'), 'no turn kept');
  assert.equal(r.top.slot.textContent, '');
  const late = defer();
  r.handler = async () => late.promise;
  const again = r.V.askSubmit(r.top);
  r.E.current.authorityEpoch += 1;
  late.reject(refusal(429, 'ASK_LIMIT_DAY'));
  await again;
  assert.equal(r.top.slot.textContent, '', 'a late refusal is dropped too');
  const dropped = defer();
  r.handler = async () => dropped.promise;
  const third = r.V.askSubmit(r.top);
  dropped.reject(Object.assign(new Error('dropped'), { droppedContext: true }));
  await third;
  assert.equal(r.top.slot.textContent, '', 'a dropped-context error says nothing');
});

test('a poll result that arrives after the authority epoch changed changes nothing', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  const read = defer();
  r.handler = async () => read.promise;
  const first = r.poll()[0];
  await r.fire(first);
  r.E.current.authorityEpoch += 1;
  read.resolve(answeredAs('t1'));
  await flush();
  assert.equal(r.turnEl('t1').outerHTML, '');
  assert.equal(r.poll().length, 0);
});

test('double sends: one send at a time; submits, examples and Ask again do nothing while sending; buttons disabled', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  r.top.box.value = 'How do hosts pitch VPNs?';
  const first = r.V.askSubmit(r.top);
  assert.equal(r.top.submit.disabled, true);
  assert.ok(r.exampleButtons.every(b => b.disabled), 'example buttons disabled');
  await r.V.askSubmit(r.top);
  await r.V.askExample('Which shows talk about mortgage rates?');
  await r.V.askAgain('t1');
  assert.equal(r.posts().length, 1);
  post.resolve({ turn_id: 't1', thread_id: 'h1' });
  await first;
  assert.ok(r.exampleButtons.every(b => !b.disabled), 're-enabled after');
  assert.equal(r.top.submit.disabled, true, 'the top Ask stays locked while t1 works (SCR-R4-01)');
  r.handler = async () => answeredAs('t1');
  await r.fire(r.poll()[0]);
  assert.equal(r.top.submit.disabled, false);
  r.okPosts();
  await r.V.askExample('Which shows talk about mortgage rates?');
  assert.equal(r.posts().length, 2, 'an example sends once the first is done');
  assert.equal(r.top.box.value, '');
});

test('a late report result after the epoch changed does not replace the form', async () => {
  const r = await askRig();
  const post = defer();
  r.handler = async () => post.promise;
  const reason = { value: 'The clip is from another episode', name: 'reason' };
  const box = { outerHTML: 'form' };
  const status = { textContent: '' };
  const submit = { disabled: false };
  const form = { dataset: { turn: 't1' }, closest: () => box, querySelector: s => (s === '[role=status]' ? status : s === '[type=submit]' ? submit : reason) };
  const sending = r.V.askReport(form);
  r.E.current.authorityEpoch += 1;
  post.resolve({});
  await sending;
  assert.equal(box.outerHTML, 'form');
  assert.equal(status.textContent, '');
});

// Screen r1 INFO 4: the sent report's Send button is replaced, so focus moves
// to the thanks line (tabindex -1), not to the page body.
test('a sent report moves focus to the thanks line', async () => {
  const r = await askRig();
  r.handler = async () => ({});
  const reason = { value: 'The clip is from another episode', name: 'reason' };
  let focused = 0;
  const thanks = { focus() { focused += 1; } };
  const turn = { querySelector: s => (s === '.report-thanks' ? thanks : null) };
  const box = { outerHTML: 'form', parentNode: turn };
  const form = { dataset: { turn: 't1' }, closest: () => box, querySelector: s => (s === '[role=status]' ? { textContent: '' } : s === '[type=submit]' ? { disabled: false } : reason) };
  await r.V.askReport(form);
  assert.equal(box.outerHTML, '<div class="report-thanks" role="status" tabindex="-1"><b>✓ Your report was sent.</b> <span>Thanks, we\'ll look at it.</span></div>');
  assert.equal(focused, 1, 'focus stayed on the removed Send button (the page body)');
});

test('the Send report button stays disabled until the text is not blank', async () => {
  const r = await askRig();
  assert.match(turnHtml(ANSWERED), /<button type="submit" class="button primary" disabled>Send report<\/button>/);
  const submit = { disabled: true };
  const form = { querySelector: s => (s === '[type=submit]' ? submit : null) };
  const area = { name: 'reason', value: '   ', closest: s => (s === '.ask-report-form' ? form : null) };
  r.V.onAskInput({ target: area });
  assert.equal(submit.disabled, true);
  area.value = ' a ';
  r.V.onAskInput({ target: area });
  assert.equal(submit.disabled, false);
  area.value = '';
  r.V.onAskInput({ target: area });
  assert.equal(submit.disabled, true);
});

test('server fields other than the question are escaped', () => {
  const H = '"><img src=x onerror=alert(1)>';
  const t = clone(ANSWERED);
  t.based_on = H;
  t.sections[0].heading = H;
  t.sections[0].paragraphs[0].text = H;
  t.uncertain = [{ text: H, cites: ['p1'] }];
  t.lines = [H, H];
  t.disclosure.model = H;
  for (const s of t.sources) Object.assign(s, { label: H, show: H, episode: H, time: H, text: H });
  assert.doesNotMatch(turnHtml(t), /<img/);
  assert.doesNotMatch(turnHtml(Object.assign(clone(t), { state: 'partial' })), /<img/);
  const nothing = Object.assign(clone(fixture('nothing')), { lines: [H] });
  assert.doesNotMatch(turnHtml(nothing), /<img/);
});

test('a clip plays only from the customer record media route: its ref is encoded into the path', () => {
  assert.match(turnHtml(ANSWERED), /<audio/);
  for (const ref of ['/media/x.mp3', 'https://evil.example/api/v1/customer/x', '//evil.example/x', '"><b>x']) {
    const t = clone(ANSWERED);
    t.sources[0].clip.ref = ref;
    const src = /<audio [^>]*src="([^"]*)"/.exec(turnHtml(t))[1];
    assert.equal(src.split('#')[0], '/api/v1/customer/records/' + encodeURIComponent(ref) + '/media', ref);
  }
  for (const ref of [null, 7, '']) {
    const t = clone(ANSWERED);
    t.sources[0].clip.ref = ref;
    assert.doesNotMatch(turnHtml(t), /<audio/, String(ref));
  }
});

test('disclosure with bad data: a non-finite count drops its number or its step; a non-finite cost reads pending', () => {
  const V = views().EWViews;
  const withD = patch => V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), patch));
  for (const patch of [{ word_searches: null }, { word_searches: NaN }, { word_searches: '3' }]) {
    const html = withD(patch);
    assert.ok(!html.includes('searches') && html.includes('your question.</li>'), JSON.stringify(patch));
    assert.ok(html.includes('Cost of this answer: $0.04'), 'cost line stays');
  }
  for (const patch of [{ passages_read: 'x' }, { passages_read: Infinity }]) assert.ok(!withD(patch).includes('Found '), JSON.stringify(patch));
  for (const patch of [{ dropped: undefined }, { dropped: Infinity }, { dropped: '2' }]) assert.ok(!withD(patch).includes(CHECKED), JSON.stringify(patch));
  for (const usd of [null, undefined, '0.04', NaN]) assert.ok(withD({ usd_settled: usd }).includes('Cost: pending'), String(usd));
  assert.ok(withD({}).includes(DROPPED_2));
});

// Markup for the screen-reader and look fixes.
test('the page has one persistent polite status outside the thread; the count is aria-hidden', async () => {
  const html = await pageHtml({ available: true });
  const status = '<p id="ask-status" class="visually-hidden" role="status" aria-live="polite"></p>';
  assert.equal(html.split(status).length, 2);
  assert.ok(html.indexOf(status) < html.indexOf('id="ask-thread"'));
  assert.ok(html.includes('<span class="ask-count" aria-hidden="true" hidden></span>'));
  assert.ok(html.includes('class="ask-example-go" aria-hidden="true">→</span>'));
});

test('turn markup: question takes focus, the working wrapper is not a live region, no inline status', () => {
  assert.ok(turnHtml(ANSWERED).includes('<h2 class="ask-q" tabindex="-1">'));
  const working = turnHtml(WORKING);
  assert.match(working, /<div class="ask-working">/);
  assert.doesNotMatch(working, /aria-live|aria-busy/);
});

test('working steps carry done and now tags, and next has none', () => {
  const lis = /<ol class="ask-steps">([\s\S]*?)<\/ol>/.exec(turnHtml(WORKING))[1].match(/<li[^>]*>[\s\S]*?<\/li>/g);
  assert.match(lis[0], /Searching the episodes<span class="ask-step-tag">done<\/span><\/li>/);
  assert.match(lis[1], /Searching again<span class="ask-step-tag">now<\/span><\/li>/);
  assert.doesNotMatch(lis[2], /ask-step-tag/);
});

test('chips stay with their sentence: the last word and the chips share one nowrap span (screen R2-05)', () => {
  const html = turnHtml(ANSWERED);
  const p = ANSWERED.sections[0].paragraphs[0];
  const cut = p.text.lastIndexOf(' ');
  assert.ok(html.includes(`${p.text.slice(0, cut + 1)}<span class="ask-cites">${p.text.slice(cut + 1)}&nbsp;<button`), 'last word, &nbsp;, chips in one span');
  assert.equal((html.match(/<span class="ask-cites">/g) || []).length, 2);
  const one = clone(ANSWERED);
  one.sections[0].paragraphs = [{ text: 'Single', cites: ['p1'] }];
  assert.ok(turnHtml(one).includes('<span class="ask-cites">Single&nbsp;<button'), 'a one-word paragraph');
});

test('lines: never inside the uncertain box; coverage once under Based on, else a paragraph or a list', () => {
  const part = fixture('partial');
  const html = turnHtml(part);
  const block = /<div class="ask-uncertain">[\s\S]*?<\/div>/.exec(html)[0];
  assert.ok(!block.includes(part.lines[0]), 'the coverage line is not in the box');
  assert.ok(block.endsWith('</ul></div>'), 'only the label, the sentence and the items');
  assert.ok(html.includes(`<p class="ask-based">${part.based_on}</p><p class="ask-coverage">${part.lines[0]}</p>`), 'small, under Based on');
  assert.doesNotMatch(html, /class="ask-lines?"/);
  assert.equal(html.split(part.lines[0]).length, 2, 'rendered once');
  const one = Object.assign(clone(ANSWERED), { lines: ['One line.'] });
  assert.ok(turnHtml(one).includes('<p class="ask-line">One line.</p>'));
  const many = Object.assign(clone(ANSWERED), { lines: ['A.', 'B.'] });
  assert.ok(turnHtml(many).includes('<ul class="ask-lines"><li>A.</li><li>B.</li></ul>'));
  const nothing = Object.assign(clone(fixture('nothing')), { lines: ['A.', 'B.'] });
  assert.ok(turnHtml(nothing).includes('<ul class="ask-lines">'));
});

test('sources: the time is an ew-time span, and a clip row pairs the play button with the time range', () => {
  const win = views(true);
  const html = win.EWViews.askTurnHtml(ANSWERED);
  assert.ok(html.includes('<span class="ew-time">12:31</span>'));
  const row = /<div class="ask-src-play">([\s\S]*?)<\/div>/.exec(html);
  assert.ok(row, 'play row');
  assert.ok(row[1].includes('class="ew-play'));
  assert.ok(row[1].includes(win.EWUI.timeRangeHtml([751, 812])));
});

test('search.css carries the Ask look fixes', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const block = css.slice(css.indexOf('/* Ask (T10) */'));
  const rule = sel => [...block.matchAll(new RegExp('(?:^|\\n)' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}', 'g'))].map(m => m[1]).join(' ');
  assert.match(rule('.ask-page'), /padding-inline: 0/);
  assert.match(rule('.ask-cites'), /white-space: nowrap/);
  assert.match(rule('.ask-cite'), /display: ?inline-flex[^}]*align-items: ?center[^}]*min-height: ?28px[^}]*padding: ?2px 8px[^}]*vertical-align: ?middle/);
  assert.match(rule('.ask-cite:focus-visible'), /outline: var\(--ew-focus-ring\); outline-offset: 2px/);
  assert.match(rule('.ask-step-tag'), /font-size: 13px[^}]*font-weight: 400/);
  assert.match(rule('.ask-para, .ask-uncertain ul'), /max-width: 68ch/);
  assert.match(rule('.ask-followup .ask-form-row'), /align-items: stretch/);
  // Screen review r3 (SCR-R3-01): sections and Sources rank alike, below the question.
  assert.match(rule('.ask-section h3, .ask-sources h3'), /font: 600 18px\/24px var\(--ew-serif\)/);
  assert.doesNotMatch(block, /\n\.ask-section h3 \{ font: var\(--ew-t-section\)/);
  assert.match(rule('.ask-lede'), /text-wrap: pretty/);
  assert.match(rule('.ask-src-play'), /display: flex/);
  assert.match(rule('.ask-src-play'), /gap: 8px/);
  assert.match(rule('.ask-src-play'), /align-items: center/);
  assert.match(rule('.ask-example-go'), /margin-left: auto[^}]*color: var\(--ew-action\)/);
  assert.match(rule('.ask-examples .ask-example:hover'), /border-color: var\(--ew-text\)/);
  assert.match(rule('.ask-card h2'), /text-wrap: balance/);
  assert.match(rule('.ask-report summary'), /color: var\(--ew-text-2\)[^}]*font-weight: 400/);
});

// ARV-R1-01 (screen r1): a thread drawn under the paused line keeps the open
// page's 16 px above its first rule (.ask-form margin-bottom there).
test('search.css: the paused line leaves --ew-s4 above a thread drawn under it', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const block = css.slice(css.indexOf('/* Ask (T10) */'));
  const m = block.match(/\n\.ask-off \+ \.ask-thread \{([^}]*)\}/);
  assert.ok(m, 'no .ask-off + .ask-thread rule');
  assert.match(m[1], /^\s*margin-top: var\(--ew-s4\);\s*$/);
  assert.match(block, /\n\.ask-form \{[^}]*margin-bottom: var\(--ew-s4\)/, 'the open page\'s gap this matches');
});

test('search.css: cite chips have a 40px tap area and room between them; the focused question shows no ring (screen R2-02, R2-06)', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const block = css.slice(css.indexOf('/* Ask (T10) */'));
  const rule = sel => [...block.matchAll(new RegExp('(?:^|\\n)' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}', 'g'))].map(m => m[1]).join(' ');
  assert.match(rule('.ask-cite'), /position: relative/);
  assert.match(rule('.ask-cite + .ask-cite'), /margin-left: var\(--ew-s2\)/);
  assert.match(rule('.ask-cite::after'), /content: ''[^}]*position: absolute[^}]*inset: -6px -4px/);
  assert.match(rule('.ask-q:focus'), /outline: none/);
});

test('search.css: passages read as quotes in muted ink; the follow-up has a top rule; report and disclosure sit close (screen R4-02, R4-04)', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const block = css.slice(css.indexOf('/* Ask (T10) */'));
  const rule = sel => [...block.matchAll(new RegExp('(?:^|\\n)' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\{([^}]*)\\}', 'g'))].map(m => m[1]).join(' ');
  assert.match(rule('.ask-src-text'), /color: var\(--ew-text-2\)/);
  assert.match(rule('.ask-src-text::before'), /content: '\u201c'/);
  assert.match(rule('.ask-src-text::after'), /content: '\u201d'/);
  assert.match(rule('.ask-followup'), /border-top: 1px solid var\(--ew-border\)[^}]*padding-top: var\(--ew-s5\)/);
  assert.match(rule('.ask-report + .ask-disclosure'), /margin-top: 0/);
});

/* ASK-UX-R1 (a): line kinds. Server lines are escaped (app.js esc); the fixed strings are raw. */
const esc = v => String(v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const TONE = "Ask can't tell whether people sound positive or negative. Here is what they said. Open a passage to judge its tone.";
const COVER = 'Ask searched the words of 12 of 40 episodes, so it may have missed some passages.';
const NOTE = 'The numbers after each paragraph link to its passages. Check each one.';
const BOX = '<h3>Not clear from the passages</h3>';

test('ASK-UX-R1 (a): the tone note shows once, above the answer, never in the box', () => {
  const t = Object.assign(clone(fixture('partial')), { lines: [COVER, TONE, 'A gate line.'], line_kinds: ['scope', 'tone', 'gate'] });
  const html = turnHtml(t);
  assert.equal(html.split(esc(TONE)).length, 2, 'once');
  assert.ok(html.indexOf(esc(TONE)) < html.indexOf('class="ask-section"'), 'above the answer');
  assert.ok(html.indexOf(NOTE) < html.indexOf(esc(TONE)), 'after the note under Based on');
  assert.ok(html.includes(`<p class="ask-tone">${esc(TONE)}</p>`));
  const block = /<div class="ask-uncertain">[\s\S]*?<\/div>/.exec(html)[0];
  for (const l of [TONE, COVER, 'A gate line.']) assert.ok(!block.includes(esc(l)), l);
  // The untagged kinds follow the answer, outside the box, before the sources.
  assert.ok(html.indexOf('<p class="ask-line">A gate line.</p>') > html.indexOf('</ul></div>'));
  assert.ok(html.indexOf('<p class="ask-line">A gate line.</p>') < html.indexOf('class="ask-sources"'));
});

test('ASK-UX-R1 (a): two coverage or tone lines still show once each', () => {
  const t = Object.assign(clone(ANSWERED), { lines: [COVER, COVER + ' 2', TONE, TONE], line_kinds: ['scope', 'scope', 'tone', 'tone'] });
  const html = turnHtml(t);
  assert.equal(html.split('class="ask-coverage"').length, 2);
  assert.equal(html.split('class="ask-tone"').length, 2);
  assert.ok(!html.includes(COVER + ' 2'));
  assert.doesNotMatch(html, /class="ask-lines?"/);
});

test('ASK-UX-R1 (a): coverage under Based on with no uncertain list; the old note and label are gone', () => {
  const t = Object.assign(clone(ANSWERED), { lines: [COVER], line_kinds: ['scope'] });
  const html = turnHtml(t);
  assert.ok(html.includes(`<p class="ask-based">${t.based_on}</p><p class="ask-coverage">${COVER}</p><p class="ask-note">${NOTE}</p>`));
  assert.ok(!html.includes('Built only from'));
  assert.ok(!html.includes(BOX));
  assert.ok(!html.includes('Less certain'));
});

test('ASK-UX-R1 (a): search.css styles the coverage line small and the box sentence', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  assert.match(css, /\n\.ask-coverage \{ font: var\(--ew-t-meta\);/);
  assert.match(css, /\n\.ask-tone \{/);
  assert.match(css, /\n\.ask-uncertain-why \{/);
  assert.match(css, /\n\.ask-totals-note \{/);
});

/* ASK-UX-R1 (e): answers with totals. */
const TOTALS = "These totals are the collection's ad counts, the same ones Search and Brands show. They don't come from the passages below.";
const READ = 'We found passages about this, but we couldn’t back any claim with their exact words, so we left the claims out. Read the passages:';
const LEAD = 'Passages about this, to read for yourself:';
const COUNTS = [{ kind: 'brand', text: 'Quince has 99 ads in 95 episodes on 73 shows.', rows: [] }];

test('ASK-UX-R1 (e): a totals-only answer: totals line under the counts, no note, no read line, the lead-in before the passages', () => {
  const t = Object.assign(clone(ANSWERED), { sections: [], based_on: null, counts: clone(COUNTS), lines: [READ], line_kinds: ['read'] });
  const html = turnHtml(t);
  assert.equal(html.split(TOTALS).length, 2, 'once');
  assert.ok(html.indexOf(TOTALS) > html.indexOf('class="ask-count"'), 'under the count block');
  assert.ok(html.includes(`</p></div><p class="ask-totals-note">${TOTALS}</p>`), 'directly under it');
  assert.ok(!html.includes(NOTE));
  assert.ok(!html.includes(READ));
  assert.ok(html.includes(`<p class="ask-line">${LEAD}</p><section class="ask-sources">`));
  // No passages: no lead-in.
  const none = turnHtml(Object.assign(clone(t), { sources: [] }));
  assert.ok(!none.includes(LEAD));
  assert.ok(!none.includes(READ));
});

test('ASK-UX-R1 (e): totals with paragraphs: the totals line and the note; no lead-in without the read line', () => {
  const t = Object.assign(clone(ANSWERED), { counts: clone(COUNTS) });
  const html = turnHtml(t);
  assert.equal(html.split(TOTALS).length, 2);
  assert.ok(html.includes(NOTE));
  assert.ok(!html.includes(LEAD));
  assert.ok(html.indexOf(TOTALS) < html.indexOf(NOTE));
});

test('ASK-UX-R1 (e): no totals: no totals line, and the read line stays as it is', () => {
  const t = Object.assign(clone(ANSWERED), { sections: [], lines: [READ], line_kinds: ['read'] });
  const html = turnHtml(t);
  assert.ok(!html.includes(TOTALS));
  assert.ok(html.includes(`<p class="ask-line">${READ}</p>`));
  assert.ok(!html.includes(LEAD));
  assert.ok(!html.includes(NOTE), 'no paragraph shows, so no note');
  assert.ok(!turnHtml(clone(ANSWERED)).includes('ask-totals-note'));
});

/* ASK-UX-R1 (c): live step counts. */
const live = (step, d) => Object.assign(clone(WORKING), { step, disclosure: Object.assign({ model: '', searches: 0, passages_read: 0, dropped: 0, usd_settled: null, cost_shown: true, word_search: false, episodes_searched: null, episodes_in_scope: null }, d) });
const stepTexts = turn => /<ol class="ask-steps">([\s\S]*?)<\/ol>/.exec(turnHtml(turn))[1].match(/<li[^>]*>[\s\S]*?<\/li>/g).map(li => li.replace(/<span class="ask-step-tag">[^<]*<\/span>/, '').replace(/<[^>]*>/g, ''));

test('ASK-UX-R1 (c): before a count exists, the steps have no numbers; never 0', () => {
  const words = ['Searching the episodes', 'Searching again', 'Writing the answer and checking each quote'];
  assert.deepEqual(stepTexts(Object.assign(clone(WORKING), { step: 1, disclosure: null })), words);
  assert.deepEqual(stepTexts(live(1, {})), words, 'zeros read as no count');
  assert.deepEqual(stepTexts(live(2, { passages_read: null, episodes_in_scope: 'x' })), words);
  assert.deepEqual(stepTexts(live(2, { passages_read: -3, episodes_in_scope: NaN })), words);
});

test('ASK-UX-R1 (c): with the counts the server wrote, the steps show them; 1 is singular', () => {
  assert.deepEqual(stepTexts(live(2, { episodes_in_scope: 412, episodes_searched: 412, word_search: true, passages_read: 17, searches: 2 })),
    ['Searching 412 episodes', 'Found 17 passages · searching again', 'Writing the answer and checking each quote']);
  assert.deepEqual(stepTexts(live(1, { episodes_in_scope: 1, passages_read: 1 })).slice(0, 2), ['Searching 1 episode', 'Found 1 passage · searching again']);
});

test('ASK-UX-R1 (c): a new count in the same step redraws and announces the same words; an unchanged read does neither', async () => {
  const r = await askRig();
  r.okPosts();
  await r.ask(r.top, 'How do hosts pitch VPNs?');
  const el = r.turnEl('t1');
  el.outerHTML = 'unchanged';
  r.handler = async () => Object.assign(live(1, {}), { turn_id: 't1', thread_id: 'h1' });
  await r.fire(r.poll()[0]);
  assert.equal(el.outerHTML, 'unchanged', 'same step, same words: no redraw');
  r.handler = async () => Object.assign(live(1, { episodes_in_scope: 412 }), { turn_id: 't1', thread_id: 'h1' });
  await r.fire(r.poll()[0]);
  assert.notEqual(el.outerHTML, 'unchanged', 'the count arrived: redrawn');
  assert.equal(r.status.textContent, 'Searching 412 episodes');
  el.outerHTML = 'unchanged';
  r.status.textContent = 'x';
  await r.fire(r.poll()[0]);
  assert.equal(el.outerHTML, 'unchanged');
  assert.equal(r.status.textContent, 'x', 'no new announcement');
  r.handler = async () => Object.assign(live(2, { episodes_in_scope: 412, passages_read: 9 }), { turn_id: 't1', thread_id: 'h1' });
  await r.fire(r.poll()[0]);
  assert.equal(r.status.textContent, 'Found 9 passages · searching again');
  r.handler = async () => Object.assign(live(3, { episodes_in_scope: 412, passages_read: 9 }), { turn_id: 't1', thread_id: 'h1' });
  await r.fire(r.poll()[0]);
  assert.equal(r.status.textContent, 'Writing the answer and checking each quote');
});

test('ASK-UX-R1 (c): the old step words and any timer are gone', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.ok(!src.includes('Reading the matching passages'));
  assert.ok(!src.includes("'Writing the answer'"));
  const html = turnHtml(live(2, { episodes_in_scope: 412, passages_read: 9 }));
  assert.doesNotMatch(html, /\d+\s*(s|sec|seconds?)\b|elapsed/i);
});

/* ASK-UX-R1 r1 fixes. */
test('ASK-UX-R1 r1 HIGH-1: no passages read (a nothing turn, totals only): no model step, no check step', () => {
  const V = views().EWViews;
  for (const passages_read of [0, null, undefined]) {
    const html = V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { model: 'muse-spark-1', passages_read, dropped: 0 }));
    assert.ok(!html.includes('Muse Spark') && !html.includes('Checked every quote') && !html.includes('No claims removed'), String(passages_read));
    assert.ok(html.includes('Searched the words of 40 episodes'), 'step 1 stays');
  }
  const nothing = turnHtml(Object.assign(clone(fixture('nothing')), { disclosure: Object.assign(clone(fixture('nothing').disclosure), { passages_read: 0 }) }));
  assert.ok(!nothing.includes('Muse Spark') && !nothing.includes('Checked every quote'));
  const read = V.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { passages_read: 1 }));
  assert.ok(read.includes('Muse Spark') && read.includes('Checked every quote'), 'one passage read: both steps');
});

test('ASK-UX-R1 r1 MED-1: step 1 counts word searches only', () => {
  const html = views().EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { searches: 2, word_searches: 1 }));
  assert.ok(html.includes('<li>Searched the words of 40 episodes for words linked to your question (1 search).</li>'), html);
  const none = views().EWViews.askDisclosureHtml(Object.assign(clone(ANSWERED.disclosure), { searches: 5, word_searches: undefined }));
  assert.ok(none.includes('<li>Searched the words of 40 episodes for words linked to your question.</li>'), none);
});

test('ASK-UX-R1 r1 LOW-2: totals with no passages show only the totals line\'s first sentence; with passages, both', () => {
  const FIRST = "These totals are the collection's ad counts, the same ones Search and Brands show.";
  const none = turnHtml(Object.assign(clone(ANSWERED), { sections: [], based_on: null, sources: [], counts: clone(COUNTS), lines: [], line_kinds: [] }));
  assert.ok(none.includes(`<p class="ask-totals-note">${FIRST}</p>`), none);
  assert.ok(!none.includes("They don't come from the passages below."));
  const some = turnHtml(Object.assign(clone(ANSWERED), { counts: clone(COUNTS) }));
  assert.ok(some.includes(`<p class="ask-totals-note">${TOTALS}</p>`));
  assert.ok(TOTALS.startsWith(FIRST + ' '), 'the first sentence is UX\'s own text');
});
