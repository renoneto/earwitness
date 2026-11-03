/* LIVE-BRIEF-CHECK (#7): "Check this brief" on the placement page (written
   red first). The button and its lines show only with the session's
   brief_check_live_available, the draft's model_checks (the brief_check
   switch), allowed_actions "placements" (the member may change the
   campaign), a pinned campaign brief (draft.pins.brief_versions.campaign)
   and a listed read. The shipped LIVE_BRIEF_TEXT holds UX's approved
   words (ws-ux/nogap-words/LIVE-BRIEF.md, 87e155669), pinned below byte for
   byte and compared with UX's lines; any empty key hides the button and every line. Most tests swap in
   stand-in words (FILL) by rewriting the source they load. Existing words
   otherwise: "Checking…" while it runs, and the boxes above
   (modelCheckIndex / modelCheckHtml; a failed ad "Muse Spark didn't check
   this ad.") after it. The request names the pinned version only; never
   brief text. Row 61 (lead decision): with the press offered, a campaign
   with no Muse Spark check says so in its "What was bought" card.
   Run: node --test tests/frontend_brief_check_live.test.cjs */
'use strict';
process.env.TZ = 'America/Los_Angeles';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const DRAFT = path.join(__dirname, 'fixtures', 'func2', 'episode-verdicts.json');
const SHIPPED = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
// UX's approved words (ws-ux/nogap-words/LIVE-BRIEF.md lines 7-13), byte for byte
// (straight ' as in the app, 87e155669).
const WORDS = {
  button: 'Check with Muse Spark',
  changed: 'The brief or this placement changed after you opened the page, so nothing was checked. Reload the page and press Check with Muse Spark again.',
  overCap: 'Muse Spark checks up to 3 ads at a time, so it checked the first 3 here. Check the others yourself.',
  limit: "Today's limit for Muse Spark checks is reached. Try again tomorrow.",
  paused: 'Muse Spark checks are paused right now. Your campaign is unchanged.',
  busy: 'Another check is running. Try again in a minute.',
  unavailable: "Muse Spark checks aren't available in this workspace.",
  // `stale` (LIVE-BRIEF.md lines 20-23, 18fd7cc9a): switch on, then off.
  stale: 'Muse Spark checked this before the brief or this placement changed. Press Check with Muse Spark to check it again.',
  staleOff: 'Muse Spark checked this before the brief or this placement changed.',
};
// Row 61 with #7 on (LIVE-BRIEF.md line 17).
const NONE_YET = 'This campaign has no Muse Spark check yet. Press Check with Muse Spark on a placement to check its ads against the brief.';
// A string with a straight ' is written in double quotes, as views.js does.
const lit = v => (v.includes("'") ? JSON.stringify(v) : `'${v}'`);
const decl = words => `const LIVE_BRIEF_TEXT = Object.freeze({ ${Object.entries(words).map(([k, v]) => `${k}: ${lit(v)}`).join(', ')} });`;
const SHIPPED_DECL = decl(WORDS);
const FILL = { button: 'FILL-button', changed: 'FILL-changed', overCap: 'FILL-overcap', limit: 'FILL-limit', paused: 'FILL-paused', busy: 'FILL-busy', unavailable: 'FILL-unavailable', stale: 'FILL-stale', staleOff: 'FILL-staleoff' };
const withWords = words => SHIPPED.replace(SHIPPED_DECL, decl(words));
const filled = () => withWords(FILL);
const PIN = { brief_id: 'brief-q', version: 2, digest: 'd'.repeat(64) };
const flush = () => new Promise(r => setTimeout(r, 0));

const read = (id, span) => ({ evidence_id: id, evidence_ref: 'sr1:' + String(id).repeat(8).slice(0, 32).padEnd(32, '0'), machine_code: null, machine_url: null,
  brief_code: null, has_clip: true, display_span: span, review: null });
const SUMMARY = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 0, checked: 0, waiting_review: 0, not_checked: 0, unconfirmed: 1 };
const episode = (eid, reads) => ({ episode_id: eid, show: 'Hey Riddle Riddle', episode_title: '#431: Quince', published: '2026-10-21', verdict: 'heard_unconfirmed',
  mismatch: [], said_code: null, brief_code: null, evidence_id: reads[0].evidence_id, evidence_ref: reads[0].evidence_ref,
  has_clip: true, display_span: reads[0].display_span, waiting_for_review: 0, not_checked_reason: null, review_revision: 1, basis: 'machine', review: null, reads });
