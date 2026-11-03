/* Judge path: Review 2026-10-31 judge path (UX walkthrough 9967ee581).
   1. Each judgepath_public pin in earwitness/app/public_rows/public_rows.json
      is on its page once (an empty new: the old line is gone from that page).
   2. Home: one primary hero button, "Judges: start here" to /access/; the
      nav keeps the only header "Sign in"; no "Open workspace" signed out.
   3. Judges' access leads with the sign-in button and the judge-login line,
      before "Where to start".
   4. Review and UX binding: Judges' access never shows a password.
   Run: node --test tests/frontend_public_judgepath.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PUB = path.join(__dirname, '..', 'app', 'public');
const read = name => fs.readFileSync(path.join(PUB, name), 'utf8');
const count = (hay, needle) => hay.split(needle).length - 1;
const between = (s, a, b) => { const i = s.indexOf(a); return s.slice(i, s.indexOf(b, i)); };
const ROWS = require(path.join(__dirname, '..', 'earwitness', 'app', 'public_rows', 'public_rows.json')).judgepath_public;

test('judge-path pins: each new line once on its page, each old line gone', () => {
  assert.equal(ROWS.length, 4);
  for (const r of ROWS) {
    const html = read(r.file);
    if (r.new) assert.equal(count(html, r.new), 1, `${r.row} new (${r.file})`);
    if (r.old) assert.equal(count(html, r.old), 0, `${r.row} old (${r.file})`);
  }
});

test('home: one primary hero button to /access/, one header Sign in, no Open workspace', () => {
  const html = read('index.html');
  const hero = between(html, '<section class="hero"', '</section>');
  assert.equal(count(hero, 'class="cta"'), 1, 'one primary hero button');
  assert.match(hero, /<a class="cta" href="\/access\/">Judges: start here<\/a>/);
  assert.doesNotMatch(hero, /Sign in/);
  const header = between(html, '<header class="site-header">', '</header>');
  assert.equal(count(header, '>Sign in</a>'), 1);
  assert.doesNotMatch(html, /Open workspace/);
});

test('Judges’ access: sign-in button and judge-login line lead, before "Where to start"', () => {
  const html = read('access.html');
  const body = between(html, '<article class="doc-body">', '</article>');
  const firstH2 = body.indexOf('<h2>');
  assert.equal(body.indexOf('<h2>Sign in with your login</h2>'), firstH2, 'sign-in is the first section');
  const button = body.indexOf('<a class="cta" href="/app/">Sign in</a>');
  const line = body.indexOf('<p>Use the judge login from your submission.</p>');
  const start = body.indexOf('<h2>Where to start</h2>');
  assert.ok(button > firstH2 && line > button && start > line, 'button, then the line, then Where to start');
  assert.match(body, /<p>Try these first: play the real ad on your Overview, or search a brand; /);
});

test('Judges\u2019 access: the sign-in section is exactly its pinned bytes (JP-4), nothing added', () => {
  const body = between(read('access.html'), '<article class="doc-body">', '</article>');
  const at = body.indexOf('<h2>Sign in with your login</h2>');
  const section = body.slice(at, body.indexOf('<h2>', at + 1));
  const jp4 = ROWS.find(r => r.row === 'JP-4').new;
  assert.equal(section, jp4 + '\n');
});

// Credential guard (security r1 MEDIUM-1, LOW-1, LOW-2). Every static public
// page is checked in three places, each after decoding entities (named and
// numeric, so &#58; and &#x3a; are colons): the visible text, every HTML
// comment, and every attribute as name=value (data-* included). Prose that
// names a password with no value passes; anything ambiguous fails.
// Every static public page, not a fixed list (security r2 LOW-3).
const PAGES = fs.readdirSync(PUB).filter(f => f.endsWith('.html')).sort();
const LABEL = String.raw`\b(?:password|passphrase|passcode|pass|pwd|pw|pin|secret|token|api[_-]?key)\b`;
// Words that follow a bare label in approved copy ("password from the login",
// "pass on the source material"). Any other next word fails.
const LABEL_PROSE = new Set(['from', 'on', 'only', 'itself', 'reach']);
// Words that follow an email address in approved copy ("Ask support@... for a new one").
const EMAIL_PROSE = new Set(['for', 'from']);
const EMAIL = String.raw`[\w.+-]+@[\w-]+(?:\.[\w-]+)+`;
const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", colon: ':', commat: '@', sol: '/', equals: '=', period: '.',
  lowbar: '_', nbsp: ' ', ndash: '–', mdash: '—', middot: '·', hellip: '…',
  ldquo: '“', rdquo: '”', lsquo: '‘', rsquo: '’' };
const decode = s => s
  .replace(/&#x([0-9a-f]+);?/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
  .replace(/&#(\d+);?/g, (_, d) => String.fromCodePoint(Number(d)))
  .replace(/&([a-z]+);/gi, (_, n) => NAMED[n.toLowerCase()] ?? ' ');

function tokenHits(text) {
  const hits = [];
  for (const m of text.matchAll(/\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*/g)) hits.push(m[0]);
  for (const m of text.matchAll(/[A-Za-z0-9+\/_-]{20,}={0,2}/g)) {
    const t = m[0];
    if ((/[A-Z]/.test(t) && /[a-z]/.test(t) && /\d/.test(t)) || /[A-Za-z0-9+\/]{16,}={1,2}$/.test(t)) hits.push(t);
  }
  for (const m of text.matchAll(/\b[0-9a-f]{32,}\b/g)) hits.push(m[0]);
  return hits;
}

