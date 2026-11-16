// Runs the built site's app.js on a fake page just big enough for it: the elements it looks up, a
// clock and animation frames that only move when a step says so, and a WebGL context that compiles
// anything and records the uniforms of every frame drawn. tests/test_website.py drives it.
//
//   node tests/website_hero_page.mjs <dist> '<steps as JSON>'
//
// Steps: {"wait": seconds}, {"until": theme id}, {"enter": true} and {"leave": true} for the
// pointer on the picker, {"press": "still" | "dot:N", "by": "pointer" | "keyboard"},
// {"focus": "still" | "dot:N"} by keyboard, {"blur": true}, and {"read": "name"}, which reports
// what the visitor would see. It prints the reads as JSON.
import fs from 'node:fs';
import vm from 'node:vm';

const [dist, stepsJson] = process.argv.slice(2);
const steps = JSON.parse(stepsJson);
let clock = 5000;          // ms, as performance.now() reports it
let active = null;         // document.activeElement
const queued = [];         // animation frame callbacks
const frames = [];         // the uniforms of each frame drawn

class ClassList {
  constructor() { this.names = new Set(); }
  add(...names) { names.forEach(name => this.names.add(name)); }
  remove(...names) { names.forEach(name => this.names.delete(name)); }
  contains(name) { return this.names.has(name); }
  toggle(name, force) { const on = force ?? !this.names.has(name); this.names[on ? 'add' : 'delete'](name); return on; }
  replace(from, to) { if (!this.names.delete(from)) return false; this.names.add(to); return true; }
}

const BUBBLES = new Set(['click', 'focusin', 'focusout']);
class Element {
  constructor(rect = [0, 0, 0, 0]) {
    Object.assign(this, { rect, children: [], parentElement: null, attributes: {}, listeners: {}, dataset: {},
                          style: { setProperty() {} }, classList: new ClassList(), textContent: '', parts: {} });
  }
  get className() { return [...this.classList.names].join(' '); }
  set className(value) { this.classList = new ClassList(); this.classList.add(...value.split(' ')); }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  fire(type, fields = {}) {
    const event = { type, target: this, preventDefault() {}, ...fields };
    for (let node = this; node; node = BUBBLES.has(type) ? node.parentElement : null)
      for (const listener of node.listeners[type] || []) listener(event);
  }
  contains(other) { for (let node = other; node; node = node.parentElement) if (node === this) return true; return false; }
  // As in Chromium: a button clicked or tapped has focus, but only keyboard focus is visible.
  matches(selector) {
    if (selector === ':focus-visible') return active === this && this.keyboardFocus;
    throw new Error(`the fake page can't match ${selector}`);
  }
  closest() { return null; }
  querySelector(selector) { return this.parts[selector] ?? null; }
  getBoundingClientRect() {
    const [left, top, width, height] = this.rect;
    return { left, top, width, height, right: left + width, bottom: top + height };
  }
  get clientWidth() { return this.rect[2]; }
  get clientHeight() { return this.rect[3]; }
  get offsetHeight() { return this.rect[3]; }
}

function focus(element, keyboard) {
  const before = active;
  if (before === element) { element.keyboardFocus ||= keyboard; return; }
  if (before) before.fire('focusout', { relatedTarget: element });
  active = element;
  element.keyboardFocus = keyboard;
  element.fire('focusin', { relatedTarget: before });
}
function blur() {
  const before = active;
  active = null;
  if (before) before.fire('focusout', { relatedTarget: null });
}

// A WebGL context that compiles every theme and keeps what each frame was drawn with.
const uniforms = {};
const gl = new Proxy({
  getShaderParameter: () => true,
  getProgramParameter: () => true,
  getUniformLocation: (program, name) => name,
  getAttribLocation: () => 0,
  uniform1f: (name, value) => { uniforms[name] = value; },
  drawArrays: () => { frames.push({ ...uniforms }); },
}, { get: (fake, key) => (key in fake ? fake[key] : /^[A-Z0-9_]+$/.test(key) ? 1 : () => ({})) });

