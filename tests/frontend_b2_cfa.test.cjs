/* base(ad) AD-FIX3: the shared "Start a campaign with this ad" run (EWB2.cfa, Ad and Search pages).
   DIALOG-STALE words (Reno via the lead, PACE-4; briefs/DIALOG-STALE-WORDS.md):
   (a) the workspace moved: today's DIALOG_STALE; (b) the session changed (signed out, or signed in as
   someone else): its own line; both at once: (b). Nothing is sent in any case. RSEARCH-R2-03: a
   create refused for its CSRF token (the member signed in again in another tab) is case (b), with no
   Try again. RSEARCH-R2-02: a held run is never sent again (run()'s own check). RA-R2-03: the held
   line takes focus (tabindex -1, as the success line).
   Run: node --test tests/frontend_b2_cfa.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const AD = { ref: 'sr1:4b1a7687', episode: 'ep-77', episodeName: 'Fall Haul', showName: 'Two Judgey Girls', brand: 'Quince', at: '22:03' };
const LOOK = { form: 'a-dialog', title: 'a-h2', meta: 's-meta--faint', field: 'a-dialog__field', label: 'a-fact__value', input: 'a-dialog__input',
  foot: 'a-dialog__foot', cancel: 'a-btn-cancel', cta: 's-cta', error: 'a-dialog__error', link: 'a-camp__new' };
const DIALOG_STALE = 'The workspace changed after this was opened. Nothing was changed. Reload the workspace to continue.';
const SESSION_STALE = 'Your sign-in changed after this was opened. Nothing was changed. Sign in again to continue.';
const D39_TAIL = 'Reload to continue as the account that is signed in now.';
const D39_BTN = '<button class="s-cta" type="button" data-action="reload-workspace">Reload page</button>';
const RELOAD_WS_BTN = '<button class="s-cta" type="button" data-action="reload-workspace">Reload workspace</button>';
const UNCONFIRMED = 'Your change could not be confirmed. Reload the workspace to continue.';

// K6 (RA-R3-02): the server marks a refused CSRF token error.reason "csrf_invalid" (asgi.py _csrf_refused);
// `oldServer` answers as a server before K6 did (the message only).
function server({ csrf = new Set(), unknown = new Set(), after = {}, oldServer = false, message = 'valid CSRF token is required' } = {}) {
  const calls = [];
  const api = async (p, o = {}) => {
    const m = o.method || 'GET';
    calls.push({ m, p });
    if (m === 'GET' && p === '/customer/campaigns') return { campaigns: [] };
    if (m === 'GET' && p === '/customer/campaigns/91') return { campaign: { id: 91, revision: 41 } };
    const step = p === '/customer/campaigns' ? 'create' : /\/targets$/.test(p) ? 'target' : 'attach';
    // The server's answer to a write whose CSRF token belongs to a session that was replaced (asgi.py).
    if (csrf.has(step)) {
      const error = { code: 'FORBIDDEN', message, status: 403, details: {} };
      if (!oldServer) error.reason = 'csrf_invalid';
      throw Object.assign(new Error(message), { status: 403, body: { error } });
    }
    if (after[step]) after[step]();
    if (unknown.has(step)) { unknown.delete(step); throw Object.assign(new Error('no answer'), { outcomeUnknown: true }); }
    if (step === 'create') return { campaign: { id: 91, name: 'Quince ads', revision: 41 } };
    if (step === 'target') return { id: 't-5', campaign_revision: 42 };
    return { campaign_revision: 43 };
  };
  return { api, posts: () => calls.filter(c => c.m === 'POST').map(c => c.p) };
}
// A signed-in member in workspace ws-A; `guard` is the pages' own (today's staleDialogForm + the authority epoch).
function rig() {
  const { win, listeners } = H.boot({ route: '#/settings' });
  H.stopTimers();
  const c = win.EW.current;
  Object.assign(c, { principal: 'oidc:iss:ana', csrf: 'a'.repeat(32), workspaceContext: 'ws-A' });
  const opened = { ws: 'ws-A', authority: c.authorityEpoch };
  const guard = () => (c.workspaceContext !== opened.ws || c.authorityEpoch !== opened.authority ? DIALOG_STALE : null);
  return { win, c, cfa: win.EWB2.cfa, guard, listeners };
}
const MOVES = {
  workspace: c => { c.workspaceContext = 'ws-B'; },
  'signed out': c => { c.authorityEpoch += 1; c.principal = null; c.csrf = ''; },
  'signed in as another person': c => { c.authorityEpoch += 1; c.principal = 'oidc:iss:bo'; c.csrf = 'b'.repeat(32); },
  'signed out and in again (a new session)': c => { c.authorityEpoch += 1; c.csrf = 'c'.repeat(32); },
  both: c => { c.workspaceContext = 'ws-B'; c.authorityEpoch += 1; c.principal = 'oidc:iss:bo'; c.csrf = 'b'.repeat(32); },
};
const WANT = { workspace: DIALOG_STALE, 'signed out': SESSION_STALE, 'signed in as another person': SESSION_STALE,
  'signed out and in again (a new session)': SESSION_STALE, both: SESSION_STALE };

test('DIALOG-STALE (a)/(b): before anything is saved, a moved workspace closes with today\'s line, a changed session with its own; both: (b); nothing is sent', async () => {
  for (const [k, move] of Object.entries(MOVES)) {
    const r = rig();
    const st = r.cfa.state(AD, []);
    move(r.c);
    const srv = server();
    await r.cfa.run(srv.api, AD, st, { guard: r.guard });
    assert.deepEqual(srv.posts(), [], `${k}: nothing sent`);
    assert.equal(st.refused, WANT[k], k);
    assert.ok(!st.held, k);
  }
});
test('DIALOG-STALE (a)/(b) after the campaign is made: nothing more is sent; the held line says what was saved and how to continue', async () => {
  for (const [k, move] of Object.entries(MOVES)) {
    const r = rig();
    const st = r.cfa.state(AD, []);
    const srv = server({ after: { create: () => move(r.c) } });
    await r.cfa.run(srv.api, AD, st, { guard: r.guard });
    assert.deepEqual(srv.posts(), ['/customer/campaigns'], k);
    // UX D39 (WORDS.md, 27c35630b): signed in now as another account or a new session: reload as that account,
    // button "Reload page" (still reload-workspace). Signed out: today's line and button.
    const tail = WANT[k] !== SESSION_STALE ? 'Reload the workspace to continue.'
      : k === 'signed out' ? 'Sign in again to continue.' : D39_TAIL;
    assert.equal(st.held, `We started “Quince ads”, but couldn’t add this episode to it. ${tail}`, k);
    const h = r.cfa.html(AD, st, LOOK);
    assert.ok(!/Try again|type="submit"/.test(h), `${k}: ${h}`);
    // K5 (RD39-01, D39 2026-10-31): signed out after a save, the line stays "…Sign in again to continue." and the
    // button reads "Reload page" too (still reload-workspace); only a moved workspace keeps "Reload workspace".
    assert.ok(h.includes(WANT[k] === SESSION_STALE ? D39_BTN : RELOAD_WS_BTN), `${k}: ${h}`);
    assert.ok(!h.includes(WANT[k] === SESSION_STALE ? RELOAD_WS_BTN : D39_BTN), `${k}: one button: ${h}`);
    assert.ok(!/Sign in again/.test(tail === D39_TAIL ? h : ''), `${k}: D39 never promises a sign-in`);
  }
});
test('RSEARCH-R2-03: "Create and add" refused for its CSRF token (signed in again in another tab: this tab\'s epoch never moved) is case (b): its line, no Try again; never "Nothing was saved. Try again."', async () => {
  const r = rig();
  const st = r.cfa.state(AD, []);
  const srv = server({ csrf: new Set(['create']) });
  await r.cfa.run(srv.api, AD, st, { guard: r.guard });
  assert.deepEqual(srv.posts(), ['/customer/campaigns'], 'one create');
  assert.equal(st.refused, SESSION_STALE);
  assert.ok(!/Try again/.test(r.cfa.failLine(st)));
  // Once the campaign exists: held with what was saved, the (b) tail, no Try again; never resent.
  const r2 = rig();
  const st2 = r2.cfa.state(AD, []);
  const srv2 = server({ csrf: new Set(['target']) });
  await r2.cfa.run(srv2.api, AD, st2, { guard: r2.guard });
  assert.equal(st2.held, `We started “Quince ads”, but couldn’t add this episode to it. ${D39_TAIL}`);
  const h = r2.cfa.html(AD, st2, LOOK);
  assert.ok(!/Try again|type="submit"/.test(h), h);
  assert.ok(h.includes(D39_BTN), h);
  await r2.cfa.run(srv2.api, AD, st2, { guard: r2.guard });
  assert.deepEqual(srv2.posts(), ['/customer/campaigns', '/customer/campaigns/91/targets']);
});
test('RSEARCH-R2-02: run() never sends a held run again, whoever calls it (its own check, before any read or write)', async () => {
  for (const step of ['create', 'target', 'attach']) {
    const r = rig();
    const st = r.cfa.state(AD, []);
    const srv = server({ unknown: new Set([step]) });
    await r.cfa.run(srv.api, AD, st, { guard: r.guard });
    assert.equal(st.held, UNCONFIRMED, step);
    const n = srv.posts().length;
    const back = await r.cfa.run(srv.api, AD, st, { guard: r.guard });
    assert.equal(back, st);
    assert.equal(srv.posts().length, n, `${step}: never resent`);
    assert.equal(st.held, UNCONFIRMED, `${step}: still held`);
    // Without a guard too (the old call shape).
    await r.cfa.run(srv.api, AD, st);
    assert.equal(srv.posts().length, n, `${step}: never resent (no guard)`);
  }
});
test('RA-R2-03: a held line takes focus as the success line does (tabindex -1); an ordinary failure line keeps none', async () => {
  const r = rig();
  const st = r.cfa.state(AD, []);
  await r.cfa.run(server({ unknown: new Set(['create']) }).api, AD, st, { guard: r.guard });
  assert.ok(r.cfa.html(AD, st, LOOK).includes(`<p class="a-dialog__error" role="alert" tabindex="-1">${UNCONFIRMED}`), r.cfa.html(AD, st, LOOK));
  const ok = r.cfa.state(AD, []);
  ok.failed = 'create';
  assert.ok(r.cfa.html(AD, ok, LOOK).includes('<p class="a-dialog__error" role="alert">We couldn’t start'), 'a failure with Try again keeps focus on Try again');
});
test('RSEARCH-R2-03 only for the CSRF refusal: another 403 (the role changed) is not a changed sign-in: never line (b)', async () => {
  const r = rig();
  const st = r.cfa.state(AD, []);
  const api = async (p, o = {}) => {
    if (o.method === 'POST') throw Object.assign(new Error('Your role changed.'), { status: 403, roleChanged: true, body: { error: { code: 'FORBIDDEN', message: 'role changed', reason: 'role_changed' } } });
    return { campaigns: [] };
  };
  await r.cfa.run(api, AD, st, { guard: r.guard });
  assert.notEqual(st.refused, SESSION_STALE);
  assert.ok(!String(r.cfa.failLine(st)).includes('Your sign-in changed'), r.cfa.failLine(st));
});
test('K6 (RA-R3-02): the server\'s code drives line (b), whatever the message says', async () => {
  for (const step of ['create', 'target']) {
    const r = rig();
    const st = r.cfa.state(AD, []);
    // A message with no "csrf" in it: only the code can say the sign-in changed.
    await r.cfa.run(server({ csrf: new Set([step]), message: 'Forbidden.' }).api, AD, st, { guard: r.guard });
    if (step === 'create') assert.equal(st.refused, SESSION_STALE, step);
    else assert.equal(st.held, `We started “Quince ads”, but couldn’t add this episode to it. ${D39_TAIL}`, step);
  }
});
test('K6: an older server (no code, the message only) still reads as line (b): the fallback', async () => {
  const r = rig();
  const st = r.cfa.state(AD, []);
  await r.cfa.run(server({ csrf: new Set(['create']), oldServer: true }).api, AD, st, { guard: r.guard });
  assert.equal(st.refused, SESSION_STALE);
});
test('K6: a role-change 403 is never line (b), even if its message says CSRF; a 403 with another code neither', async () => {
  for (const reason of ['role_changed', 'something_else']) {
    const r = rig();
    const st = r.cfa.state(AD, []);
    const api = async (p, o = {}) => {
      if (o.method === 'POST') throw Object.assign(new Error('CSRF role changed'), { status: 403,
        body: { error: { code: 'FORBIDDEN', message: 'valid CSRF token is required', status: 403, reason } } });
      return { campaigns: [] };
    };
    await r.cfa.run(api, AD, st, { guard: r.guard });
    assert.notEqual(st.refused, SESSION_STALE, reason);
    assert.ok(!String(r.cfa.failLine(st)).includes('Your sign-in changed'), reason);
  }
});

/* K4 (RA-R3-03; UX D40, WORDS.md 066f6edc1). A campaign started from an ad, then signed out for real (in another
   tab): the next write gets a 401 and the tab goes to the sign-in page. When the sign-in page draws, the dialog's
   run becomes a one-time note in this browser (localStorage): the campaign id, its name, the add state and a hash
   of the account; never a token. After the next sign-in, the first page shows it once as a notice, only to the
   account that started the campaign; anyone else's sign-in drops it unseen. "Campaigns" opens the campaign, or
   #/campaigns when that id is gone. Never a toast. */
