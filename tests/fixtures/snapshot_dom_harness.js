// Runs the autoapply page probe (submit._SNAPSHOT_JS) against a tiny fake DOM, so
// the probe's own logic is exercised without a browser. Input on stdin (JSON):
//   { "snapshot_js": "<source of () => {...}>", "dom": <node> }
// node: { tag, attrs, text, children, size: "zero", display, visibility, opacity,
//         checked, required, value, files, selected, pseudo }
//   value    the control's value (an input or textarea); a select derives its value
//            from the option marked `selected` (else its first option)
//   files    for an input[type=file]: the attached files, e.g. [{"name": "cv.pdf"}]
//   selected for an option: true when it is the chosen one
//   pseudo   computed ::before / ::after content, e.g. {"after": "\"*\""}; the fake
//            cannot evaluate stylesheets, so a test states what the browser would
// Output on stdout: the probe's return value as JSON.
//
// The fake supports a small selector grammar, enough for everything the probe uses:
// comma lists of compound selectors made of a tag, "*", .class, #id and [attr],
// [attr="v"], [attr*="v"], [attr^="v"], [attr$="v"], [attr~="v"]. It throws on any
// other (combinators, pseudo-classes), so a selector added to the probe fails loudly
// here instead of matching nothing.
const fs = require('fs');
const { snapshot_js: source, dom } = JSON.parse(fs.readFileSync(0, 'utf8'));

const SELECTOR_CACHE = new Map();

function parseCompound(text, whole) {
  let rest = text.trim();
  const parsed = { tag: null, id: null, classes: [], attrs: [] };
  const tag = /^(\*|[a-zA-Z][a-zA-Z0-9-]*)/.exec(rest);
  if (tag) {
    parsed.tag = tag[1] === '*' ? null : tag[1].toUpperCase();
    rest = rest.slice(tag[0].length);
  }
  while (rest) {
    let m;
    if ((m = /^\.([A-Za-z_][\w-]*)/.exec(rest))) parsed.classes.push(m[1]);
    else if ((m = /^#([A-Za-z_][\w-]*)/.exec(rest))) parsed.id = m[1];
    else if ((m = /^\[([a-zA-Z][\w-]*)(?:([*^$~]?=)"([^"]*)")?\]/.exec(rest))) {
      parsed.attrs.push({ name: m[1], op: m[2] || null, value: m[3] });
    } else {
      throw new Error('fake DOM does not support selector ' + whole);
    }
    rest = rest.slice(m[0].length);
  }
  if (!tag && !parsed.id && !parsed.classes.length && !parsed.attrs.length) {
    throw new Error('fake DOM does not support selector ' + whole);
  }
  return parsed;
}

function parseSelector(selector) {
  if (!SELECTOR_CACHE.has(selector)) {
    SELECTOR_CACHE.set(
      selector,
      selector.split(',').map((part) => parseCompound(part, selector))
    );
  }
  return SELECTOR_CACHE.get(selector);
}

function attrMatches(node, { name, op, value }) {
  const actual = node.getAttribute(name);
  if (actual === null) return false;
  switch (op) {
    case null: return true;
    case '=': return actual === value;
    case '*=': return actual.includes(value);
    case '^=': return actual.startsWith(value);
    case '$=': return actual.endsWith(value);
    case '~=': return actual.split(/\s+/).includes(value);
    default: throw new Error('fake DOM does not support attribute operator ' + op);
  }
}

function matches(node, selector) {
  return parseSelector(selector).some((c) => {
    if (c.tag && node.tagName !== c.tag) return false;
    if (c.id && node.id !== c.id) return false;
    const classes = (node.attrs.class || '').split(/\s+/);
    if (!c.classes.every((name) => classes.includes(name))) return false;
    return c.attrs.every((attr) => attrMatches(node, attr));
  });
}

function all(node) {
  return [node, ...node.children.flatMap(all)];
}

class FakeNode {
  constructor(spec, parent) {
    this.nodeType = 1;
    this.spec = spec;
    this.tagName = (spec.tag || 'div').toUpperCase();
    this.attrs = Object.assign({}, spec.attrs || {});
    this.id = this.attrs.id || '';
    this.parentElement = parent;
    this.checked = !!spec.checked;
    this.required = !!spec.required;
    this.src = this.attrs.src || '';
    this.children = (spec.children || []).map((child) => new FakeNode(child, this));
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? String(this.attrs[name]) : null;
  }
  getBoundingClientRect() {
    const size = this.spec.size === 'zero' ? 0 : 10;
    return { width: size, height: size, right: size, bottom: size };
  }
  closest(selector) {
    for (let n = this; n; n = n.parentElement) if (matches(n, selector)) return n;
    return null;
  }
  matches(selector) {
    return matches(this, selector);
  }
  contains(other) {
    for (let n = other; n; n = n.parentElement) if (n === this) return true;
    return false;
  }
  querySelectorAll(selector) {
    return this.children.flatMap(all).filter((n) => matches(n, selector));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
  get options() {
    return this.tagName === 'SELECT' ? this.children.filter((c) => c.tagName === 'OPTION') : undefined;
  }
  get selectedIndex() {
    if (this.tagName !== 'SELECT') return -1;
    const picked = this.options.findIndex((option) => option.spec.selected);
    return picked >= 0 ? picked : this.options.length ? 0 : -1;
  }
  get value() {
    if (this.tagName === 'SELECT') {
      const option = this.options[this.selectedIndex];
      if (!option) return '';
      return option.getAttribute('value') !== null ? option.getAttribute('value') : option.innerText;
    }
    return this.spec.value === undefined ? '' : String(this.spec.value);
  }
  get files() {
    return this.tagName === 'INPUT' && this.attrs.type === 'file' ? this.spec.files || [] : null;
  }
  get text() {
    return this.innerText; // HTMLOptionElement.text
  }
  get innerText() {
    return [this.spec.text || '', ...this.children.map((c) => c.innerText)]
      .filter(Boolean)
      .join(' ');
  }
  get textContent() {
    return this.innerText;
  }
}

const root = new FakeNode(dom, null);

globalThis.document = {
  body: root,
  querySelectorAll: (selector) => all(root).filter((n) => matches(n, selector)),
  querySelector: (selector) => all(root).find((n) => matches(n, selector)) || null,
  getElementById: (id) => all(root).find((n) => n.id === id) || null,
};
globalThis.window = {
  scrollX: 0,
  scrollY: 0,
  getComputedStyle: (n, pseudo) => {
    if (pseudo) {
      // A pseudo-element: only `content` is modelled, taken from the node's spec.
      const which = String(pseudo).replace(/^:+/, '');
      const content = (n.spec.pseudo || {})[which];
      return {
        content: content === undefined ? 'none' : content,
        display: 'inline',
        visibility: 'visible',
        opacity: '1',
      };
    }
    return {
      display: n.spec.display || 'block',
      visibility: n.spec.visibility || 'visible',
      opacity: n.spec.opacity === undefined ? '1' : String(n.spec.opacity),
      content: 'normal',
    };
  },
};
globalThis.CSS = { escape: (value) => String(value) };

const snapshot = (0, eval)('(' + source.trim() + ')')();
process.stdout.write(JSON.stringify(snapshot));
