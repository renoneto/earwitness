/* Astra fixes, reader area (triage G1, W-B/W-C/W-D):
   UX-04 Evidence: the transcript before the pod on phone; short time and
   audio notes. UX-05: "Processing stopped partway" leads the episode
   status; the tier is labelled "Coverage tier". UX-06: Find in loaded
   transcript, the loaded time range, a time control beside the limit
   note, and plain Load-the-next-part wording.
   Loader helpers copied from frontend_dv3_final_a.test.cjs.
   Run: node --test tests/frontend_dv3_astra_reader.test.cjs */
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
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, { hosted = null } = {}) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  if (hosted !== null) win.EW.current.hosted = hosted;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  return { html: main.innerHTML, win };
}
const CSS = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');

/* -- UX-04 Evidence ------------------------------------------------------ */
const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const EV_ID = 'named:ep_f.named.120000-180000';
const epAd = (id, span, display) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }, needs_review: false, self_promo: false,
  kind: 'named', source_label: 'Named read', transcript_start_s: span[0], media: null });
const record = () => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_f', show_title: 'Quiet Hours', ep_title: 'T', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_f', orig_start_s: 110, orig_end_s: 180 }, media: { playback: 'remote', alignment: 'unknown' },
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [121.5, 170], has_clip: false },
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null } });
// D-1: the server sends the read's ad break (the reads overlap; a lone read has a one-read break).
const evApi = ads => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: { ...record(), ad_break: ads.length > 1 ? { id: 'brk:0', span: [60, 170], read_count: 2, index: 1, refs: ['e1', EV_ID], brands: [], on_current_transcript: true } : { id: 'brk:1', span: [121.5, 170], read_count: 1, index: 0, refs: [EV_ID], brands: [], on_current_transcript: true } } };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  if (p === '/customer/episodes/ep_f') return { episode: { episode_id: 'ep_f', self_promo: [], screened_no_ads: false,
    screening: { label: 'Screened', jev_screened: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false }, ads } };
  return new Error('unexpected ' + p);
};
const podAds = [epAd('e1', [60, 130], 'Northwind'), epAd(EV_ID, [121.5, 170], 'Northwind')];

test('UX-04: on Evidence the pod sits in the grid after the words; phone order is facts, said, pod, side', async () => {
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(podAds), { hosted: false });
  assert.match(html, /<h2 id="ew-pod-h">Other evidence for this ad/);
  assert.match(html, /One of 2 pieces of evidence for this ad/);
  assert.match(html, /<div class="ew-reader-grid has-facts has-pod"><section class="ew-card ew-facts-card">/);
  assert.ok(html.indexOf('What was said') < html.indexOf('ew-pod-card'), 'the words come before the pod in page order');
  assert.ok(html.indexOf('ew-pod-card') < html.indexOf('ew-reader-side'), 'the pod comes before the side column');
  assert.match(CSS, /\.ew-reader-grid > \.ew-pod-card \{ grid-area: pod; \}/);
  assert.match(CSS, /\.ew-reader-grid\.has-facts\.has-pod \{ grid-template-areas: "pod pod" "said facts" "said side";/, 'desktop keeps the pod above the words');
  assert.match(CSS, /\.ew-reader-grid\.has-facts\.has-pod \{ grid-template-areas: "facts" "said" "pod" "side"; grid-template-rows: none; \}/, 'phone order');
  // The accepted facts-first rules stay as they were (UX-13).
  assert.match(CSS, /\.ew-reader-grid\.has-facts \{ grid-template-areas: "facts" "said" "side"; grid-template-rows: none; \}/);
});

test('UX-04: a read without a pod has no has-pod class', async () => {
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi([epAd(EV_ID, [121.5, 170], 'Northwind')]), { hosted: false });
  assert.match(html, /<div class="ew-reader-grid has-facts">/);
  assert.doesNotMatch(html, /has-pod|ew-pod-card/);
});

