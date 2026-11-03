/* G2-01: remove an observation from a research campaign.
   The campaign page has "Remove from campaign" on each observation and the
   evidence page's Add to campaign dialog has it beside "already attached".
   Either opens a confirm naming the ad and the campaign; confirming sends
   DELETE {ref, expected_revision} to the target evidence route; the toast
   says "Removed." with Undo, which re-attaches through the existing POST.
   Loader helpers copied from frontend_dv3_ux_evidence.test.cjs.
   Run: node --test tests/frontend_campaign_remove.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch, toastEl = null } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9171/app/' + route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: sel => (sel === '#toast' ? toastEl : null), querySelectorAll: () => [], addEventListener: () => {},
      body: { style: {} }, createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
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
function loadViews(route) {
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
// A hosted page with a working modal, toasts (with their options) and an API log.
async function page(route, api, { role = 'editor' } = {}) {
  const win = loadViews(route);
  await flushBoot();
  win.EW.current.hosted = true;
  Object.assign(win.EW.current, { principal: 'oidc:https://i.example:judge', workspaceRole: role, activeWorkspace: 'judges-demo' });
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  // Every element handed out records a focus() on it by its selector.
  const focused = [];
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal
    : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} }, focus() { focused.push(sel); } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = (m, opts) => toasts.push([m, opts]); win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, opts = {}) => {
    calls.push([opts.method || 'GET', p, opts.body]);
    const out = await api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, main, modal, toasts, calls, focused };
}
function confirmForm(win, html) {
  const hidden = {};
  for (const m of html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)) hidden[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  win.FormData = class { constructor() { this.e = Object.entries(hidden); } *entries() { yield* this.e; } get(k) { return hidden[k]; } };
  return { getAttribute: k => (k === 'id' ? 'confirm' : null), dataset: {}, querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, elements: [] };
}
// Values made in the page's realm compare as plain data.
const plain = v => JSON.parse(JSON.stringify(v));
const dataOf = tag => Object.fromEntries([...tag.matchAll(/\sdata-([a-z-]+)="([^"]*)"/g)]
  .map(([, k, v]) => [k.replace(/-([a-z])/g, (_, c) => c.toUpperCase()), v.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')]));
const unesc = h => h.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

const NAME = 'BetterHelp ads';
// The campaign page's reads; `state.refs` is what the server holds now.
const researchApi = (state = { refs: ['sr1:a', 'sr1:b'], rev: 5 }, { gone = false, name = NAME, show = 'The Vergecast', status = 'active' } = {}) => (p, opts = {}) => {
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: state.rev, name, status } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: state.rev }, mode: 'research', placements: [],
    targets: [{ id: 't1', scope: 'show_window', show_id: 's1', evidence: state.refs.slice() }], show_titles: { s1: show }, allocation: {},
    coverage_by_placement: {} } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  if (p === '/customer/campaigns/2/targets/t1/evidence' && opts.method === 'DELETE') {
    if (gone) return { target_id: 't1', ref: opts.body.ref, removed: false, campaign_revision: state.rev };
    state.refs = state.refs.filter(r => r !== opts.body.ref);
    state.rev += 1;
    return { target_id: 't1', ref: opts.body.ref, removed: true, campaign_revision: state.rev };
  }
  if (p === '/customer/campaigns/2/targets/t1/evidence' && opts.method === 'POST') {
    state.refs = state.refs.concat([opts.body.ref]).sort();
    state.rev += 1;
    return { target_id: 't1', ref: opts.body.ref, campaign_revision: state.rev };
  }
  throw new Error('unexpected request ' + (opts.method || 'GET') + ' ' + p);
};
const removeTags = html => [...html.matchAll(/<button\b[^>]*data-action="remove-observation"[^>]*>[^<]*<\/button>/g)].map(m => m[0]);

test('FREEZE r3 hosted research page: the attach note carries no possible-ad label', async () => {
  const t = await page('#/campaign/2', researchApi());
  const text = t.main.innerHTML.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
  assert.match(text, /Find a passage in Search, then use Add to campaign on a passage or an ad to keep it here\./);
  assert.doesNotMatch(text, /possible ad stays possible/i);
});

test('G2-01 campaign page: each observation has Remove from campaign naming it and the campaign', async () => {
  const t = await page('#/campaign/2', researchApi());
  const tags = removeTags(t.main.innerHTML);
  assert.equal(tags.length, 2);
  assert.match(tags[0], />Remove from campaign<\/button>$/);
  assert.deepEqual(dataOf(tags[0]), { action: 'remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '5',
    campaign: NAME, label: 'Observation · The Vergecast' });
  assert.equal(dataOf(tags[1]).ref, 'sr1:b');
  // The observation link is unchanged.
  assert.match(t.main.innerHTML, /href="#\/evidence\/sr1%3Aa"[^>]*>Observation 1 · The Vergecast</);
});

test('G2-01 a viewer sees no Remove from campaign', async () => {
  const t = await page('#/campaign/2', researchApi(), { role: 'viewer' });
  assert.equal(removeTags(t.main.innerHTML).length, 0);
  assert.match(t.main.innerHTML, /Observation 1 · The Vergecast/);
});

test('G2-01 Remove opens a confirm naming the observation and the campaign; nothing is sent yet', async () => {
  const t = await page('#/campaign/2', researchApi());
  const before = t.calls.length;
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /<h2 id="dialog-title">Remove from campaign<\/h2>/);
  // CR-SCR-R1-01: the page cannot tell an ad from a passage by this ref.
  assert.ok(m.includes('Remove this observation from “BetterHelp ads”? The ad or passage itself stays.'), m);
  assert.ok(m.includes('<b>Observation · The Vergecast</b>'), m);
  assert.match(m, /<input type="hidden" name="confirm_action" value="remove-observation">/);
  assert.match(m, /data-action="close-dialog"[^>]*>Cancel</);
  assert.match(m, /type="submit"[^>]*>Remove</);
  assert.doesNotMatch(m, /confirmed|possible|verified/i);
  assert.equal(t.calls.length, before);
});

test('G2-01 confirming DELETEs exactly {ref, expected_revision}, toasts Removed with Undo, and the count updates', async () => {
  const state = { refs: ['sr1:a', 'sr1:b'], rev: 5 };
  const t = await page('#/campaign/2', researchApi(state));
  assert.match(t.main.innerHTML, /<h2 id="obs-h">Observations · 2<\/h2>/);
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  await t.win.EWViews.handleSubmit(confirmForm(t.win, t.modal.innerHTML)); await settle();
  const del = t.calls.filter(c => c[0] === 'DELETE');
  assert.deepEqual(plain(del), [['DELETE', '/customer/campaigns/2/targets/t1/evidence', { ref: 'sr1:a', expected_revision: 5 }]]);
  const [msg, opts] = t.toasts.at(-1);
  assert.equal(msg, 'Removed.');
  assert.equal(opts.action.label, 'Undo');
  assert.deepEqual(plain(opts.action.data), { action: 'undo-remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '6', campaign: NAME });
  assert.match(t.main.innerHTML, /<h2 id="obs-h">Observations · 1<\/h2>/);
  assert.deepEqual(removeTags(t.main.innerHTML).map(x => dataOf(x).rev), ['6']);
});

test('G2-01 Undo re-attaches through the existing POST with the new revision', async () => {
  const state = { refs: ['sr1:b'], rev: 6 };
  const t = await page('#/campaign/2', researchApi(state));
  await t.win.EWViews.handleAction({ dataset: { action: 'undo-remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '6', campaign: NAME } }); await settle();
  assert.deepEqual(plain(t.calls.filter(c => c[0] !== 'GET')), [['POST', '/customer/campaigns/2/targets/t1/evidence', { ref: 'sr1:a', expected_revision: 6 }]]);
  assert.equal(t.toasts.at(-1)[0], 'Added back to “BetterHelp ads”.');
  assert.match(t.main.innerHTML, /<h2 id="obs-h">Observations · 2<\/h2>/);
});

test('G2-01 Undo that the server refuses says so in plain words and adds nothing', async () => {
  const t = await page('#/campaign/2', (p, opts = {}) => {
    if (opts.method === 'POST') { const e = new Error('stale revision; current is 9'); e.status = 412; return e; }
    return researchApi({ refs: ['sr1:b'], rev: 9 })(p, opts);
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'undo-remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '6', campaign: NAME } }); await settle();
  assert.equal(t.toasts.at(-1)[0], 'It wasn’t added back: the campaign changed since. Use Add to campaign to add it again.');
});

test('G2-01 a link that is already gone: a plain toast and no Undo', async () => {
  const t = await page('#/campaign/2', researchApi(undefined, { gone: true }));
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  await t.win.EWViews.handleSubmit(confirmForm(t.win, t.modal.innerHTML)); await settle();
  const [msg, opts] = t.toasts.at(-1);
  assert.equal(msg, 'It was already removed from “BetterHelp ads”.');
  assert.ok(!opts || !opts.action);
});

/* The evidence page's Add to campaign dialog. */
const REF = 'sr1:c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3';
const dialogApi = (p, opts = {}) => {
  if (p === '/customer/campaigns') return { campaigns: [{ id: 5, name: 'Market scan', mode: 'research', availability: 'active', revision: 2 }] };
  if (p === '/customer/campaigns/5/targets') return { ok: true, targets: [{ id: 'ct-1', scope: 'show_window', show_id: 'cs-upfirst', evidence: [REF] }] };
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 1, name: 'x' } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 1 }, mode: 'research', placements: [], targets: [], allocation: {}, coverage_by_placement: {} } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  throw new Error('unexpected request ' + p);
};
const readData = { action: 'add-to-campaign', ref: REF, episode: 'ep_ux', show: 'cs-upfirst', day: '2026-10-07', showName: 'Up First' };