function draftWith({ modelChecks = {}, allowed, pin = PIN, archived = false, reads = true, changed } = {}) {
  const d = JSON.parse(fs.readFileSync(DRAFT, 'utf8')).draft;
  const base = d.placements.find(p => p.id === 'pl-win');
  d.placements.push({ ...base, id: '41', requirements: {} });
  if (reads) d.episode_verdicts['41'] = { summary: SUMMARY, episodes: [episode('e431', [read(9101, [960, 1020])])] };
  // null: no model_checks at all (the brief_check switch off).
  if (modelChecks === null) delete d.model_checks; else d.model_checks = modelChecks;
  if (allowed !== undefined) d.allowed_actions = allowed;
  d.pins = { ...(d.pins || {}), brief_versions: pin ? { campaign: pin } : {} };
  d.brief_versions = { campaign: { ...(pin || {}), title: 'Quince podcast ads', provided_text: 'One Quince ad read. Code COZY10.' } };
  if (archived) d.campaign = { ...d.campaign, status: 'archived' };
  if (changed !== undefined) d.live_requirements_changed = changed;
  return d;
}

async function page({ views = filled(), available = true, draft = draftWith(), post, runs = [], route = '#/campaign/1/placement/41' } = {}) {
  const store = {};
  const sb = { console, URLSearchParams, AbortController, location: { hash: '#/overview' }, history: { replaceState() {} }, scrollTo() {},
    fetch: async url => { if (url === '/api/v1/session') return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) }; throw new Error('no net'); },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t } };
  sb.window = sb; sb.globalThis = sb; vm.createContext(sb);
  for (const f of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, f), 'utf8'), sb, { filename: f });
  const win = sb; win.EW.current.route = '#/overview'; win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy; win.document.querySelectorAll = () => [];
  vm.runInContext(views, win, { filename: 'views.js' });
  await flush();
  let drawn = 0; let mainHtml = '';
  const main = { get innerHTML() { return mainHtml; }, set innerHTML(v) { mainHtml = v; drawn += 1; } };
  const slots = new Map();
  win.document.querySelector = sel => {
    if (sel === '#main') return main;
    if (sel === '#live-brief-state') { if (!mainHtml.includes('id="live-brief-state"')) return null; if (!slots.has(drawn)) slots.set(drawn, { textContent: '' }); return slots.get(drawn); }
    return dummy;
  };
  win.EW.header = () => {}; win.EW.toast = () => {};
  win.EW.current.briefCheckLiveAvailable = available;
  const timers = [];
  win.setTimeout = fn => { timers.push(fn); return timers.length; };
  const calls = [];
  let current = draft;
  win.EW.api = async (p, o = {}) => {
    calls.push({ p, method: o.method, body: o.body === undefined ? undefined : JSON.parse(JSON.stringify(o.body)) });
    if (p === '/customer/campaigns/1/draft') return { draft: JSON.parse(JSON.stringify(current)) };
    if (p === '/customer/brief-check' && o.method === 'POST') return post ? post() : { run_id: 'bcr_' + '1'.repeat(24) };
    if (p.startsWith('/customer/brief-check/runs/')) return runs.shift();
    if (p.startsWith('/customer/')) return {};
    throw new Error('unexpected ' + p);
  };
  win.location.hash = route; win.EW.current.route = route;
  await win.EWViews.render();
  const click = () => win.EWViews.handleAction({ dataset: { action: 'live-brief-check', id: '41' }, disabled: false, closest: () => null, getAttribute: () => null });
  const tick = async () => { const fn = timers.shift(); assert.ok(fn, 'a poll is scheduled'); await fn(); await flush(); };
  return { win, html: () => mainHtml, slot: () => (slots.get(drawn) || { textContent: '' }).textContent, click, calls, tick, timers,
    setDraft: d => { current = d; } };
}
const BUTTON = /<button[^>]*data-action="live-brief-check"[^>]*>FILL-button<\/button>/;
const posts = calls => calls.filter(c => c.method === 'POST');

