// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
// Private details on screen: the pixel eye at the top right of the header
// hides addresses, network names, device IDs, pairing details, ports and folders as ****, on by
// default (remembered per browser), and only the display changes. Runs the real viewer.js code:
// the pure mask, the pixel icon, and installPrivacy against a small stand-in DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const read = file => fs.readFileSync(`${__dirname}/${file}`, 'utf8');
const viewer = read('viewer.js');

// ---- the pure mask: every kind of private value, and ordinary words left alone ----------------
const pure = vm.createContext({});
vm.runInContext(`${viewer.split('const $ =')[0]}; this.mask = maskPrivate; this.icon = privacyIconSvg; this.ICONS = PRIVACY_ICONS; this.TIPS = PRIVACY_TIPS;`, pure);
const SAMPLE = {
  'IPv4 address': ['Address 192.168.250.20', '192.168.250.20'],
  'IPv4 with port': ['Gateway 10.123.45.7:4711 answered', '10.123.45.7'],
  'IPv6 address': ['Console at fe80::1c2b:3aff:fe4d:5e6f%12', 'fe80::1c2b'],
  'MAC': ['MAC 02:00:00:aa:bb:cc', '02:00:00:aa:bb:cc'],
  'serial number': ['Serial: CHR0012345X', 'CHR0012345X'],
  'device ID': ['device id=0a1b2c3d4e5f6071', '0a1b2c3d4e5f6071'],
  'Wi-Fi name (quoted)': ['Wi-Fi network "Example Home 5G" joined', 'Example Home'],
  'Wi-Fi name (labelled)': ['SSID: Cafe_Guest', 'Cafe_Guest'],
  'server address': ['Jellyfin server: http://10.123.45.30:8096/web', '10.123.45.30'],
  'host and port': ['Listening at media-box.local:8920', 'media-box'],
  'host name': ['Found chromatic-1a2b.local', 'chromatic-1a2b'],
  'port': ['open on port 8096', '8096'],
  'user name': ['Jellyfin user: sample_user', 'sample_user'],
  'token': ['token=0123456789abcdef0123456789abcdef', '0123456789abcdef0123456789abcdef'],
  'pairing code': ['Pairing code 482913: check the console', '482913'],
  'pairing code (short)': ['Code 1234 · 50 s left', '1234'],
  'path with a user name': ['Saved in /home/sample-person/Documents/x.txt', 'sample-person'],
  'private folder': ['D:\\work\\private\\chromatic-tools\\latest.txt', 'chromatic-tools'],
  'log line': ['12:00:01 wifi: connected to 10.123.45.44 as host: chromatic', '10.123.45.44'],
};
for (const [kind, [text, secret]] of Object.entries(SAMPLE)) {
  const shown = pure.mask(text);
  assert.ok(!shown.toLowerCase().includes(secret.toLowerCase()) && shown.includes('*'), `masked: ${kind}: ${shown}`);
}
const plain = '12:00:01 Launcher 1.1.0 is running (Simulated USB). Using COM3. 24 fps, 16.7 ms guide. sha256 ' + 'ab'.repeat(32);
assert.equal(pure.mask(plain), plain, 'ordinary words, versions, times, COM ports and content hashes stay readable');
assert.equal(pure.mask(pure.mask(SAMPLE['server address'][0])), pure.mask(SAMPLE['server address'][0]), 'masking twice changes nothing');
assert.equal(pure.mask('192.168.250.20'), '***.***.***.**', 'dots stay, so an address still reads as one');
assert.equal(pure.mask('482913', true), '******', 'an element that is all private is starred whole');
for (const key of ['shown', 'hidden']) {
  const grid = pure.ICONS[key];
  assert.ok(grid.length === 9 && grid.every(row => row.length === 11), `${key} eye is 11 x 9 cells`);
  const svg = pure.icon(key === 'hidden');
  assert.equal((svg.match(/<rect /g) || []).length, grid.join('').split('#').length - 1, `${key} eye drawn cell by cell`);
  assert.match(svg, /shape-rendering="crispEdges"/);
}
assert.notEqual(pure.icon(true), pure.icon(false), 'the eye gets a slash while hidden');
console.log(`privacy mask: ${Object.keys(SAMPLE).length} kinds of private value starred, ordinary words kept, pixel eye 11 x 9 PASS`);

