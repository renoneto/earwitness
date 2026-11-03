/* Demo audio clips, build item 5 (Player): design
   docs/plans/2026-10-22-demo-audio-clips-design.md sections 6.1 and 6.2.
   The 3 s lead-in is always on and the Settings checkbox is gone; the
   no-clip player renders only Sol's neutral sentence; every ad gets one
   player note and no review state (PLAIN-ADS). Loader helpers copied from
   frontend_dv3_g2.test.cjs and frontend_dv3_g5.test.cjs.
   Run: node --test tests/frontend_clip_player.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

const flush = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });

// A local owner session and a preferences store the PUT really changes.
function server(prefs, calls) {
  const state = { prefs: { revision: 1, ...prefs } };
  const fetch = async (url, init = {}) => {
    const method = init.method || 'GET';
    calls.push({ url, method, body: init.body ? JSON.parse(init.body) : null });
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (url === '/api/v1/customer/preferences' && method === 'GET') return json({ ok: true, workspace_context: 'ctx-ws', preferences: state.prefs, revision: state.prefs.revision });
    if (url === '/api/v1/customer/preferences' && method === 'PUT') {
      const body = JSON.parse(init.body);
      delete body.expected_revision;
      state.prefs = { ...state.prefs, ...body, revision: state.prefs.revision + 1 };
      return json({ ok: true, workspace_context: 'ctx-ws', preferences: state.prefs, revision: state.prefs.revision });
    }
    if (url === '/api/v1/customer/reader-status') return json({ ok: true, workspace_context: 'ctx-ws', configured: false, verified: false, note: 'n' });
    throw new Error('unexpected ' + method + ' ' + url);
  };
  return { fetch, state };
}
function loadViews(fetch, route) {
  const store = {};
  const win = {
    console, URLSearchParams, AbortController, Intl,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [],
      addEventListener: (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  win.__listeners = {};
  win.window = win;
  win.globalThis = win;
  vm.createContext(win);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function render(route, api, { prefs = {} } = {}) {
  const win = loadViews(server(prefs, []).fetch, route);
  await settleAll();
  await win.EW.preferences();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async (p, o) => api(p, o);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await settleAll();
  return { html: main.innerHTML, win };
}
async function settingsWin(prefs, calls = []) {
  const win = loadViews(server(prefs, calls).fetch, '#/settings');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  return { win, main };
}
// The player card alone: from its section to the reader grid after it.
function player(html) {
  const at = html.indexOf('<section class="ew-card ew-player');
  assert.ok(at >= 0, 'a player card is rendered');
  const end = html.indexOf('<div class="ew-reader-grid', at);
  return html.slice(at, end < 0 ? undefined : end);
}
const text = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

const REF = 'sr1:c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1';
const EV_ID = 'named:ep_c.named.120000-170000';
const LOCAL = { playback: 'local', kind: 'evidence_clip', source_offset_s: 100, alignment: 'verified', timing: 'verified', trust: 'exact' };
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_c', show_title: 'Quiet Hours', ep_title: 'T', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_c', orig_start_s: 110, orig_end_s: 180 }, media: LOCAL,
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [120, 170], has_clip: true },
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go to northwind.', text_start: 0, text_end: 16 }], text_complete: true, next_cursor: null },
  ...over });
const evApi = rec => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  throw new Error('unexpected ' + p);
};
const evidence = (rec, opts) => render('#/evidence/' + encodeURIComponent(REF), evApi(rec), opts);

const NO_CLIP = 'No clip for this ad. The words are below.';
// Every sentence audioTechHtml and audioStateLine can write.
const TECH = /tech-details|Technical details|ew-audio-note|No clip \(transcript only\)|Rotated|Remote recording only|Source audio|No recording of this episode|not checked against|Timing not checked|No local playback|transcript remains readable|Clip kept|Audio changed/;

/* -- 6.1 Lead-in always on ------------------------------------------------ */

test('clipStart is the read start minus 3 s whatever the preference', async () => {
  const win = loadViews(server({}, []).fetch, '#/overview');
  for (const prefs of [undefined, {}, { clip_lead_in: false }, { clip_lead_in: true }]) {
    assert.equal(win.EW.clipStart(60, prefs), 57, String(JSON.stringify(prefs)));
    assert.equal(win.EW.clipStart(1.5, prefs), 0, 'never before the file start');
  }
  assert.equal(win.EW.clipStart(null), null, 'unknown stays unknown');
  assert.equal(win.EW.clipStart('x'), null);
});

