/* ALERT-DENY-5 (UX 811d6d4a1, AC-AS-A11-WD Addendum 4; lead-approved
   10-29): every new match withheld while current matches still show
   (matches_withheld "new"). In place of "No new matches", one line with
   no count, title or source; ALERT-DENY-1 never under it; "No new
   matches" only when there are truly none; no new-match count or unread
   count at all, never 0. Run: node --test tests/frontend_alert_deny_new.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page, alertApi, changesCard, ALERT, SOME, ALL } = require('./frontend_alert_deny_page.cjs');

const NEW = 'New matches can’t be shown here, because the rights for their shows don’t allow it.';
const NEW_ALERT = { ...ALERT, events: [], unread_event_count: 0, unread_ad_count: 0, matches_withheld: 'new',
  current_matches: { ...ALERT.current_matches, count: 1, tracked: 1, breaks: 1, tracked_by_kind: { new_finding: 1 } } };
const head = html => html.slice(html.indexOf('<p class="page-head-meta">'), html.indexOf('</p>', html.indexOf('<p class="page-head-meta">')));

test('ALERT-DENY-5: the line, byte for byte, in place of "No new matches", and nothing under it', async () => {
  const html = (await page('#/alert/5', alertApi(NEW_ALERT))).main.innerHTML;
  const card = changesCard(html);
  assert.ok(card.includes(`<div class="ew-card-body"><p class="section-note">${NEW}</p></div></section>`), card);
  assert.equal(html.split(NEW).length - 1, 1);
  assert.ok(!html.includes(SOME) && !html.includes(ALL));
  assert.doesNotMatch(card, /No new matches|No baseline yet|Current matches are your starting point/);
  assert.doesNotMatch(NEW, /\d|'/);
});

test('ALERT-DENY-5: no new-match or unread count anywhere, never 0', async () => {
  const html = (await page('#/alert/5', alertApi(NEW_ALERT))).main.innerHTML;
  assert.doesNotMatch(head(html), /unread|<strong>/);
  assert.doesNotMatch(changesCard(html), /\d+ changes?|unread|\d+ new/);
  // The current matches still show their count.
  assert.match(html.slice(html.indexOf('<aside')), /<h3>Current matches<\/h3><p class="ew-work-big">1<\/p>/);
});

test('ALERT-DENY-5: with every current match withheld too ("all"), no count either', async () => {
  const alert = { ...NEW_ALERT, matches_withheld: 'all',
    current_matches: { ...NEW_ALERT.current_matches, count: null, tracked: null, breaks: null, tracked_by_kind: null } };
  const html = (await page('#/alert/5', alertApi(alert))).main.innerHTML;
  assert.ok(html.includes(ALL) && !html.includes(NEW));
  assert.doesNotMatch(head(html), /unread|<strong>/);
  assert.doesNotMatch(changesCard(html), /\d+ changes?|unread/);
});

test('ALERT-DENY-5: truly no new matches keeps "No new matches" and its counts', async () => {
  for (const withheld of [undefined, 'some']) {
    const alert = { ...NEW_ALERT, matches_withheld: withheld };
    const html = (await page('#/alert/5', alertApi(alert))).main.innerHTML;
    assert.match(changesCard(html), /No new matches/);
    assert.match(changesCard(html), /0 changes · 0 unread/);
    assert.ok(!html.includes(NEW));
  }
});
