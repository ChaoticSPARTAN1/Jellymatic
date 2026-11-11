// SPDX-License-Identifier: GPL-3.0-or-later
// Additional terms (author attribution, GPLv3 section 7(b)): see ADDITIONAL-TERMS.md
// Exercise the actual shared input state independently of the browser DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const prefix = fs.readFileSync(`${__dirname}/viewer.js`, 'utf8').split('const $ =')[0];
const context = vm.createContext({});
vm.runInContext(`${prefix}; this.featureGuidance = remoteFeatureGuidance; this.applicationTheme = presetApplicationTheme;`,context);
const baseTheme={background:'#10141c',surface:'#1b2432',surface2:'#151c28',line:'#3a4a60',
  text:'#edf5ff',muted:'#a8b9cf',accent:'#78c9b4',accent2:'#c1eadf',glow:'#427d70'};
const theme=context.applicationTheme({id:'sample',theme:{...baseTheme,accent:'#123456'}},{});
assert.equal(theme.accent,'#123456');assert.equal(theme.background,baseTheme.background);
assert.equal(context.applicationTheme(undefined,baseTheme).accent,baseTheme.accent);
console.log('Application theme: preset colours over the offline fallback PASS');
const featureReady={preset:'sample',connected:true,session:true,linkEnabled:true,controlSupported:true,captureSupported:true,enabled:false,focused:true};
let guide=context.featureGuidance(featureReady);
assert.equal(guide.control.title,'Enable remote controls');
guide=context.featureGuidance({...featureReady,enabled:true});assert.equal(guide.control.title,'Remote controls enabled');
guide=context.featureGuidance({...featureReady,enabled:true,focused:false});assert.match(guide.control.title,/paused/);
guide=context.featureGuidance({...featureReady,connected:false,live:true,streamFresh:true});assert.equal(guide.capture.title,'Connect the device first');
guide=context.featureGuidance({...featureReady,session:false});assert.match(guide.control.text,/PC link/);
guide=context.featureGuidance({...featureReady,enabled:true,manualOnly:true,manualArmed:false});assert.match(guide.control.title,/Waiting to arm/);
guide=context.featureGuidance({...featureReady,live:true,streamFresh:false});assert.equal(guide.capture.title,'Waiting for live frames');
guide=context.featureGuidance({...featureReady,live:true,streamFresh:true});assert.equal(guide.capture.title,'Live frames received');
guide=context.featureGuidance({...featureReady,example:true});assert.equal(guide.control.title,'Local preview only');
console.log('Feature guidance: connection, opt-in, focus pause, manual-only controls and measured screen state PASS');
vm.runInContext(`this.PointerPad = PointerPad; this.TouchPad = TouchPad; this.point = bottomTouchPoint; this.merge = mergePointerPad; this.hasInput = hasPadInput; this.perfMax = perfGraphMax;`, context);
assert.ok(context.perfMax([{frame:33.3,work:83.7}]) > 83.7); // measured Wi-Fi work spikes must stay in view
assert.ok(context.perfMax([{frame:120,work:20}]) > 120);
assert.ok(Number.isFinite(context.perfMax([])) && context.perfMax([]) > 16.7);
const mapping = {0:'A',1:'B',2:'X',3:'Y',4:'L',5:'R',6:'ZL',7:'ZR',8:'SELECT',9:'START',
  12:'UP',13:'DOWN',14:'LEFT',15:'RIGHT'};
