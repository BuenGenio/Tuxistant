// Runs the site's app.js in Node against a small DOM made from a built page, as a given visitor's
// browser would, and reports what the page then shows. tests/test_website.py drives it.
//
// usage: node site_dom.mjs INPUT.json REPORT.json (files, as a wrapper round node may print too)
// INPUT: {"app": path to app.js, "pages": {name: tree}, "cases": [{"page", "userAgent", "platform",
//        "hash" (optional, the address's #part on arrival), "beforematch" (optional: true for a
//        browser that knows hidden=until-found, as Chromium does), "steps": [null, a selector to
//        click, or {"focus": selector}, ...], "probes": [selector, ...]}]}
// A tree is ["tag", {attributes}, [children]], and a child is a tree or a string of text.
// REPORT: one entry per case: for each step, after its click, each probe's matches as
//         {text, attrs, parts}, "#scrolled", what app.js last scrolled into view, and "#active", what
//         has the focus, as a probe's matches; or {"error"} if app.js threw.
//
// The DOM has what app.js uses. It gets no WebGL here, so the hero shows its still CSS drawings,
// with the themes from the themes.js beside app.js, on a page that measures nothing.
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import vm from 'node:vm';

class Text {
  constructor(data) { this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}

class Element {
  constructor(doc, tag, attrs = {}) {
    this.ownerDocument = doc;
    this.localName = tag;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map(Object.entries(attrs));
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = { setProperty() {} };
    const self = this;
    const attr = key => `data-${key.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)}`;
    this.dataset = new Proxy({}, {
      get: (_, key) => (typeof key === 'string' ? self.getAttribute(attr(key)) ?? undefined : undefined),
      set: (_, key, value) => { self.setAttribute(attr(key), value); return true; },
      deleteProperty: (_, key) => { self.removeAttribute(attr(key)); return true; },
    });
    const classes = () => (self.getAttribute('class') || '').split(/\s+/).filter(Boolean);
    const setClasses = list => self.setAttribute('class', list.join(' '));
    this.classList = {
      contains: name => classes().includes(name),
      add: (...names) => setClasses([...new Set([...classes(), ...names])]),
      remove: (...names) => setClasses(classes().filter(name => !names.includes(name))),
      toggle(name, force = !this.contains(name)) {
        if (force) this.add(name); else this.remove(name);
        return force;
      },
    };
  }

