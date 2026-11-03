/* LEGAL-PAGES (G7-legal): Access, Privacy and Terms carry real public text
   in the Exhibit design, drawn from docs/launch/pilot-notice.md with the
   lead's 2026-10-23 fact updates (wording: ws-fresh/legal-pages-wording.md).
   1. No local-build wording on any public page.
   2. The Exhibit structure: one h1, the header, a doc-nav with Pricing, the footer.
   3. Every address is SUPPORT_CONTACT; no [CONTACT] or other notice placeholder.
   4. The pilot end is a gated placeholder span until it is set.
   5. Audio and export lines follow STATUS item 11 (lead, 2026-10-23).
   LEGAL-FIX1 (screen review LGS-R1-01..10, code review LEG-R1-01 and notes
   1, 3, 4): plain words, one list of what is off on Access with agents
   explained before the clip paragraph, deletion limits stated once, no
   competition deadline, judges never charged, logs and password hash
   listed, and a hidden buyer-terms marker that keeps buying off.
   LEGAL-FIX2 (code review LEG-R2-01..03, screen review LGS-R2-01..02):
   the deletion line names what stays longer and keeps the notice's
   deletion-checks condition; logs hold network addresses in general; the
   first password sent is changed; one "deletion record" (not "short");
   "60-day" never breaks at its hyphen.
   LEGAL-FIX3 (screen review LGS-R3-01..02, code review LEG-R3-01, notes
   F; lead additions): the 60-day limit and its exceptions are stated once;
   the deletion-checks sentence says what happens in plain words; the
   password form is "on the Settings page"; the password advice covers any
   password the team sends; the log retention sentence is the Sign-in host
   check's exact wording; Pricing shows the planned prices and its only
   placeholder is [PILOT END] inside the on-only purchase terms.
   PILOT-END (lead, 2026-10-23; Reno "who cares"): the pilot end is set.
   Every page that names it says PILOT_END_TEXT; no [PILOT END] remains.
   Run: node --test tests/frontend_legal_pages.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const PUB = path.join(APP, 'public');
const read = name => fs.readFileSync(path.join(PUB, name), 'utf8');
const LEGAL = { 'access.html': 'Access', 'privacy.html': 'Privacy', 'terms.html': 'Terms' };
// PUB-48 row 28: the Access page's h1 reads "Judges' access"; its <title> stays.
const H1 = { 'access.html': 'Judges&rsquo; access' };
const SUPPORT = fs.readFileSync(path.join(APP, 'views.js'), 'utf8').match(/const SUPPORT_CONTACT = '([^']+)';/)[1];
// The public form of the deployed pilot_end, 2026-12-18T23:59:00Z (host
// layout and rights basis manifest). The app has no constant for it: the
// hosted server reads EARWITNESS_PILOT_END, and the pages are static files.
const PILOT_END_TEXT = 'December 18, 2026';
// Every date written as "Month D, YYYY" on a public page.
const DATE_RE = /\b(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}\b/g;
// The pages that name the pilot end, and how many times each does.
const PILOT_END_PAGES = { 'terms.html': 2, 'privacy.html': 1, 'pricing.html': 2 };
const AUDIO = 'Some passages that EarWitness flags as ads include a short audio clip, up to about 100 seconds, so you can hear the full detected passage. The app offers no download, export, or sharing controls for clips. Agents cannot open them. They are deleted with the other pilot data.';
const BUYER_TERMS = '<!-- buyer-terms: pending -->';
const between = (html, start, end) => { const i = html.indexOf(start); return i < 0 ? '' : html.slice(i, html.indexOf(end, i)); };
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

const { ASK_PUBLIC_ROWS, MEANING_ALERT_ROWS } = require('./ask_public_rows.cjs');
// "alert" may appear only inside approved, pinned wording (meaning alerts, LEDGER-LEAD3-RENO-ALERTS-2).
const unpinned = t => [...ASK_PUBLIC_ROWS, ...MEANING_ALERT_ROWS].reduce((acc, [, b]) => acc.split(text(b).trim()).join(''), t);
test('no public page carries local-build wording', () => {
  const pages = fs.readdirSync(PUB).filter(f => f.endsWith('.html'));
  for (const name of pages) {
    const html = read(name);
    assert.doesNotMatch(html, /local development build|development build|this local build|on this machine|not approved public legal text|Local-build|Local development and research use|not activated/i, name);
    assert.doesNotMatch(html, /owner approval/i, name);
  }
});

test('legal pages keep the Exhibit structure', () => {
  for (const [name, title] of Object.entries(LEGAL)) {
    const html = read(name);
    assert.match(html, new RegExp(`<title>${title} — EarWitness</title>`), name);
    assert.match(html, /<meta name="description" content="[^"[\]]{20,}">/, name);
    assert.equal((html.match(/<h1[ >]/g) || []).length, 1, `${name}: one h1`);
    assert.match(html, new RegExp(`<article class="doc-body">\\n<h1>${H1[name] || title}</h1>\\n<p class="lede">`), name);
    assert.match(html, /<header class="site-header">\n<a class="wordmark" href="\/">EarWitness<\/a>/, name);
    assert.match(between(html, '<nav class="doc-nav"', '</nav>'), /<a href="\/pricing\/">Pricing<\/a>/, name);
    const foot = between(html, '<footer class="site-footer">', '</footer>');
    assert.match(foot, /<p class="footer-note">[\s\S]+?<\/p>/, name);
    // Freeze tidy (Review 10-31 ~00:30Z): no "Open workspace" in any public footer; the header "Sign in" goes to /app/.
    assert.doesNotMatch(foot, /Open workspace/, name);
    assert.equal((html.match(/aria-current="page"/g) || []).length, 1, `${name}: one aria-current`);
  }
});

test('every contact is the sign-in page support address, as a mailto link', () => {
  assert.equal(SUPPORT, 'support@getearwitness.com');
  for (const name of Object.keys(LEGAL)) {
    const html = read(name);
    const mails = [...html.matchAll(/mailto:([^"]+)"/g)].map(m => m[1]);
    assert.ok(mails.length >= 1, `${name}: a support link`);
    for (const m of mails) assert.equal(m, SUPPORT, name);
    const foot = between(html, '<p class="footer-note">', '</p>');
    assert.ok(foot.includes(`<a href="mailto:${SUPPORT}">${SUPPORT}</a>`), `${name} footer`);
    for (const m of html.match(/[\w.+-]+@[\w-]+(\.[\w-]+)+/g) || []) assert.equal(m, SUPPORT, `${name}: ${m}`);
  }
});

test('no notice placeholder, pending marker or name is left; the pilot end is the set date', () => {
  for (const name of Object.keys(LEGAL)) {
    const html = read(name);
    assert.deepEqual(html.match(/\[[^\]]*\]/g) || [], [], name);
    assert.doesNotMatch(html, /class="placeholder"/, name);
    assert.doesNotMatch(html, /Reno|PENDING|OPERATOR|SIGN-IN PROVIDER|HOST AND REGION|DEADLINE|CONTACT/, name);
  }
  assert.ok(read('terms.html').includes(`It runs for about 8 weeks and ends on ${PILOT_END_TEXT}. That date does not move.`));
  assert.ok(read('terms.html').includes(`<li>Early access ends on ${PILOT_END_TEXT}. Your last month is charged only up to that day.`));
  assert.ok(read('privacy.html').includes(`<li>At ${PILOT_END_TEXT}, all access ends when the service stops.`));
});

test('PILOT-END: every public page that names the pilot end says exactly PILOT_END_TEXT; no [PILOT END] is left', () => {
  const pages = fs.readdirSync(PUB).filter(f => f.endsWith('.html'));
  for (const name of pages) {
    const html = read(name);
    assert.doesNotMatch(html, /\[PILOT END\]/, name);
    const dates = html.match(DATE_RE) || [];
    for (const d of dates) assert.equal(d, PILOT_END_TEXT, `${name}: ${d}`);
    assert.equal(dates.length, PILOT_END_PAGES[name] || 0, `${name}: pilot-end mentions`);
  }
  for (const name of Object.keys(PILOT_END_PAGES)) assert.ok(pages.includes(name), name);
});

test('access: judges join with a login the EarWitness team sends; no invite link; sign-in by email and password', () => {
  const html = read('access.html');
  assert.ok(html.includes('You join only with a login that the EarWitness team sends you.'));
  assert.match(text(html), /email address and password/);
  assert.doesNotMatch(html, /invite link|invitation link/i);
  assert.ok(html.includes('No public registration or invitations. Paid early access, when it opens, is described on <a href="/pricing/">Pricing</a>.'));
});

test('audio: the STATUS item 11 sentence as one paragraph; no "no audio" claim; exports hold no transcript text and no audio', () => {
  const access = read('access.html');
  assert.equal(access.split(`<p>${AUDIO}</p>`).length - 1, 1, 'the audio sentence is one paragraph on Access');
  for (const name of Object.keys(LEGAL)) {
    assert.doesNotMatch(read(name), /not audio files|no audio files|Audio\. The pilot/, name);
  }
  assert.ok(read('privacy.html').includes('It holds no transcript text and no audio.'));
});

test('privacy states the notice facts: host, logs, deletion limits, how to leave', () => {
  const t = text(read('privacy.html'));
  for (const line of [
    'It is hosted on Hetzner Cloud in Nuremberg, Germany.',
    'Service logs can hold account and workspace IDs and network (IP) addresses, for example of a failed sign-in. They never hold transcript text. Service logs are kept 30 days, plus up to 7 days in server backups.',
    'Our server stores your password only as a one-way hash, never the password itself. Whenever the EarWitness team sends you a password, change it on the Settings page after you sign in.',
    'After a deletion request, your access ends within 3 days. Other copies on our host, including backups, are gone within 60 days. A few things stay longer; see How to leave.',
    'A deletion record stays, during the pilot and after it: request records, removed account IDs and emails, and the signed rights records and addenda. It holds no transcript text. It has no set end date.',
    `To have your account removed, write to ${SUPPORT} from the email you sign in with.`,
    'We first shut your account out; the deletion itself runs after our safety checks confirm it works. For the time limits, see What we keep, above. Some things stay longer: your account ID next to what you wrote and any email typed into a note or other workspace text, until the workspace is deleted, and the deletion record (also under What we keep).',
    'If you want a copy of your results, export them before you ask for removal.',
    'No zero-retention promise. Deletion follows the limits above.',
    'No SOC 2 certification.',
  ]) assert.ok(t.includes(line), line);
  assert.doesNotMatch(unpinned(t), /alert/i);
});

test('privacy: plain words, each limit stated once, one section on outside requests (LGS-R1-05..08, LEG-R1-01)', () => {
  const html = read('privacy.html');
  const t = text(html);
  assert.doesNotMatch(t, /shut out|external channel|wherever someone typed|Remote media and connections/i);
  assert.equal(t.split('gone by day 60').length - 1, 0, 'the zero-retention item does not repeat the numbers');
  assert.equal(t.split('kept 30 days').length - 1, 1, 'the log retention sentence once (Sign-in host check, lead)');
  assert.equal(t.split('within 60 days').length - 1, 2, 'one deletion-request limit and one pilot-end limit');
  assert.equal((html.match(/<h2>[^<]*(load|outside|remote)[^<]*<\/h2>/gi) || []).length, 1, 'one section on outside requests');
  const notPromised = between(html, '<h2>Not promised here</h2>', '</ul>');
  assert.doesNotMatch(notPromised, /Hetzner|Nuremberg|password|\d+ days?|day \d+/, 'not promised: no repeated facts');
});

test('terms agree with Pricing on paid early access and keep the results caveats', () => {
  const t = text(read('terms.html'));
  const pricing = text(read('pricing.html'));
  for (const line of [
    'Your last month is charged only up to that day. No charge after it. All data is deleted within 60 days after it.',
    'Your workspace is ready the morning after payment. We set it up in our nightly maintenance window (01:30 Pacific).',
    'One purchase per person.',
  ]) {
    assert.ok(pricing.includes(line), `pricing: ${line}`);
    assert.ok(t.includes(line), `terms: ${line}`);
  }
  assert.ok(t.includes('A result shows what was said in a specific episode. It is not proof of ad spend, reach or delivery.'));
  assert.ok(t.includes('The demo content is public podcast episodes that EarWitness transcribed.'));
  assert.doesNotMatch(unpinned(t), /alert/i);
});

test('terms: no competition deadline, judges never charged, plain caveats (LGS-R1-01, 04, 09)', () => {
  const html = read('terms.html');
  const t = text(html);
  assert.doesNotMatch(t, /competition deadline|November 4/i);
  const paid = text(between(html, '<h2>Paid early access</h2>', '</ul>'));
  assert.ok(paid.startsWith(' Paid early access This section applies only if you buy early access. Judges are never charged.'), paid.slice(0, 120));
  assert.ok(t.includes('Some campaign checks say "Not enough data"; treat those as unknown. Answers from Ask and matches from meaning alerts are written by an AI model and can be wrong. Check each cited passage. Check anything important against the original episode.'));
  assert.ok(t.includes('The ad quote and the screens on the home page come from real episodes in our collection. They show what was heard, not proof that an ad ran as bought. The Ferncastle campaign check is made up to show how EarWitness works and proves nothing about any advertiser.'));
  assert.doesNotMatch(t, /non-contractual|contractual verification|authored explanatory|alignment|redistribution|Redistribution/);
  assert.equal(t.split('An export does not give you the right to share it.').length - 1, 1);
});

test('access: one list of what is off; agents explained before the clip paragraph; plain words (LGS-R1-02, 03, 10)', () => {
  const html = read('access.html');
  const t = text(html);
  assert.doesNotMatch(t, /remote provisioning|What is not available|Paid processing|scheduled fetching/i);
  const body = between(html, '<article class="doc-body">', '</article>');
  assert.equal((body.match(/<ul>/g) || []).length, 1, 'one body list');
  const agents = body.indexOf('No agent access: AI agents and API tokens that act for you are refused.');
  assert.ok(agents > 0, 'agents explained');
  assert.ok(agents < body.indexOf(AUDIO), 'agents explained before the clip paragraph');
  // PT-01, Reno-approved 2026-10-25 (PUBLIC-WORDING.md row 29, LEDGER-LEAD2-RENO-PUBWORD-2)
  assert.ok(html.includes('<li>No adding your own shows or episodes. The EarWitness team&rsquo;s automatic import adds new episodes each night.</li>'));
  assert.doesNotMatch(t, /no automatic checks|imports the demo content/);
});

test('buyer terms: a hidden marker on Terms and Privacy keeps buying off until Reno approves buyer terms (code review note 1)', () => {
  for (const name of ['terms.html', 'privacy.html']) {
    const html = read(name);
    assert.equal(html.split(BUYER_TERMS).length - 1, 1, name);
    assert.doesNotMatch(text(html.replace(/<!--[\s\S]*?-->/g, '')), /buyer-terms/, name);
  }
  assert.ok(!read('access.html').includes('buyer-terms'));
});

test('the placeholder chip never splits across lines', () => {
  const css = fs.readFileSync(path.join(PUB, 'public.css'), 'utf8');
  assert.match(css, /\.placeholder \{[^}]*white-space: nowrap;/);
});

test('privacy: the deletion line promises only what the sources support (LEG-R2-01..03, LGS-R2-01)', () => {
  const t = text(read('privacy.html'));
  assert.doesNotMatch(t, /The rest of your data|rest of your data/i, 'no blanket promise for the rest of your data');
  assert.doesNotMatch(t, /short deletion record|small deletion record/i, 'one name: "deletion record"');
  assert.equal(t.split('deletion record stays').length - 1, 1);
  assert.doesNotMatch(t, /address of a failed sign-in or password change/, 'logs: not only failed sign-ins');
  assert.doesNotMatch(t, /\bWe store your password/, 'the server stores the hash; the first password is sent');
});

test('"N-day" never breaks at its hyphen on the legal pages (LGS-R2-02)', () => {
  for (const name of Object.keys(LEGAL)) {
    assert.doesNotMatch(text(read(name)), /\d-day/, `${name}: use &#8209; (non-breaking hyphen)`);
  }
});

test('privacy and access: the 60-day limit and its exceptions once; plain deletion sentence; Settings page (LGS-R3-01..02, LEG-R3-01, note F)', () => {
  const privacy = text(read('privacy.html'));
  const keep = text(between(read('privacy.html'), '<h2>What we keep, and for how long</h2>', '</ul>'));
  const leave = text(between(read('privacy.html'), '<h2>How to leave</h2>', '</ul>'));
  assert.doesNotMatch(privacy, /All copies on our host/, 'no unconditional 60-day promise');
  assert.doesNotMatch(leave, /60|3 days/, 'How to leave points back for the limits');
  assert.match(keep, /A few things stay longer; see How to leave\./);
  assert.match(leave, /For the time limits, see What we keep, above\./);
  assert.doesNotMatch(privacy, /deletion checks|deletion steps|\(see above\)|see the next item|drill/i);
  assert.equal(privacy.split('until the workspace is deleted').length - 1, 1, 'the workspace exception once');
  for (const name of ['privacy.html', 'access.html']) {
    assert.doesNotMatch(read(name), /account page/, name);
  }
  assert.ok(text(read('access.html')).includes('You can change your password on the Settings page.'));
  assert.doesNotMatch(privacy, /After you first sign in/, 'any password the team sends, not only the first');
});

test('pricing: the planned prices show; no placeholder is left; the pilot end shows only in the on-only purchase terms (Reno, LEDGER-LEAD2-RENO-PRICING-1)', () => {
  const html = read('pricing.html');
  assert.doesNotMatch(html, /\[PRICE\]/);
  // PUB-48 row 26: Plan A card first, then Plan B.
  const b = html.indexOf('id="plan-b"'), a = html.indexOf('id="plan-a"');
  assert.equal((html.slice(a, b).match(/<p class="plan-price">\$99 a month<\/p>/g) || []).length, 1, 'Plan A');
  assert.equal((html.slice(b).match(/<p class="plan-price">\$199 a month<\/p>/g) || []).length, 1, 'Plan B');
  assert.deepEqual(html.match(/\[[^\]]*\]/g) || [], []);
  assert.doesNotMatch(html, /class="placeholder"/);
  const outside = html.replace(/<!--pricing-if-on:(plan_[ab])-->[\s\S]*?<!--\/pricing-if-on:\1-->/g, '');
  assert.doesNotMatch(outside, DATE_RE, 'the pilot end shows only in the on-only purchase terms');
  // LEG-R4 note 1: the pilot end once in each card's on-only terms, the same date in both.
  for (const plan of ['plan_a', 'plan_b']) {
    const block = between(html, `<!--pricing-if-on:${plan}-->`, `<!--/pricing-if-on:${plan}-->`);
    assert.equal(block.split(`<p>Early access ends on ${PILOT_END_TEXT}. Your last month`).length - 1, 1, plan);
  }
  // LGS-R4-01: the lede must hold while buying is off.
  assert.match(html, /<p class="lede">Paid early access, once it opens, runs until the pilot ends\./);
  assert.doesNotMatch(html, /<p class="lede">Paid early access runs until/);
});


test('Ask wording: rows 30-35 are on the public pages byte for byte (PT-02..PT-07)', () => {
  for (const [page, bytes] of ASK_PUBLIC_ROWS) assert.ok(read(page).includes(bytes), `${page}: ${bytes.slice(0, 60)}`);
  assert.doesNotMatch(read('privacy.html'), /the app itself fetches nothing from other sites/);
  // Row 32: the Ask item, then the meaning-alerts item (M3), then the deletion-record item, in the same list (PW2-R1-03).
  const ask = ASK_PUBLIC_ROWS.filter(([p]) => p === 'privacy.html').map(([, b]) => b).filter(b => b.startsWith('<li>Your Ask questions') || b.startsWith('<li>A deletion record'));
  assert.equal(ask.length, 2);
  const m3 = MEANING_ALERT_ROWS.filter(([p, b]) => p === 'privacy.html' && b.startsWith('<li>Your meaning alerts')).map(([, b]) => b);
  assert.equal(m3.length, 1);
  const esc = b => b.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  assert.match(read('privacy.html'), new RegExp(esc(ask[0]) + '\\s*' + esc(m3[0]) + '\\s*' + esc(ask[1])));
});

test('Meaning-alert wording: rows M1-M6 are on the public pages byte for byte (LEDGER-LEAD3-RENO-ALERTS-2)', () => {
  assert.ok(MEANING_ALERT_ROWS.length > 0);
  for (const [page, bytes] of MEANING_ALERT_ROWS) assert.ok(read(page).includes(bytes), `${page}: ${bytes.slice(0, 60)}`);
});