test('G2-01 evidence dialog: "already attached" has Remove from campaign, which opens the same confirm', async () => {
  const t = await page('#/campaign/2', dialogApi);
  await t.win.EWViews.handleAction({ dataset: readData }); await settle();
  const m = t.modal.innerHTML;
  // The existing line is unchanged; the button follows it.
  assert.match(m, /value="5\|ct-1" disabled><span><b>Market scan<\/b> · Up First · research observation · already attached<\/span>/);
  const tags = removeTags(m);
  assert.equal(tags.length, 1);
  assert.deepEqual(dataOf(tags[0]), { action: 'remove-observation', cid: '5', tid: 'ct-1', ref: REF, rev: '2', campaign: 'Market scan', label: 'Ad · Up First', kind: 'ad' });
  await t.win.EWViews.handleAction({ dataset: dataOf(tags[0]) }); await settle();
  assert.ok(t.modal.innerHTML.includes('Remove this ad from “Market scan”? The ad itself stays.'));
});

test('G2-01 evidence dialog: a viewer gets no Remove from campaign', async () => {
  const t = await page('#/campaign/2', dialogApi, { role: 'viewer' });
  await t.win.EWViews.handleAction({ dataset: readData }); await settle();
  assert.match(t.modal.innerHTML, /already attached/);
  assert.equal(removeTags(t.modal.innerHTML).length, 0);
});