test('UX-04: the time and audio notes are short and keep every caveat', async () => {
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), evApi(podAds), { hosted: false });
  // PLAIN-ADS (DEP7): every ad gets one note; with no clip the player says one neutral sentence, no reason.
  assert.ok(!html.includes('A time shows where it was heard'), 'the "A time shows" note is gone');
  assert.ok(html.includes('<p class="ew-player-note">No clip for this ad. The words are below.</p>'));
  assert.doesNotMatch(html, /Remote recording only|Timing not checked/);
  assert.doesNotMatch(html, /Where the detector placed this read|A remote stream is known/);
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /The detector’s window/, 'no detector-window caveat');
});

/* -- UX-05 and UX-06 Episode -------------------------------------------- */
const seg = (i, start, end, text) => ({ segment_id: i, start_s: start, end_s: end, text, text_start: 0, text_end: text.length });
const epRecord = (window = {}) => ({ ref: 'ep:ep_f', source_ref: 'sr1:e2e2', canonical_source_ref: 'sr1:e2e2', record_kind: 'source_publication', relationship: 'current',
  episode: { episode_id: 'ep_f', transcript_version_id: 3, ep_title: 'T', show_title: 'S', pub_date: '2026-10-09', duration_min: 168 },
  show: { show_identity_id: 'cs-long', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 10080 }, selected_bounds: { start_s: 0, end_s: 10080 },
  bounds_basis: 'full_effective_source', media: {},
  window: { segments: [seg(1, 0, 60, 'Hello there.'), seg(2, 60, 209, 'Go to AG1.')], text_complete: false, next_cursor: 'c2', truncation_reason: 'limit', ...window } });
const screening = over => ({ label: 'Screened', jev_screened: true, screened_no_ads: true, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null, ...over });
const epApi = (rec, sc) => p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_f')) return { record: rec };
  if (p === '/customer/episodes/ep_f') return { episode: { episode_id: 'ep_f', ads: [], self_promo: [], screened_no_ads: true, screening: sc } };
  if (p.startsWith('/customer/records/sr1%3Ae2e2?cursor=c2')) {
    return { record: { ref: 'sr1:e2e2', window: { segments: [seg(3, 209, 400, 'More words.'), seg(4, 400, 484, 'Last.')], text_complete: false, next_cursor: 'c3', truncation_reason: 'limit' } } };
  }
  return new Error('unexpected ' + p);
};
const statusLine = html => (html.match(/<p class="ew-episode-status">[\s\S]*?<\/p>/) || [''])[0];

test('UX-05: a stopped episode leads with "Processing stopped partway"; the tier is labelled Coverage tier', async () => {
  const { html } = await render('#/episode/ep_f', epApi(epRecord(), screening({ stopped_partway: true, stage: 'transcribe', screened_no_ads: false })));
  const line = statusLine(html);
  assert.match(line, /data-tag="gap"[^>]*>[\s\S]*Processing stopped partway/);
  assert.doesNotMatch(line, /Full coverage|data-tag="tier"/);
  assert.match(line, /The ads in this episode are not fully known\./);
  assert.match(html, /<p class="section-note ew-episode-tier">Coverage tier: Full \(transcript and ad list\) — this episode did not finish processing\.<\/p>/);
  assert.ok(html.indexOf('Processing stopped partway') < html.indexOf('Coverage tier'));
  assert.match(html, /The ads in this episode are not fully known/, 'the Ads card notice stays');
  assert.match(html, /Processing stopped partway, at /);
  assert.match(CSS, /\.ew-episode-tier \{ margin: 0; \}/);
});

test('UX-05: a screened full episode keeps the Full coverage tag first', async () => {
  const { html } = await render('#/episode/ep_f', epApi(epRecord(), screening()));
  const line = statusLine(html);
  assert.match(line, /^<p class="ew-episode-status"><span class="ew-tag ew-tier ew-tier-full" data-tag="tier">[\s\S]*Full coverage/);
  assert.doesNotMatch(html, /Coverage tier:|data-tag="gap"/);
});

