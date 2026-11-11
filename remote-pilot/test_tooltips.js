// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
// Plain-language tooltips: every interactive element of the Chromatic preset,
// and the shared viewer chrome explains itself, and the words stay plain.
// The real viewer.html is parsed into a small stand-in DOM and the real preset panels build into it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const read = file => fs.readFileSync(`${__dirname}/${file}`, 'utf8');

// ---- a minimal DOM: enough for viewer.html and the preset panels ---------------------------
const VOID = new Set(['meta', 'link', 'input', 'img', 'br', 'hr', 'source', 'area', 'col', 'wbr']);
const decode = text => text.replace(/&quot;/g, '"').replace(/&#x27;|&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const camelToData = key => `data-${key.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)}`;
const REFLECT = ['id', 'title', 'role'];
class Element {
  constructor(document, tag) {
    Object.assign(this, {ownerDocument: document, localName: tag.toLowerCase(), tagName: tag.toUpperCase(), nodeType: 1,
      attrs: new Map(), childNodes: [], parentNode: null, listeners: {}, style: {cssText: ''}});
    const self = this;
    this.dataset = new Proxy({}, {
      get: (_, key) => typeof key === 'string' ? self.getAttribute(camelToData(key)) ?? undefined : undefined,
      set: (_, key, value) => { self.setAttribute(camelToData(key), String(value)); return true; },
      deleteProperty: (_, key) => { self.removeAttribute(camelToData(key)); return true; },
      has: (_, key) => self.hasAttribute(camelToData(key)),
    });
    this.classList = {
      contains: name => self.className.split(/\s+/).includes(name),
      add: (...names) => { for (const n of names) if (!self.classList.contains(n)) self.className = `${self.className} ${n}`.trim(); },
      remove: (...names) => { self.className = self.className.split(/\s+/).filter(n => n && !names.includes(n)).join(' '); },
      toggle: (name, force) => { const on = force ?? !self.classList.contains(name); on ? self.classList.add(name) : self.classList.remove(name); return on; },
    };
  }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  hasAttribute(name) { return this.attrs.has(name); }
  removeAttribute(name) { this.attrs.delete(name); }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { value ? this.setAttribute('hidden', '') : this.removeAttribute('hidden'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { value ? this.setAttribute('disabled', '') : this.removeAttribute('disabled'); }
  get tabIndex() { return this.hasAttribute('tabindex') ? Number(this.getAttribute('tabindex')) : -1; }
  set tabIndex(value) { this.setAttribute('tabindex', value); }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get options() { return this.descendants().filter(node => node.localName === 'option'); }
  get value() {
    if (this.localName !== 'select') return this._value ?? this.getAttribute('value') ?? '';
    if (this._value !== undefined && this.options.some(o => o.value === this._value)) return this._value;
    const option = this.options.find(o => o.selected) || this.options[0];
    return option ? option.value : '';
  }
  set value(value) { this._value = String(value); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(String(value)); }
  get innerHTML() { return this._html || ''; }
  set innerHTML(value) { this.childNodes = []; this._html = value; }
  get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === this.ownerDocument.documentElement; }
  adopt(nodes) {
    return nodes.map(node => {
      if (typeof node === 'string') node = {nodeType: 3, textContent: node, parentNode: null};
      if (node.parentNode) node.parentNode.childNodes = node.parentNode.childNodes.filter(child => child !== node);
      node.parentNode = this;
      return node;
    });
  }
  append(...nodes) { this.childNodes.push(...this.adopt(nodes)); }
  replaceChildren(...nodes) { this.childNodes.forEach(node => { node.parentNode = null; }); this.childNodes = []; this.append(...nodes); }
  after(...nodes) {
    const parent = this.parentNode, adopted = parent.adopt(nodes);
    parent.childNodes.splice(parent.childNodes.indexOf(this) + 1, 0, ...adopted);
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  matches(selector) { return matches(this, selector); }
  closest(selector) { let node = this; while (node && node.nodeType === 1) { if (matches(node, selector)) return node; node = node.parentNode; } return null; }
  querySelectorAll(selector) { return this.descendants().filter(node => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatchEvent(event) { event.target = this; (this.listeners[event.type] || []).forEach(listener => listener(event)); return true; }
  getContext() { const stub = new Proxy(function () {}, {get: () => stub, set: () => true, apply: () => stub}); return stub; }
  getBoundingClientRect() { return {left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0}; }
}
for (const name of REFLECT) Object.defineProperty(Element.prototype, name, {
  get() { return this.getAttribute(name) || ''; }, set(value) { this.setAttribute(name, value); }});

function compound(node, text) {
  const parts = text.match(/^[a-z0-9-]+|#[\w-]+|\.[\w-]+|\[[^\]]+\]/gi) || [];
  assert.equal(parts.join(''), text, `unsupported selector part ${text}`);
  return parts.every(part => {
    if (part[0] === '#') return node.id === part.slice(1);
    if (part[0] === '.') return node.classList.contains(part.slice(1));
    if (part[0] === '[') {
      const [, name, value] = part.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      return value === undefined ? node.hasAttribute(name) : node.getAttribute(name) === value;
    }
    return node.localName === part.toLowerCase();
  });
}
function matches(node, selector) {
  return selector.split(',').some(one => {
    const tokens = one.trim().replace(/\s*>\s*/g, ' > ').split(/\s+/);
    const test = (el, index) => {
      if (!el || el.nodeType !== 1 || !compound(el, tokens[index])) return false;
      if (index === 0) return true;
      if (tokens[index - 1] === '>') return test(el.parentNode, index - 2);
      for (let up = el.parentNode; up && up.nodeType === 1; up = up.parentNode) if (test(up, index - 1)) return true;
      return false;
    };
    return test(node, tokens.length - 1);
  });
}
function parse(html) {
  const document = {
    createElement: tag => new Element(document, tag), getElementById: id => document.documentElement.querySelector(`#${id}`),
    querySelector: s => document.documentElement.querySelector(s), querySelectorAll: s => document.documentElement.querySelectorAll(s),
    addEventListener() {}, activeElement: null, visibilityState: 'visible', hasFocus: () => true,
  };
  const root = new Element(document, '#root');
  document.documentElement = root;
  const stack = [root];
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z0-9-]+)\s*>|<([a-z0-9-]+)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|[^<]+/gi;
  for (const [all, close, open, attrs, selfClose] of html.matchAll(token)) {
    const top = stack[stack.length - 1];
    if (close) { const at = stack.map(n => n.localName).lastIndexOf(close.toLowerCase()); if (at > 0) stack.length = at; }
    else if (open) {
      const node = new Element(document, open);
      for (const [, name, , v1, v2, v3] of (attrs || '').matchAll(/([^\s=>/]+)(\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g))
        node.setAttribute(name.toLowerCase(), decode(v1 ?? v2 ?? v3 ?? ''));
      top.append(node);
      if (!selfClose && !VOID.has(node.localName)) stack.push(node);
    } else if (!all.startsWith('<')) top.append(decode(all));
  }
  document.body = root.querySelector('body');
  return document;
}

// Builds a preset panel into a fresh copy of the real viewer page, with a full status from the receiver.
async function buildPreset(script, preset, status) {
  const document = parse(read('viewer.html'));
  document.body.dataset.preset = preset;
  const context = vm.createContext({
    document, window: {addEventListener() {}}, navigator: {getGamepads: () => []}, console, performance,
    crypto: require('node:crypto').webcrypto, setTimeout, setInterval: () => 1, clearInterval() {},
    requestAnimationFrame: () => 1, cancelAnimationFrame() {}, Event: class { constructor(type) { this.type = type; } },
    MutationObserver: class { observe() {} },
    fetch: async url => {
      const body = url.includes('/status') ? status : url.includes('manager-phrase') ? {command: 'status', phrase: 'READ CHROMATIC'} : {};
      return {ok: true, status: 200, text: async () => JSON.stringify(body)};
    },
  });
  vm.runInContext(read(script), context);
  await new Promise(resolve => setTimeout(resolve, 30));
  return {document, context};
}

const INTERACTIVE = new Set(['button', 'select', 'input', 'textarea', 'summary', 'a']);
const interactive = node => INTERACTIVE.has(node.localName) || node.tabIndex >= 0;
const tipOf = node => (node.getAttribute('data-tip') || node.closest('label')?.getAttribute('data-tip') || '').trim();
const allTips = new Set();
function checkTipped(roots, what) {
  let count = 0;
  for (const root of roots) for (const node of [root, ...root.descendants()]) {
    if (node.hasAttribute('data-tip')) allTips.add(node.getAttribute('data-tip'));
    if (!interactive(node)) continue;
    count += 1;
    assert.ok(tipOf(node), `${what}: <${node.localName} id="${node.id}" class="${node.className}"> has no tooltip`);
  }
  return count;
}

// Plain words: one or two short sentences, any technical term explained.
function checkPlainWords(tips) {
  for (const tip of tips) {
    assert.ok(tip.length >= 15 && tip.length <= 300, `tooltip length ${tip.length}: ${tip}`);
    assert.match(tip, /[.!?)]$/, `tooltip ends a sentence: ${tip}`);
    assert.ok((tip.match(/[.!?](\s|$)/g) || []).length <= 3, `at most a few short sentences: ${tip}`);
    if (/\bCHR1\b/.test(tip)) assert.match(tip, /language/, `CHR1 explained: ${tip}`);
    if (/\bFPGA\b/.test(tip)) assert.match(tip, /chip/, `FPGA explained: ${tip}`);
    if (/\bESP32\b/.test(tip)) assert.match(tip, /chip/, `ESP32 explained: ${tip}`);
    if (/\bfps\b/.test(tip)) assert.match(tip, /per second/, `fps explained: ${tip}`);
  }
}

// A preset's own tests (in its folder) can reuse the stand-in page and these checks.
module.exports = {read, parse, buildPreset, checkTipped, checkPlainWords, tipOf, allTips};

if (require.main === module) (async () => {
  // ---- the Chromatic preset -------------------------------------------------------------------
  const runtime = read('chromatic/runtime.py');
  const featureIds = [...runtime.matchAll(/(?:entry|launcher|game)\("([a-z0-9-]+)"/g)].map(m => m[1]);
  assert.ok(featureIds.length >= 20, 'feature ids found in chromatic/runtime.py');
  const managerCommands = [...read('chromatic/manager_runner.py').matchAll(/^\s+"([a-z-]+)": \((?:True|False),/gm)].map(m => m[1]);
  assert.deepEqual(managerCommands, ['status', 'backup', 'install', 'remove', 'restore-stock']);
  const states = ['available', 'waiting', 'off', 'needs-launcher', 'needs-fpga', 'manual', 'n/a', 'mystery'];
  const groups = ['connection', 'launcher', 'game', 'captures', 'firmware'];
  const status = {
    selected: true, linkEnabled: true, localClient: true, controlsArmed: false, mode: 'usb-sim', configuredMode: 'auto',
    pairingArmed: false, usbOffer: {state: 'found', ports: ['COM7'], message: 'Found the console on COM7.'},
    transport: {simulated: true, kind: 'usb', phase: 'launcher', target: 'launcher', launcherVersion: '1.1.0', label: 'Simulated USB',
      connected: true, session: true, held: 'A', resetReason: 'power-on', lines: 4, lastLineAgeS: 0.2, route: 'usb', usbState: 'nokey',
      leaseLeftMs: 4000, keyKind: 'pc', host: '127.0.0.1', error: 'example note', pairing: {state: 'waiting', code: '1234', secondsLeft: 50}},
    features: featureIds.map((id, i) => ({id, group: groups[i % groups.length], label: id, state: states[i % states.length], detail: 'detail'})),
    tail: ['line 1', 'line 2'], perf: [{fps: 10, frameMs: 100, workMs: 3}], managerCommands,
    manager: {projectFound: true, launcherSourceVersion: '1.1.0', backupFolders: 2, detail: 'ok'},
    managerJob: {command: 'status', state: 'done', exitCode: 0, lines: ['done']},
    activity: [{time: '12:00:00', action: 'mode', ok: true, detail: 'usb-sim'}, {time: '12:00:01', action: 'shot', ok: false, detail: 'failed'}],
  };
  const {document, context} = await buildPreset('presets/chromatic/panel.js', 'chromatic', status);
  const chr = context.RemotePilotChromatic;
  const $ = id => document.getElementById(id);
  const roots = ['chromatic-stage', 'chromatic-panel', 'chromatic-connect-detail', 'chromatic-usb-offer'].map($);
  roots.forEach((root, i) => assert.ok(root, `Chromatic part ${i} was built`));
  const count = checkTipped(roots, 'Chromatic preset');
  const device = $('chromatic-device');
  assert.equal(device.querySelectorAll('[data-chr-button]').length, chr.BUTTONS.length);
  assert.equal(device.querySelectorAll('.chr-side').length, chr.SIDE.length);
  for (const spot of device.querySelectorAll('.chr-side')) assert.ok(spot.tabIndex >= 0 && spot.getAttribute('aria-label'), 'edge control reachable by keyboard');
  assert.equal($('chr-usb-offer-buttons').querySelectorAll('button').length, 1);
  assert.match(tipOf($('chr-usb-offer-buttons').querySelector('button')), /COM7/);
  assert.ok(count >= 45, `expected every Chromatic control, found ${count}`);
  console.log(`chromatic tooltips: all ${count} interactive elements (device buttons, edge controls, cards, drop-downs) explain themselves PASS`);

  // Status elements explain themselves too: badges, cards, groups, facts, the screen and the graph.
  const statusParts = ['chr-sim-badge', 'chr-phase', 'chr-screen', 'chr-perf', 'chr-perf-text', 'chr-fps', 'chr-control-note',
    'chr-feedback', 'chr-activity', 'chr-tail', 'chr-facts', 'chromatic-device', 'chr-usb-pair-code', 'chr-manager-state', 'chr-mgr-out'];
  for (const id of statusParts) assert.ok($(id).getAttribute('data-tip'), `${id} has a tooltip`);
  assert.ok(document.querySelector('.chr-bezel-top').getAttribute('data-tip'));
  const cards = $('chr-features').querySelectorAll('[data-feature]');
  assert.equal(cards.length, featureIds.length);
  for (const card of cards) {
    assert.equal(card.getAttribute('data-tip'), chr.FEATURE_TIPS[card.getAttribute('data-feature')], card.getAttribute('data-feature'));
    assert.ok(card.querySelector('.chr-badge').getAttribute('data-tip'), 'badge tooltip');
  }
  for (const heading of $('chr-features').querySelectorAll('h4')) assert.ok(heading.getAttribute('data-tip'), heading.textContent);
  const facts = $('chr-facts').querySelectorAll('dt');
  assert.ok(facts.length >= 14, 'every fact row was rendered');
  for (const dt of facts) assert.equal(dt.getAttribute('data-tip'), chr.FACT_TIPS[dt.textContent], dt.textContent);
  // Every feature, badge state, group, manager job, console question, button and edge control has its own words.
  for (const id of featureIds) assert.ok(chr.FEATURE_TIPS[id], `feature ${id}`);
  for (const state of Object.keys(chr.STATES)) assert.ok(chr.STATE_TIPS[state], `state ${state}`);
  for (const group of Object.keys(chr.GROUPS)) assert.ok(chr.GROUP_TIPS[group], `group ${group}`);
  for (const command of managerCommands) assert.ok(chr.MANAGER_TIPS[command], `manager ${command}`);
  for (const option of $('chr-console-cmd').options) assert.ok(chr.CONSOLE_TIPS[option.value], `console ${option.value}`);
  for (const name of chr.BUTTONS) assert.ok(chr.BUTTON_TIPS[name], name);
  assert.equal(chr.SIDE_TIPS.length, chr.SIDE.length);
  chr.SIDE.forEach(([title], i) => assert.match(chr.SIDE_TIPS[i].toLowerCase(), new RegExp(title.split(' (')[0].toLowerCase().split(' ')[0])));
  // Drop-downs name the current choice; a live "why it's off" title is separate from the explanation.
  assert.match($('chr-mgr-cmd').getAttribute('data-tip'), /only reads/);
  $('chr-console-cmd').value = 'heap'; $('chr-console-cmd').dispatchEvent({type: 'change'});
  assert.match($('chr-console-cmd').getAttribute('data-tip'), /free memory/);
  const buttonA = device.querySelector('[data-chr-button="A"]');  // disabled here: the reason is a separate live title
  assert.ok(buttonA.disabled && buttonA.getAttribute('title') && buttonA.getAttribute('data-tip') === chr.BUTTON_TIPS.A);
  assert.doesNotMatch(chr.deviceArt().svg, /<title>/);  // the edge controls' tips moved to focusable spots
  console.log('chromatic tooltips: badges, cards, groups, facts, choices and edge controls each have their own plain words PASS');
  for (const node of [document.documentElement, ...document.documentElement.descendants()])
    if (node.hasAttribute('data-tip')) allTips.add(node.getAttribute('data-tip'));
  for (const key of Object.keys(chr).filter(key => key.endsWith('_TIPS'))) Object.values(chr[key]).forEach(tip => allTips.add(tip));

  // ---- the shared viewer chrome, the credit footer and the tooltip engine -------------------
  const shared = parse(read('viewer.html'));
  const viewerSource = read('viewer.js');
  const chrome = ['.topbar', '.connect-bar', '.activity', '#log-dialog', '.rp-credit', '.stage', '.side-stack']
    .map(selector => shared.querySelector(selector));
  chrome.forEach((root, i) => assert.ok(root, `shared part ${i}`));
  // The shared quick, motion and drawn buttons get their words from viewer.js (describeSharedButtons).
  const describe = viewerSource.match(/^function describeSharedButtons\(\) \{[\s\S]*?^\}\r?$/m)[0];
  vm.runInContext(`${describe}; describeSharedButtons();`, vm.createContext({document: shared}));
  for (const node of shared.querySelectorAll('[data-button], [data-motion], [data-visual]')) assert.ok(node.getAttribute('data-tip'), node.textContent);
  const sharedCount = checkTipped(chrome.map(root => root), 'shared viewer');
  assert.ok(sharedCount >= 20, `shared controls found ${sharedCount}`);
  const credit = shared.querySelector('.rp-credit span');
  assert.equal(credit.textContent, 'Created by ChaoticSPARTAN1');
  assert.ok(credit.tabIndex >= 0 && /ChaoticSPARTAN1/.test(credit.getAttribute('data-tip')));
  assert.equal(shared.querySelector('main').children.at(-1).localName, 'footer', 'the credit is the last thing on the page');
  const viewer = read('viewer.js');
  assert.match(viewer, /^installTooltips\(\);\r?$/m);
  assert.match(viewer, /^describeSharedButtons\(\);\r?$/m);
  const prefix = viewer.split('const $ =')[0];
  const pure = vm.createContext({});
  vm.runInContext(`${prefix}; this.tooltipLines = tooltipLines;`, pure);
  assert.deepEqual({...pure.tooltipLines('Explains it.', 'Connect first')}, {text: 'Explains it.', now: 'Right now: Connect first'});
  assert.deepEqual({...pure.tooltipLines('Explains it.', '')}, {text: 'Explains it.', now: ''});
  assert.deepEqual({...pure.tooltipLines('', 'Only a title')}, {text: 'Only a title', now: ''});
  assert.deepEqual({...pure.tooltipLines('Shows Device connected.', 'Device connected')}, {text: 'Shows Device connected.', now: ''});
  for (const [file, scope] of [['presets/chromatic/theme.css', 'chromatic']]) {
    const css = read(file);
    assert.match(css, new RegExp(`body\\[data-preset="${scope}"\\] \\.rp-tip \\{`), `${file} styles the tooltip`);
    assert.match(css, new RegExp(`body\\[data-preset="${scope}"\\] \\.rp-credit \\{`), `${file} styles the credit`);
  }
  assert.match(read('presets/chromatic/theme.css'), /\.rp-tip \{[^}]*var\(--pm-body\)[^}]*\}/, 'pixel font in the Chromatic tooltip');
  assert.match(read('presets/chromatic/theme.css'), /\.rp-tip \{[^}]*border-radius: 0[^}]*\}/, 'square pixel corners');
  console.log(`shared tooltips: ${sharedCount} header, connection, log and shared controls; small "Created by ChaoticSPARTAN1" footer; tooltip engine PASS`);


  // ---- plain words: one or two short sentences, any technical term explained ----------------------
  checkPlainWords(allTips);
  console.log(`plain words: ${allTips.size} tooltips are short, end in full sentences and explain CHR1, FPGA, ESP32 and fps PASS`);
})().catch(error => { console.error(error); process.exit(1); });
