// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
// Pure input state shared by mouse, touch pointers and every preset.
class PointerPad {
  constructor() { this.inputs = new Map(); }
  stick(id, side, x, y) {
    const length = Math.max(1, Math.hypot(x, y));
    const input = {side, x: Number((x / length).toFixed(2)), y: Number((y / length).toFixed(2))};
    this.inputs.set(id, input);
    return input;
  }
  button(id, name) {
    const input = {button: name};
    this.inputs.set(id, input);
    return input;
  }
  release(id, expected) {
    if (!expected || this.inputs.get(id) === expected) this.inputs.delete(id);
  }
  clear() { this.inputs.clear(); }
  snapshot() {
    const result = {moveX: 0, moveY: 0, lookX: 0, lookY: 0,
      hasMove: false, hasLook: false, buttons: new Set(), active: this.inputs.size > 0};
    for (const input of this.inputs.values()) {
      if (input.button) result.buttons.add(input.button);
      else {
        result[`${input.side}X`] = input.x;
        result[`${input.side}Y`] = input.y;
        result[input.side === 'move' ? 'hasMove' : 'hasLook'] = true;
      }
    }
    return result;
  }
}

function mergePointerPad(hardware, pointer, limitedBasic, mapping) {
  const buttons = new Set((hardware.buttons || 'NONE').split(',').filter(name => name !== 'NONE'));
  for (const name of pointer.buttons) buttons.add(name);
  if (limitedBasic) { buttons.delete('ZL'); buttons.delete('ZR'); }
  let padMask = hardware.padMask || 0;
  for (const [index, name] of Object.entries(mapping))
    if (buttons.has(name)) padMask |= 1 << Number(index);
  if (limitedBasic) padMask &= ~((1 << 6) | (1 << 7));
  return {...hardware, frames: 30, buttons: [...buttons].join(',') || 'NONE', padMask,
    moveX: pointer.hasMove ? pointer.moveX : hardware.moveX,
    moveY: pointer.hasMove ? pointer.moveY : hardware.moveY,
    lookX: limitedBasic ? 0 : pointer.hasLook ? pointer.lookX : hardware.lookX,
    lookY: limitedBasic ? 0 : pointer.hasLook ? pointer.lookY : hardware.lookY,
    lt: limitedBasic ? 0 : Math.max(hardware.lt || 0, buttons.has('ZL') ? 1 : 0),
    rt: limitedBasic ? 0 : Math.max(hardware.rt || 0, buttons.has('ZR') ? 1 : 0)};
}

function hasPadInput(payload) {
  return payload.buttons !== 'NONE' || ['moveX', 'moveY', 'lookX', 'lookY', 'lt', 'rt']
    .some(name => Math.abs(payload[name] || 0) > 0);
}

function perfGraphMax(history) {
  return Math.max(33.3, ...history.flatMap(point => [point.frame, point.work])) * 1.12;
}

function bottomTouchPoint(box, clientX, clientY) {
  return {x: Math.max(0, Math.min(319, Math.floor((clientX - box.left) * 320 / box.width))),
    y: Math.max(0, Math.min(239, Math.floor((clientY - box.top) * 240 / box.height)))};
}

class TouchPad {
  constructor() { this.press = null; }
  begin(id, point, now) {
    if (this.press) return null;
    this.press = {id, ...point, now}; return this.press;
  }
  move(id, point) { if (this.press?.id === id) Object.assign(this.press, point); }
  release(expected) { if (this.press === expected) this.press = null; }
  clear() { this.press = null; }
  sample() { return this.press ? {x:this.press.x,y:this.press.y,down:true} : {x:0,y:0,down:false}; }
}

function remoteFeatureGuidance(s) {
  const ready = s.connected && s.session && s.linkEnabled;
  const hint = 'On your device, start the selected build and its PC link.';
  const blocked = {tone:'waiting',location:!s.linkEnabled?'ON THIS PC':'ON YOUR DEVICE',title:'Connect the device first',
    text:!s.linkEnabled ? 'The PC connection is paused. Use Connect to device above to resume.' : !s.session ? hint : 'Keep the selected device running. Waiting for its paired connection.',
    next:'Next: enable controls or request a screen image when the paired device connects.'};
  let control = !ready ? {...blocked} : {tone:'ready',location:'ON THIS PC',title:'Enable remote controls',
    text:'Turn on Enable remote controls below. Then use the mouse, touch screen or a supported controller.',next:'Next: keep this tab focused while sending input. Turning the switch off stops remote control.'};
  if(s.example)control={tone:'neutral',location:'ON THIS PC',title:'Local preview only',text:'Try the sticks and buttons locally. This preset sends no input to a device.',next:'Next: choose a device preset and connect it.'};
  else if(!s.controlSupported)control={tone:'neutral',location:'ON THIS PC',title:'Remote control unavailable',text:'This preset does not provide remote control. Select a supported device preset.',next:'Next: connect a device build that supports Remote Pilot input.'};
  else if(ready&&s.error)control={tone:'waiting',location:'ON THIS PC',title:'Remote input stopped',text:s.error,next:'Next: check the device connection, then enable remote controls again.'};
  else if(ready&&s.enabled&&!s.focused)control={tone:'waiting',location:'ON THIS PC',title:'Input paused · tab not focused',text:'Click back into this page to use remote controls. No input is sent while this tab is inactive.',next:'Next: keep this page focused while testing.'};
  else if(ready&&s.enabled&&s.manualOnly&&!s.manualArmed)control={tone:'waiting',location:'ON THIS PC',title:'Waiting to arm button controls',text:'Waiting for the PC receiver to confirm the control session. Sticks and touch remain local preview in this build.',next:'Next: use Quick controls once Buttons armed is confirmed.'};
  else if(ready&&s.enabled)control={tone:'ready',location:'ON THIS PC',title:s.manualOnly?'Button controls armed':'Remote controls enabled',text:s.manualOnly?'Use Quick controls for the supported button presses. Sticks and touch are local preview.':'Use the mouse sticks, device buttons or connected controller. The lower screen accepts touch when supported.',next:'Next: watch the device response. Turn off Enable remote controls when finished.'};
  let capture=!ready?{...blocked}:{tone:'ready',location:'ON THIS PC',title:'Choose how to view the screens',text:'Use Grab full screens for a saved image, or turn on Live preview for incoming frames.',next:'Next: the screen status below confirms a saved image or received live frames.'};
  if(s.example)capture={tone:'neutral',location:'ON THIS PC',title:'Screen preview needs a device',text:'This example contains a local device illustration. No device screen image is being received.',next:'Next: select and connect a supported device preset.'};
  else if(!s.captureSupported)capture={tone:'neutral',location:'ON THIS PC',title:'Screen capture unavailable',text:'This preset does not provide screen capture or live video.',next:'Next: use a supported device build for screen capture.'};
  else if(ready&&s.live)capture={tone:s.streamFresh?'ready':'waiting',location:'ON YOUR DEVICE',title:s.streamFresh?'Live frames received':'Waiting for live frames',text:s.streamFresh?'The screen view is receiving frames. The badge shows the measured delivery rate.':'Keep the device running while the screen stream connects. Requested FPS is a limit, not a delivery guarantee.',next:'Next: turn Live preview off to stop requesting frames. Saved captures remain available.'};
  else if(ready&&s.savedCapture)capture={tone:'ready',location:'ON THIS PC',title:'Saved screen image available',text:'You are viewing a saved capture. Grab full screens again to update it, or enable Live preview.',next:'Next: use the session log for the capture result and saved files.'};
  return {control,capture,quick:!ready?blocked.text:s.manualOnly?'Enable remote controls, then use a supported button below. Each press is bounded.':'Use a button for one bounded press. Use remote controls for continuous sticks or controller input.',
    quickNext:!ready?'Next: connect the selected device.':s.manualOnly?'Next: watch the device response; release Ctrl to send a staged button combination.':'Next: check Requests and results for acknowledgements, captures and command output.'};
}

function presetApplicationTheme(preset, fallback) {
  return {...fallback,...preset?.theme};
}

// Text of one plain-language tooltip: the element's explanation (data-tip) and, on its own line,
// what is true right now (data-tip-now, usually why a control is unavailable). Either may be empty.
function tooltipLines(tip, now) {
  const base = String(tip || '').trim(), live = String(now || '').trim();
  if (!base) return {text: live, now: ''};
  return {text: base, now: live && !base.includes(live) ? `Right now: ${live}` : ''};
}

