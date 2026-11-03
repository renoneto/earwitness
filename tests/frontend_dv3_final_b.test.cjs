/* Design v3 final fixes (worker B): alerts hub tags and tabs, campaign
   state tags, target episode titles, alert delivery line, Check again
   feedback, settings/footer/operator markup. Loaders copied from
   frontend_dv3_g4.test.cjs.
   Run: node --test tests/frontend_dv3_final_b.test.cjs */
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
const rowOf = (html, name) => new RegExp(`<li class="ew-work-alert"[^>]*>(?:(?!</li>).)*${name}(?:(?!</li>).)*</li>`).exec(html)[0];
const css = f => fs.readFileSync(path.join(APP, f), 'utf8');

const base = { revision: 1, definition_revision: 1, query: { mode: 'ads', q: 'x' }, last_successful_at: '2026-10-14T09:00:00Z', baseline_complete: true };

test('FB1 hub: the state tag is the plain state; "N new" is its own dark tag on every row with unread events', async () => {
  const alerts = [
    { ...base, id: 1, name: 'Fresh', lifecycle: 'active_local', unread_event_count: 4, unread_ad_count: 2 },
    { ...base, id: 5, name: 'Held', lifecycle: 'paused', unread_event_count: 5, unread_ad_count: 3 },
    { ...base, id: 3, name: 'Broken', lifecycle: 'active_local', last_attempt_result: 'error', unread_event_count: 2, unread_ad_count: 1 },
    { ...base, id: 2, name: 'Calm', lifecycle: 'active_local', unread_event_count: 0, unread_ad_count: 0 },
  ];
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  const fresh = rowOf(html, 'Fresh');
  assert.match(fresh, /data-tag="alert-state"[^>]*>(?:<span[^>]*>[^<]*<\/span>)?Active</, 'state tag reads plain "Active"');
  assert.doesNotMatch(fresh, /Active · /);
  // UX review R2-05c: a paused alert's unread events are a grey outline "N unread".
  assert.match(rowOf(html, 'Held'), /<span class="ew-tag ew-tag-outline" data-tag="outline">3 unread</);
  for (const [name, n] of [['Fresh', 2], ['Broken', 1]]) {
    const row = rowOf(html, name);
    assert.match(row, new RegExp(`class="ew-tag ew-tag-count ew-work-new"[^>]*>${n} new<`), name);
    assert.doesNotMatch(row, /ew-work-new[^>]*ew-tag-confirmed|ew-tag-confirmed[^>]*ew-work-new/, name + ': not the green tag');
  }
  assert.doesNotMatch(rowOf(html, 'Calm'), /ew-work-new/);
  assert.match(css('styles.css'), /\.ew-tag-count\s*\{[^}]*var\(--ew-inverse\)/, 'dark tag style');
});

test('FB2 hub filters are underline tabs; campaign placement filters stay pills', () => {
  const work = css('work.css');
  assert.match(work, /\.ew-work-tabs \.ew-work-filter\[aria-pressed=true\]\s*\{[^}]*box-shadow: inset 0 -3px 0/);
  assert.match(work, /\.ew-work-tabs \.ew-work-filter\s*\{[^}]*border: 0/);
  assert.match(work, /\.ew-work-filter\s*\{[^}]*border-radius: var\(--ew-r-round\)/, 'pill base kept');
});

