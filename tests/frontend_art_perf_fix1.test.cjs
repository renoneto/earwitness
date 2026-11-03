/* ART-PERF-FIX1 APV-R1-05: the initials tile reads any script.
   Titles that start with "#" or are written in Chinese, Japanese or
   Persian used to get "?": only words starting with A-Z or 0-9 counted.
   Leading punctuation is skipped, the first letter or digit in any script
   is the initial, and a name with none gets a plain tile with no "?".
   Run: node --test tests/frontend_art_perf_fix1.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function ui() {
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: '#/shows' }, history: { replaceState() {} }, scrollTo() {},
    fetch: async () => { throw new Error('no network in unit test'); },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  return sandbox.EWUI;
}
const initials = (U, name, opts) => U.initialsTile(name, opts).match(/>([^<]*)<\/span>$/)[1];

test('the seven "?" shows get a real initial', () => {
  const U = ui();
  const seen = {
    '#SistersInLaw': 'S',
    '小Lin说': '小',
    '张小珺Jùn｜商业访谈录': '张',
    '硅谷101': '硅',
    '自我进化论': '自',
    'پادکست رخ': 'پ\u200Cر',   // ART-PERF-FIX3: kept apart (APV-C-08)
    '安住紳一郎の日曜天国': '安',
  };
  for (const [name, want] of Object.entries(seen)) {
    assert.equal(initials(U, name), want, name);
  }
});

test('leading punctuation and symbols are skipped; Latin titles are unchanged', () => {
  const U = ui();
  for (const [name, want] of Object.entries({
    'The Long Run': 'LR', 'The 5th Hour': '5H', '20/20': '2', 'a16z Podcast': 'AP',
    '"Quoted" Show': 'QS', '(The) Daily': 'D', '🎙 Mic Check': 'MC', '¡Hola! Amigos': 'HA',
    'Ölçü Sesi': 'ÖS', 'Радио Т': 'РТ', '#1 Hit': '1H', 'élan vital': 'ÉV',
  })) {
    assert.equal(initials(U, name), want, name);
  }
});

test('a name with no letter or digit gets a plain tile, never "?"', () => {
  const U = ui();
  for (const name of ['', '   ', '#', '— · —', '🎙🎧']) {
    const html = U.initialsTile(name, { size: 'lg' });
    assert.match(html, /^<span class="ew-tile ew-tile-lg ew-tint-\d" aria-hidden="true"><\/span>$/, name);
    assert.doesNotMatch(html, /\?/, name);
  }
  // The tint stays stable for a name.
  assert.equal(U.initialsTile('小Lin说'), U.initialsTile('小Lin说'));
});

/* ART-PERF-FIX3 APV-C-03: an initial is a whole code point, never half a
   surrogate pair (mutant O4, `w[0]`). */
test('a letter outside the Basic Multilingual Plane is a whole initial', () => {
  const U = ui();
  for (const [name, want] of Object.entries({
    '𠀋 Show': '𠀋S', '𝐓𝐡𝐞 𝐒𝐡𝐨𝐰': '𝐓𝐒', 'Show 𠀋': 'S𠀋',
  })) {
    assert.equal(initials(U, name), want, name);
  }
});

/* ART-PERF-FIX3 APV-C-08: two Arabic-script initials would join into what
   reads as a word ("پر"); U+200C ZERO WIDTH NON-JOINER keeps them apart.
   Only when both initials are Arabic-script letters; the tile is
   aria-hidden, so the joiner is never read, and the card's name is as
   given. */
test('two Arabic-script letters are kept apart by a zero width non-joiner', () => {
  const U = ui();
  for (const [name, want] of Object.entries({
    'پادکست رخ': 'پ\u200Cر', 'راديو مصر': 'ر\u200Cم', 'بودكاست': 'ب',
    'پادکست Show': 'پS', 'Show رخ': 'Sر', 'رادیو 7': 'ر7', '7 رخ': '7ر', 'ش ۷': 'ش۷',
  })) {
    assert.equal(initials(U, name), want, name);
  }
});

/* APF3-01: every script whose letters join (Syriac, N'Ko, Mongolian, Adlam,
   Mandaic) is kept apart the same way; Hebrew and Latin do not join and stay
   as they are. */
test('two letters of any joining script are kept apart; non-joining scripts are not', () => {
  const U = ui();
  for (const [name, want] of Object.entries({
    'ܟܬܒܐ ܚܕܬܐ': 'ܟ\u200Cܚ', 'ߒߞߏ ߛߓߍ': 'ߒ\u200Cߛ', 'ᠮᠣᠩᠭᠣᠯ ᠬᠡᠯᠡ': 'ᠮ\u200Cᠬ',
    '𞤀𞤣𞤤𞤢𞤥 𞤆𞤵𞤤𞤢𞤪': '𞤀\u200C𞤆', 'ࡌࡀࡍࡃࡀ ࡊࡕࡀࡁࡀ': 'ࡌ\u200Cࡊ',
    'ספר חדש': 'סח', 'ܟܬܒܐ Show': 'ܟS',
  })) {
    assert.equal(initials(U, name), want, name);
  }
});

test('Latin and other tiles stay byte-identical; the joiner stays inside the hidden tile', () => {
  const U = ui();
  for (const [name, want] of Object.entries({
    'The Long Run': 'LR', 'Радио Т': 'РТ', '小Lin说': '小', 'a16z Podcast': 'AP',
  })) {
    assert.match(U.initialsTile(name), new RegExp(`^<span class="ew-tile ew-tint-\\d" aria-hidden="true">${want}</span>$`), name);
  }
  const card = U.directoryCard({ id: 'cs-1', display_title: 'پادکست رخ', episodes: 3 }, { kind: 'show' });
  const hidden = card.match(/<span class="ew-tile[^"]*" aria-hidden="true">([^<]*)<\/span>/);
  assert.equal(hidden[1], 'پ\u200Cر');
  // The only joiner in the card is in the tile; the name a screen reader reads is unchanged.
  assert.equal(card.split('\u200C').length, 2, card);
  assert.match(card, />پادکست رخ<\/a>/);
});
