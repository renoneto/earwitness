/* FUNC-2: "did my ad run as briefed?", per episode. The placement page
   lists each episode in scope with its verdict; the campaign page tags
   each placement with the episode summary. Unconfirmed machine-heard
   reads carry Confirm / Reject (optional "Code heard"); decided reads
   say who decided and offer Undo review. Owner and editor only.
   A placement with no episodes (scope problem, older server) renders as
   before. Fixture: tests/fixtures/func2/episode-verdicts.json.
   Run: node --test tests/frontend_func2_episodes.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIXTURE = path.join(__dirname, 'fixtures', 'func2', 'episode-verdicts.json');
const freshDraft = () => JSON.parse(fs.readFileSync(FIXTURE, 'utf8')).draft;

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
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
const api = (draft, campaign = {}) => p => {
  if (p === '/customer/campaigns/1') return { campaign: { id: 1, mode: 'supplied_requirements', revision: 4, name: 'Fern Q4 host reads', brand: 'fern', requirement_provenance: {}, allowed_actions: draft.allowed_actions, ...campaign } };
  if (p === '/customer/campaigns/1/draft') return { draft };
  if (p === '/customer/campaigns/1/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};
async function renderRoute(route, { draft = freshDraft(), role, campaign } = {}) {
  const win = loadAt('#/overview', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const main = { innerHTML: '' };
  const toasts = [];
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = m => toasts.push(m);
  const realApi = win.EW.api;
  win.EW.api = async (p) => api(draft, campaign)(p);
  if (role) win.EW.current.workspaceRole = role;
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML, toasts, realApi, main };
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ')
  .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const PLACEMENT = '#/campaign/1/placement/pl-win';
// One episode's row (placement page or campaign row), by its episode id.
const epRow = (html, eid) => {
  const at = html.indexOf(`data-episode="${eid}"`);
  assert.ok(at >= 0, 'row for ' + eid);
  const start = html.lastIndexOf('<li class="ew-ep"', at);
  const ends = [html.indexOf('<li class="ew-ep"', at), html.indexOf('</ol>', at)].filter(i => i >= 0);
  assert.ok(start >= 0 && ends.length, 'row bounds for ' + eid);
  return html.slice(start, Math.min(...ends));
};
const verdictOf = (html, eid) => {
  const m = /<span class="ew-ep-verdict">([^<]*)<\/span>/.exec(epRow(html, eid));
  return m ? m[1] : null;
};
const readRow = (html, ev) => {
  const at = html.indexOf(`data-read="${ev}"`);
  assert.ok(at >= 0, 'read row for ' + ev);
  return html.slice(html.lastIndexOf('<li class="ew-ep-read', at), html.indexOf('</li>', at));
};

// ---- strings, exact (escaped through esc) ---------------------------------

test('FUNC2 placement page: every verdict string is exact and escaped', async () => {
  const { html } = await renderRoute(PLACEMENT);
  assert.equal(verdictOf(html, 'e10'), 'Heard, right code');
  assert.equal(verdictOf(html, 'e11'), 'Heard, wrong code: said FURN40, brief says FERN40; doesn&#39;t match the brief: offer said 20% off, brief says &lt;b&gt;40% off&lt;/b&gt;');
  assert.equal(verdictOf(html, 'e12'), 'Heard, code sounds like FURN&lt;40&gt; (brief: FERN40) · not confirmed');
  assert.equal(verdictOf(html, 'e13'), 'Waiting for review');
  assert.equal(verdictOf(html, 'e14'), 'Not heard in this episode');
  assert.equal(verdictOf(html, 'e15'), 'Not heard in this episode');
  assert.equal(verdictOf(html, 'e16'), 'Not checked yet');
  assert.doesNotMatch(html, /<b>40% off<\/b>|FURN<40>|<b>Co<\/b>|<i>live<\/i>/, 'no raw markup from the payload');
});

test('FUNC2 brief with no code: "Heard"; unconfirmed with no code in the brief: "Heard · not confirmed"', async () => {
  const { html } = await renderRoute('#/campaign/1/placement/pl-ep');
  assert.equal(verdictOf(html, 'e21'), 'Heard');
  assert.equal(verdictOf(html, 'e22'), 'Heard · not confirmed');
});

test('FUNC2 URL, length and no-code-heard mismatches; code/url clauses first', async () => {
  const { html } = await renderRoute('#/campaign/1/placement/pl-url');
  assert.equal(verdictOf(html, 'e41'), 'Heard, wrong URL: said fern.co/&lt;x&gt;, brief says fern.com/40; doesn&#39;t match the brief: length said 45, brief says 60–90');
  assert.equal(verdictOf(html, 'e46'), 'Heard, no code heard (brief: FERN40)');
});

test('FUNC2 other mismatch fields read offer / message / length / position', async () => {
  const draft = freshDraft();
  const ep = draft.episode_verdicts['pl-url'].episodes[0];
  ep.mismatch = [{ field: 'position_s', said: 30, brief: [0, 10] }, { field: 'message', said: 'Sleep <worse>', brief: 'Sleep better' }];
  const { html } = await renderRoute('#/campaign/1/placement/pl-url', { draft });
  assert.equal(verdictOf(html, 'e41'), 'Heard, doesn&#39;t match the brief: position said 30, brief says 0–10; doesn&#39;t match the brief: message said Sleep &lt;worse&gt;, brief says Sleep better');
});

test('FUNC2 an unconfirmed read with a brief code and no machine code', async () => {
  const draft = freshDraft();
  const ep = draft.episode_verdicts['pl-win'].episodes[2];
  ep.machine_code = null;
  const { html } = await renderRoute(PLACEMENT, { draft });
  assert.equal(verdictOf(html, 'e12'), 'Heard, no code heard (brief: FERN40) · not confirmed');
});

test('FUNC2 per-read rows: undecided reads get their own line; decided reads say who decided', async () => {
  const { html } = await renderRoute(PLACEMENT);
  // The read the episode line already describes has no second line.
  assert.doesNotMatch(readRow(html, 'ev-12'), /ew-ep-read-line/);
  assert.match(readRow(html, 'ev-12b'), /<span class="ew-ep-read-line">Heard, no code heard \(brief: FERN40\) · not confirmed<\/span>/);
  assert.match(readRow(html, 'ev-10'), /<span class="ew-ep-read-line">Confirmed by Dana &amp; &lt;b&gt;Co&lt;\/b&gt;<\/span>/);
  assert.match(readRow(html, 'ev-15'), /<span class="ew-ep-read-line">Rejected by Lee<\/span>/);
});

// ---- summary ---------------------------------------------------------------

test('FUNC2 summary: counts and singular/plural, "· K not checked" dropped at 0', async () => {
  const win = await renderRoute(PLACEMENT);
  // F2-SCR-R1-05: an episode whose read waits for confirmation is checked, not heard.
  // F2-SCR-R2-04: so is a waiting_review episode.
  assert.match(win.html, /<p class="ew-ep-summary">Confirmed heard in 2 of 6 checked episodes · 1 episode to confirm · 1 episode waiting for review · 1 episode not checked<\/p>/);
  const ep = await renderRoute('#/campaign/1/placement/pl-ep');
  assert.match(ep.html, /<p class="ew-ep-summary">Confirmed heard in 1 of 3 checked episodes · 1 episode to confirm · 1 episode waiting for review<\/p>/);
  const url = await renderRoute('#/campaign/1/placement/pl-url');
  assert.match(url.html, /<p class="ew-ep-summary">Confirmed heard in 2 of 4 checked episodes · 2 episodes not checked<\/p>/);
});

test('FUNC2 placement page: the status strip shows the summary, never "Ad found" or "Not enough data"', async () => {
  const { html } = await renderRoute(PLACEMENT);
  const strip = /<div class="ew-work-status">([\s\S]*?)<\/div>/.exec(html)[1];
  assert.match(visible(strip), /Confirmed heard in 2 of 6 checked episodes · 1 episode to confirm · 1 episode waiting for review · 1 episode not checked/);
  assert.doesNotMatch(visible(html), /Ad found|Not enough data/);
  assert.ok(html.indexOf('ew-ep-summary') > html.indexOf('ew-work-status'), 'the list sits under the status strip');
});

test('FUNC2 a placement with no episodes (scope problem) renders exactly as before', async () => {
  const { html } = await renderRoute('#/campaign/1/placement/pl-amb');
  assert.match(html, /<div class="ew-work-status"><span class="tag[^"]*">Ad found<\/span>/);
  assert.doesNotMatch(html, /ew-ep-|[Hh]eard in \d/);
  // Same page with the episode_verdicts key gone entirely.
  const draft = freshDraft();
  delete draft.episode_verdicts;
  const old = await renderRoute('#/campaign/1/placement/pl-amb', { draft });
  assert.equal(old.html, html, 'empty episodes = no payload at all');
  // A zero-episode scope with a summary still has nothing to list.
  const zero = freshDraft();
  zero.episode_verdicts['pl-amb'] = { episodes: [], summary: { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 0, checked: 0, waiting_review: 0, not_checked: 0, unconfirmed: 0 } };
  assert.equal((await renderRoute('#/campaign/1/placement/pl-amb', { draft: zero })).html, html, 'no episodes, summary present');
});

// ---- Play and Open evidence -------------------------------------------------

// The episode's evidence ref (the record's current source ref), never its evidence_id.
const refOf = (draft, pid, eid) => draft.episode_verdicts[pid].episodes.find(e => e.episode_id === eid).evidence_ref;
const playRe = ref => new RegExp(`<button type="button" class="ew-play" aria-label="Play clip, [^"]+" data-action="search-play" data-ref="${ref.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}">`);

test('FUNC2 Play only with has_clip, on heard and waiting rows; it is the in-place Search player on the evidence ref', async () => {
  const draft = freshDraft();
  const { html } = await renderRoute(PLACEMENT, { draft });
  for (const eid of ['e10', 'e12']) assert.match(epRow(html, eid), playRe(refOf(draft, 'pl-win', eid)), eid);
  for (const eid of ['e11', 'e13', 'e14', 'e16']) assert.doesNotMatch(epRow(html, eid), /data-action="search-play"/, eid);
});

test('FUNC2 F2-R1-07: no Play on a "Not heard in this episode" row, even with a rejected read that has a clip', async () => {
  const draft = freshDraft();
  const e15 = draft.episode_verdicts['pl-win'].episodes.find(e => e.episode_id === 'e15');
  assert.ok(e15.has_clip && e15.evidence_ref && e15.reads[0].review.decision === 'rejected', 'fixture: rejected read with a clip');
  const { html } = await renderRoute(PLACEMENT, { draft });
  assert.equal(verdictOf(html, 'e15'), 'Not heard in this episode');
  assert.doesNotMatch(epRow(html, 'e15'), /data-action="search-play"|ew-play/);
});

test('FUNC2 Open evidence only on heard and waiting rows, to the evidence ref', async () => {
  const draft = freshDraft();
  const { html } = await renderRoute(PLACEMENT, { draft });
  for (const eid of ['e10', 'e11', 'e12', 'e13']) {
    assert.ok(epRow(html, eid).includes(`href="#/evidence/${encodeURIComponent(refOf(draft, 'pl-win', eid))}"`), eid);
    assert.match(epRow(html, eid), />Open evidence<\/a>/, eid);
  }
  for (const eid of ['e14', 'e15', 'e16']) assert.doesNotMatch(epRow(html, eid), /Open evidence/, eid);
});

test('FUNC2 F2-R1-01: Play and Open evidence use evidence_ref, never evidence_id; no ref, neither', async () => {
  const draft = freshDraft();
  const e10 = draft.episode_verdicts['pl-win'].episodes[0];
  e10.evidence_id = 'pipe:obs-7:r1';
  e10.evidence_ref = 'sr1:0123456789abcdef0123456789abcdef';
  e10.reads[0].evidence_ref = e10.evidence_ref; // FIX3: the read row carries the actions
  const { html } = await renderRoute(PLACEMENT, { draft });
  const row = epRow(html, 'e10');
  assert.match(row, playRe('sr1:0123456789abcdef0123456789abcdef'));
  assert.ok(row.includes('href="#/evidence/sr1%3A0123456789abcdef0123456789abcdef"'));
  assert.doesNotMatch(row, /data-ref="pipe|#\/evidence\/pipe/);
  // The review request still carries the raw evidence_id.
  assert.match(readRow(html, 'ev-10'), /data-evidence="ev-10"/);
  const none = freshDraft();
  // FIX3: reads carry their own ref and actions, so no ref anywhere.
  for (const ep of none.episode_verdicts['pl-win'].episodes) { delete ep.evidence_ref; for (const r of ep.reads || []) delete r.evidence_ref; }
  const bare = (await renderRoute(PLACEMENT, { draft: none })).html;
  for (const eid of ['e10', 'e11', 'e12', 'e13']) assert.doesNotMatch(epRow(bare, eid), /data-action="search-play"|Open evidence/, eid);
  assert.equal(verdictOf(bare, 'e10'), 'Heard, right code', 'the line stays');
});

test('FUNC2 a row shows its date and show · episode, escaped; an unknown date says so', async () => {
  const { html } = await renderRoute(PLACEMENT);
  assert.match(visible(epRow(html, 'e10')), /Oct 20 Morning Show · Ep\. 10 <i>live<\/i>/);
  assert.match(epRow(html, 'e10'), /Ep\. 10 &lt;i&gt;live&lt;\/i&gt;/);
  assert.match(visible(epRow(html, 'e16')), /Date unknown/);
});

// ---- Confirm / Reject / Undo review controls -------------------------------

const controls = row => (row.match(/data-action="episode-review"[^>]*data-decision="(\w+)"/g) || []).map(s => /data-decision="(\w+)"/.exec(s)[1]);

test('FUNC2 owner/editor controls: undecided Confirm + Reject with Code heard; decided offers the other choice + Undo review', async () => {
  for (const role of [undefined, 'owner', 'editor']) {
    const { html } = await renderRoute(PLACEMENT, { role });
    assert.deepEqual(controls(readRow(html, 'ev-12')), ['confirm', 'reject'], String(role));
    assert.deepEqual(controls(readRow(html, 'ev-12b')), ['confirm', 'reject'], String(role));
    assert.deepEqual(controls(readRow(html, 'ev-10')), ['reject', 'undo'], String(role));
    assert.deepEqual(controls(readRow(html, 'ev-15')), ['confirm', 'undo'], String(role));
    const r12 = readRow(html, 'ev-12');
    assert.match(r12, /<label class="ew-ep-code">Code heard <input type="text" name="code" maxlength="40" autocomplete="off"><\/label>/);
    assert.match(r12, />Confirm<\/button>/);
    assert.match(r12, />Reject<\/button>/);
    assert.match(readRow(html, 'ev-10'), />Undo review<\/button>/);
    assert.doesNotMatch(readRow(html, 'ev-10'), /Code heard/, 'no Code heard without Confirm');
    assert.match(r12, /data-cid="1" data-pid="pl-win" data-episode="e12" data-evidence="ev-12" data-rev="1"/);
  }
});

test('FUNC2 viewer sees every line and no control', async () => {
  const { html } = await renderRoute(PLACEMENT, { role: 'viewer' });
  assert.doesNotMatch(html, /data-action="episode-review"|Code heard|Undo review/);
  assert.equal(verdictOf(html, 'e12'), 'Heard, code sounds like FURN&lt;40&gt; (brief: FERN40) · not confirmed');
  assert.match(readRow(html, 'ev-10'), /Confirmed by Dana/);
  assert.match(readRow(html, 'ev-12b'), /Heard, no code heard \(brief: FERN40\) · not confirmed/);
  const camp = await renderRoute('#/campaign/1', { role: 'viewer' });
  assert.doesNotMatch(camp.html, /data-action="episode-review"|Code heard|Undo review/);
});

// ---- campaign page ---------------------------------------------------------

const rowOf = (html, id) => html.split('<li class="ew-work-placement">').slice(1).find(c => c.includes('placement/' + encodeURIComponent(id) + '"'));

test('FUNC2 campaign row tag shows the summary; scope-problem placement keeps its label', async () => {
  const { html } = await renderRoute('#/campaign/1');
  const tagOf = id => /<div class="ew-work-pl-out"><span class="ew-work-tags">(?:<span[^>]*data-part="tag"[^>]*>)?<span class="tag[^"]*">([^<]*)<\/span>/.exec(rowOf(html, id))[1];
  assert.equal(tagOf('pl-win'), 'Confirmed heard in 2 of 6 checked episodes · 1 episode to confirm · 1 episode waiting for review · 1 episode not checked');
  assert.equal(tagOf('pl-ep'), 'Confirmed heard in 1 of 3 checked episodes · 1 episode to confirm · 1 episode waiting for review');
  assert.equal(tagOf('pl-url'), 'Confirmed heard in 2 of 4 checked episodes · 2 episodes not checked');
  assert.equal(tagOf('pl-amb'), 'Ad found');
  for (const id of ['pl-win', 'pl-ep', 'pl-url']) assert.doesNotMatch(visible(rowOf(html, id)), /Ad found|Not enough data/, id);
});

test('FUNC2 campaign verdict line and filters count by the new rule', async () => {
  const { html } = await renderRoute('#/campaign/1');
  assert.match(html, /<p class="ew-work-verdict">4 of 4 counted placements have a result: 4 with an ad found\.<\/p>/);
  assert.match(html, /data-filter="possible" aria-pressed="false">Ads found · 4<\/button>/);
  assert.match(html, /data-filter="needs-attention" aria-pressed="false">Needs attention · 3<\/button>/);
});

test('FUNC2 campaign verdict: unconfirmed waits, fully not-heard is a result, not-checked is not enough data', async () => {
  const draft = freshDraft();
  const s = draft.episode_verdicts;
  s['pl-win'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 3, checked: 3, waiting_review: 0, not_checked: 0, unconfirmed: 1 };
  s['pl-ep'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 2, checked: 2, waiting_review: 0, not_checked: 0, unconfirmed: 0 };
  s['pl-url'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 1, checked: 1, waiting_review: 0, not_checked: 4, unconfirmed: 0 };
  const { html } = await renderRoute('#/campaign/1', { draft });
  assert.match(html, /<p class="ew-work-verdict">2 of 4 counted placements have a result: 1 with an ad found and 1 with no matching ad in the available transcript; 1 with ads waiting for review; 1 without enough data\.<\/p>/);
  assert.match(html, /Ads found · 1<\/button>/);
  assert.match(html, /Needs attention · 2<\/button>/);
});

test('FUNC2 campaign row: the reads to review (and decided ones) with their controls', async () => {
  const { html } = await renderRoute('#/campaign/1');
  const win = rowOf(html, 'pl-win');
  assert.match(win, /data-episode="e12"/);
  assert.match(win, /Heard, code sounds like FURN&lt;40&gt; \(brief: FERN40\) · not confirmed/);
  assert.deepEqual(controls(readRow(win, 'ev-12')), ['confirm', 'reject']);
  assert.deepEqual(controls(readRow(win, 'ev-10')), ['reject', 'undo']);
  assert.deepEqual(controls(readRow(win, 'ev-15')), ['confirm', 'undo']);
  // F2-SCR-R1-10: checked episodes with no read are listed as not heard.
  // F2-SCR-R2-04: and waiting_review episodes.
  assert.doesNotMatch(win, /data-episode="e16"/, 'only episodes with reads, checked and not heard, or waiting');
  assert.deepEqual(controls(readRow(rowOf(html, 'pl-ep'), 'ev-22')), ['confirm', 'reject']);
});

// ---- actions ---------------------------------------------------------------

// A button element as the click handler sees it, inside a read row with a
// Code heard input.
function reviewEl(data, { code = '' } = {}) {
  const input = { value: code };
  const row = { querySelector: sel => (sel === 'input[name="code"]' ? input : null) };
  const a = {};
  return { dataset: { action: 'episode-review', ...data }, disabled: false,
    getAttribute: k => (k in a ? a[k] : null), setAttribute: (k, v) => { a[k] = String(v); }, removeAttribute: k => { delete a[k]; },
    closest: sel => (sel === '.ew-ep-read' ? row : null), input };
}
// Containers that repaint from the returned payload.
function blocks(win) {
  const made = [];
  const el = (pid, part) => { const b = { dataset: { episodes: pid, part }, innerHTML: 'OLD' }; made.push(b); return b; };
  const all = [el('pl-win', 'tag'), el('pl-win', 'list'), el('pl-ep', 'list')];
  win.document.querySelectorAll = sel => (sel === '[data-episodes]' ? all : []);
  return made;
}
const updated = () => {
  const v = freshDraft().episode_verdicts['pl-win'];
  const e12 = v.episodes[2];
  e12.verdict = 'heard_wrong';
  e12.mismatch = [{ field: 'code', said: 'FURN40', brief: 'FERN40' }];
  e12.reads[0].review = { decision: 'confirmed', by_name: 'Ana', at: '2026-10-27T20:00:00Z', code: null };
  e12.review_revision = 2;
  v.summary = { heard: 3, heard_right: 1, heard_wrong: 2, not_heard: 2, checked: 5, waiting_review: 1, not_checked: 1, unconfirmed: 0 };
  return v;
};
const data12 = { cid: '1', pid: 'pl-win', episode: 'e12', evidence: 'ev-12', rev: '1' };

test('FUNC2 Confirm posts the route with the displayed revision, then repaints from the reply', async () => {
  const t = await renderRoute(PLACEMENT);
  const made = blocks(t.win);
  const calls = [];
  t.win.EW.api = async (p, opts = {}) => { calls.push({ p, method: opts.method, body: opts.body }); return updated(); };
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].p, '/customer/campaigns/1/placements/pl-win/episodes/e12/review');
  assert.equal(calls[0].method, 'POST');
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify({ evidence_id: 'ev-12', decision: 'confirm', expected_revision: 1 }));
  const [tagBox, listBox, other] = made;
  assert.match(visible(tagBox.innerHTML), /Confirmed heard in 3 of 6 checked episodes · 1 episode waiting for review · 1 episode not checked/);
  assert.match(listBox.innerHTML, /<span class="ew-ep-verdict">Heard, wrong code: said FURN40, brief says FERN40<\/span>/);
  assert.match(listBox.innerHTML, /Confirmed by Ana/);
  assert.equal(other.innerHTML, 'OLD', 'another placement is untouched');
  assert.deepEqual(t.toasts, []);
});

test('FUNC2 Confirm sends a typed Code heard (trimmed); Reject and Undo send no code', async () => {
  const t = await renderRoute(PLACEMENT);
  blocks(t.win);
  const bodies = [];
  t.win.EW.api = async (p, opts = {}) => { bodies.push(opts.body); return updated(); };
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }, { code: '  FURN40 ' }));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'reject' }, { code: 'FURN40' }));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'undo', evidence: 'ev-10', episode: 'e10', rev: '3' }));
  assert.equal(JSON.stringify(bodies[0]), JSON.stringify({ evidence_id: 'ev-12', decision: 'confirm', expected_revision: 1, code: 'FURN40' }));
  assert.equal(JSON.stringify(bodies[1]), JSON.stringify({ evidence_id: 'ev-12', decision: 'reject', expected_revision: 1 }));
  assert.equal(JSON.stringify(bodies[2]), JSON.stringify({ evidence_id: 'ev-10', decision: 'undo', expected_revision: 3 }));
});

test('FUNC2 a Code heard over 40 characters is never sent', async () => {
  const t = await renderRoute(PLACEMENT);
  const made = blocks(t.win);
  let calls = 0;
  t.win.EW.api = async () => { calls++; return updated(); };
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }, { code: 'X'.repeat(41) }));
  assert.equal(calls, 0);
  assert.equal(t.toasts.length, 1, 'the existing error toast');
  assert.match(t.toasts[0], /^Your change was not saved\./);
  assert.ok(made.every(b => b.innerHTML === 'OLD'));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }, { code: 'X'.repeat(40) }));
  assert.equal(calls, 1, '40 is allowed');
});

test('FUNC2 refusals (403, 400, 409, 428) leave the rows and show the existing error toast', async () => {
  for (const status of [403, 400, 409, 428]) {
    const t = await renderRoute(PLACEMENT);
    const made = blocks(t.win);
    t.win.EW.api = async () => { throw Object.assign(new Error('refused'), { status }); };
    const el = reviewEl({ ...data12, decision: 'reject' });
    await t.win.EWViews.handleAction(el);
    assert.ok(made.every(b => b.innerHTML === 'OLD'), String(status));
    assert.equal(t.toasts.length, 1, String(status));
    assert.match(t.toasts[0], /^Your change was not saved/, String(status));
    assert.equal(el.disabled, false, 'the control is usable again');
  }
});

test('FUNC2 the review write carries the switch-revision header and CSRF token (real E.api)', async () => {
  const t = await renderRoute(PLACEMENT);
  blocks(t.win);
  const writes = [];
  t.win.fetch = async (url, opts = {}) => {
    writes.push({ url, method: opts.method, headers: opts.headers || {}, body: opts.body });
    return { ok: true, status: 200, text: async () => JSON.stringify(updated()) };
  };
  t.win.EW.api = t.realApi;
  t.win.EW.current.hosted = true;
  t.win.EW.current.csrf = 'tok-9';
  t.win.EW.current.displayedWorkspaceSwitchRevision = 7;
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }));
  const w = writes.find(x => x.method === 'POST');
  assert.ok(w, 'a POST went out');
  assert.equal(w.url, '/api/v1/customer/campaigns/1/placements/pl-win/episodes/e12/review');
  assert.equal(w.headers['X-EarWitness-Workspace-Switch-Revision'], '7');
  assert.equal(w.headers['X-CSRF-Token'], 'tok-9');
  assert.deepEqual(t.toasts, []);
});

test('FUNC2 campaign page: a decision repaints the row tag, its reads and the verdict line', async () => {
  const t = await renderRoute('#/campaign/1');
  const made = blocks(t.win);
  made[1].dataset.part = 'reviews';
  const verdict = { textContent: 'OLD' };
  t.win.document.querySelector = sel => (sel === '.ew-work-verdict' ? verdict : null);
  // F2-R1-03: the filter labels repaint from the same data as the verdict line.
  const filterEls = ['all', 'possible', 'needs-attention'].map(f => ({ dataset: { action: 'campaign-filter', filter: f }, textContent: 'OLD' }));
  const boxes = t.win.document.querySelectorAll;
  t.win.document.querySelectorAll = sel => (sel === '.ew-work-filter' ? filterEls : boxes(sel));
  const labels = () => filterEls.map(b => b.textContent);
  t.win.EW.api = async () => updated();
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }));
  assert.match(visible(made[0].innerHTML), /Confirmed heard in 3 of 6 checked episodes · 1 episode waiting for review/);
  assert.match(made[1].innerHTML, /Confirmed by Ana/);
  assert.equal(verdict.textContent, '4 of 4 counted placements have a result: 4 with an ad found.');
  assert.deepEqual(labels(), ['All placements · 4', 'Ads found · 4', 'Needs attention · 3']);
  // A reply that moves the counts: pl-win now heard nowhere, nothing left to check.
  const none = updated();
  none.summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 5, checked: 5, waiting_review: 0, not_checked: 0, unconfirmed: 0 };
  t.win.EW.api = async () => none;
  await t.win.EWViews.handleAction(reviewEl({ ...data12, rev: '2', decision: 'reject' }));
  assert.equal(verdict.textContent, '4 of 4 counted placements have a result: 3 with an ad found and 1 with no matching ad in the available transcript.');
  assert.deepEqual(labels(), ['All placements · 4', 'Ads found · 3', 'Needs attention · 2']);
});

// ---- FUNC2-FIX1 (code r1) ---------------------------------------------------

// F2-R1-02: a placement with episodes keeps the old outcome line and grouped
// reasons in Technical details only; one without episodes is unchanged.
function withReasons() {
  const draft = freshDraft();
  const evs = draft.allocation.evaluations;
  const rc = draft.reason_codes.evaluations;
  for (const [pid, n] of [['pl-win', 3], ['pl-ep', 1], ['pl-amb', 3]]) {
    evs[pid] = { ...evs[pid], outcome: 'supported', reasons: ['not reviewed as ad', 'advertiser unconfirmed'], occurrence_order: Array.from({ length: n }, (_, i) => `o-${pid}-${i}`) };
    rc[pid] = [{ code: 'not_reviewed_ad' }, { code: 'attribution_unconfirmed' }];
  }
  return draft;
}
const OLD_OUTCOME = /Requirement outcome:|matches here still need|The match here isn't checked yet|\d+ waiting for review/;

test('FUNC2 F2-R1-02 placement page: with episodes, no visible outcome line or grouped reasons (Technical details keep them)', async () => {
  for (const pid of ['pl-win', 'pl-ep']) {
    const { html } = await renderRoute('#/campaign/1/placement/' + pid, { draft: withReasons() });
    assert.match(html, /ew-ep-summary/, pid);
    assert.doesNotMatch(visible(html), OLD_OUTCOME, pid);
    assert.match(html, /<details class="tech-details">[\s\S]*Outcome <span class="mono">supported<\/span>[\s\S]*not reviewed as ad/, pid + ' technical details');
  }
});

test('FUNC2 F2-R1-02 placement page: no episodes (scope problem) still shows the outcome line and grouped reasons', async () => {
  const { html } = await renderRoute('#/campaign/1/placement/pl-amb', { draft: withReasons() });
  assert.match(visible(html), /Requirement outcome: /);
  assert.match(visible(html), /The matches here still need: ad review; advertiser confirmation\./);
});

test('FUNC2 F2-R1-02 campaign row: with episodes, no visible outcome line or grouped reasons; no episodes unchanged', async () => {
  const { html } = await renderRoute('#/campaign/1', { draft: withReasons() });
  for (const pid of ['pl-win', 'pl-ep']) {
    assert.doesNotMatch(visible(rowOf(html, pid)), OLD_OUTCOME, pid);
    assert.match(rowOf(html, pid), /Outcome <span class="mono">supported<\/span>/, pid + ' technical details');
  }
  assert.match(visible(rowOf(html, 'pl-amb')), /Requirement outcome: /);
  assert.match(visible(rowOf(html, 'pl-amb')), /The matches here still need: ad review; advertiser confirmation\./);
});

// F2-R1-06: fallbacks.
test('FUNC2 F2-R1-06 heard_wrong with an empty mismatch list: "Heard, doesn\'t match the brief"', async () => {
  for (const mismatch of [[], undefined]) {
    const draft = freshDraft();
    draft.episode_verdicts['pl-win'].episodes[1].mismatch = mismatch;
    const { html } = await renderRoute(PLACEMENT, { draft });
    assert.equal(verdictOf(html, 'e11'), 'Heard, doesn&#39;t match the brief', String(mismatch));
  }
});

test('FUNC2 F2-R1-06 an empty or missing reviewer name: "Confirmed" / "Rejected", no "by"', async () => {
  for (const name of ['', null, undefined]) {
    const draft = freshDraft();
    const eps = draft.episode_verdicts['pl-win'].episodes;
    eps[0].reads[0].review.by_name = name;
    eps[5].reads[0].review.by_name = name;
    const { html } = await renderRoute(PLACEMENT, { draft });
    assert.match(readRow(html, 'ev-10'), /<span class="ew-ep-read-line">Confirmed<\/span>/, String(name));
    assert.match(readRow(html, 'ev-15'), /<span class="ew-ep-read-line">Rejected<\/span>/, String(name));
    assert.doesNotMatch(readRow(html, 'ev-10') + readRow(html, 'ev-15'), / by\b/, String(name));
  }
});

// F2-R1-04: after a 409 the page data is read again, so the next click
// carries the new revision.
test('FUNC2 F2-R1-04 409: the existing toast, the draft is read again, and the next click sends the new expected_revision', async () => {
  const t = await renderRoute(PLACEMENT);
  const made = blocks(t.win);
  const fresh = freshDraft();
  fresh.episode_verdicts['pl-win'].episodes[2].review_revision = 5;
  const posts = [], gets = [];
  t.win.EW.api = async (p, opts = {}) => {
    if (opts.method === 'POST') {
      posts.push(opts.body);
      if (posts.length === 1) throw Object.assign(new Error('stale'), { status: 409 });
      return updated();
    }
    gets.push(p);
    return api(fresh)(p);
  };
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'reject' }));
  assert.equal(posts[0].expected_revision, 1);
  assert.deepEqual(t.toasts, ['Your change was not saved: it changed meanwhile.']);
  assert.ok(gets.includes('/customer/campaigns/1/draft'), 'the draft is read again');
  const list = made[1].innerHTML;
  const rev = /data-evidence="ev-12" data-rev="(\d+)"/.exec(list);
  assert.ok(rev, 'the list repainted from the new draft');
  assert.equal(rev[1], '5');
  await t.win.EWViews.handleAction(reviewEl({ ...data12, rev: rev[1], decision: 'reject' }));
  assert.equal(posts[1].expected_revision, 5);
});

// ---- FUNC2-FIX2 (code r2) ---------------------------------------------------

// F2-R2-01: with episode data, the "can count here" and "already count(s)
// for another placement" lines sit in Technical details only; with no
// episode data, or a scope problem, they stay visible as before.
function withCounts(draft = freshDraft()) {
  const evs = draft.allocation.evaluations;
  for (const pid of ['pl-win', 'pl-ep', 'pl-amb']) {
    evs[pid] = { ...evs[pid], outcome: 'supported', eligible_ids: ['occ-' + pid], allocated_elsewhere: ['occ-x-' + pid, 'occ-y-' + pid] };
  }
  return draft;
}
const COUNT_LINES = /matching occurrences? can count here|matching occurrences? already counts? for another placement/;
const techOf = html => (String(html).match(/<details class="tech-details">[\s\S]*?<\/details>/g) || []).join('\n');
const assertCountsHidden = (html, label) => {
  assert.doesNotMatch(visible(html), COUNT_LINES, label);
  const tech = techOf(html);
  assert.match(tech, /1 matching occurrence can count here\./, label + ' technical details');
  assert.match(tech, /2 matching occurrences already count for another placement\./, label + ' technical details');
};
const assertCountsShown = (html, label) => {
  assert.match(visible(html), /1 matching occurrence can count here\./, label);
  assert.match(visible(html), /2 matching occurrences already count for another placement\./, label);
  // F2-R3-01: Technical details unchanged too (no second copy of the lines).
  assert.doesNotMatch(techOf(html), COUNT_LINES, label + ' technical details');
};

test('FUNC2 F2-R2-01 placement page: with episodes, the count lines are in Technical details only', async () => {
  for (const pid of ['pl-win', 'pl-ep']) {
    const { html } = await renderRoute('#/campaign/1/placement/' + pid, { draft: withCounts() });
    assert.match(html, /ew-ep-summary/, pid);
    assertCountsHidden(html, pid);
  }
});

test('FUNC2 F2-R2-01 campaign row: with episodes, the count lines are in Technical details only', async () => {
  const { html } = await renderRoute('#/campaign/1', { draft: withCounts() });
  for (const pid of ['pl-win', 'pl-ep']) assertCountsHidden(rowOf(html, pid), pid);
});

test('FUNC2 F2-R2-01 unchanged: a scope problem or no episode data keeps the count lines visible', async () => {
  // Scope problem: pl-amb has no episodes.
  assertCountsShown((await renderRoute('#/campaign/1/placement/pl-amb', { draft: withCounts() })).html, 'pl-amb page');
  assertCountsShown(rowOf((await renderRoute('#/campaign/1', { draft: withCounts() })).html, 'pl-amb'), 'pl-amb row');
  // No episode data at all (an older server).
  const bare = () => { const d = withCounts(); delete d.episode_verdicts; return d; };
  assertCountsShown((await renderRoute(PLACEMENT, { draft: bare() })).html, 'pl-win page, no episode data');
  assertCountsShown(rowOf((await renderRoute('#/campaign/1', { draft: bare() })).html, 'pl-win'), 'pl-win row, no episode data');
});

// F2-R2-02: Play on a waiting_review row with a clip and an evidence ref.
test('FUNC2 F2-R2-02 Play shows on a waiting_review row with a clip, on its evidence ref', async () => {
  const draft = freshDraft();
  const e23 = draft.episode_verdicts['pl-ep'].episodes.find(e => e.episode_id === 'e23');
  assert.ok(e23.verdict === 'waiting_review' && e23.has_clip && e23.display_span && e23.evidence_ref, 'fixture: waiting row with a clip');
  const { html } = await renderRoute('#/campaign/1/placement/pl-ep', { draft });
  assert.equal(verdictOf(html, 'e23'), 'Waiting for review');
  assert.match(epRow(html, 'e23'), playRe(e23.evidence_ref));
  assert.ok(epRow(html, 'e23').includes(`href="#/evidence/${encodeURIComponent(e23.evidence_ref)}"`));
});

// F2-R2-03: the draft is read again only after a 409, and only the
// reviewed placement repaints.
test('FUNC2 F2-R2-03 no re-read of the draft after success or a non-409 refusal', async () => {
  for (const status of [null, 403, 400, 428, 500]) {
    const t = await renderRoute(PLACEMENT);
    blocks(t.win);
    const gets = [];
    t.win.EW.api = async (p, opts = {}) => {
      if (opts.method !== 'POST') { gets.push(p); return api(freshDraft())(p); }
      if (status) throw Object.assign(new Error('refused'), { status });
      return updated();
    };
    await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'reject' }));
    assert.deepEqual(gets, [], String(status));
  }
});

test('FUNC2 F2-R2-03 409 on the campaign page repaints the reviewed placement (not the first one)', async () => {
  const t = await renderRoute('#/campaign/1');
  const made = blocks(t.win);
  const fresh = freshDraft();
  fresh.episode_verdicts['pl-ep'].episodes[1].review_revision = 6;
  fresh.episode_verdicts['pl-win'].episodes[2].review_revision = 9;
  const posts = [];
  t.win.EW.api = async (p, opts = {}) => {
    if (opts.method === 'POST') { posts.push(opts.body); throw Object.assign(new Error('stale'), { status: 409 }); }
    return api(fresh)(p);
  };
  await t.win.EWViews.handleAction(reviewEl({ cid: '1', pid: 'pl-ep', episode: 'e22', evidence: 'ev-22', rev: '0', decision: 'confirm' }));
  assert.equal(posts.length, 1);
  assert.deepEqual(t.toasts, ['Your change was not saved: it changed meanwhile.']);
  const [winTag, winList, epList] = made;
  assert.equal(winTag.innerHTML, 'OLD', 'pl-win untouched');
  assert.equal(winList.innerHTML, 'OLD', 'pl-win untouched');
  const rev = /data-evidence="ev-22" data-rev="(\d+)"/.exec(epList.innerHTML);
  assert.ok(rev, 'pl-ep repainted from the new draft');
  assert.equal(rev[1], '6');
  assert.doesNotMatch(epList.innerHTML, /data-episode="e1\d"/, 'no pl-win rows in pl-ep');
});

// ---- FUNC2-FIX3 (screen r1) -------------------------------------------------

// F2-SCR-R1-01: every read row names itself: its time span, the brand its
// card shows, and its own Play and Open evidence (by evidence_ref).
const readText = (html, ev) => visible(readRow(html, ev)).trim();

test('FUNC2 F2-SCR-R1-01 each read row shows its time span and its card brand, escaped', async () => {
  const draft = freshDraft();
  const [r12, r12b] = draft.episode_verdicts['pl-win'].episodes[2].reads;
  r12.brand = { key: 'fern', display: 'Fern & <i>Co</i>', confirmed: true };
  r12b.brand = { key: null, display: null, under_review: true };
  r12b.display_span = [1500, 1530.4];
  const { html } = await renderRoute(PLACEMENT, { draft });
  assert.match(readRow(html, 'ev-12'), /5:00–5:32/);
  assert.match(readRow(html, 'ev-12'), /Fern &amp; &lt;i&gt;Co&lt;\/i&gt;/);
  assert.doesNotMatch(readRow(html, 'ev-12'), /<i>Co<\/i>/);
  assert.match(readRow(html, 'ev-12b'), /25:00–25:30/);
  assert.match(readRow(html, 'ev-12b'), /Brand not identified/);
  // A read with no brand and no span shows neither, and nothing invented.
  const plain = (await renderRoute(PLACEMENT)).html;
  assert.doesNotMatch(readRow(plain, 'ev-12b'), /–|Brand not identified/);
  assert.match(readRow(plain, 'ev-10'), /1:01–1:32/);
});

test('FUNC2 F2-SCR-R1-01 each read row has its own Play (with a clip) and Open evidence, on its own ref', async () => {
  const draft = freshDraft();
  const [r12, r12b] = draft.episode_verdicts['pl-win'].episodes[2].reads;
  for (const route of [PLACEMENT, '#/campaign/1']) {
    const { html } = await renderRoute(route, { draft });
    assert.match(readRow(html, 'ev-12'), playRe(r12.evidence_ref), route);
    assert.ok(readRow(html, 'ev-12').includes(`href="#/evidence/${encodeURIComponent(r12.evidence_ref)}"`), route);
    assert.doesNotMatch(readRow(html, 'ev-12b'), /data-action="search-play"/, route + ': no clip, no Play');
    assert.ok(readRow(html, 'ev-12b').includes(`href="#/evidence/${encodeURIComponent(r12b.evidence_ref)}"`), route);
  }
  // No ref: neither, never evidence_id.
  const none = freshDraft();
  for (const r of none.episode_verdicts['pl-win'].episodes[2].reads) delete r.evidence_ref;
  const bare = (await renderRoute(PLACEMENT, { draft: none })).html;
  for (const ev of ['ev-12', 'ev-12b']) assert.doesNotMatch(readRow(bare, ev), /data-action="search-play"|Open evidence|#\/evidence\/ev-/, ev);
  // F2-R1-07 still holds: a rejected read in a not-heard episode gets no Play.
  assert.doesNotMatch(epRow((await renderRoute(PLACEMENT)).html, 'e15'), /data-action="search-play"/);
});

test('FUNC2 F2-SCR-R1-01 read rows are listed in time order (no span last)', async () => {
  const draft = freshDraft();
  const e12 = draft.episode_verdicts['pl-win'].episodes[2];
  const [r12, r12b] = e12.reads;
  const late = { ...r12, evidence_id: 'ev-12c', evidence_ref: 'sr1:cccccccccccccccccccccccccccccccc', display_span: [900, 930] };
  const early = { ...r12, evidence_id: 'ev-12a', evidence_ref: 'sr1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', display_span: [12, 40] };
  e12.reads = [r12b, late, r12, early];
  for (const route of [PLACEMENT, '#/campaign/1']) {
    const row = epRow((await renderRoute(route, { draft })).html, 'e12');
    const order = [...row.matchAll(/<li class="ew-ep-read" data-read="([^"]+)"/g)].map(m => m[1]);
    assert.deepEqual(order, ['ev-12a', 'ev-12', 'ev-12c', 'ev-12b'], route);
  }
});

// F2-SCR-R1-05: an episode whose read waits for confirmation is checked,
// never heard, and the summary says how many wait.
test('FUNC2 F2-SCR-R1-05 no "0 of 0": unconfirmed episodes count as checked, with "· N episode(s) to confirm"', async () => {
  const draft = freshDraft();
  draft.episode_verdicts['pl-ep'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 0, checked: 0, waiting_review: 0, not_checked: 0, unconfirmed: 1 };
  draft.episode_verdicts['pl-url'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 1, checked: 1, waiting_review: 0, not_checked: 3, unconfirmed: 2 };
  const page = await renderRoute('#/campaign/1/placement/pl-ep', { draft });
  assert.match(page.html, /<p class="ew-ep-summary">Confirmed heard in 0 of 1 checked episode · 1 episode to confirm<\/p>/);
  assert.doesNotMatch(visible(page.html), /0 of 0/);
  const camp = (await renderRoute('#/campaign/1', { draft })).html;
  assert.match(visible(rowOf(camp, 'pl-ep')), /Confirmed heard in 0 of 1 checked episode · 1 episode to confirm/);
  assert.match(visible(rowOf(camp, 'pl-url')), /Confirmed heard in 0 of 3 checked episodes · 2 episodes to confirm · 3 episodes not checked/);
  // No unconfirmed: no tail.
  assert.doesNotMatch(visible(rowOf((await renderRoute('#/campaign/1')).html, 'pl-url')), /to confirm/);
});

// F2-SCR-R1-06: the "made by someone else" line never sits above review
// controls the member can use; elsewhere it stays.
const MADE_BY = /Only the person who made this can change it\./;
test('FUNC2 F2-SCR-R1-06 no lock line where review controls show; kept where none show', async () => {
  const draft = freshDraft();
  draft.can_change = false;
  draft.allowed_actions = ['read'];
  const page = await renderRoute(PLACEMENT, { draft, role: 'editor' });
  assert.match(page.html, /data-action="episode-review"/);
  assert.doesNotMatch(page.html, MADE_BY);
  const camp = await renderRoute('#/campaign/1', { draft, role: 'editor', campaign: { can_change: false, allowed_actions: ['read'] } });
  assert.match(camp.html, /data-action="episode-review"/);
  assert.doesNotMatch(camp.html, MADE_BY);
  // No review controls (no episode data): the line stays, as before.
  const bare = freshDraft();
  bare.can_change = false;
  bare.allowed_actions = ['read'];
  delete bare.episode_verdicts;
  assert.match((await renderRoute(PLACEMENT, { draft: bare, role: 'editor' })).html, MADE_BY);
  assert.match((await renderRoute('#/campaign/1', { draft: bare, role: 'editor', campaign: { can_change: false, allowed_actions: ['read'] } })).html, MADE_BY);
});

// F2-SCR-R1-09: a stored typed code shows on the decided read row.
test('FUNC2 F2-SCR-R1-09 a typed code shows on the decided row, against the brief code or as "Code heard: X", escaped', async () => {
  const draft = freshDraft();
  draft.episode_verdicts['pl-win'].episodes[0].reads[0].review.code = 'FERN40';
  draft.episode_verdicts['pl-win'].episodes[5].reads[0].review.code = 'FURN<40>';
  const ep = draft.episode_verdicts['pl-ep'].episodes[1];
  ep.reads[0].review = { decision: 'confirmed', by_name: 'Lee', at: '2026-10-27T20:00:00Z', code: 'QUINCE<20>' };
  const html = (await renderRoute(PLACEMENT, { draft })).html;
  assert.match(readRow(html, 'ev-10'), /Confirmed by Dana &amp; &lt;b&gt;Co&lt;\/b&gt; · Heard, right code/);
  assert.match(readRow(html, 'ev-15'), /Rejected by Lee · code sounds like FURN&lt;40&gt; \(brief: FERN40\)/);
  const noBrief = (await renderRoute('#/campaign/1/placement/pl-ep', { draft })).html;
  assert.match(readRow(noBrief, 'ev-22'), /Confirmed by Lee · Code heard: QUINCE&lt;20&gt;/);
  assert.doesNotMatch(noBrief, /QUINCE<20>/);
  // No stored code: the row is as before.
  const plain = (await renderRoute(PLACEMENT)).html;
  assert.match(readRow(plain, 'ev-10'), /<span class="ew-ep-read-line">Confirmed by Dana &amp; &lt;b&gt;Co&lt;\/b&gt;<\/span>/);
});

// F2-SCR-R1-10: the campaign row says "Not heard in this episode" for
// checked episodes with no read, as the placement page does.
test('FUNC2 F2-SCR-R1-10 campaign row lists checked episodes with no read as "Not heard in this episode"', async () => {
  const html = (await renderRoute('#/campaign/1')).html;
  const win = rowOf(html, 'pl-win');
  assert.equal(verdictOf(win, 'e14'), 'Not heard in this episode');
  assert.equal(verdictOf(win, 'e15'), 'Not heard in this episode');
  assert.doesNotMatch(epRow(win, 'e14'), /data-action="search-play"|Open evidence/);
  const url = rowOf(html, 'pl-url');
  const notHeard = freshDraft().episode_verdicts['pl-url'].episodes.filter(e => e.verdict === 'not_heard').map(e => e.episode_id);
  for (const eid of notHeard) assert.equal(verdictOf(url, eid), 'Not heard in this episode', eid);
  assert.doesNotMatch(win, /data-episode="e16"/, 'not checked stays off the row');
});

// F2-SCR-R1-11: after a decision, focus moves to the new control on the same read row.
function focusBlocks(win) {
  const focused = [];
  const parse = html => [...String(html).matchAll(/<button [^>]*data-action="episode-review"[^>]*>/g)].map(m => {
    const dataset = {};
    for (const [, k, v] of m[0].matchAll(/data-([\w-]+)="([^"]*)"/g)) dataset[k.replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
    return { dataset, focus() { focused.push(dataset); } };
  });
  const box = (pid, part) => ({ dataset: { episodes: pid, part }, innerHTML: 'OLD', querySelectorAll(sel) { return sel.includes('episode-review') ? parse(this.innerHTML) : []; } });
  const all = [box('pl-win', 'tag'), box('pl-win', 'list'), box('pl-ep', 'list')];
  win.document.querySelectorAll = sel => (sel === '[data-episodes]' ? all : []);
  return focused;
}
test('FUNC2 F2-SCR-R1-11 focus: Undo review after Confirm or Reject, Confirm after Undo, on the same read', async () => {
  for (const decision of ['confirm', 'reject']) {
    const t = await renderRoute(PLACEMENT);
    const focused = focusBlocks(t.win);
    t.win.EW.api = async () => updated();
    await t.win.EWViews.handleAction(reviewEl({ ...data12, decision }));
    assert.equal(focused.length, 1, decision);
    assert.equal(focused[0].decision, 'undo', decision);
    assert.equal(focused[0].evidence, 'ev-12', decision);
    assert.equal(focused[0].episode, 'e12', decision);
  }
  const t = await renderRoute(PLACEMENT);
  const focused = focusBlocks(t.win);
  t.win.EW.api = async () => freshDraft().episode_verdicts['pl-win'];
  await t.win.EWViews.handleAction(reviewEl({ ...data12, rev: '2', decision: 'undo' }));
  assert.equal(focused.length, 1);
  assert.deepEqual([focused[0].decision, focused[0].evidence, focused[0].episode], ['confirm', 'ev-12', 'e12']);
  // A refusal moves no focus.
  const r = await renderRoute(PLACEMENT);
  const none = focusBlocks(r.win);
  r.win.EW.api = async () => { throw Object.assign(new Error('refused'), { status: 403 }); };
  await r.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }));
  assert.deepEqual(none, []);
});

// Owner decision on FIX3 open issue 3: where read rows show, Play and Open
// evidence sit on the read rows only, not again on the episode line; an
// episode with no read rows keeps them on its line.
const epLine = (html, eid) => epRow(html, eid).split('<ul class="ew-ep-reads">')[0];
test('FUNC2 FIX3-3 no episode-line Play/Open where read rows show; kept on episodes with no read rows', async () => {
  const cases = { 'pl-win': { reads: ['e10', 'e12'], none: ['e11', 'e13'] }, 'pl-ep': { reads: ['e22'], none: ['e21', 'e23'] }, 'pl-url': { reads: ['e46'], none: ['e41'] } };
  for (const [pid, c] of Object.entries(cases)) {
    const draft = freshDraft();
    const html = (await renderRoute('#/campaign/1/placement/' + pid, { draft })).html;
    for (const eid of c.reads) {
      assert.doesNotMatch(epLine(html, eid), /data-action="search-play"|Open evidence|ew-ep-act/, eid + ' line');
      assert.match(epRow(html, eid), /Open evidence/, eid + ' read rows keep theirs');
    }
    for (const eid of c.none) {
      const ref = refOf(draft, pid, eid);
      assert.ok(epLine(html, eid).includes(`href="#/evidence/${encodeURIComponent(ref)}"`), eid + ' line keeps Open evidence');
      const ep = draft.episode_verdicts[pid].episodes.find(e => e.episode_id === eid);
      if (ep.has_clip) assert.match(epLine(html, eid), playRe(ref), eid + ' line keeps Play');
    }
    const camp = rowOf((await renderRoute('#/campaign/1', { draft })).html, pid);
    for (const eid of c.reads) assert.doesNotMatch(epLine(camp, eid), /data-action="search-play"|Open evidence|ew-ep-act/, eid + ' campaign line');
  }
  // One Play per read with a clip, never two for the same passage.
  const html = (await renderRoute(PLACEMENT)).html;
  assert.equal((epRow(html, 'e12').match(/data-action="search-play"/g) || []).length, 1);
  assert.equal((epRow(html, 'e10').match(/data-action="search-play"/g) || []).length, 1);
});

// ---- FUNC2-FIX4 (screen r2) -------------------------------------------------

// F2-SCR-R2-02: the typed code is compared after the server's fold
// (campaigns._code_key: letters and digits only, upper case).
test('FUNC2 F2-SCR-R2-02 a typed code folds like the server before it is compared', async () => {
  const draft = freshDraft();
  const e15 = draft.episode_verdicts['pl-win'].episodes[5];
  const typed = code => { e15.reads[0].review.code = code; return renderRoute(PLACEMENT, { draft }).then(r => readRow(r.html, 'ev-15')); };
  for (const code of ['fern-40', 'Fern 40', 'f.e.r.n.4.0', 'FERN40']) {
    assert.match(await typed(code), /Rejected by Lee · Heard, right code/, code);
  }
  // Digits count; a code that folds to nothing is never right.
  assert.match(await typed('fern-41'), /Rejected by Lee · code sounds like fern-41 \(brief: FERN40\)/);
  assert.match(await typed('---'), /Rejected by Lee · code sounds like --- \(brief: FERN40\)/);
  e15.reads[0].brief_code = '-';
  assert.match(await typed('---'), /Rejected by Lee · code sounds like --- \(brief: -\)/, 'both fold to nothing');
  e15.reads[0].brief_code = 'FERN40';
  // No brief code: unchanged.
  const ep = draft.episode_verdicts['pl-ep'].episodes[1];
  ep.reads[0].review = { decision: 'confirmed', by_name: 'Lee', at: '2026-10-27T20:00:00Z', code: 'quince-20' };
  assert.match(readRow((await renderRoute('#/campaign/1/placement/pl-ep', { draft })).html, 'ev-22'), /Confirmed by Lee · Code heard: quince-20/);
});

test('FUNC2 F2-SCR-R2-02 the decided read of a judge-decided episode takes the server verdict; row and line agree', async () => {
  const draft = freshDraft();
  const e10 = draft.episode_verdicts['pl-win'].episodes[0];
  // Fresh's fixture case: "quince-20"-style typing against the brief code.
  e10.reads[0].review.code = 'fern-40';
  let html = (await renderRoute(PLACEMENT, { draft })).html;
  assert.equal(verdictOf(html, 'e10'), 'Heard, right code');
  assert.match(readRow(html, 'ev-10'), /· Heard, right code<\/span>/);
  assert.doesNotMatch(readRow(html, 'ev-10'), /code sounds like/);
  // The server's verdict wins over a client comparison for that read.
  e10.reads[0].review.code = 'FERN-41';
  html = (await renderRoute(PLACEMENT, { draft })).html;
  assert.match(readRow(html, 'ev-10'), /· Heard, right code<\/span>/, 'heard_right from the server');
  const e46 = draft.episode_verdicts['pl-url'].episodes[1];
  e46.reads[0].review.code = 'FERN40';
  const url = (await renderRoute('#/campaign/1/placement/pl-url', { draft })).html;
  assert.match(readRow(url, 'ev-46'), /· code sounds like FERN40 \(brief: FERN40\)<\/span>/, 'heard_wrong from the server');
});

// F2-SCR-R2-03: with episodes, the scope sentence and the all-unknown
// requirements line sit in Technical details, like the FIX1 lines.
function withCodeScope(draft = withReasons()) {
  for (const pid of ['pl-win', 'pl-amb']) {
    draft.assessment_scope.by_unit[pid] = { entered_fields: ['code'], assessed_fields: [], unknown_fields: ['code'] };
    draft.comparisons[pid] = [{ field: 'code', expected: 'FERN40', observed: null, state: 'unknown' }];
  }
  return draft;
}
const SCOPE_LINE = /Checked against the brief: none yet\. Couldn't be checked, so left out of the outcome: Code\./;
const REQ_LINE = /The agreed field couldn't be checked from the stored record: Code\./;
const techText = html => visible(techOf(html).replace(/<details class="tech-details">|<\/details>/g, ''));
test('FUNC2 F2-SCR-R2-03 with episodes, the requirement lines are in Technical details only', async () => {
  const page = (await renderRoute(PLACEMENT, { draft: withCodeScope() })).html;
  for (const re of [SCOPE_LINE, REQ_LINE]) {
    assert.doesNotMatch(visible(page), re, 'page ' + re);
    assert.match(techText(page), re, 'page technical details ' + re);
  }
  const row = rowOf((await renderRoute('#/campaign/1', { draft: withCodeScope() })).html, 'pl-win');
  assert.doesNotMatch(visible(row), SCOPE_LINE, 'campaign row');
  assert.match(techText(row), SCOPE_LINE, 'campaign row technical details');
});

test('FUNC2 F2-SCR-R2-03 unchanged with a scope problem or no episode data', async () => {
  const bare = () => { const d = withCodeScope(); delete d.episode_verdicts; return d; };
  for (const [label, route, draft] of [['pl-amb page', '#/campaign/1/placement/pl-amb', withCodeScope()], ['pl-win page, no episode data', PLACEMENT, bare()]]) {
    const html = (await renderRoute(route, { draft })).html;
    for (const re of [SCOPE_LINE, REQ_LINE]) {
      assert.match(visible(html), re, label + ' ' + re);
      assert.doesNotMatch(techText(html), re, label + ' technical details ' + re);
    }
  }
  for (const [label, draft, pid] of [['pl-amb row', withCodeScope(), 'pl-amb'], ['pl-win row, no episode data', bare(), 'pl-win']]) {
    const row = rowOf((await renderRoute('#/campaign/1', { draft })).html, pid);
    assert.match(visible(row), SCOPE_LINE, label);
    assert.doesNotMatch(techText(row), SCOPE_LINE, label + ' technical details');
  }
});

// F2-SCR-R2-04: a waiting_review episode is checked, with its own tail,
// and the campaign row lists it with its "Waiting for review" line.
test('FUNC2 F2-SCR-R2-04 no "0 of 0": waiting_review counts as checked, with "· N episode(s) waiting for review"', async () => {
  const draft = freshDraft();
  draft.episode_verdicts['pl-ep'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 0, checked: 0, waiting_review: 1, not_checked: 0, unconfirmed: 0 };
  draft.episode_verdicts['pl-url'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 1, checked: 1, waiting_review: 2, not_checked: 1, unconfirmed: 1 };
  const page = await renderRoute('#/campaign/1/placement/pl-ep', { draft });
  assert.match(page.html, /<p class="ew-ep-summary">Confirmed heard in 0 of 1 checked episode · 1 episode waiting for review<\/p>/);
  assert.doesNotMatch(visible(page.html), /0 of 0/);
  const camp = (await renderRoute('#/campaign/1', { draft })).html;
  assert.match(visible(rowOf(camp, 'pl-ep')), /Confirmed heard in 0 of 1 checked episode · 1 episode waiting for review/);
  assert.match(visible(rowOf(camp, 'pl-url')), /Confirmed heard in 0 of 4 checked episodes · 1 episode to confirm · 2 episodes waiting for review · 1 episode not checked/);
  // None waiting: no tail.
  assert.doesNotMatch(visible(rowOf((await renderRoute('#/campaign/1')).html, 'pl-url')), /waiting for review/);
});

test('FUNC2 F2-SCR-R2-04 campaign row lists a waiting_review episode with its line and no review controls', async () => {
  const draft = freshDraft();
  // As on real data (ep_1815755d97cbdb72): no listed read, no ref.
  const e13 = draft.episode_verdicts['pl-win'].episodes[3];
  Object.assign(e13, { evidence_id: null, evidence_ref: null, has_clip: false, display_span: null, reads: [] });
  for (const role of ['owner', 'editor']) {
    const win = rowOf((await renderRoute('#/campaign/1', { draft, role })).html, 'pl-win');
    assert.equal(verdictOf(win, 'e13'), 'Waiting for review', role);
    assert.doesNotMatch(epRow(win, 'e13'), /data-action="episode-review"|name="code"|data-action="search-play"|Open evidence/, role);
  }
  const ep = rowOf((await renderRoute('#/campaign/1')).html, 'pl-ep');
  assert.equal(verdictOf(ep, 'e23'), 'Waiting for review');
});

// F2-SCR-R3-02 = F2-R4-03: with episodes, the "No checks entered…" form
// stays visible (it contradicts no episode verdict); the "Checked against
// the brief…" form stays in Technical details. Unchanged without episodes
// or with a scope problem.
const HINT = /No checks entered for this placement\. The brief text is not read automatically; add Message, Offer, Code or Length to check them\./g;
const count = (s, re) => (String(s).match(re) || []).length;
function withNoChecks(draft = withReasons(), { outcome = true, citation = false } = {}) {
  for (const pid of ['pl-win', 'pl-amb']) {
    draft.assessment_scope.by_unit[pid] = { entered_fields: [], assessed_fields: [], unknown_fields: [] };
    if (!outcome) draft.allocation.evaluations[pid].outcome = null;
    if (citation) draft.allocation.citations = { ...(draft.allocation.citations || {}), [pid]: { placement_id: pid, reasons: [] } };
  }
  return draft;
}
test('FUNC2 F2-SCR-R3-02 with episodes, "No checks entered…" stays visible, once, on every path', async () => {
  for (const [label, opts] of [['outcome', {}], ['no outcome', { outcome: false }], ['outcome and citation', { citation: true }], ['citation only', { outcome: false, citation: true }]]) {
    const page = (await renderRoute(PLACEMENT, { draft: withNoChecks(withReasons(), opts) })).html;
    assert.equal(count(visible(page), HINT), 1, 'page visible, ' + label);
    assert.equal(count(techText(page), HINT), 0, 'page technical details, ' + label);
    // The campaign row says the scope only with an outcome or a citation, as without episodes.
    const row = rowOf((await renderRoute('#/campaign/1', { draft: withNoChecks(withReasons(), opts) })).html, 'pl-win');
    assert.equal(count(visible(row), HINT), opts.outcome === false && !opts.citation ? 0 : 1, 'campaign row visible, ' + label);
    assert.equal(count(techText(row), HINT), 0, 'campaign row technical details, ' + label);
  }
});
test('FUNC2 F2-SCR-R3-02 unchanged with a scope problem or no episode data', async () => {
  const bare = d => { delete d.episode_verdicts; return d; };
  for (const [label, route, draft] of [['pl-amb page', '#/campaign/1/placement/pl-amb', withNoChecks()], ['pl-win page, no episode data', PLACEMENT, bare(withNoChecks())]]) {
    const html = (await renderRoute(route, { draft })).html;
    assert.equal(count(visible(html), HINT), 1, label);
    assert.equal(count(techText(html), HINT), 0, label + ' technical details');
  }
});
// F2-R4-03 (O10): on the citation-plus-outcome path the "Checked against
// the brief…" form sits in Technical details once.
test('FUNC2 F2-SCR-R3-02 with episodes, outcome and citation: "Checked against the brief…" once, in Technical details', async () => {
  const draft = withCodeScope();
  for (const pid of ['pl-win']) draft.allocation.citations = { [pid]: { placement_id: pid, reasons: ['citation only'] } };
  const page = (await renderRoute(PLACEMENT, { draft })).html;
  assert.doesNotMatch(visible(page), SCOPE_LINE);
  assert.equal(count(techText(page), new RegExp(SCOPE_LINE.source, 'g')), 1);
});

// F2-SCR-R3-01: the episode-summary tag wraps inside its card at 390
// (the longest tails would scroll the page sideways); other tags keep
// their one-line look.
test('FUNC2 F2-SCR-R3-01 the episode-summary tag may wrap; other tags still do not', async () => {
  const css = fs.readFileSync(path.join(APP, 'work.css'), 'utf8');
  const rule = /\[data-part="tag"\] \.tag\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, 'a rule scoped to the summary tag');
  assert.match(rule[1], /white-space:\s*normal/);
  assert.match(rule[1], /max-width:\s*100%/);
  assert.match(rule[1], /min-width:\s*0/);
  assert.match(fs.readFileSync(path.join(APP, 'styles.css'), 'utf8'), /\.tag, \.ew-tag \{[^}]*white-space: nowrap;/, 'base tag unchanged');
  // The summary tag sits in that wrapper on the campaign row and the placement page.
  const draft = freshDraft();
  draft.episode_verdicts['pl-url'].summary = { heard: 0, heard_right: 0, heard_wrong: 0, not_heard: 1, checked: 1, waiting_review: 2, not_checked: 1, unconfirmed: 1 };
  const LONG = /<span data-episodes="pl-url" data-part="tag"><span class="tag[^"]*">Confirmed heard in 0 of 4 checked episodes · 1 episode to confirm · 2 episodes waiting for review · 1 episode not checked<\/span><\/span>/;
  assert.match(rowOf((await renderRoute('#/campaign/1', { draft })).html, 'pl-url'), LONG);
  assert.match((await renderRoute('#/campaign/1/placement/pl-url', { draft })).html, LONG);
});

// ---- NOGAP-TS row 206 (func2-code-r4) ----------------------------------------

// F2-R4-01: the server verdict is the decided read's alone; another read of
// the same episode, and every read of an episode the server decided on
// another basis, is compared with the client fold.
test('FUNC2 F2-R4-01 the server verdict applies only to the read a judge decided the episode on', async () => {
  const draft = freshDraft();
  const [e10, e11] = draft.episode_verdicts['pl-win'].episodes;
  // As the split Hey Riddle Riddle reads (QUINCE20 / QUINCE15): a second
  // confirmed read of the decided episode with a wrong code.
  e10.reads.push({ ...e10.reads[0], evidence_id: 'ev-10b', display_span: [95.0, 120.0],
    review: { decision: 'confirmed', by_name: 'Dana', at: '2026-10-27T18:10:00Z', code: 'FERN-41' } });
  // Decided heard_wrong on the verified basis; the judge typed the brief code.
  e11.reads = [{ ...e10.reads[0], evidence_id: 'ev-11', display_span: [12.0, 40.0],
    review: { decision: 'confirmed', by_name: 'Lee', at: '2026-10-27T18:20:00Z', code: 'fern 40' } }];
  const html = (await renderRoute(PLACEMENT, { draft })).html;
  assert.equal(verdictOf(html, 'e10'), 'Heard, right code');
  assert.match(readRow(html, 'ev-10b'), /Confirmed by Dana · code sounds like FERN-41 \(brief: FERN40\)<\/span>/);
  assert.match(readRow(html, 'ev-11'), /Confirmed by Lee · Heard, right code<\/span>/);
});

// F2-R4-02: focus lands on the clicked read's control, also when it is not
// the episode's first read (the first read has the same control).
test('FUNC2 F2-R4-02 focus stays on the second read of an episode after Confirm and after Undo', async () => {
  const data12b = { ...data12, evidence: 'ev-12b' };
  const bothConfirmed = () => {
    const v = updated();
    v.episodes[2].reads[1].review = { decision: 'confirmed', by_name: 'Ana', at: '2026-10-27T20:01:00Z', code: null };
    return v;
  };
  let t = await renderRoute(PLACEMENT);
  let focused = focusBlocks(t.win);
  t.win.EW.api = async () => bothConfirmed();
  await t.win.EWViews.handleAction(reviewEl({ ...data12b, decision: 'confirm' }));
  assert.equal(focused.length, 1);
  assert.deepEqual([focused[0].decision, focused[0].evidence, focused[0].episode], ['undo', 'ev-12b', 'e12']);
  t = await renderRoute(PLACEMENT);
  focused = focusBlocks(t.win);
  t.win.EW.api = async () => freshDraft().episode_verdicts['pl-win'];
  await t.win.EWViews.handleAction(reviewEl({ ...data12b, rev: '2', decision: 'undo' }));
  assert.equal(focused.length, 1);
  assert.deepEqual([focused[0].decision, focused[0].evidence, focused[0].episode], ['confirm', 'ev-12b', 'e12']);
});

// CODE-HEARD r2 R2-1: a blank Confirm sends the code the row SHOWED.
test('CODE-HEARD Confirm carries the shown code: an undecided row its hint, a rejected row none', async () => {
  const { html } = await renderRoute(PLACEMENT);
  assert.match(readRow(html, 'ev-12'), /data-decision="confirm" data-shown="FURN&lt;40&gt;"/);
  const draft = freshDraft();
  const e12 = draft.episode_verdicts['pl-win'].episodes[2];
  e12.reads[0].review = { decision: 'rejected', by_name: null, at: '2026-10-27T20:00:00Z', code: null };
  const rejected = (await renderRoute(PLACEMENT, { draft })).html;
  assert.match(readRow(rejected, 'ev-12'), /data-decision="confirm" data-shown=""/);
  const noBrief = freshDraft();
  noBrief.episode_verdicts['pl-win'].episodes[2].reads[0].brief_code = null;
  assert.match(readRow((await renderRoute(PLACEMENT, { draft: noBrief })).html, 'ev-12'), /data-decision="confirm" data-shown=""/);
});

test('CODE-HEARD a blank Confirm posts shown_code; a typed code, Reject and an older button do not', async () => {
  const t = await renderRoute(PLACEMENT);
  blocks(t.win);
  const bodies = [];
  t.win.EW.api = async (p, opts = {}) => { bodies.push(opts.body); return updated(); };
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm', shown: 'FURN<40>' }));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm', shown: '' }));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm', shown: 'FURN<40>' }, { code: 'FERN40' }));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'reject', shown: 'FURN<40>' }));
  await t.win.EWViews.handleAction(reviewEl({ ...data12, decision: 'confirm' }));
  assert.deepEqual(bodies.map(b => JSON.stringify(b)), [
    JSON.stringify({ evidence_id: 'ev-12', decision: 'confirm', expected_revision: 1, shown_code: 'FURN<40>' }),
    JSON.stringify({ evidence_id: 'ev-12', decision: 'confirm', expected_revision: 1, shown_code: '' }),
    JSON.stringify({ evidence_id: 'ev-12', decision: 'confirm', expected_revision: 1, code: 'FERN40' }),
    JSON.stringify({ evidence_id: 'ev-12', decision: 'reject', expected_revision: 1 }),
    JSON.stringify({ evidence_id: 'ev-12', decision: 'confirm', expected_revision: 1 }),
  ]);
});
