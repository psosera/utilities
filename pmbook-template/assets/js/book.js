/* book.js - dark mode, sidebar and table-of-contents behaviour.
 *
 * Everything here is progressive enhancement: with JavaScript disabled the
 * page still renders as a readable two-column book with the whole outline
 * expanded. Nothing below is required to read the text.
 */
(() => {
  'use strict';

  const root = document.documentElement;
  const NARROW = window.matchMedia('(max-width: 60rem)');

  /* ---- Persistence ------------------------------------------------------
     One namespace, and every read is guarded: Safari throws on localStorage
     in private mode rather than returning null. */

  const KEY = 'pmbook:';
  const store = {
    get(name, fallback = null) {
      try { const v = localStorage.getItem(KEY + name); return v === null ? fallback : v; }
      catch { return fallback; }
    },
    set(name, value) {
      try { localStorage.setItem(KEY + name, value); } catch { /* ignore */ }
    },
  };


  /* ---- Colour scheme (light / dark) -------------------------------------
     Three states. 'auto' removes the attribute entirely and lets the theme
     stylesheet's prefers-color-scheme block decide. */

  const themeToggle = document.getElementById('theme-toggle');

  function currentMode() {
    return root.dataset.theme
      || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }

  function applyMode(mode) {
    if (mode === 'auto') delete root.dataset.theme;
    else root.dataset.theme = mode;
    store.set('mode', mode);
    if (themeToggle) {
      const dark = currentMode() === 'dark';
      themeToggle.setAttribute('aria-label',
        dark ? 'Switch to light mode' : 'Switch to dark mode');
      themeToggle.setAttribute('title', themeToggle.getAttribute('aria-label'));
      themeToggle.setAttribute('aria-pressed', String(dark));
    }
  }

  themeToggle?.addEventListener('click', () => {
    applyMode(currentMode() === 'dark' ? 'light' : 'dark');
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && NARROW.matches && root.dataset.sidebar === 'open') {
      setSidebar(false);
    }
  });

  /* ---- Print ------------------------------------------------------------
     The print stylesheet strips the interface, so this prints the chapter
     rather than the window around it. */

  document.getElementById('print-button')
    ?.addEventListener('click', () => window.print());

  /* ---- Sidebar open / closed -------------------------------------------
     On a narrow screen the sidebar overlays the text, so it starts closed and
     shuts again after a link is followed. On a wide screen it displaces the
     text and the reader's last choice is remembered. */

  const sidebar = document.getElementById('sidebar');
  const sidebarToggle = document.getElementById('sidebar-toggle');
  const scrim = document.getElementById('scrim');

  function setSidebar(open, remember = true) {
    root.dataset.sidebar = open ? 'open' : 'closed';
    sidebarToggle?.setAttribute('aria-expanded', String(open));
    sidebar?.setAttribute('aria-hidden', String(!open));
    if (remember && !NARROW.matches) store.set('sidebar', open ? 'open' : 'closed');
  }

  sidebarToggle?.addEventListener('click', () => {
    setSidebar(root.dataset.sidebar !== 'open');
  });
  scrim?.addEventListener('click', () => setSidebar(false, false));

  sidebar?.addEventListener('click', (event) => {
    if (NARROW.matches && event.target.closest('.toc-link')) setSidebar(false, false);
  });

  NARROW.addEventListener('change', (event) => {
    setSidebar(event.matches ? false : store.get('sidebar', 'open') === 'open', false);
  });

  /* ---- Sidebar resizing ------------------------------------------------- */

  const handle = document.getElementById('sidebar-resize');

  function remPx() {
    return parseFloat(getComputedStyle(root).fontSize) || 16;
  }

  function limits() {
    const style = getComputedStyle(root);
    const rem = remPx();
    const read = (name, fallback) => {
      const raw = style.getPropertyValue(name).trim();
      const n = parseFloat(raw);
      if (Number.isNaN(n)) return fallback;
      return raw.endsWith('rem') ? n * rem : n;
    };
    return [read('--sidebar-min', 13 * rem), read('--sidebar-max', 32 * rem)];
  }

  function setWidth(px, remember = true) {
    const [min, max] = limits();
    const width = Math.min(Math.max(px, min), Math.min(max, window.innerWidth - 120));
    root.style.setProperty('--sidebar-width', `${Math.round(width)}px`);
    if (remember) store.set('sidebar-width', String(Math.round(width)));
  }

  if (handle) {
    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      root.dataset.resizing = '';
      const move = (e) => setWidth(e.clientX, false);
      const up = (e) => {
        handle.releasePointerCapture(event.pointerId);
        delete root.dataset.resizing;
        setWidth(e.clientX);
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });

    // Keyboard equivalent, so the handle is not mouse-only.
    handle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 48 : 16;
      const delta = { ArrowLeft: -step, ArrowRight: step }[event.key];
      if (delta === undefined) return;
      event.preventDefault();
      setWidth(sidebar.getBoundingClientRect().width + delta);
    });
  }

  /* ---- Table of contents -----------------------------------------------
     The build decides which sections start folded, from defaultFoldedLevel in
     book.yml. A reader's own toggles override that, so what is stored is not
     "the folded ones" but the choices they have actually made - in both
     directions, or reopening a section folded by default would not survive a
     reload. Anything they have never touched follows the book's default.

     Keyed by title path rather than position, so inserting a section
     elsewhere in SUMMARY.md does not shuffle everyone else's saved state. */

  const choices = (() => {
    try {
      const stored = JSON.parse(store.get('toc-folded', '{}'));
      return stored && typeof stored === 'object' ? stored : {};
    } catch { return {}; }
  })();

  function saveChoices() {
    store.set('toc-folded', JSON.stringify(choices));
  }

  function setExpanded(item, open, remember = true) {
    const button = item.querySelector(':scope > .toc-row > .toc-toggle');
    if (!button) return;
    button.setAttribute('aria-expanded', String(open));
    if (open) delete item.dataset.collapsed;
    else item.dataset.collapsed = '';
    const key = item.dataset.tocKey;
    if (remember && key) choices[key] = open ? 'open' : 'shut';
  }

  document.querySelectorAll('.toc-item[data-toc-key]').forEach((item) => {
    const choice = choices[item.dataset.tocKey];
    if (choice) setExpanded(item, choice === 'open', false);
  });

  document.querySelector('.toc')?.addEventListener('click', (event) => {
    const button = event.target.closest('.toc-toggle');
    if (!button) return;
    const item = button.closest('.toc-item');
    setExpanded(item, button.getAttribute('aria-expanded') !== 'true');
    saveChoices();
  });

  // The page being read must always be reachable, whatever was collapsed
  // before, so force its ancestors open without recording that as a choice.
  const active = document.querySelector('.toc-link[aria-current="page"]');
  if (active) {
    for (let item = active.closest('.toc-item')?.parentElement?.closest('.toc-item');
         item; item = item.parentElement?.closest('.toc-item')) {
      const button = item.querySelector(':scope > .toc-row > .toc-toggle');
      button?.setAttribute('aria-expanded', 'true');
      delete item.dataset.collapsed;
    }
    requestAnimationFrame(() => {
      const box = active.getBoundingClientRect();
      if (box.top < 0 || box.bottom > window.innerHeight) {
        active.scrollIntoView({ block: 'center' });
      }
    });
  }

  /* ---- Heading anchors -------------------------------------------------- */

  document.querySelectorAll('.content :is(h2, h3, h4, h5, h6)[id]').forEach((h) => {
    const a = document.createElement('a');
    a.className = 'heading-anchor';
    a.href = `#${h.id}`;
    a.textContent = '#';
    a.setAttribute('aria-label', `Permalink to "${h.textContent.trim()}"`);
    h.append(a);
  });

  /* ---- Startup ---------------------------------------------------------- */

  applyMode(store.get('mode', 'auto'));

  const savedWidth = Number(store.get('sidebar-width'));
  if (savedWidth > 0) setWidth(savedWidth, false);

  setSidebar(NARROW.matches ? false : store.get('sidebar', 'open') === 'open', false);

  // Let the first paint land before colour and layout transitions turn on.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.body.classList.remove('preload');
  }));
})();
