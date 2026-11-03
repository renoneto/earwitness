/* Matches say "Open evidence" (the app's UX-10 rule, tests/frontend_dv3_ux_shell)
   where design section 6 says "View evidence": approved by the lead
   (LEDGER-LEAD3-ALERTS-3 A).
   Meaning alerts A2: the kind choice, the meaning form, the preview states,
   matches and the alert page, from fixtures (the T10 pattern). Contract:
   docs/plans/2026-10-26-meaning-alerts.md section 6 (the strings, byte for
   byte; matches use the app's "Report a mistake") and docs/ALERT-VIEW.md.
   Loaders copied from frontend_ask_screens.test.cjs.
   Run: node --test tests/frontend_alerts_meaning.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'alert-view');
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
async function renderRoute(route, api, { available = true } = {}) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.EW.current.meaningAlertsAvailable = available;
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
function views(available = true) {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = available;
  return win.EWViews;
}
const plain = html => html.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");

const el = dataset => ({ dataset, disabled: false, closest: () => null, getAttribute: () => null });

// Section 6, byte for byte.
const S = {
  words: 'Words', wordsNote: 'Matches what was said, like a saved search',
  meaning: 'Meaning', meaningNote: 'Matches what you describe in a sentence',
  sentence: 'Tell us what to watch for, in a sentence',
  ex1: 'A host says they use the product themselves', ex2: 'A host mentions a price increase',
  lookingFor: 'Looking for:', suggest: 'Suggest search words',
  termsNote: "Search words pick the passages. Meta's AI model then checks each one against your sentence.",
  before: "Write a sentence and press Preview to see what it would have flagged in recent episodes. Previewing doesn't create an alert.",
  working: 'Checking recent episodes…',
  nothing: 'Nothing would have been flagged',
  failed: "Preview didn't run", failedNote: 'This is not “nothing would match”. Your sentence is kept.', tryAgain: 'Try again',
  report: 'Report a mistake', evidence: 'Open evidence', uncertain: 'Uncertain',
  watches: 'Watches for:', shows: 'Shows:', brand: 'Brand:',
  defNote: "Matches are judged by meaning, so each one says why it matched. Meta's AI model can be wrong; wrong ones can be reported.",
  inApp: 'In the app', notAvail: 'Not available for meaning alerts',
  checkNow: 'Check now', checkingNew: 'Checking new episodes…',
  failedBanner: "The last check didn't finish. This is not “no matches”.", checkAgain: 'Check again',
  paused: 'Paused. Check now is off until you resume.', stopped: 'Stopped. This alert can be read but not checked.',
  how: 'How this check was made', muse: 'Model: Muse Spark (Meta)',
  limit: "You have reached today's limit for meaning alerts. It comes back tomorrow.",
  pausedRefusal: 'Meaning alerts are paused right now.',
  busy: 'Meaning alerts are busy right now. Try again in a minute.',   // lead LEDGER-LEAD3-ALERTS-3 D
  didntFinish: "Didn't finish",   // lead LEDGER-LEAD3-ALERTS-3 E
  gone: 'These results are no longer available',
};

test('kind choice: Words and Meaning with their notes, Meaning only when available', () => {
  const html = views(true).meaningKindHtml('words');
  for (const s of [S.words, S.wordsNote, S.meaning, S.meaningNote]) assert.ok(plain(html).includes(s), s);
  assert.match(html, /type="radio"[^>]*name="alert_kind"[^>]*value="words"[^>]*checked/);
  assert.match(html, /type="radio"[^>]*name="alert_kind"[^>]*value="meaning"/);
  assert.equal(views(false).meaningKindHtml('words'), '', 'no choice when meaning alerts are unavailable');
});

test('meaning form: sentence box (200), examples, terms line, note, Preview and Save', () => {
  const html = views().meaningFormHtml({ sentence: '', terms: [], brand: '', shows: [] });
  const text = plain(html);
  for (const s of [S.sentence, S.ex1, S.ex2, S.lookingFor, S.suggest, S.termsNote, S.before]) assert.ok(text.includes(s), s);
  assert.match(html, /<textarea[^>]*name="sentence"[^>]*maxlength="200"/);
  assert.doesNotMatch(text, /A negative mention of/, 'the brand example only with a brand');
  assert.match(html, /data-action="meaning-preview"[^>]*>Preview</);
  assert.match(html, /type="submit"[^>]*>Save alert</);
  const branded = plain(views().meaningFormHtml({ sentence: '', terms: [], brand: 'AG1', shows: [] }));
  assert.ok(branded.includes('A negative mention of AG1'));
});

test('meaning form: the search words are editable chips', () => {
  const html = views().meaningFormHtml({ sentence: 'x', terms: ['AG1', 'greens <b>'], brand: '', shows: [] });
  assert.match(html, /name="terms"[^>]*value="AG1, greens &lt;b&gt;"/);
  assert.doesNotMatch(html, /<b>/);
});

test('preview: before, working, results with counts and dates', () => {
  const V = views();
  assert.ok(plain(V.meaningPreviewHtml(null)).includes(S.before));
  assert.ok(plain(V.meaningPreviewHtml(fixture('run-working'))).includes(S.working));
  const text = plain(V.meaningPreviewHtml(fixture('run-results')));
  assert.ok(text.includes('Checked 64 passages from the last 30 episodes in scope (Sep 14 to Oct 13). 3 would have been flagged; 1 is Uncertain.'), text);
  assert.equal((V.meaningPreviewHtml(fixture('run-results')).match(/class="meaning-match"/g) || []).length, 4);
});

test('preview: nothing would have been flagged, never on a failed run', () => {
  const V = views();
  const text = plain(V.meaningPreviewHtml(fixture('run-nothing')));
  assert.ok(text.includes(S.nothing));
  assert.ok(text.includes('We checked 41 passages from the last 30 episodes in scope (Sep 14 to Oct 13). On a saved alert, press Check now to check new episodes.'));
  const failed = plain(V.meaningPreviewHtml(fixture('run-failed')));
  for (const s of [S.failed, S.failedNote, S.tryAgain]) assert.ok(failed.includes(s), s);
  assert.ok(!failed.includes(S.nothing), 'a failed preview never reads as nothing');
  assert.match(V.meaningPreviewHtml(fixture('run-failed')), /data-action="meaning-preview"[^>]*>Try again</);
});

test('preview: over the cap and not checked lines', () => {
  const text = plain(views().meaningPreviewHtml(fixture('run-overcap')));
  assert.ok(text.includes('Checked the newest 100 of 140 passages. The rest were not checked.'));
  assert.ok(text.includes('2 passages could not be checked.'));
});

test('preview: the limit and paused refusals, and hidden results', () => {
  const V = views();
  assert.ok(plain(V.meaningPreviewHtml(fixture('run-failed-limit'))).includes(S.limit));
  assert.ok(plain(V.meaningPreviewHtml({ refusal: 'ALERT_LIMIT_DAY' })).includes(S.limit));
  assert.ok(plain(V.meaningPreviewHtml({ refusal: 'ALERT_PAUSED' })).includes(S.pausedRefusal));
  assert.ok(plain(V.meaningPreviewHtml(fixture('run-hidden'))).includes(S.gone));
});

test('match: reason, Uncertain only when uncertain, quote, time · show · episode, report and evidence', () => {
  const run = fixture('run-results');
  const V = views();
  const sure = V.meaningMatchHtml(run.matches[0]);
  const text = plain(sure);
  assert.ok(text.includes(run.matches[0].reason));
  assert.ok(text.includes('“' + run.matches[0].quote + '”'));
  assert.ok(text.includes('13:31 · The Morning Run · Episode 41: Training blocks'));
  assert.ok(!text.includes(S.uncertain));
  assert.ok(text.includes(S.report));
  assert.match(sure, /<a [^>]*href="#\/evidence\/sr1%3Aepisode%3Aep1%3Aw%3A752\.0%3A813\.0"[^>]*>Open evidence<\/a>/);
  assert.ok(!plain(sure).includes('View evidence'), 'section 6 "View evidence" is replaced, not added');
  const page = views().meaningAlertHtml(fixture('alert-active'));
  assert.equal((page.match(/>Open evidence<\/a>/g) || []).length, 3, 'every match on the alert page');
  assert.ok(!plain(page).includes('View evidence'));
  assert.ok(plain(V.meaningMatchHtml(run.matches[3])).includes(S.uncertain));
  const noRef = V.meaningMatchHtml({ ...run.matches[0], ref: null });
  assert.ok(!plain(noRef).includes(S.evidence) && !plain(noRef).includes(S.report));
  const hostile = V.meaningMatchHtml({ ...run.matches[0], reason: '<img src=x>', quote: '"><script>' });
  assert.doesNotMatch(hostile, /<img|<script/);
});

test('alert page: definition, delivery, Check now, last checked and the closed disclosure', () => {
  const V = views();
  const html = V.meaningAlertHtml(fixture('alert-active'));
  const text = plain(html);
  for (const s of [S.watches, S.shows, S.brand, S.defNote, S.inApp, S.notAvail, S.checkNow, S.how, S.muse])
    assert.ok(text.includes(s), s);
  assert.ok(text.includes('“A host says they use AG1 themselves”'));
  assert.match(text, /Last checked Oct 29/);
  assert.match(text, /12 passages checked in 1 batch(?!es)/);
  assert.match(text, /Cost of this check: \$0\.02/);
  assert.match(html, /<details[^>]*class="meaning-disclosure"(?![^>]*open)/);
  assert.match(html, /data-action="meaning-check"[^>]*>Check now</);
  assert.doesNotMatch(html, /data-action="edit-alert"/, 'a saved sentence is not edited');
  assert.equal((html.match(/class="meaning-match"/g) || []).length, 3);
});

test('alert page banners: quiet, checking, failed with Check again, paused, stopped', () => {
  const V = views();
  const quiet = plain(V.meaningAlertHtml(fixture('alert-quiet')));
  assert.ok(quiet.includes('No new matches since Sep 29. Earlier matches are below.'));
  assert.ok(plain(V.meaningAlertHtml(fixture('alert-checking'))).includes(S.checkingNew));
  const failed = V.meaningAlertHtml(fixture('alert-failed'));
  assert.ok(plain(failed).includes(S.failedBanner));
  assert.match(failed, /data-action="meaning-check"[^>]*>Check again</);
  const paused = V.meaningAlertHtml(fixture('alert-paused'));
  assert.ok(plain(paused).includes(S.paused));
  assert.match(paused, /data-action="meaning-check"[^>]*disabled/);
  const stopped = V.meaningAlertHtml(fixture('alert-stopped'));
  assert.ok(plain(stopped).includes(S.stopped));
  assert.doesNotMatch(stopped, /data-action="meaning-check"/);
  assert.doesNotMatch(stopped, /data-action="meaning-lifecycle"/);
  assert.ok(!plain(V.meaningAlertHtml(fixture('alert-failed'))).includes('No new matches'),
    'a failed check is never shown as zero matches');
});

test('alert page: cost pending, a model id that is not Muse Spark, hidden checks', () => {
  const alert = fixture('alert-active');
  alert.checks[0].disclosure.usd_settled = null;
  alert.checks[0].disclosure.model = 'other-model-7';
  alert.checks.push({ run_id: 'mr_x', alert_id: alert.alert_id, kind: 'check', state: 'hidden' });
  const text = plain(views().meaningAlertHtml(alert));
  assert.ok(text.includes('Cost: pending'));
  assert.ok(text.includes('Model: other-model-7'));
  assert.ok(text.includes(S.gone));
});

test('alert page: Pause, Resume and Stop only for the maker; others read only', () => {
  const V = views();
  const mine = V.meaningAlertHtml(fixture('alert-active'));
  assert.match(mine, /data-action="meaning-lifecycle"[^>]*data-lifecycle="paused"[^>]*>Pause</);
  assert.match(mine, /data-action="stop-alert"[^>]*data-kind="meaning"[^>]*>Stop alert</);
  assert.match(V.meaningAlertHtml(fixture('alert-paused')), /data-lifecycle="active"[^>]*>Resume</);
  const other = V.meaningAlertHtml(fixture('alert-other-member'));
  assert.doesNotMatch(other, /meaning-lifecycle|stop-alert|delete-alert/);
});

test('route: #/alert/ma_… renders the meaning page from GET /customer/meaning-alerts/<id>', async () => {
  const alert = fixture('alert-active');
  const seen = [];
  const { html } = await renderRoute('#/alert/' + alert.alert_id, p => {
    seen.push(p);
    if (p === '/customer/meaning-alerts/' + alert.alert_id) return alert;
    throw new Error('unexpected request ' + p);
  });
  assert.deepEqual(seen, ['/customer/meaning-alerts/' + alert.alert_id]);
  assert.ok(plain(html).includes(S.watches));
  assert.ok(plain(html).includes(S.meaning), 'the kind shows as Meaning');
});

test('alerts list: a meaning row links to its page and shows its state and last check', async () => {
  const meaningRow = { alert_id: 'ma_' + '1'.repeat(24), kind: 'meaning', sentence: 'A host says <they> use AG1',
    lifecycle: 'active', banner: 'quiet', last_checked_at: '2026-10-29T09:00:41.000000Z', match_count: 3,
    created_at: '2026-09-20T12:00:00Z', can_change: true };
  const word = { id: 1, name: 'Words one', lifecycle: 'active_local', revision: 1, definition_revision: 1,
    query: { mode: 'ads', q: 'x' }, last_successful_at: '2026-10-14T09:00:00Z', baseline_complete: true };
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: [word, meaningRow] };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(html, /href="#\/alert\/ma_1{24}"/);
  assert.ok(plain(html).includes('A host says <they> use AG1'));
  assert.doesNotMatch(html, /<they>/);
  assert.ok(plain(html).includes('Last checked Oct 29'));
  assert.ok(plain(html).includes('Words one'));
});

test('no unapproved text: every meaning string in views.js is a section 6, v3 or M8 string', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  // "Report a mistake" lives in ui.js reportControl only (UX-08, RULES SC-4).
  for (const s of Object.values(S)) if (s !== S.report) assert.ok(src.includes(s), 'missing in views.js: ' + s);
  assert.ok(fs.readFileSync(path.join(APP, 'ui.js'), 'utf8').includes(S.report));
  assert.ok(!src.includes('Nothing is saved by previewing'), 'v3 text that is untrue');
  assert.ok(!src.includes('The alert will keep checking new ones'), 'v3 text that is untrue');
});

// Screen review round 1 (opus): one Check again on a failed check, failed
// rows marked in Recent checks, the brand example follows the brand box,
// "Last checked" once.
test('failed check: one Check again (the banner), the card has none', () => {
  const html = views().meaningAlertHtml(fixture('alert-failed'));
  assert.equal((html.match(/data-action="meaning-check"/g) || []).length, 1);
  assert.match(html, /meaning-banner[\s\S]*data-action="meaning-check"[^>]*>Check again</);
});

test('recent checks: a failed check is marked, a done one is not', () => {
  const alert = fixture('alert-failed');
  const html = views().meaningAlertHtml(alert);
  const failed = alert.checks.filter(c => c.state === 'failed').length;
  assert.ok(failed >= 1);
  assert.equal((html.match(/class="meaning-check is-failed"/g) || []).length, failed);
});

test('"Last checked <date>" shows once on the alert page', () => {
  const text = plain(views().meaningAlertHtml(fixture('alert-active')));
  assert.equal(text.split('Last checked Oct 29').length - 1, 1, text);
});

test('the brand example follows the brand typed into the form', () => {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = true;
  const box = { innerHTML: '' };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } };
  win.document.querySelector = sel => (sel === '#meaning-examples' ? box : dummy);
  win.EWViews.onMeaningBrandInput({ target: { id: 'meaning-brand', value: 'AG1' } });
  assert.ok(plain(box.innerHTML).includes('A negative mention of AG1'));
  assert.ok(plain(box.innerHTML).includes('A host mentions a price increase'));
  win.EWViews.onMeaningBrandInput({ target: { id: 'meaning-brand', value: '  ' } });
  assert.ok(!plain(box.innerHTML).includes('A negative mention of'));
  win.EWViews.onMeaningBrandInput({ target: { id: 'meaning-brand', value: '<b>x' } });
  assert.doesNotMatch(box.innerHTML, /<b>x/);
});

// Code review round 2.
test('over the cap with no matches is never "Nothing would have been flagged"', () => {
  const run = { ...fixture('run-overcap'), matches: [], flagged: 0, uncertain: 0 };
  const text = plain(views().meaningPreviewHtml(run));
  assert.ok(!text.includes(S.nothing), text);
  assert.ok(text.includes('Checked the newest 100 of 140 passages. The rest were not checked.'));
});

for (const code of ['ALERT_BUSY', 'ALERT_NOT_ACTIVE']) {
  test(`Check now refused with ${code}: the page re-renders, never "didn't finish"`, async () => {
    const win = loadViews(canonicalSessionFetch());
    const slot = { innerHTML: '' };
    win.document.querySelector = sel => (sel === '#meaning-progress' ? slot : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
    let renders = 0;
    win.EW.render = () => { renders += 1; };
    win.EW.api = async () => { const e = new Error('refused'); e.status = code === 'ALERT_BUSY' ? 429 : 409; e.body = { error: { code } }; throw e; };
    await win.EWViews.handleAction(el({ action: 'meaning-check', id: 'ma_' + '1'.repeat(24) }));
    assert.equal(renders, 1);
    assert.ok(!slot.innerHTML.includes("didn't finish"), slot.innerHTML);
  });
}

// Screen review round 2.
test('the meaning form focuses the sentence box, not the Words radio', () => {
  const html = views().meaningFormHtml({ sentence: '', terms: [], brand: '', shows: [] });
  assert.match(html, /<textarea[^>]*id="meaning-sentence"[^>]*autofocus/);
  assert.equal((html.match(/autofocus/g) || []).length, 1);
});

test('preview: the live region is the summary line, never the whole list of quotes', () => {
  const form = views().meaningFormHtml({ sentence: '', terms: [], brand: '', shows: [] });
  assert.doesNotMatch(form, /id="meaning-preview"[^>]*aria-live/);
  const html = views().meaningPreviewHtml(fixture('run-results'));
  assert.match(html, /<p class="meaning-summary" role="status">/);
  assert.doesNotMatch(html, /<ul[^>]*(role="status"|aria-live)/);
});

test('recent checks: a failed row carries the "!" mark, not colour alone', () => {
  const html = views().meaningAlertHtml(fixture('alert-failed'));
  assert.match(html, /class="meaning-check is-failed"><span class="meaning-check-mark" aria-hidden="true">!<\/span>/);
});

// Code review round 3.
test('recent checks: a check over the cap or with unchecked passages says so', () => {
  const alert = fixture('alert-active');
  alert.checks[0] = { ...alert.checks[0], candidates: 140, over_cap: 40, not_checked: 2 };
  const text = plain(views().meaningAlertHtml(alert));
  assert.ok(text.includes('Checked the newest 100 of 140 passages. The rest were not checked.'), text);
  assert.ok(text.includes('2 passages could not be checked.'));
});

test('a preview result lands only in the form that asked for it', async () => {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = true;
  const boxes = [{ innerHTML: '', dataset: {} }];
  const form = { querySelector: sel => (sel === '#meaning-sentence' ? { value: 'A sentence' } : null) };
  const timers = [];
  win.setTimeout = fn => { timers.push(fn); return timers.length; };
  win.FormData = class { constructor() {} *entries() { yield ['sentence', 'A sentence']; yield ['terms', 'blender']; } };
  win.document.querySelector = sel => (sel === '#meaning-form' ? form : sel === '#meaning-preview' ? boxes[boxes.length - 1]
    : { innerHTML: '', value: '', dataset: {}, textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
  let release;
  win.EW.api = async (p) => {
    if (p === '/customer/meaning-alerts/preview') { await new Promise(r => { release = r; }); return { run_id: 'mr_' + 'a'.repeat(24) }; }
    return fixture('run-results');
  };
  const started = win.EWViews.handleAction(el({ action: 'meaning-preview' }));
  await new Promise(r => setTimeout(r, 0));
  boxes.push({ innerHTML: 'NEW FORM', dataset: {} });   // the member closed it and opened a new form
  release();
  await started;
  while (timers.length) await timers.shift()();
  assert.equal(boxes[1].innerHTML, 'NEW FORM');
});

// Code review round 4: a second Preview press while one runs sends nothing
// and the first result still lands.
test('a second Preview press while one runs is ignored; the first result lands', async () => {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = true;
  const box = { innerHTML: '', dataset: {} };
  const form = { querySelector: sel => (sel === '#meaning-sentence' ? { value: 'A sentence' } : null) };
  const timers = [];
  win.setTimeout = fn => { timers.push(fn); return timers.length; };
  win.FormData = class { constructor() {} *entries() { yield ['sentence', 'A sentence']; yield ['terms', 'blender']; } };
  win.document.querySelector = sel => (sel === '#meaning-form' ? form : sel === '#meaning-preview' ? box
    : { innerHTML: '', value: '', dataset: {}, textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
  const posts = [];
  let release;
  win.EW.api = async (p) => {
    if (p === '/customer/meaning-alerts/preview') {
      posts.push(p);
      // The server refuses a second preview while the member's first runs.
      if (posts.length > 1) { const e = new Error('busy'); e.status = 429; e.body = { error: { code: 'ALERT_BUSY' } }; throw e; }
      await new Promise(r => { release = r; });
      return { run_id: 'mr_' + 'a'.repeat(24) };
    }
    return fixture('run-results');
  };
  const first = win.EWViews.handleAction(el({ action: 'meaning-preview' }));
  await new Promise(r => setTimeout(r, 0));
  await win.EWViews.handleAction(el({ action: 'meaning-preview' }));
  release();
  await first;
  while (timers.length) await timers.shift()();
  assert.equal(posts.length, 1);
  assert.ok(plain(box.innerHTML).includes('3 would have been flagged'), box.innerHTML);
});

// Screen review round 3: the sentence box keeps the focus after the
// dialog's own first-field focus runs.
test('choosing Meaning leaves the focus in the sentence box', async () => {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = true;
  const timers = [];
  win.setTimeout = fn => { timers.push(fn); return timers.length; };
  let focused = null;
  const sentence = { focus() { focused = 'sentence'; } };
  const modal = { innerHTML: '', open: true, setAttribute() {}, querySelector: sel => (sel === '#meaning-sentence' ? sentence : null) };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
  win.EW.sharedRead = async () => ({ shows: [] });
  // The app's openDialog focuses the first field on a later timer.
  win.EW.openDialog = () => { win.setTimeout(() => { focused = 'first-field'; }); return true; };
  await win.EWViews.handleAction({ dataset: { action: 'alert-kind' }, value: 'meaning', disabled: false, closest: () => null, getAttribute: () => null });
  while (timers.length) await timers.shift()();
  assert.equal(focused, 'sentence');
});

// Code review round 5: a boundary during the poll never leaves Preview dead.
test('after an authority boundary drops the poll, Preview works again in that form', async () => {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = true;
  const box = { innerHTML: '', dataset: {} };
  const form = { querySelector: sel => (sel === '#meaning-sentence' ? { value: 'A sentence' } : null) };
  const timers = [];
  win.setTimeout = fn => { timers.push(fn); return timers.length; };
  win.FormData = class { constructor() {} *entries() { yield ['sentence', 'A sentence']; yield ['terms', 'blender']; } };
  win.document.querySelector = sel => (sel === '#meaning-form' ? form : sel === '#meaning-preview' ? box
    : { innerHTML: '', value: '', dataset: {}, textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
  const posts = [];
  win.EW.api = async (p) => {
    if (p === '/customer/meaning-alerts/preview') { posts.push(p); return { run_id: 'mr_' + String(posts.length).repeat(24) }; }
    return fixture('run-results');
  };
  await win.EWViews.handleAction(el({ action: 'meaning-preview' }));
  win.EW.current.authorityEpoch += 1;   // a boundary (Back with typed work kept)
  while (timers.length) await timers.shift()();
  await win.EWViews.handleAction(el({ action: 'meaning-preview' }));
  assert.equal(posts.length, 2, 'the second press sent its preview');
});


// Lead LEDGER-LEAD3-ALERTS-3 C: the singular for a count of exactly 1; 2 is the control.
test('preview results: "1 passage" when one was checked, "2 passages" for two', () => {
  const V = views();
  const text = n => plain(V.meaningPreviewHtml({ ...fixture('run-results'), checked: n, uncertain: 2 }));
  assert.ok(text(1).includes('Checked 1 passage from the last 30 episodes in scope (Sep 14 to Oct 13). 3 would have been flagged; 2 are Uncertain.'), text(1));
  assert.ok(text(2).includes('Checked 2 passages from the last 30 episodes in scope (Sep 14 to Oct 13). 3 would have been flagged; 2 are Uncertain.'), text(2));
  assert.ok(text(0).includes('Checked 0 passages from'), text(0));
});

test('preview results: "1 is Uncertain" for one, "2 are Uncertain" for two', () => {
  const V = views();
  const text = u => plain(V.meaningPreviewHtml({ ...fixture('run-results'), checked: 64, flagged: 3, uncertain: u }));
  assert.ok(text(1).includes('3 would have been flagged; 1 is Uncertain.'), text(1));
  assert.ok(!text(1).includes('1 are Uncertain'));
  assert.ok(text(2).includes('3 would have been flagged; 2 are Uncertain.'), text(2));
  assert.ok(text(0).includes('3 would have been flagged; 0 are Uncertain.'), text(0));
});

test('not checked: "1 passage could not be checked." for one, "2 passages …" for two, in the preview and Recent checks', () => {
  const V = views();
  const preview = n => plain(V.meaningPreviewHtml({ ...fixture('run-overcap'), not_checked: n }));
  assert.ok(preview(1).includes('1 passage could not be checked.'), preview(1));
  assert.ok(!preview(1).includes('1 passages'));
  assert.ok(preview(2).includes('2 passages could not be checked.'), preview(2));
  const row = n => {
    const alert = fixture('alert-active');
    alert.checks[0] = { ...alert.checks[0], not_checked: n };
    return plain(V.meaningAlertHtml(alert));
  };
  assert.ok(row(1).includes('1 passage could not be checked.'), row(1));
  assert.ok(row(2).includes('2 passages could not be checked.'), row(2));
});

// Lead LEDGER-LEAD3-ALERTS-3 D: ALERT_BUSY (429) gets the approved busy line.
const busyErr = () => { const e = new Error('busy'); e.status = 429; e.body = { error: { code: 'ALERT_BUSY' } }; return e; };
function meaningFormWin() {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = true;
  const box = { innerHTML: '', dataset: {} };
  const form = { querySelector: sel => (sel === '#meaning-sentence' ? { value: 'A sentence' } : null) };
  win.setTimeout = () => 0;
  win.FormData = class { constructor() {} *entries() { yield ['sentence', 'A sentence']; yield ['terms', 'blender']; } };
  win.document.querySelector = sel => (sel === '#meaning-form' ? form : sel === '#meaning-preview' ? box
    : { innerHTML: '', value: '', dataset: {}, textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
  win.EW.api = async () => { throw busyErr(); };
  return { win, box };
}

test('busy: the preview refusal line', () => {
  const html = views().meaningPreviewHtml({ refusal: 'ALERT_BUSY' });
  assert.ok(plain(html).includes(S.busy), html);
  assert.match(html, /role="alert"/);
});

test('busy: a Preview refused with ALERT_BUSY says busy, not "Preview didn\'t run"', async () => {
  const { win, box } = meaningFormWin();
  await win.EWViews.handleAction(el({ action: 'meaning-preview' }));
  assert.ok(plain(box.innerHTML).includes(S.busy), box.innerHTML);
  assert.ok(!plain(box.innerHTML).includes(S.failed));
});

test('busy: Suggest search words refused with ALERT_BUSY says busy', async () => {
  const { win, box } = meaningFormWin();
  await win.EWViews.handleAction(el({ action: 'meaning-suggest' }));
  assert.ok(plain(box.innerHTML).includes(S.busy), box.innerHTML);
});

for (const [label, disabled, shows] of [['another run or both slots (Check now still on)', false, true], ['this alert already checking (Check now off)', true, false]]) {
  test(`busy: Check now refused with ALERT_BUSY, ${label}: the page redraws${shows ? ' and says busy' : ', no busy line'}`, async () => {
    const id = 'ma_' + '1'.repeat(24);
    const win = loadViews(canonicalSessionFetch());
    win.EW.current.route = '#/alert/' + id;
    const slot = { innerHTML: '' };
    const again = { disabled };
    win.document.querySelector = sel => (sel === '#meaning-progress' ? slot : sel === '[data-action="meaning-check"]' ? again
      : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } });
    let renders = 0;
    win.EW.render = async () => { renders += 1; };
    win.EW.api = async () => { throw busyErr(); };
    await win.EWViews.handleAction(el({ action: 'meaning-check', id }));
    assert.equal(renders, 1);
    assert.equal(plain(slot.innerHTML).includes(S.busy), shows, slot.innerHTML);
    assert.ok(!slot.innerHTML.includes("didn't finish"));
  });
}

// Lead LEDGER-LEAD3-ALERTS-3 E: a failed row in Recent checks says "Didn't finish".
test('recent checks: a failed row says "Didn\'t finish" after its date; a done row does not', () => {
  const alert = fixture('alert-failed');
  const html = views().meaningAlertHtml(alert);
  const failed = alert.checks.filter(c => c.state === 'failed').length;
  assert.equal((html.match(/<li class="meaning-check is-failed">[^]*?<\/li>/g) || []).filter(r => plain(r).includes(S.didntFinish)).length, failed, html);
  assert.ok(html.includes("Didn&#39;t finish"), 'escaped like the failed banner');
  const done = views().meaningAlertHtml(fixture('alert-active'));
  assert.ok(!plain(done).includes(S.didntFinish));
});

// Lead LEDGER-LEAD3-ALERTS-4: the three remaining singulars; plurals unchanged.
test('nothing flagged: "We checked 1 passage" for one, "2 passages" for two', () => {
  const V = views();
  const text = n => plain(V.meaningPreviewHtml({ ...fixture('run-nothing'), checked: n }));
  assert.ok(text(1).includes('We checked 1 passage from the last 30 episodes in scope'), text(1));
  assert.ok(!text(1).includes('1 passages'));
  assert.ok(text(2).includes('We checked 2 passages from the last 30 episodes in scope'), text(2));
});

test('disclosure: "1 passage checked in 1 batch" for one, "2 passages checked in 2 batches" for two', () => {
  const V = views();
  const text = (checked, batches) => {
    const a = fixture('alert-active');
    const checks = (a.checks || []).map(c => ({ ...c, disclosure: { ...(c.disclosure || {}), checked, batches } }));
    return plain(V.meaningAlertHtml({ ...a, checks }));
  };
  assert.match(text(1, 1), /1 passage checked in 1 batch(?!es)/);
  assert.doesNotMatch(text(1, 1), /1 passages|1 batches/);
  assert.match(text(2, 2), /2 passages checked in 2 batches/);
  assert.match(text(0, 0), /0 passages checked in 0 batches/);
});
