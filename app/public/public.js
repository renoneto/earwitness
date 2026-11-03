/* EarWitness public menu and phone doc-nav. Same-origin self script only; no fetches, no customer boot. */
(function () {
  "use strict";
  var button = document.querySelector("[data-menu-button]");
  var nav = document.querySelector("[data-site-nav]");
  if (!button || !nav) { return; }

  function setOpen(open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      nav.setAttribute("data-open", "true");
    } else {
      nav.removeAttribute("data-open");
    }
  }

  function isOpen() {
    return button.getAttribute("aria-expanded") === "true";
  }

  button.addEventListener("click", function () {
    setOpen(!isOpen());
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      setOpen(false);
      button.focus();
    }
  });

  nav.addEventListener("click", function (event) {
    var anchor = event.target.closest ? event.target.closest("a") : null;
    if (!anchor) { return; }
    // Modified / middle clicks stay native: close without intercepting.
    setOpen(false);
  });
})();

/* Phone doc-nav (DPM-R2-02): one sideways-scrolling row. On load, scroll the
   row (never the page) so the current page's item is fully visible, and so
   the row's left edge lands on an item edge: no item is cut at the left
   (PUB-48b row 55). Without this script the row is still a plain scrollable row. */
(function () {
  "use strict";
  var current = document.querySelector('.doc-nav a[aria-current="page"], .doc-nav a.is-current');
  var row = current && current.closest ? current.closest("ul") : null;
  if (!row) { return; }

  var added = 0; // end padding this script added, in px

  function place() {
    if (added) { row.style.paddingRight = ""; added = 0; }
    var box = row.getBoundingClientRect();
    var item = current.getBoundingClientRect();
    // Positions in the row's own scroll coordinates.
    var right = item.right - box.left + row.scrollLeft;
    var left = item.left - box.left + row.scrollLeft;
    // Always set the position: after a resize the browser may have clamped it
    // off an item edge. If the current item fits from the start, start at 0.
    if (row.scrollWidth <= row.clientWidth || right <= row.clientWidth) { row.scrollLeft = 0; return; }
    // The first item edge that still shows the whole current item, never past its own left edge.
    var target = left;
    var items = row.children || [];
    for (var i = 0; i < items.length; i += 1) {
      var edge = items[i].getBoundingClientRect().left - box.left + row.scrollLeft;
      if (edge >= right - row.clientWidth) { target = Math.min(edge, left); break; }
    }
    // Near the row's end the edge may sit past the last scroll position: add
    // just enough end padding to reach it.
    var short = target - (row.scrollWidth - row.clientWidth);
    if (short > 0 && row.style) {
      var css = typeof window !== "undefined" && window.getComputedStyle ? window.getComputedStyle(row) : null;
      var pad = css ? parseFloat(css.paddingRight) || 0 : 0;
      row.style.paddingRight = (pad + short) + "px";
      added = short;
    }
    row.scrollLeft = target;
  }

  // Measure after the web fonts load: the fallback font gives other widths.
  if (document.fonts && document.fonts.ready) { document.fonts.ready.then(place); } else { place(); }

  // Place again when the row's width changes (rotation, window resize), not on
  // height-only resizes such as a phone's address bar showing or hiding.
  var width = row.clientWidth, timer = null;
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("resize", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (row.clientWidth === width) { return; }
        width = row.clientWidth;
        place();
      }, 150);
    });
  }
})();
