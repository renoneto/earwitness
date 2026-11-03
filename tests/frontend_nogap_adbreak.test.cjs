/* NOGAP branch AB, rows 21 and 154 (UX-M4 collapse, RES-02, M4 roll card;
   in-app words "Ad break · N brands" APPROVED, LEDGER-LEAD3-DET-1): one
   sponsor roll (AG1, BetterHelp, Article) showed on Search as three
   separate ads. Search Ads now shows the ads of one overlap group
   (group_id, group_ad_count >= 2) that sit next to each other on the page
   under one "Ad break · N brands" box, like the episode page's adsBody:
   N is the different brands those ads name, and the box needs 2 or more
   ads and 2 or more brands. Each ad keeps its own card (or list row),
   head, evidence, select box and data-index; counts stay ads.
   Run: node --test tests/frontend_nogap_adbreak.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const B = (key, display) => ({ key, display, confirmed: false });
const AG1 = B('ag1', 'AG1'), HELP = B('betterhelp', 'BetterHelp'), ARTICLE = B('article', 'Article'), QUINCE = B('quince', 'Quince');
/* One ad per entry: [brand or null, group id, group_ad_count, span, brands
   the ad names (default: its brand)]. */
function bundle(ads, view = 'cards') {
  const items = ads.map(([b, , , span], i) => ({ ref: REF(i + 1), record_kind: 'evidence', status: 'finding', review: null,
    show_title: 'Show A', ep_title: 'Episode One', episode_id: 'e1', pub_date: '2026-10-09', audio: 'unavailable',
    quotation_span: span, ad_break_id: 'brk:' + i, show: { show_identity_id: 'cs-a' },
    read: { brand: b || { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false }, display_span: span, has_clip: false, needs_review: false },
    attribution: b ? [{ brand: b.key, basis: 'suggested', channel: 'spoken' }] : [] }));
  const breaks = ads.map(([b, group, n, span, named], i) => ({ id: 'brk:' + i, episode_id: 'e1', transcript_version_id: 7, on_current_transcript: true,
    span, read_count: 1, matching_read_count: 1, refs: [REF(i + 1)], pub_date: '2026-10-09', group_id: group, group_ad_count: n,
    brands: named || (b ? [b] : []) }));
  return { query: { version: 2, mode: 'ads', q: '', sort: 'newest', page: 1, view }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 },
    result: { items, total: items.length, dated_total: items.length, undated_total: 0, total_relation: 'exact', per_page: 10,
      ad_breaks: breaks, break_total: items.length, break_dated_total: items.length, break_undated_total: 0 },
    summary: { week: [] } };
}
const api = b => p => {
  if (p === '/customer/research-query') return b;
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};
async function search(ads, view = 'cards') {
  const { html } = await F.render('#/search?mode=ads' + (view === 'list' ? '&view=list' : ''), api(bundle(ads, view)));
  assert.ok(html.length > 500, view + ' rendered');
  return html;
}
const ROLL = [[AG1, 'g', 3, [600, 660]], [HELP, 'g', 3, [650, 720]], [ARTICLE, 'g', 3, [700, 780]], [QUINCE, 'q', 1, [1200, 1260]]];
const label = n => `<h3 class="ew-adbreak-h">Ad break <span aria-hidden="true">·</span> ${n} brands</h3>`;
const boxes = html => html.split(/<(?:div|li) class="ew-adbreak">/).slice(1);
const titles = part => [...part.matchAll(/<h3 class="ew-break-title"[^>]*>([\s\S]*?)<\/h3>/g)].map(m => F.text(m[1]).trim());

test('AB rows 21/154: one roll on Search is one "Ad break · N brands" box over its ads, in cards and list', async () => {
  for (const view of ['cards', 'list']) {
    const html = await search(ROLL, view);
    const box = boxes(html);
    assert.equal(box.length, 1, view + ': one Ad break box');
    // The list keeps list markup: the box is one item of the ads list.
    assert.match(html, view === 'list' ? /<li class="ew-adbreak">[\s\S]*<ul class="ew-break-list">/ : /<div class="ew-adbreak">/, view);
    assert.ok(box[0].startsWith(label(3)), view + ': the approved label, 3 brands');
    const inside = box[0].split('<!--/adbreak-->')[0];
    assert.deepEqual(titles(inside), ['AG1', 'BetterHelp', 'Article'], view + ': the roll\'s ads, each with its own head');
    assert.deepEqual(titles(html.split('<!--/adbreak-->')[1]), ['Quince'], view + ': the single ad sits outside');
    assert.equal((F.text(html).match(/Ad break/g) || []).length, 1, view);
    // Counts stay ads; every ad keeps its own select, report and index.
    assert.match(F.text(html), /Showing 1 to 4 of 4 ads/, view);
    assert.equal((html.match(/<summary>Report a mistake<\/summary>/g) || []).length, 4, view);
    assert.deepEqual([...html.matchAll(/data-index="(\d+)"/g)].map(m => m[1]).filter((x, i, a) => a.indexOf(x) === i), ['0', '1', '2', '3'], view);
  }
});

