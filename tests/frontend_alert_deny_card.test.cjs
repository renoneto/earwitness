/* ADV-R1-04 (UX 38d093c9e, AC-AS-A11-WD Addendum 4; no new words): when
   every current match is withheld (matches_withheld "all", ALERT-DENY-2
   shown), the Current matches card shows no count and no "In stored
   records: no matches."; with nothing else left on it, no card at all.
   The server sends no count then (current_matches count/tracked null).
   Run: node --test tests/frontend_alert_deny_card.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { page, alertApi, changesCard, ALERT, ALL, SOME } = require('./frontend_alert_deny_page.cjs');

const ALL_ALERT = { ...ALERT, events: [], unread_event_count: 0, unread_ad_count: 0, matches_withheld: 'all',
  current_matches: { count: null, count_units: 'distinct_results', refs: [], truncated: false, tracked: null,
    tracked_by_kind: null, breaks: null, held_for_brand_review: 0, held_ads_for_brand_review: 0 } };
const aside = html => html.slice(html.indexOf('<aside'));

test('ADV-R1-04: all withheld, nothing else on the card: no Current matches card at all', async () => {
  const html = (await page('#/alert/5', alertApi(ALL_ALERT))).main.innerHTML;
  assert.ok(html.includes(ALL));
  assert.doesNotMatch(html, /<h3>Current matches<\/h3>/);
  assert.doesNotMatch(aside(html), /ew-work-big|In stored records|no matches|no ad count|this alert’s search today/);
  // The check and saved-scope cards stay.
  assert.match(aside(html), /Compares stored records only/);
});

test('ADV-R1-04: all withheld with held reads left: the card keeps only the held line', async () => {
  const alert = { ...ALL_ALERT, current_matches: { ...ALL_ALERT.current_matches, held_for_brand_review: 2 } };
  const html = (await page('#/alert/5', alertApi(alert))).main.innerHTML;
  const card = aside(html).slice(0, aside(html).indexOf('</section>'));
  assert.match(card, /<h3>Current matches<\/h3>/);
  assert.match(card, /2 more ads have Brand not identified/);
  assert.doesNotMatch(card, /ew-work-big|In stored records|no matches|no ad count/);
});

test('ADV-R1-04: some withheld: the card keeps its count as before', async () => {
  const html = (await page('#/alert/5', alertApi({ ...ALERT, matches_withheld: 'some' }))).main.innerHTML;
  assert.match(aside(html), /<h3>Current matches<\/h3><p class="ew-work-big">2<\/p>/);
});

// ADC-C-02 (lead 10-30): every current match withheld while events still
// show is "all" too: the events list stays, the ALERT-DENY-2 line sits
// where the "some" line would (never "0" under the "some" line), the card
// shows no count, and the unread count of the listed events stays.
test('ADC-C-02: all withheld with events listed: the events, the all line, no count', async () => {
  const alert = { ...ALL_ALERT, events: ALERT.events, unread_event_count: 1, unread_ad_count: 1 };
  const html = (await page('#/alert/5', alertApi(alert))).main.innerHTML;
  const changes = changesCard(html);
  assert.match(changes, /ew-work-events/);
  assert.equal(changes.split(ALL).length - 1, 1, changes);
  assert.ok(!html.includes(SOME));
  assert.doesNotMatch(html, /No new matches/);
  assert.match(html, /<strong>1<\/strong> unread/);
  assert.doesNotMatch(html, /<h3>Current matches<\/h3>/);
  assert.doesNotMatch(aside(html), /ew-work-big|In stored records|no matches/);
});
