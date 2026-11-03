/* JUDGE-SMALL item 5: in-app clip wording PT-08..PT-11
   (qa-full-20261024/PUBLIC-WORDING.md rows 36-39; ships with CLIP-GUARD
   in DEP7b). A guarded clip is hidden, not deleted, so "kept" becomes
   untrue: the no-clip words drop it. Exact strings from the rows.
   The rendered pages are pinned by the existing tests that read these
   strings (uat_tv V-38, plain_ads FIX2, uat_d1a, uat_d7, …).
   Run: node --test tests/frontend_clip_wording.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const src = file => fs.readFileSync(path.join(APP, file), 'utf8');
const has = (file, line) => src(file).split('\n').some(l => l === line);

test('PT-08: the ad page says "No clip for this ad. The words are below."', () => {
  assert.ok(has('views.js', "    noClip: 'No clip for this ad. The words are below.',"), 'views.js noClip');
});

test('PT-09: the episode note says "No audio clips for this episode’s ads. Times and passages are still listed."', () => {
  assert.ok(src('views.js').includes('<p class="ew-ads-note">No audio clips for this episode’s ads. Times and passages are still listed.</p>'), 'views.js episode note');
});

test('PT-10: AUDIO_NONE is "No clip (transcript only)", in the code and at run time', () => {
  assert.ok(has('ui.js', "  const AUDIO_NONE = 'No clip (transcript only)';"), 'ui.js AUDIO_NONE');
  const sandbox = { console, URLSearchParams, window: {}, document: { querySelector: () => null, querySelectorAll: () => [] } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src('ui.js'), sandbox, { filename: 'ui.js' });
  assert.equal(sandbox.EWUI.AUDIO_STATES.none, 'No clip (transcript only)');
});

test('PT-11: the brand and show ad lists label the dash "No clip"', () => {
  assert.ok(src('views.js').includes('<span class="ew-play-none" aria-label="No clip" role="img">—</span>'), 'views.js aria-label');
});

test('PT-08..11: no in-app text says a clip is or is not "kept" (the full-episode-audio line aside)', () => {
  for (const file of ['app.js', 'ui.js', 'views.js']) {
    const lines = src(file).split('\n').filter(l => !/^\s*(\/\/|\/\*|\*)/.test(l))
      .filter(l => /No clip kept|No clip is kept for this ad|No audio clips are kept|aria-label="No clip kept"/.test(l));
    assert.deepEqual(lines, [], file);
  }
});
