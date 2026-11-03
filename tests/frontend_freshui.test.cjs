/* FRESHUI (fresh-data design 11, 16 step 6): Overview shows
   "Data updated <time>" and "Newest episode <date>". An unknown update
   time is left out (QA-LIVE #12); an unknown newest episode says
   "unknown"; no episodes says "No episodes yet". Neither line promises
   complete coverage of yesterday. At 320 px the two lines stack and wrap.
   Harness copied from frontend_uat_ov.test.cjs; the CSS cascade resolver
   from frontend_a1_phone_list.test.cjs.
   Run: node --test tests/frontend_freshui.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const CSS = fs.readFileSync(path.join(APP, 'search.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9174/app/' + route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
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
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };

async function views(route = '#/overview') {
  const win = loadAt(route);
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.setTimeout = () => {};
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  return { win, modal, main, toasts };
}
const withApi = (t, api) => {
  t.calls = [];
  t.win.EW.api = async (p, opts = {}) => {
    t.calls.push([opts.method || 'GET', p, opts.body]);
    const out = await api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  return t;
};
async function at(t, route) {
  t.win.location.hash = route; t.win.EW.current.route = route;
  await t.win.EWViews.render();
  return t.main.innerHTML;
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ')
  .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/\s+/g, ' ');

const empty = over => ({ coverage: { episodes: 1, episodes_with_transcript: 1, shows: 1 }, recent_events: [], offer_sequence: [],
  alert_totals: [], alert_lifecycle_counts: { active_local: 0, paused: 0, stopped: 0 }, tracked_campaigns: [], campaign_gaps: [], activity: [], ...over });
async function overview(ov, saved = [], prefs = null) {
  const t = withApi(await views(), p => {
    if (p === '/customer/overview') return ov;
    if (p.startsWith('/customer/search')) return { items: [] };
    if (p.startsWith('/customer/saved-items')) return { items: saved };
    if (p === '/customer/brands') return { brands: [] };
    return {};
  });
  if (prefs) { // the viewer's Settings preferences, read before Overview as after any earlier page
    const base = t.win.fetch;
    t.win.fetch = async (url, init) => (url === '/api/v1/customer/preferences'
      ? { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: prefs }) }
      : base(url, init));
    await settle();
    assert.equal((await t.win.EW.preferences()).time_zone, prefs.time_zone);
  }
  return at(t, '#/overview');
}
// Flat rule list: { media, selectors, decls, order }.
function parse(css, media = null, out = []) {
  let i = 0;
  while (i < css.length) {
    const open = css.indexOf('{', i);
    if (open < 0) break;
    const prelude = css.slice(i, open).trim();
    let depth = 1, j = open + 1;
    while (j < css.length && depth) { if (css[j] === '{') depth++; else if (css[j] === '}') depth--; j++; }
    const body = css.slice(open + 1, j - 1);
    if (prelude.startsWith('@media')) parse(body, prelude.slice(6).trim(), out);
    else if (!prelude.startsWith('@')) {
      const decls = {};
      for (const d of body.split(';')) {
        const k = d.indexOf(':');
        if (k > 0) decls[d.slice(0, k).trim()] = d.slice(k + 1).trim().replace(/\s+/g, ' ');
      }
      out.push({ media, selectors: prelude.split(',').map(s => s.trim()), decls, order: out.length });
    }
    i = j;
  }
  return out;
}
const RULES = parse(CSS);

// Screen media at a viewport width; pointer: fine (the headless check).
function mediaMatches(media, width) {
  if (media == null) return true;
  return media.split(',').some(part => {
    if (/\bprint\b/.test(part)) return false;
    const feats = [...part.matchAll(/\(([^)]*)\)/g)].map(m => m[1].split(':').map(s => s.trim()));
    return feats.every(([f, v]) => f === 'max-width' ? width <= parseFloat(v)
      : f === 'min-width' ? width >= parseFloat(v)
      : f === 'pointer' ? v === 'fine' : false);
  });
}
// Selectors of the form ".a .b.c .ew-row" (descendant combinators, classes only).
function matches(selector, chain) {
  const parts = selector.split(/\s+/);
  if (!parts.every(p => /^(\.[\w-]+)+$/.test(p))) return false;
  const want = parts.map(p => p.slice(1).split('.'));
  const self = want.pop();
  if (!self.every(c => chain[chain.length - 1].includes(c))) return false;
  let k = chain.length - 2;
  for (let w = want.length - 1; w >= 0; w--) {
    while (k >= 0 && !want[w].every(c => chain[k].includes(c))) k--;
    if (k < 0) return false;
    k--;
  }
  return true;
}
const specificity = s => (s.match(/\.[\w-]+/g) || []).length;
function computed(chain, width, prop) {
  let best = null;
  for (const r of RULES) {
    if (!(prop in r.decls) || !mediaMatches(r.media, width)) continue;
    for (const s of r.selectors) {
      if (!matches(s, chain)) continue;
      const sp = specificity(s);
      if (!best || sp > best.sp || (sp === best.sp && r.order >= best.order)) best = { sp, order: r.order, value: r.decls[prop] };
    }
  }
  return best && best.value;
}

const fresh = html => {
  const m = String(html).match(/<p class="ew-ov-fresh"[^>]*>[\s\S]*?<\/p>/);
  assert.ok(m, 'the freshness line is on Overview');
  // Label spans are inline: unwrap them as a browser renders them, with no
  // added space ("Newest episode: unknown").
  return { html: m[0], text: visible(m[0].replace(/<span class="ew-ov-fresh-label">([^<]*)<\/span>/g, '$1')).trim() };
};
const PROMISE = /yesterday|complete|all episodes|up to date|every episode|real[- ]time/i;

test('both lines: the last successful import time and the newest episode date', async () => {
  const html = await overview(empty({ data_updated_at: '2026-10-21T09:12:03+00:00', newest_episode: { date: '2026-09-21', has_episodes: true } }));
  const f = fresh(html);
  assert.match(f.text, /^Data updated \S/);
  assert.doesNotMatch(f.text, /Data updated: unknown/);
  assert.match(f.html, /<time datetime="2026-10-21T09:12:03Z"/);
  assert.match(f.text, /Newest episode Sep 21, 2026/);
  assert.doesNotMatch(f.text, PROMISE);
});

// QA-LIVE #12: an unknown update time is left out, not shown as
// "Data updated: unknown"; the newest episode still shows.
test('Data updated is left out when the record is missing, null or not a zoned time', async () => {
  for (const v of [undefined, null, '', 'nonsense', '2026-10-21', '2026-10-21T09:12:03', 12345,
    // QA12-R3-01: zoned in shape, but no browser time (comma fraction, no such month).
    '2026-10-21T09:12:03,5+00:00', '2026-13-45T09:12Z']) {
    const ov = empty({ newest_episode: { date: '2026-09-21', has_episodes: true } });
    if (v !== undefined) ov.data_updated_at = v;
    const f = fresh(await overview(ov));
    assert.doesNotMatch(f.text, /Data updated/, String(v));
    assert.doesNotMatch(f.html, /ew-ov-fresh-sep/, String(v));
    assert.equal(f.text, 'Newest episode Sep 21, 2026', String(v));
  }
});

test('QA-LIVE #12: an unknown update time with no episodes shows only "No episodes yet"', async () => {
  for (const v of [undefined, null]) {
    const ov = empty({ newest_episode: { date: null, has_episodes: false } });
    if (v !== undefined) ov.data_updated_at = v;
    const f = fresh(await overview(ov));
    assert.equal(f.text, 'No episodes yet', String(v));
    assert.doesNotMatch(f.html, /ew-ov-fresh-sep/, String(v));
  }
  const both = fresh(await overview(empty({ newest_episode: { date: null, has_episodes: true } })));
  assert.equal(both.text, 'Newest episode: unknown');
});

test('no episodes shows "No episodes yet", not a date', async () => {
  const f = fresh(await overview(empty({ data_updated_at: '2026-10-21T09:12:03Z', newest_episode: { date: null, has_episodes: false } })));
  assert.match(f.text, /No episodes yet/);
  assert.doesNotMatch(f.text, /Newest episode/);
  assert.doesNotMatch(f.text, PROMISE);
});

test('episodes without a known date show "Newest episode: unknown"', async () => {
  for (const ne of [{ date: null, has_episodes: true }, { date: 'Tue, 21 Oct', has_episodes: true }, undefined, null]) {
    const ov = empty({ data_updated_at: '2026-10-21T09:12:03Z' });
    if (ne !== undefined) ov.newest_episode = ne;
    const f = fresh(await overview(ov));
    assert.match(f.text, /Newest episode: unknown/, JSON.stringify(ne));
    assert.doesNotMatch(f.text, /No episodes yet/, JSON.stringify(ne));
  }
});

test('a failed overview read shows the newest episode as unknown and leaves out Data updated', async () => {
  const f = fresh(await overview(new Error('boom')));
  assert.doesNotMatch(f.text, /Data updated/);
  assert.match(f.text, /Newest episode: unknown/);
});

test('the newest episode shows its year: 2026-11-01 is 1 Nov 2026', async () => {
  const f = fresh(await overview(empty({ data_updated_at: '2026-11-02T09:00:00Z', newest_episode: { date: '2026-11-01', has_episodes: true } })));
  assert.match(f.text, /Newest episode Nov 1, 2026/);
  assert.match(f.html, /<time datetime="2026-11-01">/);
});

test('320 px phone: the two lines stack and long values wrap', () => {
  const LINE = [['overview-search'], ['ew-ov-fresh']];
  const ITEM = [['overview-search'], ['ew-ov-fresh'], ['ew-ov-fresh-item']];
  assert.equal(computed(LINE, 320, 'display'), 'flex');
  assert.equal(computed(LINE, 320, 'flex-direction'), 'column');
  assert.equal(computed(ITEM, 320, 'min-width'), '0');
  assert.equal(computed(ITEM, 320, 'overflow-wrap'), 'anywhere');
  assert.notEqual(computed(ITEM, 320, 'white-space'), 'nowrap');
  // Wider screens keep one wrapping row.
  assert.equal(computed(LINE, 1280, 'flex-wrap'), 'wrap');
  assert.notEqual(computed(LINE, 1280, 'flex-direction'), 'column');
});

// FRESHUI-SOL-R1-01: the refresh time carries its year, in the viewer's
// Settings time zone with the zone named.
test('Data updated shows the year: "Oct 21, 2026 at 7:29 PM PDT" in the viewer\'s zone', async () => {
  const html = await overview(empty({ data_updated_at: '2026-10-22T02:29:41Z', newest_episode: { date: '2026-10-19', has_episodes: true } }),
    [], { time_zone: 'America/Los_Angeles' });
  const f = fresh(html);
  assert.match(f.text, /Data updated Oct 21, 2026 at 7:29 PM PDT\b/);
  assert.match(f.html, /<time datetime="2026-10-22T02:29:41Z"[^>]*>Oct 21, 2026 at 7:29 PM PDT<\/time>/);
  const utc = fresh(await overview(empty({ data_updated_at: '2026-10-22T02:29:41Z', newest_episode: { date: '2026-10-19', has_episodes: true } }),
    [], { time_zone: 'UTC' }));
  assert.match(utc.text, /Data updated Oct 22, 2026 at 2:29 AM UTC\b/);
  // QA-LIVE #12: an unknown time is left out.
  assert.equal(fresh(await overview(empty({ newest_episode: { date: '2026-10-19', has_episodes: true } }))).text, 'Newest episode Oct 19, 2026');
});

// FRESHUI-SOL-R1-02: semibold labels, muted regular values, and an
// aria-hidden middle dot between the two items at 600 px and above only.
const LABEL = /<span class="ew-ov-fresh-label">([^<]*)<\/span>/g;
const SEP = '<span class="ew-ov-fresh-sep" aria-hidden="true">·</span>';
test('the two labels are wrapped, the dot sits between the items and is aria-hidden', async () => {
  const both = fresh(await overview(empty({ data_updated_at: '2026-10-21T09:12:03Z', newest_episode: { date: '2026-10-19', has_episodes: true } })));
  assert.deepEqual([...both.html.matchAll(LABEL)].map(m => m[1]), ['Data updated', 'Newest episode']);
  assert.match(both.html, new RegExp('</span>' + SEP + '<span class="ew-ov-fresh-item">'));
  assert.equal(both.html.split(SEP).length, 2, 'one dot');
  assert.match(both.text, /^Data updated .+ · Newest episode Oct 19, 2026$/);
  // Unknown states keep their exact words; only the label is wrapped. An
  // unknown update time is left out with its dot (QA-LIVE #12).
  const unk = fresh(await overview(new Error('boom')));
  assert.equal(unk.text, 'Newest episode: unknown');
  assert.deepEqual([...unk.html.matchAll(LABEL)].map(m => m[1]), ['Newest episode']);
  assert.equal(unk.html.split(SEP).length, 1, 'no dot');
  // "No episodes yet" has no label to wrap.
  const none = fresh(await overview(empty({ data_updated_at: '2026-10-21T09:12:03Z', newest_episode: { date: null, has_episodes: false } })));
  assert.deepEqual([...none.html.matchAll(LABEL)].map(m => m[1]), ['Data updated']);
  assert.match(none.text, /· No episodes yet$/);
});

test('labels are semibold, values stay regular, the dot shows only at 600 px and above', () => {
  const LABEL_CHAIN = [['overview-search'], ['ew-ov-fresh'], ['ew-ov-fresh-item'], ['ew-ov-fresh-label']];
  const SEP_CHAIN = [['overview-search'], ['ew-ov-fresh'], ['ew-ov-fresh-sep']];
  for (const w of [320, 390, 1440]) assert.equal(computed(LABEL_CHAIN, w, 'font-weight'), '600', String(w));
  assert.match(computed([['overview-search'], ['ew-ov-fresh']], 1440, 'font'), /^var\(--ew-t-ui\)$/); // 400 values
  assert.equal(computed(SEP_CHAIN, 320, 'display'), 'none');
  assert.equal(computed(SEP_CHAIN, 599, 'display'), 'none');
  assert.notEqual(computed(SEP_CHAIN, 600, 'display'), 'none');
  assert.notEqual(computed(SEP_CHAIN, 1440, 'display'), 'none');
  // No card, warning colour or heavier divider on the line.
  for (const prop of ['background', 'border', 'border-top', 'box-shadow']) assert.equal(computed([['overview-search'], ['ew-ov-fresh']], 1440, prop), null, prop);
});

// FRESHUI-R2-02: a date that passes the shape check but is not a real day
// never throws (Overview must render) and is never shown as a shifted day.
test('an impossible newest date is "Newest episode: unknown", not a crash', async () => {
  for (const v of ['2026-13-40', '2026-02-30', '2026-00-10']) {
    const f = fresh(await overview(empty({ data_updated_at: '2026-10-21T09:12:03Z', newest_episode: { date: v, has_episodes: true } })));
    assert.match(f.text, /Newest episode: unknown$/, v);
    assert.doesNotMatch(f.html, /<time datetime="2026-(13|02-30|00)/, v);
  }
});