// One decoded string (visible text, a comment, or name=value).
function valueHits(text, { prose = true } = {}) {
  const hits = [];
  const add = m => hits.push(m[0].trim());
  // A label, then ":", "=", "-", a dash or " is ", then a value.
  for (const m of text.matchAll(new RegExp(LABEL + String.raw`\s*(?:[:：=\-–—]|\bis\b)\s*['"‘“]?[^\s<>"'?‘’“”]+`, 'gi'))) add(m);
  // A value before the label: "hunter22 is the password" (security r2 LOW-3).
  for (const m of text.matchAll(new RegExp(String.raw`(\S+)\s+(?:is|was|=)\s+(?:the|your|my|a|our)?\s*(?:[\w-]+\s+)?` + LABEL, 'gi'))) {
    if (/\d/.test(m[1]) || /[A-Z].*[a-z]|[a-z].*[A-Z]/.test(m[1].slice(1))) add(m);
  }
  // A label, a space, then a value: fails unless the next word is approved prose.
  if (prose) for (const m of text.matchAll(new RegExp(LABEL + String.raw`[ \t ]+([^\s<>"'.,;:?!]+)`, 'gi'))) {
    if (!LABEL_PROSE.has(m[1].toLowerCase())) add(m);
  }
  // An email, then any separator and a value; or a space and a word that is not approved prose.
  for (const m of text.matchAll(new RegExp(EMAIL + String.raw`\s*[\/:|,;=\-–—·]\s*[^\s<>"']+`, 'g'))) add(m);
  for (const m of text.matchAll(new RegExp(EMAIL + String.raw`[ \t ]+([^\s<>"'.,;:?!]+)`, 'g'))) {
    if (!EMAIL_PROSE.has(m[1].toLowerCase())) add(m);
  }
  // A login / secret pair: a spaced slash, or a handle with a digit or "@" on the left.
  if (prose) for (const m of text.matchAll(/(\S+)\s*\/\s*(\S+)/g)) {
    if (/\s\/|\/\s/.test(m[0]) || /\d|@/.test(m[1]) || (/[A-Za-z]/.test(m[2]) && /\d/.test(m[2]))) add(m);
  }
  hits.push(...tokenHits(text));
  return hits;
}

