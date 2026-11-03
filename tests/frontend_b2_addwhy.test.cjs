/* Shared Add-dialog helper EWB2.addWhy.list (app/b2.js; SEARCH-7 item 2, RSEARCH-R1-02 / Ad review RA-R1-04).
   The research case ("This ad can’t be added yet") names the member's open research campaigns the dialog does
   not list. A dialog that lists a row of the member's own (a radio with a value: open, or the ad already
   attached) is never the research case. Shared by the Search and Ad pages.
   Run: node --test tests/frontend_b2_addwhy.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

// Today's dialog markup (views.js campaignAttachDialog), as a browser reads it back.
const HEAD = '<form id="attach-to-campaign" novalidate><div class="dialog-head"><h2 id="dialog-title">Add to campaign</h2></div><div class="dialog-body">';
const FOOT = '</div><div class="dialog-footer"><button class="button" type="button" data-action="close-dialog">Cancel</button></div></form>';
const rows = inner => HEAD + '<fieldset class="radio-group"><legend>Placement or research target</legend>' + inner + '</fieldset>' + FOOT;
const ATTACHED = rows('<label><input type="radio" name="choice" value="c7|t1" disabled><span><b>Quince ads</b> · Episode: TJG · research observation · already attached</span></label>');
const OPEN = rows('<label><input type="radio" name="choice" value="c7|t1" checked autofocus><span><b>Quince ads</b> · Episode: TJG · research observation</span></label>');
const THEIRS = rows('<label><input type="radio" name="choice" disabled><span><b>Holiday push</b> · Episode: TJG · research observation</span></label>');
const NONE = HEAD + '<p class="section-note">No campaign placement covers this episode.</p>' + FOOT;

const list = () => H.boot({ route: '#/search' }).win.EWB2.addWhy.list;
const ids = out => [...out].map(c => c.id);

test('RA-R1-04 / RSEARCH-R1-02: the ad is already in "Quince ads" and "Mint study" misses the episode: not the research case', () => {
  const camps = [{ id: 7, name: 'Quince ads' }, { id: 9, name: 'Mint study' }];
  assert.deepEqual(ids(list()(ATTACHED, camps)), []);
  assert.deepEqual(ids(list()(ATTACHED.replace('value="c7|t1" disabled', 'value="c7|t1" disabled=""'), camps)), [], 'as a browser reads it back');
});
test('an open row of the member\'s own: not the research case', () => {
  assert.deepEqual(ids(list()(OPEN, [{ id: 7, name: 'Quince ads' }, { id: 9, name: 'Mint study' }])), []);
});
test('nothing listed: every open research campaign is named', () => {
  assert.deepEqual(ids(list()(NONE, [{ id: 7, name: 'Quince ads' }, { id: 9, name: 'Mint study' }])), [7, 9]);
  assert.deepEqual(ids(list()(NONE, [])), []);
  assert.deepEqual(ids(list()(NONE, undefined)), []);
});
test('only another member\'s greyed row (no value): the member\'s own unlisted campaigns are named; a listed one is not', () => {
  assert.deepEqual(ids(list()(THEIRS, [{ id: 7, name: 'Quince ads' }, { id: 8, name: 'Holiday push' }])), [7]);
});
test('a name is matched escaped, as the dialog writes it', () => {
  const html = rows('<label><input type="radio" name="choice" disabled><span><b>Q &amp; “A”</b> · Episode: TJG</span></label>');
  assert.deepEqual(ids(list()(html, [{ id: 3, name: 'Q & “A”' }, { id: 4, name: 'Other' }])), [4]);
});

// The Search page's Add dialog takes the shared rule (the Ad page cherry-picks the base commit and wires it the same way).
test('Search page, RSEARCH-R1-02: "already attached" in "Quince ads", "Mint study" misses: the row shows, no "can’t be added yet", no "Go to Campaigns"', () => {
  const { win } = H.boot({ route: '#/search' });
  const camps = [{ id: 7, name: 'Quince ads' }, { id: 9, name: 'Mint study' }];
  for (const html of [ATTACHED, ATTACHED.replace('disabled>', 'disabled="">')]) {
    const d = win.EWSearchB2.dialog(html, { adding: 'x', research: camps, cfa: true });
    assert.doesNotMatch(d, /can’t be added yet|Go to Campaigns|Open “Mint study”|data-why/);
    assert.match(d, /<b>Quince ads<\/b>[^<]*· already attached/);
  }
  // Nothing listed: the research case still names both.
  assert.match(win.EWSearchB2.dialog(NONE, { adding: 'x', research: camps }), /This ad can’t be added yet[\s\S]*Go to Campaigns/);
});

// Landing screen r2 n4 (all pages): the brand mark is regular weight, as the reference .s-mark (no weight); today's
// styles.css draws every <mark> at 600, so the shared .s-mark sets its weight back.
test('n4: the shared .s-mark (app-b2.css) inherits its weight, never today\'s 600', () => {
  const fs = require('node:fs'); const path = require('node:path');
  const css = fs.readFileSync(path.join(H.APP, 'app-b2.css'), 'utf8').replace(/\s+/g, ' ');
  assert.match(css, /\.s-mark \{ background: var\(--ew-mark\); color: inherit; font-weight: inherit; padding: 0 2px; border-radius: 2px; \}/);
  assert.match(fs.readFileSync(path.join(H.APP, 'styles.css'), 'utf8'), /^mark \{[^}]*font-weight: 600/m, 'the rule it undoes');
});