// ---- installPrivacy on a stand-in DOM -----------------------------------------------------------
class Text { constructor(value) { this.nodeType = 3; this.nodeValue = value; this.parentElement = null; } }
class Element {
  constructor(tag, attrs = {}) {
    this.nodeType = 1; this.localName = tag; this.attrs = {...attrs}; this.childNodes = []; this.parentElement = null;
    this.listeners = {}; this._html = '';
    const self = this;
    this.dataset = new Proxy({}, {set: (_, k, v) => { self.attrs[`data-${k}`] = String(v); return true; }, get: (_, k) => self.attrs[`data-${k}`]});
    this.classList = {set: new Set(), toggle(name, on) { on ? this.set.add(name) : this.set.delete(name); }, contains(name) { return this.set.has(name); }};
  }
  append(...nodes) { for (const node of nodes) { const n = typeof node === 'string' ? new Text(node) : node; n.parentElement = this; this.childNodes.push(n); } return this; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  click() { this.listeners.click?.(); }
  set textContent(value) { this.childNodes = []; this.append(String(value)); observer?.queue(this.childNodes); }
  get textContent() { return texts(this).map(t => t.nodeValue).join(''); }
  set innerHTML(value) { this._html = value; }
  get innerHTML() { return this._html; }
  querySelector(sel) { return all(this).find(el => el.nodeType === 1 && sel === `.${el.attrs.class}`) || null; }
  matches(sel) {
    return sel.split(',').some(one => {
      const attr = one.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      if (attr) return attr[1] in this.attrs && (attr[2] === undefined || this.attrs[attr[1]] === attr[2]);
      return one === this.localName;
    });
  }
  closest(sel) { for (let el = this; el; el = el.parentElement) if (el.matches(sel)) return el; return null; }
}
const all = root => [root, ...root.childNodes.flatMap(child => child.nodeType === 1 ? all(child) : [child])];
const texts = root => all(root).filter(node => node.nodeType === 3);
let observer = null;
function page(stored) {
  const body = new Element('body');
  const button = new Element('button', {id: 'privacy-toggle', 'data-privacy': 'keep'}).append(new Element('span', {class: 'privacy-icon'}));
  const facts = new Element('dd').append('Address 192.168.250.20');
  const code = new Element('strong', {'data-private': ''}).append('482913');
  const tail = new Element('pre').append('wifi: joined "Example Home 5G" at 10.123.45.44');
  const option = new Element('option').append('Configured: 10.123.45.9');
  const script = new Element('script').append('const ip = "10.123.45.1";');
  body.append(button, facts, code, tail, new Element('select').append(option), script);
  const store = new Map(stored ? [['remote-pilot-privacy', stored]] : []);
  const context = vm.createContext({
    document: {body, getElementById: id => all(body).find(el => el.attrs?.id === id) || null,
      createTreeWalker: root => { const list = texts(root); let i = 0; return {nextNode: () => list[i++] || null}; }},
    NodeFilter: {SHOW_TEXT: 4},
    localStorage: {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k)},
    MutationObserver: class {
      constructor(fn) { this.fn = fn; observer = this; }
      observe() {}
      queue(nodes) { this.fn([{type: 'childList', addedNodes: nodes}]); }
    },
  });
  context.globalThis = context;
  const install = viewer.match(/^function installPrivacy\(\) \{[\s\S]*?^\}\r?$/m)[0];
  vm.runInContext(`${viewer.split('const $ =')[0]}; const $ = id => document.getElementById(id); ${install}; installPrivacy();`, context);
  return {body, button, facts, code, tail, option, script, store, privacy: context.RemotePilotPrivacy};
}
let p = page(null);
const shownText = p => p.body.textContent;
assert.ok(p.privacy.hidden && p.body.classList.contains('privacy-hidden'), 'hidden by default in a new browser');
assert.equal(p.button.getAttribute('aria-pressed'), 'true');
assert.equal(p.button.dataset.tip, pure.TIPS.hidden);
assert.match(p.button.querySelector('.privacy-icon').innerHTML, /<svg[^>]*crispEdges/);
assert.ok(!/192\.168|10\.123|482913|Example Home/.test(p.facts.textContent + p.code.textContent + p.tail.textContent), 'the page shows them as ****');
assert.equal(p.code.textContent, '******', 'the pairing code box is starred whole');
assert.equal(p.option.textContent, 'Configured: 10.123.45.9', 'drop-down choices and scripts are never rewritten');
assert.equal(p.script.textContent, 'const ip = "10.123.45.1";');
assert.equal(p.privacy.text(p.facts), 'Address 192.168.250.20', 'the real text stays available for comparisons');
// the page writes new text: it is masked as it arrives, and comparing with the real text stays equal
p.tail.textContent = 'host: media-box.local:8920 ready';
assert.ok(!p.tail.textContent.includes('media-box') && p.privacy.text(p.tail) === 'host: media-box.local:8920 ready',
  'new text is masked as it arrives; the real text is what the page compares against');