const K4_KEY = 'earWitnessCampaignNote';
const K4 = {
  added: 'Before you were signed out, we started your campaign “Quince ads” with this ad. It’s in Campaigns.',
  not: 'Before you were signed out, we started your campaign “Quince ads”, but couldn’t add this ad to it. It’s in Campaigns; add the ad again from its page.',
  unknown: 'Before you were signed out, we started your campaign “Quince ads”. Open it in Campaigns to check whether this ad is in it.',
};
const SIGNED_OUT_401 = () => Object.assign(new Error('You are signed out. Nothing was changed.'), { status: 401, signedOut: true,
  body: { error: { code: 'SIGNIN_REQUIRED', message: 'sign in', status: 401 } } });
// The sign-in page draws (views.js accessSection through EWB2.overlay), signed out.
function signinDraws(r) {
  r.c.principal = null; r.c.csrf = '';
  const P = r.win.EWB2.overlay({ accessSection: () => '<p>old</p>' });
  P.accessSection({ page: true });
}
// The first signed-in page draws its header; the notice goes right after it. Returns what was inserted.
function firstPage(r, principal, { route = '#/overview', gone = false, toasts = [] } = {}) {
  r.c.principal = principal; r.c.route = route;
  const inserted = [];
  const note = r.note || { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, remove() { this.removed = true; r.drawn = false; } };
  r.note = note;
  r.win.EW.toast = m => toasts.push(m);
  r.win.EW.api = async p => {
    r.reads = (r.reads || []).concat(p);
    if (gone) throw Object.assign(new Error('unknown object'), { status: 404 });
    return { campaign: { id: 91, revision: 43 } };
  };
  const el = r.win.document.querySelector('#header');
  el.insertAdjacentHTML = (where, html) => { inserted.push({ where, html }); r.drawn = true; };
  if (!r.orig) r.orig = r.win.document.querySelector;
  r.win.document.querySelector = sel => (sel === '#b2-note' || sel === '#b2-note a' ? (r.drawn ? note : null) : r.orig(sel));
  r.win.EWB2.paintHeader('Overview', el);
  return inserted;
}
async function k4Run(step, { unknown = false, done = false, err = null } = {}) {
  const r = rig();
  const st = r.cfa.state(AD, []);
  const api = async (p, o = {}) => {
    const m = o.method || 'GET';
    if (m === 'GET') return p === '/customer/campaigns' ? { campaigns: [] } : { campaign: { id: 91, revision: 41 } };
    const at = p === '/customer/campaigns' ? 'create' : /\/targets$/.test(p) ? 'target' : 'attach';
    if (!done && at === step) {
      if (unknown) throw Object.assign(new Error('no answer'), { outcomeUnknown: true });
      if (err) throw err();
      throw SIGNED_OUT_401();
    }
    if (at === 'create') return { campaign: { id: 91, name: 'Quince ads', revision: 41 } };
    if (at === 'target') return { id: 't-5', campaign_revision: 42 };
    return { campaign_revision: 43 };
  };
  // A dropped-context error (lateWrite) goes back to the page's own handler: the run rethrows it.
  let thrown = null;
  await r.cfa.run(api, AD, st, { guard: r.guard }).catch(e => { if (!err) throw e; thrown = e; });
  return { r, st, thrown };
}
const noteText = ins => ins.map(i => i.html).join('').replace(/<[^>]+>/g, '').replace(/&amp;/g, '&');