test('shipped: UX\'s seven strings, byte for byte, and the button reads "Check with Muse Spark"', async () => {
  assert.ok(SHIPPED.includes(SHIPPED_DECL), 'LIVE_BRIEF_TEXT holds the approved words exactly');
  assert.equal(SHIPPED.split('const LIVE_BRIEF_TEXT').length, 2);
  assert.ok(WORDS.limit.includes("'") && WORDS.unavailable.includes("'"));
  assert.ok(!SHIPPED_DECL.includes('\u2019') && !SHIPPED_DECL.includes('\u2018'), 'no curly apostrophe (R1 round 3 MED 1)');
  const on = await page({ views: SHIPPED });
  assert.match(on.html(), /<button[^>]*data-action="live-brief-check"[^>]*>Check with Muse Spark<\/button>/);
});

test('shipped: each line is the approved string', async () => {
  for (const [code, key] of [['BRIEF_CHECK_BUSY', 'busy'], ['BRIEF_CHECK_LIMIT_DAY', 'limit'], ['BRIEF_CHECK_PAUSED', 'paused'],
    ['BRIEF_CHECK_UNAVAILABLE', 'unavailable'], ['BRIEF_CHANGED', 'changed']]) {
    const p = await page({ views: SHIPPED, post: () => { const e = new Error(code); e.status = 400; e.body = { error: { code } }; throw e; } });
    await p.click();
    await flush();
    assert.equal(p.slot(), WORDS[key], code);
  }
  const p = await page({ views: SHIPPED, runs: [{ state: 'done', over_cap: 2, brief_changed: false, requirements_changed: false }] });
  await p.click();
  await p.tick();
  assert.equal(p.slot(), WORDS.overCap);
});

