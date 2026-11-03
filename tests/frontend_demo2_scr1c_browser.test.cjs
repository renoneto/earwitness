/* DEMO2-SCR1 code review r1, SCR1C-R1-02: the effective focus ring of the
   session-recovery dialog, in a real headless Chromium (the judge-flow
   browser) with every app stylesheet the page links.
   After showModal(); focus() the box itself draws no ring (it is never a
   keyboard target: tabindex -1). Its Check again button, reached with Tab,
   keeps its ring.
   Skips when that browser is not installed (a named verification gap).
   EW_FOCUS_SHOT_DIR=<dir> also writes a screenshot of the focused box.
   Run: node --test tests/frontend_demo2_scr1c_browser.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const APP = path.join(__dirname, '..', 'app');
const CHROME = process.env.EW_CHROME || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const sleep = ms => new Promise(r => setTimeout(r, ms));

// The stylesheets app/index.html links for screen, in order.
function appCss() {
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  return [...html.matchAll(/<link rel="stylesheet" href="\/app\/([^"]+)"( media="print")?>/g)]
    .filter(m => !m[2]).map(m => fs.readFileSync(path.join(APP, m[1]), 'utf8')).join('\n');
}

async function browser() {
  const root = process.env.AGENT_SCRATCH || os.tmpdir();
  const dir = fs.mkdtempSync(path.join(root, 'ew-focus-chrome-'));
  const proc = spawn(CHROME, ['--headless', '--remote-debugging-port=0', `--user-data-dir=${dir}`,
    '--no-first-run', '--hide-scrollbars', 'about:blank'], { stdio: 'ignore' });
  let port;
  for (let i = 0; i < 100 && !port; i++) {
    await sleep(100);
    try { port = fs.readFileSync(path.join(dir, 'DevToolsActivePort'), 'utf8').split('\n')[0]; } catch {}
  }
  // CTR-R2-01: close waits for the browser to exit (it writes its
  // profile while it shuts down; SIGKILL after 3 s), then removes the
  // folder; a failed setup closes too; the socket open times out.
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
    return { send, pid: proc.pid, dir, close: async () => { ws.close(); await close(); } };
  } catch (e) { await close(); throw e; }
}

// CTR-R2-01: close() returns once the browser has exited (it writes
// Default/Session Storage while it shuts down), and the profile folder is gone.
test('CTR-R2-01: the headless Chromium helper closes once the browser has exited and leaves no profile folder',
  { skip: fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}` }, async () => {
    const b = await browser();
    await b.close();
    assert.throws(() => process.kill(b.pid, 0), { code: 'ESRCH' }, 'the browser still ran when close() returned');
    assert.equal(fs.existsSync(b.dir), false, 'the profile folder is left behind');
  });

// The recovery dialog as app/views.js builds it (tabindex -1, a notice, Check again).
const PAGE = css => `<!doctype html><html><head><style>${css}</style></head><body><main><p>Page</p></main>
<dialog data-return-check class="ew-return-check" tabindex="-1" aria-labelledby="return-check-title" aria-describedby="return-check-why">
<div class="notice"><h2 id="return-check-title">Checking that you are still signed in</h2>
<p id="return-check-why">Your page is hidden until we can check that you are still signed in.</p>
<button class="button" data-action="return-check">Check again</button></div></dialog></body></html>`;

test('SCR1C-R1-02 the recovery dialog box draws no focus ring after showModal(); focus(), and Check again keeps its keyboard ring',
  { skip: fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}` }, async () => {
    const b = await browser();
    try {
      const { send } = b;
      await send('Page.enable'); await send('Runtime.enable');
      const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;
      const { frameTree } = await send('Page.getFrameTree');
      await send('Page.setDocumentContent', { frameId: frameTree.frame.id, html: PAGE(appCss()) });
      const box = await ev(`(() => { const d = document.querySelector('dialog'); d.showModal(); d.focus();
        const s = getComputedStyle(d);
        return { focused: document.activeElement === d, visible: d.matches(':focus-visible'), style: s.outlineStyle, width: s.outlineWidth }; })()`);
      if (process.env.EW_FOCUS_SHOT_DIR) {
        const r = await send('Page.captureScreenshot', { format: 'png' });
        fs.writeFileSync(path.join(process.env.EW_FOCUS_SHOT_DIR, 'recovery-box-focused.png'), Buffer.from(r.data, 'base64'));
        fs.writeFileSync(path.join(process.env.EW_FOCUS_SHOT_DIR, 'recovery-box-focused.json'), JSON.stringify(box) + '\n');
      }
      assert.ok(box.focused, 'fixture: the dialog holds focus');
      assert.equal(box.style, 'none', `the box draws a ring: ${JSON.stringify(box)}`);
      // Tab to Check again: a keyboard target keeps the app's ring.
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      const button = await ev(`(() => { const e = document.activeElement; const s = getComputedStyle(e);
        return { action: e.dataset.action, visible: e.matches(':focus-visible'), style: s.outlineStyle, width: s.outlineWidth }; })()`);
      assert.equal(button.action, 'return-check', 'fixture: Tab reaches Check again');
      assert.equal(button.style, 'solid', `Check again lost its ring: ${JSON.stringify(button)}`);
      assert.equal(button.width, '2px');
    } finally {
      await b.close();
    }
  });

// SCR1C-R1-03: the empty export status live region stays rendered (in the
// accessibility tree, so text written into it is announced), hidden only
// visually; with text it is its own visible row.
const SEARCH_HEAD = css => `<!doctype html><html><head><style>${css}</style></head><body><main id="main">
<div class="ew-search-head"><form id="search"><input id="search-query" aria-label="Search"></form>
<div class="ew-search-actions"><button class="button quiet" data-action="export-open">Export…</button></div>
<span class="ew-help" id="export-status" role="status"></span><p class="ew-scope-line">Scope</p></div></main></body></html>`;

test('SCR1C-R1-03 the empty export status stays in the accessibility tree, hidden only visually',
  { skip: fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}` }, async () => {
    const b = await browser();
    try {
      const { send } = b;
      await send('Page.enable'); await send('Runtime.enable'); await send('DOM.enable'); await send('Accessibility.enable');
      const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;
      const { frameTree } = await send('Page.getFrameTree');
      await send('Page.setDocumentContent', { frameId: frameTree.frame.id, html: SEARCH_HEAD(appCss()) });
      const status = async () => {
        const box = await ev(`(() => { const e = document.querySelector('#export-status'); const s = getComputedStyle(e); const r = e.getBoundingClientRect();
          return { display: s.display, visibility: s.visibility, w: r.width, h: r.height }; })()`);
        const { root } = await send('DOM.getDocument', { depth: -1 });
        const { nodeId } = await send('DOM.querySelector', { nodeId: root.nodeId, selector: '#export-status' });
        const { nodes } = await send('Accessibility.getPartialAXTree', { nodeId, fetchRelatives: false });
        const own = nodes[0] || {};
        return { ...box, ignored: !!own.ignored, role: own.role && own.role.value };
      };
      const empty = await status();
      assert.notEqual(empty.display, 'none', `the empty live region leaves the tree: ${JSON.stringify(empty)}`);
      assert.equal(empty.ignored, false, `ignored by the accessibility tree: ${JSON.stringify(empty)}`);
      assert.equal(empty.role, 'status');
      assert.ok(empty.w <= 1 && empty.h <= 1, `the empty line takes room on screen: ${JSON.stringify(empty)}`);
      // And no row of the band: the band is as tall as without the element.
      const heights = await ev(`(() => { const head = document.querySelector('.ew-search-head'); const e = document.querySelector('#export-status');
        const withIt = head.getBoundingClientRect().height; const next = e.nextSibling; e.remove(); const without = head.getBoundingClientRect().height;
        head.insertBefore(e, next); return { withIt, without }; })()`);
      assert.equal(heights.withIt, heights.without, `the empty line adds a row: ${JSON.stringify(heights)}`);
      await ev(`document.querySelector('#export-status').textContent = 'Preparing your export of 12 results…'`);
      const said = await status();
      assert.equal(said.ignored, false);
      assert.ok(said.w > 50 && said.h > 8, `with text the line shows: ${JSON.stringify(said)}`);
    } finally {
      await b.close();
    }
  });

// DEMO2-SCR-R2-02: the "Loading results…" note painted above the retained
// search frame is whole: its bottom sits above the search band, at 1440
// and 390 (the band no longer pulls itself up under it).
const LOADING = css => `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body><main id="main" class="page" tabindex="-1">
<p class="section-note" role="status">Loading results for episodes matching “Muse”… Showing the earlier results for ads matching “Muse” below; they are not new results.</p>
<h1 class="visually-hidden">Search</h1>
<div class="ew-search-head"><form id="search" class="ew-searchbox"><input id="search-query" aria-label="Search"><button class="button primary">Search</button></form>
<div class="ew-search-tabs"><button aria-pressed="true">Ads</button></div>
<div class="ew-search-actions"><button class="button quiet" data-action="export-open">Export…</button></div>
<span class="ew-help" id="export-status" role="status"></span><p class="ew-scope-line">All dates, all shows in this collection.</p></div>
<div class="ew-search-body"><section id="search-results"><p>Results</p></section></div></main></body></html>`;

test('SCR-R2-02 the "Loading results…" note is not under the search band at 1440 or 390',
  { skip: fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}` }, async () => {
    const b = await browser();
    try {
      const { send } = b;
      await send('Page.enable'); await send('Runtime.enable');
      const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;
      for (const width of [1440, 390]) {
        await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: width < 600 });
        const { frameTree } = await send('Page.getFrameTree');
        await send('Page.setDocumentContent', { frameId: frameTree.frame.id, html: LOADING(appCss()) });
        const box = await ev(`(() => { const n = document.querySelector('#main > .section-note').getBoundingClientRect();
          const h = document.querySelector('.ew-search-head').getBoundingClientRect();
          return { width: innerWidth, noteTop: n.top, noteBottom: n.bottom, headTop: h.top }; })()`);
        assert.equal(box.width, width, 'fixture: the viewport width');
        assert.ok(box.noteTop >= 0, `${width}: the note starts off screen: ${JSON.stringify(box)}`);
        assert.ok(box.noteBottom <= box.headTop, `${width}: the search band covers the note: ${JSON.stringify(box)}`);
      }
    } finally {
      await b.close();
    }
  });