const neutral = {buttons:'NONE',frames:1,moveX:0,moveY:0,lookX:0,lookY:0,padMask:0,lt:0,rt:0};
const pad = new context.PointerPad();
const payload = limited => context.merge(neutral, pad.snapshot(), limited, mapping);
assert.equal(context.hasInput(payload(false)), false);
pad.stick(1, 'move', .4, .7);
pad.stick(2, 'look', -2, 0);
pad.button(3, 'ZR');
pad.button(4, 'A');
assert.equal(payload(false).moveY, .7);
assert.equal(payload(false).lookX, -1);
assert.equal(payload(false).rt, 1);
assert.equal(payload(false).padMask, (1 << 0) | (1 << 7));
assert.equal(context.hasInput(payload(false)), true);
assert.equal(payload(true).lookX, 0);
assert.equal(payload(true).rt, 0);
assert.equal(payload(true).buttons, 'A');
pad.release(1);
assert.equal(payload(false).moveX, 0);
assert.equal(payload(false).lookX, -1); // releasing one pointer preserves the other
const old = pad.button(5, 'B');
pad.button(5, 'X');
pad.release(5, old); // an old delayed tap release must not release a new press
assert.ok(payload(false).buttons.includes('X'));
pad.clear();
assert.equal(pad.snapshot().active, false);
assert.equal(payload(false).buttons, 'NONE');
assert.equal(payload(false).lookX, 0);
assert.equal(context.hasInput(payload(false)), false);
// Mouse buttons coexist with a real pad; dragging overrides just its own stick.
pad.button('key:A', 'A');
pad.stick(6, 'move', 0, 0);
const hardware = {...neutral, buttons:'B',padMask:2,moveX:1,lookY:.6};
const combined = context.merge(hardware, pad.snapshot(), false, mapping);
assert.equal(combined.moveX, 0);
assert.equal(combined.lookY, .6);
assert.equal(combined.padMask, 3);
pad.button(7, 'A');
pad.release('key:A');
assert.ok(payload(false).buttons.includes('A')); // another pointer still holds A
pad.clear();
pad.stick(8, 'move', 10, 10);
assert.ok(Math.abs(payload(false).moveX - .71) < .01);
assert.ok(Math.abs(payload(false).moveY - .71) < .01);
console.log('pointer controls: independent sticks, button masks, shared presets, basic-model limits, coexistence and releases PASS');
const touch = new context.TouchPad();
const rect = {left:100,top:200,width:640,height:480};
assert.equal(context.point(rect,100,200).x,0);
assert.equal(context.point(rect,740,680).x,319);
assert.equal(context.point(rect,740,680).y,239);
assert.equal(context.point(rect,420,440).x,160);
assert.equal(context.point(rect,420,440).y,120);
const press = touch.begin(11,{x:20,y:30},0);
assert.equal(touch.begin(12,{x:50,y:50},0),null);
touch.move(11,{x:120,y:130}); assert.equal(touch.sample().x,120);
touch.release({}); assert.equal(touch.sample().down,true);
touch.release(press); assert.equal(touch.sample().down,false);
console.log('bottom touch: native LCD pixel mapping, edge clamps, drag, one finger and release ownership PASS');

const viewerSource = fs.readFileSync(`${__dirname}/viewer.js`, 'utf8');
const chordSource = viewerSource.slice(viewerSource.indexOf('function chordAllowed('),
  viewerSource.indexOf('async function pumpTouch('));
const chordElements = ['SELECT', 'X', 'ZL'].map(name => ({dataset:{button:name}, selected:false,
  classList:{toggle(_, value) { this.selected = value; }, remove() { this.selected = false; }}}));
const chordStatus = {textContent:''};
const sent = [];
const padSent = [];
let chordPreset = {capabilities:['input']};
const chordContext = vm.createContext({Set, performance:{now:()=>100},
  chordButtons:new Set(), chordUntil:0, deviceModel:'dual-stick', connected:true,
  sessionOn:true, linkEnabled:true, activePreset:()=>chordPreset, buttonMap:mapping,
  document:{querySelectorAll:()=>chordElements}, $:()=>chordStatus,
  feedback:()=>{}, post:(path, payload)=>sent.push({path,payload}),
  request:async (path,payload)=>{padSent.push({path,payload});return {ok:true};}});
vm.runInContext(chordSource, chordContext);
chordContext.stageChord('SELECT');
chordContext.stageChord('X');
assert.equal(chordStatus.textContent, 'Queued together: SELECT + X. Release Ctrl to press.');
chordContext.releaseChord();
assert.equal(JSON.stringify(sent), JSON.stringify([{path:'/api/input', payload:{buttons:'SELECT,X', frames:2}}]));
assert.equal(chordContext.chordButtons.size, 0);
chordContext.deviceModel = 'basic';
chordContext.stageChord('ZL');
chordContext.releaseChord();
assert.equal(sent.length, 1);
chordPreset = {capabilities:['input','controller'],inputButtons:['X'],controllerBasicFull:true};
chordContext.deviceModel = 'dual-stick';
chordContext.stageChord('SELECT');
chordContext.stageChord('X');
chordContext.releaseChord();
assert.equal(padSent.length, 1);
assert.equal(padSent[0].path, '/api/gamepad');
assert.equal(padSent[0].payload.padMask, (1 << 8) | (1 << 2));
console.log('button chords: one simultaneous bounded press and basic-model limits PASS');
