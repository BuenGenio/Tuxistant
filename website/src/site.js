// Every page: the folding sections, the language and appearance switches, headings that shrink
// rather than wrap, and the header that hides on the way down and comes back on the way up.
(() => {
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private window */ } },
  };

  // Sections: accordions. The page arrives with every section open and its heading plain text,
  // which is how it stays without script. Here each heading's text becomes the toggle, a button
  // inside the h2 so the outline keeps it; the first section stays open, the rest close, and a link
  // into one opens it. A closed panel is hidden until found where the browser can do that, so find
  // in page still reaches its text.
  for (const heading of document.querySelectorAll('.acc-head h2')) {
    const section = heading.closest('.acc');
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'acc-toggle';
    toggle.id = `${section.id}-toggle`;
    toggle.setAttribute('aria-controls', section.querySelector('.acc-panel').id);
    toggle.setAttribute('aria-expanded', 'true');
    toggle.append(...heading.childNodes);
    heading.append(toggle);
  }
  const HIDDEN = 'onbeforematch' in document.body ? 'until-found' : true;
  const FOLD_MS = 300;    // a little longer than the 280 ms fold in style.css
  const folding = new WeakMap();
  const toggleOf = section => section.querySelector('.acc-toggle');
  const panelOf = section => document.getElementById(toggleOf(section).getAttribute('aria-controls'));
  const isOpen = section => toggleOf(section).getAttribute('aria-expanded') === 'true';
  function setOpen(section, open, animate = !reducedMotion) {
    const panel = panelOf(section);
    clearTimeout(folding.get(section));
    toggleOf(section).setAttribute('aria-expanded', open);
    section.classList.toggle('acc-instant', !animate);
    if (open) {
      panel.hidden = false;
      if (animate) void panel.offsetHeight;     // unfold from the closed height, not from nothing
      section.classList.remove('is-closed');
    } else {
      // Safari doesn't focus a clicked button, so focus could be left inside the folded panel.
      if (panel.contains(document.activeElement)) toggleOf(section).focus();
      section.classList.add('is-closed');
      if (animate) folding.set(section, setTimeout(() => { panel.hidden = HIDDEN; }, FOLD_MS));
      else panel.hidden = HIDDEN;
    }
  }
  // Opens the section a link points into and returns its target. It opens at once, not unfolding:
  // with the panels below still folded the page is too short to scroll the target to the top.
  function reveal(hash) {
    let target = null;
    try { target = hash.length > 1 && document.getElementById(decodeURIComponent(hash.slice(1))); }
    catch { /* a malformed hash names nothing */ }
    const section = target && target.closest('.acc');
    if (!section || isOpen(section)) return null;
    setOpen(section, true, false);
    return target;
  }
  const accordions = [...document.querySelectorAll('.acc')];
  accordions.forEach((section, index) => {
    toggleOf(section).addEventListener('click', () => setOpen(section, !isOpen(section)));
    panelOf(section).addEventListener('beforematch', () => setOpen(section, true, false));
    if (index) setOpen(section, false, false);
  });
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href^="#"]');
    if (link) reveal(link.hash);      // before the browser follows it, so it lands on open text
  });
  addEventListener('hashchange', () => reveal(location.hash)?.scrollIntoView());
  reveal(location.hash)?.scrollIntoView();
  document.documentElement.classList.add('has-acc');

  // Language: the choice is remembered, so / stops following the browser. The portal is in English
  // and has no menu.
  const language = document.getElementById('lang');
  language?.addEventListener('change', () => {
    store.set('lang', language.value);
    const root = language.dataset.root;     // relative, as the site can live under a folder
    location.href = language.value === 'en' ? root || './' : `${root}${language.value}/`;
  });

  // Appearance: the page head already applied a stored choice. Auto sets nothing, because the
  // stylesheet follows the system by itself.
  const root = document.documentElement;
  const systemDark = matchMedia('(prefers-color-scheme: dark)');
  const appearance = document.getElementById('mode');
  const browserBar = document.querySelector('meta[name="theme-color"]');
  const tintBrowserBar = () => { browserBar.content = getComputedStyle(document.body).backgroundColor; };
  appearance.value = root.dataset.mode || 'auto';
  appearance.addEventListener('change', () => {
    if (appearance.value === 'auto') delete root.dataset.mode;
    else root.dataset.mode = appearance.value;
    store.set('mode', appearance.value);
    tintBrowserBar();
  });
  systemDark.addEventListener('change', tintBrowserBar);
  tintBrowserBar();

  // Headings never wrap: shrink to fit the parent's content box instead. A skill's name on the
  // portal can be 100 characters, so a heading marked .fit stops shrinking at 14 px, the
  // stylesheet cuts the rest with an ellipsis, and is-cut shows the name in full under it.
  function fitHeadings() {
    for (const heading of document.querySelectorAll('h1, h2')) {
      heading.style.fontSize = '';
      const box = getComputedStyle(heading.parentElement);
      const padding = (parseFloat(box.paddingLeft) || 0) + (parseFloat(box.paddingRight) || 0);
      const room = heading.parentElement.clientWidth - padding;
      const range = document.createRange();
      range.selectNodeContents(heading);
      const width = range.getBoundingClientRect().width;
      const fit = heading.classList.contains('fit');
      if (width > room && room > 0) {
        const size = parseFloat(getComputedStyle(heading).fontSize);
        const fitted = Math.floor(size * room / width * 10) / 10;
        heading.style.fontSize = `${fit ? Math.max(14, fitted) : fitted}px`;
      }
      if (fit) heading.classList.toggle('is-cut', range.getBoundingClientRect().width > room + 0.5);
    }
  }
  fitHeadings();
  addEventListener('resize', fitHeadings, { passive: true });
  document.fonts?.ready.then(fitHeadings);

  // Auto-hide header: it rolls away as the page scrolls down and comes back on the first scroll
  // up. It stays near the top, and for a moment after every jump (the first load, a link to a
  // section, back and forward), so a jump never hides it. It has no menu of its own: the
  // language and mode lists are the browser's, and nothing scrolls under them.
  // The scroll a jump makes is only reported with the next frame, which a busy phone, still
  // compiling the hero's shaders, can hold back past the grace. So the first scroll within 3 s of a
  // jump counts as the jump, and the grace starts again from there.
  const header = document.getElementById('site-header');
  let lastY = scrollY;
  let graceUntil = performance.now() + 900;
  let jumpUntil = performance.now() + 3000;
  const showHeader = () => header.classList.remove('is-hidden');
  const navigated = () => {
    graceUntil = performance.now() + 900;
    jumpUntil = performance.now() + 3000;
    showHeader();
  };
  addEventListener('scroll', () => {
    const y = scrollY;
    const delta = y - lastY;
    lastY = y;
    if (performance.now() < jumpUntil) {
      jumpUntil = 0;
      graceUntil = Math.max(graceUntil, performance.now() + 900);
    }
    if (y < 96 || performance.now() < graceUntil) showHeader();
    else if (delta > 6) header.classList.add('is-hidden');
    else if (delta < -6) showHeader();
  }, { passive: true });
  addEventListener('hashchange', navigated);
  addEventListener('popstate', navigated);
  addEventListener('pageshow', navigated);     // also back to this page from the cache
  // Following an in-page link scrolls before hashchange fires, so its grace starts on the click,
  // and the same section again fires no hashchange at all.
  document.addEventListener('click', event => {
    if (event.target.closest?.('a[href^="#"]')) navigated();
  }, true);
  header.addEventListener('focusin', showHeader);
})();
