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
  const header = document.getElementById('site-header');
  let lastY = scrollY;
  let graceUntil = performance.now() + 900;
  const showHeader = () => header.classList.remove('is-hidden');
  const navigated = () => { graceUntil = performance.now() + 900; showHeader(); };
  addEventListener('scroll', () => {
    const y = scrollY;
    const delta = y - lastY;
    lastY = y;
    if (y < 96 || performance.now() < graceUntil) showHeader();
    else if (delta > 6) header.classList.add('is-hidden');
    else if (delta < -6) showHeader();
  }, { passive: true });
  addEventListener('hashchange', navigated);
  addEventListener('popstate', navigated);
  addEventListener('pageshow', navigated);     // also back to this page from the cache
  document.addEventListener('click', event => {
    if (event.target.closest?.('a[href^="#"]')) navigated();     // the same section again: no hashchange
  });
  header.addEventListener('focusin', showHeader);

  // Hero: the real theme shaders fill the hero behind the title, drawn with the uniforms the
  // desktop uses. They take turns until the visitor picks one. Without WebGL the picker switches
  // a still CSS drawing of each theme instead, and with reduced motion it draws one still frame.
  function startHero() {
    const hero = document.getElementById('hero');
    const canvas = document.getElementById('tux');
    const copy = hero.querySelector('.hero-copy');
    const label = document.getElementById('theme-name');
    const dots = document.getElementById('dots');
    const programs = compileThemes(canvas);
    const themes = programs.list.length ? programs.list : window.TUX_THEMES;
    const gl = programs.list.length ? programs.gl : null;
    if (gl) root.classList.add('has-gl');

    const SLIDE_S = 10;
    const FADE_S = 0.6;
    let current = 0;
    let slideStart = performance.now() / 1000;
    let manual = false;

    themes.forEach((theme, index) => {
      const dot = document.createElement('button');
      dot.className = 'dot';
      dot.type = 'button';
      dot.setAttribute('aria-label', `${S.showTheme}: ${S.themes[theme.id]}`);
      dot.addEventListener('click', () => show(index, true));
      dots.append(dot);
    });

    function show(index, byUser) {
      current = index;
      slideStart = performance.now() / 1000;
      manual ||= Boolean(byUser);
      hero.dataset.theme = themes[index].id;
      label.textContent = S.themes[themes[index].id];
      [...dots.children].forEach((dot, i) => dot.setAttribute('aria-pressed', i === index));
      standIn();
      if (gl && reducedMotion) requestAnimationFrame(() => draw(performance.now()));
    }

    // Where the words sit in the hero, in CSS pixels from its top left corner, and how far down
    // a figure may reach: to the claims when they sit under the words, or the foot of the hero.
    const title = copy.querySelector('h1');
    const claims = hero.querySelector('.caps');
    let box = {};
    function measure() {
      const area = hero.getBoundingClientRect();
      const text = copy.getBoundingClientRect();
      const name = title.getBoundingClientRect();
      const below = claims.getBoundingClientRect().top >= text.bottom - 1;
      box = { width: area.width, height: area.height,
              x: text.left - area.left + text.width / 2, y: text.top - area.top + text.height / 2,
              copyWidth: text.width, copyHeight: text.height,
              titleTop: name.top - area.top, titleHeight: name.height, titleWidth: name.width,
              floor: (below ? claims.getBoundingClientRect().top : area.bottom) - area.top };
      standIn();
      if (gl && reducedMotion) requestAnimationFrame(() => draw(performance.now()));
    }

    // A band runs edge to edge. Anything else is centred on the words and sized to the hero, so it
    // reads as a backdrop rather than a picture beside them.
    function place(theme) {
      const { width, height, x, y, copyWidth, copyHeight, titleTop, titleHeight, titleWidth, floor } = box;
      // The ribbons ride at about 0.55 of the band's height: there they cross the words.
      if (theme.kind === 'band') return { x: 0, y: 0, width, height: Math.max(y / .55, copyHeight) };
      if (theme.aspect < .5) {      // a bar, such as the scanner: right behind the name
        const w = Math.min(width * .96, Math.max(titleWidth * 2.3, copyWidth * 1.1, 320), 640);
        const h = w * theme.aspect;
        return { x: x - w / 2, y: titleTop + titleHeight / 2 - h / 2, width: w, height: h };
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
      if (gl || !box.width) return;
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
    // listening before it starts again, so a theme the visitor keeps loops without a jump.
    const band = (t, from, to) => Math.min(1, Math.max(0, Math.min((t - from) / .6, (to - t) / .6)));
    function draw(nowMs) {
      const ratio = resize();
      const now = nowMs / 1000;
      const age = reducedMotion ? SLIDE_S / 2 : now - slideStart;
      if (!reducedMotion && age > SLIDE_S && !manual) {
        show((current + 1) % themes.length);
        return draw(nowMs);
      }
      const theme = themes[current];
      const phase = age % SLIDE_S;
      const speech = Math.abs(Math.sin(now * 5.1) * Math.sin(now * 1.7 + 1)) * .75 + .1;
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
      const fade = manual || reducedMotion ? Math.min(1, age / FADE_S || 1)
        : Math.min(1, age / FADE_S, (SLIDE_S - age) / FADE_S);
      gl.uniform1f(u.u_time, now % 1000);
      gl.uniform1f(u.u_level, (listen + speak > 0.05 ? speech : 0.1) * (reducedMotion ? .6 : 1));
      gl.uniform1f(u.u_appear, reducedMotion ? 1 : Math.max(0, fade));
      gl.uniform1f(u.u_listen, listen);
      gl.uniform1f(u.u_think, think);
      gl.uniform1f(u.u_speak, speak);
      gl.uniform1f(u.u_ink, theme.kind === 'band' && isLight() ? 1 : 0);
      gl.uniform1f(u.u_band, theme.kind === 'band' ? 1 : 0);
      gl.uniform2f(u.u_size, width, height);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    // Drawing stops while the hero is off screen or the tab is hidden.
    let visible = true;
    let frame = 0;
    function loop(time) {
      frame = 0;
      if (!visible || document.hidden) return;
      draw(time);
      frame = requestAnimationFrame(loop);
    }
    function wake() {
      if (!reducedMotion && !frame && visible && !document.hidden) frame = requestAnimationFrame(loop);
    }
    new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; wake(); }).observe(hero);
    document.addEventListener('visibilitychange', wake);
    if (reducedMotion) {
      const redraw = () => requestAnimationFrame(() => draw(performance.now()));
      appearance.addEventListener('change', redraw);
      systemDark.addEventListener('change', redraw);
      redraw();
    } else wake();
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