test('UX-06: the limit note shows the loaded time range, a time control and the next-part wording', async () => {
  const { html } = await render('#/episode/ep_f', epApi(epRecord(), screening()));
  assert.match(html, /<input type="search" class="ew-find"[^>]* data-transcript-find[^>]*placeholder="Find in loaded transcript"[^>]*aria-label="Find in loaded transcript"/);
  assert.match(html, /<section class="ew-card ew-transcript"[^>]* data-show="cs-long" data-duration="10080"/);
  assert.match(html, /data-loaded-from="0" data-loaded-to="209"/);
  assert.match(html, /<p class="section-note" data-window-bound[^>]*>Showing 0:00–3:29 of this 2:48:00 episode\. Load the next part continues from 3:29, or open the transcript at a time\.<\/p>/);
  assert.match(html, /<p class="small muted ew-find-scope" data-find-scope>Finds words in the loaded part: 0:00–3:29 of 2:48:00\.<\/p>/);
  assert.match(html, /<p class="small ew-find-more" data-find-more hidden><a class="text-link" href="#">Search this show’s transcripts for these words<\/a><\/p>/);
  assert.match(html, /<form id="transcript-jump" class="ew-transcript-jump" data-episode="ep_f" data-duration="10080" novalidate>/);
  assert.match(html, /<label for="tr-jump">Open the transcript at<\/label>/);
  assert.match(html, />Load the next part</);
  assert.match(html, /<span class="small muted">Loaded 0:00–3:29<\/span>/);
  assert.doesNotMatch(html, /chunks|Window bound|same pin|Narrow the interval|Load more of this interval/);
  assert.doesNotMatch(html, /style=/);
  assert.match(CSS, /\.ew-find-scope \{ margin: 0; \}/);
  assert.match(CSS, /\.ew-transcript-jump \{ display: flex;/);
});

test('UX-06: the Evidence reader names its loaded range without the episode jump', async () => {
  const rec = record();
  rec.window = { segments: [seg(1, 120, 150, 'Go.')], text_complete: false, next_cursor: 'c2', truncation_reason: 'byte_cap' };
  const { html } = await render('#/evidence/' + encodeURIComponent(REF), p => (p === '/customer/records/' + encodeURIComponent(REF) ? { record: rec } : evApi([])(p)), { hosted: false });
  assert.match(html, /Showing 2:00–2:30 of the selected passage\. Load the next part continues from 2:30\./);
  assert.doesNotMatch(html, /transcript-jump|chunks/);
});

test('UX-06: a complete transcript has no limit note and says where it ends (V-37)', async () => {
  const { html } = await render('#/episode/ep_f', epApi(epRecord({ text_complete: true, next_cursor: null, truncation_reason: null }), screening()));
  assert.doesNotMatch(html, /data-window-bound|transcript-jump/);
  assert.match(html, /End of transcript \(3:29\)\./);
});

test('UX-06: a cut-off window without a cursor says the rest did not load', async () => {
  const { html } = await render('#/episode/ep_f', epApi(epRecord({ next_cursor: null }), screening()));
  assert.match(html, /Loaded 0:00–3:29; the rest did not load\./);
  assert.match(html, /Showing 0:00–3:29 of this 2:48:00 episode\. Open the transcript at a time to read further\./);
  assert.doesNotMatch(html, /Load the next part/);
});

function findBox({ show = 'cs-long' } = {}) {
  const lis = [{ textContent: 'Hello there.', hidden: false }, { textContent: 'Go to AG1.', hidden: false }].map(l => ({ ...l, querySelector: () => l }));
  const status = { textContent: '' }, scope = { textContent: '' };
  const link = { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
  const more = { hidden: true, querySelector: () => link };
  const win = { dataset: { loadedFrom: '0', loadedTo: '209' } };
  const box = { dataset: { show, duration: '10080' }, querySelectorAll: () => lis,
    querySelector: sel => ({ '[data-find-status]': status, '[data-find-scope]': scope, '[data-find-more]': show ? more : null, '.window-text': win })[sel] || null };
  return { box, lis, status, scope, more, link };
}
const input = (box, v) => ({ target: { value: v, matches: s => s === '[data-transcript-find]', closest: s => (s === '.ew-transcript' ? box : null) } });

test('UX-06: Find names the loaded part and offers a show search on no match', async () => {
  const { win } = await render('#/episode/ep_f', epApi(epRecord(), screening()));
  const fire = ev => win.__listeners.input.forEach(fn => fn(ev));
  const t = findBox();
  fire(input(t.box, 'ag1'));
  assert.equal(t.status.textContent, '1 of 2 loaded lines match');
  assert.equal(t.scope.textContent, 'Finds words in the loaded part: 0:00–3:29 of 2:48:00.');
  assert.equal(t.more.hidden, true);
  fire(input(t.box, 'Evolutionary sensitivity'));
  assert.equal(t.status.textContent, 'No match in the loaded part (0:00–3:29).');
  assert.equal(t.more.hidden, false);
  assert.equal(t.link.attrs.href, '#/search?mode=mentions&q=Evolutionary+sensitivity&shows=cs-long');
  fire(input(t.box, ''));
  assert.equal(t.status.textContent, '');
  assert.equal(t.more.hidden, true);
  const noShow = findBox({ show: '' });
  fire(input(noShow.box, 'zzz'));
  assert.equal(noShow.status.textContent, 'No match in the loaded part (0:00–3:29).');
});

test('UX-06: the time control opens the transcript at that second; bad times are field errors', async () => {
  const { win } = await render('#/episode/ep_f', epApi(epRecord(), screening()));
  const went = [], errors = [];
  win.EW.go = r => went.push(r);
  win.EW.formError = (m, field) => { errors.push([m, field && field.id]); return false; };
  const field = { id: 'tr-jump' };
  const submit = async (value) => {
    win.FormData = class { *entries() { yield ['at', value]; } };
    const form = { getAttribute: () => 'transcript-jump', dataset: { episode: 'ep_f', duration: '10080' }, querySelector: sel => (sel === '#tr-jump' ? field : null), querySelectorAll: () => [] };
    await win.EWViews.handleSubmit(form);
  };
  await submit('1:00:00');
  assert.deepEqual(went, ['#/episode/ep_f?at=3600']);
  await submit('12:30');
  assert.deepEqual(went.slice(-1), ['#/episode/ep_f?at=750']);
  for (const bad of ['99:99', '3:00:00', 'soon', '']) await submit(bad);
  assert.equal(went.length, 2, 'no navigation on a bad time');
  assert.equal(errors.length, 4);
  for (const [m, id] of errors) { assert.equal(m, 'Enter a time like 12:30 or 1:05:00 within this episode.'); assert.equal(id, 'tr-jump'); }
});

test('UX-06: Load the next part updates the loaded range in the note and the limit text', async () => {
  const { win } = await render('#/episode/ep_f', epApi(epRecord(), screening()));
  const note = { textContent: 'Loaded 0:00–3:29' };
  const btn = { dataset: { cursor: 'c2' } };
  const actions = { querySelector: sel => (sel === '[data-action="window-more"]' ? btn : sel === '.small.muted' ? note : null) };
  const bound = { textContent: '', dataset: { of: 'of this 2:48:00 episode', jump: '1' } };
  const list = { lastElementChild: null, appendChild() {} };
  const box = { dataset: { count: '2', cursor: 'c2', loadedFrom: '0', loadedTo: '209' }, children: [],
    querySelector: sel => ({ 'ol.transcript': list, '.actions': actions, '[data-window-bound]': bound })[sel] || null, closest: () => null };
  await win.EWViews.handleAction({ dataset: { action: 'window-more', ref: 'sr1:e2e2', cursor: 'c2' }, closest: () => box });
  assert.equal(box.dataset.loadedTo, '484');
  assert.equal(note.textContent, 'Loaded 0:00–8:04');
  assert.equal(btn.dataset.cursor, 'c3');
  assert.equal(bound.textContent, 'Showing 0:00–8:04 of this 2:48:00 episode. Load the next part continues from 8:04, or open the transcript at a time.');
});
