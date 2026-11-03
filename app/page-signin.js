/* Redesigned Sign-in page (the hosted signed-out gate). REDESIGN-PAGE signin; build source
   docs/landing/build-spec/signin/SPEC.md (aaecbd611 on c9ea045ca); plan ws-app/logs/redesign/PLAN.md.
   Owner: the Sign-in page builder (this file, app/signin-b2.css, tests/frontend_b2_signin.test.cjs).
   Route switch: ON. false keeps today's page; true draws this one.
   render(...args, old): args are today's page function's arguments; old is
   today's page function (views.js accessSection).

   Today's accessSection keeps every sign-in rule: it reads the session, adopts
   or drops it, and picks the signed-out state (password form, provider button,
   or "not set up"). This page runs it first, and only for its signed-out card
   draws the redesigned page around the same parts: the form keeps its id,
   field ids and names, so views.js submitPasswordSignin still sends it, words
   and all. Anything else (Settings' embedded section, the workspace chooser,
   an error block) is today's markup. */
(() => {
  'use strict';
  const ON = true;
  const E = () => window.EW;
  const esc = v => E().esc(v);
  const support = () => window.EWViews._b2.SUPPORT_CONTACT;

  // The landing page's static covers (app/public/img; MANIFEST.json names, cut at the
  // subtitle), in the reference's order: 9 at 1440, the first 6 on the phone.
  const COVERS = [
    ['the-breakfast-club', 'The Breakfast Club'], ['pivot', 'Pivot'], ['the-vergecast', 'The Vergecast'],
    ['behind-the-bastards', 'Behind the Bastards'], ['club-shay-shay', 'Club Shay Shay'], ['tbpn', 'TBPN'],
    ['financial-audit', 'Financial Audit'], ['the-power-trip', 'The Power Trip'],
    ['the-ai-daily-brief-artificial-intelligence-news-and-analysis', 'The AI Daily Brief'],
  ];
  const H1 = 'Podcast ads and mentions, quoted to the second.';
  // No show count, for launch and after (spec C1, 9a1460a1f): a signed-out page has no
  // customer API, and the landing page's copy is not a source. The line is complete without it.
  const CAPTION = 'Some of the shows we check for ads.';
  const WRONG = 'Email or password is incorrect.';

  const mailto = (subject, body) => `mailto:${support()}?subject=${encodeURIComponent(subject)}${body ? '&body=' + encodeURIComponent(body) : ''}`;
  const FORGOT = () => mailto('New EarWitness password', 'Hi, please send me a new password for EarWitness. My login email is: ');
  const ACCESS = () => mailto('EarWitness access');

  // The parts of today's signed-out card this page keeps as they are.
  function parts(html) {
    const s = String(html || '');
    if (!s.includes('<section class="surface pad signin-card">')) return null;
    const note = (/<p class="section-note signin-signed-out" role="status">[^<]*<\/p>/.exec(s) || [''])[0];
    const password = s.includes('<form id="signin-password"');
    const provider = (/<div class="actions"><button [^>]*data-action="signin-start"[^>]*>[^<]*<\/button><\/div><div id="access-errors" role="alert"><\/div>/.exec(s) || [''])[0];
    const off = (/<p class="section-note">Sign-in isn't set up[\s\S]*?<\/details>/.exec(s) || [''])[0];
    if (!password && !provider && !off) return null;
    return { note, password, provider, off };
  }

  function form(p, phone) {
    const m = phone ? '-m' : '';
    const head = `<div class="si-card__head${m}"><span class="si-card__eyebrow">Invite-only pilot</span><h2 class="si-card__title${m}">Sign in</h2></div>`;
    const forgot = `<p class="si-help">Forgot your password? <a class="si-link" href="${esc(FORGOT())}">Email us for a new one.</a></p>`;
    const access = `<p class="si-help si-help--access${m}">No login yet? <a class="si-link" href="${esc(ACCESS())}">Ask for access</a></p>`;
    // Provider only: no password to forget. Not set up: today's words already say who to ask.
    if (!p.password) return `<div class="si-card${m}">${head}${p.note}${p.provider || p.off}${p.off ? '' : access}</div>`;
    return `<form id="signin-password" class="si-card${m}" novalidate>${head}${p.note}`
      + '<label class="si-field" for="signin-email">Email<input class="si-input" id="signin-email" name="email" type="email" autocomplete="username" autocapitalize="none" spellcheck="false" placeholder="you@company.com" required></label>'
      + '<label class="si-field" for="signin-password-input">Password<input class="si-input" id="signin-password-input" name="password" type="password" autocomplete="current-password" required></label>'
      + `<div class="si-alert${m}" id="signin-errors" data-form-errors role="alert"></div>`
      + `<button class="si-submit${m}" type="submit">Sign in</button>`
      + forgot + access + p.provider + '</form>';
  }
  const covers = (n, m) => COVERS.slice(0, n).map(([file, name]) => `<div class="si-cover${m}">`
    + `<img class="si-cover__img${m}" src="/public/img/cover-${esc(file)}.jpg" alt=""><span class="si-cover__name${m}">${esc(name)}</span></div>`).join('');
  const legal = m => `<p class="si-legal${m}"><a class="si-legal__link${m}" href="/privacy/">Privacy</a><span aria-hidden="true">·</span><a class="si-legal__link${m}" href="/terms/">Terms</a></p>`;

  function page(p) {
    if (window.EWB2.layout() === 'phone') {
      return '<div class="si-page si-page--phone si-page--fill si-frame-m">'
        + '<header class="s-nav s-nav--phone"><div class="s-nav__brand-m">EarWitness</div><a class="si-nav__back-m" href="/">← Home</a></header>'
        + `<div class="si-main-m"><h1 class="si-h1-m">${esc(H1)}</h1>${form(p, true)}`
        + `<div class="si-covers-m">${covers(6, '-m')}</div><p class="si-covers__cap-m">${esc(CAPTION)}</p>${legal('-m')}</div></div>`;
    }
    return '<div class="si-page si-page--fill si-frame">'
      + '<header class="si-nav"><div class="si-nav__brand">EarWitness</div><a class="si-nav__back" href="/">← Back to home</a></header>'
      + '<main class="si-main"><div class="si-intro"><div class="si-intro__head">'
      + `<span class="si-eyebrow">For brands and agencies that buy podcast ads</span><h1 class="si-h1">${esc(H1)}</h1></div>${form(p, false)}</div>`
      + `<div class="s-stack"><div class="si-covers">${covers(9, '')}</div><p class="si-covers__cap">${esc(CAPTION)}</p></div></main>`
      + `${legal('')}</div>`;
  }

  // The form's states, from what today's code sets on it: aria-busy while signing in
  // (fields disabled, F4; the values are already read), and the wrong-password words
  // (both fields marked, R2; views.js formError clears every mark before a new message).
  function sync(f) {
    const busy = f.getAttribute('aria-busy') === 'true';
    const fields = ['#signin-email', '#signin-password-input'].map(s => f.querySelector(s)).filter(Boolean);
    for (const el of fields) el.disabled = busy;
    const box = f.querySelector('#signin-errors');
    if (box && String(box.textContent || '').includes(WRONG)) for (const el of fields) el.setAttribute('aria-invalid', 'true');
  }
  function watch() {
    const f = typeof document !== 'undefined' && document.querySelector('#signin-password.si-card, #signin-password.si-card-m');
    if (!f || typeof MutationObserver !== 'function') return;
    new MutationObserver(() => sync(f)).observe(f, { subtree: true, childList: true, attributes: true, attributeFilter: ['aria-busy'] });
  }

  async function render(...args) {
    const old = args.pop();
    const opts = args[0];
    if (!(opts && opts.page)) return old(...args);
    const html = await old(...args);
    const p = !E().current.principal && parts(html);
    if (!p) return html;
    setTimeout(watch, 0);
    return page(p);
  }

  window.EWSignin = Object.freeze({ render, sync });
  window.EWB2.page('signin', { on: ON, render });
})();
