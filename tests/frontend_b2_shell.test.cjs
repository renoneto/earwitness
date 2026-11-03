/* K8 base shell (ws-app/briefs/REDESIGN-KNOWN-ISSUES.md; reviews/redesign-signin-r1.md):
   RSI-R1-04: the sign-in gate marks the page (shell header hidden, dark band
     body) only when the session read found no one; a read that finds the
     owner draws today's chooser with today's header and footer.
   RSI-R1-03: crossing 600 px with the signed-out gate on screen draws the
     gate again in the other markup and keeps what was typed.
   Run: node --test tests/frontend_b2_shell.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const signinSpec = read => ({ on: true, render: async (opts, old) => { await read(); return opts && opts.page ? '<form id="signin-password"></form>' : old(opts); } });
const field = value => Object.assign(H.element(), { value, focused: 0, focus() { this.focused++; } });

/* -- RSI-R1-04 ---------------------------------------------------------------- */
test('RSI-R1-04: a gate draw whose session read finds the owner keeps today\'s header and body', async () => {
  let who = null;
  const b = H.boot({ modules: false, pages: { signin: signinSpec(async () => { b.win.EW.current.principal = who; }) } });
  const head = H.element('header', 'topbar');
  const V = { accessSection: () => 'old' };

  // Signed in in another tab: the read finds the owner (the chooser).
  b.win.EW.current.principal = null; who = 'oidc:owner';
  await b.win.EWB2.overlay(V).accessSection({ page: true });
  b.win.EWB2.paintGuestHeader(head);
  assert.equal(head.hidden, false, 'the shell header shows over the chooser');
  assert.equal(b.body.classList.contains('ew-b2'), false, 'no dark band body');
  assert.equal(b.body.classList.contains('ew-b2--signin'), false);

  // Still signed out: the page draws its own header.
  b.win.EW.current.principal = null; who = null;
  await b.win.EWB2.overlay(V).accessSection({ page: true });
  b.win.EWB2.paintGuestHeader(head);
  assert.equal(head.hidden, true);
  assert.equal(b.body.classList.contains('ew-b2--signin'), true);
});

test('RSI-R1-04: a gate read a newer draw replaced never marks that newer draw', async () => {
  let release;
  const b = H.boot({ modules: false, pages: { signin: signinSpec(() => new Promise(res => { release = res; })) } });
  b.win.EW.current.principal = null;
  const stale = b.win.EWB2.overlay({ accessSection: () => 'old' }).accessSection({ page: true });
  b.win.EWB2.overlay({ accessSection: () => 'old' });   // the next draw starts (an invite page, say)
  release();
  await stale;
  const head = H.element('header', 'topbar');
  b.win.EWB2.paintGuestHeader(head);
  assert.equal(head.hidden, false);
  assert.equal(b.body.classList.contains('ew-b2--signin'), false);
});

/* -- RSI-R1-03 ---------------------------------------------------------------- */
async function gateOnScreen(route) {
  const b = H.boot({ route, width: 1440, modules: false, pages: { signin: signinSpec(async () => {}), overview: { on: true, render: async () => '' } } });
  b.win.EW.current.route = route;
  b.win.EW.current.principal = null;
  await b.win.EWB2.overlay({ accessSection: () => 'old' }).accessSection({ page: true });
  const mq = b.media.find(m => /max-width:\s*599px/.test(m.media) && m.listeners.length);
  return { b, mq };
}
function typed(b, email, password, focusedId) {
  const doc = b.win.document, q = doc.querySelector;
  const f = { '#signin-email': field(email), '#signin-password-input': field(password) };
  doc.querySelector = sel => f[sel] || q(sel);
  doc.activeElement = focusedId ? Object.assign(f['#' + focusedId], { id: focusedId }) : null;
  return f;
}

