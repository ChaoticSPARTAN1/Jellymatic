// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
// Chromatic preset viewer controls: honest availability, gating and isolation from other presets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/presets/chromatic/panel.js`, 'utf8');

// Pure helpers, without any DOM.
const pure = vm.createContext({});
vm.runInContext(source, pure);
const chr = pure.RemotePilotChromatic;
assert.deepEqual([...chr.BUTTONS], ['UP', 'DOWN', 'LEFT', 'RIGHT', 'A', 'B', 'START', 'SELECT', 'MENU']);

const ALL = ['wifi-link', 'pairing', 'usb-serial', 'launcher-status', 'launcher-log', 'launcher-screen', 'launcher-video',
  'launcher-input', 'launcher-hold', 'launcher-console', 'launcher-perf', 'game-screen', 'game-video', 'game-input', 'game-audio'];
const features = (overrides = {}) => ALL.map(id => ({id, state: overrides[id] || 'available', detail: `${id} detail`}));
const fpga = {'game-screen': 'needs-fpga', 'game-video': 'needs-fpga', 'game-input': 'needs-fpga', 'game-audio': 'needs-fpga'};
const ready = {selected: true, linkEnabled: true, controlsArmed: true, mode: 'lan-sim', transport: {simulated: true, phase: 'launcher',
  target: 'launcher', launcherVersion: '1.1.0', label: 'Simulated CHR1 launcher', lines: 3}, tail: ['a'], features: features(fpga)};

for (const kind of ['input', 'hold', 'shot', 'video', 'log', 'console']) assert.equal(chr.controlState(ready, kind).enabled, true, kind);
assert.equal(chr.controlState(ready, 'audio').enabled, false);
assert.match(chr.controlState(ready, 'audio').reason, /game-audio detail/);
assert.match(chr.controlState({...ready, controlsArmed: false}, 'input').reason, /Enable remote controls/);
assert.equal(chr.controlState({...ready, controlsArmed: false}, 'shot').enabled, true);
assert.equal(chr.controlState({...ready, linkEnabled: false}, 'input').enabled, false);
assert.match(chr.controlState({...ready, linkEnabled: false}, 'shot').reason, /PC link disconnected/);
assert.match(chr.controlState({...ready, selected: false}, 'input').reason, /Select the Chromatic preset/);
assert.equal(chr.controlState(null, 'input').enabled, false);
// A game owns the screen: device controls follow the game-* features.
const game = {...ready, transport: {...ready.transport, target: 'game', phase: 'game'}};
assert.equal(chr.controlState(game, 'input').enabled, false);
assert.match(chr.controlState(game, 'shot').reason, /game-screen detail/);
const bridged = {...game, features: features({'launcher-screen': 'waiting', 'launcher-input': 'waiting'})};
for (const kind of ['input', 'hold', 'shot', 'video', 'audio']) assert.equal(chr.controlState(bridged, kind).enabled, true, kind);
const serial = {...ready, mode: 'serial-sim', features: features({...fpga, 'launcher-screen': 'needs-launcher', 'launcher-input': 'needs-launcher',
  'launcher-hold': 'needs-launcher', 'launcher-video': 'needs-launcher'})};
serial.features.find(item => item.id === 'launcher-input').detail = 'Needs launcher work: CHR1 over Wi-Fi';
assert.equal(chr.controlState(serial, 'input').enabled, false);
assert.match(chr.controlState(serial, 'input').reason, /Needs launcher work/);
assert.equal(chr.controlState(serial, 'log').enabled, true);
assert.equal(chr.controlState({...ready, mode: 'off'}, 'log').enabled, false);
assert.equal(chr.controlState({...ready, transport: {lines: 0}, tail: []}, 'log').enabled, false);
console.log('chromatic controls: follow the screen owner, the control lease, the link and honest availability PASS');

assert.deepEqual([chr.badge('needs-fpga').text, chr.badge('needs-launcher').text, chr.badge('available').text, chr.badge('n/a').text, chr.badge('nope').text],
  ['Needs FPGA update', 'Needs launcher work', 'Available', 'Not applicable', 'Unknown']);
assert.match(chr.connectText(ready), /^SIMULATOR/);
assert.match(chr.connectText({...ready, transport: {...ready.transport, phase: 'stock'}}), /FPGA update/);
assert.match(chr.connectText(game), /game is running/);
assert.match(chr.connectText({...ready, mode: 'off'}), /link is off/);
assert.doesNotMatch(chr.connectText({...ready, transport: {...ready.transport, simulated: false}}), /SIMULATOR/);
assert.deepEqual([...chr.modeOptions({configuredMode: 'lan'}).map(option => option.value)],
  ['off', 'lan-sim', 'lan-sim-fpga', 'serial-sim', 'usb-sim', 'auto-sim', 'configured']);
assert.match(chr.modeOptions({configuredMode: 'lan'})[6].label, /Wi-Fi/);
assert.match(chr.modeOptions({configuredMode: 'auto'})[6].label, /USB, else Wi-Fi/);
assert.ok(!chr.modeOptions({}).some(option => ['serial', 'lan', 'usb', 'auto'].includes(option.value)));
console.log('chromatic labels: honest badges, simulator marking, no direct real-transport choice PASS');