test('AB rows 21/154: N counts the different brands named; an ad with no brand stays inside, uncounted', async () => {
  let html = await search([[AG1, 'g', 3, [0, 30]], [HELP, 'g', 3, [20, 50]], [AG1, 'g', 3, [40, 70]]]);
  assert.equal(boxes(html).length, 1);
  assert.ok(boxes(html)[0].startsWith(label(2)));
  html = await search([[AG1, 'g', 3, [0, 30]], [null, 'g', 3, [20, 50]], [HELP, 'g', 3, [40, 70]]]);
  assert.ok(boxes(html)[0].startsWith(label(2)));
  assert.deepEqual(titles(boxes(html)[0].split('<!--/adbreak-->')[0]), ['AG1', 'Brand not identified', 'BetterHelp']);
});

test('AB rows 21/154: no box over one ad, one brand, ads of different groups, or a roll split by another ad', async () => {
  const cases = {
    'no groups': [[AG1, 'a', 1, [0, 30]], [HELP, 'b', 1, [20, 50]]],
    'one ad of the roll on the page': [[AG1, 'g', 3, [0, 30]], [QUINCE, 'q', 1, [100, 130]]],
    // AB-R1-03: one ad of a roll that alone names 2 brands (the split rule
    // lets a read naming AG1 and BetterHelp join an AG1 ad): 2 brands, 1 ad.
    'one ad naming 2 brands': [[AG1, 'g', 2, [0, 30], [AG1, HELP]], [QUINCE, 'q', 1, [100, 130]]],
    'one brand': [[AG1, 'g', 2, [0, 30]], [AG1, 'g', 2, [20, 50]]],
    'two groups': [[AG1, 'g', 2, [0, 30]], [HELP, 'h', 2, [20, 50]]],
    'not next to each other': [[AG1, 'g', 2, [0, 30]], [QUINCE, 'q', 1, [100, 130]], [HELP, 'g', 2, [20, 50]]],
  };
  for (const [name, ads] of Object.entries(cases)) {
    for (const view of ['cards', 'list']) {
      const html = await search(ads, view);
      assert.equal(boxes(html).length, 0, `${name} (${view})`);
      assert.doesNotMatch(F.text(html), /Ad break/, `${name} (${view})`);
    }
  }
});

test('AB rows 21/154: Search with no roll renders as before (no box markup)', async () => {
  const html = await search([[AG1, 'a', 1, [0, 30]]]);
  assert.doesNotMatch(html, /ew-adbreak/);
});

test('AB-R1-02: in the list the Ad break box adds no left offset, so its rows stay under the column header', () => {
  const fs = require('fs'), path = require('path');
  const css = fs.readFileSync(path.join(__dirname, '..', 'app', 'search.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  // The cards' box (evidence.css .ew-adbreak) indents 12 px plus a 3 px
  // border; the list's box keeps the rule but draws it inside.
  const box = (css.match(/(?:^|\})\s*\.ew-break-list > \.ew-adbreak\s*\{([^}]*)\}/) || [])[1];
  assert.ok(box, 'a list-only rule for the box');
  assert.match(box, /padding-left:\s*0\s*;/);
  assert.match(box, /border-left:\s*0\s*;/);
  assert.match(box, /box-shadow:\s*inset 3px 0 0 var\(--ew-border\)/);
});