// ---- private details: masked on screen by default ----------------------
// The same rules as the PC Manager app's mask_private (its core module): every private
// character becomes '*', while dots, colons, slashes and dashes stay, so an address still reads as
// an address. Display only: requests, saved files and the receiver keep the real text.
const PRIVACY_STORE = 'remote-pilot-privacy';  // 'shown' = this browser chose to show private details
const PRIVACY_TIPS = {
  hidden: 'Private details are hidden: addresses, network names, device IDs, pairing codes and folders show as ****. Click to show them.',
  shown: 'Private details are showing. Click to hide them (shown as ****) before you take a screenshot.',
};
const PRIVACY_FILE_NOTE = ' The saved file is not masked, so it can hold private details.';
const PRIVACY_ICONS = {  // the PC Manager app's pixel eye, 11 x 9 cells (its theme's TAB_ICONS)
  shown: ['...........', '....###....', '..##...##..', '.#...#...#.', '#...###...#', '.#...#...#.',
    '..##...##..', '....###....', '...........'],
  hidden: ['.........#.', '....###.#..', '..##...#...', '.#...##..#.', '#...##....#', '.#..#....#.',
    '..##...##..', '..#.###....', '.#.........'],
};
const PRIVATE_RULES = (() => {
  const keys = '(?:wi-?fi\\s+)?(?:network(?:\\s+name)?|ssid)|serial(?:\\s+(?:number|no\\.?))?|device\\s+id|chip\\s+id|' +
    'mac(?:\\s+address)?|user(?:\\s*name)?|account|server(?:\\s+address)?|host(?:\\s*name)?|address|ip|port|' +
    'token|api\\s+key|access\\s+key|password|pass\\s*phrase|psk|pairing(?:\\s+(?:code|key|secret))?|pin';
  return {
    url: /\b(?:https?|wss?|tcp|udp|rtsp):\/\/[^\s"'<>]+/gi,
    labelled: new RegExp(`\\b(${keys})(\\s*[:=]\\s*|\\s+(?:named\\s+|called\\s+)?(?=["']))("[^"\\n]*"|'[^'\\n]*'|[^\\s,;)\\]]+)`, 'gi'),
    code: /\b((?:pairing\s+)?code\s+)(\d{4,8})\b/gi,
    quotedWifi: /(\b(?:wi-?fi|network|ssid|joined|connected\s+to|saved)\b[^"\n]{0,24})"([^"\n]+)"/gi,
    home: /(?<=[\\/])(?:Users|home)[\\/]([^\\/\s"'<>|,;)]+)/gi,
    privateDir: /(?<![\w-])(?:private|[A-Za-z]+Backups)[\\/]([^\s"'<>|,;)]+)/gi,
    mac: /\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b/g,
    ipv6: /(?<![\w:.])(?:(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6}))(?:%\w+)?(?![\w:])/g,
    ipv4: /(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])/g,
    localHost: /(?<![\w.-])[A-Za-z0-9][\w-]*(?:\.[\w-]+)*\.(?:local|lan|home|internal|localdomain|home\.arpa)(?![\w.-])/gi,
    hostPort: /(?<![\w.*])(?=[\w.*-]*[A-Za-z*])[A-Za-z0-9*][\w.*-]*:\d{2,5}(?![\w:])/g,
    port: /\b(ports?\s+)(\d{2,5})\b/gi,
    hex: /\b(?=[0-9]*[A-Fa-f]|[0-9A-Fa-f]{16})[0-9A-Fa-f]{12,}\b/g,
    token: /\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{28,}\b/g,
  };
})();
function privacyStars(text) { return String(text).replace(/[^.:/\\\s-]/g, '*'); }
// `text` as the page shows it while private details are hidden. With `all`, every character but
// spaces is starred (for an element that holds nothing but a private value, such as a pairing code).
function maskPrivate(text, all = false) {
  text = text == null ? '' : String(text);
  if (!text) return text;
  if (all) return text.replace(/\S/g, '*');
  const r = PRIVATE_RULES;
  text = text.replace(r.url, url => { const [scheme, rest] = url.split('://'); return `${scheme}://${privacyStars(rest)}`; });
  text = text.replace(r.labelled, (m, key, sep, value) => value.includes("://") ? m : key + sep + privacyStars(value));
  text = text.replace(r.code, (m, key, value) => key + privacyStars(value));
  text = text.replace(r.quotedWifi, (m, lead, name) => `${lead}"${privacyStars(name)}"`);
  text = text.replace(r.home, (m, name) => m.slice(0, m.length - name.length) + privacyStars(name));
  text = text.replace(r.privateDir, (m, rest) => m.slice(0, m.length - rest.length) + privacyStars(rest));
  for (const rule of [r.mac, r.ipv6, r.ipv4, r.localHost, r.hostPort]) text = text.replace(rule, privacyStars);
  text = text.replace(r.port, (m, key, value) => key + privacyStars(value));
  text = text.replace(r.hex, hex => hex.length === 40 || hex.length === 64 ? hex : privacyStars(hex));
  return text.replace(r.token, privacyStars);
}
// The pixel eye as an SVG drawn cell by cell (currentColor), like the Manager's tab icons.
function privacyIconSvg(hidden) {
  const grid = PRIVACY_ICONS[hidden ? 'hidden' : 'shown'], rects = [];
  grid.forEach((row, y) => { for (let x = 0; x < row.length; x++) if (row[x] === '#') rects.push(`<rect x="${x}" y="${y}" width="1" height="1"/>`); });
  return `<svg viewBox="0 0 ${grid[0].length} ${grid.length}" shape-rendering="crispEdges" fill="currentColor" aria-hidden="true" focusable="false">${rects.join('')}</svg>`;
}
// What an element really says (its text before masking): use it to compare before rewriting text.
function privacyText(node) { return globalThis.RemotePilotPrivacy?.text(node) ?? node.textContent; }

const $ = (id) => document.getElementById(id);
const offlineTheme = {background: '#07150d', surface: '#12251a', surface2: '#0d1d13',
  line: '#376b43', text: '#dcffe1', muted: '#a3c8a8', accent: '#53e875',
  accent2: '#a8ffc1', glow: '#3ddf68'};
let presets = [];
let selectedPreset = '';
let previewSelectedTheme = false;
let previewMapId = '';
let perfLastTimestamp = null;
let perfHistory = [];
let perfScale = 33.4;
let perfRequestAt = 0;
let perfChanging = false;
function activePreset() { return presets.find(item => item.id === selectedPreset); }

function renderPresetTheme() {
  const preset = activePreset();
  const theme = presetApplicationTheme(preset,offlineTheme);
  for (const [name,color] of Object.entries(theme))
    document.documentElement.style.setProperty(`--rp-${name}`,color);
  document.body.dataset.theme = preset ? selectedPreset : 'offline';
  for (const field of ['background','surface','accent','text']) {
    $(`theme-${field}`).value = theme[field];
  }
  $('theme-role-note').hidden = true;
}

function renderPreset() {
  cancelChord();
  const preset = activePreset();
  const example = selectedPreset === 'example';
  document.querySelector('.connect-bar').hidden = false;
  $('capture-note').hidden = false;
  $('enable-pad').disabled = example;
  document.querySelector('.enable-line').hidden = false;
  if (example) $('enable-pad').checked = false;
  const previewDefault = ['1000', '500', '333', '200', '125', '83', '42'].includes(String(preset?.previewIntervalMs)) ?
    String(preset.previewIntervalMs) : '42';
  try {
    const saved = localStorage.getItem(`remote-pilot-preview-rate-${selectedPreset}`);
    $('preview-rate').value = ['1000', '500', '333', '200', '125', '83', '42'].includes(saved) ? saved :
      previewDefault;
  } catch (_) { $('preview-rate').value = previewDefault; }
  $('game-label').textContent = 'Remote Pilot';
  $('brand-mark').textContent = preset?.mark || 'R';
  $('game-preset').value = selectedPreset;
  document.body.dataset.preset = selectedPreset;
  // Dual-screen parts (the drawn two-screen device) show only for presets with a dual-screen model.
  document.body.dataset.deviceFamily = (preset?.models || []).some(model => model === 'basic' || model === 'dual-stick') ? 'dual-screen' : 'other';
  renderPresetTheme();
  const capabilities = new Set(preset?.capabilities || []);
  $('intro').textContent = capabilities.has('controller') ?
    'View the device screen and test it from this PC. Controller input is opt in; physical device controls take priority.' :
    'View the device screen and test it from this PC. Manual controls are bounded; physical device controls take priority.';
  if (example) $('intro').textContent = 'Example preset: try the device and controller previews locally. This preset has no device connection.';
  document.querySelector('.top-screen .screen-placeholder').textContent = example ? 'Example top screen' : 'Awaiting screen image';
  document.querySelector('.bottom-screen .screen-placeholder').textContent = example ? 'Example touch screen' : 'Awaiting screen image';
  for (const selector of ['.capture-actions', '.controls', '.pilot', '.activity']) {
    document.querySelector(selector).hidden = false;
  }
  $('sound').hidden = false;
  $('console-form').hidden = false;
  $('perf-section').hidden = false;
  $('request-report').hidden = false;
  $('request-device-log').hidden = false;
  document.querySelectorAll('[data-button]').forEach(button => {
    button.hidden = false;
  });
  document.querySelectorAll('.motion,.advanced').forEach(element => {
    element.hidden = false;
  });
  $('model-detail').textContent = deviceModel === 'basic' ?
    (preset?.controllerBasicFull ? 'Basic layout: remote controller sticks and triggers use this preset’s mappings.' :
      'Basic layout: no second stick or ZL/ZR input.') :
    'Second-stick layout: right stick controls the second stick; triggers control ZL/ZR.';
  for (const [selector, text] of [
    ['.mouse-hint', 'Use the Quick controls buttons after enabling remote controls. Sticks, screen dragging and touch are preview only.'],
    ['.pilot > p.muted', 'This preset supports single Quick controls button presses. Enable remote controls below; mouse sticks and gamepad input are preview only.'],
  ]) {
    const element = document.querySelector(selector);
    if (element) {
      element.dataset.defaultCopy ??= privacyText(element);
      element.textContent = preset?.manualInputRequiresArm ? text : element.dataset.defaultCopy;
    }
  }
  if (preset?.manualInputRequiresArm) $('model-detail').textContent='Analog sticks and extra trigger input are unavailable for this preset.';
  updateFeatureControls();
}

function updateFeatureControls() {
  const preset = activePreset();
  const capabilities = new Set(preset?.capabilities || []);
  const example = selectedPreset === 'example';
  const ready = !example && connected && sessionOn && linkEnabled;
  const set = (element, supported) => {
    element.disabled = !supported || !ready;
    element.title = example ? 'Example is a local preview with no device connection.' :
      !supported ? 'This feature is unavailable for the selected preset.' :
      !ready ? 'Connect the selected device to use this feature.' : '';
  };
  for (const id of ['shot', 'auto']) set($(id), capabilities.has('screenshot'));
  for (const id of ['live', 'preview-rate']) set($(id), capabilities.has('stream'));
  const audioSupported = capabilities.has('audio') && (!preset?.audioRequiresDeviceCapture || deviceMeta?.audioCapture === true);
  set($('sound'), audioSupported);
  if (ready && capabilities.has('audio') && !audioSupported)
    $('sound').title = 'Live audio requires an updated build on the device.';
  if (preset?.audioRequiresDeviceCapture && !audioSupported) $('sound').textContent='Sound needs SD update';
  else if (!audioContext) $('sound').textContent='Enable sound';
  set($('enable-pad'), capabilities.has('controller') || preset?.manualInputRequiresArm === true);
  set($('request-report'), preset?.remoteReport === true);
  set($('request-device-log'), preset?.remoteLog === true);
  $('link-toggle').disabled = example;
  for (const element of document.querySelectorAll('#console-form input,#console-form button'))
    set(element, capabilities.has('console'));
  for (const button of document.querySelectorAll('[data-button]'))
    set(button, buttonInputAllowed(button.dataset.button) || buttonPadAllowed(button.dataset.button));
  for (const element of document.querySelectorAll('.motion button,#motion-form input,#motion-form button'))
    set(element, capabilities.has('input') && preset?.analogInput !== false &&
      (deviceModel !== 'basic' || !['lookX', 'lookY'].includes(element.name) &&
       !['turn-left', 'turn-right'].includes(element.dataset.motion)));
  $('controls-note').textContent = example ? 'Offline stub: drag the device and controller previews to try local input.' :
    preset?.manualInputRequiresArm ? 'Enable remote controls to send the listed buttons. Sticks and touch are unavailable for this preset.' :
    !capabilities.has('console') ? 'Bounded device input is available when connected. Console commands are unavailable for this preset.' :
    'Single, bounded presses and console commands are available when connected.';
  $('capture-note').textContent = example ? 'Offline stub: capture, streaming, sound and device logs require a connected device preset.' :
    ready && preset?.audioRequiresDeviceCapture && !audioSupported ?
    'Live video works with the installed build. Live sound requires a native app update; this exploration preview has no game audio.' :
    'Full screens and available logs stay in the private receiver folder. Live preview shows its measured delivery rate.';
  updateFeatureGuidance();
}

function updateFeatureGuidance() {
  if (!$('remote-control-guide')) return;
  const preset=activePreset(), caps=new Set(preset?.capabilities || []);
  const view=remoteFeatureGuidance({preset:selectedPreset,example:selectedPreset==='example',connected,session:sessionOn,linkEnabled,
    controlSupported:caps.has('controller')||preset?.manualInputRequiresArm===true,enabled:$('enable-pad').checked,
    focused:controlsFocused(),manualOnly:preset?.manualInputRequiresArm===true,manualArmed,error:lastPadError,
    captureSupported:caps.has('screenshot')||caps.has('stream'),live:$('live').checked,
    streamFresh:Boolean(streamVersion&&Date.now()-streamSeenAt<4000),savedCapture:Boolean(captureVersion)});
  for(const [id,guide] of [['remote-control',view.control],['remote-capture',view.capture]]) {
    $(`${id}-guide`).dataset.tone=guide.tone;
    for(const [part,value] of Object.entries({location:guide.location,title:guide.title,text:guide.text,next:guide.next})) {
      const node=$(`${id}-${part}`);if(node.textContent!==value)node.textContent=value;
    }
  }
  if($('quick-flow-text').textContent!==view.quick)$('quick-flow-text').textContent=view.quick;
  if($('quick-flow-next').textContent!==view.quickNext)$('quick-flow-next').textContent=view.quickNext;
}

async function loadPresets() {
  const response = await fetch('/api/presets', {cache: 'no-store'});
  if (!response.ok) throw new Error('Preset list unavailable');
  const state = await response.json();
  presets = state.presets || [];
  for (const preset of presets) {
    if (preset.styleSheet && !document.querySelector(`link[data-preset-style="${preset.id}"]`)) {
      const style = document.createElement('link');
      style.rel = 'stylesheet'; style.href = preset.styleSheet;
      style.dataset.presetStyle = preset.id;
      document.head.append(style);
    }
    // Optional preset-owned UI (validated path /presets/<id>/panel.js); it acts only
    // while body[data-preset] names its preset.
    if (preset.panelScript && !document.querySelector(`script[data-preset-panel="${preset.id}"]`)) {
      const script = document.createElement('script');
      script.src = preset.panelScript; script.dataset.presetPanel = preset.id;
      document.head.append(script);
    }
  }
  selectedPreset = state.selected || presets[0]?.id || '';
  $('game-preset').replaceChildren();
  for (const preset of presets) {
    const option = document.createElement('option');
    option.value = preset.id;
    option.textContent = preset.name;
    $('game-preset').append(option);
  }
  renderPreset();
}

let captureVersion = null;
let captureTimeMs = null;
let streamVersion = null;
let streamError = '';
let audioVersion = null;
let audioPlayedVersion = 0;
let audioContext = null;
let audioNextTime = 0;
let audioBusy = false;
let streamSeenAt = 0;
let streamTimes = [];
let nextStreamRequest = 0;
let displayedPath = '';
let connected = false;
let deviceMeta = null;
const inputOwner = Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');
let manualArmed = false, armBusy = false;
async function updateManualArm(enabled) {
  if (!activePreset()?.manualInputRequiresArm) return;
  if (!enabled) { manualArmed=false; $('enable-pad').checked=false; cancelChord(); stopDriving(); updateFeatureControls(); }
  if (armBusy && enabled) return;
  armBusy=true;
  try { await request('/api/control-arm',{enabled,owner:inputOwner},!enabled); manualArmed=enabled && $('enable-pad').checked && controlsFocused(); }
  catch (error) { manualArmed=false; $('enable-pad').checked=false; feedback(error.message,true); }
  finally { armBusy=false; updateFeatureControls(); }
}
setInterval(()=>{
  if (!activePreset()?.manualInputRequiresArm) return;
  if ($('enable-pad').checked && controlsFocused() && connected && sessionOn && linkEnabled) updateManualArm(true);
  else if (manualArmed || $('enable-pad').checked) updateManualArm(false);
},500);
let discoveryStatus = '';
let sessionOn = false;
let linkEnabled = true;
let stateBusy = false;
let streamBusy = false;
let padBusy = false;
let pendingPad = null;
let wasDriving = false;
let nextPadSend = 0;
let chordUntil = 0;
const chordButtons = new Set();
let lastPadError = '';
let deviceModel = 'dual-stick';
try { if (localStorage.getItem('remote-pilot-device-model') === 'basic') deviceModel = 'basic'; } catch (_) {}
const neutral = {buttons: 'NONE', frames: 1, moveX: 0, moveY: 0, lookX: 0, lookY: 0,
  padMask: 0, lt: 0, rt: 0};
const buttonMap = {0: 'A', 1: 'B', 2: 'X', 3: 'Y', 4: 'L', 5: 'R', 6: 'ZL', 7: 'ZR',
  8: 'SELECT', 9: 'START', 12: 'UP', 13: 'DOWN', 14: 'LEFT', 15: 'RIGHT'};
const pointerPad = new PointerPad();
const pointerGestures = new Map();
const touchPad = new TouchPad();
let touchBusy = false, pendingTouch = null, wasTouching = false, nextTouchSend = 0;

function chordAllowed(name) {
  return connected && sessionOn && linkEnabled &&
    (buttonInputAllowed(name) || buttonPadAllowed(name));
}

function buttonInputAllowed(name) {
  const preset = activePreset();
  return Boolean(preset?.capabilities.includes('input') &&
    (!preset.manualInputRequiresArm || manualArmed && $('enable-pad').checked && controlsFocused()) &&
    (!preset.inputButtons || preset.inputButtons.includes(name)) &&
    (deviceModel !== 'basic' || preset.controllerBasicFull || !['ZL', 'ZR'].includes(name)));
}

function buttonPadAllowed(name) {
  const preset = activePreset();
  return Boolean(preset?.capabilities.includes('controller') &&
    Object.values(buttonMap).includes(name) &&
    (deviceModel !== 'basic' || preset.controllerBasicFull || !['ZL', 'ZR'].includes(name)));
}

function padMaskFor(names) {
  return Object.entries(buttonMap).reduce((mask, [bit, name]) =>
    names.includes(name) ? mask | (1 << Number(bit)) : mask, 0);
}

async function pressPadButtons(names) {
  chordUntil = performance.now() + 350;
  try {
    await request('/api/gamepad', {padMask: padMaskFor(names), frames: 8});
    feedback(`Pressed ${names.join(' + ')} together.`);
  } catch (error) { feedback(error.message || 'Controller press failed.', true); }
}

function cancelChord() {
  chordButtons.clear();
  document.querySelectorAll('[data-button],[data-visual]').forEach(element =>
    element.classList.remove('chord-selected'));
  const status = $('chord-status');
  if (status) status.textContent = activePreset()?.manualInputRequiresArm ? 'This preset accepts one bounded button press at a time.' : 'Hold Ctrl, click buttons, then release Ctrl to press them together.';
}

function stageChord(name) {
  if (!chordAllowed(name)) { feedback('This button is unavailable for the selected device.', true); return; }
  if (chordButtons.has(name)) chordButtons.delete(name); else chordButtons.add(name);
  document.querySelectorAll('[data-button],[data-visual]').forEach(element => {
    const selected = chordButtons.has(element.dataset.button || element.dataset.visual);
    element.classList.toggle('chord-selected', selected);
  });
  $('chord-status').textContent = chordButtons.size ?
    `Queued together: ${[...chordButtons].join(' + ')}. Release Ctrl to press.` :
    'No buttons queued. Keep Ctrl held and click a button.';
}

function releaseChord() {
  const names = [...chordButtons];
  cancelChord();
  if (!names.length || !names.every(chordAllowed)) return;
  if (names.every(buttonInputAllowed)) {
    chordUntil = performance.now() + 250;
    post('/api/input', {buttons: names.join(','), frames: 2});
  } else pressPadButtons(names);
}

async function pumpTouch() {
  touchBusy = true;
  while (pendingTouch) {
    const sample = pendingTouch; pendingTouch = null;
    try { await request('/api/touch', sample); }
    catch (error) {
      lastPadError = error.message || 'Touch request failed';
      touchPad.clear(); wasTouching = false; pendingTouch = null;
      document.querySelector('[data-touch-controls]').classList.remove('touch-held');
      break;
    }
  }
  touchBusy = false;
}

function queueTouch(sample) { pendingTouch = sample; if (!touchBusy) pumpTouch(); }
function stopTouch() {
  touchPad.clear();
  document.querySelector('[data-touch-controls]').classList.remove('touch-held');
  if (wasTouching) { wasTouching = false; queueTouch({x:0,y:0,down:false}); }
  nextTouchSend = 0;
}
function tickTouch() {
  const screen = document.querySelector('[data-touch-controls]');
  const sample = touchPad.sample();
  screen.classList.toggle('touch-held', sample.down);
  if (sample.down) {
    screen.style.setProperty('--touch-x', `${sample.x * 100 / 320}%`);
    screen.style.setProperty('--touch-y', `${sample.y * 100 / 240}%`);
  }
  const ready = $('enable-pad').checked && controlsFocused() && connected && sessionOn && linkEnabled && activePreset()?.capabilities.includes('touch');
  if (!ready) { if (wasTouching) stopTouch(); return; }
  if (!sample.down) { if (wasTouching) stopTouch(); return; }
  if (performance.now() < nextTouchSend) return;
  wasTouching = true; nextTouchSend = performance.now() + 100;
  queueTouch(sample);
}

function controlsFocused() {
  return document.visibilityState === 'visible' && document.hasFocus();
}

function pointerChanged() {
  nextPadSend = 0;
  tickPad();
}

function clearPointers() {
  stopTouch();
  pointerPad.clear();
  for (const gesture of pointerGestures.values()) gesture.element.classList.remove('dragging');
  pointerGestures.clear();
}

function installPointerControls() {
  const touchScreen = document.querySelector('[data-touch-controls]');
  touchScreen.title = 'Tap or drag the actual bottom touch screen';
  touchScreen.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    const point = bottomTouchPoint(touchScreen.getBoundingClientRect(), event.clientX, event.clientY);
    if (!touchPad.begin(event.pointerId, point, performance.now())) return;
    touchScreen.setPointerCapture(event.pointerId); nextTouchSend = 0; tickTouch();
  });
  touchScreen.addEventListener('pointermove', event => {
    touchPad.move(event.pointerId, bottomTouchPoint(touchScreen.getBoundingClientRect(), event.clientX, event.clientY));
    tickTouch();
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture'])
    touchScreen.addEventListener(type, event => {
      const press = touchPad.press;
      if (!press || press.id !== event.pointerId) return;
      const release = () => { touchPad.release(press); tickTouch(); };
      const left = type === 'pointerup' || type === 'lostpointercapture' ? 200 - (performance.now() - press.now) : 0;
      if (left > 0) setTimeout(release, left); else release();
    });
  touchScreen.addEventListener('dragstart', event => event.preventDefault());
  touchScreen.addEventListener('contextmenu', event => event.preventDefault());
  // The drawn device buttons and the controller visualization share one state.
  for (const element of document.querySelectorAll('[data-visual]')) {
    const name = element.dataset.visual;
    element.removeAttribute('aria-hidden');
    element.setAttribute('role', 'button');
    element.tabIndex = 0;
    element.setAttribute('aria-label', `${name} remote button`);
    element.title = `Hold ${name}`;
    const begin = id => {
      const input = pointerPad.button(id, name);
      pointerChanged();
      return {input, started: performance.now()};
    };
    const finish = (id, press) => {
      // Preserve a quick click long enough for one device polling interval.
      setTimeout(() => { pointerPad.release(id, press.input); pointerChanged(); },
        Math.max(0, 120 - (performance.now() - press.started)));
    };
    element.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      if (event.ctrlKey) { event.preventDefault(); return; }
      event.preventDefault();
      element.focus({preventScroll: true});
      element.setPointerCapture(event.pointerId);
      pointerGestures.set(event.pointerId, {element, press: begin(event.pointerId)});
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      element.addEventListener(type, event => {
        const gesture = pointerGestures.get(event.pointerId);
        if (!gesture || gesture.element !== element) return;
        pointerGestures.delete(event.pointerId);
        if (type === 'pointercancel') { pointerPad.release(event.pointerId); pointerChanged(); }
        else finish(event.pointerId, gesture.press);
      });
    }
    const keyboard = new Map();
    element.addEventListener('keydown', event => {
      if (![' ', 'Enter'].includes(event.key)) return;
      event.preventDefault();
      if (!event.repeat) {
        const id = `key:${name}:${event.key}`;
        keyboard.set(event.key, {id, press: begin(id)});
      }
    });
    const releaseKeys = () => {
      for (const {id, press} of keyboard.values()) finish(id, press);
      keyboard.clear();
    };
    element.addEventListener('keyup', event => {
      const held = keyboard.get(event.key);
      if (!held) return;
      event.preventDefault();
      keyboard.delete(event.key);
      finish(held.id, held.press);
    });
    element.addEventListener('blur', releaseKeys);
    element.addEventListener('click', event => {
      if (event.ctrlKey) { event.preventDefault(); stageChord(name); }
    });
    element.addEventListener('contextmenu', event => event.preventDefault());
  }
  for (const element of document.querySelectorAll('[data-stick], [data-preview-controls]')) {
    element.title = element.dataset.stick === 'move' ? 'Drag to move' :
      element.dataset.stick === 'look' ? 'Drag to look' : 'Drag left half to move; right half to look';
    element.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      const box = element.getBoundingClientRect();
      const preview = element.hasAttribute('data-preview-controls');
      const side = preview ? (event.clientX < box.left + box.width / 2 ? 'move' : 'look') : element.dataset.stick;
      const gesture = {element, side, x: preview ? event.clientX : box.left + box.width / 2,
        y: preview ? event.clientY : box.top + box.height / 2,
        radius: preview ? Math.max(24, Math.min(box.width, box.height) / 4) : Math.max(12, box.width / 3)};
      element.setPointerCapture(event.pointerId);
      element.classList.add('dragging');
      pointerGestures.set(event.pointerId, gesture);
      pointerPad.stick(event.pointerId, side, (event.clientX - gesture.x) / gesture.radius,
        -(event.clientY - gesture.y) / gesture.radius);
      pointerChanged();
    });
    element.addEventListener('pointermove', event => {
      const gesture = pointerGestures.get(event.pointerId);
      if (!gesture || gesture.element !== element) return;
      pointerPad.stick(event.pointerId, gesture.side, (event.clientX - gesture.x) / gesture.radius,
        -(event.clientY - gesture.y) / gesture.radius);
      pointerChanged();
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      element.addEventListener(type, event => {
        const gesture = pointerGestures.get(event.pointerId);
        if (!gesture || gesture.element !== element) return;
        pointerGestures.delete(event.pointerId);
        pointerPad.release(event.pointerId);
        element.classList.remove('dragging');
        pointerChanged();
      });
    }
    element.addEventListener('dragstart', event => event.preventDefault());
    element.addEventListener('contextmenu', event => event.preventDefault());
  }
}

function feedback(message, error = false) {
  $('feedback').textContent = message;
  $('feedback').classList.toggle('error', error);
}

async function request(path, payload = {}, keepalive = false) {
  if (path==='/api/input' && activePreset()?.manualInputRequiresArm) payload={...payload,owner:inputOwner};
  const response = await fetch(path, {
    method: 'POST', cache: 'no-store', keepalive,
    headers: {'Content-Type': 'application/json', 'X-Remote-Pilot-Viewer': '1'},
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error((await response.text()).trim() || `Request failed (${response.status})`);
  return response.json();
}

async function post(path, payload = {}) {
  try {
    const result = await request(path, payload);
    feedback(result.capture ? `Capture #${result.capture}: screens and available build logs queued.` : `Queued request #${result.id}.`);
    refresh();
  } catch (error) { feedback(error.message || 'Request failed.', true); }
}

function diagnosticLinks(container, file, image = false) {
  if (!/^[a-zA-Z0-9_.-]+\.(?:bmp|txt|log)$/.test(file)) return;
  const path = `/captures/${encodeURIComponent(file)}`;
  const link = document.createElement('a'); link.href = path;
  link.textContent = image ? 'Download screens' : 'Download logs'; link.download = file;
  container.append(link);
  if (image) return;
  const copy = document.createElement('button'); copy.type = 'button'; copy.textContent = 'Copy log';
  copy.style.marginLeft = '8px';
  copy.addEventListener('click', async () => {
    copy.disabled = true;
    try {
      const response = await fetch(path, {cache: 'no-store'});
      if (!response.ok) throw new Error('Log download failed');
      const bytes = await response.arrayBuffer();
      if (bytes.byteLength > 65536) throw new Error('Log exceeds the copy limit');
      await navigator.clipboard.writeText(new TextDecoder().decode(bytes));
      feedback(`Device log copied to clipboard.${globalThis.RemotePilotPrivacy?.hidden ? ' The copy is not masked, so it can hold private details.' : ''}`);
    } catch (error) { feedback(`${error.message || 'Clipboard unavailable'}. Use Download logs to save the file.`, true); }
    finally { copy.disabled = false; }
  });
  container.append(copy);
}

function taskRows(container, tasks, captures) {
  container.replaceChildren();
  if (!tasks.length) {
    const row = document.createElement('div');
    row.className = 'task empty'; row.textContent = 'No requests yet.';
    container.append(row); return;
  }
  for (const task of tasks) {
    const row = document.createElement('div'); row.className = 'task';
    const id = document.createElement('strong'); id.textContent = `#${task.id} ${task.kind}`;
    const state = document.createElement('span'); state.className = 'state'; state.textContent = task.state;
    const result = document.createElement('span'); result.className = 'result';
    result.textContent = task.result || task.payload || 'Awaiting result';
    let linked = false;
    for (const capture of captures) {
      const item = capture.items.find(item => item.task === task.id);
      if (!item) continue;
      const label = document.createElement('span'); label.textContent = ` · Capture #${capture.id} `;
      result.append(label);
      if (item.file) {
        diagnosticLinks(result, item.file, item.kind === 'shot'); linked = true;
      }
    }
    // Standalone Read device log / Save report requests have no capture bundle.
    const saved = /^(?:Log|Report) saved: ([a-zA-Z0-9_.-]+\.(?:txt|log))$/.exec(task.result || '');
    if (!linked && saved && task.state === 'done') diagnosticLinks(result, saved[1]);
    row.append(id, state, result); container.append(row);
  }
}

function showTasks(tasks, captures = []) {
  $('queue-count').textContent = `${tasks.filter(t => t.state === 'queued' || t.state === 'sent').length} pending`;
  taskRows($('tasks'), tasks, captures);
  taskRows($('dialog-tasks'), tasks, captures);
}

function showImage(path) {
  if (displayedPath === path) return;
  displayedPath = path;
  $('top-image').src = path;
  $('bottom-image').src = path;
  document.querySelectorAll('.screen').forEach(el => el.classList.add('has-image'));
}

function clearImage() {
  displayedPath = '';
  $('top-image').removeAttribute('src');
  $('bottom-image').removeAttribute('src');
  document.querySelectorAll('.screen').forEach(el => el.classList.remove('has-image'));
}

function captureAge() {
  if (!captureTimeMs) return 'saved earlier';
  const minutes = Math.max(0, Math.floor((Date.now() - captureTimeMs) / 60000));
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

async function playPendingAudio() {
  if (!audioContext || audioBusy || !audioVersion || audioVersion <= audioPlayedVersion) return;
  audioBusy = true;
  try {
    const response = await fetch(`/live-audio.pcm?since=${audioPlayedVersion}`, {cache: 'no-store'});
    if (!response.ok) return;
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength < 4 || bytes.byteLength % 2) return;
    const pcm = new DataView(bytes);
    const deliveredVersion = pcm.getUint32(0, true);
    if (deliveredVersion < audioPlayedVersion) {
      audioPlayedVersion = deliveredVersion;
      audioNextTime = 0;
      return;
    }
    audioPlayedVersion = deliveredVersion;
    const samples = (bytes.byteLength - 4) / 2;
    if (!samples) return;
    const buffer = audioContext.createBuffer(1, samples, 16364);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples; i++) channel[i] = pcm.getInt16(4 + i * 2, true) / 32768;
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);
    const now = audioContext.currentTime;
    const aheadLimit = Math.max(.5, Math.min(2, (activePreset()?.maxLiveAudioBytes || 0) / (2 * 16364) + .1));
    if (audioNextTime < now || audioNextTime > now + aheadLimit) audioNextTime = now + .06;
    source.start(audioNextTime);
    audioNextTime += buffer.duration;
  } catch (_) { /* The next preview can resume after a transient Wi-Fi gap. */ }
  finally {
    audioBusy = false;
    if (audioContext && audioVersion > audioPlayedVersion) setTimeout(playPendingAudio, 0);
  }
}

function displayCapture() {
  if (captureVersion) {
    showImage(`/latest.bmp?v=${captureVersion}`);
    $('video-status').textContent = `Saved capture · ${captureAge()}${connected ? '' : ' · offline'}`;
  } else if (!streamVersion) {
    clearImage();
    $('video-status').textContent = 'No image';
  }
}

function updateVideoStatus() {
  if (selectedPreset === 'example') { clearImage(); $('video-status').textContent = 'Local example'; return; }
  if (!sessionOn || !connected || !linkEnabled) {
    if (captureVersion) displayCapture();
    else { clearImage(); $('video-status').textContent = 'No current image'; }
    return;
  }
  if (!$('live').checked) {
    if (captureVersion) $('video-status').textContent = `Saved capture · ${captureAge()}`;
    return;
  } else if (streamError) {
    $('video-status').textContent = `Preview retrying: ${streamError}`;
  } else if (!streamVersion) {
    $('video-status').textContent = 'Waiting for preview';
  } else if (Date.now() - streamSeenAt > 4000) {
    if (captureVersion) showImage(`/latest.bmp?v=${captureVersion}`);
    $('video-status').textContent = captureVersion ? `Saved capture · ${captureAge()} · preview delayed` : 'Preview delayed';
  } else {
    const recent = streamTimes.filter(time => Date.now() - time < 5000);
    $('video-status').textContent = recent.length > 1 ? `Live · ${((recent.length - 1) * 1000 / (recent[recent.length - 1] - recent[0])).toFixed(1)} fps` : 'Live preview';
  }
}

function updatePerf(perf) {
  if (!$('perf-toggle').checked) {
    perfLastTimestamp = null;
    perfHistory = [];
    perfScale = 33.4;
    $('perf-summary').textContent = 'Performance capture off';
    return;
  }
  if (!perf) {
    $('perf-summary').textContent = selectedPreset === 'example' ?
      'Offline stub · no device timing samples' : !connected ? 'Waiting for a connected device' :
      'Waiting for device timing · older builds may not provide telemetry';
    perfLastTimestamp = null;
    perfHistory = [];
    drawPerfGraph();
    return;
  }
  const fps = Number.isFinite(perf.fps) ? `${perf.fps.toFixed(1)} fps` : 'FPS pending';
  const work = Number.isFinite(perf.workMs) && perf.workMs >= 0 ? perf.workMs : null;
  const frame = Number.isFinite(perf.frameMs) && perf.frameMs >= 0 ? perf.frameMs : work;
  const map = typeof perf.map === 'string' && perf.map ? perf.map : 'menu';
  $('perf-summary').textContent = `${fps} · ${work === null ? 'work pending' : `${work.toFixed(1)} ms work`} · ${map} · device timing`;
  if (work === null || frame === null) return;
  const stamp = Number.isFinite(perf.timestampMs) ? perf.timestampMs : `${fps}:${work}:${frame}:${map}`;
  if (stamp === perfLastTimestamp) return;
  perfLastTimestamp = stamp;
  perfHistory.push({work, frame});
  perfHistory = perfHistory.slice(-90);
  if (!$('perf-toggle').checked) return;
  drawPerfGraph();
}

function drawPerfGraph() {
  const canvas = $('perf-graph');
  const context = canvas.getContext('2d');
  const width = canvas.width, height = canvas.height;
  context.clearRect(0, 0, width, height);
  const theme = getComputedStyle(document.documentElement);
  context.strokeStyle = theme.getPropertyValue('--rp-muted').trim() || '#5c7894'; context.lineWidth = 1;
  while (perfScale < perfGraphMax(perfHistory)) perfScale *= 2;
  const max = perfScale;
  const guideY = height - 16.67 / max * height;
  context.beginPath(); context.moveTo(0, guideY); context.lineTo(width, guideY); context.stroke();
  for (const [key, color] of [['frame', theme.getPropertyValue('--rp-accent2').trim() || '#8ee6ff'],
    ['work', theme.getPropertyValue('--rp-accent').trim() || '#37a9ff']]) {
    context.strokeStyle = color; context.lineWidth = 2; context.beginPath();
    perfHistory.forEach((point, index) => {
      const x = perfHistory.length < 2 ? 0 : index * width / (perfHistory.length - 1);
      const y = height - Math.min(point[key], max) / max * height;
      if (index) context.lineTo(x, y); else context.moveTo(x, y);
    });
    context.stroke();
  }
  context.fillStyle = theme.getPropertyValue('--rp-muted').trim() || '#a3c8a8';
  context.font = '11px system-ui';
  context.fillText(`${(max / 1.12).toFixed(1)} ms`, 6, 13);
  context.fillText('16.7 ms guide', 6, Math.max(25, guideY - 4));
}

function setConnection(kind, label) {
  const badge = $('connection');
  badge.className = `status ${kind}`;
  badge.innerHTML = '<i aria-hidden="true"></i>';
  badge.append(document.createTextNode(label));
  badge.title = label;  // the badge may be truncated; the full text stays available
  $('device-network').className = `network-indicator ${kind}`;
  $('device-network').setAttribute('aria-label', label);
}

function updateConnectControl() {
  const active = connected && sessionOn && linkEnabled;
  const game = activePreset()?.name || 'the device';
  const gameHint = 'Check the device\'s PC link screen.';
  const button = $('link-toggle');
  if (selectedPreset === 'example') {
    button.textContent = 'Local preview only';
    button.classList.remove('connected');
    $('connect-title').textContent = 'Example · offline preview';
    $('connect-detail').textContent = 'All shared panels are shown. Device actions are disabled; preview controls work locally.';
    updateFeatureControls();
    return;
  }
  button.textContent = active ? 'Disconnect' : linkEnabled && sessionOn ? 'Retry connection' : 'Connect to device';
  button.classList.toggle('connected', active);
  $('connect-title').textContent = active ? 'Your device is connected' : 'Connect to your device';
  $('connect-detail').textContent = active ? 'The paired device is responding. Screen capture and controls are available.' :
    !sessionOn ? `Remote session is off for ${game}. ${gameHint}` :
    !linkEnabled ? 'Connection paused on this PC. Press Connect to resume.' :
    discoveryStatus === 'scanning' || discoveryStatus === 'retrying' ?
      `Scanning the private network for ${game}. ${gameHint}` :
      `No paired ${game} device is responding yet. Retry connection starts a fresh scan. ${gameHint}`;
  updateFeatureControls();
}

async function refresh() {
  if (stateBusy) return;
  stateBusy = true;
  const started = performance.now();
  try {
    const response = await fetch('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error('Viewer status unavailable');
    const state = await response.json();
    const wasConnected = connected;
    streamError = typeof state.streamError === 'string' ? state.streamError : '';
    connected = Boolean(state.connected);
    deviceMeta = state.deviceMeta || null;
    discoveryStatus = state.discovery || '';
    if (state.preset && state.preset !== selectedPreset) {
      stopDriving();
      if (activePreset()?.manualInputRequiresArm || presets.find(p=>p.id===state.preset)?.manualInputRequiresArm) {
        manualArmed=false; $('enable-pad').checked=false; cancelChord();
      }
      selectedPreset = state.preset;
      perfLastTimestamp = null; perfHistory = [];
      renderPreset();
    }
    if (state.deviceMeta?.deviceModel) {
      const model = state.deviceMeta.deviceModel === 'basic' ? 'basic' : 'dual-stick';
      if (model !== deviceModel) { deviceModel = model; applyModel(); }
    }
    sessionOn = Boolean(state.session);
    linkEnabled = state.linkEnabled !== false;
    // An adapter may say what to do next (status meta.nextStep) instead of the generic wording.
    const nextStep = typeof state.deviceMeta?.nextStep === 'string' && state.deviceMeta.nextStep ? state.deviceMeta.nextStep.slice(0, 80) : '';
    setConnection(!linkEnabled ? 'off' : !sessionOn ? 'off' : connected ? 'connected' : 'waiting',
      !linkEnabled ? 'Disconnected by PC' : !sessionOn ? nextStep || 'Remote session off' :
      connected ? 'Device connected' : nextStep || 'Waiting for device');
    if (selectedPreset === 'example') setConnection('off','Example · local preview');
    if (wasConnected !== connected) renderPreset();
    updateConnectControl();
    $('latency').textContent = `Viewer ${Math.round(performance.now() - started)} ms`;
    showTasks(state.tasks || [], state.captures || []);
    if (state.captureVersion !== captureVersion) {
      captureVersion = state.captureVersion;
      captureTimeMs = state.captureAtMs;
      if (!$('live').checked || !streamVersion || Date.now() - streamSeenAt > 4000) displayCapture();
    }
    if (state.streamVersion !== streamVersion) {
      streamVersion = state.streamVersion;
      if (streamVersion) {
        streamSeenAt = Date.now();
        streamTimes.push(streamSeenAt);
        streamTimes = streamTimes.filter(time => streamSeenAt - time < 5000);
        if ($('live').checked) showImage(`/live.bmp?v=${streamVersion}`);
      }
    }
    if (state.audioVersion !== audioVersion) {
      audioVersion = state.audioVersion;
      if (audioVersion < audioPlayedVersion) audioPlayedVersion = audioVersion;
      playPendingAudio();
    }
    updateVideoStatus();
    if (!perfChanging) $('perf-toggle').checked = state.perfEnabled === true;
    $('perf-graph').hidden = !$('perf-toggle').checked;
    updatePerf(state.perf);
    if (state.controllerError && $('enable-pad').checked) {
      lastPadError = state.controllerError;
      $('enable-pad').checked = false;
      stopDriving(false);
    }
    if ((!connected || !sessionOn || !linkEnabled) && selectedPreset !== 'example') stopDriving(false, wasConnected);
  } catch (_) {
    const wasConnected = connected;
    connected = false;
    if (wasConnected) renderPreset();
    setConnection('error', 'Viewer offline');
    updateConnectControl();
    $('latency').textContent = 'Link —';
    updateVideoStatus();
    stopDriving(false);
  } finally { stateBusy = false; }
}

function pad() {
  if (!navigator.getGamepads) return null;
  return Array.from(navigator.getGamepads()).find(gamepad => gamepad && gamepad.connected && gamepad.mapping === 'standard') || null;
}

function axis(value) {
  const deadzone = 0.18;
  const v = Math.max(-1, Math.min(1, Number(value) || 0));
  if (Math.abs(v) <= deadzone) return 0;
  return Number((Math.sign(v) * (Math.abs(v) - deadzone) / (1 - deadzone)).toFixed(2));
}

function padPayload(gamepad) {
  const fullBasic = activePreset()?.controllerBasicFull === true;
  const limitedBasic = deviceModel === 'basic' && !fullBasic;
  const buttons = Object.entries(buttonMap)
    .filter(([index, name]) => (!limitedBasic || (name !== 'ZL' && name !== 'ZR')) &&
      (gamepad.buttons[Number(index)]?.pressed || (Number(index) === 6 || Number(index) === 7) && gamepad.buttons[Number(index)]?.value >= .5))
    .map(([, name]) => name);
  const padMask = Array.from(gamepad.buttons).slice(0, 16).reduce((mask, button, index) =>
    mask | ((button?.pressed || ((index === 6 || index === 7) && button?.value >= .5)) ? 1 << index : 0), 0);
  return {buttons: buttons.join(',') || 'NONE', frames: 30,
    moveX: axis(gamepad.axes[0]), moveY: -axis(gamepad.axes[1]),
    lookX: limitedBasic ? 0 : axis(gamepad.axes[2]),
    lookY: limitedBasic ? 0 : -axis(gamepad.axes[3]), padMask,
    lt: Math.max(0, Math.min(1, gamepad.buttons[6]?.value || 0)),
    rt: Math.max(0, Math.min(1, gamepad.buttons[7]?.value || 0))};
}

function visualize(payload) {
  const pressed = new Set(payload.buttons.split(','));
  document.querySelectorAll('[data-visual]').forEach(el => el.classList.toggle('active', pressed.has(el.dataset.visual)));
  $('left-stick').style.transform = `translate(${payload.moveX * 13}px,${-payload.moveY * 13}px)`;
  $('right-stick').style.transform = `translate(${payload.lookX * 13}px,${-payload.lookY * 13}px)`;
  $('circle-pad').style.transform = `translate(${payload.moveX * 7}px,${-payload.moveY * 7}px)`;
  $('c-stick').style.transform = `translate(${payload.lookX * 5}px,${-payload.lookY * 5}px)`;
}

function applyModel() {
  const basic = deviceModel === 'basic';
  $('device-model').value = deviceModel;
  $('stage-title').textContent = basic ? 'Basic layout' : 'Second-stick layout';
  $('device').classList.toggle('basic', basic);
  $('device').classList.toggle('dual-stick', !basic);
  $('device').setAttribute('aria-label', `Simulated dual-screen gamepad (${basic ? 'basic' : 'second-stick'} layout)`);
  document.querySelector('.pilot').classList.toggle('basic', basic);
  document.querySelector('.controls').classList.toggle('basic', basic);
  $('model-detail').textContent = basic ? (activePreset()?.controllerBasicFull ?
    'Basic layout: remote controller sticks and triggers use this preset’s mappings.' :
    'Basic layout: no second stick or ZL/ZR input.') :
    'Second-stick layout: right stick controls the second stick; triggers control ZL/ZR.';
  for (const name of ['lookX', 'lookY']) {
    const input = document.querySelector(`[name="${name}"]`);
    input.disabled = basic;
    if (basic) input.value = 0;
  }
  updateFeatureControls();
}

function queuePad(payload) {
  pendingPad = payload;
  if (!padBusy) pumpPad();
}

async function pumpPad() {
  padBusy = true;
  while (pendingPad) {
    const payload = pendingPad;
    pendingPad = null;
    try {
      await request('/api/gamepad', payload);
      lastPadError = '';
    } catch (error) {
      lastPadError = error.message || 'Controller request failed';
      $('enable-pad').checked = false;
      wasDriving = false;
      clearPointers();
      visualize(neutral);
      pendingPad = null;
      break;
    }
  }
  padBusy = false;
}

function stopDriving(clearVisual = true, clearPointer = true) {
  if (clearPointer) clearPointers();
  if (wasDriving) {
    wasDriving = false;
    queuePad(neutral);
  }
  nextPadSend = 0;
  if (clearVisual) visualize(neutral);
}

function tickPad() {
  updateFeatureGuidance();
  if (performance.now() < chordUntil) return;
  const gamepad = pad();
  const enabled = $('enable-pad').checked;
  const focused = controlsFocused();
  const ready = enabled && focused && connected && sessionOn && linkEnabled && activePreset()?.capabilities.includes('controller');
  const previewing = focused && Boolean(gamepad);
  const pointer = pointerPad.snapshot();
  const limitedBasic = deviceModel === 'basic' && !activePreset()?.controllerBasicFull;
  const payload = focused ? mergePointerPad(previewing ? padPayload(gamepad) : neutral,
    pointer, limitedBasic, buttonMap) : neutral;
  const activeInput = focused && (pointer.active || previewing &&
    (gamepad.buttons.some(button => button && (button.pressed || button.value > .35)) ||
      gamepad.axes.some(value => Math.abs(value) > .18)));
  visualize(payload);
  $('controller-indicator').className = `controller-indicator ${activeInput ? 'active' : previewing ? 'detected' : ''}`;
  $('controller-indicator').setAttribute('aria-label', activeInput ?
    `Controller input active${ready ? '; sent to device' : '; local preview only'}` : previewing ?
    'Controller detected; no input' : 'Mouse controls ready');
  const manualReady=activePreset()?.manualInputRequiresArm && manualArmed && enabled && focused && connected && sessionOn && linkEnabled;
  $('pad-state').textContent = manualReady ? 'Buttons armed' : ready ? activeInput ? 'Active' : 'Ready' : !focused ? 'Paused' : 'Preview only';
  $('pad-state').classList.toggle('active', ready);
  $('pad-detail').textContent = lastPadError || (activePreset()?.manualInputRequiresArm ?
    (manualReady ? 'Quick controls buttons are enabled. Sticks and touch stay in local preview.' : 'Remote input is off. Enable remote controls to use the Quick controls buttons.') : !enabled ? 'Drag sticks or hold buttons to preview; enable remote controls to send' :
    !focused ? 'Paused while this tab is out of focus' : !linkEnabled ? 'PC link disconnected' : !sessionOn ? 'Remote session is off' :
    !connected ? 'Waiting for device connection' : gamepad ? `${gamepad.id} + mouse · sending latest state` :
      'Mouse controls ready · drag sticks or hold buttons');
  if (selectedPreset === 'example') $('pad-detail').textContent = 'Local controls preview · no device connection';
  // One owner for this mode label. Focus pauses input; the enabled link mode
  // stays stable while refresh and controller polling run at different rates.
  const mode = enabled && connected && sessionOn && linkEnabled ? 'REMOTE' : 'LOCAL';
  if ($('pilot-light').textContent !== mode) $('pilot-light').textContent = mode;
  if (!ready) { stopDriving(false, false); return; }
  // Idle mouse controls should not take Wi-Fi polling slots from live video.
  if (!hasPadInput(payload)) { stopDriving(false, false); return; }
  wasDriving = true;
  const now = performance.now();
  if (now >= nextPadSend) {
    queuePad(payload);
    nextPadSend = now + 100;
  }
}

async function requestStream() {
  if (streamBusy || !$('live').checked || !connected || !sessionOn || !linkEnabled || document.visibilityState !== 'visible') return;
  const now = performance.now();
  if (now < nextStreamRequest) return;
  nextStreamRequest = now + Number($('preview-rate').value);
  streamBusy = true;
  try { await request('/api/stream'); }
  catch (error) {
    // Brief Wi-Fi gaps should not turn off a preview the user chose to leave on.
    // Refresh updates the connection badge; retry at a slower rate until it returns.
    nextStreamRequest = performance.now() + 1000;
    $('video-status').textContent = 'Preview reconnecting';
    refresh();
  }
  finally { streamBusy = false; }
}

$('shot').addEventListener('click', () => post('/api/capture'));
$('sound').addEventListener('click', async () => {
  if (audioContext) {
    await audioContext.close();
    audioContext = null;
    audioNextTime = 0;
    audioPlayedVersion = 0;
    $('sound').textContent = 'Enable sound';
    $('sound').setAttribute('aria-pressed', 'false');
    return;
  }
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) { feedback('This browser cannot play live audio.', true); return; }
  audioContext = new Context();
  await audioContext.resume();
  if (!$('live').checked) {
    $('live').checked = true;
    $('live').dispatchEvent(new Event('change'));
  }
  audioPlayedVersion = audioVersion || 0;
  $('sound').textContent = 'Mute sound';
  $('sound').setAttribute('aria-pressed', 'true');
});
$('preview-rate').addEventListener('change', () => {
  nextStreamRequest = 0;
  try { localStorage.setItem(`remote-pilot-preview-rate-${selectedPreset}`, $('preview-rate').value); } catch (_) {}
});
$('link-toggle').addEventListener('click', async () => {
  const shouldConnect = !(connected && sessionOn && linkEnabled);
  if (!shouldConnect) stopDriving();
  $('link-toggle').disabled = true;
  try {
    if (shouldConnect && linkEnabled && sessionOn) {
      await request('/api/retry', {});
      feedback('Fresh device search requested. Check the selected device\'s PC link status.');
    } else {
      const state = await request('/api/link', {connected: shouldConnect});
      linkEnabled = Boolean(state.linkEnabled);
      if (linkEnabled && state.connect?.message) feedback(state.connect.message, state.connect.ok === false);
      else feedback(linkEnabled ? 'Connecting to the paired device on your local network.' :
        'PC link disconnected; reports can still transfer.');
    }
    await refresh();
  } catch (error) { feedback(error.message || 'Could not change the PC link.', true); }
  finally { updateFeatureControls(); }
});
$('live').addEventListener('change', () => {
  if ($('live').checked) { updateVideoStatus(); requestStream(); }
  else displayCapture();
});
$('enable-pad').addEventListener('change', () => {
  if (activePreset()?.manualInputRequiresArm) updateManualArm($('enable-pad').checked);
  if (!$('enable-pad').checked) stopDriving();
  updateFeatureControls(); tickPad();
});
$('device-model').addEventListener('change', event => {
  stopDriving();
  deviceModel = event.target.value === 'basic' ? 'basic' : 'dual-stick';
  try { localStorage.setItem('remote-pilot-device-model', deviceModel); } catch (_) {}
  applyModel();
  tickPad();
});
window.addEventListener('blur', () => { cancelChord(); stopDriving(); if (activePreset()?.manualInputRequiresArm) updateManualArm(false); });
document.addEventListener('visibilitychange', () => { if (document.hidden) { cancelChord(); stopDriving(); if (activePreset()?.manualInputRequiresArm) updateManualArm(false); } else { tickPad(); requestStream(); } });
window.addEventListener('gamepaddisconnected', () => { stopDriving(); tickPad(); });
window.addEventListener('gamepadconnected', tickPad);
window.addEventListener('pagehide', () => {
  if (activePreset()?.manualInputRequiresArm) updateManualArm(false);
  if (wasTouching) request('/api/touch', {x:0,y:0,down:false}, true).catch(() => {});
  if (wasDriving) request('/api/gamepad', neutral, true).catch(() => {});
});
$('console-form').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('console').value.trim();
  if (text) post('/api/console', {text});
});
for (const button of document.querySelectorAll('[data-button]')) {
  button.addEventListener('click', event => {
    if (event.ctrlKey) { event.preventDefault(); stageChord(button.dataset.button); }
    else if (buttonInputAllowed(button.dataset.button))
      post('/api/input', {buttons: button.dataset.button, frames: 1});
    else if (buttonPadAllowed(button.dataset.button))
      pressPadButtons([button.dataset.button]);
  });
}
document.addEventListener('keyup', event => { if (event.key === 'Control') releaseChord(); });
const moves = {forward: {moveY: 1}, back: {moveY: -1}, 'turn-left': {lookX: -1}, 'turn-right': {lookX: 1}};
for (const button of document.querySelectorAll('[data-motion]')) {
  button.addEventListener('click', () => post('/api/input', {buttons: 'NONE', frames: 15, ...moves[button.dataset.motion]}));
}
$('motion-form').addEventListener('submit', event => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  post('/api/input', {buttons: 'NONE', frames: Number(data.get('frames')),
    moveX: Number(data.get('moveX')), moveY: Number(data.get('moveY')),
    lookX: Number(data.get('lookX')), lookY: Number(data.get('lookY'))});
});
$('open-log').addEventListener('click', () => $('log-dialog').showModal());
$('close-log').addEventListener('click', () => $('log-dialog').close());
$('request-report').addEventListener('click', () => post('/api/report'));
$('request-device-log').addEventListener('click', () => post('/api/log'));
$('perf-toggle').addEventListener('change', async () => {
  const preset = selectedPreset;
  const enabled = $('perf-toggle').checked;
  perfChanging = true;
  $('perf-toggle').disabled = true;
  $('perf-graph').hidden = !enabled;
  perfRequestAt = 0;
  updatePerf(null);
  try { await request('/api/performance', {preset, enabled}); }
  catch (error) { feedback(error.message, true); }
  finally { perfChanging = false; $('perf-toggle').disabled = false; refresh(); }
});
for (const field of ['background', 'surface', 'accent', 'text']) {
  $(`theme-${field}`).addEventListener('change', async () => {
    try {
      const colors = Object.fromEntries(['background', 'surface', 'accent', 'text']
        .map(name => [name, $(`theme-${name}`).value]));
      await request('/api/theme', {id: selectedPreset, colors});
      await loadPresets();
    } catch (error) { feedback(error.message || 'Could not save color.', true); }
  });
}
$('theme-reset').addEventListener('click', async () => {
  try { await request('/api/theme', {id: selectedPreset, colors: {}}); await loadPresets(); }
  catch (error) { feedback(error.message || 'Could not restore colors.', true); }
});
$('game-preset').addEventListener('change', async event => {
  stopDriving();
  if (activePreset()?.manualInputRequiresArm || presets.find(p=>p.id===event.target.value)?.manualInputRequiresArm) {
    manualArmed=false; $('enable-pad').checked=false; cancelChord();
  }
  try {
    const result = await request('/api/preset', {id: event.target.value});
    selectedPreset = result.selected;
    $('live').checked = false;
    if (audioContext) { await audioContext.close(); audioContext = null; audioNextTime = 0; audioPlayedVersion = 0;
      $('sound').textContent = 'Enable sound'; $('sound').setAttribute('aria-pressed', 'false'); }
    previewSelectedTheme = true;
    captureVersion = streamVersion = null;
    captureTimeMs = null;
    displayedPath = '';
    document.querySelectorAll('.screen').forEach(el => el.classList.remove('has-image'));
    updatePerf(null);
    renderPreset();
    await refresh();
  } catch (error) { feedback(error.message || 'Could not select preset.', true); renderPreset(); }
});
$('log-dialog').addEventListener('click', event => { if (event.target === $('log-dialog')) $('log-dialog').close(); });
setInterval(() => { if ($('auto').checked && connected && sessionOn && linkEnabled) post('/api/capture'); }, 15000);
setInterval(requestStream, 10);
// Sampling status only four times a second made the displayed preview appear
// stuck at 4 fps even when the receiver had newer frames ready.
setInterval(() => { if ($('live').checked && document.visibilityState === 'visible') refresh(); }, 30);
setInterval(() => { if (!$('live').checked || document.visibilityState !== 'visible') refresh(); }, 250);
setInterval(tickPad, 33);
setInterval(tickTouch, 33);

// Plain-language tooltips. Any element with data-tip explains itself in one
// small tip, on mouse hover (after a short pause) or keyboard focus; the preset's stylesheet
// styles #rp-tip. A title set by the code (often why a control is unavailable right now) is
// moved into data-tip-now, so the browser's own tooltip never doubles it, and shows as
// "Right now: ..." under the explanation. Touch taps show nothing; Escape or a press hides it.
function installTooltips() {
  const SELECTOR = '[data-tip],[data-tip-now]';
  const tip = document.createElement('div');
  tip.id = 'rp-tip'; tip.className = 'rp-tip'; tip.setAttribute('role', 'tooltip'); tip.hidden = true;
  const main = document.createElement('span'), now = document.createElement('span');
  main.className = 'rp-tip-text'; now.className = 'rp-tip-now';
  tip.append(main, now);
  document.body.append(tip);
  let anchor = null, how = '', pending = null, showTimer = 0, ticker = 0, quiet = null;
  let pointerX = -1, pointerY = -1, at = null;
  const adopt = node => {
    const title = node.getAttribute?.('title');
    if (title === null || title === undefined || !node.dataset) return;
    if (title.trim()) node.dataset.tipNow = title.trim(); else delete node.dataset.tipNow;
    node.removeAttribute('title');
  };
  document.querySelectorAll('[title]').forEach(adopt);
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'attributes') adopt(record.target);
      else for (const node of record.addedNodes) if (node.nodeType === 1) {
        adopt(node); node.querySelectorAll('[title]').forEach(adopt);
      }
    }
    if (anchor) fill();
  }).observe(document.documentElement, {subtree: true, childList: true, attributes: true, attributeFilter: ['title']});

  function fill() {
    const lines = tooltipLines(anchor.dataset.tip, anchor.dataset.tipNow);
    if (!lines.text) { hide(); return false; }
    if (privacyText(main) !== lines.text) main.textContent = lines.text;
    if (privacyText(now) !== lines.now) now.textContent = lines.now;
    now.hidden = !lines.now;
    return true;
  }
  function place() {
    const margin = 8, gap = 10, box = anchor.getBoundingClientRect();
    // A focused element scrolled out of view keeps its tip, but out of sight until it is back.
    tip.style.visibility = box.bottom < 0 || box.top > innerHeight || box.right < 0 || box.left > innerWidth ? 'hidden' : '';
    // Large parts (the device drawing, a screen) get the tip next to the pointer instead.
    const target = at && (box.height > 120 || box.width > 360) ?
      {left: at.x - 1, right: at.x + 1, top: at.y - 12, bottom: at.y + 14, width: 2} : box;
    const width = tip.offsetWidth, height = tip.offsetHeight;
    let top = target.bottom + gap;
    if (top + height > innerHeight - margin && target.top - gap - height >= margin) top = target.top - gap - height;
    top = Math.max(margin, Math.min(top, innerHeight - height - margin));
    const left = Math.max(margin, Math.min(target.left + target.width / 2 - width / 2, innerWidth - width - margin));
    tip.style.left = `${Math.round(left)}px`; tip.style.top = `${Math.round(top)}px`;
  }
  function show(node, mode) {
    clearTimeout(showTimer); pending = null;
    if (anchor && anchor !== node) anchor.removeAttribute('aria-describedby');
    anchor = node; how = mode; at = mode === 'pointer' ? {x: pointerX, y: pointerY} : null;
    if (!fill()) return;
    const host = node.closest('dialog[open]') || document.body;  // stay above an open dialog
    if (tip.parentNode !== host) host.append(tip);
    tip.hidden = false; place();
    node.setAttribute('aria-describedby', 'rp-tip');
    clearInterval(ticker); ticker = setInterval(tick, 400);
  }
  function hide() {
    clearTimeout(showTimer); clearInterval(ticker); pending = null;
    if (anchor) anchor.removeAttribute('aria-describedby');
    anchor = null; how = ''; at = null; tip.hidden = true;
  }
  // Panels redraw every second: follow a replaced element under a resting pointer.
  function tick() {
    if (!anchor) return;
    if (!anchor.isConnected || !anchor.getClientRects().length) {
      const again = how === 'pointer' && document.elementFromPoint(pointerX, pointerY)?.closest(SELECTOR);
      if (!again) { hide(); return; }
      anchor = again; again.setAttribute('aria-describedby', 'rp-tip');
    }
    if (fill()) place();
  }
  document.addEventListener('pointermove', event => { pointerX = event.clientX; pointerY = event.clientY; }, {passive: true});
  document.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch') return;
    pointerX = event.clientX; pointerY = event.clientY;
    const node = event.target.closest?.(SELECTOR) || null;
    if (node && node === quiet) return;
    quiet = null;
    if (node === anchor || node === pending) return;
    clearTimeout(showTimer); pending = node;
    if (!node) { if (how === 'pointer') hide(); return; }
    showTimer = setTimeout(() => show(node, 'pointer'), tip.hidden ? 450 : 0);
  });
  document.addEventListener('pointerout', event => { if (!event.relatedTarget && how !== 'focus') hide(); });
  document.addEventListener('pointerdown', () => { quiet = anchor || pending; hide(); }, true);
  document.addEventListener('focusin', event => {
    const node = event.target.closest?.(SELECTOR);
    let keyboard = false;
    try { keyboard = event.target.matches(':focus-visible'); } catch (_) {}
    if (node && keyboard) show(node, 'focus'); else if (how === 'focus') hide();
  });
  document.addEventListener('focusout', () => { if (how === 'focus') hide(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && anchor) { quiet = anchor; hide(); } });
  // Scrolling keeps a hover tip only while the pointer is still over its element.
  window.addEventListener('scroll', () => {
    if (!anchor) return;
    if (how === 'pointer' && document.elementFromPoint(pointerX, pointerY)?.closest(SELECTOR) !== anchor) hide();
    else { if (at) at = {x: pointerX, y: pointerY}; place(); }
  }, {capture: true, passive: true});
  window.addEventListener('resize', () => { if (anchor) place(); });
}
// Private details on screen: masked by default, with the pixel eye at the top
// right of the header to show them. Every text the page shows is drawn masked from its real text,
// which is kept aside (originals), so turning the eye off puts it back; what the page sends, saves
// and compares stays the real text (privacyText). Inputs marked data-private show dots; pictures
// from the device are not changed. The choice is remembered in this browser only.
function installPrivacy() {
  const originals = new WeakMap(), drawn = new WeakMap();
  const SKIP = 'script,style,textarea,noscript,option,[data-privacy="keep"]';
  let hidden = true;
  try { hidden = localStorage.getItem(PRIVACY_STORE) !== 'shown'; } catch (_) {}
  const realOf = node => (drawn.has(node) && drawn.get(node) === node.nodeValue ? originals.get(node) : node.nodeValue);
  function draw(node) {
    const parent = node.parentElement;
    if (!parent || parent.closest(SKIP)) return;
    const real = realOf(node);
    originals.set(node, real);
    const want = hidden ? maskPrivate(real, Boolean(parent.closest('[data-private]'))) : real;
    if (node.nodeValue !== want) node.nodeValue = want;
    drawn.set(node, want);
  }
  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { draw(root); return; }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) draw(node);
  }
  function text(node) {
    if (!node) return '';
    if (node.nodeType === 3) return realOf(node);
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let out = '';
    for (let part = walker.nextNode(); part; part = walker.nextNode()) out += realOf(part);
    return out;
  }
  const button = $('privacy-toggle');
  function show() {
    document.body.classList.toggle('privacy-hidden', hidden);
    if (!button) return;
    button.setAttribute('aria-pressed', String(hidden));
    button.setAttribute('aria-label', hidden ? 'Private details hidden' : 'Private details showing');
    button.dataset.tip = PRIVACY_TIPS[hidden ? 'hidden' : 'shown'];
    button.querySelector('.privacy-icon').innerHTML = privacyIconSvg(hidden);
  }
  function set(value) {
    hidden = Boolean(value);
    try { if (hidden) localStorage.removeItem(PRIVACY_STORE); else localStorage.setItem(PRIVACY_STORE, 'shown'); } catch (_) {}
    show(); walk(document.body);
  }
  globalThis.RemotePilotPrivacy = {get hidden() { return hidden; }, set, text, mask: maskPrivate};
  new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'characterData') draw(record.target);
      else for (const node of record.addedNodes) if (node.nodeType === 1 || node.nodeType === 3) walk(node);
    }
  }).observe(document.body, {subtree: true, childList: true, characterData: true});
  button?.addEventListener('click', () => set(!hidden));
  show(); walk(document.body);
}
// Plain words for the shared buttons: quick controls, motion buttons and the drawn controls.
function describeSharedButtons() {
  const words = {UP: 'Up on the direction pad', DOWN: 'Down on the direction pad', LEFT: 'Left on the direction pad',
    RIGHT: 'Right on the direction pad', A: 'The A button, usually "OK" or "action"', B: 'The B button, usually "back"',
    X: 'The X button', Y: 'The Y button', L: 'The left shoulder button', R: 'The right shoulder button',
    ZL: 'The extra left shoulder button', ZR: 'The extra right shoulder button',
    START: 'Start, usually pauses the game or opens its menu', SELECT: 'Select, usually opens a map or options'};
  const motion = {forward: 'Moves forward for a moment.', back: 'Moves backward for a moment.',
    'turn-left': 'Turns to the left a little.', 'turn-right': 'Turns to the right a little.'};
  document.querySelectorAll('[data-button]').forEach(node => {
    node.dataset.tip ||= `${words[node.dataset.button] || node.dataset.button}. Click to press it once on the device.`; });
  document.querySelectorAll('[data-motion]').forEach(node => { node.dataset.tip ||= motion[node.dataset.motion] || 'Moves the character.'; });
  document.querySelectorAll('[data-visual]').forEach(node => {
    node.dataset.tip ||= `${words[node.dataset.visual] || node.dataset.visual}. Hold it with the mouse to hold it on the device.`; });
}
installPrivacy();
installTooltips();
describeSharedButtons();
installPointerControls();
applyModel();
loadPresets().then(refresh).catch(error => { feedback(error.message || 'Preset list unavailable.', true); refresh(); });
tickPad();
