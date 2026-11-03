'use strict';
// Approved wording pins live in one file that node gates and the server both read:
// earwitness/app/public_rows/public_rows.json (App owns it). This module keeps the
// old exports so existing gates (Ask's release test, legal pages) work unchanged.
const path = require('path');
const rows = require(path.join(__dirname, '..', 'earwitness', 'app', 'public_rows', 'public_rows.json'));
const ASK_PUBLIC_ROWS = rows.ask_public;
const ASK_INAPP_ROWS = rows.ask_inapp;
const MEANING_ALERT_ROWS = rows.meaning_alerts;
module.exports = { ASK_PUBLIC_ROWS, ASK_INAPP_ROWS, MEANING_ALERT_ROWS };