/* ABC-01 (confirm review of FIX1): the list layout in a real headless
   Chromium with every app stylesheet (loader copied from
   frontend_saved_filter_layout.test.cjs). Skips when that browser is not
   installed (a named verification gap). */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const CHROME = process.env.EW_CHROME || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const SKIP = fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}`;
const sleep = ms => new Promise(r => setTimeout(r, ms));
function appCss() {
  const html = fs.readFileSync(path.join(F.APP, 'index.html'), 'utf8');
  return [...html.matchAll(/<link rel="stylesheet" href="\/app\/([^"]+)"( media="print")?>/g)]
    .filter(m => !m[2]).map(m => {
      const file = path.join(F.APP, m[1]);
      return fs.readFileSync(file, 'utf8').replace(/url\(([\w.-]+\.woff2)\)/g, (_, f) =>
        `url(data:font/woff2;base64,${fs.readFileSync(path.join(path.dirname(file), f)).toString('base64')})`);
    }).join('\n');
}
const PAGE = main => `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>${appCss()}</style></head>
<body><header id="header" class="topbar"><a class="wordmark" href="#/overview">EarWitness</a></header>
<main id="main" class="page" tabindex="-1">${main}</main></body></html>`;
async function browser() {
  const dir = fs.mkdtempSync(path.join(process.env.AGENT_SCRATCH || os.tmpdir(), 'ew-adbreak-chrome-'));
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
    // Lay out #main markup at a width.
    const load = async (width, main) => {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: width < 600 });
      const { frameTree } = await send('Page.getFrameTree');
      await send('Page.setDocumentContent', { frameId: frameTree.frame.id, html: PAGE(main) });
      await ev('document.fonts.ready.then(() => innerWidth)');
    };
    const hover = (x, y) => send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
    return { load, hover, ev, close: async () => { ws.close(); await close(); } };
  } catch (e) { await close(); throw e; }
}
// A CSS colour as this page computes it (a probe element).
const COLOUR = `(v => { const p = document.body.appendChild(document.createElement('i')); p.style.color = v;
  const c = getComputedStyle(p).color; p.remove(); return c; })`;

test('ABC-01: a hovered unselected row in the list Ad break box keeps the box\'s 3 px rule (its own inset), at 1440',
  { skip: SKIP }, async () => {
    const b = await browser();
    try {
      await b.load(1440, await search(ROLL, 'list'));
      const at = await b.ev(`(() => {
        const r = document.querySelector('.ew-adbreak .ew-row[data-index="1"]').getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, width: innerWidth };
      })()`);
      assert.equal(at.width, 1440, 'fixture: the viewport width');
      await b.hover(at.x, at.y);
      const m = await b.ev(`(() => {
        const row = document.querySelector('.ew-adbreak .ew-row[data-index="1"]'), cs = getComputedStyle(row);
        return { hovered: row.matches(':hover'), selected: row.classList.contains('is-selected'), bg: cs.backgroundColor,
          shadow: cs.boxShadow, box: getComputedStyle(row.closest('.ew-adbreak')).boxShadow,
          hover: ${COLOUR}('var(--ew-hover)'), border: ${COLOUR}('var(--ew-border)') };
      })()`);
      const s = JSON.stringify(m);
      assert.ok(m.hovered && !m.selected, `fixture: the BetterHelp row is hovered and not selected (${s})`);
      assert.equal(m.bg, m.hover, `fixture: the hover paints the row (${s})`);
      assert.equal(m.box, `${m.border} 3px 0px 0px 0px inset`, `the box draws its 3 px rule inside (${s})`);
      // The row's hover background covers the box's own shadow, so the
      // row draws the same rule itself.
      assert.equal(m.shadow, m.box, `the hovered row hides the box's rule (${s})`);
    } finally {
      await b.close();
    }
  });

test('ABC-01: the list "Ad break · N brands" label starts where the ad heads start, at 1440 and 390',
  { skip: SKIP }, async () => {
    const b = await browser();
    try {
      for (const width of [1440, 390]) {
        await b.load(width, await search(ROLL, 'list'));
        const m = await b.ev(`(() => {
          const x = e => { const t = document.createRange(); t.selectNodeContents(e); return t.getBoundingClientRect().left; };
          const box = document.querySelector('.ew-break-list > .ew-adbreak');
          return { width: innerWidth, label: x(box.querySelector('.ew-adbreak-h').firstChild),
            heads: [...box.querySelectorAll('.ew-break-title')].map(x),
            outside: x(document.querySelector('.ew-break-list > .ew-break-row .ew-break-title')) };
        })()`);
        const s = `${width}: ${JSON.stringify(m)}`;
        assert.equal(m.width, width, 'fixture: the viewport width');
        assert.equal(m.heads.length, 3, `fixture: the roll's 3 ad heads (${s})`);
        for (const head of m.heads) {
          assert.ok(Math.abs(head - m.outside) <= 0.5, `a boxed ad head is off the ad heads outside the box (${s})`);
          assert.ok(Math.abs(m.label - head) <= 0.5, `the label is off the ad heads (${s})`);
        }
      }
    } finally {
      await b.close();
    }
  });