const pressed = indexes => ({buttons: Array.from({length: 17}, (_, i) => ({pressed: indexes.includes(i), value: indexes.includes(i) ? 1 : 0})), axes: [0, 0]});
assert.deepEqual([...chr.padButtons(pressed([0, 12]))], ['UP', 'A']);
assert.deepEqual([...chr.padButtons(pressed([1, 9, 8]))], ['B', 'START', 'SELECT']);
assert.deepEqual([...chr.padButtons(pressed([16, 4, 5, 6, 7]))], []);  // guide/shoulders never reach MENU
assert.deepEqual([...chr.padButtons({buttons: [], axes: [-0.9, 0.8]})], ['DOWN', 'LEFT']);
assert.deepEqual([...chr.padButtons(pressed([0, 1, 12, 13, 14, 15]))].length, 4);
assert.ok(!Object.values(chr.PAD_MAP).includes('MENU'));
assert.deepEqual(JSON.parse(JSON.stringify(chr.chordPayload(['B', 'A']))), {buttons: ['A', 'B'], ms: 100});
assert.throws(() => chr.chordPayload(['MENU', 'A']), /MENU/);
assert.throws(() => chr.chordPayload([]), /1 to 4/);
assert.throws(() => chr.chordPayload(['UP', 'DOWN', 'LEFT', 'RIGHT', 'A']), /1 to 4/);
console.log('chromatic input: controller mapping without MENU, ordered chords of up to four PASS');

// With another preset selected the script builds nothing and sends no request.
let fetches = 0, observed = null;
const body = {dataset: {preset: 'example'}};
const dom = vm.createContext({
  document: {body, getElementById: () => { throw new Error('DOM touched'); }, querySelector: () => { throw new Error('DOM touched'); },
    querySelectorAll: () => { throw new Error('DOM touched'); }, createElement: () => { throw new Error('DOM touched'); }},
  MutationObserver: class { constructor(callback) { observed = callback; } observe(target, options) {
    assert.equal(target, body); assert.deepEqual([...options.attributeFilter], ['data-preset']); } },
  crypto: require('node:crypto').webcrypto,
  fetch: () => { fetches += 1; return new Promise(() => {}); }, setInterval: () => 1, clearInterval: () => {}, setTimeout,
});
vm.runInContext(source, dom);
for (const preset of ['example', 'another-preset']) {
  body.dataset.preset = preset; observed();
}
assert.equal(fetches, 0);
console.log('chromatic isolation: other presets never build the panel or poll the Chromatic API PASS');


// The device drawing: every Chromatic button has a place on the console layout, Menu sits on the
// right side level with the bottom of the screen bezel, the edge controls are drawn, colours come
// from the theme's DEVICE COLOURS block, and no ModRetro wordmark is drawn.
const art = chr.deviceArt();
for (const name of [...chr.BUTTONS, 'screen']) {
  const box = art.parts[name];
  assert.ok(box && box.left >= 0 && box.top >= 0 && box.left + box.width <= 100.001 && box.top + box.height <= 100.001, name);
  if (name !== 'screen') assert.match(chr.buttonArt(name), /chr-k-face/, name);
}
const [menuX, menuY, , menuH] = chr.PARTS.MENU, bezelBottom = 50;  // the bezel spans shell rows 4..50
assert.ok(menuX >= 60 && menuY < bezelBottom && menuY + menuH > bezelBottom, 'Menu on the right side at the bezel bottom');
assert.ok(chr.PARTS.A[0] > chr.PARTS.B[0] && chr.PARTS.A[1] < chr.PARTS.B[1], 'A upper right of B');
assert.deepEqual([...chr.SIDE.map(([title]) => title.split(' (')[0])],
  ['Power switch', 'Volume wheel', 'Link cable port', 'USB-C port', 'Headphone jack']);
for (const key of ['body', 'bezel', 'stripe-1', 'stripe-2', 'stripe-3', 'stripe-4', 'print', 'wheel', 'port', 'label'])
  assert.match(art.svg, new RegExp(`chr-k-${key}\\b`), key);
assert.doesNotMatch(art.svg, /MODRETRO|CHROMATIC|SAPPHIRE/i);
for (const key of ['body', 'bezel', 'button', 'ab', 'stripe-1', 'stripe-4', 'pressed'])
  assert.match(fs.readFileSync(`${__dirname}/presets/chromatic/theme.css`, 'utf8'), new RegExp(`--dev-${key}:\\s*#[0-9a-f]{6}`), key);
console.log('chromatic device drawing: console layout, side Menu, edge controls, one colour block, no wordmarks PASS');

// The shared viewer hook loads only the manifest's validated panel script.
const viewer = fs.readFileSync(`${__dirname}/viewer.js`, 'utf8');
assert.match(viewer, /if \(preset\.panelScript && !document\.querySelector\(`script\[data-preset-panel="\$\{preset\.id\}"\]`\)\)/);
assert.doesNotMatch(viewer, /chromatic/i);
for (const file of ['viewer.html', 'viewer.css', 'themes.css'])
  assert.doesNotMatch(fs.readFileSync(`${__dirname}/${file}`, 'utf8'), /chromatic/i, file);