// UX's lines (ws-ux/nogap-words/LIVE-BRIEF.md at 87e155669), byte for byte,
// by line number; each string is the line's last `…` span. The file itself is
// compared when it is reachable (repo V, above this checkout).
const UX_REL = path.join('.tmp-uat-20261015', 'launch-readiness', 'ws-ux', 'nogap-words', 'LIVE-BRIEF.md');
const UX_LINES = {
  7: "| `button` | `Check with Muse Spark` |",
  8: "| `changed` | `The brief or this placement changed after you opened the page, so nothing was checked. Reload the page and press Check with Muse Spark again.` |",
  9: "| `overCap` | `Muse Spark checks up to 3 ads at a time, so it checked the first 3 here. Check the others yourself.` |",
  10: "| `limit` | `Today's limit for Muse Spark checks is reached. Try again tomorrow.` |",
  11: "| `paused` | `Muse Spark checks are paused right now. Your campaign is unchanged.` |",
  12: "| `busy` | `Another check is running. Try again in a minute.` |",
  13: "| `unavailable` | `Muse Spark checks aren't available in this workspace.` |",
  17: "- **Row 61** (in-app, campaigns with no Muse Spark check): `…it does not check a brief you add during the pilot. To check this one yourself, fill in Message, Offer, Code or Length on the placement form.` With #7 on, it becomes: `This campaign has no Muse Spark check yet. Press Check with Muse Spark on a placement to check its ads against the brief.` (in-app; App or Meta builds it in the same commit as #7).",
  21: "- Switch on, under the old box: `Muse Spark checked this before the brief or this placement changed. Press Check with Muse Spark to check it again.`",
  22: "- Switch off (if the old box still shows): `Muse Spark checked this before the brief or this placement changed.`",
};
const UX_LINE_OF = { button: 7, changed: 8, overCap: 9, limit: 10, paused: 11, busy: 12, unavailable: 13, LIVE_BRIEF_NONE: 17, stale: 21, staleOff: 22 };
const uxString = n => { const spans = UX_LINES[n].match(/`[^`]+`/g); return spans[spans.length - 1].slice(1, -1); };
function uxFile() {
  for (let d = __dirname; ; d = path.dirname(d)) {
    const f = path.join(d, UX_REL);
    if (fs.existsSync(f)) return f;
    if (path.dirname(d) === d) return null;
  }
}
// Every live-brief string as views.js ships it, read from its own literals.
function shippedStrings() {
  const text = /\n {2}const LIVE_BRIEF_TEXT = Object\.freeze\((\{[^\n]*\})\);\n/.exec(SHIPPED);
  const none = /\n {2}const LIVE_BRIEF_NONE = ('[^'\n]*'|"[^"\n]*");\n/.exec(SHIPPED);
  assert.ok(text && none, 'both declarations found');
  return { ...vm.runInNewContext('(' + text[1] + ')'), LIVE_BRIEF_NONE: vm.runInNewContext(none[1]) };
}

test('UX file: every live-brief string in views.js equals its UX line, byte for byte (R1 round 3 MED 1)', t => {
  const file = uxFile();
  if (file) {
    const lines = fs.readFileSync(file, 'utf8').split('\n');
    for (const [n, line] of Object.entries(UX_LINES)) assert.ok(Buffer.from(lines[n - 1]).equals(Buffer.from(line)), `LIVE-BRIEF.md line ${n}`);
  } else t.diagnostic('LIVE-BRIEF.md not reachable from this checkout; its lines are compared as copied above');
  const shipped = shippedStrings();
  assert.deepEqual(Object.keys(shipped).sort(), Object.keys(UX_LINE_OF).sort(), 'every live-brief string has a UX line');
  for (const [key, n] of Object.entries(UX_LINE_OF)) {
    assert.ok(Buffer.from(shipped[key]).equals(Buffer.from(uxString(n))), `${key} (line ${n}): ${JSON.stringify(shipped[key])}`);
  }
  assert.equal(NONE_YET, uxString(17));
  for (const key of Object.keys(WORDS)) assert.equal(WORDS[key], uxString(UX_LINE_OF[key]), key);
});

test('an empty key hides the button and every line, even with the session flag on', async () => {
  const empty = Object.fromEntries(Object.keys(WORDS).map(k => [k, '']));
  for (const words of [empty, ...Object.keys(WORDS).map(k => ({ ...WORDS, [k]: '' }))]) {
    const on = await page({ views: withWords(words) });
    const off = await page({ views: withWords(words), available: false });
    assert.doesNotMatch(on.html(), /live-brief/, JSON.stringify(words));
    assert.equal(on.html(), off.html());
  }
});

test('with words: the button sits on the placement page, after the episode list', async () => {
  const p = await page();
  assert.match(p.html(), BUTTON);
  assert.match(p.html(), /data-live-brief="41"/);
  assert.ok(p.html().indexOf('data-live-brief') > p.html().indexOf('data-read="9101"'));
});

test('each gate hides the button', async () => {
  const cases = {
    'session flag off': { available: false },
    'flag not exactly true': { available: 'true' },
    'no model_checks (brief_check off)': { draft: draftWith({ modelChecks: null }) },
    'cannot change the campaign': { draft: draftWith({ allowed: ['read', 'stored_reports', 'freeze', 'reports'] }) },
    'no allowed_actions': { draft: draftWith({ allowed: null }) },
    'no pinned brief': { draft: draftWith({ pin: null }) },
    archived: { draft: draftWith({ archived: true }) },
    'no listed read': { draft: draftWith({ reads: false }) },
  };
  for (const [name, opts] of Object.entries(cases)) {
    const p = await page(opts);
    assert.doesNotMatch(p.html(), /live-brief/, name);
  }
});

test('session adoption: the flag is true only for brief_check_live_available === true', () => {
  // LIVE-BRIEF-FB: the flag is one of sessionShown's, which a render adopts.
  assert.match(SHIPPED, /function sessionShown\(session\) \{[^}]*\n      briefCheckLiveAvailable: session\.brief_check_live_available === true,\n/);
  assert.match(SHIPPED, /if \(E\.current\.adoptedSession\) Object\.assign\(E\.current, sessionShown\(E\.current\.adoptedSession\)\);/);
});

test('press: the pinned version only, "Checking…" while it runs, then the page again', async () => {
  const p = await page({ runs: [{ state: 'working' }, { state: 'done', over_cap: 0, brief_changed: false, requirements_changed: false }] });
  const reads = p.calls.length;
  await p.click();
  const [post] = posts(p.calls);
  assert.deepEqual(post, { p: '/customer/brief-check', method: 'POST', body: { campaign_id: 1, placement_id: '41', brief: PIN } });
  assert.doesNotMatch(JSON.stringify(post), /provided_text|One Quince ad read/);
  assert.equal(p.slot(), 'Checking…');
  await p.tick();
  assert.equal(p.calls.at(-1).p, '/customer/brief-check/runs/bcr_' + '1'.repeat(24));
  assert.equal(p.slot(), 'Checking…');
  await p.tick();
  // Done: the page is read again (its boxes show the rows), no extra line.
  assert.equal(p.calls.filter(c => c.p === '/customer/campaigns/1/draft').length, 2);
  assert.ok(p.calls.length > reads);
  assert.equal(p.slot(), '');
  assert.equal(p.timers.length, 0);
});

// `changed` is only for the press-time refusal (LIVE-BRIEF.md line 23). A
// run that ends after the brief or placement changed shows no run line: the
// page is read again and the stale lines cover it (R1 round 3 MED 2).
test('done: over the cap; a brief or requirements change since it was sent shows no run line', async () => {
  for (const [run, line] of [
    [{ state: 'done', over_cap: 2, brief_changed: false, requirements_changed: false }, 'FILL-overcap'],
    [{ state: 'done', over_cap: 0, brief_changed: true, requirements_changed: false }, ''],
    [{ state: 'done', over_cap: 0, brief_changed: false, requirements_changed: true }, ''],
    [{ state: 'done', over_cap: 1, brief_changed: true, requirements_changed: true }, 'FILL-overcap'],
    [{ state: 'failed', error_code: 'LIMIT_DAY' }, 'FILL-limit'],
    [{ state: 'failed', error_code: 'PAUSED' }, 'FILL-paused'],
    [{ state: 'failed', error_code: 'META_HALT' }, 'FILL-paused'],
    [{ state: 'failed', error_code: 'BRIEF_CHANGED', brief_changed: true }, ''],
    [{ state: 'failed', error_code: 'BRIEF_CHANGED', brief_changed: true, requirements_changed: true, over_cap: 0 }, ''],
  ]) {
    const p = await page({ runs: [run] });
    await p.click();
    await p.tick();
    assert.equal(p.slot(), line, JSON.stringify(run));
  }
});

test('refusals: each code shows its line; BRIEF_CHANGED reads the page again', async () => {
  for (const [code, line] of [['BRIEF_CHECK_BUSY', 'FILL-busy'], ['BRIEF_CHECK_LIMIT_DAY', 'FILL-limit'],
    ['BRIEF_CHECK_PAUSED', 'FILL-paused'], ['BRIEF_CHECK_UNAVAILABLE', 'FILL-unavailable'], ['BRIEF_CHANGED', 'FILL-changed'],
    ['INVALID_BRIEF_CHECK', '']]) {
    const p = await page({ post: () => { const e = new Error(code); e.status = 400; e.body = { error: { code } }; throw e; } });
    const before = p.calls.filter(c => c.p === '/customer/campaigns/1/draft').length;
    await p.click();
    await flush();
    assert.equal(p.slot(), line, code);
    assert.equal(p.calls.filter(c => c.p === '/customer/campaigns/1/draft').length, before + (code === 'BRIEF_CHANGED' ? 1 : 0), code);
    assert.equal(p.timers.length, 0, code);
  }
});

test('a failed ad shows the existing failure line beside the button', async () => {
  const checks = { 41: [{ check_id: 7, state: 'failed', evidence_ids: [9101], span_start_s: 960, span_end_s: 1020, model: 'muse-spark-1.3',
    cost_usd: 0.004, quotes_removed: 0, checked_at: '2026-10-30T09:12:44Z', fields: [] }] };
  const p = await page({ draft: draftWith({ modelChecks: checks }) });
  assert.match(p.html(), /Muse Spark didn&#39;t check this ad\.|Muse Spark didn't check this ad\./);
  assert.match(p.html(), BUTTON);
});

test('a dropped answer after leaving the page writes nothing', async () => {
  const p = await page({ runs: [{ state: 'done', over_cap: 2 }] });
  await p.click();
  p.win.location.hash = '#/campaign/1'; p.win.EW.current.route = '#/campaign/1';
  const draws = p.calls.filter(c => c.p === '/customer/campaigns/1/draft').length;
  await p.tick();
  assert.equal(p.calls.filter(c => c.p === '/customer/campaigns/1/draft').length, draws);
});

// ---------------------------------------------------------------- row 61: the campaign page
// The page takes the mode from the campaign read, which these stubs answer
// with {}: the draft's campaign carries it instead.
const supplied = d => ({ ...d, campaign: { ...d.campaign, mode: 'supplied_requirements' } });
const campaign = ({ draft = draftWith(), ...opts } = {}) => page({ route: '#/campaign/1', draft: supplied(draft), ...opts });
const bought = html => {
  const at = html.indexOf('ew-work-brief-card');
  assert.ok(at >= 0, 'the "What was bought" card is drawn');
  return html.slice(at, html.indexOf('</section>', at));
};

test('row 61: with the press offered and no Muse Spark check, "What was bought" says so', async () => {
  for (const checks of [{}, { 41: [] }]) {
    const p = await campaign({ views: SHIPPED, draft: draftWith({ modelChecks: checks }) });
    assert.ok(bought(p.html()).includes(`<p class="section-note">${NONE_YET}</p>`), JSON.stringify(checks));
    assert.equal(p.html().split(NONE_YET).length, 2);
  }
});

test('row 61: with the switch off, no brief_check, no words or a check stored, nothing new shows', async () => {
  const checks = { 41: [{ check_id: 7, state: 'failed', evidence_ids: [9101], span_start_s: 960, span_end_s: 1020, model: 'muse-spark-1.3',
    cost_usd: 0.004, quotes_removed: 0, checked_at: '2026-10-30T09:12:44Z', fields: [] }] };
  const base = await campaign({ views: SHIPPED, available: false });
  for (const [name, opts] of Object.entries({
    'switch off': { available: false },
    'flag not exactly true': { available: 'true' },
    'no model_checks': { draft: draftWith({ modelChecks: null }) },
    'a check stored': { draft: draftWith({ modelChecks: checks }) },
    'no words': { views: withWords({ ...WORDS, button: '' }) },
  })) {
    const p = await campaign({ views: SHIPPED, ...opts });
    bought(p.html());
    assert.ok(!p.html().includes('Muse Spark check yet'), name);
    assert.doesNotMatch(p.html(), /it does not check a brief you add/, name);
    if (name === 'switch off') assert.equal(p.html(), base.html());
  }
  assert.ok(!SHIPPED.includes('does not check a brief you add'), 'the older row 61 text is never in the code');
});

test('row 61: shown only where the button can appear (R1 round 2 LOW 2)', async () => {
  const noReads = () => ({ ...draftWith({ reads: false }), episode_verdicts: {} });
  for (const [name, opts] of Object.entries({
    'a viewer (no placements in allowed_actions)': { draft: draftWith({ allowed: ['read', 'stored_reports', 'freeze', 'reports'] }) },
    'no allowed_actions': { draft: draftWith({ allowed: null }) },
    'no registered brief': { draft: draftWith({ pin: null }) },
    'an archived campaign': { draft: draftWith({ archived: true }) },
    'no placement with a listed read': { draft: noReads() },
  })) {
    const p = await campaign({ views: SHIPPED, ...opts });
    bought(p.html());
    assert.ok(!p.html().includes(NONE_YET), name);
  }
  // The same drafts with the gate met show it (the fixture's reads, pin and rights).
  const p = await campaign({ views: SHIPPED });
  assert.ok(p.html().includes(NONE_YET));
});

// ---------------------------------------------------------------- requirements changed after a reload
test('stale: the exact bytes of both lines', () => {
  assert.equal(WORDS.stale, 'Muse Spark checked this before the brief or this placement changed. Press Check with Muse Spark to check it again.');
  assert.equal(WORDS.staleOff, 'Muse Spark checked this before the brief or this placement changed.');
  assert.ok(SHIPPED.includes(SHIPPED_DECL));
});

test('stale: a stored box whose run sent other requirements gets UX\'s stale line right after it', async () => {
  const checks = { 41: [{ check_id: 7, state: 'failed', evidence_ids: [9101], span_start_s: 960, span_end_s: 1020, model: 'muse-spark-1.3',
    cost_usd: 0.004, quotes_removed: 0, checked_at: '2026-10-30T09:12:44Z', fields: [] }] };
  const after = html => { const box = html.indexOf('data-model-check="7"'); assert.ok(box > 0); return html.indexOf('</div>', box) + '</div>'.length; };
  const again = 'Press Check with Muse Spark to check it again.';
  // Switch on (the button is on this page): the line asks for a new press.
  const on = await page({ views: SHIPPED, draft: draftWith({ modelChecks: checks, changed: [7] }) });
  assert.match(on.html(), /data-action="live-brief-check"/);
  assert.equal(on.html().indexOf(`<p class="ew-mc-stale">${WORDS.stale}</p>`), after(on.html()));
  assert.equal(on.html().split('ew-mc-stale').length, 2);
  assert.ok(!on.html().includes(WORDS.changed), 'changed is only for a refused press');
  // Switch off, the old box still shows: the line without the press.
  const off = await page({ views: SHIPPED, available: false, draft: draftWith({ modelChecks: checks, changed: [7] }) });
  assert.doesNotMatch(off.html(), /data-action="live-brief-check"/);
  assert.equal(off.html().indexOf(`<p class="ew-mc-stale">${WORDS.staleOff}</p>`), after(off.html()));
  assert.equal(off.html().split('ew-mc-stale').length, 2);
  assert.ok(!off.html().includes(again) && !off.html().includes(WORDS.changed));
  for (const [name, opts] of Object.entries({
    'not named (an imported row is never named)': { draft: draftWith({ modelChecks: checks, changed: [8] }) },
    'no list': { draft: draftWith({ modelChecks: checks }) },
    'not a list': { draft: draftWith({ modelChecks: checks, changed: '7' }) },
    'stale empty': { draft: draftWith({ modelChecks: checks, changed: [7] }), views: withWords({ ...WORDS, stale: '' }) },
    'staleOff empty, switch off': { draft: draftWith({ modelChecks: checks, changed: [7] }), available: false, views: withWords({ ...WORDS, staleOff: '' }) },
  })) {
    const p = await page({ views: SHIPPED, ...opts });
    assert.ok(p.html().includes('data-model-check="7"'), name);
    assert.ok(!p.html().includes('ew-mc-stale') && !p.html().includes('ew-mc-changed'), name);
  }
});

// ---------------------------------------------------------------- R1 round 3
const checked = (id, ev, start) => ({ check_id: id, state: 'checked', evidence_ids: [ev], span_start_s: start, span_end_s: start + 60, model: 'muse-spark-1.3',
  cost_usd: 0.004, quotes_removed: 0, checked_at: '2026-10-30T09:12:44Z', fields: [{ field: 'brand', state: 'heard', quote: 'Quince', start_s: start + 1 }] });
const threeReads = d => { d.episode_verdicts['41'] = { summary: SUMMARY, episodes: [episode('e431', [read(9101, [960, 1020]), read(9102, [1500, 1560]), read(9103, [2400, 2460])])] }; return d; };

test('MED 2: a press, the Code set to FERN20 while it runs, done with 3 rows: stale lines, no "nothing was checked"', async () => {
  const p = await page({ views: SHIPPED, draft: threeReads(draftWith()),
    runs: [{ state: 'done', over_cap: 0, brief_changed: false, requirements_changed: true }] });
  await p.click();
  assert.equal(p.slot(), 'Checking…');
  // While the run works, an editor sets the placement's Code to FERN20; the
  // run stores 3 rows against the old requirements.
  const after = threeReads(draftWith({ modelChecks: { 41: [checked(7, 9101, 960), checked(8, 9102, 1500), checked(9, 9103, 2400)] }, changed: [7, 8, 9] }));
  after.placements.find(x => x.id === '41').requirements = { code: 'FERN20' };
  p.setDraft(after);
  await p.tick();
  for (const id of [7, 8, 9]) assert.ok(p.html().includes(`data-model-check="${id}"`), `box ${id}`);
  assert.equal(p.html().split(`<p class="ew-mc-stale">${WORDS.stale}</p>`).length, 4, 'one stale line per box');
  assert.equal(p.slot(), '', 'no run line');
  assert.ok(!p.html().includes(WORDS.changed) && !p.slot().includes('nothing was checked'));
});

// After an in-place review the episode list repaints from the reply; the stale
// line stays (R1 round 3 LOW 1).
function reviewEl(data) {
  const a = {};
  return { dataset: { action: 'episode-review', ...data }, disabled: false, closest: () => null,
    getAttribute: k => (k in a ? a[k] : null), setAttribute: (k, v) => { a[k] = String(v); }, removeAttribute: k => { delete a[k]; } };
}
test('LOW 1: Confirm or Reject on a read keeps the stale line', async () => {
  const checks = { 41: [checked(7, 9101, 960)] };
  for (const [decision, available, line] of [['confirm', true, WORDS.stale], ['reject', true, WORDS.stale], ['confirm', false, WORDS.staleOff]]) {
    const p = await page({ views: SHIPPED, available, draft: draftWith({ modelChecks: checks, changed: [7] }) });
    assert.ok(p.html().includes(`<p class="ew-mc-stale">${line}</p>`), 'drawn on load');
    const list = { dataset: { episodes: '41', part: 'list' }, innerHTML: 'OLD' };
    p.win.document.querySelectorAll = sel => (sel === '[data-episodes]' ? [list] : []);
    const reply = JSON.parse(JSON.stringify(draftWith().episode_verdicts['41']));
    reply.episodes[0].reads[0].review = { decision: decision === 'confirm' ? 'confirmed' : 'rejected', by_name: 'Ana', at: '2026-10-30T10:00:00Z', code: null };
    reply.episodes[0].review_revision = 2;
    const calls = [];
    p.win.EW.api = async (path_, o = {}) => { calls.push(path_); return JSON.parse(JSON.stringify(reply)); };
    await p.win.EWViews.handleAction(reviewEl({ cid: '1', pid: '41', episode: 'e431', evidence: '9101', rev: '1', decision }));
    assert.deepEqual(calls, ['/customer/campaigns/1/placements/41/episodes/e431/review'], decision);
    assert.notEqual(list.innerHTML, 'OLD', 'the list repainted');
    assert.ok(list.innerHTML.includes('data-model-check="7"'), decision);
    assert.ok(list.innerHTML.includes(`<p class="ew-mc-stale">${line}</p>`), `${decision}, switch ${available}`);
    assert.equal(list.innerHTML.split('ew-mc-stale').length, 2);
  }
});

// J6: a placement whose episodes all have empty reads (the common "not heard")
// has no ad to check: no button, and no row 61 (R1 round 3 LOW 2).
const emptyReads = d => {
  d.episode_verdicts['41'] = { summary: SUMMARY, episodes: [{ ...episode('e431', [read(9101, [960, 1020])]), verdict: 'not_heard', reads: [] }] };
  for (const v of Object.values(d.episode_verdicts)) for (const ep of v.episodes) ep.reads = [];
  return d;
};
test('LOW 2 (J6): episodes that all have empty reads: no button and no row 61', async () => {
  const p = await page({ views: SHIPPED, draft: emptyReads(draftWith()) });
  assert.ok(p.html().includes('data-episodes="41"'), 'the episode list is drawn');
  assert.doesNotMatch(p.html(), /live-brief/);
  const c = await campaign({ views: SHIPPED, draft: emptyReads(draftWith()) });
  bought(c.html());
  assert.ok(!c.html().includes(NONE_YET));
});

// J13: the stale line asks for a press only where the button is: a viewer, an
// archived campaign or a missing pin with the switch on gets staleOff.
test('LOW 2 (J13): switch on, no button (viewer, archived, no pin): the stale line without "Press…"', async () => {
  const checks = { 41: [checked(7, 9101, 960)] };
  for (const [name, opts] of Object.entries({
    'a viewer': { allowed: ['read', 'stored_reports', 'freeze', 'reports'] },
    'an archived campaign': { archived: true },
    'no pinned brief': { pin: null },
  })) {
    const p = await page({ views: SHIPPED, available: true, draft: draftWith({ modelChecks: checks, changed: [7], ...opts }) });
    assert.doesNotMatch(p.html(), /data-action="live-brief-check"/, name);
    assert.ok(p.html().includes(`<p class="ew-mc-stale">${WORDS.staleOff}</p>`), name);
    assert.ok(!p.html().includes('Press Check with Muse Spark to check it again.'), name);
  }
});
