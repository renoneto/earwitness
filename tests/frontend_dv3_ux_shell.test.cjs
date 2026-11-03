/* Design v3 UX review, shell fixes: one disabled-button recipe, phone menu
   scrim and "Close menu", unknown-route page, matched-word weight.
   Run: node --test tests/frontend_dv3_ux_shell.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');

// Rules for a selector (exact selector-list member match), media blocks included.
function rulesFor(selector) {
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(css))) {
    const sels = m[1].split(',').map(s => s.trim().replace(/^[\s\S]*\*\/\s*/, ''));
    if (sels.includes(selector)) out.push(m[2]);
  }
  return out;
}
const decl = (selector, prop) => rulesFor(selector).map(b => (b.match(new RegExp(`(?:^|;|\\s)${prop}\\s*:\\s*([^;]+)`)) || [])[1]).filter(Boolean);

function fakeEl(extra = {}) {
  const attrs = {};
  const classes = new Set();
  return Object.assign({
    innerHTML: '', textContent: '', hidden: false, focused: false, dataset: {},
    setAttribute(k, v) { attrs[k] = String(v); }, getAttribute(k) { return attrs[k] == null ? null : attrs[k]; },
    focus() { this.focused = true; },
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, force) { const on = force === undefined ? !classes.has(c) : !!force; on ? classes.add(c) : classes.delete(c); return on; } },
  }, extra);
}

function load(route, fetch) {
  const store = {};
  const nodes = { '#header': fakeEl(), '#dataset': fakeEl(), '#main': fakeEl() };
  const sandbox = {
    console, URLSearchParams, AbortController, Intl,
    location: { hash: route },
    history: { replaceState() {} }, scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: sel => nodes[sel] || null, querySelectorAll: () => [], addEventListener() {},
      createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const f of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, f), 'utf8'), sandbox, { filename: f });
  sandbox.addEventListener = () => {};
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  return { win: sandbox, nodes };
}

test('disabled buttons keep one outline and one muted fill', () => {
  const base = decl('.button:disabled', 'border-color').join(' ');
  assert.match(base, /--ew-control/, 'outline uses the visible control border, not the faint --ew-border');
  assert.match(decl('.button:disabled', 'background').join(' '), /--ew-sunken|--ew-page/, 'single muted fill');
  // Quiet and link buttons must not strip the outline when disabled.
  const quiet = [...decl('.button.quiet:disabled', 'border-color'), ...decl('.button.link:disabled', 'border-color')];
  assert.deepEqual(quiet.filter(v => /transparent/.test(v)), [], 'no disabled rule makes the border transparent');
  assert.deepEqual([...decl('.button.quiet:disabled', 'background'), ...decl('.button.link:disabled', 'background')].filter(v => /transparent/.test(v)), [], 'no disabled rule drops the fill');
});

test('disabled recipe leaves focus styles alone', () => {
  assert.equal(rulesFor('.button:disabled:focus-visible').length, 0);
  assert.ok(/:focus-visible/.test(css), 'focus-visible rules still exist');
});

test('mark carries weight as well as the highlight', () => {
  const body = rulesFor('mark').join(';');
  assert.match(body, /background:\s*var\(--ew-mark\)/);
  assert.match(body, /font-weight:\s*(600|700|bold)/);
  assert.doesNotMatch(body, /underline/, 'an underline would read as a link');
});

test('menu scrim css: hidden on desktop, dimmed with the scrim token on phone', () => {
  assert.ok(rulesFor('.ew-menu-scrim').some(b => /display:\s*none/.test(b)), 'base rule hides it');
  const phone = rulesFor('.ew-menu-scrim:not([hidden])').join(';');
  assert.match(phone, /display:\s*block/);
  const scrim = rulesFor('.ew-menu-scrim').join(';');
  assert.match(scrim, /background:\s*var\(--ew-scrim\)/);
  assert.match(scrim, /position:\s*fixed/);
  assert.doesNotMatch(css, /style=/);
  assert.ok(decl('.primary-nav', 'z-index').length, 'the open menu sits above the scrim');
});