test('FB3 campaign directory: Draft/Tracking are outline tags, Archived neutral, never green', async () => {
  const campaigns = [
    { id: 1, name: 'A', state: 'Draft', mode: 'supplied_requirements', placement_count: 1 },
    { id: 2, name: 'B', state: 'Tracking', mode: 'supplied_requirements', placement_count: 1 },
    { id: 3, name: 'C', state: 'Archived', mode: 'supplied_requirements', placement_count: 1 },
  ];
  const { html } = await renderRoute('#/campaigns', p => { if (p === '/customer/campaigns') return { campaigns }; throw new Error(p); });
  assert.match(html, /ew-tag-outline"[^>]*>Draft</);
  assert.match(html, /ew-tag-outline"[^>]*>Tracking</);
  assert.match(html, /ew-tag-neutral"[^>]*>Archived</);
  assert.doesNotMatch(html, /ew-tag-confirmed|tag green/);
});

function targetApi(target, draft, calls, episode = new Error('episode down')) {
  return p => {
    calls.push(p);
    if (p === '/customer/campaigns/1') return { campaign: { id: 1, name: 'Camp' } };
    if (p === '/customer/campaigns/1/targets') return { targets: [target] };
    if (p === '/customer/episodes/ep_9f3c') { if (episode instanceof Error) throw episode; return { episode }; }
    if (p === '/customer/campaigns/1/draft') { if (draft instanceof Error) throw draft; return { draft }; }
    throw new Error('unexpected request ' + p);
  };
}
const ep = { id: 'tg1', scope: 'episode', episode_id: 'ep_9f3c', revision: 1 };

test('FB4 episode target without a show id: title from the campaign draft, id only under Technical details', async () => {
  const calls = [];
  const { html } = await renderRoute('#/campaign/1/target/tg1', targetApi(ep, { episode_titles: { ep_9f3c: 'The Long Run, ep 12' } }, calls));
  assert.ok(calls.includes('/customer/campaigns/1/draft'));
  assert.match(html, /<h1>Episode: The Long Run, ep 12<\/h1>/);
  assert.match(html, /<dd>One episode · The Long Run, ep 12<\/dd>/);
  const main = html.replace(/<details[\s\S]*?<\/details>/g, '');
  assert.doesNotMatch(main.replace(/href="[^"]*"/g, ''), /ep_9f3c/, 'raw id only in Technical details and links');
  assert.match(html, /<details[^>]*>(?:(?!<\/details>)[\s\S])*ep_9f3c/);
});

test('FB4 episode title unavailable: plain words, never the raw id in the main text', async () => {
  for (const [draft, episode, words] of [[{ episode_titles: {} }, { ep_title: '' }, 'Episode title not recorded'],
    [new Error('draft down'), undefined, 'Episode title unavailable'], [{ episode_titles: {} }, undefined, 'Episode title unavailable']]) {
    const { html } = await renderRoute('#/campaign/1/target/tg1', targetApi(ep, draft, [], episode));
    assert.match(html, new RegExp('<dd>One episode · ' + words + '</dd>'));
    const main = html.replace(/<details[\s\S]*?<\/details>/g, '').replace(/href="[^"]*"/g, '');
    assert.doesNotMatch(main, /ep_9f3c/);
  }
});

test('FB4 research target episode the draft does not title: the episode\'s own title', async () => {
  const { html } = await renderRoute('#/campaign/1/target/tg1', targetApi(ep, { episode_titles: {} }, [], { ep_title: 'Dan McClellan' }));
  assert.match(html, /<h1>Episode: Dan McClellan<\/h1>/);
  assert.match(html, /<dd>One episode · Dan McClellan<\/dd>/);
});

test('FB5 alert page delivery line uses the hub wording and links to Connections', async () => {
  const alert = { ...base, id: 7, name: 'One', lifecycle: 'active_local', unread_event_count: 0, events: [], delivery_json: '{}' };
  const { html } = await renderRoute('#/alert/7', p => {
    if (p === '/customer/alerts/7') return { alert };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  assert.match(html, /class="section-note ew-work-delivery">Alerts show here in the app\. EarWitness sends no email\. See <a [^>]*href="#\/connections"[^>]*>Connections<\/a>/);
  assert.doesNotMatch(html, /unconnected · cadence inactive/);
});

test('FB11 Check again disables the button and says "Checking…" while it runs, then restores it', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  let release; const gate = new Promise(r => { release = r; });
  win.EW.render = () => {};
  win.EW.toast = () => {};
  const seen = [];
  win.EW.api = async () => { seen.push([btn.disabled, btn.textContent]); await gate; return { emitted: 0 }; };
  const btn = { dataset: { action: 'check-alert', id: 'a3' }, disabled: false, textContent: 'Check again', closest: () => null };
  const first = win.EWViews.handleAction(btn);
  await flushBoot();
  assert.deepEqual(seen, [[true, 'Checking…']]);
  const second = win.EWViews.handleAction(btn);
  await flushBoot();
  assert.equal(seen.length, 1, 'a second click while running sends nothing');
  release();
  await Promise.all([first, second]);
  assert.equal(btn.disabled, false);
  assert.equal(btn.textContent, 'Check again');
});

test('FB9 settings: one primary button; both groups share the card padding', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async p => { if (p === '/customer/preferences') return { preferences: { revision: 1 } }; return {}; };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.equal((main.innerHTML.match(/class="button primary"/g) || []).length, 1);
  assert.match(main.innerHTML, /<button class="button" type="submit">Save workspace name<\/button>/);
  assert.match(css('account.css'), /\.settings-grid > section\.pad\s*\{[^}]*padding: var\(--ew-s6\)/);
});

test('FB8 dataset strip: the separator is hidden when the strip is narrow', () => {
  assert.match(css('styles.css'), /@media \(max-width: 599px\)[^{]*\{[^}]*\.dataset-strip > span:nth-child\(2\)\s*\{\s*display: none/);
});

test('FB10 operator review: the advance checkbox and Clear judgment sit in their own actions row', () => {
  const views = fs.readFileSync(path.join(APP, 'operator', 'views.js'), 'utf8');
  assert.match(views, /<div class="primary-actions"><label><input id="save-next"[^`]*<\/label><button type="button" id="clear-judgment">Clear judgment only<\/button><\/div>/);
});

test('FB6 operator dark tokens are screen-only; print has no copy of the light tokens', () => {
  const op = css('operator/styles.css');
  assert.match(op, /@media screen \{\s*html\.ew-operator \{\s*color-scheme: dark;/);
  const printBlock = op.slice(op.indexOf('@media print'));
  assert.doesNotMatch(printBlock, /--ew-text:|--ew-page:/);
  assert.match(op, /@media screen \{ \.ar-report q \{ color: #cfd6e0; \} \}/, 'the light-grey report note is screen-only');
  assert.doesNotMatch(op.replace(/@media screen \{ \.ar-report q[^}]*\} \}/, ''), /#cfd6e0/);
});
