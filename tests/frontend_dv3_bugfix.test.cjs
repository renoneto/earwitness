/* Design v3 bugfix checks (B-1..B-15 from the design v3 screenshot pack).
   Customer pages load the real app scripts in the stubbed browser sandbox
   used by frontend_dv3_g4.test.cjs; operator pages load app/operator/*.js
   with a small id-keyed element stub.
   Run: node --test tests/frontend_dv3_bugfix.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

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
const httpError = (status, message) => Object.assign(new Error(message), { status });

// B-2: an unknown campaign or alert (and the other record pages) answers 404 on the
// server; the page must show the not-found view, not "unavailable" + Retry.
test('B-2 a 404 for a campaign, alert, placement, report, brand, show or creative shows the not-found view', async () => {
  const wrong = [];
  // Record pages that know their type say it (V-16); the creative, placement and report pages keep the generic view.
  const routes = [['#/campaign/999', /We can(?:'|&#39;)t find this campaign/], ['#/alert/999', /We can(?:'|&#39;)t find this alert/],
    ['#/campaign/1/placement/exp%3Anone', /This record is not available/], ['#/campaign/1/report/99', /This record is not available/],
    ['#/brand/nope', /We can(?:'|&#39;)t find this brand/], ['#/show/cs-nope', /We can(?:'|&#39;)t find this show/],
    ['#/creative/nope', /This record is not available/]];
  for (const [route, expected] of routes) {
    const { html } = await renderRoute(route, async () => { throw httpError(404, 'unknown'); });
    if (!expected.test(html) || /is unavailable: unknown/.test(html)) wrong.push(route);
  }
  assert.deepEqual(wrong, []);
});

// B-4: the server stores a failed check as last_attempt_result 'error'
// (customer.check_alert -> _stamp_alert_run(..., "error", ...)), never 'failed'.
const ALERT = { revision: 1, definition_revision: 1, query: { version: 2, mode: 'ads', q: 'x' }, baseline_complete: true,
  last_successful_at: '2026-10-14T09:00:00Z', last_attempted_at: '2026-10-14T10:00:00Z', lifecycle: 'active_local',
  unread_event_count: 0, events: [], current_matches: { count: 1, refs: [] } };
test('B-4 a check the server recorded as error shows Check failed on the hub and the alert page', async () => {
  const broken = { ...ALERT, id: 3, name: 'Broken', last_attempt_result: 'error', error: 'filter rejected' };
  const calm = { ...ALERT, id: 2, name: 'Calm', last_attempt_result: 'success', error: '' };
  const hub = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: [broken, calm] };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  const row = name => new RegExp(`<li class="ew-work-alert"[^>]*>(?:(?!</li>).)*${name}(?:(?!</li>).)*</li>`).exec(hub.html)[0];
  assert.match(row('Broken'), />Check failed</);
  assert.match(row('Broken'), /data-action="check-alert"[^>]*>Check again</);
  assert.doesNotMatch(row('Calm'), /Check failed|check-alert/);
  const page = await renderRoute('#/alert/3', p => {
    if (p === '/customer/alerts/3') return { alert: broken };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(page.html, /Latest check failed: filter rejected\./);
});

// B-6: Save episode bookmark on Evidence posted a pinned episode bookmark
// without transcript_version_id ("pinned episodes need an integer
// transcript_version_id"). The button must carry the pinned version.
const REF = 'sr1:b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6';
const evRecord = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'confirmed',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_b6', show_title: 'Quiet Hours', ep_title: 'Why we wake', pub_date: '2026-10-07', duration_min: 41, transcript_version_id: 9 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: 'named:ep_b6.x', episode_id: 'ep_b6', transcript_version_id: 7, orig_start_s: 110, orig_end_s: 180 },
  media: { playback: 'remote', alignment: 'unknown' },
  window: { segments: [{ segment_id: 1, start_s: 110, end_s: 150, text: 'A short break.', text_start: 0, text_end: 14 }], text_complete: true, next_cursor: null },
  ...over });
const recordApi = rec => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  throw new Error('unexpected ' + p);
};
test('B-6 Save episode bookmark on Evidence and on a transcript passage carries the pinned transcript version', async () => {
  for (const rec of [evRecord(), evRecord({ record_kind: 'transcript_passage', status: null, review: null })]) {
    const { html } = await renderRoute('#/evidence/' + encodeURIComponent(REF), recordApi(rec));
    const btn = /<button[^>]*data-action="save-bookmark-episode"[^>]*>/.exec(html);
    assert.ok(btn, rec.record_kind + ': the button is on the page');
    assert.match(btn[0], /data-episode="ep_b6"/);
    assert.match(btn[0], new RegExp(`data-ref="${REF}"`));
    assert.match(btn[0], /data-tv="7"/, rec.record_kind + ': the version pinned with this record, not the episode\'s current one');
  }
});

// B-10: stale reasons that are already sentences ("campaign changed since
// freeze" from exports.py) got a second "changed" appended.
test('B-10 stale tags read as one sentence for every server reason', () => {
  const win = loadAt('#/saved?tab=reports');
  const text = reasons => win.EWSaved.archiveRow(win.EW, { kind: 'report', id: 1, campaign_id: 3, report_no: 1, title: 'R',
    captured_at: '2026-10-14T16:28:00Z', stale: true, stale_reasons: reasons }).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
  const cases = [
    [['campaign changed since freeze'], 'Stale: the campaign changed'],
    [['research view changed since capture'], 'Stale: the research view changed'],
    [['campaign removed after capture'], 'Stale: the campaign was removed'],
    [['campaign unavailable'], 'Stale: the campaign is no longer available'],
    [['pins unavailable'], 'Stale: the capture record is unavailable'],
    [['brief', 'attachments:sr1:x'], 'Stale: the brief, attached sources changed'],
  ];
  for (const [reasons, want] of cases) {
    const got = text(reasons);
    assert.ok(got.includes(want + ' '), `${reasons} -> ${got}`);
    assert.doesNotMatch(got, /changed changed|since freeze changed|available changed|removed changed|since capture changed/, String(reasons));
  }
});

// B-9: at 390 px, campaign 1's report row stale tag ("Stale: which sources
// count where, attached sources, …") kept .tag's nowrap and made the page
// 674 px wide. A report row's tag must be allowed to wrap. (Browser check:
// O/.tmp-design-v3/bugfix-shots, S-185 at 390.)
test('B-9 a campaign report row tag may wrap', () => {
  const css = fs.readFileSync(path.join(APP, 'work.css'), 'utf8');
  const rule = /\.ew-work-report \.tag\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, 'work.css has a rule for tags in a report row');
  assert.match(rule[1], /white-space:\s*normal/);
});

// ---- Operator console (app/operator/*.js) ---------------------------------
// An id-keyed element stub: document.getElementById creates an element the
// first time an id is asked for; tests may put their own element first.
function loadOperator() {
  const els = new Map();
  const mk = id => ({ id, innerHTML: '', textContent: '', value: '', disabled: false, checked: false, dataset: {}, isConnected: true,
    files: [], elements: [], appended: [],
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    addEventListener() {}, append(child) { this.appended.push(child); }, querySelector: () => null, querySelectorAll: () => [], focus() {} });
  const document = { getElementById: id => { if (!els.has(id)) els.set(id, mk(id)); return els.get(id); },
    querySelectorAll: () => [], querySelector: () => null, createElement: () => mk(''), addEventListener() {}, removeEventListener() {},
    body: { dataset: {} } };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document, sessionStorage: store, localStorage: store, window: { scrollY: 0 }, location: { hash: '' },
    history: { replaceState() {} }, console, setTimeout, clearTimeout, URLSearchParams, AbortController, CSS: { escape: s => s },
    fetch: async () => { throw new Error('no network in tests'); } };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  }
  return { ctx, els, mk, run: code => vm.runInContext(code, ctx) };
}
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 0)); };

// B-7: after "Save media association" the page re-wired the player as
// wireAudio(media, 0, selection, alignment): end got the selection and
// selection got a string, so the status read "selection is not a function".
test('B-7 saving the media association re-wires the player with no end and the transcript selection', async () => {
  const o = loadOperator();
  const wired = [];
  const selection = () => 12;
  o.ctx.wireAudio = (...args) => { wired.push(args); };
  o.ctx.wireTranscript = () => selection;
  let save;
  o.ctx.wireForm = (id, fn) => { if (id === 'media-form') save = fn; };
  const episode = { id: 'ep1', ep_title: 'Ep', show_title: 'Show', media: { kind: 'remote_full' }, alignment: 'unknown', revision: 3 };
  o.ctx.api = async (p) => {
    if (p === '/api/v1/episodes/ep1') return { episode };
    if (p === '/api/v1/episodes/ep1/transcript') return { segments: [], version_id: 1 };
    if (p.startsWith('/api/v1/evidence?')) return { items: [], total: 0 };
    if (p === '/api/v1/episodes/ep1/media') return { revision: 4 };
    throw new Error('unexpected ' + p);
  };
  await o.run('Views').episode(['ep1']);
  assert.ok(save, 'the media form is wired');
  await save({ 'media-url': '', 'media-id': '', 'media-alignment': 'verified' }, { dataset: { revision: '3' } });
  assert.equal(wired.length, 2);
  const [media, start, end, sel] = wired[1];
  assert.deepEqual([media, start, end], [episode.media, 0, null]);
  assert.equal(sel, selection, 'the selection stays a function');
});

// B-8: creating a saved search moves to watchlists/<id> and re-renders; the
// "Saved · revision N" status went to the old, detached form.
test('B-8 a save that moves to a new route shows its status on the re-rendered form', async () => {
  const o = loadOperator();
  const mkForm = () => {
    const status = { textContent: '' }, button = { disabled: false, isConnected: true };
    const f = { id: 'watch-form', isConnected: true, dataset: { revision: '0' }, elements: [], status,
      querySelector: s => (s.includes('submit') ? button : s.includes('status') ? status : null),
      addEventListener: (type, fn) => { f.onSubmit = fn; } };
    return f;
  };
  const old = mkForm(), fresh = mkForm();
  o.els.set('watch-form', old);
  o.run('App').routeHash = '#/watchlists/new';
  o.ctx.render = async () => { await settle(2); old.isConnected = false; o.els.set('watch-form', fresh); };
  o.ctx.wireForm('watch-form', async () => ({ route: '#/watchlists/5', revision: 1 }));
  await old.onSubmit({ preventDefault() {} });
  await settle();
  assert.equal(fresh.status.textContent, 'Saved · revision 1.');
});

// B-12: every drawRuns() appended another Runs panel (and another
// #proc-detail); after a run started the first still said "No runs yet".
test('B-12 starting a run redraws the one Runs panel instead of adding a second', async () => {
  const o = loadOperator();
  const run = { run_id: 'run-1', status: 'queued', revision: 1, mode: 'local', financial_status: 'reserved', stage_counts: {} };
  let started = false;
  o.ctx.api = async (p, opts = {}) => {
    if (p === '/api/v1/processing/sources') return { feed_sources: [], registered_inputs: [], uploads: {} };
    if (p === '/api/v1/processing/runs?limit=20') return { runs: started ? [run] : [] };
    if (p === '/api/v1/processing/runs' && opts.method === 'POST') { started = true; return { run, jobs: [], worker: {}, required_profile: 'local', operator_command: 'x' }; }
    throw new Error('unexpected ' + p);
  };
  o.run('App').controller = new AbortController();
  await o.run('Views').processing([]);
  const proc = o.run('Proc');
  proc.estimate = { config_digest: 'd' }; proc.estimateKey = '|local|';
  o.ctx.document.getElementById('proc-estimate').innerHTML = '<p>estimate</p>';
  await o.ctx.document.getElementById('proc-start').onclick();
  await settle();
  const panels = () => o.els.get('view').appended.filter(h => /<h2>Runs<\/h2>/.test(h.innerHTML)).length
    + (o.els.has('proc-runs') && /<h2>Runs<\/h2>/.test(o.els.get('proc-runs').innerHTML) ? 1 : 0);
  assert.equal(panels(), 1, 'one Runs panel on the page');
  const shown = o.els.has('proc-runs') ? o.els.get('proc-runs').innerHTML
    : o.els.get('view').appended.map(h => h.innerHTML).join('');
  assert.match(shown, /run-1/);
  assert.doesNotMatch(shown, /No runs yet/);
});

// B-13: when a brand's counts request fails, Creatives became "n/a" but
// Occurrences stayed "…" (still loading) for good.
test('B-13 a failed brand counts request marks both count cells n/a', async () => {
  const o = loadOperator();
  const creatives = { dataset: { creativeCount: 'Acme' }, textContent: '…', isConnected: true };
  const occurrences = { dataset: { occurrenceCount: 'Acme' }, textContent: '…', isConnected: true };
  o.ctx.document.querySelectorAll = sel => (sel === '[data-creative-count]' ? [creatives] : []);
  o.ctx.document.querySelector = sel => (sel === '[data-occurrence-count="Acme"]' ? occurrences : null);
  o.ctx.location.hash = '#/explore?tab=brands';
  o.run('App').controller = new AbortController();
  o.ctx.api = async (p) => {
    if (p === '/api/v1/brands?q=') return { brands: [{ brand: 'Acme', c: 2, ads: 1 }] };
    if (p.startsWith('/api/v1/brands/Acme/counts')) throw new Error('boom');
    throw new Error('unexpected ' + p);
  };
  await o.run('Views').explore([]);
  await settle();
  assert.equal(creatives.textContent, 'n/a');
  assert.equal(occurrences.textContent, 'n/a', 'never left loading');
});

// B-6 review F4: with no known transcript version the button must not send
// a pin the server refuses; like the Episode page, it saves metadata-only.
test('B-6 F4 with no pinned transcript version the Evidence bookmark is metadata-only', async () => {
  const rec = evRecord({ episode: { ...evRecord().episode, transcript_version_id: null },
    evidence: { ...evRecord().evidence, transcript_version_id: null } });
  const { html } = await renderRoute('#/evidence/' + encodeURIComponent(REF), recordApi(rec));
  const btn = /<button[^>]*data-action="save-bookmark-episode"[^>]*>/.exec(html)[0];
  assert.match(btn, /data-ref=""/);
  assert.match(btn, /data-tv=""/);
});