const css = fs.readFileSync(`${__dirname}/presets/chromatic/theme.css`, 'utf8');
for (const rule of css.replace(/\/\*[\s\S]*?\*\//g, '').split('}').map(part => part.split('{')[0].trim()).filter(Boolean)) {
  if (rule.startsWith('@media') || rule === '@font-face' || rule.startsWith('.chr-') || rule.startsWith('#chromatic-stage')) continue;
  for (const selector of rule.split(','))
    assert.match(selector.trim(), /^body(\[data-preset="chromatic"\]|:not\(\[data-preset="chromatic"\]\) #chromatic-)/, selector);
}
console.log('chromatic viewer hook: generic panelScript loader, shared files name no device, scoped stylesheet PASS');

// Connect without a config: the console's USB port is only listed; one "Allow USB" click from this PC opens it.
const offerStatus = {selected: true, linkEnabled: true, localClient: true, mode: 'off', transport: {},
  usbOffer: {state: 'found', ports: ['COM7'], message: 'Found the Chromatic on COM7 (USB vendor ID 374E); the port was not opened. Press Allow USB on COM7 to let Remote Pilot open it.'}};
const offer = chr.usbOfferView(offerStatus);
assert.deepEqual([offer.visible, [...offer.ports], offer.canAllow], [true, ['COM7'], true]);
assert.match(offer.text, /config\.json, which Git ignores/);
assert.equal(chr.usbOfferView({...offerStatus, localClient: false}).canAllow, false);
assert.match(chr.usbOfferView({...offerStatus, localClient: false}).text, /this PC's own browser/);
assert.equal(chr.usbOfferView({...offerStatus, linkEnabled: false}).canAllow, false);
assert.equal(chr.usbOfferView({...offerStatus, selected: false}).visible, false);
assert.equal(chr.usbOfferView({...offerStatus, usbOffer: null}).visible, false);
// The USB hand-off sign: amber while Chromatic Manager holds USB, green once it is back.
const held = chr.usbHoldView({localClient: true, usbHold: {held: true, holder: 'Chromatic Manager', reason: 'Install the start-up menu', holderRunning: true}});
assert.equal(held.visible && held.state, 'paused');
assert.match(held.text, /Chromatic Manager is using the USB cable \(Install the start-up menu\)\. Wi-Fi keeps working; USB is back in a moment\./);
assert.equal(held.canTake, false);
const stale = chr.usbHoldView({localClient: true, usbHold: {held: true, holder: 'Chromatic Manager', reason: '', holderRunning: false}});
assert.ok(stale.canTake && /press Use USB again/.test(stale.text) && /Finish it/.test(stale.text));
assert.equal(chr.usbHoldView({localClient: false, usbHold: {held: true, holder: 'Chromatic Manager', holderRunning: false}}).canTake, false);
assert.match(chr.usbHoldView({localClient: true, usbHold: {held: true, unreadable: true, holder: 'unknown'}}).text, /^Another program/);
assert.equal(chr.usbHoldView({usbHold: null}, true).state, 'back');
assert.equal(chr.usbHoldView({usbHold: null}).visible, false);
const none = chr.usbOfferView({...offerStatus, usbOffer: {state: 'none', ports: [], message: 'No Chromatic USB port found'}});
assert.deepEqual([none.visible, none.ports.length, none.canAllow], [false, 0, false]);  // the connect text explains it
assert.match(chr.connectText(offerStatus), /Allow USB on COM7/);
assert.match(chr.connectText({...offerStatus, usbOffer: null}), /Press Connect/);
// pyserial missing is named as such, never as an unplugged cable.
const noSerial = {...ready, mode: 'usb', transport: {kind: 'usb', usbState: 'no-pyserial', label: 'USB (CHR1)',
  error: 'USB support (pyserial) is not installed in this Python'}};
assert.match(chr.connectText(noSerial), /pyserial\) is not installed in this Python/);
assert.doesNotMatch(chr.connectText(noSerial), /not connected/);
assert.equal(chr.pairingView(noSerial).canStart, false);
console.log('chromatic connect: USB port listed, one local Allow click, pyserial named when missing PASS');

// Only presets with a dual-screen model are marked as dual-screen; Chromatic is a single-screen device.
assert.match(viewer, /document\.body\.dataset\.deviceFamily = \(preset\?\.models \|\| \[\]\)\.some\(model => model === 'basic' \|\| model === 'dual-stick'\) \? 'dual-screen' : 'other';/);
const family = id => {
  const manifest = JSON.parse(fs.readFileSync(`${__dirname}/presets/${id}/manifest.json`, 'utf8'));
  return manifest.models.some(model => model === 'basic' || model === 'dual-stick') ? 'dual-screen' : 'other';
};
assert.equal(family('chromatic'), 'other');
assert.equal(family('example'), 'dual-screen');
console.log('device family: Chromatic single-screen, Example dual-screen PASS');
