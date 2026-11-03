/* Check now poll route guard (A2 follow-up): the 1.5 s run poll a checking
   alert page starts stops when the member leaves the page, and a run answer
   that lands after they left never redraws. Timers are faked, so each tick
   runs on demand.
   Run: node --test tests/frontend_alerts_poll_guard.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'alert-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
const AID = 'ma_111111111111111111111111';
const RUN = 'mr_ffffffffffffffffffffffff';   // alert-checking's current_run

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
const flush = () => new Promise(res => setTimeout(res, 0));
const canonicalSessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};

// The checking alert page on screen, with faked timers and a run endpoint
// the test answers by hand.
async function checkingPage() {
  const win = loadAt('#/overview', { fetch: canonicalSessionFetch() });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flush();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : dummy);
  win.EW.header = () => {};
  win.EW.current.meaningAlertsAvailable = true;
  const timers = [];
  win.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  const runGets = [];
  const answers = [];
  win.EW.api = async p => {
    if (p === '/customer/meaning-alerts/' + AID) return fixture('alert-checking');
    if (p === '/customer/meaning-alerts/runs/' + RUN) {
      runGets.push(win.EW.current.route);
      return new Promise(res => answers.push(res));
    }
    if (p === '/customer/alerts') return { alerts: [] };
    throw new Error('unexpected read ' + p);
  };
  win.location.hash = '#/alert/' + AID;
  await win.EWViews.render();
  assert.ok(main.innerHTML.includes('meaning-definition'), 'the alert page is on screen');
  const redraws = [];
  win.EW.render = () => redraws.push(win.EW.current.route);
  // Run the poll ticks due now (1.5 s timers only), letting each settle.
  const tick = async () => {
    const due = timers.splice(0).filter(t => t.ms === 1500);
    for (const t of due) t.fn();
    await flush();
    return due.length;
  };
  const leave = async () => {
    win.location.hash = '#/alerts';
    await win.EWViews.render();
    assert.ok(!main.innerHTML.includes('meaning-definition'), 'the Alerts list replaced the alert page');
  };
  return { win, main, timers, runGets, answers, redraws, tick, leave };
}
const pending = timers => timers.filter(t => t.ms === 1500).length;

test('control: on the alert page, the poll reads the run every 1.5 s and redraws once it is done', async () => {
  const p = await checkingPage();
  assert.equal(pending(p.timers), 1, 'the checking page starts one poll');
  assert.equal(await p.tick(), 1);
  assert.equal(p.runGets.length, 1);
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check' });
  await flush();
  assert.equal(pending(p.timers), 1, 'a working run polls again');
  await p.tick();
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check', state: 'done' });
  await flush();
  assert.deepEqual(p.redraws, ['#/alert/' + AID]);
  assert.equal(pending(p.timers), 0);
});

test('leaving the alert page stops the poll: no run read after leaving and no further tick', async () => {
  const p = await checkingPage();
  assert.equal(pending(p.timers), 1);
  await p.leave();
  await p.tick();
  assert.deepEqual(p.runGets, [], 'no run read once the member left the page');
  assert.equal(pending(p.timers), 0, 'no further poll tick after leaving');
  assert.deepEqual(p.redraws, []);
});

test('a run answer that lands after the member left does not redraw the page they are on', async () => {
  const p = await checkingPage();
  await p.tick();
  assert.equal(p.runGets.length, 1, 'one run read in flight');
  await p.leave();
  const shown = p.main.innerHTML;
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check', state: 'done' });
  await flush();
  assert.deepEqual(p.redraws, [], 'the Alerts list is not redrawn by the alert page poll');
  assert.equal(p.main.innerHTML, shown);
  assert.equal(pending(p.timers), 0, 'and the poll ends');
});

test('a working answer that lands after the member left does not keep polling', async () => {
  const p = await checkingPage();
  await p.tick();
  await p.leave();
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check' });
  await flush();
  assert.equal(pending(p.timers), 0, 'no tick scheduled for a page no longer on screen');
  await p.tick();
  assert.equal(p.runGets.length, 1);
  assert.deepEqual(p.redraws, []);
});

// Leave and come back within one tick: the old poll ends at the boundary,
// so the page shown again needs a poll of its own.
async function back(p) {
  p.win.location.hash = '#/alert/' + AID;
  await p.win.EWViews.render();
  assert.ok(p.main.innerHTML.includes('meaning-definition'), 'the alert page is on screen again');
}
async function reachesDone(p) {
  assert.equal(pending(p.timers) > 0, true, 'a poll is running for the page on screen');
  await p.tick();
  assert.equal(p.answers.length, 1, 'one run read for the page on screen');
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check', state: 'done' });
  await flush();
  assert.deepEqual(p.redraws, ['#/alert/' + AID], 'the page redraws with the run finished');
  assert.equal(pending(p.timers), 0, 'and the poll ends');
}

test('leaving and coming back before the tick: the page shown again keeps polling to the end', async () => {
  const p = await checkingPage();
  await p.leave();
  await back(p);
  await reachesDone(p);
});

test('leaving and coming back while a run read is in flight: the page shown again keeps polling to the end', async () => {
  const p = await checkingPage();
  await p.tick();
  assert.equal(p.runGets.length, 1, 'one run read in flight');
  await p.leave();
  await back(p);
  // The old poll's answer lands after the return; that poll ends.
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check' });
  await flush();
  assert.deepEqual(p.redraws, [], 'the ended poll does not redraw');
  await reachesDone(p);
});

test('after the old poll ends, a redraw of the page shown again does not start a second poll', async () => {
  const p = await checkingPage();
  await p.tick();
  await p.leave();
  await back(p);
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check' });
  await flush();
  assert.equal(pending(p.timers), 1, 'one poll for the page shown again');
  await p.win.EWViews.render();
  assert.equal(pending(p.timers), 1, 'the same page redrawn keeps that one poll');
});

test('a failed run read ends the poll and redraws; the redrawn page, still checking, polls again', async () => {
  const p = await checkingPage();
  await p.tick();
  p.answers.shift()(Promise.reject(new Error('run read failed')));
  await flush();
  assert.deepEqual(p.redraws, ['#/alert/' + AID], 'the failed read redraws the page');
  assert.equal(pending(p.timers), 0);
  p.redraws.length = 0;
  await p.win.EWViews.render();
  await reachesDone(p);
});

// Code review r1 (poll fixes): a Check now answer that lands after the member
// left never redraws the page they are on and never starts a poll there.
test('a Check now answer that lands after leaving starts no poll on the page shown', async () => {
  const p = await checkingPage();
  const posts = [];
  const api = p.win.EW.api;
  p.win.EW.api = (path, opts) => (opts && opts.method === 'POST'
    ? new Promise(res => posts.push(res)) : api(path, opts));
  const button = { dataset: { action: 'meaning-check', id: AID }, disabled: false, closest: () => null, getAttribute: () => null };
  const clicked = p.win.EWViews.handleAction(button);
  await flush();
  assert.equal(posts.length, 1, 'the check request is in flight');
  await p.leave();
  posts.shift()({ run_id: RUN });
  await clicked;
  await flush();
  assert.deepEqual(p.redraws, [], 'the Alerts list is not redrawn by the late answer');
  await p.tick();
  assert.deepEqual(p.runGets, [], 'no run read once the member left the page');
  assert.equal(pending(p.timers), 0, 'no poll tick for a page no longer on screen');
});

// Code review r2 (poll fixes). The Check now click with its POST held open.
function clickCheck(p, { idle = false } = {}) {
  const posts = [];
  const api = p.win.EW.api;
  p.win.EW.api = (path, opts) => {
    if (opts && opts.method === 'POST') return new Promise((res, rej) => posts.push({ res, rej }));
    if (idle && path === '/customer/meaning-alerts/' + AID) return Promise.resolve({ ...fixture('alert-checking'), current_run: null });
    return api(path, opts);
  };
  const button = { dataset: { action: 'meaning-check', id: AID }, disabled: false, closest: () => null, getAttribute: () => null };
  return { posts, button, clicked: p.win.EWViews.handleAction(button) };
}
const refusal = (code, status) => { const e = new Error('refused'); e.status = status; e.body = { error: { code } }; return e; };

test('control: Check now while staying on the page redraws it once and keeps one poll', async () => {
  const p = await checkingPage();
  const c = clickCheck(p);
  await flush();
  c.posts.shift().res({ run_id: RUN });
  await c.clicked;
  await flush();
  assert.deepEqual(p.redraws, ['#/alert/' + AID]);
  assert.equal(pending(p.timers), 1, 'one poll for the page on screen');
});

for (const [code, status] of [['ALERT_BUSY', 429], ['ALERT_NOT_ACTIVE', 409]]) {
  test(`a Check now ${code} refusal that lands after leaving does not redraw the page shown`, async () => {
    const p = await checkingPage();
    const c = clickCheck(p);
    await flush();
    await p.leave();
    c.posts.shift().rej(refusal(code, status));
    await c.clicked;
    await flush();
    assert.deepEqual(p.redraws, []);
  });
}

for (const [label, err] of [['a failed check', Object.assign(new Error('server error'), { status: 500 })], ['the daily limit', refusal('ALERT_LIMIT_DAY', 429)]]) {
  test(`a Check now failure (${label}) that lands after leaving writes nothing on the page shown`, async () => {
    const p = await checkingPage();
    const c = clickCheck(p);
    await flush();
    await p.leave();
    const slot = { innerHTML: '' };
    const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
    p.win.document.querySelector = sel => (sel === '#main' ? p.main : sel === '#meaning-progress' ? slot : dummy);
    c.posts.shift().rej(err);
    await c.clicked;
    await flush();
    assert.equal(slot.innerHTML, '', 'no line about the left alert on the page shown');
  });
}

test('leave and come back before the run exists: the Check now answer still polls the page shown', async () => {
  const p = await checkingPage();
  // Settle the checking page's own poll so only the click can start one.
  await p.tick();
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check', state: 'done' });
  await flush();
  p.redraws.length = 0;
  assert.equal(pending(p.timers), 0);
  const c = clickCheck(p, { idle: true });
  await flush();
  await p.leave();
  await back(p);
  assert.equal(pending(p.timers), 0, 'the return read saw no run yet');
  c.posts.shift().res({ run_id: RUN });
  await c.clicked;
  await flush();
  assert.deepEqual(p.redraws, ['#/alert/' + AID], 'the page shown again redraws');
  p.redraws.length = 0;
  await reachesDone(p);
});

// Code review r3 (poll fixes): the member lands on another meaning alert's
// page, or back on this one, before the Check now answer or error lands.
const BID = 'ma_222222222222222222222222';
async function goTo(p, route, { slot } = {}) {
  const api = p.win.EW.api;
  p.win.EW.api = (path, opts) => (path === '/customer/meaning-alerts/' + BID
    ? Promise.resolve({ ...fixture('alert-checking'), alert_id: BID, current_run: null }) : api(path, opts));
  p.win.location.hash = route;
  await p.win.EWViews.render();
  assert.ok(p.main.innerHTML.includes('meaning-definition'), 'an alert page is on screen');
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  if (slot) p.win.document.querySelector = sel => (sel === '#main' ? p.main : sel === '#meaning-progress' ? slot : dummy);
}

test('a Check now answer that lands on another alert page neither redraws it nor polls', async () => {
  const p = await checkingPage();
  const c = clickCheck(p);
  await flush();
  await goTo(p, '#/alert/' + BID);
  c.posts.shift().res({ run_id: RUN });
  await c.clicked;
  await flush();
  assert.deepEqual(p.redraws, []);
  await p.tick();
  assert.deepEqual(p.runGets, [], 'no run read for the alert left behind');
});

test('a Check now failure that lands on another alert page writes nothing there', async () => {
  const p = await checkingPage();
  const c = clickCheck(p);
  await flush();
  const slot = { innerHTML: '' };
  await goTo(p, '#/alert/' + BID, { slot });
  c.posts.shift().rej(Object.assign(new Error('server error'), { status: 500 }));
  await c.clicked;
  await flush();
  assert.equal(slot.innerHTML, '');
  assert.deepEqual(p.redraws, []);
});

test('a Check now failure that lands on a return visit redraws from the server, never the stale line', async () => {
  const p = await checkingPage();
  const c = clickCheck(p);
  await flush();
  await p.leave();
  const slot = { innerHTML: '' };
  await goTo(p, '#/alert/' + AID, { slot });
  c.posts.shift().rej(Object.assign(new Error('server error'), { status: 500 }));
  await c.clicked;
  await flush();
  assert.equal(slot.innerHTML, '', 'the page read is newer than the error');
  assert.deepEqual(p.redraws, ['#/alert/' + AID]);
});

test('a return visit with a query string still gets the Check now answer', async () => {
  const p = await checkingPage();
  await p.tick();
  p.answers.shift()({ ...fixture('run-working'), run_id: RUN, kind: 'check', state: 'done' });
  await flush();
  p.redraws.length = 0;
  const c = clickCheck(p, { idle: true });
  await flush();
  await p.leave();
  await goTo(p, '#/alert/' + AID + '?from=alerts');
  assert.equal(pending(p.timers), 0, 'the return read saw no run yet');
  c.posts.shift().res({ run_id: RUN });
  await c.clicked;
  await flush();
  assert.equal(p.redraws.length, 1, 'the page shown again redraws');
  assert.equal(pending(p.timers), 1, 'and polls the run');
});

// Code review r4 (poll fixes): refusal lines describe the member or the
// service, never this alert's state, so they show on a return visit too;
// on the page never left, failures write their line and do not redraw.
function withSlot(p) {
  const slot = { innerHTML: '' };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  p.win.document.querySelector = sel => (sel === '#main' ? p.main : sel === '#meaning-progress' ? slot : dummy);
  return slot;
}

for (const code of ['ALERT_LIMIT_DAY', 'ALERT_PAUSED', 'META_HALT']) {
  test(`a ${code} refusal that lands on a return visit still shows its line`, async () => {
    const p = await checkingPage();
    const c = clickCheck(p);
    await flush();
    await p.leave();
    const slot = { innerHTML: '' };
    await goTo(p, '#/alert/' + AID, { slot });
    c.posts.shift().rej(refusal(code, 429));
    await c.clicked;
    await flush();
    assert.match(slot.innerHTML, /class="meaning-refusal"/);
    assert.deepEqual(p.redraws, [], 'a redraw would wipe the line');
  });
}

for (const [label, err, line] of [
  ['the daily limit', () => refusal('ALERT_LIMIT_DAY', 429), /class="meaning-refusal"/],
  ['a failed check', () => Object.assign(new Error('server error'), { status: 500 }), /The last check didn&#39;t finish/],
]) {
  test(`a Check now failure (${label}) on the page never left writes its line and does not redraw`, async () => {
    const p = await checkingPage();
    const c = clickCheck(p);
    await flush();
    const slot = withSlot(p);
    c.posts.shift().rej(err());
    await c.clicked;
    await flush();
    assert.match(slot.innerHTML, line);
    assert.deepEqual(p.redraws, []);
    assert.equal(c.button.disabled, false, 'Check now can be pressed again');
  });
}