const iconHidden = p.button.querySelector('.privacy-icon').innerHTML;
p.button.click();
assert.ok(!p.privacy.hidden && !p.body.classList.contains('privacy-hidden') && p.button.getAttribute('aria-pressed') === 'false');
assert.equal(p.button.dataset.tip, pure.TIPS.shown);
assert.notEqual(p.button.querySelector('.privacy-icon').innerHTML, iconHidden, 'the eye changes');
assert.ok(p.facts.textContent === 'Address 192.168.250.20' && p.code.textContent === '482913' &&
  p.tail.textContent === 'host: media-box.local:8920 ready', 'one click shows the real text again');
assert.equal(p.store.get('remote-pilot-privacy'), 'shown', 'the choice is remembered in this browser');
p = page('shown');
assert.ok(!p.privacy.hidden && p.facts.textContent === 'Address 192.168.250.20', 'a browser that chose to show keeps showing');
p.button.click();
assert.ok(p.privacy.hidden && !p.store.has('remote-pilot-privacy') && !shownText(p).includes('192.168.250.20'), 'and can hide again');
// storage that throws (private window): still hidden, nothing breaks
assert.doesNotThrow(() => {
  const q = page(null);
  q.store.get = q.store.set = q.store.delete = () => { throw new Error('blocked'); };
  q.button.click(); q.button.click();
  assert.ok(q.privacy.hidden && !q.facts.textContent.includes('192.168'));
});
console.log('privacy toggle: hidden by default, text masked as it arrives, real text kept for comparisons, one click shows it, remembered per browser PASS');

// ---- the header, the Chromatic pixel style and honest notes on saved files ---------------------
const html = read('viewer.html');
const header = html.match(/<header class="topbar">[\s\S]*?<\/header>/)[0];
assert.match(header, /<button id="privacy-toggle"[^>]*data-privacy="keep"[^>]*data-tip="Private details are hidden/, 'the eye is in the header');
assert.ok(header.lastIndexOf('privacy-toggle') > header.indexOf('link-readout'), 'at the top right, after the link readout');
assert.match(viewer, /^installPrivacy\(\);\r?\ninstallTooltips\(\);\r?$/m, 'masking starts before anything else draws');
const chromaticCss = read('presets/chromatic/theme.css');
assert.match(chromaticCss, /body\[data-preset="chromatic"\] \.privacy-toggle:not\(\[data-chr-button\]\) \{[^}]*--ink: var\(--pm-outline\)/,
  'Chromatic: the grey bevelled pixel button with the eye in the outline colour');
assert.match(read('viewer.css'), /body\.privacy-hidden input\[data-private\]\{-webkit-text-security:disc\}/);
const panel = read('presets/chromatic/panel.js');
assert.equal((panel.match(/\$\{fileNote\(\)\}/g) || []).length, 5, 'screenshot, bundle, report, log copy and collect say the file is not masked');
assert.match(panel, /'data-private': ''/, 'the pairing code is all private');
assert.doesNotMatch(panel, /tail\.textContent !== text|\('chr-mgr-out'\)\.textContent !==|connect-detail'\)\.textContent !==/,
  'the panel compares against the real text, so masking never re-scrolls the log');
console.log('privacy layout: eye at the top right of the header, Chromatic pixel bevel, private inputs dotted, saved files flagged PASS');
