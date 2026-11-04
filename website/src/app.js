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

  // Auto-hide header
  const header = document.getElementById('site-header');
  let lastY = scrollY;
  let graceUntil = performance.now() + 900;
  addEventListener('scroll', () => {
    const y = scrollY;
    const delta = y - lastY;
    lastY = y;
    if (y < 96 || performance.now() < graceUntil) header.classList.remove('is-hidden');
    else if (delta > 6) header.classList.add('is-hidden');
    else if (delta < -6) header.classList.remove('is-hidden');
  }, { passive: true });
  addEventListener('hashchange', () => { graceUntil = performance.now() + 900; });
  header.addEventListener('focusin', () => header.classList.remove('is-hidden'));

  // Hero: the real theme shaders, drawn with the uniforms the desktop blob uses.
  const canvas = document.getElementById('tux');
  const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false });
  if (!gl) return;

  const VERTEX = `attribute vec2 p; varying vec2 v;
    void main() { v = vec2(p.x * .5 + .5, .5 - p.y * .5); gl_Position = vec4(p, 0., 1.); }`;
  const HEADER = `precision highp float; varying vec2 v;
    uniform float u_time, u_level, u_appear, u_listen, u_think, u_speak, u_ink; uniform vec2 u_size;\n`;
  // Waves are added light, which a light page can't show. There they become ink: dense at the
  // crests, which are white on a dark page, and fading out before the bottom of the canvas.
  const FOOTER = `\nvoid main() {
    vec4 c = tux_theme(v);
    vec3 hue = c.rgb / max(c.a, .001);
    vec3 ink = mix(hue * .6, vec3(.05, .2, .62), smoothstep(.45, .85, hue.r));
    float a = min(1., c.a * (1. + 4. * c.a)) * (1. - smoothstep(.8, 1., v.y));
    gl_FragColor = mix(c, vec4(ink * a, a), u_ink) * u_appear;
  }`;

  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return shader;
    console.warn(gl.getShaderInfoLog(shader));
    return null;
  }

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

  const vertex = compile(gl.VERTEX_SHADER, VERTEX);
  const themes = window.TUX_THEMES.flatMap(theme => {
    const fragment = compile(gl.FRAGMENT_SHADER, HEADER + theme.src + FOOTER);
    if (!fragment) return [];
    const program = gl.createProgram();
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return [];
    const uniforms = {};
    for (const name of ['u_time', 'u_level', 'u_appear', 'u_listen', 'u_think', 'u_speak', 'u_ink', 'u_size'])
      uniforms[name] = gl.getUniformLocation(program, name);
    return [{ ...theme, program, position: gl.getAttribLocation(program, 'p'), uniforms }];
  });
  if (!themes.length) return;
  document.documentElement.classList.add('has-gl');

  const label = document.getElementById('theme-name');
  const dots = document.getElementById('dots');
  themes.forEach((theme, index) => {
    const dot = document.createElement('button');
    dot.className = 'dot';
    dot.type = 'button';
    dot.setAttribute('aria-label', `${S.showTheme}: ${S.themes[theme.id]}`);
    dot.addEventListener('click', () => show(index, true));
    dots.append(dot);
  });

  const SLIDE_S = 7;
  const FADE_S = 0.6;
  let current = 0;
  let slideStart = 0;
  let manual = false;

  function show(index, byUser) {
    current = index;
    slideStart = performance.now() / 1000;
    manual ||= Boolean(byUser);
    label.textContent = S.themes[themes[index].id];
    [...dots.children].forEach((dot, i) => dot.setAttribute('aria-pressed', i === index));
    if (reducedMotion) requestAnimationFrame(() => draw(performance.now()));
  }

  function resize() {
    const ratio = Math.min(devicePixelRatio || 1, 2);
    const width = Math.round(canvas.clientWidth * ratio);
    const height = Math.round(canvas.clientHeight * ratio);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
  }

  // A scripted turn: listen, think, speak, with a level shaped like speech.
  const band = (t, from, to) => Math.min(1, Math.max(0, Math.min((t - from) / .6, (to - t) / .6)));
  function draw(nowMs) {
    resize();
    const now = nowMs / 1000;
    const age = reducedMotion ? SLIDE_S / 2 : now - slideStart;
    if (!reducedMotion && age > SLIDE_S && !manual) {
      show((current + 1) % themes.length);
      return draw(nowMs);
    }
    const theme = themes[current];
    const phase = age % SLIDE_S;
    const speech = Math.abs(Math.sin(now * 5.1) * Math.sin(now * 1.7 + 1)) * .75 + .1;
    const listen = band(phase, -1, 3);
    const think = band(phase, 2.6, 4.2);
    const speak = band(phase, 3.8, SLIDE_S + 1);

    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    let width = canvas.width;
    let height = canvas.height;
    if (theme.kind === 'orb') {
      width = canvas.height;
      height = Math.round(width * theme.aspect);
      gl.viewport(Math.round((canvas.width - width) / 2), Math.round((canvas.height - height) / 2),
                  width, height);
    }
    gl.useProgram(theme.program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
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
    gl.uniform2f(u.u_size, width, height);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

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
  new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; wake(); }).observe(canvas);
  document.addEventListener('visibilitychange', wake);
  addEventListener('resize', () => { if (reducedMotion) draw(performance.now()); }, { passive: true });
  if (reducedMotion) {
    const redraw = () => requestAnimationFrame(() => draw(performance.now()));
    appearance.addEventListener('change', redraw);
    systemDark.addEventListener('change', redraw);
  }

  show(0);
  if (reducedMotion) draw(performance.now());
  else wake();
})();
