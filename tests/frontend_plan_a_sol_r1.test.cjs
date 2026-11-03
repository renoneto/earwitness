/* Plan A excerpt view, Sol UI review round 1 (sol/pa-r1.md): FIX-1..6,
   POLISH-1, the final-copy table and the client part of BLOCKER-1, each
   at excerpt level, with Plan B controls. Loader helpers copied from
   frontend_plan_a.test.cjs. Run: node --test tests/frontend_plan_a_sol_r1.test.cjs */
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
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route },
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
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const sessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(route = '#/overview') {
  const win = loadAt(route, { fetch: sessionFetch() });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
// A page render plus a working modal, toasts and API log.
async function page(route, api, { hosted = false, q = null } = {}) {
  const win = loadViews(route);
  await flushBoot();
  win.EW.current.hosted = hosted;
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, opts = {}) => { calls.push([opts.method || 'GET', p, opts.body]); return api(p, opts); };
  win.location.hash = route;
  win.EW.current.route = route;
  if (q !== null) win.EW.current.filters = { ...(win.EW.current.filters || {}), q };
  await win.EWViews.render();
  return { win, html: main.innerHTML, main, modal, toasts, calls };
}

const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
const SAFE = 'this episode is brought to you by Lumen lamps go to lumen dot com slash morning zebra';
const READ = { kind: 'named', source_label: 'Named read',
  brand: { key: 'lumen', display: 'Lumen', display_basis: 'brand_list', suggested: 'lumen', confirmed: false },
  needs_review: false, self_promo: false, self_promo_reason: null, confidence: 'high', display_span: [30, 45],
  display_basis: 'detected', has_clip: false, said: { web_addresses: [], promo_codes: [], spoken_brand: 'Lumen' } };
const base = { ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'finding', bounds_verified: 0 }, attribution: [{ brand: 'Lumen', basis: 'spoken', channel: 'spoken' }],
  relationship: 'current', availability: 'available', bounds_basis: 'original',
  original_bounds: { start_s: 30, end_s: 45 }, effective_bounds: { start_s: 30, end_s: 45 }, selected_bounds: { start_s: 30, end_s: 45 },
  episode: { episode_id: 'ep_a', show_title: 'Morning Show', ep_title: 'Monday', pub_date: '2026-09-28', transcript_version_id: 2, transcript_is_current: true },
  show: { show_identity_id: 'cs-a', display_title: 'Morning Show', unresolved: 0 },
  evidence: { id: 'ad-a1', episode_id: 'ep_a', transcript_version_id: 2, orig_start_s: 30, orig_end_s: 45 },
  media: { playback: 'unavailable', reason: 'clip unavailable' }, read: READ };
// Plan A: the record answered at excerpt level.
const excerptRecord = (over = {}) => ({ ...base, disclosure: 'excerpt', text: SAFE, excerpt_bounds: { start_s: 25, end_s: 50 }, ...over });
// Plan B: the full record with its transcript window.
const fullRecord = (over = {}) => ({ ...base, canonical_source_ref: REF, window: { segments: [], text_complete: true, next_cursor: null }, ...over });
const AD_A = { ref: REF, evidence_id: 'ad-a1', status: 'finding', span: [30, 45], display_span: [30, 45], brand: { key: 'lumen', display: 'Lumen' },
  needs_review: false, self_promo: false, kind: 'rule', source_label: 'Rule', media: null, transcript_start_s: 30,
  transcript_version_id: 2, on_current_transcript: true };
const api = (rec, { excerpt = true } = {}) => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_a')) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_a') return { episode: { episode_id: 'ep_a', excerpt_only: excerpt, ads: excerpt
    ? [{ ...AD_A, text: SAFE, excerpt_bounds: { start_s: 25, end_s: 50 } }] : [AD_A],
    self_promo: [], ad_breaks: [], screening: null } };
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
};
const evidence = rec => page('#/evidence/' + encodeURIComponent(REF), api(rec));
const episodeRec = (make, over = {}) => make({ ref: 'sr1:e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5', record_kind: 'source_publication',
  evidence: {}, read: null, ...over });
const aboutAudio = html => ((html.match(/<dt>Audio<\/dt><dd>([^<]*)<\/dd>/) || [])[1]);
const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');

