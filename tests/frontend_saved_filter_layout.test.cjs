/* SAVED-FILTER-FIX2 (screen review r2): the scope chip row on Search, laid
   out by every app stylesheet in a real headless Chromium (the judge-flow
   browser), with the IBM Plex fonts the app serves.
   - SF-SCR-R2-01: a chip label wraps inside its pill; the × stays inside
     the pill, and the pill never runs into Cards/List, at 1440 and 390.
   - SF-SCR-R2-02: below 600 px the chip row runs the full results width
     under the count and Cards/List row; at 1440 it stays in the count
     column, directly under the count line (SF-SCR-R1-01).
   The page is the real Search markup (app.js, ui.js, saved.js, views.js
   in a vm with fake reads; loader from frontend_saved_filter.test.cjs).
   Skips when that browser is not installed (a named verification gap).
   Run: node --test tests/frontend_saved_filter_layout.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { spawn } = require('node:child_process');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const CHROME = process.env.EW_CHROME || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const SKIP = fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}`;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };
const flush = () => new Promise(res => setTimeout(res, 0));

// A 60-character show name, the 26-character backup showcase show and
// the reviewer's 92-character title (cs-fa783e50a476 in the QA data).
const LONG = 'Chilling Tales for Dark Nights — Horror Fiction, Scary Tales';
const MEDIUM = 'What Now? with Trevor Noah';
const LONGEST = 'Chilling Tales for Dark Nights — A Horror Fiction Anthology and Scary Stories Series Podcast';
const SHOWS = [{ id: 'cs-long', display_title: LONG }, { id: 'cs-medium', display_title: MEDIUM }, { id: 'cs-longest', display_title: LONGEST }];
const BRANDS = [{ key: 'quince', display: 'Quince' }, { key: 'mint mobile', display: 'Mint Mobile' }];
const ITEM = { ref: 'sr1:a', evidence_id: 'e1', record_kind: 'ad', status: 'finding', brand: { key: 'quince', display: 'Quince' },
  show_title: 'Show', ep_title: 'Ep', pub_date: '2026-10-08', span: [1711, 1800], text: 'support our show' };

const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};

/* The Search page's #main markup for a route at a width, as views.js
   renders it (the filter rail and inspector at 1200 px and up). */
async function searchHtml(route, width) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout, Intl,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    fetch: sessionFetch,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  const win = sandbox;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flush();
  win.EW.current.hosted = false;
  win.matchMedia = q => ({ matches: width >= 1200 && /\(min-width: 1200px\)/.test(q), addEventListener() {}, removeEventListener() {} });
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? { open: false } : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.go = () => {};
  win.EW.api = async (p, o = {}) => {
    if (p === '/customer/brands') return { brands: BRANDS };
    if (p === '/customer/shows') return { shows: SHOWS };
    if (p.startsWith('/customer/filter-values?')) return { items: [] };
    if (p === '/customer/research-query') {
      return { query: o.body.query, selector: null, revision: 'rev-1', coverage: { episodes_in_scope: 10 }, summary: { week: [] },
        result: { items: [ITEM], total: 1, per_page: 10, total_relation: 'exact', dated_total: 1, undated_total: 0 } };
    }
    if (p.startsWith('/customer/preferences')) return { preferences: { revision: 1 }, revision: 1 };
    throw new Error('unexpected ' + p);
  };
  await win.EWViews.render();
  for (let i = 0; i < 25; i++) await flush();
  stopTimers();
  return main.innerHTML;
}

// Every screen stylesheet app/index.html links, in order; the Plex
// fonts inlined so text has the app's widths.
function appCss() {
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  return [...html.matchAll(/<link rel="stylesheet" href="\/app\/([^"]+)"( media="print")?>/g)]
    .filter(m => !m[2]).map(m => {
      const file = path.join(APP, m[1]);
      return fs.readFileSync(file, 'utf8').replace(/url\(([\w.-]+\.woff2)\)/g, (_, f) =>
        `url(data:font/woff2;base64,${fs.readFileSync(path.join(path.dirname(file), f)).toString('base64')})`);
    }).join('\n');
}
const PAGE = (css, main) => `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head>
<body><header id="header" class="topbar"><a class="wordmark" href="#/overview">EarWitness</a></header>
<main id="main" class="page" tabindex="-1">${main}</main></body></html>`;

