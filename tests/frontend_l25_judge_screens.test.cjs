/* L25: the judges' first screens (Sol ui-direction-r1).
   1. Settings invite panel: create is separate from Send invite; every send
      state in words; delivery is never promised.
   2. Admission refusal and failed sign-in pages: fixed copy, no details.
   3. Invite and share links: an entitlement decision (read-only preflight
      or the share read) before any invitation detail or Accept; one
      uniform refusal for every not-entitled case.
   Run: node --test tests/frontend_l25_judge_screens.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadViews(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/' },
    history: { replaceState() {} },
    scrollTo() {},
    // Boot reads one compatible session; nothing else reaches the network.
    fetch: async (url) => {
      if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
      throw new Error('no network in unit test');
    },
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
  const win = sandbox.window;
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await flush(); };
const httpError = (status, message, extra = {}) => Object.assign(new Error(message), { status, ...extra });

/* A hosted page: main element captured, header captured, api stubbed. */
async function hosted(route, api, { principal = 'oidc:https://i.example:carol', email = 'carol@example.test', ws = '', context = null, workspaces = [] } = {}) {
  const win = loadViews(route);
  await settle();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const header = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? header : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  let memberHeader = 0;
  win.EW.header = () => { memberHeader++; };
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.saveNotice = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = principal;
  win.EW.current.identity = { email, subject: '' };
  win.EW.current.activeWorkspace = ws;
  win.EW.current.workspaces = workspaces;
  win.EW.current.workspaceContext = context;
  const calls = [];
  win.EW.api = async (p, o = {}) => { calls.push({ path: p, method: o.method || 'GET', body: o.body }); return api(p, o); };
  win.location.hash = route;
  win.EW.current.route = route;
  const render = async () => { await win.EWViews.render(); await settle(); return main.innerHTML; };
  return { win, main, header, calls, toasts, render, memberHeaders: () => memberHeader };
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&#39;|&rsquo;/g, '’').replace(/\s+/g, ' ').trim();

// ---- 3. Link gate -----------------------------------------------------------

const INVITE = '#/invite?workspace=ws-secret&id=invite:abc123';
const SHARE = '#/shared?workspace=ws-secret&share_id=share:def456&type=evidence&id=ev-9';
const REFUSAL = /This link isn’t available/;

test('signed out: one generic sign-in gate for invite and share links, no reads', async () => {
  const pages = [];
  for (const route of [INVITE, SHARE]) {
    const page = await hosted(route, () => { throw new Error('no reads when signed out'); }, { principal: null });
    const html = await page.render();
    assert.equal(page.calls.filter(c => c.path !== '/session').length, 0, 'no object read before sign-in');
    assert.match(html, /<h1>Open an EarWitness link<\/h1>/);
    assert.match(text(html), /Sign in to continue\. EarWitness will only open this link for the account it was meant for\./);
    assert.match(html, /data-action="signin-start"[^>]*>Sign in</);
    assert.doesNotMatch(html, /ws-secret|abc123|def456|ev-9|Accept invitation|Technical details/);
    pages.push(html);
  }
  assert.equal(pages[0], pages[1], 'the gate does not say which kind of link it is');
});

test('invite: entitlement comes from the read-only preflight, never the accept POST', async () => {
  const page = await hosted(INVITE, (p) => {
    if (p === '/workspaces/ws-secret/invites/invite%3Aabc123/preflight') return { invitation: { state: 'pending', role: 'viewer', workspace_name: 'Acme Research' } };
    throw new Error('unexpected ' + p);
  });
  const html = await page.render();
  assert.deepEqual(page.calls.map(c => `${c.method} ${c.path}`), ['GET /workspaces/ws-secret/invites/invite%3Aabc123/preflight']);
  assert.match(html, /<h1>Workspace invitation<\/h1>/);
  assert.match(text(html), /You’ve been invited to join Acme Research\./);
  assert.match(html, /<h2>Join workspace<\/h2>/);
  assert.match(text(html), /Signed in as carol@example\.test\./);
  assert.match(text(html), /Accepting adds this account to the workspace as a viewer\./);
  assert.match(html, /<button class="button primary" type="submit">Accept invitation<\/button>/);
  assert.match(html, />Not now</);
  assert.doesNotMatch(text(html), /abc123|Technical details/, 'no invite id in what a person sees or hears');
  assert.equal(page.memberHeaders(), 0, 'wordmark-only chrome');
});

test('invite: nothing about the invitation shows while the check is running', async () => {
  let release;
  const gate = new Promise(res => { release = res; });
  const page = await hosted(INVITE, async () => { await gate; return { invitation: { state: 'pending', role: 'editor', workspace_name: 'Acme Research' } }; });
  const done = page.win.EWViews.render();
  await settle();
  const checking = page.main.innerHTML;
  assert.match(checking, /<h1>Checking this link…<\/h1>/);
  assert.match(checking, /role="status"/);
  assert.match(text(checking), /EarWitness is checking whether this link is available to this account\./);
  assert.doesNotMatch(checking, /Acme|ws-secret|Accept invitation|invitation/i);
  release();
  await done; await settle();
  assert.match(text(page.main.innerHTML), /as an editor\./);
});

test('not entitled: real and fabricated invites and shares get one identical refusal', async () => {
  const pages = [];
  for (const route of [INVITE, '#/invite?workspace=made-up&id=invite:ffffffffffff', SHARE,
    '#/shared?workspace=made-up&share_id=share:ffff&type=episode&id=e1']) {
    const page = await hosted(route, () => { throw httpError(404, 'unknown object', { body: { error: { code: 'NOT_FOUND', message: 'unknown object' } } }); });
    const html = await page.render();
    assert.match(html, REFUSAL);
    assert.match(text(html), /It may be invalid, expired, revoked, or meant for another account\./);
    assert.match(text(html), /Check that you opened the full link and signed in with the account it was sent to\./);
    assert.match(html, />Go to EarWitness</);
    assert.match(html, /data-action="signout"[^>]*>Sign out</);
    assert.doesNotMatch(html, /Accept invitation|unknown object|ws-secret|made-up|abc123|role="alert"|Technical details/);
    pages.push(html);
  }
  for (const html of pages) assert.equal(html, pages[0], 'visible and accessible text identical for every case');
});

test('malformed guest links get the same refusal without any read', async () => {
  const refusal = await (await hosted(INVITE, () => { throw httpError(404, 'unknown object'); })).render();
  for (const route of ['#/invite?workspace=ws-secret', '#/shared?workspace=ws-secret&share_id=s1']) {
    const page = await hosted(route, () => { throw new Error('no reads for a malformed link'); });
    const html = await page.render();
    assert.equal(page.calls.length, 0);
    assert.equal(html, refusal);
    assert.doesNotMatch(html, /needs a workspace|incomplete/);
  }
});

test('invite no longer pending or expired: the entitled page without Accept (Sol FIX-1 copy)', async () => {
  const bodies = { not_pending: 'It has already been accepted, withdrawn, or has expired.', expired: 'This invitation has expired.' };
  for (const [state, body] of Object.entries(bodies)) {
    const page = await hosted(INVITE, () => ({ invitation: { state } }));
    const html = await page.render();
    assert.match(html, /<h1>This invitation is no longer available<\/h1>/);
    assert.ok(text(html).includes(body), state);
    assert.doesNotMatch(html, /accepted or withdrawn\./);
    assert.match(html, />Go to EarWitness</);
    assert.doesNotMatch(html, /Accept invitation/);
  }
});

test('accept answered "invite has expired" shows the expired copy; other 400s the general one', async () => {
  for (const [message, body] of [['invite has expired', 'This invitation has expired.'], ['invite is no longer pending', 'It has already been accepted, withdrawn, or has expired.']]) {
    const page = await landing();
    const { form } = acceptForm(page.win);
    page.win.EW.api = async (p) => { if (p.endsWith('/accept')) throw httpError(400, message); throw new Error('no read ' + p); };
    await page.win.EWViews.handleSubmit(form);
    await settle();
    assert.match(page.main.innerHTML, /This invitation is no longer available/);
    assert.ok(text(page.main.innerHTML).includes(body), message);
  }
});

test('guest cards space their action group (Sol FIX-2); the landing keeps its own rhythm', async () => {
  const pages = [await (await hosted(INVITE, () => { throw new Error('signed out'); }, { principal: null })).render(),
    await (await hosted(INVITE, () => { throw httpError(404, 'unknown object'); })).render(),
    await (await hosted(INVITE, () => ({ invitation: { state: 'expired' } }))).render(),
    await (await hosted(INVITE, () => { throw httpError(500, 'boom'); })).render(),
    await (await hosted('#/access-unavailable', () => { throw new Error('no reads'); }, { principal: null })).render(),
    await (await hosted('#/signin-failed', () => { throw new Error('no reads'); }, { principal: null })).render(),
    await (await hosted(SHARE, () => { throw httpError(403, 'share is revoked'); })).render()];
  for (const html of pages) assert.match(html, /class="actions guest-actions"/);
  const land = await (await hosted(INVITE, () => ({ invitation: { state: 'pending', role: 'viewer', workspace_name: 'A' } }))).render();
  assert.doesNotMatch(land, /guest-actions/);
  const css = fs.readFileSync(path.join(APP, 'account.css'), 'utf8');
  assert.match(css, /#main \.guest-actions \{ margin-top: var\(--ew-s3\); \}/);
});

test('a failed check is not the refusal: it says so and offers Try again', async () => {
  for (const err of [httpError(500, 'internal error'), httpError(429, 'slow down'), Object.assign(new Error('The server took too long to respond.'), { timedOut: true })]) {
    const page = await hosted(INVITE, () => { throw err; });
    const html = await page.render();
    assert.match(html, /<h1>This link couldn’t be checked<\/h1>/);
    assert.match(html, /role="alert"/);
    assert.match(text(html), /Something went wrong while checking this link\. Try again\./);
    assert.match(html, /data-action="retry"[^>]*>Try again</);
    assert.doesNotMatch(html, REFUSAL);
    assert.doesNotMatch(html, /Accept invitation|internal error/);
  }
});

async function landing() {
  const page = await hosted(INVITE, () => ({ invitation: { state: 'pending', role: 'viewer', workspace_name: 'Acme Research' } }));
  await page.render();
  return page;
}
function acceptForm(win) {
  const submit = { disabled: false, textContent: 'Accept invitation' };
  const attrs = {};
  win.FormData = class { *entries() { yield ['workspace', 'ws-secret']; yield ['invite_id', 'invite:abc123']; } };
  return { submit, attrs, form: {
    getAttribute: () => 'invite-accept', setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: k => { delete attrs[k]; },
    querySelector: sel => (sel === '[type=submit]' ? submit : null), querySelectorAll: () => [] } };
}

test('accept: busy while submitting; an unconfirmed outcome asks for a reload, never a retry', async () => {
  const page = await landing();
  let seen;
  const { form, submit, attrs } = acceptForm(page.win);
  page.win.EW.api = async (p, o = {}) => {
    page.calls.push({ path: p, method: o.method || 'GET' });
    if (p.endsWith('/accept')) { seen = { disabled: submit.disabled, label: submit.textContent, busy: attrs['aria-busy'] }; throw Object.assign(new Error('Your change could not be confirmed.'), { outcomeUnknown: true }); }
    throw new Error('no other read: ' + p);
  };
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.deepEqual(seen, { disabled: true, label: 'Accepting…', busy: 'true' });
  const html = page.main.innerHTML;
  assert.match(html, /role="alert"[^>]*>[^]*Acceptance could not be confirmed\. Reload this link before trying again\./);
  assert.match(html, /data-action="reload-page"[^>]*>Reload invitation</);
  assert.doesNotMatch(html, /Accept invitation/, 'no second accept from this page');
  assert.equal(page.calls.filter(c => c.path.endsWith('/accept')).length, 1, 'never retried');
});

test('accept refused as unknown: the uniform refusal, not "Accept failed"', async () => {
  const page = await landing();
  const { form } = acceptForm(page.win);
  page.win.EW.api = async (p) => { if (p.endsWith('/accept')) throw httpError(404, 'unknown object'); throw new Error('no read ' + p); };
  await page.win.EWViews.handleSubmit(form);
  await settle();
  assert.match(page.main.innerHTML, REFUSAL);
  assert.doesNotMatch(page.main.innerHTML, /Accept failed|unknown object/);
});

test('accepted: the existing notice and chooser, no ids', async () => {
  const page = await hosted(INVITE, () => { throw new Error('no reads'); }, { workspaces: ['ws-secret'] });
  page.win.EW.current.inviteAccepted = { workspace: 'ws-secret', id: 'invite:abc123' };
  const html = await page.render();
  assert.match(html, /<h1>Invitation accepted<\/h1>/);
  assert.match(text(html), /Invitation accepted\. Choose a workspace below to continue\. Nothing else was read or changed\./);
  assert.match(html, /<label for="ws-pick">Workspace<\/label>/);
  assert.match(html, />Use workspace</);
  assert.doesNotMatch(html, /abc123|Technical details/);
});

test('share: revoked and expired keep their named pages; other failures are not the refusal', async () => {
  const cases = [
    [httpError(403, 'share is revoked'), /<h1>This shared link was revoked<\/h1>/],
    [httpError(403, 'share is expired'), /<h1>This shared link expired<\/h1>/],
    [httpError(500, 'internal error'), /<h1>This link couldn’t be checked<\/h1>/],
  ];
  for (const [err, title] of cases) {
    const page = await hosted(SHARE, () => { throw err; });
    const html = await page.render();
    assert.match(html, title);
    assert.doesNotMatch(html, /share is revoked|share is expired|internal error|share:def456/);
  }
  const revoked = await (await hosted(SHARE, () => { throw httpError(403, 'share is revoked'); })).render();
  assert.match(text(revoked), /This shared record is no longer available\./);
  assert.match(revoked, />Go to EarWitness</);
});

test('share: an entitled share still renders the shared record', async () => {
  const page = await hosted(SHARE, () => ({ title: 'Shared title', text: 'granted excerpt', evidence_id: 'ev-9' }));
  const html = await page.render();
  assert.match(html, /granted excerpt/);
  assert.doesNotMatch(html, REFUSAL);
});

// ---- 2. Admission refusal and failed sign-in ---------------------------------

test('admission refusal: fixed calm copy, wordmark only, no alert and no details', async () => {
  const page = await hosted('#/access-unavailable', () => { throw new Error('no reads'); }, { principal: null });
  const html = await page.render();
  assert.equal(page.calls.length, 0);
  assert.match(html, /<h1>Access unavailable<\/h1>/);
  assert.match(text(html), /EarWitness is a private, invite-only pilot\. This account does not have access\./);
  assert.match(text(html), /If you expected access, contact the person who invited you and tell them which sign-in account you used\./);
  assert.match(html, />Back to sign in</);
  assert.doesNotMatch(html, /role="alert"|notice error|Technical details|admitted|principal|admission/i);
  assert.equal(page.memberHeaders(), 0);
  assert.match(page.header.innerHTML, /EarWitness/);
});

test('failed sign-in: its own page with an alert and Try sign-in again', async () => {
  const page = await hosted('#/signin-failed', () => { throw new Error('no reads'); }, { principal: null });
  const html = await page.render();
  assert.match(html, /<h1>Sign-in didn’t finish<\/h1>/);
  assert.match(html, /role="alert"/);
  assert.match(text(html), /Something went wrong while signing you in\. Try again\./);
  assert.match(html, /data-action="signin-start"[^>]*>Try sign-in again</);
});

test('sign-in from an outcome page returns to the app, not to the outcome page', async () => {
  const page = await hosted('#/access-unavailable', () => ({ authorize_url: 'http://127.0.0.1:9/authorize' }), { principal: null });
  page.win.location.pathname = '/app/';
  page.win.location.href = '';
  await page.win.EWViews.handleAction({ dataset: { action: 'signin-start' }, disabled: false, closest: () => null });
  const login = page.calls.find(c => c.path === '/session/login');
  assert.equal(login.body.return_to, '/app/');
});

// ---- 1. Settings invite panel ------------------------------------------------

const OWNER = 'oidc:https://i.example:owner';
function settingsApi(invites, over = {}) {
  return (p, o = {}) => {
    if (over[p]) return over[p](o);
    if (p === '/session') return { authenticated: true, principal: OWNER, active_workspace: 'ws', workspaces: ['ws'], frontend_contract: 'earwitness/customer-ui/2' };
    if (p === '/customer/preferences') return { preferences: {} };
    if (p === '/workspaces/ws/members') return { members: [{ member: OWNER, role: 'owner', email: 'owner@example.test' }] };
    if (p === '/workspaces/ws/invites') { if (invites instanceof Error) throw invites; return { invites }; }
    if (p === '/shares') return { shares: [] };
    if (p === '/billing/status') return { configured: false };
    throw new Error('unexpected ' + p);
  };
}
async function settings(invites, over) {
  const page = await hosted('#/settings', settingsApi(invites, over), { principal: OWNER, email: 'owner@example.test', ws: 'ws', context: 'ctx-ws', workspaces: ['ws'] });
  await page.render();
  return page;
}
const row = (over) => ({ id: 'invite:1', member: 'judge@example.test', role: 'viewer', created_at: '2026-10-20T10:00:00+00:00', delivery: 'not_queued', unresolved: false, ...over });
const rowHtml = (html, id) => (new RegExp(`<li data-invite-row="${id}"[^]*?</li>`).exec(html) || [''])[0];

test('panel: create form in plain words, separate from sending; no "Queue delivery"', async () => {
  const html = (await settings([])).main.innerHTML;
  assert.match(html, /<h3>Invite someone<\/h3>/);
  assert.match(text(html), /Create the invitation first\. Sending the email is a separate step, and inbox delivery may not be confirmed\./);
  assert.match(html, /<label for="invite-member">Email address<\/label><input id="invite-member" name="member" type="email" autocomplete="email"/);
  assert.match(html, /<label for="invite-role">Role<\/label><select id="invite-role"/);
  assert.match(html, /<form id="invite-create" novalidate>/);
  assert.match(html, /<button class="button primary" type="submit">Create invitation<\/button>/);
  assert.match(html, /<h3>Pending invitations<\/h3>/);
  assert.match(text(html), /No pending invitations\./);
  assert.ok(html.indexOf('Invite someone') < html.indexOf('Pending invitations'));
  assert.doesNotMatch(html, /Queue delivery|Queue again|invite-queue|Delivered|Email sent|Invited\b/);
});

test('panel: every send state in words, with its one action', async () => {
  const html = (await settings([
    row({ id: 'a', delivery: 'not_queued' }),
    row({ id: 'b', delivery: 'queued' }),
    row({ id: 'c', delivery: 'sent' }),
    row({ id: 'd', delivery: 'failed' }),
    row({ id: 'e', delivery: 'uncertain' }),
    row({ id: 'f', delivery: 'uncertain', unresolved: true }),
  ])).main.innerHTML;
  const expect = {
    a: ['Not sent', 'No email has been sent for this invitation.', 'Send invite'],
    b: ['Pending', 'The email is ready to send, but no send result has been recorded.', 'Send invite'],
    c: ['Sent', 'The email provider accepted the send request. Inbox delivery is not guaranteed.', null],
    d: ['Send failed', 'The provider refused this attempt, so the email was not sent.', 'Try sending again'],
    e: ['Send uncertain', 'EarWitness cannot confirm whether the email was accepted. This invitation cannot be sent again.', null],
    f: ['Send unresolved', 'EarWitness could not settle whether the email was accepted. This invitation cannot be sent again. Create a new invitation for the same email if access is still needed.', null],
  };
  for (const [id, [label, why, action]] of Object.entries(expect)) {
    const li = rowHtml(html, id);
    assert.ok(li, 'row ' + id);
    assert.match(li, new RegExp(`class="ew-tag[^"]*"[^>]*>(?:<span[^>]*></span>|<b[^>]*>[^<]*</b>|<svg[^]*?</svg>)?${label}</span>`), id + ' tag');
    assert.ok(text(li).includes(why), id + ' explanation');
    assert.match(li, /judge@example\.test/);
    if (action) assert.match(li, new RegExp(`data-action="invite-send" data-invite="${id}"[^>]*>${action}</button>`));
    else assert.doesNotMatch(li, /data-action="invite-send"/);
    assert.match(li, /data-action="invite-withdraw"/, id + ' can be withdrawn');
  }
  assert.match(rowHtml(html, 'f'), /data-action="invite-new"[^>]*>Create a new invitation</);
});

test('panel: an invitation list that failed to load says so, never "No pending invitations"', async () => {
  const html = (await settings(httpError(500, 'internal error'))).main.innerHTML;
  assert.match(text(html), /Pending invitations could not be loaded\./);
  assert.match(html, /data-action="retry"/);
  assert.doesNotMatch(html, /No pending invitations/);
});

function sendButton(id) {
  const attrs = {}, own = {};
  const tag = { outerHTML: '<span class="ew-tag">Not sent</span>' };
  const why = { id: 'invite-why-0', textContent: 'No email has been sent for this invitation.' };
  const li = { attrs, tag, why, setAttribute: (k, v) => { attrs[k] = v; }, querySelector: sel => (sel === '.ew-tag' ? tag : sel === 'p.section-note[id]' ? why : null) };
  return { dataset: { action: 'invite-send', invite: id }, disabled: false, textContent: 'Send invite', closest: () => li, setAttribute: (k, v) => { own[k] = v; }, own, li };
}

test('send: posts to the send action; a recorded send is announced without promising delivery', async () => {
  let listed = [row({ id: 'a' })];
  const page = await settings(listed, {
    '/workspaces/ws/invites': () => ({ invites: listed }),
    '/workspaces/ws/invites/a/send': () => { listed = [row({ id: 'a', delivery: 'sent' })]; return { status: 'sent', unresolved: false, reason: '' }; },
  });
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  assert.ok(page.calls.some(c => c.method === 'POST' && c.path === '/workspaces/ws/invites/a/send'));
  assert.ok(!page.calls.some(c => c.path.endsWith('/deliver')));
  assert.deepEqual(page.toasts, ['Email send recorded. Inbox delivery is not guaranteed.']);
  assert.match(rowHtml(page.main.innerHTML, 'a'), />Sent</);
});

test('send: the row is busy while sending, and only that row', async () => {
  let release;
  const gate = new Promise(res => { release = res; });
  const page = await settings([row({ id: 'a' }), row({ id: 'b' })], {
    '/workspaces/ws/invites/a/send': async () => { await gate; return { status: 'sent', unresolved: false, reason: '' }; },
  });
  const btn = sendButton('a');
  const done = page.win.EWViews.handleAction(btn);
  await settle();
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, 'Sending…');
  assert.equal(btn.li.attrs['aria-busy'], 'true');
  // The row's tag and sentence say so while the request runs, and the
  // disabled button is described by that sentence.
  assert.match(btn.li.tag.outerHTML, />Sending…<\/span>$/);
  assert.equal(btn.li.why.textContent, 'A send is already in progress. Wait for this attempt to finish.');
  assert.equal(btn.own['aria-describedby'], 'invite-why-0');
  release();
  await done;
});

test('send: timeout, 409, 429 and 5xx read as uncertain and never offer a resend', async () => {
  const failures = [Object.assign(new Error('Your change could not be confirmed.'), { outcomeUnknown: true }),
    httpError(409, 'conflict'), httpError(429, 'slow down', { rateLimited: true }), httpError(502, 'bad gateway')];
  for (const err of failures) {
    const page = await settings([row({ id: 'a' })], { '/workspaces/ws/invites/a/send': () => { throw err; } });
    await page.win.EWViews.handleAction(sendButton('a'));
    await settle();
    const li = rowHtml(page.main.innerHTML, 'a');
    assert.match(li, />Send uncertain</, String(err.status));
    assert.doesNotMatch(li, /data-action="invite-send"/);
    assert.deepEqual(page.toasts, ['EarWitness could not confirm whether the email was accepted. Do not send this invitation again.']);
  }
});

test('send: a definite provider refusal is Send failed, announced as an alert', async () => {
  let listed = [row({ id: 'a' })];
  const page = await settings(listed, {
    '/workspaces/ws/invites': () => ({ invites: listed }),
    '/workspaces/ws/invites/a/send': () => { listed = [row({ id: 'a', delivery: 'failed' })]; return { status: 'failed', unresolved: false, reason: 'provider refused (422)' }; },
  });
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  const html = page.main.innerHTML;
  assert.match(html, /role="alert"[^>]*>The email was not sent\. You can try again\.</);
  assert.match(rowHtml(html, 'a'), />Try sending again</);
  assert.doesNotMatch(html, /provider refused \(422\)/);
});

test('send: a send already in progress shows Sending… for that row only', async () => {
  const page = await settings([row({ id: 'a' }), row({ id: 'b' })], {
    '/workspaces/ws/invites/a/send': () => ({ status: 'uncertain', unresolved: false, reason: 'send in progress' }),
  });
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  const a = rowHtml(page.main.innerHTML, 'a');
  assert.match(a, /aria-busy="true"/);
  assert.match(a, /<button[^>]*disabled[^>]*aria-describedby="[^"]+"[^>]*>Sending…<\/button>/);
  assert.ok(text(a).includes('A send is already in progress. Wait for this attempt to finish.'));
  assert.match(rowHtml(page.main.innerHTML, 'b'), /data-action="invite-send" data-invite="b"/);
});

test('send: sending not set up (428) shows the notice and disables every send', async () => {
  const page = await settings([row({ id: 'a' }), row({ id: 'b', delivery: 'failed' })], {
    '/workspaces/ws/invites/a/send': () => { throw httpError(428, 'invite email sending is not configured'); },
  });
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  const html = page.main.innerHTML;
  assert.match(text(html), /Email sending is not set up for this site\. You can create invitations, but EarWitness cannot email them\./);
  assert.doesNotMatch(html, /data-action="invite-send"(?![^>]*disabled)/);
  for (const id of ['a', 'b']) assert.match(rowHtml(html, id), /<button[^>]*disabled[^>]*>Send unavailable<\/button>/);
  assert.match(html, /<button class="button primary" type="submit">Create invitation<\/button>/, 'creating stays available');
});

test('send: an invitation too old to email says so and offers a new one', async () => {
  const page = await settings([row({ id: 'a' })], {
    '/workspaces/ws/invites/a/send': () => { throw httpError(400, 'invite has expired'); },
  });
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  const li = rowHtml(page.main.innerHTML, 'a');
  assert.match(li, />Send unavailable</);
  assert.ok(text(li).includes('This invitation is too old to email. Create a new invitation if access is still needed.'));
  assert.match(li, /data-action="invite-new"/);
  assert.doesNotMatch(li, /expired/i);
});

function createForm(win, member, role = 'viewer') {
  const submit = { disabled: false, textContent: 'Create invitation', focus() { this.focused = true; } };
  const box = { innerHTML: '', setAttribute() {}, focus() {}, dataset: {} };
  const attrs = {};
  const field = { attrs, setAttribute(k, v) { attrs[k] = v; }, removeAttribute(k) { delete attrs[k]; }, getAttribute: k => attrs[k] ?? null, focus() { this.focused = true; }, parentElement: null };
  const fieldError = { textContent: '', hidden: true };
  win.FormData = class { *entries() { yield ['member', member]; yield ['role', role]; } };
  return { submit, box, field, fieldError, form: {
    dataset: {}, elements: [], getAttribute: () => 'invite-create',
    querySelector: sel => (sel === '[type=submit]' ? submit : sel === '#invite-member' ? field : sel === '#invite-member-error' ? fieldError : sel === '[data-form-errors]' ? box : null),
    querySelectorAll: () => [] } };
}

test('create: an invalid address is refused once, under the field (Sol FIX-3); nothing is sent', async () => {
  const page = await settings([]);
  assert.match(page.main.innerHTML, /<p id="invite-member-error" class="field-error" role="alert" hidden><\/p>/);
  const { form, box, field, fieldError } = createForm(page.win, 'not-an-address');
  await page.win.EWViews.handleSubmit(form);
  assert.equal(fieldError.textContent, 'Enter a valid email address.');
  assert.equal(fieldError.hidden, false);
  assert.equal(box.innerHTML, '', 'no second copy under Create invitation');
  assert.equal(field.attrs['aria-invalid'], 'true');
  assert.equal(field.attrs['aria-describedby'], 'invite-member-error');
  assert.equal(field.focused, true);
  assert.ok(!page.calls.some(c => c.method === 'POST'));
  // A valid retry clears it before anything is sent.
  const ok = createForm(page.win, 'judge@example.test');
  ok.fieldError.textContent = 'Enter a valid email address.'; ok.fieldError.hidden = false;
  page.win.EW.api = async (p, o = {}) => { if (o.method === 'POST') throw httpError(400, 'x'); return settingsApi([])(p, o); };
  await page.win.EWViews.handleSubmit(ok.form);
  assert.equal(ok.fieldError.hidden, true);
  assert.equal(ok.fieldError.textContent, '');
});

test('create: success says no email was sent; failure says nothing changed', async () => {
  const page = await settings([], {
    '/workspaces/ws/invites': (o) => (o.method === 'POST' ? { invite_id: 'invite:9', member: 'judge@example.test', role: 'viewer', superseded: [] } : { invites: [] }),
  });
  const { form } = createForm(page.win, 'judge@example.test');
  await page.win.EWViews.handleSubmit(form);
  await settle();
  const post = page.calls.find(c => c.method === 'POST');
  assert.equal(JSON.stringify(post.body), JSON.stringify({ member: 'judge@example.test', role: 'viewer' }));
  assert.ok(page.toasts.includes('Invitation created for judge@example.test. No email has been sent yet.'));
  assert.ok(!page.calls.some(c => c.path.endsWith('/send')), 'creating never sends');

  const bad = await settings([], { '/workspaces/ws/invites': (o) => { if (o.method === 'POST') throw httpError(400, 'unknown role'); return { invites: [] }; } });
  const failing = createForm(bad.win, 'judge@example.test');
  await bad.win.EWViews.handleSubmit(failing.form);
  assert.match(failing.box.innerHTML, /The invitation could not be created\. Nothing was changed\. Try again\./);
});

test('send: a later server uncertain overrides a local failed; no Try sending again (review LOW-1)', async () => {
  let listed = [row({ id: 'a' })];
  const page = await settings(listed, {
    '/workspaces/ws/invites': () => ({ invites: listed }),
    '/workspaces/ws/invites/a/send': () => { listed = [row({ id: 'a', delivery: 'failed' })]; return { status: 'failed', unresolved: false, reason: 'refused' }; },
  });
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  assert.match(rowHtml(page.main.innerHTML, 'a'), />Try sending again</);
  listed = [row({ id: 'a', delivery: 'uncertain' })];
  await page.render();
  const li = rowHtml(page.main.innerHTML, 'a');
  assert.match(li, />Send uncertain</);
  assert.doesNotMatch(li, /Try sending again|data-action="invite-send"/);
});

test('send: "no longer pending" is announced in one sentence and the list is read again (review LOW-2)', async () => {
  const page = await settings([row({ id: 'a' })], {
    '/workspaces/ws/invites/a/send': () => { throw httpError(400, 'invite is no longer pending'); },
  });
  const before = page.calls.length;
  await page.win.EWViews.handleAction(sendButton('a'));
  await settle();
  assert.deepEqual([...page.toasts], ['This invitation is no longer pending, so no email was sent.']);
  const after = page.calls.slice(before).map(c => `${c.method} ${c.path}`);
  assert.ok(after.indexOf('GET /workspaces/ws/invites') > after.indexOf('POST /workspaces/ws/invites/a/send'), after.join(' | '));
});

test('Settings loading keeps the heading and the Members surface (Sol POLISH-1)', async () => {
  let release;
  const gate = new Promise(res => { release = res; });
  const page = await hosted('#/settings', async (p, o) => { if (p === '/customer/preferences') await gate; return settingsApi([])(p, o); },
    { principal: OWNER, email: 'owner@example.test', ws: 'ws', context: 'ctx-ws', workspaces: ['ws'] });
  const done = page.win.EWViews.render();
  await settle();
  const html = page.main.innerHTML;
  assert.match(html, /<h1>Settings<\/h1>/);
  assert.match(html, /<h2>Members<\/h2>/);
  assert.match(html, /Loading members and invitations…/);
  assert.doesNotMatch(html, /Opening…|No pending invitations/);
  release(); await done;
});

test('a visible announcement reserves its height plus 16 px at the page bottom (Sol POLISH-2)', async () => {
  const win = loadViews('#/overview');
  await settle();
  const toastEl = { textContent: '', getBoundingClientRect: () => ({ height: 56 }) };
  const body = { style: {} };
  win.document.querySelector = sel => (sel === '#toast' ? toastEl : null);
  win.document.body = body;
  win.setTimeout = () => 1; win.clearTimeout = () => {};
  win.EW.toast('Email send recorded. Inbox delivery is not guaranteed.');
  assert.equal(toastEl.textContent, 'Email send recorded. Inbox delivery is not guaranteed.');
  assert.equal(body.style.paddingBottom, '72px');
  win.EW.clearToast();
  assert.equal(body.style.paddingBottom, '');
});

test('withdraw confirmation wraps a long address instead of clipping it (390 browser check)', async () => {
  const page = await settings([row({ id: 'a' })]);
  const modal = { innerHTML: '', open: false, setAttribute() {}, querySelector: () => null, showModal() { this.open = true; } };
  page.win.document.querySelector = sel => (sel === '#modal' ? modal : null);
  page.win.EW.openDialog = () => {};
  const long = 'judge.with.a.deliberately.long.address.for.wrapping@competition-reviewers.example.org';
  await page.win.EWViews.handleAction({ dataset: { action: 'invite-withdraw', invite: 'invite:a', name: long }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, new RegExp(`<p class="od-keep"><strong>${long.replace(/\./g, '\\.')}</strong></p>`));
  assert.match(modal.innerHTML, /Its link can no longer be used to join\. No email is sent\./);
  assert.match(modal.innerHTML, /name="confirm_action" value="invite-withdraw"/);
});

test('share: a field refusal for the named audience is the uniform refusal, not a retry (coordinator decision)', async () => {
  const refusal = await (await hosted(SHARE, () => { throw httpError(404, 'unknown object'); })).render();
  for (const message of ['share does not grant transcript content', 'share grants no content fields']) {
    const page = await hosted(SHARE, () => { throw httpError(403, message); });
    const html = await page.render();
    assert.equal(html, refusal, message);
    assert.doesNotMatch(html, /couldn’t be checked|Try again|does not grant|no content fields/);
  }
});

// ---- Sol ui-acceptance-r2 ----------------------------------------------------

test('shared transcript: segments are joined with exactly one space, words untouched (Sol r2 FIX)', async () => {
  const segments = [{ text: ' and that is the show' }, { text: 'This episode is brought to you by Lumen.  ' }, { text: '' }, { text: '   ' },
    { text: 'Enjoy it,' }, { text: 'friends <b>' }];
  const page = await hosted(SHARE.replace('type=evidence&id=ev-9', 'type=episode&id=e1'), () => ({ segments, truncated: false }));
  const html = await page.render();
  const quote = /<div class="excerpt">([^<]*)<\/div>/.exec(html)[1];
  assert.equal(quote, 'and that is the show This episode is brought to you by Lumen. Enjoy it, friends &lt;b&gt;');
});

test('shared records keep ids out of the reading path (Sol r2 POLISH)', async () => {
  const visible = html => text(html.replace(/<details class="tech-details">[^]*?<\/details>/g, ''));
  // Evidence with no human-readable title: "Shared evidence", never the episode id.
  const ev = await hosted(SHARE.replace('type=episode', 'type=evidence').replace('id=ev-9', 'id=ev-9'), () => ({ evidence: { evidence_id: 'fixture-ad', episode_id: 'e1', excerpt: 'granted excerpt' } }));
  const evHtml = await ev.render();
  assert.match(evHtml, /<h2>Shared evidence<\/h2>/);
  const evText = visible(evHtml);
  assert.doesNotMatch(evText, /share:def456|share share|\be1\b|ws-secret|fixture-ad/);
  assert.match(evHtml, /<details class="tech-details">[^]*fixture-ad/);
  // A granted title still heads the card.
  const titled = await (await hosted(SHARE.replace('type=episode', 'type=evidence'), () => ({ evidence: { title: 'Lumen host read', excerpt: 'x' } }))).render();
  assert.match(titled, /<h2>Lumen host read<\/h2>/);
  // Transcript: no share id anywhere a person reads.
  const tr = await (await hosted(SHARE.replace('type=evidence', 'type=episode'), () => ({ segments: [{ text: 'hello' }] }))).render();
  assert.doesNotMatch(visible(tr), /share:def456|share share|ws-secret/);
  // Subtitle names the workspace by its display name when the session knows it.
  const named = await hosted(SHARE.replace('type=evidence&id=ev-9', 'type=episode&id=e1'), () => ({ segments: [{ text: 'hello' }] }));
  named.win.EW.current.workspaceNames = { 'ws-secret': 'Acme Research' };
  const namedHtml = await named.render();
  assert.match(namedHtml, /<p>Workspace Acme Research<\/p>/);
  assert.doesNotMatch(namedHtml, /share:def456/);
});

// ---- code review round 2 -----------------------------------------------------

test('share: the two transient 403s offer Try again, not the uniform refusal (review r2 LOW)', async () => {
  for (const message of ['share projection or audience changed', 'session unavailable']) {
    const page = await hosted(SHARE, () => { throw httpError(403, message); });
    const html = await page.render();
    assert.match(html, /<h1>This link couldn’t be checked<\/h1>/, message);
    assert.match(html, /data-action="retry"[^>]*>Try again</, message);
    assert.doesNotMatch(html, REFUSAL);
    assert.doesNotMatch(html, new RegExp(message));
  }
  const field = await (await hosted(SHARE, () => { throw httpError(403, 'share does not grant transcript content'); })).render();
  assert.match(field, REFUSAL, 'a field rule stays the uniform refusal');
});

test('create: the same invalid address twice is announced twice (review r2 NIT)', async () => {
  const page = await settings([]);
  const { form, fieldError } = createForm(page.win, 'not-an-address');
  const writes = [];
  let value = '';
  Object.defineProperty(fieldError, 'textContent', { get: () => value, set: v => { value = v; writes.push(v); } });
  await page.win.EWViews.handleSubmit(form); await settle();
  await page.win.EWViews.handleSubmit(form); await new Promise(res => setTimeout(res, 120));
  assert.equal(value, 'Enter a valid email address.');
  const second = writes.lastIndexOf('Enter a valid email address.');
  const first = writes.indexOf('Enter a valid email address.');
  assert.ok(first < second, writes.join(' | '));
  assert.ok(writes.slice(first + 1, second).includes(''), 'the region is cleared between the two messages: ' + writes.join(' | '));
});