test('K4 not added: signed out at the target step ("Quince ads": the attach never ran); the sign-in page keeps the note; the first page after sign-in shows the "not added" line once', async () => {
  const { r } = await k4Run('target');
  assert.equal(r.win.localStorage.getItem(K4_KEY), null, 'nothing kept before the sign-in page draws');
  signinDraws(r);
  const kept = JSON.parse(r.win.localStorage.getItem(K4_KEY));
  assert.deepEqual(Object.keys(kept).sort(), ['add', 'at', 'cid', 'name', 'v', 'who']);
  assert.equal(kept.add, 'not');
  assert.ok(!JSON.stringify(kept).includes('a'.repeat(32)) && !JSON.stringify(kept).includes('oidc:iss:ana'), 'no token, no principal');
  const toasts = [];
  const ins = firstPage(r, 'oidc:iss:ana', { toasts });
  assert.equal(ins.length, 1);
  assert.equal(ins[0].where, 'afterend');
  assert.equal(noteText(ins), K4.not);
  assert.ok(ins[0].html.includes('<a href="#/campaign/91">Campaigns</a>'), ins[0].html);
  assert.deepEqual(toasts, [], 'never a toast');
  assert.equal(r.win.localStorage.getItem(K4_KEY), null, 'once: gone from storage');
  assert.equal(firstPage(r, 'oidc:iss:ana', { route: '#/search' }).length, 0, 'never shown again');
  assert.ok(r.note.removed, 'the next page drops the shown notice');
});
test('K4 not added: signed out at the attach step (a 401 is a refusal): the "not added" line', async () => {
  const { r } = await k4Run('attach');
  signinDraws(r);
  assert.equal(noteText(firstPage(r, 'oidc:iss:ana')), K4.not);
});
test('K4 not known: the attach got no answer, then the tab was signed out: the "not known" line, never "added"', async () => {
  const { r, st } = await k4Run('attach', { unknown: true });
  assert.equal(st.held, UNCONFIRMED);
  signinDraws(r);
  assert.equal(noteText(firstPage(r, 'oidc:iss:ana')), K4.unknown);
});
test('K4 not known: signed out while the attach is still on its way: the "not known" line', async () => {
  const r = rig();
  const st = r.cfa.state(AD, []);
  let release;
  const api = async (p, o = {}) => {
    if ((o.method || 'GET') === 'GET') return { campaigns: [] };
    if (p === '/customer/campaigns') return { campaign: { id: 91, name: 'Quince ads', revision: 41 } };
    if (/\/targets$/.test(p)) return { id: 't-5', campaign_revision: 42 };
    return new Promise(res => { release = res; });
  };
  const running = r.cfa.run(api, AD, st, { guard: r.guard });
  for (let i = 0; i < 5; i++) await H.flush();
  signinDraws(r);
  release({ campaign_revision: 43 });
  await running;
  assert.equal(noteText(firstPage(r, 'oidc:iss:ana')), K4.unknown);
});
// RKD-02: app.js throws lateWrite 'unknown' when the identity or display moved before the answer came (or a 5xx
// after that move): the likeliest path during a sign-out. Its answer is not known, so never "added".
test('K4 not known: the attach answer came after the sign-out moved the identity (lateWrite unknown): the "not known" line, never "added"', async () => {
  const late = () => Object.assign(new Error('Your change in Acme could not be confirmed.'), { status: 0, droppedContext: true, lateWrite: 'unknown', workspace: 'ws-A' });
  const { r, thrown } = await k4Run('attach', { err: late });
  assert.equal(thrown && thrown.lateWrite, 'unknown', 'the page handles the late write');
  signinDraws(r);
  const kept = JSON.parse(r.win.localStorage.getItem(K4_KEY));
  assert.equal(kept.add, 'unknown');
  const text = noteText(firstPage(r, 'oidc:iss:ana'));
  assert.equal(text, K4.unknown);
  assert.notEqual(text, K4.added);
});
test('K4 added: the ad was added, then the tab was signed out with the dialog still open: the "added" line', async () => {
  const { r, st } = await k4Run('none', { done: true });
  assert.ok(st.done);
  signinDraws(r);
  assert.equal(noteText(firstPage(r, 'oidc:iss:ana')), K4.added);
});
test('K4 another account signs in next: the note is dropped unseen, and the first account never sees it later', async () => {
  const { r } = await k4Run('target');
  signinDraws(r);
  assert.equal(firstPage(r, 'oidc:iss:bo').length, 0);
  assert.equal(r.win.localStorage.getItem(K4_KEY), null, 'dropped');
  assert.equal(firstPage(r, 'oidc:iss:ana').length, 0);
});
test('K4 link: "Campaigns" opens the campaign; when that id is gone it falls back to #/campaigns', async () => {
  const { r } = await k4Run('target');
  signinDraws(r);
  firstPage(r, 'oidc:iss:ana', { gone: true });
  for (let i = 0; i < 5; i++) await H.flush();
  assert.deepEqual(r.reads, ['/customer/campaigns/91']);
  assert.equal(r.note.attrs.href, '#/campaigns');
  const k = await k4Run('target');
  signinDraws(k.r);
  firstPage(k.r, 'oidc:iss:ana');
  for (let i = 0; i < 5; i++) await H.flush();
  assert.equal(k.r.note.attrs.href, undefined, 'a campaign that is there keeps its own link');
});
test('K4 no note: no campaign made (signed out at the create step), a run the person closed, a run left for another page, or a sign-in page with no run', async () => {
  const a = await k4Run('create');
  signinDraws(a.r);
  assert.equal(a.r.win.localStorage.getItem(K4_KEY), null, 'nothing was started');
  const b = await k4Run('target');
  // Close on the dialog, through the page-first click listener: the run is forgotten.
  const click = b.r.listeners.find(l => l.type === 'click' && l.capture);
  click.fn({ target: { closest: sel => (sel === '[data-action="close-dialog"]' ? {} : null) }, stopImmediatePropagation() {} });
  signinDraws(b.r);
  assert.equal(b.r.win.localStorage.getItem(K4_KEY), null, 'closed by hand');
  // RKD-01: Escape on the dialog, through the page-first keydown listener: the run is forgotten too.
  const x = await k4Run('target');
  const key = x.r.listeners.find(l => l.type === 'keydown' && l.capture);
  key.fn({ key: 'Escape', target: {}, preventDefault() {}, stopImmediatePropagation() {} });
  signinDraws(x.r);
  assert.equal(x.r.win.localStorage.getItem(K4_KEY), null, 'Escape');
  const e = await k4Run('target');
  firstPage(e.r, 'oidc:iss:ana', { route: '#/campaigns' });
  signinDraws(e.r);
  assert.equal(e.r.win.localStorage.getItem(K4_KEY), null, 'moved on to another page');
  const c = rig();
  signinDraws(c);
  assert.equal(c.win.localStorage.getItem(K4_KEY), null);
});
test('K4 a stored note that is not ours (bad JSON, wrong shape) is dropped, never drawn', async () => {
  for (const raw of ['{', JSON.stringify({ v: 1, who: 'x', cid: 91, name: 'Q', add: 'maybe' }), JSON.stringify({ v: 2 })]) {
    const r = rig();
    r.win.localStorage.setItem(K4_KEY, raw);
    assert.equal(firstPage(r, 'oidc:iss:ana').length, 0, raw);
    assert.equal(r.win.localStorage.getItem(K4_KEY), null, raw);
  }
});