for (const route of ['#/overview', '#/settings']) {
  test(`RSI-R1-03: crossing 600 px on the signed-out gate (${route}) draws it again and keeps the typed email and password`, async () => {
    const { b, mq } = await gateOnScreen(route);
    typed(b, 'ana@example.com', 'hunter2', 'signin-password-input');
    let renders = 0, drawn;
    b.win.EW.render = async () => {
      renders++;
      await b.win.EWB2.overlay({ accessSection: () => 'old' }).accessSection({ page: true });
      drawn = typed(b, '', '');   // the new markup's empty fields
    };
    mq.listeners[0]({ matches: true });
    for (let i = 0; i < 5; i++) await H.flush();
    assert.equal(renders, 1, 'the gate is drawn again in the other markup');
    assert.equal(drawn['#signin-email'].value, 'ana@example.com');
    assert.equal(drawn['#signin-password-input'].value, 'hunter2');
    assert.equal(drawn['#signin-password-input'].focused, 1, 'focus returns to the field it was in');
    assert.equal(drawn['#signin-email'].focused, 0);
  });
}

// RKB-01 (review redesign-known-b-r1, mutant M11): a field the new gate draw already filled (autofill,
// an invite's email) keeps its own value; only an empty field takes back what was typed.
test('RSI-R1-03 (RKB-01): crossing 600 px never writes over a field the new draw already filled', async () => {
  const { b, mq } = await gateOnScreen('#/overview');
  typed(b, 'ana@example.com', 'hunter2', 'signin-email');
  let drawn;
  b.win.EW.render = async () => {
    await b.win.EWB2.overlay({ accessSection: () => 'old' }).accessSection({ page: true });
    drawn = typed(b, 'invite@example.com', '');   // the new markup comes with its email filled
  };
  mq.listeners[0]({ matches: true });
  for (let i = 0; i < 5; i++) await H.flush();
  assert.equal(drawn['#signin-email'].value, 'invite@example.com', 'the filled field keeps its value');
  assert.equal(drawn['#signin-password-input'].value, 'hunter2', 'the empty field takes back what was typed');
  assert.equal(drawn['#signin-email'].focused, 1, 'focus still returns');
});

test('RSI-R1-03: a crossing whose draw finds a signed-in person puts nothing back', async () => {
  const { b, mq } = await gateOnScreen('#/overview');
  typed(b, 'ana@example.com', 'hunter2');
  let drawn;
  b.win.EW.render = async () => {
    b.win.EW.current.principal = 'oidc:owner';
    await b.win.EWB2.overlay({ accessSection: () => 'old' }).accessSection({ page: true });
    drawn = typed(b, '', '');
  };
  mq.listeners[0]({ matches: true });
  for (let i = 0; i < 5; i++) await H.flush();
  assert.equal(drawn['#signin-email'].value, '');
  assert.equal(drawn['#signin-password-input'].value, '');
});

test('RSI-R1-03: away from the gate, a crossing on today\'s page draws nothing (unchanged)', async () => {
  const b = H.boot({ route: '#/alerts', width: 1440, modules: false, pages: { overview: { on: true, render: async () => '' } } });
  b.win.EW.current.route = '#/alerts';
  const mq = b.media.find(m => /max-width:\s*599px/.test(m.media) && m.listeners.length);
  let renders = 0;
  b.win.EW.render = () => { renders++; };
  mq.listeners[0]({ matches: true });
  assert.equal(renders, 0);
});

/* -- n8 (landing screen-r2): today's dialogs on a redesigned page --------------- */
const B2CSS = require('node:fs').readFileSync(require('node:path').join(H.APP, 'app-b2.css'), 'utf8');
const additions = B2CSS.slice(B2CSS.indexOf('/* ===== Base additions'));
test('n8: on a redesigned page the dialog\'s controls are 44 px on the phone (spec: every control 44 × 44 at 390)', () => {
  // styles.css sizes .button, input and select by --ew-target (40 px); the phone raises it inside #modal only.
  assert.match(additions, /@media \(max-width: 599px\) \{\s*body\.ew-b2 #modal \{\s*--ew-target: 44px;\s*\}\s*\}/);
});
test('n8: on a redesigned page a dialog select is --ew-target tall in WebKit too (min-height alone leaves it 23 px)', () => {
  assert.match(additions, /body\.ew-b2 #modal select:not\(\[multiple\]\):not\(\[size\]\) \{\s*height: var\(--ew-target\);\s*\}/);
});
test('n8: today\'s pages keep today\'s dialog (every rule is under body.ew-b2)', () => {
  for (const m of additions.matchAll(/^[^\n{}]*#modal[^\n{]*\{/gm)) assert.match(m[0], /body\.ew-b2 #modal/);
});
