/* UAT fix round, final: kind-aware not-found (V-16), F-1 guard (no customer
   surface renders "Brand not named" / "Not named" / "Unnamed spot"), and
   the V-31 leftover (no "Corpus" word on a customer page).
   Run: node --test tests/frontend_uat_final.test.cjs */
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
    location: { hash: route, href: 'http://127.0.0.1:9179/app/' + route },
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
async function page(route, api) {
  const win = loadViews(route);
  await flushBoot();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, opts = {}) => { const out = api(p, opts); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const gone = () => Object.assign(new Error('unknown'), { status: 404 });
const EW = route => loadAt(route).EW;

/* -- V-16 -- */
const KINDS = [
  ['ad record', '#/search', 'Back to Search'],
  ['alert', '#/alerts', 'Back to Alerts'],
  ['campaign', '#/campaigns', 'Back to Campaigns'],
  ['brand', '#/brands', 'Back to Brands'],
  ['show', '#/shows', 'Back to Shows'],
  ['episode', '#/shows', 'Back to Shows'],
];

test('V-16 notFound({ kind }) names the record type, gives a way back to its list, and keeps Go to Overview', () => {
  const E = EW('#/overview');
  for (const [kind, list, label] of KINDS) {
    const html = E.notFound({ kind });
    assert.match(html, new RegExp(`<h1>We can(?:'|&#39;)t find this ${kind}</h1>`), kind); // R1-15: h2 -> h1
    assert.match(html, /<p>It may have been removed, or the link may be wrong\.<\/p>/, kind);
    assert.ok(html.includes(`<a class="button primary" href="${list}">${label}</a>`), `${kind}: primary link ${html}`);
    assert.ok(html.includes('<a class="button" href="#/overview">Go to Overview</a>'), `${kind}: secondary Overview ${html}`);
    assert.ok(html.indexOf(label) < html.indexOf('Go to Overview'), 'list link comes first');
  }
});

test('V-16 notFound() with no kind keeps today\'s generic message, in local and hosted mode', () => {
  const E = EW('#/overview');
  assert.match(E.notFound(), /This record is not available/);
  assert.match(E.notFound(), /different local dataset revision/);
  assert.match(E.notFound(), /<a class="button primary" href="#\/overview">Go to Overview<\/a>/);
  E.current.hosted = true;
  assert.match(E.notFound(), /This record isn(?:'|&#39;)t available in this workspace/);
  assert.doesNotMatch(E.notFound(), /We can(?:'|&#39;)t find/);
});

test('V-16 a record left behind in another workspace keeps its cause, even with a kind', () => {
  const E = EW('#/overview');
  E.current.hosted = true; E.current.activeWorkspace = 'ws-b'; E.current.recordLeftWs = 'ws-a';
  const html = E.notFound({ kind: 'episode' });
  assert.match(html, /This record belongs to another workspace\. This tab now shows ws-b\./);
  assert.doesNotMatch(html, /We can(?:'|&#39;)t find/);
});

test('V-16 a 404 on each record page shows its own kind and list link', async () => {
  const cases = [
    ['#/evidence/nope', 'ad record', '#/search'],
    ['#/alert/nope', 'alert', '#/alerts'],
    ['#/campaign/99', 'campaign', '#/campaigns'],
    ['#/brand/nope', 'brand', '#/brands'],
    ['#/show/nope', 'show', '#/shows'],
    ['#/episode/nope', 'episode', '#/shows'],
  ];
  for (const [route, kind, list] of cases) {
    const { html } = await page(route, () => gone());
    assert.match(html, new RegExp(`We can(?:'|&#39;)t find this ${kind}`), route);
    assert.ok(html.includes(`href="${list}"`), `${route}: list link`);
    assert.match(html, /Go to Overview/, route);
    assert.doesNotMatch(html, /different local dataset revision/, route);
  }
});

test('V-16 a campaign whose draft read 404s also says campaign', async () => {
  const { html } = await page('#/campaign/2', p => (/\/draft$/.test(p) ? gone()
    : { campaign: { id: 2, name: 'Old', status: 'active' }, reports: [], targets: [], placements: [] }));
  assert.match(html, /We can(?:'|&#39;)t find this campaign/);
});

test('V-16 every record-page caller in views.js passes a kind', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  for (const kind of ['ad record', 'alert', 'campaign', 'brand', 'show', 'episode']) {
    assert.ok(src.includes(`E.notFound({ kind: '${kind}' })`), `a caller passes ${kind}`);
  }
});

/* -- F-1 guard: scan every customer source file, comments removed -- */
function stripComments(src, ext) {
  if (ext === '.html') return src.replace(/<!--[\s\S]*?-->/g, '');
  if (ext === '.css') return src.replace(/\/\*[\s\S]*?\*\//g, '');
  // Line and block comments, leaving string and template literals alone.
  let out = '', i = 0, quote = null;
  while (i < src.length) {
    const c = src[i], n = src[i + 1];
    if (quote) {
      out += c;
      if (c === '\\') { out += src[i + 1] || ''; i += 2; continue; }
      if (c === quote) quote = null;
      i++; continue;
    }
    if (c === '/' && n === '*') { const e = src.indexOf('*/', i + 2); i = e < 0 ? src.length : e + 2; continue; }
    if (c === '/' && n === '/' && src[i - 1] !== ':') { const e = src.indexOf('\n', i); i = e < 0 ? src.length : e; continue; }
    if (c === '"' || c === "'" || c === '`') quote = c;
    out += c; i++;
  }
  return out;
}
function customerFiles() {
  const out = [];
  const walk = (dir, skipOperator) => {
    for (const name of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, name.name);
      if (name.isDirectory()) { if (!(skipOperator && name.name === 'operator') && name.name !== 'fonts') walk(p, skipOperator); }
      else if (/\.(js|html|css)$/.test(name.name)) out.push(p);
    }
  };
  walk(APP, true);
  return out;
}

test('F-1 guard: no customer surface renders "Brand not named", "Not named" or "Unnamed spot"', () => {
  const files = customerFiles();
  assert.ok(files.some(f => f.endsWith('views.js')) && files.some(f => f.endsWith('index.html')), 'the scan covers the customer files');
  assert.ok(!files.some(f => f.includes(`${path.sep}operator${path.sep}`)), 'operator pages are out of scope');
  const banned = /Brand not named|Not named|Unnamed spot/i;
  const found = [];
  for (const f of files) {
    const src = stripComments(fs.readFileSync(f, 'utf8'), path.extname(f));
    src.split('\n').forEach((l, i) => { if (banned.test(l)) found.push(`${path.relative(APP, f)}:${i + 1}`); });
  }
  assert.deepEqual(found, []);
});

test('F-1 guard: the comment stripper keeps strings and drops comments', () => {
  assert.equal(stripComments('a("Not named"); // Not named\n/* Not named */ b', '.js').includes('// Not'), false);
  assert.match(stripComments('a("Not named"); // x', '.js'), /"Not named"/);
  assert.doesNotMatch(stripComments('// Not named\nb', '.js'), /Not named/);
  assert.doesNotMatch(stripComments('<!-- Not named --><p>x</p>', '.html'), /Not named/);
});

/* -- V-31 leftover -- */
test('V-31 the Overview link says "Collection definitions", and no customer page says "corpus"', async () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /link\('#\/settings', 'Collection definitions'\)/);
  assert.doesNotMatch(src, /'Corpus definitions'/);
  const words = [];
  for (const f of customerFiles().filter(f => !f.endsWith('.css'))) {
    const src = stripComments(fs.readFileSync(f, 'utf8'), path.extname(f));
    // class names and CSS selectors may keep the word; text may not
    const text = src.replace(/class="[^"]*"/g, '').replace(/['"`.#][\w-]*corpus[\w-]*/gi, '');
    text.split('\n').forEach((l, i) => { if (/corpus/i.test(l)) words.push(`${path.relative(APP, f)}:${i + 1}`); });
  }
  assert.deepEqual(words, []);
});