/* FIX-1: the excerpt and the actions each get a full-width row. */
test('FIX-1 an excerpt ad row puts the excerpt and the actions on their own full-width rows', async () => {
  const { html } = await page('#/episode/ep_a', api(episodeRec(excerptRecord, { text: null, excerpt_bounds: null })));
  const row = (html.match(/<li class="ew-ad-row[^"]*"[^>]*data-ref="sr1:c3[^>]*>/) || [''])[0];
  assert.match(row, /\bew-ad-row-excerpt\b/);
  const rule = sel => new RegExp(sel.replace(/[.>]/g, m => '\\' + m).replace(/ /g, '\\s*') + '[^{]*\\{[^}]*grid-column:\\s*1\\s*/\\s*-1');
  assert.match(css, rule('.ew-ad-row-excerpt > .ew-ad-excerpt'));
  assert.match(css, rule('.ew-ad-row-excerpt > .ew-ad-actions'));
});
test('FIX-1 Plan B control: a full ad row keeps its grid', async () => {
  const { html } = await page('#/episode/ep_a', api(episodeRec(fullRecord), { excerpt: false }));
  assert.match(html, /<li class="ew-ad-row/);
  assert.doesNotMatch(html, /ew-ad-row-excerpt/);
});

/* FIX-2: Jump to excerpt only with excerpt text; Plan B keeps Jump to the words. */
test('FIX-2 with an excerpt the jump reads Jump to excerpt and targets the Excerpt heading', async () => {
  const { html } = await evidence(excerptRecord());
  assert.match(html, /data-action="jump-words"[^>]*>Jump to excerpt</);
  assert.doesNotMatch(html, /Jump to the words/);
  assert.match(html, /<h2 id="ew-said-h">Excerpt<\/h2>/);
});
test('FIX-2 with no excerpt the jump row is omitted', async () => {
  const { html } = await evidence(excerptRecord({ text: null, excerpt_bounds: null }));
  assert.doesNotMatch(html, /jump-words/);
  assert.doesNotMatch(html, /ew-facts-jump/);
});
test('FIX-2 Plan B control: Jump to the words is kept', async () => {
  const { html } = await evidence(fullRecord());
  assert.match(html, /data-action="jump-words"[^>]*>Jump to the words</);
  assert.doesNotMatch(html, /Jump to excerpt/);
});

/* FIX-3: no Save excerpt without an excerpt. */
test('FIX-3 with no excerpt the header offers no Save excerpt', async () => {
  const { html } = await evidence(excerptRecord({ text: null, excerpt_bounds: null }));
  assert.doesNotMatch(html, /data-action="save-excerpt"/);
  assert.doesNotMatch(html, /Save excerpt/);
});
test('FIX-3 with an excerpt Save excerpt stays', async () => {
  const { html } = await evidence(excerptRecord());
  assert.match(html, /data-action="save-excerpt"[^>]*>Save excerpt</);
});

/* FIX-4: no transcript availability claims at excerpt level. */
test('FIX-4 the excerpt episode page makes no transcript claim', async () => {
  const { html } = await page('#/episode/ep_a', api(episodeRec(excerptRecord, { text: null, excerpt_bounds: null })));
  assert.match(html, /<span>Ads not checked yet\.<\/span>/);
  assert.doesNotMatch(html, /Transcript ready/);
  assert.match(html, /This page shows this episode and its ads\. A bookmark saves the episode\./);
  assert.doesNotMatch(html, /This page shows a saved transcript version/);
  assert.equal(aboutAudio(html), 'Clip unavailable');
  assert.doesNotMatch(html, /No clip \(transcript only\)/);
  assert.match(html, /Transcript version 2/, 'the retained provenance stays in About');
});
test('FIX-4 the excerpt evidence About says Clip unavailable', async () => {
  const { html } = await evidence(excerptRecord());
  assert.equal(aboutAudio(html), 'Clip unavailable');
  assert.doesNotMatch(html, /No clip \(transcript only\)/);
});
test('FIX-4 Plan B control: the full episode page keeps its transcript words', async () => {
  const { html } = await page('#/episode/ep_a', api(episodeRec(fullRecord), { excerpt: false }));
  assert.match(html, /Transcript ready\. Ads not checked yet\./);
  assert.match(html, /This page shows a saved transcript version; a bookmark saved here keeps it\./);
  assert.equal(aboutAudio(html), 'No clip (transcript only)');
  const ev = await evidence(fullRecord());
  assert.equal(aboutAudio(ev.html), 'No clip (transcript only)');
});

/* FIX-5: a visible Clip unavailable state in the player area. */
test('FIX-5 the excerpt player shows the Clip unavailable label, then the sentence', async () => {
  const { html } = await evidence(excerptRecord());
  const player = (html.match(/<section class="ew-card ew-player[\s\S]*?<\/section>/) || [''])[0];
  const label = player.search(/<strong>Clip unavailable<\/strong>/);
  const sentence = player.indexOf('No clip for this ad.');
  assert.ok(label >= 0, 'label in the player area');
  assert.ok(sentence > label, 'sentence after the label');
  assert.doesNotMatch(player, /transcript/i);
});
test('FIX-5 Plan B control: the full no-clip note is unchanged', async () => {
  const { html } = await evidence(fullRecord());
  assert.match(html, /No clip for this ad\. The words are below\./);
  assert.doesNotMatch(html, /<strong>Clip unavailable<\/strong>/);
});

/* FIX-6: never assert unseen words were spoken. */
test('FIX-6 with no excerpt What this shows marks an ad', async () => {
  const { html } = await evidence(excerptRecord({ text: null, excerpt_bounds: null }));
  assert.match(html, /We found an ad at this time in this episode\./);
  assert.doesNotMatch(html, /These words were heard/);
});
test('FIX-6 with an excerpt the existing sentence stays', async () => {
  const { html } = await evidence(excerptRecord());
  assert.match(html, /These words were heard at this time in this episode\./);
});

/* POLISH-1 and the final-copy table. */
test('copy: final strings and no TODO(copy) markers', async () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /TODO\(copy\)/);
  const { win, html } = await evidence(excerptRecord());
  assert.doesNotMatch(html, /TODO\(copy\)/);
  assert.equal(win.EWViews._planA.exportNotice(), '<p class="notice">Includes ad details and excerpts. Full transcripts are not included.</p>');
  assert.equal(win.EWViews._planA.refusalText('plan'), 'Not available on your plan');
  const none = await evidence(excerptRecord({ text: null, excerpt_bounds: null }));
  assert.match(none.html, /Excerpt unavailable/);
  const ep = await page('#/episode/ep_a', api(episodeRec(excerptRecord, { text: null, excerpt_bounds: null })));
  assert.match(ep.html, /Full transcripts are not included in your plan\./);
  assert.match(src, /No payment is needed for this workspace\./);
  assert.match(src, /Your payment failed\. Access continues until \$\{esc\(ends\)\}\./);
});

/* BLOCKER-1, client part. */
test('BLOCKER-1 a plan-refused report row reads "Report N — Not available on your plan" with no action', async () => {
  const { win } = await evidence(excerptRecord());
  const row = win.EWViews._planA.reportRow('7', { report_no: 1, exportable: false, export_refusal: 'plan', captured_at: '2026-10-01T00:00:00Z' });
  assert.match(row, /Report 1 — Not available on your plan/);
  assert.doesNotMatch(row, /<a |<button|download/i);
});
test('BLOCKER-1 an opened plan-refused report shows a plain Not available on your plan state', async () => {
  const refused = Object.assign(new Error('not available on your plan'), { status: 403, body: { error: { details: { reason: 'plan' } } } });
  const { html } = await page('#/campaign/7/report/1', p => {
    if (p === '/customer/campaigns/7/reports/1') throw refused;
    throw new Error('unexpected ' + p);
  });
  assert.match(html, /<h2>Not available on your plan<\/h2>/);
  assert.doesNotMatch(html, /role="alert"/);
  assert.doesNotMatch(html, /Download frozen HTML/);
  assert.match(html, /href="#\/campaign\/7"/);
});

/* App's guard question: after a workspace or person change the plan flag is
   reset (unknown). Unknown never renders full transcript: the evidence and
   episode views take their level from the server's record (disclosure /
   excerpt_only), and a Plan A server never sends transcript text. */
test('reset gap: with the plan flag unknown, an excerpt record still renders the excerpt view and asks for no transcript', async () => {
  const { win, html, calls } = await evidence(excerptRecord());
  // The flag the page set is cleared, as a switch does; render again.
  win.EW.clearWorkspaceState({ boundary: false });
  assert.notEqual(win.EW.current.excerptOnly, true);
  const main = win.document.querySelector('#main');
  await win.EWViews.render();
  for (const out of [html, main.innerHTML]) {
    assert.match(out, /<h2 id="ew-said-h">Excerpt<\/h2>/);
    assert.doesNotMatch(out, /What was said|Open in the full transcript/);
  }
  assert.ok(!calls.some(([, p]) => /window|transcript/.test(p)), 'no transcript read: ' + JSON.stringify(calls));
});
test('reset gap: with the plan flag unknown, the episode page still says full transcripts are not included', async () => {
  const { win } = await page('#/episode/ep_a', api(episodeRec(excerptRecord, { text: null, excerpt_bounds: null })));
  win.EW.clearPersonState();
  const main = win.document.querySelector('#main');
  await win.EWViews.render();
  assert.match(main.innerHTML, /Full transcripts are not included in your plan\./);
});