function credentialHits(html) {
  const hits = [];
  const comments = [...html.matchAll(/<!--([\s\S]*?)-->/g)].map(m => m[1]);
  const markup = html.replace(/<!--[\s\S]*?-->/g, ' ');
  const visible = decode(markup.replace(/<[^>]+>/g, ' '));
  hits.push(...valueHits(visible).map(h => `text: ${h}`));
  for (const c of comments) hits.push(...valueHits(decode(c)).map(h => `comment: ${h}`));
  for (const tag of markup.matchAll(/<[a-z][^>]*>/gi)) {
    const attrs = [...tag[0].matchAll(/([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)]
      .map(a => [a[1].toLowerCase(), decode(a[2] ?? a[3] ?? a[4] ?? '')]);
    for (const [name, value] of attrs) {
      // No inline handler and no javascript: link on a static public page (code/security r2b).
      if (/^on[a-z]+$/.test(name)) hits.push(`handler: ${name}`);
      if (/^javascript:/i.test(value.replace(/[\s\x00-\x1f]/g, ''))) hits.push(`attr ${name}: javascript:`);
    }
    // A label in name/property/id with the value in content or value: <meta name="api-token" content="...">.
    const labelled = attrs.some(([n, v]) => ['name', 'property', 'id', 'itemprop', 'http-equiv'].includes(n) && new RegExp(LABEL, 'i').test(v.replace(/[_:.-]/g, ' ')));
    for (const [n, v] of attrs) if (labelled && ['content', 'value'].includes(n) && v.trim()) hits.push(`attr ${n}: labelled value`);
    for (const a of tag[0].matchAll(/([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)) {
      const name = a[1].toLowerCase();
      const value = decode(a[2] ?? a[3] ?? a[4] ?? '');
      // Links and image paths hold slashes and file names: label and email
      // checks only there; everything else gets every check.
      const path = ['href', 'src', 'srcset', 'action'].includes(name);
      const found = path ? valueHits(`${name}=${value}`, { prose: false }).filter(h => !tokenHits(value).includes(h))
                         : valueHits(`${name}=${value}`, { prose: false }).concat(valueHits(value));
      hits.push(...found.map(h => `attr ${name}: ${h}`));
    }
  }
  // No inline script body and no form value on a static public page (security r2 LOW-3): public.js is the only script.
  for (const m of markup.matchAll(/<script\b([^>]*)>([\s\S]*?)(?:<\/script\s*>|$)/gi)) {
    if (m[2].trim() || !/\bsrc\s*=\s*(["']?)\/public\/public\.js\1(?:\s|$)/.test(m[1])) hits.push(`script: ${(m[2].trim() || m[1]).slice(0, 40)}`);
  }
  for (const m of markup.matchAll(/<(?:input|textarea|select|option)\b[^>]*>|<button\b[^>]*\bvalue\s*=[^>]*>/gi)) hits.push(`form: ${m[0].slice(0, 60)}`);
  return [...new Set(hits)];
}

test('credential guard: catches the credential shapes from the security review', () => {
  const bad = [
    'Password: hunter22', 'password:x9', 'login?pass=abc', 'pass=abc',
    "password: 'hunter22'", 'password: “hunter22”', '<input type="hidden" value="hunter22">', '<input value="Tr0ub4dor9">',
    '<script>var p="hunter22"</script>', 'hunter22 is the password', 'Tr0ub4dor is your password',
    '<script>var p="hunter22"</script >', '<script>var p="hunter22"', '<script src="https://cdn.example/x.js"></script>',
    '<button name="pw" value="hunter22">Go</button>', '<a onclick="go()">x</a>', '<a href="java\tscript:go()">x</a>',
    '<meta name="api-token" content="hunterhunter">', 'Hunter22 is the judge password', 'password：hunter22',
    'key 0123456789abcdef0123456789abcdef',
    'pw: Tr0ub4dor', 'pwd=Tr0ub4dor', 'passcode: 482913', 'Passcode 482913', 'PIN: 4829', 'pin 4829',
    'secret: abc', 'token=ab12cd34ef56', 'api_key: sk-abcdef123456', 'api-key=sk-abcdef123456',
    'Password is hunter22', 'password - hunter22', 'password hunterhunter', 'Username judge1 password Tr0ub4dor',
    'Your login: judge1 / Tr0ub4dor', 'judge1 / hunterhunter', 'judge1/Tr0ub4dor',
    'judge1@example.com / Tr0ub4dor', 'judge1@example.com / hunterhunter', 'judge1@example.com — Tr0ub4dor',
    'judge1@example.com: hunterhunter', 'judge1@example.com Kx7pQ2mZ', 'judge1@example.com hunterhunter',
    'password&colon; hunter22', 'password&#58; hunter22', 'password&#x3a; hunter22', 'password&#X3A; hunter22',
    'judge1&#64;example.com / Tr0ub4dor', 'password&nbsp;: hunter22', 'Password: <code>hunter22</code>',
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqdWRnZTEifQ.c2lnbmF0dXJl', 'dGhpcyBpcyBhIHNlY3JldCB0b2tlbg==',
    'Use key Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5', '<!-- pw Tr0ub4dor -->', '<!-- password: hunter22 -->',
    '<p data-pw="Tr0ub4dor">x</p>', '<span data-secret="abc">x</span>', "<a data-login='judge1@example.com / Tr0ub4dor'>x</a>",
    '<a href="/app/?token=ab12cd34ef56">x</a>', '<meta name="x" content="dGhpcyBpcyBhIHNlY3JldCB0b2tlbg==">',
  ];
  for (const s of bad) assert.ok(credentialHits(s).length, s);
});

test('credential guard: passes approved copy that names a password with no value', () => {
  const ok = [
    '<h2>Sign in with your login</h2>', '<a class="cta" href="/app/">Sign in</a>', '<p>Use the judge login from your submission.</p>',
    '<p>Sign in with the email address and password from the login the EarWitness team sends you. You can change your password on the Settings page. Forgot your password? Ask <a href="mailto:support@getearwitness.com">support@getearwitness.com</a> for a new one.</p>',
    'We keep your password only as a one-way hash, never the password itself.',
    'If you forget your password, change it on the Settings page.',
    'They do not let any outside agent or token reach your workspace.',
    'AI agents and API tokens that act for you are refused.',
    'We pass on the source material.',
    '<img src="/public/img/cover-the-ai-daily-brief-artificial-intelligence-news-and-analysis.jpg" alt="">',
    '<!--pricing-if-on:plan_b-->', '<!-- buyer-terms: pending -->',
  ];
  for (const s of ok) assert.deepEqual(credentialHits(s), [], s);
});

test('no static public page shows a credential (Review and UX binding for /access/)', () => {
  assert.ok(PAGES.length >= 8, `public pages found: ${PAGES.length}`);
  for (const name of PAGES) assert.deepEqual(credentialHits(read(name)), [], name);
});

test('freeze tidy: no "Open workspace" on any public page; /access/ keeps one way in', () => {
  for (const name of PAGES) assert.doesNotMatch(read(name), /Open workspace|Open the workspace/, name);
  const body = between(read('access.html'), '<article class="doc-body">', '</article>');
  assert.equal(count(body, 'href="/app/"'), 1, 'one /app/ link in the article: the Sign in button');
  assert.equal(count(body, '<p class="doc-links"><a href="/privacy/">Privacy facts</a></p>'), 1);
});