async function browser() {
  const root = process.env.AGENT_SCRATCH || os.tmpdir();
  const dir = fs.mkdtempSync(path.join(root, 'ew-chips-chrome-'));
  const proc = spawn(CHROME, ['--headless', '--remote-debugging-port=0', `--user-data-dir=${dir}`,
    '--no-first-run', '--hide-scrollbars', 'about:blank'], { stdio: 'ignore' });
  let port;
  for (let i = 0; i < 100 && !port; i++) {
    await sleep(100);
    try { port = fs.readFileSync(path.join(dir, 'DevToolsActivePort'), 'utf8').split('\n')[0]; } catch {}
  }
  const exited = new Promise(r => proc.once('exit', r));
  const close = async () => {
    if (proc.exitCode === null && proc.signalCode === null) {
      proc.kill();
      let timer;
      await Promise.race([exited, new Promise(r => { timer = setTimeout(() => { proc.kill('SIGKILL'); r(); }, 3000); })]);
      clearTimeout(timer);
      await Promise.race([exited, sleep(1000)]);
    }
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  };
  try {
    if (!port) throw new Error('headless Chromium did not start');
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(list.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('headless Chromium socket did not open in 10 s')), 10000);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); });
      ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('headless Chromium socket error')); });
    });
    let id = 0; const pending = new Map();
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pending.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
    await send('Page.enable'); await send('Runtime.enable');
    const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value;
    // Lay out #main markup at a width; returns the measured boxes.
    const layout = async (width, main) => {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: width < 600 });
      const { frameTree } = await send('Page.getFrameTree');
      await send('Page.setDocumentContent', { frameId: frameTree.frame.id, html: PAGE(CSS, main) });
      return ev(`(async () => {
        await document.fonts.ready;
        const box = e => { if (!e) return null; const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
        const results = document.querySelector('.ew-search-results');
        return {
          width: innerWidth,
          plex: document.fonts.check('14px "IBM Plex Sans"'),
          results: box(results),
          count: box(results.querySelector('.ew-count-line')),
          tools: box(results.querySelector('.ew-results-tools')),
          row: box(results.querySelector('.filter-summary.ew-chips')),
          chips: [...results.querySelectorAll('.filter-summary .filter-chip')].map(c => {
            const t = document.createRange(); t.selectNodeContents(c.firstChild);
            return { name: c.getAttribute('aria-label'), pill: box(c), label: box(t), x: box(c.querySelector('.ew-chip-x')) };
          }),
        };
      })()`);
    };
    return { layout, close: async () => { ws.close(); await close(); } };
  } catch (e) { await close(); throw e; }
}
const CSS = SKIP ? '' : appCss();
const inside = (a, b, slack = 0.5) => a.left >= b.left - slack && a.right <= b.right + slack && a.top >= b.top - slack && a.bottom <= b.bottom + slack;
const meets = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

test('SF-SCR-R2-01: a long show chip keeps its label and × inside the pill, clear of Cards/List, at 1440 and 390',
  { skip: SKIP }, async () => {
    const b = await browser();
    try {
      assert.equal(LONG.length, 60, 'fixture: a 60-character name');
      assert.equal(MEDIUM.length, 26, 'fixture: a 26-character name');
      for (const width of [1440, 390]) for (const [id, name] of [['cs-long', LONG], ['cs-medium', MEDIUM], ['cs-longest', LONGEST]]) {
        const html = await searchHtml(`#/search?mode=ads&sort=newest&shows=${id}`, width);
        const m = await b.layout(width, html);
        assert.equal(m.width, width, 'fixture: the viewport width');
        assert.ok(m.plex, 'fixture: IBM Plex Sans is loaded');
        assert.ok(m.tools, 'fixture: Cards/List is on the page');
        const chip = m.chips.find(c => c.name === `Remove ${name}`);
        assert.ok(chip, `${width}: no chip named "Remove ${name}": ${JSON.stringify(m.chips.map(c => c.name))}`);
        const at = `${width}, "${name}": ${JSON.stringify({ pill: chip.pill, label: chip.label, x: chip.x, tools: m.tools })}`;
        assert.ok(inside(chip.label, chip.pill), `the label runs out of its pill (${at})`);
        assert.ok(inside(chip.x, chip.pill), `the × sits outside its pill (${at})`);
        assert.ok(!meets(chip.pill, m.tools), `the pill overlaps Cards/List (${at})`);
        assert.ok(chip.pill.right <= m.results.right + 0.5, `the pill runs past the results column (${at})`);
      }
    } finally {
      await b.close();
    }
  });

test('SF-SCR-R2-02: below 600 px the chip row runs the full results width under the count and Cards/List; at 1440 it sits under the count line',
  { skip: SKIP }, async () => {
    const b = await browser();
    try {
      // The seeded "Mint Mobile ads in October": a brand and two dates.
      const route = '#/search?mode=ads&sort=newest&brands=mint%20mobile&from=2026-10-01&to=2026-10-31';
      const phone = await b.layout(390, await searchHtml(route, 390));
      assert.equal(phone.chips.length, 3, 'fixture: brand, From and Through chips');
      const p = JSON.stringify({ results: phone.results, count: phone.count, tools: phone.tools, row: phone.row });
      assert.ok(Math.abs(phone.row.left - phone.results.left) <= 0.5 && Math.abs(phone.row.right - phone.results.right) <= 0.5,
        `390: the chip row is not full width (${p})`);
      assert.ok(phone.row.top >= phone.tools.bottom && phone.row.top >= phone.count.bottom, `390: the chip row is not under the count and Cards/List (${p})`);
      assert.ok(phone.row.top - Math.max(phone.tools.bottom, phone.count.bottom) <= 16, `390: the chip row is far under the count row (${p})`);
      // SF-R2-01: the count line and Cards/List share one row, count first.
      assert.ok(phone.count.top < phone.tools.bottom && phone.tools.top < phone.count.bottom
        && phone.count.left < phone.tools.left, `390: the count line and Cards/List are not one row, count first (${p})`);
      const wide = await b.layout(1440, await searchHtml(route, 1440));
      const w = JSON.stringify({ count: wide.count, tools: wide.tools, row: wide.row });
      assert.ok(wide.row.top >= wide.count.bottom && wide.row.top - wide.count.bottom <= 8, `1440: the chip row is not directly under the count line (${w})`);
      assert.ok(wide.row.right <= wide.tools.left, `1440: the chip row is not in the count column (${w})`);
    } finally {
      await b.close();
    }
  });