test('G2-01 a passage is named a passage in the confirm', async () => {
  const t = await page('#/campaign/2', dialogApi);
  await t.win.EWViews.handleAction({ dataset: { ...readData, kind: 'passage' } }); await settle();
  const tag = removeTags(t.modal.innerHTML)[0];
  assert.equal(dataOf(tag).kind, 'passage');
  await t.win.EWViews.handleAction({ dataset: dataOf(tag) }); await settle();
  assert.ok(t.modal.innerHTML.includes('Remove this passage from “Market scan”? The passage itself stays.'));
});

/* CR-SCR-R1-01 / CR-SCR-R1-04: the confirm says what the item is, never
   "ad" for a mention, and names it by kind and time, not by position. */
const MUSE = 'Meta’s Muse: podcast coverage'.replace('’', "'");
const WREF = 'sr1:' + 'd'.repeat(32) + ':w:2701.72:2705.8';
test('CR-SCR-R1-01 Meta\'s Muse: a mention passage on the campaign page is a passage in the confirm', async () => {
  const t = await page('#/campaign/2', researchApi({ refs: [WREF], rev: 3 }, { name: MUSE, show: 'Last Week in AI' }));
  const tag = removeTags(t.main.innerHTML)[0];
  assert.deepEqual(dataOf(tag), { action: 'remove-observation', cid: '2', tid: 't1', ref: WREF, rev: '3', campaign: MUSE,
    label: 'Passage at 45:02 · Last Week in AI', kind: 'passage' });
  await t.win.EWViews.handleAction({ dataset: dataOf(tag) }); await settle();
  const m = unesc(t.modal.innerHTML);
  assert.ok(m.includes('Remove this passage from “Meta\'s Muse: podcast coverage”? The passage itself stays.'), m);
  assert.ok(m.includes('<b>Passage at 45:02 · Last Week in AI</b>'), m);
  assert.doesNotMatch(m, /\bad\b/);
});
test('CR-SCR-R1-01 a confirm opened without a kind never says "ad"', async () => {
  const t = await page('#/campaign/2', researchApi());
  await t.win.EWViews.handleAction({ dataset: { action: 'remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '5', campaign: NAME } }); await settle();
  assert.ok(t.modal.innerHTML.includes('Remove this observation from “BetterHelp ads”? The ad or passage itself stays.'), t.modal.innerHTML);
});
test('CR-SCR-R1-04 evidence dialog: an ad with a known brand is named by it', async () => {
  const t = await page('#/campaign/2', dialogApi);
  await t.win.EWViews.handleAction({ dataset: { ...readData, brandName: 'BetterHelp' } }); await settle();
  const tag = removeTags(t.modal.innerHTML)[0];
  assert.equal(dataOf(tag).label, 'BetterHelp ad · Up First');
  await t.win.EWViews.handleAction({ dataset: dataOf(tag) }); await settle();
  assert.ok(t.modal.innerHTML.includes('Remove this ad from “Market scan”? The ad itself stays.'));
  assert.ok(t.modal.innerHTML.includes('<b>BetterHelp ad · Up First</b>'));
});

test('CR-SCR-R1-04 evidence dialog: an ad is named with its time when the page knows it', async () => {
  const t = await page('#/campaign/2', dialogApi);
  await t.win.EWViews.handleAction({ dataset: { ...readData, brandName: 'BetterHelp', at: '30:00' } }); await settle();
  const tag = removeTags(t.modal.innerHTML)[0];
  assert.equal(dataOf(tag).label, 'BetterHelp ad at 30:00 · Up First');
  await t.win.EWViews.handleAction({ dataset: dataOf(tag) }); await settle();
  assert.ok(t.modal.innerHTML.includes('<b>BetterHelp ad at 30:00 · Up First</b>'));
});

/* CR-R1-02 / CR-SCR-R1-02: after Remove, focus goes to Undo; after Undo,
   to the restored row's Remove; the toast never expires under focus or
   the pointer. */
test('CR-R1-02 after Remove (mouse or keyboard: one submit path), focus goes to the toast\'s Undo', async () => {
  const t = await page('#/campaign/2', researchApi({ refs: ['sr1:a', 'sr1:b'], rev: 5 }));
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  t.focused.length = 0;
  await t.win.EWViews.handleSubmit(confirmForm(t.win, t.modal.innerHTML)); await settle();
  assert.equal(t.toasts.at(-1)[1].action.label, 'Undo');
  assert.equal(t.focused.at(-1), '#toast .toast-action');
});
test('CR-R1-02 Undo gets focus at once, before the page re-reads (a slow re-render never leaves focus on the body)', async () => {
  const t = await page('#/campaign/2', researchApi({ refs: ['sr1:a', 'sr1:b'], rev: 5 }));
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  const api = t.win.EW.api;
  t.win.EW.api = async (p, opts = {}) => { t.focused.push(`API ${opts.method || 'GET'} ${p}`); return api(p, opts); };
  t.focused.length = 0;
  await t.win.EWViews.handleSubmit(confirmForm(t.win, t.modal.innerHTML)); await settle();
  const del = t.focused.indexOf('API DELETE /customer/campaigns/2/targets/t1/evidence');
  const reread = t.focused.indexOf('API GET /customer/campaigns/2/draft', del);
  const undo = t.focused.indexOf('#toast .toast-action', del);
  assert.ok(del >= 0 && reread > del, t.focused.join('\n'));
  assert.ok(undo > del && undo < reread, t.focused.join('\n'));
});
test('CR-R1-02 after Undo, focus goes to the restored observation\'s Remove', async () => {
  const t = await page('#/campaign/2', researchApi({ refs: ['sr1:b'], rev: 6 }));
  t.focused.length = 0;
  await t.win.EWViews.handleAction({ dataset: { action: 'undo-remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '6', campaign: NAME } }); await settle();
  assert.equal(t.toasts.at(-1)[0], 'Added back to “BetterHelp ads”.');
  assert.equal(t.focused.at(-1), '#main [data-action="remove-observation"][data-tid="t1"][data-ref="sr1:a"]');
});
test('CR-R1-02 an action toast waits while it has focus or the pointer, then goes', () => {
  const undo = { tag: 'undo' };
  let hover = false;
  const el = { textContent: '', innerHTML: '', getBoundingClientRect: () => ({ height: 40 }),
    contains: x => x === undo, matches: sel => sel === ':hover' && hover };
  const win = loadAt('#/campaign/2', { toastEl: el });
  const timers = [];
  win.setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; }; win.clearTimeout = () => {};
  win.EW.toast('Removed.', { action: { label: 'Undo', data: { action: 'undo-remove-observation' } } });
  assert.equal(timers.length, 1);
  win.document.activeElement = undo;
  timers.at(-1)[0]();
  assert.match(el.innerHTML, /Undo/, 'focused: still shown');
  assert.equal(timers.length, 2, 'focused: the timer is re-armed');
  win.document.activeElement = null; hover = true;
  timers.at(-1)[0]();
  assert.match(el.innerHTML, /Undo/, 'hovered: still shown');
  assert.equal(timers.length, 3);
  hover = false;
  timers.at(-1)[0]();
  assert.equal(el.textContent, '', 'left alone: it goes');
  // A plain toast is unchanged: it goes on time even under the pointer.
  hover = true;
  win.EW.toast('Saved.');
  timers.at(-1)[0]();
  assert.equal(el.textContent, '');
});

/* CR-SCR-R1-03: the confirm opens on Cancel, never on the red Remove. */
test('CR-SCR-R1-03 the remove confirm puts initial focus on Cancel, not on Remove', async () => {
  const t = await page('#/campaign/2', researchApi());
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /<button class="button" type="button" data-action="close-dialog" autofocus>Cancel<\/button>/);
  assert.match(m, /<button class="button danger armed" type="submit">Remove<\/button>/);
  assert.equal((m.match(/autofocus/g) || []).length, 1);
});

/* CR-R1-03: an archived campaign offers no Remove (the server refuses it). */
test('CR-R1-03 an archived campaign shows its observations without Remove from campaign', async () => {
  const t = await page('#/campaign/2', researchApi(undefined, { status: 'archived' }));
  assert.match(t.main.innerHTML, /Observation 1 · The Vergecast/);
  assert.equal(removeTags(t.main.innerHTML).length, 0);
});

/* CR-R1-04: a stale remove (412) says so plainly; the confirm has no
   entries, so never "restore my entries". The page reloads behind it. */
test('CR-R1-04 a stale remove says nothing was removed in plain words and reloads the page', async () => {
  const state = { refs: ['sr1:a', 'sr1:b'], rev: 5 };
  const base = researchApi(state);
  const t = await page('#/campaign/2', (p, opts = {}) => {
    if (opts.method === 'DELETE') { state.rev = 7; const e = new Error('stale revision; current is 7'); e.status = 412; e.body = { error: { code: 'CONFLICT', current_revision: 7 } }; return e; }
    return base(p, opts);
  });
  await t.win.EWViews.handleAction({ dataset: dataOf(removeTags(t.main.innerHTML)[0]) }); await settle();
  const reads = t.calls.filter(c => c[0] === 'GET' && c[1] === '/customer/campaigns/2/draft').length;
  const form = confirmForm(t.win, t.modal.innerHTML);
  const box = { innerHTML: '', dataset: {}, focus() {}, setAttribute() {} };
  form.querySelector = sel => (sel === '#form-errors' ? box : null);
  await t.win.EWViews.handleSubmit(form); await settle();
  assert.doesNotMatch(box.innerHTML, /restore my entries|Discard my entries|Your entries/);
  assert.equal(t.toasts.at(-1)[0], 'Nothing was removed: “BetterHelp ads” changed since you opened this. Try again on the updated page.');
  assert.ok(!t.toasts.at(-1)[1] || !t.toasts.at(-1)[1].action);
  assert.ok(t.calls.filter(c => c[0] === 'GET' && c[1] === '/customer/campaigns/2/draft').length > reads, 'the page reloads');
  assert.deepEqual(removeTags(t.main.innerHTML).map(x => dataOf(x).rev), ['7', '7']);
});

/* CR-R1-06: an Undo the server refuses for any other reason is said in
   plain words, never the server's own message. */
test('CR-R1-06 an Undo refused for another reason shows a plain message, not the server text', async () => {
  for (const status of [400, 403]) {
    const t = await page('#/campaign/2', (p, opts = {}) => {
      if (opts.method === 'POST') { const e = new Error('evidence is outside the target scope'); e.status = status; return e; }
      return researchApi({ refs: ['sr1:b'], rev: 6 })(p, opts);
    });
    await t.win.EWViews.handleAction({ dataset: { action: 'undo-remove-observation', cid: '2', tid: 't1', ref: 'sr1:a', rev: '6', campaign: NAME } }); await settle();
    assert.equal(t.toasts.at(-1)[0], 'It wasn’t added back to “BetterHelp ads”. Use Add to campaign to add it again.', String(status));
  }
});

/* app.js: a toast may carry one action button, dispatched like any other. */
test('G2-01 a toast with an action shows its button; a plain toast stays text', () => {
  const el = { textContent: '', innerHTML: '', getBoundingClientRect: () => ({ height: 40 }) };
  const win = loadAt('#/campaign/2', { toastEl: el });
  win.setTimeout = () => 0; win.clearTimeout = () => {};
  win.EW.toast('Removed.', { action: { label: 'Undo', data: { action: 'undo-remove-observation', cid: '2', ref: 'sr1:"a"' } } });
  assert.equal(el.innerHTML, 'Removed. <button type="button" class="toast-action" data-action="undo-remove-observation" data-cid="2" data-ref="sr1:&quot;a&quot;">Undo</button>');
  win.EW.clearToast();
  win.EW.toast('Saved.');
  assert.equal(el.textContent, 'Saved.');
});