/* RKD-03 (privacy hardening): the note carries when it was written (at, ms) and is dropped unseen once it is older
   than 24 hours. A clock that moved (the note looks written in the future) never shows it either. */
const K4_DAY = 24 * 60 * 60 * 1000;
const vm = require('node:vm');
const setNow = (r, ms) => vm.runInContext(`Date.now = () => ${ms};`, r.win);
async function k4Aged(writtenAt, readAt) {
  const { r } = await k4Run('target');
  setNow(r, writtenAt);
  signinDraws(r);
  assert.equal(JSON.parse(r.win.localStorage.getItem(K4_KEY)).at, writtenAt, 'written at the clock time');
  setNow(r, readAt);
  const ins = firstPage(r, 'oidc:iss:ana');
  assert.equal(r.win.localStorage.getItem(K4_KEY), null, 'taken out either way');
  return ins;
}
test('RKD-03 K4 note age: shown inside 24 hours; dropped unseen past 24 hours', async () => {
  const t0 = 1_793_000_000_000;
  assert.equal(noteText(await k4Aged(t0, t0)), K4.not, 'right away');
  assert.equal(noteText(await k4Aged(t0, t0 + K4_DAY - 1)), K4.not, 'just inside a day');
  assert.equal(noteText(await k4Aged(t0, t0 + K4_DAY)), K4.not, 'exactly a day');
  assert.equal((await k4Aged(t0, t0 + K4_DAY + 1)).length, 0, 'past a day');
  assert.equal((await k4Aged(t0, t0 + 30 * K4_DAY)).length, 0, 'a month later');
});
test('RKD-03 K4 note age: a clock that moved back (the note looks written in the future) never shows it', async () => {
  const t0 = 1_793_000_000_000;
  assert.equal((await k4Aged(t0, t0 - 1)).length, 0, '1 ms back');
  assert.equal((await k4Aged(t0, t0 - K4_DAY)).length, 0, 'a day back');
});
// RKD2-01 (review redesign-known-d2-r1, mutant Z4): the page clock is set to the note's own time plus a minute,
// so a string `at` is fresh by age and only the type check can drop it; the same time as a number is shown.
test('RKD-03 K4 a stored note with no usable time is dropped unseen', async () => {
  const base = { v: 1, who: '00000000', cid: 91, name: 'Quince ads', add: 'not' };
  const t0 = 1_793_000_000_000;
  for (const at of [undefined, null, String(t0), t0]) {
    const r = rig();
    const who = await (async () => { const k = await k4Run('target'); signinDraws(k.r); return JSON.parse(k.r.win.localStorage.getItem(K4_KEY)).who; })();
    const note = { ...base, who };
    if (at !== undefined) note.at = at;
    r.win.localStorage.setItem(K4_KEY, JSON.stringify(note));
    setNow(r, t0 + 60_000);
    const ins = firstPage(r, 'oidc:iss:ana');
    if (at === t0) assert.equal(noteText(ins), K4.not, 'control: the same time as a number is shown');
    else assert.equal(ins.length, 0, JSON.stringify(at));
    assert.equal(r.win.localStorage.getItem(K4_KEY), null, JSON.stringify(at));
  }
});