// The hero of a 390 px phone: the claims sit under the words.
const html = fs.readFileSync(`${dist}/index.html`, 'utf8');
const strings = html.match(/<script type="application\/json" id="strings">([\s\S]*?)<\/script>/)[1];
const hero = new Element([0, 0, 390, 600]);
const canvas = Object.assign(new Element([0, 0, 390, 600]), { width: 0, height: 0, getContext: () => gl });
const copy = new Element([16, 72, 358, 265]);
const title = new Element([16, 100, 358, 40]);
const tagline = new Element([16, 146, 358, 48]);
const pick = new Element([16, 290, 358, 28]);
const label = new Element();
const dots = new Element();
const claims = new Element([16, 343, 358, 240]);
const header = new Element([0, 0, 390, 56]);
pick.append(label, dots);
copy.append(title, tagline, pick);
hero.append(canvas, copy, claims);
Object.assign(hero.parts, { '.hero-copy': copy, '.pick': pick, '.caps': claims });
Object.assign(copy.parts, { h1: title, '.tagline': tagline });
const root = new Element();
const examplesPrompt = new Element();     // the examples' call to action, worded for the tab on show
const byId = {
  strings: Object.assign(new Element(), { textContent: strings }), download: new Element(),
  'dl-meta-text': new Element(), lang: new Element(), mode: new Element(), 'site-header': header,
  hero, tux: canvas, 'theme-name': label, dots,
};
const document = {
  documentElement: root, body: new Element(), hidden: false, dir: 'ltr',
  get activeElement() { return active; },
  getElementById: id => byId[id] ?? null,
  querySelector: selector => ({ 'meta[name="theme-color"]': new Element(),
                                '#examples .acc-cta p': examplesPrompt })[selector] ?? null,
  querySelectorAll: () => [],
  createElement: () => new Element(),
  createRange: () => ({ selectNodeContents() {}, getBoundingClientRect: () => ({ width: 170 }) }),
  addEventListener() {},
};
const stored = new Map();
const page = {
  document, navigator: { userAgent: 'Mozilla/5.0 (X11; Linux x86_64)', platform: 'Linux x86_64' },
  location: {}, scrollY: 0, devicePixelRatio: 1, console,
  localStorage: { getItem: key => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, String(value)) },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  getComputedStyle: () => ({ backgroundColor: 'rgb(247, 248, 251)', fontSize: '40px' }),
  addEventListener() {},
  performance: { now: () => clock },
  requestAnimationFrame: callback => queued.push(callback),
  ResizeObserver: class { observe() {} },
  IntersectionObserver: class { observe() {} },
};
page.window = page;
vm.createContext(page);
vm.runInContext(fs.readFileSync(`${dist}/themes.js`, 'utf8'), page);
vm.runInContext(fs.readFileSync(`${dist}/app.js`, 'utf8'), page);

// Frames come every 50 ms, while something has asked for one.
function run(seconds, done = () => false) {
  const end = clock + seconds * 1000;
  while (clock < end && !done()) {
    clock += 50;
    for (const callback of queued.splice(0)) callback(clock);
  }
}
const pauseButton = () => pick.children.find(child => child.classList.contains('still')) ?? null;
const target = name => (name === 'still' ? pauseButton() : dots.children[Number(name.split(':')[1])]);

const reads = {};
for (const step of steps) {
  if ('wait' in step) run(step.wait);
  if ('until' in step) run(60, () => hero.dataset.theme === step.until);
  if (step.enter) pick.fire('pointerenter');
  if (step.leave) pick.fire('pointerleave');
  if (step.blur) blur();
  if (step.focus) focus(target(step.focus), true);
  if (step.press) {
    const button = target(step.press);
    if (step.by === 'keyboard') focus(button, true);
    else focus(button, false);      // Chromium focuses what a pointer clicks or taps
    button.fire('click');
  }
  if (step.read) {
    const pause = pauseButton();
    reads[step.read] = {
      theme: hero.dataset.theme, label: label.textContent, inkLight: hero.classList.contains('ink-light'),
      gl: root.classList.contains('has-gl'), pause: pause && { pressed: pause.getAttribute('aria-pressed'),
                                                               title: pause.title, label: pause.getAttribute('aria-label') },
      focused: active === pause ? 'still' : active ? 'other' : null,
      focusVisible: Boolean(active?.matches(':focus-visible')),
      frames: frames.length, frame: frames.at(-1) ?? null, clock,
    };
  }
}
console.log(JSON.stringify(reads));