test('a stored lead-in of false still starts the clip and Back 3 s before the read', async () => {
  for (const prefs of [{ clip_lead_in: false }, {}]) {
    const { html } = await evidence(record(), { prefs });
    // read start 120, recording offset 100: 120 - 100 - 3 = 17
    assert.match(html, /\/media#t=17\.0"/, JSON.stringify(prefs));
    assert.match(html, /data-start="120" data-end="170" data-offset="100" data-lead="17"/);
  }
});

test('Settings has no lead-in checkbox and Save sends no lead-in value', async () => {
  const calls = [];
  const { win, main } = await settingsWin({ revision: 4, clip_lead_in: true }, calls);
  assert.match(main.innerHTML, /<form id="settings-display"/);
  assert.doesNotMatch(main.innerHTML, /clip_lead_in|Start clips 3 seconds early/);
  const form = { dataset: {}, isConnected: true, elements: [], getAttribute: a => (a === 'id' ? 'settings-display' : null),
    closest: () => null, contains: () => false, querySelectorAll: () => [],
    querySelector: s => ({ '.ew-save-state': { dataset: {}, textContent: '' }, '[type=submit]': { disabled: false, textContent: '' },
      '[data-form-errors]': { innerHTML: '', setAttribute() {}, focus() {} } }[s] || null) };
  win.FormData = class { *entries() { yield* [['expected_revision', '4'], ['display_name_set', ''], ['search_tab', 'ads'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', '']]; } };
  main.querySelector = s => (s === '#settings-display' ? form : null);
  await win.EWViews.handleSubmit(form); await settleAll();
  const put = calls.find(c => c.method === 'PUT');
  assert.ok(put, 'saved');
  assert.equal('clip_lead_in' in put.body, false, 'the stored value is left as it was');
});

/* -- 6.2 No clip ------------------------------------------------------------ */

// What the server sends when there is no clip, for each reason in 6.2.
const NO_CLIP_CASES = [
  ['not selected (no clip row)', {}],
  ['read over 90 s (no recording proof)', { playback: 'unavailable', reason: 'no authorized recording proof', alignment: 'unknown' }],
  ['detached version', { playback: 'unavailable', reason: 'registered media lacks pinned recording proof', alignment: 'unknown' }],
  ['file missing or changed', { playback: 'unavailable', alignment: 'mismatch', reason: 'recording alignment mismatch', trust: 'rotated' }],
  ['audio denied', { playback: 'remote', alignment: 'unknown', reason: 'audio not allowed' }],
];
for (const [name, media] of NO_CLIP_CASES) {
  test(`no clip, ${name}: only Sol's sentence, no technical details or reason`, async () => {
    const card = player((await evidence(record({ media }))).html);
    assert.equal(card.split(NO_CLIP).length - 1, 1, 'the sentence once');
    assert.doesNotMatch(card, TECH);
    assert.doesNotMatch(card, /<audio|data-action="clip-toggle"/, 'nothing to play');
    if (media.reason) assert.ok(!card.includes(media.reason), 'no raw reason');
  });
}

/* -- 6.2 Player note ------------------------------------------------------ */

// PLAIN-ADS (DEP7): every ad, whatever its review, gets the eyebrow "Ad"
// and one note; how its times were set stays internal. A mention or an ad
// EarWitness removed is never called an ad on the player.
const NOTE = 'A time shows where it was heard, not proof it ran as bought.';
for (const status of ['finding', 'confirmed', 'unresolved', 'mention', 'rejected']) {
  for (const basis of [undefined, 'checked', 'corrected']) {
    test(`${status} (${basis || 'detected'}): no "time shows" note, no review state`, async () => {
      const rec = record({ status, review: { judgment: 'ad', bounds_verified: basis === 'checked' ? 1 : 0 } });
      if (basis) rec.read.display_basis = basis;
      for (const media of [LOCAL, {}, { ...LOCAL, trust: 'rotated' }]) {
        const card = player((await evidence({ ...rec, media })).html);
        assert.equal(card.split(`<p class="ew-player-note">${NOTE}</p>`).length - 1, 0, card);
        assert.doesNotMatch(card, /time shows where it was heard/, card);
        assert.doesNotMatch(card, /review team|detector|Detected window|not checked/, card);
        const ad = status !== 'mention' && status !== 'rejected';
        assert.equal(/<span class="eyebrow ew-player-kind">Ad<\/span>/.test(card), ad, card);
      }
    });
  }
}

/* -- 6.3 Phone layout (CSS rules only; rendering is Sol's screenshot acceptance) -- */

// Every rule inside evidence.css's "@media (max-width: 599px)" blocks, as selector -> declarations.
function phoneRules() {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = {};
  let at = 0;
  while ((at = css.indexOf('@media (max-width: 599px)', at)) >= 0) {
    let i = css.indexOf('{', at) + 1, depth = 1;
    const from = i;
    while (depth) { if (css[i] === '{') depth++; else if (css[i] === '}') depth--; i++; }
    for (const m of css.slice(from, i - 1).matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      for (const sel of m[1].split(',').map(s => s.trim())) rules[sel] = (rules[sel] || '') + m[2];
    }
    at = i;
  }
  return rules;
}
const px = (decl, prop) => { const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*(\\d+)px`).exec(decl || ''); return m ? Number(m[1]) : null; };

test('phone: the clip player row is two rows, play and times, then −5s, +5s and Speed', () => {
  const r = phoneRules();
  assert.match(r['.ew-clip .ew-player-row'] || '', /display:\s*grid/);
  assert.match(r['.ew-clip .ew-player-row'] || '', /grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)/);
  assert.match(r['.ew-clip .ew-player-controls'] || '', /grid-column:\s*1\s*\/\s*-1/);
  assert.equal(px(r['.ew-player .ew-play-lg'], 'width'), 52, 'the 52 px play button is kept');
  assert.equal(r['.ew-player .ew-player-row'], undefined, 'the no-clip row keeps its own layout');
});

test('phone: the ad-row play target is at least 44 px', () => {
  const r = phoneRules();
  assert.ok(px(r['.ew-ad-row > .ew-play'], 'width') >= 44, r['.ew-ad-row > .ew-play']);
  assert.ok(px(r['.ew-ad-row > .ew-play'], 'height') >= 44);
  assert.ok(Number((/grid-template-columns:\s*(\d+)px/.exec(r['.ew-ad-row'] || '') || [])[1]) >= 44, 'the column fits it');
});

/* -- Sol R1-01: a real loading state ------------------------------------- */

const LOADING = 'Loading clip…';
// A fake player region, its live status, its play button and its audio element.
function fakePlayer(html) {
  const tag = html.match(/<section[^>]*data-clip-id="ev"[^>]*>/)[0];
  const dataset = {};
  for (const m of tag.matchAll(/data-([a-z-]+)="([^"]*)"/g)) dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[2];
  const attrsOf = () => { const a = {}; return { attrs: a, setAttribute: (k, v) => { a[k] = String(v); }, removeAttribute: k => { delete a[k]; }, getAttribute: k => (k in a ? a[k] : null) }; };
  const status = { textContent: '' };
  const err = { hidden: true };
  const cls = new Set();
  const box = { dataset, ...attrsOf(), classList: { add: c => cls.add(c), remove: c => cls.delete(c), toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)), contains: c => cls.has(c) },
    querySelector: sel => (sel === '[data-clip-status]' ? status : sel === '.ew-clip-error' ? err : null) };
  const btn = { dataset: { clip: 'ev' }, ...attrsOf(), classList: { toggle() {} }, innerHTML: '' };
  btn.setAttribute('aria-label', 'Play clip, 2:00 to 2:50');
  // preload="none": nothing is buffered until a play (HAVE_NOTHING).
  const audio = { tagName: 'AUDIO', dataset: { clip: 'ev' }, paused: true, ended: false, currentTime: 0, readyState: 0, closest: () => box,
    play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; } };
  return { box, btn, audio, status, err, cls };
}
async function playerWin() {
  const r = await evidence(record({ status: 'finding' }));
  const p = fakePlayer(r.html);
  r.win.document.querySelectorAll = sel => (sel === 'audio[data-clip]' ? [p.audio] : /clip-toggle/.test(sel) ? [p.btn] : []);
  r.win.document.getElementById = id => (id === 'clip-ev' ? p.audio : null);
  const fire = (type, over = {}) => { Object.assign(p.audio, over); for (const fn of r.win.__listeners[type] || []) fn({ type, target: p.audio }); };
  return { ...r, ...p, fire };
}
const busy = p => p.box.getAttribute('aria-busy') === 'true' && p.btn.getAttribute('aria-busy') === 'true' && p.status.textContent === LOADING;
const idle = p => p.box.getAttribute('aria-busy') !== 'true' && p.btn.getAttribute('aria-busy') !== 'true' && p.status.textContent === '';
const showsPlaying = p => p.btn.getAttribute('aria-pressed') === 'true' || p.btn.getAttribute('aria-label') === 'Pause clip';

test('loading: the player holds an empty polite live status; Speed and the scrubber keep their labels', async () => {
  const card = player((await evidence(record())).html);
  assert.equal(card.split('<p class="ew-clip-status" role="status" aria-live="polite" data-clip-status></p>').length - 1, 1, card);
  assert.match(card, /<label class="ew-player-speed"><span>Speed<\/span><select data-clip-speed="ev">/);
  assert.match(card, /<input type="range"[^>]*aria-label="Position in clip, 2:00 to 2:50">/);
  const noClip = player((await evidence(record({ media: {} }))).html);
  assert.doesNotMatch(noClip, /data-clip-status/, 'nothing to load without a clip');
});

test('(2a) the seek bar: a slider over the clip span in seconds, elapsed on the left, total on the right, with its spoken value', async () => {
  const card = player((await evidence(record())).html);
  // The read 2:00-2:50: a 50 s clip.
  assert.match(card, /<div class="ew-player-progress"><span class="ew-time" data-clip-elapsed>0:00<\/span><input type="range" min="0" max="50" step="1" value="0" data-clip-seek="ev" aria-valuemin="0" aria-valuemax="50" aria-valuenow="0" aria-valuetext="0:00 of 0:50" aria-label="Position in clip, 2:00 to 2:50"><span class="ew-time">0:50<\/span><\/div>/, card);
});

test('loading: an ad row with a clip holds the live status too', async () => {
  const views = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  const row = views.slice(views.indexOf('function episodeAdRow('), views.indexOf('// The coverage sentence and tag under the episode title.'));
  assert.match(row, /<p class="ew-clip-status" role="status" aria-live="polite" data-clip-status><\/p>/);
});

test('loading: a requested play is busy at once, and the play event does not present it as playing', async () => {
  const p = await playerWin();
  assert.ok(idle(p));
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  assert.ok(busy(p), JSON.stringify([p.box.attrs, p.btn.attrs, p.status.textContent]));
  assert.ok(!showsPlaying(p));
  p.fire('play');
  assert.ok(busy(p));
  assert.ok(!showsPlaying(p), 'play is not playing');
  p.fire('timeupdate', { currentTime: 17.5 });
  assert.ok(busy(p), 'only playing, pause, ended or error clear it');
});

test('loading: a play that starts outside the button (media keys) is busy and not shown as playing', async () => {
  const p = await playerWin();
  p.fire('play', { paused: false });
  assert.ok(busy(p));
  assert.ok(!showsPlaying(p));
});

test('loading: playing clears it and presents the pause control', async () => {
  const p = await playerWin();
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('playing');
  assert.ok(idle(p), JSON.stringify([p.box.attrs, p.btn.attrs, p.status.textContent]));
  assert.equal(p.btn.getAttribute('aria-label'), 'Pause clip');
  assert.equal(p.btn.getAttribute('aria-pressed'), 'true');
});

for (const type of ['waiting', 'stalled']) {
  test(`loading: ${type} while playing is busy again and not presented as playing`, async () => {
    const p = await playerWin();
    await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
    p.fire('playing');
    // Below HAVE_FUTURE_DATA (3): the element has run out of data.
    p.fire(type, { readyState: 2 });
    assert.ok(busy(p));
    assert.ok(!showsPlaying(p));
    p.fire('playing');
    assert.ok(idle(p));
  });
}

/* CLIPUI-R5-01: browsers fire stalled after ~3 s with no new network data
   whatever is buffered, so a clip that still plays from its buffer
   (readyState 3 or 4) is not loading and keeps Pause. */
for (const type of ['stalled', 'waiting']) {
  for (const readyState of [3, 4]) {
    test(`loading: ${type} with readyState ${readyState} while playing stays playing, not busy, and keeps Pause (CLIPUI-R5-01)`, async () => {
      const p = await playerWin();
      await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
      p.fire('playing', { readyState: 4 });
      p.fire(type, { readyState });
      assert.ok(idle(p), JSON.stringify([p.box.attrs, p.btn.attrs, p.status.textContent]));
      assert.ok(showsPlaying(p));
      assert.equal(p.btn.getAttribute('aria-label'), 'Pause clip');
      p.fire('timeupdate', { currentTime: 20 });
      p.fire('timeupdate', { currentTime: 30 });
      assert.ok(idle(p));
      assert.equal(p.btn.getAttribute('aria-label'), 'Pause clip');
      // The control it shows is the one it does: Pause pauses.
      await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
      assert.equal(p.audio.paused, true);
    });
  }
}

test('loading: stalled while paused is not a load', async () => {
  const p = await playerWin();
  p.fire('stalled', { paused: true });
  assert.ok(idle(p));
});

for (const type of ['pause', 'ended', 'error']) {
  test(`loading: ${type} clears it and shows the play control`, async () => {
    const p = await playerWin();
    await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
    p.fire(type, { paused: true, ended: type === 'ended' });
    assert.ok(idle(p));
    assert.ok(!showsPlaying(p));
    assert.equal(p.btn.getAttribute('aria-label'), 'Play clip, 2:00 to 2:50');
  });
}

/* -- CLIPUI-SOL-R2-02: the failed state drops the normal-state lines ------ */

// evidence.css rules outside any @media block, as selector -> declarations.
function topRules() {
  let css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  let at;
  while ((at = css.indexOf('@media')) >= 0) {
    let i = css.indexOf('{', at) + 1, depth = 1;
    while (depth) { if (css[i] === '{') depth++; else if (css[i] === '}') depth--; i++; }
    css = css.slice(0, at) + css.slice(i);
  }
  const rules = {};
  for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    for (const sel of m[1].split(',').map(x => x.trim())) rules[sel] = (rules[sel] || '') + m[2];
  }
  return rules;
}

test('failed: "Clip available" and the playback hint are hidden while the player has is-error', async () => {
  const card = player((await evidence(record())).html);
  assert.match(card, /<p class="ew-audio-note"><strong>Clip available<\/strong><\/p>/);
  assert.equal(card.split('<p class="ew-player-note ew-clip-hint">Playback starts only when requested.</p>').length - 1, 1, card);
  const r = topRules();
  for (const sel of ['.ew-clip.is-error .ew-audio-note', '.ew-clip.is-error .ew-clip-hint']) {
    assert.match(r[sel] || '', /display:\s*none/, sel);
  }
  // Sol's player note is not the hint: it stays in the failed state.
  assert.doesNotMatch(card, /<p class="ew-player-note ew-clip-hint">The detector/);
});

test('failed: error sets is-error; Retry clears it and the error line', async () => {
  const p = await playerWin();
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('error', { paused: true });
  assert.ok(p.cls.has('is-error'));
  assert.equal(p.err.hidden, false);
  p.audio.load = () => {};
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-retry', clip: 'ev' } });
  assert.ok(!p.cls.has('is-error'));
  assert.equal(p.err.hidden, true);
});

/* CLIPUI-R6-01: a media error leaves audio.error set; per the HTML spec,
   play() then rejects (MEDIA_ERR_SRC_NOT_SUPPORTED, which a 403 or 404
   source reports) and fires no event, and only load() resets error. */
function failedAudio(p) {
  p.audio.loads = 0;
  p.audio.load = function () { this.loads++; this.error = null; this.paused = true; this.readyState = 0; };
  p.audio.play = function () {
    if (this.error) return Promise.reject(new Error('NotSupportedError'));
    this.paused = false; return Promise.resolve();
  };
}

test('failed: Play (not Retry) after a failed load takes the Retry path and can play', async () => {
  const p = await playerWin();
  failedAudio(p);
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('error', { paused: true, error: { code: 4 } });
  assert.ok(p.cls.has('is-error'));
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  await settleAll();
  assert.equal(p.audio.loads, 1, 'Play reloads a failed clip');
  assert.ok(!p.cls.has('is-error'), 'the error is cleared as Retry would');
  assert.equal(p.err.hidden, true);
  assert.equal(p.audio.paused, false, 'play was requested after the reload');
  assert.ok(busy(p), 'loading while the reload runs');
  p.fire('playing', { readyState: 4 });
  assert.ok(idle(p));
  assert.equal(p.btn.getAttribute('aria-label'), 'Pause clip');
});

test('failed: a play() that rejects with no event never leaves "Loading clip…" behind', async () => {
  const p = await playerWin();
  failedAudio(p);
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('error', { paused: true, error: { code: 4 } });
  // The reload is refused too (for example a NotAllowedError): no event follows.
  p.audio.play = function () { return Promise.reject(new Error('NotAllowedError')); };
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  await settleAll();
  assert.ok(idle(p), JSON.stringify([p.box.attrs, p.btn.attrs, p.status.textContent]));
  assert.ok(!showsPlaying(p));
  assert.ok(!(p.cls.has('is-error') && p.status.textContent === LOADING), 'never the error line and Loading together');
  // The reload then fails through its error event: a coherent failed state.
  p.fire('error', { paused: true, error: { code: 4 } });
  assert.ok(p.cls.has('is-error'));
  assert.equal(p.err.hidden, false);
  assert.ok(idle(p));
});

test('failed: after a failed play (paused stays false), one Play press reloads (R7-01)', async () => {
  const p = await playerWin();
  failedAudio(p);
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('play');
  // The source fails after play() started: error is set and paused stays false.
  p.fire('error', { paused: false, error: { code: 4 } });
  assert.ok(p.cls.has('is-error'));
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  await settleAll();
  assert.equal(p.audio.loads, 1, 'the first Play press reloads');
  assert.ok(!p.cls.has('is-error'), 'the error is cleared');
  assert.equal(p.err.hidden, true);
  assert.ok(busy(p), JSON.stringify([p.box.attrs, p.btn.attrs, p.status.textContent]));
});

test('loading: a rejected play() leaves the busy state alone while the clip is not paused (R7-02)', async () => {
  const p = await playerWin();
  // An older play request is rejected while a newer one keeps the element playing and loading.
  // The rejection lands after the newer request's waiting event.
  p.audio.play = function () { this.paused = false; return new Promise((_, rej) => setTimeout(() => rej(new Error('AbortError')), 0)); };
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('waiting', { readyState: 2 });
  await settleAll();
  assert.ok(busy(p), JSON.stringify([p.box.attrs, p.btn.attrs, p.status.textContent]));
});

/* -- CLIPUI-R9-01: Play after the read's end replays from the lead-in --- */

test('replay: Play after the clip stopped at the read\u2019s end starts again at the lead-in and keeps playing', async () => {
  const p = await playerWin();
  assert.equal(p.box.dataset.lead, '17', 'lead-in start: 120 - 100 offset - 3 s');
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('playing', { readyState: 4 });
  // Played to the read's end (170 s, media time 70): the clip stops itself.
  p.fire('timeupdate', { currentTime: 70.1 });
  assert.equal(p.audio.paused, true, 'stops at the end');
  p.fire('pause');
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  assert.equal(p.audio.currentTime, 17, 'the position moves back to the lead-in start');
  assert.equal(p.audio.paused, false, 'play was requested');
  p.fire('playing', { readyState: 4 });
  p.fire('timeupdate');
  assert.equal(p.audio.paused, false, 'not paused again by the next time update');
  assert.equal(p.btn.getAttribute('aria-label'), 'Pause clip');
});

// CLIPUI-R10-01: only a Play at or past the read's end rewinds.
async function pauseAt(p, t) {
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('playing', { readyState: 4 });
  p.fire('timeupdate', { currentTime: t });
  if (!p.audio.paused) p.audio.pause();
  p.fire('pause');
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
}

test('replay: Play after a pause mid-read resumes where it paused', async () => {
  const p = await playerWin();
  await pauseAt(p, 40);
  assert.equal(p.audio.currentTime, 40);
  assert.equal(p.audio.paused, false);
});

test('replay: Play after stopping exactly at the read\u2019s end starts at the lead-in', async () => {
  const p = await playerWin();
  await pauseAt(p, 70);
  assert.equal(p.audio.currentTime, 17);
  assert.equal(p.audio.paused, false);
});

test('replay: with no known end, Play after a pause resumes where it paused', async () => {
  const p = await playerWin();
  p.box.dataset.end = '';
  await pauseAt(p, 40);
  assert.equal(p.audio.currentTime, 40);
  assert.equal(p.audio.paused, false);
});

/* -- CLIPUI-R10-02: the chosen speed survives load() --------------------- */

for (const recovery of ['clip-retry', 'clip-toggle']) {
  test(`speed: the chosen speed survives ${recovery === 'clip-retry' ? 'Retry' : 'Play'} after a failed load`, async () => {
    const p = await playerWin();
    failedAudio(p);
    // The media load algorithm resets playbackRate to defaultPlaybackRate.
    Object.assign(p.audio, { playbackRate: 1, defaultPlaybackRate: 1 });
    const load = p.audio.load;
    p.audio.load = function () { load.call(this); this.playbackRate = this.defaultPlaybackRate; };
    const select = { tagName: 'SELECT', dataset: { clipSpeed: 'ev' }, value: '1.5' };
    for (const fn of p.win.__listeners.change || []) fn({ type: 'change', target: select });
    assert.equal(p.audio.playbackRate, 1.5);
    await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
    p.fire('error', { paused: false, error: { code: 4 } });
    await p.win.EWViews.handleAction({ dataset: { action: recovery, clip: 'ev' } });
    await settleAll();
    assert.equal(p.audio.loads, 1, 'the clip reloaded');
    assert.equal(p.audio.defaultPlaybackRate, 1.5);
    assert.equal(p.audio.playbackRate, 1.5, 'the select still reads 1.5\u00d7, so the clip plays at 1.5\u00d7');
  });
}

/* -- Owner decision (a), replacing CLIP-UI-SOL-R3-01: a second press on a
   loading clip (pressed, not yet playing) stops it ----------------------- */

test('loading: a second press during the initial load stops the clip, clears loading and reads as stopped', async () => {
  const p = await playerWin();
  let pauses = 0;
  const pause = p.audio.pause;
  p.audio.pause = function () { pauses++; pause.call(this); };
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('play');
  assert.ok(busy(p));
  assert.equal(p.btn.getAttribute('aria-disabled'), null, 'the loading control is available: a press stops it');
  assert.ok(!showsPlaying(p), 'keeps the Play presentation');
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  assert.equal(pauses, 1, 'the second press stops the pending play');
  assert.equal(p.audio.paused, true);
  assert.ok(idle(p), 'the loading state clears');
  assert.ok(!showsPlaying(p), 'reads as stopped');
  assert.equal(p.btn.getAttribute('aria-pressed'), 'false');
  // A press once playing still pauses.
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  p.fire('playing', { readyState: 4, paused: false });
  assert.equal(p.btn.getAttribute('aria-label'), 'Pause clip');
  await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
  assert.equal(pauses, 2);
  assert.equal(p.audio.paused, true);
});

test('loading: the unavailable play control looks unavailable (evidence.css)', () => {
  const r = topRules();
  assert.match(r['.ew-play[aria-disabled=true]'] || '', /cursor:\s*progress/);
  assert.match(r['.ew-play[aria-disabled=true]'] || '', /opacity:\s*\.6/);
});

for (const end of ['pause', 'ended', 'error']) {
  test(`loading: ${end} during the initial load makes the control available again`, async () => {
    const p = await playerWin();
    await p.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: 'ev' } });
    assert.ok(busy(p));
    p.fire(end, { paused: true });
    assert.equal(p.btn.getAttribute('aria-disabled'), null);
    assert.ok(idle(p));
  });
}

/* -- Sol R1-03: the rotated-feed note in product words -------------------- */

test('rotated feed with a local clip: Sol’s note inside Technical details, under Audio changed since transcription', async () => {
  const card = player((await evidence(record({ media: { ...LOCAL, trust: 'rotated' } }))).html);
  assert.match(card, /<p class="ew-audio-note"><strong>Audio changed since transcription<\/strong><\/p><details class="tech-details"><summary>Technical details<\/summary><p class="small muted">This feed can change what plays near the start of an episode\. This clip may not match the transcript exactly\.<\/p><\/details>/);
  assert.doesNotMatch(card, /aligns approximately|creative/);
});