  get children() { return this.childNodes.filter(node => node instanceof Element); }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  get id() { return this.getAttribute('id') ?? ''; }
  set id(value) { this.setAttribute('id', value); }
  get className() { return this.getAttribute('class') ?? ''; }
  set className(value) { this.setAttribute('class', value); }
  get type() { return this.getAttribute('type') ?? ''; }
  set type(value) { this.setAttribute('type', value); }
  get href() { return this.getAttribute('href') ?? ''; }
  set href(value) { this.setAttribute('href', value); }
  get hash() { const href = this.href; return href.includes('#') ? href.slice(href.indexOf('#')) : ''; }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) {
    if (value) this.setAttribute('hidden', value === true ? '' : value);
    else this.removeAttribute('hidden');
  }
  get offsetHeight() { return 0; }
  get clientWidth() { return 0; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 }; }
  getContext() { return null; }     // no WebGL here

  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(...(String(value) ? [String(value)] : [])); }
  append(...nodes) {
    for (let node of nodes) {
      if (typeof node === 'string') node = new Text(node);
      node.parentNode?.removeChild(node);
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  removeChild(node) {
    this.childNodes.splice(this.childNodes.indexOf(node), 1);
    node.parentNode = null;
    return node;
  }
  replaceChildren(...nodes) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  contains(node) {
    for (; node; node = node.parentNode) if (node === this) return true;
    return false;
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  matches(selector) { return parse(selector).some(steps => matchSteps(this, steps, steps.length - 1)); }
  closest(selector) {
    for (let element = this; element instanceof Element && !element.isDocument; element = element.parentNode)
      if (element.matches(selector)) return element;
    return null;
  }
  querySelectorAll(selector) { return this.descendants().filter(element => element.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter(other => other !== listener);
  }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() { this.ownerDocument.scrolledTo = this; }
}

// Selectors: tag, *, #id, .class, [attr], [attr="value"] and [attr^="value"], joined by descendant
// and child combinators, in comma lists. Anything else throws, rather than matching wrongly.
function parse(selector) {
  return selector.split(',').map(alternative => {
    const steps = [];
    let combinator = ' ';
    for (const part of alternative.trim().match(/(?:\[[^\]]*\]|[^\s>[])+|>/g)) {
      if (part === '>') combinator = '>';
      else { steps.push([combinator, compound(part)]); combinator = ' '; }
    }
    return steps;
  });
}

function compound(text) {
  const tokens = text.match(/^[a-z][\w-]*|^\*|#[\w-]+|\.[\w-]+|\[[^\]]+\]/gi) || [];
  if (tokens.join('') !== text) throw new Error(`site_dom.mjs can't read the selector ${text}`);
  return tokens.map(token => {
    if (token === '*') return () => true;
    if (token[0] === '#') return element => element.id === token.slice(1);
    if (token[0] === '.') return element => element.classList.contains(token.slice(1));
    if (token[0] === '[') {
      const [, name, op, value] = token.match(/^\[([\w-]+)(?:(\^?=)"([^"]*)")?\]$/) ||
        (() => { throw new Error(`site_dom.mjs can't read the selector ${token}`); })();
      return element => {
        const actual = element.getAttribute(name);
        if (actual === null) return false;
        return !op || (op === '=' ? actual === value : actual.startsWith(value));
      };
    }
    return element => element.localName === token.toLowerCase();
  });
}

function matchSteps(element, steps, index) {
  if (!(element instanceof Element) || element.isDocument) return false;
  const [combinator, tests] = steps[index];
  if (!tests.every(test => test(element))) return false;
  if (index === 0) return true;
  if (combinator === '>') return matchSteps(element.parentNode, steps, index - 1);
  for (let up = element.parentNode; up instanceof Element && !up.isDocument; up = up.parentNode)
    if (matchSteps(up, steps, index - 1)) return true;
  return false;
}

function documentFrom(tree) {
  const doc = new Element(null, '#document');
  doc.isDocument = true;
  const build = node => {
    if (typeof node === 'string') return new Text(node);
    const [tag, attrs, children] = node;
    const element = new Element(doc, tag, attrs);
    element.append(...children.map(build));
    return element;
  };
  doc.append(build(tree));
  doc.documentElement = doc.children[0];
  doc.body = doc.querySelector('body');
  doc.activeElement = doc.body;
  doc.getElementById = id => doc.descendants().find(element => element.id === id) ?? null;
  doc.createElement = tag => new Element(doc, tag);
  doc.createRange = () => ({ selectNodeContents() {}, getBoundingClientRect: () => ({ width: 0 }) });
  Object.defineProperty(doc, 'dir', { get: () => doc.documentElement.getAttribute('dir') || '' });
  return doc;
}

// A click: the target's listeners, then each ancestor's, then the document's.
function click(doc, selector) {
  const target = doc.querySelector(selector);
  if (!target) throw new Error(`nothing to click at ${selector}`);
  const event = { type: 'click', target, preventDefault() {} };
  for (let node = target; node; node = node.parentNode) {
    for (const listener of node.listeners?.click || []) listener.call(node, event);
  }
}

function describe(element) {
  return {
    text: element.textContent,
    attrs: Object.fromEntries(element.attributes),
    parts: element.childNodes.map(node => (node instanceof Element ? [node.localName, node.textContent]
                                                                    : ['#text', node.data])),
  };
}

function run(app, themes, tree, { userAgent, platform, hash = '', beforematch = false, steps, probes }) {
  const document = documentFrom(tree);
  if (beforematch) document.body.onbeforematch = null;    // what 'onbeforematch' in body looks for
  const context = {
    document,
    navigator: { userAgent, platform },
    location: { hash, href: `http://127.0.0.1/${hash}` },
    matchMedia: media => ({ media, matches: false, addEventListener() {} }),
    localStorage: { getItem: () => null, setItem() {} },
    addEventListener() {},
    scrollY: 0,
    performance,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: () => 0,
    getComputedStyle: () => ({ fontSize: '16px', backgroundColor: 'rgb(0, 0, 0)' }),
    devicePixelRatio: 1,
    IntersectionObserver: class { observe() {} },
    ResizeObserver: class { observe() {} },
    console,
  };
  context.window = context;
  try {
    vm.createContext(context);
    vm.runInContext(themes, context, { filename: 'themes.js' });
    vm.runInContext(app, context, { filename: 'app.js' });
    const special = { '#scrolled': () => document.scrolledTo, '#active': () => document.activeElement };
    return steps.map(step => {
      if (step?.focus) {
        const target = document.querySelector(step.focus);
        if (!target) throw new Error(`nothing to focus at ${step.focus}`);
        target.focus();
      } else if (step) click(document, step);
      return Object.fromEntries(probes.map(probe => [probe, probe in special
        ? [special[probe]()].filter(Boolean).map(describe) : document.querySelectorAll(probe).map(describe)]));
    });
  } catch (error) {
    return { error: String(error?.stack || error) };
  }
}

const [inputPath, reportPath] = process.argv.slice(2);
const input = JSON.parse(readFileSync(inputPath, 'utf8'));
const app = readFileSync(input.app, 'utf8');
const themes = readFileSync(new URL('themes.js', pathToFileURL(input.app)), 'utf8');
writeFileSync(reportPath, JSON.stringify(input.cases.map(item => run(app, themes, input.pages[item.page], item))));