test('header markup carries a hidden scrim and a Menu button', () => {
  const { win, nodes } = load('#/search');
  win.EW.header('Search');
  const h = nodes['#header'].innerHTML;
  assert.match(h, /<div class="ew-menu-scrim" data-action="menu-close" aria-hidden="true" hidden><\/div>/);
  assert.match(h, /data-action="menu"[^>]*aria-expanded="false"/);
  assert.match(h, /Menu<\/button>/);
  assert.doesNotMatch(h, /Close menu/);
});

function menuDom(win, nodes) {
  nodes['.primary-nav'] = fakeEl();
  nodes['.mobile-menu'] = fakeEl();
  nodes['.ew-menu-scrim'] = fakeEl({ hidden: true });
  return nodes;
}

test('opening the menu shows the scrim and reads "Close menu"; menu-close reverses it and returns focus', async () => {
  const { win, nodes } = load('#/search');
  menuDom(win, nodes);
  const btn = nodes['.mobile-menu'];
  btn.dataset = { action: 'menu' };
  await win.EWViews.handleAction(btn);
  assert.ok(nodes['.primary-nav'].classList.contains('menu-open'));
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(nodes['.ew-menu-scrim'].hidden, false);
  assert.match(btn.innerHTML, /Close menu/);
  assert.doesNotMatch(btn.innerHTML, />Menu</);

  btn.focused = false;
  await win.EWViews.handleAction({ dataset: { action: 'menu-close' } });
  assert.ok(!nodes['.primary-nav'].classList.contains('menu-open'));
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(nodes['.ew-menu-scrim'].hidden, true);
  assert.match(btn.innerHTML, /Menu$/);
  assert.doesNotMatch(btn.innerHTML, /Close menu/);
  assert.equal(btn.focused, true, 'focus returns to the button');
});

test('the Menu button toggles closed again with the same label swap', async () => {
  const { win, nodes } = load('#/search');
  menuDom(win, nodes);
  const btn = nodes['.mobile-menu'];
  btn.dataset = { action: 'menu' };
  await win.EWViews.handleAction(btn);
  await win.EWViews.handleAction(btn);
  assert.equal(nodes['.ew-menu-scrim'].hidden, true);
  assert.match(btn.innerHTML, /Menu$/);
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
});

test('closeMenu (Escape path) hides the scrim and restores the label', () => {
  const { win, nodes } = load('#/search');
  menuDom(win, nodes);
  const btn = nodes['.mobile-menu'];
  nodes['.primary-nav'].classList.add('menu-open');
  nodes['.ew-menu-scrim'].hidden = false;
  btn.innerHTML = 'Close menu';
  win.EW.closeMenu(true);
  assert.equal(nodes['.ew-menu-scrim'].hidden, true);
  assert.match(btn.innerHTML, /Menu$/);
  assert.equal(btn.focused, true);
});

const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 0)); };

test('unknown route: Page not found, no section marked current, no record wording', async () => {
  const { win, nodes } = load('#/nope', async (url) => {
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    throw new Error('unexpected ' + url);
  });
  await settle();
  const sections = [];
  const realHeader = win.EW.header;
  win.EW.header = s => { sections.push(s); realHeader(s); };
  win.location.hash = '#/nope'; win.EW.current.route = '#/nope';
  await win.EWViews.render(); await settle();
  assert.deepEqual(sections, ['Page not found']);
  const h = nodes['#header'].innerHTML;
  assert.doesNotMatch(h, /aria-current/, 'no nav item is current');
  assert.match(h, /<span class="mobile-section">Page not found<\/span>/);
  const main = nodes['#main'].innerHTML;
  assert.match(main, /Page not found/);
  assert.match(main, /There is no page at this address\./);
  assert.doesNotMatch(main, /record/i);
  assert.match(main, /href="#\/overview"/);
});

test('record not-found copy is unchanged', () => {
  const { win } = load('#/ad/x');
  assert.match(win.EW.notFound(), /This record is not available/);
});

// UX-10: one verb for the evidence action on every customer page.
test('UX-10 customer pages say "Open evidence", never "View evidence" or "Inspect"', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  for (const f of ['views.js', 'ui.js', 'app.js', 'saved.js']) {
    const src = fs.readFileSync(path.join(__dirname, '..', 'app', f), 'utf8');
    assert.doesNotMatch(src, /View evidence|\bInspect\b/, f);
  }
});
