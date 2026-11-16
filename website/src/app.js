// Tuxistant frontpage: live theme shaders, OS-aware download, language and appearance switches,
// auto-hide header.
(() => {
  const S = JSON.parse(document.getElementById('strings').textContent);
  const OS_NAMES = { linux: 'Linux', macos: 'macOS', windows: 'Windows' };
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private window */ } },
  };

  function detectOS() {
    const ua = navigator.userAgent.toLowerCase();
    const platform = (navigator.userAgentData?.platform || navigator.platform || '').toLowerCase();
    if (/android|iphone|ipad|ipod/.test(ua)) return null;
    if (platform.includes('mac') || ua.includes('mac os')) return 'macos';
    if (platform.includes('win') || ua.includes('windows')) return 'windows';
    if (/linux|x11|cros/.test(platform + ua)) return 'linux';
    return null;
  }

  // Download button
  const fill = (text, values) => text.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? '');
  const os = detectOS();
  const shownOS = os || 'linux';
  const button = document.getElementById('download');
  const meta = document.getElementById('dl-meta-text');
  const release = S.releases[shownOS];
  const osName = OS_NAMES[shownOS];
  if (release.url) {
    button.textContent = fill(S.dlFor, { os: osName });
    button.href = release.url;
    button.removeAttribute('aria-disabled');
  } else {
    button.textContent = fill(release.version ? S.dlPending : S.dlSoon, { os: osName });
    button.removeAttribute('href');
    button.setAttribute('aria-disabled', 'true');
  }
  meta.textContent = release.version
    ? fill(S.dlMeta, { version: release.version, channel: S[release.channel] })
    : S.dlPlanned;

  // A section's download is the hero's file: the visitor's own system when that has a release, and
  // otherwise Linux, phones included. It names the system it downloads for, as the hero does.
  const ctaOS = S.releases[os]?.url ? os : 'linux';
  for (const link of document.querySelectorAll('[data-cta="download"]')) {
    link.href = S.releases[ctaOS].url;
    link.textContent = fill(S.dlFor, { os: OS_NAMES[ctaOS] });
  }

  // OS tabs
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  function selectTab(id, focus) {
    for (const tab of tabs) {
      const on = tab.dataset.os === id;
      tab.setAttribute('aria-selected', on);
      tab.tabIndex = on ? 0 : -1;
      document.getElementById(`panel-${tab.dataset.os}`).hidden = !on;
      if (on && focus) tab.focus();
    }
    examplesAsk(id);
  }
  // The examples end by asking about the system on show. Where it has a release: whether the
  // visitor wants it. Where it has none: that it is coming, and whether they have a desktop the
  // download beside it runs on. The Donate card just below stays the page's one donation ask.
  function examplesAsk(id) {
    const prompt = document.querySelector('#examples .acc-cta p');
    if (S.releases[id].url) {
      prompt.textContent = S.ctaExamples;
      return;
    }
    const sentences = [fill(S.ctaSoon, { os: OS_NAMES[id] })];
    if (S.releases[ctaOS].url) sentences.push(fill(S.ctaSoonAlso, { os: OS_NAMES[ctaOS] }));
    // A sentence to a span, so a narrow screen breaks between them rather than inside one.
    prompt.replaceChildren();
    sentences.forEach((sentence, index) => {
      // After a full-width stop, as Chinese ends a sentence with, no space.
      if (index && !/[\u3000-\u303f\uff00-\uffef]$/.test(sentences[index - 1])) prompt.append(' ');
      const span = document.createElement('span');
      span.textContent = sentence;
      prompt.append(span);
    });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.os));
    tab.addEventListener('keydown', event => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      const direction = document.dir === 'rtl' ? -step : step;
      selectTab(tabs[(index + direction + tabs.length) % tabs.length].dataset.os, true);
    });
  });
  selectTab(shownOS);

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

  // Language: the choice is remembered, so / stops following the browser.
  const language = document.getElementById('lang');
  language.addEventListener('change', () => {
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
  const isLight = () => (root.dataset.mode ? root.dataset.mode === 'light' : !systemDark.matches);
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

  // Headings never wrap: shrink to fit instead.
  function fitHeadings() {
    for (const heading of document.querySelectorAll('h1, h2')) {
      heading.style.fontSize = '';
      const room = heading.parentElement.clientWidth;
      const range = document.createRange();
      range.selectNodeContents(heading);
      const width = range.getBoundingClientRect().width;
      if (width > room) {
        const size = parseFloat(getComputedStyle(heading).fontSize);
        heading.style.fontSize = `${Math.floor(size * room / width * 10) / 10}px`;
      }
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

  // Hero: the real theme shaders fill the hero behind the title, drawn with the uniforms the
  // desktop uses. They take turns until the visitor picks one, and a theme keeps its turn while the
  // pointer or the keyboard is on the picker. The pause button there, or reduced motion, draws one
  // still frame. Without WebGL, or while the browser has taken it away, the picker switches a still
  // CSS drawing of each theme instead.
  function startHero() {
    const hero = document.getElementById('hero');
    const canvas = document.getElementById('tux');
    const copy = hero.querySelector('.hero-copy');
    const label = document.getElementById('theme-name');
    const pick = hero.querySelector('.pick');
    const dots = document.getElementById('dots');
    let programs = compileThemes(canvas);
    let themes = programs.list.length ? programs.list : window.TUX_THEMES;
    const gl = programs.list.length ? programs.gl : null;
    let lost = false;
    root.classList.add(gl ? 'has-gl' : 'no-gl');

    const SLIDE_S = 10;
    const FADE_S = 0.6;
    let current = 0;
    let slideStart = performance.now() / 1000;
    let manual = false;
    let paused = Boolean(gl) && !reducedMotion && store.get('still') === '1';
    const still = () => reducedMotion || paused;
    const redraw = () => { if (gl && !lost && still()) requestAnimationFrame(() => draw(performance.now())); };

    // Held: what the picker names doesn't change under the visitor's pointer or keyboard. Once
    // they leave, the theme has at least three more seconds. Only keyboard focus holds: Chromium
    // also focuses a button that is clicked or tapped, and that focus stays after the pointer
    // has gone, so play would start the motion but never the turns.
    const byKeyboard = element => { try { return element.matches(':focus-visible'); } catch { return true; } };
    let hovered = false;
    let focused = false;
    let heldAt = 0;        // when the hold began, or 0
    let heldFor = 0;       // seconds of this theme's turn spent held
    function hold() {
      const now = performance.now() / 1000;
      if ((hovered || focused) && !heldAt) heldAt = now;
      else if (!(hovered || focused) && heldAt) {
        heldFor += now - heldAt;
        heldAt = 0;
        heldFor += Math.max(0, now - slideStart - heldFor - (SLIDE_S - 3));
      }
    }
    pick.addEventListener('pointerenter', () => { hovered = true; hold(); });
    pick.addEventListener('pointerleave', () => { hovered = false; hold(); });
    pick.addEventListener('focusin', event => { focused = byKeyboard(event.target); hold(); });
    pick.addEventListener('focusout', event => { if (!pick.contains(event.relatedTarget)) focused = false; hold(); });

    themes.forEach((theme, index) => {
      const dot = document.createElement('button');
      dot.className = 'dot';
      dot.type = 'button';
      dot.setAttribute('aria-label', `${S.showTheme}: ${S.themes[theme.id]}`);
      dot.addEventListener('click', () => show(index, true));
      dots.append(dot);
    });
    // Pause stops the motion and the turns: the theme shows its still frame, as with reduced motion.
    // Its name stays put for a screen reader, which also hears pressed or not; the tooltip says what
    // a click does next.
    if (gl && !reducedMotion) {
      const pause = document.createElement('button');
      pause.className = 'still';
      pause.type = 'button';
      pause.setAttribute('aria-label', S.pauseMotion);
      const mark = () => {
        pause.setAttribute('aria-pressed', paused);
        pause.title = paused ? S.playMotion : S.pauseMotion;
      };
      mark();
      pause.addEventListener('click', () => {
        paused = !paused;
        mark();
        store.set('still', paused ? '1' : '0');
        if (paused) redraw();
        else {
          slideStart = performance.now() / 1000 - 1.5;     // on from the still frame's moment
          heldFor = 0;
          if (heldAt) heldAt = slideStart + 1.5;
          wake();
        }
      });
      pick.append(pause);
    }

    // A bar is a figure much wider than tall, such as the scanner; a band runs edge to edge.
    const isBar = theme => theme.kind !== 'band' && theme.aspect < .5;

    function show(index, byUser) {
      current = index;
      slideStart = performance.now() / 1000;
      heldFor = 0;
      if (heldAt) heldAt = slideStart;
      manual ||= Boolean(byUser);
      hero.dataset.theme = themes[index].id;
      if (!isBar(themes[index])) hero.classList.remove('ink-light');
      label.textContent = S.themes[themes[index].id];
      [...dots.children].forEach((dot, i) => dot.setAttribute('aria-pressed', i === index));
      standIn();
      redraw();
    }

    // Where the words sit in the hero, in CSS pixels from its top left corner, and how far down
    // a figure may reach: to the claims when they sit under the words, or the foot of the hero.
    // The roof is the header's foot, when the hero runs on up under it.
    const title = copy.querySelector('h1');
    const tagline = copy.querySelector('.tagline');
    const claims = hero.querySelector('.caps');
    let box = {};
    function measure() {
      const area = hero.getBoundingClientRect();
      const text = copy.getBoundingClientRect();
      const name = title.getBoundingClientRect();
      const word = document.createRange();     // the h1 is as wide as its column: this is its ink
      word.selectNodeContents(title);
      const below = claims.getBoundingClientRect().top >= text.bottom - 1;
      box = { width: area.width, height: area.height, below,
              x: text.left - area.left + text.width / 2, y: text.top - area.top + text.height / 2,
              copyWidth: text.width, copyHeight: text.height,
              titleTop: name.top - area.top, titleHeight: name.height, inkWidth: word.getBoundingClientRect().width,
              taglineTop: tagline.getBoundingClientRect().top - area.top,
              roof: Math.max(0, header.offsetHeight - area.top - scrollY),
              floor: (below ? claims.getBoundingClientRect().top : area.bottom) - area.top };
      standIn();
      redraw();
    }

    // A band runs edge to edge. Anything else is centred on the words and sized to the hero, so it
    // reads as a backdrop rather than a picture beside them.
    function place(theme) {
      const { width, height, x, y, copyWidth, copyHeight, below, titleTop, titleHeight, inkWidth,
              taglineTop, roof, floor } = box;
      // The ribbons ride at about 0.55 of the band's height: there they cross the words.
      if (theme.kind === 'band') return { x: 0, y: 0, width, height: Math.max(y / .55, copyHeight) };
      if (isBar(theme)) {
        // A bar, such as the scanner: behind the name, and clear of everything else. Beside the
        // claims it keeps to the middle column and 12 px of each 20 px column gap of .hero-in,
        // and on a phone to the 16 px gutter either side. Its foot stays above the tagline and
        // its top below the header: where there isn't room for both, it gets smaller, though
        // never shorter than the name.
        const foot = taglineTop - 4;
        const w = Math.min(width - 32, Math.max(inkWidth * 2.3, 320), below ? 640 : copyWidth + 24,
                           Math.max(foot - roof - 4, titleHeight + 12) / theme.aspect);
        const h = w * theme.aspect;
        return { x: x - w / 2, y: Math.min(titleTop + titleHeight / 2 - h / 2, foot - h), width: w, height: h };
      }
      // A figure: the top third of it, where Tux's eyes are (about 0.36 down to their lower edge),
      // peeks out over the name, and its feet stay above the claims.
      const top = titleTop + titleHeight * .25;
      const side = Math.min(Math.max(copyHeight * 1.3, Math.min(width, 640) * .82), height * 1.15,
                            (floor + 10 - top) / .61);
      return { x: x - side / 2, y: top - side * .36, width: side, height: side * theme.aspect };
    }

    // Without WebGL the CSS drawing of the theme takes the place its shader would.
    function standIn() {
      if ((gl && !lost) || !box.width) return;
      const spot = place(themes[current]);
      for (const [key, value] of [['left', spot.x], ['top', spot.y], ['width', spot.width], ['height', spot.height]])
        hero.style.setProperty(`--actor-${key}`, `${Math.round(value)}px`);
    }

    const watch = new ResizeObserver(measure);
    watch.observe(hero);
    watch.observe(copy);
    measure();
    show(0);
    if (!gl) return;

    // Few enough pixels that a phone stays cool: at most 1.5 per CSS pixel, and a budget for the
    // whole canvas on a big screen.
    function resize() {
      const area = canvas.clientWidth * canvas.clientHeight || 1;
      const ratio = Math.min(devicePixelRatio || 1, 1.5, Math.sqrt(1.6e6 / area));
      const width = Math.round(canvas.clientWidth * ratio);
      const height = Math.round(canvas.clientHeight * ratio);
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      return ratio;
    }

    // A scripted turn: listen, think, speak, with a level shaped like speech. It hands back to
    // listening before it starts again, so a theme the visitor keeps loops without a jump. A still
    // frame is a fixed moment of listening, as the label says, at a time when Tux's eyes are open:
    // his blinks run on the clock.
    const band = (t, from, to) => Math.min(1, Math.max(0, Math.min((t - from) / .6, (to - t) / .6)));
    function draw(nowMs) {
      if (lost) return;
      const ratio = resize();
      const now = nowMs / 1000;
      const t = still() ? 2 : now;
      const age = still() ? 1.5 : now - slideStart;
      const turn = age - heldFor - (heldAt ? now - heldAt : 0);    // how much of its turn it has had
      if (!still() && !manual && turn > SLIDE_S) {
        show((current + 1) % themes.length);
        return draw(nowMs);
      }
      const theme = themes[current];
      const phase = age % SLIDE_S;
      const speech = Math.abs(Math.sin(t * 5.1) * Math.sin(t * 1.7 + 1)) * .75 + .1;
      const listen = Math.max(band(phase, -1, 3), band(phase, SLIDE_S - .9, SLIDE_S + 3));
      const think = band(phase, 2.6, 4.2);
      const speak = band(phase, 3.8, SLIDE_S - .3);

      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const spot = place(theme);
      const width = Math.round(spot.width * ratio);
      const height = Math.round(spot.height * ratio);
      gl.viewport(Math.round(spot.x * ratio), canvas.height - Math.round(spot.y * ratio) - height,
                  width, height);
      gl.useProgram(theme.program);
      gl.bindBuffer(gl.ARRAY_BUFFER, programs.buffer);
      gl.enableVertexAttribArray(theme.position);
      gl.vertexAttribPointer(theme.position, 2, gl.FLOAT, false, 0, 0);
      const u = theme.uniforms;
      const fade = manual || still() || heldAt ? Math.min(1, age / FADE_S || 1)
        : Math.min(1, age / FADE_S, (SLIDE_S - turn) / FADE_S);
      // The name is light while a bar is mostly there, so it turns with the bar's fade rather than
      // a moment after the next theme has begun.
      hero.classList.toggle('ink-light', isBar(theme) && fade > .5);
      gl.uniform1f(u.u_time, t % 1000);
      gl.uniform1f(u.u_level, (listen + speak > 0.05 ? speech : 0.1) * (still() ? .6 : 1));
      gl.uniform1f(u.u_appear, still() ? 1 : Math.max(0, fade));
      gl.uniform1f(u.u_listen, listen);
      gl.uniform1f(u.u_think, think);
      gl.uniform1f(u.u_speak, speak);
      gl.uniform1f(u.u_ink, theme.kind === 'band' && isLight() ? 1 : 0);
      gl.uniform1f(u.u_band, theme.kind === 'band' ? 1 : 0);
      gl.uniform2f(u.u_size, width, height);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    // Drawing stops while nothing it draws is on screen (on a phone, once the visitor reads the
    // claims under the words), while the tab is hidden, and while it stands still.
    let visible = true;
    let seenFrom = 0;      // how far down the hero the screen begins
    let frame = 0;
    function onScreen() {
      const spot = place(themes[current]);
      return visible && !document.hidden && seenFrom < spot.y + spot.height;
    }
    function loop(time) {
      frame = 0;
      if (lost || still() || !onScreen()) return;
      draw(time);
      frame = requestAnimationFrame(loop);
    }
    function wake() {
      if (!lost && !still() && !frame && onScreen()) frame = requestAnimationFrame(loop);
    }
    new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      seenFrom = entry.intersectionRect.top - entry.boundingClientRect.top;
      wake();
    }, { threshold: Array.from({ length: 21 }, (_, i) => i / 20) }).observe(hero);
    document.addEventListener('visibilitychange', wake);
    appearance.addEventListener('change', redraw);
    systemDark.addEventListener('change', redraw);

    // A phone may take the context away while the tab is in the background. The still drawings
    // stand in until the browser gives it back, and then the programs are built again.
    canvas.addEventListener('webglcontextlost', event => {
      event.preventDefault();      // without this the browser never gives it back
      lost = true;
      root.classList.replace('has-gl', 'no-gl');
      standIn();
    });
    canvas.addEventListener('webglcontextrestored', () => {
      const again = compileThemes(canvas);
      if (again.list.length !== themes.length) return;
      programs = again;
      themes = again.list;
      lost = false;
      root.classList.replace('no-gl', 'has-gl');
      redraw();
      wake();
    });
    redraw();
    wake();
  }

  // Every theme compiled for WebGL1, with the footer that turns a band into ink on a light page.
  // An empty list means the CSS drawings stand in.
  function compileThemes(canvas) {
    const none = { list: [] };
    const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false });
    if (!gl) return none;
    const VERTEX = `attribute vec2 p; varying vec2 v;
      void main() { v = vec2(p.x * .5 + .5, .5 - p.y * .5); gl_Position = vec4(p, 0., 1.); }`;
    const HEADER = `precision highp float; varying vec2 v;
      uniform float u_time, u_level, u_appear, u_listen, u_think, u_speak, u_ink, u_band; uniform vec2 u_size;\n`;
    // Waves are added light, which a light page can't show. There they become ink: dense at the
    // crests, which are white on a dark page. A band fades out before its foot in either mode.
    const FOOTER = `\nvoid main() {
      vec4 c = tux_theme(v);
      vec3 hue = c.rgb / max(c.a, .001);
      vec3 ink = mix(hue * .6, vec3(.05, .2, .62), smoothstep(.45, .85, hue.r));
      float a = min(1., c.a * (1. + 4. * c.a));
      gl_FragColor = mix(c, vec4(ink * a, a), u_ink) * u_appear * (1. - u_band * smoothstep(.8, 1., v.y));
    }`;
    const compile = (type, source) => {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
      console.warn(gl.getShaderInfoLog(shader));
      return null;
    };
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const vertex = compile(gl.VERTEX_SHADER, VERTEX);
    if (!vertex) return none;
    const list = window.TUX_THEMES.flatMap(theme => {
      const fragment = compile(gl.FRAGMENT_SHADER, HEADER + theme.src + FOOTER);
      if (!fragment) return [];
      const program = gl.createProgram();
      gl.attachShader(program, vertex);
      gl.attachShader(program, fragment);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return [];
      const uniforms = {};
      for (const name of ['u_time', 'u_level', 'u_appear', 'u_listen', 'u_think', 'u_speak', 'u_ink', 'u_band', 'u_size'])
        uniforms[name] = gl.getUniformLocation(program, name);
      return [{ ...theme, program, position: gl.getAttribLocation(program, 'p'), uniforms }];
    });
    return { gl, buffer, list };
  }

  startHero();
})();
